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
REM Try known paths first, fallback to recursive search
set NAPCAT_HOME=%PROJECT_DIR%\NapCatQQ\napcat
if exist "!NAPCAT_HOME!\launcher.bat" (
    echo Launching NapCatQQ...
    echo   WebUI: http://localhost:6099
    pushd "!NAPCAT_HOME!"
    start "NapCatQQ" launcher.bat !BOT_QQ_MAIN!
    popd
    echo   Waiting for NapCat init ^(5s^)...
    timeout /t 5 /nobreak >nul
    echo   NapCat started. QQ: !BOT_QQ_MAIN!
    echo.
) else if exist "%PROJECT_DIR%\NapCatQQ\launcher.bat" (
    echo Launching NapCatQQ ^(root^)...
    echo   WebUI: http://localhost:6099
    pushd "%PROJECT_DIR%\NapCatQQ"
    start "NapCatQQ" launcher.bat !BOT_QQ_MAIN!
    popd
    echo   Waiting for NapCat init ^(5s^)...
    timeout /t 5 /nobreak >nul
    echo   NapCat started. QQ: !BOT_QQ_MAIN!
    echo.
) else (
    REM fallback: recursive search
    set NAPCAT_FOUND=
    set NAPCAT_DIR2=
    for /r "%PROJECT_DIR%\NapCatQQ" %%f in (launcher.bat) do (
        set NAPCAT_FOUND=%%f
        set NAPCAT_DIR2=%%~dpf
    )
    if defined NAPCAT_FOUND (
        echo Launching NapCatQQ...
        echo   WebUI: http://localhost:6099
        pushd "!NAPCAT_DIR2:~0,-1!"
        start "NapCatQQ" launcher.bat !BOT_QQ_MAIN!
        popd
        echo   Waiting for NapCat init ^(5s^)...
        timeout /t 5 /nobreak >nul
        echo   NapCat started. QQ: !BOT_QQ_MAIN!
        echo.
    ) else (
        echo [WARNING] NapCatQQ not found. Run scripts\deploy.bat first.
        echo   QQ messages will NOT work!
        echo.
    )
)

set ROOT=%CD%
echo Starting admin panel: http://localhost:5190
start "moon-panel" cmd /k "cd /d "%ROOT%" && "%ROOT%\.venv\Scripts\python.exe" "%ROOT%\astrbot_plugin_suli_tavern\panel_main.py" --port 5190"

cd AstrBot
echo Starting AstrBot...
echo QQ: %BOT_QQ_MAIN%
echo.
python main.py
pause
