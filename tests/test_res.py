"""Sandbox tests for LeaseResources: scratch folders, real res.py subprocesses, real dispatchers, a stub for ClaudeMail.

Run with the venv's python:  ~/venvs/LeaseResources/bin/python -B tests/test_res.py [-v] [TestName ...]
Scratch goes under LEASERES_TEST_SCRATCH (default: the system temp folder). Measured numbers are printed as
'MEASURED ...' lines and collected in <scratch>/results.json. POSIX only for now (process-group kills, a copied bash
standing in for a Claude process); the Windows paths of res.py are untested.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SERVICE = HERE.parent
WINDOWS = sys.platform == "win32"
PYTHON = sys.executable
SCRATCH = Path(os.environ.get("LEASERES_TEST_SCRATCH") or tempfile.gettempdir()) / ("leaseres-tests-" + time.strftime("%H%M%S"))
RESULTS = {}
# The suite runs its own copy of the code, never the synced service files: touching those (the keeper test did) restarts
# the production dispatcher on every machine (found on Moosfels, 2026-09-27).
CODE = SCRATCH / "code"
CODE.mkdir(parents=True, exist_ok=True)
for _name in ["res.py", "probes.py"]:
    shutil.copy2(SERVICE / _name, CODE / _name)
RES = CODE / "res.py"
sys.path.insert(0, str(CODE))

import psutil  # noqa: E402


WORLDS = []
POSIX_ONLY = unittest.skipIf(sys.platform == "win32", "POSIX process groups / sessions; see Running-on.md for the Windows re-test")


class Base(unittest.TestCase):
    """Every test stops everything its sandboxes started, pass or fail (22 dispatchers were left behind on Moosfels)."""

    def tearDown(self):
        while WORLDS:
            WORLDS.pop().stop()


STUB_MAIL = '''
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
if args and args[0] == "whoami":
    print("machine stub")
    print("session none")
    print("desk    " + os.environ.get("STUB_DESK", "Altiplano:TestDesk") + "  (stub)")
    sys.exit(0)
if args and args[0] == "desk-of":
    time.sleep(float(os.environ.get("STUB_DESK_SLEEP", "0")))
    for folder in args[1:]:
        print(os.environ.get("STUB_DESK_OF", "StubDesk") + "\\t" + folder)
    sys.exit(0)
if args and args[0] == "send":
    record = {"machine": os.environ.get("LEASERES_MACHINE", ""), "at": time.time(), "argv": args}
    for i, a in enumerate(args):
        if a in ("--to", "--subject", "--body-file") and i + 1 < len(args):
            record[a[2:]] = args[i + 1]
    record["body"] = Path(record["body-file"]).read_text(encoding="utf-8")
    with open(Path(__file__).with_name("mail.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\\n")
    print("sent (stub) to " + record.get("to", ""))
    sys.exit(0)
sys.exit(2)
'''

JOBS = {
    "sleepy.py": '''
import sys, time
n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
for i in range(n):
    print("tick", i)
    time.sleep(1)
print("done")
''',
    "ramhog.py": '''
import sys, time
mb, seconds = int(sys.argv[1]), float(sys.argv[2])
block = bytearray(mb * 1000 * 1000)
for i in range(0, len(block), 4096):
    block[i] = 1
print("holding", mb, "MB", flush=True)
end = time.time() + seconds
while time.time() < end:
    print("still holding", flush=True)
    time.sleep(1)
print("done")
''',
    "silent.py": '''
import sys, time
print("one line, then silence")
time.sleep(float(sys.argv[1]))
print("done")
''',
    "gpuuser.py": '''
import json, os, sys, time
path, use, seconds = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
def write(active):
    try:
        data = json.load(open(path))
    except Exception:
        data = {"total": 0, "pids": {}}
    pids = data.get("pids", {})
    if active:
        pids[str(os.getpid())] = use
    else:
        pids.pop(str(os.getpid()), None)
    data = {"total": sum(pids.values()), "pids": pids}
    json.dump(data, open(path + ".tmp", "w"))
    os.replace(path + ".tmp", path)
write(True)
print("using gpu", use, flush=True)
time.sleep(seconds)
write(False)
print("done")
''',
    "fail.py": '''
print("about to fail")
raise SystemExit(3)
''',
}

TRIAL_TREE = '''
import json, os, socket, subprocess, sys, time
out = sys.argv[1]
gpu = sys.argv[2]
py = sys.executable
pids = {"main": os.getpid()}
sleeper = "import time; time.sleep(600)"
a = subprocess.Popen([py, "-c", sleeper])
b = subprocess.Popen([py, "-c", "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); time.sleep(600)"])
c = subprocess.Popen([py, "-c", "import os, time; os.setsid(); time.sleep(600)"])
d = subprocess.Popen([py, "-c", "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'], start_new_session=True); time.sleep(2)"])
burn = "import time\\nend = time.time() + 4\\nwhile time.time() < end: pass\\ntime.sleep(600)"
e = subprocess.Popen([py, "-c", burn])
f = subprocess.Popen([py, "-c", burn])
pids.update(a=a.pid, b=b.pid, c=c.pid, d=d.pid, e=e.pid, f=f.pid)
time.sleep(1.0)
import psutil
pids["all"] = [p.pid for p in psutil.Process().children(recursive=True)] + [os.getpid()]
json.dump(pids, open(out, "w"))
json.dump({"total": 70, "pids": {str(os.getpid()): 70}}, open(gpu, "w"))
block = bytearray(400 * 1000 * 1000)
for i in range(0, len(block), 4096):
    block[i] = 1
with open(out + ".bin", "wb") as fh:
    chunk = os.urandom(1 << 20)
    for i in range(300):
        fh.write(chunk)
        fh.flush()
        os.fsync(fh.fileno())      # every block: Windows' write cache otherwise keeps it off the disk counters
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
payload = b"x" * 1400
sent = 0
end = time.time() + 3
while time.time() < end and sent < 60 * 1000 * 1000:
    try:
        sock.sendto(payload, ("192.0.2.2", 9))
        sent += len(payload)
    except OSError:
        time.sleep(0.001)
print("sent", sent, "bytes of UDP", flush=True)
pids["all2"] = [p.pid for p in psutil.Process().children(recursive=True)]
json.dump(pids, open(out, "w"))
time.sleep(600)
'''


FAKE_CLAUDE_C = """#include <stdlib.h>
#include <unistd.h>
#include <sys/wait.h>
/* Stands in for a Claude process: runs argv[1] in a child shell that leads its own session, as Claude's Bash tool
   does, and waits for it. A copied Apple binary would be SIGKILLed by code signing, hence a compiled stub. */
int main(int argc, char **argv) {
    if (argc < 2) return 0;
    pid_t pid = fork();
    if (pid == 0) { setsid(); execl("/bin/sh", "sh", "-c", argv[1], (char *)0); _exit(127); }
    int status = 0;
    waitpid(pid, &status, 0);
    return 0;
}
"""


FAKE_CLAUDE_PY = """import os, subprocess, sys
# Stands in for a Claude process where no compiled stub is available (Windows): it is found through the CLAUDE_PID
# its children inherit, as the detector finds a Claude process of any name, and runs argv[1] through the platform
# shell (cmd.exe /c on Windows, /bin/sh -c in a new session elsewhere), as the tools do.
os.environ["CLAUDE_PID"] = str(os.getpid())
os.environ["CLAUDECODE"] = "1"
extra = {} if os.name == "nt" else {"start_new_session": True, "executable": "/bin/sh"}
sys.exit(subprocess.call(sys.argv[1], shell=True, **extra))
"""


def fake_claude(world, case) -> list:
    """The command prefix of a stand-in Claude process: a compiled stub named claude where a C compiler exists (macOS,
    Linux), otherwise a Python script identified through CLAUDE_PID (Windows)."""
    if not WINDOWS and shutil.which("cc") is not None and os.environ.get("LEASERES_TEST_FAKE") != "python":
        source = world.root / "fake_claude.c"
        source.write_text(FAKE_CLAUDE_C)
        fake = world.root / "claude"
        subprocess.run(["cc", "-o", str(fake), str(source)], check=True)
        return [str(fake)]
    script = world.root / "fake_claude.py"
    script.write_text(FAKE_CLAUDE_PY)
    return [PYTHON, str(script)]


def q(path) -> str:
    """A path quoted for the platform shell."""
    return '"' + str(path) + '"' if WINDOWS else "'" + str(path) + "'"


class T11Mine(Base):
    """res mine: the per-process cpu and disk columns show real rates (cpu was always 0.00 before 2026-09-27 10:00)."""

    def test_per_process_columns(self):
        w = World("mine")
        fake = fake_claude(w, self)
        (w.allowed / "busy.py").write_text("import time\nend = time.time() + 5\nwhile time.time() < end: pass\n")
        (w.allowed / "writer.py").write_text(
            "import os, time\nchunk = os.urandom(1 << 20)\nend = time.time() + 5\n"
            "with open(os.path.join(os.path.dirname(__file__), 'w.bin'), 'wb') as fh:\n"
            "    while time.time() < end:\n        fh.write(chunk)\n        fh.flush()\n        os.fsync(fh.fileno())\n")
        out = w.root / "mine.txt"
        env = w.env("Altiplano")
        if WINDOWS:
            cmd = ('start "" /b ' + q(PYTHON) + ' busy.py & start "" /b ' + q(PYTHON) + ' writer.py & ' + q(PYTHON)
                   + ' -c "import time; time.sleep(1.5)" & ' + q(PYTHON) + ' -B ' + q(RES) + ' mine > ' + q(out))
        else:
            cmd = (q(PYTHON) + " busy.py & " + q(PYTHON) + " writer.py & sleep 1.5; " + q(PYTHON) + " -B " + q(RES)
                   + " mine > " + q(out) + "; wait")
        subprocess.run(fake + [cmd], cwd=str(w.allowed), env=env, timeout=60)
        text = out.read_text()
        rows = {}
        for line in text.splitlines():
            words = line.split()
            if words and words[0].isdigit() and "sh -c" not in line and "cmd.exe" not in line and ("busy.py" in line or "writer.py" in line):
                rows["busy" if "busy.py" in line else "writer"] = line
        measured("mine.busy_row", " ".join(rows.get("busy", "").split()[:8]))
        measured("mine.writer_row", " ".join(rows.get("writer", "").split()[:8]))
        self.assertIn("Claude session process", text)
        busy_cpu = float(rows["busy"].split()[3])
        self.assertGreaterEqual(busy_cpu, 0.5)
        self.assertTrue("MB/s" in rows["writer"] or "GB/s" in rows["writer"], rows["writer"])


class T12Escapes(Base):
    """Compliance: long jobs that left their tool shell are counted too: nohup/& after the shell exited (session id of
    the gone shell, works even for Apple binaries whose environment macOS hides) and setsid (CLAUDECODE in the
    environment); a leased job started from the same session is not counted."""

    def test_backgrounded_jobs_counted(self):
        w = World("escapes")
        fake = fake_claude(w, self)
        (w.allowed / "sleeper.py").write_text("import sys, time\ntime.sleep(float(sys.argv[1]))\n")
        w.dispatcher("Altiplano")
        env = w.env("Altiplano", CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="fake-session")
        lid = lease_id(w.res("Altiplano", "request", "--need", "cpu=0.5", "--for", "1m", "--desk", "Altiplano:Owner").stdout)
        leased = w.res("Altiplano", "run", "--lease", lid, "--", PYTHON, str(w.allowed / "sleeper.py"), "14", CLAUDECODE="1")
        self.assertEqual(leased.returncode, 0, leased.stdout + leased.stderr)
        if WINDOWS:
            jobs = ['echo shell-snapshots-fake >nul & start "" /b ' + q(PYTHON) + ' sleeper.py 15']
        else:
            jobs = [
                "true shell-snapshots-fake; nohup " + PYTHON + " sleeper.py 15 >/dev/null 2>&1 &",
                "true shell-snapshots-fake; nohup /bin/sleep 17 >/dev/null 2>&1 & sleep 3",
                "true shell-snapshots-fake; " + PYTHON + " -c 'import os, time; os.setsid(); time.sleep(16)' >/dev/null 2>&1 &",
            ]
        for job in jobs:
            subprocess.Popen(fake + [job], cwd=str(w.allowed), env=env)

        def rows():
            path = w.data / "leases" / "Altiplano" / "unleased.jsonl"
            if not path.exists():
                return []
            return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

        def find(text):
            return [r for r in rows() if r["event"] == "seen" and any(text in c for c in r.get("commands", []))]
        nohup_py = w.wait(lambda: find("sleeper.py 15"), 25, "nohup python job")
        if WINDOWS:
            measured("escapes.detached_python_windows", {k: nohup_py[0][k] for k in ("how", "desk", "age", "session")})
            self.assertEqual(nohup_py[0]["how"], "left the Claude tree (environment marker)")
            time.sleep(3)
            self.assertFalse(find("sleeper.py 14"), "a leased job must not count as unleased")
            w.stop()
            return
        nohup_sleep = w.wait(lambda: find("sleep 17"), 25, "nohup /bin/sleep job")
        setsid_py = w.wait(lambda: find("time.sleep(16)"), 25, "setsid python job")
        measured("escapes.nohup_python", {k: nohup_py[0][k] for k in ("how", "desk", "age", "session")})
        measured("escapes.nohup_apple_sleep", {k: nohup_sleep[0][k] for k in ("how", "desk", "age")})
        measured("escapes.setsid_python", {k: setsid_py[0][k] for k in ("how", "desk", "age", "session")})
        self.assertEqual(nohup_sleep[0]["how"], "backgrounded, its tool shell gone")
        self.assertEqual(setsid_py[0]["how"], "left the Claude tree (environment marker)")
        self.assertEqual(setsid_py[0]["session"], "fake-session")
        time.sleep(3)
        self.assertFalse(find("sleeper.py 14"), "a leased job must not count as unleased")
        measured("escapes.leased_counted", len(find("sleeper.py 14")))
        w.wait(lambda: len([r for r in rows() if r["event"] == "ended"]) >= 3, 30, "ended records")
        w.stop()


def measured(key, value):
    RESULTS[key] = value
    print("MEASURED " + key + " = " + json.dumps(value), flush=True)


class World:
    """One sandbox: a data folder (the synced leases/ and logs/), a config folder, per-machine local folders, an
    allowed root with job scripts, a folder outside it, and a ClaudeMail stub."""

    def __init__(self, name: str, extra: dict = None):
        self.root = SCRATCH / name
        if self.root.exists():
            shutil.rmtree(self.root)
        self.data = self.root / "data"
        self.config = self.root / "config"
        self.allowed = self.root / "allowed"
        self.outside = self.root / "outside"
        for folder in [self.data, self.config, self.allowed, self.outside]:
            folder.mkdir(parents=True)
        (self.root / "mail.py").write_text(STUB_MAIL, encoding="utf-8")
        for name_, text in JOBS.items():
            (self.allowed / name_).write_text(text, encoding="utf-8")
        (self.outside / "evil.py").write_text("print('should never run')\n", encoding="utf-8")
        with open(SERVICE / "config" / "defaults.json", encoding="utf-8") as fh:
            config = json.load(fh)
        config.update({"mail_script": str(self.root / "mail.py"), "allowed_roots": [str(self.allowed)], "notify": False,
                       "tick_seconds": 0.5, "sample_seconds": 1, "smoothing_seconds": 3, "overrun_samples": 2,
                       "unleased_check_seconds": 2, "unleased_after_seconds": 3, "answer_wait_seconds": 8})
        config.update(extra or {})
        (self.config / "defaults.json").write_text(json.dumps(config, indent=1), encoding="utf-8")
        self.procs = []
        WORLDS.append(self)

    def env(self, machine: str, **extra) -> dict:
        env = dict(os.environ)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.update({"LEASERES_DATA": str(self.data), "LEASERES_CONFIG": str(self.config), "LEASERES_MACHINE": machine,
                    "LEASERES_LOCAL": str(self.root / ("local-" + machine))})
        env.update(extra)
        return env

    def res(self, machine: str, *args, cwd=None, timeout=60, **env):
        run = subprocess.run([PYTHON, "-B", str(RES)] + list(args), capture_output=True, text=True, timeout=timeout,
                             cwd=str(cwd or self.allowed), env=self.env(machine, **env))
        return run

    def dispatcher(self, machine: str, **env) -> subprocess.Popen:
        out = open(self.root / ("dispatcher-" + machine + ".out"), "ab")
        self.files = getattr(self, "files", []) + [out]
        proc = subprocess.Popen([PYTHON, "-B", str(RES), "dispatch"], stdout=out, stderr=subprocess.STDOUT,
                                env=self.env(machine, **env), cwd=str(SERVICE))
        self.procs.append(proc)
        self.wait(lambda: (self.data / "leases" / machine / "dispatcher.json").exists(), 20, "dispatcher report")
        return proc

    def stop(self):
        """Kill every process this sandbox started: the dispatchers and keepers it launched with their trees, and
        anything whose command line names the sandbox folder (detached runners, their jobs, stand-in Claude processes)."""
        victims = {}
        for proc in self.procs:
            try:
                root = psutil.Process(proc.pid)
                for p in [root] + root.children(recursive=True):
                    victims[p.pid] = p
            except psutil.Error:
                pass
        marker = str(self.root)
        me = os.getpid()
        for p in psutil.process_iter(["cmdline"]):
            try:
                if p.pid != me and marker in " ".join(p.info.get("cmdline") or []):
                    victims[p.pid] = p
            except psutil.Error:
                continue
        for p in victims.values():
            try:
                p.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(list(victims.values()), timeout=5)
        for proc in self.procs:
            try:
                proc.wait(1)
            except (subprocess.TimeoutExpired, OSError):
                pass
        for fh in getattr(self, "files", []):
            fh.close()

    def mails(self) -> list:
        path = self.root / "mail.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def mail_matching(self, text: str) -> list:
        return [m for m in self.mails() if text in m.get("subject", "")]

    def lease(self, machine: str, ident: str) -> dict:
        try:
            return json.loads((self.data / "leases" / machine / (ident + ".json")).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def logtext(self, machine: str) -> str:
        return "".join(p.read_text(encoding="utf-8") for p in sorted((self.data / "logs").glob("leaseresources." + machine.casefold() + ".*")))

    @staticmethod
    def wait(cond, timeout, what="condition", step=0.25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = cond()
            if value:
                return value
            time.sleep(step)
        raise AssertionError("timed out after " + str(timeout) + " s waiting for " + what)


def lease_id(output: str) -> str:
    for word in output.replace(":", " ").split():
        if word.startswith("L") and "-" in word and word[1:7].isdigit():
            return word
    raise AssertionError("no lease id in: " + output)


def pid_alive(pid: int) -> bool:
    try:
        p = psutil.Process(pid)
        return p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


class T1TrialKill(Base):
    """R1 and the process-tree kill: a runaway command with children, a grandchild, a setsid child and an orphaned
    new-session grandchild is killed at 10 s, and the profile shows its peaks on cpu, ram, disk, net and a simulated GPU."""

    def test_runaway_tree(self):
        w = World("trial", {"file_dimensions": {}})
        gpu = w.root / "gpu.json"
        cfg = json.loads((w.config / "defaults.json").read_text())
        cfg["file_dimensions"] = {"gpu:0": {"capacity": 100, "file": str(gpu), "label": "simulated GPU"}}
        (w.config / "defaults.json").write_text(json.dumps(cfg))
        script = w.outside / "tree.py"
        script.write_text(TRIAL_TREE, encoding="utf-8")
        pids_file = w.root / "pids.json"
        t0 = time.monotonic()
        run = w.res("Altiplano", "trial", "--json", "--", PYTHON, str(script), str(pids_file), str(gpu), cwd=w.root, timeout=60)
        wall = time.monotonic() - t0
        self.assertEqual(run.returncode, 3, run.stderr)          # cut off: lease needed
        record = json.loads(run.stdout)
        pids = json.loads(pids_file.read_text())
        everyone = set(pids["all"]) | set(pids.get("all2", [])) | {pids[k] for k in "abcdef"} | {pids["main"]}
        time.sleep(0.5)
        alive = [p for p in everyone if pid_alive(p)]
        measured("R1.trial_seconds", record["seconds"])
        measured("R1.wall_seconds", round(wall, 2))
        measured("R1.processes_in_tree", len(everyone))
        measured("R1.killed_reported", len(record["killed"]))
        measured("R1.alive_after", alive)
        prof = {d: round(v["peak"], 1) for d, v in record["profile"].items()}
        measured("R1.peaks", prof)
        self.assertFalse(record["finished"])
        self.assertGreaterEqual(record["seconds"], 9.9)
        self.assertLess(record["seconds"], 11.5)
        self.assertEqual(alive, [], "processes survived the trial")
        self.assertGreaterEqual(record["profile"]["cpu"]["peak"], 1.5)
        self.assertGreaterEqual(record["profile"]["ram"]["peak"], 400e6)
        disks = {d: v["peak"] for d, v in record["profile"].items() if d.startswith("disk:")}
        self.assertGreaterEqual(max(disks.values()), 30e6, disks)
        nets = {d: v["peak"] for d, v in record["profile"].items() if d.startswith("net:")}
        self.assertGreaterEqual(max(nets.values()), 3e6, nets)
        self.assertEqual(record["profile"]["gpu:0"]["peak"], 70)
        w.stop()

    def test_quick_command_finishes(self):
        w = World("trial-quick", {"file_dimensions": {"gpu:VeryLongSimulatedName0": {"capacity": 100, "file": "/nonexistent.json"}}})
        run = w.res("Altiplano", "trial", "--", PYTHON, "-c", "print('quick')")
        self.assertEqual(run.returncode, 0, run.stderr)
        long_line = [l for l in run.stdout.splitlines() if "gpu:VeryLongSimulatedName0" in l][0]
        self.assertIn("gpu:VeryLongSimulatedName0  ", long_line, "a long dimension name must not run into its value")
        self.assertIn("no lease needed", run.stdout)
        self.assertIn("quick", run.stdout)
        first = run.stdout.splitlines()[0]
        measured("R1.quick_trial_line", first)


class T3Queue(Base):
    """R3: two GPU-heavy leases (GPU simulated as a file dimension): the second queues on gpu:0 and its owner gets mail
    when it is granted; FIFO; a queued run order starts on grant; refusal names the dimension."""

    def test_gpu_queue(self):
        w = World("queue")
        gpu = w.root / "gpu.json"
        cfg = json.loads((w.config / "defaults.json").read_text())
        cfg["file_dimensions"] = {"gpu:0": {"capacity": 100, "file": str(gpu), "label": "simulated GPU"}}
        (w.config / "defaults.json").write_text(json.dumps(cfg))
        w.dispatcher("Altiplano")
        t = time.monotonic()
        a = w.res("Altiplano", "request", "--need", "gpu:0=60,cpu=0.5", "--for", "2m", "--desk", "Altiplano:DeskA")
        answer_a = time.monotonic() - t
        self.assertIn("granted", a.stdout, a.stdout + a.stderr)
        la = lease_id(a.stdout)
        measured("R3.first_answer_seconds", round(answer_a, 2))
        run_a = w.res("Altiplano", "run", "--lease", la, "--", PYTHON, str(w.allowed / "gpuuser.py"), str(gpu), "60", "8")
        self.assertEqual(run_a.returncode, 0, run_a.stdout + run_a.stderr)
        b = w.res("Altiplano", "request", "--need", "gpu=60", "--for", "2m", "--desk", "Altiplano:DeskB")
        self.assertIn("queued", b.stdout, b.stdout + b.stderr)
        self.assertIn("gpu:0", b.stdout)
        lb = lease_id(b.stdout)
        c = w.res("Altiplano", "request", "--need", "gpu:0=50", "--for", "2m", "--desk", "Altiplano:DeskC")
        self.assertIn("queued", c.stdout)
        lc = lease_id(c.stdout)
        run_c = w.res("Altiplano", "run", "--lease", lc, "--", PYTHON, str(w.allowed / "sleepy.py"), "2")
        self.assertIn("Run order written", run_c.stdout, run_c.stdout + run_c.stderr)
        measured("R3.second_answer", b.stdout.splitlines()[0])
        w.wait(lambda: w.lease("Altiplano", lb).get("state") == "granted", 40, "B granted after A ends")
        a_end = w.lease("Altiplano", la).get("ended")
        b_granted = w.lease("Altiplano", lb).get("granted")
        grant_mail = w.wait(lambda: [m for m in w.mail_matching("granted") if m["to"] == "Altiplano:DeskB"], 10, "grant mail to DeskB")
        measured("R3.A_ended_B_granted", [a_end, b_granted])
        measured("R3.grant_mail_subject", grant_mail[0]["subject"])
        self.assertEqual(w.lease("Altiplano", lc).get("state"), "queued", "C must not overtake while B holds gpu")
        rel = w.res("Altiplano", "release", lb)
        self.assertIn("released", rel.stdout, rel.stdout + rel.stderr)
        w.wait(lambda: w.lease("Altiplano", lc).get("state") in ("running", "finished"), 20, "C started after B released")
        w.wait(lambda: w.mail_matching("finished") and any(m["to"] == "Altiplano:DeskC" for m in w.mail_matching("finished")), 20, "C finished mail")
        started_mail = [m for m in w.mail_matching("started") if m["to"] == "Altiplano:DeskC"]
        self.assertTrue(started_mail, "queued run order should mail when it starts")
        finished_a = [m for m in w.mail_matching("finished") if m["to"] == "Altiplano:DeskA"]
        self.assertTrue(finished_a)
        self.assertIn("done", finished_a[0]["body"])
        big = w.res("Altiplano", "request", "--need", "ram=10T", "--for", "1m", "--desk", "Altiplano:DeskA")
        self.assertIn("refused", big.stdout)
        self.assertIn("ram", big.stdout)
        measured("R3.refusal", big.stdout.splitlines()[0])
        nogpu = w.res("Altiplano", "request", "--need", "vram=1G", "--for", "1m", "--desk", "Altiplano:DeskA")
        measured("R3.no_such_kind", nogpu.stdout.splitlines()[0])
        self.assertIn("refused", nogpu.stdout)
        w.stop()


class T4Overrun(Base):
    """R4: a lease that exceeds its declared RAM produces mail to its owner and is not killed; completion mail carries
    the log tail. Also stall and past-duration mail."""

    def test_ram_overrun(self):
        w = World("overrun")
        w.dispatcher("Altiplano")
        req = w.res("Altiplano", "request", "--need", "ram=100M,cpu=0.5", "--for", "5m", "--desk", "Altiplano:Owner")
        lid = lease_id(req.stdout)
        run = w.res("Altiplano", "run", "--lease", lid, "--", PYTHON, str(w.allowed / "ramhog.py"), "600", "15")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        t0 = time.monotonic()
        mail = w.wait(lambda: w.mail_matching("uses more ram"), 30, "overrun mail")
        after = time.monotonic() - t0
        pid = w.lease("Altiplano", lid).get("pid")
        measured("R4.overrun_mail_after_seconds", round(after, 1))
        measured("R4.overrun_mail_first_line", mail[0]["body"].splitlines()[0])
        self.assertTrue(pid and pid_alive(pid), "the job must keep running after the overrun mail")
        self.assertEqual(mail[0]["to"], "Altiplano:Owner")
        fin = w.wait(lambda: w.mail_matching("finished"), 40, "completion mail")
        self.assertIn("still holding", fin[0]["body"])
        self.assertIn("done", fin[0]["body"])
        measured("R4.completion_subject", fin[0]["subject"])
        peak = w.lease("Altiplano", lid).get("peak", {})
        measured("R4.recorded_peaks", {d: round(v) for d, v in peak.items()})
        self.assertGreaterEqual(peak.get("ram", 0), 600e6)
        peak_line = [l for l in fin[0]["body"].splitlines() if l.startswith("Peak use")][0]
        measured("R4.completion_peak_line", peak_line)
        self.assertNotIn("ram 0 B", peak_line)
        measured("R4.overrun_mails", len(w.mail_matching("uses more ram")))
        w.stop()

    def test_stall_and_duration(self):
        w = World("stall", {"stall_minutes": 0.1})
        w.dispatcher("Altiplano")
        lid = lease_id(w.res("Altiplano", "request", "--need", "cpu=0.5", "--for", "3s", "--desk", "Altiplano:Owner").stdout)
        w.res("Altiplano", "run", "--lease", lid, "--", PYTHON, str(w.allowed / "silent.py"), "20")
        t0 = time.monotonic()
        dur = w.wait(lambda: w.mail_matching("ran past its duration"), 20, "duration mail")
        t_dur = time.monotonic() - t0
        stall = w.wait(lambda: w.mail_matching("looks stalled"), 30, "stall mail")
        t_stall = time.monotonic() - t0
        measured("R4.duration_mail_after_seconds", round(t_dur, 1))
        measured("R4.stall_mail_after_seconds", round(t_stall, 1))
        self.assertTrue(pid_alive(w.lease("Altiplano", lid).get("pid")))
        w.wait(lambda: w.mail_matching("finished"), 30, "completion")
        w.stop()


@POSIX_ONLY
class T5Survive(Base):
    """R5 as far as possible here: a leased job survives the kill of the shell and process group that started it (what an
    interrupted turn does), and a dispatcher restart; a killed runner is reported as lost; failures are reported."""

    def test_survives_group_kill_and_dispatcher_restart(self):
        w = World("survive")
        disp = w.dispatcher("Altiplano")
        lid = lease_id(w.res("Altiplano", "request", "--need", "cpu=0.5", "--for", "5m", "--desk", "Altiplano:Owner").stdout)
        env = w.env("Altiplano")
        shell = subprocess.Popen(["/bin/bash", "-c", '"$0" -B "$1" run --lease "$2" -- "$0" "$3" 12; sleep 60',
                                  PYTHON, str(RES), lid, str(w.allowed / "sleepy.py")], env=env, cwd=str(w.allowed),
                                 start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        w.wait(lambda: w.lease("Altiplano", lid).get("state") == "running", 20, "job running")
        job_pid = w.lease("Altiplano", lid)["pid"]
        os.killpg(shell.pid, signal.SIGINT)
        os.killpg(shell.pid, signal.SIGHUP)
        os.killpg(shell.pid, signal.SIGKILL)
        shell.wait(5)
        time.sleep(1)
        survived_group_kill = pid_alive(job_pid)
        measured("R5.job_alive_after_group_kill", survived_group_kill)
        self.assertTrue(survived_group_kill)
        disp.terminate()
        disp.wait(5)
        time.sleep(2)
        w.dispatcher("Altiplano")
        fin = w.wait(lambda: w.mail_matching("finished"), 40, "completion after dispatcher restart")
        body = fin[0]["body"]
        ticks = sum(1 for line in body.splitlines() if line.startswith("tick"))
        measured("R5.ticks_in_mail_tail", ticks)
        self.assertIn("tick 11", body)
        self.assertIn("exit code 0", body)
        w.stop()

    def test_lost_and_failed(self):
        w = World("lost")
        w.dispatcher("Altiplano")
        lid = lease_id(w.res("Altiplano", "request", "--need", "cpu=0.5", "--for", "5m", "--desk", "Altiplano:Owner").stdout)
        w.res("Altiplano", "run", "--lease", lid, "--", PYTHON, str(w.allowed / "sleepy.py"), "60")
        w.wait(lambda: w.lease("Altiplano", lid).get("state") == "running", 20, "running")
        run = json.loads((w.data / "leases" / "Altiplano" / (lid + ".run.json")).read_text())
        os.kill(run["runner_pid"], signal.SIGKILL)
        os.kill(run["pid"], signal.SIGKILL)
        lost = w.wait(lambda: w.mail_matching(" lost "), 20, "lost mail")
        measured("R5.lost_subject", lost[0]["subject"])
        lid2 = lease_id(w.res("Altiplano", "request", "--need", "cpu=0.5", "--for", "5m", "--desk", "Altiplano:Owner").stdout)
        w.res("Altiplano", "run", "--lease", lid2, "--", PYTHON, str(w.allowed / "fail.py"))
        failed = w.wait(lambda: w.mail_matching("failed"), 20, "failed mail")
        self.assertIn("exit code 3", failed[0]["body"])
        self.assertIn("about to fail", failed[0]["body"])
        measured("R5.failed_subject", failed[0]["subject"])
        w.stop()


class T6CrossMachine(Base):
    """R6 with two machine folders: a request made as Altiplano with --on Mistral is granted by Mistral's dispatcher, the
    answer mail goes to the Altiplano desk, and a run ordered from Altiplano starts on Mistral and reports back."""

    def test_on_other_machine(self):
        w = World("cross")
        w.dispatcher("Altiplano")
        w.dispatcher("Mistral")
        t0 = time.monotonic()
        req = w.res("Altiplano", "request", "--need", "cpu=1", "--for", "2m", "--on", "Mistral", "--desk", "Altiplano:Asker")
        self.assertIn("Sent request", req.stdout, req.stdout + req.stderr)
        lid = lease_id(req.stdout)
        answer = w.wait(lambda: [m for m in w.mail_matching("granted") if m["to"] == "Altiplano:Asker"], 20, "grant mail")
        measured("R6.grant_mail_after_seconds", round(time.monotonic() - t0, 1))
        self.assertEqual(answer[0]["machine"], "Mistral", "the answer must come from Mistral's dispatcher")
        self.assertTrue(w.lease("Mistral", lid))
        self.assertFalse((w.data / "leases" / "Altiplano" / (lid + ".json")).exists())
        run = w.res("Altiplano", "run", "--lease", lid, "--on", "Mistral", "--", PYTHON, str(w.allowed / "sleepy.py"), "3")
        self.assertIn("Run order written", run.stdout, run.stdout + run.stderr)
        fin = w.wait(lambda: [m for m in w.mail_matching("finished") if m["to"] == "Altiplano:Asker"], 30, "finished mail")
        self.assertEqual(fin[0]["machine"], "Mistral")
        log = (w.data / "leases" / "Mistral" / (lid + ".log")).read_text()
        self.assertIn("starting on Mistral", log)
        measured("R6.finished_subject", fin[0]["subject"])
        status = w.res("Altiplano", "status", lid, "--on", "Mistral")
        self.assertIn("finished", status.stdout)
        w.stop()


class T8Safety(Base):
    """R8 after D-260927-7: a session's direct local run may use any program; anything that arrives through the synced
    folder (orders from another machine, and files planted there claiming to be local) keeps the program rule in full,
    is refused and logged, and never runs."""

    def test_local_bypass_and_synced_refusals(self):
        w = World("safety")
        w.dispatcher("Altiplano")
        w.dispatcher("Mistral")
        # A link from the allowed root to a script outside it must not help. POSIX: a file symlink. Windows: symlinks
        # need admin rights or Developer Mode, so a directory junction (no privilege needed) tests the same escape.
        if WINDOWS:
            subprocess.run(["cmd", "/c", "mklink", "/J", str(w.allowed / "linkdir"), str(w.outside)], check=True, capture_output=True)
            linked = w.allowed / "linkdir" / "evil.py"
        else:
            (w.allowed / "link.py").symlink_to(w.outside / "evil.py")
            linked = w.allowed / "link.py"
        marker = w.root / "marker-local.txt"
        shell = (["cmd", "/c", "echo local-ok& echo ran> " + str(marker)] if WINDOWS
                 else ["/bin/sh", "-c", "echo local-ok; echo ran > " + str(marker)])
        lid = lease_id(w.res("Altiplano", "request", "--need", "cpu=0.5", "--for", "1m", "--desk", "Altiplano:Owner").stdout)
        run = w.res("Altiplano", "run", "--lease", lid, "--", *shell)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        w.wait(lambda: w.lease("Altiplano", lid).get("state") == "finished", 20, "local shell run finished")
        self.assertTrue(marker.exists())
        self.assertIn("local-ok", (w.data / "leases" / "Altiplano" / (lid + ".log")).read_text())
        self.assertIn("program rule not applied", w.logtext("Altiplano"))
        measured("R8.local_nonpython_run", w.lease("Altiplano", lid).get("state"))

        cases = {
            "shell": (["cmd", "/c", "echo remote> " + str(w.root / "marker-remote.txt")] if WINDOWS
                      else ["/bin/sh", "-c", "echo remote > " + str(w.root / "marker-remote.txt")]),
            "outside": [PYTHON, str(w.outside / "evil.py")],
            "inline": [PYTHON, "-c", "print(1)"],
            "module": [PYTHON, "-m", "http.server"],
            "link": [PYTHON, str(linked)],
        }
        remote = {}
        for name, argv in cases.items():
            rid = lease_id(w.res("Altiplano", "request", "--need", "cpu=0.5", "--for", "1m", "--on", "Mistral", "--desk", "Altiplano:Owner").stdout)
            w.wait(lambda: w.lease("Mistral", rid).get("state") == "granted", 15, "remote grant " + name)
            w.res("Altiplano", "run", "--lease", rid, "--on", "Mistral", "--", *argv)
            w.wait(lambda: w.lease("Mistral", rid).get("state") == "refused", 15, "remote refusal " + name)
            self.assertFalse((w.data / "leases" / "Mistral" / (rid + ".run.json")).exists(), name)
            remote[name] = w.lease("Mistral", rid).get("answer", "")[:100]
        self.assertFalse((w.root / "marker-remote.txt").exists())
        self.assertEqual(w.logtext("Mistral").count("REFUSED"), len(cases))
        w.wait(lambda: len([m for m in w.mail_matching("refused") if m["machine"] == "Mistral"]) == len(cases), 10, "refusal mails")
        measured("R8.remote_refusals", remote)

        spoof_marker = w.root / "marker-spoof.txt"
        spoofs = {}
        for start in ["res", "dispatcher"]:
            sid = lease_id(w.res("Altiplano", "request", "--need", "cpu=0.5", "--for", "1m", "--desk", "Altiplano:Owner").stdout)
            order = {"lease": sid, "argv": ["/bin/sh", "-c", "echo spoof > " + str(spoof_marker)], "cwd": str(w.allowed),
                     "from_machine": "Altiplano", "ordered": time.strftime("%Y-%m-%dT%H:%M:%S-05:00"), "start": start,
                     "desk": "Altiplano:Owner"}
            (w.data / "leases" / "Altiplano" / (sid + ".order.json")).write_text(json.dumps(order))
            spoofs[start] = sid
        w.wait(lambda: w.lease("Altiplano", spoofs["dispatcher"]).get("state") == "refused", 15, "planted dispatcher order refused")
        time.sleep(4)
        self.assertFalse(spoof_marker.exists(), "a planted order must never run")
        self.assertNotIn(w.lease("Altiplano", spoofs["res"]).get("state"), ("running", "finished", "starting"))
        self.assertIn("REFUSED run of " + spoofs["dispatcher"], w.logtext("Altiplano"))
        measured("R8.planted_orders", {k: w.lease("Altiplano", v).get("state") for k, v in spoofs.items()})

        qid = None
        blocker = lease_id(w.res("Altiplano", "request", "--need", "ram=20G", "--for", "1m", "--desk", "Altiplano:Owner").stdout)
        qid = lease_id(w.res("Altiplano", "request", "--need", "ram=20G", "--for", "1m", "--desk", "Altiplano:Owner").stdout)
        if w.lease("Altiplano", qid).get("state") == "queued":
            q = w.res("Altiplano", "run", "--lease", qid, "--", *shell)
            self.assertNotEqual(q.returncode, 0)
            self.assertIn("Refused", q.stdout + q.stderr)
            measured("R8.queued_local_shell", "refused at once")
        w.stop()


class T9HistoryAndCompliance(Base):
    """Duration history per command, and the compliance record: a long command of a (fake) Claude session without a
    lease is recorded with its desk and lifetime; leased runs and res itself are not."""

    def test_history(self):
        w = World("history")
        w.dispatcher("Altiplano")
        for i in range(2):
            lid = lease_id(w.res("Altiplano", "request", "--need", "cpu=0.5", "--for", "1h", "--desk", "Altiplano:Owner").stdout)
            run = w.res("Altiplano", "run", "--lease", lid, "--", PYTHON, str(w.allowed / "sleepy.py"), "2")
            w.wait(lambda: w.lease("Altiplano", lid).get("state") == "finished", 20, "finished")
        measured("R-history.second_run_output", run.stdout.splitlines()[0])
        self.assertIn("previous runs of this command took", run.stdout)
        self.assertIn("far from that", run.stdout)
        fin = w.wait(lambda: [m for m in w.mail_matching("finished") if lid in m["subject"]], 15, "second run's mail")
        self.assertIn("Earlier runs", fin[-1]["body"])
        short_peak = w.lease("Altiplano", lid).get("peak", {})
        measured("R-history.short_job_peaks", {d: round(v) for d, v in short_peak.items()})
        self.assertGreater(short_peak.get("ram", 0), 1e6, "a 2-second job must still have its RAM peak recorded")
        w.stop()

    def test_unleased_detection(self):
        w = World("compliance")
        fake = fake_claude(w, self)
        w.dispatcher("Altiplano")
        if WINDOWS:
            long_cmd = "echo shell-snapshots-fake >nul & " + q(PYTHON) + ' -c "import time; time.sleep(12)"'
            short_cmd = "echo shell-snapshots-fake >nul & " + q(PYTHON) + ' -c "import time; time.sleep(1)"'
            expect = "time.sleep(12)"
        else:
            long_cmd, short_cmd, expect = ("/bin/zsh -c 'sleep 12; true' shell-snapshots-fake",
                                           "/bin/zsh -c 'sleep 1; true' shell-snapshots-fake", "sleep 12")
        # w.env drops CLAUDE_CODE_SESSION_ID: a session id inherited from the Claude session running the suite made the
        # detector take that session's real desk (Moosfels, round 3)
        claude = subprocess.Popen(fake + [long_cmd], cwd=str(w.allowed), env=w.env("Altiplano"))
        quick = subprocess.Popen(fake + [short_cmd], cwd=str(w.allowed), env=w.env("Altiplano"))

        def rows():
            path = w.data / "leases" / "Altiplano" / "unleased.jsonl"
            if not path.exists():
                return []
            return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        def mentions(r, text):
            return any(text in c for c in r.get("commands", [])) or text in r.get("shell", "")
        seen = w.wait(lambda: [r for r in rows() if r["event"] == "seen" and mentions(r, expect)], 25, "unleased record")
        self.assertEqual(seen[0]["desk"], "Altiplano:StubDesk")
        self.assertTrue(any(expect in c for c in seen[0]["commands"]) or expect in seen[0]["shell"], seen[0])
        claude.wait(20)
        ended = w.wait(lambda: [r for r in rows() if r["event"] == "ended" and r["key"] == seen[0]["key"]], 15, "ended record")
        measured("R-compliance.seen", {k: seen[0][k] for k in ("age", "desk", "commands")})
        measured("R-compliance.lifetime", ended[0]["lifetime"])
        short_text = "time.sleep(1)" if WINDOWS else "sleep 1;"
        self.assertFalse([r for r in rows() if mentions(r, short_text)], "a 1 s command is not a long command")
        comp = w.res("Altiplano", "compliance", "--days", "1")
        self.assertIn("without a lease: ", comp.stdout)
        measured("R-compliance.report", comp.stdout.strip().splitlines())
        w.stop()


class T10Keeper(Base):
    """The keeper (res.py watch) restarts the dispatcher onto new code when res.py or probes.py change."""

    def test_restart_on_code_change(self):
        w = World("keeper")
        out = open(w.root / "watch.out", "ab")
        keeper = subprocess.Popen([PYTHON, "-B", str(RES), "watch"], stdout=out, stderr=subprocess.STDOUT,
                                  env=w.env("Altiplano"), cwd=str(SERVICE))
        try:
            w.wait(lambda: w.logtext("Altiplano").count("dispatcher started") == 1, 20, "first dispatcher")
            t0 = time.monotonic()
            os.utime(CODE / "probes.py")                # the sandbox copy, never the synced file
            w.wait(lambda: w.logtext("Altiplano").count("dispatcher started") == 2, 20, "restarted dispatcher")
            measured("keeper.restart_seconds", round(time.monotonic() - t0, 1))
            self.assertIn("code changed; dispatcher restarting", w.logtext("Altiplano"))
        finally:
            for child in psutil.Process(keeper.pid).children(recursive=True):
                child.terminate()
            keeper.terminate()
            keeper.wait(5)
            out.close()


class T13TrialWatchdog(Base):
    """The 10-second kill does not depend on the sampling loop: with one sample every 30 s (standing in for a probe
    call that hangs, as psutil's open_files did on Windows) a 60-second tree still dies at 10 s and res returns."""

    def test_watchdog_kills_when_sampling_stalls(self):
        w = World("watchdog", {"trial_sample_seconds": 30})
        script = w.outside / "tree2.py"
        script.write_text("import subprocess, sys, time\n"
                          "kids = [subprocess.Popen([sys.executable, '-c', 'import subprocess, sys, time; "
                          "subprocess.Popen([sys.executable, \\'-c\\', \\'import time; time.sleep(60)\\']); time.sleep(60)']) "
                          "for i in range(2)]\n"
                          "open(sys.argv[1], 'w').write(' '.join(str(k.pid) for k in kids))\ntime.sleep(60)\n")
        pids_file = w.root / "pids2.txt"
        t0 = time.monotonic()
        run = w.res("Altiplano", "trial", "--json", "--", PYTHON, str(script), str(pids_file), cwd=w.root, timeout=40)
        wall = time.monotonic() - t0
        self.assertEqual(run.returncode, 3, run.stderr)          # cut off: lease needed
        record = json.loads(run.stdout)
        time.sleep(0.5)
        survivors = [p for p in psutil.process_iter(["cmdline"]) if "time.sleep(60)" in " ".join(p.info.get("cmdline") or [])
                     and str(w.root) in " ".join(p.info.get("cmdline") or []) + str(script)]
        kids = [int(x) for x in pids_file.read_text().split()] if pids_file.exists() else []
        alive = [k for k in kids if pid_alive(k)]
        measured("watchdog.trial_seconds", record["seconds"])
        measured("watchdog.wall_seconds", round(wall, 1))
        measured("watchdog.killed", len(record["killed"]))
        self.assertFalse(record["finished"])
        self.assertLess(wall, 16)
        self.assertEqual(alive, [])


class T14Units(Base):
    """In-process checks of the pieces written for Windows, runnable on any platform: retried replace and read, the
    disk attribution cap, the virtual-interface filter, and the unleased detector on a Windows-shaped process table."""

    def load(self, w):
        for key, value in w.env("Altiplano").items():
            if key.startswith("LEASERES_"):
                os.environ[key] = value
        import importlib.util
        spec = importlib.util.spec_from_file_location("res_under_test", RES)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_replace_and_read_retry(self):
        import probes
        w = World("units-io")
        target = w.root / "x.json"
        real = os.replace
        calls = {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise PermissionError(5, "Access is denied")
            return real(a, b)
        probes.os.replace = flaky
        try:
            probes.write_json(target, {"a": 1})
        finally:
            probes.os.replace = real
        self.assertEqual(probes.read_json(target), {"a": 1})
        measured("units.replace_attempts", calls["n"])
        (w.root / "half.json").write_text('{"a": ')
        self.assertEqual(probes.read_json(w.root / "half.json"), {})
        self.assertEqual(probes.read_json(w.root / "missing.json"), {})

    def test_disk_cap(self):
        import probes
        probe = probes.DiskProbe.__new__(probes.DiskProbe)
        probe.dims = {"disk:PhysicalDrive0": {"capacity": 150e6}, "disk:PhysicalDrive1": {"capacity": 150e6}}
        probe.mounts = {"disk:PhysicalDrive0": ["C:"], "disk:PhysicalDrive1": ["F:"]}
        r = probes.Reading(1)
        r.disk_rate = 355e6
        use = probe.tree([r], 1.0, {"disk:PhysicalDrive0": 1.6e6, "disk:PhysicalDrive1": 0.0})
        measured("units.disk_cap", {d: round(v) for d, v in use.items()})
        self.assertLessEqual(sum(use.values()), 1.6e6 + 1)

    def test_virtual_interfaces(self):
        import probes
        from collections import namedtuple
        Stat = namedtuple("Stat", "isup speed")
        Addr = namedtuple("Addr", "family address")
        Io = namedtuple("Io", "bytes_sent bytes_recv")
        stats = {"Ethernet": Stat(True, 1000), "vEthernet (WSL)": Stat(True, 4294), "Tailscale": Stat(True, 4294),
                 "VMware Network Adapter VMnet8": Stat(True, 100), "Wi-Fi": Stat(True, 4294)}
        addrs = {name: [Addr(socket.AF_INET, "192.0.2." + str(i + 10))] for i, name in enumerate(stats)}
        saved = (probes.psutil.net_if_stats, probes.psutil.net_if_addrs, probes.psutil.net_io_counters)
        probes.psutil.net_if_stats = lambda: stats
        probes.psutil.net_if_addrs = lambda: addrs
        probes.psutil.net_io_counters = lambda pernic=False: {n: Io(0, 0) for n in stats}
        try:
            with open(SERVICE / "config" / "defaults.json") as fh:
                config = json.load(fh)
            probe = probes.NetProbe(config, Path(tempfile.mkdtemp()))
        finally:
            probes.psutil.net_if_stats, probes.psutil.net_if_addrs, probes.psutil.net_io_counters = saved
        caps = {d: probe.dims[d]["capacity"] for d in probe.dims}
        measured("units.net_dimensions", caps)
        self.assertEqual(sorted(caps), ["net:Ethernet", "net:Wi-Fi"])
        self.assertEqual(caps["net:Ethernet"], 125e6)
        self.assertEqual(caps["net:Wi-Fi"], 125e6, "a 4294 Mbit/s link speed is ignored")

    def test_detector_windows_shape(self):
        w = World("units-detector")
        res = self.load(w)
        now = time.time()
        base = 9000000

        class Fake:
            def __init__(self, pid, ppid, name, cmd, age, env=None):
                self.pid = pid
                self.info = {"pid": pid, "ppid": ppid, "name": name, "create_time": now - age, "cmdline": cmd, "username": None}
                self.env = env or {}

            def environ(self):
                return self.env

            def cwd(self):
                return str(w.allowed)
        mark = {"CLAUDECODE": "1", "CLAUDE_PID": str(base + 1), "CLAUDE_CODE_SESSION_ID": "sess-1"}
        mark2 = {"CLAUDECODE": "1", "CLAUDE_PID": str(base + 200)}
        ps_wrapper = ["C:/WINDOWS/system32/cmd.exe", "/d", "/s", "/c",
                      "chcp 65001 >nul & powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $__claudeCodeScript"]
        ps_host = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "$__claudeCodeScript = ..."]
        git_bash = ["C:/Program Files/Git/usr/bin/bash.exe", "--login", "-i", "C:/Users/goocy/.claude/shell-snapshots/snapshot-bash-1.sh"]
        first = [
            Fake(base + 1, 4, "claude.exe", ["claude.exe"], 3000),
            # a persistent PowerShell host reading from stdin (hypothetical shape), with CLAUDECODE
            Fake(base + 101, base + 1, "powershell.exe", ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"], 3000, mark),
            Fake(base + 102, base + 101, "python.exe", ["python.exe", "long.py"], 300, mark),
            Fake(base + 103, base + 101, "conhost.exe", ["conhost.exe", "0x4"], 3000, mark),
            Fake(base + 109, base + 101, "python.exe", ["python.exe", "quick.py"], 10, mark),
            Fake(base + 203, base + 101, "powershell.exe", ["powershell.exe", "-File", "x.ps1"], 400, mark),
            Fake(base + 104, base + 1, "bash.exe", ["bash.exe", "--login", "-i", "shell-snapshots/snap.sh"], 3000, mark),
            Fake(base + 105, 999, "python.exe", ["python.exe", "-m", "pymobiledevice3", "usbmux", "forward"], 900, mark),
            Fake(base + 106, 999, "python.exe", ["python.exe", "job.py"], 900, dict(mark, LEASERES_LEASE="L1")),
            Fake(base + 107, base + 1, "bash.exe", ["bash.exe", "-c", "source shell-snapshots/snap.sh; sleep 200"], 300, mark),
            Fake(base + 108, base + 107, "sleep.exe", ["sleep.exe", "200"], 300, mark),
            Fake(base + 110, base + 1, "node.exe", ["node.exe", "mcp-server.js"], 3000, mark),
            Fake(base + 111, base + 110, "python.exe", ["python.exe", "helper.py"], 3000, mark),
            Fake(base + 112, base + 1, "cmd.exe", ["cmd.exe", "/c", "npx", "some-mcp-server"], 3000, mark),
            Fake(base + 200, 4, "node.exe", ["node.exe", "cli.js"], 3000),
            Fake(base + 201, base + 200, "pwsh.exe", ["pwsh.exe", "-NoLogo"], 3000, mark2),
            Fake(base + 202, base + 201, "python.exe", ["python.exe", "train.py"], 400, mark2),
            # the PowerShell tool as seen on Moosfels: cmd /c -> powershell $__claudeCodeScript -> python
            Fake(base + 301, base + 1, "cmd.exe", ps_wrapper, 300, mark),
            Fake(base + 302, base + 301, "powershell.exe", ps_host, 300, mark),
            Fake(base + 303, base + 302, "python.exe", ["python.exe", "-c", "import time; time.sleep(150)"], 290, mark),
            Fake(base + 304, base + 302, "conhost.exe", ["conhost.exe", "0x4"], 300, mark),
            # a long Start-Sleep: the pair alone
            Fake(base + 311, base + 1, "cmd.exe", ps_wrapper, 300, mark),
            Fake(base + 312, base + 311, "powershell.exe", ps_host, 300, mark),
            # Git Bash kept by the Bash tool, its parent chain not reaching claude.exe; an MSYS fork of bash, then
            # sleep.exe whose environment cannot be read
            Fake(base + 401, 77, "bash.exe", git_bash, 3000, mark),
            Fake(base + 402, base + 401, "bash.exe", git_bash, 200, mark),
            Fake(base + 403, base + 402, "sleep.exe", ["/usr/bin/sleep", "150"], 200, {}),
            # the same without a marker in the command line, known by CLAUDECODE
            Fake(base + 501, 78, "bash.exe", ["bash.exe", "--login", "-i"], 3000, mark),
            Fake(base + 502, base + 501, "bash.exe", ["bash.exe", "--login", "-i"], 200, mark),
            Fake(base + 503, base + 502, "sleep.exe", ["/usr/bin/sleep", "150"], 200, {}),
            # a background Bash command: its per-command shell still alive in this scan, gone in the next
            Fake(base + 600, base + 1, "bash.exe", ["bash.exe", "-c", "source shell-snapshots/snap.sh; sleep 150 &"], 250, mark),
            Fake(base + 601, base + 600, "sleep.exe", ["/usr/bin/sleep", "150"], 200, {}),
            # Git Bash as seen on Moosfels (round 3): the tool's bash under claude.exe with the command in eval '...',
            # the MSYS bash below it, and sleep.exe orphaned by the fork/exec with only MSYSTEM in its environment
            Fake(base + 801, base + 1, "bash.exe", ["C:/Program Files/Git/bin/bash.exe", "-c",
                 "source /c/Users/goocy/.claude/shell-snapshots/snapshot-bash-1.sh 2>/dev/null || true && eval 'date +%T; sleep 150; date +%T' < /dev/null && pwd -P >| /c/x-cwd"], 200, mark),
            Fake(base + 802, base + 801, "bash.exe", ["C:/Program Files/Git/usr/bin/bash.exe", "-c",
                 "source /c/Users/goocy/.claude/shell-snapshots/snapshot-bash-1.sh 2>/dev/null || true && eval 'date +%T; sleep 150; date +%T' < /dev/null && pwd -P >| /c/x-cwd"], 200, mark),
            Fake(base + 803, 12345, "sleep.exe", ["C:/Program Files/Git/usr/bin/sleep.exe", "150"], 199, {"MSYSTEM": "MINGW64"}),
            # just under the threshold now, gone at the next scan after having lived past it
            Fake(base + 701, 999, "python.exe", ["python.exe", "short.py"], 119, mark),
        ]
        second = [f for f in first if f.pid not in (base + 600, base + 701)]
        for f in second:
            if f.pid == base + 601:
                f.info["ppid"] = base + 600            # the dead parent's pid stays, as on Windows
        saved = res.psutil.process_iter
        try:
            d = res.Dispatcher(res.load_config())
            d.config = dict(d.config, unleased_after_seconds=120)
            shift = time.time() - now                 # rebase the fake ages to this moment
            for f in first:
                f.info["create_time"] += shift
            res.psutil.process_iter = lambda attrs=None: list(first)
            d.check_unleased()
            time.sleep(1.5)
            res.psutil.process_iter = lambda attrs=None: list(second)
            d.check_unleased()
        finally:
            res.psutil.process_iter = saved
        rows = [json.loads(l) for l in (w.data / "leases" / "Altiplano" / "unleased.jsonl").read_text().splitlines() if l.strip()]
        seen = {int(r["key"].partition("-")[0]) - base: r for r in rows if r["event"] == "seen"}
        measured("units.detector_recorded", {str(k): r["how"] + " | " + (r["commands"][0][:40] if r["commands"] else "") for k, r in sorted(seen.items())})
        self.assertEqual(sorted(seen), [102, 105, 107, 202, 203, 301, 311, 402, 502, 600, 601, 701, 801])
        self.assertIn("time.sleep(150)", seen[301]["commands"][0])
        self.assertEqual(seen[301]["session"], "sess-1")
        self.assertIn("sleep 150", " ".join(seen[402]["commands"]))
        self.assertEqual(seen[402]["how"], "command of a persistent tool shell")
        self.assertEqual(seen[601]["how"], "backgrounded, its tool shell gone")
        self.assertEqual(seen[801]["commands"][0], "date +%T; sleep 150; date +%T")
        self.assertIn("ended between two scans", seen[701].get("note", ""))
        ended = [r for r in rows if r["event"] == "ended"]
        self.assertTrue(ended and all(r.get("session") is not None for r in ended))
        self.assertEqual([r["session"] for r in ended if r["key"] == seen[701]["key"]], ["sess-1"])
        d2 = res.Dispatcher(res.load_config())
        d2.restore_unleased()                         # the fake processes do not exist: all end, lifetime unknown
        comp = w.res("Altiplano", "compliance", "--days", "1")
        self.assertEqual(comp.returncode, 0, comp.stderr)
        self.assertIn("without a lease: 13", comp.stdout)


class T15ScanDoesNotBlock(Base):
    """A slow compliance scan (every desk lookup taking 3 s, and long tool shells on the machine to record: at least the
    one running this suite) must not delay lease answers (Moosfels round 3: grant mails timed out, and a local run
    found its lease still unanswered and was refused as a queued run)."""

    def test_request_answered_while_scan_is_slow(self):
        w = World("noblock", {"unleased_after_seconds": 1})
        fake = fake_claude(w, self)
        long_cmd = ("echo shell-snapshots-fake >nul & " + q(PYTHON) + ' -c "import time; time.sleep(40)"' if WINDOWS
                    else "/bin/zsh -c 'sleep 40; true' shell-snapshots-fake")
        subprocess.Popen(fake + [long_cmd], cwd=str(w.allowed), env=w.env("Altiplano"))   # no session: a desk lookup
        time.sleep(2)
        w.dispatcher("Altiplano", STUB_DESK_SLEEP="6")
        time.sleep(2)                                   # the scan is now busy with desk lookups
        t0 = time.monotonic()
        req = w.res("Altiplano", "request", "--need", "cpu=0.5", "--for", "1m", "--desk", "Altiplano:Owner")
        answer = time.monotonic() - t0
        measured("noblock.answer_seconds", round(answer, 2))
        self.assertIn("granted", req.stdout, req.stdout + req.stderr)
        self.assertLess(answer, 3)
        lid = lease_id(req.stdout)
        shell = ["cmd", "/c", "echo ok"] if WINDOWS else ["/bin/sh", "-c", "echo ok"]
        run = w.res("Altiplano", "run", "--lease", lid, "--", *shell)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


class T16StableRecords(Base):
    """A long tool command that keeps spawning and reaping short children, some of which are res.py, is one record
    for its whole life: one "seen", one "ended"."""

    def test_child_churn_keeps_one_record(self):
        w = World("churn", {"unleased_after_seconds": 3})
        fake = fake_claude(w, self)
        (w.allowed / "res.py").write_text("import time\ntime.sleep(0.8)\n")          # an ignored name, short-lived
        (w.allowed / "churn.py").write_text(
            "import subprocess, sys, time\nend = time.time() + 16\nwhile time.time() < end:\n"
            "    subprocess.call([sys.executable, 'res.py'])\n    subprocess.call([sys.executable, '-c', 'pass'])\n"
            "    time.sleep(0.3)\n")
        w.dispatcher("Altiplano")
        cmd = ("echo shell-snapshots-fake >nul & " + q(PYTHON) + " churn.py" if WINDOWS
               else "true shell-snapshots-fake; " + q(PYTHON) + " churn.py")
        shell = subprocess.Popen(fake + [cmd], cwd=str(w.allowed), env=w.env("Altiplano"))
        shell.wait(40)

        def rows():
            path = w.data / "leases" / "Altiplano" / "unleased.jsonl"
            return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []
        mine = lambda: [r for r in rows() if any("churn.py" in c for c in r.get("commands", [])) or "churn.py" in r.get("shell", "")]
        seen = w.wait(lambda: [r for r in mine() if r["event"] == "seen"], 10, "seen record")
        keys = {r["key"] for r in seen}
        w.wait(lambda: [r for r in rows() if r["event"] == "ended" and r["key"] in keys], 15, "ended record")
        ended = [r for r in rows() if r["event"] == "ended" and r["key"] in keys]
        measured("churn.records", {"seen": len(seen), "ended": len(ended), "lifetime": ended[0].get("lifetime")})
        self.assertEqual(len(seen), 1, seen)
        self.assertEqual(len(ended), 1, ended)
        self.assertGreaterEqual(ended[0]["lifetime"], 14)


class T17Teppich(Base):
    """Teppich's notes: no start-up burst in the dispatcher's rates, learned capacities rising in a running meter."""

    def test_first_sample_rates(self):
        import probes
        burner = subprocess.Popen([PYTHON, "-c", "import time\nend = time.time() + 0.4\nwhile time.time() < end: pass\ntime.sleep(5)"])
        try:
            time.sleep(0.6)
            proc = psutil.Process(burner.pid)
            config = {"disk_skip_mounts": ["/Library/Developer/CoreSimulator", "/System/Volumes/"]}
            quiet = probes.Meter(config, Path(tempfile.mkdtemp()), first_sample_rates=False)
            eager = probes.Meter(config, Path(tempfile.mkdtemp()))
            quiet.last_wall = eager.last_wall = time.time() - 10.0      # the burner was born after the 'last sample'
            q = quiet.sample({"t": [proc]})[1]["t"]["cpu"]
            e = eager.sample({"t": [proc]})[1]["t"]["cpu"]
            measured("teppich.first_sample_cpu", {"dispatcher": round(q, 2), "trial": round(e, 2)})
            self.assertEqual(q, 0.0)
            self.assertGreater(e, 0.05)                  # its life's average: 0.4 s of cpu over its age
        finally:
            burner.kill()

    def test_learned_capacity_rises_in_a_running_meter(self):
        import probes
        config = {"disk_skip_mounts": ["/Library/Developer/CoreSimulator", "/System/Volumes/"], "disk_default_bytes_per_s": 150e6}
        meter = probes.Meter(config, Path(tempfile.mkdtemp()), first_sample_rates=False)
        disks = [d for d in meter.dimensions() if d.startswith("disk:")]
        if not disks:
            self.skipTest("no disk dimension here")
        raised = meter.raise_learned({disks[0]: 322e6})
        measured("teppich.learned_raise", {disks[0]: meter.dimensions()[disks[0]]["capacity"]})
        self.assertEqual(raised, [disks[0]])
        self.assertEqual(meter.dimensions()[disks[0]]["capacity"], 322e6)


def main():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    print("scratch: " + str(SCRATCH), flush=True)
    try:
        socket.create_connection(("192.0.2.2", 445), timeout=2).close()
    except OSError:
        print("note: 192.0.2.2 not reachable; the network peak in R1 may be missing", flush=True)
    program = unittest.main(exit=False, verbosity=2)
    (SCRATCH / "results.json").write_text(json.dumps(RESULTS, indent=1))
    print("results: " + str(SCRATCH / "results.json"))
    sys.exit(0 if program.result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
