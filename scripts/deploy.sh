#!/bin/bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  moon-qqbot 一键安装脚本${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_DIR/.venv"

# ── 检查 Python ──
echo -e "${YELLOW}[检查] Python 环境...${NC}"
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}[错误] 未找到 python3，请安装 Python 3.10+${NC}"
    exit 1
fi
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo -e "${GREEN}  Python $PYTHON_VERSION ✓${NC}"

# ── 检查 Git ──
if ! command -v git &>/dev/null; then
    echo -e "${RED}[错误] 未找到 git，请先安装 Git${NC}"
    exit 1
fi
echo -e "${GREEN}  Git ✓${NC}"

# ── 输入 QQ 号 ──
echo ""
if [ ! -f "$PROJECT_DIR/.env" ]; then
    read -p "请输入你的机器人 QQ 号: " QQ_NUMBER
    if [ -z "$QQ_NUMBER" ]; then
        echo -e "${RED}[错误] QQ 号不能为空${NC}"
        exit 1
    fi
    echo "QQ=$QQ_NUMBER" > "$PROJECT_DIR/.env"
    echo "BOT_QQ_MAIN=$QQ_NUMBER" >> "$PROJECT_DIR/.env"
    echo -e "${GREEN}  QQ 号已保存 ✓${NC}"
else
    source "$PROJECT_DIR/.env" 2>/dev/null || true
    QQ_NUMBER="${BOT_QQ_MAIN:-未知}"
    echo -e "${GREEN}  .env 已存在，QQ: $QQ_NUMBER${NC}"
fi

# ── 创建虚拟环境 ──
echo ""
echo -e "${YELLOW}[1/5] Python 虚拟环境...${NC}"
VENV_NEW=0
USE_VENV=1

if [ ! -d "$VENV_DIR" ]; then
    if python3 -m venv "$VENV_DIR" 2>/dev/null; then
        echo -e "${GREEN}  虚拟环境已创建 ✓${NC}"
        VENV_NEW=1
    else
        echo -e "${YELLOW}  venv 创建失败 (可能缺少 python3-venv 包)${NC}"
        echo -e "${YELLOW}  将使用全局 pip 安装依赖${NC}"
        USE_VENV=0
    fi
else
    echo -e "${GREEN}  虚拟环境已存在，跳过创建${NC}"
fi

if [ "$USE_VENV" -eq 1 ]; then
    source "$VENV_DIR/bin/activate"
    pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple 2>/dev/null || true
    if [ "$VENV_NEW" -eq 1 ]; then
        echo "  正在安装项目依赖..."
pip install -r "$PROJECT_DIR/requirements.txt"
        echo -e "${GREEN}  依赖已安装 ✓${NC}"
    else
        echo -e "${GREEN}  依赖跳过 (虚拟环境已存在)${NC}"
    fi
else
    pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple 2>/dev/null || true
    echo "  正在安装项目依赖..."
    pip3 install -r "$PROJECT_DIR/requirements.txt" 2>/dev/null || pip install -r "$PROJECT_DIR/requirements.txt"
    echo -e "${GREEN}  依赖已安装 (全局) ✓${NC}"
fi

# ── 安装 AstrBot ──
echo ""
echo -e "${YELLOW}[2/5] AstrBot 框架...${NC}"
ASTRBOT_DIR="$PROJECT_DIR/AstrBot"
if [ ! -d "$ASTRBOT_DIR" ]; then
    if ! git clone https://github.com/AstrBotDevs/AstrBot.git "$ASTRBOT_DIR"; then
        echo -e "${RED}[错误] AstrBot 克隆失败，请检查网络连接${NC}"
        exit 1
    fi
    cd "$ASTRBOT_DIR"
    echo "  正在安装 AstrBot 依赖..."
    pip install -r requirements.txt
    echo -e "${GREEN}  AstrBot 已克隆 + 依赖已安装 ✓${NC}"
else
    echo -e "${GREEN}  AstrBot 已存在，跳过克隆和依赖安装${NC}"
fi

# ── 部署插件 ──
echo ""
echo -e "${YELLOW}[3/5] 部署插件...${NC}"
PLUGIN_DIR="$ASTRBOT_DIR/data/plugins"
mkdir -p "$PLUGIN_DIR"

NEW_COUNT=0
SKIP_COUNT=0
for plugin_dir in "$PROJECT_DIR"/astrbot_plugin_*; do
    if [ -d "$plugin_dir" ]; then
        plugin_name=$(basename "$plugin_dir")
        target="$PLUGIN_DIR/$plugin_name"
        if [ ! -e "$target" ]; then
            ln -sf "$plugin_dir" "$target" 2>/dev/null || cp -r "$plugin_dir" "$target"
            echo "  $plugin_name ✓"
            ((NEW_COUNT++)) || true
        else
            ((SKIP_COUNT++)) || true
        fi
    fi
done
echo -e "${GREEN}  已部署 $NEW_COUNT 个，跳过 $SKIP_COUNT 个 ✓${NC}"

# ── 复制角色卡 ──
echo ""
echo -e "${YELLOW}[4/5] 角色卡...${NC}"
CHAR_SRC="$PROJECT_DIR/characters"
CHAR_DST="$ASTRBOT_DIR/data/plugins/astrbot_plugin_suli_tavern/characters"
mkdir -p "$CHAR_DST"
cp "$CHAR_SRC"/*.json "$CHAR_DST/" 2>/dev/null || true
echo -e "${GREEN}  角色卡已就绪 ✓${NC}"

# ── 检查 NapCat ──
echo ""
echo -e "${YELLOW}[5/5] NapCat (QQ 协议)...${NC}"
NAPCAT_FOUND=0
if command -v napcat &>/dev/null; then NAPCAT_FOUND=1; fi
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q 'napcat'; then NAPCAT_FOUND=1; fi
if [ -d "/opt/napcat" ] || [ -d "$HOME/napcat" ] || [ -d "$HOME/NapCat" ]; then NAPCAT_FOUND=1; fi

if [ "$NAPCAT_FOUND" -eq 1 ]; then
    echo -e "${GREEN}  NapCat 已安装，跳过${NC}"
else
    echo -e "${CYAN}  NapCat 未检测到。请选择安装方式:${NC}"
    echo "  1) Docker (推荐)"
    echo "  2) 手动安装"
    echo "  3) 跳过 (已安装)"
    read -p "请选择 [1-3]: " NAPCAT_CHOICE

    case $NAPCAT_CHOICE in
        1)
            if command -v docker &>/dev/null; then
                docker run -d --name napcat \
                    -p 3000:3000 -p 3001:3001 -p 6099:6099 \
                    -v napcat_data:/app/data \
                    napneko/napcat:latest 2>/dev/null && \
                echo -e "${GREEN}  NapCat Docker 已启动 ✓${NC}" || \
                echo -e "${YELLOW}  Docker 启动失败，请检查 Docker 是否运行${NC}"
            else
                echo -e "${YELLOW}  未检测到 Docker，请先安装 Docker${NC}"
            fi
            ;;
        2)
            echo -e "${CYAN}  请访问 https://napcat.napneko.icu/ 安装 NapCat${NC}"
            ;;
        3)
            echo -e "${GREEN}  跳过 NapCat 安装${NC}"
            ;;
    esac
fi

# ── 完成 ──
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  安装完成！${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "下一步:"
if [ "$NAPCAT_FOUND" -eq 0 ]; then
    echo -e "  1. 安装并启动 NapCat，登录 QQ 号 ${CYAN}$QQ_NUMBER${NC}"
    echo -e "  2. 启动 AstrBot: ${CYAN}bash scripts/start.sh${NC}"
else
    echo -e "  1. 启动 AstrBot: ${CYAN}bash scripts/start.sh${NC}"
fi
echo -e "  3. 打开管理面板: ${CYAN}http://localhost:5190${NC}"
echo -e "  4. 在面板中配置 LLM API (OpenAI 兼容接口)"
echo ""
