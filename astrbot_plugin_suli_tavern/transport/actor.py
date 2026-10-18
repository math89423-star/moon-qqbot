"""GroupActor — per-(bot,group) 单线程社会状态串行化。

P0-1 地基：Single Writer Principle
- ctx.messages 仅 Actor 有写权，外部 on_message 禁止 ctx.add_message()
- 所有投递为 Immutable BatchEvent / TaskResultEvent（task_events.py）
- 3s InputCoalescer 在 Actor 外，Actor 不 sleep
- 队列为 event-class aware：TaskResult/Cancel 优先级 0，BatchEvent 优先级 1

D05：Batch（含 Gate/VLM/Main 网络等待）在后台任务串行执行，
控制事件（Cancel/TaskResult）在主循环立即处理，不被慢 batch 队头阻塞。
Batch 之间仍串行（前一个完成后才跑下一个），保证 ctx 单写者语义。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from .group_context import GroupChatContext
from .task_events import BatchEvent, CancelEvent, ImageDescriptionReadyEvent, TaskResultEvent

# 优先级：越小越优先
_PRIO_TASK = 0
_PRIO_CONTROL = 0
_PRIO_BATCH = 1
_PRIO_IMAGE = 1


@dataclass(order=True)
class _QueuedItem:
    priority: int
    seq: int
    event: Any = field(compare=False)


class GroupActor:
    """per-(bot_id, group_id) 社会 Actor，单线程串行人格状态。"""

    def __init__(
        self,
        bot_id: str,
        group_id: int,
        ctx: GroupChatContext,
        scheduler: Any,  # GroupChatScheduler 引用（避免循环导入）
    ) -> None:
        self.bot_id = bot_id
        self.group_id = group_id
        self.ctx = ctx
        self._scheduler = scheduler
        self._queue: asyncio.PriorityQueue[_QueuedItem] = asyncio.PriorityQueue()
        self._seq_counter = 0
        self._task: asyncio.Task | None = None
        self._running = False
        # D05: 后台 batch 链（串行），主循环永不 await 网络等待。
        self._batch_task: asyncio.Task | None = None
        self._batch_version = 0
        # 背压：Normal Conversation 可 coalesce/drop，TaskLifecycle 永不丢
        self._maxsize_normal = 100

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name=f"actor-{self.bot_id[-4:]}:{self.group_id}")

    async def stop(self) -> None:
        self._running = False
        if self._batch_task and not self._batch_task.done():
            self._batch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await self._batch_task
        self._batch_task = None
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ── 入队（外部调用，线程安全） ──

    def _next_seq(self) -> int:
        self._seq_counter += 1
        return self._seq_counter

    async def enqueue_batch(self, event: BatchEvent) -> None:
        # 背压：仅对 Batch/Image 类做有界丢弃，TaskResult 永不丢
        if self._queue.qsize() >= self._maxsize_normal:
            # 丢最早的 Batch（非 Task），保留 TaskResult
            # PriorityQueue 不支持 peek，退化为：若队满且全是 Batch，丢当前新事件并告警
            # P0 简化：直接丢新 Batch 并记录（最老可能是 task 请求，已在 infra 侧要求永不丢 old task）
            logger.warning("Actor %s:%s 队列满(%d) 丢弃新 BatchEvent %s", self.bot_id[-4:], self.group_id, self._queue.qsize(), event.event_id)
            return
        await self._queue.put(_QueuedItem(priority=_PRIO_BATCH, seq=self._next_seq(), event=event))

    async def enqueue_task_result(self, event: TaskResultEvent) -> None:
        await self._queue.put(_QueuedItem(priority=_PRIO_TASK, seq=self._next_seq(), event=event))

    async def enqueue_cancel(self, event: CancelEvent) -> None:
        await self._queue.put(_QueuedItem(priority=_PRIO_CONTROL, seq=self._next_seq(), event=event))

    async def enqueue_image_ready(self, event: ImageDescriptionReadyEvent) -> None:
        await self._queue.put(_QueuedItem(priority=_PRIO_IMAGE, seq=self._next_seq(), event=event))

    # ── 主循环 ──

    async def _run(self) -> None:
        logger.info("GroupActor 启动 bot=%s group=%s", self.bot_id[-4:], self.group_id)
        while self._running:
            try:
                item: _QueuedItem = await self._queue.get()
            except asyncio.CancelledError:
                break
            ev = item.event
            try:
                if isinstance(ev, BatchEvent):
                    # D05: 只创建后台任务、不等待 —— 控制事件可立即进入状态机。
                    self._launch_serial(ev, self._handle_batch, kind="batch")
                elif isinstance(ev, TaskResultEvent):
                    # 二次人格化含 LLM 等待，同样走后台串行链；
                    # Cancel 保持 inline 以保证即时响应。
                    self._launch_serial(ev, self._handle_task_result, kind="result")
                elif isinstance(ev, CancelEvent):
                    await self._handle_cancel(ev)
                elif isinstance(ev, ImageDescriptionReadyEvent):
                    await self._handle_image_ready(ev)
                else:
                    logger.warning("Actor 未知事件类型 %s", type(ev))
            except asyncio.CancelledError:
                raise
            except BaseException:
                logger.exception("Actor 处理异常 bot=%s group=%s event=%s", self.bot_id[-4:], self.group_id, getattr(ev, "event_id", "?"))
            finally:
                self._queue.task_done()

    # ── 处理（P0-1 阶段：委托给 scheduler 现有管线，P0-2 后替换为 Gate→MainDecision） ──

    def _launch_serial(self, ev: Any, handler: Any, kind: str = "batch") -> None:
        """D05: ctx 变更操作投后台串行链，主循环立即返回取下一个事件。

        - Batch / TaskResult 之间串行（await 前一个）→ ctx 单写者语义不变。
        - Cancel 走主循环 inline，不排在慢操作后面。
        """
        self._batch_version += 1
        version = self._batch_version
        prev = self._batch_task if (self._batch_task and not self._batch_task.done()) else None
        if prev is not None:
            logger.info("Actor %s 排队 group=%s version=%d (前序仍在执行)", kind, self.group_id, version)

        async def _runner() -> None:
            if prev is not None:
                with contextlib.suppress(BaseException):
                    await prev
            await handler(ev)

        task = asyncio.create_task(_runner(), name=f"actor-{kind}-{self.bot_id[-4:]}:{self.group_id}:v{version}")

        def _done_cb(t: asyncio.Task) -> None:
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                return
            except BaseException:
                return
            if exc is not None:
                logger.exception("Actor 后台 %s 异常 group=%s version=%d", kind, self.group_id, version)

        task.add_done_callback(_done_cb)
        self._batch_task = task

    async def _handle_batch(self, ev: BatchEvent) -> None:
        # Single Writer：消息落库由 _handle_actor_batch 统一负责，此处仅委托
        # 避免双写 ctx.add_message / last_event（审计 P0-1 double-write）
        try:
            await self._scheduler._handle_actor_batch(self.ctx, ev)
        except BaseException:
            logger.exception("Actor _handle_batch 失败 group=%s", self.group_id)

    async def _handle_task_result(self, ev: TaskResultEvent) -> None:
        # P0-3 实现：二次人格化。P0-1 仅日志 + 入 ctx 消息
        logger.info("Actor 收到 TaskResult task=%s status=%s group=%s", ev.task_id, ev.status, self.group_id)
        # 过渡：直接委托 scheduler 的 task_result 二次合成（P0-3 后实现）
        try:
            await self._scheduler._handle_actor_task_result(self.ctx, ev)
        except AttributeError:
            # 未实现时仅落日志
            pass

    async def _handle_cancel(self, ev: CancelEvent) -> None:
        logger.info("Actor 收到 Cancel task=%s group=%s", ev.task_id, self.group_id)
        try:
            await self._scheduler._handle_actor_cancel(self.ctx, ev)
        except AttributeError:
            pass

    async def _handle_image_ready(self, ev: ImageDescriptionReadyEvent) -> None:
        # VLM 描述就绪：不再直接改 history object，而是经 Actor 追加描述
        # P0-1 过渡：找到 origin_message_id 对应消息，追加描述文本
        try:
            await self._scheduler._handle_actor_image_ready(self.ctx, ev)
        except AttributeError:
            pass


class ActorRegistry:
    """per-(bot,group) Actor 注册表。"""

    def __init__(self) -> None:
        self._actors: dict[str, GroupActor] = {}
        self._seq = 0

    def _key(self, bot_id: str, group_id: int) -> str:
        return f"{bot_id}:{group_id}"

    def get_or_create(self, bot_id: str, group_id: int, ctx: GroupChatContext, scheduler: Any) -> GroupActor:
        k = self._key(bot_id, group_id)
        actor = self._actors.get(k)
        if actor is None:
            actor = GroupActor(bot_id, group_id, ctx, scheduler)
            self._actors[k] = actor
            actor.start()
        return actor

    def get(self, bot_id: str, group_id: int) -> GroupActor | None:
        return self._actors.get(self._key(bot_id, group_id))

    async def stop_all(self) -> None:
        for a in list(self._actors.values()):
            await a.stop()
        self._actors.clear()
