"""任务级工具上下文 — 后台任务的 allowlist 与触发人透传 (进程级单例)。

用途:
  - dispatcher 派发后台任务前 push 本次任务的工具 allowlist / 触发人;
  - 任务结束 pop, 恢复无任务态 (无任务时全放行)。
  - 在拿不到调用方 contextvars 的执行位置, 可经 current_trigger() 读触发人
    (pixiv 单图压回看触发原文等判定用)。

说明: 多 bot 各自独立进程 → 模块级状态天然 per-bot, 无需 key。
无活跃任务 (栈空) → 全量 (兼容健康检查/直连调试, 行为不变)。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


# ── 任务级工具 allowlist (栈式, 可嵌套) ──
# 任务执行器据此拦截未授权工具调用；无活跃任务时 None = 全量。
_SCOPE_STACK: list[frozenset[str]] = []
# 健康探针旁路计数 (>0 时执行门全放行，供本地探针与健康检查用)。
_SCOPE_SUSPEND: int = 0

# ── 任务级触发人 (requester_id) 透传 ──
# 确认门靠它判定“同一用户确认”，缺失则永不过。
# 与 _SCOPE_STACK 同生命周期：dispatcher 包任务执行时一并压栈。
_TRIGGER_STACK: list[str] = []


def push_task_scope(tools: frozenset[str] | set[str] | list[str] | tuple | None) -> bool:
    """压入任务 allowlist。空/None 不压栈 (fail-open)。返回是否生效。"""
    scope = frozenset(str(t).strip() for t in (tools or []) if str(t).strip())
    if not scope:
        return False
    _SCOPE_STACK.append(scope)
    return True


def pop_task_scope() -> None:
    """弹出任务 allowlist (幂等)。"""
    if _SCOPE_STACK:
        _SCOPE_STACK.pop()


def current_scope() -> frozenset[str] | None:
    """当前生效 allowlist，无任务时 None。"""
    return _SCOPE_STACK[-1] if _SCOPE_STACK else None


@contextmanager
def suspend_task_scope() -> Iterator[None]:
    """暂停任务过滤 (健康探针全量盘点用，可嵌套)。"""
    global _SCOPE_SUSPEND
    _SCOPE_SUSPEND += 1
    try:
        yield
    finally:
        _SCOPE_SUSPEND = max(0, _SCOPE_SUSPEND - 1)


def _scope_suspended() -> bool:
    return _SCOPE_SUSPEND > 0


def push_trigger(trigger_uid: str) -> bool:
    """压入任务触发人。空串不压栈。返回是否生效。"""
    uid = str(trigger_uid or "").strip()
    if not uid:
        return False
    _TRIGGER_STACK.append(uid)
    return True


def pop_trigger() -> None:
    """弹出任务触发人 (幂等)。"""
    if _TRIGGER_STACK:
        _TRIGGER_STACK.pop()


def current_trigger() -> str:
    """当前任务触发人，无任务时空串。"""
    return _TRIGGER_STACK[-1] if _TRIGGER_STACK else ""


@contextmanager
def task_scope(
    tools: frozenset[str] | set[str] | list[str] | tuple | None,
    trigger_uid: str | None = None,
) -> Iterator[None]:
    """任务级 allowlist 上下文 (dispatcher 包后台任务执行用)。"""
    pushed = push_task_scope(tools)
    pushed_trigger = push_trigger(trigger_uid or "")
    try:
        yield
    finally:
        if pushed:
            pop_task_scope()
        if pushed_trigger:
            pop_trigger()


def is_tool_allowed(tool_name: str) -> bool:
    """当前任务是否允许该工具 (无任务/探针旁路时全放行)。"""
    if _scope_suspended():
        return True
    scope = current_scope()
    return True if scope is None else str(tool_name) in scope


def _scope_denied_text(tool_name: str) -> str:
    scope = current_scope() or frozenset()
    return (
        f"⛔ 工具 {tool_name} 本轮任务未授权 (仅限 {sorted(scope)})。"
        "请用已授权工具完成，缺失能力请如实告知用户，不要编造。"
    )
