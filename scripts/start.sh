#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "[错误] 未找到虚拟环境，请先运行 deploy.sh"
    exit 1
fi

source "$VENV_DIR/bin/activate"

if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

ASTRBOT_DIR="$PROJECT_DIR/AstrBot"
if [ ! -d "$ASTRBOT_DIR" ]; then
    echo "[错误] 未找到 AstrBot 目录，请先运行 deploy.sh"
    exit 1
fi

echo "正在启动管理面板: http://localhost:5190"
cd "$PROJECT_DIR/astrbot_plugin_suli_tavern"
python3 panel_main.py --port 5190 &
PANEL_PID=$!
cd "$ASTRBOT_DIR"

echo "正在启动 AstrBot..."
echo "QQ: ${BOT_QQ_MAIN:-未设置}"
echo ""
python3 main.py

kill $PANEL_PID 2>/dev/null
