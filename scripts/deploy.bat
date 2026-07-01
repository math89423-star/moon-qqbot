@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ========================================
echo   moon-qqbot 一键安装脚本
echo ========================================
echo.

cd /d "%~dp0"
cd ..
set PROJECT_DIR=%CD%

REM ── 检查 Python ──
echo [检查] Python 环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请安装 Python 3.10+
    pause
    exit /b 1
)
python -c "import sys; print(f'  Python {sys.version_info.major}.{sys.version_info.minor} ^^!')"
echo.

REM ── 检查 Git ──
echo [检查] Git 环境...
git --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Git，请先安装 Git
    pause
    exit /b 1
)
echo   Git ^^!
echo.

REM ── 输入 QQ 号 ──
if not exist ".env" (
    set /p QQ_NUMBER="请输入你的机器人 QQ 号: "
    if "!QQ_NUMBER!"=="" (
        echo [错误] QQ 号不能为空
        pause
        exit /b 1
    )
    echo QQ=!QQ_NUMBER!> .env
    echo BOT_QQ_MAIN=!QQ_NUMBER!>> .env
    echo   QQ 号已保存 ^^!
) else (
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        if "%%a"=="BOT_QQ_MAIN" set QQ_NUMBER=%%b
    )
    echo   .env 已存在，QQ: !QQ_NUMBER!
)
echo.

REM ── 创建虚拟环境 ──
echo [1/5] Python 虚拟环境...
if not exist ".venv" (
    python -m venv .venv
    echo   虚拟环境已创建 ^^!
    set VENV_NEW=1
) else (
    echo   虚拟环境已存在，跳过创建
    set VENV_NEW=0
)
call .venv\Scripts\activate.bat
python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple >nul 2>&1
if "!VENV_NEW!"=="1" (
    echo   正在安装依赖...
python -m pip install -r requirements.txt
    echo   依赖已安装 ^^!
) else (
    echo   依赖跳过 (虚拟环境已存在)
)
echo.

REM ── 安装 AstrBot ──
echo [2/5] AstrBot 框架...
if not exist "AstrBot" (
    git clone https://github.com/AstrBotDevs/AstrBot.git
    if errorlevel 1 (
        echo   [错误] AstrBot 克隆失败，请检查网络连接
        pause
        exit /b 1
    )
    echo   AstrBot 已克隆 ^^!
    cd AstrBot
    echo   正在安装依赖...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo   [错误] AstrBot 依赖安装失败
        pause
        exit /b 1
    )
    echo   AstrBot 依赖已安装 ^^!
    cd ..
) else (
    echo   AstrBot 已存在，跳过克隆和依赖安装
)
echo.

REM ── 部署插件 ──
echo [3/5] 部署插件...
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
        echo   %%i ^^!
    )
)
if !PLUGIN_COUNT! gtr 0 (
    echo   已部署 !PLUGIN_COUNT! 个插件
)
if !PLUGIN_SKIP! gtr 0 (
    echo   已跳过 !PLUGIN_SKIP! 个 (已存在)
)
echo.

REM ── 复制角色卡 ──
echo [4/5] 角色卡...
set CHAR_DST=AstrBot\data\plugins\astrbot_plugin_suli_tavern\characters
if not exist "%CHAR_DST%" mkdir "%CHAR_DST%"
copy "characters\*.json" "%CHAR_DST%\" /Y >nul 2>&1
echo   角色卡已就绪 ^^!
echo.

REM ── 检查 NapCat ──
echo [5/5] NapCat (QQ 协议) ...

set NAPCAT_FOUND=0
if exist "C:\NapCat" set NAPCAT_FOUND=1
if exist "C:\NapCatWin" set NAPCAT_FOUND=1
if exist "%USERPROFILE%\NapCat" set NAPCAT_FOUND=1
if exist "D:\NapCat" set NAPCAT_FOUND=1
tasklist /FI "IMAGENAME eq napcat.exe" 2>nul | find /I "napcat.exe" >nul && set NAPCAT_FOUND=1
tasklist /FI "IMAGENAME eq qq.exe" 2>nul | find /I "qq.exe" >nul && set NAPCAT_FOUND=1

if !NAPCAT_FOUND!==1 (
    echo   NapCat 已安装，跳过
) else (
    echo   NapCat 未检测到，请访问安装:
    echo   https://napcat.napneko.icu/
    echo   推荐使用 NapCatWin (Windows GUI 版本)
    echo   安装后登录 QQ 号 !QQ_NUMBER!
)
echo.

REM ── 完成 ──
echo ========================================
echo   安装完成！
echo ========================================
echo.
echo 下一步:
if !NAPCAT_FOUND!==0 (
echo   1. 安装并启动 NapCat，登录 QQ 号 !QQ_NUMBER!
echo   2. 启动: scripts\start.bat
) else (
echo   1. 启动: scripts\start.bat
)
echo   2. 管理面板自动打开: http://localhost:5190
echo   3. 在面板中配置 LLM API (OpenAI 兼容接口)
echo.
pause
