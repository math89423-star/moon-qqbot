"""[DEPRECATED] Reply Gate — 已合并入 IntentGate Stage 2。

保留为 re-export shim，确保旧 import 路径不中断。
新代码请直接使用:
  from .intent_gate import IntentGate, GateContext, IntentResult
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
from astrbot_plugin_suli_gate import GateContext, IntentGate, IntentResult

# ── Backward-compatible wrapper ────────────────────────────


class ReplyGate:
    """[DEPRECATED] 回复门控 — 已合并入 IntentGate.evaluate_intent()。

    保留此类以兼容旧 import 路径。新代码应直接使用 IntentGate。
    """

    @staticmethod
    async def decide(plugin_or_tavern, messages: list[dict], *args,
                     trigger_reason: str = "batch",
                     thread_hint: dict | None = None, **kwargs) -> str:
        """[DEPRECATED] 使用 IntentGate.evaluate_intent() 替代。

        保留旧签名以兼容现有调用。
        """
        logger.warning(
            "ReplyGate.decide() is deprecated. Use IntentGate.evaluate_intent() instead."
        )
        # Minimal emulation: default to "reply" (fail-open)
        return "reply"


__all__ = ["GateContext", "IntentGate", "IntentResult", "ReplyGate"]
