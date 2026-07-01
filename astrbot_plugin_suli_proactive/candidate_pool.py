"""候选池 — 主动行为候选的收集、评分、去重、TTL 管理。

从 proactive_engine.py 的 candidate_pool 逻辑提取。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class ProactiveCandidate:
    """单个主动行为候选。"""
    reason: str
    action: str = "message"
    topic: str = ""
    motive: str = ""
    source: str = ""  # 来源: "group_share", "news", "timer", "manual"
    score: float = 0.0
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    accepted: bool = False
    sent: bool = False


class CandidatePool:
    """主动行为候选池。

    特性:
      - TTL 过期自动清理 (基于 Config.candidate_pool_ttl_hours)
      - 容量上限 (Config.candidate_pool_max_size)
      - 签名去重 (同原因+同话题=重复)
      - 优先级排序 (score 降序)
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._pool: list[ProactiveCandidate] = []
        self._signatures: set[str] = set()

    # ── 签名生成 ──────────────────────────────────────

    @staticmethod
    def _make_signature(reason: str, topic: str, source: str) -> str:
        raw = f"{reason}|{topic}|{source}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    # ── 添加候选 ──────────────────────────────────────

    def offer(self, candidate: ProactiveCandidate) -> bool:
        """向池中添加候选。返回 False 表示被去重过滤。"""
        sig = self._make_signature(
            candidate.reason, candidate.topic, candidate.source,
        )
        if sig in self._signatures:
            logger.debug("候选池去重: reason=%s topic=%s", candidate.reason, candidate.topic)
            return False

        if candidate.expires_at <= 0:
            ttl = self._config.candidate_pool_ttl_hours * 3600
            candidate.expires_at = time.time() + ttl

        self._pool.append(candidate)
        self._signatures.add(sig)
        self._enforce_capacity()
        logger.debug(
            "候选池新增: reason=%s action=%s score=%.2f pool_size=%d",
            candidate.reason, candidate.action, candidate.score, len(self._pool),
        )
        return True

    def _enforce_capacity(self) -> None:
        """超出容量时移除最低分条目。"""
        max_size = self._config.candidate_pool_max_size
        while len(self._pool) > max_size:
            # 找到最低分且未接受的
            worst = None
            for c in self._pool:
                if not c.accepted and (worst is None or c.score < worst.score):
                    worst = c
            if worst is None:
                # 全部已接受, 移除最老的
                worst = self._pool[0]
            self._remove(worst)

    def _remove(self, candidate: ProactiveCandidate) -> None:
        sig = self._make_signature(
            candidate.reason, candidate.topic, candidate.source,
        )
        self._signatures.discard(sig)
        self._pool.remove(candidate)

    # ── 查询 ──────────────────────────────────────────

    def get_best(self) -> ProactiveCandidate | None:
        """获取最高分未发送候选。"""
        self._expire_stale()
        unsent = [c for c in self._pool if not c.sent]
        if not unsent:
            return None
        unsent.sort(key=lambda c: c.score, reverse=True)
        return unsent[0]

    def has_pending(self) -> bool:
        return any(not c.sent for c in self._pool)

    def mark_sent(self, candidate: ProactiveCandidate) -> None:
        candidate.sent = True
        candidate.accepted = True

    # ── 维护 ──────────────────────────────────────────

    def _expire_stale(self) -> None:
        now = time.time()
        expired = [c for c in self._pool if now > c.expires_at > 0]
        for c in expired:
            self._remove(c)
        if expired:
            logger.debug("候选池清理: 过期 %d 条, 剩余 %d", len(expired), len(self._pool))

    def cleanup(self) -> int:
        """清理所有过期条目。返回清理数。"""
        before = len(self._pool)
        self._expire_stale()
        return before - len(self._pool)

    @property
    def size(self) -> int:
        return len(self._pool)
