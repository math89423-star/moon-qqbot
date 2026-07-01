"""主动行为调度器 — 后台轮询循环 + 用户扫描 + 群冷场检测。

整合:
  - 私聊主动行为 (提取自 _scheduler_loop + _tick)
  - 群聊冷场破冰 (继承自原有 proactive_speaker.py)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config
    from .decision_gate import DecisionGate
    from .relationship import RelationshipManager

from .candidate_pool import CandidatePool, ProactiveCandidate

logger = logging.getLogger(__name__)


class ProactiveScheduler:
    """主动行为调度器。

    核心循环: 每 N 秒扫描用户列表, 对每个用户执行决策门控,
    通过者加入候选池, 从候选池选最佳执行。

    同时保留群聊冷场检测的原有能力。
    """

    def __init__(
        self,
        config: Config,
        relationships: RelationshipManager,
        decision_gate: DecisionGate,
        candidate_pool: CandidatePool,
        # 可选: 外部能力注册
        available_abilities: set[str] | None = None,
        # 群聊调度器 (从 suli_tavern 注入)
        group_scheduler: Any = None,
        # TavernClient (从 suli_tavern 注入, 用于 LLM 调用)
        tavern: Any = None,
    ) -> None:
        self._cfg = config
        self._rel = relationships
        self._gate = decision_gate
        self._pool = candidate_pool
        self._abilities = available_abilities or set()
        self._group_scheduler = group_scheduler
        self._tavern = tavern

        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

        # 群聊主动发言状态
        self._group_last_proactive_at: dict[int, float] = {}

        # 消息发送回调 (由 main.py 设置)
        self._send_callback: Any = None

    def set_send_callback(self, callback: Any) -> None:
        """设置主动消息生成/发送回调 (由 main.py 注入)。"""
        self._send_callback = callback

    def set_tavern(self, tavern: Any) -> None:
        """设置 TavernClient (由 suli_tavern 注入)。"""
        self._tavern = tavern

    # ── 生命周期 ─────────────────────────────────────

    async def start(self) -> None:
        """启动后台调度循环。"""
        if self._task and not self._task.done():
            logger.info("主动调度器已在运行, 跳过")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._scheduler_loop())
        logger.info(
            "主动调度器已启动 (间隔=%ds, 私聊=%s, 群聊=%s)",
            self._cfg.check_interval_seconds,
            "开" if self._cfg.private_proactive_enabled else "关",
            "开" if self._cfg.group_proactive_enabled else "关",
        )

    async def stop(self) -> None:
        """停止后台调度循环。"""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("主动调度器已停止")

    # ── 核心循环 ─────────────────────────────────────

    async def _scheduler_loop(self) -> None:
        """后台调度主循环。"""
        logger.info("主动调度循环开始")
        while not self._stop_event.is_set():
            try:
                # 动态超时: 查找下一次计划发送时间
                timeout = self._next_timeout()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=timeout,
                    )
                    break  # stop_event.set() 触发
                except TimeoutError:
                    pass  # 正常超时, 执行 tick

                await self._tick()

            except asyncio.CancelledError:
                logger.info("主动调度循环取消")
                break
            except Exception:
                logger.exception("主动调度循环异常")

    def _next_timeout(self) -> float:
        """计算下一次 tick 的等待时间。

        动态计算: 取所有用户的 next_proactive_at 最小值。
        若无用户, 回退到 check_interval_seconds。
        """
        base = self._cfg.check_interval_seconds
        now = time.time()
        nearest = base

        for user in self._rel.get_all_users().values():
            if user.next_proactive_at > now:
                wait = user.next_proactive_at - now
                nearest = min(nearest, wait)

        # 加入小量 jitter 避免 thundering herd
        jitter = random.uniform(0, min(3.0, nearest * 0.1))
        return max(1.0, min(nearest + jitter, base))

    async def _tick(self) -> None:
        """一次调度周期 — 扫描用户 + 群聊检测。"""
        # 1. 候选池维护
        self._pool.cleanup()

        # 2. 私聊主动行为
        if self._cfg.private_proactive_enabled:
            await self._tick_private()

        # 3. 群聊主动行为
        if self._cfg.group_proactive_enabled:
            await self._tick_group()

    # ── 私聊 Tick ────────────────────────────────────

    async def _tick_private(self) -> None:
        """扫描目标用户, 执行决策门控。"""
        target_ids = self._cfg.target_user_ids
        if not target_ids:
            return

        for user_id in target_ids:
            try:
                user = self._rel.get_user(user_id)
                now = time.time()

                # 检查是否到计划时间
                if user.next_proactive_at > now:
                    continue

                # 执行决策门控
                result = self._gate.evaluate(user_id)
                if not result.allowed:
                    continue

                # 加入候选池
                candidate = ProactiveCandidate(
                    reason=result.reason,
                    action=result.action,
                    motive=result.motive,
                    source="scheduler",
                    score=0.5,
                )
                if self._pool.offer(candidate):
                    # 安排下一次检查
                    user.next_proactive_at = now + self._cfg.check_interval_seconds * 2

            except Exception:
                logger.exception("私聊 tick 异常: user_id=%s", user_id)

        # 从候选池选择最佳执行
        await self._execute_best_candidate()

    async def _execute_best_candidate(self) -> None:
        """从候选池选择最高分候选并执行。"""
        if not self._send_callback:
            return

        candidate = self._pool.get_best()
        if candidate is None:
            return

        try:
            logger.info(
                "执行主动行为: reason=%s action=%s motive=%s",
                candidate.reason, candidate.action, candidate.motive,
            )
            # 委托给 main.py 的回调 (连接 LLM 生成 + 发送)
            await self._send_callback(candidate)
            self._pool.mark_sent(candidate)
        except Exception:
            logger.exception("主动行为执行失败")

    # ── 群聊 Tick ────────────────────────────────────

    async def _tick_group(self) -> None:
        """群聊冷场破冰 — 检测所有活跃群的静默状态。

        与原有的 proactive_speaker.py 逻辑对齐:
          1. 群静默超过 silence_threshold
          2. 冷却时间已过
          3. 随机 dice 通过
        """
        if self._group_scheduler is None:
            return

        try:
            active_groups = self._group_scheduler.get_active_contexts()
        except Exception:
            return

        for group_id, ctx in active_groups.items():
            if await self._should_speak_group(group_id, ctx):
                logger.info(
                    "群 %d: 主动破冰触发 (沉默=%.0fs)",
                    group_id, time.time() - ctx.last_active,
                )
                try:
                    _group_task = asyncio.create_task(  # noqa: RUF006
                        self._group_scheduler.evaluate_proactive(ctx),
                    )
                except Exception:
                    logger.exception("群聊主动发言异常: group_id=%d", group_id)

    async def _should_speak_group(self, group_id: int, ctx: Any) -> bool:
        """群聊主动发言四重门控。

        与原有 ProactiveChatScheduler._should_speak() 对齐,
        但约束来自 Config 而非硬编码。
        """
        cfg = self._cfg

        # 等级门控
        try:
            tier = self._group_scheduler.get_group_tier(group_id)
            if tier != "full":
                return False
        except Exception:
            pass

        # 门控 1: 静默时长
        silence = time.time() - ctx.last_active
        if silence < cfg.group_silence_threshold:
            return False

        # 门控 2: 冷却时间
        last = self._group_last_proactive_at.get(group_id, 0)
        if time.time() - last < cfg.group_proactive_cooldown:
            return False

        # 门控 3: 热度足够
        if getattr(ctx, "heat", 0) < 0.5:
            return False

        # 门控 4: 随机 dice
        if random.random() > cfg.group_proactive_chance:
            return False

        self._group_last_proactive_at[group_id] = time.time()
        return True
