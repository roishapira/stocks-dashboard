@echo off
REM ============================================================
REM  Make the daily scan actually run.
REM
REM  The task created by setup_scheduled_task.bat used schtasks
REM  defaults, which meant: no wake from sleep, no catch-up on a
REM  missed run, and a hard refusal to start on battery. Result:
REM  the scan fired on well under half the trading days.
REM
REM  This applies the four settings schtasks cannot set.
REM  Safe to re-run.
REM ============================================================
setlocal
set TASK=DiCarlo BX Scanner

echo Updating scheduled task "%TASK%"...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "try {" ^
  "  $t = Get-ScheduledTask -TaskName '%TASK%';" ^
  "  $t.Settings.WakeToRun = $true;" ^
  "  $t.Settings.StartWhenAvailable = $true;" ^
  "  $t.Settings.DisallowStartIfOnBatteries = $false;" ^
  "  $t.Settings.StopIfGoingOnBatteries = $false;" ^
  "  $t.Settings.ExecutionTimeLimit = 'PT2H';" ^
  "  $t.Settings.RestartCount = 2;" ^
  "  $t.Settings.RestartInterval = 'PT15M';" ^
  "  Set-ScheduledTask -InputObject $t | Out-Null;" ^
  "  $s = (Get-ScheduledTask -TaskName '%TASK%').Settings;" ^
  "  Write-Host '  WakeToRun                 :' $s.WakeToRun;" ^
  "  Write-Host '  StartWhenAvailable        :' $s.StartWhenAvailable;" ^
  "  Write-Host '  DisallowStartIfOnBatteries:' $s.DisallowStartIfOnBatteries;" ^
  "  Write-Host '  StopIfGoingOnBatteries    :' $s.StopIfGoingOnBatteries;" ^
  "  Write-Host '  ExecutionTimeLimit        :' $s.ExecutionTimeLimit;" ^
  "  Write-Host '  RestartCount / Interval   :' $s.RestartCount '/' $s.RestartInterval;" ^
  "} catch { Write-Host ('ERROR: ' + $_.Exception.Message); exit 1 }"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Failed. Try running this file as Administrator.
    pause
    exit /b 1
)

echo.
echo Done. The scan now wakes the PC at 07:00, catches up if a run was
echo missed, runs on battery, and retries twice on failure.
echo.
echo Note: the task is still "Interactive only" - it needs Roi to be
echo logged on (a locked screen is fine, a shut-down PC is not).
echo To change that: Task Scheduler ^> Properties ^> "Run whether user is
echo logged on or not" - it will ask for the Windows password.
echo.
pause
