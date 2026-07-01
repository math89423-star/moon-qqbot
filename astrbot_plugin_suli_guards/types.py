"""暮恩守卫系统 — 共享类型定义。

定义所有守卫共用的数据类、配置和协议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


# ═══════════════════════════════════════════════════════════════
# InjectionVerdict
# ═══════════════════════════════════════════════════════════════


@dataclass
class InjectionVerdict:
    """注入检测判定结果。

    Attributes:
        blocked: 是否拦截 (仅 safety 硬线为 True; 警惕值过线走仲裁)
        action: "pass" | "block" | "arbitrate"
            - pass: 安全通过
            - block: D4 安全硬线命中, 立即拦截 (CSAM/性暴力)
            - arbitrate: 警惕值累积过线, 需要 LLM 仲裁裁决
        score: 本轮命中评分
        cumulative_score: 滑动窗口内累积警惕值
        matched_patterns: 命中的模式标签列表
        flagged_messages: 触发警惕值累积的用户消息 (供仲裁器审查)
        reason: 人类可读的原因
        reply: 拦截/仲裁时发送给用户的回复文本
        severity: 严重级别 — "none" | "low" | "medium" | "high"
        check_time_ms: 检测耗时 (毫秒)

    向后兼容属性:
        verdict: "block" | "arbitrate" | "pass" (字符串, 旧 API)
        block: bool (property, 旧 API — 仅 safety 硬线为 True)
    """

    blocked: bool = False
    action: str = "pass"  # "pass" | "block" | "arbitrate"
    score: int = 0
    cumulative_score: int = 0
    matched_patterns: list[str] = field(default_factory=list)
    flagged_messages: list[str] = field(default_factory=list)
    reason: str = ""
    reply: str = ""
    severity: str = "none"
    check_time_ms: float = 0.0

    @property
    def verdict(self) -> str:
        """旧 API 兼容: 返回 'block' / 'arbitrate' / 'pass' 字符串。"""
        if self.action == "block":
            return "block"
        if self.action == "arbitrate":
            return "arbitrate"
        return "pass"

    @property
    def block(self) -> bool:
        """旧 API 兼容: 仅 safety 硬线为 True (警惕值过线走仲裁, 不直接 block)。"""
        return self.action == "block"


# ═══════════════════════════════════════════════════════════════
# AbuseVerdict
# ═══════════════════════════════════════════════════════════════


@dataclass
class AbuseVerdict:
    """防滥用闸判定结果。

    Attributes:
        action: "allow" | "drop" | "degrade" | "cooldown"
        reason: 人类可读的原因 (用于日志)
        degrade_mode: action=degrade 时的降级模式
                      "readonly" — 只读不回
                      "flash_only" — 只用 flash, 禁止 advanced
                      "no_judge" — 跳过 judge, 走规则 fallback
        cooldown_seconds: action=cooldown 时的冷却秒数
        quota_remaining: 剩余配额快照 (供日志/调试)
    """

    action: str = "allow"
    reason: str = ""
    degrade_mode: str = ""
    cooldown_seconds: int = 0
    quota_remaining: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
# AbuseGuardConfig
# ═══════════════════════════════════════════════════════════════


@dataclass
class AbuseGuardConfig:
    """AbuseGuard 所需的配置阈值。

    由宿主插件注入，避免直接依赖 suli_tavern.Config。
    """

    # 限流: token bucket
    rate_capacity: int = 10  # 令牌桶容量
    rate_refill_per_second: float = 0.5  # 每秒填充令牌数

    # 重复检测
    repeat_threshold: float = 0.85  # 相似度阈值 (difflib)
    repeat_window: int = 5  # 检查最近 N 条消息
    repeat_admin_bypass: bool = True  # 管理员跳过重复检测

    # 每日配额
    daily_judge_limit: int = 50  # 每人每日 judge 调用上限
    daily_reply_limit: int = 100  # 每人每日回复上限
    daily_advanced_limit: int = 10  # 每人每日 advanced 调用上限

    # 线程深度
    thread_max_depth: int = 5  # 最大连续自回复深度
    thread_cooldown_seconds: int = 300  # 超深度后的冷却秒数

    # 管理员 QQ
    admin_qq: int | None = None


# ═══════════════════════════════════════════════════════════════
# MemoryStore — GroomingGuard 记忆写入协议
# ═══════════════════════════════════════════════════════════════


class MemoryStore(Protocol):
    """GroomingGuard 写入负面事实所需的记忆存储接口。

    GroomingGuard 调用 memory_store.remember(...) 写入调教检测记录。
    此协议显式定义接口契约，避免 duck-typing 在运行时静默失败。
    宿主插件（如 suli_tavern）的 UserMemoryStore 自然满足此协议。
    """

    async def remember(
        self, user_id: str, user_name: str, fact_value: str, category: str,
    ) -> None:
        """写入一条用户记忆事实。

        Args:
            user_id: QQ 号
            user_name: 用户名
            fact_value: 事实内容
            category: 类别 (GroomingGuard 固定使用 "风险")
        """
        ...


# ═══════════════════════════════════════════════════════════════
# PermanentStore — BotDetector 持久化协议
# ═══════════════════════════════════════════════════════════════


class PermanentStore(Protocol):
    """BotDetector 持久化接口 — 与 bot_db 的 get_config/set_config 兼容。

    宿主插件注入其 bot_db 实例 (或任意实现此协议的对象)。
    默认实现 (MemoryPermanentStore) 仅存内存，重启丢失。
    """

    def get_config(self, key: str, default: str = "") -> str:
        """读取持久化配置值。

        Args:
            key: 配置键
            default: 键不存在时的默认值

        Returns:
            字符串值
        """
        ...

    def set_config(self, key: str, value: str) -> None:
        """写入持久化配置值。

        Args:
            key: 配置键
            value: 字符串值
        """
        ...


# ═══════════════════════════════════════════════════════════════
# MemoryPermanentStore — 默认内存实现 (重启丢失)
# ═══════════════════════════════════════════════════════════════


class MemoryPermanentStore:
    """PermanentStore 的默认内存实现 — 重启丢失。

    suli_tavern 启动时注入自己的 DB-backed 实现替换此默认值。
    """

    def __init__(self) -> None:
        import logging
        _log = logging.getLogger(__name__)
        _log.warning(
            "⚠️ MemoryPermanentStore: 使用非持久化内存存储 — "
            "重启后所有 BotDetector 检测状态（嫌疑分、永久冷却、action_taken）将归零。"
            "如果你的部署需要持久化防御状态，请注入 DB-backed 实现。"
        )
        self._store: dict[str, str] = {}

    def get_config(self, key: str, default: str = "") -> str:
        return self._store.get(key, default)

    def set_config(self, key: str, value: str) -> None:
        self._store[key] = value


# ═══════════════════════════════════════════════════════════════
# BotSuspicion — BotDetector 判定结果
# ═══════════════════════════════════════════════════════════════


@dataclass
class BotSuspicion:
    """Bot 嫌疑评估结果。

    Attributes:
        score: 嫌疑分 (0.0-1.0, >=0.7 为疑似 bot)
        flags: 触发的检测维度标签列表
        willingness_penalty: 回复意愿惩罚因子 (0.0-1.0)
        action_taken: 是否已采取自动处置
    """

    score: float = 0.0
    flags: list[str] = field(default_factory=list)
    willingness_penalty: float = 1.0
    action_taken: bool = False
