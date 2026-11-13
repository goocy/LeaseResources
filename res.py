"""res: resource leases for the Claude sessions on the house machines (LeaseResources).

Every resource is a dimension (cpu, ram, each disk, each GPU and its memory, each network interface; see probes.py).
A command that finishes within a 10-second trial needs nothing more. Anything longer asks this machine's dispatcher
(or another machine's, with --on) for a lease: the trial's profile plus a margin, for a stated duration. The dispatcher
grants it when, on every dimension, the unleased use measured now plus the leases already granted plus the request
stays under capacity times the headroom; otherwise it queues it and mails the owner desk when it is granted. A leased
job runs detached from every Claude process and the dispatcher tells its owner when it ends, stalls, runs past its
duration or uses more than it declared: through the output of a process waiting on the lease (res run --wait, res wait)
while one is alive, otherwise by mail to the owner desk (ClaudeMail). It never kills a leased job. Design: README.md beside this file,
and the design sketch for decision D-260927-4.

Commands for sessions (python3 on the Mac, python on Windows; any interpreter works, res.py moves itself into its venv):
    now                                   every dimension: capacity, use, leased, unleased, free
    mine                                  what this Claude session's process tree uses right now
    trial -- COMMAND...                   run under the meter, kill the whole process tree at 10 s, print the profile
    request (--profile TRIAL | --need DIM=AMOUNT,...) --for DURATION [--on MACHINE] [--why TEXT] [--desk M:D]
    run --lease ID [--on MACHINE] [--wait] [--cwd DIR] -- PROGRAM...   start a leased job, detached, with a log
    wait ID                               attach to a lease on this machine: its output and notices arrive here, no mail
    extend ID [--for DURATION] [--profile TRIAL | --need ...] [--on MACHINE]   --for sets the new total, from the start
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
Locally, never synced: ~/.claude/leaseresources/ (the dispatcher's heartbeat, disk map, learned capacities), and in
the local state folder attached/<id>.json (the process waiting on a lease) and notices/<id>/ (notices written for it).

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
import atexit            # noqa: E402
import collections       # noqa: E402
import copy              # noqa: E402
import datetime          # noqa: E402
import functools         # noqa: E402
import json              # noqa: E402
import shutil            # noqa: E402
import signal            # noqa: E402
import socket            # noqa: E402
import statistics        # noqa: E402
import subprocess        # noqa: E402
import time              # noqa: E402
import threading         # noqa: E402
import base64            # noqa: E402

import psutil            # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probes            # noqa: E402
from probes import kind_of, read_json, write_json, UNITS   # noqa: E402

SERVICE_DIR = Path(__file__).resolve().parent
SCRIPT = SERVICE_DIR / "res.py"
CODE_FILES = [SERVICE_DIR / "res.py", SERVICE_DIR / "probes.py"]
SYNC_ROOT = SERVICE_DIR.parent.parent


def default_local_dir(kind: str) -> Path:
    """Machine-local folders (house rule of 2026-09-28: nothing a program keeps rewriting lives in ResilioSync).
    kind 'state': ~/Library/Application Support/LeaseResources, %LOCALAPPDATA%\\LeaseResources\\state,
    $XDG_STATE_HOME/LeaseResources; kind 'logs': ~/Library/Logs/LeaseResources, %LOCALAPPDATA%\\LeaseResources\\logs,
    $XDG_STATE_HOME/LeaseResources/logs."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "LeaseResources"
        return base / kind
    if sys.platform == "darwin":
        if kind == "logs":
            return Path.home() / "Library" / "Logs" / "LeaseResources"
        return Path.home() / "Library" / "Application Support" / "LeaseResources"
    base = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / "LeaseResources"
    return base / "logs" if kind == "logs" else base


DATA_DIR = Path(os.environ.get("LEASERES_DATA") or SERVICE_DIR)
LEASES_ROOT = DATA_DIR / "leases"                        # synced: only write-once cross-machine files (see README)
LOG_DIR = Path(os.environ.get("LEASERES_LOGS") or ((DATA_DIR / "logs") if os.environ.get("LEASERES_DATA")
                                                   else default_local_dir("logs")))
CONFIG_DIR = Path(os.environ.get("LEASERES_CONFIG") or (SERVICE_DIR / "config"))
LOCAL_DIR = Path(os.environ.get("LEASERES_LOCAL") or default_local_dir("state"))
OLD_LOCAL_DIR = Path.home() / ".claude" / "leaseresources"          # until 2026-09-28
THIS_MACHINE = ""                                                    # set by load_config()
REPORTS_VIA = ""                                                     # set by load_config(): "files" or "postman"
MAIL_PY = SYNC_ROOT / "Services" / "ClaudeMail" / "mail.py"
TASK_NAME = "LeaseResources Dispatcher"
LAUNCHD_LABEL = "com.goocy.leaseresources"
EXIT_CODE_CHANGED = 3
EXIT_ALREADY_RUNNING = 4
EXIT_STILL_RUNNING = 75                                # `res run --wait` / `res wait` stopped for a notice; the job runs on
ATTENTION_EVENTS = ["overrun", "duration", "stall"]    # notices that end the waiting while the job keeps running
END_EVENTS = ["finished", "failed", "lost", "start-failed", "refused"]      # notices after which nothing more comes
ACTIVE = ["granted", "starting", "running"]            # states whose need counts against capacity
OPEN = ["requested", "queued"] + ACTIVE
ENDED = ["finished", "failed", "lost", "expired", "released", "killed", "refused"]
SUFFIXES = [".order.json", ".run.json", ".extend.json", ".release.json"]


# ---------------------------------------------------------------- config, machine, time

def load_config() -> dict:
    """config/defaults.json overlaid with config/<machine>.json (lower case); each machine edits only its own file."""
    with open(CONFIG_DIR / "defaults.json", encoding="utf-8") as fh:
        config = json.load(fh)
    global THIS_MACHINE
    THIS_MACHINE = this_machine(config)
    own = CONFIG_DIR / (THIS_MACHINE.casefold() + ".json")
    if own.is_file():
        with open(own, encoding="utf-8") as fh:
            config.update(json.load(fh))
    global REPORTS_VIA
    assert config["reports_via"] in ("files", "postman"), "reports_via must be files or postman: " + repr(config["reports_via"])
    REPORTS_VIA = config["reports_via"]
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
    """This machine's lease records: local state, never synced. Another machine's are not readable here; its report
    (read_report) summarises them. The legacy synced folder is returned for other machines only for old files."""
    if machine == THIS_MACHINE:
        return LOCAL_DIR / "leases"
    return LEASES_ROOT / machine


def synced_dir(machine: str) -> Path:
    return LEASES_ROOT / machine


def inbox(machine: str, kind: str) -> Path:
    """Write-once files for another machine's dispatcher: requests/<id>.json, asks/<id>.<kind>.<stamp>.json. Each is
    written by the requesting machine only and deleted by the target once taken."""
    return synced_dir(machine) / kind


def file_stamp() -> str:
    """UTC, ending in Z, since 2026-10-27: a local-time stamp goes back with the clock (a time zone change, the DST
    fall-back), and then a new report sorted as the oldest and was deleted at once (Pulvermaar, 2026-10-26)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%y%m%d-%H%M%S-") + "%03dZ" % (now.microsecond // 1000)


REPORT_SLOTS = 5
CLOCK_AHEAD_SECONDS = 120
POSTMAN_SERVICES = {"reports": "LeaseResources", "beats": "LeaseResourcesBeats"}     # with reports_via "postman"


@functools.lru_cache(maxsize=None)
def spool():
    """This machine's postman spool (Services/Postman), where reports go and come from while reports_via is "postman".
    Imported only then, so a machine without a postman is untouched while the switch is off."""
    sys.path.insert(0, str(SYNC_ROOT / "Services" / "Postman"))
    import postman
    return postman.Spool(postman.spool_root(), postman.load_config())


def report_slots(machine: str, kind: str) -> list:
    """The fixed files of one kind the dispatcher publishes, slot-0.json to slot-4.json: "reports" (what other machines
    need about its leases) or "beats" (its heartbeat with the compliance summary). Since 2026-11-09 each one overwrites
    the oldest slot instead of being a new <stamp>.json with older ones deleted: every deletion left a deleted-file
    record that all Resilio peers keep and walk for good (some 80,000 from the reports of LeaseResources, ClaudeMail and
    the supervisors; Pektolith's sync stalled for 12 days in October). With five slots a file changes only every few
    beats, so no peer is still fetching it when it changes (the Resilio conflicts that made files write-once)."""
    assert kind in ("reports", "beats"), kind
    return [synced_dir(machine) / kind / ("slot-%d.json" % i) for i in range(REPORT_SLOTS)]


def file_time(path: Path) -> float:
    """A file's modification time; -inf for one that is not there (an empty slot is used first)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return float("-inf")


def report_entries(machine: str, kind: str) -> list:
    """[(path, report)] of one kind that parse, newest first: by the `updated` written inside (ISO with its offset, so
    a time zone change or the DST fall-back does not reorder them), then by file time; never by name. The <stamp>.json
    files of the code before slots are read the same way until their machine deletes them."""
    assert kind in ("reports", "beats"), kind
    folder = synced_dir(machine) / kind
    if not folder.is_dir():
        return []
    entries = []
    for path in folder.iterdir():
        if not path.name.endswith(".json") or path.name.startswith("."):
            continue
        mtime = file_time(path)
        report = read_json(path)                         # {} when gone, unreadable or not an object
        if not report:
            continue
        written = parse_iso(report.get("updated"))
        entries.append((mtime if written is None else written, mtime, path, report))
    entries.sort(key=lambda e: (e[0], e[1]), reverse=True)
    return [(path, report) for _, _, path, report in entries]


def report_names(machine: str, kind: str) -> list:
    """The files of one kind that parse, newest first (report_entries)."""
    return [path for path, _ in report_entries(machine, kind)]


def read_report(machine: str, kind: str) -> dict:
    """The newest dispatcher report (or beat) that parses; Resilio may still be delivering the very newest one. With
    reports_via "postman", the one the postman holds for the machine: no slots to choose from."""
    if REPORTS_VIA == "postman":
        found = [s["body"] for s in spool().states(POSTMAN_SERVICES[kind]) if s["origin"] == machine]
        assert len(found) <= 1, found
        return found[0] if found else {}
    assert REPORTS_VIA == "files", "load_config() has not set reports_via"
    entries = report_entries(machine, kind)
    return entries[0][1] if entries else {}


def read_published(machine: str) -> dict:
    """The newest report with the newest beat over it: the beat's `updated`, `pid` and `compliance` are the later ones.
    A dispatcher before 2026-10-31 wrote no beats; its reports carried all three."""
    return dict(read_report(machine, "reports"), **read_report(machine, "beats"))


def write_report(machine: str, kind: str, report: dict) -> bool:
    """Overwrites the oldest of the five slots of the kind (report_slots says why) through a temp name and a rename,
    then deletes any other file there, the <stamp>.json of the code before; after that nothing is ever deleted. Slots
    with a file time more than CLOCK_AHEAD_SECONDS in the future were written while this clock ran ahead; readers would
    take them as newest for as long, so the report goes into all of them at once. A slot Resilio holds past the
    retries (Windows) is skipped for the next oldest. False, and logged, when no slot took it.
    With reports_via "postman" the report goes to the postman instead, and no slot file is written."""
    if REPORTS_VIA == "postman":
        assert spool().me() == machine, "the postman runs as " + spool().me() + ", LeaseResources as " + machine
        spool().publish(POSTMAN_SERVICES[kind], report)
        return True
    assert REPORTS_VIA == "files", "load_config() has not set reports_via"
    now = time.time()
    slots = sorted(report_slots(machine, kind), key=file_time)     # oldest first; an empty slot before any other
    ahead = [p for p in slots if file_time(p) > now + CLOCK_AHEAD_SECONDS]
    written, failed = [], []
    for path in (ahead or slots):
        try:
            write_json(path, report)
            written.append(path)
        except OSError as exc:
            failed.append(path.name + ": " + str(exc))
            continue
        if not ahead:
            break
    if not written:
        log(machine, "WARN " + kind + " not written, every slot refused: " + "; ".join(failed))
        return False
    keep = report_slots(machine, kind)
    for old in (synced_dir(machine) / kind).iterdir():
        if old.name.endswith(".json") and not old.name.startswith(".") and old not in keep:
            try:
                old.unlink()
            except OSError:
                pass
    return True


def report_lease(machine: str, ident: str) -> dict:
    return (read_report(machine, "reports").get("leases") or {}).get(ident) or {}


def lease_path(machine: str, ident: str) -> Path:
    return machine_dir(machine) / (ident + ".json")


def side_path(machine: str, ident: str, suffix: str) -> Path:
    if suffix.startswith(".log"):
        return LOG_DIR / "leases" / (ident + suffix)
    return machine_dir(machine) / (ident + suffix)


def seed_local_state(machine: str) -> None:
    """Once per machine: copy the state the pre-2026-09-28 code kept in the synced folder (lease records, runner and
    order files, trials, history, compliance record, job logs) and in ~/.claude/leaseresources (learned capacities,
    disk map) into the local folders. The synced copies are left alone; the user deletes them once every machine runs
    this code."""
    marker = LOCAL_DIR / "seeded.json"
    if marker.exists():
        return
    counts = collections.Counter()
    old = LEASES_ROOT / machine
    new = LOCAL_DIR / "leases"
    try:
        new.mkdir(parents=True, exist_ok=True)
        if old.is_dir():
            for path in old.iterdir():
                if not path.is_file() or path.name == "dispatcher.json" or path.name.startswith("."):
                    continue
                if path.name.endswith(".log") or path.name.endswith(".log.1"):
                    target = LOG_DIR / "leases" / path.name
                else:
                    target = new / path.name
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    counts["files"] += 1
            for sub, target_dir in [("trials", LOCAL_DIR / "trials"), ("done", new / "done")]:
                if (old / sub).is_dir():
                    target_dir.mkdir(parents=True, exist_ok=True)
                    for path in (old / sub).iterdir():
                        if path.is_file() and not (target_dir / path.name).exists():
                            shutil.copy2(path, target_dir / path.name)
                            counts[sub] += 1
        if OLD_LOCAL_DIR != LOCAL_DIR:
            if (OLD_LOCAL_DIR / "disks.json").is_file() and not (LOCAL_DIR / "disks.json").exists():
                shutil.copy2(OLD_LOCAL_DIR / "disks.json", LOCAL_DIR / "disks.json")
                counts["local"] += 1
            old_learned = read_json(OLD_LOCAL_DIR / "learned_peaks.json")
            if old_learned:                       # merged, highest per dimension: new code may already have learned some
                learned = read_json(LOCAL_DIR / "learned_peaks.json")
                for dim, value in old_learned.items():
                    learned[dim] = max(float(value), float(learned.get(dim, 0)))
                write_json(LOCAL_DIR / "learned_peaks.json", learned)
                counts["learned"] += len(old_learned)
    except OSError as exc:
        log(machine, "WARN seeding local state: " + str(exc))
        return
    write_json(marker, {"at": now_iso(), "from": str(old), "counts": dict(counts)})
    if counts:
        log(machine, "seeded local state from the synced copy: " + json.dumps(dict(counts)))


def is_lease_file(name: str) -> bool:
    return name.endswith(".json") and not any(name.endswith(s) for s in SUFFIXES) and name not in \
        ("dispatcher.json", "history.json") and not name.startswith(".")


PARSED = {}     # folder -> {file name: ((st_mtime_ns, st_size), parsed object)}, as read_folder last saw the folder


def read_folder(folder: Path, wanted) -> list:
    """Copies of the JSON objects in the files of folder whose names `wanted` accepts. A file is parsed again only when
    its modification time or size changed since the last call in this process, and the dispatcher lists its lease
    records every second (all of them parsed every second until 2026-10-31). The listing's figures are those of the
    whole file on Windows as well, because every writer replaces a file whole (write_json renames a closed temporary
    file over it). An object that comes back empty (unreadable, or replaced while it was read) is read again next time.
    Copies, because the dispatcher changes the leases it is handed before it saves them."""
    seen = PARSED.get(folder, {})
    now = {}
    with os.scandir(folder) as entries:
        for entry in entries:
            if not wanted(entry.name) or not entry.is_file():
                continue
            stat = entry.stat()
            key = (stat.st_mtime_ns, stat.st_size)
            if entry.name in seen and seen[entry.name][0] == key:
                now[entry.name] = seen[entry.name]
                continue
            data = read_json(Path(entry.path))
            if data:
                now[entry.name] = (key, data)
    PARSED[folder] = now
    return [copy.deepcopy(data) for _, data in now.values()]


def all_leases(machine: str) -> list:
    folder = machine_dir(machine)
    if not folder.is_dir():
        return []
    found = [data for data in read_folder(folder, is_lease_file) if data.get("id")]
    return sorted(found, key=lambda d: (d.get("requested", ""), d.get("id", "")))


def log(machine: str, message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now()
        path = LOG_DIR / ("leaseresources." + machine.casefold() + "." + now.strftime("%y%m%d") + ".log")
        try:
            if path.stat().st_size > 20e6:              # a runaway loop must not fill the disk; one day's log is capped
                return
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(now.strftime("%Y-%m-%d %H:%M:%S") + " " + message + "\n")
    except OSError:
        pass


def new_id(prefix: str, machine: str, folder: Path) -> str:
    """<prefix><yymmdd-HHMMSS><mmm>-<Machine>: milliseconds included, because a request sent to another machine leaves
    the inbox once taken, so the folder alone cannot tell a new id from one used a moment ago (two requests in one
    second got one id, 2026-09-28). A local record of ids sent to other machines covers the rest."""
    now = datetime.datetime.now()
    base = prefix + now.strftime("%y%m%d-%H%M%S") + "%03d" % (now.microsecond // 1000) + "-" + machine
    sent = LOCAL_DIR / "sent_ids.json"
    used = read_json(sent)
    ident, n = base, 1
    while (folder / (ident + ".json")).exists() or ident in used:
        n += 1
        ident = base + "-" + str(n)
    if prefix == "L":
        used = {k: v for k, v in used.items() if age_of(v) < 86400}
        used[ident] = now_iso()
        write_json(sent, used)
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

RESTORE_TOLERANCE = 5.0                                # Macs: seconds a recorded key may be off the kernel's create time


def clock_shift() -> float:
    """The seconds psutil adds to every create_time() it returns in this process. psutil 7.1 and later, on macOS and
    the BSDs, moves create times by however far the system's boot time has moved since psutil was imported (the clock
    being corrected, mostly after sleep). The kernel's own value never changes, so a dispatcher that had run for days
    and a runner started a second ago disagreed about the runner by two whole seconds, and four jobs were reported
    lost while they ran (Altiplano, 2026-10-07 and -08). Zero on Windows, and with a psutil that does not shift."""
    adjust = getattr(psutil._psplatform, "adjust_proc_create_time", None)
    if adjust is None:
        return 0.0
    try:
        return float(adjust(0.0))
    except (psutil.Error, OSError, TypeError, ValueError):
        return 0.0


def shifts_create_times() -> bool:
    """Whether this psutil moves create times at all (the Macs: yes; Windows: no)."""
    return getattr(psutil._psplatform, "adjust_proc_create_time", None) is not None


def created(proc) -> float:
    """When the kernel says the process started: the same number in every process, for the process's whole life. Use
    it wherever a create time is recorded for, or compared with, another process's reading, never proc.create_time()
    (see clock_shift). Read through a new psutil.Process, because an old one keeps the first create time it returned,
    with the shift of that moment."""
    return psutil.Process(proc.pid).create_time() - clock_shift()


def process(pid, create_time=None, tolerance: float = 1.0):
    try:
        proc = psutil.Process(int(pid))
        if create_time is not None and abs(created(proc) - float(create_time)) > tolerance:
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
    named = os.environ.get("CLAUDE_PID", "").strip()
    try:
        for parent in psutil.Process().parents():
            try:
                if parent.name().casefold() in names or (named.isdigit() and parent.pid == int(named)):
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
            shown = []
            for arg in argv[1:]:
                path_like = "/" in arg or chr(92) in arg
                shown.append(".../" + arg.replace(chr(92), "/").rpartition("/")[2] if path_like and len(arg) > 40 else arg)
            argv = [Path(argv[0].replace(chr(92), "/")).name] + shown
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
    """The desk that gets this lease's mail: --desk, else the calling session's desk from `mail.py whoami --desk`, which
    prints Machine:Desk alone. Parsing the human-readable `whoami` cut desk names at their first space (2026-09-30,
    "Stahlwerk:Spaced Desk Name" became "Stahlwerk:Spaced", and its lease mail went to General)."""
    if given:
        return given.strip()
    run = subprocess.run([sys.executable, "-B", mail_script(config), "whoami", "--desk"], capture_output=True,
                         text=True, timeout=30, creationflags=probes.no_window())
    desk = run.stdout.strip()
    # No fallback to Machine:General since 2026-10-30: under ClaudeMail v4 that desk is Home, where every notice would
    # open a new session. A desk that cannot be told is an error the requester sees.
    if not (run.returncode == 0 and ":" in desk and len(desk.splitlines()) == 1):
        raise SystemExit("mail.py whoami --desk did not name this session's desk (exit " + str(run.returncode) + "): "
                         + " ".join((run.stdout + run.stderr).split())[-300:] + "\nGive --desk Machine:Desk.")
    return desk


def desk_known(config: dict, desk: str) -> bool:
    """Whether ClaudeMail has this Machine:Desk (its folder under mail/<Machine>/desks/, case and a leading 'yyMMdd '
    date stamp ignored, as mail.py matches it). ClaudeMail refuses mail to any other desk."""
    machine, _, name = desk.partition(":")
    name = name.strip()
    if len(name) > 7 and name[:6].isdigit() and name[6] == " ":
        name = name[7:].strip()
    root = Path(mail_script(config)).parent / "mail"
    try:
        for m in root.iterdir():
            if m.name.casefold() == machine.strip().casefold():
                return any(d.is_dir() and d.name.casefold() == name.casefold() for d in (m / "desks").iterdir())
    except OSError:
        pass
    return False


def attached_path(ident: str) -> Path:
    return LOCAL_DIR / "attached" / (ident + ".json")


def notice_dir(ident: str) -> Path:
    return LOCAL_DIR / "notices" / ident


def attached_waiter(ident: str):
    """The live process waiting on this lease (`res run --wait` or `res wait`), or None. While there is one, the
    dispatcher writes the lease's notices into notice_dir for it instead of mailing them. A file left by a waiter that
    was killed is harmless: the pid must still belong to a process with the recorded create time."""
    record = read_json(attached_path(ident))
    if not record.get("pid") or record.get("create_time") is None:
        return None
    return process(record.get("pid"), record.get("create_time"))


def attach(ident: str) -> None:
    """Register this process as the waiter of a lease, until it exits."""
    me = psutil.Process()
    write_json(attached_path(ident), {"lease": ident, "pid": me.pid, "create_time": created(me), "attached": now_iso()})
    atexit.register(detach, ident)


def detach(ident: str) -> None:
    path = attached_path(ident)
    if read_json(path).get("pid") == os.getpid():
        try:
            path.unlink()
        except OSError:
            pass


def take_notices(ident: str) -> list:
    """Claim the notices the dispatcher wrote for this waiter, oldest first: (path, notice) pairs. Claiming renames the
    file to <name>.<pid>, so the dispatcher, which mails what no live waiter claimed, and this process never both take
    one notice. The caller deletes each file once it has printed it."""
    folder = notice_dir(ident)
    try:
        names = sorted(p.name for p in folder.iterdir() if p.name.endswith(".json"))
    except OSError:
        return []
    taken = []
    for name in names:
        mine = folder / (name + "." + str(os.getpid()))
        try:
            probes.replace_retrying(folder / name, mine)
        except OSError:
            continue                                    # the dispatcher took it for mail in this very moment
        taken.append((mine, read_json(mine)))
    return taken


def send_mail(config: dict, machine: str, desk: str, subject: str, body: str, for_session: str = "") -> bool:
    """Mail to the owner desk; with for_session, only that session of the desk takes it while ClaudeMail can reach it
    (sibling sessions on one desk took each other's lease notices, Altiplano 2026-09-30)."""
    beat_if_dispatcher()
    outbox = LOCAL_DIR / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / (datetime.datetime.now().strftime("%y%m%d-%H%M%S-%f") + ".md")
    path.write_text(body, encoding="utf-8")
    # One target since 2026-10-30. The second try on the bare machine without --for-session would, under ClaudeMail v4,
    # open a new Home session for every notice that the first try could not place; ClaudeMail itself now tells a
    # sender's session when mail waits, and this failure is notified below.
    owner = ["--for-session", for_session] if for_session else []
    try:
        run = subprocess.run([sys.executable, "-B", mail_script(config), "send", "--to", desk, "--subject", subject,
                              "--body-file", str(path), "--to-own-desk"] + owner, capture_output=True, text=True, timeout=60,
                             cwd=str(SERVICE_DIR), env=mail_env(), creationflags=probes.no_window())
        ok = run.returncode == 0
        detail = " ".join((run.stdout + run.stderr).split())[:300]
    except (OSError, subprocess.SubprocessError) as exc:
        ok, detail = False, str(exc)
    log(machine, ("mailed " if ok else "WARN mail failed ") + desk + ": " + subject + " (ClaudeMail: " + detail + ")")
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
    beat_if_dispatcher()
    log(machine, "notified: " + title + " - " + " ".join(body.split()))
    if not config.get("notify", True):
        return
    try:
        if sys.platform == "win32":
            encoded = base64.b64encode(WINDOWS_TOAST.encode("utf-16-le")).decode("ascii")
            env = dict(os.environ, LEASERES_TITLE=title, LEASERES_BODY=body)
            run = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], env=env,
                                 capture_output=True, timeout=30, creationflags=probes.no_window())
            if run.returncode != 0:
                log(machine, "WARN notification failed: powershell exit " + str(run.returncode) + ": "
                    + " ".join(run.stderr.decode("utf-8", "replace").split())[:300])
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


REPORT_SECONDS = 600       # a beat every 10 minutes; a report only on a change (since 2026-10-31; until then a report on
                           # every change and every 10 minutes, and every minute until 2026-10-30, which left some 40,000
                           # deleted files in each reports folder for every Resilio peer to walk)
FRESH_SECONDS = 1500       # a dispatcher counts as reporting while its newest beat is younger than this


def dispatcher_state(machine: str, config: dict) -> tuple:
    """(reporting, its newest report). This machine's dispatcher counts as running while its local heartbeat is under
    two minutes old, as take_lock trusts it; another machine's while its beat is younger than FRESH_SECONDS."""
    report = read_published(machine)
    if machine == this_machine(config):
        beat = local_heartbeat()
        return beat.is_file() and age_of(read_json(beat).get("updated")) < 120, report
    fresh = age_of(report.get("updated")) < FRESH_SECONDS
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
        if dim in busy and sys.platform != "win32":     # Windows' per-disk read/write times stay 0 (Stahlwerk): not shown
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
    width = max([len(d) for d in list(use) + list(meter.system_only())] + [10]) + 2
    for dim in sorted(use):
        if use[dim] > 0:
            print("  " + dim.ljust(width) + human(use[dim], kind_of(dim)) + "   (machine: " + human(system.get(dim, 0.0), kind_of(dim)) + ")")
    for dim in sorted(meter.system_only()):
        print("  " + dim.ljust(width) + "not measurable per process (machine: " + human(system.get(dim, 0.0), kind_of(dim)) + ")")
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
    return LOCAL_DIR / "trials"


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


WIN_CREATE_SUSPENDED = 0x4


TRIAL_CUT_OFF = 3                                       # exit code of `res trial` when the command needed a lease


def trial_exit(code: int = 0) -> None:
    """End a trial: exit code 0 when the command finished inside the limit, TRIAL_CUT_OFF (3) when it was cut off and
    needs a lease. On Windows the process exits hard once its output is flushed, so a probe's hung native thread (psutil
    did hang) cannot keep res alive."""
    sys.stdout.flush()
    sys.stderr.flush()
    if sys.platform == "win32":
        os._exit(code)
    if code:
        sys.exit(code)


def _win_kernel():
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    return ctypes, k32


def job_limit_structure(ctypes):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION (144 bytes on 64-bit Windows)."""
    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]

    class Io(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ["ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount"]]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]
    return Extended


def win_job_for(child):
    """Windows: put a child that was started suspended into a new Job Object with KILL_ON_JOB_CLOSE, then resume it.
    Every process it starts inherits the job (breaking away is not allowed), so TerminateJobObject, or res dying,
    ends the whole tree. Returns the job handle, or None if any step failed (the child is resumed regardless and
    the psutil tree kill remains)."""
    job = None
    try:
        ctypes, k32 = _win_kernel()
        job = k32.CreateJobObjectW(None, None)
        if job:
            info = job_limit_structure(ctypes)()
            info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            ok = ok and k32.AssignProcessToJobObject(job, ctypes.c_void_p(int(child._handle)))
            if not ok:
                k32.CloseHandle(job)
                job = None
    except Exception:
        job = None
    try:
        import ctypes as _ct
        ntdll = _ct.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [_ct.c_void_p]
        ntdll.NtResumeProcess(_ct.c_void_p(int(child._handle)))
    except Exception:
        pass
    return job


def win_job_kill(job) -> None:
    try:
        ctypes, k32 = _win_kernel()
        k32.TerminateJobObject(job, 1)
    except Exception:
        pass


def win_close(job) -> None:
    try:
        ctypes, k32 = _win_kernel()
        k32.CloseHandle(job)
    except Exception:
        pass


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
    job = None
    out_path.write_bytes(b"")
    if True:
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": os.getcwd(),
                  "env": dict(os.environ, PYTHONUNBUFFERED="1")}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) | WIN_CREATE_SUSPENDED
        else:
            kwargs["start_new_session"] = True
        try:
            child = subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            raise SystemExit("Cannot start " + argv[0] + ": " + str(exc))
        if sys.platform == "win32":
            job = win_job_for(child)                  # assigned while suspended, so no child escapes it; then resumed
    threading.Thread(target=pump_output, args=(child.stdout, out_path, int(num(config, "trial_output_max_bytes", 10e6)), False),
                     daemon=True).start()
    t0 = time.monotonic()
    root = process(child.pid)
    seen = {}
    probe_errors = set()
    peaks = collections.defaultdict(float)
    sums = collections.defaultdict(float)
    samples = 0
    last_session_scan = 0.0
    exit_code = None
    exit_seen = None
    deadline = threading.Event()
    killed_pids = set()

    def kill_everything() -> list:
        """The one kill path, for the watchdog and the normal end alike: the Windows job, the POSIX process group,
        every process ever seen in the tree."""
        targets = [p for p in list(seen.values()) if alive(p)]
        killed_pids.update(p.pid for p in targets)
        if job is not None:
            win_job_kill(job)
        return kill_tree(targets, group_leader=child.pid)

    def watchdog():
        deadline.set()
        kill_everything()

    # The 10-second guarantee does not depend on the sampling loop: a timer thread kills at the limit even if a
    # probe call hangs (psutil on Windows did, 2026-09-27).
    timer = threading.Timer(limit, watchdog)
    timer.daemon = True
    timer.start()
    while True:
        elapsed = time.monotonic() - t0
        if exit_code is None:
            exit_code = child.poll()
            if exit_code is not None and not deadline.is_set():
                exit_seen = time.monotonic() - t0
        if deadline.is_set() or elapsed >= limit:
            break
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
        probe_errors.update(meter.errors)
        use = per.get("trial", {})
        for dim in system_only:
            use[dim] = max(0.0, system.get(dim, 0.0) - baseline.get(dim, 0.0))
        for dim, value in use.items():
            peaks[dim] = max(peaks[dim], value)
            sums[dim] += value
        samples += 1
        if exit_code is not None:
            leftovers = [p for p in seen.values() if p.pid != child.pid and alive(p)]
            if not leftovers:
                break
        deadline.wait(step)                             # wakes at once when the watchdog fires
    timer.cancel()
    elapsed = min(time.monotonic() - t0, limit + 0.5) if deadline.is_set() else time.monotonic() - t0
    finished = exit_seen is not None
    still = kill_everything()
    killed = sorted(killed_pids)
    if job is not None:
        win_close(job)
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
              "attribution": {d: "machine delta over baseline" for d in system_only}, "probe_errors": sorted(probe_errors)}
    write_json(folder / (ident + ".json"), record)
    for error in sorted(probe_errors):
        print("WARN probe failed, its dimensions are missing from some samples of this profile: " + error)
    if args.json:
        print(json.dumps(record, indent=1))
        trial_exit(0 if finished else TRIAL_CUT_OFF)
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
    width = max([len(d) for d in profile] + [10]) + 2
    for dim in sorted(profile, key=lambda d: (list(dims).index(d) if d in dims else 99, d)):
        mark = "  (machine delta, not per process)" if dim in system_only else ""
        print("  " + dim.ljust(width) + human(profile[dim]["peak"], kind_of(dim)) + " / " + human(profile[dim]["mean"], kind_of(dim)) + mark)
    if not finished:
        print("For the full run: " + res_command() + " request --profile " + ident + " --for <duration> --why \"...\""
              + "   (exit code " + str(TRIAL_CUT_OFF) + ": cut off, lease needed)")
    trial_exit(0 if finished else TRIAL_CUT_OFF)


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
    for folder in [LOCAL_DIR / "trials"] + sorted(LEASES_ROOT.glob("*/trials")):
        path = folder / (ident + ".json")
        if path.is_file():
            return read_json(path)
    return None


def wait_for_answer(path: Path, keys: tuple, seconds: float) -> dict:
    deadline = time.monotonic() + seconds
    data = read_json(path)
    while time.monotonic() < deadline:
        data = read_json(path) or data                  # a transient empty read (Windows replace) keeps the last one
        if data and keys[0](data):
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
    folder = machine_dir(target) if target == here else inbox(target, "requests")
    folder.mkdir(parents=True, exist_ok=True)
    ident = new_id("L", here, folder)
    lease = {"id": ident, "machine": target, "from_machine": here, "desk": owner_desk(config, args.desk),
             "session": os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "why": args.why or "", "need": need,
             "duration": duration, "state": "requested", "requested": now_iso(), "trial": args.profile or "",
             "command": command, "command_key": command_key(command) if command else "", "margin": margin}
    if not desk_known(config, lease["desk"]):
        # The user's ruling (2026-09-30, relayed by Stahlwerk): an unknown desk warns the sender instead of its mail
        # going quietly to General. The request stands. Since 2026-10-30 its mail is not sent anywhere else: ClaudeMail
        # refuses it, and the failure is a desktop notification and a log line on this machine.
        print("Warning: ClaudeMail has no desk " + lease["desk"] + ", so this lease's mail will be refused, and only a"
              " desktop notification on " + here + " will say so. If that is wrong, `res release " + ident
              + "` and request again with --desk Machine:Desk (`mail.py sessions` lists the desks).")
    path = lease_path(target, ident) if target == here else folder / (ident + ".json")
    write_json(path, lease)                       # to another machine: a write-once file its dispatcher takes and deletes
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
    lease = read_json(path) if target == here else report_lease(target, args.id)
    if not lease and target == here:
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
    if target != here:
        write_json(inbox(target, "asks") / (args.id + ".extend." + file_stamp() + ".json"), ask)
    else:
        write_json(side_path(target, args.id, ".extend.json"), ask)
    if target != here:
        print("Extension of " + args.id + " sent to " + target + "; the answer comes by mail to "
              + (lease.get("desk") or "the lease's desk"))
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
    lease = read_json(path) if target == here else report_lease(target, args.id)
    if not lease and target == here:
        raise SystemExit("No lease " + args.id + " on " + target)
    if lease.get("state") in ENDED:
        print(args.id + " is already " + lease.get("state"))
        return
    if lease.get("state") in ("running", "starting") and not args.kill:
        raise SystemExit(args.id + " is running; the job keeps its lease until it ends. Add --kill to stop its whole process tree.")
    ask = {"asked": now_iso(), "kill": bool(args.kill), "from_machine": here}
    if target != here:
        write_json(inbox(target, "asks") / (args.id + ".release." + file_stamp() + ".json"), ask)
    else:
        write_json(side_path(target, args.id, ".release.json"), ask)
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
    # Double spawn: a short-lived intermediate starts the runner and exits at once, so the runner never has a live
    # ancestor in the caller's tree. Detaching flags alone were not enough: stopping a Claude tool task kills the tree
    # through live parent pids, and `res run --wait` stayed the runner's parent (two leases lost on Stahlwerk).
    argv = [str(python), "-B", str(SCRIPT), "_launch", machine, ident, str(launch)]
    middle = popen_detached(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=str(SERVICE_DIR), env=dict(os.environ, LEASERES_MACHINE=machine))
    try:
        middle.wait(30)
    except subprocess.TimeoutExpired:
        pass
    return middle


def cmd_launch(args, config: dict) -> None:
    """The intermediate of the double spawn: start the runner detached, then exit at once."""
    python = venv_python(windowless=True)
    if not python.exists():
        python = Path(sys.executable)
    argv = [str(python), "-B", str(SCRIPT), "_runner", args.machine, args.ident, args.launch]
    popen_detached(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   cwd=str(SERVICE_DIR), env=dict(os.environ, LEASERES_MACHINE=args.machine))


def cmd_run(args, config: dict) -> None:
    argv = list(args.command or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise SystemExit("Give the program after --, e.g. res run --lease L... -- python job.py")
    here = this_machine(config)
    target = canonical_machine(args.on, config) if args.on else here
    path = lease_path(target, args.lease)
    if target == here:
        lease = read_json(path)
        if not lease:
            raise SystemExit("No lease " + args.lease + " on " + target + " (" + str(path) + ")")
    else:
        # The report lags a new request by Resilio's time both ways; until it lists the lease, the request itself still
        # lies in the target's inbox and names the desk (without it the note below read "and mails  when it starts").
        lease = report_lease(target, args.lease) or read_json(inbox(target, "requests") / (args.lease + ".json")) \
            or {"state": "requested", "desk": ""}
    state = lease.get("state")
    if state == "requested" and target == here:
        # Not answered yet (a busy dispatcher): wait for the answer, since a granted lease starts here directly (any
        # program, D-260927-7) while an unanswered one would become a queued run under the program rule.
        lease = wait_for_answer(path, (lambda d: d.get("state") != "requested",), num(config, "answer_wait_seconds", 6) * 2)
        state = lease.get("state")
    if state in ENDED:
        raise SystemExit(args.lease + " is " + state + "; request a new lease.")
    if target == here and side_path(target, args.lease, ".order.json").exists():
        raise SystemExit(args.lease + " already has a run order; one lease, one run.")
    waiting = bool(args.wait) and target == here
    if waiting:
        refuse_second_waiter(args.lease)
        attach(args.lease)                              # before anything starts, so the first notice already comes here
    elif args.wait:
        print("--wait works only for a lease on this machine; " + target + "'s dispatcher reports by mail.")
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
    if target == here:
        write_json(side_path(target, args.lease, ".order.json"), order)
    else:
        write_json(inbox(target, "asks") / (args.lease + ".order." + file_stamp() + ".json"), order)
    log(here, "run order " + args.lease + " on " + target + " (" + order["start"] + "): " + " ".join(argv))
    history = (read_json(machine_dir(target) / "history.json").get(command_key(argv)) or []) if target == here else []
    if history:
        print("History: " + history_note(history, float(lease.get("duration", 0))))
    if not local_start:
        where = "on " + target + " " if target != here else ""
        print("Run order written; the dispatcher " + where + "starts it when the lease is granted (now: " + str(state)
              + ")" + ("." if waiting else ", and mails " + (lease.get("desk") or "the lease's desk") + " when it starts and ends.")
              + "\n  " + str(path))
        if waiting:
            wait_on(target, args.lease, config, lease.get("desk", ""))
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
    if not waiting:
        print("The dispatcher mails " + lease.get("desk", "") + " when it ends, stalls, overruns or runs past "
              + human_duration(lease.get("duration")) + ". No need to poll.")
        print("Mail to a desk can be taken by another session of that desk. To have this session woken instead, with no"
              " mail, start the job as a background command with --wait: " + res_command() + " run --lease <id> --wait --"
              " <program>. For this job, now, as a background command: " + res_command() + " wait " + args.lease)
        return
    wait_on(target, args.lease, config, lease.get("desk", ""))


def refuse_second_waiter(ident: str) -> None:
    other = attached_waiter(ident)
    if other is not None and other.pid != os.getpid():
        raise SystemExit(ident + " already has a waiting process (pid " + str(other.pid) + "); its notices go there. One"
                         + " lease, one waiter.")


def wait_on(machine: str, ident: str, config: dict, desk: str, from_end: bool = False, grace=None) -> None:
    """Wait on a lease of this machine as its attached waiter (the caller has called attach): stream the job's log and
    print the notices the dispatcher writes for this process instead of mailing them. Never returns. A notice that
    needs attention while the job runs on (overrun, past its duration, stalled) ends the waiting with
    EXIT_STILL_RUNNING, because the exit is what wakes a Claude session that started this as a background command. The
    notice of the end exits with the job's exit code (1 if there is none). Should the job or the lease end and no notice
    come within the grace period (the dispatcher is down), it exits the same way and the dispatcher mails later."""
    run_path = side_path(machine, ident, ".run.json")
    log_path = side_path(machine, ident, ".log")
    if grace is None:
        grace = 3 * (num(config, "sample_seconds", 5) + num(config, "tick_seconds", 1))
    shown = 0
    if from_end:
        try:
            shown = log_path.stat().st_size
        except OSError:
            pass

    def stop(signum, frame):
        sys.exit(128 + signum)                          # through atexit, so the attached record goes

    for name in ["SIGTERM", "SIGHUP", "SIGBREAK"]:
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), stop)
            except (OSError, ValueError):
                pass

    def stream() -> None:
        nonlocal shown
        try:
            size = log_path.stat().st_size
            if size < shown:
                shown = 0                               # the log rotated
            if size > shown:
                with open(log_path, "rb") as fh:
                    fh.seek(shown)
                    sys.stdout.write(fh.read(size - shown).decode("utf-8", "replace"))
                    sys.stdout.flush()
                shown = size
        except OSError:
            pass

    def leave(note: str = "") -> None:
        stream()
        run = read_json(run_path)
        if run.get("ended"):
            print("\nEnded with exit code " + str(run.get("exit")) + note)
        else:
            print("\n" + ident + " is " + str(read_json(lease_path(machine, ident)).get("state")) + note)
        sys.exit(run.get("exit") if isinstance(run.get("exit"), int) else 1)

    print("Waiting on " + ident + ": the job's output and the dispatcher's notices (ended, stalled, overrun, past its"
          " duration) arrive here. " + (desk or "The owner desk") + " gets mail only if this waiting process dies."
          " Stopping it stops only the waiting, not the job.", flush=True)
    ended_at = None
    try:
        while True:
            if read_json(attached_path(ident)).get("pid") != os.getpid():
                attach(ident)                           # the record was cleared away; without it the notices are mailed
            run = read_json(run_path)
            over = bool(run.get("ended")) or read_json(lease_path(machine, ident)).get("state") in ENDED
            stream()
            events = []
            for path, notice in take_notices(ident):
                print("\n[res] " + str(notice.get("subject", "")) + "\n" + str(notice.get("body", "")), flush=True)
                try:
                    path.unlink()                       # only now: a waiter that dies before printing leaves it to be mailed
                except OSError:
                    pass
                events.append(notice.get("event", ""))
            if any(e in END_EVENTS for e in events):
                leave()
            if any(e in ATTENTION_EVENTS for e in events) and not over:
                print("\nThe job is STILL RUNNING. This waiting process stops here so that its session sees the notice above;"
                      " re-attach with: " + res_command() + " wait " + ident, flush=True)
                sys.exit(EXIT_STILL_RUNNING)
            if over:
                if ended_at is None:
                    ended_at = time.monotonic()
                if time.monotonic() - ended_at >= grace:
                    leave(" (no notice of the end from the dispatcher within " + human_duration(grace) + "; if it is not"
                          " running, it mails " + (desk or "the owner desk") + " when it runs again)" if grace else "")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopped waiting; the job is not touched. Re-attach with: " + res_command() + " wait " + ident)
        sys.exit(130)


def cmd_wait(args, config: dict) -> None:
    """Attach to a lease of this machine that already has a waiter no more (or never had one): from now on its notices
    arrive in this process's output instead of by mail."""
    here = this_machine(config)
    lease = read_json(lease_path(here, args.id))
    if not lease:
        raise SystemExit("No lease " + args.id + " on " + here + ". res wait attaches only to a lease on this machine; a"
                         + " lease on another machine reports by mail (res status " + args.id + " --on MACHINE shows it).")
    if lease.get("from_machine") != here:
        raise SystemExit(args.id + " was requested from " + str(lease.get("from_machine")) + "; its notices go to "
                         + lease.get("desk", "") + " by mail, and res wait attaches only to leases requested on this machine.")
    refuse_second_waiter(args.id)
    attach(args.id)
    grace = 3 * (num(config, "sample_seconds", 5) + num(config, "tick_seconds", 1))
    if lease.get("state") in ENDED and age_of(lease.get("ended") or lease.get("answered")) > grace:
        grace = 0                                       # long over: print what is still pending for it and leave
    print(args.id + " is " + str(lease.get("state")) + "; log " + str(side_path(here, args.id, ".log")))
    wait_on(here, args.id, config, lease.get("desk", ""), from_end=True, grace=grace)


def pump_output(stream, path: Path, cap: int, rotate: bool) -> None:
    """Copy a child's output into path with a size cap, so no job or trial can fill a disk (or the synced folder).
    rotate=True (leased jobs): when the file reaches cap it becomes <path>.1, replacing an older one, and writing starts
    again, so at most about 2 x cap is kept, the newest output always included. rotate=False (trials): after cap bytes
    the rest is read and discarded, with a note. Reading never stops, so the child never blocks on a full pipe."""
    written = 0
    truncated = False
    out = open(path, "ab")
    try:
        written = out.tell()
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not chunk:
                break
            if written + len(chunk) > cap:
                if rotate:
                    out.close()
                    try:
                        probes.replace_retrying(path, path.with_name(path.name + ".1"))
                    except OSError:
                        pass
                    out = open(path, "ab")
                    out.write(("[res] " + now_iso() + " log reached " + str(cap // 1000000) + " MB; the earlier part is in "
                               + path.name + ".1\n").encode("utf-8"))
                    written = out.tell()
                else:
                    if not truncated:
                        out.write(("\n[res] output cut at " + str(cap // 1000000) + " MB; the rest was discarded\n").encode("utf-8"))
                        out.flush()
                        truncated = True
                    continue
            out.write(chunk)
            out.flush()
            written += len(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            out.close()
        except OSError:
            pass


def wait_with_peak(child) -> tuple:
    """Wait for the leased program and return (exit code, its peak resident bytes), so even a job shorter than one
    dispatcher sample has a RAM peak: POSIX from wait4's rusage (bytes on macOS, KiB on Linux), Windows from the
    process's peak working set, read while it runs."""
    if sys.platform != "win32" and hasattr(os, "wait4"):
        try:
            _, status, usage = os.wait4(child.pid, 0)
            child.returncode = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)
            scale = 1 if sys.platform == "darwin" else 1024
            return child.returncode, int(usage.ru_maxrss) * scale
        except (OSError, ChildProcessError):
            return child.wait(), None
    peak = 0
    try:
        proc = psutil.Process(child.pid)
    except psutil.Error:
        proc = None
    while True:
        try:
            return child.wait(timeout=0.5), peak or None
        except subprocess.TimeoutExpired:
            pass
        try:
            info = proc.memory_info() if proc else None
            peak = max(peak, int(getattr(info, "peak_wset", 0) or getattr(info, "rss", 0) or 0))
        except psutil.Error:
            pass


def cmd_runner(args, config: dict) -> None:
    """The detached parent of a leased job: starts it with unbuffered output into the log, records pid and exit code."""
    machine, ident = args.machine, args.ident
    launch = Path(args.launch)
    run_path = side_path(machine, ident, ".run.json")
    me = psutil.Process()
    run = {"runner_pid": me.pid, "runner_create_time": created(me), "started": now_iso()}
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
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as out:
            out.write(("[res] " + now_iso() + " starting on " + machine + ": " + " ".join(argv) + "  (cwd " + cwd + ")\n").encode("utf-8"))
            out.flush()
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": cwd, "env": env}
        if sys.platform == "win32":
            kwargs["creationflags"] = probes.no_window()
        child = subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        write_json(run_path, dict(run, error=str(exc), ended=now_iso(), exit=None))
        return
    pumper = threading.Thread(target=pump_output, args=(child.stdout, log_path, int(num(config, "lease_log_max_bytes", 50e6)), True),
                              daemon=True)
    pumper.start()
    try:
        child_ctime = created(psutil.Process(child.pid))
    except psutil.Error:
        child_ctime = None
    run.update({"pid": child.pid, "create_time": child_ctime, "argv": argv, "cwd": cwd})
    write_json(run_path, run)
    code, peak_rss = wait_with_peak(child)
    if peak_rss:
        run["child_peak_rss"] = peak_rss
    pumper.join(5)                                  # a grandchild may keep the pipe open; the log still gets the end line
    with open(log_path, "ab") as out:
        out.write(("\n[res] " + now_iso() + " ended with exit code " + str(code) + "\n").encode("utf-8"))
    write_json(run_path, dict(run, ended=now_iso(), exit=code))


# ---------------------------------------------------------------- leases, status, compliance

def read_run(machine: str, ident: str) -> dict:
    """The runner's record; a runner started by the pre-2026-09-28 code still writes it into the synced folder."""
    record = read_json(side_path(machine, ident, ".run.json"))
    if not record:
        record = read_json(synced_dir(machine) / (ident + ".run.json"))
    return record


def summary_line(lease: dict) -> list:
    state = lease.get("state", "?")
    when = lease.get("started") or lease.get("granted") or lease.get("requested")
    return [lease.get("id", "?"), state, lease.get("desk", ""), describe_need(lease.get("need", {}))[:60],
            human_duration(lease.get("duration")), human_duration(age_of(when)) + " ago",
            " ".join(lease.get("run_argv") or lease.get("command") or [])[:50]]


def cmd_leases(args, config: dict) -> None:
    machines = config["machines"] if args.all else [canonical_machine(args.on, config) if args.on else this_machine(config)]
    here = this_machine(config)
    for machine in machines:
        leases = all_leases(machine) if machine == here else list((read_report(machine, "reports").get("leases") or {}).values())
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
        if machine == here:
            print_compliance(machine, config, 7)
        else:
            print("\n".join(report.get("compliance") or ["(no compliance summary in its report)"]))
        return
    lease = read_json(lease_path(machine, args.id)) if machine == here else report_lease(machine, args.id)
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


UNLEASED_FILES = ["unleased.1.jsonl", "unleased.jsonl"]     # the compliance record: the rotated older file first


def jsonl_rows(data: bytes) -> list:
    """The objects of the lines in data; a line that does not parse is passed over."""
    rows = []
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def iter_unleased(machine: str) -> list:
    """The whole compliance record, every field."""
    rows = []
    for name in UNLEASED_FILES:
        try:
            rows += jsonl_rows((machine_dir(machine) / name).read_bytes())
        except FileNotFoundError:                      # no record yet, or the rotation's moment between the two files
            continue
    return rows


COUNTED = ("event", "at", "key", "desk", "lifetime")     # what compliance_lines counts in a compliance record row
UNLEASED_TAKEN = {}   # (st_dev, st_ino) of a record file -> (bytes taken, their rows cut to COUNTED)


def unleased_counted(machine: str) -> list:
    """The compliance record's rows cut to COUNTED, each file read only past what this process took from it before
    (until 2026-10-31 the dispatcher re-read the whole record, 3 to 10 MB, every 10 minutes). The record is only
    appended to, a whole line at a time, and its rotation renames unleased.jsonl to unleased.1.jsonl, which keeps the
    file's identity and so what was taken from it. A line still being written waits for the next call."""
    global UNLEASED_TAKEN
    rows, taken = [], {}
    for name in UNLEASED_FILES:
        try:
            fh = open(machine_dir(machine) / name, "rb")
        except FileNotFoundError:                      # no record yet, or the rotation's moment between the two files
            continue
        with fh:
            stat = os.fstat(fh.fileno())
            assert stat.st_ino, "no file identity for " + name
            ident = (stat.st_dev, stat.st_ino)
            done, kept = UNLEASED_TAKEN[ident] if ident in UNLEASED_TAKEN else (0, [])
            assert stat.st_size >= done, name + " shrank from " + str(done) + " to " + str(stat.st_size) + " bytes"
            fh.seek(done)
            data = fh.read()
        whole = data[:data.rfind(b"\n") + 1]
        kept = kept + [{k: row[k] for k in COUNTED if k in row} for row in jsonl_rows(whole)]
        taken[ident] = (done + len(whole), kept)
        rows += kept
    UNLEASED_TAKEN = taken
    return rows


def print_compliance(machine: str, config: dict, days: float) -> None:
    print("\n".join(compliance_lines(machine, days)))


def compliance_lines(machine: str, days: float) -> list:
    lines = []
    since = time.time() - days * 86400
    trials = read_folder(trial_dir(machine), lambda name: name.startswith("T") and name.endswith(".json")) \
        if trial_dir(machine).is_dir() else []
    trials = [t for t in trials if (parse_iso(t.get("started")) or 0) >= since]
    leases = [l for l in all_leases(machine) if (parse_iso(l.get("requested")) or 0) >= since]
    runs = [l for l in leases if l.get("started")]
    rows = [r for r in unleased_counted(machine) if (parse_iso(r.get("at")) or 0) >= since]
    starts = [r for r in rows if r.get("event") == "seen"]
    ends = {r.get("key"): r for r in rows if r.get("event") == "ended"}
    lines.append("Compliance on " + machine + ", last " + format(days, "g") + " days (guidance only, D-260927-4 option b):")
    lines.append("  trials: " + str(len(trials)) + " (" + str(len([t for t in trials if t.get("finished")])) + " finished inside the limit, "
                 + str(len([t for t in trials if not t.get("finished")])) + " cut off)")
    by_state = collections.Counter(l.get("state") for l in leases)
    lines.append("  lease requests: " + str(len(leases)) + (" (" + ", ".join(k + " " + str(v) for k, v in sorted(by_state.items())) + ")" if leases else ""))
    lines.append("  leased runs: " + str(len(runs)))
    lifetimes = [float(ends[r.get("key")]["lifetime"]) for r in starts
                 if r.get("key") in ends and isinstance(ends[r.get("key")].get("lifetime"), (int, float))]
    lines.append("  long commands of Claude sessions without a lease: " + str(len(starts))
                 + (" (median lifetime of the ended ones " + human_duration(statistics.median(lifetimes)) + ")" if lifetimes else ""))
    desks = collections.Counter(r.get("desk", "?") for r in starts)
    for desk, count in desks.most_common(8):
        lines.append("    " + desk + ": " + str(count))
    return lines


def cmd_notify_test(args, config: dict) -> None:
    """Show one desktop notification the way the dispatcher would (osascript on the Mac, a toast on Windows)."""
    machine = this_machine(config)
    if not config.get("notify", True):
        print("Note: notify is false in this machine's config; sending anyway for the test.")
    notify(dict(config, notify=True), machine, args.title, args.body)
    print("Sent a notification titled '" + args.title + "'. If none appeared, see the log for 'notification failed':\n  "
          + str(LOG_DIR / ("leaseresources." + machine.casefold() + "." + datetime.datetime.now().strftime("%y%m%d") + ".log")))


def cmd_compliance(args, config: dict) -> None:
    machine = canonical_machine(args.on, config) if args.on else this_machine(config)
    if machine != this_machine(config):
        print("\n".join(read_published(machine).get("compliance") or ["No compliance summary from " + machine + " yet."]))
        print("(the last 7 days, from " + machine + "'s dispatcher report; the full record stays on that machine)")
        return
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
    if len(history) > 500:                          # at most 500 commands, the most recently run ones
        ranked = sorted(history, key=lambda k: str((history[k] or [{}])[-1].get("ended") or ""), reverse=True)
        history = {k: history[k] for k in ranked[:500]}
    write_json(path, history)


# ---------------------------------------------------------------- the dispatcher

def tool_command(shell_command: str) -> str:
    """The command a Claude Bash tool call runs, from its shell's `-c` string: after the snapshot preamble the tool
    passes it as `eval '<command>'`, with single quotes inside written as '\\''. Returns '' if there is none."""
    marker = " eval '"
    shell_command = " " + shell_command
    start = shell_command.rfind(marker)
    if start < 0:
        return ""
    i = start + len(marker)
    out = []
    while i < len(shell_command):
        ch = shell_command[i]
        if ch == "'":
            if shell_command[i:i + 4] == "'" + chr(92) + "''":
                out.append("'")
                i += 4
                continue
            return "".join(out)
        out.append(ch)
        i += 1
    return "".join(out)


class Dispatcher:
    def __init__(self, config: dict):
        self.config = config
        self.machine = this_machine(config)
        self.folder = machine_dir(self.machine)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.meter = probes.Meter(config, LOCAL_DIR, first_sample_rates=False)
        self.probe_errors = set()                     # what the probes raised in the last sample, each logged once
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
        self.cmdline_refused = set()                  # Windows: process keys that show neither owner nor command line
        self.kernel_created = {}                      # macOS: pid -> (the scan's psutil.Process, the kernel's create time)
        self.unleased_pending = {}                    # key -> candidate younger than the threshold
        self.tool_members = {}                        # key -> the tool shell a process was seen under
        self.desk_cache = {}                          # working folder -> desk (from mail.py desk-of)
        self.last_report = None                       # the stable part of the last report this process wrote
        self.beat_at = -1e9
        self.learned = read_json(LOCAL_DIR / "learned_peaks.json")
        self.learned_dirty = False
        self.notice_seq = 0
        self.version = code_version()

    # -- state helpers
    def leases(self) -> list:
        return all_leases(self.machine)

    def save(self, lease: dict) -> None:
        write_json(lease_path(self.machine, lease["id"]), lease)

    def mail(self, lease: dict, subject: str, body: str, event: str = "") -> None:
        """Tell the lease's owner: through its attached waiter while one is alive (a notice file that process prints
        and deletes), otherwise by mail to its desk. Never both: flush_notices mails a notice only when its waiter is
        gone without having taken it."""
        ident = lease["id"]
        desk = lease["desk"]                                  # every lease names its desk (owner_desk)
        footer = ("\n\nFrom the LeaseResources dispatcher on " + self.machine + " (" + str(SCRIPT) + "). Lease file: "
                  + str(lease_path(self.machine, ident)) + ". Replies to this mail reach nobody; use `res status "
                  + ident + (" --on " + self.machine if lease.get("from_machine") != self.machine else "") + "`.")
        session = lease.get("session") or ""
        if lease.get("from_machine") == self.machine and attached_waiter(ident) is not None:
            self.notice_seq += 1
            name = file_stamp() + "-" + "%04d" % (self.notice_seq % 10000) + ".json"
            try:
                write_json(notice_dir(ident) / name, {"lease": ident, "event": event, "at": now_iso(), "subject": subject,
                                                      "body": body, "footer": footer, "desk": desk, "session": session})
                log(self.machine, "notice for the waiter of " + ident + ": " + subject)
                return
            except OSError as exc:
                log(self.machine, "WARN cannot write a notice for the waiter of " + ident + ", mailing it: " + str(exc))
        send_mail(self.config, self.machine, desk, subject, body + footer, session)

    def flush_notices(self) -> None:
        """Mail the notices that were written for an attached waiter which is gone without having taken them. What is
        pending is read from the notice folders, not from memory, so it survives a restart of the dispatcher. A file
        named <stamp>.json is untaken; <stamp>.json.<pid> was claimed by the waiter with that pid, which deletes it
        after printing; <stamp>.json.mail was claimed here for mailing."""
        try:
            folders = sorted(p for p in (LOCAL_DIR / "notices").iterdir() if p.is_dir())
        except OSError:
            return
        for folder in folders:
            waiter = attached_waiter(folder.name)
            try:
                paths = sorted(folder.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for path in paths:
                claim = path.name.partition(".json")[2]
                if claim == "":
                    if waiter is not None:
                        continue
                    claimed = path.with_name(path.name + ".mail")
                    try:
                        probes.replace_retrying(path, claimed)
                    except OSError:
                        continue                        # a waiter that attached just now took it
                    path = claimed
                elif claim[1:].isdigit():
                    if waiter is not None and str(waiter.pid) == claim[1:]:
                        continue                        # its waiter is printing it
                elif claim != ".mail":                  # a temporary file of write_json
                    try:
                        if time.time() - path.stat().st_mtime > 60:
                            path.unlink()
                    except OSError:
                        pass
                    continue
                notice = read_json(path)
                if notice.get("subject"):
                    log(self.machine, "the waiter of " + folder.name + " is gone; mailing its notice: " + notice["subject"])
                    send_mail(self.config, self.machine, notice["desk"], notice["subject"],
                              str(notice.get("body", "")) + str(notice.get("footer", "")), notice.get("session") or "")
                self.drop(path)
            if waiter is None:
                try:
                    folder.rmdir()
                except OSError:
                    pass

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
        # Meter a lease from the moment its runner exists, not only once watch() has marked it running: a 2-second
        # job would otherwise end between two samples unseen (no RAM peak on Windows, 2026-09-27).
        leases = [l for l in self.leases() if l.get("state") in ("granted", "starting", "running")
                  and side_path(self.machine, l["id"], ".run.json").exists()]
        trees = {}
        for lease in leases:
            run = read_run(self.machine, lease["id"])
            proc = process(run.get("runner_pid"), run.get("runner_create_time"))
            if proc is not None:
                trees[lease["id"]] = tree_of(proc)
        before = set(self.meter.dimensions())
        system, per = self.meter.sample(trees)
        after = set(self.meter.dimensions())
        for error in self.meter.errors:
            if error not in self.probe_errors:
                log(self.machine, "WARN probe failed, its dimensions are left out of the sample: " + error)
        self.probe_errors = set(self.meter.errors)
        if after != before:
            log(self.machine, "disks changed: added " + (", ".join(sorted(after - before)) or "none") + "; gone "
                + (", ".join(sorted(before - after)) or "none") + "; dimensions now " + ", ".join(self.meter.dimensions()))
            self.meter.raise_learned(self.learned)
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
        if self.learned_dirty:
            for dim in self.meter.raise_learned(self.learned):
                log(self.machine, "learned capacity of " + dim + " raised to " + human(self.learned[dim], kind_of(dim)))

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
            self.mail(lease, "Lease " + lease["id"] + " on " + self.machine + ": " + lease["state"], self.answer_body(lease), "answer")

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
                self.mail(lease, "Lease " + lease["id"] + " granted on " + self.machine, self.answer_body(lease), "granted")
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
                      + " script under this machine's allowed roots (config/" + self.machine.casefold() + ".json).", "refused")
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
                parts.append("duration now " + human_duration(lease["duration"]) + " in total, counted from the start")
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
                self.mail(lease, "Extension of " + ident + " on " + self.machine, answer, "extended")
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
                run = read_run(self.machine, ident)
                proc = process(run.get("runner_pid"), run.get("runner_create_time"))
                still = kill_tree(tree_of(proc)) if proc else []
                lease.update(state="killed", answer="killed on request of its owner" + (", survivors " + str(still) if still else ""),
                             ended=now_iso())
            else:
                lease.update(state="released", answer="released by its owner", ended=now_iso())
            self.save(lease)
            log(self.machine, "release " + ident + ": " + lease["state"])

    def ingest_synced(self) -> None:
        """Take requests and asks other machines wrote for this one into local state, then delete them. They come from
        the synced folder, so a run order among them is always started by this dispatcher under the program rule,
        whatever it claims about its origin. Also takes requests written in the old layout (leases/<Machine>/<id>.json)
        by machines still on the pre-2026-09-28 code, without deleting those."""
        for path in sorted(inbox(self.machine, "requests").glob("*.json")) if inbox(self.machine, "requests").is_dir() else []:
            data = read_json(path)
            if not data.get("id"):
                if time.time() - path.stat().st_mtime > 120:
                    self.drop(path)                   # never completed by Resilio
                continue
            local = lease_path(self.machine, data["id"])
            if not local.exists():
                data.update(state="requested", machine=self.machine, via="synced request")
                write_json(local, data)
                log(self.machine, "took request " + data["id"] + " from " + str(data.get("from_machine")))
            self.drop(path)
        asks = inbox(self.machine, "asks")
        for path in sorted(asks.glob("*.json")) if asks.is_dir() else []:
            name = path.name
            kind = next((k for k in ["order", "extend", "release"] if "." + k + "." in name), None)
            data = read_json(path)
            if kind is None or not data:
                if kind is None or time.time() - path.stat().st_mtime > 120:
                    self.drop(path)
                continue
            ident = name.partition("." + kind + ".")[0]
            if kind == "order":
                data["start"] = "dispatcher"          # from the synced folder: never a direct local start
                data["via"] = "synced ask"
            target = side_path(self.machine, ident, "." + kind + ".json")
            if not (kind == "order" and target.exists()):
                write_json(target, data)
                log(self.machine, "took " + kind + " for " + ident + " from " + str(data.get("from_machine")))
            self.drop(path)
        legacy = synced_dir(self.machine)
        if legacy.is_dir():
            for path in legacy.glob("L*.json"):
                if any(path.name.endswith(s) for s in SUFFIXES):
                    continue
                data = read_json(path)
                ident = data.get("id")
                if not ident or data.get("state") != "requested" or data.get("from_machine") == self.machine:
                    continue
                local = lease_path(self.machine, ident)
                if not local.exists():
                    write_json(local, dict(data, via="legacy synced request"))
                    log(self.machine, "took legacy request " + ident + " from " + str(data.get("from_machine")))
                order = legacy / (ident + ".order.json")
                if order.exists() and not side_path(self.machine, ident, ".order.json").exists():
                    write_json(side_path(self.machine, ident, ".order.json"), dict(read_json(order), start="dispatcher",
                                                                                    via="legacy synced order"))

    @staticmethod
    def drop(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

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
            run = read_run(self.machine, ident)
            if run.get("error"):
                lease.update(state="failed", answer="could not start: " + run["error"], ended=now_iso())
                self.save(lease)
                self.mail(lease, "Leased job " + ident + " failed to start on " + self.machine, lease["answer"], "start-failed")
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
                              + format(value * 1.25, ".6g") + (" --on " + self.machine if lease.get("from_machine") != self.machine else ""),
                              "overrun")
            started = parse_iso(lease.get("started"))
            if started and time.time() - started > float(lease.get("duration") or 0) and "duration" not in notices:
                notices["duration"] = {"at": now_iso()}
                changed = True
                log(self.machine, "past duration " + ident)
                self.mail(lease, "Leased job " + ident + " ran past its duration",
                          "The leased job " + ident + " on " + self.machine + " has run " + human_duration(time.time() - started)
                          + ", past its stated " + human_duration(lease.get("duration")) + ". It keeps running.\nLog tail:\n\n"
                          + tail(side_path(self.machine, ident, ".log"), 10) + "\n\nExtend: res extend " + ident
                          + " --for <new total duration, counted from the job's start, not an addition>",
                          "duration")
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
                          + str(log_path) + "):\n\n" + tail(log_path, 10), "stall")
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
                      + "\nYou get mail when it ends, stalls, overruns or runs past " + human_duration(lease.get("duration")) + ".",
                      "started")
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
        if run.get("child_peak_rss"):
            peaks["ram"] = max(float(peaks.get("ram", 0.0)), float(run["child_peak_rss"]))
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
        self.mail(lease, "Leased job " + ident + " " + state + " on " + self.machine, body, state)

    # -- compliance: long commands of Claude sessions without a lease
    def claude_names(self) -> list:
        return [n.casefold() for n in self.config.get("claude_process_names", ["claude", "claude.exe"])]

    def is_tool_shell(self, cmd: str, name: str) -> bool:
        markers = self.config.get("tool_shell_markers", ["shell-snapshots"])
        shell_names = [n.casefold() for n in self.config.get("tool_shell_names", [])]
        return any(m in cmd for m in markers) or name.casefold() in shell_names

    def ignored(self, cmd: str) -> bool:
        """A marker with a space is matched as a phrase; a file name such as `res.py` only as the name of a program or
        script in the command, so `test_res.py` does not count as res.py."""
        markers = self.config.get("ignore_command_markers", [])
        names = set()
        for token in cmd.split():
            token = token.strip(chr(34) + "'")
            names.add(token.replace(chr(92), "/").rpartition("/")[2].casefold())
        for marker in markers:
            if " " in marker:
                if marker in cmd:
                    return True
            elif marker.casefold() in names:
                return True
        return False

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

    def claude_env(self, proc, key: str, any_marker: bool = False) -> dict:
        """The Claude markers in a process's environment ({} when absent or unreadable). Cached per process: an
        environment never changes. macOS does not reveal the environment of Apple's own binaries (/bin/sleep, zsh);
        Homebrew, venv and other programs are readable, and on Windows psutil reads it for the user's processes."""
        if key not in self.env_cache:
            try:
                env = proc.environ()
            except (psutil.Error, OSError):
                env = {}
            found = {}
            if env.get("CLAUDECODE") == "1":
                found = {"session": env.get("CLAUDE_CODE_SESSION_ID", ""), "claude_pid": env.get("CLAUDE_PID", ""),
                         "leased": bool(env.get("LEASERES_LEASE"))}
                launcher = env.get("CLAUDE_CODE_SHELL_LAUNCHER_SCRIPT", "")
                if launcher:                          # the PowerShell tool keeps the command it runs here
                    found["command"] = " ".join(launcher.split())[:300]
            self.env_cache[key] = found
        found = self.env_cache[key]
        if any_marker or not found or not found.get("leased"):
            return found
        return {}

    def per_command_shell(self, cmd_parts: list, name: str) -> bool:
        """A tool shell that runs one command and ends (the Mac's `zsh -c 'source <snapshot>...'`), as opposed to a
        persistent host that runs many (a PowerShell host reading commands, a persistent bash)."""
        if not self.is_tool_host(name, " ".join(cmd_parts)):
            return False
        flags = ["-c", "-command", "-encodedcommand", "-enc", "-ec", "/c"]
        for index, part in enumerate(cmd_parts[1:], start=1):
            if part.casefold() in flags:
                following = cmd_parts[index + 1] if index + 1 < len(cmd_parts) else ""
                return following not in ("", "-")
        return False

    def marked_tree(self, pid: int, info: dict, children: dict) -> bool:
        """Whether this shell or something below it carries a tool marker (`shell-snapshots` from the Bash tool,
        `__claudeCodeScript` from the PowerShell tool), which tells a tool call from an MCP server or hook started
        through `sh -c` / `cmd /c`."""
        markers = self.config.get("tool_shell_markers", ["shell-snapshots"])
        stack = [pid]
        seen = set()
        while stack:
            current = stack.pop()
            if current in seen or current not in info:
                continue
            seen.add(current)
            cmd = " ".join(info[current][1].get("cmdline") or [])
            if any(m in cmd for m in markers):
                return True
            stack.extend(children.get(current, []))
        return False

    def is_tool_host(self, name: str, cmd: str) -> bool:
        hosts = [n.casefold() for n in self.config.get("tool_host_names", [])]
        return name.casefold() in hosts or self.is_tool_shell(cmd, name)

    def check_unleased(self) -> None:
        """Long commands that Claude sessions run without a lease. Every process of this user is placed by the nearest
        tool shell on its parent chain (parents followed only while older than the child, since Windows reuses a dead
        parent's pid). A tool shell is a shell from tool_host_names that carries a tool marker (`shell-snapshots` from
        the Bash tool, `__claudeCodeScript` from the PowerShell tool) itself, or, when it runs one command
        (`-c`, `/c`, `-Command`), in a descendant; it counts whether or not a live Claude process is found above it,
        because on Windows the Git Bash (MSYS) shells' parent chain need not reach claude.exe.
          (a) a per-command tool shell older than unleased_after_seconds is recorded at the outermost such shell,
              listing the real commands below it (the PowerShell tool's `cmd /c` + `powershell` pair is one);
          (a') under a persistent tool shell (one that runs many commands: Git Bash on Windows, a PowerShell host), each
              direct child older than the threshold (an MSYS fork of bash, with `sleep` below it, is such a child);
          (b) POSIX, no tool shell on the chain: a process in the session of a tool shell that has gone (nohup / &);
          (c) no tool shell on the chain and no live Claude above: CLAUDECODE=1 in its environment (setsid, double
              fork, Start-Process, a background command whose shell exited).
        Claude processes are those named in claude_process_names and those a process names in CLAUDE_PID. Excluded:
        leased jobs (LEASERES_LEASE), res.py/mail.py/house.py and what runs under them, ignore_process_names (conhost),
        other users' processes, and what sits under a Claude process without a tool shell (MCP servers, hooks). A
        candidate that ends before a scan sees it past the threshold is recorded when it ends if it lived long enough,
        so the scan interval cannot hide a command that ran, say, 150 s."""
        names = self.claude_names()
        ignore_names = [n.casefold() for n in self.config.get("ignore_process_names", [])]
        after = num(self.config, "unleased_after_seconds", 120)
        younger = min(after, 30.0)
        try:
            user = psutil.Process().username()
        except psutil.Error:
            user = None
        children = collections.defaultdict(list)
        info = {}
        attrs = ["pid", "ppid", "name", "create_time", "cmdline", "username"]
        if sys.platform == "win32":
            attrs.remove("cmdline")                   # read by read_cmdline, which asks a refusing process only once
        shifting = shifts_create_times()
        for p in psutil.process_iter(attrs):
            data = p.info
            if shifting and data.get("create_time") is not None:
                data["create_time"] = self.created_in_scan(p, data["create_time"])
            if "cmdline" not in data and not self.read_cmdline(p, data):
                continue                              # ended meanwhile; process_iter leaves such a process out too
            info[data["pid"]] = (p, data)
            children[data.get("ppid")].append(data["pid"])
        now = time.time()

        def key_of(pid):
            return str(pid) + "-" + format(float(info[pid][1].get("create_time") or 0), ".2f")

        def mine(pid):
            owner = info[pid][1].get("username")
            return not user or not owner or owner == user

        def name_of(pid):
            return str(info[pid][1].get("name") or "")

        def parts_of(pid):
            return info[pid][1].get("cmdline") or []

        def cmd_of(pid):
            return " ".join(parts_of(pid)) if pid in info else ""

        def age_of_pid(pid):
            return now - float(info[pid][1].get("create_time") or now)

        claudes = {pid for pid in info if name_of(pid).casefold() in names}
        for pid, (proc, data) in info.items():
            if pid in claudes or not mine(pid) or name_of(pid).casefold() in ignore_names or age_of_pid(pid) < min(younger, 3.0):
                continue
            named = str(self.claude_env(proc, key_of(pid), any_marker=True).get("claude_pid") or "")
            if named.isdigit() and int(named) in info:
                claudes.add(int(named))

        def real_parent(pid):
            parent = info[pid][1].get("ppid")
            if parent not in info or parent == pid:
                return None
            if float(info[parent][1].get("create_time") or 0) > float(info[pid][1].get("create_time") or 0) + 1.0:
                return None
            return parent

        shell_cache = {}

        def tool_shell(pid):
            """None, 'per-command' or 'persistent'."""
            if pid in shell_cache:
                return shell_cache[pid]
            kind = None
            parts, name = parts_of(pid), name_of(pid)
            if self.is_tool_host(name, " ".join(parts)) and not self.ignored(" ".join(parts)):
                per = self.per_command_shell(parts, name)
                own_marker = self.is_tool_shell(" ".join(parts), name)
                if per and (own_marker or self.marked_tree(pid, info, children)):
                    kind = "per-command"
                elif not per and (own_marker or self.claude_env(info[pid][0], key_of(pid), any_marker=True)):
                    kind = "persistent"               # e.g. a Git Bash kept by the Bash tool, marked or with CLAUDECODE
            shell_cache[pid] = kind
            return kind

        def placement(pid):
            """(outermost tool shell on the chain or None, its kind, whether a live Claude is above, chain)."""
            chain, seen, current, claude_above = [], set(), pid, False
            while current is not None and current not in seen:
                seen.add(current)
                chain.append(current)
                parent = real_parent(current)
                if parent in claudes:
                    claude_above = True
                    break
                current = parent
            outer = None
            for member in chain:
                if tool_shell(member):
                    outer = member
            return outer, (tool_shell(outer) if outer else None), claude_above, chain

        def session_of(pids):
            for candidate in pids:
                if candidate in info:
                    marks = self.claude_env(info[candidate][0], key_of(candidate), any_marker=True)
                    if marks.get("session"):
                        return marks["session"]
            return ""

        def below(pid):
            out, stack = [], list(children.get(pid, []))
            while stack:
                d = stack.pop()
                if d in info:
                    out.append(d)
                    stack.extend(children.get(d, []))
            return out

        found = {}
        for pid, (proc, data) in info.items():
            name = name_of(pid)
            if pid in claudes or name.casefold() in ignore_names or not mine(pid):
                continue
            cmd = cmd_of(pid)
            if self.ignored(cmd):
                continue
            key = key_of(pid)
            outer, kind, claude_above, chain = placement(pid)
            if outer is not None and key not in self.tool_members:
                # Remember everything seen inside a tool shell: if its shell exits (a background Bash command), the
                # orphan is still known to be a tool command even where its environment cannot be read (MSYS?).
                self.tool_members[key] = {"shell": cmd_of(outer)[-160:], "claude_pid": real_parent(outer),
                                          "session": session_of(chain), "cwd_pid": outer}
                try:
                    self.tool_members[key]["cwd"] = psutil.Process(outer).cwd()
                except (psutil.Error, OSError):
                    self.tool_members[key]["cwd"] = ""
            age = age_of_pid(pid)
            if age < younger:
                continue
            record = None
            if outer is not None:
                if kind == "per-command" and pid == outer:
                    descendants = below(pid)
                    commands = [cmd_of(d) for d in descendants]
                    if any(self.ignored(c) for c in commands):
                        continue
                    real = [cmd_of(d) for d in descendants if not tool_shell(d) and name_of(d).casefold() not in ignore_names]
                    asked = tool_command(cmd) or next((tool_command(cmd_of(d)) for d in descendants if tool_command(cmd_of(d))), "") \
                        or next((self.claude_env(info[d][0], key_of(d), any_marker=True).get("command", "")
                                 for d in [pid] + descendants
                                 if self.claude_env(info[d][0], key_of(d), any_marker=True).get("command")), "")
                    listed = ([asked[:300]] if asked else []) + [c[:300] for c in real[:4] if not asked or asked not in c]
                    record = {"how": "tool shell", "claude_pid": real_parent(pid), "cwd_pid": pid,
                              "session": session_of([pid] + descendants), "shell": cmd[-160:],
                              "commands": listed or [cmd[-300:]]}
                elif kind == "persistent" and pid != outer and real_parent(pid) == outer:
                    descendants = below(pid)
                    commands = [cmd_of(d) for d in descendants]
                    if any(self.ignored(c) for c in commands):
                        continue
                    real = [c for d, c in zip(descendants, commands) if not tool_shell(d) and name_of(d).casefold() not in ignore_names]
                    asked = tool_command(cmd)
                    shown = ([asked[:300]] if asked else ([cmd[:300]] if not tool_shell(pid) else [])) + [c[:300] for c in real[:3]]
                    record = {"how": "command of a persistent tool shell", "claude_pid": real_parent(outer), "cwd_pid": outer,
                              "session": session_of([pid] + descendants + [outer]), "shell": cmd_of(outer)[-160:],
                              "commands": shown or [cmd[:300]]}
                else:
                    continue                          # a shell itself, or deeper inside a tree counted at its top
            elif claude_above:
                continue                              # MCP servers, hooks and their children
            else:
                if sys.platform != "win32":
                    try:
                        sid = os.getsid(pid)
                    except OSError:
                        sid = None
                    shell = self.shell_sids.get(sid)
                    if shell is not None and sid != pid and process(sid) is None:
                        record = {"how": "backgrounded, its tool shell gone", "claude_pid": shell["claude_pid"],
                                  "cwd": shell["cwd"], "shell": shell["shell"], "commands": [cmd[:300]]}
                member = self.tool_members.get(key)
                if record is None and member is not None:
                    record = {"how": "backgrounded, its tool shell gone", "claude_pid": member.get("claude_pid"),
                              "cwd": member.get("cwd", ""), "session": member.get("session", ""),
                              "shell": member.get("shell", ""), "commands": [cmd[:300]]}
                if record is None:
                    marks = self.claude_env(proc, key)
                    if marks:
                        record = {"how": "left the Claude tree (environment marker)", "claude_pid": marks.get("claude_pid"),
                                  "session": marks.get("session"), "cwd_pid": pid, "commands": [cmd[:300]]}
                if record is None:
                    continue
                parent = real_parent(pid)
                if parent is not None and parent not in claudes and placement(parent)[0] is None \
                        and (self.escaped_parent(parent, info, now, younger) or key_of(parent) in self.tool_members):
                    self.unleased_pending.pop(key, None)
                    continue                          # its parent is recorded instead
            record.update(pid=pid, age=age, first=now - age)
            if age >= after:
                found[key] = record
            else:
                self.unleased_pending[key] = record   # not long enough yet; recorded later, or when it ends
        for key, rec in found.items():
            self.unleased_pending.pop(key, None)
            if key not in self.unleased_seen:
                self.write_seen(key, rec, now)
        for key in list(self.unleased_pending):
            if key in found:
                continue
            pid_text = key.partition("-")[0]
            alive_now = int(pid_text) in info and key_of(int(pid_text)) == key
            if alive_now:
                continue
            rec = self.unleased_pending.pop(key)
            lifetime = now - rec.get("first", now)
            if lifetime >= after:                     # ended between two scans after running long enough
                self.write_seen(key, dict(rec, age=lifetime), now, note="recorded at its end; it ended between two scans")
                self.end_record(key, now)
        live_keys = {key_of(pid) for pid in info}
        for key in list(self.unleased_seen):
            # A record ends only when its process ends: whether it qualifies can flicker with its children (a test
            # suite spawning short res.py children is skipped in the scans that catch one), which must not split one
            # command into several records (Moosfels round 4: four pairs for one suite run).
            if key not in live_keys:
                self.end_record(key, now)
        self.env_cache = {k: v for k, v in self.env_cache.items() if k in live_keys}
        self.kernel_created = {pid: v for pid, v in self.kernel_created.items() if pid in info}
        self.cmdline_refused &= live_keys
        self.tool_members = {k: v for k, v in self.tool_members.items() if k in live_keys}

    def created_in_scan(self, proc, reading: float) -> float:
        """macOS: the kernel's create time of a process of the scan, in place of the reading psutil hands out, so that
        a key means the same process to this dispatcher at any later time and to the dispatcher after it. psutil's
        reading holds the clock shift (see clock_shift) of the moment the Process object was first asked, process_iter
        hands out the same object in every scan, and the shift moves meanwhile, so taking the present shift from an old
        reading is wrong. The kernel's time is read once, through a new Process, when the scan first meets an object,
        and remembered for as long as process_iter hands out that object (a new object under a known pid is another
        process, or the same one read anew): a few calls per new process, none later. The shift is asked before and
        after the reading, and the reading repeated if it moved in between, so no key is built on a shift that did not
        belong to its reading."""
        known = self.kernel_created.get(proc.pid)
        if known is not None and known[0] is proc:
            return known[1]
        shift = clock_shift()
        try:
            for _attempt in range(3):
                value = psutil.Process(proc.pid).create_time() - shift
                after = clock_shift()
                if after == shift:
                    break
                shift = after
        except psutil.Error:                          # ended since process_iter read it
            value = reading - shift
        self.kernel_created[proc.pid] = (proc, value)
        return value

    def read_cmdline(self, proc, data: dict) -> bool:
        """Windows: put the command line into data as process_iter does (None when refused); False when the process
        has ended. Unlike parent, name and create time, psutil does not keep a command line between scans, and every
        refusal costs it about 12 ms (two attempts, each starting with an enumeration of all pids). On Moosfels that
        was 152 processes of services and other users, 1.8 s of kernel time in every scan and 0.3 cores all day
        (2026-10-03). So a process that shows neither its owner nor its command line is not asked again: what this
        user may open of a process does not change while the process lives. A command line that can be read is read
        anew in every scan, as before."""
        key = str(data["pid"]) + "-" + format(float(data.get("create_time") or 0), ".2f")
        if key in self.cmdline_refused:
            data["cmdline"] = None
            return True
        try:
            data["cmdline"] = proc.cmdline()
        except psutil.AccessDenied:
            data["cmdline"] = None
            if data.get("username") is None and data.get("create_time"):
                self.cmdline_refused.add(key)
        except psutil.ZombieProcess:                  # a NoSuchProcess by class, but process_iter keeps it, with None
            data["cmdline"] = None
        except psutil.NoSuchProcess:
            return False
        return True

    def write_seen(self, key: str, rec: dict, now: float, note: str = "") -> None:
        rec = dict(rec)
        cwd = rec.pop("cwd", None)
        cwd_pid = rec.pop("cwd_pid", None)
        if cwd is None:
            try:
                cwd = psutil.Process(cwd_pid).cwd() if cwd_pid else ""
            except (psutil.Error, OSError, ValueError):
                cwd = ""
        session = rec.get("session") or ""
        if not session:
            session = self.session_of_claude(rec.get("claude_pid"))
        record = {"event": "seen", "at": now_iso(), "key": key, "how": rec.get("how"), "shell_pid": rec.get("pid"),
                  "claude_pid": rec.get("claude_pid"), "session": session, "age": round(rec.get("age", 0)),
                  "cwd": cwd, "desk": self.session_desk(session) or self.desk_of(cwd), "shell": rec.get("shell", ""),
                  "commands": rec.get("commands", [])}
        if note:
            record["note"] = note
        self.unleased_seen[key] = dict(record, first=rec.get("first", now - rec.get("age", 0)))
        self.append_unleased(record)
        log(self.machine, "unleased long command (" + str(record["how"]) + ") in " + record["desk"] + ": "
            + (record["commands"][0] if record["commands"] else record["shell"][-80:]))

    def end_record(self, key: str, now: float) -> None:
        seen = self.unleased_seen.pop(key, None)
        if seen is None:
            return
        self.append_unleased({"event": "ended", "at": now_iso(), "key": seen.get("key") or key, "desk": seen.get("desk"),
                              "session": seen.get("session", ""), "how": seen.get("how"),
                              "lifetime": round(now - seen.get("first", now))})

    def restore_unleased(self) -> None:
        """After a restart: take back the records that were seen and not ended, so a process is recorded once. On the
        Macs a key written by the code up to 2026-10-08 holds psutil's create time, which was ahead of the kernel's by
        the whole seconds of that dispatcher's clock shift (see clock_shift; 1 and 2 s were seen), while the scan now
        keys by the kernel's. There a record is taken back when its pid lives and was created within RESTORE_TOLERANCE
        of the key's time (a pid cannot come round again that fast), is tracked under the key the scan gives the
        process, and keeps the key it was written with for its `ended` row, which is what `res compliance` pairs the
        two by."""
        open_records = {}
        for row in iter_unleased(self.machine):
            if row.get("event") == "seen":
                open_records[row.get("key")] = row
            elif row.get("event") == "ended":
                open_records.pop(row.get("key"), None)
        now = time.time()
        for key, row in open_records.items():
            pid_text, _, ctime = str(key).partition("-")
            proc = process(pid_text, ctime, RESTORE_TOLERANCE if shifts_create_times() else 1.0) if pid_text.isdigit() else None
            scan_key, first = key, None
            if proc is not None and shifts_create_times():
                try:
                    first = created(proc)
                    scan_key = pid_text + "-" + format(first, ".2f")
                except psutil.Error:
                    proc = None
            if proc is None:
                self.append_unleased({"event": "ended", "at": now_iso(), "key": key, "desk": row.get("desk"),
                                      "session": row.get("session", ""), "how": row.get("how"), "lifetime": None,
                                      "note": "ended while the dispatcher was down"})
                continue
            self.unleased_seen[scan_key] = dict(row, first=float(ctime or now) if first is None else first, restored=True)

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

    def session_of_claude(self, claude_pid) -> str:
        """The session ClaudeMail registered for a Claude process (its hooks record claude_pid), for records whose
        processes do not show their environment (Apple's own binaries on the Mac)."""
        text = str(claude_pid or "")
        if not text.isdigit():
            return ""
        best, best_active = "", ""
        folder = Path.home() / ".claude" / "claudemail" / "sessions"
        try:
            paths = list(folder.glob("*.json"))
        except OSError:
            return ""
        for path in paths:
            record = read_json(path)
            if str(record.get("claude_pid")) == text and str(record.get("last_active") or "") >= best_active:
                best, best_active = str(record.get("session") or ""), str(record.get("last_active") or "")
        return best

    def session_desk(self, session: str) -> str:
        """The desk ClaudeMail registered for this session (its start folder), which a later `cd` does not change."""
        if not session:
            return ""
        record = read_json(Path.home() / ".claude" / "claudemail" / "sessions" / (session + ".json"))
        desk = str(record.get("desk") or "").strip()
        return self.machine + ":" + desk if desk else ""

    def compliance_loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                self.track_shells()
                self.check_unleased()
            except Exception as exc:
                log(self.machine, "WARN compliance scan: " + type(exc).__name__ + ": " + str(exc))
            spent = time.monotonic() - started
            if spent > 5:
                log(self.machine, "compliance scan took " + format(spent, ".1f") + " s")
            time.sleep(max(0.5, num(self.config, "unleased_check_seconds", 5) - spent))

    def desk_of(self, folder: str) -> str:
        if not folder:
            return self.machine + ":?"
        cached = self.desk_cache.get(folder)
        if cached:
            return cached
        desk = self.desk_of_uncached(folder)
        if not desk.endswith(":?"):
            self.desk_cache[folder] = desk
        return desk

    def desk_of_uncached(self, folder: str) -> str:
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
        path = self.folder / "unleased.jsonl"
        try:
            if path.stat().st_size > num(self.config, "compliance_log_max_bytes", 5e6):
                probes.replace_retrying(path, self.folder / "unleased.1.jsonl")     # keeps at most two files
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    # -- report, housekeeping
    def report(self) -> None:
        """Publish what other machines need (`--on`) into the slot files (write_report): a report when its content
        changed since the last one this process wrote (measurements that change every sample do not count as a change),
        and every REPORT_SECONDS a beat, about 1 KB, with the compliance summary, by which other machines see that this
        dispatcher runs. Until 2026-10-31 the report itself, 33 KB on Altiplano, was the heartbeat and carried the
        compliance summary, which changes nearly every time it is counted, so a report went out every 10 minutes."""
        dims = self.meter.dimensions()
        limits = self.limits()
        leases = self.leases()
        keep = ["id", "state", "answer", "desk", "why", "need", "duration", "requested", "granted", "started", "ended",
                "exit", "seconds", "from_machine", "run_argv", "history_note"]
        summary = {}
        for lease in leases:
            if lease.get("state") in OPEN or age_of(lease.get("ended") or lease.get("answered")) < 86400:
                summary[lease["id"]] = dict({k: lease[k] for k in keep if k in lease},
                                            log=str(side_path(self.machine, lease["id"], ".log")))
        if time.monotonic() - self.beat_at > REPORT_SECONDS:
            if write_report(self.machine, "beats", {"updated": now_iso(), "pid": os.getpid(),
                                                    "compliance": compliance_lines(self.machine, 7)}):
                self.beat_at = time.monotonic()
        stable = {"machine": self.machine, "pid": os.getpid(), "version": self.version,
                  "capacity": {d: {"capacity": i["capacity"], "limit": limits[d], "label": i.get("label", "")} for d, i in dims.items()},
                  "planned": self.planned(), "queue": [l["id"] for l in leases if l.get("state") == "queued"],
                  "active": [l["id"] for l in leases if l.get("state") in ACTIVE], "leases": summary}
        if stable != self.last_report:
            if write_report(self.machine, "reports", dict(stable, updated=now_iso(), unleased=self.unleased_now(),
                                                          system=self.last_system)):
                self.last_report = stable                # not written: tried again on the next pass
        if self.learned_dirty:
            write_json(LOCAL_DIR / "learned_peaks.json", self.learned)
            self.learned_dirty = False

    def housekeeping(self) -> None:
        keep = 86400 * num(self.config, "keep_days", 30)
        done = self.folder / "done"
        for lease in self.leases():
            if lease.get("state") in ENDED and age_of(lease.get("ended") or lease.get("answered")) > 7 * 86400:
                done.mkdir(exist_ok=True)
                for path in [lease_path(self.machine, lease["id"])] + [side_path(self.machine, lease["id"], s) for s in SUFFIXES]:
                    if path.exists():
                        try:
                            probes.replace_retrying(path, done / path.name)
                        except OSError:
                            pass
        try:
            for path in (LOCAL_DIR / "attached").iterdir():           # left by waiters that were killed
                if attached_waiter(path.name.rpartition(".json")[0]) is None and time.time() - path.stat().st_mtime > 3600:
                    path.unlink()
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
        # this machine's daily logs and diagnostic snapshots after keep_days; the LaunchAgent's bootstrap log is cut
        # back when it passes 10 MB (it only holds tracebacks, but a crash loop would grow it)
        prefixes = ["leaseresources." + self.machine.casefold() + ".", "snapshot-" + self.machine]
        jobs = LOG_DIR / "leases"
        if jobs.is_dir():
            for path in jobs.iterdir():
                try:
                    if time.time() - path.stat().st_mtime > keep:
                        path.unlink()
                except OSError:
                    pass
        if LOG_DIR.is_dir():
            for path in LOG_DIR.iterdir():
                try:
                    if any(path.name.casefold().startswith(pre.casefold()) for pre in prefixes) \
                            and time.time() - path.stat().st_mtime > keep:
                        path.unlink()
                    elif path.name == "launchd.log" and sys.platform == "darwin" and path.stat().st_size > 10e6:
                        with open(path, "r+b") as fh:
                            fh.truncate(0)
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


def beat() -> None:
    write_json(local_heartbeat(), {"pid": os.getpid(), "updated": now_iso()})


def beat_if_dispatcher() -> None:
    """Called before each mail and toast, whose calls may take up to 60 and 30 s: a pass of the main loop that mails many
    notices (a backlog after an outage, mail failing each time) stays alive to the keeper, which reads 600 s without a
    beat as a hang. Only in the dispatcher holding the lock; other commands mail too."""
    if read_json(local_heartbeat()).get("pid") == os.getpid():
        beat()


def take_lock(machine: str) -> bool:
    held = read_json(local_heartbeat())
    pid = held.get("pid")
    if isinstance(pid, int) and pid != os.getpid() and process(pid) is not None:
        if age_of(held.get("updated")) < 120:
            return False
        try:
            psutil.Process(pid).terminate()
            log(machine, "stopped a dispatcher that stopped reporting (pid " + str(pid) + ")")
        except psutil.Error:
            pass
    beat()
    return True


def cmd_dispatch(args, config: dict) -> None:
    machine = this_machine(config)
    if not take_lock(machine):
        sys.exit(EXIT_ALREADY_RUNNING)
    seed_local_state(machine)
    d = Dispatcher(config)
    log(machine, "dispatcher started (pid " + str(os.getpid()) + ", code " + d.version + ", dimensions "
        + ", ".join(d.meter.dimensions()) + ")")
    for error in d.meter.errors:
        log(machine, "WARN probe failed at start, asked again at every sample: " + error)
    d.restore_unleased()
    # The compliance scan runs in its own thread: its first pass on a machine with many long-lived tool shells (each new
    # record asks ClaudeMail for a desk) must never delay answers to lease requests (Moosfels, round 3).
    threading.Thread(target=d.compliance_loop, name="compliance", daemon=True).start()
    time.sleep(1.0)
    d.sample()
    last_sample = last_unleased = last_beat = last_house = time.monotonic()
    via = REPORTS_VIA
    d.report()
    while True:
        try:
            try:
                d.config = load_config()
            except (OSError, ValueError) as exc:
                log(machine, "WARN cannot read config, keeping the last one: " + str(exc))
            beat()
            d.ingest_synced()
            d.handle_asks()
            d.handle_new()
            d.handle_orders()
            try:
                d.flush_notices()
            except Exception as exc:                          # a damaged notice file must not hold up metering and the queue
                log(machine, "WARN notices: " + type(exc).__name__ + ": " + str(exc))
            now = time.monotonic()
            if now - last_sample >= num(d.config, "sample_seconds", 5):
                d.sample()
                d.watch()
                d.serve_queue()
                d.report()
                last_sample = now
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
        if REPORTS_VIA != via:                                # the restarted one publishes its report and beat at once
            log(machine, "reports_via is now " + REPORTS_VIA + "; dispatcher restarting to publish there")
            sys.exit(EXIT_CODE_CHANGED)
        time.sleep(num(d.config, "tick_seconds", 1))


def keep(machine: str, child: subprocess.Popen, stall: float):
    """Waits for the dispatcher child to exit and returns its exit code; or kills it and returns None once its main loop
    has written no heartbeat for `stall` seconds (counted from the child's start until its first beat). A hung main loop
    cannot end itself: on Mistral on 2026-11-12 psutil's open_files() had killed one of its own helper threads with
    TerminateThread, which leaves the Windows loader lock held forever; from then on no thread of the process could start
    (subprocess.run's reader thread for nvidia-smi waited for 11 hours, past its timeout) and even os._exit hangs, since
    ExitProcess takes that lock too. Only a kill from outside (TerminateProcess) ends such a process. Waiting needs no
    thread here (WaitForSingleObject). The silence is counted in polls that found no new beat, not by the clock, so a Mac
    waking from sleep does not read as a hang.
    The dispatcher is the child itself, or on Windows the base interpreter that the venv's python.exe launcher (the
    child) starts as its own child; it is the process that names itself in the heartbeat. The kill goes to it and to the
    launcher only, never further down: the leased jobs' runners are the dispatcher's children (detached, still its
    children by parent pid), and they run on."""
    poll = 5.0
    ours = None
    seen = None
    quiet = 0.0
    while True:
        try:
            return child.wait(timeout=poll)
        except subprocess.TimeoutExpired:
            pass
        beat = read_json(local_heartbeat())
        pid = beat.get("pid")
        if ours is None and isinstance(pid, int) and (pid == child.pid or parent_of(pid) == child.pid):
            ours = pid
        if ours is not None and pid == ours and beat.get("updated") != seen:
            seen, quiet = beat.get("updated"), 0.0
        else:
            quiet += poll
        if quiet > stall:
            log(machine, "WARN dispatcher pid " + str(ours or child.pid) + " wrote no heartbeat for " + str(int(quiet))
                + " s; killed, a new one starts")
            if ours is None:                         # never beat, so it started no runner yet: its launched interpreter
                try:
                    victims = [p.pid for p in psutil.Process(child.pid).children()]
                except psutil.NoSuchProcess:         # it exited since the last wait; child.wait() below collects it
                    victims = []
            else:
                victims = [ours] if ours != child.pid else []
            for victim in victims:
                try:
                    psutil.Process(victim).kill()
                except psutil.NoSuchProcess:         # already gone, which is what the kill is for
                    pass
            child.kill()
            child.wait()
            return None


def parent_of(pid: int):
    try:
        return psutil.Process(pid).ppid()
    except psutil.NoSuchProcess:                     # an earlier dispatcher's beat, its writer gone
        return None


def cmd_watch(args, config: dict) -> None:
    """The keeper the LaunchAgent / scheduled task runs: keeps a dispatcher child alive, restarts it at once after a code
    change or a hang (see keep). Kept tiny, because only a restart of the task renews it."""
    machine = this_machine(config)
    stall = float(config["dispatcher_stall_seconds"])
    log(machine, "watch started (pid " + str(os.getpid()) + ")")
    while True:
        started = time.monotonic()
        try:
            child = subprocess.Popen([sys.executable, "-B", str(SCRIPT), "dispatch"], creationflags=probes.no_window())
        except OSError as exc:
            log(machine, "WARN cannot start the dispatcher: " + str(exc))
            code = -1
        else:
            code = keep(machine, child, stall)
        if code is None or code == EXIT_CODE_CHANGED:
            continue
        if code == EXIT_ALREADY_RUNNING:
            time.sleep(60)
            continue
        log(machine, "WARN dispatcher exited with code " + str(code) + " after " + str(int(time.monotonic() - started)) + " s")
        time.sleep(30)


# ---------------------------------------------------------------- main

def lenient_output() -> None:
    """Printing never raises on a character the output stream cannot encode. Job output is echoed as it was decoded
    (UTF-8 with replacement: the trial's output tail, a lease's log tail, the waiter's stream and its notices), so it
    can hold U+FFFD where a child wrote another encoding, and anything outside the console's code page. A Windows
    console or pipe encodes as cp1252 and raises on anything outside it (Python's default handler there,
    surrogateescape, raises as strict does on everything but lone surrogates): `res trial` raised UnicodeEncodeError
    in the middle of its report, after the command had finished with exit code 0, and the profile was lost (Moosfels
    2026-10-03). With backslashreplace such a character is printed as its escape (U+FFFD as a backslash and ufffd);
    encoding and buffering stay as they were."""
    for stream in [sys.stdout, sys.stderr]:
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass                                        # no stream at all (pythonw), or one that is not a text wrapper


def main() -> None:
    lenient_output()
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
    p.add_argument("--for", dest="duration", help="the lease's new total duration, counted from the job's start (not added"
                   " to the old one): a job 4 h into a 5 h lease that needs 2 h more asks --for 6h")
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
    p = sub.add_parser("wait", help="attach to a lease on this machine: its output and notices arrive here, not by mail")
    p.add_argument("id")
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
    p = sub.add_parser("notify-test", help="show one desktop notification, to check they reach the screen")
    p.add_argument("--title", default="LeaseResources: test notification")
    p.add_argument("--body", default="If you can read this, the dispatcher's notifications reach you.")
    sub.add_parser("dispatch")
    sub.add_parser("watch")
    p = sub.add_parser("_launch")
    p.add_argument("machine")
    p.add_argument("ident")
    p.add_argument("launch")
    p = sub.add_parser("_runner")
    p.add_argument("machine")
    p.add_argument("ident")
    p.add_argument("launch")
    args = parser.parse_args()
    config = load_config()
    if args.cmd not in ("_runner", "_launch"):
        seed_local_state(THIS_MACHINE)
    handlers = {"now": cmd_now, "mine": cmd_mine, "trial": cmd_trial, "request": cmd_request, "extend": cmd_extend,
                "release": cmd_release, "run": cmd_run, "wait": cmd_wait, "leases": cmd_leases, "status": cmd_status,
                "compliance": cmd_compliance, "notify-test": cmd_notify_test, "dispatch": cmd_dispatch, "watch": cmd_watch, "_runner": cmd_runner, "_launch": cmd_launch}
    handlers[args.cmd](args, config)


if __name__ == "__main__":
    main()
