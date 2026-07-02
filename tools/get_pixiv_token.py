#!/usr/bin/env python3
"""Pixiv refresh_token 获取工具 — 一次性完成。

1. 生成登录链接 → 浏览器打开 → 登录
2. 跳转到 pixiv://... 页面后复制完整 URL 粘贴回来
3. 自动交换 token → 保存到 tools/pixiv_refresh_token.txt
"""
from __future__ import annotations

import hashlib
import secrets
import sys
import urllib.parse
from base64 import urlsafe_b64encode
from pathlib import Path

import requests

LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
AUTH_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
CALLBACK_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"

# ── 1. 生成 PKCE ─────────────────────────────────

verifier = secrets.token_urlsafe(32)
digest = hashlib.sha256(verifier.encode("ascii")).digest()
challenge = urlsafe_b64encode(digest).rstrip(b"=").decode()

params = urllib.parse.urlencode({
    "code_challenge": challenge,
    "code_challenge_method": "S256",
    "client": "pixiv-android",
})
login_url = f"{LOGIN_URL}?{params}"

print()
print("在浏览器中打开此链接并回车:")
print()
print(f"  {login_url}")
print()
print("登录成功后会跳转到 pixiv:// 开头的空白页——")
print("复制地址栏完整 URL 粘贴回来:")

callback = input("> ").strip()

# ── 2. 提取 code ─────────────────────────────────

parsed = urllib.parse.urlparse(callback)
qs = urllib.parse.parse_qs(parsed.query)
code = qs.get("code", [None])[0]

if not code:
    print("❌ 未找到 code 参数。请确认复制了完整 URL。")
    print(f"   解析到: {qs}")
    sys.exit(1)

# ── 3. 交换 token ────────────────────────────────

print()
print("交换 token...")

try:
    resp = requests.post(
        AUTH_TOKEN_URL,
        headers={
            "user-agent": "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)",
            "app-os-version": "14.6",
            "app-os": "ios",
        },
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "include_policy": "true",
            "redirect_uri": CALLBACK_URI,
        },
        proxies={"https": "http://172.31.160.1:7897", "http": "http://172.31.160.1:7897"},
        timeout=30,
    )
    resp.raise_for_status()
except requests.RequestException as e:
    print(f"❌ 网络错误: {e}")
    if hasattr(e, "response") and e.response is not None:
        print(f"   响应: {e.response.text[:500]}")
    sys.exit(1)

data = resp.json()
# 打印完整响应用于调试
import json as _json
print(f"   完整响应: {_json.dumps(data, indent=2, ensure_ascii=False)[:500]}")
refresh_token = data.get("refresh_token")

if not refresh_token:
    print("❌ 未获取到 refresh_token。完整响应:")
    print(data)
    sys.exit(1)

# ── 4. 保存 ──────────────────────────────────────

save_path = Path(__file__).parent / "pixiv_refresh_token.txt"
save_path.write_text(refresh_token)

print()
print("=" * 60)
print("   ✅ 成功！refresh_token 已保存")
print("=" * 60)
print(f"   文件: {save_path}")
print(f"   Token: {refresh_token}")
print()
print("不要泄露此 token。下一步由我写入 bot_config。")
