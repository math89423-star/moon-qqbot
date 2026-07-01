"""Layer 0 防滥用闸 — 纯规则/计数器/状态机, 零 LLM 成本。

在 IntentJudge 之前执行, 确保昂贵资源不被廉价攻击消耗。

设计原则 (来自架构裁定):
  - 身份和循环是状态, 不是语义 — 能用计数器和状态机确定性挡掉的, 绝不放进 LLM
  - judge 之前必须有一道纯计数的闸, 决定"这个人现在还配不配叫 judge"
  - 这道闸是 Layer 0 的, 0 成本, 且不可被 prompt 注入绕过

组件:
  TokenBucket      — 单用户滑动窗口限流 (令牌桶)
  QuotaManager     — 每用户每日 judge/reply/advanced 硬配额
  ThreadDepthGuard — 线程连续自回复深度 → 强制冷却 + 退避
  RepeatDetector   — 用户消息重复/复读检测
  AbuseGuard       — 编排器, 统一入口

用法:
  from astrbot_plugin_suli_guards import AbuseGuard, AbuseVerdict, AbuseGuardConfig

  config = AbuseGuardConfig(rate_capacity=10, ...)

  verdict = AbuseGuard.check_rate(user_id, config)
  if verdict.action == "drop":
      return  # 静默丢弃
"""

from __future__ import annotations

import difflib
import logging
import time
from dataclasses import dataclass

from .types import AbuseGuardConfig, AbuseVerdict

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# TokenBucket — 单用户滑动窗口限流
# ═══════════════════════════════════════════════════════════════

@dataclass
class _Bucket:
    """单个令牌桶状态。"""
    tokens: float = 0.0
    last_refill: float = 0.0
    dropped: int = 0
    last_logged: float = 0.0


_buckets: dict[str, _Bucket] = {}

_DEFAULT_RATE_PER_SECOND = 0.5
_DEFAULT_BURST_CAPACITY = 3
_DEFAULT_CLEANUP_INTERVAL = 600


def _get_bucket(bot_id: str, user_id: str, burst_capacity: int = _DEFAULT_BURST_CAPACITY) -> _Bucket:
    key = f"{bot_id}:{user_id}" if bot_id else user_id
    if key not in _buckets:
        _buckets[key] = _Bucket(tokens=float(burst_capacity), last_refill=time.time())
    return _buckets[key]


def _refill_bucket(bucket: _Bucket, rate: float, burst: float, now: float) -> None:
    elapsed = now - bucket.last_refill
    if elapsed > 0:
        bucket.tokens = min(burst, bucket.tokens + elapsed * rate)
    bucket.last_refill = now


def _cleanup_buckets(now: float) -> None:
    last_cleanup = getattr(_cleanup_buckets, "_last_run", 0.0)
    if now - last_cleanup < _DEFAULT_CLEANUP_INTERVAL:
        return
    _cleanup_buckets._last_run = now  # type: ignore[attr-defined]

    stale = [
        uid for uid, b in _buckets.items()
        if now - b.last_refill > 1800
    ]
    for uid in stale:
        del _buckets[uid]
    if stale:
        logger.debug("TokenBucket: 清理 %d 个过期桶", len(stale))


def check_token_bucket(
    user_id: str,
    rate_per_second: float = _DEFAULT_RATE_PER_SECOND,
    burst_capacity: int = _DEFAULT_BURST_CAPACITY,
    bot_id: str = "",
) -> AbuseVerdict:
    """单用户令牌桶限流。"""
    if not user_id:
        return AbuseVerdict(action="allow")

    now = time.time()
    _cleanup_buckets(now)

    bucket = _get_bucket(bot_id or "", user_id, burst_capacity)
    _refill_bucket(bucket, rate_per_second, burst_capacity, now)

    if bucket.tokens >= 1.0:
        bucket.tokens -= 1.0
        return AbuseVerdict(action="allow")

    bucket.dropped += 1
    if now - bucket.last_logged > 30:
        logger.info(
            "防滥用-限流: user=%s 令牌耗尽 (累计丢弃=%d, rate=%.1f/s)",
            user_id[:8], bucket.dropped, rate_per_second,
        )
        bucket.last_logged = now

    return AbuseVerdict(action="drop", reason=f"rate_limited: {bucket.dropped} dropped")


# ═══════════════════════════════════════════════════════════════
# QuotaManager — 每用户每日硬配额
# ═══════════════════════════════════════════════════════════════

@dataclass
class _DailyQuota:
    judge_count: int = 0
    reply_count: int = 0
    advanced_count: int = 0
    day_start: float = 0.0


_quotas: dict[str, _DailyQuota] = {}

_DEFAULT_DAILY_JUDGE = 50
_DEFAULT_DAILY_REPLY = 30
_DEFAULT_DAILY_ADVANCED = 5
_QUOTA_DAY_SECONDS = 86400


def _get_quota(bot_id: str, user_id: str) -> _DailyQuota:
    now = time.time()
    key = f"{bot_id}:{user_id}" if bot_id else user_id
    quota = _quotas.get(key)
    if quota is None or (now - quota.day_start) > _QUOTA_DAY_SECONDS:
        quota = _DailyQuota(day_start=now)
        _quotas[key] = quota
    return quota


def check_quotas(
    user_id: str,
    action: str = "judge",
    daily_judge: int = _DEFAULT_DAILY_JUDGE,
    daily_reply: int = _DEFAULT_DAILY_REPLY,
    daily_advanced: int = _DEFAULT_DAILY_ADVANCED,
    bot_id: str = "",
) -> AbuseVerdict:
    """检查用户每日配额。"""
    if not user_id:
        return AbuseVerdict(action="allow")

    quota = _get_quota(bot_id or "", user_id)
    remaining = {
        "judge": max(0, daily_judge - quota.judge_count),
        "reply": max(0, daily_reply - quota.reply_count),
        "advanced": max(0, daily_advanced - quota.advanced_count),
    }

    if action == "judge":
        if quota.judge_count >= daily_judge:
            logger.info("防滥用-配额: user=%s judge 配额耗尽 (%d/%d)", user_id[:8], quota.judge_count, daily_judge)
            return AbuseVerdict(action="degrade", reason="judge_quota_exhausted", degrade_mode="no_judge", quota_remaining=remaining)
        quota.judge_count += 1

    elif action == "reply":
        if quota.reply_count >= daily_reply:
            logger.info("防滥用-配额: user=%s reply 配额耗尽 (%d/%d) → readonly", user_id[:8], quota.reply_count, daily_reply)
            return AbuseVerdict(action="degrade", reason="reply_quota_exhausted", degrade_mode="readonly", quota_remaining=remaining)
        quota.reply_count += 1

    elif action == "advanced":
        if quota.advanced_count >= daily_advanced:
            logger.info("防滥用-配额: user=%s advanced 配额耗尽 (%d/%d) → flash_only", user_id[:8], quota.advanced_count, daily_advanced)
            return AbuseVerdict(action="degrade", reason="advanced_quota_exhausted", degrade_mode="flash_only", quota_remaining=remaining)
        quota.advanced_count += 1

    return AbuseVerdict(action="allow", quota_remaining=remaining)


def record_reply_quota(user_id: str) -> None:
    if not user_id:
        return
    _get_quota(user_id).reply_count += 1


def record_advanced_quota(user_id: str) -> None:
    if not user_id:
        return
    _get_quota(user_id).advanced_count += 1


# ═══════════════════════════════════════════════════════════════
# ThreadDepthGuard — 线程连续自回复深度
# ═══════════════════════════════════════════════════════════════

_DEFAULT_MAX_DEPTH = 3
_DEFAULT_DEPTH_COOLDOWN = 300
_DEFAULT_DEPTH_BACKOFF_BASE = 60

_depth_exceed_count: dict[str, int] = {}


def check_thread_depth(
    thread: dict | None,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    base_cooldown: int = _DEFAULT_DEPTH_COOLDOWN,
    backoff_base: int = _DEFAULT_DEPTH_BACKOFF_BASE,
    bot_id: str = "",
) -> AbuseVerdict:
    """检查对话线程的连续自回复深度。"""
    if not thread:
        return AbuseVerdict(action="allow")

    exchange_count = thread.get("exchange_count", 0)
    if exchange_count < max_depth:
        return AbuseVerdict(action="allow")

    user_id = thread.get("user_name", "") or str(id(thread))
    _prefix = f"{bot_id}:" if bot_id else ""
    thread_key = f"{_prefix}depth_{user_id}"

    exceed_times = _depth_exceed_count.get(thread_key, 0) + 1
    _depth_exceed_count[thread_key] = exceed_times

    cooldown = min(base_cooldown * (2 ** (exceed_times - 1)), 3600)

    logger.info(
        "防滥用-线程深度: thread=%s exchange=%d (阈值=%d) → 冷却 %ds (第%d次超限)",
        thread.get("user_name", "?")[:20], exchange_count, max_depth, cooldown, exceed_times,
    )

    return AbuseVerdict(
        action="cooldown",
        reason=f"thread_depth: exchange={exchange_count} >= max={max_depth}",
        cooldown_seconds=cooldown,
    )


def reset_thread_depth_counter(thread_key: str) -> None:
    _depth_exceed_count.pop(f"depth_{thread_key}", None)


# ═══════════════════════════════════════════════════════════════
# RepeatDetector — 用户消息重复/复读检测
# ═══════════════════════════════════════════════════════════════

_DEFAULT_REPEAT_THRESHOLD = 0.80
_DEFAULT_REPEAT_WINDOW = 10
_DEFAULT_REPEAT_MIN_LEN = 4


def check_repeat(
    content: str,
    recent_messages: list[dict],
    user_id: str = "",
    threshold: float = _DEFAULT_REPEAT_THRESHOLD,
    window: int = _DEFAULT_REPEAT_WINDOW,
    min_len: int = _DEFAULT_REPEAT_MIN_LEN,
) -> AbuseVerdict:
    """检测用户消息是否与最近消息高度重复 (复读/刷屏)。"""
    if not content or len(content) < min_len:
        return AbuseVerdict(action="allow")
    if not recent_messages:
        return AbuseVerdict(action="allow")

    same_user_msgs = [
        str(m.get("content", ""))
        for m in recent_messages[-window:]
        if str(m.get("user_id", "")) == user_id and str(m.get("content", ""))
    ]

    if len(same_user_msgs) < 2:
        return AbuseVerdict(action="allow")

    for prev_msg in same_user_msgs[-3:]:
        if len(prev_msg) < min_len:
            continue
        len_ratio = min(len(content), len(prev_msg)) / max(len(content), len(prev_msg))
        if len_ratio < 0.4:
            continue
        ratio = difflib.SequenceMatcher(None, content, prev_msg).ratio()
        if ratio >= threshold:
            logger.info(
                "防滥用-重复: user=%s ratio=%.2f msg=「%s」",
                user_id[:8] if user_id else "?", ratio, content[:50],
            )
            return AbuseVerdict(action="drop", reason=f"repeat_content: similarity={ratio:.2f} >= {threshold}")

    return AbuseVerdict(action="allow")


# ═══════════════════════════════════════════════════════════════
# AbuseGuard — 统一编排器
# ═══════════════════════════════════════════════════════════════


class AbuseGuard:
    """Layer 0 防滥用闸 — 统一入口。

    纯静态方法, 模块级状态。
    所有检查 0 LLM 成本, 不可被 prompt 注入绕过。
    接受 AbuseGuardConfig 而非 suli_tavern.Config, 实现框架解耦。
    """

    @staticmethod
    def check_rate(user_id: str, config: AbuseGuardConfig, bot_id: str = "") -> AbuseVerdict:
        """单用户令牌桶限流。应在 on_message 最开始调用。"""
        return check_token_bucket(
            user_id,
            rate_per_second=config.rate_refill_per_second,
            burst_capacity=config.rate_capacity,
            bot_id=bot_id,
        )

    @staticmethod
    def check_repeat_msg(
        content: str, recent_messages: list[dict],
        user_id: str, config: AbuseGuardConfig,
    ) -> AbuseVerdict:
        """用户消息重复检测。应在消息入库前调用。"""
        return check_repeat(
            content, recent_messages, user_id,
            threshold=config.repeat_threshold,
        )

    @staticmethod
    def check_quotas(
        user_id: str, action: str, config: AbuseGuardConfig, bot_id: str = "",
    ) -> AbuseVerdict:
        """每日配额检查。应在 judge 调用前 / 回复发送前调用。"""
        return check_quotas(
            user_id, action,
            daily_judge=config.daily_judge_limit,
            daily_reply=config.daily_reply_limit,
            daily_advanced=config.daily_advanced_limit,
            bot_id=bot_id,
        )

    @staticmethod
    def check_thread_depth(
        thread: dict | None, config: AbuseGuardConfig, bot_id: str = "",
    ) -> AbuseVerdict:
        """线程连续自回复深度检查。应在 judge 之前调用。"""
        return check_thread_depth(
            thread,
            max_depth=config.thread_max_depth,
            base_cooldown=config.thread_cooldown_seconds,
            bot_id=bot_id,
        )


# ═══════════════════════════════════════════════════════════════
# 自检
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("abuse_guard.py: 模块加载成功")
    print(f"  TokenBucket: {len(_buckets)} 个桶")
    print(f"  QuotaManager: {len(_quotas)} 个配额")
    print(f"  ThreadDepthGuard: {len(_depth_exceed_count)} 个退避计数")
