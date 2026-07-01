"""astrbot_plugin_suli_gate — 统一 Intent Gate (意图行为闸)。

Full Gate 四大职责:
  1. group_context — 群聊上下文分析
  2. task          — 意图/任务判断
  3. tools         — 工具放行决策
  4. reply_baseline — 回复基调 (stance/style/sticker_mood)

+ 加权唤醒分 (compute_wake_weight) + Grace Period + Opportunity Score

零框架耦合。LLM 调用通过 duck-typed tavern.chat() 接口注入。

用法:
  from astrbot_plugin_suli_gate import (
      GateContext, IntentGate, FullGateResult,
      GroupContext, TaskDecision, ToolDecision, ReplyBaseline,
      GracePeriod, compute_wake_weight, WAKE_THRESHOLD,
  )
"""

from __future__ import annotations

from ._gate_protocol import GateResultProtocol
from .intent_gate import (
    WAKE_THRESHOLD,
    CrossBotAction,
    FullGateResult,
    GateContext,
    GracePeriod,
    GroupContext,
    GroupSituation,
    IntentGate,
    IntentResult,
    RelevanceResult,
    ReplyBaseline,
    TaskDecision,
    ToolDecision,
    compute_wake_weight,
)
from .opportunity import (
    ActiveMotive,
    MotiveType,
    OpportunityCache,
    OpportunityScore,
    PendingOpportunity,
)

__all__ = [
    "WAKE_THRESHOLD",
    "ActiveMotive",
    "CrossBotAction",
    "FullGateResult",
    "GateContext",
    "GateResultProtocol",
    "GracePeriod",
    "GroupContext",
    "GroupSituation",
    "IntentGate",
    "IntentResult",
    "MotiveType",
    "OpportunityCache",
    "OpportunityScore",
    "PendingOpportunity",
    "RelevanceResult",
    "ReplyBaseline",
    "TaskDecision",
    "ToolDecision",
    "compute_wake_weight",
]
