@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ========================================
echo   astrbot-moon 一键安装脚本
echo ========================================
echo.

REM ── 检查 Python ──
echo [检查] Python 环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请安装 Python 3.10+
    pause
    exit /b 1
)
for /f "tokens=2 delims=." %%i in ('python -c "import sys; print(sys.version)"') do (
    echo   Python %%i ^^!
)
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
set /p QQ_NUMBER="请输入你的机器人 QQ 号: "
if "%QQ_NUMBER%"=="" (
    echo [错误] QQ 号不能为空
    pause
    exit /b 1
)
echo QQ=%QQ_NUMBER%> .env
echo BOT_QQ_MAIN=%QQ_NUMBER%>> .env
echo   QQ 号已保存 ^^!
echo.

REM ── 安装 AstrBot ──
echo [1/4] 安装 AstrBot...
if not exist "AstrBot" (
    git clone https://github.com/Soulter/AstrBot.git
    echo   AstrBot 已克隆 ^^!
) else (
    echo   AstrBot 已存在，跳过克隆
)
cd AstrBot
pip install -r requirements.txt -q
echo   AstrBot 依赖已安装 ^^!
cd ..
echo.

REM ── 部署插件 ──
echo [2/4] 部署插件...
if not exist "AstrBot\data\plugins" mkdir "AstrBot\data\plugins"

for /d %%i in (astrbot_plugin_*) do (
    if exist "AstrBot\data\plugins\%%i" (
        echo   %%i (已存在，跳过)
    ) else (
        mklink /J "AstrBot\data\plugins\%%i" "%%i" >nul 2>&1
        if errorlevel 1 (
            xcopy "%%i" "AstrBot\data\plugins\%%i\" /E /I /Y /Q >nul
        )
        echo   %%i ^^!
    )
)
echo   插件部署完成 ^^!
echo.

REM ── 复制角色卡 ──
echo [3/4] 复制角色卡...
set CHAR_DST=AstrBot\data\plugins\astrbot_plugin_suli_tavern\characters
if not exist "%CHAR_DST%" mkdir "%CHAR_DST%"
if exist "characters\*.json" (
    copy "characters\*.json" "%CHAR_DST%\" /Y >nul
    echo   角色卡已复制 ^^!
)
echo.

REM ── 安装 NapCat ──
echo [4/4] 安装 NapCat...
echo   NapCat 需要单独安装。
echo   请访问: https://napcat.napneko.icu/
echo   推荐使用 NapCatWin (Windows GUI 版本)
echo   下载后登录 QQ 号 %QQ_NUMBER% 即可
echo.

REM ── 完成 ──
echo ========================================
echo   安装完成！
echo ========================================
echo.
echo 下一步:
echo   1. 启动 NapCatWin 并登录 QQ 号 %QQ_NUMBER%
echo   2. 启动 AstrBot: cd AstrBot ^&^& python main.py
echo   3. 打开管理面板: http://localhost:6190
echo   4. 在面板中配置 LLM API (OpenAI 兼容接口)
echo.
echo 快速启动: scripts\start.bat
echo.
pause
