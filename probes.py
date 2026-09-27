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
io_counters, which on Windows counts all I/O, not only disk) and spread across the disks the tree has files open on;
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
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp" + str(os.getpid()))
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


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
    """The paths a process has open plus its working folder: where its disk bytes probably go."""
    paths = []
    try:
        paths.append(proc.cwd())
    except (psutil.Error, OSError):
        pass
    try:
        paths.extend(f.path for f in proc.open_files())
    except (psutil.Error, OSError):
        pass
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

    def __init__(self, config: dict, local_dir: Path):
        self.config = config
        self.local_dir = local_dir
        self.dims = {}

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


def _mac_physical_disk(mountpoint: str):
    try:
        out = subprocess.run(["diskutil", "info", "-plist", mountpoint], capture_output=True, timeout=10).stdout
        info = plistlib.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException):
        return None
    stores = info.get("APFSPhysicalStores") or []
    for store in stores:
        ident = store.get("APFSPhysicalStore")
        if ident:
            return _whole_disk(ident)
    ident = info.get("ParentWholeDisk") or info.get("DeviceIdentifier")
    return _whole_disk(ident) if ident else None


WINDOWS_PARTITIONS = "Get-Partition | Where-Object DriveLetter | ForEach-Object { [string]$_.DriveLetter + ' ' + [string]$_.DiskNumber }"


def disk_map(local_dir: Path, max_age: float = 3600) -> dict:
    """physical disk name (as psutil.disk_io_counters(perdisk=True) names it) -> list of mount points / drive letters.
    Cached per machine in the local folder for an hour, because it costs a diskutil or PowerShell call per volume."""
    cache = local_dir / "disks.json"
    cached = read_json(cache)
    if cached.get("host") == socket.gethostname() and time.time() - float(cached.get("at", 0)) < max_age:
        return cached.get("map", {})
    mapping = {}
    if sys.platform == "darwin":
        for part in psutil.disk_partitions(all=False):
            disk = _mac_physical_disk(part.mountpoint)
            if disk:
                mapping.setdefault(disk, []).append(part.mountpoint)
    elif sys.platform == "win32":
        try:
            out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_PARTITIONS],
                                 capture_output=True, text=True, timeout=30, creationflags=no_window()).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            letter, _, number = line.strip().partition(" ")
            if letter and number.strip().isdigit():
                mapping.setdefault("PhysicalDrive" + number.strip(), []).append(letter + ":")
    else:
        for part in psutil.disk_partitions(all=False):
            name = Path(part.device).name
            base = name.rstrip("0123456789") if name.startswith("sd") or name.startswith("hd") else name
            mapping.setdefault(base, []).append(part.mountpoint)
    try:
        write_json(cache, {"host": socket.gethostname(), "at": time.time(), "map": mapping})
    except OSError:
        pass
    return mapping


def _learned(local_dir: Path, name: str) -> dict:
    return read_json(local_dir / (name + ".json"))


class DiskProbe(Probe):
    """One dimension per physical disk, in bytes/s. Capacity: 'disk_capacity' in the config (by name or label alias),
    else the highest rate ever seen on that disk (learned by the dispatcher), at least 'disk_default_bytes_per_s'."""

    def __init__(self, config, local_dir):
        super().__init__(config, local_dir)
        counters = psutil.disk_io_counters(perdisk=True) or {}
        mapping = disk_map(local_dir)
        skip = [s for s in config.get("disk_skip_mounts", []) if s]
        learned = _learned(local_dir, "learned_peaks")
        configured = config.get("disk_capacity", {})
        default = float(config.get("disk_default_bytes_per_s", 150e6))
        self.mounts = {}
        for disk in sorted(counters):
            mounts = mapping.get(disk, [])
            kept = [m for m in mounts if not any(m.startswith(s) for s in skip)]
            if not kept:
                continue
            dim = "disk:" + disk
            aliases = [disk] + kept + [m.rstrip(":") for m in kept if m.endswith(":")]
            capacity = None
            for alias in aliases:
                if alias in configured:
                    capacity = float(configured[alias])
            if capacity is None:
                capacity = max(default, float(learned.get(dim, 0)))
            label = ", ".join(kept[:3]) + (" ..." if len(kept) > 3 else "")
            self.dims[dim] = {"capacity": capacity, "label": label, "aliases": aliases}
            self.mounts[dim] = kept
        self.prev = counters
        self.last_rates = {}

    def system(self, dt):
        now = psutil.disk_io_counters(perdisk=True) or {}
        use = {}
        busy = {}
        for dim in self.dims:
            disk = dim.partition(":")[2]
            a, b = self.prev.get(disk), now.get(disk)
            if a is None or b is None or dt <= 0:
                use[dim] = 0.0
                continue
            use[dim] = max(0.0, ((b.read_bytes + b.write_bytes) - (a.read_bytes + a.write_bytes)) / dt)
            busy[dim] = max(0.0, min(1.0, ((b.read_time + b.write_time) - (a.read_time + a.write_time)) / 1000.0 / dt))
        self.prev = now
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
        total = sum(r.disk_rate for r in readings)
        result = {dim: 0.0 for dim in self.dims}
        if total <= 0 or not self.dims:
            return result
        devices = set()
        for r in readings:
            for path in r.paths:
                dev = self.device_of(path)
                if dev:
                    devices.add(dev)
        candidates = sorted(devices) if devices else sorted(self.dims)
        weights = {d: system.get(d, 0.0) for d in candidates}
        wsum = sum(weights.values())
        for d in candidates:
            result[d] = total * (weights[d] / wsum if wsum > 0 else 1.0 / len(candidates))
        return result


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
            if any(name.casefold().startswith(s) for s in skip):
                continue
            dim = "net:" + name
            if name in configured:
                capacity = float(configured[name])
            elif st.speed and st.speed > 0:
                capacity = st.speed * 125000.0
            else:
                capacity = max(default, float(learned.get(dim, 0)))
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
    """gpu:N and vram:N for every NVIDIA GPU, through nvidia-smi."""

    def __init__(self, config, local_dir):
        super().__init__(config, local_dir)
        self.exe = shutil.which("nvidia-smi")
        if not self.exe and sys.platform == "win32":
            candidate = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "nvidia-smi.exe"
            self.exe = str(candidate) if candidate.is_file() else None
        self.gpus = {}
        self.apps = {}
        self.vram_per_process = True
        if not self.exe:
            return
        self._query()
        for index, info in sorted(self.gpus.items()):
            self.dims["gpu:" + index] = {"capacity": 100.0, "label": info["name"], "aliases": []}
            self.dims["vram:" + index] = {"capacity": info["total"], "label": info["name"] + " memory", "aliases": []}

    def _run(self, args: list) -> list:
        try:
            out = subprocess.run([self.exe] + args, capture_output=True, text=True, timeout=10,
                                 creationflags=no_window()).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [[field.strip() for field in line.split(",")] for line in out.splitlines() if line.strip()]

    def _query(self):
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
        known = True
        for row in self._run(["--query-compute-apps=gpu_uuid,pid,used_memory", "--format=csv,noheader,nounits"]):
            if len(row) < 3 or row[0] not in uuids or not row[1].isdigit():
                continue
            mem = _number(row[2])
            if mem is None:
                known = False
                continue
            apps.setdefault(uuids[row[0]], {})[int(row[1])] = mem * MIB
        self.gpus, self.apps, self.vram_per_process = gpus, apps, known

    def system(self, dt):
        self._query()
        use = {}
        for index, info in self.gpus.items():
            use["gpu:" + index] = info["util"]
            use["vram:" + index] = info["used"]
        return use

    def tree(self, readings, dt, system):
        pids = {r.pid for r in readings}
        result = {}
        for index, info in self.gpus.items():
            apps = self.apps.get(index, {})
            mine = sum(mem for pid, mem in apps.items() if pid in pids)
            everyone = sum(apps.values())
            result["vram:" + index] = mine
            result["gpu:" + index] = info["util"] * (mine / everyone) if everyone > 0 else 0.0
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

    def __init__(self, config: dict, local_dir: Path):
        self.config = config
        self.local_dir = local_dir
        self.probes = []
        for cls in PROBES:
            try:
                probe = cls(config, local_dir)
            except Exception as exc:                     # a broken probe must not take the others down
                sys.stderr.write("probe " + cls.__name__ + " unavailable: " + str(exc) + "\n")
                continue
            if probe.dims:
                self.probes.append(probe)
        self.prev = {}                                    # (pid, create_time) -> (time, cpu seconds, disk bytes)
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
            if probe.system_only or (isinstance(probe, NvidiaProbe) and not probe.vram_per_process):
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

    def _read(self, proc: psutil.Process, now: float, want_paths: bool):
        try:
            with proc.oneshot():
                ctime = proc.create_time()
                times = proc.cpu_times()
                cpu = times.user + times.system
                rss = proc.memory_info().rss
            disk = disk_bytes(proc)
        except (psutil.Error, OSError):
            return None
        reading = Reading(proc.pid)
        reading.rss = rss
        key = (proc.pid, ctime)
        before = self.prev.get(key)
        if before is not None:
            span = now - before[0]
            if span > 0:
                reading.cpu_rate = max(0.0, (cpu - before[1]) / span)
                if disk is not None and before[2] is not None:
                    reading.disk_rate = max(0.0, (disk - before[2]) / span)
        elif ctime > self.last_wall - 0.5:
            span = max(0.05, time.time() - ctime)        # born since the last sample: its whole life so far
            reading.cpu_rate = cpu / span
            reading.disk_rate = (disk or 0) / span
        self.prev[key] = (now, cpu, disk)
        if want_paths:
            reading.paths = process_paths(proc)
        return reading

    def sample(self, trees: dict, want_paths: bool = True) -> tuple:
        """trees: {name: [psutil.Process]} -> (system {dim: use}, {name: {dim: use}}). System-only dimensions are
        absent from the per-tree results."""
        now = time.monotonic()
        dt = max(1e-3, now - self.last)
        system = {}
        for probe in self.probes:
            try:
                system.update(probe.system(dt))
            except Exception:
                pass
        per_tree = {}
        seen = set()
        for name, procs in trees.items():
            readings = []
            for proc in procs:
                r = self._read(proc, now, want_paths)
                if r is not None:
                    readings.append(r)
                    seen.add(proc.pid)
            use = {}
            for probe in self.probes:
                if probe.system_only:
                    continue
                try:
                    use.update(probe.tree(readings, dt, system))
                except Exception:
                    pass
            for dim in self.system_only():
                use.pop(dim, None)
            per_tree[name] = use
        self.prev = {k: v for k, v in self.prev.items() if k[0] in seen}
        self.last = now
        self.last_wall = time.time()
        return system, per_tree

    def disk_busy(self) -> dict:
        for probe in self.probes:
            if isinstance(probe, DiskProbe):
                return dict(getattr(probe, "busy", {}))
        return {}
