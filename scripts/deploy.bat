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
) else (
    echo   AstrBot 已存在，跳过克隆
)
cd AstrBot
echo   正在安装/更新依赖...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo   [错误] AstrBot 依赖安装失败
    pause
    exit /b 1
)
echo   AstrBot 依赖已安装 ^^!
cd ..
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

REM ── NapCatQQ ──
echo [5/5] NapCatQQ (QQ 协议) ...

set NAPCAT_DIR=%PROJECT_DIR%\NapCatQQ
set NAPCAT_CONFIG=%NAPCAT_DIR%\config
set NAPCAT_LAUNCHER=%NAPCAT_DIR%\launcher.bat

if exist "%NAPCAT_LAUNCHER%" (
    echo   NapCatQQ 已安装，跳过
) else (
    echo   正在下载 NapCatQQ...
    if not exist "%NAPCAT_DIR%" mkdir "%NAPCAT_DIR%"

    REM GitHub API 获取最新版本 → 国内镜像加速下载
    powershell -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\dl_napcat.ps1" -OutDir "%NAPCAT_DIR%"

    if errorlevel 1 (
        echo   [警告] 自动下载失败，请手动下载 NapCatQQ:
        echo   https://github.com/NapNeko/NapCatQQ/releases
        echo   下载 NapCat.Shell.Windows.Node.zip 解压到: %NAPCAT_DIR%
    ) else (
        REM 查找 launcher（NapCatQQ 可能有多层子目录）
        for /r "%NAPCAT_DIR%" %%f in (launcher.bat) do (
            if not exist "%NAPCAT_LAUNCHER%" (
                move "%%f" "%NAPCAT_LAUNCHER%" >nul 2>&1
            )
        )
        if exist "%NAPCAT_LAUNCHER%" (
            echo   NapCatQQ 已就绪 ^^!
        ) else (
            echo   [警告] NapCatQQ 已解压但未找到 launcher.bat
            echo   请检查目录: %NAPCAT_DIR%
        )
    )
)

REM 配置 NapCat OneBot WebSocket → AstrBot
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
    echo   NapCat OneBot 配置已生成
) else (
    echo   NapCat 配置已存在，跳过
)
echo.

REM ── 完成 ──
echo ========================================
echo   安装完成！
echo ========================================
echo.
echo 下一步:
echo   1. 启动: scripts\start.bat
echo   2. NapCat 扫码登录 QQ 号 !QQ_NUMBER! (http://localhost:6099)
echo   3. 管理面板: http://localhost:5190
echo   4. 在面板中配置 LLM API (OpenAI 兼容接口)
echo.
pause
