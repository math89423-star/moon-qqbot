"""Durable TaskLedger — 用户承诺状态（P0-3 地基，P0-1 仅内存投影）。

原则：
- Durable Ledger = source of truth（qqbot 库 task_ledger 表，P0-3 落盘）
- Actor memory ledger = projection/cache
- TaskLedger 归 QQBot 拥有，执行器仅持执行状态，同 task_id 关联，执行器不直写此表
- 保存：task_id, bot_id, group_id, requester_id, origin_message_id, brief, status, created_at, artifacts

P0-1 阶段：仅内存实现，供 Actor 投递与查询；P0-3 接 MySQL（infra/mysql 单实例 qqbot 库）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, cast

TaskStatus = Literal["pending", "queued", "running", "completed", "partial", "failed", "cancelled", "expired"]


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    bot_id: str
    group_id: str
    requester_id: str
    origin_message_id: str
    brief: dict = field(default_factory=dict)
    status: TaskStatus = "pending"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    artifacts: list = field(default_factory=list)
    result_ref: str = ""


class TaskLedger:
    """Durable + 投影：MySQL 为 truth，内存为 cache。3+2 小池，短事务。"""

    def __init__(self) -> None:
        self._by_id: dict[str, TaskRecord] = {}
        self._by_group: dict[str, set[str]] = {}
        self._db_ok: bool | None = None  # None=未探活，True/False 已判定
        self._db_pool = None

    def _db(self):
        """取 infra/mysql 的 qqbot 库连接（小池，按需探活）。"""
        if self._db_ok is False:
            return None
        try:
            import os
            import pymysql

            if self._db_pool is None:
                # 首次探活：尝试一次短连接
                conn = pymysql.connect(
                    host=os.getenv("KNOWLEDGE_MYSQL_HOST", "mysql"),
                    port=int(os.getenv("KNOWLEDGE_MYSQL_PORT", "3306")),
                    user=os.getenv("KNOWLEDGE_MYSQL_USER", "qqbot"),
                    password=os.getenv("KNOWLEDGE_MYSQL_PASSWORD", ""),
                    database=os.getenv("KNOWLEDGE_MYSQL_DB", "qqbot"),
                    charset="utf8mb4",
                    connect_timeout=2,
                    autocommit=True,
                )
                # 探活成功，保留连接池占位（复用单连接，后续每次新建短连接，避免长事务）
                try:
                    conn.close()
                except Exception:
                    pass
                self._db_ok = True
            # 每次新建短连接（事务短，不持池）
            import pymysql as _pm

            return _pm.connect(
                host=os.getenv("KNOWLEDGE_MYSQL_HOST", "mysql"),
                port=int(os.getenv("KNOWLEDGE_MYSQL_PORT", "3306")),
                user=os.getenv("KNOWLEDGE_MYSQL_USER", "qqbot"),
                password=os.getenv("KNOWLEDGE_MYSQL_PASSWORD", ""),
                database=os.getenv("KNOWLEDGE_MYSQL_DB", "qqbot"),
                charset="utf8mb4",
                connect_timeout=2,
                autocommit=True,
            )
        except Exception:
            self._db_ok = False
            return None

    @staticmethod
    def new_task_id() -> str:
        return f"task-{uuid.uuid4().hex[:8]}"

    def _gkey(self, bot_id: str, group_id: str) -> str:
        return f"{bot_id}:{group_id}"

    def create(self, bot_id: str, group_id: str, requester_id: str, origin_message_id: str, brief: dict) -> TaskRecord:
        tid = self.new_task_id()
        now = time.time()
        rec = TaskRecord(
            task_id=tid,
            bot_id=bot_id,
            group_id=str(group_id),
            requester_id=requester_id,
            origin_message_id=origin_message_id,
            brief=dict(brief) if brief else {},
            status="pending",
            created_at=now,
            updated_at=now,
        )
        self._by_id[tid] = rec
        self._by_group.setdefault(self._gkey(bot_id, str(group_id)), set()).add(tid)
        # Durable 写（best-effort，失败不阻断内存）
        try:
            import json

            conn = self._db()
            if conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO qqbot.task_ledger (task_id, bot_id, group_id, requester_id, origin_message_id, brief_json, status, artifacts_json, result_ref, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (tid, bot_id, str(group_id), requester_id, origin_message_id, json.dumps(brief or {}, ensure_ascii=False), "pending", "[]", "", int(now * 1000), int(now * 1000)),
                    )
                conn.close()
        except Exception:
            pass
        return rec

    def get(self, task_id: str) -> TaskRecord | None:
        # 优先内存， miss 时回查 DB
        rec = self._by_id.get(task_id)
        if rec is not None:
            return rec
        try:
            import json

            conn = self._db()
            if conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT task_id, bot_id, group_id, requester_id, origin_message_id, brief_json, status, artifacts_json, result_ref, created_at, updated_at FROM qqbot.task_ledger WHERE task_id=%s", (task_id,))
                    row = cur.fetchone()
                    if row:
                        rec2 = TaskRecord(
                            task_id=row[0], bot_id=row[1], group_id=row[2], requester_id=row[3], origin_message_id=row[4],
                            brief=json.loads(row[5] or "{}"), status=row[6], artifacts=json.loads(row[7] or "[]"), result_ref=row[8] or "",
                            created_at=row[9] / 1000, updated_at=row[10] / 1000,
                        )
                        self._by_id[task_id] = rec2
                        self._by_group.setdefault(self._gkey(rec2.bot_id, rec2.group_id), set()).add(task_id)
                        conn.close()
                        return rec2
                conn.close()
        except Exception:
            pass
        return None

    def update_status(self, task_id: str, status: TaskStatus, artifacts: list | None = None, result_ref: str | None = None) -> TaskRecord | None:
        rec = self._by_id.get(task_id)
        # 若内存 miss，先从 DB 拉
        if rec is None:
            rec = self.get(task_id)
            if rec is None:
                return None
        rec.status = status
        rec.updated_at = time.time()
        if artifacts is not None:
            rec.artifacts = list(artifacts)
        if result_ref is not None:
            rec.result_ref = result_ref
        try:
            import json

            conn = self._db()
            if conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE qqbot.task_ledger SET status=%s, artifacts_json=%s, result_ref=%s, updated_at=%s WHERE task_id=%s",
                                (status, json.dumps(rec.artifacts, ensure_ascii=False), rec.result_ref, int(rec.updated_at * 1000), task_id))
                conn.close()
        except Exception:
            pass
        return rec

    def list_by_group(self, bot_id: str, group_id: str, status_filter: set[str] | None = None) -> list[TaskRecord]:
        # 优先 DB（truth），回退内存
        try:
            import json

            conn = self._db()
            if conn:
                sql = "SELECT task_id, bot_id, group_id, requester_id, origin_message_id, brief_json, status, artifacts_json, result_ref, created_at, updated_at FROM qqbot.task_ledger WHERE bot_id=%s AND group_id=%s"
                params: list = [bot_id, str(group_id)]
                if status_filter:
                    placeholders = ",".join(["%s"] * len(status_filter))
                    sql += f" AND status IN ({placeholders})"
                    params.extend(list(status_filter))
                sql += " ORDER BY updated_at DESC LIMIT 50"
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                    out: list[TaskRecord] = []
                    for row in rows:
                        rec = TaskRecord(
                            task_id=row[0], bot_id=row[1], group_id=row[2], requester_id=row[3], origin_message_id=row[4],
                            brief=json.loads(row[5] or "{}"), status=row[6], artifacts=json.loads(row[7] or "[]"), result_ref=row[8] or "",
                            created_at=row[9] / 1000, updated_at=row[10] / 1000,
                        )
                        out.append(rec)
                        self._by_id[rec.task_id] = rec
                        self._by_group.setdefault(self._gkey(rec.bot_id, rec.group_id), set()).add(rec.task_id)
                    conn.close()
                    return out
                conn.close()
        except Exception:
            pass
        tids = self._by_group.get(self._gkey(bot_id, str(group_id)), set())
        out = []
        for tid in tids:
            rec = self._by_id.get(tid)
            if rec is None:
                continue
            if status_filter and rec.status not in status_filter:
                continue
            out.append(rec)
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out

    def cancel(self, task_id: str) -> TaskRecord | None:
        return self.update_status(task_id, "cancelled")

    # ── D15: 异步变体 — 同步 pymysql 不得直接跑在 async 调度链 ──

    async def create_async(self, *args: Any, **kwargs: Any) -> TaskRecord:
        return await asyncio.to_thread(self.create, *args, **kwargs)

    async def get_async(self, *args: Any, **kwargs: Any) -> TaskRecord | None:
        return await asyncio.to_thread(self.get, *args, **kwargs)

    async def update_status_async(self, *args: Any, **kwargs: Any) -> TaskRecord | None:
        return await asyncio.to_thread(self.update_status, *args, **kwargs)

    async def list_by_group_async(self, *args: Any, **kwargs: Any) -> list[TaskRecord]:
        return await asyncio.to_thread(self.list_by_group, *args, **kwargs)

    async def cancel_async(self, task_id: str) -> TaskRecord | None:
        return await asyncio.to_thread(self.cancel, task_id)


async def ledger_call(ledger: object, name: str, *args: Any, **kwargs: Any) -> Any:
    """D15: 账本调用统一入口 — 优先 async 变体（线程池），兼容只有同步方法的 fake。

    真实 TaskLedger 走 *_async（to_thread，不阻塞事件循环）；
    离线探针的 NS fake 只有同步方法，直接调用。
    """
    async_fn = cast("Callable[..., Any] | None", getattr(ledger, f"{name}_async", None))
    if async_fn is not None:
        return await async_fn(*args, **kwargs)
    fn = getattr(ledger, name)
    res = fn(*args, **kwargs)
    if asyncio.iscoroutine(res):
        return await res
    if isinstance(res, asyncio.Future):
        return await res
    return res


# 全局单例（按需 per-bot 隔离通过 bot_id 前缀实现，符合 Typed Key 原则）
_global_ledger: TaskLedger | None = None


def get_task_ledger() -> TaskLedger:
    global _global_ledger
    if _global_ledger is None:
        _global_ledger = TaskLedger()
    return _global_ledger
