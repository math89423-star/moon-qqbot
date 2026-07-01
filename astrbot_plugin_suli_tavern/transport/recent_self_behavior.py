"""近期自我行为记忆 — 机器人最近的发言/行为滑动窗口。

设计目标:
  - 极轻量: 纯内存 dict[deque]，零文件 I/O，零 DB 写入
  - 短窗口: 默认 30s TTL，只保留"刚才发生了什么"
  - Per-bot, per-group 隔离: 复合键 "bot_id:group_id"
  - 不替代 UserMemoryStore/BotExperienceStore — 这是即时行为层，
    不是长期记忆层

用途:
  1. 触发合并: "我刚回过这个话题 → 跳过新触发"
  2. Gate 感知: "我 15 秒前刚发了言 → 降低本次 urgency"
  3. 语气变化: "上一条是 serious → 这条可以轻松一点"

用法:
  from .recent_self_behavior import RecentSelfBehaviorStore, RecentReply

  store = RecentSelfBehaviorStore(ttl_seconds=30)
  store.record(bot_id, group_id, text="来了～", target_user_id="123", ...)
  recent = store.get_recent(bot_id, group_id)  # → list[RecentReply]
  if store.recently_addressed(bot_id, group_id, user_id="123"):
      ...  # skip
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from collections import deque

logger = logging.getLogger(__name__)

# ── 默认 TTL ──────────────────────────────────────────

_DEFAULT_TTL = 30.0  # 秒
_MAX_ENTRIES = 8     # 每个 key 最多保留条数


@dataclass
class RecentReply:
    """bot 最近一条发言的轻量记录。"""

    timestamp: float
    text_preview: str = ""          # 前 80 字，供日志/调试
    target_user_id: str = ""        # 回复目标
    stance: str = ""                # Gate reply_stance
    intent_type: str = ""           # Gate intent_type
    domain: str = ""                # Gate domain
    trigger_reason: str = ""        # mention/batch/debounce/etc
    reply_length: int = 0           # 回复字数


class RecentSelfBehaviorStore:
    """bot 近期自我行为滑动窗口。

    极轻量: 纯内存。重启丢失是预期行为——这只是"刚才"的记忆。
    """

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL):
        self._ttl = ttl_seconds
        self._store: dict[str, deque[RecentReply]] = {}

    # ── 公共 API ──────────────────────────────────────

    def record(
        self,
        bot_id: str,
        group_id: str,
        *,
        text: str = "",
        target_user_id: str = "",
        stance: str = "",
        intent_type: str = "",
        domain: str = "",
        trigger_reason: str = "",
    ) -> None:
        """记录一条 bot 发言。

        应在回复成功发送后调用 (无论发送结果如何，bot 确实说了)。
        """
        key = _key(bot_id, group_id)
        self._gc(key)  # 写入前清理过期条目

        entry = RecentReply(
            timestamp=time.time(),
            text_preview=(text[:80] if text else ""),
            target_user_id=target_user_id,
            stance=stance,
            intent_type=intent_type,
            domain=domain,
            trigger_reason=trigger_reason,
            reply_length=len(text) if text else 0,
        )

        if key not in self._store:
            self._store[key] = deque(maxlen=_MAX_ENTRIES)
        self._store[key].append(entry)

        logger.debug(
            "RecentSelfBehavior: record bot=%s group=%s stance=%s target=%s len=%d",
            bot_id[:8], group_id, stance, target_user_id[:8] if target_user_id else "-",
            entry.reply_length,
        )

    def get_recent(
        self, bot_id: str, group_id: str, max_age_seconds: float | None = None,
    ) -> list[RecentReply]:
        """获取指定 bot+群 的近期发言。

        Args:
            max_age_seconds: 最大年龄，默认使用初始化时的 ttl
        """
        key = _key(bot_id, group_id)
        self._gc(key)
        entries = list(self._store.get(key, deque()))
        if not entries:
            return []

        cutoff = time.time() - (max_age_seconds if max_age_seconds is not None else self._ttl)
        return [e for e in entries if e.timestamp >= cutoff]

    def recently_addressed(
        self,
        bot_id: str,
        group_id: str,
        user_id: str = "",
        *,
        max_age_seconds: float | None = None,
    ) -> RecentReply | None:
        """检查 bot 最近是否回复过指定用户。

        Returns:
            最近的匹配回复条目，或 None
        """
        if not user_id:
            return None
        recent = self.get_recent(bot_id, group_id, max_age_seconds=max_age_seconds)
        for entry in reversed(recent):  # 最新优先
            if entry.target_user_id == user_id:
                return entry
        return None

    def last_reply_age(self, bot_id: str, group_id: str) -> float | None:
        """bot 上次发言距今多少秒。没发言过返回 None。"""
        recent = self.get_recent(bot_id, group_id)
        if not recent:
            return None
        return time.time() - recent[-1].timestamp

    def most_recent(self, bot_id: str, group_id: str) -> RecentReply | None:
        """bot 最近一次发言。"""
        recent = self.get_recent(bot_id, group_id)
        return recent[-1] if recent else None

    # ── 内部 ──────────────────────────────────────────

    def _gc(self, key: str) -> None:
        """清理 key 下的过期条目。"""
        entries = self._store.get(key)
        if not entries:
            return
        cutoff = time.time() - self._ttl
        # 从左侧弹出过期条目 (deque 左侧最旧)
        while entries and entries[0].timestamp < cutoff:
            entries.popleft()
        if not entries:
            del self._store[key]


def _key(bot_id: str, group_id: str | int) -> str:
    """构建复合键。"""
    return f"{bot_id}:{group_id}"


# ── 模块级单例 ────────────────────────────────────

_store: RecentSelfBehaviorStore | None = None


def get_store(ttl_seconds: float = _DEFAULT_TTL) -> RecentSelfBehaviorStore:
    """获取或创建模块级单例。

    首次调用时创建，后续调用返回同一实例。
    """
    global _store
    if _store is None:
        _store = RecentSelfBehaviorStore(ttl_seconds=ttl_seconds)
        logger.info("RecentSelfBehaviorStore 初始化: ttl=%.0fs", ttl_seconds)
    return _store
