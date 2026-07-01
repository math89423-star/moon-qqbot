"""[DEPRECATED] Intent Judge — 已合并入 IntentGate Stage 2。

保留为 re-export shim + JudgeDecision 兼容层。
新代码请直接使用:
  from .intent_gate import IntentGate, GateContext, IntentResult
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
from astrbot_plugin_suli_gate import GateContext, IntentGate, IntentResult

# ── JudgeDecision backward-compatible wrapper ──────────────


@dataclass
class ReplyTarget:
    """[DEPRECATED] 回复目标 — 已合并入 IntentResult.target_user_id/name。"""
    user_id: str = ""
    user_name: str = ""
    message_index: int = -1


@dataclass
class JudgeDecision:
    """[DEPRECATED] Judge 决策 — 已合并入 IntentResult。

    保留此 dataclass 以兼容旧代码中的字段访问。
    新代码应使用 IntentResult。
    """
    reasoning: str = ""
    decision: str = "pass"
    reply_target: ReplyTarget = field(default_factory=ReplyTarget)
    mode: str = "normal"
    intent: str = "question"
    domain: str = "none"
    should_profile: bool = False
    peer_is_suspected_bot: bool = False
    suspicion_score: float = 0.0
    peer_play: str = ""
    peer_target: str = ""
    parse_ok: bool = True
    parse_error: str = ""

    @property
    def should_reply(self) -> bool:
        return self.decision == "reply"

    @property
    def trigger_user_id(self) -> str:
        return self.reply_target.user_id

    @staticmethod
    def from_intent_result(result: IntentResult, relevance=None) -> "JudgeDecision":
        """从新 IntentResult 构造兼容的 JudgeDecision。"""
        return JudgeDecision(
            reasoning=result.reasoning,
            decision="reply" if result.should_reply else "pass",
            reply_target=ReplyTarget(
                user_id=result.target_user_id,
                user_name=result.target_user_name,
            ),
            mode="advanced" if result.model_tier in ("pro", "opus") else "normal",
            intent=result.intent_type,
            domain=result.domain,
            should_profile=result.should_profile,
            parse_ok=result.parse_ok,
            parse_error=result.parse_error,
        )


class IntentJudge:
    """[DEPRECATED] 意图判断器 — 已合并入 IntentGate。

    保留此类以兼容旧 import 路径。
    """

    @staticmethod
    async def evaluate(tavern, ctx, trigger_reason: str = "batch",
                       trigger_uid: str = "", config=None,
                       thread_context=None, bot_suspicion=None) -> None:
        """[DEPRECATED] 使用 IntentGate.evaluate_relevance() + evaluate_intent() 替代。"""
        logger.warning(
            "IntentJudge.evaluate() is deprecated. Use IntentGate instead."
        )
        # FIXME: ctx here is GroupChatContext not GateContext


__all__ = [
    "GateContext",
    "IntentGate",
    "IntentJudge",
    "IntentResult",
    "JudgeDecision",
    "ReplyTarget",
]
