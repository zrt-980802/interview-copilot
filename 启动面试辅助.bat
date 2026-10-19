@echo off
title Interview Copilot Launcher
setlocal
set "PROJECT_DIR=%~dp0"
if /i "%~1"=="--test" goto :after_open

echo [1/3] Checking STT service (port 8760)...
curl -s -o nul -m 2 http://127.0.0.1:8760/v1/models >nul 2>&1
if errorlevel 1 (
    if defined INTERVIEW_COPILOT_STT_DIR (
        echo     STT not running. Starting configured local service...
        start "STT-Service" /min cmd /c "cd /d %INTERVIEW_COPILOT_STT_DIR% && venv\Scripts\python.exe stt_server.py"
        ping -n 11 127.0.0.1 >nul
    ) else (
        echo     STT not running. Set INTERVIEW_COPILOT_STT_DIR or start an OpenAI-compatible STT service.
    )
) else (
    echo     STT online.
)

echo [2/3] Checking copilot service (port 8527)...
curl -s -o nul -m 2 http://127.0.0.1:8527/api/state >nul 2>&1
if errorlevel 1 (
    echo     Copilot not running. Starting in background...
    start "InterviewCopilot" /min cmd /c "cd /d %PROJECT_DIR% && venv\Scripts\python.exe server.py"
    ping -n 5 127.0.0.1 >nul
) else (
    echo     Copilot online.
)

echo [3/3] Opening teleprompter window...

:after_open
if defined TEST (
    echo TEST-MODE: parse OK.
    exit /b 0
)
where chrome >nul 2>&1
if %errorlevel%==0 (
    start "" chrome --app=http://127.0.0.1:8527 --window-size=1150,920 --window-position=60,40
) else (
    start "" http://127.0.0.1:8527
)
echo.
echo ============================================
echo   Ready! Teleprompter window opened.
echo   JD/resume loaded. History auto-saved.
echo   (This window can be closed now.)
echo ============================================
endlocal
exit /b 0
