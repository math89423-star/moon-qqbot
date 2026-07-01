@echo off
chcp 65001 >nul

cd /d "%~dp0.."

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

cd AstrBot
echo 正在启动 AstrBot...
echo QQ: %BOT_QQ_MAIN%
echo 管理面板: http://localhost:6190
echo.
python main.py
pause
