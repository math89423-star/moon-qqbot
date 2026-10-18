"""astrbot_plugin_suli_gate — Slim 意图闸 (现役)。

现役: GateContext + IntentGate.evaluate_slim → Main 终审。
旧 Full Gate (FullGateResult 四职责) 已随 Actor 统一删除；
Relevance/Intent/GracePeriod 仅供 DEPRECATED shim 兼容保留。

+ 加权唤醒分 (compute_wake_weight) + Opportunity Score (opportunity.py)。

零框架耦合。LLM 调用通过 duck-typed tavern.chat() 接口注入。

用法:
  from astrbot_plugin_suli_gate import (
      GateContext, IntentGate,
      GroupContext,
      GracePeriod, compute_wake_weight, WAKE_THRESHOLD,
  )
"""

from __future__ import annotations

from ._gate_protocol import GateResultProtocol
from .intent_gate import (
    WAKE_THRESHOLD,
    GateContext,
    GracePeriod,
    GroupContext,
    IntentGate,
    IntentResult,
    RelevanceResult,
    compute_wake_weight,
)
from .opportunity import (
    ActiveMotive,
    MotiveType,
    OpportunityCache,
    OpportunityScore,
    PendingOpportunity,
    compute_rule_score,
)

__all__ = [
    "WAKE_THRESHOLD",
    "ActiveMotive",
    "GateContext",
    "GateResultProtocol",
    "GracePeriod",
    "GroupContext",
    "IntentGate",
    "IntentResult",
    "MotiveType",
    "OpportunityCache",
    "OpportunityScore",
    "PendingOpportunity",
    "RelevanceResult",
    "compute_rule_score",
    "compute_wake_weight",
]
