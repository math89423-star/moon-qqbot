"""VLM 图片解析模块 — 下载 QQ 图片 → VLM 描述 → 注入对话上下文。

设计:
  - 从 OneBot V11 的 image segment 提取 URL，下载后 base64 编码
  - 调用活跃 VLM (从 bot_config 读取) 做图片描述
  - 结果以文本形式注入对话，让 LLM 能"看到"图片内容
  - 同时提供 describe_image 工具供 LLM function calling 使用

用法:
  from .vision import describe_image_from_url, has_active_vlm

  if has_active_vlm():
      desc = await describe_image_from_url("http://gchat.qpic.cn/...")
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import re
import time
from typing import Optional

import aiohttp
from PIL import Image

logger = logging.getLogger(__name__)

# 图片下载最大尺寸 (bytes) — 5MB，超过自动 resize 压缩
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# VLM 图片最大尺寸 — 3MB，超过则在 SHA256 之后 resize (保护缓存一致性)
VLM_MAX_IMAGE_BYTES = 3 * 1024 * 1024

# resize 后最小长边像素 (保持可辨识)
RESIZE_MIN_LONG_SIDE = 512

# JPEG 渐进压缩质量步进
RESIZE_QUALITY_STEPS = [85, 70, 55, 40]

# VLM 请求超时 (1024 tokens @ ~30 tok/s ≈ 35s, 留余量)
# VLM API 总超时 (秒) — aiohttp 层
VLM_TIMEOUT = 20.0  # 直连 api.vectorengine.ai 实测 ~9s, 20s 充裕
# asyncio.wait_for 硬超时 — 防止 aiohttp 超时失效时连接池耗尽 (TRAPS §四)
VLM_HARD_TIMEOUT = 25.0

# VLM 描述最大 token (详细描述需要更多空间)
# 旧值 2048 不够: 新 prompt 输入 ~3000 token + 要求逐项详细输出 + 文字逐字记录 → 频繁截断
VLM_MAX_TOKENS = 4096

# 单次最多解析图片数 — VLM 调用昂贵，超过此数礼貌拒绝
MAX_VLM_IMAGES = 2

# ═══════════════════════════════════════════════════════════════
# VLM 结果缓存 — SHA-256 字节级精确匹配，跨 bot 共享
# ═══════════════════════════════════════════════════════════════

#   1. SHA-256 做 key — 字节级精确匹配，零误判。命中的是"同一个文件被反复发"
#      (表情包常见场景)，不是"同一个梗的不同版本"(那需要 pHash，第二阶段)
#   2. 缓存只存 VLM 客观描述 (图片里有什么) — 这是少数该真共享的状态之一
#      绝不存 bot 对图的反应/解读 (那是 per-bot 的下游步骤，否则人格污染)
#   3. 持久化到磁盘 JSON — 重启不丢、天然支持未来拆进程后仍共享
#   4. 容量上限 500 条 + LRU 淘汰 + 7 天 TTL — 表情包描述不过期但空间有限
#   5. hit_count 计数器 — 为后续"对见过的表情包有反应"提供熟悉度数据

_VLM_CACHE_MAX_ENTRIES = 500
_VLM_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 天
_VLM_CACHE_SAVE_INTERVAL = 10  # 每 N 次写入触发一次磁盘保存

# 内存缓存: {sha256_hex: {"description": str, "timestamp": float, "hit_count": int, "last_hit": float}}
_vlm_cache: dict[str, dict] = {}
_vlm_cache_loaded = False
_vlm_cache_write_count = 0
_vlm_cache_dir: str | None = None


def init_vlm_cache_dir(cache_dir: str) -> None:
    """设置 VLM 缓存目录 (由宿主插件在启动时调用)。

    应在 AstrBot data 目录下创建，如 data/vlm_cache/。
    """
    global _vlm_cache_dir
    _vlm_cache_dir = cache_dir
    os.makedirs(cache_dir, exist_ok=True)


def _get_vlm_cache_path() -> str:
    """获取 VLM 缓存 JSON 文件路径。"""
    if _vlm_cache_dir is not None:
        return os.path.join(_vlm_cache_dir, "vlm_descriptions.json")
    # per-plugin 数据目录
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        data_dir = str(Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_suli_services")
    except Exception:
        data_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "plugin_data", "astrbot_plugin_suli_services")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "vlm_descriptions.json")


def _load_vlm_cache() -> dict[str, dict]:
    """从磁盘加载 VLM 缓存 (惰性加载，首次调用时触发)。"""
    global _vlm_cache, _vlm_cache_loaded
    if _vlm_cache_loaded:
        return _vlm_cache
    _vlm_cache_loaded = True

    cache_path = _get_vlm_cache_path()
    try:
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # 清理过期条目
                now = time.time()
                expired = [
                    k for k, v in data.items()
                    if now - v.get("timestamp", 0) > _VLM_CACHE_TTL_SECONDS
                ]
                for k in expired:
                    del data[k]
                if expired:
                    logger.info("VLM 缓存清理 %d 条过期记录 (TTL=%d天)", len(expired), _VLM_CACHE_TTL_SECONDS // 86400)
                _vlm_cache = data
                logger.info("VLM 缓存已加载: %d 条", len(_vlm_cache))
                return _vlm_cache
    except Exception:
        logger.warning("VLM 缓存加载失败，使用空缓存", exc_info=True)

    _vlm_cache = {}
    return _vlm_cache


def _save_vlm_cache() -> None:
    """保存 VLM 缓存到磁盘 (批量写入，每 N 次写触发一次)。"""
    cache_path = _get_vlm_cache_path()
    try:
        cache_dir = os.path.dirname(cache_path)
        os.makedirs(cache_dir, exist_ok=True)
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_vlm_cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, cache_path)  # 原子替换，防止写入中途崩溃损坏文件
    except Exception:
        logger.warning("VLM 缓存保存失败", exc_info=True)


def _compute_image_sha256(image_data: bytes) -> str:
    """计算图片字节的 SHA-256 哈希 (hex 字符串)。"""
    return hashlib.sha256(image_data).hexdigest()


def _get_cached_vlm_result(image_data: bytes, user_query: str = "") -> str | None:
    """查询 VLM 缓存 — 从图片字节计算 SHA256，委托 _get_cached_vlm_result_by_key。"""
    sha256 = _compute_image_sha256(image_data)
    cache_key = sha256
    if user_query and user_query.strip():
        query_hash = hashlib.sha1(user_query.strip().encode("utf-8", errors="ignore")).hexdigest()[:20]
        cache_key = f"{sha256}:q{query_hash}"
    return _get_cached_vlm_result_by_key(cache_key)


def _get_cached_vlm_result_by_key(cache_key: str) -> str | None:
    """通过预计算缓存键查询 VLM 缓存 (跳过 SHA256 计算)。

    副作用: 命中时更新 hit_count 和 last_hit (用于 LRU 淘汰优先级)。
    """
    cache = _load_vlm_cache()
    entry = cache.get(cache_key)
    if entry is None:
        return None

    # TTL 检查
    if time.time() - entry.get("timestamp", 0) > _VLM_CACHE_TTL_SECONDS:
        del cache[cache_key]
        return None

    # 更新命中统计
    entry["hit_count"] = entry.get("hit_count", 0) + 1
    entry["last_hit"] = time.time()
    logger.info(
        "VLM 缓存命中: key=%s... hit_count=%d (跳过 API 调用)",
        cache_key[:24], entry["hit_count"],
    )
    return entry.get("description", "")


def _set_cached_vlm_result(image_data: bytes, description: str, user_query: str = "") -> None:
    """写入 VLM 缓存 — 存储 VLM 返回的描述文本。

    Args:
        image_data: 发送给 VLM 的已处理图片字节 (用于计算 SHA-256)
        description: VLM 返回的描述文本
        user_query: 用户对图片的具体提问 (为空=通用描述缓存)
    """
    global _vlm_cache_write_count
    cache = _load_vlm_cache()
    sha256 = _compute_image_sha256(image_data)
    cache_key = sha256
    if user_query and user_query.strip():
        query_hash = hashlib.sha1(user_query.strip().encode("utf-8", errors="ignore")).hexdigest()[:20]
        cache_key = f"{sha256}:q{query_hash}"
    return _set_cached_vlm_result_by_key(cache_key, description)


def _set_cached_vlm_result_by_key(cache_key: str, description: str) -> None:
    """通过预计算缓存键写入 VLM 缓存 (跳过 SHA256 计算)。"""
    global _vlm_cache_write_count
    cache = _load_vlm_cache()

    # 容量上限 → LRU 淘汰 (按 last_hit 升序，淘汰最不活跃的 10%)
    if len(cache) >= _VLM_CACHE_MAX_ENTRIES and cache_key not in cache:
        sorted_entries = sorted(
            cache.items(),
            key=lambda x: x[1].get("last_hit", x[1].get("timestamp", 0)),
        )
        evict_count = max(1, int(_VLM_CACHE_MAX_ENTRIES * 0.1))
        for old_key, _ in sorted_entries[:evict_count]:
            del cache[old_key]
        logger.info("VLM 缓存 LRU 淘汰: %d 条 (容量=%d)", evict_count, _VLM_CACHE_MAX_ENTRIES)

    cache[cache_key] = {
        "description": description,
        "timestamp": time.time(),
        "hit_count": 1,
        "last_hit": time.time(),
    }

    # 批量保存: 每 N 次写入触发一次磁盘持久化
    _vlm_cache_write_count += 1
    if _vlm_cache_write_count % _VLM_CACHE_SAVE_INTERVAL == 0:
        _save_vlm_cache()

    logger.info("VLM 缓存写入: key=%s... desc_len=%d", cache_key[:24], len(description))


def get_vlm_cache_stats() -> dict:
    """返回 VLM 缓存统计信息 (供管理面板展示)。"""
    cache = _load_vlm_cache()
    now = time.time()
    total = len(cache)
    total_hits = sum(v.get("hit_count", 0) for v in cache.values())
    high_hit = sum(1 for v in cache.values() if v.get("hit_count", 0) > 10)
    # 最近 24h 内新增的条目
    recent = sum(1 for v in cache.values() if now - v.get("timestamp", 0) < 86400)
    return {
        "total_entries": total,
        "max_entries": _VLM_CACHE_MAX_ENTRIES,
        "total_hits": total_hits,
        "high_hit_entries": high_hit,  # 命中 >10 次的"群内常见表情包"
        "recent_entries_24h": recent,
        "ttl_days": _VLM_CACHE_TTL_SECONDS // 86400,
    }


def detect_image_intent(text: str) -> bool:
    """检测用户消息是否显式表达了让 bot 看图/分析图的意图。

    设计原则:
      - 只有用户明确想让 bot 识别图片时才返回 True
      - 随便发张表情包/梗图不触发 — 避免无效 VLM 调用
      - 关键词覆盖: 直接请求、询问图片内容、分析/识别命令

    Args:
        text: 用户消息的纯文本部分 (不含图片描述注入)

    Returns:
        True 表示用户想要 bot 看/分析图片
    """
    if not text or not text.strip():
        return False

    lower = text.lower()

    # ── 强信号: 命中任意一个即认为用户有意图 ──
    strong_patterns = [
        # 直接请求看图
        "看看这张", "看看这个图", "看看图片", "看这张图",
        "看一下这张", "看一下这个", "看下这张", "看下这个",
        "看下这个图", "看下这张图", "看下这图",
        "看下图", "看看图", "看看这个",
        "来看看", "都来看", "也来看",  # 口语化看图请求
        "这是什么图", "这个图", "这张图",
        "看图说话", "你看到", "你看见",
        # 来看/看下你的... (如"来看下你的新头像")
        "来看下", "看下你的", "看看你的", "瞧瞧你的",
        "看你头像", "看你新头像", "看下你头像",
        # 询问图片内容
        "这是什么", "这是啥", "什么图", "图里是什么",
        "图片里是什么", "图中是什么", "这里面是什么",
        "里面是什么", "这人是谁", "这是谁", "这是哪",
        # 分析/识别请求
        "分析这张", "分析一下这张", "描述这张", "识别这张",
        "识别一下", "解读这张", "讲解一下这张",
        # 帮助请求
        "帮我看看", "帮我看下", "帮我分析", "帮我识别",
        "帮我解读", "帮我描述",
        # 能力询问
        "你能看图", "你能识别", "你能看出", "你看得出",
        "你认得", "你认识这", "你能辨认",
        # 命令式
        "看一下图", "看一眼", "瞧瞧这张", "瞧瞧这个",
        "看这张照片", "看看这个图",
        # 翻译/OCR
        "翻译图", "提取文字", "图里写了什么", "图片写了什么",
        "写了什么字",
        # 追问图片
        "图里", "图中", "图片上", "照片里",
        # 识别能力询问
        "能识别吗", "能看出来吗", "看得出吗", "能认出来吗",
        "你知道这是啥", "认得出吗",
        # 梗/Meme 分析
        "这是什么梗", "这什么梗", "分析这个梗",
        # 指向性指令 (需配合图片)
        "你看这个", "你看这张", "看这个图", "看这张图",
        # 猜测/辨识
        "猜猜这是什么", "猜猜这是谁", "这是什么角色",
        "这是在画什么", "这是画的什么",
        # 二次元/角色辨认
        "这是哪个角色", "这是什么角色", "这角色是谁",
        # 图片内容直接提问
        "这是什么图片", "这是啥图片", "这图是啥",
    ]

    for pat in strong_patterns:
        if pat in lower:
            return True

    # ── 弱信号: 需要图片相关上下文才触发 ──
    # 单独 "看看" / "你看" 太常见 (如 "你看看这个方案")，不触发
    # 但如果消息极短且含 "图" 字，可能是 "什么图" 的变体
    weak_patterns = [
        "这是p的", "这图", "这照片", "这图里",
    ]
    for pat in weak_patterns:
        if pat in lower:
            return True

    return False


def detect_reverse_prompt_intent(text: str) -> bool:
    """检测用户是否要求反推图片的生成提示词。

    识别模式:
      - 直接要求: "反推提示词" "反推生成提示词" "反推prompt"
      - 间接要求: "这图的提示词是什么" "怎么写prompt" "生成这张图的提示词"
      - 命令式: "给我提示词" "写个prompt" "还原提示词"
      - 自然语言版: "要自然语言版本的" (配合反推)

    Args:
        text: 用户消息的纯文本部分

    Returns:
        True 表示用户要求从图片反推生成提示词
    """
    if not text or not text.strip():
        return False

    lower = text.lower()

    # 核心信号: "反推" + 提示词/prompt
    reverse_patterns = [
        "反推提示词", "反推生成提示词", "反推prompt",
        "反推一下提示词", "反推下提示词",
        "反推她的提示词", "反推他的提示词", "反推这张图的提示词",
        "反推自然语言", "反推出提示词",
    ]
    for pat in reverse_patterns:
        if pat in lower:
            return True

    # "反推" 单独出现 + 图片上下文
    if "反推" in lower:
        # 检查是否有图片/提示词相关上下文
        context_signals = ["提示词", "prompt", "生成", "这张图", "那个图", "图片", "tag", "tags"]
        for sig in context_signals:
            if sig in lower:
                return True

    # "提示词" + 请求动作
    prompt_request_patterns = [
        "给我提示词", "给个提示词", "写个提示词", "写提示词",
        "这图的提示词", "这张图的提示词", "图片的提示词",
        "生成提示词", "还原提示词", "提取提示词",
        "要自然语言版本", "要自然语言版",
        "推测提示词", "猜猜提示词", "推断提示词",
        "这个的prompt", "这张的prompt",
        "能反推", "能不能反推", "可以反推",
    ]
    for pat in prompt_request_patterns:
        if pat in lower:
            return True

    # 组合信号: 图片 + 提示词 (跨距离匹配)
    has_image_ref = any(w in lower for w in ["这张图", "这图", "那个图", "图片", "照片", "插画", "同人图"])
    has_prompt_ref = any(w in lower for w in ["提示词", "prompt", "tag", "怎么生成", "如何生成", "怎么写"])
    if has_image_ref and has_prompt_ref:
        return True

    return False


# ── VLM 配置注入 ──────────────────────────────────
# 默认从环境变量读取, 宿主插件可通过 init_vlm_provider() 注入自定义逻辑

_vlm_config_provider: callable | None = None  # () → dict | None

# ── VLM 用量追踪 ──────────
# 此前 VLM 的 token 消耗完全不可见——只在 log 中出现，不写 token_usage 表。
# 以下模块级变量让调用方可以在 VLM 完成后读取用量并写入统一统计。

_vlm_last_usage: dict = {}  # {"input_tokens": int, "output_tokens": int, "model": str, "provider": str}


def get_last_vlm_usage() -> dict:
    """读取最近一次 (或累计多次) VLM 调用的 token 用量。

    Returns:
        {"input_tokens": int, "output_tokens": int, "model": str, "provider": str}
        无数据时返回空 dict。
    """
    return dict(_vlm_last_usage)


def _reset_vlm_usage() -> None:
    """重置 VLM 用量累计 (每次群/私聊消息开始前调用)。"""
    _vlm_last_usage.clear()


def init_vlm_provider(provider: callable) -> None:
    """注入 VLM 配置提供器 (由宿主插件在启动时调用)。

    provider 签名: () → dict | None
      返回 dict: {"api_base": str, "api_key": str, "model_name": str, "provider": str}
      返回 None: VLM 不可用

    suli_tavern 应调用:
        from astrbot_plugin_suli_services.vision import init_vlm_provider
        init_vlm_provider(lambda: get_config_service().resolve_active_vlm())
    """
    global _vlm_config_provider
    _vlm_config_provider = provider


def _resolve_vlm_config_from_env() -> Optional[dict]:
    """从环境变量解析 VLM 配置 (默认 fallback)。"""
    api_base = os.environ.get("OPENAI_BASE_URL") or os.environ.get("VLM_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("VLM_API_KEY")
    model = os.environ.get("VLM_MODEL_NAME") or os.environ.get("OPENAI_MODEL")
    if api_base and api_key and model:
        return {
            "api_base": api_base,
            "api_key": api_key,
            "model_name": model,
            "provider": "env",
        }
    return None


def has_active_vlm() -> bool:
    """检查是否有可用的 VLM 配置。"""
    try:
        if _vlm_config_provider is not None:
            return _vlm_config_provider() is not None
        return _resolve_vlm_config_from_env() is not None
    except Exception:
        return False


def _get_vlm_config() -> Optional[dict]:
    """获取活跃 VLM 的配置信息。"""
    try:
        if _vlm_config_provider is not None:
            vlm = _vlm_config_provider()
            if vlm is not None:
                return {
                    "api_base": vlm.base_url if hasattr(vlm, 'base_url') else vlm.get("api_base", ""),
                    "api_key": vlm.api_key if hasattr(vlm, 'api_key') else vlm.get("api_key", ""),
                    "model_name": vlm.model_name if hasattr(vlm, 'model_name') else vlm.get("model_name", ""),
                    "provider": vlm.provider if hasattr(vlm, 'provider') else vlm.get("provider", "custom"),
                }
            return None
        return _resolve_vlm_config_from_env()
    except Exception:
        logger.warning("无法获取 VLM 配置", exc_info=True)
        return None


def _detect_image_format(data: bytes) -> str:
    """通过文件签名检测图片格式，替代 Python 3.13 中已移除的 imghdr。"""
    if len(data) < 12:
        return "jpeg"
    h = data[:12]
    if h[:4] == b"\x89PNG":
        return "png"
    if h[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if h[:4] == b"GIF8":
        return "gif"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "webp"
    if h[:2] == b"BM":
        return "bmp"
    return "jpeg"  # 默认当作 JPEG


def _to_static_jpeg(data: bytes, img_format: str = "jpeg") -> bytes:
    """将任意图片转为静态 JPEG，消除动画（GIF/APNG）避免 VLM 处理多帧超时。

    QQ mface（动画贴图）是 GIF 或 APNG 格式，包含数十帧，
    直接 base64 发给 VLM 会导致请求超时或模型困惑。
    此函数始终提取第一帧并编码为 JPEG。

    Args:
        data: 原始图片字节
        img_format: 原始格式标识

    Returns:
        JPEG 字节 (第一帧静态图)
    """
    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        logger.warning("无法解析图片格式，返回原始数据")
        return data

    # GIF/APNG: 只取第一帧
    if img_format in ("gif", "png"):
        try:
            img.seek(0)
        except Exception:
            pass

    # 统一转 RGB
    if img.mode in ("RGBA", "P", "LA", "PA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        if img.mode in ("RGBA", "LA", "PA"):
            background.paste(
                img, (0, 0),
                img if img.mode == "LA" else img.split()[-1],
            )
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    result = buf.getvalue()
    logger.debug(
        "图片转静态 JPEG: %s %d bytes → JPEG %d bytes",
        img_format, len(data), len(result),
    )
    return result


def _resize_image(data: bytes, img_format: str = "jpeg") -> bytes:
    """将图片压缩到 MAX_IMAGE_BYTES 以下。

    策略:
      1. JPEG/WebP: 渐进降低 quality 直到符合限制
      2. PNG/BMP: 先尝试转换格式为 JPEG (更小)，再降 quality
      3. 若 quality 降至最低仍超限: 等比缩小尺寸 (0.75x → 0.5x)
      4. 兜底: 长边不低于 RESIZE_MIN_LONG_SIDE

    Args:
        data: 原始图片字节
        img_format: 原始格式标识 (jpeg/png/webp/bmp/gif)

    Returns:
        压缩后的图片字节 (保证 ≤ MAX_IMAGE_BYTES 或尽力接近)
    """
    original_size = len(data)
    if original_size <= MAX_IMAGE_BYTES:
        return data

    logger.info(
        "图片过大 (%d bytes > %d bytes)，开始压缩...",
        original_size, MAX_IMAGE_BYTES,
    )

    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        logger.warning("无法解析图片格式，跳过压缩")
        return data

    # GIF 动图: 只取第一帧
    if img_format == "gif":
        try:
            img.seek(0)
        except Exception:
            pass

    # 统一转 RGB (RGBA → RGB，避免保存 JPEG 报错)
    if img.mode in ("RGBA", "P", "LA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        background.paste(
            img, (0, 0),
            img if img.mode != "RGBA" else img.split()[-1],
        )
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # 确定保存格式: PNG/BMP → JPEG; JPEG/WebP 保持不变
    save_format = "JPEG" if img_format in ("png", "bmp") else "JPEG"

    width, height = img.size
    scales = [1.0, 0.75, 0.5]  # 尺寸缩放序列

    for scale in scales:
        if scale < 1.0:
            new_w = max(int(width * scale), RESIZE_MIN_LONG_SIDE)
            new_h = max(int(height * scale), RESIZE_MIN_LONG_SIDE)
            # 保持宽高比
            ratio = min(new_w / width, new_h / height)
            new_w = max(int(width * ratio), RESIZE_MIN_LONG_SIDE)
            new_h = max(int(height * ratio), RESIZE_MIN_LONG_SIDE)
            img_resized = img.resize((new_w, new_h), Image.LANCZOS)
            logger.debug("尺寸缩放: %dx%d → %dx%d", width, height, new_w, new_h)
        else:
            img_resized = img

        for quality in RESIZE_QUALITY_STEPS:
            buf = io.BytesIO()
            img_resized.save(buf, format=save_format, quality=quality, optimize=True)
            result = buf.getvalue()
            if len(result) <= MAX_IMAGE_BYTES:
                logger.info(
                    "图片压缩成功: %d → %d bytes (scale=%.0f%%, quality=%d)",
                    original_size, len(result), scale * 100, quality,
                )
                return result

    # 最终兜底: 最小尺寸 + 最低 quality
    final_w = max(int(width * 0.4), RESIZE_MIN_LONG_SIDE)
    final_h = max(int(height * 0.4), RESIZE_MIN_LONG_SIDE)
    ratio = min(final_w / width, final_h / height)
    final_w = max(int(width * ratio), RESIZE_MIN_LONG_SIDE)
    final_h = max(int(height * ratio), RESIZE_MIN_LONG_SIDE)
    img_final = img.resize((final_w, final_h), Image.LANCZOS)
    buf = io.BytesIO()
    img_final.save(buf, format=save_format, quality=RESIZE_QUALITY_STEPS[-1], optimize=True)
    result = buf.getvalue()
    logger.warning(
        "图片压缩至最低: %d → %d bytes (%dx%d, quality=%d)",
        original_size, len(result), final_w, final_h, RESIZE_QUALITY_STEPS[-1],
    )
    return result


async def _download_image(url: str, bot=None, file_id: str = "") -> Optional[bytes]:
    """从 URL 下载图片，返回压缩后的字节 (≤ 5MB)。

    优先使用 OneBot get_image API (绕过 CDN rkey 鉴权)，
    file_id 为空时 fallback 到 HTTP 下载。
    """
    # ── 路径 1: OneBot get_image API (无 rkey 问题) ──
    if bot is not None and file_id:
        try:
            resp = await bot.call_api("get_image", file=file_id)
            file_data: bytes | None = None
            if isinstance(resp, dict):
                raw = resp.get("file") or resp.get("data") or resp.get("image") or b""
                if isinstance(raw, str):
                    # NapCat/LLOneBot: file=base64 string
                    import base64 as _b64
                    try:
                        file_data = _b64.b64decode(raw)
                    except Exception:
                        logger.debug("get_image: file 字段不是有效 base64, len=%d", len(raw))
                        file_data = None
                elif isinstance(raw, bytes):
                    file_data = raw
            elif isinstance(resp, bytes):
                file_data = resp
            elif isinstance(resp, str):
                import base64 as _b64
                try:
                    file_data = _b64.b64decode(resp)
                except Exception:
                    file_data = None

            if file_data and len(file_data) > 512:  # 有效图片至少 > 512 bytes
                img_format = _detect_image_format(file_data)
                if len(file_data) > MAX_IMAGE_BYTES:
                    file_data = _resize_image(file_data, img_format)
                logger.info("OneBot get_image 成功: %d bytes", len(file_data))
                return file_data
            logger.debug(
                "get_image 数据过小或无效: %d bytes, fallback HTTP",
                len(file_data) if file_data else 0,
            )
        except Exception:
            logger.debug("get_image API 失败，fallback HTTP", exc_info=True)

    # ── 路径 2: HTTP 下载 (fallback) ──
    async def _do_fetch(headers: dict) -> tuple[int, bytes, str]:
        """执行单次 HTTP GET，返回 (status, body_bytes, body_text)。"""
        async with aiohttp.ClientSession(
            trust_env=False,  # 国内 QQ CDN, 不走 SOCKS5 代理
        ) as session, session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=15),
            headers=headers,
            max_redirects=3,  # SSRF 防护: 限制重定向次数
        ) as resp:
            data = await resp.read()
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                text = ""
            return resp.status, data, text

    try:
        # ── 尝试 1: 带 Referer/Origin (QQ CDN 通常接受) ──
        status, data, text = await _do_fetch({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "image/avif,image/webp,image/apng,image/svg+xml,"
                "image/*,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://multimedia.nt.qq.com.cn/",
            "Origin": "https://multimedia.nt.qq.com.cn",
        })
        # ── 尝试 2: HTTP 400 → 去掉 Referer 重试 (部分 CDN 拒绝对非浏览器 Referer) ──
        if status == 400:
            logger.debug("图片下载 HTTP 400，去掉 Referer 重试...")
            status, data, text = await _do_fetch({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "image/*,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })

        if status != 200:
            logger.warning(
                "图片下载失败: HTTP %d, url=%s, body=%s",
                status, url[:150], text[:200],
            )
            return None

        if len(data) < 64:
            logger.warning("图片过小: %d bytes", len(data))
            return None

        # 检测格式 + 超过 5MB 自动压缩
        img_format = _detect_image_format(data)
        if len(data) > MAX_IMAGE_BYTES:
            data = _resize_image(data, img_format)

        logger.info("图片下载成功: %d bytes", len(data))
        return data
    except aiohttp.ClientError as e:
        logger.warning("图片下载网络错误: %s", e)
        return None
    except Exception:
        logger.exception("图片下载异常")
        return None


async def describe_image_from_url(
    image_url: str,
    bot=None,
    file_id: str = "",
    api_base: str = "",
    api_key: str = "",
    model: str = "",
    user_query: str = "",
) -> Optional[str]:
    """从 QQ 图片 URL 下载图片并调用 VLM 描述。

    Args:
        image_url: QQ 图片 URL (如 http://gchat.qpic.cn/...)
        bot: OneBot Bot 实例 (可选, 提供时优先用 get_image API)
        file_id: OneBot 图片 file ID (可选, 配合 bot 使用)
        api_base: 覆盖 VLM API base URL (空=使用全局配置)
        api_key: 覆盖 VLM API key
        model: 覆盖 VLM 模型名
        user_query: 用户对图片的具体提问 (为空=通用描述)

    Returns:
        中文图片描述文本，失败返回 None
    """
    # ── Per-bot VLM 覆盖 (bot_id 感知路由) ──
    if api_base and api_key and model:
        vlm_cfg = {
            "api_base": api_base,
            "api_key": api_key,
            "model_name": model,
            "provider": "per-bot",
        }
    else:
        vlm_cfg = _get_vlm_config()
    if not vlm_cfg:
        logger.warning("VLM 未配置，跳过图片描述")
        return None

    # 1. 下载图片 (OneBot get_image 优先，HTTP fallback)
    image_data = await _download_image(image_url, bot=bot, file_id=file_id)
    if not image_data:
        return None

    # 2. SHA-256 缓存查询 — 在 resize 之前 (保护缓存一致性)
    #    同一张图多次发送 → 同样的原始 SHA256 → 命中缓存，跳过 VLM API
    #    user_query 空 = 通用描述共享；非空 = 具体提问独立键
    _original_sha = _compute_image_sha256(image_data)
    _cache_key = _original_sha
    if user_query and user_query.strip():
        _cache_key = f"{_original_sha}:q{hashlib.sha1(user_query.encode()).hexdigest()[:20]}"
    cached = _get_cached_vlm_result_by_key(_cache_key)
    if cached:
        logger.info(
            "VLM 缓存命中: sha256=%s... query=%s",
            _original_sha[:16], (user_query or "")[:30],
        )
        return cached

    # 3. 检测图片格式 → 统一转静态 JPEG (消除 GIF/APNG 多帧)
    if not isinstance(image_data, bytes):
        logger.error("describe_image_from_url: image_data 不是 bytes (type=%s)", type(image_data).__name__)
        return None
    img_format = _detect_image_format(image_data)
    image_data = _to_static_jpeg(image_data, img_format)

    # 4. VLM 图片 resize: 超过 3MB 压缩 (减少 API 延迟 + token 消耗)
    if len(image_data) > VLM_MAX_IMAGE_BYTES:
        _orig_size = len(image_data)
        image_data = _resize_image(image_data, "jpeg")
        logger.info(
            "VLM 图片 resize: %d → %d bytes (阈值 %d)",
            _orig_size, len(image_data), VLM_MAX_IMAGE_BYTES,
        )

    # 5. Base64 编码
    mime_type = "image/jpeg"
    _img_kb = len(image_data) / 1024
    logger.info(
        "VLM 准备发送: %s, %.0fKB, model=%s",
        image_url[:60] if image_url else "?", _img_kb, model,
    )
    b64 = base64.b64encode(image_data).decode("ascii")
    data_uri = f"data:{mime_type};base64,{b64}"

    # 4. 构建 OpenAI Vision API 请求
    api_base = vlm_cfg["api_base"].rstrip("/")
    api_key = vlm_cfg["api_key"]
    model = vlm_cfg["model_name"]

    # 兼容不同的 API 路径
    if "/v1" in api_base:
        endpoint = f"{api_base}/chat/completions"
    else:
        endpoint = f"{api_base}/v1/chat/completions"

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "[图片分析任务]\n"
                        "请仔细观察这张图片，先判断类型，再按该类型的检查清单逐项描述。\n"
                        "目标是让一个没看过图的人仅凭你的描述就能在脑中还原画面，并理解图的用途和情绪。\n"
                        "\n"
                        "## 一、判断图片类型（明确归类为以下之一）\n"
                        "1. 二次元插画/同人图 — 动漫风插画、角色立绘、Pixiv/Twitter类作品（最常见）\n"
                        "2. 标签包/作品信息页 — Pixiv/Twitter/画师主页的截图，含标签列表+缩略图\n"
                        "3. 表情包/贴图 — 萌系/卡通表情、静态或动态贴图、QQ sticker\n"
                        "4. 梗图/Meme — 网络迷因、搞笑配文图、二创改图\n"
                        "5. 漫画/条漫 — 多格叙事漫画、四格、长条漫\n"
                        "6. 聊天记录截图 — QQ/微信/其他聊天软件的对话截图\n"
                        "7. 真人照片 — 实拍照片、自拍、Cosplay照、风景照等\n"
                        "8. 文字截图/信息图 — 以文字为主的通知截图、教程、表格、公告等\n"
                        "9. 其他 — 以上类型都不匹配\n"
                        "\n"
                        "## 二、根据类型逐项描述（越详细越好，不限制句数）\n"
                        "\n"
                        "### 二次元插画/同人图（重点类型，请详尽描述）：\n"
                        "【角色识别 — 仅描述视觉特征，禁止猜测 IP/角色名】\n"
                        "- ★ 不要猜测或断言角色是谁、出自哪个 IP/作品。猜错比不猜更糟糕\n"
                        "- ★ 宁可说「蓝发少女」也不要说「疑似 XXX 角色」\n"
                        "- 判断是官方立绘、同人创作、还是无法判断\n"
                        "- 如有多个角色，逐一列出并说明互动关系（贴贴/对战/日常/CP向等）\n"
                        "【角色外观 — 每个角色逐一记录】\n"
                        "- 发型发色、瞳色、肤色、体型（萝莉/少女/御姐/正太/少年/成男等）\n"
                        "- 头部：兽耳/角/发饰/眼镜/眼罩等特征\n"
                        "- 服饰：上衣/下装/外套/鞋袜/手套，风格（制服/和服/洋装/机甲/日常/泳装等）\n"
                        "- 手持物：武器/道具/食物/手机等\n"
                        "- 尾巴/翅膀/光环等非人特征\n"
                        "【神态和动作】\n"
                        "- 面部表情（笑/怒/羞/困/冷淡/困惑/哭等）及嘴型眼型细节\n"
                        "- 身体姿势（站立/坐/躺/奔跑/跳跃/回头等）\n"
                        "- 手的动作（挥手/比耶/托腮/握拳/指人等）\n"
                        "- 整体动态感和情绪传达\n"
                        "【构图与镜头】\n"
                        "- 景别：特写/半身/全身/膝上/胸上\n"
                        "- 视角：平视/俯视/仰视/荷兰角（倾斜）\n"
                        "- 镜头感：正面/侧面/背面/斜侧，是否看向镜头\n"
                        "- 单人/双人/多人，角色在画面中的位置关系\n"
                        "【画风与技法】\n"
                        "- 画风标签：赛璐璐动画风/厚涂/半厚涂/平涂/水彩/水墨/像素/线稿/素描/新海诚风/油画风 等\n"
                        "- 上色方式：渐变柔和/色块分明/高饱和/低饱和/黑白\n"
                        "- 线条：细腻/粗犷/无描线/彩色描线\n"
                        "- 如能识别出特定画师风格请注明（如「类似 ASK 的画风」「米山舞风」）\n"
                        "【背景与场景】\n"
                        "- 背景类型：纯色/渐变/模糊摄影/精细场景/透明底/白底\n"
                        "- 场景元素：室内/室外/都市/自然/天空/水下/抽象等\n"
                        "- 时间感：白天/黄昏/夜晚，季节感：春/夏/秋/冬\n"
                        "【色彩光影】\n"
                        "- 主色调（暖/冷/中性），配色风格（清新/厚重/暗黑/粉嫩等）\n"
                        "- 光源方向（顶光/侧光/逆光/散射光），是否有明显高光或边缘光\n"
                        "- 整体对比度（高对比戏剧感/柔和平淡）\n"
                        "【图中文字】\n"
                        "- 画面上所有文字逐字记录：台词/拟声词/标签/水印/签名\n"
                        "- 文字语言（中文/日文/英文/韩文等）\n"
                        "【整体评价】\n"
                        "- 画面整体氛围（温馨/悲伤/燃/色气/搞笑/治愈/黑暗等）\n"
                        "- 作画质量（精细/中等/简略），是否有明显崩坏或粗糙处\n"
                        "\n"
                        "### 标签包/作品信息页：\n"
                        "- 判断来源平台（Pixiv/Twitter/微博/其他）\n"
                        "- 缩略图内容简要描述\n"
                        "- 标签列表逐字原文（通常用 # 或空格分隔，可能是日文/英文）\n"
                        "- 标题/作品名（如有）\n"
                        "- 作者名/画师ID（如有）\n"
                        "- 数值信息：浏览量/点赞数/收藏数/日期等\n"
                        "- 如果是多图作品页（缩略图旁有 1/3 等标记），说明共几张\n"
                        "\n"
                        "### 表情包/贴图：\n"
                        "- 主体角色或形象（动物/人物/卡通/简笔画等）\n"
                        "- 表情和肢体动作的细节\n"
                        "- 画风：可爱/搞怪/呆萌/贱萌/沙雕/抽象/暴走 等\n"
                        "- 配文内容逐字原文 + 文字位置（上方/下方/覆盖在图上）\n"
                        "- 表达的情绪（开心/无语/委屈/生气/害羞/炫耀/嘲讽等）\n"
                        "- 使用场景推测（聊到什么时候会发这个表情）\n"
                        "- 是否是常见表情包系列的模板（注意：不要猜测角色名/IP，只描述视觉特征即可）\n"
                        "\n"
                        "### 梗图/Meme：\n"
                        "- 画面内容和主体\n"
                        "- 所有配文逐字原文（梗图的配文对理解至关重要）\n"
                        "- 梗的来源和含义（如能识别，如「女人吼猫」「Distracted Boyfriend」等模板名）\n"
                        "- 表达的立场、讽刺方向或笑点\n"
                        "- 是否是二创改图（在原梗上做了修改）\n"
                        "\n"
                        "### 漫画/条漫：\n"
                        "- 格子数量、阅读顺序（左→右/右→左/上→下）\n"
                        "- 每格内容概括（场景+人物+动作）\n"
                        "- 所有对话/旁白/拟声词逐字原文\n"
                        "- 整体剧情一句话总结\n"
                        "- 画风和来源（日漫/韩漫/国漫/同人漫等）\n"
                        "\n"
                        "### 聊天记录截图：\n"
                        "- 是群聊还是私聊，参与者大致几人\n"
                        "- 按时间顺序列出每条消息的发送者（昵称/备注名）和内容\n"
                        "- 特别标记：@提及、图片、红包、表情、文件等非文字消息\n"
                        "- 对话主题和关键信息总结\n"
                        "- 聊天软件类型（QQ/微信/Telegram/Discord等）\n"
                        "\n"
                        "### 真人照片：\n"
                        "- 场景（室内/室外/日/夜/天气/地点类型）\n"
                        "- 人物（数量、性别、大致年龄、衣着风格、动作神态）\n"
                        "- 如是 Cosplay 照片，说明角色和还原度\n"
                        "- 光线和色彩特点\n"
                        "- 构图和拍摄手法（自拍/他拍/广角/人像模式等）\n"
                        "- 传达的氛围和情绪\n"
                        "\n"
                        "### 文字截图/信息图：\n"
                        "- 文字内容尽量原文转述，重要信息逐字摘录\n"
                        "- 排版结构（标题/正文/表格/列表/弹幕等）\n"
                        "- 信息来源（公众号名/应用界面/网页/游戏界面等）\n"
                        "- 如果是表格或数据图，描述行列结构和关键数值\n"
                        "\n"
                        "## 三、输出格式\n"
                        "【类型】判断的类型名（从上述 9 类中选一个最匹配的）\n"
                        "【文字】图片中所有可见文字的逐字原文，按位置从上到下记录。不同位置的文字用「｜」分隔。如果图中没有任何文字，写「无文字」\n"
                        "【描述】根据对应类型的检查清单逐项展开，越详细越好。不确定处用「似乎」「可能」\n"
                        "【属性】补充标签: 画风/色调/情绪/用途，用简短关键词列出（如「赛璐璐, 暖色调, 温馨, 头像」）\n"
                        "【备注】图片清晰度、是否有裁剪或拼接痕迹、是否有马赛克/涂鸦/水印覆盖等\n"
                        "\n"
                        "核心原则（违反会降低分析质量）：\n"
                        "- 所有文字务必逐字原文转述，包含日文假名/英文/符号，不要概括或翻译\n"
                        "- 二次元插画是最高频类型，外观细节和画风标签是后续对话的关键信息，请优先保证描述的准确度和细节量\n"
                        "- ★ 禁止猜测角色名/作品名/IP。宁可说「蓝色长发少女」也不要说「疑似 XX 角色」。猜错角色比不描述角色更破坏对话体验\n"
                        "- 表情包/梗图必须说明表达了什么情绪和使用场景——这决定了 bot 能否正确回应\n"
                        "- 描述客观中立，不要评价「好看」「丑」「质量高」等主观判断\n"
                        "- 图片模糊或无法辨认的部分，直接说「无法辨认」，不要猜测填充\n"
                        "- 用中文输出，语气专业但自然"
                    ),
                },
                # ── 用户具体提问注入: 将用户问题添加到图片分析指令中 ──
                # 非空时 VLM 会针对用户的具体问题重点回答，同时缓存键也包含此提问。
                *([
                    {
                        "type": "text",
                        "text": (
                            "\n\n【用户对这张图的具体提问】\n"
                            f"用户问：{user_query.strip()}\n"
                            "请在完成上述通用分析的同时，重点回答用户的这个问题。"
                        ),
                    },
                ] if user_query and user_query.strip() else []),
                {
                    "type": "image_url",
                    "image_url": {"url": data_uri},
                },
            ],
        }
    ]

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": VLM_MAX_TOKENS,
        "temperature": 0.3,
    }

    # 5. 调用 VLM API (双层超时: aiohttp + asyncio.wait_for 硬兜底)
    logger.info(
        "VLM 调用: model=%s endpoint=%s provider=%s image_kb=%.0f prompt_chars=%d",
        model, endpoint, vlm_cfg.get("provider", "?"),
        len(image_data) / 1024, len(str(messages)),
    )
    # ★ 日志全覆盖: 打印 VLM 收到的 prompt (截断图片 base64)
    _vlm_prompt_debug = str(messages)
    _vlm_prompt_debug = re.sub(r'"url": "data:image/\w+;base64,[^"]+"', '"url": "[base64 image]"', _vlm_prompt_debug)
    logger.info("VLM prompt: %s", _vlm_prompt_debug[:2000])

    _start = time.time()
    async def _vlm_request():
        async with aiohttp.ClientSession(
            trust_env=False,  # 国内 VLM 代理商, 不走 SOCKS5 代理
        ) as session, session.post(
            endpoint,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=VLM_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.warning(
                    "VLM API 返回 %d: %s",
                    resp.status, text[:200],
                )
                return None

            # 记录 response headers (排查截断问题)
            _resp_headers = dict(resp.headers)
            logger.debug(
                "VLM response headers: %s",
                {k: v for k, v in _resp_headers.items()
                 if k not in ("authorization", "set-cookie")},
            )

            data = await resp.json()
            choices = data.get("choices", [])
            usage = data.get("usage", {})
            if not choices:
                logger.warning("VLM 返回空 choices, usage=%s", usage)
                return None

            # ── VLM 用量写入追踪 ──
            _vlm_last_usage["input_tokens"] = (
                _vlm_last_usage.get("input_tokens", 0)
                + int(usage.get("prompt_tokens", 0) or 0)
            )
            _vlm_last_usage["output_tokens"] = (
                _vlm_last_usage.get("output_tokens", 0)
                + int(usage.get("completion_tokens", 0) or 0)
            )
            _vlm_last_usage["model"] = model
            _vlm_last_usage["provider"] = vlm_cfg.get("provider", "?")
            # ────────────────────────────────────────────

            choice0 = choices[0]
            finish_reason = choice0.get("finish_reason", "unknown")
            content = choice0.get("message", {}).get("content", "")
            elapsed = int((time.time() - _start) * 1000)
            logger.info(
                "VLM 图片描述完成: %dms, model=%s, finish=%s, "
                "content_len=%d, usage=%s",
                elapsed, model, finish_reason,
                len(content), usage,
            )
            logger.info("VLM 完整输出:\n%s", content)
            if finish_reason == "length":
                logger.warning(
                    "⚠️ VLM 输出被 max_tokens 截断！"
                    " content_len=%d, 需增大 VLM_MAX_TOKENS (当前=%d)",
                    len(content), VLM_MAX_TOKENS,
                )
            result = content.strip() if content else None
            # 缓存 VLM 描述 — 用原始 SHA256 key (resize 之前计算)
            if result:
                _set_cached_vlm_result_by_key(_cache_key, result)
            return result

    try:
        result = await asyncio.wait_for(
            _vlm_request(),
            timeout=VLM_HARD_TIMEOUT,
        )
        return result
    except asyncio.TimeoutError:
        _elapsed = time.time() - _start
        # 区分: aiohttp 20s 超时 vs asyncio.wait_for 25s 硬兜底
        if _elapsed < VLM_HARD_TIMEOUT - 1.0:
            logger.warning(
                "VLM API 超时 — aiohttp 层 %.0fs 限制触发 (已等待 %.1fs, VLM 端点响应过慢)",
                VLM_TIMEOUT, _elapsed,
            )
        else:
            logger.warning(
                "VLM API 超时 — asyncio.wait_for %.0fs 硬兜底触发 (%.1fs)",
                VLM_HARD_TIMEOUT, _elapsed,
            )
        return None
    except aiohttp.ClientError as e:
        logger.warning("VLM API 网络错误: %s", e)
        return None
    except Exception:
        logger.exception("VLM 图片描述异常")
        return None


async def describe_images_from_urls(image_urls: list[str], bot=None, file_ids: list[str] | None = None, user_query: str = "") -> list[str]:
    """批量描述多张图片（顺序执行，避免并发冲击 VLM API）。

    Args:
        image_urls: QQ 图片 URL 列表
        bot: OneBot Bot 实例 (可选)
        file_ids: OneBot 图片 file ID 列表 (可选, 与 image_urls 一一对应)
        user_query: 用户对图片的具体提问 (为空=通用描述)

    Returns:
        描述文本列表（只包含成功的结果）
    """
    descriptions: list[str] = []
    fids = file_ids or []
    for i, url in enumerate(image_urls[:MAX_VLM_IMAGES]):
        fid = fids[i] if i < len(fids) else ""
        desc = await describe_image_from_url(url, bot=bot, file_id=fid, user_query=user_query)
        if desc:
            descriptions.append(desc)
    return descriptions
