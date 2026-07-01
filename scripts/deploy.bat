@echo off
setlocal enabledelayedexpansion

echo ========================================
echo   moon-qqbot One-Click Setup
echo ========================================
echo.

cd /d "%~dp0"
cd ..
set PROJECT_DIR=%CD%

REM -- Check Python --
echo [Check] Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+
    pause
    exit /b 1
)
python -c "import sys; print(f'  Python {sys.version_info.major}.{sys.version_info.minor}')"
echo.

REM -- Check Git --
echo [Check] Git...
git --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git not found. Install Git first.
    pause
    exit /b 1
)
echo   Git OK
echo.

REM -- QQ number --
if not exist ".env" (
    set /p QQ_NUMBER="Enter Bot QQ number: "
    if "!QQ_NUMBER!"=="" (
        echo [ERROR] QQ number is required
        pause
        exit /b 1
    )
    echo QQ=!QQ_NUMBER!> .env
    echo BOT_QQ_MAIN=!QQ_NUMBER!>> .env
    echo   QQ number saved
) else (
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        if "%%a"=="BOT_QQ_MAIN" set QQ_NUMBER=%%b
    )
    echo   .env exists, QQ: !QQ_NUMBER!
)
echo.

REM -- Create venv --
echo [1/5] Python virtual environment...
if not exist ".venv" (
    python -m venv .venv
    echo   venv created
    set VENV_NEW=1
) else (
    echo   venv exists, skipped
    set VENV_NEW=0
)
call .venv\Scripts\activate.bat
python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple >nul 2>&1
if "!VENV_NEW!"=="1" (
    echo   Installing dependencies...
    python -m pip install -r requirements.txt
    echo   Done
) else (
    echo   Dependencies skipped (venv exists)
)
echo.

REM -- Install AstrBot --
echo [2/5] AstrBot framework...
if not exist "AstrBot" (
    git clone https://github.com/AstrBotDevs/AstrBot.git
    if errorlevel 1 (
        echo   [ERROR] Clone failed. Check network.
        pause
        exit /b 1
    )
    echo   AstrBot cloned
) else (
    echo   AstrBot exists, skipped
)
cd AstrBot
echo   Installing/updating AstrBot dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo   [ERROR] AstrBot dependency install failed
    pause
    exit /b 1
)
echo   AstrBot dependencies OK
cd ..
echo.

REM -- Deploy plugins --
echo [3/5] Deploy plugins...
if not exist "AstrBot\data\plugins" mkdir "AstrBot\data\plugins"
set PLUGIN_COUNT=0
set PLUGIN_SKIP=0
for /d %%i in (astrbot_plugin_*) do (
    if exist "AstrBot\data\plugins\%%i" (
        set /a PLUGIN_SKIP+=1
    ) else (
        mklink /J "AstrBot\data\plugins\%%i" "%%i" >nul 2>&1
        if errorlevel 1 (
            xcopy "%%i" "AstrBot\data\plugins\%%i\" /E /I /Y /Q >nul
        )
        set /a PLUGIN_COUNT+=1
        echo   %%i
    )
)
if !PLUGIN_COUNT! gtr 0 (
    echo   Deployed !PLUGIN_COUNT! plugins
)
if !PLUGIN_SKIP! gtr 0 (
    echo   Skipped !PLUGIN_SKIP! (already exist)
)
echo.

REM -- Copy character cards --
echo [4/5] Character cards...
set CHAR_DST=AstrBot\data\plugins\astrbot_plugin_suli_tavern\characters
if not exist "%CHAR_DST%" mkdir "%CHAR_DST%"
copy "characters\*.json" "%CHAR_DST%\" /Y >nul 2>&1
echo   Character cards ready
echo.

REM -- NapCatQQ --
echo [5/5] NapCatQQ (QQ protocol) ...

set NAPCAT_DIR=%PROJECT_DIR%\NapCatQQ
set NAPCAT_CONFIG=%NAPCAT_DIR%\config
set NAPCAT_LAUNCHER=%NAPCAT_DIR%\launcher.bat

if exist "%NAPCAT_LAUNCHER%" (
    echo   NapCatQQ already installed, skipped
) else (
    echo   Downloading NapCatQQ...
    if not exist "%NAPCAT_DIR%" mkdir "%NAPCAT_DIR%"

    powershell -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\dl_napcat.ps1" -OutDir "%NAPCAT_DIR%"

    if errorlevel 1 (
        echo   [WARNING] Auto-download failed. Manual install:
        echo   https://github.com/NapNeko/NapCatQQ/releases
        echo   Download NapCat.Shell.Windows.Node.zip and extract to: %NAPCAT_DIR%
    ) else (
        for /r "%NAPCAT_DIR%" %%f in (launcher.bat) do (
            if not exist "%NAPCAT_LAUNCHER%" (
                move "%%f" "%NAPCAT_LAUNCHER%" >nul 2>&1
            )
        )
        if exist "%NAPCAT_LAUNCHER%" (
            echo   NapCatQQ ready
        ) else (
            echo   [WARNING] NapCatQQ extracted but launcher.bat not found
            echo   Check: %NAPCAT_DIR%
        )
    )
)

REM Configure NapCat OneBot WebSocket -> AstrBot
if not exist "%NAPCAT_CONFIG%" mkdir "%NAPCAT_CONFIG%"
set ONEBOT_CONFIG=%NAPCAT_CONFIG%\onebot11.json
if not exist "%ONEBOT_CONFIG%" (
    (
    echo {
    echo     "network": {
    echo         "websocketClients": [
    echo             {
    echo                 "name": "AstrBot",
    echo                 "url": "ws://localhost:6199/ws",
    echo                 "messagePostFormat": "array",
    echo                 "reportSelfMessage": false,
    echo                 "reconnectInterval": 3000
    echo             }
    echo         ]
    echo     },
    echo     "webui": {
    echo         "port": 6099,
    echo         "token": ""
    echo     }
    echo }
    ) > "%ONEBOT_CONFIG%"
    echo   NapCat OneBot config generated
) else (
    echo   NapCat config exists, skipped
)
echo.

REM -- Done --
echo ========================================
echo   Setup Complete!
echo ========================================
echo.
echo Next steps:
echo   1. Launch: scripts\start.bat
echo   2. NapCat scan QR to login QQ !QQ_NUMBER! (http://localhost:6099)
echo   3. Admin panel: http://localhost:5190
echo   4. Configure LLM API in panel (OpenAI compatible)
echo.
pause
