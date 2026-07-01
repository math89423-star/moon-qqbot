"""群聊上下文 — GroupChatContext dataclass。
从 group_chat.py 拆分出的独立模块。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class GroupChatContext:
    """单个群聊的完整上下文状态。

    每个群独立一个实例，由 GroupChatScheduler 管理生命周期。
    """

    group_id: int
    messages: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    # ── 消息计数 (全局自增) ──
    message_counter: int = 0

    # ── 上次 bot 发言 ──
    last_bot_reply_at: float = 0.0  # 上次 bot 发言时间
    last_bot_reply_text: str = ""   # 上次 bot 发言内容
    last_reply_time: float = 0.0    # 别名, 部分代码使用
    last_reply_target: str = ""     # A2 fix: 上次 bot 回复的目标 user_id (per-user 热窗口)

    # ── 回复冷却 ──
    reply_cooldown_until: float = 0.0

    # ── 最近事件引用 ──
    last_bot = None    # Bot 实例
    last_event = None  # GroupMessageEvent 实例

    # ── P0: 上下文压缩摘要 ──
    summary: str = ""  # 早期消息的压缩摘要
    summary_timestamp: float = 0.0  # 摘要覆盖到的最新时间戳

    # ── P1: 热度状态机 ──
    heat: float = 0.0
    heat_updated_at: float = field(default_factory=time.time)

    # ── P1.5: 能量疲劳值 (from Heartflow) ──
    energy: float = 1.0           # 当前能量 0.1~1.0, 回复消耗/静默恢复
    energy_updated_at: float = field(default_factory=time.time)
    energy_last_reset_date: str = ""  # 隔日检测 (YYYY-MM-DD)
    total_messages_seen: int = 0  # 统计: 群内总消息数
    total_replies_sent: int = 0   # 统计: bot 总回复数

    # ── 领域检测 ──
    active_domains: dict[str, float] = field(default_factory=dict)
    last_domain_update: float = 0.0

    # ── 情感系统 ──
    emotion_updated: bool = False  # 本轮是否已更新情绪 (同一 trigger 只更新一次)

    # ── 对话线程追踪 ──
    # {user_id: {user_name, started_at, last_bot_reply_at, last_user_msg_at, exchange_count}}
    conversation_threads: dict[str, dict] = field(default_factory=dict)

    # ── 群聊总结追踪 ──
    message_count_since_last_summary: int = 0
    last_summary_at: float = 0.0

    # ── 上下文有效期 (秒) ──
    CONTEXT_TTL: float = 1800.0  # 30 分钟无活动视为过期

    @property
    def is_expired(self) -> bool:
        """是否过期 (最近活动超过 CONTEXT_TTL 秒)。"""
        return time.time() - self.last_active > self.CONTEXT_TTL

    @property
    def last_active(self) -> float:
        """最近活动时间戳 (last_reply_time 与最新消息时间戳取最大值)。"""
        latest = self.last_reply_time
        if self.messages:
            latest_msg = max(m.get("timestamp", 0) for m in self.messages)
            latest = max(latest, latest_msg)
        return latest

    def is_on_cooldown(self, cooldown_seconds: float) -> bool:
        """是否在回复冷却期内。"""
        if self.last_reply_time == 0.0:
            return False
        return time.time() - self.last_reply_time < cooldown_seconds

    @property
    def messages_since_last_reply(self) -> list[dict]:
        """上次 bot 回复之后到达的消息。"""
        if self.last_reply_time == 0.0:
            return list(self.messages)
        return [m for m in self.messages if m.get("timestamp", 0) > self.last_reply_time]

    def add_message(
        self, user_id: str, user_name: str, content: str
    ) -> None:
        """添加一条消息到上下文窗口。"""
        self.messages.append({
            "user_id": user_id,
            "user_name": user_name,
            "content": content,
            "timestamp": time.time(),
        })
        self.message_counter += 1
