@echo off
chcp 65001 >nul

cd /d "%~dp0.."
set PROJECT_DIR=%CD%

if not exist "AstrBot\main.py" (
    echo [错误] 未找到 AstrBot 目录，请先运行 scripts\deploy.bat
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

if exist ".env" (
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        set %%a=%%b
    )
)

REM ── 启动 NapCatWin ──
set NAPCAT_EXE=%PROJECT_DIR%\NapCatWin\NapCatWin.exe
if exist "%NAPCAT_EXE%" (
    echo 正在启动 NapCatWin...
    echo   管理面板: http://localhost:6099
    start "" "%NAPCAT_EXE%"
    echo   等待 NapCat 初始化 ^(5 秒^)...
    timeout /t 5 /nobreak >nul
    echo   NapCat 已启动，请扫码登录 QQ %BOT_QQ_MAIN%
    echo.
) else (
    echo [警告] 未找到 NapCatWin，请先运行 scripts\deploy.bat
    echo   QQ 消息将无法收发！
    echo.
)

set ROOT=%CD%
echo 正在启动管理面板: http://localhost:5190
start "moon-panel" cmd /k "cd /d "%ROOT%\AstrBot" && "%ROOT%\.venv\Scripts\python.exe" "%ROOT%\astrbot_plugin_suli_tavern\panel_main.py" --port 5190"

cd AstrBot
echo 正在启动 AstrBot...
echo QQ: %BOT_QQ_MAIN%
echo.
python main.py
pause
