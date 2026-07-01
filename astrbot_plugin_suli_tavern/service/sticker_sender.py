"""表情包发送模块 — 从 astrbot_plugin_suli_meme 共享图库读取。

架构:
  - 图库来源: astrbot_plugin_suli_meme 的 plugin_data/astrbot_plugin_suli_meme/memes/ 目录 (共享)
  - 类别元数据: astrbot_plugin_suli_meme 的 memes_data.json (共享)
  - 标签: 目录名 + 中文同义词 → 模糊搜索
  - LLM 通过 send_sticker(tag) 工具按标签搜索
  - 通过 contextvars 获取 bot/event 发送图片
  - 无匹配图片时静默跳过 (不报错)

两个 bot 共用同一图库，各自用自己的注入逻辑:
  -  (astrbot_plugin_suli_meme): Persona 注入 → &&happy&& 标签格式
  - 暮恩 (本模块):   LLM tool + 叙事 Effects 驱动

用法:
  from .sticker_sender import send_sticker_by_tag, set_sticker_context

  set_sticker_context(bot, event)              # 调用前设置上下文
  result = await send_sticker_by_tag("害羞")   # 搜索 + 发送
"""

from __future__ import annotations

import collections
import contextvars
import json
import logging
import os
import random
from pathlib import Path

from .._astrbot_adapter import BotAdapter as Bot
from .._astrbot_adapter import EventAdapter as Event
from .._astrbot_adapter import MessageSegment

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════
# 共享图库路径解析
# ═════════════════════════════════════════════════════════════════

def _has_image_files(directory: Path) -> bool:
    """检查目录下 (递归一层) 是否有图片文件。"""
    if not directory.is_dir():
        return False
    for sub in directory.iterdir():
        if sub.is_dir():
            for f in sub.iterdir():
                if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                    return True
    return False


def _resolve_shared_memes_dir() -> Path:
    """解析 astrbot_plugin_suli_meme 共享图库目录。

    优先级:
      1. 环境变量 MEME_MANAGER_MEMES_DIR
      2. AstrBot plugin_data/astrbot_plugin_suli_meme/memes/ (runtime 数据 — WebUI 管理)
      3. 插件本地 stickers/ 回退 (兼容旧版)
    """
    env_dir = os.environ.get("MEME_MANAGER_MEMES_DIR")
    if env_dir:
        return Path(env_dir)

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        plugin_data = Path(get_astrbot_plugin_data_path())
        shared = plugin_data / "astrbot_plugin_suli_meme" / "memes"
        if _has_image_files(shared):
            return shared
    except Exception:
        pass

    # 回退: 插件本地 stickers 目录
    return Path(__file__).resolve().parent.parent / "stickers"


_SHARED_MEMES_DIR = _resolve_shared_memes_dir()
_SHARED_MEMES_DATA = _SHARED_MEMES_DIR.parent / "memes_data.json"

# ═════════════════════════════════════════════════════════════════
# 中文标签映射 — 英文类别名 → 中文同义词
#
# ★ 每条 key 必须被 _DIR_TO_TAG_KEY 中至少一个目录指向，
#   否则 LLM 用该标签搜索时永远无结果（孤儿标签）。
# ═════════════════════════════════════════════════════════════════

_TAG_SYNONYMS: dict[str, list[str]] = {
    # ── 情绪 — angry / happy / sad / surprised ──
    "开心":   ["高兴", "快乐", "喜悦", "好耶", "庆祝", "满足", "幸福", "兴奋", "耶",
              # 笑死/得意 归于开心 (共用 happy 目录)
              "笑死", "好笑", "哈哈", "笑喷", "搞笑", "乐", "草", "笑死我了",
              "得意", "骄傲", "叉腰", "炫耀", "得逞", "嘚瑟"],
    "生气":   ["愤怒", "怒火", "暴躁", "怒了", "哼", "炸毛", "气鼓鼓", "发火", "恼怒", "火大"],
    "难过":   ["伤心", "悲伤", "哭了", "emo", "心碎", "委屈", "憋屈", "呜呜",
              "想哭", "低落", "失望", "心疼", "泪目", "哭哭"],
    "惊讶":   ["吃惊", "震惊", "意外", "目瞪口呆", "懵了", "吓到", "震撼", "我靠", "啊", "啥",
              # 慌张/紧张 归于惊讶
              "慌张", "慌乱", "紧张", "不安", "害怕", "慌了"],
    # ── 社交姿态 — shy / like / meow / baka+fool / color / see ──
    "害羞":   ["羞涩", "脸红", "腼腆", "不好意思", "扭捏", "傲娇", "被戳穿", "嘴硬",
              # 尴尬 归于害羞 (共用 shy 目录)
              "尴尬", "社死", "尬住", "不知所措", "汗", "抠地", "脚趾"],
    "点赞":   ["喜欢", "赞同", "同意", "比心", "说得好", "附议", "赞", "好评", "nice",
              # 贴贴/摸头 归于点赞 (like/affection 最近)
              "贴贴", "蹭蹭", "抱抱", "亲亲", "粘人", "亲近",
              "摸头", "安慰", "摸摸", "乖", "不哭", "拍拍"],
    "卖萌":   ["可爱", "萌", "喵", "撒娇", "求关注", "萌混过关", "喵呜", "蹭", "打滚"],
    "打趣":   ["开玩笑", "调侃", "吐槽", "损", "逗乐", "闹着玩", "调戏", "逗你玩"],
    "挑逗":   ["撩", "逗弄", "恶作剧", "惹", "勾引", "调皮", "逗", "玩"],
    "吃瓜":   ["八卦", "前排", "劲爆", "瓜", "偷看", "窥屏",
              # 喝茶 归于吃瓜 (共用 see 目录)
              "喝茶", "围观", "看戏", "淡定", "我就看看", "优雅"],
    # ── 日常状态 — morning+sleep / sigh+confused+cpu / givemoney / reply / work ──
    "问好":   ["早安", "晚安", "早上好", "起床", "睡觉", "困了", "睡了", "冒泡", "来了", "拜拜",
              "困", "累了", "疲惫", "打哈欠"],
    "无语":   ["叹气", "无奈", "扶额", "无话可说", "沉默", "汗", "无语了",
              # 水群/摆烂/嫌弃 归于无语 (共用 sigh/confused/cpu 目录)
              "水群", "万用", "划水", "摸鱼", "随便", "路过",
              "摆烂", "躺平", "放弃治疗", "不想努力", "随便吧", "开摆", "算了",
              "嫌弃", "拒绝", "哒咩", "不要", "走开", "鄙视", "退退退"],
    # ── 小众但 LLM 可能用的短标签 → 聚合到最近的目录 ──
    "讨钱":   ["给钱", "打钱", "报酬", "付费", "givemoney"],
    "回复":   ["在吗", "等待", "reply"],
    "工作":   ["干活", "忙碌", "搬砖", "work"],
}

# ═════════════════════════════════════════════════════════════════
# 英文目录名 → 中文标签 key 映射
# ── astrbot_plugin_suli_meme 内置默认图库是英文目录名 (angry/happy/shy...)，
#    _TAG_SYNONYMS 的 key 是中文。这层映射桥接两者。
#    LLM 传 "得意" 能搜到 happy 目录 → happy → 开心 → tags 含 "得意"。
#
# ★ 每个实际目录都必须有一个 CN key 映射（否则该目录图片只能通过
#   英文目录名搜到）。每个 CN key 也必须有目录映射（否则是死标签）。
# ═════════════════════════════════════════════════════════════════

_DIR_TO_TAG_KEY: dict[str, str] = {
    # 情绪直接对应
    "angry": "生气",
    "happy": "开心",      # ← 也承载: 笑死, 得意 (synonyms merged into 开心)
    "sad": "难过",
    "surprised": "惊讶",  # ← 也承载: 慌张, 紧张
    # 社交姿态
    "shy": "害羞",        # ← 也承载: 尴尬
    "like": "点赞",       # ← 也承载: 贴贴, 摸头
    "meow": "卖萌",
    "baka": "打趣",
    "fool": "打趣",
    "color": "挑逗",
    "see": "吃瓜",        # ← 也承载: 喝茶
    # 日常状态
    "morning": "问好",
    "sleep": "问好",
    "sigh": "无语",       # ← 也承载: 水群, 摆烂, 嫌弃
    "confused": "无语",
    "cpu": "无语",
    # 小众 (之前无 CN 映射, 现在补上)
    "givemoney": "讨钱",
    "reply": "回复",
    "work": "工作",
}


def _build_catalog() -> dict:
    """从 astrbot_plugin_suli_meme 共享目录动态构建 sticker catalog。

    每个子目录 = 一个类别标签，目录内图片 = 该类别的 sticker。
    目录名、中文同义词、memes_data 描述全部合并为 tag。

    Returns:
        {"stickers": [{"file": "angry/img.jpg", "tags": [...], "desc": "...", "intensity": "?"}, ...]}
    """
    stickers: list[dict] = []
    memes_dir = _SHARED_MEMES_DIR

    # 读取类别描述 (可选)
    category_descs: dict[str, str] = {}
    try:
        if _SHARED_MEMES_DATA.exists():
            category_descs = json.loads(_SHARED_MEMES_DATA.read_text(encoding="utf-8"))
    except Exception:
        pass

    if not memes_dir.is_dir():
        logger.info("共享图库目录 %s 不存在，sticker catalog 为空", memes_dir)
        return {"stickers": []}

    for category_dir in sorted(memes_dir.iterdir()):
        if not category_dir.is_dir():
            continue

        cat_name = category_dir.name.lower()
        if cat_name.startswith("."):
            continue

        # 构建该类别下所有图片的 sticker 条目
        image_files = sorted(
            f for f in category_dir.iterdir()
            if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp")
        )
        if not image_files:
            continue

        # 合并标签: 目录名 + 中文标签 key + 中文同义词
        cn_key = _DIR_TO_TAG_KEY.get(cat_name)
        if cn_key:
            tags = [cat_name, cn_key, *_TAG_SYNONYMS.get(cn_key, [])]
        else:
            tags = [cat_name]
        desc = category_descs.get(cat_name, cat_name)
        # 强度推断: 强烈情绪 → high, 中性 → medium / low
        high_intensity = {"生气", "惊讶", "开心", "难过", "笑死"}
        low_intensity = {"问好", "点赞"}
        intensity_key = cn_key or cat_name
        intensity = "high" if intensity_key in high_intensity else (
            "low" if intensity_key in low_intensity else "medium"
        )

        for img_file in image_files:
            # file 路径相对于 memes_dir (保证可跨插件解析)
            relative_path = f"{category_dir.name}/{img_file.name}"
            stickers.append({
                "file": relative_path,
                "tags": tags,
                "desc": desc,
                "intensity": intensity,
            })

    logger.info(
        "表情包 catalog 已从共享图库构建: %d 张图片, %d 个有效分类, 来源=%s",
        len(stickers),
        len({s["tags"][0] for s in stickers}),
        memes_dir,
    )
    return {"stickers": stickers}


# ═════════════════════════════════════════════════════════════════
# 上下文传递 (工具执行器无法直接访问 bot/event)
# ═════════════════════════════════════════════════════════════════

_sticker_bot: contextvars.ContextVar[Bot | None] = contextvars.ContextVar(
    "sticker_bot", default=None
)
_sticker_event: contextvars.ContextVar[Event | None] = contextvars.ContextVar(
    "sticker_event", default=None
)


def set_sticker_context(bot: Bot, event: Event) -> None:
    """设置表情包发送上下文 (在 LLM 调用前调用)。"""
    _sticker_bot.set(bot)
    _sticker_event.set(event)


def clear_sticker_context() -> None:
    """清除上下文 (LLM 调用后)。"""
    _sticker_bot.set(None)
    _sticker_event.set(None)


# ═════════════════════════════════════════════════════════════════
# Catalog 加载
# ═════════════════════════════════════════════════════════════════

_catalog: dict = _build_catalog()


def _get_catalog() -> dict:
    """获取 sticker catalog，每次调用重新扫描目录。

    运行中通过 webui 新增/删除图片后无需重启即可生效。
    """
    global _catalog
    _catalog = _build_catalog()
    return _catalog


# ═════════════════════════════════════════════════════════════════
# 已发送降权: 同一群内最近发送过的表情包降低随机权重
# ═════════════════════════════════════════════════════════════════

# Per-group 最近发送记录 (LRU, 最多保留 8 条)
# ★ ADR-001 进程隔离: 双 Bot 各自独立容器，此 dict 天然 per-bot 安全。
#   如需单进程多 bot → key 改为 f"{bot_id}:{group_id}"。
_recent_stickers: dict[str, collections.deque[str]] = {}
_MAX_RECENT = 8
# 最近发送过的表情包权重系数 (0.0=完全排除, 0.15=大幅降权但仍可能抽到)
_RECENT_WEIGHT = 0.15


def _record_sticker_sent(group_id: str, file_path: str) -> None:
    """记录表情包已发送到群, 供后续降权使用。"""
    if not group_id:
        return
    if group_id not in _recent_stickers:
        _recent_stickers[group_id] = collections.deque(maxlen=_MAX_RECENT)
    _recent_stickers[group_id].append(file_path)


# ═════════════════════════════════════════════════════════════════
# 搜索 & 发送
# ═════════════════════════════════════════════════════════════════

def _search_sticker(tag: str, group_id: str = "") -> dict | None:
    """按标签搜索表情包，同类降权取用。

    搜索逻辑: 对 sticker 的所有 tag 做子串匹配 (大小写不敏感)，
    允许 LLM 传入 "生气" 匹配到 angry 类别。

    降权: 同一群最近发送过的表情包权重降至 {_RECENT_WEIGHT}，
    降低连发同一张的概率，但不完全排除。

    Args:
        tag: 标签关键词 (大小写不敏感, 支持中英文)
        group_id: 群号 (用于降权已发送表情包, 空字符串=不降权)

    Returns:
        sticker dict (含 file/tags/desc/intensity) 或 None
    """
    tag_lower = tag.strip().lower()
    if not tag_lower:
        return None

    matches: list[dict] = []
    for s in _get_catalog().get("stickers", []):
        s_tags = [t.lower() for t in s.get("tags", [])]
        if any(tag_lower in t or t in tag_lower for t in s_tags):
            file_path = _SHARED_MEMES_DIR / s.get("file", "")
            if file_path.exists():
                matches.append(s)

    if not matches:
        return None

    # ── 降权: 最近发送过的表情包降低权重 ──
    recent = _recent_stickers.get(group_id, collections.deque()) if group_id else None
    if recent and len(matches) > 1:
        weights = [
            _RECENT_WEIGHT if s["file"] in recent else 1.0
            for s in matches
        ]
        picked = random.choices(matches, weights=weights, k=1)[0]
        _recent_weighted = any(w == _RECENT_WEIGHT for w in weights)
        logger.debug(
            "表情包 %s (%s) 从 %d 候选中降权选出 (recent=%d weighted=%s)",
            picked["file"], tag, len(matches), len(recent), _recent_weighted,
        )
    else:
        picked = random.choice(matches)
        logger.debug("表情包 %s (%s) 从 %d 候选中随机选出", picked["file"], tag, len(matches))
    return picked


def get_available_tags() -> list[str]:
    """返回所有可用标签列表 (含中文同义词, 供 tool description 使用)。"""
    tags: set[str] = set()
    for s in _get_catalog().get("stickers", []):
        for t in s.get("tags", []):
            tags.add(t)
    return sorted(tags)


def get_category_summary() -> str:
    """返回可用类别摘要 (供 intent_gate 等模块使用)。

    格式: "生气/angry(12), 开心/happy(15), ..."
    """
    cats: dict[str, int] = {}
    for s in _get_catalog().get("stickers", []):
        tags = s.get("tags", [])
        if tags:
            primary = tags[0]  # 目录名 (English)
            cats[primary] = cats.get(primary, 0) + 1
    return ", ".join(
        f"{_TAG_SYNONYMS.get(_DIR_TO_TAG_KEY.get(c, c), [c])[0]}/{c}({n})"
        for c, n in sorted(cats.items())
    )


async def send_sticker_by_tag(tag: str, group_id: str = "") -> str:
    """按标签搜索并发送表情包。

    发送后返回描述文本给 LLM，让其知道表情包已发出。

    Args:
        tag: 表情包标签 (中英文均可, 如 "害羞" / "shy" / "喜欢")
        group_id: 群号 (用于降权已发送表情包, 空字符串=不降权)

    Returns:
        中文结果描述 (会作为 tool result 注入上下文)
    """
    if not tag or not tag.strip():
        return "❌ 表情包标签不能为空。"

    sticker = _search_sticker(tag, group_id=group_id)
    if not sticker:
        available = get_available_tags()
        # 只展示中文标签
        cn_tags = sorted({t for t in available if any("一" <= c <= "鿿" for c in t)})
        shown = cn_tags[:15]
        return (
            f"🎫 表情包标签「{tag}」暂无可用图片。"
            f"可用标签: {', '.join(shown)}"
        )

    bot = _sticker_bot.get(None)
    event = _sticker_event.get(None)

    if bot is None or event is None:
        logger.warning("表情包上下文未设置，无法发送: tag=%s", tag)
        return f"🎫 表情包已准备: {sticker['desc']} (离线模式，未发送)"

    image_path = _SHARED_MEMES_DIR / sticker["file"]
    try:
        await bot.send(
            event,
            MessageSegment.image(f"file:///{image_path}"),
        )
        _record_sticker_sent(group_id, sticker["file"])
        logger.info(
            "表情包已发送: %s (tag=%s, intensity=%s)",
            sticker["file"], tag, sticker.get("intensity", "?"),
        )
        return f"🎫 已发送表情包: {sticker['desc']}"
    except Exception:
        logger.exception("表情包发送失败: %s", sticker["file"])
        return f"❌ 表情包发送失败: {sticker['file']}"


async def send_sticker_direct(event: Event, tag: str, bot: Bot | None = None, group_id: str = "") -> str:
    """直接发送表情包到 event (不走 contextvars)。

    用于管线直接调用场景 (如 "react" 级别的表情包回复)，无需设置上下文。

    Args:
        event: AstrBot 事件
        tag: 表情包标签
        bot: Bot 实例 (可选，未提供时从 context 获取)
        group_id: 群号 (用于降权已发送表情包, 空字符串=不降权)

    Returns:
        中文结果描述
    """
    if not tag or not tag.strip():
        return "❌ 表情包标签不能为空。"

    sticker = _search_sticker(tag, group_id=group_id)
    if not sticker:
        return f"🎫 标签「{tag}」无匹配图片。"

    resolved_bot = bot or _sticker_bot.get(None)
    if resolved_bot is None:
        logger.warning("表情包上下文未设置，无法发送: tag=%s", tag)
        return f"🎫 表情包已准备: {sticker['desc']} (离线模式，未发送)"

    image_path = _SHARED_MEMES_DIR / sticker["file"]
    try:
        await resolved_bot.send(
            event,
            MessageSegment.image(f"file:///{image_path}"),
        )
        _record_sticker_sent(group_id, sticker["file"])
        logger.info(
            "表情包已发送(direct): %s (tag=%s), event_id=%s",
            sticker["file"], tag, getattr(event, "message_id", "?"),
        )
        return f"🎫 已发送表情包: {sticker['desc']}"
    except Exception:
        logger.exception("表情包发送失败: %s", sticker["file"])
        return f"❌ 表情包发送失败: {sticker['file']}"
