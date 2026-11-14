"""Resource probes for LeaseResources: every resource is a named dimension with a capacity and a measured use.

A probe reports, for one kind of resource, the dimensions it sees on this machine (name, capacity, label), the whole
machine's current use of each, and where it can, the use of given process trees. Nothing here knows which resource is
scarce; adding a dimension is one new Probe subclass appended to PROBES.

Kinds and units:
    cpu     cores (core-seconds per second)
    ram     bytes resident
    disk:D  bytes per second read + written on physical disk D (disk0, PhysicalDrive1); label names its mounts or letters
    gpu:N   percent utilisation of NVIDIA GPU N (nvidia-smi)
    vram:N  bytes of that GPU's memory in use
    net:I   bytes per second on interface I (the busier direction)
    any     a 'file_dimensions' entry in the config: a JSON file {"total": x, "pids": {"<pid>": x}} written by someone
            else (a simulator in the tests, or a future exporter)

Per-process use is exact for cpu and ram, near-exact for disk bytes (macOS: proc_pid_rusage; Windows and Linux: psutil
io_counters, which on Windows counts all I/O, not only disk) and put on the disks each process has files open on;
vram per process comes from nvidia-smi where the driver reports it, GPU utilisation is split by vram share. Network use
per process cannot be measured with psutil, so net dimensions are 'system only': callers attribute them themselves.

Requires psutil. No regular expressions (house rule).
"""
from __future__ import annotations

import ctypes
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil

UNITS = {"cpu": "cores", "ram": "B", "disk": "B/s", "gpu": "%", "vram": "B", "net": "B/s"}
MIB = 1024 * 1024


def kind_of(dim: str) -> str:
    return dim.partition(":")[0]


def read_json(path: Path) -> dict:
    """A JSON object from a file, {} when it is missing. On Windows a file being replaced, or held by Resilio, briefly
    refuses to open (PermissionError) or reads short; both are retried for up to about a second before giving up."""
    delay = 0.02
    for attempt in range(8):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (PermissionError, ValueError, OSError):
            if attempt == 7 or not Path(path).exists():
                return {}
            time.sleep(delay)
            delay = min(delay * 2, 0.3)
    return {}


def replace_retrying(source, target) -> None:
    """os.replace, retried with backoff for about 3 s: on Windows it fails with WinError 5 while any reader or Resilio
    has the target open (seen on Moosfels, 2026-09-27)."""
    delay = 0.02
    for attempt in range(12):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 11:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + "." + str(os.getpid()) + ".tmp")   # *.tmp is in Resilio's ignore list
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    try:
        replace_retrying(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- per-process counters

class _RUsageInfoV2(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in [
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups", "ri_pageins", "ri_wired_size",
        "ri_resident_size", "ri_phys_footprint", "ri_proc_start_abstime", "ri_proc_exit_abstime",
        "ri_child_user_time", "ri_child_system_time", "ri_child_pkg_idle_wkups", "ri_child_interrupt_wkups",
        "ri_child_pageins", "ri_child_elapsed_abstime", "ri_diskio_bytesread", "ri_diskio_byteswritten"]]


_LIBPROC = None


def _mac_disk_bytes(pid: int):
    """Cumulative disk bytes read + written by one process on macOS (psutil has no io_counters there)."""
    global _LIBPROC
    if _LIBPROC is None:
        try:
            _LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib")
        except OSError:
            _LIBPROC = False
    if not _LIBPROC:
        return None
    info = _RUsageInfoV2()
    if _LIBPROC.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(2), ctypes.byref(info)) != 0:
        return None
    return int(info.ri_diskio_bytesread + info.ri_diskio_byteswritten)


def disk_bytes(proc: psutil.Process):
    if sys.platform == "darwin":
        return _mac_disk_bytes(proc.pid)
    try:
        io = proc.io_counters()
        return int(io.read_bytes + io.write_bytes)
    except (psutil.Error, AttributeError, NotImplementedError):
        return None


def process_paths(proc: psutil.Process) -> list:
    """The files a process has open: where its disk bytes go. Not its working folder, which says nothing about that (a
    reader in C: reading from G: was put on C: and capped at C:'s rate, Mistral 2026-09-28). On Windows the files under
    the Windows folder are left out: every process holds kernel32.dll.mui and KernelBase.dll.mui there, which would put
    every job on C:. Windows was skipped from 2026-09-27 to 2026-10-26 on the suspicion that open_files() stalled a
    trial on Moosfels; on Mistral on 2026-10-26 (psutil 7.2.2) it took 0.04 to 0.4 s per process (2017 files of
    a busy program in 0.41 s) and holds the GIL meanwhile, so the Meter reads it at most every disk_paths_seconds. A process
    whose files this user may not read (another user's or an elevated one: 143 of 290 readable processes on Mistral)
    has no known disk, and its cpu and ram are still read."""
    try:
        paths = [f.path for f in proc.open_files()]
    except psutil.AccessDenied:
        return []
    if sys.platform == "win32":
        system = os.environ["SystemRoot"].casefold() + "\\"
        paths = [p for p in paths if not p.casefold().startswith(system)]
    return paths


class Reading:
    """One process at one sample: counters, and their rates since the previous sample of the same process."""
    __slots__ = ("pid", "rss", "cpu_rate", "disk_rate", "paths")

    def __init__(self, pid):
        self.pid = pid
        self.rss = 0
        self.cpu_rate = 0.0
        self.disk_rate = 0.0
        self.paths = []


# ---------------------------------------------------------------- probes

class Probe:
    """Base class. `dims` maps dimension -> {"capacity": float, "label": str, "aliases": [str]}."""
    system_only = False          # True: no per-process measurement; callers attribute use themselves
    keep_without_dims = False    # True: kept by the Meter with no dimensions yet, because they can appear in a later sample

    def __init__(self, config: dict, local_dir: Path):
        self.config = config
        self.local_dir = local_dir
        self.dims = {}
        self.learnable = set()           # dimensions whose capacity is learned (not configured), so it may rise

    def start(self) -> None:
        """Called once by the Meter after construction; what it raises is reported, and the probe is kept."""

    def system(self, dt: float) -> dict:
        return {}

    def tree(self, readings: list, dt: float, system: dict) -> dict:
        return {}


class CpuProbe(Probe):
    def __init__(self, config, local_dir):
        super().__init__(config, local_dir)
        self.cores = psutil.cpu_count(logical=True) or 1
        self.dims = {"cpu": {"capacity": float(self.cores), "label": str(self.cores) + " logical cores", "aliases": []}}
        self.prev = psutil.cpu_times()

    def system(self, dt):
        now = psutil.cpu_times()
        total = sum(now) - sum(self.prev)
        idle = (now.idle - self.prev.idle) + (getattr(now, "iowait", 0) - getattr(self.prev, "iowait", 0))
        self.prev = now
        busy = 0.0 if total <= 0 else max(0.0, min(1.0, 1.0 - idle / total))
        return {"cpu": busy * self.cores}

    def tree(self, readings, dt, system):
        return {"cpu": sum(r.cpu_rate for r in readings)}


class RamProbe(Probe):
    def __init__(self, config, local_dir):
        super().__init__(config, local_dir)
        total = float(psutil.virtual_memory().total)
        self.dims = {"ram": {"capacity": total, "label": "physical memory", "aliases": ["memory"]}}

    def system(self, dt):
        vm = psutil.virtual_memory()
        return {"ram": float(vm.total - vm.available)}

    def tree(self, readings, dt, system):
        return {"ram": float(sum(r.rss for r in readings))}


def _whole_disk(identifier: str) -> str:
    """'disk0s2' or 'disk3s1s1' -> 'disk0' / 'disk3'."""
    if not identifier.startswith("disk"):
        return identifier
    digits = ""
    for ch in identifier[4:]:
        if not ch.isdigit():
            break
        digits += ch
    return "disk" + digits


def _mac_physical_disk(mountpoint: str) -> str:
    """The whole disk under a mount. Raises when diskutil fails or names none. Until 2026-11-10 every failure returned
    None and the mount was left out of the map without a word: on Sandbank, overloaded, `diskutil info /` ran past its
    timeout at 14:47 and again at the 16:27 boot, disk0 vanished from the dispatcher, that incomplete map was cached,
    and with the disks and mounts unchanged it was never asked again, so every lease naming disk:disk0 was refused."""
    out = subprocess.run(["diskutil", "info", "-plist", mountpoint], capture_output=True, timeout=10, check=True).stdout
    info = plistlib.loads(out)
    for store in info.get("APFSPhysicalStores", []):
        return _whole_disk(store["APFSPhysicalStore"])
    ident = info.get("ParentWholeDisk") or info.get("DeviceIdentifier")
    assert ident, "diskutil names no disk for " + mountpoint
    return _whole_disk(ident)


WINDOWS_PARTITIONS = "Get-Partition | Where-Object DriveLetter | ForEach-Object { [string]$_.DriveLetter + ' ' + [string]$_.DiskNumber }"


def disk_signature(counters: dict, skip=()) -> list:
    """What changes when a disk or a volume comes or goes: the physical disks the I/O counters name, and the mounts that
    skip keeps. Cheap (under a millisecond on Mistral), so DiskProbe compares it on every sample."""
    mounts = [p.mountpoint for p in psutil.disk_partitions(all=False) if not any(p.mountpoint.startswith(s) for s in skip)]
    return [sorted(counters), sorted(mounts)]


def disk_map(local_dir: Path, signature: list, max_age: float = 3600, skip=()) -> dict:
    """physical disk name (as psutil.disk_io_counters(perdisk=True) names it) -> list of mount points / drive letters.
    Cached per machine in the local folder for an hour and for as long as the disks and mounts are the ones it was built
    with (signature, from disk_signature), because it costs a diskutil or PowerShell call per volume. Until 2026-10-26 the
    cache ignored the disks present, so a disk plugged in within the hour was missing from every new meter.
    A mount that starts with an entry of skip (the config's disk_skip_mounts) is left out before that call: until
    2026-10-03 the map held every mount and DiskProbe dropped those afterwards, on Altiplano 11 of 12 diskutil calls,
    about 1 s of every first meter in a local folder and 3 s below the dispatcher. The cache names the entries it was
    built with and serves every caller whose list has them all."""
    skip = sorted(set(s for s in skip if s))
    cache = local_dir / "disks.json"
    cached = read_json(cache)
    if (cached.get("host") == socket.gethostname() and time.time() - float(cached.get("at", 0)) < max_age
            and cached.get("signature") == signature and all(s in skip for s in cached["skip"])):
        return cached["map"]
    mapping = {}
    if sys.platform == "darwin":
        for part in psutil.disk_partitions(all=False):
            if any(part.mountpoint.startswith(s) for s in skip):
                continue
            mapping.setdefault(_mac_physical_disk(part.mountpoint), []).append(part.mountpoint)
    elif sys.platform == "win32":
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_PARTITIONS],
                             capture_output=True, text=True, timeout=30, creationflags=no_window(), check=True).stdout
        for line in out.splitlines():
            letter, _, number = line.strip().partition(" ")
            if letter and number.strip().isdigit() and not any((letter + ":").startswith(s) for s in skip):
                mapping.setdefault("PhysicalDrive" + number.strip(), []).append(letter + ":")
    else:
        for part in psutil.disk_partitions(all=False):
            if any(part.mountpoint.startswith(s) for s in skip):
                continue
            name = Path(part.device).name
            base = name.rstrip("0123456789") if name.startswith("sd") or name.startswith("hd") else name
            mapping.setdefault(base, []).append(part.mountpoint)
    write_json(cache, {"host": socket.gethostname(), "at": time.time(), "map": mapping, "skip": skip, "signature": signature})
    return mapping


WINDOWS_SERIALS = "Get-PhysicalDisk | ForEach-Object { [string]$_.DeviceId + ' ' + [string]$_.SerialNumber }"


def disk_serials(local_dir: Path, signature: list) -> dict:
    """physical disk name -> serial number (upper case, no spaces), for matching a disk to its benchmark (D-261114-1).
    Windows only, where Get-PhysicalDisk names them without elevation; elsewhere {}, so a Mac's or a Linux machine's
    disks keep a configured or learned capacity. Cached in the local folder while the disks and mounts stay the same."""
    if sys.platform != "win32":
        return {}
    cache = local_dir / "serials.json"
    cached = read_json(cache)
    if cached.get("host") == socket.gethostname() and cached.get("signature") == signature:
        return cached["serials"]
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_SERIALS],
                         capture_output=True, text=True, timeout=30, creationflags=no_window(), check=True).stdout
    serials = {}
    for line in out.splitlines():
        number, _, serial = line.strip().partition(" ")
        serial = "".join(serial.split()).upper()
        if number.isdigit() and serial:
            serials["PhysicalDrive" + number] = serial
    write_json(cache, {"host": socket.gethostname(), "signature": signature, "serials": serials})
    return serials


MIB = 1024 * 1024                # DriveBench and the older benchmark scripts write MB for MiB


def _read_speeds(path: Path) -> tuple:
    """(outer read in MiB/s, kind) from one benchmark report, or (None, ''). DriveBench reports and Surface scans have
    a line 'Read average  155.78 MB/s   outer  207.48 MB/s   inner ...'; the console captures of the older
    benchmarkDrive.py have one 'Read completed in 5.48 seconds (186.98 MB/s).' per location, the first one outermost."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        words = line.split()
        if words[:2] == ["Read", "average"] and "outer" in words:
            return float(words[words.index("outer") + 1]), "outer read"
    located = [float(line.partition("(")[2].partition(" MB/s")[0]) for line in lines
               if line.strip().startswith("Read completed in") and " MB/s)" in line]
    if located:
        return max(located), "fastest of " + str(len(located)) + " read locations"
    return None, ""


def benchmarked_capacity(root: Path, serial: str) -> tuple:
    """(bytes/s, source) from the newest benchmark of the disk with this serial in the archive `root`, one folder per
    disk named '<model> <serial>' (DriveBench files its reports there), or (None, ''). The capacity is the outer-track
    sequential read: a spinning disk is fastest there, and a disk-bound lease is sized to what the disk can do at best
    (D-261114-1). A folder matches when its last word is the serial, or one is the other's start, at least 8
    characters long: SAS disks appear both as 'ABCD1234' and as 'ABCD12340000K0000XYZ'."""
    found = []
    for folder in root.iterdir():
        token = folder.name.split()[-1].upper() if folder.is_dir() and folder.name.split() else ""
        if not token or not (token == serial or (min(len(token), len(serial)) >= 8
                                                  and (serial.startswith(token) or token.startswith(serial)))):
            continue
        for path in folder.iterdir():
            report = "Benchmark" in path.name or (path.name.startswith("Surface ") and "health" not in path.name)
            if path.suffix.lower() != ".txt" or not report:   # not 'Surface health', 'Health' or 'Summary'
                continue
            speed, kind = _read_speeds(path)
            if speed is not None:
                found.append((path.stat().st_mtime, speed, kind, path))
    if not found:
        return None, ""
    _, speed, kind, path = max(found)
    return speed * MIB, kind + " " + format(speed, ".2f") + " MiB/s in " + path.parent.name + "/" + path.name


def _learned(local_dir: Path, name: str) -> dict:
    return read_json(local_dir / (name + ".json"))


class DiskProbe(Probe):
    """One dimension per physical disk, in bytes/s. Capacity: 'disk_capacity' in the config (by name or label alias),
    else the outer-track read of the disk's newest benchmark in the 'disk_benchmarks' archive, matched by serial
    (Windows only, D-261114-1), else the highest rate ever seen on that disk (learned by the dispatcher), at least
    'disk_default_bytes_per_s'. Each dimension says where its capacity came from ('source'), so a disk that still
    needs a benchmark shows in `res now` and in the report.
    The dimensions are rebuilt whenever a disk or a mount comes or goes (disk_signature, checked on every sample): until
    2026-10-26 they were fixed when the probe was made, and Mistral's dispatcher, started with five disks, refused every
    lease on the hot-swapped F: and H: for two days ('no such disk here') and put their bytes on the other disks.
    A map that raises leaves the signature unchanged, so the next sample asks again; the first map is made in start(),
    which the Meter calls inside its error reporting, so a probe whose first map failed stays and fills in later."""
    keep_without_dims = True
    retry_seconds = 60.0         # after a failed map: each diskutil call may block for up to 10 s, and the dispatcher samples in its main loop

    def __init__(self, config, local_dir):
        super().__init__(config, local_dir)
        self.skip = [s for s in config.get("disk_skip_mounts", []) if s]
        self.signature = None
        self.failed = None                           # (monotonic time, error) of the last failed map
        self.mounts = {}
        self.prev = psutil.disk_io_counters(perdisk=True)
        self.last_rates = {}
        self.busy = {}

    def start(self) -> None:
        self.refresh(self.prev)

    def refresh(self, counters: dict) -> None:
        signature = disk_signature(counters, self.skip)
        if signature == self.signature:
            return
        if self.failed is not None and time.monotonic() - self.failed[0] < self.retry_seconds:
            raise RuntimeError(self.failed[1] + " (asked again " + str(int(self.retry_seconds)) + " s after it failed)")
        try:
            mapping = disk_map(self.local_dir, signature, skip=self.skip)
        except Exception as exc:
            self.failed = (time.monotonic(), "disk map failed: " + type(exc).__name__ + ": " + str(exc))
            raise
        self.failed = None
        learned = _learned(self.local_dir, "learned_peaks")
        serials = disk_serials(self.local_dir, signature)
        # The archive is optional: a machine without it (or with a config from before 2026-11-14, which Resilio can
        # deliver after this file) keeps learned capacities, and each disk's source says so.
        named = self.config.get("disk_benchmarks")
        archive = Path(os.path.expanduser(named)) if named and Path(os.path.expanduser(named)).is_dir() else None
        configured = self.config.get("disk_capacity", {})
        default = float(self.config.get("disk_default_bytes_per_s", 150e6))
        dims, mounts, learnable = {}, {}, set()
        for disk in sorted(counters):
            kept = [m for m in mapping.get(disk, []) if not any(m.startswith(s) for s in self.skip)]
            if not kept:
                continue
            dim = "disk:" + disk
            aliases = [disk] + kept + [m.rstrip(":") for m in kept if m.endswith(":")]
            capacity, source = None, ""
            for alias in aliases:
                if alias in configured:
                    capacity, source = float(configured[alias]), "configured (disk_capacity " + alias + ")"
            if capacity is None and disk in serials and archive is not None:
                capacity, source = benchmarked_capacity(archive, serials[disk])
                source = "benchmark: " + source if capacity is not None else ""
            if capacity is None:
                capacity = max(default, float(learned.get(dim, 0)))
                learnable.add(dim)
                source = ("no benchmark for serial " + serials[disk] + (" in " + str(archive) if archive is not None
                                                                         else " (no disk_benchmarks archive here)")
                          + "; ") if disk in serials else ""
                source += "learned" if float(learned.get(dim, 0)) > default else "default"
            label = ", ".join(kept[:3]) + (" ..." if len(kept) > 3 else "")
            dims[dim] = {"capacity": capacity, "label": label, "aliases": aliases, "source": source}
            mounts[dim] = kept
        self.dims, self.mounts, self.learnable, self.signature = dims, mounts, learnable, signature

    def system(self, dt):
        now = psutil.disk_io_counters(perdisk=True)
        prev, self.prev = self.prev, now             # advanced before a refresh that may raise, so no rate spans two samples
        self.refresh(now)
        use = {}
        busy = {}
        for dim in self.dims:
            disk = dim.partition(":")[2]
            a, b = prev.get(disk), now.get(disk)
            if a is None or b is None or dt <= 0:
                use[dim] = 0.0
                continue
            use[dim] = max(0.0, ((b.read_bytes + b.write_bytes) - (a.read_bytes + a.write_bytes)) / dt)
            busy[dim] = max(0.0, min(1.0, ((b.read_time + b.write_time) - (a.read_time + a.write_time)) / 1000.0 / dt))
        self.last_rates = use
        self.busy = busy
        return use

    def device_of(self, path: str):
        best, best_len = None, -1
        folded = path.casefold() if sys.platform == "win32" else path
        for dim, mounts in self.mounts.items():
            for mount in mounts:
                m = mount.casefold() if sys.platform == "win32" else mount
                if folded.startswith(m) and len(m) > best_len:
                    best, best_len = dim, len(m)
        return best

    def tree(self, readings, dt, system):
        """Each process's bytes go to the disks it has files open on, split by what each of those disks physically
        moved, and no disk gets more than it moved: Windows' per-process counters include reads served from the page
        cache (355 MB/s per process against 1.6 MB/s on the disk, Moosfels 2026-09-27) and network and pipe I/O. The
        bytes of a process with no file open on a known disk go to no disk; they stay in the machine's unleased use.
        Until 2026-10-26 a tree's bytes went to the disks by what each moved whenever no open file was known, always on
        Windows, so a job reading only F: was blamed for C:, G: and E:, which other programs kept busy
        (Mistral, 2026-10-25 and -26)."""
        result = {dim: 0.0 for dim in self.dims}
        for r in readings:
            disks = sorted(set(self.device_of(p) for p in r.paths) - {None})
            moved = sum(system[d] for d in disks)
            for d in disks:
                result[d] += r.disk_rate * system[d] / moved if moved > 0 else 0.0
        return {d: min(v, system[d]) for d, v in result.items()}


def _usable_interface(addrs) -> bool:
    for a in addrs:
        if a.family not in (socket.AF_INET, socket.AF_INET6):
            continue
        address = str(a.address).partition("%")[0].casefold()
        if address.startswith("127.") or address == "::1" or address.startswith("fe80") or address.startswith("169.254."):
            continue
        return True
    return False


class NetProbe(Probe):
    """One dimension per interface that is up and has a routable address. Capacity: 'net_capacity' in the config, else
    the link speed psutil reports, else the highest rate seen, at least 'net_default_bytes_per_s'. System only."""
    system_only = True

    def __init__(self, config, local_dir):
        super().__init__(config, local_dir)
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        skip = [s.casefold() for s in config.get("net_skip", [])]
        learned = _learned(local_dir, "learned_peaks")
        configured = config.get("net_capacity", {})
        default = float(config.get("net_default_bytes_per_s", 125e6))
        for name, st in stats.items():
            if not st.isup or not _usable_interface(addrs.get(name, [])):
                continue
            if any(s in name.casefold() for s in skip):
                continue
            dim = "net:" + name
            bogus = [int(b) for b in config.get("net_bogus_speeds_mbit", [4294, 4295])]
            if name in configured:
                capacity = float(configured[name])
            elif st.speed and st.speed > 0 and st.speed not in bogus:
                capacity = st.speed * 125000.0
            else:
                capacity = max(default, float(learned.get(dim, 0)))
                self.learnable.add(dim)
            self.dims[dim] = {"capacity": capacity, "label": (str(st.speed) + " Mbit/s link" if st.speed else "link speed unknown"),
                              "aliases": [name]}
        self.prev = psutil.net_io_counters(pernic=True) or {}

    def system(self, dt):
        now = psutil.net_io_counters(pernic=True) or {}
        use = {}
        for dim in self.dims:
            name = dim.partition(":")[2]
            a, b = self.prev.get(name), now.get(name)
            if a is None or b is None or dt <= 0:
                use[dim] = 0.0
                continue
            use[dim] = max(0.0, (b.bytes_sent - a.bytes_sent) / dt, (b.bytes_recv - a.bytes_recv) / dt)
        self.prev = now
        return use


def _number(text: str):
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        return None


class NvidiaProbe(Probe):
    """gpu:N and vram:N for every NVIDIA GPU, through nvidia-smi. A call that fails or runs past its timeout raises, so
    the sample leaves the GPU out and the dispatcher logs `WARN probe failed`; until 2026-11-12 it returned no rows, and
    the GPU left the sample without a word (and a failed first call left the machine without GPU dimensions until the
    dispatcher restarted). The dimensions come from the first query that succeeds."""

    def __init__(self, config, local_dir):
        super().__init__(config, local_dir)
        self.exe = shutil.which("nvidia-smi")
        if not self.exe and sys.platform == "win32":
            candidate = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "nvidia-smi.exe"
            self.exe = str(candidate) if candidate.is_file() else None
        self.keep_without_dims = self.exe is not None
        self.gpus = {}
        self.apps = {}
        self.users = {}
        self.vram_per_process = True

    def start(self) -> None:
        if self.exe:
            self.system(0.0)

    def _run(self, args: list) -> list:
        out = subprocess.run([self.exe] + args, capture_output=True, text=True, timeout=10, creationflags=no_window(),
                             check=True).stdout
        return [[field.strip() for field in line.split(",")] for line in out.splitlines() if line.strip()]

    def _query(self):
        self.gpus, self.apps, self.users = {}, {}, {}      # a failed query leaves no stale figures for tree()
        gpus = {}
        uuids = {}
        for row in self._run(["--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total",
                              "--format=csv,noheader,nounits"]):
            if len(row) < 6:
                continue
            util, used, total = _number(row[3]), _number(row[4]), _number(row[5])
            gpus[row[0]] = {"name": row[2], "util": util or 0.0, "used": (used or 0.0) * MIB, "total": (total or 0.0) * MIB}
            uuids[row[1]] = row[0]
        apps = {}
        users = {}
        known = True
        for row in self._run(["--query-compute-apps=gpu_uuid,pid,used_memory", "--format=csv,noheader,nounits"]):
            if len(row) < 3 or row[0] not in uuids or not row[1].isdigit():
                continue
            users.setdefault(uuids[row[0]], set()).add(int(row[1]))
            mem = _number(row[2])
            if mem is None:
                known = False                     # WDDM drivers report [N/A]; which pids use the GPU is still known
                continue
            apps.setdefault(uuids[row[0]], {})[int(row[1])] = mem * MIB
        self.gpus, self.apps, self.users, self.vram_per_process = gpus, apps, users, known

    def system(self, dt):
        self._query()
        use = {}
        for index, info in sorted(self.gpus.items()):
            self.dims.setdefault("gpu:" + index, {"capacity": 100.0, "label": info["name"], "aliases": []})
            self.dims.setdefault("vram:" + index, {"capacity": info["total"], "label": info["name"] + " memory", "aliases": []})
            use["gpu:" + index] = info["util"]
            use["vram:" + index] = info["used"]
        return use

    def tree(self, readings, dt, system):
        """A tree's GPU use comes only from its own processes on the card (nvidia-smi's compute apps), never from the
        machine's total: a trial on Mistral otherwise asked for 84 % of a GPU it did not touch, because Ollama was busy.
        With per-process memory: that memory, and utilisation split by memory share. Without (WDDM): a tree holding a
        compute process gets the card's figures, split evenly among the processes on it; a tree holding none gets 0."""
        pids = {r.pid for r in readings}
        result = {}
        for index, info in self.gpus.items():
            users = self.users.get(index, set())
            on_card = users & pids
            if not on_card:
                result["vram:" + index] = 0.0
                result["gpu:" + index] = 0.0
                continue
            apps = self.apps.get(index, {})
            if apps and all(pid in apps for pid in users):
                mine = sum(apps[pid] for pid in on_card)
                everyone = sum(apps.values())
                result["vram:" + index] = mine
                result["gpu:" + index] = info["util"] * (mine / everyone) if everyone > 0 else 0.0
            else:
                share = len(on_card) / max(1, len(users))
                result["vram:" + index] = info["used"] * share
                result["gpu:" + index] = info["util"] * share
        return result


class FileProbe(Probe):
    """Dimensions fed by JSON files named in the config's 'file_dimensions' ({dim: {"capacity", "file", "label"}}).
    The file holds {"total": use, "pids": {"<pid>": use}}. Used by the tests to simulate a GPU on a machine without one."""

    def __init__(self, config, local_dir):
        super().__init__(config, local_dir)
        self.files = {}
        for dim, spec in (config.get("file_dimensions") or {}).items():
            if not isinstance(spec, dict) or not spec.get("file"):
                continue
            self.dims[dim] = {"capacity": float(spec.get("capacity", 100)), "label": spec.get("label", "from " + spec["file"]),
                              "aliases": list(spec.get("aliases", []))}
            self.files[dim] = Path(os.path.expanduser(spec["file"]))
        self.data = {}

    def system(self, dt):
        self.data = {dim: read_json(path) for dim, path in self.files.items()}
        return {dim: float(d.get("total", 0) or 0) for dim, d in self.data.items()}

    def tree(self, readings, dt, system):
        pids = {str(r.pid) for r in readings}
        result = {}
        for dim, d in self.data.items():
            per = d.get("pids") or {}
            result[dim] = float(sum(float(v) for k, v in per.items() if k in pids))
        return result


PROBES = [CpuProbe, RamProbe, DiskProbe, NvidiaProbe, NetProbe, FileProbe]


# ---------------------------------------------------------------- the meter

class Meter:
    """Samples every probe: the machine's use of every dimension, and the use of named process trees."""

    def __init__(self, config: dict, local_dir: Path, first_sample_rates: bool = True):
        """first_sample_rates: give a process first seen in this sample its whole life's average as its rate (right
        for a 10-second trial, where short commands matter); the dispatcher turns it off, because a program's start-up
        burst averaged over a fraction of a second read as 1.45 cores for a sleeping job (Teppich, 2026-09-27)."""
        self.config = config
        self.local_dir = local_dir
        self.first_sample_rates = first_sample_rates
        self.probes = []
        self.errors = []                                  # what the probes raised in the last sample (or in start)
        # Both failures go into self.errors, which the dispatcher logs, and to stderr where there is one: under pythonw
        # (the Windows dispatcher) sys.stderr is None, and until 2026-11-12 writing to it raised AttributeError, so a
        # probe failing at start would have ended the dispatcher.
        for cls in PROBES:
            try:
                probe = cls(config, local_dir)
            except Exception as exc:                     # a broken probe must not take the others down
                error = cls.__name__ + " unavailable: " + type(exc).__name__ + ": " + str(exc)
                self.errors.append(error)
                if sys.stderr is not None:
                    sys.stderr.write("probe " + error + "\n")
                continue
            try:
                probe.start()
            except Exception as exc:                     # reported; the probe asks again in its next sample
                error = cls.__name__ + ".start: " + type(exc).__name__ + ": " + str(exc)
                self.errors.append(error)
                if sys.stderr is not None:
                    sys.stderr.write("probe " + error + "\n")
            if probe.dims or probe.keep_without_dims:
                self.probes.append(probe)
        self.prev = {}                                    # (pid, create_time) -> (time, cpu seconds, disk bytes)
        self.paths = {}                                   # (pid, create_time) -> (time, open files), see process_paths
        self.paths_seconds = float(config["disk_paths_seconds"])
        self.last = time.monotonic()
        self.last_wall = time.time()

    def dimensions(self) -> dict:
        dims = {}
        for probe in self.probes:
            dims.update(probe.dims)
        return dims

    def system_only(self) -> set:
        dims = set()
        for probe in self.probes:
            if probe.system_only:
                dims.update(probe.dims)
        return dims

    def resolve(self, name: str):
        """A dimension name as a user or another machine wrote it -> this machine's dimension, or None."""
        dims = self.dimensions()
        if name in dims:
            return name
        kind, _, rest = name.partition(":")
        for dim, info in dims.items():
            if kind_of(dim) != kind:
                continue
            if rest and any(rest.casefold() == str(a).casefold() for a in info.get("aliases", [])):
                return dim
        if not rest:
            matches = [d for d in dims if kind_of(d) == kind]
            if len(matches) == 1:
                return matches[0]
            if kind in ("gpu", "vram") and kind + ":0" in dims:
                return kind + ":0"
        return None

    def _read(self, proc: psutil.Process, now: float):
        try:
            with proc.oneshot():
                ctime = proc.create_time()
                times = proc.cpu_times()
                cpu = times.user + times.system
                rss = proc.memory_info().rss
            disk = disk_bytes(proc)
        except (psutil.Error, OSError):
            return None
        # The rates span this process's own two readings. Until 2026-11-13 they spanned two sample starts, and a process
        # read later in a sample than in the one before (behind other processes' open files, 0.04 to 0.4 s each on
        # Windows) was given the extra seconds of cpu and bytes over too short a span: 43.5 and 114.7 cores on Mistral's
        # 32 threads, L261113-141333440, a job whose pools of 32 fresh workers were read fast once, then slowly.
        read_at = time.monotonic()
        reading = Reading(proc.pid)
        reading.rss = rss
        key = (proc.pid, ctime)
        before = self.prev.get(key)
        if before is not None:
            span = read_at - before[0]
            if span > 0:
                reading.cpu_rate = max(0.0, (cpu - before[1]) / span)
                if disk is not None and before[2] is not None:
                    reading.disk_rate = max(0.0, (disk - before[2]) / span)
        elif self.first_sample_rates and ctime > self.last_wall - 0.5:
            span = max(0.05, time.time() - ctime)        # born since the last sample: its whole life so far
            reading.cpu_rate = cpu / span
            reading.disk_rate = (disk or 0) / span
        self.prev[key] = (read_at, cpu, disk)
        # Open files matter only for a process that moved bytes. They are read at once when none are known (a writer
        # that opened, filled and closed a 200 MB file within a second was missed by a plain 1-second cache, suite
        # T1 on Mistral, 2026-10-26) and otherwise at most every disk_paths_seconds; an idle process is never read.
        known = self.paths.get(key)
        if reading.disk_rate > 0 and (not known or not known[1] or now - known[0] >= self.paths_seconds):
            try:
                self.paths[key] = known = (now, process_paths(proc))
            except psutil.NoSuchProcess:
                return None
        reading.paths = known[1] if known else []
        return reading

    def sample(self, trees: dict) -> tuple:
        """trees: {name: [psutil.Process]} -> (system {dim: use}, {name: {dim: use}}). System-only dimensions are
        absent from the per-tree results. A probe that raises leaves its dimensions out of this sample and the others
        go on; what it raised is in self.errors for the caller to report (until 2026-10-26 it was dropped unseen)."""
        now = time.monotonic()
        dt = max(1e-3, now - self.last)
        system = {}
        self.errors = []
        for probe in self.probes:
            try:
                system.update(probe.system(dt))
            except Exception as exc:
                self.errors.append(type(probe).__name__ + ".system: " + type(exc).__name__ + ": " + str(exc))
        per_tree = {}
        seen = set()
        for name, procs in trees.items():
            readings = []
            for proc in procs:
                if proc.pid in seen:                     # in an earlier tree too (a leased job's own `res run`): read once,
                    continue                             # counted there; a second read milliseconds later is no rate
                r = self._read(proc, now)
                if r is not None:
                    readings.append(r)
                    seen.add(proc.pid)
            use = {}
            for probe in self.probes:
                if probe.system_only:
                    continue
                try:
                    use.update(probe.tree(readings, dt, system))
                except Exception as exc:
                    self.errors.append(type(probe).__name__ + ".tree: " + type(exc).__name__ + ": " + str(exc))
            for dim in self.system_only():
                use.pop(dim, None)
            per_tree[name] = use
        self.prev = {k: v for k, v in self.prev.items() if k[0] in seen}
        self.paths = {k: v for k, v in self.paths.items() if k[0] in seen}
        self.last = now
        self.last_wall = time.time()
        return system, per_tree

    def raise_learned(self, learned: dict) -> list:
        """Raise learned capacities to the highest rate seen, in place, so a running dispatcher uses them at once (it
        used to see a rise only after a restart; `res now` builds a new meter and always saw it)."""
        raised = []
        for probe in self.probes:
            for dim in probe.learnable:
                value = float(learned.get(dim, 0))
                if dim in probe.dims and value > probe.dims[dim]["capacity"]:
                    probe.dims[dim]["capacity"] = value
                    raised.append(dim)
        return raised

    def disk_busy(self) -> dict:
        for probe in self.probes:
            if isinstance(probe, DiskProbe):
                return dict(getattr(probe, "busy", {}))
        return {}
