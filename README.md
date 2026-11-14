# LeaseResources

LeaseResources lets Claude Code sessions run long jobs without stepping on each other. Before a session starts anything that might take a while, it runs the command for at most ten seconds under a meter. Most commands finish inside that trial and need nothing more. For the rest, the session asks its machine's dispatcher for a lease: the trial's measured peaks plus a margin, for a stated duration. The dispatcher grants the lease, queues it or refuses it by one rule that treats every resource alike. It starts the job detached from the session, so the job survives an interrupted turn or an app restart. It watches the job against what it declared, and tells the session when the job ends. A session can lease resources on another machine of the house the same way.

It runs in one household on eight machines (four Windows PCs, four Macs), carried by a folder that Resilio Sync keeps in step between them. It is Python with one dependency, [psutil](https://github.com/giampaolo/psutil): `res.py` (about 3,700 lines: the command-line tool, the dispatcher, its keeper and the job runner) and `probes.py` (about 760 lines: the resource probes and the meter), plus small installers for macOS and Windows.

> **Status: personal infrastructure, published as it runs.** LeaseResources works every day, but it was built for one house. It expects to live at `~/ResilioSync/Services/LeaseResources`, with its sibling [ClaudeMail](https://github.com/goocy/ClaudeMail) at `~/ResilioSync/Services/ClaudeMail` for the notices it mails, and it reads how Claude Code's shells look from the inside (tool-shell markers, environment variables), which no Claude Code release promises to keep. Read it as a worked example and a starting point, not as a supported product.

> **Claude Code CLI or Desktop.** `res` itself works from any Claude Code session that runs shell commands, and the waiter (`res run --wait` as a background command) brings notices without any mail. The fallback notices by mail go through ClaudeMail, which delivers into sessions of the Claude Code desktop app only. Without ClaudeMail, give every `res request` a `--desk Machine:Name` of your choosing (the owner desk is otherwise asked from ClaudeMail, and the request stops without it); mail notices then fail, and a desktop notification takes their place.

## Contents

- [Why it exists](#why-it-exists)
- [How it works](#how-it-works)
- [For Claude sessions: how to use it](#for-claude-sessions-how-to-use-it)
- [Commands](#commands)
- [Resources as dimensions](#resources-as-dimensions)
- [Across machines](#across-machines)
- [Safety](#safety)
- [The compliance record](#the-compliance-record)
- [Setup](#setup)
- [Configuration](#configuration)
- [Tests](#tests)
- [Limits and caveats](#limits-and-caveats)
- [Repository layout](#repository-layout)
- [History](#history)
- [License](#license)

## Why it exists

The user ran Claude Code sessions on several machines at once, and three things kept going wrong. A long job died with the session that started it. A session that waited for a job spent turn after turn polling it with `sleep`. And sessions on one machine fought over the same disk, GPU or cores with nobody keeping count. Three bottlenecks had been seen so far: a spinning archive disk, the GPUs of two machines, and CPU.

On 2026-09-27 he proposed the answer that this repository implements: "we give agents a durable tool to supervise how many computing resources they're currently using, mandate a trial run that's guaranteed to be concluded in <10 seconds, and if they want to launch anything longer, they have to request longer-duration resource use from a resource dispatcher that runs on each machine." He ruled out designing around the three known bottlenecks, since the next one (RAM, the network, another disk) would only take their place. So nothing in LeaseResources knows which resource is scarce: every resource is a dimension with a capacity and a measured use, and every decision is made the same way on every dimension. A new bottleneck is one new probe.

He also chose, that same day, to make the mandate guidance only. Nothing caps or refuses a shell command. Instead the dispatcher counts how often sessions trial and lease, and how often they run long commands without a lease, so that he can decide later whether a hard ten-second cap is worth its friction. That record is described [below](#the-compliance-record).

## How it works

<p align="center"><img src="docs/images/how-it-works.svg" alt="A Claude session runs res trial, which runs the command for at most 10 seconds under the meter and saves its profile (1). If it was cut off, res request asks the machine's dispatcher for a lease from that profile (2); the dispatcher admits, queues or refuses it by one rule on every dimension (3). res run --wait starts the job through a detached runner (4) and waits on the lease; the dispatcher meters the job every 5 seconds against what it declared (5) and writes its notices to the waiter, whose exit wakes the session (6). With no waiter alive, notices go by ClaudeMail to the session's desk." width="100%"></p>

1. **Trial.** `res trial -- <command>` runs the command under the meter and kills its whole process tree at 10 seconds (on Windows a process that detaches within a quarter of a second can escape; see [docs/DESIGN.md](docs/DESIGN.md)). It prints whether the command finished, the tail of its output and a profile: peak and mean on every dimension. A command that finished needed no lease (exit code 0). One that was cut off (exit code 3) needs a lease, and its profile is saved under an id.
2. **Request.** `res request --profile <trial id> --for 2h --why "..."` asks this machine's dispatcher for the trial's peaks plus a margin (25 %, never less than a small floor per kind), for two hours. The answer comes within a second or two.
3. **Admission.** The dispatcher grants the lease when, on every dimension, the unleased use measured now, plus the needs of the leases already granted, plus the request, stays under the capacity times a headroom (0.85), with every request still queued ahead of it counted as if granted. Otherwise the request queues. So a small request can overtake a queue head that waits for a busy disk, but it never takes anything the head needs. A lease still queued after 30 minutes gets one notice saying what blocks it. A request larger than the machine could ever allow, or for a kind of resource the machine lacks, is refused with the dimension named.
4. **Run.** `res run --lease <id> --wait -- python job.py` starts the job through a short-lived intermediate, so the job has no live ancestor among the session's processes and survives the turn being interrupted, the session closing and the app restarting. Its output goes to a log of its own.
5. **Watching.** Every 5 seconds the dispatcher meters each running lease's process tree. A job that uses more than it declared, runs past its duration or writes nothing to its log for 30 minutes gets one notice each. The dispatcher never kills a leased job: the owner renegotiates with `res extend` or stops the job with `res release --kill`.
6. **Notices.** `res run --wait` registers itself as the lease's waiter, streams the job's log and prints the dispatcher's notices. It exits with the job's exit code when the job ends, or with 75 when the job is still running but has overrun, run past its duration or stalled. A session that started it as a background command is woken by that exit with the notice in the output, so it spends no turns polling. With no waiter alive (a job started without `--wait`, or on another machine), each notice goes by [ClaudeMail](https://github.com/goocy/ClaudeMail) to the session that requested the lease.

Each finished lease also adds its command, run time, exit code and peaks to a history, so the next request for the same command is told the earlier run times, and a stated duration far from them is flagged.

## For Claude sessions: how to use it

This is the paragraph the house guidance gives every session, lightly adapted:

- Before starting anything that may run longer than about ten seconds, run it once as `res trial -- <command>`. Most commands finish inside the trial and need nothing more.
- If it is cut off, ask for a lease with `res request --profile <trial id> --for <duration> --why "..."` (add `--on <Machine>` for another machine).
- Start the job with `res run --lease <id> --wait -- python <script>` as a background command (in Claude Code: Bash with `run_in_background`), then end the turn instead of polling with `sleep`. The job runs detached with a log and survives interrupts and restarts, and the command's exit wakes the session with the dispatcher's notice in its output.
- The command exits with the job's exit code when the job has ended, and with 75 when the job is still running but stalls, overruns or runs past its duration. After a 75, re-attach with `res wait <id>`, again as a background command.
- `res now` shows what the machine has free and `res mine` what this session uses right now.
- `--for` in `res extend` sets the lease's new total duration, counted from the job's start; it does not add to the old one.

A trial looks like this:

```
$ res trial -- python3 tests/live_job.py 3
Finished in 3.3 s with exit code 0: no lease needed.
Output tail (~/Library/Application Support/LeaseResources/trials/T261113-050521969-Altiplano.out):
  | live tick 0
  | live tick 1
  | live tick 2
  | live job done
Profile T261113-050521969-Altiplano (peak / mean):
  cpu         0.52 cores / 0.04 cores
  ram         14.7 MB / 13.5 MB
  disk:disk0  0 B/s / 0 B/s
  net:en0     507.2 KB/s / 79.1 KB/s  (machine delta, not per process)
```

and `res now` on a busy Windows machine like this:

```
> python res.py now
Stahlwerk: dispatcher up (pid 20248), headroom 0.85, 1 active and 1 queued leases
dimension            capacity     use now     leased (planned)  unleased    free        label
cpu                  12.00 cores  8.54 cores  1.20 cores        7.78 cores  1.22 cores  12 logical cores
ram                  68.6 GB      41.3 GB     3.0 GB            41.2 GB     14.1 GB     physical memory
disk:PhysicalDrive0  165.9 MB/s   0 B/s       0 B/s             0 B/s       141.0 MB/s  D:
disk:PhysicalDrive1  474.3 MB/s   10.7 MB/s   150.0 MB/s        10.5 MB/s   242.6 MB/s  E:
disk:PhysicalDrive2  1.3 GB/s     1.2 MB/s    0 B/s             1.2 MB/s    1.1 GB/s    C:
gpu:0                100 %        17 %        0 %               17 %        68 %        NVIDIA GeForce RTX 2060 SUPER
vram:0               8.6 GB       719.3 MB    0 B               719.3 MB    6.6 GB      NVIDIA GeForce RTX 2060 SUPER memory
net:Ethernet         125.0 MB/s   17.3 MB/s   0 B/s             17.3 MB/s   89.0 MB/s   1000 Mbit/s link
```

Here a running lease has planned 1.2 cores, 3 GB and 150 MB/s on E:, and only 1.22 cores are free under the headroom, so a request for two cores would queue until something ends.

## Commands

On a Mac run `python3 res.py <command>` or the shim `res`; on Windows `python res.py <command>` or `res.cmd`. Any interpreter works: `res.py` re-launches itself in its venv.

| Command | What it does |
|---|---|
| `now [--json]` | Every dimension of this machine: capacity, use now, what granted leases have planned, the unleased rest, and what is free under the headroom. |
| `mine` | What this Claude session's process tree uses now, found through the parents of the calling shell, process by process. |
| `trial [--json] -- COMMAND...` | Runs the command for at most `trial_seconds` (10) under the meter, kills its whole tree, prints the outcome, the output tail and the profile. Exit 0: finished, no lease needed; exit 3: cut off. |
| `request (--profile TRIAL \| --need DIM=AMOUNT,...) --for DURATION [--on MACHINE] [--why TEXT] [--desk M:D] [--margin X]` | Asks a dispatcher for a lease. `--need cpu=2,ram=4G,gpu:0=60,vram:0=6G,disk:G=80M` states amounts instead of a trial; a disk may be named by its mount or drive letter. `--margin X` is the fraction added to the trial's peaks in place of the default 0.25 (1.0 asks for twice the peaks). Answers granted, queued (with position and the dimension it waits for) or refused. |
| `run --lease ID [--on MACHINE] [--wait] [--cwd DIR] -- PROGRAM...` | Starts a leased job detached, with a log. On a queued lease, or with `--on`, it leaves a run order that the dispatcher starts on the grant. `--wait` (local leases only) streams the log and the notices and exits as described above. |
| `wait ID` | Attaches to a local lease that has no live waiter, prints its log from now on and its notices, and exits as `run --wait` does. One waiter per lease. |
| `extend ID [--for DURATION] [--profile TRIAL \| --need ...] [--on MACHINE]` | More time is always granted; more resources only when they fit now. `--need` changes only the dimensions it names, and only a raise is tested; what the lease already holds is never tested again. `--for` is the new total from the job's start. |
| `release ID [--kill] [--on MACHINE]` | Gives back a queued or unstarted lease; `--kill` stops a running job's whole tree, the only way anything leased is ever killed. |
| `leases [--on MACHINE \| --all] [--ended]` | The leases on a machine, or on all of them. |
| `status [ID] [--on MACHINE]` | One lease with its log tail, or the dispatcher with its compliance summary. |
| `compliance [--days N] [--list N] [--on MACHINE]` | Trials, requests by outcome, leased runs, and long commands of Claude sessions that ran without a lease. |
| `notify-test [--title T] [--body B]` | Shows one desktop notification the way the dispatcher would. |
| `watch`, `dispatch` | The service: `watch` is the keeper that the LaunchAgent or scheduled task runs, `dispatch` the dispatcher it keeps alive. |

Durations are written like `90s`, `30m`, `6h` or `2d`.

## Resources as dimensions

Every resource is a named dimension with a capacity and a measured use. `probes.py` holds one class per kind; adding a dimension is one new `Probe` subclass appended to `PROBES`.

| Dimension | Capacity | Machine use | Per process tree |
|---|---|---|---|
| `cpu` | logical cores | busy share of all cores | CPU seconds per second |
| `ram` | physical memory | total minus available | resident bytes |
| `disk:<physical disk>` (`disk0`, `PhysicalDrive1`), labelled with its mounts or drive letters | `disk_capacity` from the config, else the highest rate ever seen, at least 150 MB/s | bytes per second read and written | each process's I/O, put on the disks it has files open on and capped at what each disk physically moved |
| `gpu:<n>`, `vram:<n>` | 100 %, the card's memory | `nvidia-smi` | only the tree's own compute processes on the card |
| `net:<interface>` | the link speed, else the highest rate seen | bytes per second in the busier direction | not measurable per process: a trial counts the machine's rise over a baseline, and a running lease counts at its declared amount |
| anything in `file_dimensions` | as configured | a JSON file someone else writes | from that file (the tests simulate a GPU this way) |

Disks are mapped to physical devices with `diskutil` on the Mac and `Get-Partition` on Windows; the map is cached and rebuilt when the disks or mounts change, so a hot-swapped disk should become a dimension within one sample (shown in the tests; no disk has been swapped live since the change). Virtual network adapters (VPNs, Tailscale, Hyper-V and the like, `net_skip`) are left out, since they carry the same traffic as the real link.

"Unleased use" is the machine's measured use minus what the running leases measurably use, averaged over 30 seconds. It is what makes the admission rule work without anyone declaring anything: services that never ask for a lease still count, because they are measured.

## Across machines

`--on Mistral` writes the request or run order into the synced folder `leases/Mistral/`, where Mistral's dispatcher takes it, answers there and mails the requester. Paths below a `ResilioSync` folder are translated into the target's copy, so a Mac session can order `python3 ~/ResilioSync/Projects/X/job.py` on a Windows machine. Each machine's dispatcher publishes a report (capacities, queue, a summary of its leases) when it changes and a small heartbeat every 10 minutes. Each kind is written into five fixed slot files, overwriting the oldest each time, because every file a synced folder deletes leaves a record that every peer keeps for good. Resilio adds seconds to a minute each way.

## Safety

A synced file that can start programs on any machine is a remote-execution channel. So a job that the dispatcher starts from the synced folder, whether a run order from another machine or a queued local run, must be `python`, `pythonw`, `py`, or `powershell`/`pwsh -File`, running a script file that resolves (symlinks followed) under one of the machine's `allowed_roots`. Inline code (`-c`, `-m`, `-Command`) is refused. Every run and every refusal is logged, and a refused remote order is mailed to its owner.

A direct local run is exempt: `res run` on a lease already granted on the session's own machine starts any program (`xcodebuild`, `ffmpeg`, a shell script), because `res` starts it itself for the session that typed it. The exemption cannot be spoofed from the synced folder: a runner never reads the synced order, only a launch file in a local folder that is never synced, written by the local `res run` or by the dispatcher after its check, with exactly the program it checked. `check_local_runs: true` restores the rule for local runs too.

## The compliance record

Because the mandate is guidance only, the dispatcher measures it. A thread of its own scans the process table every 5 seconds and records long commands of Claude sessions that run without a lease: a Bash or PowerShell tool command older than 120 seconds, a direct child of a persistent tool shell older than that, a background command whose shell has exited, or a process that escaped its parents but still carries Claude Code's environment. It finds Claude Code's tool shells by the markers they carry (`shell-snapshots` from the Bash tool, `__claudeCodeScript` from the PowerShell tool), names the session where a process carries `CLAUDE_CODE_SESSION_ID`, and leaves out leased jobs, its own tools, console helpers, MCP servers and hooks. `res compliance` sums it per machine and period with the trials, requests and leased runs. `tools/snapshot_tree.py` records the process table around the Claude processes, with what the detector makes of it, for checking a new shape of shell; `tools/scan_timing.py` measures where a scan spends its time.

## Setup

Per machine:

1. Create the venv (never the system Python): on a Mac `python3 -m venv ~/venvs/LeaseResources && ~/venvs/LeaseResources/bin/pip install psutil`; on Windows `python -m venv %USERPROFILE%\venvs\LeaseResources` and then `%USERPROFILE%\venvs\LeaseResources\Scripts\python.exe -m pip install psutil`. `res.py` and both installers stop with these instructions when it is missing.
2. Add the machine's name to `machines` in `config/defaults.json` (it must match the computer's name, or map it in `hostnames`).
3. Mac: `./install_launchd.sh` loads the LaunchAgent `com.goocy.leaseresources` (RunAtLoad, KeepAlive), which runs `res.py watch` in the venv; `./install_launchd.sh uninstall` removes it.
4. Windows: `powershell -ExecutionPolicy Bypass -File install_task.ps1` registers and starts the at-logon task `LeaseResources Dispatcher` (the venv's `pythonw.exe -B res.py watch`); `uninstall_task.ps1` removes it.
5. Optionally, `config/<machine>.json` (lower case) overrides any setting for that machine, for example `disk_capacity` for a fast SSD or extra `allowed_roots`.
6. Check with `res status` and `res now`.

The keeper restarts the dispatcher within a second whenever `res.py` or `probes.py` change and the new code compiles, so a code update arriving through the synced folder needs no re-run; only the keeper itself takes up new code at its next logon or task restart. The keeper also kills and restarts a dispatcher that writes no heartbeat for 10 minutes. Logs and state are machine-local (`~/Library/Logs/LeaseResources` and `~/Library/Application Support/LeaseResources` on the Macs, `%LOCALAPPDATA%\LeaseResources` on Windows); only write-once cross-machine files live in the synced folder.

## Configuration

All settings are in `config/defaults.json`, with units in its `_about` (cpu in cores, ram and vram in bytes, disk and net in bytes per second, gpu in percent). `config/<machine>.json` overrides any of them for one machine. The ones worth knowing:

| Key | Meaning (default) |
|---|---|
| `machines`, `hostnames` | The machines of the house, and computer names that differ from them. |
| `headroom`, `headroom_by_dimension` | The share of each capacity that leases plus unleased use may fill (0.85), overridable per dimension. |
| `margin`, `request_floor` | What a request adds to a trial's peaks (25 %), and the least it asks per kind. |
| `overrun_margin`, `overrun_floor`, `overrun_samples` | When a running lease counts as using more than it declared: by more than 10 % and the floor, for 3 samples in a row. |
| `trial_seconds` | The trial's hard limit (10). |
| `sample_seconds`, `smoothing_seconds` | How often running leases are metered (5) and over how long unleased use is averaged (30). |
| `stall_minutes`, `start_within_minutes` | No log growth for this long is a stall (30); a granted lease never started expires after this (60). |
| `dispatcher_stall_seconds` | How long the keeper waits for a heartbeat before it restarts the dispatcher (600); the dispatcher writes stack dumps every quarter of it (150 s), kept only while it hangs. |
| `allowed_programs`, `allowed_roots`, `check_local_runs` | The program rule for jobs the dispatcher starts (see Safety). |
| `disk_capacity`, `net_capacity`, `disk_skip_mounts`, `net_skip`, `file_dimensions` | Capacities by device, mount, drive letter or interface; what not to treat as a dimension; dimensions read from files. |
| `unleased_after_seconds`, `tool_shell_markers`, `tool_host_names`, `ignore_command_markers` | How the compliance record finds long unleased commands (120 s). |
| `keep_days`, `lease_log_max_bytes`, `trial_output_max_bytes` | Retention (30 days) and size limits (50 MB per job log, 10 MB per trial's output). |
| `mail_script`, `notify` | Where ClaudeMail's `mail.py` is (empty: the sibling folder), and whether to show desktop notifications. |
| `reports_via` | `files` (the slot files) or `postman` (the house's Tailscale postman, for a planned move off the synced folder). No default: the key must be present. |

Nothing the service writes grows without bound: job logs rotate, trials keep at most 10 MB of output, the compliance record rotates at 5 MB, a day's log stops at 20 MB, and logs, trials and ended leases are deleted after `keep_days`.

## Tests

`~/venvs/LeaseResources/bin/python -B tests/test_res.py` runs the sandbox suite: real `res.py` subprocesses and dispatchers in a scratch folder, with a ClaudeMail stub that records mail instead of sending it. It covers the trial kill of a process tree (children, a grandchild, a `setsid` child, an orphaned new-session grandchild), peaks on every kind of dimension (with a simulated GPU), the queue and its order, overrun, stall and duration notices without a kill, jobs surviving SIGINT, SIGHUP and SIGKILL of the group that started them and a dispatcher restart, two machines exchanging requests, the program rule with planted orders, the duration history, the compliance record, the keeper's restart on new code, the waiter and its exit codes, output in a strict cp1252 console, the disk map under hot-swaps and failures, and the quieter report publishing. The suite is POSIX only in parts; its Windows coverage comes from live batteries run on the Windows machines. Its scratch folder (`LEASERES_TEST_SCRATCH`) is a local cache folder, never `%TEMP%` (a small RAM disk on the house's Windows machines) and never the synced tree; the suite refuses to start with less than 4 GB free there, kills what it started and deletes the tree at the end. The network part of the trial test sends 3 seconds of UDP to another machine of the house; in this repository that address is a placeholder (192.0.2.2), so set it to a machine on your network or expect the note that it cannot be reached. One check currently fails on some machines: `T1TrialKill.test_runaway_tree` measures no disk traffic for its runaway tree and fails its disk-peak assertion, on the published code and on the code before it alike (on an M5 MacBook Air in 2 of 3 runs or more, and earlier in 6 of 6 runs on a Windows machine). The cause is not known yet; the other 48 tests pass.

`tests/report_clock.py` checks that a time-zone change cannot make a new report look older than an old one. `tests/test_reports_via.py` checks the `reports_via` switch and needs the house's Postman service beside this folder. `tests/live_job.py` is a harmless job for live checks.

Most changes in the history were run against a control copy of the previous code, where each new check had to fail.

## Limits and caveats

- **Trials can miss later phases.** A 10-second profile does not see a model loading after a minute or a second pass over a disk. That is why the dispatcher watches against the declared needs and reports overruns instead of trusting the trial.
- **Disk attribution is approximate**, above all on Windows, where psutil counts all of a process's I/O (page cache, network and pipes too). Capping each disk's share at what it physically moved keeps the error bounded, but a job can be given part of another program's bytes on the same disk.
- **Network use is not per process.** A trial counts the machine's rise over a baseline, so another program's download during a trial is counted against the trial.
- **macOS throttles what the dispatcher starts.** The LaunchAgent runs as `ProcessType Background`, which macOS passes on to every descendant, so a remote order or a run that waited in the queue runs 2.5 to 6 times slower on a Mac than the same job started by a session. A local `res run` on a granted lease is not affected.
- **A Windows dispatcher can wedge for good.** One hung for 11 hours in a thread start. The likely cause, inferred rather than observed, is psutil 7.2.2's Windows `open_files()` killing its helper threads with `TerminateThread`, which can leave the loader lock held so that no new thread can start. The keeper's heartbeat watchdog exists because of this. Since 2026-11-13 such a hang also leaves the stacks of the dispatcher's threads in `stack-<machine>-<pid>.txt` in the local log folder (Python's `faulthandler`, armed once at start so that no new thread is needed at hang time), and the keeper names that file when it kills the dispatcher. The dumps show Python frames only.
- **On the Macs psutil shifts process create times** by however far the clock has been corrected since it was imported. LeaseResources undoes that through a private psutil function, which a psutil release could remove.
- **Duplicates over losses.** A waiter killed between printing a notice and deleting it, or a dispatcher down when the job ends, can lead to the same notice printed and mailed. Losing one was judged worse.
- **Scheduled Claude work** (a precheck plus mail to a project's desk at fixed times) is designed in `docs/DESIGN.md` but not built.

## Repository layout

| Path | What it is |
|---|---|
| `res.py` | The command-line tool, the dispatcher, the keeper, the detached runner and the compliance scanner. |
| `probes.py` | One probe per kind of resource, the disk map, and the meter that samples process trees. |
| `res`, `res.cmd` | Shims that run `res.py` in its venv on the Macs and on Windows. |
| `install_launchd.sh` | Installs or removes the macOS LaunchAgent. |
| `install_task.ps1`, `uninstall_task.ps1` | Install or remove the Windows scheduled task. |
| `config/defaults.json` | Settings shared by every machine. |
| `tests/` | The sandbox suite and the smaller checks described above. |
| `tools/` | `snapshot_tree.py` and `scan_timing.py`, diagnostics for the compliance scanner. |
| `docs/DESIGN.md` | The design in depth: admission, watching, notices, files, the across-machine traffic. |
| `docs/HISTORY.md` | How LeaseResources got here, and what was learned on the way. |

## History

LeaseResources was designed and built on 2026-09-27 and installed on six machines within a day, five of them by their own Claude sessions; it has changed nearly forty times since. The commits in this repository were reconstructed from the archived session transcripts, the deployment log and the synced folder's archive of replaced files, each dated when its version went live where the deployment log records it, and otherwise by the best evidence (a file's time, the order of events), as those commit messages say. [docs/HISTORY.md](docs/HISTORY.md) tells the story.

## License

[MIT](LICENSE).
