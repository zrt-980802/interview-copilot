@echo off
setlocal
if /i "%~1"=="--test" exit /b 0
if defined TEST exit /b 0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch.ps1" -Mode Desktop
if errorlevel 1 pause
endlocal
