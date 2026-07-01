"""主动发言调度器 — 群冷场时自主破冰。
从 group_chat.py 拆分出的独立模块。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from .group_context import GroupChatContext

logger = logging.getLogger(__name__)


class ProactiveChatScheduler:
    """主动对话调度器 — 基于沉默检测的后台轮询。

    每隔 60 秒扫描所有已启用群聊，检查是否满足主动发言条件:
      1. 群静默超过 proactive_silence_threshold (默认 15 分钟)
      2. 群热度足够 (不是死群，默认 heat > 1.0)
      3. 主动发言后冷却已过 (默认 30 分钟)
      4. 随机 dice (默认 30% 概率)

    满足全部条件 → 调用 GroupChatScheduler.evaluate_proactive()

    设计要点:
      - 不伪装 user 消息污染历史 — 直接注入 system prompt 引导 LLM
      - 随机因子避免机械定时感
      - 通过 GroupChatScheduler 的公开 API (get_active_contexts / evaluate_proactive) 交互
    """

    POLL_INTERVAL = 60  # 轮询间隔 (秒)

    def __init__(
        self,
        scheduler,  # GroupChatScheduler (duck-typed)
        config,     # Config
    ) -> None:
        self._scheduler = scheduler
        self._config = config
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动后台轮询 (幂等)。"""
        if self._task and not self._task.done():
            logger.info("主动对话调度器已在运行，跳过")
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "主动对话调度器已启动 "
            "(沉默阈值=%ds, 冷却=%ds, 概率=%.0f%%)",
            self._config.proactive_silence_threshold,
            self._config.proactive_cooldown,
            self._config.proactive_chance * 100,
        )

    async def stop(self) -> None:
        """停止后台轮询。"""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("主动对话调度器已停止")

    async def _poll_loop(self) -> None:
        """后台轮询主循环 — 每 60s 检查所有启用群。"""
        logger.info("主动对话轮询已开始 (间隔 %ds)", self.POLL_INTERVAL)
        while True:
            try:
                await asyncio.sleep(self.POLL_INTERVAL)
            except asyncio.CancelledError:
                logger.info("主动对话轮询已取消")
                break

            try:
                for group_id, ctx in self._scheduler.get_active_contexts().items():
                    if await self._should_speak(group_id, ctx):
                        logger.info(
                            "群 %d: 主动发言触发 (沉默=%.0fs, heat=%.1f)",
                            group_id,
                            time.time() - ctx.last_active,
                            ctx.heat,
                        )
                        # 不 await — 主动发言不阻塞轮询
                        asyncio.create_task(
                            self._scheduler.evaluate_proactive(ctx),
                        )
            except Exception:
                logger.exception("主动对话轮询异常")

    async def _should_speak(
        self, group_id: int, ctx: GroupChatContext,
    ) -> bool:
        """四重门控: 判断群聊是否应该主动发言。"""
        cfg = self._config

        # 总开关
        if not cfg.proactive_enabled:
            return False

        # 等级门控: 只有 full 群才参与主动发言
        if self._scheduler.get_group_tier(group_id) != "full":
            return False

        # 能量恢复: 静默期间 energy 持续恢复 (否则长时间 silence 后 energy 仍为旧值)
        if getattr(cfg, "energy_enabled", True):
            self._scheduler._update_energy(ctx, did_reply=False)

        # 门控 0: 能量过低 → 不主动 (累趴了)
        if getattr(cfg, "energy_enabled", True) and ctx.energy < 0.2:
            logger.debug("群 %d: 主动发言跳过 — 能量不足 (%.2f)", group_id, ctx.energy)
            return False

        # 门控 1: 群已沉默足够久
        silence = time.time() - ctx.last_active
        if silence < cfg.proactive_silence_threshold:
            return False

        # 门控 2: 热度足够 (之前聊得热闹，不是死群) — 能量调制
        _heat_gate = cfg.heat_active_threshold * 0.5
        if getattr(cfg, "energy_enabled", True) and ctx.energy > 0:
            _heat_gate /= ctx.energy  # 疲劳时门槛抬升
        if ctx.heat < _heat_gate:
            return False

        # 门控 3: 主动发言后有冷却
        if ctx.last_reply_time > 0:
            if time.time() - ctx.last_reply_time < cfg.proactive_cooldown:
                return False

        # 门控 4: 随机因子 (情绪调制)
        chance = cfg.proactive_chance
        if cfg.emotion_enabled:
            ignored = len(ctx.messages_since_last_reply)
            if ignored > 12:
                # 被冷落太久了 → 不主动贴上去
                logger.debug("群 %d: 主动发言跳过 — 被冷落 (%d条无人回应)", group_id, ignored)
                return False
            # 最近被频繁cue到 (<3条消息内有人理) → 更愿意主动
            if ignored < 3:
                chance *= 1.5
        if random.random() > chance:
            return False

        return True
