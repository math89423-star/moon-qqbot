#!/usr/bin/env python3
"""用 headless Chrome 打开抖音视频页，从页面 SSR JSON 中提取视频直链。
比 yt-dlp 的 DouyinIE 更可靠，因为 Chrome 自己处理所有反爬签名。
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path


def extract_via_chrome(video_url: str, timeout_sec: int = 15) -> dict | None:
    """返回 {title, video_url, duration} 或 None。"""
    profile = Path("/tmp/chrome_douyin_video_profile")
    if profile.exists():
        import shutil
        shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True, exist_ok=True)

    # Headless Chrome: 打开视频页，用 CDP 获取渲染后的 HTML
    js_script = """
    (async () => {
        // 等待页面加载 (最多 timeout 秒)
        const deadline = Date.now() + %d * 1000;
        let html = '';
        while (Date.now() < deadline) {
            html = document.documentElement.outerHTML;
            // 检查是否有视频数据
            if (html.includes('video_id') || html.includes('playAddr') || html.includes('play_addr')) {
                break;
            }
            await new Promise(r => setTimeout(r, 500));
        }
        return html;
    })()
    """ % timeout_sec

    cmd = [
        "google-chrome" if Path("/usr/bin/google-chrome").exists() else
        "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
        f"--user-data-dir={profile}",
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--window-size=1920,1080",
        "--virtual-time-budget=%d000" % timeout_sec,  # ms
        video_url,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_sec + 10,
        )
    except subprocess.TimeoutExpired:
        return None

    # 尝试从 stdout/stderr 中找到 CDP 输出的 HTML
    # Chrome headless 模式下，--dump-dom 直接输出 DOM
    # 这里用 --virtual-time-budget 自动退出，但 HTML 不会自动输出
    # 换个方式：用 --headless --dump-dom
    return None


def extract_via_dump_dom(video_url: str, timeout_sec: int = 15) -> dict | None:
    """用 Chrome --dump-dom 获取渲染后的 HTML，从中提取视频数据。"""
    profile = Path("/tmp/chrome_douyin_video_profile")
    import shutil
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True, exist_ok=True)

    chrome_paths = [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    chrome = None
    for p in chrome_paths:
        if Path(p).exists():
            chrome = p
            break

    if chrome is None:
        # Try Windows Chrome via WSL
        win_chrome = "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"
        if Path(win_chrome).exists():
            chrome = win_chrome

    if chrome is None:
        return None

    cmd = [
        chrome,
        f"--user-data-dir={profile}",
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        "--disable-extensions",
        f"--virtual-time-budget={timeout_sec * 1000}",
        "--dump-dom",
        video_url,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_sec + 15,
        )
    except subprocess.TimeoutExpired:
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    html = result.stdout
    if not html or len(html) < 500:
        return None

    # 抖音页面在 <script id="RENDER_DATA" type="application/json"> 中嵌入了 SSR 数据
    # 或者在 window._ROUTER_DATA 中
    # 或者在 <script id="SSR_HYDRATED_DATA" 中
    data = None

    # 方法1: RENDER_DATA
    m = re.search(r'<script[^>]*id="RENDER_DATA"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            # URL-decode + JSON parse
            from urllib.parse import unquote
            raw = unquote(m.group(1))
            data = json.loads(raw)
        except (json.JSONDecodeError, Exception):
            pass

    # 方法2: window._ROUTER_DATA
    if data is None:
        m = re.search(r'window\._ROUTER_DATA\s*=\s*({.*?});', html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

    # 方法3: 直接搜 video playAddr
    if data is None:
        # 直接正则搜视频直链
        m = re.search(r'"playAddr":\s*"([^"]+)"', html)
        play_addr = m.group(1) if m else None
        title_m = re.search(r'<title[^>]*>(.*?)</title>', html)
        title = title_m.group(1).strip() if title_m else "抖音视频"
        if play_addr:
            # 反转义
            play_addr = play_addr.replace('\\u0026', '&').replace('\\\\/', '/')
            return {"title": title, "video_url": play_addr, "duration": 0}

    if data:
        # 遍历嵌套结构找视频地址
        def find_video(d, depth=0):
            if depth > 10:
                return None
            if isinstance(d, dict):
                for key in ("playAddr", "play_addr", "downloadAddr", "download_addr",
                           "video", "bitRateList", "playApi"):
                    if key in d and isinstance(d[key], (str, list)):
                        if isinstance(d[key], str) and d[key].startswith("http"):
                            return d[key]
                        if isinstance(d[key], list) and d[key] and isinstance(d[key][0], dict):
                            # bitRateList: [{playAddr: ...}, ...]
                            for item in d[key]:
                                for vk in ("playAddr", "play_addr"):
                                    if vk in item:
                                        return item[vk]
                for v in d.values():
                    r = find_video(v, depth + 1)
                    if r:
                        return r
            elif isinstance(d, list):
                for item in d:
                    r = find_video(item, depth + 1)
                    if r:
                        return r
            return None

        video_url_found = find_video(data)

        # 找标题
        def find_title(d, depth=0):
            if depth > 10:
                return None
            if isinstance(d, dict):
                for key in ("desc", "title", "share_info"):
                    if key in d:
                        if key == "share_info" and isinstance(d[key], dict):
                            return d[key].get("title") or d[key].get("desc")
                        if isinstance(d[key], str):
                            return d[key]
                for v in d.values():
                    r = find_title(v, depth + 1)
                    if r:
                        return r
            return None

        title = find_title(data) or "抖音视频"

        if video_url_found:
            return {"title": title, "video_url": video_url_found, "duration": 0}

    return None


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    if not url:
        print("Usage: python extract_douyin_via_chrome.py <douyin_url>")
        sys.exit(1)

    print(f"=== 用 headless Chrome 提取: {url[:80]}... ===")
    result = extract_via_dump_dom(url)

    if result:
        print(f"标题: {result['title']}")
        print(f"视频: {result['video_url'][:100]}...")
        print("--- JSON OUTPUT ---")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("✗ 未能提取视频数据")
        sys.exit(1)
