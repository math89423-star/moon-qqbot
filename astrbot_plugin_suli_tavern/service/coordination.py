"""Bot 间协调服务 — 原子发言权 token (ADR-001 阶段 1.1)。"""
from __future__ import annotations
import asyncio, logging, time
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .bot_db import BotDatabase
logger = logging.getLogger(__name__)
DEFAULT_TOKEN_TTL = 15.0
MAX_WAIT_FOR_PEER = 5.0
WAIT_POLL_INTERVAL = 0.3

class CoordinationService:
    def __init__(self, bot_id: str, db: "BotDatabase") -> None:
        self._bot_id = str(bot_id)
        self._db = db

    async def try_acquire(self, group_id: str, *, ttl: float = DEFAULT_TOKEN_TTL,
                          reply_target: str = "", wait_for_peer: bool = True) -> bool:
        gid = str(group_id)
        if self._db.coordination_acquire_token(gid, self._bot_id, ttl=ttl, reply_target=reply_target):
            logger.info("[coord] bot %s 获取群 %s 发言权 (TTL=%.1fs)", self._bot_id[:8], gid, ttl)
            return True
        if not wait_for_peer:
            return False
        deadline = time.time() + MAX_WAIT_FOR_PEER
        waited = 0.0
        while time.time() < deadline:
            await asyncio.sleep(WAIT_POLL_INTERVAL)
            waited += WAIT_POLL_INTERVAL
            if self._db.coordination_acquire_token(gid, self._bot_id, ttl=ttl, reply_target=reply_target):
                logger.info("[coord] bot %s 等待 %.1fs 后获取群 %s 发言权", self._bot_id[:8], waited, gid)
                return True
        logger.info("[coord] bot %s 群 %s 等待 %.1fs 未获取, 退让", self._bot_id[:8], gid, MAX_WAIT_FOR_PEER)
        return False

    async def release(self, group_id: str) -> None:
        self._db.coordination_release_token(str(group_id), self._bot_id)

    def is_peer_replying(self, group_id: str) -> bool:
        return self._db.coordination_is_peer_replying(str(group_id), self._bot_id)
