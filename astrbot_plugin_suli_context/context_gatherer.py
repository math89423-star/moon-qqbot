"""Pre-flight 上下文分析层 — 在 LLM 调用前评估复杂度 + 推荐工具 + 预收集上下文。

设计原则:
  - 分析阶段纯规则 (零 LLM 调用)，<1ms 完成
  - 收集阶段并发执行工具调用，总超时 5 秒
  - 低复杂度日常闲聊跳过收集，零额外开销
  - 所有冷却独立管理，防止重复调用浪费 token

数据流:
  trigger → ContextGatherer.analyze() → ContextGatherer.collect() → ModelRouter → LLM

用法:
  from .context_gatherer import ContextGatherer, ContextPreflight

  preflight = ContextGatherer.analyze(ctx, trigger_reason, trigger_user_id, config)
  if preflight.should_collect:
      collected = await ContextGatherer.collect(preflight, tavern, provider)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────

# 复杂度阈值: 低于此值跳过上下文收集
DEFAULT_COLLECT_THRESHOLD = 2.0

# Pre-flight 收集总超时 (秒)
PREFLIGHT_TIMEOUT = 5.0

# ── 问句检测正则 ─────────────────────────────────────────

# 中文问句特征: 吗呢吧啊？? 或者疑问词开头
_QUESTION_RE = re.compile(
    r"[吗呢吧啊]？|[吗呢吧啊]\?|"
    r"？$|\?$|"
    r"^(什么|怎么|如何|为什么|为啥|哪[个些]|谁|多少|几点|"
    r"能不能|可以不|行不行|有没有|是不是|要不要|该不该|"
    r"帮我|帮我查|帮我找|帮我看|推荐|建议|介绍)"
)

# 图片相关 URL / 标记
_IMAGE_SIGNAL_RE = re.compile(
    r"\[图片\]|\[用户发送了图片:|image|\.jpg|\.png|\.webp|\.gif|"
    r"gchat\.qpic\.cn|multimedia\.nt\.qq\.com\.cn|c2cpicdw\.qpic\.cn",
    re.IGNORECASE,
)

# 隐含信息需求 — 用户可能没直接问但需要上下文
_IMPLICIT_NEED_PATTERNS: list[tuple[str, str]] = [
    # (正则, 需要的信息类型)
    (r"跑不动|跑不了|带不动|卡死了|太慢了", "设备信息"),
    (r"报错|出错了|崩溃|闪退|打不开|连不上", "错误排查"),
    (r"帮我画|生成.*图|画一张|出张图", "生图能力"),
    (r"这个(?:模型|节点|lora|插件)怎|好不好用|效果", "模型评价"),
    (r"有没有.*推荐|推荐.*有没有|哪个好|哪个更", "对比推荐"),
    (r"参数.*多少|设多少|怎么调|怎么设置", "参数配置"),
    (r"最新|最近.*消息|新的|出了|发布了", "最新动态"),
]

# ── 冷却管理器 ───────────────────────────────────────────

# 模块级冷却记录: {key: last_call_timestamp}
_preflight_cooldowns: dict[str, float] = {}


def _check_cooldown(key: str, cooldown_seconds: float) -> bool:
    """检查 key 是否在冷却期内。返回 True = 冷却中, False = 允许调用。"""
    now = time.time()
    last = _preflight_cooldowns.get(key, 0)
    if now - last < cooldown_seconds:
        return True
    _preflight_cooldowns[key] = now
    return False


def _clear_expired_cooldowns(max_age: float = 300.0) -> None:
    """清理过期的冷却记录 (释放内存)。"""
    now = time.time()
    stale = [k for k, v in _preflight_cooldowns.items() if now - v > max_age]
    for k in stale:
        del _preflight_cooldowns[k]


# ── Preflight 结果 ───────────────────────────────────────


@dataclass
class ContextPreflight:
    """Pre-flight 分析结果。

    Attributes:
        complexity_score: 上下文复杂度 (0.0–10.0)
        recommended_tools: 推荐调用的工具名列表 (如 ["search_knowledge"])
        tool_details: {tool_name: {"query": ..., "reason": ...}}
        has_unresolved_images: 是否有未解析的图片
        message_intent: 消息意图类型 — "question"|"exclaim"|"agree"|"chat"|"unknown"
        implicit_needs: 隐含的信息需求列表 (如 ["设备信息"])
        should_collect: 是否应该执行 Pre-flight 收集
        trigger_user_affinity_level: 触发用户好感等级 (0 表示未启用/未知)
        active_domain_count: 活跃领域数
    """

    complexity_score: float = 0.0
    recommended_tools: list[str] = field(default_factory=list)
    tool_details: dict[str, dict] = field(default_factory=dict)
    has_unresolved_images: bool = False
    message_intent: str = "unknown"
    implicit_needs: list[str] = field(default_factory=list)
    should_collect: bool = False
    trigger_user_affinity_level: int = 0
    active_domain_count: int = 0

    @property
    def tool_chain_depth(self) -> int:
        """推荐的工具数量 (供模型路由使用)。"""
        return len(self.recommended_tools)

    @property
    def is_technical(self) -> bool:
        """是否是技术话题 (复杂度 >= 4.0 或需要知识库/网页搜索)。"""
        return (
            self.complexity_score >= 4.0
            or "search_knowledge" in self.recommended_tools
            or "web_search" in self.recommended_tools
        )


# ── ContextGatherer ──────────────────────────────────────


class ContextGatherer:
    """Pre-flight 上下文分析与收集器。

    纯静态方法，无内部状态 (冷却使用模块级 dict)。
    """

    # ── 分析 ───────────────────────────────────────────

    @staticmethod
    def analyze(
        ctx,  # GroupChatContext (duck-typed)
        trigger_reason: str = "",
        trigger_user_id: str = "",
        config=None,  # Config
    ) -> ContextPreflight:
        """分析当前上下文，评估复杂度并推荐工具。

        纯规则驱动，不调用 LLM。O(n) 扫描最近消息。

        Args:
            ctx: GroupChatContext
            trigger_reason: "mention"|"nickname"|"reply"|"batch"|"debounce"|"proactive"
            trigger_user_id: 触发用户的 QQ 号
            config: Config 对象

        Returns:
            ContextPreflight 分析结果
        """
        preflight = ContextPreflight()
        score = 0.0

        # ── 1. 技术领域激活 ──
        active_domains = getattr(ctx, "active_domains", {}) or {}
        domain_threshold = getattr(config, "domain_active_threshold", 2.0) if config else 2.0
        tech_domain_count = 0
        for key, val in active_domains.items():
            if val >= domain_threshold:
                tech_domain_count += 1
                score += min(val * 0.8, 2.0)  # 单领域最多 +2.0
        score = min(score, 4.0)  # 技术领域总上限 +4.0
        preflight.active_domain_count = tech_domain_count

        # ── 2. 消息内容分析 ──
        recent_messages = getattr(ctx, "messages", []) or []
        # 取触发用户的最新一条消息
        trigger_msg = ""
        if trigger_user_id and recent_messages:
            for m in reversed(recent_messages):
                if str(m.get("user_id", "")) == trigger_user_id:
                    trigger_msg = str(m.get("content", ""))
                    break

        # 如果没有触发用户消息，取最新一条非 bot 消息
        if not trigger_msg and recent_messages:
            for m in reversed(recent_messages):
                uid = str(m.get("user_id", ""))
                if not uid.startswith("bot_"):
                    trigger_msg = str(m.get("content", ""))
                    break

        # 2a. 图片检测
        image_count = 0
        for m in recent_messages[-5:]:
            content = str(m.get("content", ""))
            if _IMAGE_SIGNAL_RE.search(content):
                image_count += 1

        # 检查是否有图片 URL 但未解析 (有 URL 而非 [用户发送了图片: 标记)
        unresolved_images = 0
        for m in recent_messages[-3:]:
            content = str(m.get("content", ""))
            has_url = bool(re.search(
                r"https?://.*\.(?:jpg|jpeg|png|webp|gif)",
                content, re.IGNORECASE,
            ))
            has_vlm_result = "[用户发送了图片:" in content
            if has_url and not has_vlm_result:
                unresolved_images += 1

        if image_count > 0:
            score += min(image_count * 1.5, 3.0)
        preflight.has_unresolved_images = unresolved_images > 0
        if unresolved_images > 0:
            score += 1.5

        # 2b. 问句检测
        has_question = bool(trigger_msg and _QUESTION_RE.search(trigger_msg))
        if has_question:
            score += 1.0

        # 2c. 仲裁/裁决关键词检测
        _ARBITRATION_KW = [
            "裁决", "仲裁", "裁定", "评评理", "谁说的对", "帮我判断",
            "帮断断",
        ]
        if trigger_msg and any(kw in trigger_msg for kw in _ARBITRATION_KW):
            score += 2.0

        # 2d. 隐含信息需求
        if trigger_msg:
            for pat, need_type in _IMPLICIT_NEED_PATTERNS:
                if re.search(pat, trigger_msg):
                    if need_type not in preflight.implicit_needs:
                        preflight.implicit_needs.append(need_type)
                    score += 0.8

        # ── 3. 对话深度 ──
        bot_msg_count = sum(
            1 for m in recent_messages[-10:]
            if str(m.get("user_id", "")).startswith("bot_")
        )
        if bot_msg_count >= 3:
            score += 1.5
        elif bot_msg_count >= 1:
            score += 0.5

        # ── 4. 多参与者 ──
        unique_speakers: set[str] = set()
        for m in recent_messages[-10:]:
            uid = str(m.get("user_id", ""))
            if uid and not uid.startswith("bot_"):
                unique_speakers.add(uid)
        if len(unique_speakers) >= 4:
            score += 1.0
        elif len(unique_speakers) >= 3:
            score += 0.5

        # ── 5. 触发原因 ──
        if trigger_reason in ("mention", "reply"):
            score += 1.0
        elif trigger_reason == "nickname":
            score += 0.8
        elif trigger_reason == "proactive":
            score += 0.5  # 主动发言也需要一点思考

        # ── 6. 用户好感 ──
        affinity_level = 0
        if trigger_user_id and config and getattr(config, "emotion_enabled", False):
            try:
                from astrbot_plugin_suli_emotion import get_user_relation
                admin_qq = getattr(config, "super_admin_qq", 0)
                rel = get_user_relation(trigger_user_id, admin_qq=admin_qq, peer_bot_qq=config.peer_bot_qq)
                affinity_level = rel.affinity.level
                if affinity_level >= 3:
                    score += 1.5
                elif affinity_level >= 2:
                    score += 0.8
                elif affinity_level <= -1:
                    score -= 0.5  # 低好感降低投入
            except Exception:
                pass
        preflight.trigger_user_affinity_level = affinity_level

        # ── 7. 消息意图识别 ──
        preflight.message_intent = ContextGatherer._classify_intent(trigger_msg)

        # ── 8. 工具推荐 ──
        preflight.recommended_tools = []
        preflight.tool_details = {}

        # 8a. 技术领域活跃 + 含问句 → search_knowledge
        if tech_domain_count > 0 and (has_question or preflight.implicit_needs):
            query = ContextGatherer._extract_search_query(
                trigger_msg, active_domains,
            )
            preflight.recommended_tools.append("search_knowledge")
            preflight.tool_details["search_knowledge"] = {
                "query": query,
                "reason": f"技术话题活跃 ({', '.join(active_domains)}), 含疑问",
            }

        # 8b. 未解析图片 → describe_image
        if unresolved_images > 0:
            preflight.recommended_tools.append("describe_image")
            preflight.tool_details["describe_image"] = {
                "reason": f"检测到 {unresolved_images} 张未解析图片",
            }

        # 8c. 提到特定用户 → get_memory
        if trigger_msg and preflight.message_intent in ("question", "unknown"):
            if any(kw in trigger_msg for kw in ["记得", "之前说过", "上次", "你记不记得"]):
                preflight.recommended_tools.append("get_memory")
                preflight.tool_details["get_memory"] = {
                    "reason": "用户询问记忆相关信息",
                }

        # 8d. 最新动态类话题 → web_search (知识库可能过时)
        if trigger_msg and has_question:
            _news_kw = ["最新", "最近", "新出的", "刚发布", "今天", "最近有", "现在的"]
            if any(kw in trigger_msg for kw in _news_kw):
                if "web_search" not in preflight.recommended_tools:
                    preflight.recommended_tools.append("web_search")
                    preflight.tool_details["web_search"] = {
                        "query": ContextGatherer._extract_search_query(
                            trigger_msg, active_domains,
                        ),
                        "reason": "话题涉及时效性内容",
                    }

        # 8e. 询问 bot 能力/状态
        if trigger_msg:
            _status_kw = ["你能做什么", "你能干嘛", "你有什么能力", "你有哪些功能",
                          "你在线吗", "你还在吗", "你活着吗", "你正常吗",
                          "状态", "系统状态"]
            if any(kw in trigger_msg for kw in _status_kw):
                preflight.recommended_tools.append("check_lport_status")

        # 8f. 询问可用模型
        if trigger_msg:
            _model_kw = ["有哪些模型", "什么模型", "模型列表", "支持哪些",
                         "checkpoint", "lora.*有哪些", "模型.*推荐"]
            if any(re.search(kw, trigger_msg) for kw in _model_kw):
                preflight.recommended_tools.append("list_available_models")

        # ── 9. 决定是否收集 ──
        threshold = (
            getattr(config, "preflight_collect_threshold", DEFAULT_COLLECT_THRESHOLD)
            if config else DEFAULT_COLLECT_THRESHOLD
        )
        has_tools = len(preflight.recommended_tools) > 0
        preflight.should_collect = (
            has_tools
            and preflight.complexity_score >= threshold
        )

        # 上限截断
        preflight.complexity_score = min(round(score, 1), 10.0)

        if preflight.should_collect or preflight.complexity_score >= 3.0:
            logger.info(
                "Pre-flight 分析: 复杂度=%.1f, 意图=%s, 工具=%s, 收集=%s, "
                "图片未解析=%s, 隐含需求=%s",
                preflight.complexity_score,
                preflight.message_intent,
                preflight.recommended_tools or "[]",
                preflight.should_collect,
                preflight.has_unresolved_images,
                preflight.implicit_needs or "[]",
            )

        return preflight

    # ── 收集 ───────────────────────────────────────────

    @staticmethod
    async def collect(
        preflight: ContextPreflight,
        tavern,  # duck-typed: .chat_with_tools() 或类似接口
        provider: str = "deepseek",
        config=None,
        bot_id: str = "",
    ) -> dict[str, str]:
        """执行 Pre-flight 推荐的轻量工具调用，收集上下文。

        并发执行，总超时 PREFLIGHT_TIMEOUT 秒。
        冷却门控: 同群同工具在冷却期内跳过。

        Args:
            preflight: analyze() 返回的分析结果
            tavern: TavernClient (需要 .chat_with_tools 方法，用于知识库/VLM)
            provider: LLM provider
            config: Config 对象

        Returns:
            {tool_name: result_text, ...} — 收集到的上下文片段
        """
        if not preflight.should_collect or not preflight.recommended_tools:
            return {}

        collected: dict[str, str] = {}
        tasks = []

        for tool_name in preflight.recommended_tools:
            detail = preflight.tool_details.get(tool_name, {})
            # 冷却检查 (per-bot 同工具 30 秒冷却)
            cooldown_key = f"preflight:{bot_id}:{tool_name}" if bot_id else f"preflight:{tool_name}"
            cooldown_s = (
                getattr(config, "preflight_tool_cooldown", 30) if config else 30
            )
            if _check_cooldown(cooldown_key, cooldown_s):
                logger.debug("Pre-flight 收集: %s 冷却中, 跳过", tool_name)
                continue

            task = ContextGatherer._collect_one(
                tool_name, detail, tavern, provider,
            )
            tasks.append((tool_name, task))

        if not tasks:
            return collected

        # 并发执行，总超时
        logger.info(
            "Pre-flight 收集: 启动 %d 个工具 (%s)",
            len(tasks),
            ", ".join(t[0] for t in tasks),
        )

        async def _run_with_timeout(tool_name, coro):
            try:
                return tool_name, await asyncio.wait_for(
                    coro, timeout=min(PREFLIGHT_TIMEOUT, 4.0),
                )
            except TimeoutError:
                logger.debug("Pre-flight 收集: %s 超时", tool_name)
                return tool_name, None
            except Exception as e:
                logger.debug("Pre-flight 收集: %s 失败: %s", tool_name, e)
                return tool_name, None

        results = await asyncio.gather(
            *[_run_with_timeout(name, coro) for name, coro in tasks],
        )

        for tool_name, result in results:
            if result:
                collected[tool_name] = result

        # 定期清理过期冷却记录
        _clear_expired_cooldowns()

        if collected:
            logger.info(
                "Pre-flight 收集完成: %d/%d 成功 → %s",
                len(collected), len(tasks),
                ", ".join(f"{k}({len(v)}字)" for k, v in collected.items()),
            )

        return collected

    @staticmethod
    async def _collect_one(
        tool_name: str,
        detail: dict,
        tavern,
        provider: str,
    ) -> str | None:
        """执行单个 Pre-flight 工具调用。"""
        if tool_name == "search_knowledge":
            return await ContextGatherer._collect_knowledge(
                detail.get("query", ""),
            )
        if tool_name == "describe_image":
            return await ContextGatherer._collect_vision(
                tavern, provider,
            )
        if tool_name == "get_memory":
            return await ContextGatherer._collect_memory()
        if tool_name == "web_search":
            return await ContextGatherer._collect_web_search(
                detail.get("query", ""),
            )
        if tool_name == "check_lport_status":
            return await ContextGatherer._collect_status()
        if tool_name == "list_available_models":
            return await ContextGatherer._collect_models()
        return None

    @staticmethod
    async def _collect_knowledge(query: str) -> str | None:
        """查询知识库。"""
        if not query:
            return None
        try:
            from astrbot_plugin_suli_services import get_knowledge_base
            kb = get_knowledge_base()
            results = kb.search(query, top_n=2)
            if results:
                # 限制每个结果长度
                truncated = [r[:500] for r in results]
                return "📚 知识库:\n" + "\n---\n".join(truncated)
        except Exception as e:
            logger.debug("Pre-flight KB 查询失败: %s", e)
        return None

    @staticmethod
    async def _collect_vision(tavern, provider: str) -> str | None:
        """调用 VLM 解析图片 — 从最近的图片 URL 获取描述。"""
        try:
            from astrbot_plugin_suli_services import has_active_vlm
            if not has_active_vlm():
                return None

            # 实际 URL 需要从上下文获取 — 这里返回触发标记
            # 具体 URL 由 LLM 在 function calling 中传入
            return None  # Pre-flight 阶段无法直接获取图片 URL
        except Exception as e:
            logger.debug("Pre-flight VLM 调用失败: %s", e)
        return None

    @staticmethod
    async def _collect_memory() -> str | None:
        """查询用户记忆 — 返回空 (记忆在 _build_messages 中注入)。"""
        return None  # 记忆由 _build_messages 统一注入

    @staticmethod
    async def _collect_web_search(query: str) -> str | None:
        """网页搜索。"""
        if not query:
            return None
        try:
            from astrbot_plugin_suli_services import format_web_results, web_search
            results = await web_search(query, max_results=3)
            if results:
                return format_web_results(results, query)[:800]
        except Exception as e:
            logger.debug("Pre-flight 网页搜索失败: %s", e)
        return None

    @staticmethod
    async def _collect_status() -> str | None:
        """查询 L-Port 系统状态。"""
        try:
            from .tools import execute_health_check
            return await execute_health_check({})
        except Exception as e:
            logger.debug("Pre-flight 状态查询失败: %s", e)
        return None

    @staticmethod
    async def _collect_models() -> str | None:
        """查询可用模型列表。"""
        try:
            from .tools import execute_list_models
            result = await execute_list_models({"filter_type": "all"})
            return result[:600] if result else None
        except Exception as e:
            logger.debug("Pre-flight 模型查询失败: %s", e)
        return None

    # ── 辅助方法 ───────────────────────────────────────

    @staticmethod
    def _classify_intent(message: str) -> str:
        """分类消息意图 (纯规则，<0.1ms)。

        Returns:
            "question" | "exclaim" | "agree" | "chat" | "unknown"
        """
        if not message or not message.strip():
            return "unknown"

        msg = message.strip()

        # 问句特征
        if _QUESTION_RE.search(msg):
            return "question"

        # 感叹特征
        if re.search(r"[！!]{2,}|[～~]$|草|我靠|卧槽|牛逼|太.*了|好.*啊", msg):
            return "exclaim"

        # 附和特征 (短消息，表示赞同)
        if len(msg) <= 6 and re.search(
            r"^(嗯|对|是|好|行|可以|没错|确实|笑死|草|6|乐|典|蚌|好好好|"
            r"对的对的|是这样|没错没错|是的|就是|确实如此)$",
            msg,
        ):
            return "agree"

        # 闲聊特征 (有实际内容但非问句)
        if len(msg) >= 4:
            return "chat"

        return "unknown"

    @staticmethod
    def _extract_search_query(
        message: str,
        active_domains: dict[str, float],
    ) -> str:
        """从触发消息中提取知识库搜索关键词。

        简单策略: 取消息核心名词 + 活跃领域关键词。
        """
        if not message:
            return ""

        # 移除常见口语前缀
        cleaned = re.sub(
            r"^(草|笑死|确实|好好好|对的对的|嗯嗯|好的|来了|6)\s*",
            "", message,
        ).strip()

        # 移除问句后缀
        cleaned = re.sub(r"[？?！!。，,～~]+$", "", cleaned).strip()

        # 移除人称/语气词
        cleaned = re.sub(
            r"(我|你|他|她|它|我们|你们|他们|这个|那个|哪个|怎么|什么|"
            r"帮我|帮我看|帮我查|帮我找|能不能|可不可以|有没有|是不是)\s*",
            " ", cleaned,
        ).strip()

        # 取前 40 字作为 query
        query = cleaned[:40].strip()
        return query if len(query) >= 2 else message[:40]


# ── 格式化: 预收集上下文 → prompt 注入文本 ─────────────


def format_collected_context(
    collected: dict[str, str],
    preflight: ContextPreflight,
) -> str:
    """将 Pre-flight 收集到的上下文格式化为 LLM prompt 注入文本。

    Args:
        collected: {tool_name: result_text, ...}
        preflight: 分析结果 (用于生成思考提示)

    Returns:
        格式化的 prompt 文本 (可能为空)
    """
    if not collected:
        return ""

    parts: list[str] = []

    # 背景信息
    bg_lines = ["[预收集的背景信息 — 回复前已自动查询]"]
    for tool_name, result in collected.items():
        if not result:
            continue
        label = {
            "search_knowledge": "📚 知识库",
            "describe_image": "🖼️ 图片分析",
            "get_memory": "📝 用户记忆",
            "web_search": "🌐 网页搜索",
            "check_lport_status": "🔌 系统状态",
            "list_available_models": "📦 可用模型",
        }.get(tool_name, f"🔧 {tool_name}")
        bg_lines.append(f"\n{label}:\n{result[:600]}")

    if len(bg_lines) > 1:
        parts.append("\n".join(bg_lines))

    # 思考提示 (高复杂度时注入)
    if preflight.complexity_score >= 4.0 and preflight.message_intent == "question":
        parts.append(
            "\n\n[思考提示]\n"
            "上面是回复前自动查到的背景信息。请:\n"
            "1. 先在心里理清回答思路再说话\n"
            "2. 如果背景信息足以回答问题，自然地引用它们（不要逐条列举）\n"
            "3. 如果背景信息不够或存在矛盾，诚实指出不确定性\n"
            "4. 技术细节务必准确，不要凭记忆编造"
        )
    elif preflight.complexity_score >= 3.0:
        parts.append(
            "\n\n[提示]\n"
            "回复前已自动查询了一些背景信息（见上）。请自然地参考它们回答。"
        )

    return "\n".join(parts)
