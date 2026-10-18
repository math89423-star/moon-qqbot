"""Async Memory Writer — 异步记忆写入管理器。

核心职责:
  1. 将记忆写入操作从主回复路径中分离
  2. 后台异步执行: 事件提取 / 记忆候选 / 关系更新 / 时间线
  3. Read-Write 分离: 主回复只读, 写入异步

设计原则 (来自 EverHome 方法论):
  -先说话, 再记日记
  - Read path 和 Write path 分离
  - 主回复不应该等待 WRITE Canonical Life 完成

参考:
  - SillyTavern: non-blocking summarization
  - Character.AI: post-turn pipeline
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class MemoryWriteTask:
    """记忆写入任务。"""
    task_type: str            # memory_candidate / relationship_delta / event_extraction / summary
    user_id: str
    bot_id: str
    group_id: str = ""
    content: str = ""
    metadata: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    priority: int = 0         # 0=正常, >0=高优


class AsyncMemoryWriter:
    """异步记忆写入管理器。

    核心流程:
      1. 主回复路径调用 schedule_write() 注册写入任务
      2. 后台 worker 异步处理队列中的任务
      3. 写入失败不影响主回复

    典型用法:
      writer = get_memory_writer()
      writer.schedule_write(MemoryWriteTask(
          task_type="memory_candidate",
          user_id="123456",
          bot_id="789012",
          content="用户提到喜欢猫",
      ))
      # 主回复继续, 不等待写入完成
    """

    def __init__(self, max_workers: int = 2):
        self._queue: asyncio.Queue[MemoryWriteTask] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._max_workers = max_workers
        self._running = False
        self._handlers: dict[str, Callable] = {}
        self._stats = {
            "total_scheduled": 0,
            "total_completed": 0,
            "total_failed": 0,
            "avg_latency_ms": 0.0,
        }

    async def start(self) -> None:
        """启动后台 workers。"""
        if self._running:
            return

        self._running = True
        for i in range(self._max_workers):
            task = asyncio.create_task(self._worker(f"worker-{i}"))
            self._workers.append(task)

        logger.info("AsyncMemoryWriter 启动, %d workers", self._max_workers)

    async def stop(self) -> None:
        """停止后台 workers。"""
        self._running = False
        for task in self._workers:
            task.cancel()
        self._workers.clear()
        logger.info("AsyncMemoryWriter 停止")

    def register_handler(self, task_type: str, handler: Callable) -> None:
        """注册任务类型处理器。

        Args:
            task_type: 任务类型 (memory_candidate / relationship_delta / ...)
            handler: 异步处理函数 (task: MemoryWriteTask) -> None
        """
        self._handlers[task_type] = handler
        logger.info("注册记忆写入处理器: %s", task_type)

    def schedule_write(self, task: MemoryWriteTask) -> None:
        """调度异步写入任务 (非阻塞)。

        这个方法立即返回, 不等待写入完成。
        """
        try:
            self._queue.put_nowait(task)
            self._stats["total_scheduled"] += 1
            logger.debug(
                "调度记忆写入: type=%s, user=%s, bot=%s",
                task.task_type, task.user_id, task.bot_id,
            )
        except asyncio.QueueFull:
            logger.warning("记忆写入队列已满, 丢弃任务: %s", task.task_type)
            self._stats["total_failed"] += 1

    async def _worker(self, name: str) -> None:
        """后台 worker: 从队列取任务并执行。"""
        logger.info("记忆写入 worker 启动: %s", name)

        while self._running:
            try:
                task = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=1.0,  # 1秒超时, 避免阻塞退出检查
                )
            except asyncio.TimeoutError:
                continue

            start_time = time.time()

            try:
                handler = self._handlers.get(task.task_type)
                if handler:
                    await handler(task)
                else:
                    logger.debug("无处理器: %s", task.task_type)

                self._stats["total_completed"] += 1
                latency_ms = (time.time() - start_time) * 1000
                self._stats["avg_latency_ms"] = (
                    self._stats["avg_latency_ms"] * 0.9 + latency_ms * 0.1
                )

            except Exception as e:
                self._stats["total_failed"] += 1
                logger.error(
                    "记忆写入失败: type=%s, user=%s, error=%s",
                    task.task_type, task.user_id, str(e),
                )

    def get_stats(self) -> dict:
        """获取统计信息。"""
        return {
            **self._stats,
            "queue_size": self._queue.qsize(),
            "workers_active": len(self._workers),
        }


# ═══════════════════════════════════════════════════════════════
# §2 预定义处理器
# ═══════════════════════════════════════════════════════════════

async def _handle_memory_candidate(task: MemoryWriteTask) -> None:
    """处理记忆候选任务: 从对话中提取值得记住的信息。"""
    # TODO: 接入 ProfileAgent / MemoryTier
    logger.debug("处理记忆候选: user=%s, content=%s", task.user_id, task.content[:50])


async def _handle_relationship_delta(task: MemoryWriteTask) -> None:
    """处理关系变化任务: 更新好感度/亲密度。"""
    # TODO: 接入 EmotionEngine / AffinityManager
    logger.debug("处理关系变化: user=%s", task.user_id)


async def _handle_event_extraction(task: MemoryWriteTask) -> None:
    """处理事件提取任务: 从对话中提取关键事件。"""
    # TODO: 接入事件提取逻辑
    logger.debug("处理事件提取: user=%s", task.user_id)


async def _handle_summary(task: MemoryWriteTask) -> None:
    """处理摘要任务: 生成对话摘要。"""
    # TODO: 接入 GroupSummarizer
    logger.debug("处理摘要生成: group=%s", task.group_id)


# ═══════════════════════════════════════════════════════════════
# §3 单例 + 初始化
# ═══════════════════════════════════════════════════════════════

_writer: AsyncMemoryWriter | None = None


def get_memory_writer() -> AsyncMemoryWriter:
    """获取异步记忆写入管理器单例。"""
    global _writer
    if _writer is None:
        _writer = AsyncMemoryWriter()
        # 注册默认处理器
        _writer.register_handler("memory_candidate", _handle_memory_candidate)
        _writer.register_handler("relationship_delta", _handle_relationship_delta)
        _writer.register_handler("event_extraction", _handle_event_extraction)
        _writer.register_handler("summary", _handle_summary)
    return _writer
