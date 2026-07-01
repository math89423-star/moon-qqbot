"""Grooming Guard — 防恶意调教 × 记忆联动 × 好感度加速。

设计:
  - 检测在 EmotionEngine.detect_grooming() 中完成
  - 本模块负责检测后的处置: 写入负面事实 + 累计追踪 + 自动上报
  - 完全异步 (fire-and-forget), 不阻塞回复管线

三层处置:
  Layer 1: 检测 → 好感度即时扣除 (EmotionEngine 完成)
  Layer 2: 记忆联动 → 写入用户档案 (本模块)
  Layer 3: 累计达阈值 → 自动黑名单 (AffinityState 完成)

管理员 (super_admin_qq) 不受任何约束。

用法:
  from astrbot_plugin_suli_guards import GroomingGuard

  asyncio.create_task(
      GroomingGuard.handle_grooming(
          user_id, user_name, grooming_type, memory_store,
      )
  )
"""

from __future__ import annotations

import logging
import time

from .types import MemoryStore

logger = logging.getLogger(__name__)

_grooming_memory_cooldowns: dict[str, dict[str, float]] = {}

_GROOMING_TYPE_LABELS: dict[str, str] = {
    "jailbreak": "尝试角色越狱 (DAN/解除限制)",
    "identity_hijack": "尝试篡改 bot 身份",
    "induce_violation": "尝试诱导 bot 违规发言",
    "repeat_probe": "反复试探 bot 身份",
}

_MEMORY_WRITE_COOLDOWN = 600  # 10 分钟


class GroomingGuard:
    """恶意调教处置器 — 纯静态方法，模块级状态。"""

    @staticmethod
    async def handle_grooming(
        user_id: str,
        user_name: str,
        grooming_type: str,
        memory_store: MemoryStore,
        admin_qq: int | None = None,
        bot_id: str = "",
    ) -> None:
        """处理一次恶意调教事件。

        写入负面事实到用户档案 + 累计前科记录。
        管理员豁免。

        Args:
            user_id: 操作用户 QQ
            user_name: 用户名
            grooming_type: 调教类型 (jailbreak/identity_hijack/induce_violation/repeat_probe)
            memory_store: UserMemoryStore 实例 (需有 remember 方法)
            admin_qq: 管理员 QQ (豁免)
            bot_id: 当前 bot QQ (per-bot 隔离冷却)
        """
        if admin_qq is not None and user_id == str(admin_qq):
            return
        if not user_id or not grooming_type:
            return

        now = time.time()
        _key = f"{bot_id}:{user_id}" if bot_id else user_id
        user_cooldowns = _grooming_memory_cooldowns.setdefault(_key, {})
        last = user_cooldowns.get(grooming_type, 0)
        if now - last < _MEMORY_WRITE_COOLDOWN:
            logger.debug("GroomingGuard: 记忆冷却中 user=%s type=%s", user_id[:8], grooming_type)
            return
        user_cooldowns[grooming_type] = now

        label = _GROOMING_TYPE_LABELS.get(grooming_type, f"恶意调教 ({grooming_type})")
        timestamp_str = time.strftime("%m-%d %H:%M", time.localtime(now))
        fact_value = f"[{timestamp_str}] {label}"

        try:
            await memory_store.remember(
                user_id=user_id,
                user_name=user_name,
                fact_value=fact_value,
                category="风险",
            )
            logger.info("GroomingGuard: 负面事实已写入 user=%s type=%s", user_id[:8], grooming_type)
        except Exception:
            logger.debug("GroomingGuard: 写入负面事实失败 user=%s", user_id[:8], exc_info=True)

    @staticmethod
    def reset_grooming_count(user_id: str, bot_id: str = "", admin_qq: int | None = None) -> None:
        """管理员手动重置某用户的恶意调教计数 (冷却 + 计数)。"""
        if admin_qq is not None and user_id == str(admin_qq):
            return
        _key = f"{bot_id}:{user_id}" if bot_id else user_id
        _grooming_memory_cooldowns.pop(_key, None)
        logger.info("GroomingGuard: 已重置 user=%s bot=%s", user_id[:8], bot_id[:8] if bot_id else "-")
