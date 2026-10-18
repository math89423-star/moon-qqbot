"""Intent Router — 意图路由器 (Fast/Think/Agent 三级路径)。

核心职责:
  1. 根据用户消息复杂度选择处理路径
  2. Fast Path: 社交闲聊, <2s TTFT 目标
  3. Think Path: 需要推理的问题, 允许更长延迟
  4. Agent Path: 工具调用, 走后台任务执行器

设计原则:
  - Reasoning should be earned, not default (推理应被触发, 不是默认)
  - 大多数群聊消息走 Fast Path
  - 路由判断 <50ms (规则 + 简单分类器, 不用 LLM)

参考:
  - Character.AI: 大多数 turn 是 Social Conversation
  - OpenAI Latency Guidance: 使用更快的小模型 for simple tasks
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class RouteType(Enum):
    """处理路径类型。"""
    FAST = "fast"       # 快速闲聊: 轻上下文 + 快模型
    THINK = "think"     # 深度思考: 完整上下文 + 推理模型
    AGENT = "agent"     # 工具调用: 后台任务执行器
    IGNORE = "ignore"   # 忽略: 不回复


@dataclass
class RouteDecision:
    """路由决策结果。"""
    route: RouteType
    reason: str                     # 决策原因 (用于日志/调试)
    suggested_model_tier: str = ""  # 建议的模型层级 (lite/pro)
    reasoning_effort: str = ""      # 推理强度 (none/low/medium/high)
    priority: int = 0               # 优先级 (0=正常, >0=高优)


class IntentRouter:
    """意图路由器 — 基于规则的快速路径选择。

    不使用 LLM 做路由 (<50ms 目标), 而是:
      1. 关键词匹配 (高频模式)
      2. 正则表达式 (结构化模式)
      3. 消息特征 (长度/复杂度)
      4. 上下文状态 (是否被 @/回复)

    典型延迟: <10ms
    """

    # ── Fast Path 模式 ──
    # 社交闲聊: 打招呼/告别/简单回应/表情包
    _FAST_PATTERNS: list[re.Pattern] = [
        # 打招呼
        re.compile(r"^(早|早上好|早安|晚安|晚上好|你好|嗨|hi|hello|hey|在吗|在不在|在？|在~?)\s*[！!~～]*$", re.IGNORECASE),
        # 简单回应
        re.compile(r"^(好的?|ok|嗯|哦|知道了|明白|收到|谢谢|感谢|辛苦|加油|666|哈哈哈?|笑死|绝了|牛逼|厉害)\s*[！!~～]*$", re.IGNORECASE),
        # 告别
        re.compile(r"^(拜拜|再见|晚安|睡了|困了|走了|溜了|88|886|bye|goodbye)\s*[！!~～]*$", re.IGNORECASE),
        # 单个表情/emoji
        re.compile(r"^[\U0001f600-\U0001f64f\U0001f300-\U0001f5ff\U0001f680-\U0001f6ff\U0001f1e0-\U0001f1ff]{1,3}$"),
        # 简单问候回应
        re.compile(r"^(嗯嗯|好滴|好哒|好嘞|没问题|可以的?|行|可以|当然)\s*[！!~～]*$", re.IGNORECASE),
    ]

    # ── Think Path 模式 ──
    # 需要推理: 提问/分析/建议/复杂请求
    _THINK_KEYWORDS: list[str] = [
        "为什么", "怎么", "如何", "是什么", "什么是",
        "分析", "解释", "说明", "比较", "对比",
        "建议", "意见", "看法", "觉得",
        "应该", "能不能", "可以吗", "好不好",
        "帮我", "帮忙", "请问", "问一下",
        "怎么回事", "怎么回事", "什么情况",
        "推荐", "选择", "决定",
    ]

    # ── Agent Path 模式 ──
    # 工具调用: 搜索/生图/查数据
    _AGENT_KEYWORDS: list[str] = [
        "搜索", "搜一下", "查一下", "找一下", "帮我搜",
        "画", "生图", "画一个", "生成图片", "AI画",
        "看图", "识别", "这是什么图",
        "视频", "提取", "转发",
        "状态", "查询", "分数", "排名",
    ]

    # ── Ignore 模式 ──
    # 纯图片/纯表情/系统消息
    _IGNORE_PATTERNS: list[re.Pattern] = [
        # 纯图片消息 (无文字)
        re.compile(r"^\[图片(?:\s*URL:\s*https?://\S+)?\]$"),
        # 纯转发消息
        re.compile(r"^\[转发消息\]"),
        # 系统消息
        re.compile(r"^\[(?:系统|通知|提醒)\]"),
    ]

    def route(
        self,
        message: str,
        *,
        is_at_bot: bool = False,
        is_reply_bot: bool = False,
        has_tool_suggestion: bool = False,
        mentioned_tools: list[str] | None = None,
        user_affinity: int = 0,
        recent_bot_reply: bool = False,
    ) -> RouteDecision:
        """路由用户消息到合适的处理路径。

        Args:
            message: 用户消息文本
            is_at_bot: 是否 @了 bot
            is_reply_bot: 是否回复 bot 的消息
            has_tool_suggestion: Gate 是否建议了工具调用
            mentioned_tools: Gate 建议的具体工具列表
            user_affinity: 用户好感度
            recent_bot_reply: bot 最近是否回复过 (避免刷屏)

        Returns:
            RouteDecision 实例
        """
        # ── 0. 忽略检查 ──
        if self._should_ignore(message):
            return RouteDecision(
                route=RouteType.IGNORE,
                reason="纯图片/转发/系统消息",
            )

        # ── 1. Agent Path (工具调用) ──
        if has_tool_suggestion or self._is_tool_request(message):
            return RouteDecision(
                route=RouteType.AGENT,
                reason="Gate 建议工具 / 检测到工具调用意图",
                suggested_model_tier="pro",
            )

        # ── 2. Think Path (需要推理) ──
        if self._needs_thinking(message):
            return RouteDecision(
                route=RouteType.THINK,
                reason="检测到推理需求 (提问/分析/建议)",
                suggested_model_tier="pro",
                reasoning_effort="low",  # 默认低强度推理
            )

        # ── 3. Fast Path (默认) ──
        # 社交闲聊: 用最快路径
        if self._is_casual_chat(message):
            return RouteDecision(
                route=RouteType.FAST,
                reason="社交闲聊 (打招呼/简单回应/告别)",
                suggested_model_tier="lite",
            )

        # ── 默认: Fast Path ──
        # 大多数消息走快速路径, 除非明确需要推理
        return RouteDecision(
            route=RouteType.FAST,
            reason="默认快速路径",
            suggested_model_tier="lite",
        )

    def _should_ignore(self, message: str) -> bool:
        """检查是否应该忽略。"""
        stripped = message.strip()
        for pattern in self._IGNORE_PATTERNS:
            if pattern.match(stripped):
                return True
        return False

    def _is_tool_request(self, message: str) -> bool:
        """检查是否明确请求工具调用。"""
        msg_lower = message.lower()
        for keyword in self._AGENT_KEYWORDS:
            if keyword in msg_lower:
                return True
        return False

    def _needs_thinking(self, message: str) -> bool:
        """检查是否需要推理。"""
        msg_lower = message.lower()
        for keyword in self._THINK_KEYWORDS:
            if keyword in msg_lower:
                return True
        # 长消息通常需要更多思考
        if len(message) > 200:
            return True
        return False

    def _is_casual_chat(self, message: str) -> bool:
        """检查是否是社交闲聊。"""
        stripped = message.strip()
        for pattern in self._FAST_PATTERNS:
            if pattern.match(stripped):
                return True
        # 短消息 (≤20 字) 大概率是闲聊
        if len(stripped) <= 20:
            return True
        return False


# ═══════════════════════════════════════════════════════════════
# §2 单例
# ═══════════════════════════════════════════════════════════════

_router: IntentRouter | None = None


def get_intent_router() -> IntentRouter:
    """获取意图路由器单例。"""
    global _router
    if _router is None:
        _router = IntentRouter()
    return _router
