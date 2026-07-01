"""安全协程任务包装器 — C4 fire-and-forget 统一异常回调.

用法:
  from ._safe_task import safe_task

  # 替代 asyncio.create_task(coro)
  safe_task(coro, name="profile_build")

设计:
  - 包装 asyncio.create_task + add_done_callback
  - 协程未捕获异常自动记录 logger.error
  - 保持与 asyncio.create_task 相同的返回类型和语义
"""

from __future__ import annotations

import asyncio
import logging
from typing import Coroutine

logger = logging.getLogger(__name__)


def safe_task(
    coro: Coroutine,
    *,
    name: str = "",
    log_level: int = logging.ERROR,
) -> asyncio.Task:
    """创建 fire-and-forget 任务，自动记录未捕获异常。

    替代裸 asyncio.create_task() — 确保协程异常不会被静默丢弃。

    Args:
        coro: 要执行的协程
        name: 任务名称 (用于日志, 可选)
        log_level: 异常日志级别 (默认 ERROR)

    Returns:
        asyncio.Task (与 create_task 返回值相同)

    Example:
        safe_task(maybe_build_profile(...), name="profile:user123")
    """
    task = asyncio.create_task(coro, name=name or None)

    def _on_done(t: asyncio.Task) -> None:
        try:
            exc = t.exception()
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            return  # 正常取消 / 尚未完成
        if exc is not None:
            _label = name or t.get_name() or "?"
            logger.log(
                log_level,
                "safe_task: 未捕获异常 in %s: %s",
                _label, exc, exc_info=exc,
            )

    task.add_done_callback(_on_done)
    return task
