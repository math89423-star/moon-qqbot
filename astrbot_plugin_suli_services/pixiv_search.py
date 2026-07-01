"""Pixiv 插画搜索客户端 — pixivpy3 OAuth refresh_token 认证 + 图片下载/缩放。

password login 已失效 (上游 API 变更)，唯一认证方式为 refresh_token。
用户需通过外部工具 (如 gppt) 获取 refresh_token 后存入 bot_config。

用法:
  from .pixiv_search import search_pixiv, format_pixiv_results
  from .pixiv_search import download_pixiv_image, resize_for_qq

  # 搜索
  results = await search_pixiv("艦これ", refresh_token="...", count=3)
  # 下载 (优先 url_original 真原图) + 缩放
  path = await download_pixiv_image(results[0]["url_original"], "/tmp/pixiv/")
  final = resize_for_qq(path, max_mb=10, max_dim=4096)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

logger = logging.getLogger(__name__)

# ── 限流 ────────────────────────────────────────────
# Pixiv API 搜索间隔 (秒) — search_span_limit 通常为 6,
# 低于此间隔高频触发 HTTP 429。
_SEARCH_INTERVAL: float = 6.0
_last_search_time: float = 0.0

# ── 去重 ────────────────────────────────────────────
# 最近展示过的 illust ID → 时间戳, 避免短期内重复推同一张图。
# TTL 30 分钟自动清理。
_DEDUP_TTL: float = 1800.0  # 30 分钟
_recently_shown: dict[int, float] = {}


def _cleanup_dedup() -> None:
    """清理过期的去重记录。"""
    now = time.time()
    stale = [iid for iid, ts in _recently_shown.items() if now - ts > _DEDUP_TTL]
    for iid in stale:
        del _recently_shown[iid]


def mark_illust_shown(illust_id: int) -> None:
    """标记插画已展示 (executor 发图后调用)。"""
    _recently_shown[illust_id] = time.time()
    _cleanup_dedup()


def _is_duplicate(illust_id: int) -> bool:
    """检查插画是否在去重窗口内已展示过。"""
    _cleanup_dedup()
    return illust_id in _recently_shown


# ── 权重评分 ─────────────────────────────────────────
# 综合多个信号给每张插画打分，按分排序后选 Top-N。

# 权重系数 — 相对比例决定影响力
_W_BOOKMARK = 3.0    # 收藏/点赞数 — 最强质量信号
_W_VIEW = 0.2        # 浏览数 — 弱信号 (可刷)
_W_RECENCY = 4.0     # 时效性 — 越新越高
_W_TAG_MATCH = 1.2   # 标签命中 — query 完全命中某个 tag 时加分

# 时效衰减半衰期 (天) — 超过此天数的图 recency 分值减半
_RECENCY_HALF_LIFE = 30.0  # 约 1 个月

# 去重降权系数 — 30 分钟内展示过的图分数乘以此系数
_DEDUP_PENALTY = 0.15

import math as _math


def _score_illust(item: dict, query_terms: set[str]) -> float:
    """计算单张插画的综合质量分。

    信号权重 (越新越靠前 >> 点赞越多越靠前):
      - 时效性 (created_at): 指数衰减, 半衰期 30 天 — 权重最高
      - 收藏数 (bookmark_count): log 变换 — 核心质量信号
      - 标签匹配: query 中的词完全命中了某个 tag
      - 浏览数 (view_count): 参考信号

    返回分越高 = 越优先推荐。
    """
    bookmark_count = item.get("bookmark_count", 0) or 0
    view_count = item.get("view_count", 0) or 0

    # 收藏分 — log 压缩防止头部垄断
    bm_score = _math.log(bookmark_count + 1) * _W_BOOKMARK

    # 浏览分 — 低权参考
    view_score = _math.log(view_count + 1) * _W_VIEW

    # 时效分 — 越新越高, 指数衰减
    created = item.get("created_at", "")
    recency_score = 0.0
    if created and len(created) >= 10:
        try:
            from datetime import datetime as _dt
            created_dt = _dt.strptime(created[:10], "%Y-%m-%d")
            days_ago = max(0, (time.time() - created_dt.timestamp()) / 86400.0)
            lam = _math.log(2) / _RECENCY_HALF_LIFE
            recency_score = _math.exp(-lam * days_ago) * _W_RECENCY
        except (ValueError, OSError):
            pass

    # 标签命中分
    tag_match_score = 0.0
    if query_terms:
        tags_lower = {t.lower() for t in item.get("tags", [])}
        hits = len(query_terms & tags_lower)
        if hits > 0:
            tag_match_score = hits * _W_TAG_MATCH

    return bm_score + view_score + recency_score + tag_match_score


def _rank_results(
    results: list[dict[str, Any]],
    query: str,
    count: int,
    sort: str,
) -> list[dict[str, Any]]:
    """对搜索结果去重降权 + 评分 + 排序，返回 Top-N。

    排序规则 (用户期望):
      1. 越新的图越靠前 (recency 权重最高)
      2. 点赞/收藏越多的越靠前 (bookmark 权重次高)
      3. 30 分钟内展示过的图降权 (×0.15)，不硬排除

    当 sort=popular_desc 时 Pixiv 已按热度排，评分微调不颠覆原始顺序。
    当 sort=date_desc 时评分可有效把高质量图提到前面。
    """
    query_terms = {t.lower() for t in query.split() if t}

    # 评分 + 去重降权
    scored: list[tuple[dict, float]] = []
    dup_count = 0
    for item in results:
        iid = item.get("id")
        score = _score_illust(item, query_terms)
        if iid and _is_duplicate(iid):
            score *= _DEDUP_PENALTY
            dup_count += 1
        scored.append((item, score))
    if dup_count:
        logger.debug("Pixiv 去重降权: %d 张在窗口内 (×%.2f)", dup_count, _DEDUP_PENALTY)

    if not scored:
        return []

    # 排序: 按综合分降序
    scored.sort(key=lambda x: x[1], reverse=True)

    logger.debug(
        "Pixiv 评分 Top-%d: %s",
        min(count, len(scored)),
        [(s[0].get("id"), f"{s[1]:.1f}") for s in scored[:count]],
    )

    return [item for item, _ in scored[:count]]

# ── QQ 图片限制 ────────────────────────────────────
# NapCat 走 base64 上传, 单图实测 ~10MB 以上不稳定 (发送失败/客户端转低质量).
# 留安全余量: 上限 10MB. 长边 4096px — 超过的真大图才缩 (Pixiv 原图常 1500x2000
# ~4000x6000, 大多不触发; 只有大长图/超清图才进缩放分支).
QQ_MAX_IMAGE_BYTES: int = 10 * 1024 * 1024   # 10 MB
QQ_MAX_IMAGE_DIM: int = 4096                   # 4096 px 长边

# ── 搜索参数校验 ──────────────────────────────────

_VALID_SORTS = frozenset({"date_desc", "popular_desc"})
_VALID_TARGETS = frozenset(
    {"partial_match_for_tags", "exact_match_for_tags", "title_and_caption"}
)

# ── Pixiv 图片 Referer (防盗链) ────────────────────
_PIXIV_REFERER = "https://www.pixiv.net/"


# ═══════════════════════════════════════════════════════════════
# 内部辅助
# ═══════════════════════════════════════════════════════════════

def _translate_error(exc: Exception) -> Exception:
    """将 pixivpy3 异常翻译为分类异常。"""
    msg = str(exc).lower()
    if "invalid" in msg and ("refresh" in msg or "grant" in msg):
        return PermissionError("Pixiv refresh_token 已失效，请重新获取")
    if "rate" in msg or "429" in msg:
        return RuntimeError("Pixiv API 频率超限 (429)，请稍后重试")
    if "502" in msg or "503" in msg:
        return RuntimeError("Pixiv 服务暂时不可用，请稍后重试")
    return RuntimeError(f"Pixiv 搜索失败: {exc}")


def _run_search_sync(
    refresh_token: str,
    query: str,
    sort: str,
    search_target: str,
    retries: int = 2,
) -> tuple[list[dict[str, Any]], str]:
    """同步执行 pixivpy3 搜索 (在线程池中运行)。

    Returns:
        (results, new_refresh_token) — Pixiv 每次 auth 都会轮换 refresh_token,
        旧 token 立即失效。调用方必须用 new_refresh_token 覆盖存储。

    ★ SSL/连接瞬断会自动重试。auth 只做一次 (每次 auth 轮换 token,重试时用同一个 api 实例只重试搜索)。
    """
    from pixivpy3 import AppPixivAPI  # type: ignore[import-untyped]

    # ── auth (SSL/连接瞬断最多重试 1 次; 注意: 每次 auth 轮换 token, 重试有概率失联) ──
    api = AppPixivAPI()
    for auth_attempt in range(2):
        try:
            api.auth(refresh_token=refresh_token)
            break
        except Exception as exc:
            msg = str(exc).lower()
            is_transient = any(kw in msg for kw in (
                "ssl", "eof", "connection", "timeout", "reset", "broken pipe",
            ))
            if is_transient and auth_attempt == 0:
                logger.warning("Pixiv auth 瞬断, 2s 后重试: %s", exc)
                time.sleep(2.0)
                continue
            raise _translate_error(exc) from exc

    new_token = getattr(api, "refresh_token", "") or ""

    # ── search (可重试 — 不碰 token) ──
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            result = api.search_illust(
                word=query,
                search_target=search_target,  # type: ignore[arg-type]
                sort=sort,  # type: ignore[arg-type]
                offset=0,
            )
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            is_transient = any(kw in msg for kw in (
                "ssl", "eof", "connection", "timeout", "reset", "broken pipe",
            ))
            if is_transient and attempt < retries:
                logger.warning(
                    "Pixiv 搜索瞬断 (attempt %d/%d): %s — %.1fs 后重试",
                    attempt + 1, retries + 1, exc, 2.0 * (attempt + 1),
                )
                time.sleep(2.0 * (attempt + 1))  # 2s, 4s 递增
                continue
            raise _translate_error(exc) from exc

        if isinstance(result, dict) and "error" in result:
            error_msg = result["error"].get("message", str(result["error"]))
            raise RuntimeError(f"Pixiv API 返回错误: {error_msg}")

        illusts = result.get("illusts", []) if isinstance(result, dict) else []
        return _parse_illusts(illusts), new_token

    # 所有重试耗完
    assert last_exc is not None
    raise _translate_error(last_exc) from last_exc


def _parse_illusts(illusts: list[dict]) -> list[dict[str, Any]]:
    """将 pixivpy3 返回的原始 illust dict 转换为我们需要的精简格式。"""
    results: list[dict[str, Any]] = []
    _no_original_count = 0  # 诊断: 多少张图没有 original_image_url
    for illust in illusts:
        if not illust or not isinstance(illust, dict) or illust.get("id") is None:
            continue

        image_urls = illust.get("image_urls") or {}
        user = illust.get("user") or {}
        tags = illust.get("tags") or []
        meta_sp = illust.get("meta_single_page") or {}
        meta_pages = illust.get("meta_pages") or []

        # url_original: 单页取自 meta_single_page, 多页取自 meta_pages
        url_original = ""
        if meta_sp.get("original_image_url"):
            # 单页插画
            url_original = meta_sp["original_image_url"]
        elif meta_pages:
            # 多页: collect 所有页的 original URL, 按 illust_id 随机抽一页
            page_origs: list[str] = []
            for pg in meta_pages:
                if not isinstance(pg, dict):
                    continue
                pg_urls = pg.get("image_urls") or {}
                # meta_pages[].image_urls key 是 "original" (不是 original_image_url)
                orig = pg_urls.get("original") or pg_urls.get("original_image_url") or ""
                if orig:
                    page_origs.append(orig)
            if page_origs:
                url_original = page_origs[illust["id"] % len(page_origs)]

        # 兜底: image_urls 里也可能有 original (某些 API 版本)
        if not url_original:
            url_original = image_urls.get("original") or ""

        # 诊断: 统计无原图的情况
        if not url_original:
            _no_original_count += 1
            logger.debug(
                "Pixiv illust %d 无 original URL — meta_single_page=%s meta_pages_count=%d image_urls_keys=%s",
                illust["id"],
                "present" if meta_sp else "empty",
                len(meta_pages),
                list(image_urls.keys())[:5] if image_urls else [],
            )

        results.append({
            "id": illust["id"],
            "title": illust.get("title", "") or "",
            "author": user.get("name", "未知"),
            "author_id": user.get("id", 0),
            "tags": [t.get("name", "") for t in tags if t.get("name")],
            "url_medium": image_urls.get("medium", ""),
            "url_large": image_urls.get("large", ""),
            "url_original": url_original,
            "page_count": illust.get("page_count", 1),
            "bookmark_count": illust.get("total_bookmarks", 0),
            "view_count": illust.get("total_view", 0),
            "created_at": illust.get("create_date", ""),
            "caption": str(illust.get("caption", "") or "")[:300],
            "is_r18": illust.get("x_restrict", 0) >= 1,
        })

    if _no_original_count > 0:
        logger.info(
            "Pixiv _parse_illusts: %d/%d 张图无 original_image_url (回退到 url_large)",
            _no_original_count, len(results),
        )
    return results


# ═══════════════════════════════════════════════════════════════
# 图片下载 + 缩放
# ═══════════════════════════════════════════════════════════════

async def download_pixiv_image(url: str, dest_dir: str | Path) -> str:
    """从 Pixiv 下载单张图片到本地目录。

    Pixiv 图片需要 Referer: https://www.pixiv.net/ 否则返回 403。
    使用 aiohttp 异步下载，文件名取 URL 最后一段 (含扩展名)。

    Args:
        url: Pixiv 图片 URL (如 i.pximg.net/c/600x1200/img-master/...)
        dest_dir: 保存目录

    Returns:
        本地文件绝对路径

    Raises:
        ValueError: URL 为空
        RuntimeError: 下载失败 (HTTP 非 200 / 网络异常)
    """
    import aiohttp

    if not url:
        raise ValueError("图片 URL 不能为空")

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    # 从 URL 提取文件名
    url_path = url.split("?")[0]
    fname = os.path.basename(url_path) or f"pixiv_{int(time.time())}.jpg"
    # 确保有合理扩展名
    if "." not in fname.rsplit("/", 1)[-1]:
        fname += ".jpg"
    dest_path = dest / fname

    headers = {
        "Referer": _PIXIV_REFERER,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }

    # 获取代理 (aiohttp 不自动读 HTTP_PROXY 环境变量)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None

    try:
        async with aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            async with session.get(url, proxy=proxy) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"Pixiv 图片下载失败: HTTP {resp.status}"
                    )
                data = await resp.read()
    except aiohttp.ClientError as e:
        raise RuntimeError(f"Pixiv 图片下载网络异常: {e}") from e

    dest_path.write_bytes(data)
    size_kb = len(data) / 1024
    logger.info(
        "Pixiv 图片已下载: %s (%.1f KB) → %s", url[:80], size_kb, dest_path
    )
    return str(dest_path)


def resize_for_qq(
    image_path: str | Path,
    max_bytes: int = QQ_MAX_IMAGE_BYTES,
    max_dim: int = QQ_MAX_IMAGE_DIM,
) -> str:
    """检查图片是否超过 QQ 限制，超过则缩放/压缩。

    策略 (尽量保留原图质量, 同时保证 NapCat 稳定传输 ≤10MB):
      1. 尺寸 > max_dim → 等比缩放到 max_dim (LANCZOS, 保留原格式)
      2. 文件 > max_bytes:
         - JPEG: 递降 quality 92→85→75 (止于 75, 再低视觉损失明显)
         - PNG 巨图 (常 15-20MB): 转 JPEG q85 — PNG optimize 压不动, 保留 PNG 会爆 NapCat
         - GIF/WebP 动图: 保留原格式不动 (避免动图变静图); 超字节由尺寸缩放兜底

    注意: 此函数直接修改原文件。
    """
    from PIL import Image

    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"图片不存在: {path}")

    file_size = path.stat().st_size
    with Image.open(path) as im:
        w, h = im.size
        orig_format = (im.format or "JPEG").upper()

    # 快速路径: 都合格直接返回
    if file_size <= max_bytes and max(w, h) <= max_dim:
        logger.debug("Pixiv 图片无需处理: %s (%dx%d, %.1fMB)", path.name, w, h, file_size / 1e6)
        return str(path)

    img = Image.open(path)
    orig_w, orig_h = img.size
    logger.info(
        "Pixiv 图片需处理: %s (%dx%d, %.1fMB, fmt=%s)",
        path.name, orig_w, orig_h, file_size / 1e6, orig_format,
    )

    # ── 阶段 1: 尺寸缩放 (保留原格式) ──
    if max(orig_w, orig_h) > max_dim:
        ratio = max_dim / max(orig_w, orig_h)
        new_w, new_h = int(orig_w * ratio), int(orig_h * ratio)
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        logger.debug("  尺寸: %dx%d → %dx%d", orig_w, orig_h, new_w, new_h)

    # GIF/WebP 动图: 保留原格式, 不压字节 (避免动图变静图); 仅靠尺寸缩放兜底
    if orig_format in ("GIF", "WEBP"):
        img.save(path, format=orig_format)
        logger.info(
            "  完成(动图保留): %dx%d, %.1fMB → %.1fMB (format=%s)",
            img.size[0], img.size[1], file_size / 1e6, path.stat().st_size / 1e6, orig_format,
        )
        return str(path)

    # ── 阶段 2: 文件大小压缩 ──
    # PNG (非动图): 尺寸缩放后仍超字节 → 转 JPEG q85 (PNG optimize 压不动, 原图 15-20MB 会爆 NapCat)
    if orig_format == "PNG":
        img.save(path, format="PNG", optimize=True)
        if path.stat().st_size <= max_bytes:
            logger.info("  完成: PNG, %.1fMB → %.1fMB", file_size / 1e6, path.stat().st_size / 1e6)
            return str(path)
        # PNG 超字节 → 转 JPEG (alpha 通道敷白底)
        img = _flatten_to_rgb(img)
        img.save(path, format="JPEG", quality=85, optimize=True)
        logger.info("  完成: PNG→JPEG q85, %.1fMB → %.1fMB", file_size / 1e6, path.stat().st_size / 1e6)
        return str(path)

    # JPEG: 递降 quality, 止于 75 (再低视觉损失明显)
    return _save_jpeg_bounded(img, path, max_bytes, file_size)


def _flatten_to_rgb(img: Any) -> Any:
    """将带 alpha/palette 的图像压平到 RGB 白底 (供转 JPEG 用)."""
    from PIL import Image

    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB")


def _save_jpeg_bounded(
    img: Any, path: Path, max_bytes: int, orig_file_size: int,
) -> str:
    """JPEG 递降 quality 阶梯压缩; 仍超字节则缩到 2400px 兜底再存 q75.

    quality 阶梯 92→85→75, 止于 75 (再低视觉损失明显).
    """
    for quality in (92, 85, 75):
        img.save(path, format="JPEG", quality=quality, optimize=True)
        if path.stat().st_size <= max_bytes:
            logger.info(
                "  完成: JPEG q=%d, %.1fMB → %.1fMB",
                quality, orig_file_size / 1e6, path.stat().st_size / 1e6,
            )
            return str(path)

    # 兜底: 缩到 2400px 再 q75
    from PIL import Image as _Img
    if max(img.size) > 2400:
        ratio = 2400 / max(img.size)
        img = img.resize(
            (int(img.size[0] * ratio), int(img.size[1] * ratio)),
            _Img.Resampling.LANCZOS,
        )
    img.save(path, format="JPEG", quality=75, optimize=True)
    logger.warning("  JPEG 兜底压缩 q75+缩2400: %.1fMB", path.stat().st_size / 1e6)
    return str(path)


# ═══════════════════════════════════════════════════════════════
# 搜索 API
# ═══════════════════════════════════════════════════════════════

async def search_pixiv(
    query: str,
    refresh_token: str,
    sort: str = "date_desc",
    count: int = 3,
    search_target: str = "partial_match_for_tags",
    on_token_rotated: Callable[[str], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    """异步 Pixiv 插画搜索。

    Args:
        query: 搜索关键词/标签 (日/中/英文, 空格分隔 = AND)
        refresh_token: Pixiv OAuth refresh_token
        sort: "date_desc" (最新) 或 "popular_desc" (热门 — 需 Premium)
        count: 返回条数 (1-5)
        search_target: 匹配策略
        on_token_rotated: 可选回调，auth 后 Pixiv 下发新 refresh_token 时调用。
                          签名: async def(new_token: str) -> None。

    Returns:
        [dict] 每条含 id/title/author/tags/url_*/bookmark_count/view_count/
               created_at/caption/is_r18。失败时返回空列表。

    Raises:
        ValueError: 参数校验失败
        PermissionError: refresh_token 失效
        RuntimeError: API 错误/限流
    """
    if not query.strip():
        raise ValueError("搜索关键词不能为空")
    if not refresh_token:
        raise PermissionError("Pixiv refresh_token 未配置")
    if count < 1 or count > 5:
        raise ValueError(f"count 必须在 1-5 之间, 收到 {count}")
    if sort not in _VALID_SORTS:
        raise ValueError(f"sort 必须是 date_desc 或 popular_desc, 收到 {sort!r}")
    if search_target not in _VALID_TARGETS:
        raise ValueError(
            f"search_target 必须是 partial_match_for_tags / "
            f"exact_match_for_tags / title_and_caption, 收到 {search_target!r}"
        )

    # ── 限流 ──
    global _last_search_time
    elapsed = time.time() - _last_search_time
    if elapsed < _SEARCH_INTERVAL:
        wait = _SEARCH_INTERVAL - elapsed
        logger.debug("Pixiv 搜索限流: 距上次搜索 %.1f 秒, 等待 %.1f 秒", elapsed, wait)
        await asyncio.sleep(wait)

    # ── 在线程池中执行同步 pixivpy3 调用 ──
    # 多拉结果 (×5) 供评分 + R-18 过滤
    fetch_count = max(count * 5, 30)
    new_token = ""

    async def _do_search(q: str, target: str):
        nonlocal new_token
        raw, tok = await asyncio.to_thread(
            _run_search_sync, refresh_token, q, sort, target,
        )
        new_token = tok
        return raw

    raw_results: list[dict] = []
    try:
        raw_results = await _do_search(query, search_target)
        _last_search_time = time.time()
    except (ValueError, PermissionError, RuntimeError):
        raise
    except Exception as exc:
        logger.error("Pixiv 搜索未预期异常: %s", exc, exc_info=True)
        raise _translate_error(exc) from exc

    # ── 兜底: 首次搜索无结果时，用更宽松策略重试 ──
    if len(raw_results) < count and search_target != "title_and_caption":
        logger.info(
            "Pixiv 首搜结果不足 (got=%d < want=%d), 用 title_and_caption 重试",
            len(raw_results), count,
        )
        await asyncio.sleep(1.5)  # 短暂间隔防限流
        try:
            fallback_results = await _do_search(query, "title_and_caption")
            # 合并去重
            seen_ids = {r.get("id") for r in raw_results}
            for r in fallback_results:
                if r.get("id") not in seen_ids:
                    raw_results.append(r)
                    seen_ids.add(r.get("id"))
            _last_search_time = time.time()
            logger.info("Pixiv 兜底搜索补充 %d 条, 共 %d", len(fallback_results), len(raw_results))
        except Exception:
            logger.exception("Pixiv 兜底搜索失败, 仅用首搜结果")

    # Pixiv 每次 auth 都轮换 refresh_token — 旧 token 立即失效
    if new_token and new_token != refresh_token and on_token_rotated is not None:
        try:
            await on_token_rotated(new_token)
        except Exception:
            logger.exception("on_token_rotated 回调失败 — 下次搜索将因旧 token 失效而报错")

    # ── 内容安全过滤: 在数据层排除 R-18 图像 ──
    # 必须在评分/排序前过滤，确保 R-18 永不进入下游任何链路。
    safe_results = [r for r in raw_results if not r.get("is_r18")]
    r18_count = len(raw_results) - len(safe_results)
    if r18_count > 0:
        logger.info(
            "Pixiv 内容过滤: 排除 %d 张 R-18 (query=%s, total=%d)",
            r18_count, query[:40], len(raw_results),
        )
    raw_results = safe_results

    # 评分 + 去重降权 + 排序 → Top-N
    return _rank_results(raw_results, query, count, sort)


def format_pixiv_results(results: list[dict[str, Any]], query: str) -> str:
    """将 Pixiv 搜索结果格式化为 LLM 可读文本 (含图片已发送提示)。

    与 download + send 配套使用: 当 executor 已发送图片后，此函数返回简洁的
    插画信息文本供 LLM 在回复中引用。
    """
    if not results:
        return f"🎨 未在 Pixiv 上找到与「{query}」相关的插画。"

    lines = [
        f"🎨 已从 Pixiv 为你找到 {len(results)} 张关于「{query}」的插画 ↓",
    ]

    for i, item in enumerate(results, 1):
        tags_str = ", ".join(item["tags"][:6])
        created = item.get("created_at", "")
        if created and len(created) >= 10:
            created = created[:10]

        lines.append(
            f"\n  {i}.「{item['title']}」by {item['author']}"
        )
        if tags_str:
            lines.append(f"     标签: {tags_str}")
        lines.append(
            f"     ❤️{item['bookmark_count']}收藏  👁️{item['view_count']}浏览"
            f"  pixiv.net/artworks/{item['id']}"
        )
        if created:
            lines.append(f"     创建: {created}")

    return "\n".join(lines)
