"""Opportunity Score — 主动回复机会评分系统。

从 self_evolution/engine/opportunity_cache.py 提取核心概念。

为 suli_gate 的 3-Stage Intent Gate 提供:
  - 多维度机会评分 (9 个维度)
  - 回复动机分类 (5 种 MotiveType)
  - 分层回复策略 (ignore / react / text_lite / full)
  - TTL 机会缓存 (warm / peek / consume)

用法:
  from astrbot_plugin_suli_gate.opportunity import (
      OpportunityScore, PendingOpportunity, OpportunityCache, MotiveType,
  )

  score = OpportunityScore(thread=0.6, persona_drive=0.4)
  level = score.level_from_score()  # "full"
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

# ═════════════════════════════════════════════════════════════════
# MotiveType — 回复动机
# ═════════════════════════════════════════════════════════════════

class MotiveType(Enum):
    """bot 为什么要在这个时机说话。"""
    SEEK_CONNECTION = "seek_connection"    # 想找人聊天，社交需求驱动
    CONTINUE_THREAD = "continue_thread"    # 未完话题续接
    LIGHT_RELIEF = "light_relief"          # 轻松一下，活跃气氛
    CURIOUS_PROBE = "curious_probe"        # 好奇探问
    SELF_PROTECTIVE = "self_protective"    # 防御姿态 (被质疑/被攻击时澄清)
    NONE = "none"


# ═════════════════════════════════════════════════════════════════
# OpportunityScore — 9 维机会评分
# ═════════════════════════════════════════════════════════════════

@dataclass
class OpportunityScore:
    """多维度机会评分 (每维 0.0-1.0)。

    维度说明:
      question:       消息是否在提问 (高分 = 明确提问)
      thread:         是否有未完成的对话线程 (高分 = 上下文连贯)
      topic_hook:     是否有可参与的话题切入点 (高分 = bot 有话说)
      natural_landing: 是否自然的接话时机 (高分 = 不突兀)
      emotion:         情绪相容度 (高分 = 情绪对得上)
      persona_drive:   人格内在驱动 (高分 = 人格 wants to say something)
      bot_activity:    bot 自身活跃度 (高分 = 最近比较活跃)
      relation:        关系亲密度 (高分 = 对方好感度高)
      novelty:         话题新鲜度 (高分 = 新话题，低分 = 重复话题)
    """

    question: float = 0.0
    thread: float = 0.0
    topic_hook: float = 0.0
    natural_landing: float = 0.0
    emotion: float = 0.0
    persona_drive: float = 0.0
    bot_activity: float = 0.0
    relation: float = 0.0
    novelty: float = 0.0

    negative_override: float = 0.0
    # 负面否决分 (< 0 = 被否决, 无论总分多少都不回复)

    @property
    def total(self) -> float:
        """加权总分 (各维度平均 + 负向否决)。"""
        dims = [
            self.question, self.thread, self.topic_hook,
            self.natural_landing, self.emotion, self.persona_drive,
            self.bot_activity, self.relation, self.novelty,
        ]
        base = sum(dims) / len(dims)
        if self.negative_override < 0.0:
            return min(base, self.negative_override)
        return base

    @property
    def is_blocked(self) -> bool:
        """被否决 = 无论分数如何都不回复。"""
        return self.negative_override < 0.0

    def level_from_score(self) -> str:
        """分数 → 回复层级。

        ignore:    不回复
        react:     仅表情包 reaction (不调 LLM)
        text_lite: 短回复 (30-50 字)
        full:      完整 LLM 回复
        """
        if self.is_blocked or self.total < 0.15:
            return "ignore"
        if self.total < 0.25:
            return "react"
        if self.total < 0.35:
            return "text_lite"
        return "full"


# ═════════════════════════════════════════════════════════════════
# ActiveMotive — 驱动锚点
# ═════════════════════════════════════════════════════════════════

@dataclass
class ActiveMotive:
    """当前驱动 bot 说话的动机。"""
    motive: MotiveType
    strength: float                  # 0.0-1.0
    source: str = ""                 # 触发来源描述


# ═════════════════════════════════════════════════════════════════
# PendingOpportunity — TTL 机会缓存
# ═════════════════════════════════════════════════════════════════

@dataclass
class PendingOpportunity:
    """待消费的回复机会。"""
    scope_id: str                    # 群/私聊 scope
    score: OpportunityScore
    anchor_text: str                 # 触发锚点文本
    anchor_type: str                 # "question" | "thread" | "topic_hook" | "trigger"
    motive: ActiveMotive
    created_at: float
    expires_at: float
    message_ids: list[str] = field(default_factory=list)
    trigger_reason: str = ""
    trigger_user_id: str = ""
    trigger_user_name: str = ""

    def is_expired(self, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        return now > self.expires_at

    def is_high_score(self) -> bool:
        return self.score.total >= 0.45


# ═════════════════════════════════════════════════════════════════
# OpportunityCache — warm / peek / consume
# ═════════════════════════════════════════════════════════════════

class OpportunityCache:
    """异步安全的回复机会缓存。

    模式:
      warm:   预热一个机会 (消息到达时记录)
      peek:   查看当前最高分机会 (不消费)
      consume: 取出所有待消费机会 (在 tick 时消费)
    """

    _MAX_PER_SCOPE: int = 3
    _DEFAULT_TTL: float = 180.0

    def __init__(self) -> None:
        self._data: dict[str, list[PendingOpportunity]] = {}
        self._lock = asyncio.Lock()

    async def warm(
        self,
        scope_id: str,
        score: OpportunityScore,
        anchor_text: str = "",
        anchor_type: str = "",
        motive: ActiveMotive | None = None,
        message_ids: list[str] | None = None,
        trigger_reason: str = "",
        ttl: float | None = None,
        trigger_user_id: str = "",
        trigger_user_name: str = "",
    ) -> None:
        """预热一个回复机会。被否决的机会不会进入缓存。"""
        if score.is_blocked:
            return
        async with self._lock:
            now = time.time()
            opp = PendingOpportunity(
                scope_id=scope_id,
                score=score,
                anchor_text=anchor_text,
                anchor_type=anchor_type,
                motive=motive or ActiveMotive(motive=MotiveType.NONE, strength=0.0),
                created_at=now,
                expires_at=now + (ttl if ttl is not None else self._DEFAULT_TTL),
                message_ids=message_ids or [],
                trigger_reason=trigger_reason,
                trigger_user_id=trigger_user_id,
                trigger_user_name=trigger_user_name,
            )
            self._data.setdefault(scope_id, []).append(opp)
            # 按分数降序排序，只保留最高分
            lst = self._data[scope_id]
            lst.sort(key=lambda x: x.score.total, reverse=True)
            if len(lst) > self._MAX_PER_SCOPE:
                self._data[scope_id] = lst[:self._MAX_PER_SCOPE]

    async def peek(self, scope_id: str) -> list[PendingOpportunity]:
        """查看当前所有有效机会 (不消费)。"""
        now = time.time()
        async with self._lock:
            if scope_id not in self._data:
                return []
            return [o for o in self._data[scope_id] if not o.is_expired(now)]

    async def consume(self, scope_id: str) -> list[PendingOpportunity]:
        """取出并清空所有有效机会。"""
        async with self._lock:
            now = time.time()
            if scope_id not in self._data:
                return []
            valid = [o for o in self._data[scope_id] if not o.is_expired(now)]
            self._data[scope_id] = []
            return valid

    async def has_any(self, scope_id: str) -> bool:
        """是否有待消费的机会。"""
        return len(await self.peek(scope_id)) > 0

    async def remove_one(self, scope_id: str, opp: PendingOpportunity) -> None:
        """移除特定机会 (如被 policy 拦截后清理)。"""
        async with self._lock:
            if scope_id not in self._data:
                return
            self._data[scope_id] = [o for o in self._data[scope_id] if o is not opp]

    async def remove_expired(self, scope_id: str) -> None:
        """清理过期机会。"""
        async with self._lock:
            if scope_id not in self._data:
                return
            now = time.time()
            self._data[scope_id] = [
                o for o in self._data[scope_id] if not o.is_expired(now)
            ]
