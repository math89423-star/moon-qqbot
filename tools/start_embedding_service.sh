#!/bin/bash
# suli_qqbot — BGE-M3 Embedding 服务管理 (WSL2 宿主机 GPU)
#
# 用法: bash tools/start_embedding_service.sh [start|stop|restart|status]
#
# 服务在 WSL2 宿主机上运行，提供 GPU embedding 给 Docker 容器。
# Docker 通过 host.docker.internal:8880 访问。

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONDA_ENV="/home/administrator/miniconda3/envs/qqbot"
SERVICE_SCRIPT="$ROOT/tools/embedding_service.py"
PIDFILE="/tmp/embedding_service.pid"
LOGFILE="/tmp/embedding_service.log"
PORT=8880

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

_get_pid() {
    if [ -f "$PIDFILE" ]; then
        cat "$PIDFILE"
    else
        echo ""
    fi
}

_is_running() {
    local pid
    pid=$(_get_pid)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    return 1
}

do_start() {
    if _is_running; then
        echo -e "  Embedding 服务: ${GREEN}已在运行${NC} (PID: $(_get_pid), :$PORT)"
        return
    fi

    # 清理残留 PID 文件
    rm -f "$PIDFILE"

    echo -n "启动 Embedding 服务 (BGE-M3 on GPU)... "
    nohup "$CONDA_ENV/bin/python" "$SERVICE_SCRIPT" > "$LOGFILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"

    # 等待就绪
    local waited=0
    while [ $waited -lt 30 ]; do
        if curl -s "http://localhost:$PORT/health" 2>/dev/null | grep -q '"model_loaded":true'; then
            echo -e "${GREEN}完成${NC}"
            echo "  PID: $pid  |  端口: $PORT"
            echo "  日志: $LOGFILE"
            echo "  健康检查: curl http://localhost:$PORT/health"
            return
        fi
        # 检查进程是否存活
        if ! kill -0 "$pid" 2>/dev/null; then
            echo -e "${RED}失败 (进程已退出)${NC}"
            echo "  日志: tail $LOGFILE"
            rm -f "$PIDFILE"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done

    echo -e "${YELLOW}超时 (30s)${NC}"
    echo "  进程在运行但 health check 未通过。查看日志: tail $LOGFILE"
}

do_stop() {
    local pid
    pid=$(_get_pid)

    if [ -z "$pid" ]; then
        # fallback: 按端口查找
        pid=$(ss -tlnp 2>/dev/null | grep ":$PORT" | grep -oP 'pid=\K\d+' | head -1)
    fi

    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        echo -e "  Embedding 服务: ${YELLOW}未运行${NC}"
        rm -f "$PIDFILE"
        return
    fi

    echo -n "停止 Embedding 服务 (PID: $pid)... "
    kill "$pid" 2>/dev/null || true

    # 等待退出 (最多 10s)
    for i in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo -e "${GREEN}完成${NC}"
            rm -f "$PIDFILE"
            return
        fi
        sleep 1
    done

    # 强制
    echo -n " 强制停止... "
    kill -9 "$pid" 2>/dev/null || true
    sleep 1
    rm -f "$PIDFILE"
    echo -e "${GREEN}完成${NC}"
}

do_status() {
    echo -e "${CYAN}─── Embedding 服务 ───${NC}"

    if _is_running; then
        local pid
        pid=$(_get_pid)
        echo -e "  状态: ${GREEN}运行中${NC} (PID: $pid)"
        echo "  端口: $PORT"

        # Health check
        local health
        health=$(curl -s "http://localhost:$PORT/health" 2>/dev/null || echo '{"error":"unreachable"}')
        echo "  健康: $health"

        # GPU 内存
        local vram
        vram=$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader 2>/dev/null | grep "^$pid" | awk -F', ' '{print $2}')
        if [ -n "$vram" ]; then
            echo "  GPU VRAM: $vram"
        fi
    else
        echo -e "  状态: ${RED}未运行${NC}"
    fi

    echo "  日志: $LOGFILE"
}

# ── 主入口 ──

case "${1:-start}" in
    start)
        do_start
        ;;
    stop)
        do_stop
        ;;
    restart)
        do_stop
        sleep 1
        do_start
        ;;
    status)
        do_status
        ;;
    *)
        echo "用法: bash tools/start_embedding_service.sh [start|stop|restart|status]"
        exit 1
        ;;
esac
