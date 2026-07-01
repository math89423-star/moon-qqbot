"""关系状态机 — 角色系统 + 情感门控。

从 core_store.py + proactive_engine.py 提取的 persona-agnostic 模式。
零硬编码角色文本 — 所有标签/阈值来自 Config。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config, RoleConstraints

logger = logging.getLogger(__name__)


class Role(str, Enum):  # noqa: UP042
    """用户角色 — 决定主动行为约束等级。"""
    OWNER = "owner"
    FRIEND = "friend"


class RelationshipMode(str, Enum):  # noqa: UP042
    """关系状态机模式。"""
    NORMAL = "normal"          # 正常
    BACKOFF = "backoff"        # 后退 (用户表达了不适)
    HURT = "hurt"              # 受伤 (用户说了伤人的话)
    REFUSING = "refusing"      # 拒绝 (用户明确拒绝互动)
    ATTACHED = "attached"      # 依恋 (正向)
    CAREFUL = "careful"        # 小心翼翼 (刚和好)


@dataclass
class RelationshipState:
    """单个用户的关系状态。"""
    mode: RelationshipMode = RelationshipMode.NORMAL
    backoff_until: float = 0.0
    hurt_until: float = 0.0
    refusing_until: float = 0.0
    attached_until: float = 0.0
    careful_until: float = 0.0
    updated_at: float = field(default_factory=time.time)


@dataclass
class UserProactiveState:
    """单个用户的主动行为运行时状态。"""
    user_id: str
    role: Role = Role.FRIEND

    # 日程
    next_proactive_at: float = 0.0
    last_proactive_at: float = 0.0
    last_user_active_at: float = 0.0

    # 计数
    sent_today: int = 0
    last_sent_date: str = ""  # "YYYY-MM-DD"
    photo_sent_today: int = 0
    screen_peek_today: int = 0
    poke_sent_today: int = 0

    # 休息
    rest_silence_until: float = 0.0

    # 计划
    planned_reason: str = ""
    planned_action: str = ""
    planned_motive: str = ""
    planned_topic: str = ""

    # 去重
    recent_topics: list[str] = field(default_factory=list)
    ignored_streak: int = 0

    # 关系状态机
    relationship: RelationshipState = field(default_factory=RelationshipState)

    # 标志
    enabled: bool = True
    manual_disabled: bool = False
    proactive_sending: bool = False
    has_active_session: bool = False


class RelationshipManager:
    """关系管理 — 角色判定 + 状态机转换。

    所有角色标签/阈值来自 Config, 零硬编码 persona。
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        # ★ ADR-001 进程隔离: 双 Bot 各自独立容器，此 dict 天然 per-bot 安全。
        #   如需单进程多 bot → key 改为 f"{bot_id}:{user_id}"。
        self._users: dict[str, UserProactiveState] = {}

    # ── 角色判定 ──────────────────────────────────────

    def get_user(self, user_id: str) -> UserProactiveState:
        """获取或创建用户状态。"""
        if user_id not in self._users:
            role = (
                Role.OWNER
                if user_id in self._config.target_user_ids
                else Role.FRIEND
            )
            self._users[user_id] = UserProactiveState(user_id=user_id, role=role)
        return self._users[user_id]

    def get_role(self, user_id: str) -> Role:
        return self.get_user(user_id).role

    def get_role_constraints(self, user_id: str) -> RoleConstraints:
        role = self.get_role(user_id)
        if role == Role.OWNER:
            return self._config.owner_constraints
        return self._config.friend_constraints

    def get_role_label(self, user_id: str) -> str:
        role = self.get_role(user_id)
        if role == Role.OWNER:
            return "主人"
        return "朋友"

    # ── 关系状态机 ────────────────────────────────────

    def get_relationship_mode(self, user_id: str) -> RelationshipMode:
        if not self._config.enable_relationship_state:
            return RelationshipMode.NORMAL
        state = self.get_user(user_id).relationship
        now = time.time()
        # 检查过期
        if state.mode == RelationshipMode.BACKOFF and now > state.backoff_until:
            state.mode = RelationshipMode.CAREFUL
            state.careful_until = now + 3600  # 小心翼翼 1 小时
        elif state.mode == RelationshipMode.HURT and now > state.hurt_until:
            state.mode = RelationshipMode.CAREFUL
            state.careful_until = now + 3600
        elif _state_is_normalizing(state, now):
            state.mode = RelationshipMode.NORMAL
        return state.mode

    def is_blocked_by_relationship(self, user_id: str) -> bool:
        """关系状态是否阻塞主动行为。"""
        mode = self.get_relationship_mode(user_id)
        return mode in (
            RelationshipMode.BACKOFF,
            RelationshipMode.HURT,
            RelationshipMode.REFUSING,
        )

    def set_relationship_mode(
        self, user_id: str, mode: RelationshipMode, duration_seconds: float,
    ) -> None:
        user = self.get_user(user_id)
        user.relationship.mode = mode
        user.relationship.updated_at = time.time()
        until = time.time() + duration_seconds
        if mode == RelationshipMode.BACKOFF:
            user.relationship.backoff_until = until
        elif mode == RelationshipMode.HURT:
            user.relationship.hurt_until = until
        elif mode == RelationshipMode.REFUSING:
            user.relationship.refusing_until = until
        elif mode == RelationshipMode.ATTACHED:
            user.relationship.attached_until = until
        elif mode == RelationshipMode.CAREFUL:
            user.relationship.careful_until = until
        logger.info(
            "用户 %s 关系状态变更: %s (持续 %.0fs)",
            user_id, mode.value, duration_seconds,
        )

    # ── 空闲/间隔计算 ──────────────────────────────────

    def idle_minutes(self, user_id: str) -> float:
        """用户空闲时间 (分钟) — 自上次活跃。"""
        user = self.get_user(user_id)
        if user.last_user_active_at <= 0:
            return float("inf")
        return (time.time() - user.last_user_active_at) / 60.0

    def interval_minutes(self, user_id: str) -> float:
        """距离上次主动消息的时间 (分钟)。"""
        user = self.get_user(user_id)
        if user.last_proactive_at <= 0:
            return float("inf")
        return (time.time() - user.last_proactive_at) / 60.0

    def mark_user_active(self, user_id: str) -> None:
        user = self.get_user(user_id)
        user.last_user_active_at = time.time()

    def mark_proactive_sent(self, user_id: str) -> None:
        user = self.get_user(user_id)
        now = time.time()
        user.last_proactive_at = now
        user.sent_today += 1
        user.proactive_sending = False

    # ── 日常计数重置 ──────────────────────────────────

    def reset_daily_counter_if_needed(self, user_id: str) -> bool:
        """返回 True 表示刚重置。"""
        user = self.get_user(user_id)
        today = time.strftime("%Y-%m-%d")
        if user.last_sent_date != today:
            user.sent_today = 0
            user.photo_sent_today = 0
            user.screen_peek_today = 0
            user.poke_sent_today = 0
            user.last_sent_date = today
            return True
        return False

    # ── 用户休息 ──

    def set_user_rest(self, user_id: str) -> None:
        user = self.get_user(user_id)
        hours = self._config.user_rest_silence_hours
        user.rest_silence_until = time.time() + hours * 3600
        logger.info(
            "用户 %s 已设置休息静默, 持续 %dh", user_id, hours,
        )

    def is_user_resting(self, user_id: str) -> bool:
        user = self.get_user(user_id)
        if user.rest_silence_until <= 0:
            return False
        if time.time() > user.rest_silence_until:
            user.rest_silence_until = 0.0
            return False
        return True

    # ── 用户启用检查 ──

    def is_user_enabled(self, user_id: str) -> bool:
        user = self.get_user(user_id)
        return not user.manual_disabled and user.enabled

    def get_all_users(self) -> dict[str, UserProactiveState]:
        return self._users


def _state_is_normalizing(state: RelationshipState, now: float) -> bool:
    """检查关系状态是否应该恢复到 NORMAL。"""
    return (
        (state.mode == RelationshipMode.REFUSING and now > state.refusing_until)
        or (state.mode == RelationshipMode.CAREFUL and now > state.careful_until)
        or (state.mode == RelationshipMode.ATTACHED and now > state.attached_until)
    )
