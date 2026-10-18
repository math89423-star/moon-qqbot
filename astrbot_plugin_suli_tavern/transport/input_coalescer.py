"""InputCoalescer — 累积窗口在 Actor 外（不冻结 Actor）。

D04 重写：事件合并语义只决定何时触发、哪些事件合成一轮，
全量消息真相由调用方落库，Coalescer 不拥有唯一副本。

- 同用户追发文本在窗口内合并，裸图吸收保留 source 但不重置计时
- 不同用户消息进入 per-key 排队队列（FIFO），禁止单槽覆盖
- 窗口与累计上限均从 scheduler._config 读取（actor_coalesce_window /
  actor_coalesce_max_wait），不再使用固定常量
- 达到 max_wait 必须触发，不能无限续窗
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field

from astrbot.api import logger

from .task_events import BatchEvent, MessageEvent

_BARE_IMAGE_RE = re.compile(
    r"^\[图片(?:×\d+)?(?:\s*URL:\s*\S+)?\]"
    r"(?:\s*\[图片(?:×\d+)?(?:\s*URL:\s*\S+)?\])*\s*$"
)

ACCUM_WINDOW = 3.0

# 直接互动短窗口（秒）— mention/reply/nickname/thread_continuation 专用。
# 被@/回复/叫昵称时用户要即时感，不必等满 3s；追发仍续窗合并，上限由 max_wait 兜底。
_DIRECT_WINDOW = 1.0
_DIRECT_TRIGGER_REASONS = frozenset({"mention", "reply", "nickname", "thread_continuation"})


@dataclass
class _CoalesceState:
    trigger_reason: str
    trigger_user_id: str
    trigger_user_name: str
    texts: list[str] = field(default_factory=list)
    source_events: list[MessageEvent] = field(default_factory=list)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    initial_event: MessageEvent | None = None
    started_at: float = field(default_factory=time.monotonic)


class InputCoalescer:
    """per-(bot,group) 累积器，产 BatchEvent 投 Actor。"""

    def __init__(self, actor_registry, scheduler) -> None:
        self._states: dict[str, _CoalesceState] = {}
        # D04: per-key FIFO 排队，不同用户消息不再单槽覆盖。
        # 兼容旧形状：若外部曾写入单个 dict，读取时按单元素队列处理。
        self._pending: dict[str, list[dict]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._actor_registry = actor_registry
        self._scheduler = scheduler
        self._trigger_timeout = 30.0  # 默认，启动后从 config 覆盖

    def _window_seconds(self) -> float:
        """D04: 合并窗口从配置读取，边界钳制。"""
        try:
            cfg = getattr(self._scheduler, "_config", None)
            v = float(getattr(cfg, "actor_coalesce_window", ACCUM_WINDOW))
        except (TypeError, ValueError):
            return ACCUM_WINDOW
        if v <= 0:
            return ACCUM_WINDOW
        return min(v, 10.0)

    def _max_wait_seconds(self) -> float:
        """D04: 累计等待上限从配置读取，默认 = 窗口 + 2s（≥1.5s），避免无限续窗。"""
        window = self._window_seconds()
        default = max(window + 2.0, 1.5)
        try:
            cfg = getattr(self._scheduler, "_config", None)
            raw = getattr(cfg, "actor_coalesce_max_wait", None)
            if raw is None:
                return default
            v = float(raw)
        except (TypeError, ValueError):
            return default
        if v <= 0:
            return default
        return min(v, 30.0)

    def set_trigger_timeout(self, seconds: float) -> None:
        self._trigger_timeout = float(seconds)

    def _key(self, bot_id: str, group_id: int) -> str:
        return f"{bot_id}:{group_id}"

    async def push(self, ev: MessageEvent) -> None:
        """推入一条原始 MessageEvent。"""
        k = self._key(ev.bot_id, int(ev.group_id))
        # 若已有活跃窗口
        state = self._states.get(k)
        if state is not None:
            # 同用户 → 合并
            if ev.user_id == state.trigger_user_id:
                txt = (ev.plaintext or "").strip()
                if _BARE_IMAGE_RE.match(txt):
                    # D04: 裸图吸收不重置计时，但必须保留 source/附件。
                    state.source_events.append(ev)
                    logger.info(
                        "Coalescer 裸图吸收(保留source) group=%s user=%s sources=%d",
                        ev.group_id, ev.user_id[:8], len(state.source_events),
                    )
                    return
                if not txt and not getattr(ev, "attachments", ()):
                    return
                if txt:
                    state.texts.append(txt)
                state.source_events.append(ev)
                state.event.set()
                logger.info(
                    "Coalescer 追加 group=%s +%d chars → %d chars",
                    ev.group_id, len(txt), sum(len(t) for t in state.texts),
                )
                return
            # D04: 不同用户 → FIFO 排队，禁止覆盖。
            self._pending.setdefault(k, []).append({
                "reason": ev.trigger_reason,
                "timestamp": time.time(),
                "event": ev,
            })
            logger.debug(
                "Coalescer 排队 group=%s user=%s reason=%s depth=%d",
                ev.group_id, ev.user_id[:8], ev.trigger_reason, len(self._pending[k]),
            )
            return

        # 首条：建窗口
        state = _CoalesceState(
            trigger_reason=ev.trigger_reason,
            trigger_user_id=ev.user_id,
            trigger_user_name=ev.user_name,
            texts=[(ev.plaintext or "").strip()] if (ev.plaintext or "").strip() else [],
            source_events=[ev],
            initial_event=ev,
        )
        self._states[k] = state
        # 启动窗口协程
        task = asyncio.create_task(self._window_loop(k))
        self._tasks[k] = task

    async def _window_loop(self, key: str) -> None:
        try:
            while True:
                state = self._states.get(key)
                if state is None:
                    break
                window = self._window_seconds()
                if state.trigger_reason in _DIRECT_TRIGGER_REASONS:
                    window = min(window, _DIRECT_WINDOW)
                max_wait = max(self._max_wait_seconds(), window)
                state.event.clear()
                # D04: 剩余预算 = min(窗口, 累计上限-已用)，到期必须触发。
                remaining = max_wait - (time.monotonic() - state.started_at)
                if remaining <= 0:
                    await self._emit_batch(key)
                    if not await self._open_next_pending(key):
                        break
                    continue
                try:
                    await asyncio.wait_for(state.event.wait(), timeout=min(window, remaining))
                    # 新文本到达 → 重置计时（累计上限在下一轮强制触发）
                    continue
                except TimeoutError:
                    pass
                # 窗口关闭 → 产 BatchEvent 投 Actor
                await self._emit_batch(key)
                # 检查 pending 队列
                if not await self._open_next_pending(key):
                    break
        finally:
            self._tasks.pop(key, None)
            # 清理：窗口结束后 _states 已在 _emit_batch 中 pop，无需再清

    def _pop_next_pending(self, key: str) -> dict | None:
        """弹出下一个未过期的排队事件，跳过过期项。"""
        queue = self._pending.get(key)
        if not queue:
            # 兼容旧形状：单个 dict（历史写入）按单元素队列处理
            legacy = self._pending.pop(key, None)
            if isinstance(legacy, dict) and "event" in legacy:
                age = time.time() - legacy.get("timestamp", 0)
                if age > self._trigger_timeout:
                    logger.info("Coalescer 丢弃过期 pending group=%s age=%.1fs", key, age)
                    return self._pop_next_pending(key)
                return legacy
            return None
        while queue:
            item = queue.pop(0)
            age = time.time() - item.get("timestamp", 0)
            if age > self._trigger_timeout:
                logger.info("Coalescer 丢弃过期 pending group=%s age=%.1fs", key, age)
                continue
            return item
        self._pending.pop(key, None)
        return None

    async def _open_next_pending(self, key: str) -> bool:
        """若有排队事件则开新窗口，返回是否继续循环。"""
        pending = self._pop_next_pending(key)
        if pending is None:
            return False
        pev: MessageEvent = pending["event"]
        new_state = _CoalesceState(
            trigger_reason=pending["reason"] or pev.trigger_reason,
            trigger_user_id=pev.user_id,
            trigger_user_name=pev.user_name,
            texts=[(pev.plaintext or "").strip()] if (pev.plaintext or "").strip() else [],
            source_events=[pev],
            initial_event=pev,
        )
        self._states[key] = new_state
        return True

    async def _emit_batch(self, key: str) -> None:
        state = self._states.pop(key, None)
        if state is None:
            return
        merged = " ".join(t for t in state.texts if t)
        # 取 ctx 与 actor
        try:
            bot_id, group_id_str = key.split(":", 1)
            group_id = int(group_id_str)
            ctx = self._scheduler._contexts.get(self._scheduler._make_ctx_key(bot_id, group_id))
            if ctx is None:
                from .group_context import GroupChatContext
                ctx = GroupChatContext(group_id=group_id)
                # P0-1 修复：不在 Coalescer 直接写 scheduler._contexts（违 SingleWriter），
                # 仅构造 ctx 并交由 ActorRegistry.get_or_create 持有；Actor 为唯一写者
                # 临时保留键以便 Actor 找到 ctx，待 P0 验收后由 Actor 完全持有
                self._scheduler._contexts[self._scheduler._make_ctx_key(bot_id, group_id)] = ctx
            # 对话线程 DB 恢复 (重启不失忆，一次性)
            try:
                self._scheduler._restore_ctx_threads(ctx, bot_id, group_id)
            except Exception:
                logger.debug("对话线程恢复钩子失败 key=%s", key, exc_info=True)
            actor = self._actor_registry.get_or_create(bot_id, group_id, ctx, self._scheduler)
            # 单调 seq：用 actor 内部 _next_seq 保证有序，避免硬编码 0
            batch = BatchEvent(
                event_id=f"batch-{uuid.uuid4().hex[:8]}",
                seq=actor._next_seq(),
                bot_id=bot_id,
                group_id=str(group_id),
                merged_text=merged,
                source_events=tuple(state.source_events),
                trigger_reason=state.trigger_reason,
                trigger_user_id=state.trigger_user_id,
                trigger_user_name=state.trigger_user_name,
            )
            await actor.enqueue_batch(batch)
            logger.info("Coalescer 发 Batch group=%s merged=%d chars reason=%s", group_id, len(merged), state.trigger_reason)
        except Exception:
            logger.exception("Coalescer emit 失败 key=%s", key)
