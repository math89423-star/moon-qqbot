"""MainDecision — Gate candidate → Main 终审的一次 LLM 结构化产出。

按裁定 P0-2：Gate 仅给 candidate_route，Main 单次 LLM 同步产
response_mode + immediate_reply + task_brief（含 source_message_ids grounding）。

response_mode 支持 REPLY_AND_DELEGATE，覆盖 CHAT+TASK 并发。

此文件仅数据结构 + 轻量校验，不含 LLM 调用逻辑（调用在 group_chat._handle_actor_batch 后的 Main LLM）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TaskBrief:
    objective: str = ""
    context_facts: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    required_outputs: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    source_message_ids: list[str] = field(default_factory=list)
    continuation_of: str | None = None
    # 生图确认（Main 判定，dispatcher 复核）：本轮有待确认生图单且用户本句是
    # 短确认（执行/开画…）才填 true；单字“好”不算；风格/尺寸讨论、长句一律 false。
    user_confirmed: bool = False
    # 生图主体（Main 语义判定，dispatcher 与字面规则双判）：用户本句给了可画的
    # 具体主体（人物/角色/动物/场景/物品/明确画什么，或本句带图/引用图当主体）
    # 才填 true；纯空话（画一张/来一张类）、只有客套量词（谢谢/麻烦/嘛哦）填 false；
    # 拿不准填 true（别误杀，字面规则兜底真空）。缺省 true = fail-open。
    has_subject: bool = True


@dataclass(slots=True)
class MainDecision:
    response_mode: str = "IGNORE"  # IGNORE|REPLY_ONLY|DELEGATE_ONLY|REPLY_AND_DELEGATE
    immediate_reply: str = ""
    # 连发补充 (REPLY_ONLY): 回完正题后的 0-2 条追问/展开/玩笑，发送侧逐条直发
    follow_ups: list[str] = field(default_factory=list)
    task_brief: TaskBrief | None = None
    reasoning: str = ""
    # grounding 校验：brief 是否可追溯到原话
    grounding_ok: bool = True
    # 自主配图意图（NSFW）：{"prompt": "≤100字本幕画面描述"} 或 None。
    # 仅 Brain 在 [NSFW配图]行放行时填写；派单与否由 group_chat 复核 zone，dispatcher 三验。
    illustrate: dict | None = None
    # 自主配图意图（NSFW）：{"prompt": "≤100字本幕画面描述"} 或 None。
    # 仅 Brain 在 [NSFW配图]行放行时填写；派单与否由 group_chat 复核 zone，dispatcher 三验。

    def is_delegate(self) -> bool:
        return self.response_mode in ("DELEGATE_ONLY", "REPLY_AND_DELEGATE")

    def is_reply(self) -> bool:
        return self.response_mode in ("REPLY_ONLY", "REPLY_AND_DELEGATE")


def validate_brief(brief: TaskBrief, allowed_ids: set[str]) -> bool:
    """校验 source_message_ids 是否均在批量中（防幻觉）。"""
    if not brief.source_message_ids:
        return False
    return all(mid in allowed_ids for mid in brief.source_message_ids)
