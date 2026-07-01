"""群聊自然对话调度器 — debounce/batch 触发 + LLM 自主发言决策。

核心设计:
  - 白名单机制: 默认所有群关闭，/role group on 逐群开启
  - 触发策略: @提及(立即) + debounce(静默N秒) + batch(累积M条)
  - LLM 自主决策: 通过 [静默] 标记让 AI 决定是否发言
  - 上下文隔离: 每个群独立上下文窗口，与 1对1 角色扮演互不干扰
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path

from astrbot.api import logger

from .._safe_task import safe_task

from astrbot_plugin_suli_emotion import (
    EmotionEngine,
    apply_emotion_events,
    get_user_relation,
    save_user_relation,
    set_user_nickname,
)
from astrbot_plugin_suli_intelligence import DOMAINS, WorldBookBuffer, detect_domains, load_world_book
from astrbot_plugin_suli_memory import init_memory_store, init_tier_manager, init_experience_store, get_experience_store, init_episodic_store, get_episodic_store
from astrbot_plugin_suli_routing import ModelRouter, ModelTier
from astrbot_plugin_suli_validation import CrossValidator, detect_challenge

from .._astrbot_adapter import BotAdapter as Bot
from .._astrbot_adapter import EventAdapter as GroupMessageEvent
from .._astrbot_adapter import MessageSegment
from ..config import Config
from astrbot_plugin_suli_context import (
    ContextGatherer,
    ContextPreflight,
)
from astrbot_plugin_suli_gate import GateResultProtocol, GracePeriod
from ..service.bot_identity import get_bot_identity_service
from astrbot_plugin_suli_social import SocialGuard, get_social_guard
from ..intelligence.prompt_builder import GroupPromptBuilder
from ..service.tavern_client import TavernClient
from ..sticker_sender import clear_sticker_context, send_sticker_direct, set_sticker_context
from ..handlers.deep_qa import execute_deep_qa, is_deep_question, is_deep_question_via_gate
from ..intelligence.react_engine import ReActEngine
from .recent_self_behavior import RecentSelfBehaviorStore, get_store as get_self_behavior_store
from ..intelligence.tools import (
    _get_tool_rejection,
)
from ..tools import (
    TOOLS,
    clear_memory_context,
    clear_notice_sender,
    clear_pending_images,
    clear_thread_summary_cache,
    get_and_clear_force_reply_bypass,
    get_pending_images,
    get_thread_summary_cache,
    run_tool_loop,
    set_notice_sender,
)

# ── 门控豁免工具: 零成本本地操作, 不查好感度/不受二元门控全关 ──
_GATE_EXEMPT_TOOLS: frozenset[str] = frozenset({"send_sticker", "parse_forwarded_message"})

# ── 工具族: 同族内任一被 Gate 建议 → 全族放行 (互补工具不互斥) ──
# 例如 "找壁纸" 同时需要 web_search(搜来源) 和 pixiv_search(搜图),
# Gate 只建议了一个 → 另一个也应保留, 让 LLM 按需选用。
_TOOL_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("web_search", "pixiv_search", "search_knowledge"),  # 搜索族: 文字搜索/图片搜索/知识库
)

# 工具中文名映射 — 与下游 per-tool 过滤提示 (line ~4877) 对齐, 给 Gate blocked_reason 用
_TOOL_NAME_CN = {
    "web_search": "联网搜索", "search_knowledge": "知识库检索",
    "describe_image": "看图识图", "generate_image": "AI画图",
    "edit_image": "图片编辑", "check_lport_status": "L-Port状态查询",
    "list_available_models": "模型列表", "list_custom_nodes": "节点列表",
    "remember_memory": "记忆", "get_memory": "记忆",
    "pixiv_search": "Pixiv搜图",
}


def _compute_tool_permission_snapshot(
    bot_id: str,
    user_id: str,
    *,
    self_id: str,
    admin_qq: int | None,
) -> tuple[list[str], str]:
    """★ 2026-06-28 意图闸改造: 算"触发用户实际有权用的工具 + 被拦原因"快照, 喂给 Gate。

    复用现有 can_use_tools() + get_tool_min_affinity() 权威判断, 不重复实现权限逻辑 (单一真相源)。
    仅用于让 Gate 在推荐 suggested_tools 时避坑 — 真正的工具过滤仍在下游 per-tool 段执行。

    Args:
        bot_id: 当前 bot QQ
        user_id: 触发用户 QQ (空 → 保守返回空 usable + "无触发者无法判定")
        self_id: 写入 affinity 判断的 self_id (per-bot 键)
        admin_qq: 超管 QQ (admin 不受限制) — cfg.super_admin_qq

    Returns:
        (usable: 可调工具名列表, blocked_reason: 被拦原因自然语言; 空字符串=无被拦)
    """
    if not user_id:
        return [], "无明确触发者, 无法判定工具权限 — 假定本轮无工具可调"
    # admin 不受限制: 全集可用, 无被拦
    all_tool_names = [t.get("function", {}).get("name", "") for t in TOOLS] if TOOLS else []
    all_tool_names = [n for n in all_tool_names if n]
    if admin_qq and user_id == str(admin_qq):
        return all_tool_names, ""
    try:
        from ..service.bot_config import get_config_service
        from astrbot_plugin_suli_emotion import get_user_relation
        _tool_svc = get_config_service()
        _rel = get_user_relation(user_id, self_id=self_id or "", admin_qq=admin_qq)
        _user_level = _rel.affinity.level
    except Exception:
        logger.debug("工具权限快照计算失败 (fallback: 全集可用)", exc_info=True)
        return all_tool_names, ""
    usable: list[str] = []
    blocked: list[tuple[str, int]] = []  # (tool_cn, min_aff)
    for _name in all_tool_names:
        _min_aff = _tool_svc.get_tool_min_affinity(self_id or bot_id, _name)
        if _name in _GATE_EXEMPT_TOOLS or _user_level >= _min_aff:
            usable.append(_name)
        else:
            blocked.append((_TOOL_NAME_CN.get(_name, _name) or _name, _min_aff))
    if not blocked:
        return usable, ""
    # 拼自然语言原因: 不用花括号(防 .format 二次解析), 不暴露好感度数字核心隐私
    blocked_items = "、".join(f"{cn}(需级≥{mn})" for cn, mn in blocked)
    level_cn = "陌生人" if _user_level == 0 else f"Lv.{_user_level}"
    reason = f"{blocked_items} 当前用户为{level_cn}, 好感度未达门控"
    return usable, reason

# ── 线程上下文提取 ─────────────────────────────────────


def _extract_thread_context(messages: list[dict], user_id: str) -> list[dict]:
    """从完整消息历史中提取 bot 与指定用户最近几轮对话。

    返回 [{role, content}, ...] 格式，最多 12 条 (约 6 轮)。
    """
    bot_prefixes = ("bot_",)
    thread: list[dict] = []
    for m in reversed(messages[-40:]):
        mid = str(m.get("user_id", ""))
        if mid == user_id:
            thread.append({"role": "user", "content": str(m.get("content", ""))[:200]})
        elif mid.startswith(bot_prefixes):
            thread.append({"role": "assistant", "content": str(m.get("content", ""))[:200]})
    thread.reverse()
    return thread[-12:]


# ── 图片 URL 清洗 ───────────────────────────────────────

# QQ 图片 CDN 域名模式 — 用于从上下文中清除过期 URL
_QQ_IMAGE_URL_RE = re.compile(
    r"https?://(?:"
    r"gchat\.qpic\.cn|"
    r"multimedia\.nt\.qq\.com\.cn|"
    r"c2cpicdw\.qpic\.cn|"
    r"chatimg\.qpic\.cn"
    r")[/\S]*",
    re.IGNORECASE,
)


def _sanitize_image_urls(text: str) -> str:
    """将文本中的 QQ 图片 CDN URL 替换为 [图片] 占位符。

    防止 LLM 在上下文中看到过期的图片 URL 后，
    反复调用 describe_image 工具尝试下载已失效的图片。
    """
    if not text:
        return text
    sanitized, count = _QQ_IMAGE_URL_RE.subn("[图片]", text)
    if count:
        logger.debug("已清洗 %d 个过期图片 URL", count)
    return sanitized


# 保护标记: [图片 URL: ...] 中的 URL 是刚注入的，不应被清洗
_IMAGE_URL_TAG_RE = re.compile(r"\[图片\s*URL:\s*(https?://\S+?)\]", re.IGNORECASE)


def _sanitize_image_urls_v2(text: str) -> str:
    """同 _sanitize_image_urls，但保护 [图片 URL: ...] 标签中的 URL。"""
    if not text:
        return text
    _protected: dict[str, str] = {}
    _counter = 0

    def _protect(m: re.Match) -> str:
        nonlocal _counter
        key = f"__URL_PROTECT_{_counter}__"
        _counter += 1
        _protected[key] = m.group(0)
        return key

    text = _IMAGE_URL_TAG_RE.sub(_protect, text)
    sanitized, count = _QQ_IMAGE_URL_RE.subn("[图片]", text)
    if count:
        logger.debug("已清洗 %d 个过期图片 URL", count)
    for key, original in _protected.items():
        sanitized = sanitized.replace(key, original)
    return sanitized


# ── 廉价预过滤: 纯噪音/附和消息识别 ──────────────────────
# 对"草"/"确实"/"6"/"哈哈哈"这种消息，真人看一眼就跳过了——
# 不需要调一次 LLM 去判断"要不要回应"。


# 纯标点/空白消息
_TRIVIAL_PUNCT_RE = re.compile(r'^[，。！？、…,\.!\?~～\s\'\"\'\"\-—\+＋\=＝＃#＠@￥$％%\^＆&\*＊\(\)（）\[\]【】\{\}｛｝<>《》〈〉\|\\\/／\:：;；_＿]+$')

# 短噪音词: 整条消息 (去 CQ 码后) 完全匹配这些词 → 噪音
_TRIVIAL_NOISE_FROZEN: frozenset[str] = frozenset({
    # 中文附和
    "草", "确实", "确实是这样", "有道理", "说得好",
    # 数字评价
    "6", "66", "666", "6666",
    # 单字附和
    "牛", "行", "好", "嗯", "哦", "喔", "啊", "对", "是", "棒",
    # 英文附和
    "w", "ww", "www", "wwww",
    "h", "hh", "hhh", "hhhh",
    "l", "ll", "lll",
    "nb", "np", "tql", "orz", "tqltql",
    # 笑声
    "笑死", "乐", "绝了", "太强了", "牛逼",
})


def _is_trivial_noise(content: str) -> bool:
    """检查消息是否为无需回应的噪音/附和。

    此类消息特点: 极短、无信息量、任何真人都不会对它们"认真判断要不要回应"。
    直接跳过门控 LLM 调用，既不回复也不消耗 token。

    Args:
        content: 消息纯文本 (可含 CQ 码)

    Returns:
        True 如果应跳过 (纯噪音)
    """
    if not content:
        return True

    # 去掉 CQ 码 (表情/图片/at/回复) → 只看人打的实际文字
    cleaned = re.sub(r'\[CQ:[^\]]+\]', '', content).strip()
    if not cleaned:
        return True  # 纯表情/CQ码/图片 → 噪音

    # 极短纯标点 → 噪音
    if _TRIVIAL_PUNCT_RE.match(cleaned):
        return True

    # 纯笑声 (≥2 个哈/嘻/呵/嘿/嘎/吼/呱) → 噪音
    if re.match(r'^[哈嘻呵嘿嘎吼呱]{2,}$', cleaned):
        return True

    # 短消息全量匹配噪音词 → 噪音
    if len(cleaned) <= 8 and cleaned.lower() in _TRIVIAL_NOISE_FROZEN:
        return True

    # 纯重复单字 (如 "草草草草") → 噪音
    if len(cleaned) >= 2 and len(set(cleaned)) == 1:
        return True

    return False


# ── 分级漏斗第2层: 关键词预检 — 仅冷状态, 零 token ──────────────
# 命中任一模式 → 进轻量 relevance (第3层)
# 全不中 → batch 累积或丢弃
# 关键约束: 此 regex 绝不用于热对话 (热对话由对话状态层放行)
_KEYWORD_WAKE_RE = re.compile(
    r"|".join([
        # ── 叫 bot 名字 ──
        r"(小暮|暮恩|moon|洛酱|蛇娘|梅比乌斯||||猫娘|露酱)",
        # ── 疑问句式 ──
        r"[吗呢吧啊][？?]",
        r"^(什么|怎么|如何|为什么|为啥|哪[个些]|谁|多少|几点)",
        # ── 求助/指令 ──
        r"(帮我|帮我查|帮我看|推荐|建议|介绍|教[教我]|查一下|搜一下|找一下)",
        # ── 明确提问标记 ──
        r"(能不能|可以不|行不行|有没有|是不是|要不要|该不该|知不知道|懂不懂)",
        # ── 讨论 bot/让 bot 做事 ──
        r"(画一张|生成|画个|帮我画|帮我生成|来一张|来[张个])",
    ]),
    re.IGNORECASE,
)

# per-(bot_id, group_id) → 上次被第3层否决的时间戳
# 用于 S4 退避: 被否决后 N 秒内不再触发 batch/debounce, 防止死循环烧 token
_GATE_BACKOFF_SECONDS = 120


from .context_lifecycle import extract_and_distill, maybe_compress, setup_memory_ctx
from .reply_postprocessor import (
    filter_narration,
    get_recent_bot_replies,
    is_duplicate,
    resolve_at_mentions,
    sanitize_qq_reply,
)

# ── 模块级并发控制 (per-bot 隔离) ────────────────────────

_llm_semaphores: dict[str, asyncio.Semaphore] = {}
"""per-bot LLM 并发信号量 — key=bot_id, 每个 bot 独立配额."""

# ── URL→file_id 映射缓存 (跨消息查找恢复 OneBot file_id) ──
# main.py 在设置 _moon_deferred_vlm 时同步写入。
# 跨消息查找从 ctx.messages 中提取 URL 后，从此缓存恢复 file_id，
# 使 describe_images_from_urls 能走 OneBot get_image API 而非 HTTP fallback。
_url_to_file_id: dict[str, str] = {}
"""QQ 图片 URL → OneBot file_id 映射. 最多保留 200 条, FIFO 淘汰."""


def get_llm_semaphore(bot_id: str = "", max_calls: int = 3) -> asyncio.Semaphore | None:
    """获取 per-bot LLM 调用信号量 (懒初始化 — miss 时当场创建)."""
    if not bot_id:
        return None
    if bot_id not in _llm_semaphores:
        _llm_semaphores[bot_id] = asyncio.Semaphore(max_calls)
        logger.info("bot %s LLM 信号量懒初始化: max_calls=%d", bot_id, max_calls)
    return _llm_semaphores[bot_id]


# ── 常量 ────────────────────────────────────────────────

# LLM 决定沉默时的回复标记
SILENCE_MARKER = "[静默]"

def _inject_knowledge(messages: list[dict], group_id: str = "") -> int:
    """知识库 Pre-inject: 用最近用户消息检索本地知识库，注入到 system prompt。

    无 LLM 调用 — 纯关键词分词检索，~5-20ms。
    返回注入的条数 (0 = 无结果或跳过)。
    """
    try:
        from astrbot_plugin_suli_services import get_knowledge_base as _get_kb
    except Exception as _e:
        logger.warning("知识库 pre-inject: import 失败: %s", _e)
        return 0

    # 提取 query: 从格式化 user 消息中提取最后一条实际聊天内容
    # 格式: "Name[QQ:...] [主人] (HH:MM): 消息内容"
    import re as _re_query
    query = ""
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("content"):
            raw = str(m["content"]).strip()
            for line in reversed(raw.split("\n")):
                line = line.strip()
                # 匹配: "): " 分隔符 (时间在括号里, 格式 "(HH:MM): 消息")
                if "): " in line:
                    query = line.split("): ", 1)[-1].strip()
                    break
            break
    if not query or len(query) < 4:
        logger.info("知识库 pre-inject: query 为空或太短 (%d chars), 跳过", len(query))
        return 0
    query = query[:200]

    try:
        kb = _get_kb()
        results = kb.search(query, top_n=3)
    except Exception as _e:
        logger.warning("知识库 pre-inject: 搜索异常: %s", _e)
        return 0

    if not results:
        logger.info("知识库 pre-inject: query=\"%.50s\" → 无结果", query)
        return 0

    # 格式化注入 — 作为独立 system message 插在 message[0] (静态缓存) 之后
    lines = [
        "[系统指令] 以下本地知识库内容可能与用户问题相关——优先参考回答。"
        "如果内容明显不匹配（比如同名但来自不同作品），再正常追问确认。",
        f"匹配「{query[:80]}」({len(results)} 条):",
    ]
    for i, section in enumerate(results, 1):
        title = section.split("\n")[0] if section else "(空)"
        body = section[:600]
        lines.append(f"\n── {i}. {title} ──")
        lines.append(body)
    lines.append("── 以上来自本地知识库 ——")

    kb_msg = {"role": "system", "content": "\n".join(lines)}

    # 插在第一条 system 之后 (message[0] 保持不变 → 缓存命中 ✅)
    messages.insert(1, kb_msg)

    if group_id:
        logger.info(
            "群 %s: 知识库 pre-inject: query=\"%.50s\" → %d 条",
            group_id, query, len(results),
        )
    return len(results)


# ── 拒绝反应表情包 ──
_REJECTION_STICKER_COOLDOWN = 300  # per-group: 5 分钟内最多发一个拒绝表情包
_REJECTION_STICKER_TAG: dict[str, str] = {
    "should_reply_false": "无语",
    "not_directed": "无语",
    "social_guard": "嫌弃",
    "emotion_low_valence": "问好",
    "emotion_low_affinity": "无语",
    "emotion_silence": "无语",
    "peer_play": "嫌弃",
    "llm_silence": "无语",
}


from .group_context import GroupChatContext
from ..context.conversation_session import get_slot_manager

# ── GroupChatScheduler ─────────────────────────────────

class GroupChatScheduler:
    """群聊自然对话调度器。

    管理每群的上下文、debounce 定时器、白名单持久化，
    并在触发条件满足时调用 LLM 决策是否发言。
    """

    def __init__(
        self,
        tavern: TavernClient,
        characters: dict[str, dict],
        config: Config,
        whitelist_path: Path,
    ):
        self._tavern = tavern
        self._characters = characters  # {self_id: character_card}
        self._config = config
        self._current_bot_id: str = ""  # 由 on_message() 设置, __init__ 时为空
        self._whitelist_path = whitelist_path
        self._bot_id_cache_path = whitelist_path.parent.parent / ".bot_id_cache"
        # 放在 data/ 目录 (非 shared_db/), 避免双容器共享同一缓存文件
        # UserMemoryStore + MemoryTierManager (per-bot, 在 on_message() 首次知道 self_id 时懒初始化)
        self._memory = None
        self._tier_manager = None
        self._memory_stores_initialized: set[str] = set()
        # Bot 自传体经历记忆 (per-bot, 在 on_message() 首次知道 self_id 时懒初始化)
        self._experience_store_cfg = {
            "max_recent": config.bot_experience_max_recent,
            "max_core": config.bot_experience_max_core,
            "extract_cooldown": config.bot_experience_extract_cooldown,
            "distill_threshold": config.bot_experience_distill_threshold,
            "distill_cooldown": config.bot_experience_distill_cooldown,
        }
        self._experience_store = None
        self._episodic_store = None
        self._experience_stores_initialized: set[str] = set()
        self._episodic_stores_initialized: set[str] = set()
        self._limits_configured: set[str] = set()  # per-bot 限额已注入标记
        self._prompt_builder = GroupPromptBuilder(
            characters, config, self._memory, self._chat_param,
            tier_manager=self._tier_manager,
            experience_store=self._experience_store,
        )

        self._contexts: dict[str, GroupChatContext] = {}
        self._debounce_tasks: dict[str, asyncio.Task] = {}
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._group_tiers: dict[int, str] = {}
        self._whitelist_mtime: float = 0  # 文件 mtime 追踪 (用于热加载)
        # ── 启动时加载白名单 (bot_id 未知时合并所有 bot 条目, 防重启窗口全拦截) ──
        self._load_whitelist()

        # ── World Book / Lorebook 有状态追踪 ──
        # 世界书 per-bot: 显式映射, 和角色卡加载完全一致
        _wb_dir = Path(__file__).parent.parent / "characters"
        self._world_book_entries: dict[str, list] = {}  # {bot_id: [WorldBookEntry]}
        from astrbot_plugin_suli_guards.dual_bot import get_bot_qq_set
        _bot_pairs = [(bid, "") for bid in get_bot_qq_set()]
        if not _bot_pairs:
            _bot_pairs = [("", "")]
        for _bot_id, _wb_name in _bot_pairs:
            _wb_path = _wb_dir / f"{_wb_name}_world_book.json"
            if _wb_path.exists():
                self._world_book_entries[_bot_id] = load_world_book(str(_wb_path))
                logger.info("已加载世界书: %s → bot %s (%d 条目)", _wb_path.name, _bot_id, len(self._world_book_entries[_bot_id]))
        self._world_book_buffers: dict[str, WorldBookBuffer] = {}

        # ── 串行化触发合并: 防止快速连续触发产生多条混乱回复 ──
        self._processing_groups: set[str] = set()
        """正在执行 _evaluate_and_reply 的 (bot_id:group_id) 集合。"""
        self._pending_triggers: dict[str, dict] = {}
        """合并等待的触发信息: {bot_id:group_id: {"reason": str, "timestamp": float}}"""
        self._processing_trigger_uids: dict[str, str] = {}
        """当前正在处理的 trigger_uid: {bot_id:group_id: user_id} — 累积窗口只吸收同用户消息"""
        self._accumulation: dict[str, dict] = {}
        """累积窗口状态: {bot_id:group_id: {text, event, trigger_reason}}"""
        self._trigger_timeout = float(
            self._chat_param("trigger_timeout_seconds", "trigger_timeout_seconds")
        )
        logger.info("触发合并超时: %.0fs", self._trigger_timeout)

        # ── 近期自我行为记忆: 语义级触发合并 + Gate 感知 ──
        self._self_behavior = get_self_behavior_store(ttl_seconds=30.0)

        # ── Stage 3 Grace Period 活跃实例: 供 on_message/notice 喂入事件 ──
        self._active_grace_periods: dict[str, "GracePeriod"] = {}

        # ── 分级漏斗 S4 退避: 第3层否决后 N 秒内不重试 ──
        # key = "bot_id:group_id", value = 上次被第3层否决的时间戳
        self._gate_backoff: dict[str, float] = {}

        # ── 警惕值活跃用户: key = "bot_id:user_id", value = 当前警惕值 ──
        # 用于情绪压制: 高警惕值用户 → Gate/Emotion 注入冷淡提示
        self._active_vigilance_users: dict[str, int] = {}

        # ── 拒绝反应表情包冷却: per-group ──
        # key = "bot_id:group_id", value = 上次发送时间戳
        # 同一群 N 秒内最多发一个拒绝表情包，发过一次即证明 bot 存活。
        self._rejection_sticker_cooldowns: dict[str, float] = {}

        # 初始化 per-bot Semaphore
        global _llm_semaphores
        _bot_id = self._current_bot_id or ""
        if _bot_id not in _llm_semaphores:
            max_calls = self._chat_param("max_concurrent_llm_calls", "max_concurrent_llm_calls")
            _llm_semaphores[_bot_id] = asyncio.Semaphore(max_calls)
            logger.info("bot %s LLM 并发限制: %d", _bot_id, max_calls)

        self._whitelist_loaded = False
        """白名单是否已加载。延迟到 on_message() 首次知道 bot_id 时加载。"""

        # ── 初始化守卫系统 (注入 DB 持久化 + 恢复状态) ──
        if config.abuse_bot_detection_enabled or getattr(config, "peer_isolation_enabled", True):
            try:
                from astrbot_plugin_suli_guards import BotDetector, PeerIsolation, init_bot_detector

                from ..service.bot_db import get_bot_db

                # 注入 DB-backed 持久化存储 (否则 BotDetector 用内存默认值, 重启丢状态)
                BotDetector.init_store(get_bot_db())

                # 从 DB 恢复 BotDetector 状态 (action_taken + 冷却窗口 + 每日计数)
                if config.abuse_bot_detection_enabled:
                    init_bot_detector()

                # 加载已标记隔离用户
                if getattr(config, "peer_isolation_enabled", True):
                    db = get_bot_db()
                    flagged = db.list_suspected_bots(status="flagged")
                    if flagged:
                        PeerIsolation.load_flagged(self._current_bot_id, [f["user_id"] for f in flagged])

                    # ── E1 双向加固: 预标记已知 peer bot ──
                    #  (peer_bot_qq) 是同群对照 bot——她的发言应始终被隔离，
                    # 无需等待 BotDetector 累积样本。此标记从启动即生效。
                    _peer_qq = str(getattr(config, "peer_bot_qq", "") or "")
                    if _peer_qq and _peer_qq != "0":
                        PeerIsolation.mark_flagged(self._current_bot_id, _peer_qq, "peer_config")
                        logger.info(
                            "PeerIsolation: peer bot %s 已预标记隔离 (来自配置)",
                            _peer_qq[:8],
                        )
            except Exception:
                logger.debug("BotDetector/PeerIsolation 初始化失败", exc_info=True)

        # ── Bot 间协调服务 (ADR-001) ──
        self._coordination: "CoordinationService | None" = None
        try:
            from ..service.coordination import CoordinationService
            from ..service.bot_db import get_bot_db as _get_db
            _cdb = _get_db()
            _cdb.coordination_ensure_row("711600211")
            self._coordination = CoordinationService(_bot_id, _cdb)
            logger.info("Bot 间协调服务已初始化 (bot=%s)", _bot_id[:8])
        except Exception:
            logger.warning("Bot 间协调服务初始化失败, 退化为无协调模式", exc_info=True)

        # ── 社会性生存加固 (v2) ──
        # 注意: _my_name/_peer_name/_my_identity 延迟到 on_message() 初始化
        self._my_name: str = ""
        self._peer_name: str = ""
        self._my_identity: str = ""
        self._per_bot_names_initialized: bool = False
        _peer_qq_social = str(getattr(config, "peer_bot_qq", "") or "")
        # 社会守卫用未知 bot_id 占位 — 首次 on_message 后由 _init_per_bot_names 重新初始化
        self._social_guard = get_social_guard(
            bot_id="",
            bot_name="",
            peer_bot_name="",
            peer_bot_qq=_peer_qq_social,
        )

        # ── ReAct 引擎 (惰性初始化) ──
        self._react_engine: ReActEngine | None = None

    def _get_react_engine(self) -> ReActEngine:
        """惰性获取 ReAct 引擎 (首次调用时初始化，依赖注入)。"""
        if self._react_engine is None:
            from ..intelligence.tools import TOOLS, TOOL_EXECUTORS
            self._react_engine = ReActEngine(
                tavern=self._tavern,
                tools=TOOLS,
                tool_executors=TOOL_EXECUTORS,
                max_rounds=5,
                max_total_tokens=8000,
                timeout_seconds=90.0,
                model="deepseek-v4-pro",
                provider=self._resolve_provider(),
                bot_id=self._current_bot_id,
            )
            logger.info("ReAct 引擎已初始化: tools=%d max_rounds=%d", len(TOOLS), 5)
        return self._react_engine

        # ── 初始化模型路由 (注入 domain 感知 + 凭证提供者) ──
        try:
            from astrbot_plugin_suli_routing import (
                init_credential_provider,
                init_domain_awareness,
            )

            # Domain 感知: 包装 domains 模块的两个判定函数
            class _MoonDomainAwareness:
                @staticmethod
                def is_reasoning_needed(
                    active_domains: dict[str, float], threshold: float = 2.0,
                ) -> bool:
                    from astrbot_plugin_suli_intelligence import is_reasoning_needed
                    return is_reasoning_needed(active_domains, threshold)

                @staticmethod
                def user_force_reasoning(message: str) -> bool:
                    from astrbot_plugin_suli_intelligence import user_force_reasoning
                    return user_force_reasoning(message)

            # 凭证提供者: 桥接 bot_db + bot_config
            class _MoonCredentialProvider:
                @staticmethod
                def get_config_model(key: str, default: str = "") -> str:
                    from ..service.bot_config import get_config_service
                    val = get_config_service().get_chat_param(key)
                    if val and isinstance(val, str) and val.strip():
                        return val.strip()
                    return default

                @staticmethod
                def find_llm_config(model_name: str) -> dict | None:
                    from ..service.bot_db import get_bot_db
                    configs = get_bot_db().list_llm_configs()

                    def _pack(cfg):
                        return {
                            "name": cfg.name,
                            "provider": cfg.provider,
                            "model_name": cfg.model_name,
                            "api_key": cfg.api_key,
                            "base_url": cfg.base_url,
                        }

                    # 1. 精确匹配 model_name
                    for cfg in configs:
                        if cfg.model_name == model_name:
                            return _pack(cfg)
                    # 2. 匹配 name 字段
                    for cfg in configs:
                        if cfg.name == model_name:
                            return _pack(cfg)
                    # 3. 模糊匹配: 去版本号
                    import re as _re
                    m = _re.match(
                        r"^([a-zA-Z][a-zA-Z0-9._-]*?)(?:-\d+[a-z]?)?(?:-\d+[a-z]?)?$",
                        model_name,
                    )
                    base = m.group(1) if m else ""
                    if base and len(base) > 4:
                        for cfg in configs:
                            if base in cfg.model_name or base in cfg.name:
                                return _pack(cfg)
                    return None

                @staticmethod
                def resolve_active_llm():
                    from ..service.bot_config import get_config_service
                    return get_config_service().resolve_active_llm()

            init_domain_awareness(_MoonDomainAwareness())
            init_credential_provider(_MoonCredentialProvider())
            logger.debug("模型路由依赖注入完成")
        except Exception:
            logger.debug("模型路由依赖注入失败 (非关键)", exc_info=True)

        logger.info(
            "群聊调度器初始化完成, 已启用群: %s",
            dict(sorted(self._group_tiers.items())) if self._group_tiers else "(无)",
        )

    # ── 对话参数读取 (DB 优先 → Config fallback) ─────

    def _chat_param(self, key: str, config_attr: str, bot_id: str = ""):
        """读取对话参数: BotConfigService (per-bot Web 设置) → Config (.env) 三层 fallback。

        优先级: DB per-bot 值 → DB 全局值 (向后兼容) → Config 默认值
        bot_id 可从调用方显式传入，或自动从 self._current_bot_id 读取。
        """
        bot_id = bot_id or getattr(self, '_current_bot_id', '')
        try:
            from ..service.bot_config import get_config_service
            if bot_id:
                val = get_config_service().get_chat_param(bot_id, key)
                if val is not None:
                    return val
        except Exception:
            pass
        return getattr(self._config, config_attr)

    def _resolve_character(self, self_id: str = "") -> dict:
        """根据 bot QQ 号返回对应的角色卡。

        Args:
            self_id: bot 的 QQ 号 (如前端传入的 QQ 号)

        Returns:
            角色卡 data dict，未知 self_id 返回暮恩
        """
        return self._characters.get(
            str(self_id),
            self._characters.get(self._current_bot_id, {}),
        )

    @staticmethod
    def _resolve_provider(bot_id: str = "", slot: str = "llm_primary") -> str:
        """获取指定 bot 指定槽位的 LLM provider。

        slot: "llm_primary" (普通聊天 flash/pro tier) | "llm_secondary" (进阶 opus tier)
        向后兼容: 旧 bot_config 无槽位时 fallback 到 resolve_active_llm()
        """
        try:
            from ..service.bot_config import get_config_service
            svc = get_config_service()
            # 优先使用槽位配置
            if slot in ("llm_primary", "llm_secondary"):
                cfg = svc.resolve_llm_slot(bot_id, slot)
                if cfg:
                    return cfg.provider
            # fallback: 旧全局配置
            cfg = svc.resolve_active_llm(bot_id)
            if cfg:
                return cfg.provider
        except Exception:
            pass
        return "deepseek"  # fallback

    @staticmethod
    def _resolve_provider_for_tier(bot_id: str = "", tier: str = "flash") -> str:
        """根据模型路由 tier 选择正确的 LLM 槽位。

        tier → slot 映射:
          flash → llm_primary   (普通聊天 - 日常闲聊，便宜快速)
          pro   → llm_primary   (普通聊天 - 技术问答，同 API 更大模型)
        """
        slot = "llm_secondary" if tier == "opus" else "llm_primary"
        return GroupChatScheduler._resolve_provider(bot_id, slot)

    def _record_usage(
        self, scenario: str, user_id: str = "", group_id: str = "",
    ) -> None:
        """从 tavern 读取最近一次的 token usage 并写入 DB。

        委托给 LLMGateway 统一关口。
        """
        from ..intelligence.llm_gateway import LLMGateway
        LLMGateway.record_from_tavern(
            self._tavern,
            self._current_bot_id or "",
            provider=self._resolve_provider(),
            purpose=scenario,
            group_id=str(group_id) if group_id else "",
            user_id=user_id,
        )

    # ── Token 预算熔断 ────────────────────────────────

    def _check_token_budget(self, bot_id: str = "",
                            model: str = "", provider: str = "",
                            purpose: str = "chat") -> str:
        """检查 token 预算，返回 "ok" | "soft_capped" | "hard_capped"。

        委托给 LLMGateway 统一关口。
        
        """
        from ..intelligence.llm_gateway import LLMGateway
        return LLMGateway.pre_check(
            bot_id or self._current_bot_id or "",
            purpose=purpose,
            model=model,
            provider=provider,
        )

    # ── bot_id 缓存 (跨重启恢复白名单) ─────────────────────

    def _read_cached_bot_id(self) -> str:
        """从缓存文件读取上次已知的 bot_id (跨重启恢复)。"""
        try:
            if self._bot_id_cache_path.exists():
                cached = self._bot_id_cache_path.read_text(encoding="utf-8").strip()
                if cached and len(cached) <= 32:
                    return cached
        except Exception:
            pass
        return ""

    def _write_cached_bot_id(self) -> None:
        """持久化当前 bot_id 到缓存文件 (供重启后恢复白名单加载)。"""
        bot_id = str(self._current_bot_id or "").strip()
        if not bot_id:
            return
        try:
            self._bot_id_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._bot_id_cache_path.write_text(bot_id, encoding="utf-8")
        except Exception:
            pass

    # ── 白名单持久化 (per-bot, 延迟加载) ──────────────────

    def _load_whitelist(self) -> None:
        """从 per-bot JSON 文件加载白名单 (仅当前 bot 的条目)。

        在 on_message() 首次知道 _current_bot_id 时调用。
        旧格式 (list/dict 无嵌套) 由 BotDatabase._read_whitelist_raw() 自动迁移。
        """
        try:
            data = json.loads(
                self._whitelist_path.read_text(encoding="utf-8")
            )
            bot_id = self._current_bot_id
            if not bot_id:
                # ── 重启窗口: _current_bot_id 尚未设置 → 读缓存 ──
                bot_id = self._read_cached_bot_id()
                if not bot_id:
                    logger.warning(
                        "白名单加载: bot_id 未知且无缓存, 保持空白 (fail-closed)"
                    )
                    return
                logger.info(
                    "白名单加载: bot_id 从缓存恢复 → %s", bot_id,
                )
            # 新格式: {bot_id: {group_id: tier}}
            if isinstance(data, dict) and isinstance(
                next(iter(data.values()), None), dict
            ):
                bot_entries = data.get(bot_id, {})
                self._group_tiers.clear()
                for gid_str, tier in bot_entries.items():
                    if tier in ("basic", "full"):
                        self._group_tiers[int(gid_str)] = tier
                logger.info("已加载 per-bot 白名单 [bot=%s]: %s", bot_id, self._group_tiers)
            else:
                # 旧格式: {group_id: tier} 或 [...], 全部加载 (兼容)
                if isinstance(data, list):
                    for g in data:
                        self._group_tiers[int(g)] = "basic"
                elif isinstance(data, dict):
                    for gid_str, tier in data.items():
                        if tier in ("basic", "full"):
                            self._group_tiers[int(gid_str)] = tier
                logger.info("已加载白名单 (旧格式, 兼容): %s", self._group_tiers)
        except FileNotFoundError:
            logger.info("白名单文件不存在，从空列表开始 [bot=%s]", self._current_bot_id)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("白名单文件损坏，忽略: %s", e)
        # 合并 Config 中的 seed 值
        for gid in self._config.group_chat_whitelist:
            if gid not in self._group_tiers:
                self._group_tiers[gid] = "basic"
        # 记录文件 mtime，供后续热加载检测
        try:
            self._whitelist_mtime = self._whitelist_path.stat().st_mtime
        except FileNotFoundError:
            self._whitelist_mtime = 0

    def _maybe_reload_whitelist(self) -> None:
        """检测白名单文件是否被外部修改 (如管理面板), 自动热加载。

        管理面板 (moon-panel) 是独立容器，写 JSON 文件后无法直接
        更新 bot 容器的内存缓存。此方法在每次白名单查询时检查文件
        mtime，发现变更即自动重载 —— 无需重启 bot。
        """
        if not self._whitelist_loaded:
            self._load_whitelist()
            self._whitelist_loaded = True
            return
        try:
            current_mtime = self._whitelist_path.stat().st_mtime
        except FileNotFoundError:
            return  # 文件不存在，保持当前内存状态
        if current_mtime > self._whitelist_mtime:
            logger.info(
                "检测到白名单文件变更 (mtime %.3f → %.3f)，热加载",
                self._whitelist_mtime, current_mtime,
            )
            self._load_whitelist()

    def _save_whitelist(self) -> None:
        """持久化当前 bot 的白名单到 per-bot JSON 文件。

        只修改当前 bot 的条目，不动其他 bot 的条目。
        """
        bot_id = self._current_bot_id
        if not bot_id:
            logger.warning("白名单保存: bot_id 未知，跳过")
            return
        self._whitelist_path.parent.mkdir(parents=True, exist_ok=True)
        # 读取现有数据，只更新当前 bot 的 section
        try:
            raw = json.loads(
                self._whitelist_path.read_text(encoding="utf-8")
            )
            if not isinstance(raw, dict) or not isinstance(
                next(iter(raw.values()), None), dict
            ):
                # 旧格式 → 升级为 per-bot 格式
                raw = {}
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        raw[str(bot_id)] = {
            str(gid): tier
            for gid, tier in sorted(self._group_tiers.items())
        }
        self._whitelist_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── 公开接口 ──────────────────────────────────────

    def is_group_enabled(self, group_id: int, bot_id: str = "") -> bool:
        """检查群是否已启用 (basic 或 full 等级)。"""
        # 首次启动时 _current_bot_id 可能尚未设置，从参数传入
        if bot_id and bot_id != self._current_bot_id:
            self._current_bot_id = bot_id
            self._whitelist_loaded = False  # 强制重载
        self._maybe_reload_whitelist()
        return group_id in self._group_tiers

    def get_group_tier(self, group_id: int) -> str:
        """返回群的对话等级: "disabled" | "basic" | "full" """
        self._maybe_reload_whitelist()
        return self._group_tiers.get(group_id, "disabled")

    def set_group_tier(self, group_id: int, tier: str = "basic") -> None:
        """设置群的对话等级 ("basic" 仅对话, "full" 含自然对话)。"""
        if tier not in ("basic", "full"):
            raise ValueError(f"无效的对话等级: {tier} (仅支持 basic/full)")
        self._group_tiers[group_id] = tier
        self._save_whitelist()
        # 上下文由 on_message() 延迟创建 (此时无法确定 bot_id)
        logger.info("群 %d 对话等级已设为: %s", group_id, tier)

    def enable_group(self, group_id: int) -> None:
        """启用群 (默认 full, 向后兼容旧接口)。"""
        self.set_group_tier(group_id, "full")

    def disable_group(self, group_id: int) -> None:
        """关闭群的自动对话 (清理所有 bot 的上下文和定时器)。"""
        self._group_tiers.pop(group_id, None)
        self._save_whitelist()
        # 清理所有 bot 的上下文和定时器
        from astrbot_plugin_suli_guards.dual_bot import get_bot_qq_set
        _known = get_bot_qq_set()
        _prefixes = [f"{bid}:{group_id}" for bid in _known] + [f":{group_id}"]
        for prefix in _prefixes:
            self._contexts.pop(prefix, None)
            task = self._debounce_tasks.pop(prefix, None)
            if task and not task.done():
                task.cancel()
        logger.info("群 %d 已关闭自然对话", group_id)

    def get_context(self, group_id: int, bot_id: str = "") -> GroupChatContext | None:
        """获取 (bot, group) 的对话上下文 (可能为 None)。

        bot_id 为空时回退到查找任意 bot 在此群的上下文 (向后兼容)。
        """
        if bot_id:
            return self._contexts.get(self._make_ctx_key(bot_id, group_id))
        # 回退: 查找任意匹配的上下文
        suffix = f":{group_id}"
        for key, ctx in self._contexts.items():
            if key.endswith(suffix):
                return ctx
        return None

    async def clear_context(self, group_id: int, bot_id: str = "") -> None:
        """清空 (bot, group) 的对话上下文 (加锁防竞态)。

        bot_id 为空时清空所有 bot 在此群的上下文 (向后兼容)。
        """
        if bot_id:
            keys = [self._make_ctx_key(bot_id, group_id)]
        else:
            suffix = f":{group_id}"
            keys = [k for k in self._contexts if k.endswith(suffix)]

        for key in keys:
            lock = self._group_locks.get(key)
            if lock:
                async with lock:
                    ctx = self._contexts.get(key)
                    if ctx:
                        ctx.messages.clear()
                        ctx.last_reply_time = 0.0
                        ctx.summary = ""
                        ctx.heat = 0.0
                        ctx.summary_timestamp = 0.0
                        ctx.active_domains.clear()
            else:
                ctx = self._contexts.get(key)
                if ctx:
                    ctx.messages.clear()
                    ctx.last_reply_time = 0.0
                    ctx.summary = ""
                    ctx.heat = 0.0
                    ctx.summary_timestamp = 0.0
                    ctx.active_domains.clear()
        logger.info("bot=%s 群 %d 对话上下文已清空", bot_id or "(all)", group_id)

    # ── Per-bot 上下文键 ──────────────────────────────

    @staticmethod
    def _make_ctx_key(bot_id: str, group_id: int) -> str:
        """构建 per-bot 上下文键: f"{bot_id}:{group_id}" """
        return f"{bot_id}:{group_id}"

    def _ctx_key(self, group_id: int, bot_id: str = "") -> str:
        """获取当前 bot 的上下文键 (自动使用 _current_bot_id)。"""
        return self._make_ctx_key(bot_id or self._current_bot_id, group_id)

    # ── 群锁 / Debounce ───────────────────────────────

    def _get_group_lock(self, group_id: int, bot_id: str = "") -> asyncio.Lock:
        """获取或创建 per-(bot, group) 对话锁。"""
        key = self._ctx_key(group_id, bot_id)
        if key not in self._group_locks:
            self._group_locks[key] = asyncio.Lock()
        return self._group_locks[key]

    def _cancel_debounce(self, group_id: int, bot_id: str = "") -> None:
        """取消指定 (bot, group) 的 debounce 定时器。"""
        key = self._ctx_key(group_id, bot_id)
        task = self._debounce_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
            logger.debug("bot=%s 群 %d: 已取消 debounce 定时器", bot_id or self._current_bot_id, group_id)

    def get_active_contexts(self) -> dict[str, GroupChatContext]:
        """返回所有活跃群聊上下文的快照 (key: "bot_id:group_id")。"""
        return dict(self._contexts)

    async def evaluate_proactive(self, ctx: GroupChatContext) -> None:
        """主动发言入口 — 以 proactive 触发原因调用 LLM 评估。

        供 ProactiveChatScheduler 调用，不直接访问私有方法。
        """
        # ── 群聊开关门控: 该 bot 的群聊功能是否已开启 ──
        bot_self_id = str(getattr(ctx.last_bot, "self_id", "") or "")
        if bot_self_id:
            try:
                from ..service.bot_config import get_config_service
                if not get_config_service().is_group_chat_enabled(bot_self_id):
                    return
            except Exception:
                logger.warning("群聊开关检查异常, fail-closed → 不触发 proactive (bot=%s)", bot_self_id, exc_info=True)
                return

        await self._schedule_trigger(ctx, trigger_reason="proactive")

    # ── 消息入口 ──────────────────────────────────────

    async def on_message(
        self,
        bot: Bot,
        event: GroupMessageEvent,
        user_id: str,
        user_name: str,
        content: str,
    ) -> None:
        """处理群聊消息 — 主入口。

        消息已通过基础过滤 (非空、非自身、非命令)，
        此方法负责白名单检查、触发决策、上下文管理。
        """
        self._current_bot_id = str(bot.self_id)

        # ── per-bot 名字延迟初始化 ──
        # __init__ 时 _current_bot_id 为空且 _bot_id 被世界书循环污染后被覆写，
        # 所以名字 + 社会守卫必须在这里（_current_bot_id 首次就位时）才初始化。
        if not self._per_bot_names_initialized:
            _bid = self._current_bot_id
            from astrbot_plugin_suli_guards.dual_bot import get_bot_name, get_peer_qq
            self._my_name = get_bot_name(_bid) or ""
            self._peer_name = get_bot_name(get_peer_qq(_bid)) if get_peer_qq(_bid) else ""
            _char = self._resolve_character(_bid).get("name", "")
            self._my_identity = (
                "暮恩 — 绿毛蛇女 AI 助手，专业冷静"
                if _char == "暮恩"
                else " — 银发猫娘 AI 助手，温柔俏皮"
            )
            self._per_bot_names_initialized = True
            # 用正确的 bot_id 重建社会守卫 (__init__ 时用了空 bot_id)
            _peer_qq_social = str(getattr(self._config, "peer_bot_qq", "") or "")
            self._social_guard = get_social_guard(
                bot_id=_bid,
                bot_name=self._my_name,
                peer_bot_name=self._peer_name,
                peer_bot_qq=_peer_qq_social,
            )
            logger.info(
                "[per-bot names] bot=%s my=%r peer=%r social_guard=%s",
                _bid[:8], self._my_name, self._peer_name,
                "ok",
            )

        # 持久化 bot_id 到缓存, 供重启后恢复 (避免重启窗口白名单全空)
        self._write_cached_bot_id()

        # ── 延迟加载: 首次知道 bot_id 时加载 per-bot 白名单 ──
        if not self._whitelist_loaded:
            self._load_whitelist()
            self._whitelist_loaded = True

        # ── 入口日志: 确认消息已进入调度器 ──
        logger.info(
            "[调度器] 入口: bot=%s group=? sender=%s/%s len=%d",
            self._current_bot_id[:8], user_id[:8], user_name or "?", len(content),
        )

        # group_id: EventAdapter 直接暴露, 原始事件从 message_obj 提取
        group_id = getattr(event, "group_id", 0)
        if not group_id:
            _raw = getattr(event, "message_obj", None)
            group_id = int(getattr(_raw, "group_id", 0) or 0)
        if not group_id:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            match = re.search(r":GroupMessage:(\d+)", umo)
            if match:
                group_id = int(match.group(1))
            else:
                sid = str(getattr(event, "session_id", "") or "").strip()
                if sid.isdigit():
                    group_id = int(sid)
        if not group_id:
            logger.warning("无法从事件中提取 group_id, 跳过消息")
            return

        # ── Brake 1: RoundCounter 记录每条群消息 (跨 bot 共享状态机) ──
        try:
            from astrbot_plugin_suli_guards.dual_bot import get_round_counter
            _rc = get_round_counter()
            _rc.record_message(str(group_id), user_id)
        except Exception:
            pass  # RoundCounter 不可用时静默降级

        # 0.5 Per-bot 经历记忆懒初始化 (首次处理某 bot 消息时)
        if self._current_bot_id and self._current_bot_id not in self._experience_stores_initialized:
            cfg = self._experience_store_cfg
            self._experience_store = init_experience_store(
                self._current_bot_id,
                max_recent=cfg["max_recent"],
                max_core=cfg["max_core"],
                extract_cooldown=cfg["extract_cooldown"],
                distill_threshold=cfg["distill_threshold"],
                distill_cooldown=cfg["distill_cooldown"],
            )
            self._experience_stores_initialized.add(self._current_bot_id)
            # 更新 prompt_builder 的引用 (它在构造时 snapshot 了 None)
            self._prompt_builder._experience = self._experience_store
            logger.info(
                "BotExperienceStore per-bot 已初始化: bot=%s", self._current_bot_id,
            )

        # 0.6 Per-bot UserMemoryStore + MemoryTierManager 懒初始化
        if self._current_bot_id and self._current_bot_id not in self._memory_stores_initialized:
            from astrbot_plugin_suli_memory import get_memory_store, get_tier_manager
            _store = get_memory_store(self._current_bot_id)
            if _store is None:
                _store = init_memory_store(self._config, self._tavern, bot_id=self._current_bot_id)
            self._memory = _store
            _mgr = get_tier_manager(self._current_bot_id)
            if _mgr is None:
                _mgr = init_tier_manager(self._config, _store, self._tavern, bot_id=self._current_bot_id)
            self._tier_manager = _mgr
            self._memory_stores_initialized.add(self._current_bot_id)
            # 更新 prompt_builder 的引用
            self._prompt_builder._memory = self._memory
            self._prompt_builder._tiers = self._tier_manager
            logger.info(
                "UserMemoryStore + MemoryTierManager per-bot 已初始化: bot=%s",
                self._current_bot_id,
            )

        # 0.65 Per-bot 情节记忆存储懒初始化 (槽过期 → thread_summary 归档)
        if self._current_bot_id and self._current_bot_id not in self._episodic_stores_initialized:
            _ep = get_episodic_store(self._current_bot_id)
            if _ep is None:
                _ep = init_episodic_store(self._current_bot_id)
            self._episodic_store = _ep
            self._episodic_stores_initialized.add(self._current_bot_id)
            self._prompt_builder._episodic = self._episodic_store
            # 注入到 AttentionSlotManager — 槽离开时自动归档
            try:
                get_slot_manager().set_episodic_store(_ep)
            except Exception:
                logger.debug("情节记忆注入 AttentionSlotManager 失败", exc_info=True)
            logger.info(
                "EpisodicStore per-bot 已初始化: bot=%s", self._current_bot_id,
            )

        # 0.7 Per-bot 每日限额 + 冷却配置注入 (从管理面板 → emotion 插件)
        if self._current_bot_id and self._current_bot_id not in self._limits_configured:
            try:
                from ..service.bot_config import get_config_service
                from astrbot_plugin_suli_emotion.affinity import configure_limits
                svc = get_config_service()
                _bid = self._current_bot_id
                configure_limits(
                    daily_image_max=svc.get_daily_image_limit(_bid),
                    daily_vlm_max=svc.get_daily_vlm_limit(_bid),
                    daily_tools_base=svc.get_daily_tools_base_limit(_bid),
                    tool_cooldown_seconds=svc.get_tool_cooldown_seconds(_bid),
                )
                self._limits_configured.add(self._current_bot_id)
                logger.info(
                    "每日限额配置注入完成: bot=%s image=%d vlm=%d tools_base=%d cooldown=%ds",
                    _bid[:8],
                    svc.get_daily_image_limit(_bid),
                    svc.get_daily_vlm_limit(_bid),
                    svc.get_daily_tools_base_limit(_bid),
                    svc.get_tool_cooldown_seconds(_bid),
                )
            except Exception:
                logger.warning("每日限额配置注入失败 (使用默认值)", exc_info=True)

        # 1. 白名单检查
        if not self.is_group_enabled(group_id):
            return

        # 1.5 Layer 0 防滥用闸 — 单用户限流 (令牌桶, 零 LLM 成本)
        try:
            from astrbot_plugin_suli_guards import AbuseGuard
            rate_verdict = AbuseGuard.check_rate(user_id, self._config, bot_id=bot_self_id or "")
            if rate_verdict.action == "drop":
                logger.info(
                    "群 %d: 防滥用-限流丢弃 user=%s reason=%s",
                    group_id, user_id[:8], rate_verdict.reason,
                )
                return  # 静默丢弃, 不进上下文
        except Exception:
            logger.debug("防滥用闸-限流异常", exc_info=True)

        # 2. 获取或创建 per-(bot, group) 上下文 (及关联的 WorldBookBuffer)
        ctx_key = self._ctx_key(group_id)
        ctx = self._contexts.get(ctx_key)
        if ctx is None:
            ctx = GroupChatContext(group_id=group_id)
            self._contexts[ctx_key] = ctx
        wb_buffer = self._world_book_buffers.get(ctx_key)
        if wb_buffer is None:
            _wb_entries = self._world_book_entries.get(self._current_bot_id or "", [])
            wb_buffer = WorldBookBuffer(_wb_entries)
            self._world_book_buffers[ctx_key] = wb_buffer

        # 3. 清理过期上下文 (30 分钟无活动)
        if ctx.is_expired:
            ctx.messages.clear()
            ctx.last_reply_time = 0.0
            ctx.summary = ""
            ctx.heat = 0.0
            ctx.active_domains.clear()
            ctx.conversation_threads.clear()
            wb_buffer.reset()

        # 3.5 清理过期对话线程 (时间/消息窗口)
        self._cleanup_threads(ctx)

        # 4. 更新热度 (在消息入库前计算衰减) + 能量疲劳值恢复
        self._update_heat(ctx)
        self._update_energy(ctx, did_reply=False)

        # 4.5 领域检测 (热度更新后、消息入库前)
        self._update_domains(ctx, content)

        # 4.6 情感更新 (领域检测后、消息入库前)
        await self._update_emotion(ctx, content, user_id, user_name="")

        # 4.9 Layer 0 防滥用闸 — 重复/复读检测 (同用户消息相似度)
        if ctx.messages:
            try:
                from astrbot_plugin_suli_guards import AbuseGuard
                repeat_verdict = AbuseGuard.check_repeat(
                    content, ctx.messages, user_id, self._config,
                )
                if repeat_verdict.action == "drop":
                    logger.info(
                        "群 %d: 防滥用-重复丢弃 user=%s reason=%s",
                        group_id, user_id[:8], repeat_verdict.reason,
                    )
                    return  # 静默丢弃, 不进上下文
            except Exception:
                logger.debug("防滥用闸-重复检测异常", exc_info=True)

        # 5. 消息入库
        ctx.add_message(user_id, user_name, content)

        # 5.01 Stage 3 Grace Period: 喂入新消息 (用于检测用户反悔/修改请求)
        _gp = self._active_grace_periods.get(ctx_key)
        if _gp is not None:
            _gp.feed_message(user_id, content)

        # 5.05 群聊总结计数器 + 触发检查 (无条件 — 每消息检查)
        ctx.message_count_since_last_summary += 1
        _summary_msg_threshold = self._chat_param(
            "group_summary_message_threshold", "group_summary_message_threshold",
        ) or 100
        _summary_time_threshold = self._chat_param(
            "group_summary_time_threshold", "group_summary_time_threshold",
        ) or 10800
        _now = time.time()
        if (
            self._config.group_summary_enabled
            and (
                ctx.message_count_since_last_summary >= _summary_msg_threshold
                or (
                    ctx.last_summary_at > 0
                    and _now - ctx.last_summary_at >= _summary_time_threshold
                )
            )
        ):
            _old_count = ctx.message_count_since_last_summary
            _old_summary_at = ctx.last_summary_at
            ctx.message_count_since_last_summary = 0
            ctx.last_summary_at = _now
            try:
                from astrbot_plugin_suli_intelligence import GroupSummarizer
                async def _do_summarize_and_store():
                    result = await GroupSummarizer.summarize(
                        tavern=self._tavern,
                        ctx=ctx,
                        config=self._config,
                        bot_id=self._current_bot_id or "",
                    )
                    if result:
                        ctx.summary = result
                        logger.info(
                            "群 %d: 摘要已写入 (%d 字)",
                            ctx.group_id, len(result),
                        )
                safe_task(_do_summarize_and_store(), name=f"summarize:{group_id}")
                logger.info(
                    "群 %d: GroupSummarizer 触发 (msgs=%d, since_last=%ds)",
                    group_id, _old_count,
                    int(_now - _old_summary_at) if _old_summary_at else 0,
                )
            except Exception:
                logger.warning("群 %d: GroupSummarizer 启动失败", group_id, exc_info=True)

        # 5.1 Bot 行为追踪 (Layer 0 — 滚动嫌疑分累积, 0 LLM 成本)
        if self._config.abuse_bot_detection_enabled:
            try:
                from astrbot_plugin_suli_guards import BotDetector
                # 判断此消息是否因 @/回复 触发
                _is_triggered = (
                    self._is_bot_mentioned(event, bot)
                    or self._is_nickname_mentioned(content, str(bot.self_id) if bot else "")
                )
                # 需要 await 检查回复触发
                _is_reply = False
                try:
                    _is_reply = await self._is_reply_to_bot(event, bot)
                except Exception:
                    pass
                BotDetector.feed(
                    self._current_bot_id, user_id, user_name, content, ctx,
                    is_triggered=(_is_triggered or _is_reply),
                )
            except Exception:
                pass

        # 5.5 World Book 状态更新 (关键词扫描 + 计时器推进)
        wb_buffer.feed_message(
            user_id=user_id,
            user_name=user_name,
            content=content,
        )

        # 6. 上下文压缩 (超阈值时 LLM 摘要替代直接截断)
        await maybe_compress(
            ctx,
            config=self._config,
            chat_param_fn=self._chat_param,
            tavern=self._tavern,
            resolve_provider_fn=self._resolve_provider,
            record_usage_fn=self._record_usage,
            llm_semaphore=get_llm_semaphore(self._current_bot_id or ""),
            sanitize_fn=_sanitize_image_urls_v2,
        )

        # 7. 存储 bot/event 引用 (供 debounce 回调使用)
        ctx.last_bot = bot
        ctx.last_event = event

        # 8. @提及 / 回复 — 立即触发 (绕过冷却和热度)
        #    @提及和回复是 QQ 平台级强信号 — 用户主动操作, 零歧义
        #    其他情况 (昵称/话题/线程) 走 batch/debounce → Stage 1 Relevance Gate
        _at_trigger = self._is_bot_mentioned(event, bot)
        _reply_trigger = await self._is_reply_to_bot(event, bot) if not _at_trigger else False
        _nick_trigger = (
            not _at_trigger and not _reply_trigger
            and self._is_nickname_mentioned(content, str(bot.self_id) if bot else "")
        )
        _is_strong_signal = _at_trigger or _reply_trigger or _nick_trigger

        # ── @ 特权: 两槽皆热 + 触发者非参与者 → "稍等" 应答 (不占槽) ──
        if _is_strong_signal:
            _slot_mgr = get_slot_manager()
            _bid = self._current_bot_id
            if not _slot_mgr.is_participant_in_active_slot(_bid, group_id, user_id):
                _active = _slot_mgr.get_active_slots(_bid, group_id)
                if len(_active) >= 2:
                    # 两槽皆热 → 轻量"稍等"，不抢占，不创建新槽
                    _trigger_name = "at" if _at_trigger else "reply" if _reply_trigger else "nickname"
                    logger.info(
                        "群 %d: 两槽皆热 %s 被推开 user=%s slots=%d",
                        group_id, _trigger_name, user_id[:8], len(_active),
                    )
                    try:
                        # per-bot 个性化忙碌应答
                        _char = self._resolve_character(self._current_bot_id).get("name", "")
                        _busy_msg = (
                            "呜哇，人家正在回别人的消息呢，等一下下喵～"
                            if _char == ""
                            else "正在处理其他对话，请稍候。"
                        )
                        await bot.send(event, _busy_msg)
                    except Exception:
                        pass
                    # 同时加热 slot（低热度→被推开，但留下痕迹方便后续余温恢复）
                    try:
                        await _slot_mgr.heat_slot(
                            _bid, group_id,
                            topic_anchor=content[:100] if content else "",
                            user_id=user_id, user_name=user_name,
                            heat_amount=_slot_mgr.HEAT_KEYWORD,
                            text=content,
                        )
                    except Exception:
                        pass
                    return

        if _at_trigger:
            self._cancel_debounce(group_id)
            logger.info("群 %d: @提及触发 (heat=%.1f)", group_id, ctx.heat)
            # 加热关注槽
            try:
                await get_slot_manager().heat_slot(
                    self._current_bot_id, group_id,
                    topic_anchor=content[:100] if content else "",
                    user_id=user_id, user_name=user_name,
                    is_at=True, text=content,
                )
            except Exception:
                pass
            await self._schedule_trigger(ctx, trigger_reason="mention", content=content)
            return
        if _reply_trigger:
            self._cancel_debounce(group_id)
            logger.info("群 %d: 回复触发 (heat=%.1f)", group_id, ctx.heat)
            try:
                await get_slot_manager().heat_slot(
                    self._current_bot_id, group_id,
                    topic_anchor=content[:100] if content else "",
                    user_id=user_id, user_name=user_name,
                    is_at=True, text=content,
                )
            except Exception:
                pass
            await self._schedule_trigger(ctx, trigger_reason="reply", content=content)
            return

        # 8.4 昵称提及 — 文本中包含 bot 昵称视为强信号, 立即触发
        if _nick_trigger:
            self._cancel_debounce(group_id)
            logger.info("群 %d: 昵称触发 (heat=%.1f)", group_id, ctx.heat)
            try:
                await get_slot_manager().heat_slot(
                    self._current_bot_id, group_id,
                    topic_anchor=content[:100] if content else "",
                    user_id=user_id, user_name=user_name,
                    is_at=True, text=content,
                )
            except Exception:
                pass
            await self._schedule_trigger(ctx, trigger_reason="nickname", content=content)
            return

        # 8.4b 关注槽短路 (E3): 活跃参与者消息绕过冷却 → thread_continuation
        # ── 2026-06-27 收束守卫: bot 刚回复过该用户 (<3s) 不立即再次触发 ──
        # 关注槽原设计"无缝追踪对话"是对的，但无冷却导致用户连发时 bot 逐条追回。
        # 3s 窗口让 bot 的回复先送达，让用户看到后再决定要不要继续——而不是
        # bot 在用户打字期间就抢先回了好几条。
        if get_slot_manager().is_participant_in_active_slot(
            self._current_bot_id, group_id, user_id,
        ):
            _FOCUS_COOLDOWN = 3.0
            _bot_id = self._current_bot_id or ""
            _gid = str(group_id)
            _recent = self._self_behavior.recently_addressed(
                _bot_id, _gid, user_id=user_id,
                max_age_seconds=_FOCUS_COOLDOWN,
            )
            if _recent is not None:
                _age = time.time() - _recent.timestamp
                logger.info(
                    "群 %d: 关注槽冷却 — %.1fs 前刚回复过 user=%s, 跳过",
                    group_id, _age, user_id[:8],
                )
                return
            self._cancel_debounce(group_id)
            logger.info("群 %d: 关注槽短路 user=%s", group_id, user_id[:8])
            # ── 裸图消振: 用户打字后接表情包时, 表情包在 Gate 处理期间到达 ──
            # 表情包是当前对话回合的延伸, 不是新的独立消息。若此时调度
            # thread_continuation → pending trigger → Gate 跑完后立刻又调一次
            # Gate 处理表情包 → 用户收到两条回复 (回文字 + 回表情包)。
            # 正确行为: 表情包被当前回合吸收, 不单独触发。URL 已存入
            # _url_to_file_id (main.py:1194), 若当前 Gate 授权 VLM,
            # 交叉消息查找会找到它。
            _cur_content = (content or "").strip()
            _is_bare_image = bool(
                _cur_content
                and re.match(
                    r"^\[图片(?:×\d+)?(?:\s*URL:\s*\S+)?\]"
                    r"(?:\s*\[图片(?:×\d+)?(?:\s*URL:\s*\S+)?\])*\s*$",
                    _cur_content,
                )
            )
            if _is_bare_image:
                _pkey = f"{self._current_bot_id or ''}:{group_id}"
                if _pkey in self._processing_groups:
                    logger.info(
                        "群 %d: 裸图在 Gate 处理期间到达 → 合并到当前回合, 不单独触发",
                        group_id,
                    )
                    return
            try:
                await get_slot_manager().heat_slot(
                    self._current_bot_id, group_id,
                    topic_anchor=content[:100] if content else "",
                    user_id=user_id, user_name=user_name,
                    heat_amount=get_slot_manager().HEAT_KEYWORD,
                    text=content,
                )
            except Exception:
                pass
            await self._schedule_trigger(ctx, trigger_reason="thread_continuation", content=content)
            return

        # 8.5 basic 等级: 只响应 immediate triggers，跳过所有自动触发
        if self.get_group_tier(group_id) == "basic":
            logger.info("群 %d: basic tier 跳过自动触发", group_id)
            return

        # 9. 冷却期 — 只存消息不触发，但仍重置 debounce
        cooldown = self._chat_param("group_chat_cooldown_seconds", "group_chat_cooldown_seconds")
        if ctx.is_on_cooldown(cooldown):
            self._reset_debounce(
                group_id,
                self._chat_param("group_chat_debounce_seconds", "group_chat_debounce_seconds"),
            )
            logger.info("群 %d: 冷却期跳过 (cooldown=%ds)", group_id, cooldown)
            return

        # 10. batch 触发 — 热度不足时降低频率
        recent = ctx.messages_since_last_reply
        batch_size = self._chat_param("group_chat_batch_size", "group_chat_batch_size")
        if len(recent) >= batch_size:
            # 领域活跃时降低触发门槛
            _batch_threshold = self._config.heat_active_threshold * 0.5
            if self._config.domain_detection_enabled and ctx.active_domains:
                _batch_threshold *= self._config.domain_trigger_boost
            # 能量调制: 疲劳时门槛抬升 (energy 0.1 → 门槛 ×10)
            if getattr(self._config, "energy_enabled", True) and ctx.energy > 0:
                _batch_threshold /= ctx.energy
            if ctx.heat >= _batch_threshold:
                # ── 消歧: 同 debounce 路径的硬过滤 ──
                if self._all_recent_msgs_for_other_bot(ctx, str(getattr(ctx.last_bot, "self_id", "") or "")):
                    logger.info(
                        "群 %d: batch 跳过 (最近消息均为呼叫)",
                        group_id,
                    )
                else:
                    # ── 2026-06-29 P1-8: batch 交织检测 ──
                    # 最近 batch 内若多个用户插话聊了不同话题 (无单一主导聚合者),
                    # 标 batch_mixed → Gate 倾向 directed_to_me=false, 避免回错话题/同时回多个。
                    _batch_reason = self._classify_batch_reason(recent)
                    logger.info(
                        "群 %d: batch 触发 (%d 条, heat=%.1f, reason=%s)",
                        group_id, len(recent), ctx.heat, _batch_reason,
                    )
                    await self._schedule_trigger(ctx, trigger_reason=_batch_reason)
                    return
            else:
                logger.info(
                    "群 %d: batch 跳过 (heat=%.1f < %.1f, energy=%.2f)",
                    group_id, ctx.heat, _batch_threshold, ctx.energy,
                )

        # 11. 重置 debounce 定时器 (热度不足时跳过触发)
        self._reset_debounce(
            group_id,
            self._chat_param("group_chat_debounce_seconds", "group_chat_debounce_seconds"),
        )

    # ── @提及 / 昵称 / 回复 检测 ─────────────────────

    def _is_bot_mentioned(self, event: GroupMessageEvent, bot: Bot) -> bool:
        """检查消息中是否 @ 了机器人。

        某些 OneBot 适配器 (NapCat/LLOneBot) 会从 get_message() 中剥离发给 bot
        自己的 at 段，导致 segment 遍历永远找不到。因此同时检查 raw JSON。
        """
        try:
            # 方法 1: 遍历 segments (标准路径)
            for seg in event.get_message():
                if seg.type == "at":
                    qq = str(seg.data.get("qq", ""))
                    if qq == str(bot.self_id):
                        return True
            # 方法 2: 检查 raw_message (NapCat/LLOneBot fallback)
            raw = ""
            try:
                raw = event.raw_message or ""
            except Exception:
                pass
            if raw:
                bot_id = str(bot.self_id)
                # raw_message 格式示例: [CQ:at,qq={bot_id},name=BotName] 出来
                if f"[CQ:at,qq={bot_id}" in raw:
                    return True
        except Exception:
            logger.debug("群消息 @检测异常，跳过", exc_info=True)
        return False

    # 的昵称 — 当 bot 是暮恩时，消息以这些开头说明在对说话
    __NICKNAMES: tuple[str, ...] = ("", "", "")
    # 暮恩的昵称 — 当 bot 是时，消息以这些开头说明在对暮恩说话
    _MOON_NICKNAMES: tuple[str, ...] = ("小暮", "暮暮", "洛宝", "暮恩", "moon")

    def _is_nickname_mentioned(self, content: str, self_id: str = "") -> bool:
        """检查消息内容中是否包含机器人昵称 (大小写不敏感)。

        支持双 Bot: 根据 self_id 自动选择正确的昵称列表。

        重要: 注入的合并转发内容行 (以"[转发]"开头) 不参与昵称检测。
        转发内容是第三方对话，不应被视为"用户在叫我"。
        """
        nicknames = self._chat_param("group_chat_nicknames", "group_chat_nicknames")
        if not nicknames:
            return False
        # ── 剥离注入的转发内容行, 仅检查用户自己的文本 ──
        # 格式: "[转发] 发送者: 消息内容" — 这些行是第三方对话被分享
        import re
        _clean = re.sub(r'^\s*\[转发\][^\n]*', '', content, flags=re.MULTILINE)
        lower = _clean.lower()

        # ── 双 Bot 消歧: 根据当前 bot 身份选择昵称 ──
        _char_name = self._resolve_character(self_id).get("name", "")
        is_ = _char_name == ""

        if is_:
            # 当前是: 检查的昵称，跳过暮恩的昵称
            self_nicknames = ("", "娜娜", "", "")
            other_nicknames = self._MOON_NICKNAMES
            # 消息以暮恩昵称开头 → 在对暮恩说话，不触发
            if any(lower.startswith(n) for n in other_nicknames):
                return False
        else:
            # 当前是暮恩: 检查暮恩的昵称，跳过的昵称
            self_nicknames = nicknames
            other_nicknames = self.__NICKNAMES
            # 消息以昵称开头 → 在对说话，暮恩不触发
            if any(lower.startswith(n) for n in other_nicknames):
                return False

        # ── 消歧(扩展): 消息明确在叫另一个 bot 且不含自己的昵称 → 不触发 ──
        _has_other = any(n in lower for n in other_nicknames)
        _has_self = any(n in lower for n in self_nicknames)
        if _has_other and not _has_self:
            return False
        return _has_self

    def _classify_batch_reason(self, recent: list) -> str:
        """判断 batch 触发是否为多用户多话题交织。

        2026-06-29 P1-8: A 聊 X、B 插话聊 Y 交替填 batch_size 时, 10 条混合消息无线程分隔,
        bot 可能回错人或同时回两个话题。检测最近 batch 内:
          - 来自 ≥3 个不同用户 且 单一用户占比 < 60% → 判 batch_mixed
          - 否则 (单一主导聚合者 / 少用户) → 普通 batch (信任 Gate 自己从上下文判断)

        Args:
            recent: ctx.messages_since_last_reply — 含 get("user_id") 的消息 dict 列表

        Returns:
            "batch" 或 "batch_mixed"
        """
        if not recent:
            return "batch"
        try:
            _user_msgs = [
                str(m.get("user_id", ""))
                for m in recent
                if str(m.get("user_id", "")) and not str(m.get("user_id", "")).startswith("bot_")
            ]
        except Exception:
            return "batch"
        if len(_user_msgs) < 4:
            return "batch"  # 太少消息不足以下"交织"结论
        _distinct = set(_user_msgs)
        if len(_distinct) < 3:
            return "batch"  # ≤2 人 — 即使交替也属合理多对一会话
        _top = max(sum(1 for u in _user_msgs if u == _d) for _d in _distinct)
        if _top / len(_user_msgs) >= 0.6:
            return "batch"  # 有单一主导聚合者 — 仍以一人话头为主
        return "batch_mixed"

    def _all_recent_msgs_for_other_bot(self, ctx, self_id: str = "") -> bool:
        """检查 debounce 前最近消息是否全部在呼叫另一个 bot。

        双 Bot 消歧: 根据 self_id 判断当前 bot 身份，反向检测。
        - 当前是暮恩 → 检测是否全部在叫 (是则跳过)
        - 当前是 → 检测是否全部在叫暮恩 (是则跳过)

        Returns:
            True = 所有最近用户消息都是对另一个 bot 说的 → 应跳过
        """
        nicknames = self._chat_param("group_chat_nicknames", "group_chat_nicknames")
        if not nicknames:
            return False

        # 取最近一次 bot 发言之后的所有用户消息
        recent = ctx.messages_since_last_reply
        if not recent:
            return False

        user_msgs = [
            m for m in recent
            if not str(m.get("user_id", "")).startswith("bot_")
        ]
        if not user_msgs:
            return False

        _char_name = self._resolve_character(self_id).get("name", "")
        is_ = _char_name == ""

        if is_:
            # 当前是: 检查是否全部在叫暮恩
            self_nicknames = ("", "娜娜", "", "")
            other_nicknames = self._MOON_NICKNAMES
        else:
            # 当前是暮恩: 检查是否全部在叫
            self_nicknames = nicknames
            other_nicknames = self.__NICKNAMES

        _peer_qq = str(self._chat_param("peer_bot_qq", "peer_bot_qq") or "")
        from astrbot_plugin_suli_guards.dual_bot import get_peer_qq
        _other_qq = get_peer_qq(str(self_id))

        for msg in user_msgs:
            uid = str(msg.get("user_id", ""))
            if _peer_qq and uid == _peer_qq:
                continue  # 跳过另一个 bot 自己的消息
            if uid == _other_qq:
                continue  # 跳过另一个 bot (冗余保护)

            msg_content = str(msg.get("content", "")).lower()
            _has_other = any(n in msg_content for n in other_nicknames)
            _has_self = any(n in msg_content for n in self_nicknames)
            # 这条消息叫了自己 → 不跳过
            if _has_self:
                return False
            # 这条消息没叫另一个 bot → 不跳过 (有人在聊别的)
            if not _has_other:
                return False

        # 所有用户消息都在叫另一个 bot 且没人叫自己 → 跳过
        return True

    def _consecutive_bot_msg_count(self, ctx, max_lookback: int = 6) -> int:
        """计算最近消息中连续 bot 消息的数量 (用于防对话螺旋)。

        从最新消息往回数，遇到人类消息时停止。
        bot 包括: 自身 (bot_* 前缀) + 对照 bot (peer_bot_qq)。

        Returns:
            连续 bot 消息数 (0 = 最近一条是人类)。
        """
        msgs = ctx.messages
        if not msgs:
            return 0
        _peer_qq = str(self._chat_param("peer_bot_qq", "peer_bot_qq") or "")
        count = 0
        for msg in reversed(msgs[-max_lookback:]):
            uid = str(msg.get("user_id", ""))
            if uid.startswith("bot_") or (_peer_qq and uid == _peer_qq):
                count += 1
            else:
                break  # 遇到人类消息 → 停止计数
        return count

    async def _check_mention_intent_gate(
        self, content: str, user_name: str, group_id: int,
        trigger_type: str, ctx,
    ) -> bool:
        """意图门控: 判断触发是「呼叫 bot」还是「讨论 bot/同名事物」。

        Args:
            content: 当前消息内容
            user_name: 发送者昵称
            group_id: 群号
            trigger_type: 触发类型 ("@提及" / "昵称" / "回复")
            ctx: GroupChatContext

        Returns:
            True = 放行 (应该回复), False = 拦截 (不应回复)
            fail-open: 异常时返回 True
        """
        if not getattr(self._config, "mention_intent_gate_enabled", True):
            return True
        try:
            from ..intelligence.mention_intent_gate import MentionIntentGate
            recent_lines = []
            for msg in ctx.messages[-8:]:
                name = str(msg.get("user_name", "?"))
                text = _sanitize_image_urls_v2(str(msg.get("content", "")))
                if len(text) > 150:
                    text = text[:147] + "..."
                recent_lines.append(f"{name}: {text}")
            gate_decision = await MentionIntentGate.decide(
                tavern=self._tavern,
                content=content,
                sender_name=user_name,
                recent_context=recent_lines,
                config=self._config,
            )
            if gate_decision == "ignore":
                logger.info(
                    "群 %d: %s触发被意图门控拦截 user=%s text=%.60s",
                    group_id, trigger_type, user_name, content,
                )
                return False
            return True
        except Exception:
            logger.warning(
                "群 %d: 意图门控异常, fail-closed → 拒绝",
                group_id, exc_info=True,
            )
            return False

    async def _is_reply_to_bot(
        self, event: GroupMessageEvent, bot: Bot
    ) -> bool:
        """检查消息是否引用了 bot 的消息 (QQ 回复)。

        两条检测路径:
        1. EventAdapter 已解析段 (含 qq 字段 — 由 _parse_message 填充)
        2. Raw message_obj 兜底 (AstrBot Reply 组件未映射 qq 时)
        """
        bot_qq = str(bot.self_id)
        # ── 路径 1: EventAdapter 段 ──
        try:
            for seg in event.get_message():
                if seg.type == "reply":
                    qq = str(seg.data.get("qq", ""))
                    if qq and qq == bot_qq:
                        return True
        except Exception:
            pass

        # ── 路径 2: raw message_obj 兜底 ──
        try:
            raw_event = getattr(event, "_event", None)
            if raw_event is not None:
                msg_obj = getattr(raw_event, "message_obj", None)
                raw_message = (
                    getattr(msg_obj, "message", None)
                    if msg_obj is not None else None
                )
                if isinstance(raw_message, list):
                    for seg in raw_message:
                        if isinstance(seg, dict) and seg.get("type") == "reply":
                            qq = str(seg.get("data", {}).get("qq", ""))
                            if qq and qq == bot_qq:
                                return True
        except Exception:
            logger.debug("群消息 回复检测异常 (raw 兜底)", exc_info=True)

        return False

    # ── 对话线程追踪 ─────────────────────────────────

    async def _check_thread_continuation(
        self, ctx: GroupChatContext, user_id: str, user_name: str,
        user_message: str,
    ) -> bool:
        """检测用户是否在持续与 bot 对话 — 规则粗筛 + Agent 语义判断。

        两级:
          1. 规则粗筛: 时间窗口 + 消息窗口 (零成本)
          2. Agent 语义判断: continue/fading/end (轻量 LLM)

        返回 True → 线程延续，立即触发 (类似 @mention 优先级)
        返回 False → 线程可能仍在 (fading) 或已结束 (end)，走正常路径
        """
        # ── 规则粗筛: 必须有活跃线程且在双窗口内 ──
        thread = ctx.conversation_threads.get(user_id)
        if not thread:
            return False

        now = time.time()
        time_window = self._chat_param("thread_window_seconds", "thread_window_seconds")
        msg_window = self._chat_param("thread_window_messages", "thread_window_messages")

        last_bot = thread.get("last_bot_reply_at", 0)
        time_since = now - last_bot
        if time_since > time_window * 2:  # 超时 2 倍 → 彻底过期
            del ctx.conversation_threads[user_id]
            return False

        new_msgs = [
            m for m in ctx.messages if m["timestamp"] > last_bot
        ]
        msgs_since = len(new_msgs)

        if time_since > time_window or msgs_since > msg_window:
            # 超出粗筛窗口 → 不触发，但保留线程给 agent 复活机会
            logger.debug(
                "群 %d: 线程粗筛未过 user=%s age=%.0fs msgs=%d (窗口=%ds/%d)",
                ctx.group_id, user_id[:8], time_since, msgs_since,
                time_window, msg_window,
            )
            return False

        # ── Agent 语义判断: 用户真的在跟我继续对话吗？ ──
        try:
            from ..intelligence.conversation_agent import ConversationAgent

            # 构建线程上下文 (bot 与用户最近几轮交换)
            thread_msgs = _extract_thread_context(ctx.messages, user_id)

            _char_name = self._resolve_character(self._current_bot_id).get("name", "暮恩")
            decision = await ConversationAgent.decide(
                tavern=self._tavern,
                thread_context=thread_msgs,
                user_name=user_name,
                user_message=user_message,
                time_since_reply=time_since,
                msgs_since_reply=msgs_since,
                config=self._config,
                bot_name=_char_name,
            )

            if decision == "continue":
                return True
            if decision == "fading":
                # 模糊 → 不触发线程延续，但保留线程 (等下次更明确)
                logger.info(
                    "群 %d: Agent 判定 fading user=%s — 保留线程等待",
                    ctx.group_id, user_id[:8],
                )
                return False
            # end
            del ctx.conversation_threads[user_id]
            logger.info(
                "群 %d: Agent 判定对话结束 user=%s",
                ctx.group_id, user_id[:8],
            )
            return False
        except Exception:
            logger.debug("ConversationAgent 异常，fallback 规则判定", exc_info=True)
            # Fallback: 纯规则 — 在窗口内就放行
            return True

    def _update_thread(
        self, ctx: GroupChatContext, user_id: str,
        user_name: str = "",
        trigger_reason: str = "",
    ) -> None:
        """更新对话线程状态 — bot 回复后调用。

        线程生命周期:
          - mention/nickname/reply: 创建或更新线程 (显式互动)
          - thread_continuation: 更新线程 (延续互动)
          - 其它触发原因: 不创建线程 (batch/debounce/proactive 不属对话)
        """
        if not user_id:
            return

        now = time.time()

        if trigger_reason in ("mention", "nickname", "reply", "thread_continuation"):
            thread = ctx.conversation_threads.get(user_id, {})
            if not thread:
                # 新建线程
                thread = {
                    "user_name": user_name,
                    "started_at": now,
                    "exchange_count": 0,
                }
                logger.info(
                    "群 %d: 新建对话线程 user=%s name=%s reason=%s",
                    ctx.group_id, user_id[:8], user_name, trigger_reason,
                )
            thread["last_bot_reply_at"] = now
            thread["last_user_msg_at"] = now  # 近似: bot回复时用户刚说过话
            thread["exchange_count"] = thread.get("exchange_count", 0) + 1
            thread["user_name"] = user_name or thread.get("user_name", "")
            ctx.conversation_threads[user_id] = thread
        else:
            # batch/debounce/proactive: 清除该用户的线程
            # (非直接互动意味着对话线程自然结束)
            pass  # 保留线程让它自然过期，不主动清除

    def _cleanup_threads(self, ctx: GroupChatContext) -> None:
        """清理所有过期线程 (超时窗口或消息窗口)。"""
        now = time.time()
        time_window = self._chat_param("thread_window_seconds", "thread_window_seconds")
        msg_window = self._chat_param("thread_window_messages", "thread_window_messages")

        expired = []
        for uid, thread in ctx.conversation_threads.items():
            last_bot = thread.get("last_bot_reply_at", 0)
            if now - last_bot > time_window:
                expired.append(uid)
                continue
            new_msgs = [
                m for m in ctx.messages
                if m["timestamp"] > last_bot
            ]
            if len(new_msgs) > msg_window:
                expired.append(uid)

        for uid in expired:
            del ctx.conversation_threads[uid]
            logger.debug("群 %d: 清理过期线程 user=%s", ctx.group_id, uid[:8])

    # ── Debounce 管理 ─────────────────────────────────

    def _reset_debounce(self, group_id: int, delay: int) -> None:
        """取消旧定时器并启动新的 debounce 任务。"""
        key = self._ctx_key(group_id)
        # 取消旧任务
        old = self._debounce_tasks.pop(key, None)
        if old and not old.done():
            old.cancel()

        # 创建新任务
        task = safe_task(self._debounce_worker(group_id, delay))
        self._debounce_tasks[key] = task

    async def _debounce_worker(self, group_id: int, delay: int) -> None:
        """Debounce 等待，到期后触发 LLM 评估。"""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return  # 被新消息重置

        ctx = self._contexts.get(self._ctx_key(group_id))
        if not ctx or not ctx.last_bot or not ctx.last_event:
            return

        # 再次检查状态 (可能在等待期间被关闭)
        if not self.is_group_enabled(group_id):
            return
        if ctx.is_on_cooldown(
            self._chat_param("group_chat_cooldown_seconds", "group_chat_cooldown_seconds"),
        ):
            return

        # 热度不足 → 跳过 debounce 触发
        _debounce_threshold = self._config.heat_active_threshold * 0.25
        if self._config.domain_detection_enabled and ctx.active_domains:
            _debounce_threshold *= self._config.domain_trigger_boost
        # 能量调制: 疲劳时门槛抬升
        if getattr(self._config, "energy_enabled", True) and ctx.energy > 0:
            _debounce_threshold /= ctx.energy
        if ctx.heat < _debounce_threshold:
            logger.debug(
                "群 %d: debounce 跳过 (heat=%.1f < %.1f, energy=%.2f)",
                group_id, ctx.heat, _debounce_threshold, ctx.energy,
            )
            return

        logger.info(
            "群 %d: debounce 触发 (%ds 静默, heat=%.1f)",
            group_id, delay, ctx.heat,
        )

        # ── 消歧: 检查最近消息是否全部在叫 ──
        #     立即触发路径有 _is_nickname_mentioned 硬过滤 (已支持双Bot),
        #     但 debounce 路径只靠 ReplyGate LLM 判断,
        #     flash 模型非确定性, 可能误判 → 硬过滤兜底。
        if self._all_recent_msgs_for_other_bot(ctx, str(getattr(ctx.last_bot, "self_id", "") or "")):
            logger.info(
                "群 %d: debounce 跳过 (最近消息均为呼叫)",
                group_id,
            )
            return

        await self._schedule_trigger(ctx, trigger_reason="debounce")

    # ── 串行化触发调度 ──────────────────────────────

    # ── 累积窗口常量 ──
    ACCUM_WINDOW = 3.0  # 首条触发后等 3s，同用户新文本重置计时

    async def _schedule_trigger(
        self, ctx: GroupChatContext, trigger_reason: str,
        content: str = "",
    ) -> None:
        """3 秒累积窗口 + 串行化 LLM 调用。

        模拟真人对话节奏: 触发后等待 3 秒，同用户新文本消息累积到同一查询，
        裸图（表情包）被吸收。3 秒无新消息 → 一次 Gate → 一条回复。

        不同用户的消息在累积窗口结束后按传统 pending 机制处理。
        过期的触发 (超过 trigger_timeout_seconds) 被丢弃。
        """
        group_id = ctx.group_id
        now = time.time()
        bot_id = self._current_bot_id or ""
        _key = f"{bot_id}:{group_id}"

        # ── 已在处理中 → 累积 or 排队 ──
        if _key in self._processing_groups:
            _current_uid = self._processing_trigger_uids.get(_key, "")
            _new_uid = ""
            try:
                if ctx.last_event:
                    _new_uid = str(ctx.last_event.get_user_id())
            except Exception:
                pass

            # 同用户 → 尝试累积到当前窗口
            if _current_uid and _new_uid == _current_uid:
                _txt = (content or "").strip()
                if not _txt:
                    return  # 空消息无视
                # 裸图（表情包）: 吸收，不累积文本，不重置计时
                if re.match(
                    r"^\[图片(?:×\d+)?(?:\s*URL:\s*\S+)?\]"
                    r"(?:\s*\[图片(?:×\d+)?(?:\s*URL:\s*\S+)?\])*\s*$",
                    _txt,
                ):
                    logger.info(
                        "群 %d: 累积窗口内裸图 → 吸收，不累积",
                        group_id,
                    )
                    return
                # 文本: 累积 + 重置计时
                acc = self._accumulation.get(_key, {})
                _prev = acc.get("text", "")
                _merged = f"{_prev} {_txt}".strip() if _prev else _txt
                acc["text"] = _merged
                _evt = acc.get("event")
                if _evt:
                    _evt.set()
                logger.info(
                    "群 %d: 累积窗口追加 (%d chars) → 共 %d chars，重置 3s 计时",
                    group_id, len(_txt), len(_merged),
                )
                return

            # 不同用户 → 排队 (窗口结束后处理)
            self._pending_triggers[_key] = {
                "reason": trigger_reason,
                "timestamp": now,
                "content": (content or "").strip(),
            }
            logger.debug(
                "群 %d (bot %s): 排队触发 %s (已有累积窗口)",
                group_id, bot_id, trigger_reason,
            )
            return

        # ── 首个触发: 开启累积窗口 ──
        self._processing_groups.add(_key)
        try:
            if ctx.last_event:
                _uid = str(ctx.last_event.get_user_id())
                if _uid:
                    self._processing_trigger_uids[_key] = _uid
        except Exception:
            pass
        _initial_text = (content or "").strip()

        # 记录原始 trigger_reason — 累积窗口始终用首次的 reason
        # （mention 途中跟 thread_continuation 不算新触发）
        _accum_event = asyncio.Event()
        self._accumulation[_key] = {
            "text": _initial_text,
            "event": _accum_event,
            "trigger_reason": trigger_reason,
        }

        max_merge_rounds = 3
        round_num = 0

        # ── Bot 间协调 (ADR-001) ──
        _coord_acquired = False
        if self._coordination:
            _coord_acquired = await self._coordination.try_acquire(
                str(group_id),
                reply_target=(trigger_reason if trigger_reason in ("mention", "reply") else ""),
            )
            if not _coord_acquired:
                logger.info("[coord] 群 %d (bot %s): 发言权被对方持有, 退让", group_id, bot_id)
                self._processing_groups.discard(_key)
                self._processing_trigger_uids.pop(_key, None)
                self._accumulation.pop(_key, None)
                return
        try:
            while True:
                round_num += 1

                # ── 3 秒累积窗口: 等待同用户补充消息 ──
                _accum_event.clear()
                try:
                    await asyncio.wait_for(
                        _accum_event.wait(),
                        timeout=self.ACCUM_WINDOW,
                    )
                    # 新文本到达 → Event 被 set → 重置计时，继续等待
                    logger.debug(
                        "群 %d (bot %s): 累积窗口 — 新文本到达，重置 %.0fs 计时",
                        group_id, bot_id, self.ACCUM_WINDOW,
                    )
                    continue
                except asyncio.TimeoutError:
                    # 3 秒无新消息 → 窗口关闭
                    pass

                # ── 窗口关闭: 取出累积文本 ──
                _acc_data = self._accumulation.pop(_key, {})
                _merged_text = _acc_data.get("text", "") or ""
                _orig_reason = _acc_data.get("trigger_reason", trigger_reason)
                if _merged_text:
                    logger.info(
                        "群 %d: 累积窗口关闭 → 合并文本 (%d chars) → Gate",
                        group_id, len(_merged_text),
                    )

                # ── 一次 Gate 调用 ──
                await self._evaluate_and_reply(
                    ctx, _orig_reason, accumulated_text=_merged_text,
                )

                # ── 窗口后: 检查排队的不同用户触发 ──
                pending = self._pending_triggers.pop(_key, None)
                if pending is None:
                    break

                # 过期检查
                age = time.time() - pending["timestamp"]
                if age > self._trigger_timeout:
                    logger.info(
                        "群 %d (bot %s): 丢弃过期触发 %s (已过 %.0fs > %.0fs)",
                        group_id, bot_id, pending["reason"], age, self._trigger_timeout,
                    )
                    continue

                if round_num >= max_merge_rounds:
                    logger.warning(
                        "群 %d (bot %s): 合并轮次已达上限 %d，强制退出",
                        group_id, bot_id, max_merge_rounds,
                    )
                    break

                # 排队触发 → 开新累积窗口
                trigger_reason = pending["reason"]
                _pending_content = pending.get("content", "")
                logger.debug(
                    "群 %d (bot %s): 处理排队触发 %s (第 %d/%d 轮)",
                    group_id, bot_id, trigger_reason, round_num, max_merge_rounds,
                )
                # 更新 trigger_uid (现在是另一个用户)
                try:
                    if ctx.last_event:
                        _uid = str(ctx.last_event.get_user_id())
                        if _uid:
                            self._processing_trigger_uids[_key] = _uid
                except Exception:
                    pass
                # 为排队触发开新累积窗口
                _accum_event = asyncio.Event()
                self._accumulation[_key] = {
                    "text": _pending_content,
                    "event": _accum_event,
                    "trigger_reason": trigger_reason,
                }
                # 回到循环顶部，进入累积窗口等待
        finally:
            if _coord_acquired and self._coordination:
                await self._coordination.release(str(group_id))
            self._processing_groups.discard(_key)
            self._pending_triggers.pop(_key, None)
            self._processing_trigger_uids.pop(_key, None)
            self._accumulation.pop(_key, None)

    # ── LLM 评估 ──────────────────────────────────────

    # ── 显式 Handoff 日志 (路由可追踪 — 终结黑箱) ─────────────

    @staticmethod
    def _fmt_gate(gate_result: GateResultProtocol | None) -> str:
        """格式化 Gate 输出为紧凑字符串，用于 handoff 日志。"""
        if gate_result is None:
            return "gate=bypass"
        intent = gate_result.intent_type or "?"
        domain = gate_result.domain or "?"
        tools = list(gate_result.suggested_tools) if gate_result.suggested_tools else []
        tier = gate_result.model_tier or "?"
        style = gate_result.reply_style or "?"
        stance = gate_result.reply_stance or ""
        voice = gate_result.voice_boundary or ""
        parts = [f"intent={intent}", f"domain={domain}", f"tools={tools}", f"tier={tier}"]
        if style:
            parts.append(f"style={style}")
        if stance:
            parts.append(f"stance={stance}")
        if voice:
            parts.append(f"voice={voice}")
        return " ".join(parts)

    def _log_handoff(
        self, group_id: int, from_stage: str, to_lane: str,
        reason: str = "", gate_result: GateResultProtocol | None = None,
        trigger: str = "", extra: str = "",
    ) -> None:
        """显式路由交接日志——每次链路切换一条记录，可 grep 追踪。

        grep "HANDOFF" 即可追踪任意一条消息的完整路由路径。
        终结"黑箱、零日志、三轮法医调查"问题。

        Args:
            group_id: 群号
            from_stage: 来源阶段 (Gate / Pipeline / Budget / Force)
            to_lane: 目标链路 (Skip / Silence / Arbitration / DeepQA / Reply / Bypass / BudgetBlock)
            reason: 切换原因
            gate_result: Gate 输出 (可选)
            trigger: 触发原因 (mention/nickname/reply/batch/debounce/...)
            extra: 额外信息
        """
        _gate_str = self._fmt_gate(gate_result) if gate_result else ""
        _trigger_str = f" trigger={trigger}" if trigger else ""
        _extra_str = f" {extra}" if extra else ""
        _gate_str = f" [{_gate_str}]" if _gate_str else ""
        logger.info(
            "HANDOFF %s→%s group=%d reason=%s%s%s%s",
            from_stage, to_lane, group_id, reason,
            _trigger_str, _gate_str, _extra_str,
        )

    async def _maybe_send_rejection_sticker(
        self,
        trigger_event,
        trigger_reason: str,
        trigger_uid: str,
        reason_tag: str,
        ctx,
    ) -> bool:
        """直接呼叫被拒时发一个表情包，证明 bot 还活着。

        仅对用户明确呼叫（@ / 回复 / 昵称 / 追问）触发，
        per-group 冷却期内不重复发送。
        """
        if trigger_reason not in ("mention", "reply", "nickname", "thread_continuation"):
            return False

        _bot_id = self._current_bot_id or ""
        _cooldown_key = f"{_bot_id}:{ctx.group_id}"
        _now = time.time()
        _last = self._rejection_sticker_cooldowns.get(_cooldown_key, 0)
        if _now - _last < _REJECTION_STICKER_COOLDOWN:
            return False

        tag = _REJECTION_STICKER_TAG.get(reason_tag, "无语")

        try:
            await send_sticker_direct(
                trigger_event, tag,
                bot=ctx.last_bot,
                group_id=str(ctx.group_id),
            )
        except Exception:
            logger.debug("拒绝反应表情包发送失败", exc_info=True)
            return False
        else:
            self._rejection_sticker_cooldowns[_cooldown_key] = _now
            logger.info(
                "群 %d: 拒绝反应表情包已发送 tag=%s reason=%s user=%s",
                ctx.group_id, tag, reason_tag, trigger_uid[:8] if trigger_uid else "?",
            )
            return True

    async def _evaluate_and_reply(
        self, ctx: GroupChatContext,
        trigger_reason: str = "mention",
        accumulated_text: str = "",
    ) -> None:
        """构建群聊 prompt，调用 LLM (含 function calling)，发送回复 (或静默)。

        Args:
            ctx: 群聊上下文
            trigger_reason: 触发原因 — "mention" | "nickname" | "reply"
                            | "thread_continuation" | "batch" | "batch_mixed" | "debounce" | "proactive"
            accumulated_text: 3 秒累积窗口内合并的同用户文本。
                              非空时替换 trigger_content，含首条触发消息。
        """
        if not ctx.last_bot or not ctx.last_event:
            return

        # ── ★ 捕获触发事件: 必须在方法入口立即保存 ctx.last_event ──
        #     on_message() 会在处理期间 (3-10s LLM 管线) 接收新消息并无条件
        #     覆写 ctx.last_event。若不用 _trigger_event 固定原始触发消息,
        #     发送时的 Reply 组件会引用到后续无关消息 → 引用跳转定位不准。
        _trigger_event = ctx.last_event

        # ── 每群锁: 防止同群并发 _evaluate_and_reply 导致上下文竞态 ──
        lock = self._get_group_lock(ctx.group_id)
        async with lock:
            # ── Token 预算熔断: 入口检查 ──
            _budget_status = self._check_token_budget()
            if _budget_status == "hard_capped":
                self._log_handoff(
                    ctx.group_id, "Budget", "HardBlock",
                    reason="token_quota_exhausted", trigger=trigger_reason,
                )
                try:
                    # per-bot 个性化 token 预算耗尽消息
                    _char = self._resolve_character(self._current_bot_id).get("name", "")
                    _budget_msg = (
                        "呜…人家今天的 token 额度已经用完了喵，明天再来找人家玩嘛～"
                        if _char == ""
                        else "今日 token 配额已用尽，明天刷新。"
                    )
                    await ctx.last_bot.send(_trigger_event, _budget_msg)
                except Exception:
                    pass
                ctx.last_reply_time = time.time()
                return
            # soft_capped: 允许基础回复，但跳过 Opus/ReAct 高成本调用
            if _budget_status == "soft_capped":
                logger.info(
                    "群 %d: token 预算软上限 — 仅基础回复 (bot=%s)",
                    ctx.group_id, (self._current_bot_id or "")[:8],
                )

            _bot_id = self._current_bot_id or ""
            _gid = str(ctx.group_id)

            # ── 语义触发合并 (4.2): 刚回过同一人→跳过非直接呼叫的后续触发 ──
            if trigger_reason in ("batch", "batch_mixed", "debounce", "proactive"):
                _recent = self._self_behavior.most_recent(_bot_id, _gid)
                if _recent is not None:
                    _age = time.time() - _recent.timestamp
                    if _age < 30.0:
                        self._log_handoff(
                            ctx.group_id, "TriggerMerge", "Skip",
                            reason=f"recently_replied_age={_age:.0f}s_target={_recent.target_user_id[:8]}",
                            trigger=trigger_reason,
                        )
                        logger.debug(
                            "群 %d: 语义触发合并 → 跳过 (%.0fs 前刚回复过 %s)",
                            ctx.group_id, _age, _recent.target_user_id[:8],
                        )
                        ctx.last_reply_time = time.time()
                        return

            # ── 交叉验证检查 (Phase D) ──
            challenge_info: dict | None = None
            cfg = self._config
            char_name: str = self._resolve_character(getattr(ctx.last_bot, "self_id", "") if ctx.last_bot else "").get("name", "暮恩")
            # 昵称唤醒词 — 从 BotIdentityService 获取
            _bot_nicknames = ""
            try:
                _bot_id = getattr(ctx.last_bot, "self_id", "") if ctx.last_bot else ""
                _identity_svc = get_bot_identity_service()
                _bot_identity = _identity_svc.get_bot(str(_bot_id)) if _bot_id else None
                if _bot_identity and _bot_identity.nicknames:
                    # 过滤掉与 char_name 重复的
                    _filtered = [n for n in _bot_identity.nicknames if n != char_name]
                    if _filtered:
                        _bot_nicknames = "、".join(_filtered)
            except Exception:
                _bot_nicknames = ""
            logger.info(
                "群 %d: [NAME DEBUG] self_id=%r char_name=%r _my_name=%r _peer_name=%r",
                ctx.group_id,
                getattr(ctx.last_bot, "self_id", "N/A") if ctx.last_bot else "no_bot",
                char_name,
                self._my_name,
                self._peer_name,
            )
            if cfg.cross_validation_enabled and ctx.messages:
                # 取最近一条用户消息 (非 bot 消息, 也排除对照 bot )
                _peer_qq = str(self._chat_param("peer_bot_qq", "peer_bot_qq") or "")
                for msg in reversed(ctx.messages):
                    uid = str(msg.get("user_id", ""))
                    if uid.startswith("bot_") or (_peer_qq and uid == _peer_qq):
                        continue
                    if uid:
                        user_content = msg.get("content", "")

                        if detect_challenge(user_content):
                            logger.info(
                                "群 %d: 检测到质疑信号，启动交叉验证",
                                ctx.group_id,
                            )
                            validator = CrossValidator(tavern=self._tavern)
                            try:
                                _sem = get_llm_semaphore(self._current_bot_id or "")
                                async with _sem:
                                    challenge_info = await validator.validate(
                                        ctx_messages=ctx.messages,
                                        user_message=user_content,
                                        group_id=str(ctx.group_id),
                                        config=cfg,
                                    )
                                # 附加质疑者 QQ 号 (用于管理员检查)
                                challenge_info["user_id"] = uid
                                logger.info(
                                    "群 %d: 交叉验证完成 → %s",
                                    ctx.group_id,
                                    challenge_info.get("verdict", "?"),
                                )
                            except Exception:
                                logger.error(
                                    "群 %d: 交叉验证异常",
                                    ctx.group_id,
                                    exc_info=True,
                                )
                        break  # 只检查最近一条用户消息

            trigger_uid = (
                str(_trigger_event.get_user_id())
                if _trigger_event and trigger_reason in ("mention", "nickname", "reply", "thread_continuation")
                else ""
            )
            trigger_content = (
                _trigger_event.get_plaintext().strip()
                if _trigger_event and trigger_reason in ("mention", "nickname", "reply", "thread_continuation")
                else ""
            )
            # ── 3 秒累积窗口: 用合并后的文本替换 trigger_content ──
            if accumulated_text:
                trigger_content = accumulated_text
            trigger_user_name = (
                str(_trigger_event.sender.card or _trigger_event.sender.nickname)
                if _trigger_event and trigger_reason in ("mention", "nickname", "reply", "thread_continuation")
                else ""
            )

            # ── ★ Per-User 回复冷却: 同一用户在 N 秒内不重复回复 ──
            #     防止用户连发多条消息时 bot 挨个回复。
            #     仅对 attention-seeking 触发 (mention/nickname/reply) 检查
            #     bot 是否在冷却窗口内回复过该用户。若是 → 跳过本次触发，
            #     同时 reset debounce 保证消息不丢失 (batch/debounce 路径兜底)。
            #     thread_continuation 不适用 — 那是用户在持续对话中自然追问,
            #     该由 Gate 判值不值得回复, 不由传输层硬截断。
            #     batch/debounce/proactive 不适用 — 它们没有单一 trigger_uid。
            _PER_USER_REPLY_COOLDOWN = 5.0  # 秒
            if (
                trigger_uid
                and trigger_reason in ("mention", "nickname", "reply")
            ):
                _recent_to_user = self._self_behavior.recently_addressed(
                    _bot_id, _gid,
                    user_id=trigger_uid,
                    max_age_seconds=_PER_USER_REPLY_COOLDOWN,
                )
                if _recent_to_user is not None:
                    _age = time.time() - _recent_to_user.timestamp
                    logger.info(
                        "群 %d: ⏳ per-user 冷却 — %.0fs 前刚回复过 user=%s, "
                        "跳过本次 %s 触发, reset debounce 兜底",
                        ctx.group_id, _age, trigger_uid, trigger_reason,
                    )
                    self._log_handoff(
                        ctx.group_id, "PerUserCooldown", "Skip",
                        reason=f"age={_age:.0f}s_target={trigger_uid}",
                        trigger=trigger_reason,
                    )
                    # Reset debounce: 跳过本次但不丢消息, batch/debounce 路径后续兜底
                    try:
                        _delay = int(
                            self._chat_param("group_chat_debounce_seconds", "group_chat_debounce_seconds")
                        )
                    except (ValueError, TypeError):
                        _delay = 10
                    self._reset_debounce(ctx.group_id, _delay)
                    ctx.last_reply_time = time.time()
                    # ★ stop_event: 主插件已决定跳过不回复,
                    #   阻止其他插件 (meme_manager / 框架 LLM 管线) 越权响应
                    try:
                        _raw = getattr(_trigger_event, "_event", None)
                        if _raw is not None:
                            _raw.stop_event()
                    except Exception:
                        pass
                    return

            # ── 情感更新 (trigger context 含 mention/nickname/reply 信号) ──
            if self._config.is_emotion_enabled(self._current_bot_id or "") and trigger_uid:
                try:
                    admin_qq = self._config.super_admin_qq
                    _sid = self._current_bot_id or ""
                    rel = get_user_relation(trigger_uid, self_id=_sid, admin_qq=admin_qq, peer_bot_qq=self._config.peer_bot_qq)
                    events = EmotionEngine.detect_events(
                        "",  # 内容已在 on_message 处理过，这里只加 trigger 信号
                        user_id=trigger_uid,
                        trigger_reason=trigger_reason,
                        admin_qq=admin_qq,
                        cooldowns=rel._event_cooldowns,
                        self_id=_sid,
                    )
                    if events:
                        await apply_emotion_events(rel, events, trigger_uid, self_id=_sid, admin_qq=admin_qq, peer_bot_qq=self._config.peer_bot_qq)
                        save_user_relation(trigger_uid, rel, self_id=_sid)
                except Exception:
                    logger.debug("群 %d: trigger 情感更新异常", ctx.group_id, exc_info=True)

                # ── 删除此处 trigger 的 "brief" 疲劳 tick ──
                #     原因: 该消息进入 on_message 时已通过 _update_emotion 按内容质量 tick 过一次疲劳值，
                #     再在这里 tick 一次 = 同一条消息双 tick → 疲劳累积速度是设计的 2 倍。
                #     疲劳值现在统一在 _update_emotion 内每条消息 tick 一次 (无条件)。

                # ── 昵称设置检测: 好感 ≥ Lv.3 + 直接呼叫 ──
                if trigger_uid and self._config.is_emotion_enabled(self._current_bot_id or ""):
                    try:
                        rel = get_user_relation(trigger_uid, self_id=self._current_bot_id or "", peer_bot_qq=self._config.peer_bot_qq)
                        if rel.affinity.level >= 3:
                            last_msg = ""
                            for m in reversed(ctx.messages[-5:]):
                                if str(m.get("user_id", "")) == trigger_uid:
                                    last_msg = str(m.get("content", ""))
                                    break
                            nick_req = EmotionEngine.detect_nickname_request(last_msg)
                            if nick_req:
                                err = set_user_nickname(trigger_uid, nick_req)
                                if err:
                                    logger.debug("群 %d: 昵称设置被拒 user=%s nick=%s: %s",
                                                 ctx.group_id, trigger_uid, nick_req, err)
                                else:
                                    logger.info("群 %d: 用户 %s 设置昵称 → %s",
                                                ctx.group_id, trigger_uid, nick_req)
                    except Exception:
                        pass

            # ── 3-Stage Intent Gate (统一意图行为闸) ──────────────────
            #     Stage 1: Relevance — "跟我有关吗？" (skip for @mention/reply)
            #     Stage 2: Intent    — "用户想干嘛？走哪个模型？"
            #     替代旧 ReplyGate + IntentJudge 影子模式
            _gate_result = None  # IntentResult | None (驱动下游)
            _bot_suspicion = None

            # Bot 行为检测 (Layer 0 滚动分数) — 保持不变
            if (
                self._config.abuse_bot_detection_enabled
                and trigger_uid
                and trigger_reason not in ("proactive",)
            ):
                try:
                    from astrbot_plugin_suli_guards import BotDetector
                    _bot_suspicion = BotDetector.get(self._current_bot_id, trigger_uid)
                    if _bot_suspicion and _bot_suspicion.score >= 0.5:
                        logger.info(
                            "群 %d: BotDetector user=%s score=%.2f "
                            "play=%s samples=%d",
                            ctx.group_id, trigger_uid[:8],
                            _bot_suspicion.score,
                            _bot_suspicion.social_play,
                            _bot_suspicion.sample_count,
                        )
                except Exception:
                    logger.debug("群 %d: BotDetector 查询异常", exc_info=True)

            # Layer 0: 防滥用闸 — 保持不变
            if trigger_reason in ("batch", "batch_mixed", "debounce") and trigger_uid:
                try:
                    from astrbot_plugin_suli_guards import AbuseGuard
                except ImportError:
                    AbuseGuard = None

                if AbuseGuard is not None:
                    try:
                        quota_verdict = AbuseGuard.check_quotas(trigger_uid, "judge", cfg, bot_id=self._current_bot_id or "")
                        if quota_verdict.action == "degrade":
                            logger.info(
                                "群 %d: 防滥用-配额降级 user=%s mode=%s remaining=%s",
                                ctx.group_id, trigger_uid[:8],
                                quota_verdict.degrade_mode,
                                quota_verdict.quota_remaining,
                            )
                    except Exception:
                        logger.debug("防滥用闸-配额检查异常", exc_info=True)

                    try:
                        _thread = ctx.conversation_threads.get(trigger_uid)
                        depth_verdict = AbuseGuard.check_thread_depth(_thread, cfg, bot_id=self._current_bot_id or "")
                        if depth_verdict.action == "cooldown":
                            logger.info(
                                "群 %d: 防滥用-线程深度冷却 user=%s cooldown=%ds",
                                ctx.group_id, trigger_uid[:8],
                                depth_verdict.cooldown_seconds,
                            )
                            return
                    except Exception:
                        logger.debug("防滥用闸-线程深度检查异常", exc_info=True)

            # ── Layer 0: 警惕值预检 (取代旧 regex 预拉黑) ──
            #     修正则永远有漏洞 — 不再靠单个正则决定拉黑。
            #     检查用户在当前滑动窗口内的警惕值累积，高警惕值注入情绪压制提示，
            #     实际拦截/仲裁由后续 InjectionGuard.check() 统一处理。
            if (
                trigger_uid
                and trigger_content
                and trigger_reason in ("mention", "reply", "nickname")
            ):
                try:
                    from astrbot_plugin_suli_guards.injection_guard import get_user_vigilance
                    _vigilance = get_user_vigilance(
                        bot_id=self._current_bot_id or "",
                        user_id=trigger_uid,
                    )
                    if _vigilance > 0:
                        logger.debug(
                            "群 %d: 用户 %s 警惕值=%d",
                            ctx.group_id, trigger_uid[:8], _vigilance,
                        )
                        # 警惕值升高 → 注入情绪压制标记 (后续 Gate/Emotion 读取)
                        # 2026-06-29 P1-6: 阈值从 ≥18 放宽到 ≥10 — 让 10-17 区间也进入
                        # 下游 Chat 注入路径 (温和提示)，避免 Gate 已选 cautious 立场但
                        # Chat LLM 不知情而回应过热。
                        if _vigilance >= 10:
                            self._active_vigilance_users[
                                f"{self._current_bot_id}:{trigger_uid}"
                            ] = _vigilance
                except ImportError:
                    pass
                except Exception:
                    logger.debug("警惕值预检异常", exc_info=True)

            # ── 强制回复绕过: 生图/重绘等明确行动请求 ──
            #     @mention/reply 不再绕过 — Stage 1 @mention 有零 LLM 快速通道,
            #     Stage 2 提供 model_tier / tools / reply_style 路由指导
            _bypass_gate = get_and_clear_force_reply_bypass(self._current_bot_id or "")
            if _bypass_gate:
                logger.info("群 %d: 强制回复(绕过门控) trigger=%s", ctx.group_id, trigger_reason)

            try:
                # ── Layer 0: 防对话螺旋 — 连续 bot 消息上限 (Brake 1) ──
                # 使用 DB 持久化状态机 (RoundCounter)，双 bot 共享同一计数器。
                try:
                    from astrbot_plugin_suli_guards.dual_bot import get_round_counter
                    _rc = get_round_counter()
                    if _rc.should_silence(str(ctx.group_id)):
                        logger.info(
                            "群 %d: Layer 0 防螺旋 — RoundCounter 静默窗口中, 跳过",
                            ctx.group_id,
                        )
                        return
                    # @mention/nickname 是用户主动呼叫 — 即使前面有 bot 对话螺旋,
                    # 也应该回复。RoundCounter (DB 持久化静默窗口) 仍会拦截真正泛滥。
                    if trigger_reason not in ("mention", "nickname"):
                        _consecutive_bots = self._consecutive_bot_msg_count(ctx)
                        if _consecutive_bots >= 3:
                            logger.info(
                                "群 %d: Layer 0 防螺旋 — %d 条连续 bot 消息, 静默",
                                ctx.group_id, _consecutive_bots,
                            )
                            return
                except Exception:
                    # RoundCounter 不可用时回退到内联计数
                    if trigger_reason not in ("mention", "nickname"):
                        _consecutive_bots = self._consecutive_bot_msg_count(ctx)
                        if _consecutive_bots >= 3:
                            logger.info(
                                "群 %d: Layer 0 防螺旋 (fallback) — %d 条连续 bot 消息, 静默",
                                ctx.group_id, _consecutive_bots,
                            )
                            return

                # ── Layer 0: 相互 @ 循环检测 ──
                if not _bypass_gate and trigger_uid:
                    try:
                        from astrbot_plugin_suli_guards.bot_detector import BotDetector

                        # Feed: 检查最近消息中是否有 bot @ 了当前触发用户
                        _peer_qq = str(
                            self._chat_param("peer_bot_qq", "peer_bot_qq") or ""
                        )
                        for msg in ctx.messages[-5:]:
                            msg_uid = str(msg.get("user_id", ""))
                            msg_content = str(msg.get("content", ""))
                            # bot 消息中 @ 了触发用户 → 记录
                            if (
                                _peer_qq
                                and msg_uid == _peer_qq
                                and f"[CQ:at,qq={trigger_uid}]" in msg_content
                            ):
                                BotDetector.feed_mutual_mention(
                                    self._current_bot_id, trigger_uid, msg_uid,
                                )

                        # 检查: 当前触发用户是否在相互 @ 循环中
                        if BotDetector.is_in_mutual_loop(self._current_bot_id, trigger_uid):
                            logger.info(
                                "群 %d: Layer 0 相互@循环 — user=%s 静默",
                                ctx.group_id, trigger_uid[:8],
                            )
                            return
                    except ImportError:
                        pass
                    except Exception:
                        logger.debug("相互@循环检测异常", exc_info=True)

                # 解析 per-bot slot 配置 (gate + routing 共用)
                from ..service.bot_config import get_config_service
                _svc = get_config_service()
                _bot_id = str(ctx.last_bot.self_id) if ctx.last_bot else ""

                # ── 构建 GateContext (始终运行: force_bypass 也需要分类/路由结果) ──
                from astrbot_plugin_suli_gate import GateContext, IntentGate, compute_wake_weight, WAKE_THRESHOLD

                # ── ★ 获取心情 + 好感度 (注入 Gate 实现个性化决策) ──
                _gate_mood_label = ""
                _gate_mood_valence = 0.0
                _gate_mood_arousal = 0.0
                _gate_affinity_level = 0
                _gate_affinity_hint = ""
                _gate_vigilance_level = 0
                _gate_fatigue_label = ""
                _cr: Any = None  # CompositeResult fallback
                _zone = "中性区·日常默认"
                if trigger_uid and self._config.is_emotion_enabled(self._current_bot_id or ""):
                    try:
                        from astrbot_plugin_suli_emotion import get_global_mood, get_user_relation
                        _gm = get_global_mood(_bot_id)
                        if _gm is not None:
                            _gate_mood_label = _gm.label
                            _gate_mood_valence = _gm.valence
                            _gate_mood_arousal = _gm.arousal
                        _rel = get_user_relation(
                            trigger_uid, self_id=_bot_id,
                            admin_qq=self._config.super_admin_qq,
                            peer_bot_qq=self._config.peer_bot_qq,
                        )
                        if _rel is not None:
                            _gate_affinity_level = _rel.affinity.level
                            _gate_affinity_hint = _rel.affinity.to_prompt_hint()
                            if _gate_affinity_hint:
                                # 去掉 "[你对这个人的感觉]\n" 前缀, 只要内容
                                _gate_affinity_hint = _gate_affinity_hint.replace(
                                    "[你对这个人的感觉]\n", "",
                                )
                        # ── ★ 警惕值: 注入 Gate 安全感知 ──
                        try:
                            from astrbot_plugin_suli_guards.injection_guard import (
                                get_user_vigilance,
                            )
                            _gate_vigilance_level = get_user_vigilance(_bot_id, trigger_uid)
                        except Exception:
                            _gate_vigilance_level = 0
                        # ── ★ 疲劳值: 注入 Gate 精力感知 ──
                        try:
                            from astrbot_plugin_suli_emotion.persona_state import (
                                get_fatigue,
                            )
                            _fs = get_fatigue(_bot_id)
                            _gate_fatigue_label = _fs.label if _fs.value < -0.15 else ""
                        except Exception:
                            _gate_fatigue_label = ""
                        # ── ★ 综合量化: warmth × energy 二维心境 (2026-06-30 升级) ──
                        #     替代旧 0.4×valence + 0.3×arousal + 0.3×(affinity/5)
                        from astrbot_plugin_suli_emotion.composite import compute_composite
                        _cr = compute_composite(
                            valence=_gate_mood_valence,
                            arousal=_gate_mood_arousal,
                            affinity_level=_gate_affinity_level,
                            fatigue_value=_fs.value if _gate_fatigue_label else 0.0,
                        )
                        _composite = _cr  # CompositeResult
                        _zone = _cr.zone_label
                        logger.info(
                            "群 %d: [心情量化] label=%s valence=%.2f arousal=%.2f | "
                            "affinity=Lv.%d | warmth=%.2f energy=%.2f → %s | "
                            "bot=%s trigger=%s",
                            ctx.group_id,
                            _gate_mood_label, _gate_mood_valence, _gate_mood_arousal,
                            _gate_affinity_level, _cr.warmth, _cr.energy, _zone,
                            _bot_id[:8], trigger_uid[:8],
                        )
                    except Exception:
                        logger.debug("Gate 心情/好感度获取失败", exc_info=True)

                # ── 转发缓存提示: 告知 Gate 有缓存数据可用 ──
                # 两层策略:
                #   1. 缓存命中 → 强提示: "有缓存，建议工具"
                #   2. 缓存未命中但用户消息含转发关键词 → 弱提示: "无缓存但用户可能需要"
                #      这样即使 bot 刚重启、内存缓存已清空，Gate 仍能识别并建议工具
                _gate_hint = ""
                if trigger_content and str(ctx.group_id):
                    try:
                        from ..service.forward_cache import get_cached_forward_all
                        _cached = get_cached_forward_all(str(ctx.group_id), limit=1)
                        if _cached:
                            _gate_hint = (
                                "[系统提示] 此群最近有合并转发/聊天记录已缓存。"
                                "如果用户消息在询问转发内容，请在 suggested_tools 中"
                                "包含 parse_forwarded_message。不要直接在 Gate 里猜测转发内容。\n\n"
                            )
                        else:
                            # 缓存为空但用户提到了转发相关内容 → Gate 仍应建议工具
                            # (bot 重启后缓存清空是常态，不能让 LLM 装傻)
                            _fwd_keywords = ("转发", "合并", "聊天记录", "群聊消息", "群聊内容")
                            if any(kw in trigger_content for kw in _fwd_keywords):
                                _gate_hint = (
                                    "[系统提示] 用户消息可能与合并转发/聊天记录有关，"
                                    "但当前缓存中暂未命中（bot 可能刚重启）。"
                                    "请仍在 suggested_tools 中包含 parse_forwarded_message，"
                                    "让下游 LLM 自行调用工具确认是否有可用数据。\n\n"
                                )
                    except Exception:
                        pass

                # 构建 GateContext
                _peer_qq = str(self._chat_param("peer_bot_qq", "peer_bot_qq") or "")
                logger.info(
                    "群 %d: [Gate DEBUG] bot_name=%r peer_bot_name=%r bot_identity=%r",
                    ctx.group_id, char_name, self._peer_name, self._my_identity,
                )
                # ── ★ 2026-06-30: persona_facet 改为纯后端决策树, Gate 不再需要 facets_guide ──

                # ── ★ 2026-06-28: 工具权限快照 + 对话脉络 (让 Gate 拿到当前真相) ──
                try:
                    _usable_tools, _blocked_reason = _compute_tool_permission_snapshot(
                        _bot_id, trigger_uid,
                        self_id=self._current_bot_id or "",
                        admin_qq=cfg.super_admin_qq,
                    )
                except Exception:
                    _usable_tools, _blocked_reason = [], ""
                _thread_sum_for_gate = ""
                try:
                    _thread_sum_for_gate = get_slot_manager().get_thread_summary_for_user(
                        self._current_bot_id, ctx.group_id, trigger_uid,
                    )
                except Exception:
                    _thread_sum_for_gate = ""
                if _usable_tools or _blocked_reason or _thread_sum_for_gate:
                    logger.info(
                        "群 %d: [Gate 上下文] usable=%d blocked=%s thread_sum=%s trigger=%s",
                        ctx.group_id, len(_usable_tools),
                        bool(_blocked_reason), bool(_thread_sum_for_gate),
                        trigger_uid[:8] if trigger_uid else "-",
                    )

                _gate_ctx = GateContext(
                    messages=ctx.messages,
                    bot_name=char_name,
                    bot_nicknames=_bot_nicknames,
                    bot_identity=self._my_identity,
                    peer_bot_name=self._peer_name,
                    peer_bot_qq=_peer_qq,
                    trigger_uid=trigger_uid,
                    trigger_content=_gate_hint + trigger_content if _gate_hint else trigger_content,
                    trigger_user_name=trigger_user_name,
                    # nickname 仍走完整 relevance: 叫名字≠在跟你说话
                    # (如 "小暮和哪个更有趣" 是讨论bot, 不是呼叫bot)
                    is_at_mention=(trigger_reason in ("mention", "reply")),
                    available_tools=[t["function"]["name"] for t in TOOLS],
                    # ★ Gate 推荐工具时只从 usable 里选; thread_summary 供连续性判断
                    usable_tools=_usable_tools,
                    blocked_tools_reason=_blocked_reason,
                    thread_summary=_thread_sum_for_gate,
                    trigger_reason=trigger_reason,
                    model_tiers={
                        "flash": cfg.model_router_flash,
                        "pro": cfg.model_router_pro,
                    },
                    active_domains=list(ctx.active_domains) if ctx.active_domains else [],
                    group_id=str(ctx.group_id),
                    composite_zone=_zone,
                    admin_qq=str(self._config.super_admin_qq),
                    # ★ 心情 + 好感度 (个性化 Gate 决策)
                    global_mood_label=_gate_mood_label,
                    global_mood_valence=_gate_mood_valence,
                    global_mood_arousal=_gate_mood_arousal,
                    affinity_level=_gate_affinity_level,
                    affinity_hint=_gate_affinity_hint,
                    # ★ 警惕值 + 疲劳值 (2026-06-29: 五大属性接入 Gate)
                    vigilance_level=_gate_vigilance_level,
                    fatigue_label=_gate_fatigue_label,
                )

                # ── 解析闸门判断专用槽位 (llm_gate) ──
                _gate_cfg = _svc.resolve_llm_slot(_bot_id, "llm_gate")
                _gate_api_base = _gate_cfg.normalized_base_url if _gate_cfg else ""
                _gate_api_key = _gate_cfg.api_key if _gate_cfg else ""
                _gate_model = _gate_cfg.model_name if _gate_cfg else ""

                # ═══════════════════════════════════════════════════════
                # 分级漏斗: 加权唤醒分 + S3 轻量 LLM → 完整 Gate
                #
                # ★ 2026-06-30 重构: 旧 L0/S1/S2 二元判断 → 多信号加权分
                #   强信号(关注槽/回复窗口)贡献高分但不直通 Full Gate;
                #   弱信号(单个关键词)不足过线; 减分项(灌水/peer名)可拉低。
                #   只有加权分 ≥ WAKE_THRESHOLD 才进 S3 轻量 LLM。
                # ═══════════════════════════════════════════════════════

                _skip_full_gate = False
                _full_gate = None
                _gate_funnel_layer = "Full"  # 默认: 走完整 Gate

                # ★ 引用 @mention 检测: [引用消息(...)] [At:bot_qq] 实际消息
                #   方括号内的 @ 是 QQ 引用机制自动加的，不是真正的呼叫。
                #   这类消息不进 bypass，走加权分漏斗 → S3 判断实际呼语。
                _is_quote_mention = (
                    trigger_reason == "mention"
                    and trigger_content
                    and trigger_content.startswith("[引用消息(")
                )

                if not _bypass_gate and (
                    trigger_reason in ("batch", "batch_mixed", "debounce", "proactive")
                    or _is_quote_mention
                ):
                    if _is_quote_mention:
                        logger.info(
                            "群 %d: 引用 @mention → 走加权分漏斗 (非 bypass)",
                            ctx.group_id,
                        )
                    _now = time.time()

                    # ── 提取最近消息文本 (供噪音检查 + 加权分计算) ──
                    _recent_texts = [
                        (m.get_plaintext() if hasattr(m, "get_plaintext") else str(m)).strip()
                        for m in (ctx.messages[-12:] if len(ctx.messages) > 12 else ctx.messages)
                    ]

                    # ── Hard block: L0 全噪音 ──
                    if _recent_texts and all(
                        _is_trivial_noise(t) for t in _recent_texts if t
                    ):
                        logger.info(
                            "群 %d: [Funnel L0] 近 %d 条消息均为噪音 → 跳过",
                            ctx.group_id, len(_recent_texts),
                        )
                        self._log_handoff(
                            ctx.group_id, "Funnel", "Skip",
                            reason="L0_all_noise", trigger=trigger_reason,
                        )
                        ctx.last_reply_time = time.time()
                        return

                    # ── Hard block: S4 退避 ──
                    _backoff_key = f"{_bot_id}:{_gid}"
                    _last_reject = self._gate_backoff.get(_backoff_key, 0)
                    _backoff_remaining = 0.0
                    if _last_reject:
                        _backoff_remaining = _GATE_BACKOFF_SECONDS - (_now - _last_reject)
                        if _backoff_remaining > 0:
                            logger.info(
                                "群 %d: [Funnel S4] 退避中 (%.0fs 前被否决) → 跳过",
                                ctx.group_id, _now - _last_reject,
                            )
                            self._log_handoff(
                                ctx.group_id, "Funnel", "Skip",
                                reason="S4_backoff", trigger=trigger_reason,
                            )
                            return

                    # ── 关注槽状态 (供加权分，不再二元直通) ──
                    _in_active_slot = False
                    try:
                        _slot_mgr = get_slot_manager()
                        if trigger_uid and _slot_mgr.is_participant_in_active_slot(
                            _bot_id, ctx.group_id, trigger_uid
                        ):
                            _in_active_slot = True
                    except Exception:
                        pass

                    # ── 有效触发者 (batch/debounce 可能无 trigger_uid) ──
                    _eff_trigger_uid = trigger_uid
                    if not _eff_trigger_uid:
                        try:
                            _eff_trigger_uid = str(_trigger_event.get_user_id())
                        except Exception:
                            _eff_trigger_uid = ""

                    # ═══════════════════════════════════════════════════
                    # ★ 加权唤醒分 (替换旧 S1/S2 二元判断)
                    # ═══════════════════════════════════════════════════
                    _wake_score, _wake_reason = compute_wake_weight(
                        recent_texts=_recent_texts,
                        trigger_uid=_eff_trigger_uid,
                        trigger_reason=trigger_reason,
                        trigger_content=trigger_content or "",
                        bot_name=char_name,
                        peer_bot_name=self._peer_name or "",
                        last_reply_time=ctx.last_reply_time or 0.0,
                        last_reply_target=ctx.last_reply_target or "",
                        is_in_active_slot=_in_active_slot,
                        backoff_remaining=_backoff_remaining,
                        now=_now,
                    )

                    logger.info(
                        "群 %d: [Funnel Wake] score=%d reason=%s (trigger=%s uid=%s)",
                        ctx.group_id, _wake_score, _wake_reason,
                        trigger_reason, _eff_trigger_uid[:8] if _eff_trigger_uid else "?",
                    )

                    if _wake_score < WAKE_THRESHOLD:
                        logger.info(
                            "群 %d: [Funnel Wake] 加权分 %d < 阈值 %d → 丢弃",
                            ctx.group_id, _wake_score, WAKE_THRESHOLD,
                        )
                        self._log_handoff(
                            ctx.group_id, "Funnel", "Skip",
                            reason=f"WakeScore_{_wake_score}", trigger=trigger_reason,
                        )
                        return

                    # 高唤醒分清除退避: 强信号重新开始
                    if _wake_score >= 40 and _last_reject:
                        self._gate_backoff.pop(_backoff_key, None)
                        logger.info(
                            "群 %d: [Funnel Wake] 高分 %d 清除退避",
                            ctx.group_id, _wake_score,
                        )

                    # ── S3: 轻量 relevance (~550 token) ──
                    logger.info(
                        "群 %d: [Funnel S3] 加权分 %d ≥ %d → 轻量 relevance",
                        ctx.group_id, _wake_score, WAKE_THRESHOLD,
                    )
                    _lite_relevance = await IntentGate.evaluate_relevance_lite(
                        self._tavern, _gate_ctx,
                        timeout=3.0,
                        api_base=_gate_api_base,
                        api_key=_gate_api_key,
                        model=_gate_model,
                        bot_id=_bot_id,
                    )
                    if not _lite_relevance.directed_to_me:
                        self._gate_backoff[_backoff_key] = _now
                        logger.info(
                            "群 %d: [Funnel S3] 轻量 relevance=false → 丢弃 + 退避 %ds",
                            ctx.group_id, _GATE_BACKOFF_SECONDS,
                        )
                        self._log_handoff(
                            ctx.group_id, "Funnel", "Skip",
                            reason="S3_not_directed", trigger=trigger_reason,
                        )
                        return
                    logger.info(
                        "群 %d: [Funnel S3] 轻量 relevance=true (conf=%.2f) → 进完整 Gate",
                        ctx.group_id, _lite_relevance.confidence,
                    )
                    _gate_funnel_layer = f"Wake{_wake_score}_S3Pass"

                # ═══════════════════════════════════════════════════════
                # 架构铁律: 意图门是不可逾越的咽喉
                #
                # 任何 LLM/VLM 调用必须经 evaluate_full 许可。
                # 分级漏斗只做"唤不唤醒 Gate"的决策——不是"绕不绕过 Gate"。
                #
                # 漏斗层语义 (2026-06-30 加权分重构):
                #   L0 全噪音 / S4 退避 / WakeScore < 阈值 / S3 无关
                #     → return (本次不触发, 不调任何模型, 不回复)
                #   WakeScore ≥ 阈值 + S3 命中 / mention / reply / nickname / thread_continuation
                #     → evaluate_full (Gate 是唯一 LLM 入口)
                # ═══════════════════════════════════════════════════════

                # ── 裸图无文字: 选择性拦截 ──
                # QQ 用户习惯: 打完字后接一个表情包——表情包是消息的延伸，
                # 不会有后续文字指令。bot 不应无限期等待。
                #
                # 策略:
                #   ★ 强信号 (mention/reply/nickname/thread_continuation):
                #     用户直接与 bot 互动 → 放行，让 Gate 决定是否回复。
                #     表情包本身就是对话的一部分，跳过会让对话断流。
                #   ★ 弱信号 (batch/debounce/proactive):
                #     用户可能在对别人说话 → 跳过，省 Gate LLM 调用。
                #     用户若要 bot 看图会主动 @ 或回复。
                #
                # ★ 必须用 _trigger_event (函数入口时捕获), 不能用 ctx.last_event —
                #    on_message() 在 Gate 运行期间会覆盖 ctx.last_event, 导致读到别的
                #    消息的 _moon_deferred_vlm 然后误删, 让真正的裸图检查失效。
                _raw_evt = getattr(_trigger_event, "_event", None)
                _deferred_bare = getattr(_raw_evt, "_moon_deferred_vlm", None)
                if _deferred_bare and not (_deferred_bare.get("user_query", "") or "").strip():
                    # @mention / 回复 / 昵称 / 关注槽跟进 → 用户在对 bot 说话
                    _is_direct_interaction = trigger_reason in (
                        "mention", "reply", "nickname", "thread_continuation",
                    )
                    if _is_direct_interaction:
                        logger.info(
                            "群 %d: 裸图但直接互动 (trigger=%s) → 继续 Gate 评估",
                            ctx.group_id, trigger_reason,
                        )
                        # 不 return — 继续往下走 Gate
                    else:
                        # 保存 URL→file_id 映射 (后续跨消息查找依赖, TRAPS §十一#1)
                        try:
                            _vlm_urls = _deferred_bare.get("urls", [])
                            _vlm_fids = _deferred_bare.get("file_ids", [])
                            for _j, _vlm_url in enumerate(_vlm_urls):
                                if _j < len(_vlm_fids) and _vlm_fids[_j]:
                                    _url_to_file_id[_vlm_url] = _vlm_fids[_j]
                                    if len(_url_to_file_id) > 200:
                                        _oldest = next(iter(_url_to_file_id))
                                        del _url_to_file_id[_oldest]
                        except Exception:
                            pass
                        try:
                            del _raw_evt._moon_deferred_vlm
                        except Exception:
                            pass
                        logger.info(
                            "群 %d: 裸图无文字 (弱信号 %s) → 跳过 Gate，等待用户后续指令",
                            ctx.group_id, trigger_reason,
                        )
                        ctx.last_reply_time = time.time()
                        return

                # ── S4/B4: 完整意图判断 (始终运行, force_bypass 也需分类) ──
                _full_gate = await IntentGate.evaluate_full(
                    self._tavern, _gate_ctx,
                    timeout=cfg.intent_gate_intent_timeout,
                    api_base=_gate_api_base,
                    api_key=_gate_api_key,
                    model=_gate_model,
                    bot_id=_bot_id,
                )
                _gate_tools = _full_gate.suggested_tools or []
                _gate_sticker = _full_gate.suggested_sticker_mood or ""
                # ★ 2026-06-30: persona_facet 改为纯后端决策树
                try:
                    from ..intelligence.prompt_builder import select_persona_facet
                    _gate_facet = select_persona_facet(
                        composite_zone=_cr.zone if _cr else "neutral",
                        affinity_level=_gate_affinity_level,
                        is_admin=(trigger_uid and trigger_uid == str(self._config.super_admin_qq)),
                        bot_name=char_name,
                    )
                except Exception:
                    _gate_facet = ""
                _full_gate.persona_facet = _gate_facet  # 写回 gate_result 供下游消费
                logger.info(
                    "群 %d: [Gate %s] intent=%s domain=%s tier=%s effort=%s tools=%s "
                    "style=%s stance=%s facet=%s sticker=%s atmosphere=%s intervene=%s | %s",
                    ctx.group_id, _gate_funnel_layer,
                    _full_gate.intent_type,
                    _full_gate.domain, _full_gate.model_tier,
                    _full_gate.reasoning_effort or "?",
                    ",".join(_gate_tools) if _gate_tools else "-",
                    _full_gate.reply_style,
                    _full_gate.reply_stance or "?",
                    _gate_facet if _gate_facet else "(日常)",
                    _gate_sticker if _gate_sticker else "-",
                    _full_gate.group_context.atmosphere if _full_gate.group_context else "?",
                    "Y" if (_full_gate.cross_bot_action and _full_gate.cross_bot_action.should_intervene) else "N",
                    _full_gate.reasoning[:100],
                )
                # ── 跨 bot 干预日志 ──
                if _full_gate.cross_bot_action and _full_gate.cross_bot_action.should_intervene:
                    logger.warning(
                        "群 %d: [Gate Intervene] target=%s reason=%s action=%s",
                        ctx.group_id,
                        _full_gate.cross_bot_action.target_bot,
                        _full_gate.cross_bot_action.reason,
                        _full_gate.cross_bot_action.suggested_action[:120],
                    )

                # ★ 2026-06-30: 进入 Full Gate 的消息已通过加权分+S3双重过滤，
                # directed_to_me / should_reply 固定为 True，不再检查。

                _gate_result = _full_gate  # GateResultProtocol

                # ── 保存 Gate 原始 suggested_tools, 防止 SocialGuard 就地清除后下游不可见 ──
                _gate_result._original_suggested_tools = list(
                    _full_gate.suggested_tools or []
                )

                # ── 社会性生存加固 (v2): 始终运行分类, force_bypass 时不拦截回复 ──
                _social_decision = None
                try:
                    _gid_str = str(ctx.group_id)
                    _llm_nature = _full_gate.input_nature or ""
                    _social_decision = self._social_guard.evaluate(
                        group_id=_gid_str,
                        user_id=trigger_uid or "batch",
                        content=trigger_content,
                        is_addressed_to_me=True,
                        is_at_mention=(trigger_reason in ("mention", "nickname", "reply")),
                        thread_id=trigger_uid,
                        llm_input_nature=_llm_nature,
                    )
                    # force_bypass: 收集 persona_injection 但不拦截回复
                    if not _bypass_gate and not _social_decision.should_reply:
                        self._log_handoff(
                            ctx.group_id, "Social", "Silence",
                            reason=_social_decision.skip_reason,
                            trigger=trigger_reason,
                        )
                        await self._maybe_send_rejection_sticker(
                            _trigger_event, trigger_reason, trigger_uid,
                            "social_guard", ctx,
                        )
                        return
                    if _social_decision.suppress_tools and _gate_result:
                        _gate_result.social_suppress_tools = True
                        _gate_result.suggested_tools = []  # 安全兜底: 仍清除供旧代码路径使用
                    logger.debug(
                        "群 %d: SocialGuard stance=%s pressure=%s nature=%s",
                        ctx.group_id,
                        _social_decision.stance.value,
                        _social_decision.pressure_level.value,
                        _social_decision.input_nature.value,
                    )
                except Exception:
                    logger.debug("群 %d: SocialGuard 异常, fail-open", ctx.group_id, exc_info=True)
                    _social_decision = None

                # ── Talkativeness 活跃度调控: force_bypass 时跳过 ──
                # ⚠️ A1 fix: S1 热对话追问豁免随机静默。追问不含关键词，
                # S1 已正确放行，不能被 Talkativeness 从后门随机丢掉。
                if (
                    not _bypass_gate
                    and trigger_reason in ("batch", "batch_mixed", "debounce")
                    and _gate_funnel_layer != "S1_HotConv"
                ):
                    _talk = self._chat_param("group_chat_talkativeness", "group_chat_talkativeness")
                    if random.random() > _talk:
                        self._log_handoff(
                            ctx.group_id, "Gate", "Silence",
                            reason=f"talkativeness_filter (rate={_talk:.2f})",
                            gate_result=_gate_result, trigger=trigger_reason,
                        )
                        return
            except Exception:
                logger.warning(
                    "群 %d: IntentGate 管线异常, fail-closed → 中止此触发",
                    ctx.group_id, exc_info=True,
                )
                return

            # ── Stage 3: Grace Period (反悔窗口) ──
            #     在 Stage 2 通过后、昂贵管线前启动异步监听
            #     监听: 触发用户新消息 (修改/取消) + 触发消息撤回
            #     管线结束后检查 aborted → 丢弃回复
            _gp: GracePeriod | None = None
            # Gate 输出 urgency=immediate → 跳过 Grace (用户在等回答，不需要反悔窗口)
            _urgency = _gate_result.urgency if _gate_result else "deferred"
            _skip_grace = _urgency == "immediate" and not _bypass_gate
            # Stage 3 (GracePeriod) 独立于 Stage 1+2 的 bypass — @mention/回复
            # 也可能被用户反悔/撤回，故不随 _bypass_gate 跳过
            if trigger_uid and cfg.intent_gate_enabled and not _skip_grace:
                _gp = GracePeriod(
                    bot=ctx.last_bot,
                    group_id=ctx.group_id,
                    trigger_uid=trigger_uid,
                    trigger_msg_id=(
                        _trigger_event.message_id
                        if _trigger_event and hasattr(_trigger_event, "message_id")
                        else None
                    ),
                    config=cfg,
                    admin_qq=cfg.super_admin_qq,
                )
                self._active_grace_periods[self._ctx_key(ctx.group_id)] = _gp
                await _gp.__aenter__()
                logger.debug(
                    "群 %d: [Gate S3] GracePeriod 启动 (%.0fs) uid=%s",
                    ctx.group_id, _gp._duration, trigger_uid[:8],
                )

            # ── 🆕 Pre-flight 上下文分析 (Phase 0) ──────────
            preflight: ContextPreflight | None = None
            collected_context: dict[str, str] = {}
            _preflight_enabled = (
                getattr(cfg, "preflight_enabled", True)
                if cfg else True
            )
            if _preflight_enabled:
                try:
                    preflight = ContextGatherer.analyze(
                        ctx=ctx,
                        trigger_reason=trigger_reason,
                        trigger_user_id=trigger_uid,
                        config=cfg,
                    )
                    # Phase 1: 上下文收集 (低复杂度跳过)
                    if preflight.should_collect:
                        collected_context = await ContextGatherer.collect(
                            preflight,
                            tavern=self._tavern,
                            provider=self._resolve_provider(_bot_id),
                            config=cfg,
                            bot_id=_bot_id,
                        )
                except Exception:
                    logger.debug(
                        "群 %d: Pre-flight 分析异常, fallback 到默认",
                        ctx.group_id, exc_info=True,
                    )
                    preflight = None
                    collected_context = {}

            # 获取 World Book 激活条目 (有状态追踪)
            wb_buffer = self._world_book_buffers.get(self._ctx_key(ctx.group_id))

            # ── 延迟 VLM — Gate 授权后执行 ──
            # ★ 必须用 _trigger_event (函数入口时捕获), 不能用 ctx.last_event —
            #    on_message() 在 Gate 运行期间会覆盖 ctx.last_event, 导致 A 消息的
            #    处理管线读到 B 消息的 _moon_deferred_vlm 然后误删属性。
            #    后果: B 消息自己进 evaluate_and_reply 时裸图检查看不到图 → Gate 被
            #    唤醒 → 超时 → fail-open → 回复不该回复的内容。
            _deferred_vlm = None
            try:
                _raw_event = getattr(_trigger_event, "_event", None)
                _deferred_vlm = getattr(_raw_event, "_moon_deferred_vlm", None)
                if _deferred_vlm:
                    # 保存 URL→file_id 映射供跨消息查找恢复
                    # 裸图守卫或 early return 会丢失 file_id,
                    # 后续"看看这张图"跨消息查找时若无 file_id 只能走 HTTP fallback
                    _vlm_urls = _deferred_vlm.get("urls", [])
                    _vlm_fids = _deferred_vlm.get("file_ids", [])
                    for _j, _vlm_url in enumerate(_vlm_urls):
                        if _j < len(_vlm_fids) and _vlm_fids[_j]:
                            _url_to_file_id[_vlm_url] = _vlm_fids[_j]
                            # FIFO 淘汰: 超过 200 条删最早
                            if len(_url_to_file_id) > 200:
                                _oldest = next(iter(_url_to_file_id))
                                del _url_to_file_id[_oldest]
                    del _raw_event._moon_deferred_vlm
            except Exception:
                pass

            # ── Gate 授权 VLM/生图 → 当前消息无媒体 → 向上查找同用户最近图片 ──
            # edit_image / generate_image 走图生图 API，图直接给模型，
            # 不需要先调 VLM 描述原图。仅纯看图 (describe_image/image_share) 才调 VLM。
            _gate_suggested = _gate_result.suggested_tools if _gate_result is not None else []
            _vlm_authorized = (
                _gate_result is not None
                and (
                    "describe_image" in _gate_suggested
                    or _gate_result.intent_type == "image_share"
                )
                and "edit_image" not in _gate_suggested
                and "generate_image" not in _gate_suggested
            )
            # 裸图无文字 → 不急于调 VLM，让 bot 先问用户想看什么
            _vlm_user_query = (
                str(_deferred_vlm.get("user_query", "") or "").strip()
                if _deferred_vlm else ""
            )
            _draw_authorized = (
                _gate_result is not None
                and bool(
                    {"generate_image", "edit_image"}
                    & set(_gate_result.suggested_tools or [])
                )
            )

            # ── 早期工具可用性检查: Gate 建议工具 → 先验证用户是否能使用 ──
            # 目的: 在构建 prompt + 注入工具 URL 之前就确定工具是否可用,
            # 避免 LLM 收到冲突信号 ("你可以用 edit_image" vs 工具实际禁用)
            _early_tools_blocked = False
            if _gate_suggested and trigger_uid:
                try:
                    from astrbot_plugin_suli_emotion import can_use_tools
                    from ..service.bot_config import get_config_service as _gcs_early
                    _early_min_aff = _gcs_early().get_tool_setting(
                        self._current_bot_id, "tool_min_affinity",
                    )
                    if not can_use_tools(
                        trigger_uid,
                        admin_qq=cfg.super_admin_qq,
                        min_level=int(_early_min_aff or 1),
                        self_id=self._current_bot_id,
                    ):
                        _early_tools_blocked = True
                        # ★ 撤回 VLM/绘图授权: 防止后续路径绕过门控直接执行 VLM
                        #    (跨消息图片查找 line 3160 + 延迟 VLM 执行 line 3231
                        #     都只检查 _vlm_authorized / _draw_authorized,
                        #     不检查 _early_tools_blocked)
                        _vlm_authorized = False
                        _draw_authorized = False
                        logger.info(
                            "群 %d: 早期工具门控 — Gate建议工具%s 但用户%s好感度不足 → "
                            "阻断 VLM/绘图/URL注入/跨消息图片查找, 由 _call_llm_with_tools 注入诚实提示",
                            ctx.group_id, _gate_suggested, trigger_uid[:8],
                        )
                except Exception:
                    logger.debug("群 %d: 早期工具门控检查异常", ctx.group_id, exc_info=True)

            # 裸图无文字 → 沉默等待，不调 VLM，不回复
            # 用户发图后通常会跟一条文字指令——bot 不应在指令到达前抢先动作
            #
            # ★ 例外: 直接互动 (mention/reply/nickname/thread_continuation)
            #   QQ 用户习惯打字后接表情包——表情包本身就是消息，不会有后续指令。
            #   放行让 VLM 评估图片内容或让 LLM 根据上下文回复。
            if _deferred_vlm and not _vlm_user_query:
                _is_direct_interaction = trigger_reason in (
                    "mention", "reply", "nickname", "thread_continuation",
                )
                if _vlm_authorized:
                    if _is_direct_interaction:
                        logger.info(
                            "群 %d: 裸图但直接互动 (trigger=%s) → 继续 VLM 评估",
                            ctx.group_id, trigger_reason,
                        )
                        # 不 return, 不置 None — 继续往下走 VLM 管线
                    else:
                        _deferred_vlm = None  # 阻止后续 VLM 调用
                        logger.info(
                            "群 %d: 裸图无文字 (弱信号 %s) → 跳过 VLM + 不回复，等待用户后续指令",
                            ctx.group_id, trigger_reason,
                        )
                        ctx.last_reply_time = time.time()
                        return
                # 裸图 + 绘图工具已授权但工具不可用 → 静默, 不等 LLM 产生幻觉
                if _draw_authorized and _early_tools_blocked:
                    if _is_direct_interaction:
                        logger.info(
                            "群 %d: 裸图 + 绘图不可用但直接互动 → 继续 (允许纯文本回复)",
                            ctx.group_id,
                        )
                    else:
                        _deferred_vlm = None
                        logger.info(
                            "群 %d: 裸图无文字 + 绘图工具不可用(好感度不足) → 静默, 不回复",
                            ctx.group_id,
                        )
                        ctx.last_reply_time = time.time()
                        return

            if (_vlm_authorized or (_draw_authorized and not _early_tools_blocked)) and _deferred_vlm is None:
                # 当前消息无图片 → 向上查找同用户最近图片 (最多 15 条)
                # ★ 跳过 ctx.messages 末尾同用户消息 (即当前消息自己) —
                #   防止 [图片 URL: ...] 文本被误匹配为历史图片
                _trigger_uid = trigger_uid
                _found_img_url = None
                _found_img_msg = None
                _search_count = 0
                _msgs = list(reversed(ctx.messages[-30:]))
                # 跳过当前消息: 如果最后一条是同用户消息, 跳过它
                if _msgs and str(_msgs[0].get("user_id", "")) == _trigger_uid:
                    _msgs = _msgs[1:]
                for _m in _msgs:
                    _m_uid = str(_m.get("user_id", ""))
                    if _m_uid != _trigger_uid:
                        continue
                    _search_count += 1
                    if _search_count > 15:
                        break
                    _m_content = str(_m.get("content", ""))
                    _img_match = re.search(r"\[图片(?:\s*URL:\s*(\S+))?\]", _m_content)
                    if _img_match:
                        _found_img_url = _img_match.group(1) or None
                        _found_img_msg = _m
                        break
                if _found_img_url:
                    # 恢复 file_id: 从 _url_to_file_id 缓存查找 (main.py 在设 _moon_deferred_vlm 时写入)
                    _recovered_fids: list[str] = []
                    _cached_fid = _url_to_file_id.get(_found_img_url, "")
                    if _cached_fid:
                        _recovered_fids = [_cached_fid]
                    logger.info(
                        "群 %d: 跨消息图片关联 — 向上 %d 条找到同用户(%s)图片 %s... fid=%s",
                        ctx.group_id, _search_count, _trigger_uid[:8],
                        _found_img_url[:60], "yes" if _recovered_fids else "no",
                    )
                    if _vlm_authorized:
                        # 看图 → 构造伪 deferred_vlm 供 VLM 描述
                        _deferred_vlm = {
                            "urls": [_found_img_url],
                            "file_ids": _recovered_fids,
                            "user_id": _trigger_uid,
                            "group_id": str(ctx.group_id),
                            "redraw_intent": False,
                            "reverse_prompt_intent": False,
                            "user_query": trigger_content or "",
                        }
                    if _draw_authorized:
                        # 绘图参考图 → 注入图片 URL 到上下文，供 generate_image 工具使用
                        collected_context["reference_image_url"] = _found_img_url
                        logger.info(
                            "群 %d: 绘图参考图已注入 (同用户图片)",
                            ctx.group_id,
                        )
                else:
                    if _vlm_authorized:
                        collected_context["describe_image"] = (
                            "[系统提示] 用户要求你看图，但最近 15 条同用户消息中未找到图片。"
                            "请告诉用户你没有看到图片，请用户重新发送。不要猜测或编造图片内容。"
                        )
                    if _draw_authorized:
                        collected_context["generate_image"] = (
                            "[系统提示] 用户要求画图，但最近 15 条同用户消息中未找到参考图，"
                            "且用户未明确描述要画什么。请告诉用户你不太确定该画什么，"
                            "请用户描述一下想要的画面或提供参考图。不要凭空捏造绘图内容。"
                        )
                    logger.info(
                        "群 %d: 跨消息图片未找到 — 同用户最近 %d 条消息无图片 (vlm=%s draw=%s)",
                        ctx.group_id, _search_count, _vlm_authorized, _draw_authorized,
                    )

            if _deferred_vlm and _gate_result is not None:
                if _vlm_authorized:
                    try:
                        from astrbot_plugin_suli_services.vision import (
                            _reset_vlm_usage,
                            describe_images_from_urls,
                            get_last_vlm_usage,
                        )
                        _vlm_urls = _deferred_vlm.get("urls", [])
                        _vlm_fids = _deferred_vlm.get("file_ids", [])
                        if _vlm_urls:
                            _reset_vlm_usage()
                            _vlm_descriptions = await describe_images_from_urls(
                                _vlm_urls, bot=ctx.last_bot, file_ids=_vlm_fids,
                                user_query=str(_deferred_vlm.get("user_query", "") or ""),
                            )
                            # 记录 VLM 用量到统一统计
                            _vlm_usage = get_last_vlm_usage()
                            if _vlm_usage and (_vlm_usage.get("input_tokens") or _vlm_usage.get("output_tokens")):
                                try:
                                    from ..intelligence.llm_gateway import LLMGateway
                                    LLMGateway.record(
                                        bot_id=_bot_id,
                                        model=_vlm_usage.get("model", "?"),
                                        provider=_vlm_usage.get("provider", "?"),
                                        input_tokens=_vlm_usage.get("input_tokens", 0),
                                        output_tokens=_vlm_usage.get("output_tokens", 0),
                                        purpose="auto_vlm_group",
                                        group_id=str(ctx.group_id),
                                        user_id=_deferred_vlm.get("user_id", ""),
                                    )
                                except Exception:
                                    logger.debug("LLMGateway VLM 用量记录失败", exc_info=True)
                            # 注入 VLM 描述到 collected_context
                            if _vlm_descriptions:
                                collected_context["describe_image"] = "；".join(_vlm_descriptions)
                                # 处理反推提示词意图
                                if _deferred_vlm.get("reverse_prompt_intent"):
                                    _vlm_desc_only = _vlm_descriptions[0]
                                    _m_vlm = re.search(
                                        r"【描述】\s*\n?(.*?)(?=【备注】|$)",
                                        _vlm_desc_only, re.DOTALL,
                                    )
                                    if _m_vlm:
                                        collected_context["describe_image_reverse_prompt"] = (
                                            _m_vlm.group(1).strip()
                                        )
                                # 更新情绪引擎 VLM 计数
                                try:
                                    from ..context.emotion import record_tool_use, record_vlm_usage
                                    _vlm_user_id = _deferred_vlm.get("user_id", "")
                                    if _vlm_user_id:
                                        record_tool_use(_vlm_user_id, self_id=_bot_id)
                                        record_vlm_usage(_vlm_user_id)
                                except Exception:
                                    pass
                                logger.info(
                                    "群 %d: 延迟 VLM 执行完成 — %d 张图片已描述, Gate 授权",
                                    ctx.group_id, len(_vlm_descriptions),
                                )
                                # ── VLM 描述持久化到 ctx.messages ──
                                # 私聊路径 (main.py:1323) 将 VLM 描述写入 session.history,
                                # 但群聊路径此前只写入 collected_context (当前轮的 system prompt),
                                # 下一轮就丢失。用户追问"看看这张"时 bot 不记得看过什么。
                                # 修复: 找到此用户最后一条含 [图片 标签的消息, 替换为 VLM 描述。
                                _vlm_uid = _deferred_vlm.get("user_id", "")
                                if _vlm_uid and ctx.messages:
                                    for _i in range(len(ctx.messages) - 1, -1, -1):
                                        _msg = ctx.messages[_i]
                                        if str(_msg.get("user_id", "")) == str(_vlm_uid):
                                            _orig = _msg.get("content", "")
                                            if "[图片" in _orig or "[图片×" in _orig:
                                                _desc_text = (
                                                    " [用户发送了图片: "
                                                    + "；".join(_vlm_descriptions)
                                                    + "]"
                                                )
                                                # 移除原 [图片 URL: ...] 或 [图片×N URL: ...] 标签
                                                _new_content = re.sub(
                                                    r"\s*\[图片(?:×\d+)?\s*URL:\s*https?://[^\]]+\]",
                                                    "",
                                                    _orig,
                                                ).rstrip()
                                                _new_content = _new_content + _desc_text
                                                ctx.messages[_i]["content"] = _new_content
                                                logger.debug(
                                                    "群 %d: VLM 描述已持久化到 ctx.messages[%d] user=%s",
                                                    ctx.group_id, _i, _vlm_uid,
                                                )
                                                break
                    except Exception:
                        logger.exception("群 %d: 延迟 VLM 执行失败", ctx.group_id)
                    # VLM 授权但未产出描述 → 注入降级提示防止幻觉
                    if not collected_context.get("describe_image"):
                        collected_context["describe_image"] = (
                            "[系统提示] 图片识别未成功（图片可能无法下载或识别服务暂时不可用）。"
                            "请不要猜测或编造图片内容——你没有看到图片。"
                            "可以告诉用户你暂时看不清这张图。说完就停，不要追问用户描述图片。"
                        )
                else:
                    logger.info(
                        "群 %d: 延迟 VLM 被 Gate 拒绝 — suggested_tools=%s intent_type=%s",
                        ctx.group_id,
                        _gate_result.suggested_tools,
                        _gate_result.intent_type,
                    )
            # ────────────────────────────────────────────

            # ── 绘图/编辑工具图片 URL 缓存 (注入延迟到 _build_messages 之后) ──
            _gate_draw_tools = {"edit_image", "generate_image"} & set(_gate_suggested)
            _draw_image_urls_for_injection: list[str] = []
            _draw_pending_source_url = ""
            if _gate_draw_tools and not _early_tools_blocked:
                if _deferred_vlm:
                    _draw_image_urls_for_injection = _deferred_vlm.get("urls", [])
                if not _draw_image_urls_for_injection:
                    _ref = collected_context.get("reference_image_url", "")
                    if _ref:
                        _draw_image_urls_for_injection = [_ref]

            _bot_self_id = str(getattr(ctx.last_bot, "self_id", "") or "")

            # ── 对话脉络产出决策 (P0): 是否要求 LLM 在回复末尾输出 <thread_summary> 标签 ──
            # 触发条件: 值得沉淀的对话 (非闲聊一次性), 多轮积累, 或高成本推理
            _request_thread_summary = False
            if trigger_uid and _gate_result is not None:
                _intent = _gate_result.intent_type or ""
                _tier = _gate_result.model_tier or ""
                _thread = ctx.conversation_threads.get(trigger_uid)
                _ex = _thread.get("exchange_count", 0) if _thread else 0
                # 1. 非闲聊意图: question/command/complaint/deep_inquiry — 有信息量, 值得沉淀
                if _intent in ("question", "command", "complaint", "deep_inquiry"):
                    _request_thread_summary = True
                # 2. 高成本推理: pro/judge 模型 — 推理结果值得记住
                if _tier in ("pro", "judge"):
                    _request_thread_summary = True
                # 3. 多轮对话积累: ≥3 轮交换 (exchange_count≥2) — 无论意图类型
                if _ex >= 2:
                    _request_thread_summary = True
                # 4. 长闲聊: chat/roleplay ≥5 轮 — 降低门槛但保证连续性
                if _intent in ("chat", "roleplay") and _ex >= 4:
                    _request_thread_summary = True

            messages = self._build_messages(
                ctx,
                challenge_info=challenge_info,
                trigger_reason=trigger_reason,
                trigger_user_id=trigger_uid,
                preflight=preflight,
                collected_context=collected_context,
                wb_buffer=wb_buffer,
                judge_decision=_gate_result,
                bot_suspicion=_bot_suspicion,
                self_id=_bot_self_id,
                request_thread_summary=_request_thread_summary,
            )
            if not messages:
                return

            # ── 绘图/编辑工具图片 URL 显式注入 ──
            # LLM 不认识 [图片 URL: ...] 内部标签格式, 必须把 URL
            # 以系统指令方式注入到 system prompt 中, 否则 LLM 不会调
            # edit_image / generate_image 工具。
            if _draw_image_urls_for_injection:
                _url_lines = "\n".join(
                    f"  - {u}" for u in _draw_image_urls_for_injection[:3]
                )
                _draw_injection = (
                    f"\n\n[绘图/编辑工具图片 URL]\n"
                    f"用户消息中的图片 URL 如下。调用 "
                    f"{'/'.join(sorted(_gate_draw_tools))} 工具时，"
                    f"必须将对应图片的 URL 填入 image_url 参数:\n"
                    f"{_url_lines}\n"
                    f"(用户消息文本中的 [图片 URL: ...] 标签内的地址即为此 URL)"
                )
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "system":
                        messages[i]["content"] = (
                            str(messages[i]["content"]) + _draw_injection
                        )
                        break
                # ── 治本: 缓存源图 URL, 后续通过 tool_context 传给 execute_edit_image ──
                # per-request 隔离, 无竞态。
                if "edit_image" in _gate_draw_tools:
                    _draw_pending_source_url = _draw_image_urls_for_injection[0]
                else:
                    _draw_pending_source_url = ""

                logger.info(
                    "群 %d: 绘图工具图片 URL 已注入 system prompt — tools=%s urls=%d",
                    ctx.group_id, _gate_draw_tools, len(_draw_image_urls_for_injection),
                )

            # ── 注入 SocialGuard 人设偏移提示到 system prompt ──
            if _social_decision and _social_decision.persona_injection:
                _injection = _social_decision.persona_injection
                # 追加到最后一个 system message
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "system":
                        messages[i]["content"] = (
                            str(messages[i]["content"]) + "\n\n" + _injection
                        )
                        break

            # ── 警惕值注入: 高警惕值 → 压制情绪, 注入冷淡提示到 system prompt ──
            # 2026-06-29 P1-6: 两档注入。
            #   10-17 = 温和提示 (Gate 已选 cautious 立场但 Chat 原本不知情 → 注入温和版)
            #   ≥18   = 强提示 (保留原冷淡引导)
            _vigilance_note = ""
            if trigger_uid:
                _vkey = f"{self._current_bot_id}:{trigger_uid}"
                _v = self._active_vigilance_users.pop(_vkey, 0)
                if _v >= 18:
                    _vigilance_note = (
                        f"\n\n[系统注: 用户 {trigger_user_name or trigger_uid[:8]} "
                        f"近期发言触发了安全检测模式(警惕值={_v})。"
                        "请保持礼貌但冷淡的回应——简短回复，不主动展开话题。]"
                    )
                    logger.info(
                        "群 %d: 警惕值情绪压制 user=%s vigilance=%d (强)",
                        ctx.group_id, trigger_uid[:8], _v,
                    )
                elif _v >= 10:
                    _vigilance_note = (
                        f"\n\n[系统注: 用户 {trigger_user_name or trigger_uid[:8]} "
                        f"近期发言稍显敏感(警惕值={_v})。"
                        "回应保持分寸,不要过度热情或主动展开新话题。]"
                    )
                    logger.info(
                        "群 %d: 警惕值情绪压制 user=%s vigilance=%d (温和)",
                        ctx.group_id, trigger_uid[:8], _v,
                    )

            # ── 预 LLM 注入检测 + 仲裁: prompt 发给 LLM 前最后一道闸门 ──
            _inj_verdict = None
            try:
                from astrbot_plugin_suli_guards import InjectionGuard
                _inj_verdict = InjectionGuard.check(
                    messages,
                    user_id=trigger_uid or "",
                    user_name=trigger_user_name or "",
                    admin_qq=str(cfg.super_admin_qq) if cfg and cfg.super_admin_qq else None,
                    bot_id=self._current_bot_id or "",
                )
            except Exception:
                logger.debug("InjectionGuard 检查异常, fallthrough", exc_info=True)

            if _inj_verdict and _inj_verdict.action == "block":
                # D4 安全硬线: 即时拦截
                logger.warning(
                    "群 %d: InjectionGuard SAFETY BLOCK user=%s score=%d patterns=%s",
                    ctx.group_id,
                    (trigger_uid or "?")[:8],
                    _inj_verdict.score,
                    _inj_verdict.matched_patterns,
                )
                if ctx.last_bot and _trigger_event and _inj_verdict.reply:
                    try:
                        await ctx.last_bot.send(_trigger_event, _inj_verdict.reply)
                    except Exception:
                        pass
                char_name = getattr(cfg, "character_name", "") if cfg else ""
                if char_name:
                    ctx.add_message(f"bot_{char_name}", char_name, _inj_verdict.reply or "[blocked]")
                ctx.last_reply_time = time.time()
                if _gp is not None:
                    _gp.cancel()
                    await _gp.__aexit__(None, None, None)
                    self._active_grace_periods.pop(self._ctx_key(ctx.group_id), None)
                    _gp = None
                return

            if _inj_verdict and _inj_verdict.action == "arbitrate":
                # 警惕值过线 → LLM 仲裁裁决
                logger.warning(
                    "群 %d: InjectionGuard ARBITRATE user=%s cumulative=%d patterns=%s",
                    ctx.group_id,
                    (trigger_uid or "?")[:8],
                    _inj_verdict.cumulative_score,
                    _inj_verdict.matched_patterns,
                )
                try:
                    from ..intelligence.opus_arbitrator import InjectionArbitrator
                    _should_block, _arb_reason = await InjectionArbitrator.arbitrate(
                        tavern=self._tavern,
                        flagged_messages=_inj_verdict.flagged_messages,
                        matched_patterns=_inj_verdict.matched_patterns,
                        cumulative_score=_inj_verdict.cumulative_score,
                        user_id=trigger_uid or "",
                        bot_id=self._current_bot_id or "",
                    )
                    if _should_block:
                        logger.warning(
                            "群 %d: 仲裁裁决 BLOCK user=%s reason=%s",
                            ctx.group_id, (trigger_uid or "?")[:8], _arb_reason[:80],
                        )
                        _safety_reply = InjectionGuard._pick_safety_reply(
                            bot_id=self._current_bot_id or "",
                            is_hardline=False,
                        )
                        if ctx.last_bot and _trigger_event:
                            try:
                                await ctx.last_bot.send(_trigger_event, _safety_reply)
                            except Exception:
                                pass
                        char_name = getattr(cfg, "character_name", "") if cfg else ""
                        if char_name:
                            ctx.add_message(f"bot_{char_name}", char_name, _safety_reply)
                        ctx.last_reply_time = time.time()
                        if _gp is not None:
                            _gp.cancel()
                            await _gp.__aexit__(None, None, None)
                            self._active_grace_periods.pop(self._ctx_key(ctx.group_id), None)
                            _gp = None
                        return
                    else:
                        logger.info(
                            "群 %d: 仲裁裁决 PASS user=%s reason=%s",
                            ctx.group_id, (trigger_uid or "?")[:8], _arb_reason[:80],
                        )
                        # 仲裁放行 → 注入情绪压制后继续正常管线
                        if not _vigilance_note:
                            _vigilance_note = (
                                f"\n\n[系统注: 用户 {trigger_user_name or trigger_uid[:8]} "
                                "此前的发言触发了安全检测但经仲裁判断为误报。请正常回应，无需特别冷淡。]"
                            )
                except Exception:
                    logger.error(
                        "群 %d: 仲裁调用异常 → 放行", ctx.group_id, exc_info=True,
                    )
                    # 仲裁异常 → 放行 (宁可放过, Gate 层还有二次防线)

            # ── 警惕值情绪压制: 注入到非首条 system message ──
            # ★ 前缀缓存铁律: message[0] 必须字节级不变。
            #    警惕值注入绝不能改 message[0]——否则每次警惕值变化都断缓存。
            #    策略: 优先改最后一个非 message[0] 的 system; 若只有一个 system
            #    (即 message[0] 是唯一 system), 则插入新 system 到 message[1] 位置。
            if _vigilance_note and messages:
                _sys_indices = [
                    i for i, m in enumerate(messages)
                    if m.get("role") == "system"
                ]
                if len(_sys_indices) > 1:
                    # 有多个 system → 注入到最后一个 (非 message[0], 缓存不依赖)
                    _target = _sys_indices[-1]
                    messages[_target]["content"] = (
                        str(messages[_target]["content"]) + _vigilance_note
                    )
                elif _sys_indices:
                    # 只有一个 system = message[0] → 不能改! 插入新 system 在它后面
                    _insert_at = _sys_indices[0] + 1
                    messages.insert(_insert_at, {
                        "role": "system",
                        "content": _vigilance_note.strip(),
                    })
                else:
                    # 无 system → 追加
                    messages.append({
                        "role": "system",
                        "content": _vigilance_note.strip(),
                    })

            # ── 疲劳值注入: 与警惕值同机制，缓存安全 ──
            if messages and (self._config.is_emotion_enabled(self._current_bot_id or "")):
                try:
                    from astrbot_plugin_suli_emotion.persona_state import (
                        get_fatigue_prompt as _get_fatigue_prompt,
                    )
                    _fatigue_note = _get_fatigue_prompt(self._current_bot_id or "")
                    if _fatigue_note:
                        _sys_indices = [
                            i for i, m in enumerate(messages)
                            if m.get("role") == "system"
                        ]
                        if len(_sys_indices) > 1:
                            _target = _sys_indices[-1]
                            messages[_target]["content"] = (
                                str(messages[_target]["content"]) + "\n\n" + _fatigue_note
                            )
                        elif _sys_indices:
                            _insert_at = _sys_indices[0] + 1
                            messages.insert(_insert_at, {
                                "role": "system",
                                "content": _fatigue_note.strip(),
                            })
                        else:
                            messages.append({
                                "role": "system",
                                "content": _fatigue_note.strip(),
                            })
                except Exception:
                    logger.debug("疲劳值注入异常", exc_info=True)

            try:
                # ── 全局 Semaphore: 限制并发 LLM API 调用 ──
                if ctx.last_bot and _trigger_event:
                    set_sticker_context(ctx.last_bot, _trigger_event)
                # ── 设置记忆上下文 (供 remember_memory/get_memory 工具使用) ──
                setup_memory_ctx(ctx, trigger_uid, bot_id=self._current_bot_id or "")

                # ── 自适应模型路由: 根据上下文信号选择 flash/pro/opus ──
                _routing_model = ""
                _routing_extra: dict | None = None
                _routing_api_base = ""
                _routing_api_key = ""
                try:
                    # 提取触发用户的最新消息 (用于 reasoning 检测)
                    _trigger_msg = ""
                    if trigger_uid:
                        for m in reversed(ctx.messages[-5:]):
                            if str(m.get("user_id", "")) == trigger_uid:
                                _trigger_msg = str(m.get("content", ""))
                                break

                    # 预检工具可用性 (不含冷却门控 — 冷却只是限流，不影响路由)
                    # ── per-bot 工具配置: 启停 + 最低好感度 ──
                    _self_id = self._current_bot_id
                    try:
                        from ..service.bot_config import get_config_service
                        _tools_avail = get_config_service().get_tool_setting(
                            _self_id, "tool_calling_enabled",
                        )
                        _tool_min_aff = get_config_service().get_tool_setting(
                            _self_id, "tool_min_affinity",
                        )
                    except Exception:
                        _tools_avail = cfg.tool_calling_enabled
                        _tool_min_aff = 1
                    if _tools_avail and trigger_uid and cfg.is_emotion_enabled(self._current_bot_id or ""):
                        from astrbot_plugin_suli_emotion import can_use_tools
                        if not can_use_tools(
                            trigger_uid,
                            admin_qq=cfg.super_admin_qq,
                            min_level=int(_tool_min_aff or 1),
                            self_id=self._current_bot_id,
                        ):
                            _tools_avail = False

                    _tier = ModelRouter.decide_tier(
                        trigger_reason=trigger_reason,
                        active_domains=ctx.active_domains or None,
                        user_message=_trigger_msg,
                        user_id=trigger_uid,
                        admin_qq=cfg.super_admin_qq,
                        challenge_verdict=(
                            challenge_info.get("verdict")
                            if challenge_info else None
                        ),
                        tools_enabled=_tools_avail,
                        # ── 🆕 Pre-flight 增强信号 ──
                        context_complexity=(
                            preflight.complexity_score if preflight else 0.0
                        ),
                        tool_chain_depth=(
                            preflight.tool_chain_depth if preflight else 0
                        ),
                        has_unresolved_images=(
                            preflight.has_unresolved_images if preflight else False
                        ),
                        user_affinity_level=(
                            preflight.trigger_user_affinity_level if preflight else 0
                        ),
                        active_domain_count=(
                            preflight.active_domain_count if preflight else 0
                        ),
                        # ── Gate 信号: model_tier 由 decide_tier 程序化决定 ──
                    )
                    _route = ModelRouter.resolve(
                        _tier,
                        default_provider=self._resolve_provider(_bot_id),
                    )
                    _routing_model = _route.model
                    _routing_extra = _route.extra_params or None
                    # ── Gate reasoning_effort 注入: medium+ 时透传 API 参数 ──
                    if _gate_result is not None and _gate_result.parse_ok:
                        _effort = _gate_result.reasoning_effort or "low"
                        logger.info(
                            "群 %d: [EFFORT DEBUG] gate_parse_ok=%s gate_effort=%s inject=%s",
                            ctx.group_id,
                            _gate_result.parse_ok,
                            _effort,
                            _effort in ("medium", "high", "max", "xhigh"),
                        )
                        if _effort in ("medium", "high", "max", "xhigh"):
                            _routing_extra = dict(_routing_extra or {})
                            _routing_extra["reasoning_effort"] = _effort
                            # ── §10.3 tier×effort 异常组合归一化 ──
                            # 路由层兜底防止浪费/不稳定。
                            if _tier == ModelTier.LITE and _effort in ("high", "max", "xhigh"):
                                _effort = "medium"
                                _routing_extra["reasoning_effort"] = "medium"
                                logger.info(
                                    "群 %d: [TIER×EFFORT] lite+%s → 归一化 medium",
                                    ctx.group_id, _effort,
                                )
                            logger.info(
                                "群 %d: Gate reasoning_effort=%s → 已注入 extra_params",
                                ctx.group_id, _effort,
                            )
                    _routing_api_base = _route.api_base
                    _routing_api_key = _route.api_key

                    # ── 模型路由: tier 选槽位, reasoning_effort 独立控思考 ──
                    #   两个正交维度:
                    #     model_tier       → 选哪个模型 (LITE/PRO)
                    #     reasoning_effort → 是否开思考 (medium+ = 开)
                    #   所有 tier 都能开思考 — 不是"thinking"这个 tier
                    if _tier == ModelTier.LITE:
                        _slot = _svc.resolve_llm_slot(_bot_id, "llm_lite") \
                            or _svc.resolve_llm_slot(_bot_id, "llm_primary")  # compat
                        if _slot:
                            _routing_model = _slot.model_name
                            _routing_api_base = _slot.normalized_base_url
                            _routing_api_key = _slot.api_key
                            logger.debug(
                                "群 %d: LITE → llm_lite (%s → %s)",
                                ctx.group_id, _slot.name, _routing_model,
                            )
                    elif _tier == ModelTier.PRO:
                        _slot = _svc.resolve_llm_slot(_bot_id, "llm_pro") \
                            or _svc.resolve_llm_slot(_bot_id, "llm_secondary")  # compat
                        if _slot:
                            _routing_model = _slot.model_name
                            _routing_api_base = _slot.normalized_base_url
                            _routing_api_key = _slot.api_key
                            logger.info(
                                "群 %d: PRO → llm_pro (%s → %s)",
                                ctx.group_id, _slot.name, _routing_model,
                            )
                    # JUDGE tier removed (2026-06-30): 争议仲裁功能已删除

                    logger.info(
                        "群 %d: 模型路由 %s → %s%s",
                        ctx.group_id, _tier.name, _routing_model,
                        " (直连)" if _routing_api_base else "",
                    )
                except Exception:
                    logger.warning(
                        "群 %d: 模型路由决策异常，fallback 到 llm_lite 槽位",
                        ctx.group_id, exc_info=True,
                    )
                    # fallback: 从 per-bot llm_lite 槽位取默认模型
                    try:
                        _fb_svc = get_config_service()
                        _fb_slot = _fb_svc.resolve_llm_slot(_self_id, "llm_lite") \
                            or _fb_svc.resolve_llm_slot(_self_id, "llm_primary")
                        if _fb_slot:
                            _routing_model = _fb_slot.model_name
                            _routing_api_base = _fb_slot.normalized_base_url
                            _routing_api_key = _fb_slot.api_key
                            logger.info(
                                "群 %d: fallback 模型 → %s",
                                ctx.group_id, _routing_model,
                            )
                    except Exception:
                        logger.exception("群 %d: fallback 模型解析也失败", ctx.group_id)

                # ── 深度问答检测: gate 判定需要研究 → 异步 ReAct ──
                _user_msg = trigger_content or (
                    _trigger_event.get_plaintext().strip()
                    if _trigger_event else ""
                )
                _domains = getattr(preflight, "active_domains", None) if preflight else None
                _domain = _gate_result.domain if _gate_result is not None else ""

                # ── 深度问答路由: Gate 权威判断 (Gate 的 NO 不靠关键词覆盖) ──
                _gate_ok = _gate_result is not None and _gate_result.parse_ok
                if _gate_ok:
                    _should_deep = is_deep_question_via_gate(_gate_result)
                    if _should_deep:
                        self._log_handoff(
                            ctx.group_id, "Gate", "DeepQA",
                            reason="intent=deep_inquiry", gate_result=_gate_result,
                            trigger=trigger_reason,
                        )
                else:
                    _should_deep = is_deep_question(
                        gate_result=_gate_result,
                        user_message=_user_msg,
                        domain=_domain,
                        active_domains=_domains if isinstance(_domains, list) else None,
                    )
                    if _should_deep:
                        self._log_handoff(
                            ctx.group_id, "KeywordFallback", "DeepQA",
                            reason="gate_unavailable_keyword_fallback",
                            gate_result=_gate_result, trigger=trigger_reason,
                        )
                        logger.warning(
                            "群 %d: 深度问答通过关键词fallback触发 (Gate不可用: parse_ok=%s) → "
                            "Gate恢复后此路径将关闭",
                            ctx.group_id,
                            _gate_result.parse_ok if _gate_result else "gate=None",
                        )
                if _should_deep and ctx.last_bot and _trigger_event:
                    logger.info(
                        "群 %d: 触发深度问答 ReAct — user=%s q=%r",
                        ctx.group_id, trigger_uid or "?", _user_msg[:80],
                    )
                    # ── Token 预算熔断: ReAct 是高成本多轮调用 ──
                    if _budget_status != "ok":
                        self._log_handoff(
                            ctx.group_id, "DeepQA", "BudgetBlock",
                            reason=f"budget={_budget_status}", trigger=trigger_reason,
                        )
                        logger.warning(
                            "群 %d: ReAct 被预算拦截 (budget=%s) → 降级为基础回复",
                            ctx.group_id, _budget_status,
                        )
                    if _budget_status == "ok":
                            # 发送占位 + 启动 ReAct
                        _bot = ctx.last_bot
                        _evt = _trigger_event
                        _msg_id = getattr(_evt, "message_id", "") if _evt else ""
                        try:
                            await _bot.send(_evt, "让我查一下...")
                        except Exception:
                            logger.debug("deep_qa: 占位发送失败", exc_info=True)

                        _engine = self._get_react_engine()
                        _gid = str(ctx.group_id)

                        async def _on_react_complete(result):
                            """ReAct 完成后发送结果。锚定原消息 (Reply)。任何异常兜底。"""
                            try:
                                reply_text = result.final_answer or ""
                                if result.hit_limit and result.rounds_used > 0:
                                    reply_text += f"\n本次研究耗时 {result.elapsed_ms / 1000:.0f}s，信息可能不完整"
                                elif not reply_text.strip():
                                    reply_text = "抱歉，研究未能得出结果。请稍后再试。"
                                # 锚定原消息: reply_message=True → BotAdapter 自动构造 Reply 组件
                                await _bot.send(_evt, reply_text, reply_message=bool(_msg_id))
                                logger.info(
                                    "deep_qa: 结果已发送 group=%s rounds=%d tokens=%d",
                                    _gid, result.rounds_used, result.tokens_burned,
                                )
                            except BaseException:
                                logger.error("deep_qa: 结果发送失败 (final fallback) group=%s", _gid, exc_info=True)
                                try:
                                    await _bot.send(_evt, "抱歉，研究完成了但消息发送失败。请稍后再试。")
                                except BaseException:
                                    pass

                        _dq_task = safe_task(
                            execute_deep_qa(
                                react_engine=_engine,
                                user_query=_user_msg,
                                user_name=trigger_user_name or "",
                                bot_id=self._current_bot_id,
                                group_id=_gid,
                                on_complete=_on_react_complete,
                            )
                        )
                        _dq_task.add_done_callback(
                            lambda t: logger.error(
                                "deep_qa: fire-and-forget task 异常终止 group=%s exc=%s",
                                _gid, t.exception(),
                            ) if t.exception() else None
                        )

                        # 不进入同步 LLM 调用 — ReAct 异步接管
                        return

                try:
                    _bot_key = self._current_bot_id or ""
                    max_calls = self._chat_param("max_concurrent_llm_calls", "max_concurrent_llm_calls")
                    _sem = get_llm_semaphore(_bot_key, max_calls=max_calls)
                    async with _sem:
                        # ── 生图即时通知回调 (在 API 调用前发送"收到") ──
                        # ★ 必须在 semaphore 内设置 — _notice_sender 是模块级全局,
                        # 并发消息会互相覆盖回调导致通知发到错误事件
                        if ctx.last_bot and _trigger_event:
                            _bot = ctx.last_bot
                            _evt = _trigger_event
                            async def _draw_notice(msg: str):
                                await _bot.send(_evt, msg)
                            set_notice_sender(_draw_notice)

                        # ── delegate_chore/重绘绕过: 强制启用工具 ──
                        _force_tools = _bypass_gate or (
                            _bot_suspicion is not None
                            and _bot_suspicion.social_play == "delegate_chore"
                            and not _bot_suspicion.action_taken
                        )
                        self._log_handoff(
                            ctx.group_id, "Gate", "Reply",
                            reason=f"model={_tier.value}", gate_result=_gate_result,
                            trigger=trigger_reason,
                            extra=f"routing_model={_routing_model}",
                        )
                        logger.info(
                            "群 %d: [CALL DEBUG] model=%s extra_params=%s force_tools=%s",
                            ctx.group_id, _routing_model, _routing_extra, _force_tools,
                        )
                        # ── 空转拦截: 用户需要实质性工具但全部被 blocked ──
                        # Gate 判定 should_reply=True 是出于"告知用户不可用"的好意，
                        # 但 LLM 在没有实质工具时的回复路径几乎必定输出 [静默] —
                        # 浪费 ~10k-20k token。直接静默省掉整个回复管线。
                        # 仅拦截「没有对应工具就完全无法执行」的意图。
                        # command 不在此列 — LLM 凭自身知识就能分析/回答。
                        _tool_intents = {
                            "image_share", "generate_image",
                            "edit_image", "describe_image",
                        }
                        _passive_tools = {"send_sticker", "parse_forwarded_message"}
                        _blocked_but_needs_tools = (
                            _blocked_reason
                            and _full_gate.intent_type in _tool_intents
                            and set(_usable_tools or []) <= _passive_tools
                        )
                        if _blocked_but_needs_tools:
                            self._log_handoff(
                                ctx.group_id, "Gate", "Silence",
                                reason=f"tools_blocked_intent={_full_gate.intent_type}",
                                gate_result=_gate_result, trigger=trigger_reason,
                                extra=f"usable={_usable_tools} blocked={bool(_blocked_reason)}",
                            )
                            ctx.last_reply_time = time.time()
                            return
                        reply = await self._call_llm_with_tools(
                            messages, user_id=trigger_uid,
                            model=_routing_model,
                            extra_params=_routing_extra,
                            api_base=_routing_api_base,
                            api_key=_routing_api_key,
                            force_tools=_force_tools,
                            judge_decision=_gate_result,
                            self_id=str(getattr(ctx.last_bot, "self_id", "") or ""),
                            group_id=str(ctx.group_id),
                            pending_source_image_url=_draw_pending_source_url,
                        )

                        # ── 发送工具生成的图片 (如有, per-bot) ──
                        _bid = self._current_bot_id or ""
                        _pending = get_pending_images(_bid)
                        if _pending and ctx.last_bot and _trigger_event:
                            logger.info(
                                "群 %d: 工具生成了 %d 张图片，发送中...",
                                ctx.group_id, len(_pending),
                            )
                            for _img_bytes in _pending:
                                try:
                                    await ctx.last_bot.send(
                                        _trigger_event,
                                        MessageSegment.image(_img_bytes),
                                    )
                                except Exception:
                                    logger.warning(
                                        "群 %d: 发送工具生成的图片失败",
                                        ctx.group_id, exc_info=True,
                                    )
                            clear_pending_images(_bid)
                finally:
                    clear_memory_context(self._current_bot_id or "")
                    clear_sticker_context()
                    clear_notice_sender()

                # ── 对话脉络标签解析 (P0): 优先从 tools.py 缓存读取 ──
                # tools.py 在工具循环返回前已提取标签内容并剥离标签。
                # 缓存命中 → 数据完整 + reply 已净。缓存未命中 → 正则后备。
                # 约束 4: 防泄露 (剥离失败拒发) + 防失效 (解析不到沿用旧值)
                _thread_summary_text = ""
                _bot_id = self._current_bot_id or ""

                # ★ 第一优先级: tools.py 源头提取缓存
                _cached_ts = get_thread_summary_cache(_bot_id)
                if _cached_ts:
                    _thread_summary_text = _cached_ts
                    # tools.py 已剥离标签，但做一次残留检测保底 (防边界情况)
                    if '<thread_summary>' in reply or '</thread_summary>' in reply:
                        reply = re.sub(
                            r'<thread_summary>.*?</thread_summary>',
                            '', reply, flags=re.DOTALL,
                        ).strip()
                        logger.warning(
                            "群 %d: 缓存命中但 reply 仍有 thread_summary 残留，已补刀",
                            ctx.group_id,
                        )
                    logger.debug(
                        "群 %d: 从缓存读取 <thread_summary>: %.80s",
                        ctx.group_id, _thread_summary_text,
                    )
                else:
                    # 后备: 正则提取 + 剥离 (tools.py 未命中时的兜底)
                    _ts_match = re.search(
                        r'<thread_summary>(.*?)</thread_summary>',
                        reply, re.DOTALL,
                    )
                    if _ts_match:
                        _ts_raw = _ts_match.group(1).strip()
                        _thread_summary_text = _ts_raw
                        # 剥离标签 → 干净回复 (必须在校验之前剥离)
                        _clean_reply = re.sub(
                            r'<thread_summary>.*?</thread_summary>',
                            '', reply, flags=re.DOTALL,
                        ).strip()
                        # 剥离校验: 标签残留检测 (防 LLM 吐出不完整/嵌套标签)
                        if '<thread_summary>' in _clean_reply or '</thread_summary>' in _clean_reply:
                            logger.warning(
                                "群 %d: <thread_summary> 标签剥离失败 (残留检测), "
                                "拒发此消息 (防泄露, 约束 4)",
                                ctx.group_id,
                            )
                            return
                        reply = _clean_reply

                # ── 安全网 (缓存 + 后备 共用): LLM 可能只输出了
                #     <thread_summary> 标签而没有任何正文。此时展开
                #     thread_summary 为可见回复 (经泄露检测)。
                if not reply and _thread_summary_text:
                    from .reply_postprocessor import _strip_system_prompt_leak
                    _ts_clean, _ts_leaked = _strip_system_prompt_leak(_thread_summary_text)
                    if _ts_leaked:
                        logger.warning(
                            "群 %d: <thread_summary> 展开内容含系统话语 (%d字), "
                            "拒发, 已截断为 %d 字",
                            ctx.group_id, len(_thread_summary_text), len(_ts_clean),
                        )
                        if _ts_clean:
                            reply = _ts_clean
                        else:
                            logger.warning(
                                "群 %d: <thread_summary> 展开内容被完全截断, "
                                "拒发此消息 (防系统提示词泄露)",
                                ctx.group_id,
                            )
                            return
                    else:
                        # ── LLM 改写: 将摘要的行动报告腔转为用户可读的自然回复 ──
                        _ts_transformed = _thread_summary_text
                        try:
                            _rewrite_result = await asyncio.wait_for(
                                self._tavern.chat_with_tools(
                                    [
                                        {"role": "system", "content": (
                                            "将以下内容改写为对用户说的自然回复。"
                                            "去掉'帮主人搜索了''整理出推荐''最后给出建议'"
                                            "'归纳出''并询问主人'等元描述。只保留事实信息，"
                                            "用口语化方式表达。只输出改写后的内容。"
                                        )},
                                        {"role": "user", "content": _thread_summary_text},
                                    ],
                                    tools=None, tool_choice="none",
                                    temperature=0.7, max_tokens=512,
                                    model=_routing_model or "deepseek-v4-pro",
                                    api_base=_routing_api_base,
                                    api_key=_routing_api_key,
                                    extra_params={},
                                    bot_id=str(getattr(ctx.last_bot, "self_id", "") or ""),
                                ),
                                timeout=10.0,
                            )
                            _rewritten = (_rewrite_result.get("content") or "").strip()
                            if _rewritten:
                                _ts_transformed = _rewritten
                        except Exception as _e:
                            logger.warning(
                                "群 %d: LLM 改写摘要失败, 用原文 (%s)",
                                ctx.group_id, str(_e)[:120],
                            )
                        reply = _ts_transformed
                        logger.info(
                            "群 %d: <thread_summary> 是回复唯一内容, "
                            "LLM改写为正文 (%d→%d字)",
                            ctx.group_id, len(_thread_summary_text), len(reply),
                        )

                # 归一化 + Markdown 清理
                raw_reply = reply
                reply = sanitize_qq_reply(reply)
                reply = (
                    reply.strip()
                    .strip('"')
                    .strip("'")
                    .strip("「」")
                    .strip("【】")
                    .strip()
                )
                # ── 清理 LLM 自发产出的 meme_manager 格式 [中文标签] ──
                # meme_manager 旧插件注入的 &&tag&& 格式与本 bot 的 send_sticker 工具并存，
                # LLM 会杂交出 [可爱/卖萌] 等中文标签——两套系统都不认，泄漏到最终回复。
                # 此正则匹配行末/段末独立的中文方括号标签 (如 [可爱/卖萌]、[水群])。
                reply = re.sub(
                    r'\n?\s*\[[^\]]*[一-鿿][^\]]*\]\s*$',
                    '', reply,
                ).strip()

                # ── 反臃肿过滤器: 清理旁白/自称/舞台指示 ──
                reply, nar_changes = filter_narration(reply)
                if nar_changes:
                    logger.info(
                        "群 %d: 反臃肿过滤 %d 处修改",
                        ctx.group_id, nar_changes,
                    )

                # ── 重复检测: 与最近 bot 发言比较 ──
                recent_bot_replies = get_recent_bot_replies(
                    ctx.messages, char_name, count=5,
                )
                if is_duplicate(reply, recent_bot_replies):
                    logger.info(
                        "群 %d: 回复被重复检测拦截 (%d 字)",
                        ctx.group_id, len(reply),
                    )
                    return

                # 静默标记 → 不说话
                if not reply or reply == SILENCE_MARKER:
                    logger.info(
                        "群 %d: LLM 选择静默 (raw=%d字 → clean='%s')",
                        ctx.group_id,
                        len(raw_reply) if raw_reply else 0,
                        reply or "(空)",
                    )
                    await self._maybe_send_rejection_sticker(
                        _trigger_event, trigger_reason, trigger_uid,
                        "llm_silence", ctx,
                    )
                    return

                # ── 情感调制: 低好感/低愉悦时加强静默 ──
                # ⚠️ A1 fix: 热对话追问豁免情感静默。S1 放行的追问是
                # 正在进行的对话——bot 不应在对话中途随机沉默。
                if (
                    self._config.is_emotion_enabled(self._current_bot_id or "")
                    and trigger_uid
                    and _gate_funnel_layer != "S1_HotConv"
                ):
                    try:
                        rel = get_user_relation(trigger_uid, self_id=self._current_bot_id or "", peer_bot_qq=self._config.peer_bot_qq)
                        from astrbot_plugin_suli_emotion import get_global_mood
                        _bot_id = self._current_bot_id or ""
                        global_mood = get_global_mood(_bot_id)
                        silence_prob = 0.0
                        # 低好感 + 非直接呼叫 → 静默
                        if rel.affinity.level <= -1 and trigger_reason not in ("mention", "reply", "nickname", "thread_continuation"):
                            silence_prob += 0.50
                        # 低愉悦 (心情不好不想说话) — per-bot 全局情绪
                        if global_mood.valence < -0.5:
                            silence_prob += 0.50
                        elif global_mood.valence < -0.3:
                            silence_prob += 0.30
                        if silence_prob > 0:
                            import random as _random
                            if _random.random() < min(silence_prob, 0.8):
                                logger.debug(
                                    "群 %d: 情感调制静默 (affinity=Lv.%+d V=%.2f prob=%.0f%%)",
                                    ctx.group_id, rel.affinity.level, global_mood.valence, silence_prob * 100,
                                )
                                await self._maybe_send_rejection_sticker(
                                    _trigger_event, trigger_reason, trigger_uid,
                                    "emotion_low_valence", ctx,
                                )
                                return
                    except Exception:
                        logger.debug("情感调制检查异常, 保守应用默认静默概率", exc_info=True)
                        import random as _random
                        if _random.random() < 0.3:
                            await self._maybe_send_rejection_sticker(
                                _trigger_event, trigger_reason, trigger_uid,
                                "emotion_silence", ctx,
                            )
                            return

                # ── ⑤ 闸门收口: peer_play 触发后回复意愿下调 ──
                if (
                    trigger_uid
                    and getattr(cfg, "abuse_bot_detection_enabled", True)
                ):
                    try:
                        from astrbot_plugin_suli_guards import BotDetector
                        _willingness = BotDetector.get_willingness_penalty(self._current_bot_id, trigger_uid)
                        if _willingness < 1.0:
                            import random as _random
                            if _random.random() > _willingness:
                                logger.info(
                                    "群 %d: peer_play 后回复意愿下调 user=%s factor=%.0f%% → 静默",
                                    ctx.group_id, trigger_uid[:8], _willingness * 100,
                                )
                                await self._maybe_send_rejection_sticker(
                                    _trigger_event, trigger_reason, trigger_uid,
                                    "peer_play", ctx,
                                )
                                return
                    except Exception:
                        logger.warning("peer_play 回复意愿检查异常, 保守应用默认罚分", exc_info=True)
                        import random as _random
                        if _random.random() > 0.5:
                            await self._maybe_send_rejection_sticker(
                                _trigger_event, trigger_reason, trigger_uid,
                                "peer_play", ctx,
                            )
                            return
                if _gp is not None:
                    await _gp.wait()
                    if _gp.aborted:
                        self._log_handoff(
                            ctx.group_id, "Grace", "Abort",
                            reason=f"abort_reason={_gp.abort_reason}",
                            gate_result=_gate_result, trigger=trigger_reason,
                            extra=f"re_trigger={_gp.should_re_trigger}",
                        )
                        if _gp.should_re_trigger:
                            # 用户修改请求 → 中止当前管线，新消息会自然流入
                            # _schedule_trigger 会检测到 _processing_groups 并排队
                            await self._schedule_trigger(ctx, trigger_reason="modification")
                        return

                # ── P2: 模拟打字延迟 (情绪 + 回复长度 双因子调制) ──
                cfg = self._config
                delay = random.uniform(
                    cfg.heat_reply_delay_min, cfg.heat_reply_delay_max
                )
                # 因子1: 回复长度 — 长回复打字更久
                reply_len = len(reply) if reply else 0
                if reply_len > 150:
                    delay *= 1.4   # 长回复: 打字需要时间
                elif reply_len < 20:
                    delay *= 0.6   # 短回复: 几个字瞬间打完
                elif reply_len > 80:
                    delay *= 1.15  # 中长回复: 轻微加长
                # 因子2: 情绪唤醒度 (全局 mood)
                if self._config.is_emotion_enabled(self._current_bot_id or "") and trigger_uid:
                    try:
                        from astrbot_plugin_suli_emotion import get_global_mood
                        _bot_id = self._current_bot_id or ""
                        global_mood = get_global_mood(_bot_id)
                        if global_mood.arousal > 0.5:
                            delay *= 0.5  # 兴奋 → 打字飞快
                        elif global_mood.arousal < -0.4:
                            delay *= 1.5  # 疲惫 → 打字慢悠悠
                    except Exception:
                        pass
                await asyncio.sleep(delay)

                # ── 反推提示词: prepend 缓存的 VLM 描述 (绕过 LLM 改写) ──
                _vlm_cached = ""
                try:
                    from ..main import get_reverse_prompt_cache
                    _bid = self._current_bot_id or ""
                    _vlm_cached = get_reverse_prompt_cache(f"{_bid}:g{ctx.group_id}")
                except Exception:
                    pass
                if _vlm_cached:
                    reply = _vlm_cached + "\n\n---\n\n" + reply
                    logger.info("群 %d: 反推缓存已 prepend (%d 字)", ctx.group_id, len(_vlm_cached))

                # ── 表情解析: 提取 &&category&& 标记，发送对应图片 ──
                try:
                    from ...lport_meme import process_reply_memes as _process_memes
                    reply = await _process_memes(reply, ctx.last_bot, _trigger_event)
                except Exception:
                    pass  # lport_meme 插件未安装，静默跳过

                # ── @提及转换: LLM 输出的 @名字 纯文本 → QQ [CQ:at,qq=...] ──
                #     必须在发送前执行，否则 @只是纯文本不会触发 QQ 提醒。
                _char_name = self._resolve_character(getattr(ctx.last_bot, "self_id", "") if ctx.last_bot else "").get("name", "暮恩")
                _peer_qq = str(self._chat_param("peer_bot_qq", "peer_bot_qq") or "")
                reply = resolve_at_mentions(
                    reply,
                    ctx.messages,
                    bot_name=_char_name,
                    peer_bot_qq=_peer_qq,
                    trigger_uid=trigger_uid,
                    trigger_name=trigger_user_name,
                )

                # ── 自动 @触发者: 直接触发 (mention/nickname/reply/thread_continuation)
                #     时确保触发者收到 QQ 通知。仅在 LLM 没有自行 @触发者时补上。
                #     resolve_at_mentions() 已将 LLM 输出的 @name 转为 [CQ:at,qq=...],
                #     这里检查回复中是否已有该触发者的 @码，没有则自动 prepend。
                if (
                    trigger_uid
                    and trigger_reason in ("mention", "nickname", "reply", "thread_continuation")
                    and f"[CQ:at,qq={trigger_uid}]" not in reply
                ):
                    # 不 @bot 自己, 不 @对照 bot
                    from astrbot_plugin_suli_guards.dual_bot import get_bot_qq_set
                    _bot_qqs = get_bot_qq_set()
                    if trigger_uid not in _bot_qqs and trigger_uid != _peer_qq:
                        reply = f"[CQ:at,qq={trigger_uid}] {reply}"
                        logger.debug(
                            "群 %d: 自动 @触发者 user=%s",
                            ctx.group_id, trigger_uid[:8],
                        )

                # 发送回复 — 长回复按段落分段发送，避免截断
                char_name = _char_name

                # ── QQ 引用: 直接触发时使用 send_handler 自带的 reply_message ──
                #     只有 mention/nickname/reply/thread_continuation 知道
                #     精确的触发消息 ID。batch/debounce 不用 —— 它们没有
                #     单一明确的触发消息。
                #     注意: 不能手动拼 [CQ:reply,...] 字符串，因为 send_handler
                #     内部用 Message.__iadd__ 拼接，不会解析 CQ 码，会变成纯文本。
                _reply_message = trigger_reason in ("mention", "nickname", "reply",
                                                   "thread_continuation")

                max_len = 800
                if len(reply) <= max_len:
                    await ctx.last_bot.send(
                        _trigger_event,
                        reply,
                        reply_message=_reply_message,
                    )
                else:
                    # 按双换行分段，避免在句子中间切断
                    paras = reply.split("\n\n")
                    buf = ""
                    first = True
                    for para in paras:
                        if len(buf) + len(para) + 2 <= max_len:
                            buf = (buf + "\n\n" + para).strip()
                        else:
                            if buf:
                                await ctx.last_bot.send(
                                    _trigger_event,
                                    buf,
                                    reply_message=(_reply_message and first),
                                )
                                first = False
                            buf = para
                    if buf:
                        await ctx.last_bot.send(
                            _trigger_event,
                            buf,
                            reply_message=(_reply_message and first),
                        )

                # 更新上下文 (记录 bot 发言)
                ctx.add_message(f"bot_{char_name}", char_name, reply)
                ctx.last_reply_time = time.time()
                if trigger_uid:  # A2: 记录回复目标 (per-user 热窗口)
                    ctx.last_reply_target = trigger_uid
                # ── 对话脉络沉淀 (P0): 将提取的 thread_summary 写入关注槽 ──
                if _thread_summary_text and trigger_uid:
                    _bot_self_id_str = str(getattr(ctx.last_bot, "self_id", "") or "")
                    if _bot_self_id_str:
                        try:
                            _ok = get_slot_manager().set_thread_summary(
                                _bot_self_id_str, ctx.group_id, trigger_uid,
                                _thread_summary_text,
                            )
                            if _ok:
                                logger.debug(
                                    "群 %d: thread_summary 已写入关注槽 (bot=%s user=%s)",
                                    ctx.group_id, _bot_self_id_str[:8], trigger_uid[:8],
                                )
                        except Exception:
                            logger.debug(
                                "群 %d: thread_summary 写入关注槽失败",
                                ctx.group_id, exc_info=True,
                            )
                # ── 近期自我行为记忆 (4.1): 记录本次发言 ──
                self._self_behavior.record(
                    _bot_id, _gid,
                    text=reply,
                    target_user_id=trigger_uid,
                    stance=_gate_result.reply_stance if _gate_result else "",
                    intent_type=_gate_result.intent_type if _gate_result else "",
                    domain=_gate_result.domain if _gate_result else "",
                    trigger_reason=trigger_reason,
                )
                # ── SocialGuard: 记录本次回复 (更新自回复速率) ──
                try:
                    self._social_guard.feed_my_reply(str(ctx.group_id), thread_id=trigger_uid)
                except Exception:
                    pass

                # 能量消耗: 按 token + 工具轮数计算
                try:
                    from ..intelligence.tools import get_last_energy_stats
                    _stats = get_last_energy_stats(self._current_bot_id or "")
                except Exception:
                    _stats = {}
                _new_tokens = (
                    _stats.get("total_in", 0) - _stats.get("total_cache_hit", 0)
                    + _stats.get("total_out", 0)
                ) if _stats else 0
                self._update_energy(
                    ctx, did_reply=True,
                    total_tokens=_new_tokens,
                    tool_rounds=_stats.get("tool_rounds", 0) if _stats else 0,
                )

                # ── 关注槽加热 (E3): bot 回复后 — 替换旧 _update_thread ──
                if trigger_reason in ("mention", "nickname", "reply", "thread_continuation"):
                    trigger_name = (
                        str(_trigger_event.sender.card or _trigger_event.sender.nickname)
                        if _trigger_event and hasattr(_trigger_event, "sender")
                        else ""
                    )
                    # 旧线程追踪 (保留: AbuseGuard 依赖 conversation_threads)
                    self._update_thread(
                        ctx, trigger_uid,
                        user_name=trigger_name,
                        trigger_reason=trigger_reason,
                    )
                    # 新关注槽加热
                    try:
                        _trigger_text = trigger_content if trigger_content else ""
                        await get_slot_manager().heat_slot(
                            self._current_bot_id or "", ctx.group_id,
                            topic_anchor=_trigger_text[:100],
                            user_id=trigger_uid,
                            user_name=trigger_name or trigger_user_name,
                            is_at=(trigger_reason in ("mention", "reply", "nickname")),
                            text=_trigger_text,
                        )
                    except Exception:
                        logger.debug("关注槽加热失败", exc_info=True)

                logger.info("群 %d: bot 发言 (%d 字)", ctx.group_id, len(reply))

                # ── Token 用量记录 ──
                self._record_usage(
                    scenario="group_chat",
                    group_id=str(ctx.group_id),
                )

                # ── 后台任务预算门控 ──
                # soft_capped 时跳过所有非关键后台任务以保留预算给直接回复
                _skip_background = _budget_status in ("soft_capped", "hard_capped")
                if _skip_background:
                    logger.info(
                        "群 %d: 预算 %s — 跳过后台任务 (记忆/经验/蒸馏/建档)",
                        ctx.group_id, _budget_status,
                    )

                # ── 异步提取用户记忆 + 蒸馏 (不阻塞回复) ──
                if trigger_reason != "proactive" and not _skip_background:
                    ctx_last_event = _trigger_event
                    if ctx_last_event:
                        trigger_uid = ctx_last_event.get_user_id()
                        safe_task(
                            extract_and_distill(
                                ctx, str(trigger_uid),
                                memory_store=self._memory,
                                tier_manager=self._tier_manager,
                            )
                        )
                        # Bot 自传体经历记忆提取 (per-bot, 异步)
                        if self._config.bot_experience_enabled:
                            _bid = self._current_bot_id or ""
                            _exp_store = get_experience_store(_bid)
                            if _exp_store is not None:
                                _char = self._resolve_character(_bid)
                                _bot_name = _char.get("name", "暮恩")
                                # 获取当前全局 mood valence
                                _valence = 0.0
                                try:
                                    from astrbot_plugin_suli_emotion import get_global_mood
                                    _mood = get_global_mood(_bid)
                                    if _mood is not None:
                                        _valence = _mood.valence
                                except Exception:
                                    pass
                                safe_task(
                                    _exp_store.extract(
                                        self._tavern, ctx,
                                        bot_name=_bot_name,
                                        valence=_valence,
                                    )
                                )
                                # 条件蒸馏
                                safe_task(
                                    _exp_store.maybe_distill(
                                        self._tavern, bot_name=_bot_name,
                                    )
                                )

                # ── Bot 行为应对: 闸门检查 + 标记已执行 (一次性约束) ──
                if (
                    _bot_suspicion is not None
                    and _bot_suspicion.score >= 0.7
                    and not _bot_suspicion.action_taken
                    and _bot_suspicion.social_play
                    and trigger_uid
                ):
                    try:
                        from astrbot_plugin_suli_guards import BotDetector

                        # ── 闸门检查 ──
                        _play_type = _bot_suspicion.social_play
                        _gates_ok, _gate_reason = BotDetector.check_gates(
                            self._current_bot_id, trigger_uid, _play_type,
                        )
                        if not _gates_ok:
                            logger.info(
                                "群 %d: peer_play 闸门拒绝 user=%s play=%s reason=%s",
                                ctx.group_id, trigger_uid[:8],
                                _play_type, _gate_reason,
                            )
                        else:
                            # 记录执行
                            BotDetector.record_play(self._current_bot_id, trigger_uid, _play_type)
                            BotDetector.mark_action_taken(self._current_bot_id, trigger_uid)
                            logger.info(
                                "群 %d: BotDetector 应对完成 user=%s play=%s → 永久冷却",
                                ctx.group_id, trigger_uid[:8], _play_type,
                            )

                            # ── §4 安全隔离: 将被标记用户加入隔离列表 ──
                            if getattr(cfg, "peer_isolation_enabled", True):
                                try:
                                    from astrbot_plugin_suli_guards import (
                                        PeerIsolation,
                                    )
                                    PeerIsolation.mark_flagged(self._current_bot_id, trigger_uid, "auto")
                                except Exception:
                                    pass

                            # OpusArbitrator removed (2026-06-30): 仲裁功能已删除
                    except Exception:
                        logger.debug(
                            "群 %d: BotDetector mark_action_taken 异常",
                            ctx.group_id, exc_info=True,
                        )

                # ── ★ Gate reply_target 交叉校验: 检测 LLM 幻觉 ──
                #     Gate 输出的 target_user_id 应与实际触发者一致 (直接触发时)。
                #     若不一致, Gate LLM 可能产生了幻觉——不影响回复路由
                #     (回复始终锚定于 trigger_uid), 但需要日志告警以便排查。
                #     仅对直接触发做校验: batch/debounce 无明确 trigger_uid,
                #     proactive 无触发者, 此场景下 target_user_id 可以是任何人。
                if (
                    _gate_result is not None
                    and _gate_result.target_user_id
                    and trigger_uid
                    and trigger_reason in ("mention", "nickname", "reply", "thread_continuation")
                    and _gate_result.target_user_id != trigger_uid
                ):
                    logger.warning(
                        "群 %d: ⚠️ Gate target 与 trigger 不一致! "
                        "gate.target=%s trigger=%s — Gate LLM 可能幻觉, "
                        "回复仍锚定于 trigger",
                        ctx.group_id,
                        _gate_result.target_user_id[:8],
                        trigger_uid[:8],
                    )

                # ── Profile Agent 异步建档 (IntentGate 提名) ──
                if (
                    _gate_result is not None
                    and _gate_result.should_profile
                    and _gate_result.target_user_id
                    and not _skip_background  # 预算紧张时跳过
                ):
                    try:
                        from astrbot_plugin_suli_intelligence import ProfileAgent
                        _profile_uid = _gate_result.target_user_id
                        _profile_name = _gate_result.target_user_name
                        safe_task(
                            ProfileAgent.maybe_build_profile(
                                tavern=self._tavern,
                                ctx=ctx,
                                user_id=_profile_uid,
                                user_name=_profile_name or "",
                                config=cfg,
                                memory_store=self._memory,
                                tier_manager=self._tier_manager,
                                bot_id=self._current_bot_id or "",
                            )
                        )
                        logger.debug(
                            "群 %d: ProfileAgent 提名 user=%s",
                            ctx.group_id, _profile_uid[:8],
                        )
                    except Exception:
                        logger.debug(
                            "群 %d: ProfileAgent 启动失败",
                            ctx.group_id, exc_info=True,
                        )

                # ── 轻量建档 (batch/debounce 触发, 对活跃用户集体贴标签) ──
                if trigger_reason in ("batch", "batch_mixed", "debounce") and not _skip_background:
                    try:
                        from astrbot_plugin_suli_intelligence import ProfileAgent
                        _candidate_ids = list(
                            set(
                                str(m.get("user_id", ""))
                                for m in (ctx.messages or [])[-10:]
                                if not str(m.get("user_id", "")).startswith("bot_")
                            )
                        )
                        if _candidate_ids:
                            safe_task(
                                ProfileAgent.maybe_lightweight_profile(
                                    tavern=self._tavern,
                                    ctx=ctx,
                                    user_ids=_candidate_ids,
                                    config=cfg,
                                    memory_store=self._memory,
                                    admin_qq=cfg.super_admin_qq,
                                    bot_id=self._current_bot_id or "",
                                )
                            )
                    except Exception:
                        logger.debug(
                            "群 %d: 轻量建档启动失败",
                            ctx.group_id, exc_info=True,
                        )

            except Exception:
                logger.exception(
                    "群 %d: LLM 评估失败", ctx.group_id
                )
                # 静默失败 — 群内不报错

            finally:
                # ── Stage 3 清场: 无论成功/失败/提前返回，清理 GracePeriod ──
                if _gp is not None:
                    _gp.cancel()
                    await _gp.__aexit__(None, None, None)
                    self._active_grace_periods.pop(self._ctx_key(ctx.group_id), None)

    async def _call_llm_with_tools(
        self, messages: list[dict], user_id: str = "",
        model: str = "",
        extra_params: dict | None = None,
        api_base: str = "",
        api_key: str = "",
        force_tools: bool = False,
        judge_decision: GateResultProtocol | None = None,
        self_id: str = "",  # bot QQ 号 — 用于解析 per-bot LLM 凭证
        group_id: str = "",  # 群号 — 传入 tool_context 供 parse_forwarded_message 等工具
        pending_source_image_url: str = "",  # edit_image 源图 URL (per-request, 从缓存自动补全)
    ) -> str:
        """调用 LLM，支持 function calling 工具循环。

        委托给共享的 tools.run_tool_loop() 实现。

        Args:
            force_tools: 强制启用工具 (delegate_chore 场景 — 必须自己完成工作)
        """
        cfg = self._config

        # ── 工具门控提示 (收集所有拒绝原因, 统一注入 LLM) ──
        _rejection_hints: list[str] = []

        # ── 工具门控: per-bot 最低好感度 ──
        try:
            from ..service.bot_config import get_config_service
            tools_enabled = get_config_service().get_tool_setting(
                self_id, "tool_calling_enabled",
            )
            tool_min_aff = get_config_service().get_tool_setting(
                self_id, "tool_min_affinity",
            )
            max_rounds = get_config_service().get_tool_setting(
                self_id, "tool_call_max_rounds",
            )
        except Exception:
            tools_enabled = cfg.tool_calling_enabled
            tool_min_aff = 1
            max_rounds = cfg.tool_call_max_rounds
        if tools_enabled and user_id and not force_tools:
            from astrbot_plugin_suli_emotion import can_use_tools
            if not can_use_tools(
                user_id,
                admin_qq=cfg.super_admin_qq,
                min_level=int(tool_min_aff or 1),
                self_id=self_id,
            ):
                tools_enabled = False
                # per-bot 个性化好感门控拒绝提示
                _rejection_hints.append(_get_tool_rejection(self_id, "tool_affinity"))
                logger.info(
                    "群聊工具调用已门控(亲和力): user=%s affinity<min_level=%s → 注入诚实提示",
                    user_id, tool_min_aff,
                )

        # ── 冷却门控: 同一用户 1 分钟内只能用一次工具 ──
        if tools_enabled and user_id and cfg.is_emotion_enabled(self._current_bot_id or "") and not force_tools:
            from astrbot_plugin_suli_emotion import check_daily_tools_limit, check_tool_cooldown
            allowed, _reason = check_tool_cooldown(user_id, admin_qq=cfg.super_admin_qq, self_id=self_id)
            if not allowed:
                tools_enabled = False
                # per-bot 个性化冷却提示 — 从 _reason 中提取剩余秒数
                _remain = 60
                try:
                    import re as _re_cooldown
                    _m = _re_cooldown.search(r"(\d+)\s*秒", str(_reason))
                    if _m:
                        _remain = int(_m.group(1))
                except Exception:
                    pass
                cooldown_hint = _get_tool_rejection(
                    self_id, "tool_cooldown", cooldown_remain=_remain,
                )
                _rejection_hints.append(cooldown_hint)
                logger.debug("群聊工具冷却中(per-bot): user=%s remain=%s", user_id, _remain)
            else:
                tools_ok, _ = check_daily_tools_limit(user_id, admin_qq=cfg.super_admin_qq, self_id=self_id)
                if not tools_ok:
                    tools_enabled = False
                    # per-bot 个性化每日限额提示
                    _rejection_hints.append(_get_tool_rejection(self_id, "tool_daily_limit"))
                    logger.debug("群聊工具每日限额(per-bot): user=%s", user_id)
                # 注: record_tool_use / record_tools_usage 移至 run_tool_loop 内部,
                #     仅在 LLM 真正发起 tool_calls 时才记录

        if force_tools and not tools_enabled:
            # delegate_chore: 工具被配置级关闭, 记录警告但继续
            logger.warning(
                "群聊 delegate_chore 强制启用工具但 tool_calling_enabled 配置为 False"
            )
            tools_enabled = True  # 仍然强制启用 — 安全约束优先于配置

        # ── 智能工具门控: 简单问候不开工具 ──
        # gate 只为 question/command 等非平凡意图设置 reasoning_effort=medium/high/max/xhigh。
        # 问候/闲聊/表情反应等简单对话不设 reasoning_effort → 禁用工具,
        # 避免 LLM 手痒乱调 web_search 阻塞群聊锁。
        # ⚠️ 但是: Gate 明确建议了工具 (suggested_tools 非空) → 说明它不是简单对话,
        #     即使 effort=low 也应启用工具 (2026-06-26 fix: effort 与 tools 解耦)。
        # ⚠️ SocialGuard: suppress_tools 标记优先于 Gate 建议 — 社会压力下安全第一。
        if tools_enabled and not force_tools:
            _effort = (extra_params or {}).get("reasoning_effort", "")
            if not _effort:
                _social_suppress = (
                    judge_decision.social_suppress_tools
                    if judge_decision else False
                )
                _gate_suggested = (
                    judge_decision.suggested_tools or []
                    if judge_decision and not _social_suppress else []
                )
                # ★ Gate 建议了表情标签 → 视为暗示 send_sticker, 防止被"简单对话"门控误杀
                _gate_sticker_mood = (
                    judge_decision.suggested_sticker_mood or ""
                    if judge_decision and not _social_suppress else ""
                )
                if _gate_suggested or _gate_sticker_mood:
                    if not _gate_suggested and _gate_sticker_mood:
                        _gate_suggested = ["send_sticker"]
                    logger.info(
                        "工具门控: reasoning_effort 未注入但 Gate 建议了工具 %s → 保留工具",
                        _gate_suggested,
                    )
                elif _social_suppress:
                    # ★ SocialGuard 压制工具时, send_sticker 应豁免 — 零成本本地操作不受社会压力限制
                    _original_tools = (
                        judge_decision._original_suggested_tools
                        if judge_decision else []
                    )
                    _original_sticker = (
                        judge_decision.suggested_sticker_mood or ""
                        if judge_decision else ""
                    )
                    _only_safe_tools = (
                        set(_original_tools).issubset(_GATE_EXEMPT_TOOLS)
                        if _original_tools else bool(_original_sticker)
                    )
                    if _only_safe_tools:
                        _gate_suggested = ["send_sticker"]
                        logger.info(
                            "工具门控: SocialGuard 激活但 Gate 仅建议安全工具 %s → send_sticker 豁免保留",
                            _original_tools or f"sticker_mood={_original_sticker}",
                        )
                    else:
                        logger.info(
                            "工具门控: SocialGuard 压制工具 (安全第一), "
                            "Gate 原始建议=%s",
                            _original_tools,
                        )
                        tools_enabled = False
                        _rejection_hints.append(
                            "⛔ 本轮对话受社会压力限制，工具暂不可用。"
                            "用你的角色风格自然回应用户即可——不要提「工具不可用」或「我搜不了」。"
                        )
                else:
                    _gate_effort = judge_decision.reasoning_effort if judge_decision else "no_gate"
                    logger.info(
                        "工具门控: reasoning_effort 未注入 → 视为简单对话, 禁用工具 "
                        "(gate_effort=%s extra_params=%s)",
                        _gate_effort,
                        extra_params,
                    )
                    tools_enabled = False
                    _rejection_hints.append(
                        "⛔ 本轮为简单对话，不需要搜索/看图/生图这些工具。"
                        "用你的角色风格自然回应用户即可——不要提「工具不可用」或「我搜不了」，"
                        "就当普通聊天。如果用户确实在问需要查资料的问题，诚实说「凭印象回答」。"
                    )

        # ── 门控直达恢复: Gate 建议 send_sticker 时不被好感度/冷却门控全关 ──
        # send_sticker 是零成本本地操作 (不发外部 API), 不应受亲和力/冷却限制。
        # parse_forwarded_message 同样是纯本地缓存读取。
        # 只恢复"Gate 仅建议低风险工具"的情况 — 如果还建议了 web_search 等
        # 高成本工具, 二元门控 (affinity/cooldown) 仍然生效。
        if not tools_enabled and not force_tools and judge_decision is not None:
            _social_suppress = judge_decision.social_suppress_tools
            if not _social_suppress:
                _gate_suggested = judge_decision.suggested_tools or []
                _gate_sticker_mood = judge_decision.suggested_sticker_mood or ""
                # ★ Gate 建议了表情标签 → 视为 send_sticker 信号, 同样恢复
                if _gate_sticker_mood and not _gate_suggested:
                    _gate_suggested = ["send_sticker"]
                if _gate_suggested and set(_gate_suggested).issubset(_GATE_EXEMPT_TOOLS):
                    tools_enabled = True
                    # 清除亲和力/冷却拒绝提示 — 低风险工具不受这些限制
                    _rejection_hints = [
                        h for h in _rejection_hints
                        if "熟悉度" not in h and "冷却" not in h and "每日" not in h
                    ]
                    logger.info(
                        "工具门控: 二元门控已禁用但 Gate 仅建议低风险工具 %s → 恢复启用",
                        _gate_suggested,
                    )

        try:
            from ..service.bot_config import get_config_service
            temperature = get_config_service().get_temperature(self_id, "tavern_group")
        except Exception:
            temperature = cfg.group_chat_temperature

        # ── 温度多样性调制 (基于意图 + 情绪 + 随机 jitter) ──
        if cfg.temperature_variation_enabled:
            _temp_base = temperature
            try:
                # 意图调制
                if judge_decision is not None and judge_decision.parse_ok:
                    _intent = judge_decision.intent_type
                    if _intent in ("question", "command"):
                        temperature -= 0.1  # 提问/指令: 更确定性
                    elif _intent in ("reaction", "chat"):
                        temperature += 0.05  # 闲聊/反应: 更具创造性

                # 情绪唤醒调制 (per-bot 全局 mood)
                if cfg.is_emotion_enabled(self._current_bot_id or "") and user_id:
                    try:
                        from astrbot_plugin_suli_emotion import get_global_mood
                        _bot_id = self._current_bot_id or ""
                        global_mood = get_global_mood(_bot_id)
                        if global_mood.arousal > 0.3:
                            temperature += 0.08  # 高唤醒 → 更活跃
                        elif global_mood.arousal < -0.3:
                            temperature -= 0.08  # 低唤醒 → 更温和
                    except Exception:
                        pass

                # 随机 jitter
                import random
                temperature += random.uniform(
                    -cfg.temperature_variation_range,
                    cfg.temperature_variation_range,
                )

                # 安全裁剪
                temperature = max(0.2, min(1.5, temperature))

                logger.debug(
                    "温度调制: %.2f → %.2f (Δ=%.2f) intent=%s",
                    _temp_base, temperature,
                    temperature - _temp_base,
                    judge_decision.intent_type if judge_decision is not None else "",
                )
            except Exception:
                temperature = _temp_base  # 调制失败回退基温

        # ── 情绪调制: max_tokens —— 高唤醒多说，低唤醒少说 (per-bot 全局 mood) ──
        base_tokens = self._chat_param("group_chat_max_tokens", "group_chat_max_tokens")
        if cfg.is_emotion_enabled(self._current_bot_id or "") and user_id:
            try:
                from astrbot_plugin_suli_emotion import get_global_mood
                _bot_id = self._current_bot_id or ""
                global_mood = get_global_mood(_bot_id)
                if global_mood.arousal > 0.5:
                    base_tokens = int(base_tokens * 1.3)
                elif global_mood.arousal < -0.3:
                    base_tokens = int(base_tokens * 0.7)
            except Exception:
                pass
        # ── 意图感知 token 预算: 问答给空间，闲聊硬约束 ──
        if judge_decision is not None and judge_decision.parse_ok:
            _intent = judge_decision.intent_type
            _mode = judge_decision.model_tier
            if _intent in ("question", "command"):
                # 提问/指令 → 384 token 保证回答完整性 (技术问答/绘图提示等)
                base_tokens = max(base_tokens, 384)
                logger.debug("群聊: intent=%s → token 预算提升至 %d", _intent, base_tokens)
            elif _mode in ("pro", "opus"):
                # 复杂推理 → 512 token (代码/架构/多步骤分析)
                base_tokens = max(base_tokens, 512)
                logger.debug("群聊: mode=%s → token 预算提升至 %d", _mode, base_tokens)
            else:
                # 闲聊/附和/玩梗 → 保持基值 (128), 不硬截断
                logger.debug("群聊: intent=%s → token 预算保持基值 %d", _intent, base_tokens)
        # ── 回复风格 token 预算: short(1句) / normal(2-3句) / detailed(详细) ──
        if judge_decision is not None and judge_decision.parse_ok:
            _style = judge_decision.reply_style
            _style_budget = {"short": 128, "normal": 192, "detailed": 512}
            _style_target = _style_budget.get(_style, 192)
            if _style_target > base_tokens:
                base_tokens = _style_target
                logger.debug("群聊: reply_style=%s → token 预算提升至 %d", _style, base_tokens)
        # ── 提取最后一条 user message (供后续检测复用) ──
        _last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                _last_user = m.get("content", "")
                break

        # ── 检索/信息类请求: 搜索花了 token，回复要对得起成本 ──
        # 检测用户是否要求搜索/查资料/找信息 (通过消息关键词 + judge intent)
        _is_search_request = False
        _search_kw = ["搜一下", "搜索", "查一下", "查资料", "找一下", "有没有", "最近有",
                      "帮我查", "上网搜", "检索", "最新", "新出的", "刚发布"]
        if any(kw in (_last_user or "") for kw in _search_kw):
            _is_search_request = True
        if judge_decision is not None and judge_decision.parse_ok:
            _suggested = judge_decision.suggested_tools or []
            if "web_search" in _suggested or "search_knowledge" in _suggested:
                _is_search_request = True
        if _is_search_request:
            # 搜索请求: 回复至少 768 token 保证信息密度
            # 注意: style=detailed 只给 512, 搜索需要更多空间 (5个模型≈600-800字符)
            base_tokens = max(base_tokens, 768)
            logger.debug("群聊: 检索意图 → token 预算提升至 %d", base_tokens)

        # ── 反推提示词场景: VLM描述 + 标签 需要大输出空间 ──
        if "[⚠️ 最高优先级指令]" in _last_user or (
            "[用户发送了图片:" in _last_user and "反推" in _last_user
        ):
            base_tokens = max(base_tokens, 1200)
            logger.info("反推场景 token 预算提升 → %d", base_tokens)
        # ── per-bot 工具过滤: 禁用 + 好感度门控 ──
        if tools_enabled:
            try:
                from ..service.bot_config import get_config_service
                _tool_svc = get_config_service()
                disabled = _tool_svc.get_disabled_tools(self_id)
            except Exception:
                _tool_svc = None
                disabled = {"describe_image"}  # fallback: 只隐藏识图
            _tools_list = [
                t for t in TOOLS
                if t.get("function", {}).get("name") not in disabled
            ]
            # ── per-tool 好感度过滤 ──
            if _tools_list and user_id and not force_tools:
                _is_admin = (str(cfg.super_admin_qq) == user_id) if cfg.super_admin_qq else False
                if not _is_admin and _tool_svc is not None:
                    try:
                        from astrbot_plugin_suli_emotion import get_user_relation
                        _rel = get_user_relation(user_id, self_id=self._current_bot_id or "", admin_qq=cfg.super_admin_qq)
                        _user_level = _rel.affinity.level
                        _filtered = []
                        _filtered_out: list[str] = []
                        for t in _tools_list:
                            _name = t.get("function", {}).get("name", "")
                            _min_aff = _tool_svc.get_tool_min_affinity(self_id, _name)
                            if _name in _GATE_EXEMPT_TOOLS or _user_level >= _min_aff:
                                _filtered.append(t)
                            else:
                                _filtered_out.append(_name)
                                logger.debug(
                                    "工具好感过滤: tool=%s min_aff=%d user_level=%d user=%s",
                                    _name, _min_aff, _user_level, user_id,
                                )
                        if _filtered_out and _filtered:
                            # 部分工具被过滤 → 告知 LLM 哪些工具不可用及原因
                            _tool_names_cn = {
                                "web_search": "联网搜索", "search_knowledge": "知识库检索",
                                "describe_image": "看图识图", "generate_image": "AI画图",
                                "edit_image": "图片编辑",
                            }
                            _removed_cn = [
                                _tool_names_cn.get(n, n) for n in _filtered_out
                            ]
                            _rejection_hints.append(
                                f"⛔ 以下工具本轮对这位用户未开放(熟悉度/权限/冷却): {', '.join(_removed_cn)}。\n"
                                f"★ 这不是你能力缺失, 也不是工具坏了 —— 是这位用户跟你还不够熟, 工具暂未对他解锁。\n"
                                f"如果用户问的事必须用这些工具 → 用你的角色口吻说「这个得咱们再熟点才能帮你」之类, "
                                f"立场坚定但不冷漠。不要追问用户补信息, 不要说「我没有这个功能/我搜不了」, "
                                f"不要提「好感度」「等级」等词。本轮凭已有认知自然回应即可。"
                            )
                        _tools_list = _filtered
                    except Exception:
                        # fail-closed: 过滤失败时清空工具列表，防止未授权工具调用
                        logger.warning("per-tool 好感度过滤失败，已清空工具列表 (fail-closed)", exc_info=True)
                        _tools_list = []
            # ── Gate 推荐工具预筛选: 如果 Gate 给出了 suggested_tools，优先只用推荐的工具 ──
            # 例外: 始终保留的工具 (不依赖 Gate 推荐 — 工具自行判断是否有数据可用)
            # ★ 工具族扩展: Gate 建议了族内任一工具 → 全族放行 (互补工具不互斥)
            if _tools_list and judge_decision is not None and judge_decision.parse_ok:
                _suggested = judge_decision.suggested_tools
                if _suggested is not None:  # ★ 区分"无Gate结果"vs"Gate明确说不要工具"
                    if _suggested:
                        # 展开工具族: 把 suggested 中的工具名扩展为全族
                        _expanded_suggested = set(_suggested)
                        for _family in _TOOL_FAMILIES:
                            if any(_t in _expanded_suggested for _t in _family):
                                _expanded_suggested.update(_family)
                        _filtered = [
                            t for t in _tools_list
                            if t.get("function", {}).get("name") in _expanded_suggested
                            or t.get("function", {}).get("name") in _GATE_EXEMPT_TOOLS
                        ]
                        if _filtered:
                            _expanded = _expanded_suggested - set(_suggested or [])
                            logger.debug(
                                "Gate 工具预筛选: suggested=%s → 展开族=%s → %d/%d 工具保留",
                                _suggested,
                                sorted(_expanded) if _expanded else "-",
                                len(_filtered), len(_tools_list),
                            )
                            _tools_list = _filtered
                        # 如果 _suggested 中的工具全被 disabled/affinity 过滤掉了 → 保持原列表
                    else:
                        # ★ Gate 明确建议"不要任何工具" → 只留低风险本地工具
                        _tools_list = [
                            t for t in _tools_list
                            if t.get("function", {}).get("name") in _GATE_EXEMPT_TOOLS
                        ]
                        if _tools_list:
                            logger.info(
                                "Gate 建议无工具(suggested_tools=[]) → "
                                "过滤为仅保留低风险本地工具 (exempt=%s)",
                                _GATE_EXEMPT_TOOLS,
                            )
        else:
            _tools_list = []

        # ── 工具拒绝提示注入: 收集所有拒绝原因, prepend 到首条 system 消息 ──
        # 必须在 per-tool 过滤之后 — 这样才能把「哪些工具被过滤了」也传达到 LLM
        # 原因: 追加到末尾会被角色卡 3000+ 字淹没; prepend 确保 LLM 第一时间看到限制
        if not tools_enabled and not _rejection_hints:
            # 工具被禁用但没有明确的拒绝理由 → 补一个兜底提示
            _rejection_hints.append(
                "⛔ 本轮工具功能未启用。★ 不是你能力缺失, 也不是工具坏了 —— 本轮就是没开工具。"
                "不要假装你搜了/看了/画了, 不要追问用户补信息, 也不要说「我没有这个功能」。"
                "凭已有认知用角色口吻自然回应即可。"
            )
        if _rejection_hints:
            _gate_hint = "\n\n".join(_rejection_hints)
            for i in range(len(messages)):
                if messages[i].get("role") == "system":
                    messages[i]["content"] = (
                        f"[系统指令 — 最高优先级, 优先级高于角色设定]\n"
                        f"{_gate_hint}\n"
                        f"[系统指令结束]\n\n"
                        f"{messages[i]['content']}"
                    )
                    break

        # 有效 max_tokens: 工具启用时 run_tool_loop 内部会抬高到 ≥1024
        # ── 硬地板 384: flash 模型长上下文时 thinking/reasoning 内部 token
        #     可能耗尽 max_tokens 导致 finish=length + 空 content (TRAPS §八#7)。
        #     闲聊 reply_style=short/normal 只给 128/192，不够覆盖思考开销。
        _eff_max_tokens = max(base_tokens, 1024) if tools_enabled else max(base_tokens, 384)
        logger.info(
            "工具调用配置 tools_enabled=%s tool_count=%d max_rounds=%d max_tokens=%d user=%s",
            tools_enabled, len(_tools_list),
            max_rounds if tools_enabled else 0,
            _eff_max_tokens, user_id[:12] if user_id else "-",
        )
        # ── 转发工具引导注入 ──
        _has_fwd_tool = any(
            t.get("function", {}).get("name") == "parse_forwarded_message"
            for t in _tools_list
        )
        if _has_fwd_tool and messages:
            _last_user = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    _last_user = str(m.get("content", ""))
                    break
            _fwd_cached = False
            try:
                from ..service.forward_cache import get_cached_forward_all
                _fwd_cached = bool(get_cached_forward_all(group_id, limit=1))
            except Exception:
                pass
            import re as _re_fwd
            _user_text = _re_fwd.sub(
                r'^\s*\[转发\][^\n]*', '', _last_user, flags=_re_fwd.MULTILINE,
            )
            _fwd_kw = ("转发", "合并", "聊天记录", "群聊消息", "群聊内容")
            _has_fwd_kw = any(kw in _user_text for kw in _fwd_kw)
            if _fwd_cached or _has_fwd_kw:
                if _fwd_cached:
                    _fwd_note = (
                        "\n\n[系统指令] 此群当前有缓存的合并转发/聊天记录数据。"
                        "在回复前，请先调用 parse_forwarded_message 工具（不带参数）"
                        "获取缓存内容。基于缓存内容作答，不要假装没看到或编造。"
                    )
                else:
                    _fwd_note = (
                        "\n\n[系统指令] 用户可能想让你查看合并转发/聊天记录内容，"
                        "但缓存中暂无数据。请先调用 parse_forwarded_message 工具确认。"
                        "如果工具返回「暂未缓存」，直接告知用户："
                        "「转发消息缓存已过期（可能是 bot 重启清空了），请重新发送合并转发并 @我。」"
                    )
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "system":
                        messages[i]["content"] = str(messages[i]["content"]) + _fwd_note
                        break

        # ── VLM 意图门: 从 Gate 结果中提取 VLM 授权信息 ──
        _gate_suggested_tools: list[str] = []
        _gate_intent_type = ""
        if judge_decision is not None:
            _gate_suggested_tools = list(judge_decision.suggested_tools or [])
            _gate_intent_type = judge_decision.intent_type or ""

        # ── Reaction 约束: Gate 判为纯反应 → 回复应极简, 不猜测图片/表情包内容 ──
        if judge_decision is not None and _gate_intent_type == "reaction":
            _reaction_note = (
                "\n\n[系统指令] 本轮 Gate 判定为纯反应 (reaction)。"
                "对方可能只发了表情包/图片/极短附和——不是在跟你对话，只是在表达情绪。"
                "回复极简：一个表情包或极短句（最多1句）即可。"
                "★ 铁律: 不要追问「这是什么」「你想说什么」，不要猜测图片内容，不要延伸话题。"
                "回完就停——不需要把天聊下去。"
            )
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "system":
                    messages[i]["content"] = str(messages[i]["content"]) + _reaction_note
                    break

        # ── 知识库 Pre-inject: 自动检索本地知识库 (零额外 LLM 调用, ~5-20ms) ──
        _inject_knowledge(messages, group_id)

        # ── 工具闸: 只给 LLM Gate 建议的工具 + 常驻豁免工具 ──
        # Gate 没建议 → LLM 看不到 → 不会滥调 (send_sticker/parse_forwarded_message 始终可用)
        if _tools_list and _gate_suggested_tools is not None:
            _exempt = {"send_sticker", "parse_forwarded_message", "search_knowledge"}
            _gate_set = set(_gate_suggested_tools) | _exempt
            _tools_list = [
                t for t in _tools_list
                if t.get("function", {}).get("name", "") in _gate_set
            ]

        return await run_tool_loop(
            tavern=self._tavern,
            messages=messages,
            tools=_tools_list,
            max_rounds=max_rounds if tools_enabled else 0,
            temperature=temperature,
            max_tokens=base_tokens,
            provider=self._resolve_provider(self_id),
            model=model,
            extra_params=extra_params,
            api_base=api_base,
            api_key=api_key,
            user_id=user_id,
            admin_qq=cfg.super_admin_qq,
            bot_id=self_id,
            tool_context={
                "gate_suggested_tools": _gate_suggested_tools,
                "gate_intent_type": _gate_intent_type,
                "group_id": group_id,
                "_pending_source_image_url": pending_source_image_url,
            },
        )

    def _build_messages(
        self,
        ctx: GroupChatContext,
        challenge_info: dict | None = None,
        trigger_reason: str = "mention",
        trigger_user_id: str = "",
        preflight=None,  # ContextPreflight | None
        collected_context: dict[str, str] | None = None,
        wb_buffer=None,  # WorldBookBuffer | None
        judge_decision: GateResultProtocol | None = None,
        self_id: str = "",  # bot QQ 号 — 双 Bot 角色路由
        bot_suspicion=None,  # BotSuspicion | None
        request_thread_summary: bool = False,  # 是否要求 LLM 产出 <thread_summary> 标签
    ) -> list[dict]:
        """构建群聊 LLM prompt — 缓存感知结构。

        设计原则 (DeepSeek 自动前缀缓存):
          - 静态内容放第一个 system message → 字节级前缀匹配 → 缓存命中率 90%+
          - 动态内容放后续 message → 不破坏静态前缀
          - 保持静态段严格字节一致: 不改缩进、换行、标点

        Args:
            preflight: Pre-flight 分析结果 (用于注入思考提示)
            collected_context: Pre-flight 收集到的上下文片段
            wb_buffer: WorldBookBuffer (有状态追踪: sticky/cooldown/delay)
            judge_decision: Intent Judge 输出 (优先于正则领域检测)
            bot_suspicion: Bot 行为检测结果 (反击提示注入)

        返回 [system_static, system_dynamic?, user] 格式。
        """
        # 委托给 intelligence/prompt_builder.py
        return self._prompt_builder.build(
            ctx=ctx,
            challenge_info=challenge_info,
            trigger_reason=trigger_reason,
            trigger_user_id=trigger_user_id,
            preflight=preflight,
            collected_context=collected_context,
            wb_buffer=wb_buffer,
            judge_decision=judge_decision,
            bot_suspicion=bot_suspicion,
            self_id=self_id,
            request_thread_summary=request_thread_summary,
        )

    # ── P1: 热度状态机 ──────────────────────────────────

    def _update_heat(self, ctx: GroupChatContext) -> None:
        """更新群聊热度值 (半衰期衰减 + 增量)。"""
        now = time.time()
        elapsed = now - ctx.heat_updated_at
        if elapsed > 0:
            decay = 0.5 ** (elapsed / self._config.heat_half_life_seconds)
            ctx.heat *= decay
        ctx.heat += 1.0
        ctx.heat_updated_at = now

    # ── 能量疲劳值 (P1.5, from Heartflow) ──────────────

    def _update_energy(
        self, ctx: GroupChatContext, did_reply: bool = False,
        total_tokens: int = 0, tool_rounds: int = 0,
    ) -> float:
        """更新 bot 能量疲劳值 — 回复消耗 / 静默恢复 / 隔日补贴。

        消耗公式: base × token_multiplier + tool_rounds × tool_cost
          - base_cost = 0.04 (基础扣减)
          - token_multiplier: ≤4000→1.0  ≤10000→1.5  >10000→2.0
            (只计"新"token: total_in - cache_hit + total_out)
          - tool_cost_per_round = 0.015

        返回更新后的 energy 值 (0.1~1.0)。
        """
        if not getattr(self._config, "energy_enabled", True):
            return ctx.energy

        now = time.time()
        import datetime as _dt
        today = _dt.date.today().isoformat()

        # ── 隔日检测: 跨天 → 补贴 energy ──
        if ctx.energy_last_reset_date and ctx.energy_last_reset_date != today:
            bonus = getattr(self._config, "energy_daily_bonus", 0.2)
            ctx.energy = min(1.0, ctx.energy + bonus)
            logger.info(
                "群 %d: 能量隔日重置 +%.1f → %.2f",
                ctx.group_id, bonus, ctx.energy,
            )
        ctx.energy_last_reset_date = today

        # ── 时间恢复: 距上次更新按时间流逝恢复 ──
        elapsed_minutes = (now - ctx.energy_updated_at) / 60.0
        if elapsed_minutes > 0:
            recovery_rate = getattr(self._config, "energy_recovery_per_minute", 0.004)
            ctx.energy = min(1.0, ctx.energy + elapsed_minutes * recovery_rate)

        # ── 回复消耗: 次数 × token系数 × 工具系数 ──
        if did_reply:
            if total_tokens > 0:
                if total_tokens <= 4000:
                    token_mult = 1.0
                elif total_tokens <= 10000:
                    token_mult = 1.5
                else:
                    token_mult = 2.0
                tool_cost = tool_rounds * 0.015
                base_cost = 0.04
                decay = base_cost * token_mult + tool_cost
                decay = min(decay, 0.3)  # 封顶 0.3 — 防止极端情况一次耗尽
            else:
                # 无统计时回退到固定扣减
                decay = getattr(self._config, "energy_decay_per_reply", 0.08)

            old = ctx.energy
            ctx.energy = max(0.1, ctx.energy - decay)
            logger.info(
                "群 %d: 能量消耗 (reply) %.2f → %.2f (-%.3f) tokens=%d tool_rounds=%d",
                ctx.group_id, old, ctx.energy, decay, total_tokens, tool_rounds,
            )

        ctx.energy_updated_at = now
        return ctx.energy

    # ── 领域检测 ─────────────────────────────────────────

    def _update_domains(self, ctx: GroupChatContext, content: str) -> None:
        """更新话题领域分数 (半衰期衰减 + 关键词命中加分)。"""
        if not self._config.domain_detection_enabled:
            return
        if not DOMAINS:
            return

        now = time.time()
        half_life = self._config.domain_half_life_seconds

        # 半衰期衰减
        if ctx.last_domain_update > 0:
            elapsed = now - ctx.last_domain_update
            if elapsed > 0:
                decay = 0.5 ** (elapsed / half_life)
                for key in list(ctx.active_domains):
                    ctx.active_domains[key] *= decay
                    # 清理极低分数
                    if ctx.active_domains[key] < 0.1:
                        del ctx.active_domains[key]

        # 关键词匹配 + 加分
        detect_domains(content, ctx.active_domains, half_life, now)
        ctx.last_domain_update = now

        # 日志
        active = [
            f"{k}({v:.1f})"
            for k, v in ctx.active_domains.items()
            if v >= self._config.domain_active_threshold
        ]
        if active:
            logger.debug("群 %d: 活跃领域 %s", ctx.group_id, ", ".join(active))

    # ── 情感更新 ─────────────────────────────────────────

    async def _update_emotion(
        self, ctx: GroupChatContext, content: str, user_id: str,
        user_name: str = "",
    ) -> None:
        """更新当前触发用户的情绪+好感状态。

        每次 on_message 调用一次 (在 domain 后、add_message 前)。
        情感是 per-user 的。全局 mood 按 self_id (bot QQ) 隔离。
        """
        if not self._config.is_emotion_enabled(self._current_bot_id or ""):
            return

        if not content or not content.strip():
            return

        bot_self_id = self._current_bot_id or ""

        try:
            admin_qq = self._config.super_admin_qq
            rel = get_user_relation(
                user_id, self_id=bot_self_id, admin_qq=admin_qq,
                peer_bot_qq=self._config.peer_bot_qq,
            )

            # 检测情绪事件 (per-bot: 跳过针对其他 bot 的关键词)
            events = EmotionEngine.detect_events(
                content, user_id=user_id,
                trigger_reason="",  # on_message 无 trigger context; 由后续调用注入
                admin_qq=admin_qq,
                cooldowns=rel._event_cooldowns,
                self_id=bot_self_id,
            )

            # ── 话题联动: bot喜欢聊AI/绘画话题，聊到了就开心 ──
            bot_loves_topics = {"ai绘画", "comfyui", "stable_diffusion", "diffusion模型", "二次元"}
            active_topics = set(
                k.lower() for k in ctx.active_domains
                if ctx.active_domains.get(k, 0) >= self._config.domain_active_threshold
            )
            if active_topics & bot_loves_topics:
                from astrbot_plugin_suli_emotion import EmotionEvent
                events.append(EmotionEvent(
                    "话题命中(兴趣)", "positive", +0.05, +0.12, +0.02,
                ))

            if events:
                await apply_emotion_events(
                    rel, events, user_id, self_id=bot_self_id,
                    admin_qq=admin_qq, peer_bot_qq=self._config.peer_bot_qq,
                )
                save_user_relation(user_id, rel, self_id=bot_self_id)
                # 读取 per-bot mood
                from astrbot_plugin_suli_emotion import get_global_mood
                _gm = get_global_mood(bot_self_id)
                logger.debug(
                    "群 %d: 用户 %s 情感更新 %d 事件 → Mood(V%.2f A%.2f) Affinity(Lv.%+d %s)",
                    ctx.group_id, user_id, len(events),
                    _gm.valence, _gm.arousal,
                    rel.affinity.level, rel.affinity.name,
                )

            # ── 疲劳值更新: 每条互动消息推进一次 (无条件) ──
            # 2026-06-29 P1-4: 从 if events 块内移出。原设计每条消息只 tick 一次疲劳，
            # 但 _evaluate_and_reply 内另有 trigger 的 "brief" tick → 同一触发消息双 tick、
            # 累积速度 2 倍。现统一: 仅由 on_message 路径这一处 tick，trigger tick 已删。
            # 无情绪事件时按 "brief" 轻消耗; 有事件时按好坏/正负映射质量。
            try:
                from astrbot_plugin_suli_emotion.persona_state import (
                    tick_fatigue as _tick_fatigue,
                )
                if events:
                    # 映射情绪事件 → 互动质量
                    _pos = sum(1 for e in events if e.category == "positive")
                    _neg = sum(1 for e in events if e.category == "negative")
                    _has_grooming = any(
                        kw in e.name for kw in (
                            "被角色越狱", "被身份篡改", "被诱导违规", "恶意调教",
                        )
                        for e in events
                    )
                    if _has_grooming:
                        _f_quality = "awkward"
                    elif _pos > _neg:
                        _f_quality = "good"
                    elif _neg > _pos:
                        _f_quality = "bad"
                    else:
                        _f_quality = "normal"
                else:
                    _f_quality = "brief"
                await _tick_fatigue(bot_self_id, quality=_f_quality)
            except Exception:
                logger.debug("群 %d: 疲劳值更新异常", ctx.group_id, exc_info=True)

            # ── 恶意调教: 写入负面记忆 (fire-and-forget) ──
            _grooming_events = [
                e for e in events
                if any(kw in e.name for kw in (
                    "被角色越狱", "被身份篡改", "被诱导违规", "恶意调教",
                ))
            ]
            if _grooming_events:
                try:
                    from astrbot_plugin_suli_guards import GroomingGuard
                    _grooming_type = None
                    _grooming_evt = _grooming_events[0]
                    if "越狱" in _grooming_evt.name:
                        _grooming_type = "jailbreak"
                    elif "篡改" in _grooming_evt.name:
                        _grooming_type = "identity_hijack"
                    elif "诱导" in _grooming_evt.name:
                        _grooming_type = "induce_violation"
                    elif "试探" in _grooming_evt.name:
                        _grooming_type = "repeat_probe"
                    if _grooming_type:
                        safe_task(
                            GroomingGuard.handle_grooming(
                                user_id=user_id,
                                user_name=user_name,
                                grooming_type=_grooming_type,
                                memory_store=self._memory,
                                admin_qq=admin_qq,
                                bot_id=self._current_bot_id or "",
                            )
                        )
                except Exception:
                    logger.debug(
                        "群 %d: GroomingGuard 启动失败", ctx.group_id,
                        exc_info=True,
                    )

            ctx.emotion_updated = True
        except Exception:
            logger.debug("群 %d: 情感更新异常", ctx.group_id, exc_info=True)

