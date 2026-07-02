#!/usr/bin/env python3
"""v2: 用 cloudscraper (和 pixivpy3 相同HTTP客户端) 获取 refresh_token。
然后立即用 pixivpy3 验证 token 是否有效。
"""
from __future__ import annotations

import hashlib
import secrets
import sys
import urllib.parse
from base64 import urlsafe_b64encode
from pathlib import Path

import cloudscraper

LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
AUTH_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
CALLBACK_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"

# ── 1. PKCE ─────────────────────────────────────

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
print(f"  {login_url}")
print()
print("登录成功后会跳转到 pixiv:// 页面——复制完整 URL 粘贴:")
callback = input("> ").strip()

# ── 2. 提取 code ─────────────────────────────────

parsed = urllib.parse.urlparse(callback)
qs = urllib.parse.parse_qs(parsed.query)
code = qs.get("code", [None])[0]
if not code:
    print(f"❌ 未找到 code. 解析: {qs}")
    sys.exit(1)

# ── 3. 用 cloudscraper 交换 token ────────────────

print()
print("用 cloudscraper 交换 token...")

scraper = cloudscraper.create_scraper()
try:
    resp = scraper.post(
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
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
except Exception as e:
    print(f"❌ 交换失败: {e}")
    if hasattr(e, "response") and e.response is not None:
        print(f"   响应: {e.response.text[:500]}")
    sys.exit(1)

refresh_token = data.get("refresh_token")
print(f"   响应 status={resp.status_code}, refresh_token={'有' if refresh_token else '无'}")
if not refresh_token:
    print(f"   完整: {data}")
    sys.exit(1)

# ── 4. 立即验证 token ────────────────────────────

print()
print("验证 token (pixivpy3 auth)...")

from pixivpy3 import AppPixivAPI  # type: ignore[import-untyped]
api = AppPixivAPI()
try:
    api.auth(refresh_token=refresh_token)
    # auth 后旧 token 立即失效，必须用新的
    new_token = api.refresh_token
    print(f"✅ 验证成功! new_token: {new_token[:20]}...")
except Exception as e:
    print(f"❌ 验证失败: {e}")
    sys.exit(1)

# ── 5. 保存新 token（旧 token 已被 auth 消费）──

save_path = Path(__file__).parent / "pixiv_refresh_token.txt"
save_path.write_text(new_token)

print()
print("=" * 60)
print("   ✅ 成功！refresh_token 已验证并保存")
print("=" * 60)
print(f"   Token: {refresh_token}")
print()
print("不要泄露。下一步: python tools/set_pixiv_token.py")
