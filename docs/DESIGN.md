# LeaseResources design

This is the design in depth, adapted from the service's own README. The [README](../README.md) is the short version; [HISTORY.md](HISTORY.md) explains why things are the way they are. Dates mark when a behaviour was introduced, where that explains it.

## Principles

- **No favourite bottleneck.** Every resource is a named dimension with a capacity and a measured use, and every decision is made the same way on every dimension. Nothing knows which resource is scarce, so a new bottleneck is one new probe, not a new mechanism.
- **Measure, don't declare.** Admission counts what the machine is actually using, so services and programs that never ask for anything are still accounted for.
- **Report, never kill.** The dispatcher tells a lease's owner when its job overruns, runs past its duration or stalls; only the owner kills, with `res release --kill`.
- **Guidance, measured.** Nothing forces a session to trial or lease. The dispatcher records how often sessions do and how often they run long commands without a lease, and the user decides from that record whether a hard cap is needed.
- **Shaped like ClaudeMail.** One dispatcher per machine under a small keeper, one synced script that every machine runs, per-machine config, restarts onto new code by itself, never starts Claude.

## Dimensions and probes

`probes.py` holds one `Probe` subclass per kind of resource (cpu, ram, disk, gpu with its vram, net, and dimensions read from files). Each reports, for its dimensions, the capacity, the machine's total use and, where it can, the use of a given set of processes. The `Meter` samples a process tree with all probes. A probe that raises in a sample leaves its dimensions out of that sample while the others go on, and what it raised is logged once until it changes, so a failure never reads as zero use.

| Dimension | Capacity | Machine use | Per process tree |
|---|---|---|---|
| `cpu` | logical cores | busy share of all cores | CPU seconds per second (exact) |
| `ram` | physical memory | total minus available | resident bytes (shared pages counted in each process) |
| `disk:<physical disk>` | `disk_capacity` in the config, else the outer-track read of the disk's newest benchmark in the drive archive named by `disk_benchmarks` (one folder per drive named `<model> <serial>`, holding benchmark reports with a `Read average ... outer ... MB/s` line, MB meaning MiB), matched by the serial Windows reports, else the highest rate ever seen, at least `disk_default_bytes_per_s` (150 MB/s); a learned capacity rises as soon as a higher rate is seen | bytes per second read and written | each process's bytes, put on the disks it has files open on, split by what each of those disks physically moved, and no disk given more than it moved |
| `gpu:<n>`, `vram:<n>` | 100 %, the card's memory | `nvidia-smi` | only through the tree's own compute processes on the card: their memory and a memory share of utilisation, or an even share of the card's figures where the driver reports `[N/A]` (WDDM); a tree with no process on the card gets 0 |
| `net:<interface>` | `net_capacity`, else the link speed, else the highest rate seen, at least 125 MB/s | bytes per second in the busier direction | not measurable per process: a trial counts the machine's rise over a baseline taken just before it, and the dispatcher counts a running lease at its declared amount |
| `file_dimensions` | as configured | a JSON file `{"total": x, "pids": {"<pid>": x}}` | from that file |

**Disks.** A disk dimension is a physical device (`disk0` on a Mac, `PhysicalDrive1` on Windows), labelled with its mounts or drive letters, and a request may name it either way. Disks are mapped with `diskutil` on the Mac and `Get-Partition` on Windows. The map is cached in `disks.json` with the disks and mounts it was built for, and every meter compares those on every sample (under a millisecond), so a hot-swapped disk should be a dimension within one sample and a pulled one gone (shown in the tests, not yet on a live swap). Apple system volumes, simulator images and cryptex mounts are skipped before `diskutil` is asked about them (`disk_skip_mounts`). A disk map that fails is not cached and is asked again at most once a minute; a running meter keeps the disk dimensions it had meanwhile, so a slow `diskutil` on an overloaded Mac can no longer make a disk vanish and every lease that names it be refused.

**Per-process disk bytes.** On the Mac they come from `proc_pid_rusage` (physical I/O), on Windows from psutil's `io_counters`, which count all I/O, including reads served from the page cache, network and pipes. Each process's bytes are put on the disks it has files open on (psutil `open_files()`, read at once for a process that starts moving bytes and then at most every `disk_paths_seconds`), never on the disk of its working folder. Capping each disk's share at what it physically moved keeps the Windows overcount bounded. On Windows the files under the Windows folder are ignored, since every process holds some there.

**Writes that drain from the page cache.** A process's write counters rise when it writes into the page cache, but the disk's counters rise later, as the cache drains. A job that copied 3 GB from a RAM disk to a hard disk wrote all of it into the cache in under a second and then waited in `fsync` for 15 s while the disk wrote 210 MB/s, and its process counters did not move. Matching process bytes to disk bytes sample by sample therefore credited none of those jobs with any disk use, and the whole copy was counted as unleased use, still held by the 30-second average after the job ended. So when the dispatcher computes unleased use, it credits a running lease with its disk's traffic up to the lease's need, or with what its processes were seen moving if that is more. This cannot overbook the disk, because the lease's full need is already counted as granted. The per-process figure stays what the lease's own peaks record.

**Network interfaces** that are down, have no routable address, or match `net_skip` (WSL, VMware, VirtualBox, Hyper-V, Tailscale, ZeroTier, VPN adapters, the Macs' `utun`) are left out, since they carry the same traffic as the real link. Virtual adapters that report a fake 4294 Mbit/s link speed (`net_bogus_speeds_mbit`) have their speed ignored.

**Startup bursts** are not rates: a process is metered from its second sample on, since a program's first fraction of a second once read as 1.45 cores for a sleeping job.

**Each process's rates span its own two readings**, timed when it is read, not the two sample starts. Reading other processes' open files delays a process within a sample by up to 0.4 s each on Windows, so when a pool of 32 fresh workers was read quickly in one sample and slowly in the next, the late workers were given seconds of extra cpu over too short a span, and a 32-thread machine read 43.5 and 114.7 cores for one job (fixed 2026-11-13). A process in two lease trees (a leased job's own `res run`) is read once per sample and counted in the first.

## The trial

`res trial -- <command>` starts the command and meters its whole process tree every `trial_sample_seconds` (0.25). A timer thread kills the tree at `trial_seconds` (10), independently of the sampling loop. On the Macs the kill goes to the command's own process group plus every process seen in the tree (including a `setsid` child and an orphaned new-session grandchild). On Windows the command is started suspended, put into a Job Object with kill-on-close, and only then resumed, so no child can start outside the job. Background processes the command leaves behind are killed too. The trial prints whether it finished, the output tail and the profile (peak and mean per dimension), saves the profile under an id, and exits 0 when the command finished inside the limit and 3 when it was cut off. It keeps at most `trial_output_max_bytes` (10 MB) of output.

Output echoed from jobs (the trial's tail, log tails, the waiter's stream) is read as UTF-8 with undecodable bytes replaced, and printed through streams that escape what the console cannot encode, so a script that prints in cp1252 on a Windows console no longer ends `res trial` with a traceback in place of its profile.

## Admission

A request names its needs per dimension: a trial's peaks plus `margin` (25 %), at least `request_floor` per kind, or explicit amounts with `--need`. It is granted when, on every dimension,

> unleased use + needs of the leases already granted + needs of the requests queued ahead of it + the request ≤ capacity × headroom

where unleased use is the machine's use minus the measured use of running leases (on a disk at least the lease's own need, if the disk moved that much), averaged over `smoothing_seconds` (30), and `headroom` is 0.85, overridden per dimension or kind in `headroom_by_dimension`: 1.0 for disk, net and gpu (a job that gets less of a disk, a link or a GPU's compute only runs slower) and 0.95 for cpu, while ram and GPU memory, where a job can fail, keep 0.85. A machine's own `headroom_by_dimension` is merged into the default one key by key. Otherwise it queues. The queue is served in request order with the same rule, so a request may overtake a blocked queue head, but only into room that leaves everything the requests ahead of it need. On the dimension the head waits for, nothing overtakes. Strict first come, first served had a cpu-only request wait 14 hours behind a head that was blocked on a busy disk. A request larger than capacity × headroom on cpu, ram, a GPU or its memory, or for a kind the machine lacks (vram on a Mac), is refused with the dimension named. A disk or network need above that limit is cut to the limit and granted, with a note in the answer. A job that runs a disk flat out peaks at about the disk's own speed, so its trial peak plus the 25 % margin always exceeds the disk's capacity, while a job given less of a disk only runs slower. On such a capped dimension the sum is held to the capacity itself, so traffic nobody leased fits into the headroom instead of keeping the lease waiting until it falls to zero. The answer comes within a second or two.

A queued lease's report entry carries `waits_for`, the dimension it waits for now, and the reason as of the last change of that dimension. A lease still queued `queue_notice_minutes` (30) after its answer gets one notice. The notice names what blocks it and lists the long commands of Claude sessions running on that machine without a lease. It also gives the `res release` line. On 2026-11-12 a lease that waited 15 hours at the head of the queue was most likely blocked by its own job, which had been started without the lease while the lease waited.

A granted lease that is never started expires after `start_within_minutes` (60). `res extend` always grants more time and grants more resources only when they fit now; `--for` sets the new total duration counted from the job's start. `--need` changes only the dimensions it names, and only the dimensions it raises are tested, by the admission rule without the queue and with the lease's new amount in place of its old one. A cut is always accepted, and what the lease already holds is never tested again: until 2026-11-13 the whole changed need was, and a lease holding 180 of 186 MB/s on one disk, with 13 MB/s there that the meter did not put on the lease, was refused more of another disk.

## Running a job

`res run --lease <id> -- <program>` writes a launch file into a local folder that is never synced and starts the runner through a short-lived intermediate that exits at once (a double spawn). The runner starts the job in its own session or process group (outside Claude's job object on Windows, where breaking away is allowed), with `PYTHONUNBUFFERED=1`, `LEASERES_LEASE` set, and its output going to `<id>.log`. The job therefore has no live ancestor among the session's processes and survives an interrupted turn, the session closing and an app restart. On a queued lease, or with `--on`, `res run` instead writes a run order that the dispatcher starts when the lease is granted.

Job logs rotate at `lease_log_max_bytes` (50 MB, the older part kept as `<id>.log.1`).

## Watching

Every `sample_seconds` (5) the dispatcher meters each running lease's process tree. A lease that uses more than it declared on a measurable dimension, by more than `overrun_margin` (10 %) and more than the kind's `overrun_floor`, for `overrun_samples` (3) samples in a row, produces one notice per dimension. Running past its duration produces one notice, and no log growth for `stall_minutes` (30) produces one. When the job ends (finished, failed with its exit code, or lost because its runner vanished, after a reboot say), the owner gets a finish notice with the status, run time, peaks against the declared needs, earlier run times of the same command and the last 30 log lines, all paths absolute.

Each finished lease adds its command, run time, exit code and peaks to `history.json` (20 runs for each of at most 500 commands). The next request from a trial of the same command, and every `res run`, is told the earlier run times, and a stated duration below half or above three times the median is flagged.

## Notices: the waiter first, mail otherwise

Until 2026-10-01 every notice was ClaudeMail to the owner's desk. A desk is a project folder that several sessions may share, and such mail was taken by sibling sessions. Now a notice goes to a process that waits on the lease while one is alive, and by mail only otherwise.

- `res run --wait` and `res wait <id>` register as the lease's waiter (`attached/<id>.json` in the local state folder; leases requested on this machine only, one waiter per lease).
- While the waiter lives, the dispatcher writes each notice as a file under `notices/<id>/` instead of mailing it. The waiter prints it and deletes it. Claims are by rename, so a notice is either printed or mailed, not both.
- Start, grant and extension notices are printed and the waiting continues. Overrun, past-duration and stall notices end the waiting with exit code 75 after printing that the job is still running and that `res wait <id>` re-attaches. The finish notice ends it with the job's exit code (1 if there is none).
- If the job has ended and no finish notice comes within 3 × (`sample_seconds` + `tick_seconds`), 18 s by default, the dispatcher is down: the waiter exits anyway and the dispatcher mails the full notice when it runs again.
- A notice whose waiter died before taking it is mailed on the next dispatcher tick. This works from the files, so it survives a dispatcher restart.
- With no waiter alive, the notice is mail: `mail.py send --to <desk> --for-session <requesting session>` from this folder, so the sender is `<Machine>:LeaseResources` and only the session that requested the lease takes it. If ClaudeMail refuses the mail (an unknown desk, say), it goes nowhere else and the user gets a desktop notification.

The exit is what wakes a Claude session that started the command as a background command: the harness re-invokes the session with the notice in the command's output. A session therefore never needs to poll.

## The dispatcher and its keeper

`res.py watch` is the keeper that a LaunchAgent (Mac) or an at-logon scheduled task (Windows) runs. It starts `res.py dispatch` and restarts it within a second whenever `res.py` or `probes.py` change and the new code compiles. It also watches the dispatcher's local heartbeat (`dispatcher.json`), written at the start of every pass of the main loop and before each mail and notification the dispatcher sends. If no new beat comes for `dispatcher_stall_seconds` (600; the keeper counts its checks, not the clock, so a Mac waking from sleep is not a hang), the keeper kills the dispatcher from outside and starts a new one. The runners of leased jobs are detached and go on; the new dispatcher picks them up as after any restart. This watchdog exists because the dispatcher on one machine hung for 11 hours in a thread start. The cause was inferred, not observed: psutil 7.2.2's Windows `open_files()` kills its helper threads with `TerminateThread`, and a thread killed while holding the Windows loader lock leaves a process in which no new thread can ever start, not even the one `subprocess.run` needs for its timeout.

A hang also leaves the stacks of the dispatcher's threads. At its start the dispatcher starts one thread of its own, the stack watch. Once no pass of the main loop has come for a quarter of `dispatcher_stall_seconds` (150 s), the watch writes every thread's stack into `stack-<machine>-<pid>.txt` in the machine-local log folder, and again every 150 s while that lasts; every pass of the main loop empties the file, so a healthy dispatcher never dumps. After a hang the file holds only the dumps written since the last healthy pass, and the keeper names it in the warning it logs when it kills the dispatcher. A dispatcher that leaves for new code deletes its file; housekeeping deletes old ones after `keep_days`. The watch is started only once because once the loader lock is held no new thread can start, while a thread started earlier still dumps (tried with a script that takes the loader lock and never releases it). It dumps with `faulthandler.dump_traceback` while holding the GIL, so the other threads stand still while their frames are read. Its first form, `faulthandler.dump_traceback_later` with `repeat`, dumped every 150 s from a C thread that reads the other threads' frames without the GIL while they run; the Windows dispatchers died of access violations in the traceback code every few hours until it was replaced. The cost of holding the GIL: a hang inside C code that never releases it shows no dump until it does. This works the same on Windows and macOS and needs nothing installed; `py-spy` was set aside because on macOS it needs root and it is not installed everywhere. The dumps show Python frames only, not native ones.

The dispatcher's main loop answers requests, starts orders, meters leases, sends notices and publishes its report. The compliance scan runs in a thread of its own, so a slow scan never delays lease answers.

## Across machines

`--on <Machine>` writes the request or order into the synced `leases/<Machine>/`, where that machine's dispatcher takes it into its local state, answers there and mails the requester's desk. Every synced file has exactly one writer:

- `requests/<id>.json` (a lease request from another machine) and `asks/<id>.<order|extend|release>.<stamp>.json`, written by the requesting machine and deleted by the target's dispatcher once taken;
- `reports/slot-0.json` to `slot-4.json`, the dispatcher's report (capacities, queue, a summary of open and recent leases), written only when its content changes;
- `beats/slot-0.json` to `slot-4.json`, a heartbeat of about 1 KB (time, pid, compliance summary of the last 7 days) every 10 minutes.

Each report or beat overwrites the oldest of its five slots through a temporary name and a rename, and readers take the newest that parses by the `updated` field inside, then by file time, never by name. Until 2026-11-10 each report was a new file and older ones were deleted. Every deletion leaves a record that every Resilio peer keeps and walks for good, and some 80,000 of them, from this service, ClaudeMail and the house supervisors, stalled one machine's sync for 12 days. Another machine's dispatcher counts as reporting while its newest beat is under 25 minutes old.

Paths below a `ResilioSync` folder in a remote order are translated into the target machine's copy. A run order that arrives from another machine is always started under the program rule below, whatever it claims about its origin.

`reports_via` switches reports and beats from the slot files to the house's postman service over Tailscale, the first step of a planned move of all machine-to-machine messages off the synced folder. Requests and asks stay on files for now.

## Safety

A leased program that the dispatcher starts must be one of `allowed_programs` (`python`, `pythonw`, `py`, `powershell`, `pwsh` with `-File`), running a script file that resolves, symlinks followed, under one of the machine's `allowed_roots`. Inline code (`-c`, `-m`, `-Command`) is refused. Every run and every refusal is logged, and a refused remote order is mailed to its owner.

A direct local run is exempt (the user's ruling of 2026-09-27): `res run` on a lease already granted on the session's own machine starts any program, because `res` starts it itself for the session that typed it. Queued local runs and orders from other machines keep the rule in full. The exemption cannot be spoofed: a runner never reads the synced order, only a launch file in the local `launch/` folder written by the local `res run` or by the dispatcher after its check, holding exactly the program that was checked. An order planted in the synced folder is either checked and refused or never started.

## The compliance record

Every `unleased_check_seconds` (5), a thread of the dispatcher scans the process table (well under a tenth of a second warm on a Mac, about 0.1 s on a Windows machine with 460 processes) and places every process of this user by the nearest tool shell on its parent chain. A parent is followed only while it is older than the child, because Windows keeps a dead parent's pid and soon reuses it.

A tool shell is a shell from `tool_host_names` that carries a tool marker (`shell-snapshots` from the Bash tool, `__claudeCodeScript` from the PowerShell tool) itself or, when it runs one command, in a descendant; a shell that runs many commands also counts when it has `CLAUDECODE` in its environment. Counted, each record saying how:

- (a) a per-command tool shell older than `unleased_after_seconds` (120), recorded at the outermost such shell with the command the tool was asked to run and the real processes below it;
- (a') under a persistent tool shell, each direct child older than the threshold;
- (b) with no tool shell on the chain, a process the detector saw inside a tool shell earlier, or on the Macs one in the session of a tool shell that has exited (a background command whose shell is gone);
- (c) with no tool shell and no Claude process above it, `CLAUDECODE=1` in its environment (after `setsid`, a double fork, `Start-Process`, or reparenting).

The record names the session where any process of the tree carries `CLAUDE_CODE_SESSION_ID`, and the desk that ClaudeMail registered for that session, otherwise the desk of the working folder. A candidate that ends between two scans after passing the threshold is recorded when it ends. Excluded: the shells themselves, leased jobs (`LEASERES_LEASE`), anything that is or runs under `res.py`, `mail.py` or `house.py` (`ignore_command_markers`), console helpers, other users' processes, and whatever sits under a Claude process without a tool shell (MCP servers, hooks). Only the topmost qualifying process of a tree is recorded, and open records survive a dispatcher restart. Each record is written once when seen and once when it ends. `res compliance` sums the records with the trials, requests and runs.

Blind spots: on macOS a `setsid` Apple binary started within one scan of its shell's end (its environment is hidden and its session new); and a process a leased job leaves behind inherits `LEASERES_LEASE` and is never counted.

## Files

Only write-once cross-machine files live in the synced folder (a house rule since 2026-09-28: nothing a program keeps rewriting may live there, since every rewrite is a chance for a conflict copy).

- **Synced code and config:** `res.py`, `probes.py`, the shims, `tools/`, `config/defaults.json` overlaid by `config/<machine>.json` (only that machine edits it), `tests/`.
- **Synced traffic** under `leases/<Machine>/`: requests, asks, report slots and beat slots, as above.
- **Machine-local state** (`~/Library/Application Support/LeaseResources/` on the Macs, `%LOCALAPPDATA%\LeaseResources\state\` on Windows, `$XDG_STATE_HOME/LeaseResources/` elsewhere): lease records, orders, runner records, asks, `done/`, `history.json`, the compliance record `unleased.jsonl`, `trials/`, `launch/`, `attached/`, `notices/`, `learned_peaks.json`, `disks.json`, `dispatcher.json` (heartbeat and lock), `outbox/`.
- **Machine-local logs** (`~/Library/Logs/LeaseResources/` on the Macs, `%LOCALAPPDATA%\LeaseResources\logs\` on Windows): the daily `leaseresources.<machine>.<yyMMdd>.log`, job logs `leases/<id>.log`, diagnostic snapshots, a dispatcher's stack dumps `stack-<machine>-<pid>.txt`, and the LaunchAgent's `launchd.log`.

The tests point `LEASERES_DATA`, `LEASERES_LOCAL` and `LEASERES_LOGS` at their scratch folder. Each run's folder is named `leaseres-tests-<HHMMSS>-<host>-<pid>-<create time>` in a single `mkdir`, so the owner is known from the moment the folder exists; a starting run deletes a leftover only when it was made on the same host and no process with that pid and create time runs there any more, killing whatever still runs from it first, and leaves every other folder alone, so runs on one machine can overlap.

**Size limits.** A job's log rotates at 50 MB; a trial keeps at most 10 MB of output; the compliance record rotates at 5 MB; a day's log stops at 20 MB; logs, snapshots, stack files, trials and ended leases go after `keep_days` (30); a stack file is emptied on every healthy pass and written only while a pass is overdue, a few KB every 150 s, until the keeper kills the dispatcher; ended leases move to `done/` after a week; the duration history keeps 20 runs of at most 500 commands; the Mac's `launchd.log` is emptied past 10 MB.

## Known limits

- Windows has no equivalent of POSIX session scanning, so a trial can miss a process that detaches and whose parent exits within one sample (0.25 s).
- Disk attribution by open files misses a file opened, used and closed between two samples, and `open_files()` holds Python's GIL for its whole call (0.04 to 0.4 s per process on Windows), which can delay a trial's kill by that much.
- A lease names a disk by its device number, and on Windows a reboot can renumber disks.
- On the Macs, psutil 7.1 and later shift every create time they report by however far the clock has been corrected since psutil was imported. LeaseResources undoes that through a private psutil function for the checks that decide whether a runner or waiter still lives, and keys the compliance scan by the kernel's time.
- On the Macs whatever the dispatcher starts runs throttled, because the LaunchAgent's `ProcessType Background` passes to every descendant: 2.5 to 6 times slower in measurements. A local `res run` is not affected. Whether to change the type is an open decision.
- Some duplicates are kept on purpose: a waiter killed between printing and deleting a notice, or a dispatcher down at the job's end, can lead to a notice printed and also mailed. An orphaned waiter whose output nobody reads swallows the lease's notices.
- The dispatcher meters only processes started by `res run`. A service can hold a lease with `res request`, but its use counts as unleased.

## Designed, not built: scheduled Claude work

A file per machine, `schedules/<Machine>.json`, would list jobs with a desk, times, weekdays, a precheck script and a brief. At a due time the dispatcher runs the precheck under the program rule and the trial's 10-second kill. Exit 0 with no output means nothing to do. Exit 0 with output means work: the dispatcher mails the desk the brief with the precheck's output appended and a `schedule:<name>:<date>` conversation id, and watches for the reply. No reply within the expected time means a desktop notification; missed times while the machine slept run once at the next start. The project's own visible session does the work, holding leases like anyone else.
