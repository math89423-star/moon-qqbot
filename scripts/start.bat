@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0.."
set PROJECT_DIR=%CD%

if not exist "AstrBot\main.py" (
    echo [ERROR] AstrBot not found. Run scripts\deploy.bat first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

if exist ".env" (
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        set %%a=%%b
    )
)

REM -- Launch NapCatQQ --
set NAPCAT_LAUNCHER=
set NAPCAT_HOME=
for /r "%PROJECT_DIR%\NapCatQQ" %%f in (launcher.bat) do (
    set NAPCAT_LAUNCHER=%%f
    set NAPCAT_HOME=%%~dpf
) 2>nul
if defined NAPCAT_LAUNCHER (
    echo Launching NapCatQQ...
    echo   WebUI: http://localhost:6099
    start "NapCatQQ" cmd /k "cd /d "!NAPCAT_HOME!" && "!NAPCAT_LAUNCHER!""
    echo   Waiting for NapCat init ^(5s^)...
    timeout /t 5 /nobreak >nul
    echo   NapCat started. Scan QR to login QQ !BOT_QQ_MAIN!
    echo.
) else (
    echo [WARNING] NapCatQQ not found. Run scripts\deploy.bat first.
    echo   QQ messages will NOT work!
    echo.
)

set ROOT=%CD%
echo Starting admin panel: http://localhost:5190
start "moon-panel" cmd /k "cd /d "%ROOT%\AstrBot" && "%ROOT%\.venv\Scripts\python.exe" "%ROOT%\astrbot_plugin_suli_tavern\panel_main.py" --port 5190"

cd AstrBot
echo Starting AstrBot...
echo QQ: %BOT_QQ_MAIN%
echo.
python main.py
pause
