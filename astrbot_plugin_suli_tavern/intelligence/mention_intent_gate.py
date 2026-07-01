"""[DEPRECATED] Mention Intent Gate — 已合并入 IntentGate Stage 1。

保留为 re-export shim，确保旧 import 路径不中断。
新代码请直接使用:
  from .intent_gate import IntentGate, GateContext, RelevanceResult
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
from astrbot_plugin_suli_gate import GateContext, IntentGate, RelevanceResult

# ── Backward-compatible wrapper ────────────────────────────


class MentionIntentGate:
    """[DEPRECATED] 提及意图门控 — 已合并入 IntentGate.evaluate_relevance()。

    保留此类以兼容旧 import 路径。新代码应直接使用 IntentGate。
    """

    @staticmethod
    async def decide(tavern, content: str, sender_name: str,
                     recent_context: list[str], config) -> str:
        """[DEPRECATED] 使用 IntentGate.evaluate_relevance() 替代。"""
        logger.warning(
            "MentionIntentGate.decide() is deprecated. Use IntentGate.evaluate_relevance() instead."
        )
        ctx = GateContext(
            messages=[{"user_id": "", "user_name": sender_name, "content": content}],
            bot_name=getattr(config, "bot_name", "暮恩"),
            peer_bot_name=getattr(config, "peer_bot_name", ""),
            is_at_mention=False,
        )
        result = await IntentGate.evaluate_relevance(tavern, ctx)
        return "reply" if result.directed_to_me else "ignore"


__all__ = ["GateContext", "IntentGate", "MentionIntentGate", "RelevanceResult"]
