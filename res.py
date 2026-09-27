"""res: resource leases for the Claude sessions on the house machines (LeaseResources).

Every resource is a dimension (cpu, ram, each disk, each GPU and its memory, each network interface; see probes.py).
A command that finishes within a 10-second trial needs nothing more. Anything longer asks this machine's dispatcher
(or another machine's, with --on) for a lease: the trial's profile plus a margin, for a stated duration. The dispatcher
grants it when, on every dimension, the unleased use measured now plus the leases already granted plus the request
stays under capacity times the headroom; otherwise it queues it and mails the owner desk when it is granted. A leased
job runs detached from every Claude process and the dispatcher mails the owner desk (ClaudeMail) when it ends, stalls,
runs past its duration or uses more than it declared. It never kills a leased job. Design: README.md beside this file,
and the design sketch for decision D-260927-4.

Commands for sessions (python3 on the Mac, python on Windows; any interpreter works, res.py moves itself into its venv):
    now                                   every dimension: capacity, use, leased, unleased, free
    mine                                  what this Claude session's process tree uses right now
    trial -- COMMAND...                   run under the meter, kill the whole process tree at 10 s, print the profile
    request (--profile TRIAL | --need DIM=AMOUNT,...) --for DURATION [--on MACHINE] [--why TEXT] [--desk M:D]
    run --lease ID [--on MACHINE] [--wait] [--cwd DIR] -- PROGRAM...   start a leased job, detached, with a log
    extend ID [--for DURATION] [--profile TRIAL | --need ...] [--on MACHINE]
    release ID [--kill] [--on MACHINE]
    leases [--on MACHINE | --all]         the leases on a machine
    status [ID] [--on MACHINE]            one lease with its log tail, or the dispatcher and compliance summary
    compliance [--days N] [--on MACHINE]  trials, requests, runs, and long unleased commands of Claude sessions
Service: watch (the keeper the LaunchAgent / scheduled task runs), dispatch, _runner (internal).

On disk (synced by Resilio), under this folder's leases/<Machine>/:
    <id>.json          a lease: written once by the requester (state 'requested'), from then on only by that machine's
                       dispatcher (granted, queued, refused, starting, running, finished, failed, lost, expired,
                       released, killed)
    <id>.order.json    the run order (res run); <id>.run.json the runner's record (pid, start, end, exit code)
    <id>.log           the job's output; <id>.extend.json / <id>.release.json asks, consumed by the dispatcher
    trials/<id>.json   trial profiles (and their output in trials/<id>.out)
    dispatcher.json    the dispatcher's report; history.json durations per command; unleased.jsonl long commands
                       of Claude sessions that ran without a lease (the compliance record, D-260927-4 option b)
Locally, never synced: ~/.claude/leaseresources/ (the dispatcher's heartbeat, disk map, learned capacities).

Requires psutil in ~/venvs/LeaseResources. No regular expressions (house rule).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True                          # no __pycache__ in the synced folder

VENV_DIR = Path.home() / "venvs" / "LeaseResources"


def venv_python(windowless: bool = False) -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / ("pythonw.exe" if windowless else "python.exe")
    return VENV_DIR / "bin" / "python"


def ensure_venv() -> None:
    """Run inside ~/venvs/LeaseResources (house convention: never system Python); re-launch there if needed."""
    try:
        import psutil  # noqa: F401
        return
    except ImportError:
        pass
    target = venv_python()
    if not target.exists():
        if sys.platform == "win32":
            how = "python -m venv %USERPROFILE%\\venvs\\LeaseResources && %USERPROFILE%\\venvs\\LeaseResources\\Scripts\\pip install psutil"
        else:
            how = "/opt/homebrew/bin/python3 -m venv ~/venvs/LeaseResources && ~/venvs/LeaseResources/bin/pip install psutil"
        raise SystemExit("LeaseResources needs its venv at " + str(VENV_DIR) + " with psutil, and it is missing.\nCreate it with:\n  " + how)
    if os.environ.get("LEASERES_REEXEC") == "1" or Path(sys.executable) == target:
        raise SystemExit("psutil is missing from " + str(VENV_DIR) + ". Install it with: " + str(target) + " -m pip install psutil")
    os.environ["LEASERES_REEXEC"] = "1"
    argv = [str(target), "-B", str(Path(__file__).resolve())] + sys.argv[1:]
    if sys.platform == "win32":
        import subprocess
        sys.exit(subprocess.call(argv))
    os.execv(str(target), argv)


if __name__ == "__main__":
    ensure_venv()

import argparse          # noqa: E402
import collections       # noqa: E402
import datetime          # noqa: E402
import json              # noqa: E402
import shutil            # noqa: E402
import signal            # noqa: E402
import socket            # noqa: E402
import statistics        # noqa: E402
import subprocess        # noqa: E402
import time              # noqa: E402
import base64            # noqa: E402

import psutil            # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probes            # noqa: E402
from probes import kind_of, read_json, write_json, UNITS   # noqa: E402

SERVICE_DIR = Path(__file__).resolve().parent
SCRIPT = SERVICE_DIR / "res.py"
CODE_FILES = [SERVICE_DIR / "res.py", SERVICE_DIR / "probes.py"]
SYNC_ROOT = SERVICE_DIR.parent.parent
DATA_DIR = Path(os.environ.get("LEASERES_DATA") or SERVICE_DIR)
LEASES_ROOT = DATA_DIR / "leases"
LOG_DIR = DATA_DIR / "logs"
CONFIG_DIR = Path(os.environ.get("LEASERES_CONFIG") or (SERVICE_DIR / "config"))
LOCAL_DIR = Path(os.environ.get("LEASERES_LOCAL") or (Path.home() / ".claude" / "leaseresources"))
MAIL_PY = SYNC_ROOT / "Services" / "ClaudeMail" / "mail.py"
TASK_NAME = "LeaseResources Dispatcher"
LAUNCHD_LABEL = "com.goocy.leaseresources"
EXIT_CODE_CHANGED = 3
EXIT_ALREADY_RUNNING = 4
ACTIVE = ["granted", "starting", "running"]            # states whose need counts against capacity
OPEN = ["requested", "queued"] + ACTIVE
ENDED = ["finished", "failed", "lost", "expired", "released", "killed", "refused"]
SUFFIXES = [".order.json", ".run.json", ".extend.json", ".release.json"]


# ---------------------------------------------------------------- config, machine, time

def load_config() -> dict:
    """config/defaults.json overlaid with config/<machine>.json (lower case); each machine edits only its own file."""
    with open(CONFIG_DIR / "defaults.json", encoding="utf-8") as fh:
        config = json.load(fh)
    own = CONFIG_DIR / (this_machine(config).casefold() + ".json")
    if own.is_file():
        with open(own, encoding="utf-8") as fh:
            config.update(json.load(fh))
    return config


def canonical_machine(name: str, config: dict) -> str:
    for machine in config["machines"]:
        if machine.casefold() == name.strip().casefold():
            return machine
    raise SystemExit("Unknown machine '" + name + "'. Known: " + ", ".join(config["machines"]))


def this_machine(config: dict) -> str:
    override = os.environ.get("LEASERES_MACHINE", "").strip()
    if override:
        return canonical_machine(override, config)
    host = socket.gethostname().partition(".")[0].casefold()
    for alias, machine in config.get("hostnames", {}).items():
        if alias.partition(".")[0].casefold() == host:
            return canonical_machine(machine, config)
    return canonical_machine(host, config)


def num(config: dict, key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(value):
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def age_of(value) -> float:
    stamp = parse_iso(value)
    return float("inf") if stamp is None else time.time() - stamp


def machine_dir(machine: str) -> Path:
    return LEASES_ROOT / machine


def lease_path(machine: str, ident: str) -> Path:
    return machine_dir(machine) / (ident + ".json")


def side_path(machine: str, ident: str, suffix: str) -> Path:
    return machine_dir(machine) / (ident + suffix)


def is_lease_file(path: Path) -> bool:
    name = path.name
    return name.endswith(".json") and not any(name.endswith(s) for s in SUFFIXES) and name not in \
        ("dispatcher.json", "history.json") and not name.startswith(".")


def all_leases(machine: str) -> list:
    folder = machine_dir(machine)
    if not folder.is_dir():
        return []
    found = []
    for path in folder.iterdir():
        if path.is_file() and is_lease_file(path):
            data = read_json(path)
            if data.get("id"):
                found.append(data)
    return sorted(found, key=lambda d: (d.get("requested", ""), d.get("id", "")))


def log(machine: str, message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now()
        path = LOG_DIR / ("leaseresources." + machine.casefold() + "." + now.strftime("%y%m%d") + ".log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(now.strftime("%Y-%m-%d %H:%M:%S") + " " + message + "\n")
    except OSError:
        pass


def new_id(prefix: str, machine: str, folder: Path) -> str:
    stamp = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
    base = prefix + stamp + "-" + machine
    ident, n = base, 1
    while (folder / (ident + ".json")).exists():
        n += 1
        ident = base + "-" + str(n)
    return ident


# ---------------------------------------------------------------- amounts and durations

SIZE_UNITS = {"k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12, "ki": 1024.0, "mi": 1024.0 ** 2, "gi": 1024.0 ** 3, "ti": 1024.0 ** 4}
TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> float:
    """'6h', '90m', '1h30m', '2d', '45s', or plain seconds."""
    text = text.strip().casefold()
    if not text:
        raise ValueError("empty duration")
    if text.replace(".", "", 1).isdigit():
        return float(text)
    total, digits = 0.0, ""
    for ch in text:
        if ch.isdigit() or ch == ".":
            digits += ch
        elif ch in TIME_UNITS and digits:
            total += float(digits) * TIME_UNITS[ch]
            digits = ""
        elif ch == " ":
            continue
        else:
            raise ValueError("cannot read duration '" + text + "' (use e.g. 45s, 90m, 6h, 1h30m, 2d)")
    if digits:
        raise ValueError("duration '" + text + "' ends without a unit")
    return total


def parse_amount(text: str) -> float:
    """'2', '0.5', '4G', '500M', '6GiB', '80%', '100M/s' -> a float in the dimension's unit."""
    text = text.strip()
    for tail in ["/s", "B", "b", "%"]:
        if text.endswith(tail):
            text = text[: -len(tail)]
    lowered = text.casefold()
    for unit in sorted(SIZE_UNITS, key=len, reverse=True):
        if lowered.endswith(unit) and lowered[: -len(unit)].replace(".", "", 1).isdigit():
            return float(lowered[: -len(unit)]) * SIZE_UNITS[unit]
    return float(lowered)


def human(value: float, kind: str) -> str:
    unit = UNITS.get(kind, "")
    if unit == "cores":
        return format(value, ".2f") + " cores"
    if unit == "%":
        return format(value, ".0f") + " %"
    if unit in ("B", "B/s"):
        v = float(value)
        for suffix in ["B", "KB", "MB", "GB", "TB"]:
            if abs(v) < 1000 or suffix == "TB":
                text = (format(v, ".0f") if suffix == "B" else format(v, ".1f")) + " " + suffix
                return text + ("/s" if unit == "B/s" else "")
            v /= 1000.0
    return format(value, ".2f")


def human_duration(seconds) -> str:
    if seconds is None:
        return "?"
    seconds = float(seconds)
    if seconds < 90:
        return format(seconds, ".0f") + " s"
    if seconds < 5400:
        return format(seconds / 60, ".0f") + " min"
    if seconds < 172800:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return str(hours) + " h" + (" " + str(minutes) + " min" if minutes else "")
    return format(seconds / 86400, ".1f") + " d"


def describe_need(need: dict) -> str:
    return ", ".join(d + " " + human(v, kind_of(d)) for d, v in sorted(need.items()))


# ---------------------------------------------------------------- processes

def process(pid, create_time=None):
    try:
        proc = psutil.Process(int(pid))
        if create_time is not None and abs(proc.create_time() - float(create_time)) > 1.0:
            return None
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return proc
    except (psutil.Error, ValueError, TypeError):
        return None


def alive(proc) -> bool:
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def tree_of(proc) -> list:
    if proc is None:
        return []
    try:
        return [proc] + proc.children(recursive=True)
    except psutil.Error:
        return [proc]


def kill_tree(procs: list, group_leader=None) -> list:
    """SIGKILL (TerminateProcess on Windows) every process given, and the process group / tree of the leader. Returns
    the pids still alive afterwards."""
    if group_leader is not None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(group_leader)], capture_output=True,
                           creationflags=probes.no_window())
        else:
            try:
                os.killpg(group_leader, signal.SIGKILL)
            except OSError:
                pass
    targets = []
    for proc in procs:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            pass
        targets.append(proc)
    gone, still = psutil.wait_procs(targets, timeout=3)
    return [p.pid for p in still if alive(p)]


def detach_flags() -> dict:
    """Popen arguments that put a child outside the caller's process group, session and (Windows) job, so an interrupt
    of the Claude turn, the session closing or an app restart does not reach it."""
    if sys.platform == "win32":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                 | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x1000000))
        return {"creationflags": flags}
    return {"start_new_session": True}


def popen_detached(argv, **kwargs):
    try:
        return subprocess.Popen(argv, **dict(kwargs, **detach_flags()))
    except OSError:
        if sys.platform != "win32":
            raise
        # The job Claude runs in may forbid breaking away; then the child stays in it (untested, see README).
        flags = detach_flags()["creationflags"] & ~getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x1000000)
        return subprocess.Popen(argv, **dict(kwargs, creationflags=flags))


def claude_ancestor(config: dict):
    names = [n.casefold() for n in config.get("claude_process_names", ["claude", "claude.exe"])]
    try:
        for parent in psutil.Process().parents():
            try:
                if parent.name().casefold() in names:
                    return parent
            except psutil.Error:
                continue
    except psutil.Error:
        pass
    return None


def short_cmd(proc, limit: int = 90) -> str:
    try:
        argv = proc.cmdline()
        if argv:
            argv = [Path(argv[0].replace(chr(92), "/")).name] + argv[1:]
        text = " ".join(argv) or proc.name()
    except psutil.Error:
        return "?"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def tail(path: Path, lines: int) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


# ---------------------------------------------------------------- paths, programs, safety

def translate_path(text: str) -> str:
    """A path from another machine: anything below a 'ResilioSync' folder is moved under this machine's copy."""
    if not text:
        return text
    parts = [p for p in str(text).replace(chr(92), "/").split("/") if p]
    for index, part in enumerate(parts):
        if part.casefold() == SYNC_ROOT.name.casefold():
            return str(SYNC_ROOT.joinpath(*parts[index + 1:]))
    return os.path.expanduser(text)


def allowed_roots(config: dict) -> list:
    roots = []
    for root in config.get("allowed_roots", []):
        try:
            roots.append(Path(os.path.expanduser(translate_path(root))).resolve())
        except OSError:
            continue
    return roots


def under(path: Path, root: Path) -> bool:
    a = [p.casefold() for p in path.parts] if sys.platform == "win32" else list(path.parts)
    b = [p.casefold() for p in root.parts] if sys.platform == "win32" else list(root.parts)
    return len(a) > len(b) and a[: len(b)] == b


def program_name(argv0: str) -> str:
    name = Path(argv0.replace(chr(92), "/")).name.casefold()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def check_program(argv: list, cwd: str, config: dict) -> tuple:
    """(ok, reason, script). A leased program must be an allowed interpreter (python, pythonw, py, powershell -File)
    running a script file under one of this machine's allowed roots; inline code (-c, -m, -Command) is refused."""
    if not argv:
        return False, "no program given", None
    name = program_name(argv[0])
    allowed = [p.casefold() for p in config.get("allowed_programs", [])]
    is_python = name in ("python", "pythonw", "py") or (name.startswith("python") and name[6:].replace(".", "").isdigit())
    if name not in allowed and not (is_python and "python" in allowed):
        return False, "program '" + argv[0] + "' is not one of the allowed interpreters (" + ", ".join(allowed) + ")", None
    script = None
    if name in ("powershell", "pwsh"):
        for index, arg in enumerate(argv[1:], start=1):
            low = arg.casefold()
            if low in ("-command", "-c", "-encodedcommand", "-enc", "-e", "-ec"):
                return False, "powershell inline commands are refused; use -File <script>", None
            if low in ("-file", "-f") and index + 1 < len(argv):
                script = argv[index + 1]
                break
        if script is None:
            return False, "powershell needs -File <script>", None
    else:
        skip_next = False
        for arg in argv[1:]:
            if skip_next:
                skip_next = False
                continue
            if arg in ("-c", "-m") or (arg.startswith("-c") and len(arg) > 2 and not arg.startswith("--")):
                return False, "inline python (" + arg + ") is refused; give a script file", None
            if arg in ("-X", "-W", "-Q"):
                skip_next = True
                continue
            if arg.startswith("-"):
                continue
            script = arg
            break
        if script is None:
            return False, "no script file given to " + argv[0], None
    path = Path(os.path.expanduser(translate_path(script)))
    if not path.is_absolute():
        path = Path(cwd or os.getcwd()) / path
    try:
        resolved = path.resolve()
    except OSError:
        return False, "cannot resolve script " + str(path), None
    if not resolved.is_file():
        return False, "script not found: " + str(resolved), None
    roots = allowed_roots(config)
    if not any(under(resolved, root) for root in roots):
        return False, ("script " + str(resolved) + " is outside the allowed roots on this machine ("
                       + ", ".join(str(r) for r in roots) + ")"), str(resolved)
    return True, "", str(resolved)


def command_key(argv: list) -> str:
    """What makes two runs 'the same command' for the duration history: interpreter name without version, then args."""
    if not argv:
        return ""
    name = program_name(argv[0])
    if name.startswith("python"):
        name = "python"
    return " ".join([name] + [str(a) for a in argv[1:]])


def resolve_interpreter(argv: list) -> list:
    """Give a bare interpreter name an absolute path. Under launchd PATH lacks Homebrew, so look there too."""
    if not argv or os.path.isabs(argv[0]):
        return argv
    extra = ["/opt/homebrew/bin", "/usr/local/bin"] if sys.platform == "darwin" else []
    path = os.pathsep.join([os.environ.get("PATH", "")] + extra)
    found = shutil.which(argv[0], path=path)
    return [found] + list(argv[1:]) if found else argv


# ---------------------------------------------------------------- mail and notifications

def mail_script(config: dict) -> str:
    return os.path.expanduser(config.get("mail_script") or str(MAIL_PY))


def mail_env() -> dict:
    """Mail from a service: no session id, so ClaudeMail takes the desk of this folder as the sender."""
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    return env


def owner_desk(config: dict, given) -> str:
    """The desk that gets this lease's mail: --desk, else the calling session's desk from `mail.py whoami`."""
    if given:
        return given.strip()
    try:
        out = subprocess.run([sys.executable, "-B", mail_script(config), "whoami"], capture_output=True, text=True,
                             timeout=30, creationflags=probes.no_window()).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] == "desk":
            return words[1]
    return this_machine(config) + ":General"


def send_mail(config: dict, machine: str, desk: str, subject: str, body: str) -> bool:
    outbox = LOCAL_DIR / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / (datetime.datetime.now().strftime("%y%m%d-%H%M%S-%f") + ".md")
    path.write_text(body, encoding="utf-8")
    ok = False
    for target in [desk, desk.partition(":")[0]]:
        try:
            run = subprocess.run([sys.executable, "-B", mail_script(config), "send", "--to", target, "--subject", subject,
                                  "--body-file", str(path), "--to-own-desk"], capture_output=True, text=True, timeout=60,
                                 cwd=str(SERVICE_DIR), env=mail_env(), creationflags=probes.no_window())
            ok = run.returncode == 0
            detail = " ".join((run.stdout + run.stderr).split())[:300]
        except (OSError, subprocess.SubprocessError) as exc:
            detail = str(exc)
        log(machine, ("mailed " if ok else "WARN mail failed ") + target + ": " + subject + " (" + detail + ")")
        if ok:
            break
    if not ok:
        notify(config, machine, "LeaseResources: " + subject, "Mail to " + desk + " failed; see the log.")
    try:
        path.unlink()
    except OSError:
        pass
    return ok


WINDOWS_TOAST = (
    "$ErrorActionPreference = 'Stop'\n"
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
    "$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)\n"
    "$texts = $xml.GetElementsByTagName('text')\n"
    "$texts.Item(0).AppendChild($xml.CreateTextNode($env:LEASERES_TITLE)) | Out-Null\n"
    "$texts.Item(1).AppendChild($xml.CreateTextNode($env:LEASERES_BODY)) | Out-Null\n"
    "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}' + [char]92 + 'WindowsPowerShell' + [char]92 + 'v1.0' + [char]92 + 'powershell.exe'\n"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))\n"
)
MAC_NOTIFY = ["on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run"]


def notify(config: dict, machine: str, title: str, body: str) -> None:
    log(machine, "notified: " + title + " - " + " ".join(body.split()))
    if not config.get("notify", True):
        return
    try:
        if sys.platform == "win32":
            encoded = base64.b64encode(WINDOWS_TOAST.encode("utf-16-le")).decode("ascii")
            env = dict(os.environ, LEASERES_TITLE=title, LEASERES_BODY=body)
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], env=env,
                           capture_output=True, timeout=30, creationflags=probes.no_window())
        elif sys.platform == "darwin":
            cmd = ["osascript"]
            for line in MAC_NOTIFY:
                cmd += ["-e", line]
            subprocess.run(cmd + [title, body], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        log(machine, "WARN notification failed: " + str(exc))


def res_command() -> str:
    python = "python3" if sys.platform == "darwin" else "python"
    return python + ' "' + str(SCRIPT) + '"'


# ---------------------------------------------------------------- now, mine

def lease_trees(machine: str) -> dict:
    trees = {}
    for lease in all_leases(machine):
        if lease.get("state") != "running":
            continue
        run = read_json(side_path(machine, lease["id"], ".run.json"))
        proc = process(run.get("runner_pid"), run.get("runner_create_time"))
        if proc is not None:
            trees[lease["id"]] = tree_of(proc)
    return trees


def headroom(config: dict, dim: str) -> float:
    per = config.get("headroom_by_dimension", {})
    for key in [dim, kind_of(dim)]:
        if key in per:
            return float(per[key])
    return num(config, "headroom", 0.85)


def dispatcher_state(machine: str, config: dict) -> tuple:
    report = read_json(machine_dir(machine) / "dispatcher.json")
    fresh = age_of(report.get("updated")) < 180
    return fresh, report


def cmd_now(args, config: dict) -> None:
    machine = this_machine(config)
    meter = probes.Meter(config, LOCAL_DIR)
    leases = all_leases(machine)
    trees = lease_trees(machine)
    meter.sample(trees)
    time.sleep(1.0)
    system, per = meter.sample(trees)
    dims = meter.dimensions()
    system_only = meter.system_only()
    planned = collections.defaultdict(float)
    running_need = collections.defaultdict(float)
    for lease in leases:
        if lease.get("state") in ACTIVE:
            for d, v in (lease.get("need") or {}).items():
                planned[d] += float(v)
                if lease.get("state") == "running":
                    running_need[d] += float(v)
    fresh, report = dispatcher_state(machine, config)
    if args.json:
        print(json.dumps({"machine": machine, "dimensions": dims, "use": system, "planned": planned,
                          "dispatcher_fresh": fresh}, indent=1))
        return
    print(machine + ": dispatcher " + ("up (pid " + str(report.get("pid")) + ")" if fresh else "NOT RUNNING")
          + ", headroom " + str(num(config, "headroom", 0.85)) + ", " + str(len([l for l in leases if l.get("state") in ACTIVE]))
          + " active and " + str(len([l for l in leases if l.get("state") == "queued"])) + " queued leases")
    rows = [["dimension", "capacity", "use now", "leased (planned)", "unleased", "free", "label"]]
    busy = meter.disk_busy()
    for dim in dims:
        kind = kind_of(dim)
        cap = dims[dim]["capacity"]
        use = system.get(dim, 0.0)
        if dim in system_only:
            measured = min(use, running_need.get(dim, 0.0))
        else:
            measured = sum(t.get(dim, 0.0) for t in per.values())
        unleased = max(0.0, use - measured)
        free = cap * headroom(config, dim) - unleased - planned.get(dim, 0.0)
        label = dims[dim].get("label", "")
        if dim in busy:
            label += "; busy " + format(busy[dim] * 100, ".0f") + " %"
        rows.append([dim, human(cap, kind), human(use, kind), human(planned.get(dim, 0.0), kind), human(unleased, kind),
                     human(max(0.0, free), kind), label])
    print_table(rows)


def print_table(rows: list) -> None:
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip())


def cmd_mine(args, config: dict) -> None:
    root = claude_ancestor(config)
    if root is None:
        print("Not inside a Claude session (no claude process among this command's parents); showing this shell's tree.")
        try:
            root = psutil.Process().parent()
        except psutil.Error:
            root = psutil.Process()
    me = os.getpid()
    procs = [p for p in tree_of(root) if p.pid != me]
    meter = probes.Meter(config, LOCAL_DIR)

    def counters():
        """Per process: (monotonic time, cpu seconds, disk bytes). Rates come from two of these a second apart, as in
        probes.Meter; psutil's cpu_percent() would return its 0.0 baseline on these freshly made Process objects."""
        found = {}
        for proc in procs:
            try:
                with proc.oneshot():
                    times = proc.cpu_times()
                    found[proc.pid] = (time.monotonic(), times.user + times.system, probes.disk_bytes(proc))
            except (psutil.Error, OSError):
                continue
        return found

    before = counters()
    meter.sample({"mine": procs})
    time.sleep(1.0)
    system, per = meter.sample({"mine": procs})
    after = counters()
    use = per.get("mine", {})
    print("Claude session process " + str(root.pid) + ", " + str(len(procs)) + " processes in its tree:")
    for dim in sorted(use):
        if use[dim] > 0:
            print("  " + dim.ljust(14) + human(use[dim], kind_of(dim)) + "   (machine: " + human(system.get(dim, 0.0), kind_of(dim)) + ")")
    for dim in sorted(meter.system_only()):
        print("  " + dim.ljust(14) + "not measurable per process (machine: " + human(system.get(dim, 0.0), kind_of(dim)) + ")")
    rows = [["pid", "age", "cpu (cores)", "ram", "disk", "command"]]
    now = time.time()
    for proc in procs:
        a, b = before.get(proc.pid), after.get(proc.pid)
        cpu = disk = "?"
        if a and b and b[0] > a[0]:
            cpu = format(max(0.0, (b[1] - a[1]) / (b[0] - a[0])), ".2f")
            if a[2] is not None and b[2] is not None:
                disk = human(max(0.0, (b[2] - a[2]) / (b[0] - a[0])), "disk")
        try:
            with proc.oneshot():
                rows.append([str(proc.pid), human_duration(now - proc.create_time()), cpu,
                             human(proc.memory_info().rss, "ram"), disk, short_cmd(proc)])
        except psutil.Error:
            continue
    print()
    print_table(rows)


# ---------------------------------------------------------------- trial

def trial_dir(machine: str) -> Path:
    return machine_dir(machine) / "trials"


def session_tree_pids(proc) -> list:
    """POSIX: every process in the session the trial command leads (catches children that left the parent chain)."""
    if sys.platform == "win32":
        return []
    found = []
    for p in psutil.process_iter(["pid"]):
        try:
            if os.getsid(p.pid) == proc.pid:
                found.append(p)
        except OSError:
            continue
    return found


def cmd_trial(args, config: dict) -> None:
    argv = list(args.command or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise SystemExit("Give the command after --, e.g. res trial -- python job.py")
    machine = this_machine(config)
    limit = num(config, "trial_seconds", 10)
    step = num(config, "trial_sample_seconds", 0.25)
    folder = trial_dir(machine)
    folder.mkdir(parents=True, exist_ok=True)
    ident = new_id("T", machine, folder)
    out_path = folder / (ident + ".out")
    meter = probes.Meter(config, LOCAL_DIR)
    system_only = meter.system_only()
    time.sleep(0.3)
    baseline, _ = meter.sample({})
    started_wall = now_iso()
    with open(out_path, "wb") as out:
        kwargs = {"stdout": out, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": os.getcwd(),
                  "env": dict(os.environ, PYTHONUNBUFFERED="1")}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        else:
            kwargs["start_new_session"] = True
        try:
            child = subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            raise SystemExit("Cannot start " + argv[0] + ": " + str(exc))
    t0 = time.monotonic()
    root = process(child.pid)
    seen = {}
    peaks = collections.defaultdict(float)
    sums = collections.defaultdict(float)
    samples = 0
    last_session_scan = 0.0
    exit_code = None
    while True:
        elapsed = time.monotonic() - t0
        if root is not None:
            for p in tree_of(root):
                try:
                    seen.setdefault((p.pid, p.create_time()), p)
                except psutil.Error:
                    pass
        if time.monotonic() - last_session_scan > 1.0:
            for p in session_tree_pids(child):
                try:
                    seen.setdefault((p.pid, p.create_time()), p)
                except psutil.Error:
                    pass
            last_session_scan = time.monotonic()
        live = [p for p in seen.values() if alive(p)]
        system, per = meter.sample({"trial": live})
        use = per.get("trial", {})
        for dim in system_only:
            use[dim] = max(0.0, system.get(dim, 0.0) - baseline.get(dim, 0.0))
        for dim, value in use.items():
            peaks[dim] = max(peaks[dim], value)
            sums[dim] += value
        samples += 1
        if exit_code is None:
            exit_code = child.poll()
        if exit_code is not None:
            leftovers = [p for p in seen.values() if p.pid != child.pid and alive(p)]
            if not leftovers:
                break
        if elapsed >= limit:
            break
        time.sleep(step)
    elapsed = time.monotonic() - t0
    finished = exit_code is not None
    survivors = [p for p in seen.values() if alive(p)]
    killed = [p.pid for p in survivors]
    still = kill_tree(survivors, group_leader=child.pid) if survivors else []
    if exit_code is None:
        try:
            exit_code = child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    profile = {d: {"peak": peaks[d], "mean": sums[d] / max(1, samples)} for d in peaks}
    record = {"id": ident, "machine": machine, "command": argv, "command_key": command_key(argv), "cwd": os.getcwd(),
              "started": started_wall, "seconds": round(elapsed, 2), "finished": finished,
              "exit": exit_code if finished else None, "killed": killed, "survivors": still,
              "profile": profile, "session": os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "output": str(out_path),
              "attribution": {d: "machine delta over baseline" for d in system_only}}
    write_json(folder / (ident + ".json"), record)
    if args.json:
        print(json.dumps(record, indent=1))
        return
    if finished:
        print("Finished in " + format(elapsed, ".1f") + " s with exit code " + str(exit_code) + ": no lease needed."
              + (" Killed " + str(len(killed)) + " background processes it left behind." if killed else ""))
    else:
        print("Did not finish within " + format(limit, ".0f") + " s: killed its whole process tree after "
              + format(elapsed, ".1f") + " s (" + str(len(killed)) + " processes" + (", STILL ALIVE: " + str(still) if still else "")
              + ").")
    out_tail = tail(out_path, 15)
    if out_tail:
        print("Output tail (" + str(out_path) + "):")
        for line in out_tail.splitlines():
            print("  | " + line)
    print("Profile " + ident + " (peak / mean):")
    dims = meter.dimensions()
    for dim in sorted(profile, key=lambda d: (list(dims).index(d) if d in dims else 99, d)):
        mark = "  (machine delta, not per process)" if dim in system_only else ""
        print("  " + dim.ljust(14) + human(profile[dim]["peak"], kind_of(dim)) + " / " + human(profile[dim]["mean"], kind_of(dim)) + mark)
    if not finished:
        print("For the full run: " + res_command() + " request --profile " + ident + " --for <duration> --why \"...\"")


# ---------------------------------------------------------------- request, extend, release

def need_from_profile(profile: dict, config: dict, margin: float) -> dict:
    floors = config.get("request_floor", {})
    need = {}
    for dim, values in profile.items():
        kind = kind_of(dim)
        peak = float(values.get("peak", 0.0)) if isinstance(values, dict) else float(values)
        floor = float(floors.get(kind, 0.0))
        if kind in ("cpu", "ram") or peak >= 0.5 * floor:
            need[dim] = max(peak * (1.0 + margin), floor)
    return need


def parse_need(text: str) -> dict:
    need = {}
    for part in text.split(","):
        if not part.strip():
            continue
        name, eq, amount = part.partition("=")
        if not eq:
            raise SystemExit("--need takes DIM=AMOUNT pairs, e.g. cpu=2,ram=4G,gpu:0=60,vram:0=6G,disk:G=80M")
        try:
            need[name.strip()] = parse_amount(amount)
        except ValueError:
            raise SystemExit("Cannot read the amount '" + amount + "' for " + name.strip())
    return need


def find_trial(ident: str):
    for folder in sorted(LEASES_ROOT.glob("*/trials")):
        path = folder / (ident + ".json")
        if path.is_file():
            return read_json(path)
    return None


def wait_for_answer(path: Path, keys: tuple, seconds: float) -> dict:
    deadline = time.monotonic() + seconds
    data = read_json(path)
    while time.monotonic() < deadline:
        data = read_json(path)
        if keys[0](data):
            return data
        time.sleep(0.2)
    return data


def cmd_request(args, config: dict) -> None:
    if args.extend:
        args.id = args.extend
        return cmd_extend(args, config)
    here = this_machine(config)
    target = canonical_machine(args.on, config) if args.on else here
    if not args.duration:
        raise SystemExit("Give --for DURATION (e.g. 90m, 6h, 2d).")
    duration = parse_duration(args.duration)
    margin = num(config, "margin", 0.25) if args.margin is None else float(args.margin)
    command = []
    need = {}
    trial = None
    if args.profile:
        trial = find_trial(args.profile)
        if trial is None:
            raise SystemExit("No trial " + args.profile + " (res trial prints its id).")
        need.update(need_from_profile(trial.get("profile", {}), config, margin))
        command = trial.get("command", [])
    if args.need:
        need.update(parse_need(args.need))
    if not need:
        raise SystemExit("Give --profile TRIAL_ID or --need DIM=AMOUNT,...")
    folder = machine_dir(target)
    folder.mkdir(parents=True, exist_ok=True)
    ident = new_id("L", here, folder)
    lease = {"id": ident, "machine": target, "from_machine": here, "desk": owner_desk(config, args.desk),
             "session": os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "why": args.why or "", "need": need,
             "duration": duration, "state": "requested", "requested": now_iso(), "trial": args.profile or "",
             "command": command, "command_key": command_key(command) if command else "", "margin": margin}
    path = lease_path(target, ident)
    write_json(path, lease)
    fresh, report = dispatcher_state(target, config)
    if target != here:
        print("Sent request " + ident + " to " + target + " (" + describe_need(need) + " for " + human_duration(duration)
              + "). Its dispatcher answers by mail to " + lease["desk"] + " once Resilio has carried the file over;")
        print("  " + str(path))
        if not fresh:
            print("  Warning: " + target + "'s dispatcher last reported " + str(report.get("updated") or "never")
                  + "; nothing happens until it runs.")
        print("  To start the job on grant, add now: " + res_command() + " run --lease " + ident + " --on " + target + " -- <program>")
        return
    if not fresh:
        print("Request " + ident + " written, but the dispatcher on " + here + " is not running (last report "
              + str(report.get("updated") or "never") + "). It is answered when the dispatcher starts: install_launchd.sh"
              + " or install_task.ps1.\n  " + str(path))
        return
    answer = wait_for_answer(path, (lambda d: d.get("state") != "requested",), num(config, "answer_wait_seconds", 6))
    print_answer(answer, path)


def print_answer(lease: dict, path: Path) -> None:
    state = lease.get("state")
    ident = lease.get("id")
    if state == "requested":
        print("No answer yet from the dispatcher; " + res_command() + " status " + ident + " shows it later.\n  " + str(path))
        return
    print(state + " " + ident + " on " + lease.get("machine", "") + ": " + lease.get("answer", ""))
    if lease.get("history_note"):
        print("  " + lease["history_note"])
    if state == "granted":
        print("  Start it: " + res_command() + " run --lease " + ident + " -- <program>   (within "
              + str(int(lease.get("start_within_minutes", 60))) + " min)")
    elif state == "queued":
        print("  ClaudeMail goes to " + lease.get("desk", "") + " when it is granted. To start the job then without a turn:"
              + " " + res_command() + " run --lease " + ident + " -- <program>")
    print("  " + str(path))


def cmd_extend(args, config: dict) -> None:
    here = this_machine(config)
    target = canonical_machine(args.on, config) if args.on else here
    path = lease_path(target, args.id)
    lease = read_json(path)
    if not lease:
        raise SystemExit("No lease " + args.id + " on " + target)
    ask = {"asked": now_iso(), "from_machine": here}
    if args.duration:
        ask["duration"] = parse_duration(args.duration)
    margin = float(lease.get("margin", num(config, "margin", 0.25)))
    need = {}
    if getattr(args, "profile", None):
        trial = find_trial(args.profile)
        if trial is None:
            raise SystemExit("No trial " + args.profile)
        need.update(need_from_profile(trial.get("profile", {}), config, margin))
    if getattr(args, "need", None):
        need.update(parse_need(args.need))
    if need:
        ask["need"] = need
    if len(ask) == 2:
        raise SystemExit("Give --for DURATION and/or --need / --profile.")
    write_json(side_path(target, args.id, ".extend.json"), ask)
    if target != here:
        print("Extension of " + args.id + " sent to " + target + "; the answer comes by mail to " + lease.get("desk", ""))
        return
    answer = wait_for_answer(path, (lambda d: (d.get("last_extend") or {}).get("asked") == ask["asked"],),
                             num(config, "answer_wait_seconds", 6))
    result = answer.get("last_extend") or {}
    if result.get("asked") != ask["asked"]:
        print("No answer yet; the dispatcher handles it within a few seconds when running.")
        return
    print(result.get("answer", ""))


def cmd_release(args, config: dict) -> None:
    here = this_machine(config)
    target = canonical_machine(args.on, config) if args.on else here
    path = lease_path(target, args.id)
    lease = read_json(path)
    if not lease:
        raise SystemExit("No lease " + args.id + " on " + target)
    if lease.get("state") in ENDED:
        print(args.id + " is already " + lease.get("state"))
        return
    if lease.get("state") in ("running", "starting") and not args.kill:
        raise SystemExit(args.id + " is running; the job keeps its lease until it ends. Add --kill to stop its whole process tree.")
    write_json(side_path(target, args.id, ".release.json"), {"asked": now_iso(), "kill": bool(args.kill), "from_machine": here})
    if target != here:
        print("Release of " + args.id + " sent to " + target)
        return
    answer = wait_for_answer(path, (lambda d: d.get("state") in ENDED,), num(config, "answer_wait_seconds", 6))
    print(args.id + ": " + str(answer.get("state")) + " " + str(answer.get("answer", "")))


# ---------------------------------------------------------------- run and the runner

def start_runner(machine: str, ident: str, program: list, cwd: str) -> subprocess.Popen:
    """Start the detached runner on exactly this program. What it runs travels in a launch file in the local folder,
    which is never synced, not in the synced order: nothing written into leases/ by another machine can change what a
    runner executes, and a check made before this call covers exactly what runs (no swap between check and start)."""
    launch = LOCAL_DIR / "launch" / (ident + ".json")
    write_json(launch, {"argv": list(program), "cwd": cwd, "machine": machine, "lease": ident, "written": now_iso()})
    python = venv_python(windowless=True)
    if not python.exists():
        python = Path(sys.executable)
    argv = [str(python), "-B", str(SCRIPT), "_runner", machine, ident, str(launch)]
    return popen_detached(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          cwd=str(SERVICE_DIR), env=dict(os.environ, LEASERES_MACHINE=machine))


def cmd_run(args, config: dict) -> None:
    argv = list(args.command or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise SystemExit("Give the program after --, e.g. res run --lease L... -- python job.py")
    here = this_machine(config)
    target = canonical_machine(args.on, config) if args.on else here
    path = lease_path(target, args.lease)
    lease = read_json(path)
    if not lease:
        raise SystemExit("No lease " + args.lease + " on " + target + " (" + str(path) + ")")
    state = lease.get("state")
    if state in ENDED:
        raise SystemExit(args.lease + " is " + state + "; request a new lease.")
    if side_path(target, args.lease, ".order.json").exists():
        raise SystemExit(args.lease + " already has a run order; one lease, one run.")
    cwd = os.path.abspath(args.cwd or os.getcwd())
    local_start = target == here and state == "granted"
    # A direct local run is started by this very process for the session that typed it, so it may run any program
    # (D-260927-7). Everything else is started later by a dispatcher from the synced folder and keeps the program rule;
    # a queued local run is checked now as well, so the refusal comes at once instead of by mail.
    if (not local_start or config.get("check_local_runs", False)) and target == here:
        ok, reason, script = check_program(argv, cwd, config)
        if not ok:
            log(here, "REFUSED run of " + args.lease + ": " + reason + " (" + " ".join(argv) + ")")
            raise SystemExit("Refused: " + reason + ". Only a run on a lease already granted on this machine may use any"
                             + " program; queued and cross-machine runs are started from the synced folder and must be a"
                             + " script under the allowed roots. Every refusal is logged.")
    order = {"lease": args.lease, "argv": resolve_interpreter(argv) if target == here else argv, "cwd": cwd,
             "from_machine": here, "ordered": now_iso(), "start": "res" if local_start else "dispatcher",
             "desk": lease.get("desk", "")}
    write_json(side_path(target, args.lease, ".order.json"), order)
    log(here, "run order " + args.lease + " on " + target + " (" + order["start"] + "): " + " ".join(argv))
    history = read_json(machine_dir(target) / "history.json").get(command_key(argv)) or []
    if history:
        print("History: " + history_note(history, float(lease.get("duration", 0))))
    if not local_start:
        where = "on " + target + " " if target != here else ""
        print("Run order written; the dispatcher " + where + "starts it when the lease is granted (now: " + str(state)
              + "), and mails " + lease.get("desk", "") + " when it starts and ends.\n  " + str(path))
        return
    log(here, "local run " + args.lease + " started by res itself (program rule not applied): " + " ".join(order["argv"]))
    start_runner(target, args.lease, order["argv"], cwd)
    run_path = side_path(target, args.lease, ".run.json")
    deadline = time.monotonic() + 10
    run = {}
    while time.monotonic() < deadline:
        run = read_json(run_path)
        if run.get("pid") or run.get("error"):
            break
        time.sleep(0.1)
    if run.get("error"):
        raise SystemExit("The runner could not start the program: " + run["error"])
    if not run.get("pid"):
        raise SystemExit("The runner did not report within 10 s; see " + str(run_path))
    log_path = side_path(target, args.lease, ".log")
    print("Started " + args.lease + " detached: pid " + str(run["pid"]) + ", log " + str(log_path))
    print("The dispatcher mails " + lease.get("desk", "") + " when it ends, stalls, overruns or runs past "
          + human_duration(lease.get("duration")) + ". No need to poll.")
    if args.wait:
        print("Waiting (interrupting this only stops the waiting, not the job)...")
        shown = 0
        while True:
            run = read_json(run_path)
            try:
                size = log_path.stat().st_size
                if size > shown:
                    with open(log_path, "rb") as fh:
                        fh.seek(shown)
                        sys.stdout.write(fh.read(size - shown).decode("utf-8", "replace"))
                        sys.stdout.flush()
                    shown = size
            except OSError:
                pass
            if run.get("ended"):
                print("\nEnded with exit code " + str(run.get("exit")))
                sys.exit(run.get("exit") if isinstance(run.get("exit"), int) else 1)
            time.sleep(1.0)


def cmd_runner(args, config: dict) -> None:
    """The detached parent of a leased job: starts it with unbuffered output into the log, records pid and exit code."""
    machine, ident = args.machine, args.ident
    launch = Path(args.launch)
    run_path = side_path(machine, ident, ".run.json")
    me = psutil.Process()
    run = {"runner_pid": me.pid, "runner_create_time": me.create_time(), "started": now_iso()}
    if not under(launch.resolve(), (LOCAL_DIR / "launch").resolve()):
        write_json(run_path, dict(run, error="launch file outside the local folder: " + str(launch), ended=now_iso(), exit=None))
        return
    order = read_json(launch)
    try:
        launch.unlink()
    except OSError:
        pass
    argv = resolve_interpreter(order.get("argv") or [])
    cwd = order.get("cwd") or str(Path.home())
    if not argv:
        write_json(run_path, dict(run, error="empty launch file", ended=now_iso(), exit=None))
        return
    env = dict(os.environ, PYTHONUNBUFFERED="1", LEASERES_LEASE=ident)
    env.pop("LEASERES_REEXEC", None)
    if sys.platform == "darwin":
        env["PATH"] = os.pathsep.join([env.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"])
    log_path = side_path(machine, ident, ".log")
    try:
        with open(log_path, "ab") as out:
            out.write(("[res] " + now_iso() + " starting on " + machine + ": " + " ".join(argv) + "  (cwd " + cwd + ")\n").encode("utf-8"))
            out.flush()
            kwargs = {"stdout": out, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": cwd, "env": env}
            if sys.platform == "win32":
                kwargs["creationflags"] = probes.no_window()
            child = subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        write_json(run_path, dict(run, error=str(exc), ended=now_iso(), exit=None))
        return
    try:
        child_ctime = psutil.Process(child.pid).create_time()
    except psutil.Error:
        child_ctime = None
    run.update({"pid": child.pid, "create_time": child_ctime, "argv": argv, "cwd": cwd})
    write_json(run_path, run)
    code = child.wait()
    with open(log_path, "ab") as out:
        out.write(("\n[res] " + now_iso() + " ended with exit code " + str(code) + "\n").encode("utf-8"))
    write_json(run_path, dict(run, ended=now_iso(), exit=code))


# ---------------------------------------------------------------- leases, status, compliance

def summary_line(lease: dict) -> list:
    state = lease.get("state", "?")
    when = lease.get("started") or lease.get("granted") or lease.get("requested")
    return [lease.get("id", "?"), state, lease.get("desk", ""), describe_need(lease.get("need", {}))[:60],
            human_duration(lease.get("duration")), human_duration(age_of(when)) + " ago",
            " ".join(lease.get("run_argv") or lease.get("command") or [])[:50]]


def cmd_leases(args, config: dict) -> None:
    machines = config["machines"] if args.all else [canonical_machine(args.on, config) if args.on else this_machine(config)]
    for machine in machines:
        leases = all_leases(machine)
        if not args.ended:
            leases = [l for l in leases if l.get("state") in OPEN or age_of(l.get("ended") or l.get("answered")) < 86400]
        if not leases and args.all:
            continue
        print(machine + ":")
        if not leases:
            print("  (no leases)")
            continue
        print_table([["id", "state", "desk", "need", "for", "since", "command"]] + [summary_line(l) for l in leases])


def cmd_status(args, config: dict) -> None:
    here = this_machine(config)
    machine = canonical_machine(args.on, config) if args.on else here
    if not args.id:
        fresh, report = dispatcher_state(machine, config)
        print(machine + " dispatcher: " + ("up" if fresh else "NOT REPORTING") + ", last report " + str(report.get("updated") or "never")
              + (", pid " + str(report.get("pid")) if report.get("pid") else ""))
        if report.get("queue"):
            print("  queue: " + ", ".join(report["queue"]))
        print_compliance(machine, config, 7)
        return
    lease = read_json(lease_path(machine, args.id))
    if not lease:
        raise SystemExit("No lease " + args.id + " on " + machine)
    for key in ["id", "state", "answer", "desk", "why", "need", "duration", "requested", "granted", "started", "ended",
                "exit", "seconds", "peak", "notices", "history_note", "run_argv", "run_cwd"]:
        if key in lease and lease[key] not in ("", None, {}, []):
            value = lease[key]
            if key == "need" or key == "peak":
                value = describe_need(value)
            elif key == "duration" or key == "seconds":
                value = human_duration(value)
            elif isinstance(value, (dict, list)):
                value = json.dumps(value)
            print(key.ljust(13) + str(value))
    log_path = side_path(machine, args.id, ".log")
    if log_path.exists():
        print("log          " + str(log_path))
        text = tail(log_path, int(num(config, "log_tail_lines", 30)))
        for line in text.splitlines():
            print("  | " + line)


def iter_unleased(machine: str) -> list:
    path = machine_dir(machine) / "unleased.jsonl"
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return rows


def print_compliance(machine: str, config: dict, days: float) -> None:
    since = time.time() - days * 86400
    trials = [read_json(p) for p in trial_dir(machine).glob("T*.json")] if trial_dir(machine).is_dir() else []
    trials = [t for t in trials if (parse_iso(t.get("started")) or 0) >= since]
    leases = [l for l in all_leases(machine) if (parse_iso(l.get("requested")) or 0) >= since]
    runs = [l for l in leases if l.get("started")]
    rows = [r for r in iter_unleased(machine) if (parse_iso(r.get("at")) or 0) >= since]
    starts = [r for r in rows if r.get("event") == "seen"]
    ends = {r.get("key"): r for r in rows if r.get("event") == "ended"}
    print("Compliance on " + machine + ", last " + format(days, "g") + " days (guidance only, D-260927-4 option b):")
    print("  trials: " + str(len(trials)) + " (" + str(len([t for t in trials if t.get("finished")])) + " finished inside the limit, "
          + str(len([t for t in trials if not t.get("finished")])) + " cut off)")
    by_state = collections.Counter(l.get("state") for l in leases)
    print("  lease requests: " + str(len(leases)) + (" (" + ", ".join(k + " " + str(v) for k, v in sorted(by_state.items())) + ")" if leases else ""))
    print("  leased runs: " + str(len(runs)))
    lifetimes = [float(ends[r.get("key")].get("lifetime", 0)) for r in starts if r.get("key") in ends]
    print("  long commands of Claude sessions without a lease: " + str(len(starts))
          + (" (median lifetime of the ended ones " + human_duration(statistics.median(lifetimes)) + ")" if lifetimes else ""))
    desks = collections.Counter(r.get("desk", "?") for r in starts)
    for desk, count in desks.most_common(8):
        print("    " + desk + ": " + str(count))


def cmd_compliance(args, config: dict) -> None:
    machine = canonical_machine(args.on, config) if args.on else this_machine(config)
    print_compliance(machine, config, float(args.days))
    if args.list:
        for row in iter_unleased(machine)[-int(args.list):]:
            print(json.dumps(row))


# ---------------------------------------------------------------- history

def history_note(runs: list, duration: float) -> str:
    seconds = [float(r["seconds"]) for r in runs if r.get("seconds") is not None]
    if not seconds:
        return ""
    last = seconds[-3:]
    median = statistics.median(seconds)
    note = "previous runs of this command took " + ", ".join(human_duration(s) for s in reversed(last)) + " (median " + human_duration(median) + ")"
    if duration and median > 0 and (duration < median / 2 or duration > median * 3):
        note += "; the stated duration " + human_duration(duration) + " is far from that"
    return note


def record_history(machine: str, lease: dict) -> None:
    key = lease.get("run_key") or lease.get("command_key")
    if not key:
        return
    path = machine_dir(machine) / "history.json"
    history = read_json(path)
    runs = history.get(key) or []
    runs.append({"lease": lease["id"], "seconds": lease.get("seconds"), "exit": lease.get("exit"), "ended": lease.get("ended"),
                 "peak": lease.get("peak", {}), "state": lease.get("state")})
    history[key] = runs[-20:]
    write_json(path, history)


# ---------------------------------------------------------------- the dispatcher

class Dispatcher:
    def __init__(self, config: dict):
        self.config = config
        self.machine = this_machine(config)
        self.folder = machine_dir(self.machine)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.meter = probes.Meter(config, LOCAL_DIR)
        self.window = collections.deque()             # (monotonic time, {dim: unleased})
        self.last_system = {}
        self.last_measured = {}                       # lease id -> {dim: use}
        self.overrun_counts = collections.defaultdict(int)
        self.log_sizes = {}                           # lease id -> (size, monotonic time of last growth)
        self.last_written = {}                        # lease id -> monotonic time of the last peak write
        self.peaks = {}                               # lease id -> {dim: peak}, kept between the throttled writes
        self.unleased_seen = {}                       # key -> record
        self.shell_sids = {}                          # POSIX: tool shell pid (= its session id) -> claude pid, cwd
        self.env_cache = {}                           # process key -> Claude markers in its environment
        self.learned = read_json(LOCAL_DIR / "learned_peaks.json")
        self.learned_dirty = False
        self.version = code_version()

    # -- state helpers
    def leases(self) -> list:
        return all_leases(self.machine)

    def save(self, lease: dict) -> None:
        write_json(lease_path(self.machine, lease["id"]), lease)

    def mail(self, lease: dict, subject: str, body: str) -> None:
        desk = lease.get("desk") or (self.machine + ":General")
        footer = ("\n\nFrom the LeaseResources dispatcher on " + self.machine + " (" + str(SCRIPT) + "). Lease file: "
                  + str(lease_path(self.machine, lease["id"])) + ". Replies to this mail reach nobody; use `res status "
                  + lease["id"] + (" --on " + self.machine if lease.get("from_machine") != self.machine else "") + "`.")
        send_mail(self.config, self.machine, desk, subject, body + footer)

    # -- measuring
    def unleased_now(self) -> dict:
        if not self.window:
            return {}
        total = collections.defaultdict(float)
        for _, sample in self.window:
            for d, v in sample.items():
                total[d] += v
        return {d: v / len(self.window) for d, v in total.items()}

    def sample(self) -> None:
        leases = [l for l in self.leases() if l.get("state") == "running"]
        trees = {}
        for lease in leases:
            run = read_json(side_path(self.machine, lease["id"], ".run.json"))
            proc = process(run.get("runner_pid"), run.get("runner_create_time"))
            if proc is not None:
                trees[lease["id"]] = tree_of(proc)
        system, per = self.meter.sample(trees)
        system_only = self.meter.system_only()
        running_need = collections.defaultdict(float)
        for lease in leases:
            for d, v in (lease.get("need") or {}).items():
                running_need[d] += float(v)
        unleased = {}
        for dim in self.meter.dimensions():
            if dim in system_only:
                measured = min(system.get(dim, 0.0), running_need.get(dim, 0.0))
            else:
                measured = sum(t.get(dim, 0.0) for t in per.values())
            unleased[dim] = max(0.0, system.get(dim, 0.0) - measured)
        now = time.monotonic()
        self.window.append((now, unleased))
        horizon = num(self.config, "smoothing_seconds", 30)
        while self.window and now - self.window[0][0] > horizon:
            self.window.popleft()
        self.last_system = system
        self.last_measured = per
        for dim, value in system.items():
            if kind_of(dim) in ("disk", "net") and value > float(self.learned.get(dim, 0)):
                self.learned[dim] = value
                self.learned_dirty = True

    # -- admission
    def limits(self) -> dict:
        return {d: info["capacity"] * headroom(self.config, d) for d, info in self.meter.dimensions().items()}

    def planned(self, exclude=None) -> dict:
        total = collections.defaultdict(float)
        for lease in self.leases():
            if lease.get("state") in ACTIVE and lease.get("id") != exclude:
                for d, v in (lease.get("need") or {}).items():
                    total[d] += float(v)
        return total

    def normalise(self, need: dict) -> tuple:
        """(this machine's need, notes, refusal or '')"""
        dims = self.meter.dimensions()
        kinds = {kind_of(d) for d in dims}
        result, notes = {}, []
        for name, amount in need.items():
            dim = self.meter.resolve(name)
            if dim is not None:
                result[dim] = result.get(dim, 0.0) + float(amount)
                continue
            if kind_of(name) not in kinds:
                return {}, notes, ("this machine has no " + kind_of(name) + " dimension (asked for " + name + "); its dimensions: "
                                   + ", ".join(dims))
            notes.append("ignored " + name + " (no such " + kind_of(name) + " here; this machine has "
                         + ", ".join(d for d in dims if kind_of(d) == kind_of(name)) + ")")
        return result, notes, ""

    def fits(self, need: dict, exclude=None) -> tuple:
        """(fits now, blocking dimension, text)"""
        limits = self.limits()
        unleased = self.unleased_now()
        planned = self.planned(exclude)
        for dim, amount in need.items():
            total = unleased.get(dim, 0.0) + planned.get(dim, 0.0) + amount
            if total > limits.get(dim, 0.0):
                kind = kind_of(dim)
                return False, dim, (dim + ": unleased " + human(unleased.get(dim, 0.0), kind) + " + leased "
                                    + human(planned.get(dim, 0.0), kind) + " + this " + human(amount, kind) + " > "
                                    + human(limits.get(dim, 0.0), kind) + " allowed")
        return True, "", ""

    def handle_request(self, lease: dict) -> None:
        need, notes, refusal = self.normalise(lease.get("need") or {})
        limits = self.limits()
        if not refusal:
            for dim, amount in need.items():
                if amount > limits.get(dim, 0.0):
                    kind = kind_of(dim)
                    cap = self.meter.dimensions()[dim]["capacity"]
                    refusal = (dim + " needs " + human(amount, kind) + " but " + self.machine + " allows at most "
                               + human(limits[dim], kind) + " (" + human(cap, kind) + " x " + str(headroom(self.config, dim)) + ")")
                    break
        lease["need_asked"] = lease.get("need")
        lease["need"] = need
        lease["answered"] = now_iso()
        lease["start_within_minutes"] = num(self.config, "start_within_minutes", 60)
        runs = read_json(self.folder / "history.json").get(lease.get("command_key") or "") or []
        note = history_note(runs, float(lease.get("duration") or 0))
        if note:
            lease["history_note"] = note
        if refusal:
            lease["state"] = "refused"
            lease["answer"] = "refused: " + refusal
            lease["ended"] = now_iso()
        else:
            queue_ahead = [l for l in self.leases() if l.get("state") == "queued"]
            ok, dim, text = self.fits(need)
            if ok and not queue_ahead:
                self.grant(lease, notes)
            else:
                lease["state"] = "queued"
                lease["waits_for"] = dim if not ok else "the queue"
                lease["answer"] = ("queued at position " + str(len(queue_ahead) + 1) + ", waiting for "
                                   + (text if not ok else "the leases ahead of it") + ("; " + "; ".join(notes) if notes else ""))
        self.save(lease)
        log(self.machine, "request " + lease["id"] + " from " + lease.get("desk", "?") + ": " + lease["state"] + " - " + lease.get("answer", ""))
        if lease.get("from_machine") != self.machine:
            self.mail(lease, "Lease " + lease["id"] + " on " + self.machine + ": " + lease["state"], self.answer_body(lease))

    def grant(self, lease: dict, notes=None) -> None:
        lease["state"] = "granted"
        lease["granted"] = now_iso()
        lease["answer"] = ("granted: " + describe_need(lease.get("need", {})) + " for " + human_duration(lease.get("duration"))
                           + ("; " + "; ".join(notes) if notes else ""))

    def answer_body(self, lease: dict) -> str:
        lines = ["Your lease request " + lease["id"] + " on " + self.machine + " is " + lease["state"] + ".", "",
                 lease.get("answer", "")]
        if lease.get("history_note"):
            lines.append(lease["history_note"])
        order = read_json(side_path(self.machine, lease["id"], ".order.json"))
        if lease["state"] == "granted":
            if order:
                lines.append("Its run order is being started now: " + " ".join(order.get("argv", [])))
            else:
                on = " --on " + self.machine if lease.get("from_machine") != self.machine else ""
                lines.append("Start the job within " + str(int(lease.get("start_within_minutes", 60))) + " min: "
                             + "res run --lease " + lease["id"] + on + " -- <program>")
        return "\n".join(lines)

    def serve_queue(self) -> None:
        for lease in [l for l in self.leases() if l.get("state") == "queued"]:
            ok, dim, text = self.fits(lease.get("need") or {})
            if not ok:
                if lease.get("waits_for") != dim:
                    lease["waits_for"] = dim
                    self.save(lease)
                break                                       # first come, first served: nobody overtakes
            self.grant(lease)
            self.save(lease)
            log(self.machine, "granted queued " + lease["id"])
            if not side_path(self.machine, lease["id"], ".order.json").exists():
                self.mail(lease, "Lease " + lease["id"] + " granted on " + self.machine, self.answer_body(lease))
            else:
                self.start_order(lease)

    # -- runs
    def start_order(self, lease: dict) -> None:
        order = read_json(side_path(self.machine, lease["id"], ".order.json"))
        if not order or order.get("start") != "dispatcher":
            return
        # Everything here comes from the synced folder, whatever machine the file claims to be from: the program
        # rule always applies, and exactly the checked program is handed to the runner.
        argv = list(order.get("argv") or [])
        cwd = translate_path(order.get("cwd") or "") or str(Path.home())
        if argv:
            argv = [argv[0]] + [translate_path(a) if ("/" in a or chr(92) in a) else a for a in argv[1:]]
        ok, reason, script = check_program(argv, cwd, self.config)
        if not ok:
            log(self.machine, "REFUSED run of " + lease["id"] + " ordered from " + str(order.get("from_machine")) + ": " + reason
                + " (" + " ".join(argv) + ")")
            lease.update(state="refused", answer="run refused: " + reason, ended=now_iso())
            self.save(lease)
            self.mail(lease, "Run of " + lease["id"] + " refused on " + self.machine, "The run order was refused: " + reason
                      + "\n\nProgram: " + " ".join(argv) + "\nA leased program must be python, pythonw, py or powershell -File with a"
                      + " script under this machine's allowed roots (config/" + self.machine.casefold() + ".json).")
            return
        lease.update(state="starting", starting=now_iso())
        self.save(lease)
        log(self.machine, "starting run of " + lease["id"] + ": " + " ".join(argv))
        start_runner(self.machine, lease["id"], resolve_interpreter(argv), cwd)

    def handle_orders(self) -> None:
        for lease in self.leases():
            if lease.get("state") == "granted" and side_path(self.machine, lease["id"], ".order.json").exists() \
                    and not side_path(self.machine, lease["id"], ".run.json").exists():
                order = read_json(side_path(self.machine, lease["id"], ".order.json"))
                if order.get("start") == "dispatcher":
                    self.start_order(lease)
                elif age_of(order.get("ordered")) > 60:
                    lease.update(state="failed", answer="the runner never reported", ended=now_iso())
                    self.save(lease)

    def handle_asks(self) -> None:
        for path in list(self.folder.glob("*.extend.json")):
            ident = path.name[: -len(".extend.json")]
            ask = read_json(path)
            lease = read_json(lease_path(self.machine, ident))
            try:
                path.unlink()
            except OSError:
                pass
            if not lease or lease.get("state") in ENDED:
                continue
            parts = []
            if "duration" in ask:
                lease["duration"] = float(ask["duration"])
                lease.get("notices", {}).pop("duration", None)
                parts.append("duration now " + human_duration(lease["duration"]))
            if ask.get("need"):
                need, notes, refusal = self.normalise(ask["need"])
                merged = dict(lease.get("need") or {})
                merged.update(need)
                ok, dim, text = (False, "", refusal) if refusal else self.fits(merged, exclude=ident)
                if ok or lease.get("state") == "queued":
                    lease["need"] = merged
                    for d in need:
                        lease.get("notices", {}).pop("overrun:" + d, None)
                    parts.append("need now " + describe_need(merged))
                else:
                    parts.append("more resources refused for now (" + text + "); the lease keeps " + describe_need(lease.get("need", {})))
            answer = ident + ": " + "; ".join(parts)
            lease["last_extend"] = {"asked": ask.get("asked"), "answer": answer, "at": now_iso()}
            self.save(lease)
            log(self.machine, "extend " + answer)
            if ask.get("from_machine") and ask.get("from_machine") != self.machine:
                self.mail(lease, "Extension of " + ident + " on " + self.machine, answer)
        for path in list(self.folder.glob("*.release.json")):
            ident = path.name[: -len(".release.json")]
            ask = read_json(path)
            lease = read_json(lease_path(self.machine, ident))
            try:
                path.unlink()
            except OSError:
                pass
            if not lease or lease.get("state") in ENDED:
                continue
            if lease.get("state") in ("running", "starting"):
                if not ask.get("kill"):
                    continue
                run = read_json(side_path(self.machine, ident, ".run.json"))
                proc = process(run.get("runner_pid"), run.get("runner_create_time"))
                still = kill_tree(tree_of(proc)) if proc else []
                lease.update(state="killed", answer="killed on request of its owner" + (", survivors " + str(still) if still else ""),
                             ended=now_iso())
            else:
                lease.update(state="released", answer="released by its owner", ended=now_iso())
            self.save(lease)
            log(self.machine, "release " + ident + ": " + lease["state"])

    def handle_new(self) -> None:
        for lease in self.leases():
            if lease.get("state") == "requested":
                self.handle_request(lease)

    # -- watching
    def watch(self) -> None:
        overrun_margin = num(self.config, "overrun_margin", 0.1)
        floors = self.config.get("overrun_floor", {})
        need_samples = int(num(self.config, "overrun_samples", 3))
        stall_seconds = 60 * num(self.config, "stall_minutes", 30)
        system_only = self.meter.system_only()
        for lease in self.leases():
            ident = lease["id"]
            state = lease.get("state")
            if state == "granted" and not side_path(self.machine, ident, ".order.json").exists():
                if age_of(lease.get("granted")) > 60 * float(lease.get("start_within_minutes", 60)):
                    lease.update(state="expired", answer="granted but never started", ended=now_iso())
                    self.save(lease)
                    log(self.machine, "expired " + ident)
                continue
            if state not in ("granted", "starting", "running"):
                continue
            run = read_json(side_path(self.machine, ident, ".run.json"))
            if run.get("error"):
                lease.update(state="failed", answer="could not start: " + run["error"], ended=now_iso())
                self.save(lease)
                self.mail(lease, "Leased job " + ident + " failed to start on " + self.machine, lease["answer"])
                continue
            if state in ("granted", "starting"):
                if run.get("pid"):
                    state = self.mark_running(lease, run)
                elif state == "starting" and age_of(lease.get("starting")) > 60:
                    lease.update(state="failed", answer="the runner never reported", ended=now_iso())
                    self.save(lease)
                    continue
                else:
                    continue
            if run.get("ended"):
                self.finish(lease, run)
                continue
            proc = process(run.get("runner_pid"), run.get("runner_create_time"))
            if proc is None:
                self.finish(lease, run, lost=True)
                continue
            if state != "running":
                continue
            notices = lease.setdefault("notices", {})
            changed = False
            measured = self.last_measured.get(ident, {})
            peaks = lease.setdefault("peak", {})
            peaks.update({d: max(v, peaks.get(d, 0.0)) for d, v in self.peaks.get(ident, {}).items()})
            for dim, value in measured.items():
                if value > peaks.get(dim, 0.0):
                    peaks[dim] = value
            self.peaks[ident] = dict(peaks)
            need = lease.get("need") or {}
            for dim, value in measured.items():
                if dim in system_only:
                    continue
                allowed = float(need.get(dim, 0.0))
                floor = float(floors.get(kind_of(dim), 0.0))
                key = ident + "|" + dim
                if value > allowed * (1 + overrun_margin) and value - allowed > floor:
                    self.overrun_counts[key] += 1
                else:
                    self.overrun_counts[key] = 0
                if self.overrun_counts[key] >= need_samples and "overrun:" + dim not in notices:
                    kind = kind_of(dim)
                    notices["overrun:" + dim] = {"at": now_iso(), "measured": value, "declared": allowed}
                    changed = True
                    log(self.machine, "overrun " + ident + " " + dim + " " + human(value, kind) + " > " + human(allowed, kind))
                    self.mail(lease, "Leased job " + ident + " uses more " + dim + " than declared",
                              "The leased job " + ident + " on " + self.machine + " uses " + human(value, kind) + " of " + dim
                              + ", declared " + human(allowed, kind) + " (margin " + format(overrun_margin * 100, ".0f")
                              + " %). It keeps running; nothing is killed.\nPid " + str(run.get("pid")) + ", command: "
                              + " ".join(run.get("argv", [])) + "\nLog: " + str(side_path(self.machine, ident, ".log"))
                              + "\nIf it really needs that much, renegotiate: res extend " + ident + " --need " + dim + "="
                              + format(value * 1.25, ".6g") + (" --on " + self.machine if lease.get("from_machine") != self.machine else ""))
            started = parse_iso(lease.get("started"))
            if started and time.time() - started > float(lease.get("duration") or 0) and "duration" not in notices:
                notices["duration"] = {"at": now_iso()}
                changed = True
                log(self.machine, "past duration " + ident)
                self.mail(lease, "Leased job " + ident + " ran past its duration",
                          "The leased job " + ident + " on " + self.machine + " has run " + human_duration(time.time() - started)
                          + ", past its stated " + human_duration(lease.get("duration")) + ". It keeps running.\nLog tail:\n\n"
                          + tail(side_path(self.machine, ident, ".log"), 10) + "\n\nExtend: res extend " + ident + " --for <more>")
            log_path = side_path(self.machine, ident, ".log")
            try:
                size = log_path.stat().st_size
            except OSError:
                size = 0
            last_size, since = self.log_sizes.get(ident, (-1, time.monotonic()))
            if size != last_size:
                self.log_sizes[ident] = (size, time.monotonic())
                if "stall" in notices:
                    notices.pop("stall")
                    changed = True
            elif time.monotonic() - since > stall_seconds and "stall" not in notices:
                notices["stall"] = {"at": now_iso(), "log_bytes": size}
                changed = True
                log(self.machine, "stalled " + ident)
                self.mail(lease, "Leased job " + ident + " looks stalled",
                          "The leased job " + ident + " on " + self.machine + " has written no output for "
                          + human_duration(time.monotonic() - since) + ". It keeps running; nothing is killed.\nPid "
                          + str(run.get("pid")) + ", measured now: " + describe_need(measured) + "\nLog tail ("
                          + str(log_path) + "):\n\n" + tail(log_path, 10))
            if changed or time.monotonic() - self.last_written.get(ident, 0) > 60:
                self.save(lease)
                self.last_written[ident] = time.monotonic()

    def mark_running(self, lease: dict, run: dict) -> str:
        order = read_json(side_path(self.machine, lease["id"], ".order.json"))
        lease.update(state="running", started=run.get("started") or now_iso(), pid=run.get("pid"),
                     run_argv=run.get("argv") or order.get("argv"), run_cwd=run.get("cwd") or order.get("cwd"),
                     run_key=command_key(order.get("argv") or []))
        self.save(lease)
        log(self.machine, "running " + lease["id"] + " pid " + str(run.get("pid")))
        if order.get("start") == "dispatcher":
            self.mail(lease, "Leased job " + lease["id"] + " started on " + self.machine,
                      "The run order of lease " + lease["id"] + " started on " + self.machine + ": pid " + str(run.get("pid"))
                      + "\nCommand: " + " ".join(run.get("argv") or []) + "\nLog: " + str(side_path(self.machine, lease["id"], ".log"))
                      + "\nYou get mail when it ends, stalls, overruns or runs past " + human_duration(lease.get("duration")) + ".")
        return "running"

    def finish(self, lease: dict, run: dict, lost: bool = False) -> None:
        ident = lease["id"]
        if lost:
            state = "lost"
            end = now_iso()
        else:
            state = "finished" if run.get("exit") == 0 else "failed"
            end = run.get("ended")
        started = parse_iso(lease.get("started") or run.get("started"))
        seconds = (parse_iso(end) or time.time()) - started if started else None
        lease.update(state=state, ended=end, exit=run.get("exit"), seconds=seconds)
        peaks = lease.setdefault("peak", {})
        peaks.update({d: max(v, peaks.get(d, 0.0)) for d, v in self.peaks.pop(ident, {}).items()})
        if not lease.get("run_key"):
            order = read_json(side_path(self.machine, ident, ".order.json"))
            lease["run_key"] = command_key(order.get("argv") or [])
        self.save(lease)
        if not lost:
            runs = read_json(self.folder / "history.json").get(lease.get("run_key")) or []
            previous = history_note(runs, 0)
            record_history(self.machine, lease)
        else:
            previous = ""
        log(self.machine, state + " " + ident + " exit " + str(run.get("exit")) + " after " + human_duration(seconds))
        log_path = side_path(self.machine, ident, ".log")
        need = lease.get("need") or {}
        peaks = lease.get("peak") or {}
        machine_only = self.meter.system_only()
        compare = "; ".join(d + " " + ("not measurable per process" if d in machine_only else human(peaks.get(d, 0.0), kind_of(d)))
                            + " of " + human(need.get(d, 0.0), kind_of(d)) for d in sorted(set(need) | set(peaks)))
        if lost:
            head = ("The leased job " + ident + " on " + self.machine + " is lost: its runner is gone without recording an end"
                    + " (a reboot, a crash or a kill). It is not restarted.")
        else:
            head = ("The leased job " + ident + " on " + self.machine + " " + ("finished" if state == "finished" else "FAILED")
                    + " with exit code " + str(run.get("exit")) + " after " + human_duration(seconds) + ".")
        body = (head + "\n\nCommand: " + " ".join(run.get("argv") or lease.get("run_argv") or []) + "\nWorking folder: "
                + str(run.get("cwd") or lease.get("run_cwd") or "") + "\nLog: " + str(log_path) + "\nPeak use vs declared: "
                + (compare or "n/a") + ("\nEarlier runs: " + previous if previous else "")
                + "\n\nLast " + str(int(num(self.config, "log_tail_lines", 30))) + " lines of the log:\n\n```\n"
                + tail(log_path, int(num(self.config, "log_tail_lines", 30))) + "\n```")
        self.mail(lease, "Leased job " + ident + " " + state + " on " + self.machine, body)

    # -- compliance: long commands of Claude sessions without a lease
    def claude_names(self) -> list:
        return [n.casefold() for n in self.config.get("claude_process_names", ["claude", "claude.exe"])]

    def is_tool_shell(self, cmd: str, name: str) -> bool:
        markers = self.config.get("tool_shell_markers", ["shell-snapshots"])
        shell_names = [n.casefold() for n in self.config.get("tool_shell_names", [])]
        return any(m in cmd for m in markers) or name.casefold() in shell_names

    def ignored(self, cmd: str) -> bool:
        return any(m in cmd for m in self.config.get("ignore_command_markers", []))

    def track_shells(self) -> None:
        """Every sample: remember the session id of each live tool shell (POSIX: Claude starts each tool shell as a
        session leader), so a job backgrounded with nohup or & keeps pointing at its shell after the shell exits."""
        if sys.platform == "win32":
            return
        names = self.claude_names()
        claudes = set()
        procs = []
        for proc in psutil.process_iter(["pid", "ppid", "name"]):
            procs.append(proc)
            if str(proc.info.get("name") or "").casefold() in names:
                claudes.add(proc.info["pid"])
        now = time.time()
        for proc in procs:
            if proc.info.get("ppid") not in claudes or proc.info["pid"] in self.shell_sids:
                continue
            try:
                cmd = " ".join(proc.cmdline())
                sid = os.getsid(proc.pid)
            except (psutil.Error, OSError):
                continue
            if sid != proc.pid or not self.is_tool_shell(cmd, str(proc.info.get("name") or "")) or self.ignored(cmd):
                continue
            try:
                cwd = psutil.Process(proc.info["ppid"]).cwd()
            except (psutil.Error, OSError):
                cwd = ""
            self.shell_sids[proc.pid] = {"claude_pid": proc.info["ppid"], "cwd": cwd, "seen": now, "shell": cmd[-160:]}
        for sid in [s for s, v in self.shell_sids.items() if now - v["seen"] > 86400]:
            self.shell_sids.pop(sid, None)

    def claude_env(self, proc, key: str) -> dict:
        """The Claude markers in a process's environment ({} when absent or unreadable). Cached per process: an
        environment never changes. macOS does not reveal the environment of Apple's own binaries (/bin/sleep, zsh);
        Homebrew, venv and other programs are readable, and on Windows psutil reads it for the user's processes."""
        if key in self.env_cache:
            return self.env_cache[key]
        try:
            env = proc.environ()
        except (psutil.Error, OSError):
            env = {}
        found = {}
        if env.get("CLAUDECODE") == "1" and not env.get("LEASERES_LEASE"):
            found = {"session": env.get("CLAUDE_CODE_SESSION_ID", ""), "claude_pid": env.get("CLAUDE_PID", "")}
        self.env_cache[key] = found
        return found

    def check_unleased(self) -> None:
        """Long commands that Claude sessions run without a lease, three ways: (a) a tool shell that has lived longer
        than unleased_after_seconds; (b) POSIX: a process in the session of a tool shell that has gone, i.e. a job
        backgrounded with nohup or & whose shell exited; (c) a process that left every Claude tree (setsid, double
        fork, reparented to launchd/init) but carries CLAUDECODE=1 in its environment. Leased jobs (LEASERES_LEASE in
        their environment, started through res.py) and res.py, mail.py and house.py themselves are excluded. Only the
        topmost qualifying process of each tree is recorded."""
        names = self.claude_names()
        after = num(self.config, "unleased_after_seconds", 120)
        try:
            user = psutil.Process().username()
        except psutil.Error:
            user = None
        children = collections.defaultdict(list)
        info = {}
        for p in psutil.process_iter(["pid", "ppid", "name", "create_time", "cmdline", "username"]):
            data = p.info
            info[data["pid"]] = (p, data)
            children[data.get("ppid")].append(data["pid"])
        now = time.time()

        def cmd_of(pid):
            return " ".join(info[pid][1].get("cmdline") or []) if pid in info else ""

        def claude_below(pid):
            """The direct child of a live claude process on pid's chain (pid itself included), else None."""
            seen = set()
            while pid in info and pid not in seen:
                seen.add(pid)
                parent = info[pid][1].get("ppid")
                if parent in info and str(info[parent][1].get("name") or "").casefold() in names:
                    return pid
                pid = parent
            return None

        found = {}                                    # key -> record
        for pid, (proc, data) in info.items():
            if str(data.get("name") or "").casefold() in names:
                continue
            age = now - float(data.get("create_time") or now)
            if age < after or (user and data.get("username") and data.get("username") != user):
                continue
            cmd = cmd_of(pid)
            if self.ignored(cmd):
                continue
            key = str(pid) + "-" + format(float(data.get("create_time") or 0), ".2f")
            top = claude_below(pid)
            if top is not None:
                if top != pid or not self.is_tool_shell(cmd, str(data.get("name") or "")):
                    continue                          # inside a live Claude tree: counted at its tool shell, if any
                descendants, stack = [], list(children.get(pid, []))
                while stack:
                    d = stack.pop()
                    if d in info:
                        descendants.append(d)
                        stack.extend(children.get(d, []))
                commands = [cmd_of(d) for d in descendants]
                if any(self.ignored(c) for c in commands):
                    continue
                found[key] = {"how": "tool shell", "pid": pid, "claude_pid": data.get("ppid"), "age": age,
                              "cwd_pid": data.get("ppid"), "shell": cmd[-160:], "commands": [c[:160] for c in commands[:4]]}
                continue
            record = None
            if sys.platform != "win32":
                try:
                    sid = os.getsid(pid)
                except OSError:
                    sid = None
                shell = self.shell_sids.get(sid)
                if shell is not None and sid != pid and process(sid) is None:
                    record = {"how": "backgrounded, its tool shell gone", "claude_pid": shell["claude_pid"], "cwd": shell["cwd"],
                              "shell": shell["shell"]}
            if record is None:
                marks = self.claude_env(proc, key)
                if marks:
                    record = {"how": "left the Claude tree (environment marker)", "claude_pid": marks.get("claude_pid"),
                              "session": marks.get("session"), "cwd_pid": pid}
            if record is None:
                continue
            parent = data.get("ppid")
            if parent in info and self.escaped_parent(parent, info, now, after):
                continue                              # its parent is recorded instead
            record.update(pid=pid, age=age, commands=[cmd[:300]])
            found[key] = record
        for key, rec in found.items():
            if key in self.unleased_seen:
                continue
            cwd = rec.pop("cwd", None)
            cwd_pid = rec.pop("cwd_pid", None)
            if cwd is None:
                try:
                    cwd = psutil.Process(cwd_pid).cwd() if cwd_pid else ""
                except (psutil.Error, OSError, ValueError):
                    cwd = ""
            record = {"event": "seen", "at": now_iso(), "key": key, "how": rec.get("how"), "shell_pid": rec.get("pid"),
                      "claude_pid": rec.get("claude_pid"), "session": rec.get("session", ""), "age": round(rec.get("age", 0)),
                      "cwd": cwd, "desk": self.desk_of(cwd), "shell": rec.get("shell", ""), "commands": rec.get("commands", [])}
            self.unleased_seen[key] = dict(record, first=now - rec.get("age", 0))
            self.append_unleased(record)
            log(self.machine, "unleased long command (" + str(record["how"]) + ") in " + record["desk"] + ": "
                + (record["commands"][0] if record["commands"] else record["shell"][-80:]))
        for key in list(self.unleased_seen):
            if key not in found:
                seen = self.unleased_seen.pop(key)
                self.append_unleased({"event": "ended", "at": now_iso(), "key": key, "desk": seen.get("desk"),
                                      "how": seen.get("how"), "lifetime": round(now - seen.get("first", now))})
        live = set(info)
        self.env_cache = {k: v for k, v in self.env_cache.items() if int(k.partition("-")[0]) in live}

    def escaped_parent(self, pid: int, info: dict, now: float, after: float) -> bool:
        """Whether pid would itself be recorded as an escaped job (so its children are not recorded separately)."""
        proc, data = info[pid]
        if pid <= 1 or now - float(data.get("create_time") or now) < after:
            return False
        if self.ignored(" ".join(data.get("cmdline") or [])):
            return False
        if sys.platform != "win32":
            try:
                sid = os.getsid(pid)
            except OSError:
                sid = None
            if sid in self.shell_sids and sid != pid and process(sid) is None:
                return True
        key = str(pid) + "-" + format(float(data.get("create_time") or 0), ".2f")
        return bool(self.claude_env(proc, key))

    def desk_of(self, folder: str) -> str:
        if not folder:
            return self.machine + ":?"
        try:
            out = subprocess.run([sys.executable, "-B", mail_script(self.config), "desk-of", folder], capture_output=True,
                                 text=True, timeout=20, creationflags=probes.no_window(), env=mail_env()).stdout
            desk = out.partition("\t")[0].strip()
            if desk:
                return self.machine + ":" + desk
        except (OSError, subprocess.SubprocessError):
            pass
        return self.machine + ":?"

    def append_unleased(self, record: dict) -> None:
        with open(self.folder / "unleased.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    # -- report, housekeeping
    def report(self) -> None:
        dims = self.meter.dimensions()
        limits = self.limits()
        leases = self.leases()
        write_json(self.folder / "dispatcher.json", {
            "machine": self.machine, "pid": os.getpid(), "version": self.version, "updated": now_iso(),
            "capacity": {d: {"capacity": i["capacity"], "limit": limits[d], "label": i.get("label", "")} for d, i in dims.items()},
            "unleased": self.unleased_now(), "planned": self.planned(), "system": self.last_system,
            "queue": [l["id"] for l in leases if l.get("state") == "queued"],
            "active": [l["id"] for l in leases if l.get("state") in ACTIVE]})
        if self.learned_dirty:
            write_json(LOCAL_DIR / "learned_peaks.json", self.learned)
            self.learned_dirty = False

    def housekeeping(self) -> None:
        keep = 86400 * num(self.config, "keep_days", 30)
        done = self.folder / "done"
        for lease in self.leases():
            if lease.get("state") in ENDED and age_of(lease.get("ended") or lease.get("answered")) > 7 * 86400:
                done.mkdir(exist_ok=True)
                for path in [lease_path(self.machine, lease["id"])] + [side_path(self.machine, lease["id"], s) for s in SUFFIXES + [".log"]]:
                    if path.exists():
                        try:
                            os.replace(path, done / path.name)
                        except OSError:
                            pass
        for folder in [done, trial_dir(self.machine)]:
            if folder.is_dir():
                for path in folder.iterdir():
                    try:
                        if time.time() - path.stat().st_mtime > keep:
                            path.unlink()
                    except OSError:
                        pass


def code_version() -> str:
    parts = []
    for path in CODE_FILES:
        try:
            stat = path.stat()
            parts.append(str(stat.st_mtime_ns) + "-" + str(stat.st_size))
        except OSError:
            parts.append("?")
    return "/".join(parts)


def code_compiles() -> bool:
    try:
        for path in CODE_FILES:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        return True
    except (OSError, SyntaxError, ValueError):
        return False


def local_heartbeat() -> Path:
    return LOCAL_DIR / "dispatcher.json"


def take_lock(machine: str) -> bool:
    beat = read_json(local_heartbeat())
    pid = beat.get("pid")
    if isinstance(pid, int) and pid != os.getpid() and process(pid) is not None:
        if age_of(beat.get("updated")) < 120:
            return False
        try:
            psutil.Process(pid).terminate()
            log(machine, "stopped a dispatcher that stopped reporting (pid " + str(pid) + ")")
        except psutil.Error:
            pass
    write_json(local_heartbeat(), {"pid": os.getpid(), "updated": now_iso()})
    return True


def cmd_dispatch(args, config: dict) -> None:
    machine = this_machine(config)
    if not take_lock(machine):
        sys.exit(EXIT_ALREADY_RUNNING)
    d = Dispatcher(config)
    log(machine, "dispatcher started (pid " + str(os.getpid()) + ", code " + d.version + ", dimensions "
        + ", ".join(d.meter.dimensions()) + ")")
    time.sleep(1.0)
    d.sample()
    last_sample = last_unleased = last_beat = last_house = time.monotonic()
    d.report()
    while True:
        try:
            try:
                d.config = load_config()
            except (OSError, ValueError) as exc:
                log(machine, "WARN cannot read config, keeping the last one: " + str(exc))
            write_json(local_heartbeat(), {"pid": os.getpid(), "updated": now_iso()})
            d.handle_asks()
            d.handle_new()
            d.handle_orders()
            now = time.monotonic()
            if now - last_sample >= num(d.config, "sample_seconds", 5):
                d.sample()
                d.track_shells()
                d.watch()
                d.serve_queue()
                d.report()
                last_sample = now
            if now - last_unleased >= num(d.config, "unleased_check_seconds", 60):
                d.check_unleased()
                last_unleased = now
            if now - last_beat >= 3600:
                log(machine, "heartbeat: " + str(len([l for l in d.leases() if l.get("state") in ACTIVE])) + " active leases")
                last_beat = now
            if now - last_house >= 3600:
                d.housekeeping()
                last_house = now
        except Exception as exc:                              # the dispatcher itself must keep running
            log(machine, "WARN dispatcher: " + type(exc).__name__ + ": " + str(exc))
        if code_version() != d.version and code_compiles():
            log(machine, "code changed; dispatcher restarting onto the new code")
            sys.exit(EXIT_CODE_CHANGED)
        time.sleep(num(d.config, "tick_seconds", 1))


def cmd_watch(args, config: dict) -> None:
    """The keeper the LaunchAgent / scheduled task runs: keeps a dispatcher child alive, restarts it at once after a code
    change. Kept tiny, because only a restart of the task renews it."""
    machine = this_machine(config)
    log(machine, "watch started (pid " + str(os.getpid()) + ")")
    while True:
        started = time.monotonic()
        try:
            code = subprocess.call([sys.executable, "-B", str(SCRIPT), "dispatch"], creationflags=probes.no_window())
        except OSError as exc:
            log(machine, "WARN cannot start the dispatcher: " + str(exc))
            code = -1
        if code == EXIT_CODE_CHANGED:
            continue
        if code == EXIT_ALREADY_RUNNING:
            time.sleep(60)
            continue
        log(machine, "WARN dispatcher exited with code " + str(code) + " after " + str(int(time.monotonic() - started)) + " s")
        time.sleep(30)


# ---------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(prog="res", description="Resource leases (LeaseResources). See README.md beside res.py.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("now", help="every dimension on this machine")
    p.add_argument("--json", action="store_true")
    sub.add_parser("mine", help="this Claude session's process tree")
    p = sub.add_parser("trial", help="run a command for at most 10 s under the meter")
    p.add_argument("--json", action="store_true")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p = sub.add_parser("request", help="ask a dispatcher for a lease")
    p.add_argument("--profile")
    p.add_argument("--need")
    p.add_argument("--for", dest="duration")
    p.add_argument("--on")
    p.add_argument("--why")
    p.add_argument("--desk")
    p.add_argument("--margin", type=float)
    p.add_argument("--extend", help="extend this lease instead (same as res extend)")
    p = sub.add_parser("extend", help="more time or resources for a lease")
    p.add_argument("id")
    p.add_argument("--for", dest="duration")
    p.add_argument("--profile")
    p.add_argument("--need")
    p.add_argument("--on")
    p = sub.add_parser("release", help="give a lease back")
    p.add_argument("id")
    p.add_argument("--kill", action="store_true")
    p.add_argument("--on")
    p = sub.add_parser("run", help="start a leased job detached")
    p.add_argument("--lease", required=True)
    p.add_argument("--on")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--cwd")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p = sub.add_parser("leases", help="the leases on a machine")
    p.add_argument("--on")
    p.add_argument("--all", action="store_true")
    p.add_argument("--ended", action="store_true", help="include leases that ended more than a day ago")
    p = sub.add_parser("status", help="one lease, or the dispatcher")
    p.add_argument("id", nargs="?")
    p.add_argument("--on")
    p = sub.add_parser("compliance", help="how often sessions trial, request, and run long without a lease")
    p.add_argument("--days", default="7")
    p.add_argument("--on")
    p.add_argument("--list", help="also print the last N unleased records")
    sub.add_parser("dispatch")
    sub.add_parser("watch")
    p = sub.add_parser("_runner")
    p.add_argument("machine")
    p.add_argument("ident")
    p.add_argument("launch")
    args = parser.parse_args()
    config = load_config()
    handlers = {"now": cmd_now, "mine": cmd_mine, "trial": cmd_trial, "request": cmd_request, "extend": cmd_extend,
                "release": cmd_release, "run": cmd_run, "leases": cmd_leases, "status": cmd_status,
                "compliance": cmd_compliance, "dispatch": cmd_dispatch, "watch": cmd_watch, "_runner": cmd_runner}
    handlers[args.cmd](args, config)


if __name__ == "__main__":
    main()
