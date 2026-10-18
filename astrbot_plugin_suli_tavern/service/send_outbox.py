"""发送 outbox — 主动发送幂等账本，重放靠幂等键去重。

状态：pending → sent / deduplicated。进程内内存实现，查询只读。
"""
from __future__ import annotations

import time

_RECORDS: dict[str, dict[str, object]] = {}


def claim(key: str) -> bool:
    """占位：首次返回 True，已存在返回 False（调用方跳过重发）。"""
    now = time.time()
    if key in _RECORDS:
        rec = _RECORDS[key]
        rec["hits"] = int(rec.get("hits", 1)) + 1
        rec["status"] = "deduplicated"
        return False
    _RECORDS[key] = {"status": "pending", "ts": now, "hits": 1}
    if len(_RECORDS) > 500:
        _old = next(iter(_RECORDS))
        del _RECORDS[_old]
    return True


def mark_sent(key: str) -> None:
    rec = _RECORDS.get(key)
    if rec is not None:
        rec["status"] = "sent"


def status(key: str) -> str:
    rec = _RECORDS.get(key)
    return str((rec or {}).get("status", "unknown"))


def idempotency_key(*parts: object) -> str:
    return ":".join(str(p or "") for p in parts)
