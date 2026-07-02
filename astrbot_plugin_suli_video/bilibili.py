"""B站 + 抖音视频解析与下载 — 基于 yt-dlp。

设计:
  - yt-dlp 的 extract_info/download 是同步阻塞的 → asyncio.to_thread() 包装
  - DASH 分离流用 ffmpeg 合并 (容器自带 ffmpeg)
  - 优先 bestvideo+bestaudio (DASH), 回退 best (单文件流)
  - 下载前检查 filesize，超 20MB 自动降级或拒绝
  - 超时 120s (bot 不能卡住)
  - 支持平台: B站 (bilibili.com/b23.tv) + 抖音 (douyin.com)

用法:
  from .bilibili import get_video_info, download_video, is_supported_video_url
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────
_BILIBILI_URL_PATTERN = re.compile(
    r"(?:bilibili\.com/video/|b23\.tv/|b22\.tv/)",
    re.IGNORECASE,
)
_DOUYIN_URL_PATTERN = re.compile(
    r"(?:douyin\.com/video/|v\.douyin\.com/)",
    re.IGNORECASE,
)
# yt-dlp 格式选择器 — 多级回退 (容器有 ffmpeg 合并 DASH 分离流)
# B站多数视频只有 DASH (video-only + audio-only), best 匹配不到单文件流
# 优先: DASH video+audio 合并 → 720p/480p
# 回退: 传统单文件流 (少数老视频)
_DEFAULT_FORMAT = (
    "bestvideo[height<=720]+bestaudio/"
    "bestvideo[height<=480]+bestaudio/"
    "best[height<=720]/best[height<=480]/best[height<=360]/best"
)
# 默认最大文件大小 (MB)
_DEFAULT_MAX_MB = 20
# 下载超时 (秒)
_DOWNLOAD_TIMEOUT = 120
# 抖音 cookie 文件路径 (Netscape 格式, 由浏览器扩展导出, 挂在共享卷)
# 宿主路径: runtime/data-luna/douyin_cookies.txt
# 容器路径: /AstrBot/data/douyin_cookies.txt
_DOUYIN_COOKIE_FILE = "/AstrBot/data/douyin_cookies.txt"

# 抖音: videofetch 解析器 (按优先级依次尝试)
_DOUYIN_PARSERS = [
    "SnapAnyVideoClient",
    "VideoFKVideoClient",
    "IIILabVideoClient",
    "VgetVideoClient",
]

# B站反爬: HTTP 412 需要模拟真实浏览器请求头
_BILIBILI_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
    "Origin": "https://www.bilibili.com",
}
# 抖音请求头 — 需要 Referer 否则 API 拒请求
_DOUYIN_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com",
    "Origin": "https://www.douyin.com",
}


def _get_headers(url: str) -> dict[str, str]:
    """根据 URL 域名返回合适的请求头。"""
    if _BILIBILI_URL_PATTERN.search(url):
        return _BILIBILI_HEADERS
    if _DOUYIN_URL_PATTERN.search(url):
        return _DOUYIN_HEADERS
    # 兜底: 通用浏览器 UA
    return {"User-Agent": _BILIBILI_HEADERS["User-Agent"]}


# ── 公开 API ────────────────────────────────────────

def is_bilibili_url(url: str) -> bool:
    """检查是否为 B站/B23 视频链接。"""
    return bool(_BILIBILI_URL_PATTERN.search(url))


def is_douyin_url(url: str) -> bool:
    """检查是否为抖音视频链接。"""
    return bool(_DOUYIN_URL_PATTERN.search(url))


def is_supported_video_url(url: str) -> bool:
    """检查是否为支持的视频链接 (B站 + 抖音)。"""
    return is_bilibili_url(url) or is_douyin_url(url)


async def get_video_info(url: str) -> dict[str, Any]:
    """获取视频元数据 (不下文件)。B站 + 抖音通用。

    Args:
        url: 视频 URL (bilibili.com/video/ 或 douyin.com/video/ 或短链)

    Returns:
        {
            "title": str,
            "duration": int (秒),
            "uploader": str,
            "platform": "bilibili" | "douyin",
            "formats": [{format_id, height, filesize, ext, url}, ...]
        }
        filesize 可能为 None (yt-dlp 不总能拿到预估大小)

    Raises:
        ValueError: 不支持的 URL 或解析失败
        RuntimeError: yt-dlp 内部错误
    """
    if not is_supported_video_url(url):
        raise ValueError(f"不支持的视频链接: {url[:80]}")

    return await asyncio.to_thread(_extract_info_sync, url)


async def download_video(
    url: str,
    target_dir: str,
    max_mb: int = _DEFAULT_MAX_MB,
) -> Path | None:
    """下载视频到目标目录，自动选 ≤max_mb 的最佳质量。B站 + 抖音通用。

    Args:
        url: 视频 URL
        target_dir: 保存目录
        max_mb: 最大文件大小 (MB)，默认 20

    Returns:
        下载后的文件路径，无可用格式 (都超限) 返回 None

    Raises:
        ValueError: 不支持的 URL 或解析失败
        RuntimeError: 下载失败
    """
    if not is_supported_video_url(url):
        raise ValueError(f"不支持的视频链接: {url[:80]}")

    return await asyncio.to_thread(_download_sync, url, target_dir, max_mb)


# ── 抖音: videofetch 下载 (CLI subprocess) ─────────────


def _download_douyin_sync(url: str, target_dir: str, max_mb: int) -> Path | None:
    """用 videofetch CLI 下载抖音视频。返回文件路径或 None (超限/失败)。"""
    import json as _json

    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    cfg = _json.dumps({
        p: {"work_dir": str(target)} for p in _DOUYIN_PARSERS
    })

    for parser in _DOUYIN_PARSERS:
        cmd = [
            "videodl", "-i", url,
            "-g",                    # 仅通用解析器
            "-a", parser,
            "-c", cfg,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=90,
            )
        except subprocess.TimeoutExpired:
            logger.warning("[douyin] videodl %s 超时", parser)
            continue
        except FileNotFoundError:
            raise RuntimeError(
                "videofetch 未安装。请确保 Docker 镜像已重建并包含 videofetch。"
            ) from None

        # 找下载的文件 (videodl 创建 {work_dir}/{ParserName}/{title}.mp4)
        downloaded = sorted(
            target.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if downloaded:
            result_path = downloaded[0]
            actual_mb = result_path.stat().st_size / 1e6
            if actual_mb > max_mb:
                result_path.unlink(missing_ok=True)
                logger.info("[douyin] %s: %.1fMB > %dMB, 尝试下一个解析器", parser, actual_mb, max_mb)
                continue
            logger.info("[douyin] videodl %s 下载成功: %s (%.1fMB)", parser, result_path.name, actual_mb)
            return result_path

        # parser 失败 → 试下一个
        stderr_tail = result.stderr.strip()[-200:] if result.stderr else "(empty)"
        logger.warning("[douyin] videodl %s 失败: %s", parser, stderr_tail)

    return None


# ── 向后兼容别名 ──────────────────────────────────────
get_bilibili_info = get_video_info
download_bilibili_video = download_video


# ── 同步实现 (在线程池中运行) ────────────────────────


def _extract_info_sync(url: str) -> dict[str, Any]:
    """同步提取视频元数据 (B站: yt-dlp, 抖音: 占位信息 + videofetch 下载)。"""
    # 抖音: yt-dlp 的 DouyinIE 已废, 跳过 info 提取
    if _DOUYIN_URL_PATTERN.search(url):
        video_id = url.rstrip("/").split("/")[-1].split("?")[0]
        return {
            "title": f"抖音视频-{video_id}",
            "duration": 0,
            "uploader": "抖音",
            "webpage_url": url,
            "platform": "douyin",
            "formats": [],
        }

    try:
        import yt_dlp  # type: ignore[import-not-found]  # Docker-only dep
    except ImportError:
        raise RuntimeError("yt-dlp 未安装，无法解析视频链接") from None

    opts: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "socket_timeout": 30,
        "http_headers": _get_headers(url),
    }
    # 抖音需要浏览器 cookie (s_v_web_id 由页面 JS 生成)
    if _DOUYIN_URL_PATTERN.search(url):
        if Path(_DOUYIN_COOKIE_FILE).exists():
            opts["cookiefile"] = _DOUYIN_COOKIE_FILE
        else:
            raise ValueError(
                "抖音视频需要浏览器 cookie 才能下载。\n"
                "请用浏览器扩展导出 cookies.txt 放到共享目录。"
            )

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:  # type: ignore[attr-defined]
        msg = str(e)
        if "Private video" in msg or "private" in msg.lower():
            raise ValueError("该视频为私有视频，无法访问") from e
        if "Video unavailable" in msg or "unavailable" in msg.lower():
            raise ValueError("视频不可用——可能已删除或链接失效") from e
        if "sign in" in msg.lower() or "login" in msg.lower():
            raise ValueError("该视频需要登录才能访问") from e
        raise ValueError(f"解析失败: {e}") from e
    except Exception as e:
        raise RuntimeError(f"yt-dlp 解析异常: {e}") from e

    if info is None:
        raise ValueError("无法获取视频信息——链接可能无效")

    # 提取格式列表 (只保留有意义的字段)
    formats: list[dict[str, Any]] = []
    for fmt in info.get("formats", []):  # type: ignore[union-attr]
        url_d = fmt.get("url", "")
        if not url_d:
            continue
        formats.append({
            "format_id": fmt.get("format_id", "?"),
            "height": fmt.get("height") or 0,
            "width": fmt.get("width") or 0,
            "filesize": fmt.get("filesize"),  # 可能为 None
            "ext": fmt.get("ext", "mp4"),
            "vcodec": fmt.get("vcodec", "none"),
            "acodec": fmt.get("acodec", "none"),
            "tbr": fmt.get("tbr"),  # 总码率 (kbps), 用于估算大小
            "url": url_d,
        })

    return {
        "title": info.get("title", "未知标题"),
        "duration": info.get("duration", 0) or 0,
        "uploader": info.get("uploader", "未知"),
        "webpage_url": info.get("webpage_url", url),
        "platform": "bilibili" if _BILIBILI_URL_PATTERN.search(url) else "douyin",
        "formats": formats,
    }


def _download_sync(url: str, target_dir: str, max_mb: int) -> Path | None:
    """同步下载视频。B站用 yt-dlp, 抖音用 videofetch CLI。"""
    # 抖音走 videofetch (yt-dlp DouyinIE 已废)
    if _DOUYIN_URL_PATTERN.search(url):
        return _download_douyin_sync(url, target_dir, max_mb)

    import yt_dlp  # type: ignore[import-not-found]  # Docker-only dep

    # 1. 获取元数据 + 完整格式列表
    info = _extract_info_sync(url)
    title = info.get("title", "unknown")

    # 2. DASH 流 size 是分开的 (video+audio), 合并后通常 ≤20MB (≤720p)
    #    真正的大视频由 post-download 检查兜底
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    out_template = str(target / "%(title).100s-%(id)s.%(ext)s")

    download_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": _DEFAULT_FORMAT,
        "outtmpl": out_template,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "http_headers": _get_headers(url),
        # DASH 合并: yt-dlp 自动调 ffmpeg 合并 video+audio
        "merge_output_format": "mp4",
    }
    if _DOUYIN_URL_PATTERN.search(url) and Path(_DOUYIN_COOKIE_FILE).exists():
        download_opts["cookiefile"] = _DOUYIN_COOKIE_FILE

    try:
        with yt_dlp.YoutubeDL(download_opts) as ydl:  # type: ignore[arg-type]
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:  # type: ignore[attr-defined]
        raise RuntimeError(f"下载失败: {e}") from e

    # 3. 找到下载的文件 (yt-dlp 可能改名)
    downloaded = sorted(
        target.glob("*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not downloaded:
        raise RuntimeError("下载完成但找不到输出文件")

    result = downloaded[0]
    actual_mb = result.stat().st_size / 1e6

    # 4. 最终大小检查
    if actual_mb > max_mb:
        result.unlink(missing_ok=True)
        logger.warning(
            "下载后文件超限: %.1fMB > %dMB, 已删除", actual_mb, max_mb,
        )
        return None

    logger.info(
        "下载完成: %s (%.1fMB)", result.name, actual_mb,
    )
    return result
