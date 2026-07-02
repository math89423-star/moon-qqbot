"""深度问答 — ReAct 异步研究入口。

触发条件:
  - gate domain=technical + 高复杂度信号
  - 用户显式要求深入研究 ("研究一下"、"深入分析"等)
  - 群聊普通问题 (含工具调用) 继续走 run_tool_loop，不触发 ReAct

流程:
  1. 检测是否应触发深度问答
  2. 先发送占位消息 "让我查一下..."
  3. asyncio.create_task() → ReAct 循环
  4. 循环结束后发送研究结果

注意: ReAct 是异步执行的——用户会先看到"让我查一下..."，
     然后等研究完成后再收到结果。群聊实时链路不受影响。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrbot_plugin_suli_gate import GateResultProtocol
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any
    from collections.abc import Awaitable

logger = logging.getLogger(__name__)

# ── 按群 + per-bot 并发锁 ─────────────────────────────────────────
# 同一群同一 bot 同时只允许一个 ReAct 任务在跑。
# 复合键 f"{bot_id}:{group_id}" — 两个 bot 在同一群的深度问答互不阻塞。
_deep_qa_locks: dict[str, asyncio.Lock] = {}
_deep_qa_active: set[str] = set()  # 正在执行深度问答的 "bot_id:group_id"


def _get_deep_qa_lock(bot_id: str, group_id: str) -> asyncio.Lock:
    """获取或创建按 bot+群的深度问答锁。"""
    key = f"{bot_id}:{group_id}"
    if key not in _deep_qa_locks:
        _deep_qa_locks[key] = asyncio.Lock()
    return _deep_qa_locks[key]


# ── 深度问答触发检测 ─────────────────────────────────────────

# 用户显式研究意图的关键词
# NOTE: "为什么"/"怎么理解"/"讲清楚" 已移除 —— 这些是日常对话高频词，
# 不应触发 41s 深度研究。误触发案例: "为什么 bot 不回复我消息了"。

_RESEARCH_KEYWORDS = (
    "研究一下", "深入研究", "深入分析", "帮我分析一下",
    "帮我查查", "帮我查一下", "帮我搜一下",
    "仔细分析", "详细分析", "全面分析",
    "对比一下", "比较一下", "有什么区别",
)

# 触发深度研究的最短消息长度 (纯 "为什么" 不算)
_MIN_RESEARCH_LENGTH = 8


def is_deep_question(
    gate_result: dict | Any,
    user_message: str = "",
    domain: str = "",
    active_domains: list[str] | None = None,
) -> bool:
    """判断一个问题是否需要深度研究 (ReAct 循环)。

    当前策略: 仅显式关键词触发。不隐式触发——
    避免"随口问的技术小问题被郑重其事地查一分半"的体验。
    显式触发跑稳后再考虑是否开放隐式触发。

    Args:
        gate_result: Gate 评估结果 (未使用，保留接口兼容)
        user_message: 用户消息原文
        domain: gate 输出的 domain 字段 (未使用，保留接口兼容)
        active_domains: 当前活跃领域列表 (未使用，保留接口兼容)
    """
    # ── 仅显式研究请求 ──
    msg_lower = user_message.lower().strip()
    for kw in _RESEARCH_KEYWORDS:
        if kw in msg_lower and len(user_message) >= _MIN_RESEARCH_LENGTH:
            logger.warning("deep_qa: [FALLBACK] Gate 不可用, 关键词回退触发 → %r", kw)
            return True

    # 隐式触发已禁用 (v1)
    # 待 ReAct 跑稳后可按需启用: domain=technical + intent=question + 活跃领域≥2

    return False


def is_deep_question_via_gate(gate_result: GateResultProtocol | None) -> bool:
    """通过 Gate 输出判断是否需要深度研究（主路径）。

    Gate 的 intent_type 和 suggested_tools 是 LLM 驱动的意图分类。
    当 Gate 输出 intent_type="deep_inquiry" 或 suggested_tools 含 "deep_research" 时，
    直接走 ReAct 链路——不再依赖关键词匹配。

    Args:
        gate_result: GateResultProtocol | None

    Returns:
        True 如果 Gate 判定需要深度研究
    """
    if gate_result is None:
        return False
    if gate_result.intent_type == "deep_inquiry":
        return True
    if gate_result.suggested_tools and "deep_research" in gate_result.suggested_tools:
        return True
    return False


# ── 深度问答执行 ──────────────────────────────────────────────

async def execute_deep_qa(
    *,
    react_engine,     # ReActEngine 实例
    user_query: str,
    user_name: str = "",
    bot_id: str = "",
    group_id: str = "",
    placeholders: list[Awaitable] | None = None,  # 占位消息发送后回调
    on_complete=None,  # async callable(result: ReActResult) → None
) -> None:
    """异步执行深度问答: 占位 → ReAct → 回传结果。

    此函数设计为通过 asyncio.create_task() 调用，
    不阻塞群聊实时链路。

    按 bot+群并发控制: 同一群同一 bot 同时只允许一个 ReAct 任务。
    新任务在前一个完成前被跳过 (不排队——立刻提示"我还在查上一个")。
    """
    lock = _get_deep_qa_lock(bot_id, group_id)
    _dq_key = f"{bot_id}:{group_id}"
    if lock.locked():
        logger.info("deep_qa: bot=%s 群 %s 已有深度问答在跑，跳过新任务", bot_id, group_id)
        if on_complete:
            try:
                from astrbot_plugin_suli_tavern.intelligence.react_engine import ReActResult
                busy_result = ReActResult(
                    final_answer="我还在查上一个问题，请稍等～",
                    rounds_used=0, tokens_burned=0, hit_limit=False,
                )
                await on_complete(busy_result)
            except BaseException:
                pass
        return

    async with lock:
        _deep_qa_active.add(_dq_key)
        try:
            # ── 发送占位 ──
            if placeholders:
                for ph in placeholders:
                    try:
                        await ph
                    except Exception:
                        logger.debug("deep_qa: 占位消息发送失败", exc_info=True)

            # ── 执行 ReAct ──
            logger.info("deep_qa: 开始研究 bot=%s group=%s q=%r", bot_id, group_id, user_query[:100])
            result = await react_engine.run(
                user_query=user_query,
                context={"group_id": group_id, "user_name": user_name},
            )

            logger.info(
                "deep_qa: 研究完成 rounds=%d tokens=%d elapsed=%dms hit_limit=%s",
                result.rounds_used, result.tokens_burned,
                result.elapsed_ms, result.hit_limit,
            )

            # ── 回传结果 ──
            if on_complete:
                await on_complete(result)
            else:
                logger.warning("deep_qa: on_complete 回调未设置，结果丢弃")

        except BaseException:
            # 包住一切异常 (含 asyncio.CancelledError) — 绝不能让用户停在占位消息上
            logger.error("deep_qa: 执行异常 (fire-and-forget task)", exc_info=True)
            if on_complete:
                try:
                    from astrbot_plugin_suli_tavern.intelligence.react_engine import ReActResult
                    error_result = ReActResult(
                        final_answer="抱歉，研究过程中遇到了意外问题。请稍后再试。",
                        rounds_used=0, tokens_burned=0, hit_limit=True,
                    )
                    await on_complete(error_result)
                except BaseException:
                    logger.error("deep_qa: 连错误兜底消息都发送失败了!", exc_info=True)
        finally:
            _deep_qa_active.discard(_dq_key)
