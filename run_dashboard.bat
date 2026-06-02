@echo off
echo ===================================
echo  DiCarlo BX Scanner - Dashboard
echo  http://localhost:5555
echo ===================================
cd /d "%~dp0"
start http://localhost:5555
python dashboard.py
