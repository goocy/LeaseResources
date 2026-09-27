<#
    Sets up the LeaseResources dispatcher on this Windows machine (the counterpart of install_launchd.sh):
      1. checks the venv ~\venvs\LeaseResources with psutil;
      2. registers the Scheduled Task 'LeaseResources Dispatcher' (at logon, hidden, continuous), which runs
         `res.py watch`, the dispatcher's keeper; the dispatcher restarts itself whenever res.py or probes.py change;
      3. starts (or restarts) that task right away.

    The task runs in the interactive session: toasts from any other session are invisible, and the dispatcher must
    see the user's processes (the Claude sessions and their shells).

    Usage:   powershell -ExecutionPolicy Bypass -File .\install_task.ps1
    Remove:  powershell -ExecutionPolicy Bypass -File .\uninstall_task.ps1
#>

$ErrorActionPreference = "Stop"
$TaskName   = "LeaseResources Dispatcher"
$ProjectDir = $PSScriptRoot
$Script     = Join-Path $ProjectDir "res.py"
$User       = "$env:USERDOMAIN\$env:USERNAME"
$Venv       = Join-Path $env:USERPROFILE "venvs\LeaseResources"
$python     = Join-Path $Venv "Scripts\python.exe"
$pythonw    = Join-Path $Venv "Scripts\pythonw.exe"

if (-not (Test-Path $python)) {
    throw "The venv $Venv is missing. Create it with:  python -m venv `"$Venv`"  and then  `"$python`" -m pip install psutil"
}
& $python -c "import psutil"
if ($LASTEXITCODE -ne 0) { throw "psutil is missing from $Venv. Install it with:  `"$python`" -m pip install psutil" }
if (-not (Test-Path $pythonw)) { $pythonw = $python }   # fallback (will show a console)

# Fails loudly if this hostname is not one of the machines in config/defaults.json.
& $python -B $Script status | Out-Null
if ($LASTEXITCODE -ne 0) { throw "This machine is not in config/defaults.json (machines / hostnames)." }

# -B: no __pycache__ in the synced tree.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-B `"$Script`" watch" -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Registered and started scheduled task '$TaskName' (at logon, continuous)." -ForegroundColor Green
Write-Host "Logs:  $ProjectDir\logs\leaseresources.<machine>.<yyMMdd>.log"
Write-Host "Check: python `"$Script`" status"
Write-Host "Note: leased jobs started by the dispatcher (cross-machine and queued runs) are children of this task;"
Write-Host "      they are started with CREATE_BREAKAWAY_FROM_JOB so ending the task should not end them (untested)."
