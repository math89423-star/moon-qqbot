"""主动行为原因/动作框架 — 时段窗口 + 权重选择 + 亲密度过滤。

从社区插件 constants.py + proactive_engine.py 提取, zero persona hardcoding。
"""

from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config, ProactiveAction, ProactiveReason

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════
# 时段判定
# ═════════════════════════════════════════════════════════════════

def _current_daypart() -> str:
    """返回当前时段名称。"""
    hour = time.localtime().tm_hour
    if 5 <= hour < 9:
        return "morning"
    if 9 <= hour < 12:
        return "noon"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def _is_quiet_time(config: Config) -> bool:
    """检查当前是否在免打扰时段。"""
    now = time.localtime()
    current_minutes = now.tm_hour * 60 + now.tm_min

    def _parse_hhmm(s: str) -> int:
        try:
            h, m = s.strip().split(":")
            return int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            return -1

    start = _parse_hhmm(config.quiet_hours_start)
    end = _parse_hhmm(config.quiet_hours_end)
    if start < 0 or end < 0:
        return False
    if start < end:
        return start <= current_minutes < end
    # 跨午夜 (如 22:00 - 08:00)
    return current_minutes >= start or current_minutes < end


# ═════════════════════════════════════════════════════════════════
# 原因选择
# ═════════════════════════════════════════════════════════════════

class ReasonActionEngine:
    """原因/动作选择引擎。

    所有原因和动作定义来自 Config, 零硬编码。
    """

    def __init__(self, config: Config) -> None:
        self._config = config

    # ── 原因 ──────────────────────────────────────────

    def get_available_reasons(self, is_owner: bool) -> list[ProactiveReason]:  # noqa: FBT001
        """获取当前可用的原因列表 (过滤时段 + 亲密关系)。"""
        daypart = _current_daypart()
        reasons: list[ProactiveReason] = []
        for r in self._config.proactive_reasons:
            # 时段过滤
            if r.daypart not in {"any", daypart}:
                continue
            # 亲密原因仅主人可见
            if r.intimate and not is_owner:
                continue
            reasons.append(r)
        return reasons

    def choose_reason(self, is_owner: bool) -> ProactiveReason | None:  # noqa: FBT001
        """按权重随机选择一个原因。"""
        available = self.get_available_reasons(is_owner)
        if not available:
            return None
        return self._weighted_choice(available, attr="priority")

    def is_reason_allowed_now(self, reason_key: str, is_owner: bool) -> bool:  # noqa: FBT001
        """检查某个原因现在是否可用。"""
        available = self.get_available_reasons(is_owner)
        return any(r.key == reason_key for r in available)

    # ── 动作 ──────────────────────────────────────────

    def get_available_actions(self, is_owner: bool) -> list[ProactiveAction]:  # noqa: FBT001
        """获取当前可用动作列表 (过滤亲密 + 能力依赖)。"""
        actions: list[ProactiveAction] = []
        for a in self._config.proactive_actions:
            if a.intimate and not is_owner:
                continue
            actions.append(a)
        return actions

    def choose_action(self, is_owner: bool, available_abilities: set[str] | None = None) -> ProactiveAction | None:  # noqa: FBT001
        """按权重随机选择一个动作。过滤不可用的能力。"""
        available = self.get_available_actions(is_owner)
        if available_abilities is not None:
            available = [
                a for a in available
                if not a.requires_ability or a.requires_ability in available_abilities
            ]
        if not available:
            return None
        return self._weighted_choice(available, attr="weight")

    # ── 亲密动作检查 ──────────────────────────────────

    def is_intimate_action(self, action_key: str) -> bool:
        for a in self._config.proactive_actions:
            if a.key == action_key:
                return a.intimate
        return False

    def is_intimate_reason(self, reason_key: str) -> bool:
        for r in self._config.proactive_reasons:
            if r.key == reason_key:
                return r.intimate
        return False

    # ── 工具 ──────────────────────────────────────────

    @staticmethod
    def _weighted_choice(items: list, attr: str = "weight") -> Any:
        """按属性加权随机选择。"""
        weights = [max(0.0, getattr(x, attr, 1.0)) for x in items]
        total = sum(weights)
        if total <= 0:
            return random.choice(items)
        r = random.random() * total
        cumulative = 0.0
        for item, w in zip(items, weights, strict=False):
            cumulative += w
            if r <= cumulative:
                return item
        return items[-1]

    # ── 时段窗口计算 ──────────────────────────────────

    @staticmethod
    def delay_until_daypart(daypart: str) -> float:
        """计算距离下一个目标时段的延迟 (小时)。"""
        now = time.localtime()
        current_hour = now.tm_hour + now.tm_min / 60.0
        targets = {
            "morning": 7.0,
            "noon": 11.0,
            "afternoon": 14.0,
            "evening": 18.0,
            "night": 21.0,
        }
        target_hour = targets.get(daypart, current_hour + 1)
        if target_hour <= current_hour:
            target_hour += 24
        return max(0.25, target_hour - current_hour)
