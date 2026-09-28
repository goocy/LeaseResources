"""Snapshot the process table around the Claude processes, to see what the unleased-command detector sees.

Written 2026-09-27 for Moosfels, where every Bash-tool command (Git Bash / MSYS) escaped the detector. Start it detached
just before a long foreground Bash command, so it samples while the command runs:

    PowerShell tool:  & $PY -B tools/snapshot_tree.py --detach --delay 20
    then Bash tool:   sleep 150

(`$PY` is the venv's python.) It waits --delay seconds, then takes --count snapshots --every seconds apart. For every
process that is a Claude process, a descendant of one, carries CLAUDECODE in its environment, or is a shell, sleep or
python, it records pid, ppid, whether the parent exists and is older (Windows reuses pids), create time, name, exe,
command line, user, whether psutil can read its environment (and the error if not), and the CLAUDECODE, CLAUDE_PID,
CLAUDE_CODE_SESSION_ID and LEASERES_LEASE values. It also runs the current detector (res.py beside it) against a
scratch folder with a short threshold (--after, default 15 s) and stores what it would record.

Output: snapshot-<machine>-<yyMMdd-HHMMSS>.json in the machine's local LeaseResources log folder (since 2026-09-28;
~/Library/Logs/LeaseResources or %LOCALAPPDATA%\\LeaseResources\\logs; pass --out to put it elsewhere, e.g. to share it) and a
readable tree in a .txt beside it. Needs the LeaseResources venv (psutil). No regular expressions (house rule).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
TOOLS = Path(__file__).resolve().parent
SERVICE = TOOLS.parent

try:
    import psutil
except ImportError:
    raise SystemExit("psutil is missing: run this with the LeaseResources venv's python (~/venvs/LeaseResources).")

INTERESTING = ["bash", "sh", "zsh", "sleep", "cmd", "powershell", "pwsh", "python", "pythonw", "py", "conhost", "node",
               "claude", "mintty", "git"]
ENV_KEYS = ["CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "LEASERES_LEASE", "MSYSTEM"]


def local_logs() -> Path:
    """The machine's LeaseResources log folder (not synced since 2026-09-28), as res.py computes it."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "LeaseResources" / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "LeaseResources"
    return Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / "LeaseResources" / "logs"


def stem(name: str) -> str:
    name = (name or "").casefold()
    return name[:-4] if name.endswith(".exe") else name


def iso(stamp) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(stamp)).isoformat(timespec="milliseconds")
    except (TypeError, ValueError, OSError):
        return ""


def snapshot() -> list:
    rows = {}
    for proc in psutil.process_iter(["pid", "ppid", "name", "create_time", "cmdline", "exe", "username"]):
        data = dict(proc.info)
        env_state, env_values = "readable", {}
        try:
            env = proc.environ()
            env_values = {k: env.get(k) for k in ENV_KEYS if k in env}
            if not env:
                env_state = "empty"
        except psutil.AccessDenied:
            env_state = "AccessDenied"
        except psutil.NoSuchProcess:
            env_state = "gone"
        except (psutil.Error, OSError) as exc:
            env_state = type(exc).__name__ + ": " + str(exc)[:80]
        data["environ"] = env_state
        data["env"] = env_values
        rows[data["pid"]] = data
    claude_pids = {pid for pid, d in rows.items() if stem(d.get("name")) == "claude"}
    for d in rows.values():
        named = str(d["env"].get("CLAUDE_PID") or "")
        if named.isdigit() and int(named) in rows:
            claude_pids.add(int(named))
    children = {}
    for pid, d in rows.items():
        children.setdefault(d.get("ppid"), []).append(pid)
    related = set(claude_pids)
    stack = list(claude_pids)
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in related:
                related.add(child)
                stack.append(child)
    for pid, d in rows.items():
        if d["env"].get("CLAUDECODE") or stem(d.get("name")) in INTERESTING:
            related.add(pid)
    out = []
    for pid in sorted(related):
        d = rows[pid]
        parent = rows.get(d.get("ppid"))
        d["parent_exists"] = parent is not None
        d["parent_older"] = bool(parent) and float(parent.get("create_time") or 0) <= float(d.get("create_time") or 0) + 1.0
        d["parent_name"] = parent.get("name") if parent else None
        d["is_claude"] = pid in claude_pids
        d["created"] = iso(d.get("create_time"))
        out.append(d)
    return out


def tree_text(rows: list) -> str:
    by_pid = {r["pid"]: r for r in rows}
    kids = {}
    for r in rows:
        kids.setdefault(r.get("ppid"), []).append(r["pid"])
    roots = [r["pid"] for r in rows if r.get("ppid") not in by_pid or not r.get("parent_older")]
    lines = []

    def show(pid, depth, seen):
        if pid in seen:
            return
        seen.add(pid)
        r = by_pid[pid]
        cmd = " ".join(r.get("cmdline") or [])[:160]
        flags = ("CLAUDE " if r["is_claude"] else "") + ("env:" + r["environ"])
        marks = ",".join(k + "=" + str(v)[:12] for k, v in r["env"].items())
        lines.append("  " * depth + str(pid) + " <" + str(r.get("ppid")) + (" dead-parent" if not r["parent_exists"] else "")
                     + (" younger-parent" if r["parent_exists"] and not r["parent_older"] else "") + "> " + str(r.get("name"))
                     + " [" + r["created"][11:19] + "] " + flags + (" " + marks if marks else "") + " :: " + cmd)
        for child in sorted(kids.get(pid, [])):
            if by_pid[child].get("parent_older"):
                show(child, depth + 1, seen)

    seen = set()
    for root in sorted(roots):
        show(root, 0, seen)
    return "\n".join(lines)


def detector(after: float):
    """A Dispatcher from res.py beside this folder, writing to a scratch folder, with a short threshold."""
    scratch = Path(tempfile.mkdtemp(prefix="leaseres-snapshot-"))
    os.environ["LEASERES_DATA"] = str(scratch / "data")
    os.environ["LEASERES_LOCAL"] = str(scratch / "local")
    os.environ["LEASERES_LOGS"] = str(scratch / "logs")
    sys.path.insert(0, str(SERVICE))
    import res
    d = res.Dispatcher(res.load_config())
    d.config = dict(d.config, unleased_after_seconds=after)
    return d, d.folder / "unleased.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot the process table around Claude processes.")
    parser.add_argument("--delay", type=float, default=20)
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--every", type=float, default=15)
    parser.add_argument("--after", type=float, default=15, help="detector threshold for the dry run, seconds")
    parser.add_argument("--out")
    parser.add_argument("--detach", action="store_true", help="start again detached and return at once")
    args = parser.parse_args()
    if args.detach:
        argv = [sys.executable, "-B", str(Path(__file__).resolve()), "--delay", str(args.delay), "--count", str(args.count),
                "--every", str(args.every), "--after", str(args.after)] + (["--out", args.out] if args.out else [])
        if sys.platform == "win32":
            flags = 0x00000008 | 0x00000200            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            try:
                child = subprocess.Popen(argv, creationflags=flags | 0x01000000, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                child = subprocess.Popen(argv, creationflags=flags, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            child = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("snapshot_tree started detached (pid " + str(child.pid) + "); first snapshot in " + format(args.delay, "g")
              + " s, output in " + str(local_logs()))
        return
    machine = socket.gethostname().partition(".")[0]
    stamp = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
    out = Path(args.out) if args.out else local_logs() / ("snapshot-" + machine + "-" + stamp + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        disp, rows_path = detector(args.after)
    except Exception as exc:                            # the snapshot is still worth having without the dry run
        disp, rows_path = None, None
        detector_error = type(exc).__name__ + ": " + str(exc)
    else:
        detector_error = ""
    time.sleep(args.delay)
    result = {"machine": machine, "python": sys.version, "psutil": psutil.__version__, "platform": sys.platform,
              "me": os.getpid(), "detector_error": detector_error, "snapshots": []}
    texts = []
    for index in range(args.count):
        rows = snapshot()
        entry = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "processes": rows}
        if disp is not None:
            try:
                disp.track_shells()
                disp.check_unleased()
                entry["detector_members"] = sorted(disp.tool_members)
                entry["detector_pending"] = sorted(disp.unleased_pending)
            except Exception as exc:
                entry["detector_error"] = type(exc).__name__ + ": " + str(exc)
        result["snapshots"].append(entry)
        texts.append("== snapshot " + str(index + 1) + " at " + entry["at"] + "\n" + tree_text(rows))
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=1, default=str)
        if index + 1 < args.count:
            time.sleep(args.every)
    if rows_path is not None and rows_path.exists():
        result["detector_records"] = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        texts.append("== detector records (threshold " + format(args.after, "g") + " s)\n"
                     + "\n".join(json.dumps(r)[:300] for r in result["detector_records"]))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, default=str)
    out.with_suffix(".txt").write_text("\n\n".join(texts) + "\n", encoding="utf-8")
    print("wrote " + str(out) + " and " + str(out.with_suffix(".txt")))


if __name__ == "__main__":
    main()
