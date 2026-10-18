"""Context Budget Scheduler — 上下文预算调度器。

核心职责:
  1. 根据对话类型/消息复杂度动态调整 Token Budget
  2. 确保总预算不超限 (延迟敏感)
  3. 优先保障 Always-On 部分 (identity/personality/voice)

设计原则:
  - QQ 群聊 latency > exhaustive memory
  - 普通闲聊: ~6K-8K token
  - 深度对话: ~10K-12K token
  - 工具调用: ~5K-7K token (最小化 persona)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .context_runtime import (
    BUDGET_DEEP_CHAT,
    BUDGET_FAST_CHAT,
    BUDGET_TOOL_CALL,
    TokenBudget,
)
from ..intelligence.intent_router import RouteType

logger = logging.getLogger(__name__)


@dataclass
class BudgetAdjustment:
    """预算调整指令。"""
    # 调整因子 (1.0 = 不变, 0.5 = 减半, 2.0 = 翻倍)
    identity_factor: float = 1.0
    recent_chat_factor: float = 1.0
    memory_factor: float = 1.0
    canon_factor: float = 1.0

    # 绝对上限
    hard_limit: int = 15000  # 硬上限 (token)

    # 调整原因
    reason: str = ""


class BudgetScheduler:
    """上下文预算调度器 — 动态调整 token 分配。"""

    # ── 场景 → 预算映射 ──
    _ROUTE_BUDGETS: dict[RouteType, TokenBudget] = {
        RouteType.FAST: BUDGET_FAST_CHAT,
        RouteType.THINK: BUDGET_DEEP_CHAT,
        RouteType.AGENT: BUDGET_TOOL_CALL,
        RouteType.IGNORE: TokenBudget(),  # 不回复, 零预算
    }

    def schedule(
        self,
        route_type: RouteType,
        *,
        message_length: int = 0,
        memory_count: int = 0,
        world_book_count: int = 0,
        user_affinity: int = 0,
        group_activity: str = "normal",  # low / normal / high
    ) -> TokenBudget:
        """根据场景动态调度 token 预算。

        Args:
            route_type: 路由类型 (Fast/Think/Agent)
            message_length: 用户消息长度 (字符)
            memory_count: 检索到的记忆条数
            world_book_count: 检索到的世界书条数
            user_affinity: 用户好感度
            group_activity: 群活跃度 (low/normal/high)

        Returns:
            调整后的 TokenBudget
        """
        # ── 1. 基础预算 (按路由类型) ──
        base = self._ROUTE_BUDGETS.get(route_type, BUDGET_FAST_CHAT)

        # ── 2. 动态调整 ──
        adjustment = self._compute_adjustment(
            route_type=route_type,
            message_length=message_length,
            memory_count=memory_count,
            world_book_count=world_book_count,
            user_affinity=user_affinity,
            group_activity=group_activity,
        )

        # ── 3. 应用调整 ──
        adjusted = TokenBudget(
            identity_kernel=int(base.identity_kernel * adjustment.identity_factor),
            personality_kernel=base.personality_kernel,
            voice_kernel=base.voice_kernel,
            relationship_snapshot=base.relationship_snapshot,
            recent_chat=int(base.recent_chat * adjustment.recent_chat_factor),
            retrieved_memory=int(base.retrieved_memory * adjustment.memory_factor),
            retrieved_canon=int(base.retrieved_canon * adjustment.canon_factor),
            current_state=base.current_state,
        )

        # ── 4. 硬上限检查 ──
        if adjusted.total_budget > adjustment.hard_limit:
            logger.warning(
                "Token 预算超限: %d > %d, 截断 recent_chat",
                adjusted.total_budget, adjustment.hard_limit,
            )
            excess = adjusted.total_budget - adjustment.hard_limit
            adjusted = TokenBudget(
                identity_kernel=adjusted.identity_kernel,
                personality_kernel=adjusted.personality_kernel,
                voice_kernel=adjusted.voice_kernel,
                relationship_snapshot=adjusted.relationship_snapshot,
                recent_chat=max(500, adjusted.recent_chat - excess),
                retrieved_memory=adjusted.retrieved_memory,
                retrieved_canon=adjusted.retrieved_canon,
                current_state=adjusted.current_state,
            )

        logger.info(
            "Budget 调度: route=%s, base=%d, adjusted=%d, reason=%s",
            route_type.value, base.total_budget, adjusted.total_budget,
            adjustment.reason or "none",
        )

        return adjusted

    def _compute_adjustment(
        self,
        route_type: RouteType,
        message_length: int,
        memory_count: int,
        world_book_count: int,
        user_affinity: int,
        group_activity: str,
    ) -> BudgetAdjustment:
        """计算预算调整因子。"""
        adj = BudgetAdjustment()

        # ── 消息长度调整 ──
        if message_length > 500:
            # 长消息: 增加上下文
            adj.recent_chat_factor = 1.3
            adj.reason = "长消息"
        elif message_length < 20:
            # 短消息: 减少上下文
            adj.recent_chat_factor = 0.7
            adj.reason = "短消息"

        # ── 记忆/世界书数量调整 ──
        if memory_count > 5:
            adj.memory_factor = 0.8  # 记忆太多, 截断
            adj.reason += " + 记忆过多"
        elif memory_count == 0:
            adj.memory_factor = 0.5  # 无记忆, 减少预算
            adj.reason += " + 无记忆"

        if world_book_count > 3:
            adj.canon_factor = 0.7  # 世界书太多, 截断
            adj.reason += " + 世界书过多"

        # ── 群活跃度调整 ──
        if group_activity == "high":
            # 高活跃群: 减少上下文, 加快响应
            adj.recent_chat_factor = 0.6
            adj.hard_limit = 10000
            adj.reason += " + 高活跃群"
        elif group_activity == "low":
            # 低活跃群: 可以更多上下文
            adj.recent_chat_factor = 1.2
            adj.hard_limit = 12000
            adj.reason += " + 低活跃群"

        # ── 好感度调整 ──
        if user_affinity >= 4:
            # 高好感: 增加关系相关上下文
            adj.identity_factor = 1.1
            adj.reason += " + 高好感"

        return adj


# ═══════════════════════════════════════════════════════════════
# §2 单例
# ═══════════════════════════════════════════════════════════════

_scheduler: BudgetScheduler | None = None


def get_budget_scheduler() -> BudgetScheduler:
    """获取预算调度器单例。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = BudgetScheduler()
    return _scheduler
