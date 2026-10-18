"""Immutable Actor 事件定义 — P0-1 Actor 单写者地基。

所有事件 frozen=True，不可变，跨 on_message → InputCoalescer → Actor 传递。
禁止依赖共享 mutable ctx.last_event。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


def task_result_at_prefix(
    group_id: str = "",
    requester_id: str = "",
    text: str = "",
    known_bot_ids: frozenset | set | tuple = frozenset(),
) -> str:
    """派单结果 @ 前缀 (2026-10-15): 群里人多, 结果不@会被刷掉。

    返回 "" = 不@ (私聊/请求者为空/请求者是bot/正文已自带@);
    否则返回 "[CQ:at,qq={rid}] " (调用方直接拼在正文前)。
    纯函数, 行为锁死在 test_task_at.py。
    """
    _rid = str(requester_id or "").strip()
    _gid = str(group_id or "").strip()
    _t = str(text or "")
    if not _rid or not _gid or _gid == "0":
        return ""
    try:
        if _rid in (known_bot_ids or frozenset()):
            return ""
    except Exception:
        pass
    if "[CQ:at,qq=" in _t or "[At:" in _t:
        return ""
    return f"[CQ:at,qq={_rid}] "


@dataclass(frozen=True, slots=True)
class MessageEvent:
    """群内一条原始消息的不可变投递。"""

    event_id: str
    seq: int
    bot_id: str
    group_id: str
    user_id: str
    user_name: str
    message_id: str
    plaintext: str
    timestamp: float
    reply_to: str | None = None
    attachments: tuple = field(default_factory=tuple)  # (url, file_id) 元组序列
    trigger_reason: str = ""  # mention|reply|nickname|thread_continuation|batch|debounce|proactive
    raw_event: object | None = None  # 携带原始 EventAdapter 引用（仅用于 send 回复时取 self_id/group_id），不参与相等


@dataclass(frozen=True, slots=True)
class BatchEvent:
    """InputCoalescer 3s 累积后的批量投递（Actor 消费单位）。"""

    event_id: str
    seq: int
    bot_id: str
    group_id: str
    # 合并后的文本（首条 + 同用户追发）
    merged_text: str
    # 合并包含的原始 MessageEvent 列表（保持顺序，仅用于溯源）
    source_events: tuple
    trigger_reason: str  # 首条的 trigger_reason
    timestamp: float = field(default_factory=time.time)
    # 首条触发者的身份（用于 Gate/MainDecision 快速取）
    trigger_user_id: str = ""
    trigger_user_name: str = ""


@dataclass(frozen=True, slots=True)
class TaskResultEvent:
    """执行器/Worker 完成后的结构化结果重投递（高优先级）。"""

    event_id: str
    seq: int
    bot_id: str
    group_id: str
    task_id: str
    origin_message_id: str
    requester_id: str
    status: Literal["completed", "partial", "failed", "cancelled", "expired"]
    summary: str = ""
    facts: tuple = field(default_factory=tuple)
    evidence: tuple = field(default_factory=tuple)
    uncertainties: tuple = field(default_factory=tuple)
    artifacts: tuple = field(default_factory=tuple)  # artifact_id 列表
    suggested_answer_points: tuple = field(default_factory=tuple)
    elapsed: float = 0.0
    timestamp: float = field(default_factory=time.time)
    error_code: str | None = None
    # 2026-09-08: 原始失败明细（上游 500/超时/空响应等），二次人格化如实上报用。
    error: str = ""
    # D09: origin 原始事件（Batch source 首条的 raw_event），结果发送时优先引用它。
    # 不参与相等比较；跨进程不序列化，仅同进程投递使用。
    origin_event: Any = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class CancelEvent:
    """任务取消指令。"""

    event_id: str
    seq: int
    bot_id: str
    group_id: str
    task_id: str
    requester_id: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ImageDescriptionReadyEvent:
    """VLM 完成后的描述就绪（替代直接改 history object）。"""

    event_id: str
    seq: int
    bot_id: str
    group_id: str
    origin_message_id: str
    user_id: str
    descriptions: tuple = field(default_factory=tuple)
    urls: tuple = field(default_factory=tuple)


def new_event_id() -> str:
    return f"evt-{uuid.uuid4().hex[:8]}"


def now_ts() -> float:
    return time.time()
