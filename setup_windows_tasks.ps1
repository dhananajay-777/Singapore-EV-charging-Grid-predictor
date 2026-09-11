<#
  Registers the two scheduled tasks on this PC.

  Only needed if you are running on your own machine instead of GitHub.
  If you have set up GitHub Actions, skip this file entirely - running both
  would collect the same hours twice.

  Run once, in PowerShell started as Administrator:

      powershell -ExecutionPolicy Bypass -File .\setup_windows_tasks.ps1

  Hourly : sg_utilisation_logger.py      two API calls, a few seconds
  Daily  : sg_ev_carpark_pipeline.py     both joins, then a workbook rebuild

  WakeToRun is on for the hourly task. Without it the machine sleeps overnight
  and you lose the 02:00-05:00 window, which is exactly the window the
  busy-versus-broken test depends on.
#>

param(
  [string]$ProjectDir = "C:\Users\dj_777\Desktop\Ev charging Project",
  [string]$Python     = "C:\Users\dj_777\AppData\Local\Programs\Python\Python314\python.exe",
  [string]$LtaKey     = ""      # optional: sets the LTA_KEY user variable
)

if (-not (Test-Path $Python))     { throw "Python not found at $Python" }
if (-not (Test-Path $ProjectDir)) { throw "Project folder not found at $ProjectDir" }

# --------------------------------------------------------------- the key
# The scripts no longer contain a key. They read LTA_KEY, then lta_key.txt
# beside the script. One of the two has to exist or the runs will fail.
$keyFile = Join-Path $ProjectDir "lta_key.txt"

if ($LtaKey -ne "") {
  [Environment]::SetEnvironmentVariable("LTA_KEY", $LtaKey, "User")
  Write-Host "LTA_KEY saved as a user environment variable."
}
elseif (-not (Test-Path $keyFile) -and
        -not [Environment]::GetEnvironmentVariable("LTA_KEY", "User") -and
        -not $env:LTA_KEY) {
  throw ("No LTA key found. Either rerun with -LtaKey ""<your key>"", or put " +
         "the key on one line in $keyFile . That filename is in .gitignore.")
}

# --------------------------------------------------------------- hourly
$logger = Join-Path $ProjectDir "sg_utilisation_logger.py"
if (-not (Test-Path $logger)) { throw "sg_utilisation_logger.py not found in $ProjectDir" }

New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir "logs") | Out-Null

# a wrapper, so a failed run leaves something to read rather than a bare exit code
$hourlyCmd = Join-Path $ProjectDir "run_hourly.cmd"
@"
@echo off
cd /d "%~dp0"
"$Python" sg_utilisation_logger.py >> logs\hourly.log 2>&1
"@ | Set-Content -Encoding ASCII $hourlyCmd

$action = New-ScheduledTaskAction -Execute $hourlyCmd -WorkingDirectory $ProjectDir

# start at the next :05, then repeat every hour indefinitely
$start   = (Get-Date).Date.AddHours((Get-Date).Hour + 1).AddMinutes(5)
$trigger = New-ScheduledTaskTrigger -Once -At $start `
           -RepetitionInterval (New-TimeSpan -Hours 1)

$settings = New-ScheduledTaskSettingsSet `
  -WakeToRun `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -RunOnlyIfNetworkAvailable `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName "EV utilisation hourly" -Force `
  -Action $action -Trigger $trigger -Settings $settings `
  -Description "Samples LTA connector status and HDB carpark availability every hour." | Out-Null
Write-Host "Registered: EV utilisation hourly (first run $start)"

# --------------------------------------------------------------- daily
# Two steps in one file: the joins, then the utilisation workbook rebuilt from
# whatever the hourly task has accumulated. The rebuild is allowed to fail on
# day one, when there is not yet enough history to build from.
$dailyCmd = Join-Path $ProjectDir "run_daily.cmd"
@"
@echo off
cd /d "%~dp0"
echo ---- %date% %time% >> logs\daily.log
"$Python" sg_ev_carpark_pipeline.py >> logs\daily.log 2>&1
"$Python" sg_utilisation_logger.py --rebuild-only >> logs\daily.log 2>&1
exit /b 0
"@ | Set-Content -Encoding ASCII $dailyCmd

$dAction  = New-ScheduledTaskAction -Execute $dailyCmd -WorkingDirectory $ProjectDir
$dTrigger = New-ScheduledTaskTrigger -Daily -At 6:10pm
$dSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
  -RunOnlyIfNetworkAvailable -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 45)

Register-ScheduledTask -TaskName "EV workbooks daily" -Force `
  -Action $dAction -Trigger $dTrigger -Settings $dSettings `
  -Description "Rebuilds the inner-join, outer-join and utilisation workbooks." | Out-Null
Write-Host "Registered: EV workbooks daily (18:10)"

# --------------------------------------------------------------- check
Start-ScheduledTask -TaskName "EV utilisation hourly"
Start-Sleep -Seconds 25
Get-ScheduledTask -TaskName "EV*" |
  Get-ScheduledTaskInfo |
  Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime | Format-Table

Write-Host ""
Write-Host "LastTaskResult 0 means it ran cleanly. Anything else: open logs\hourly.log"
Write-Host "Check what landed:  python sg_utilisation_logger.py --status"
Write-Host ""
Write-Host "Windows will still sleep unless you stop it. On mains power:"
Write-Host "  powercfg /change standby-timeout-ac 0"
Write-Host ""
Write-Host "To stop the schedule later:"
Write-Host "  Disable-ScheduledTask -TaskName 'EV utilisation hourly'"
Write-Host "  Disable-ScheduledTask -TaskName 'EV workbooks daily'"
Write-Host "To remove them completely:"
Write-Host "  Unregister-ScheduledTask -TaskName 'EV utilisation hourly' -Confirm:`$false"
Write-Host "  Unregister-ScheduledTask -TaskName 'EV workbooks daily'   -Confirm:`$false"
