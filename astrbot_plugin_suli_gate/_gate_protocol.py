"""GateResultProtocol — Gate 评估结果的结构类型契约。

[历史] 此协议描述已删除的 FullGateResult 形状。现役 SlimGateResult
只有 candidate_route/task_hint/group_context 等少量字段；此处保留协议
名仅为兼容三个旧链消费点 (group_chat._fmt_gate/_log_handoff、
prompt_builder._build_gate_directive、deep_qa.is_deep_question_via_gate) 的
import 不炸。运行时 duck-typed，不做强制校验。新代码不要再依赖本协议。

用法:
    from astrbot_plugin_suli_gate import GateResultProtocol

    def handle(gate: GateResultProtocol | None) -> None:
        if gate is not None and gate.parse_ok:
            print(gate.task.intent_type)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .intent_gate import GroupContext
    from .opportunity import OpportunityScore


class GateResultProtocol(Protocol):
    """Gate 评估结果的结构类型契约 (历史遗留，描述已删除的 Full 形状)。

    保留旧字段名仅为兼容旧链消费点的 import/注解 — 新代码不要再用。
    """

    # ── 四大职责 ──
    group_context: GroupContext
    task: object  # TaskDecision
    tools: object  # ToolDecision
    reply_baseline: object  # ReplyBaseline

    # ── 机会评分 (LLM 输出) ──
    llm_opportunity_score: OpportunityScore

    # ── 元数据 ──
    should_profile: bool
    reply_target_user_id: str
    reply_target_user_name: str
    model_tier: str
    reasoning_effort: str
    reasoning: str

    # ── 解析状态 ──
    parse_ok: bool
    parse_error: str

    # ── 运行时元数据 ──
    _original_suggested_tools: list[str]
    social_suppress_tools: bool

    # ═════════════════════════════════════════════════════════
    # ★ 历史 property 别名区 (随 Full 退役，仅留名兼容注解)
    # ═════════════════════════════════════════════════════════

    # -- Stage 1 dead fields (永远固定值) --
    @property
    def directed_to_me(self) -> bool: ...
    @property
    def relevance_confidence(self) -> float: ...
    @property
    def relevance_reasoning(self) -> str: ...
    @property
    def target_users(self) -> list[str]: ...
    @property
    def fast_path(self) -> bool: ...
    @property
    def should_reply(self) -> bool: ...

    # -- 字段路径别名 --
    @property
    def intent_type(self) -> str: ...
    @intent_type.setter
    def intent_type(self, value: str) -> None: ...
    @property
    def urgency(self) -> str: ...
    @urgency.setter
    def urgency(self, value: str) -> None: ...
    @property
    def domain(self) -> str: ...
    @domain.setter
    def domain(self, value: str) -> None: ...
    @property
    def input_nature(self) -> str: ...
    @input_nature.setter
    def input_nature(self, value: str) -> None: ...
    @property
    def suggested_tools(self) -> list[str]: ...
    @suggested_tools.setter
    def suggested_tools(self, value: list[str]) -> None: ...
    @property
    def reply_style(self) -> str: ...
    @reply_style.setter
    def reply_style(self, value: str) -> None: ...
    @property
    def reply_stance(self) -> str: ...
    @reply_stance.setter
    def reply_stance(self, value: str) -> None: ...
    @property
    def suggested_sticker_mood(self) -> str: ...
    @suggested_sticker_mood.setter
    def suggested_sticker_mood(self, value: str) -> None: ...
    @property
    def voice_boundary(self) -> str: ...
    @property
    def persona_facet(self) -> str: ...
    @property
    def target_user_id(self) -> str: ...
    @target_user_id.setter
    def target_user_id(self, value: str) -> None: ...
    @property
    def target_user_name(self) -> str: ...
    @target_user_name.setter
    def target_user_name(self, value: str) -> None: ...
    @property
    def intent_reasoning(self) -> str: ...
    @intent_reasoning.setter
    def intent_reasoning(self, value: str) -> None: ...
