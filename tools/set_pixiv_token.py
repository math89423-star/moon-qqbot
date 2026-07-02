"""从 pixiv_refresh_token.txt 读取 token 写入共享 bot_config。"""
import sqlite3
import sys
from pathlib import Path

DB = "/home/administrator/suli_qqbot/runtime/shared/db/none_qqbot.db"
TOKEN_FILE = Path(__file__).parent / "pixiv_refresh_token.txt"

if not TOKEN_FILE.exists():
    print(f"❌ Token 文件不存在: {TOKEN_FILE}")
    print("   请先运行 python tools/get_pixiv_token_v2.py")
    sys.exit(1)

token = TOKEN_FILE.read_text().strip()
if not token:
    print("❌ Token 文件为空")
    sys.exit(1)

conn = sqlite3.connect(DB)
conn.execute(
    "INSERT OR REPLACE INTO bot_config (key, value, updated_at) "
    "VALUES ('pixiv_refresh_token', ?, strftime('%s','now'))",
    (token,),
)
conn.commit()

row = conn.execute(
    "SELECT key, substr(value,1,8)||'...'||substr(value,-4) "
    "FROM bot_config WHERE key='pixiv_refresh_token'"
).fetchone()
conn.close()

print(f"✅ 已写入: {row[0]} = {row[1]}" if row else "❌ 写入失败")
