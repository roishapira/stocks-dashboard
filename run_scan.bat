@echo off
echo ===================================
echo  DiCarlo BX Scanner - Running Scan
echo ===================================
cd /d "%~dp0"
python scanner.py
echo.
echo Scan complete. Press any key to close.
pause >nul
