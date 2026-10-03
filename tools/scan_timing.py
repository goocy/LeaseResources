"""Where a compliance scan (Dispatcher.check_unleased in res.py) spends its time, first pass against repeated passes.

Written 2026-10-03 on Moosfels, where the dispatcher used a third of a core all day. A first pass over the process table
says little about a running dispatcher: psutil keeps its Process objects between calls of process_iter, and with them
parent, name and create time, but not the command line. So everything here is measured in one process, pass after pass.

    scan_timing.py attrs <ppid|create_time|name|cmdline|username|all|none> [passes]
        process_iter(["pid", <attr>]) several times: pass 1 is a first pass, the rest are what a running dispatcher pays.
    scan_timing.py parts [rounds]
        after one warm pass: the cost of each attribute alone, called on the cached Process objects.
    scan_timing.py scan <folder with res.py> [passes] [profile]
        check_unleased() of that folder's res.py against a scratch state folder; 'profile' adds a cProfile of two
        further passes.
    scan_timing.py compare <old folder> <new folder> [rounds] [threshold seconds]
        both detectors side by side on the live process table with a short threshold, so that records are made: the
        sets they keep, the records they write, and (when the new one has read_cmdline) every command line read both ways.
    scan_timing.py full <old folder> <new folder>
        parts, scan of each with a profile, and compare, each in a process of its own (about 70 s: a leased job).

Every line is flushed, so a run that is cut off still shows what it measured. State goes to a temp folder that is
deleted at the end; the desk lookup (a mail.py subprocess per new record) is replaced by a constant. Needs the
LeaseResources venv (psutil). No regular expressions (house rule).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True

try:
    import psutil
except ImportError:
    raise SystemExit("psutil is missing: run this with the LeaseResources venv's python (~/venvs/LeaseResources).")

SIX = ["pid", "ppid", "name", "create_time", "cmdline", "username"]


class Clock:
    def __init__(self):
        self.wall = time.perf_counter()
        self.cpu = os.times()

    def read(self) -> str:
        wall = time.perf_counter() - self.wall
        cpu = os.times()
        user, system = cpu[0] - self.cpu[0], cpu[1] - self.cpu[1]
        return "wall " + format(wall, ".3f") + " s, cpu " + format(user + system, ".3f") + " s (user " + format(user, ".3f") \
            + ", system " + format(system, ".3f") + ")"


def say(*parts) -> None:
    print(*parts, flush=True)


def attrs_mode(name: str, passes: int) -> None:
    wanted = {"all": SIX, "none": None}.get(name, ["pid", name])
    psutil.process_iter.cache_clear()
    for index in range(passes):
        clock = Clock()
        count = 0
        for _ in psutil.process_iter(wanted):
            count += 1
        say("process_iter(" + str(wanted) + ") pass " + str(index + 1) + ": " + str(count) + " processes, " + clock.read())


def parts_mode(rounds: int) -> None:
    clock = Clock()
    procs = list(psutil.process_iter(SIX))
    say("warm pass, all six: " + str(len(procs)) + " processes, " + clock.read())
    for round_index in range(rounds):
        say("round " + str(round_index + 1) + ", each attribute alone on the cached Process objects:")
        clock = Clock()
        psutil.pids()
        say("  pids(): " + clock.read())
        clock = Clock()
        again = list(psutil.process_iter())
        say("  process_iter() without attributes: " + str(len(again)) + " processes, " + clock.read())
        for attr in SIX[1:]:
            clock = Clock()
            failed = 0
            for proc in again:
                try:
                    getattr(proc, attr)()
                except psutil.Error:
                    failed += 1
            say("  " + attr + ": " + clock.read() + ", " + str(failed) + " raised")
        clock = Clock()
        for proc in again:
            try:
                proc.as_dict(attrs=SIX)
            except psutil.Error:
                pass
        say("  as_dict(all six): " + clock.read())


def detector(folder: str, name: str, scratch: Path, after=None):
    """A Dispatcher from the res.py of that folder, loaded as a module of its own name, its state in scratch."""
    os.environ["LEASERES_DATA"] = str(scratch / "data")
    os.environ["LEASERES_LOCAL"] = str(scratch / "local")
    os.environ["LEASERES_LOGS"] = str(scratch / "logs")
    spec = importlib.util.spec_from_file_location(name, str(Path(folder) / "res.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    (scratch / "local").mkdir(parents=True, exist_ok=True)
    disks = module.default_local_dir("state") / "disks.json"
    if disks.exists():                                # the machine's disk map, so the meter does not build one
        shutil.copy2(disks, scratch / "local" / "disks.json")
    disp = module.Dispatcher(module.load_config())
    if after is not None:
        disp.config = dict(disp.config, unleased_after_seconds=after)
    disp.desk_of = lambda cwd: disp.machine + ":?"
    path = Path(module.__file__).resolve()
    say(name + ": " + str(path) + ", " + str(path.stat().st_size) + " bytes")
    return disp


def scan_mode(folder: str, passes: int, profile: bool) -> None:
    scratch = Path(tempfile.mkdtemp(prefix="leaseres-scan-timing-"))
    try:
        say("psutil " + psutil.__version__ + ", " + str(len(psutil.pids())) + " processes")
        clock = Clock()
        disp = detector(folder, "res_scan", scratch)
        say("Dispatcher built: " + clock.read())
        for index in range(passes):
            clock = Clock()
            disp.track_shells()
            disp.check_unleased()
            say("check_unleased pass " + str(index + 1) + ": " + clock.read() + "; members " + str(len(disp.tool_members))
                + ", pending " + str(len(disp.unleased_pending)) + ", seen " + str(len(disp.unleased_seen)))
        if profile:
            import cProfile
            import io
            import pstats
            prof = cProfile.Profile()
            prof.enable()
            disp.check_unleased()
            disp.check_unleased()
            prof.disable()
            for order in ["tottime", "cumulative"]:
                text = io.StringIO()
                pstats.Stats(prof, stream=text).sort_stats(order).print_stats(12)
                say("two more passes under cProfile, by " + order + ":")
                for line in text.getvalue().splitlines():
                    if line.strip() and "Ordered by" not in line and "List reduced" not in line:
                        say("  " + line[:170])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def compare_mode(old_folder: str, new_folder: str, rounds: int, after: float) -> None:
    scratch = Path(tempfile.mkdtemp(prefix="leaseres-scan-timing-"))
    try:
        old = detector(old_folder, "res_old", scratch / "old", after)
        new = detector(new_folder, "res_new", scratch / "new", after)

        def describe(keys) -> str:
            out = []
            for key in sorted(keys)[:6]:
                try:
                    proc = psutil.Process(int(key.partition("-")[0]))
                    out.append(key + " " + proc.name() + " age " + format(time.time() - proc.create_time(), ".0f") + " s")
                except psutil.Error:
                    out.append(key + " (gone)")
            return "; ".join(out)

        for index in range(rounds):
            clock = Clock()
            old.check_unleased()
            old_time = clock.read()
            clock = Clock()
            new.check_unleased()
            say("round " + str(index + 1) + ": old " + old_time + "; new " + clock.read())
            for what in ["tool_members", "unleased_pending", "unleased_seen"]:
                a, b = set(getattr(old, what)), set(getattr(new, what))
                say("  " + what + ": old " + str(len(a)) + ", new " + str(len(b)) + ", only old " + str(len(a - b))
                    + ", only new " + str(len(b - a)))
                if a - b:
                    say("    only old: " + describe(a - b))
                if b - a:
                    say("    only new: " + describe(b - a))
            if hasattr(new, "cmdline_refused"):
                say("  refusals remembered by new: " + str(len(new.cmdline_refused)))
            if index + 1 < rounds:
                time.sleep(1.0)

        def records(disp) -> set:
            rows = set()
            for path in disp.folder.glob("unleased*.jsonl"):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        row = json.loads(line)
                        rows.add((row.get("event"), row.get("key"), row.get("how"), tuple(row.get("commands") or [])))
            return rows

        a, b = records(old), records(new)
        say("records written: old " + str(len(a)) + ", new " + str(len(b)) + "; identical in event, key, how and commands: "
            + str(len(a & b)) + ", only old " + str(len(a - b)) + ", only new " + str(len(b - a)))
        for row in sorted(a - b, key=str)[:5]:
            say("  only old: " + str(row)[:200])
        for row in sorted(b - a, key=str)[:5]:
            say("  only new: " + str(row)[:200])
        if not hasattr(new, "read_cmdline"):
            return
        same = different = refused = remembered = gone = 0
        for proc in psutil.process_iter(["pid", "create_time", "username", "cmdline"]):
            data = {k: proc.info[k] for k in ["pid", "create_time", "username"]}
            key = str(data["pid"]) + "-" + format(float(data.get("create_time") or 0), ".2f")
            known = key in new.cmdline_refused
            if not new.read_cmdline(proc, data):
                gone += 1
            elif data["cmdline"] == proc.info["cmdline"]:
                same += 1
                refused += 1 if data["cmdline"] is None else 0
                remembered += 1 if known else 0
            else:
                different += 1
                say("  DIFFERENT " + key + " " + str(proc.info.get("username")) + ": process_iter " + str(proc.info["cmdline"])[:80]
                    + " | read_cmdline " + str(data["cmdline"])[:80])
        say("command lines, process_iter against read_cmdline: " + str(same) + " equal (" + str(refused) + " of them refused both"
            + " ways, " + str(remembered) + " answered from the remembered refusals), " + str(different) + " different, "
            + str(gone) + " ended in between")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def full_mode(old_folder: str, new_folder: str) -> None:
    me = str(Path(__file__).resolve())
    steps = [["parts", "2"], ["scan", old_folder, "6", "profile"], ["scan", new_folder, "6", "profile"],
             ["compare", old_folder, new_folder, "4", "15"]]
    for step in steps:
        say("")
        say("==== " + " ".join(step))
        subprocess.run([sys.executable, "-B", me] + step)


def main() -> None:
    args = sys.argv[1:]
    mode = args[0] if args else ""
    if mode == "attrs" and len(args) >= 2:
        attrs_mode(args[1], int(args[2]) if len(args) > 2 else 4)
    elif mode == "parts":
        parts_mode(int(args[1]) if len(args) > 1 else 2)
    elif mode == "scan" and len(args) >= 2:
        scan_mode(args[1], int(args[2]) if len(args) > 2 else 4, "profile" in args[3:])
    elif mode == "compare" and len(args) >= 3:
        compare_mode(args[1], args[2], int(args[3]) if len(args) > 3 else 3, float(args[4]) if len(args) > 4 else 15.0)
    elif mode == "full" and len(args) >= 3:
        full_mode(args[1], args[2])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
