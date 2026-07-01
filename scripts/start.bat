@echo off
chcp 65001 >nul

cd /d "%~dp0"
cd ..
set PROJECT_DIR=%CD%

if not exist "%PROJECT_DIR%\.venv" (
    echo [错误] 未找到虚拟环境，请先运行 deploy.bat
    pause
    exit /b 1
)

call "%PROJECT_DIR%\.venv\Scripts\activate.bat"

if exist "%PROJECT_DIR%\.env" (
    for /f "tokens=1,2 delims==" %%a in (%PROJECT_DIR%\.env) do (
        set %%a=%%b
    )
)

if not exist "%PROJECT_DIR%\AstrBot" (
    echo [错误] 未找到 AstrBot 目录，请先运行 deploy.bat
    pause
    exit /b 1
)

cd "%PROJECT_DIR%\AstrBot"
echo 正在启动 AstrBot...
echo QQ: %BOT_QQ_MAIN%
echo 管理面板: http://localhost:6190
echo.
python main.py
pause
