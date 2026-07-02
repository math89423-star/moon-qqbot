"""决策门控 — 决定"现在应该主动发消息吗？"

从社区插件 proactive_engine.py 的 _should_send() ~30 检查中提取 10 个核心检查。
全部参数化, 零 persona 硬编码。

检查链 (按顺序, 短路返回):
  1. 引擎总开关
  2. 用户手动禁用 / 休息静默
  3. 免打扰时段
  4. 会话占用 / 正在发送中
  5. 关系状态阻塞
  6. 每日配额耗尽
  7. 用户空闲不足
  8. 主动间隔不足
  9. 话题去重
  10. 时段容量封顶
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

from .reason_action import ReasonActionEngine, _is_quiet_time
from .relationship import RelationshipManager, Role, UserProactiveState

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """门控检查结果。"""
    allowed: bool
    reason: str = ""        # 通过时的原因 key / 阻止时的解释
    action: str = "message"  # 推荐动作
    motive: str = ""        # 动机描述
    blocked: bool = False
    block_reason: str = ""


class DecisionGate:
    """主动行为决策门控。

    10 个顺序检查, 每个检查给出明确的通过/阻止原因。
    设计为纯函数: 输入状态, 输出 GateResult。
    """

    def __init__(
        self, config: Config, relationships: RelationshipManager,
        reason_engine: ReasonActionEngine,
    ) -> None:
        self._cfg = config
        self._rel = relationships
        self._re = reason_engine

    def evaluate(self, user_id: str) -> GateResult:
        """执行完整的决策检查链。

        Returns:
            GateResult — allowed=True 表示可以发送主动消息。
        """
        # 0: 总开关
        if not self._cfg.enabled:
            return self._block("引擎总开关已关闭")

        user = self._rel.get_user(user_id)
        is_owner = user.role == Role.OWNER

        # 1: 用户启用 / 休息静默
        if not self._rel.is_user_enabled(user_id):
            return self._block("用户手动禁用")
        if self._rel.is_user_resting(user_id):
            return self._block("用户休息静默中")

        # 2: 免打扰时段
        if _is_quiet_time(self._cfg):
            return self._block("免打扰时段")

        # 3: 会话占用 / 正在发送
        if user.proactive_sending:
            return self._block("正在发送中")

        # 4: 关系状态阻塞
        if self._rel.is_blocked_by_relationship(user_id):
            mode = self._rel.get_relationship_mode(user_id)
            return self._block(f"关系状态阻塞: {mode.value}")

        # 5: 每日配额
        constraints = self._rel.get_role_constraints(user_id)
        self._rel.reset_daily_counter_if_needed(user_id)
        if user.sent_today >= constraints.daily_limit:
            return self._block(f"每日配额已满 ({user.sent_today}/{constraints.daily_limit})")

        # 6: 用户空闲
        idle = self._rel.idle_minutes(user_id)
        if idle < constraints.idle_minutes:
            return self._block(
                f"用户空闲不足 ({idle:.0f}m < {constraints.idle_minutes}m)",
            )

        # 7: 主动间隔
        interval = self._rel.interval_minutes(user_id)
        if interval < constraints.interval_minutes:
            return self._block(
                f"主动间隔不足 ({interval:.0f}m < {constraints.interval_minutes}m)",
            )

        # 8: 话题去重 — 检查最近话题是否重复
        if self._is_topic_repeating(user_id):
            return self._block("话题重复")

        # 9: 选择原因 + 动作
        reason = self._re.choose_reason(is_owner)
        if reason is None:
            return self._block("无可用原因")

        action = self._re.choose_action(is_owner)
        if action is None:
            return self._block("无可用动作")

        # 10: 动作配额 (如生图每日限制)
        if not self._check_action_quota(user, action.key):
            return self._block(f"动作配额不足: {action.key}")

        return GateResult(
            allowed=True,
            reason=reason.key,
            action=action.key,
            motive=reason.label,
        )

    # ── 检查实现 ──────────────────────────────────────

    def _is_topic_repeating(self, user_id: str) -> bool:
        user = self._rel.get_user(user_id)
        if not user.recent_topics:
            return False
        # 如果最近 N 个话题中有完全相同 → 重复
        window = self._cfg.recent_topic_window
        recent = user.recent_topics[-window:]
        # 过于简单的检查: 如果最近 3 个话题相同 → 阻止
        if len(recent) >= 3 and len(set(recent)) == 1:
            logger.debug("用户 %s: 话题重复 — %s", user_id, recent[-1])
            return True
        return False

    def _check_action_quota(self, user: UserProactiveState, action_key: str) -> bool:
        constraints = self._rel.get_role_constraints(user.user_id)
        if action_key == "photo":
            return user.photo_sent_today < constraints.photo_daily_limit
        if action_key == "poke":
            return user.poke_sent_today < constraints.poke_daily_limit
        if action_key == "screen_peek":
            return user.screen_peek_today < constraints.screen_peek_daily_limit
        return True  # message/sticker 无限制

    # ── 工具 ──────────────────────────────────────────

    @staticmethod
    def _block(reason: str) -> GateResult:
        return GateResult(allowed=False, blocked=True, block_reason=reason)

    @staticmethod
    def _now_ts() -> float:
        return time.time()
