@echo off
echo ===================================
echo  Setting up daily scan schedule
echo  (Windows Task Scheduler)
echo ===================================
echo.
echo This will create a scheduled task that runs
echo the scanner every weekday at 7:00 AM (before market open).
echo.
echo Press any key to create the task, or Ctrl+C to cancel.
pause >nul

set SCANNER_PATH=%~dp0scanner.py
set WORK_DIR=%~dp0

schtasks /create /tn "DiCarlo BX Scanner" /tr "python \"%SCANNER_PATH%\"" /sc weekly /d MON,TUE,WED,THU,FRI /st 07:00 /rl HIGHEST /f

if %ERRORLEVEL% EQU 0 (
    echo.
    echo Task created successfully!
    echo Schedule: Mon-Fri at 7:00 AM
    echo.
    echo To modify: Open Task Scheduler and find "DiCarlo BX Scanner"
    echo To remove: schtasks /delete /tn "DiCarlo BX Scanner" /f
) else (
    echo.
    echo Error creating task. Try running as Administrator.
)

echo.
pause
