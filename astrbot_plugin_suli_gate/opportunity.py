"""Opportunity Score — 主动回复机会评分系统。

从 self_evolution/engine/opportunity_cache.py 提取核心概念。

为 suli_gate 的 3-Stage Intent Gate 提供:
  - 多维度机会评分 (9 个维度)
  - 回复动机分类 (5 种 MotiveType)
  - 分层回复策略 (ignore / react / text_lite / full)
  - TTL 机会缓存 (warm / peek / consume)
  - 规则计算函数 (rule-based scoring)

用法:
  from astrbot_plugin_suli_gate.opportunity import (
      OpportunityScore, PendingOpportunity, OpportunityCache, MotiveType,
      compute_rule_score,
  )

  # 规则计算 (7 个维度)
  score = compute_rule_score(
      trigger_content="帮我查一下天气",
      thread_summary="用户在问天气",
      affinity_level=2,
      heat=3.5,
      ...
  )

  # LLM 计算 (2 个维度: topic_hook, natural_landing)
  # 在 Gate prompt 中顺带输出

  level = score.level_from_score()  # "full"
"""

from __future__ import annotations

import asyncio
import re
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


# ═══════════════════════════════════════════════════════════════
# 规则计算函数 — 7 个维度的 rule-based scoring
# ═══════════════════════════════════════════════════════════════

# 疑问词/模式
_QUESTION_PATTERNS = re.compile(
    r"(什么|怎么|如何|为什么|吗|呢|？|\?|是不是|能不能|可以吗|请问|问一下|告诉我|帮我|查一下|搜索)",
    re.IGNORECASE,
)


def _score_question(trigger_content: str) -> float:
    """消息是否在提问 (0.0-1.0)。"""
    if not trigger_content:
        return 0.0
    if _QUESTION_PATTERNS.search(trigger_content):
        return 0.8
    # 问号
    if "?" in trigger_content or "？" in trigger_content:
        return 0.7
    return 0.0


def _score_thread(thread_summary: str, has_active_slot: bool) -> float:
    """是否有未完成的对话线程 (0.0-1.0)。"""
    if has_active_slot:
        return 0.9
    if thread_summary:
        return 0.6
    return 0.0


def _score_emotion(
    mood_valence: float,
    mood_arousal: float,
    input_nature: str,
) -> float:
    """情绪相容度 (0.0-1.0)。

    高分 = 情绪对得上，bot 有能力/意愿回应。
    """
    # 正面情绪 + 高唤醒 → 更愿意参与
    base = 0.5
    if mood_valence > 0.3:
        base += 0.2
    elif mood_valence < -0.3:
        base -= 0.1  # 低落时少主动

    if mood_arousal > 0.3:
        base += 0.1  # 高能量时更积极

    # 输入性质调制
    if input_nature in ("playful_banter", "sincere_chat"):
        base += 0.1
    elif input_nature in ("hostile", "provoking"):
        base -= 0.2

    return max(0.0, min(1.0, base))


def _score_persona_drive(
    atmosphere: str,
    intent_type: str,
) -> float:
    """人格内在驱动 (0.0-1.0)。

    高分 = 人格 wants to say something。
    """
    base = 0.3  # 基础驱动

    # 气氛调制
    if atmosphere in ("teasing_bot", "banter"):
        base += 0.3  # 被逗/打趣时更想回应
    elif atmosphere == "serious_help":
        base += 0.2  # 认真求助时想帮忙
    elif atmosphere == "water_chat":
        base += 0.1  # 水群时偶尔想参与

    # 意图调制
    if intent_type in ("question", "command"):
        base += 0.2
    elif intent_type == "roleplay":
        base += 0.3  # 角色扮演时人格驱动强

    return max(0.0, min(1.0, base))


def _score_bot_activity(
    total_replies_sent: int,
    last_reply_seconds_ago: float,
) -> float:
    """bot 自身活跃度 (0.0-1.0)。

    高分 = 最近比较活跃 (但不是太频繁)。
    """
    # 最近回复过 → 活跃
    if last_reply_seconds_ago < 300:  # 5 分钟内
        return 0.7
    elif last_reply_seconds_ago < 1800:  # 30 分钟内
        return 0.5
    elif last_reply_seconds_ago < 3600:  # 1 小时内
        return 0.3

    # 历史回复数调制
    if total_replies_sent > 50:
        return 0.4  # 老 bot
    elif total_replies_sent > 10:
        return 0.5  # 有一定经验
    return 0.6  # 新 bot 更积极


def _score_relation(affinity_level: int) -> float:
    """关系亲密度 (0.0-1.0)。

    高分 = 对方好感度高。
    """
    # affinity_level: -2 ~ 4
    # 归一化到 0.0-1.0
    return max(0.0, min(1.0, (affinity_level + 2) / 6.0))


def _score_novelty(
    main_topic: str,
    recent_topics: list[str],
) -> float:
    """话题新鲜度 (0.0-1.0)。

    高分 = 新话题，低分 = 重复话题。
    """
    if not main_topic:
        return 0.5  # 无话题信息

    # 检查是否与最近话题重复
    if main_topic in recent_topics:
        return 0.2  # 重复话题

    # 部分匹配
    for topic in recent_topics[-5:]:  # 只看最近 5 个
        if topic and main_topic in topic:
            return 0.3
        if topic and topic in main_topic:
            return 0.3

    return 0.8  # 新话题


def compute_rule_score(
    *,
    trigger_content: str = "",
    thread_summary: str = "",
    has_active_slot: bool = False,
    mood_valence: float = 0.0,
    mood_arousal: float = 0.0,
    input_nature: str = "",
    atmosphere: str = "water_chat",
    intent_type: str = "chat",
    total_replies_sent: int = 0,
    last_reply_seconds_ago: float = 0.0,
    affinity_level: int = 0,
    main_topic: str = "",
    recent_topics: list[str] | None = None,
    # LLM 维度 (来自 Gate 输出)
    topic_hook: float = 0.0,
    natural_landing: float = 0.0,
) -> OpportunityScore:
    """计算 OpportunityScore — 混合规则 + LLM。

    规则维度 (7个): question, thread, emotion, persona_drive,
                    bot_activity, relation, novelty
    LLM 维度 (2个): topic_hook, natural_landing

    Args:
        trigger_content: 触发消息文本
        thread_summary: 对话线程摘要
        has_active_slot: 是否有活跃关注槽
        mood_valence: 心情效价 (-1.0 ~ +1.0)
        mood_arousal: 心情唤醒度 (-1.0 ~ +1.0)
        input_nature: 输入性质 (genuine_help/playful_banter/...)
        atmosphere: 群聊气氛 (water_chat/banter/teasing_bot/...)
        intent_type: 意图类型 (question/chat/command/...)
        total_replies_sent: bot 历史总回复数
        last_reply_seconds_ago: 距上次回复的秒数
        affinity_level: 好感等级 (-2 ~ 4)
        main_topic: 当前核心话题
        recent_topics: 最近话题列表 (用于 novelty 计算)
        topic_hook: [LLM] 话题切入点分数 (0.0-1.0)
        natural_landing: [LLM] 自然接话时机分数 (0.0-1.0)

    Returns:
        OpportunityScore
    """
    recent_topics = recent_topics or []

    return OpportunityScore(
        question=_score_question(trigger_content),
        thread=_score_thread(thread_summary, has_active_slot),
        topic_hook=topic_hook,  # LLM 输出
        natural_landing=natural_landing,  # LLM 输出
        emotion=_score_emotion(mood_valence, mood_arousal, input_nature),
        persona_drive=_score_persona_drive(atmosphere, intent_type),
        bot_activity=_score_bot_activity(total_replies_sent, last_reply_seconds_ago),
        relation=_score_relation(affinity_level),
        novelty=_score_novelty(main_topic, recent_topics),
    )
