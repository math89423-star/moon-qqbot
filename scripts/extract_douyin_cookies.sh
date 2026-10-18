#!/usr/bin/env bash
# 从 Chrome cookie DB 提取抖音 cookies → Netscape 格式
# 前提: 已用 Chrome 访问过 douyin.com, 然后完全关闭 Chrome
set -euo pipefail

CHROME_DB="${CHROME_DB:-/mnt/c/Users/<你的Windows用户名>/AppData/Local/Google/Chrome/User Data/Default/Network/Cookies}"
OUTPUT="${OUTPUT:-$PWD/runtime/data/douyin_cookies.txt}"

echo "=== 从 Chrome 提取抖音 cookies ==="

python3 "$(dirname "$0")/extract_cookies.py" "$CHROME_DB" "$OUTPUT" douyin tiktok

echo "=== 完成 ==="
echo "重启容器: bash cmd.bash restart"
