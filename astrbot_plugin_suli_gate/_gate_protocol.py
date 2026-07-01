"""GateResultProtocol — Gate 评估结果的结构类型契约。

所有 FullGateResult 消费者（group_chat.py, prompt_builder.py 等）
通过此协议只读访问 gate 输出，不依赖具体 dataclass。

用法:
    from astrbot_plugin_suli_gate import GateResultProtocol

    def handle(gate: GateResultProtocol | None) -> None:
        if gate is not None and gate.parse_ok:
            print(gate.task.intent_type)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .intent_gate import CrossBotAction, GroupContext, GroupSituation


class GateResultProtocol(Protocol):
    """Gate 评估结果的结构类型契约。

    
    保留旧字段名作为向后兼容 — 新代码应使用 task.* / tools.* / reply_baseline.*。

    协议不要求继承——FullGateResult 通过结构子类型自然满足。
    """

    # ── 四大职责 ──
    group_context: GroupContext
    task: object  # TaskDecision
    tools: object  # ToolDecision
    reply_baseline: object  # ReplyBaseline

    # ── 元数据 ──
    cross_bot_action: CrossBotAction | None
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
    # ★ 向后兼容 property 别名 (已通过 FullGateResult property 实现)
    # 以下通过 Protocol 的 structural typing 自然满足 —
    # FullGateResult 的 property 别名使得旧代码仍能 work。
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
    @property
    def group_situation(self) -> GroupSituation | None: ...
