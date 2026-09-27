# Removes the scheduled task 'LeaseResources Dispatcher'. leases\ and logs\ are left alone; running leased jobs keep running.
$ErrorActionPreference = "Stop"
$TaskName = "LeaseResources Dispatcher"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Removed scheduled task '$TaskName'."
