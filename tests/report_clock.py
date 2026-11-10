"""Report slots: the newest report is read whatever the clock does, and nothing is deleted once a machine runs it.

Since 2026-11-09 a dispatcher overwrites the oldest of five fixed files per kind (reports/ and beats/, slot-0.json to
slot-4.json) instead of writing a new <stamp>.json and deleting older ones (every deletion left a record that all
Resilio peers keep and walk for good). The 2026-10-27 case stays covered: a time zone change (Pulvermaar,
2026-10-26) must not make a new report look older. This switches the process to a time zone six hours
behind between writes, with leftover <stamp>.json reports from before, and checks what is kept and what is read; then
that slots written while the clock ran ahead are overwritten first, and that a slot that cannot be written is skipped.

    ~/venvs/LeaseResources/bin/python -B tests/report_clock.py [path to res.py]    (default: the live one)
"""
import datetime, importlib.util, json, os, shutil, sys, tempfile, time
from pathlib import Path

sys.dont_write_bytecode = True
SCRIPT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "res.py").resolve()
sys.path.insert(0, str(SCRIPT.parent))                  # res.py imports probes.py from beside it
spec = importlib.util.spec_from_file_location("res_under_test", SCRIPT)
res = importlib.util.module_from_spec(spec)
spec.loader.exec_module(res)
scratch = Path(tempfile.mkdtemp(prefix="leaseresources-clock-"))
res.LEASES_ROOT = scratch
folder = scratch / "Testmachine" / "reports"
folder.mkdir(parents=True)


def set_zone(zone: str) -> None:
    os.environ["TZ"] = zone
    time.tzset()


failures = []


def expect(label: str, cond: bool, detail="") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else "  " + str(detail)))
    if not cond:
        failures.append(label)


set_zone("Europe/Paris")
local = datetime.datetime.now()
for minutes in [2, 1]:                          # what Pulvermaar had: local-time names from before the switch
    old = folder / (local - datetime.timedelta(minutes=minutes)).strftime("%y%m%d-%H%M%S-000.json")
    old.write_text(json.dumps({"n": -1}))
    os.utime(old, (time.time() - 60 * minutes,) * 2)
(folder / "261109-120000-000Z.json").write_text(json.dumps({"n": -2, "updated": "2026-11-09T07:00:00-05:00"}))
res.write_report("Testmachine", "reports", {"n": 0, "updated": res.now_iso()})
expect("a report written before the clock went back is the newest", res.read_report("Testmachine", "reports")["n"] == 0)
set_zone("America/Bogota")                     # the local clock goes back 6 hours
for n in range(1, 8):
    time.sleep(0.01)
    res.write_report("Testmachine", "reports", {"n": n, "updated": res.now_iso()})
    names = sorted(p.name for p in folder.glob("*.json"))
    expect("report " + str(n) + " after the clock went back is kept and read", res.read_report("Testmachine", "reports")["n"] == n, names)
expect("five reports kept, the five slots", names == ["slot-%d.json" % i for i in range(5)], names)
expect("the leftover reports of the code before are gone", all(json.loads((folder / n).read_text())["n"] >= 0 for n in names), names)
expect("the kept ones are the last five written", sorted(json.loads((folder / n).read_text())["n"] for n in names) == [3, 4, 5, 6, 7], names)
expect("report_names lists the slots newest first", [p.name for p in res.report_names("Testmachine", "reports")][0]
       == next(n for n in names if json.loads((folder / n).read_text())["n"] == 7))

# beats are a kind of their own, with their own five slots
for n in range(3):
    res.write_report("Testmachine", "beats", {"b": n, "updated": res.now_iso()})
beats = sorted(p.name for p in (scratch / "Testmachine" / "beats").glob("*.json"))
expect("beats go into their own slots", beats == ["slot-0.json", "slot-1.json", "slot-2.json"], beats)
expect("read_published puts the newest beat over the newest report",
       res.read_published("Testmachine").get("b") == 2 and res.read_published("Testmachine").get("n") == 7)

# a clock that ran ahead: slots written then hold a future time and must not stay the newest once the clock is right
ahead = time.time() + 3 * 3600
for n in ["slot-1.json", "slot-3.json"]:
    future = datetime.datetime.fromtimestamp(ahead).astimezone().isoformat(timespec="seconds")
    (folder / n).write_text(json.dumps({"n": 100, "updated": future}))
    os.utime(folder / n, (ahead, ahead))
expect("a report written while the clock ran ahead reads as newest", res.read_report("Testmachine", "reports")["n"] == 100)
expect("written", res.write_report("Testmachine", "reports", {"n": 9, "updated": res.now_iso()}) is True)
expect("after one write report 9 is read", res.read_report("Testmachine", "reports")["n"] == 9)
expect("both future slots now hold report 9", [json.loads((folder / n).read_text())["n"] for n in ["slot-1.json", "slot-3.json"]] == [9, 9])
res.write_report("Testmachine", "reports", {"n": 10, "updated": res.now_iso()})
expect("the next report goes into one slot only", [json.loads((folder / n).read_text())["n"] for n in names].count(10) == 1)

# a slot Resilio holds (Windows refuses the rename) is skipped for the next oldest
oldest = min((folder / n for n in names), key=lambda p: p.stat().st_mtime)
real_write, logged = res.write_json, []
def refusing(path, data):
    if Path(path) == oldest:
        raise PermissionError(13, "held by Resilio", str(path))
    real_write(path, data)
res.write_json, real_log, res.log = refusing, res.log, lambda machine, text: logged.append(text)
res.write_report("Testmachine", "reports", {"n": 11, "updated": res.now_iso()})
expect("a held slot is skipped: report 11 is read", res.read_report("Testmachine", "reports")["n"] == 11)
expect("the held slot is unchanged", json.loads(oldest.read_text())["n"] != 11)
res.write_json = lambda path, data: (_ for _ in ()).throw(PermissionError(13, "held", str(path)))
before = {n: (folder / n).read_text() for n in names}
expect("with every slot held write_report says so", res.write_report("Testmachine", "reports", {"n": 12}) is False)
expect("... logs it once and changes nothing", len(logged) == 1 and "every slot refused" in logged[0]
       and before == {n: (folder / n).read_text() for n in names}, logged)
res.write_json, res.log = real_write, real_log
time.sleep(1.1)                                 # `updated` has whole seconds; a file time within the same one could lose
res.write_report("Testmachine", "reports", {"n": 13})           # no `updated`: ordered by its file time
expect("a report without `updated` is read by its file time", res.read_report("Testmachine", "reports")["n"] == 13)
expect("still exactly the five slots", sorted(p.name for p in folder.glob("*.json")) == names)
shutil.rmtree(scratch)
print(str(len(failures)) + " failed" if failures else "all passed")
sys.exit(1 if failures else 0)
