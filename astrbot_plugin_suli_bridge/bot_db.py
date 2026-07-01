"""本地 bot 数据库 — SQLite 持久化配置、用户记忆、知识库索引。

与 L-Port DB 完全隔离，存储 suli_qqbot 自身的运行时数据：
  - bot_config: 键值配置 (active_llm_id, temperature_*, admin_token, ...)
  - user_memories: 用户长期记忆 (从 JSON 迁移)
  - knowledge_sections: 知识库章节索引 (从 markdown 迁移)

线程安全：WAL 模式 + check_same_thread=False。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DB_DIR = Path("data")
DB_PATH = DB_DIR / "shared_db" / "suli_qqbot.db"

# ── Provider 分组 — 与 L-Port llm_config_service.py 一致 ──
VLM_PROVIDERS = frozenset({"gpt4v", "claude", "gemini", "nano_banana", "llama"})
LOCAL_PROVIDERS = frozenset({"llama", "ollama"})


@dataclass
class LLMConfigRO:
    """只读 LLM 配置视图 — 字段与 L-Port llm_config 表一一对应"""

    id: int
    name: str
    provider: str  # 'deepseek' | 'custom' | 'gpt4v' | 'claude' | 'gemini' | 'llama' | ...
    provider_name: str
    api_key: str  # 真实 key — QQ bot 内部使用, 严禁通过消息外发
    base_url: str
    model_name: str
    is_active: bool
    vlm_resize_max_dim: Optional[int]
    token_budget_cap: Optional[int]
    config_type: str = ""  # 显式类型: ""=自动, "llm"=强制LLM, "vlm"=强制VLM

    @property
    def is_vlm(self) -> bool:
        if self.config_type:
            return self.config_type == "vlm"
        return self.provider in VLM_PROVIDERS

    @property
    def is_local(self) -> bool:
        return self.provider in LOCAL_PROVIDERS

    @property
    def is_llm(self) -> bool:
        """云端 LLM (排除 VLM 和本地)"""
        if self.config_type:
            return self.config_type == "llm"
        return not self.is_vlm or self.is_local

    @property
    def normalized_base_url(self) -> str:
        """规范化 base_url: 保证以 /v1 结尾, 适配 OpenAI 兼容客户端。

        与 L-Port llm_service.py:422-431 逻辑一致。
        """
        url = self.base_url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if url.endswith("/chat/completions"):
            url = url.replace("/chat/completions", "")
        url = url.removesuffix("/v1")
        return url.rstrip("/") + "/v1"


# ── 表 DDL ─────────────────────────────────────────────────

_TABLE_BOT_CONFIG = """
CREATE TABLE IF NOT EXISTS bot_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
)
"""

_TABLE_USER_MEMORIES = """
CREATE TABLE IF NOT EXISTS user_memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    user_name     TEXT DEFAULT '',
    fact_key      TEXT NOT NULL,
    fact_value    TEXT NOT NULL,
    category      TEXT NOT NULL DEFAULT '其他',
    importance    REAL NOT NULL DEFAULT 0.5,
    created_at    REAL NOT NULL,
    last_accessed REAL NOT NULL,
    UNIQUE(user_id, fact_key)
)
"""

_TABLE_KNOWLEDGE_SECTIONS = """
CREATE TABLE IF NOT EXISTS knowledge_sections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    tokens     TEXT DEFAULT '[]',
    updated_at REAL NOT NULL
)
"""

_TABLE_TOKEN_USAGE = """
CREATE TABLE IF NOT EXISTS token_usage (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        REAL NOT NULL,
    scenario         TEXT NOT NULL DEFAULT '',
    user_id          TEXT DEFAULT '',
    group_id         TEXT DEFAULT '',
    model            TEXT DEFAULT '',
    provider         TEXT DEFAULT '',
    input_tokens     INTEGER DEFAULT 0,
    output_tokens    INTEGER DEFAULT 0,
    cache_hit_tokens  INTEGER DEFAULT 0,
    cache_miss_tokens INTEGER DEFAULT 0,
    latency_ms       INTEGER DEFAULT 0
)
"""

_IDX_TOKEN_TIME = """
CREATE INDEX IF NOT EXISTS idx_token_usage_timestamp
ON token_usage(timestamp)
"""

_IDX_TOKEN_SCENARIO = """
CREATE INDEX IF NOT EXISTS idx_token_usage_scenario
ON token_usage(scenario, timestamp)
"""

_TABLE_LLM_CONFIG = """
CREATE TABLE IF NOT EXISTS llm_config (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL DEFAULT '',
    provider           TEXT NOT NULL DEFAULT 'custom',
    provider_name      TEXT NOT NULL DEFAULT '',
    api_key            TEXT NOT NULL DEFAULT '',
    base_url           TEXT NOT NULL DEFAULT '',
    model_name         TEXT NOT NULL DEFAULT '',
    is_active          INTEGER NOT NULL DEFAULT 0,
    vlm_resize_max_dim INTEGER,
    token_budget_cap   INTEGER,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
)
"""


_MIGRATE_LLM_CONFIG_TYPE = """
ALTER TABLE llm_config ADD COLUMN config_type TEXT NOT NULL DEFAULT ''
"""

_TABLE_SUSPECTED_BOTS = """
CREATE TABLE IF NOT EXISTS suspected_bots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL UNIQUE,
    user_name     TEXT NOT NULL DEFAULT '',
    group_id      TEXT NOT NULL DEFAULT '',
    suspicion_score REAL NOT NULL DEFAULT 0.0,
    marked_by     TEXT NOT NULL DEFAULT 'auto',
    status        TEXT NOT NULL DEFAULT 'flagged',
    notes         TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
)
"""

_IDX_SUSPECT_BOT_USER = """
CREATE INDEX IF NOT EXISTS idx_suspected_bots_user_id
ON suspected_bots(user_id)
"""

_IDX_SUSPECT_BOT_STATUS = """
CREATE INDEX IF NOT EXISTS idx_suspected_bots_status
ON suspected_bots(status)
"""

# 索引
_IDX_MEM_USER = """
CREATE INDEX IF NOT EXISTS idx_user_memories_user_id
ON user_memories(user_id)
"""

_IDX_MEM_CATEGORY = """
CREATE INDEX IF NOT EXISTS idx_user_memories_category
ON user_memories(user_id, category)
"""

_IDX_KB_SOURCE = """
CREATE INDEX IF NOT EXISTS idx_knowledge_sections_source
ON knowledge_sections(source)
"""

_TABLE_GROUP_SUMMARIES = """
CREATE TABLE IF NOT EXISTS group_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    message_range_start INTEGER,
    message_range_end INTEGER,
    created_at REAL NOT NULL
)
"""

_IDX_GROUP_SUMMARIES = """
CREATE INDEX IF NOT EXISTS idx_group_summaries_group_id
ON group_summaries(group_id, created_at DESC)
"""

# ── 默认配置值 ────────────────────────────────────────────

_DEFAULT_CONFIGS: dict[str, str] = {
    "admin_token": "",
    "active_llm_id": "",
    "active_vlm_id": "",
    "temperature_bridge_chat": "0.7",
    "temperature_tavern_private": "0.9",
    "temperature_tavern_group": "0.8",
    "temperature_memory_extract": "0.2",
    "temperature_context_compress": "0.3",
    "temperature_cross_validation": "0.1",
}

# ── 迁移标记键 ────────────────────────────────────────────

_MIGRATED_JSON_KEY = "_migrated_json_memories"
_MIGRATED_KB_KEY = "_migrated_knowledge_base"
_MIGRATED_LLM_KEY = "_migrated_llm_configs"


class BotDatabase:
    """本地 bot SQLite 数据库管理器。

    用法:
        db = BotDatabase()
        db.init()                   # 建表 + 默认值 + 迁移
        val = db.get_config("key")  # 读取配置
        db.set_config("key", val)   # 写入配置
    """

    def __init__(self, db_path: Path | None = None):
        self._path = db_path or DB_PATH
        self._conn: sqlite3.Connection | None = None

    # ── 连接管理 ──────────────────────────────────────────

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("BotDatabase 未初始化，请先调用 init()")
        return self._conn

    def init(self) -> None:
        """初始化数据库：建表、默认值、迁移。幂等，可多次调用。"""
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(DB_PATH.parent, 0o700)
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
        )
        os.chmod(self._path, 0o600)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = sqlite3.Row

        self._create_tables()
        self._seed_defaults()
        self._migrate_json_memories()
        self._migrate_knowledge_base()
        self._migrate_llm_configs()

        logger.info(
            "BotDatabase 就绪: %s (%.1f KB)",
            self._path,
            self._path.stat().st_size / 1024 if self._path.exists() else 0,
        )

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── 建表 ──────────────────────────────────────────────

    def _create_tables(self) -> None:
        for stmt in [
            _TABLE_BOT_CONFIG,
            _TABLE_USER_MEMORIES,
            _TABLE_KNOWLEDGE_SECTIONS,
            _TABLE_TOKEN_USAGE,
            _TABLE_LLM_CONFIG,
            _MIGRATE_LLM_CONFIG_TYPE,
            _TABLE_SUSPECTED_BOTS,
            _TABLE_GROUP_SUMMARIES,
            _IDX_MEM_USER,
            _IDX_MEM_CATEGORY,
            _IDX_KB_SOURCE,
            _IDX_TOKEN_TIME,
            _IDX_TOKEN_SCENARIO,
            _IDX_SUSPECT_BOT_USER,
            _IDX_SUSPECT_BOT_STATUS,
            _IDX_GROUP_SUMMARIES,
        ]:
            try:
                self.conn.execute(stmt)
            except Exception:
                # 幂等: 列/索引已存在等错误静默跳过
                pass
        self.conn.commit()

    # ── 默认值 ────────────────────────────────────────────

    def _seed_defaults(self) -> None:
        """写入缺失的默认配置值。已有键不覆盖。"""
        now = time.time()
        for key, value in _DEFAULT_CONFIGS.items():
            existing = self.conn.execute(
                "SELECT 1 FROM bot_config WHERE key = ?", (key,),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO bot_config (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )

        # admin_token: 若为空则自动生成
        row = self.conn.execute(
            "SELECT value FROM bot_config WHERE key = 'admin_token'",
        ).fetchone()
        if row and not row["value"]:
            token = secrets.token_hex(16)
            self.conn.execute(
                "UPDATE bot_config SET value = ?, updated_at = ? WHERE key = 'admin_token'",
                (token, now),
            )
            logger.info("Admin token 已自动生成 (掩码: %s****)", token[:4])

        self.conn.commit()

    # ── JSON → DB 迁移 ────────────────────────────────────

    def _migrate_json_memories(self) -> None:
        """将 data/user_memories/*.json 迁移到 user_memories 表。"""
        row = self.conn.execute(
            "SELECT value FROM bot_config WHERE key = ?",
            (_MIGRATED_JSON_KEY,),
        ).fetchone()
        if row and row["value"] == "done":
            return  # 已迁移

        json_dir = DB_DIR / "user_memories"
        if not json_dir.exists() or not json_dir.is_dir():
            self.conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?, 'done', ?)",
                (_MIGRATED_JSON_KEY, time.time()),
            )
            self.conn.commit()
            return

        json_files = sorted(json_dir.glob("*.json"))
        if not json_files:
            self.conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?, 'done', ?)",
                (_MIGRATED_JSON_KEY, time.time()),
            )
            self.conn.commit()
            return

        migrated_users = 0
        migrated_facts = 0

        for path in json_files:
            try:
                mem = json.loads(path.read_text(encoding="utf-8"))
                user_id = str(mem.get("user_id", path.stem))
                user_name = mem.get("user_name", "")
                facts = mem.get("facts", [])

                for f in facts:
                    if isinstance(f, str):
                        # 旧格式: ["string", ...]
                        self.conn.execute(
                            "INSERT OR IGNORE INTO user_memories "
                            "(user_id, user_name, fact_key, fact_value, category, "
                            " importance, created_at, last_accessed) "
                            "VALUES (?, ?, ?, ?, '其他', 0.5, ?, ?)",
                            (user_id, user_name, f"fact_legacy_{hash(f) & 0x7FFFFFFF}",
                             f, time.time(), time.time()),
                        )
                        migrated_facts += 1
                    elif isinstance(f, dict):
                        self.conn.execute(
                            "INSERT OR IGNORE INTO user_memories "
                            "(user_id, user_name, fact_key, fact_value, category, "
                            " importance, created_at, last_accessed) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                user_id, user_name,
                                str(f.get("key", f"fact_{time.time()}")),
                                str(f.get("value", "")),
                                str(f.get("category", "其他")),
                                float(f.get("importance", 0.5)),
                                float(f.get("created_at", time.time())),
                                float(f.get("last_accessed", time.time())),
                            ),
                        )
                        migrated_facts += 1

                migrated_users += 1
            except Exception:
                logger.warning("迁移用户记忆文件失败: %s", path, exc_info=True)

        self.conn.execute(
            "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?, 'done', ?)",
            (_MIGRATED_JSON_KEY, time.time()),
        )
        self.conn.commit()
        logger.info(
            "JSON 记忆迁移完成: %d 用户, %d 条事实 → SQLite",
            migrated_users, migrated_facts,
        )

    # ── Markdown → DB 迁移 ────────────────────────────────

    def _migrate_knowledge_base(self) -> None:
        """将 knowledge/*.md 章节索引写入 knowledge_sections 表。"""
        row = self.conn.execute(
            "SELECT value FROM bot_config WHERE key = ?",
            (_MIGRATED_KB_KEY,),
        ).fetchone()
        if row and row["value"] == "done":
            return

        from .knowledge_base import KnowledgeBase

        kb = KnowledgeBase()
        if not kb._sections:
            self.conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?, 'done', ?)",
                (_MIGRATED_KB_KEY, time.time()),
            )
            self.conn.commit()
            return

        now = time.time()
        for sec in kb._sections:
            tokens_json = json.dumps(
                sorted(sec.tokens) if sec.tokens else [],
                ensure_ascii=False,
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO knowledge_sections "
                "(title, content, source, tokens, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sec.title, sec.content, sec.source, tokens_json, now),
            )

        self.conn.execute(
            "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?, 'done', ?)",
            (_MIGRATED_KB_KEY, time.time()),
        )
        self.conn.commit()
        logger.info("知识库迁移完成: %d 章节 → SQLite", len(kb._sections))

    # ── L-Port → Bot LLM 配置迁移 ───────────────────────────

    def _migrate_llm_configs(self) -> None:
        """将 L-Port llm_config 表一次性迁移到 bot 自己的 llm_config 表。

        仅在 bot 的 llm_config 表为空且 L-Port DB 存在时执行。
        迁移后标记 _MIGRATED_LLM_KEY = "done"。
        """
        row = self.conn.execute(
            "SELECT value FROM bot_config WHERE key = ?",
            (_MIGRATED_LLM_KEY,),
        ).fetchone()
        if row and row["value"] == "done":
            return

        # 检查是否已有配置 (可能是手动添加的)
        existing = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM llm_config",
        ).fetchone()
        if existing and existing["cnt"] > 0:
            self.conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?, 'done', ?)",
                (_MIGRATED_LLM_KEY, time.time()),
            )
            self.conn.commit()
            return

        # 尝试从 L-Port 迁移
        lport_db = os.getenv(
            "LPORT_DB_PATH",
            "/mnt/d/L-Port-Comfy/app/backend/instance/lport_desktop.db",
        )
        if not os.path.exists(lport_db):
            logger.info("L-Port DB 不存在 (%s)，跳过 LLM 配置迁移", lport_db)
            self.conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?, 'done', ?)",
                (_MIGRATED_LLM_KEY, time.time()),
            )
            self.conn.commit()
            return

        try:
            from ...astrbot_plugin_suli_bridge.config_reader import LPortConfigReader
            reader = LPortConfigReader(lport_db)
            lport_configs = reader.list_all()
        except Exception:
            logger.warning("无法从 L-Port 读取配置，跳过迁移", exc_info=True)
            self.conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?, 'done', ?)",
                (_MIGRATED_LLM_KEY, time.time()),
            )
            self.conn.commit()
            return

        if not lport_configs:
            logger.info("L-Port 无 LLM 配置，跳过迁移")
            self.conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?, 'done', ?)",
                (_MIGRATED_LLM_KEY, time.time()),
            )
            self.conn.commit()
            return

        # 建立 old_id → new_id 映射
        old_to_new: dict[int, int] = {}
        now = time.time()
        migrated = 0

        for cfg in lport_configs:
            cur = self.conn.execute(
                "INSERT INTO llm_config "
                "(name, provider, provider_name, api_key, base_url, model_name, "
                " is_active, vlm_resize_max_dim, token_budget_cap, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cfg.name, cfg.provider, cfg.provider_name,
                    cfg.api_key, cfg.base_url, cfg.model_name,
                    int(cfg.is_active),
                    cfg.vlm_resize_max_dim, cfg.token_budget_cap,
                    now, now,
                ),
            )
            old_to_new[cfg.id] = cur.lastrowid
            migrated += 1

        # 同步 active_llm_id / active_vlm_id
        active_llm_id = self.get_config("active_llm_id", "")
        if active_llm_id:
            old_id = int(active_llm_id)
            if old_id in old_to_new:
                self.set_config("active_llm_id", str(old_to_new[old_id]))
                active_llm_id = str(old_to_new[old_id])
            else:
                active_llm_id = ""

        active_vlm_id = self.get_config("active_vlm_id", "")
        if active_vlm_id:
            old_id = int(active_vlm_id)
            if old_id in old_to_new:
                self.set_config("active_vlm_id", str(old_to_new[old_id]))
                active_vlm_id = str(old_to_new[old_id])
            else:
                active_vlm_id = ""

        # 回退: 若 active_llm_id 仍为空, 自动选首个 LLM
        if not active_llm_id:
            all_configs = self.list_llm_configs()
            for c in all_configs:
                if c.is_llm and c.is_active:
                    self.set_config("active_llm_id", str(c.id))
                    logger.info("自动选择活跃 LLM (is_active): id=%d name=%s", c.id, c.name)
                    break
            else:
                for c in all_configs:
                    if c.is_llm:
                        self.set_config("active_llm_id", str(c.id))
                        logger.info("自动选择活跃 LLM (首个): id=%d name=%s", c.id, c.name)
                        break

        # 标记迁移完成
        self.conn.execute(
            "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?, 'done', ?)",
            (_MIGRATED_LLM_KEY, time.time()),
        )
        self.conn.commit()
        logger.info(
            "LLM 配置迁移完成: %d 条配置 ← L-Port (%s)",
            migrated, lport_db,
        )

    # ── 配置 CRUD ─────────────────────────────────────────

    def get_config(self, key: str, default: str = "") -> str:
        """读取单个配置值。"""
        row = self.conn.execute(
            "SELECT value FROM bot_config WHERE key = ?", (key,),
        ).fetchone()
        return row["value"] if row else default

    def set_config(self, key: str, value: str) -> None:
        """写入单个配置值 (UPSERT)。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?, ?, ?)",
            (key, str(value), time.time()),
        )
        self.conn.commit()

    def get_all_configs(self) -> dict[str, str]:
        """读取所有非迁移、非敏感的配置项。"""
        rows = self.conn.execute(
            "SELECT key, value FROM bot_config "
            r"WHERE key NOT LIKE '\_%' ESCAPE '\' "
            "ORDER BY key",
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_many_configs(self, data: dict[str, str]) -> None:
        """批量写入配置。"""
        now = time.time()
        self.conn.executemany(
            "INSERT OR REPLACE INTO bot_config (key, value, updated_at) VALUES (?, ?, ?)",
            [(k, str(v), now) for k, v in data.items()],
        )
        self.conn.commit()

    # ── 用户记忆 CRUD ─────────────────────────────────────

    def get_memory_users(
        self, page: int = 1, per_page: int = 20,
    ) -> tuple[list[dict], int]:
        """分页获取有记忆的用户列表。"""
        offset = (page - 1) * per_page
        total_row = self.conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS cnt FROM user_memories",
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

        rows = self.conn.execute(
            "SELECT user_id, user_name, COUNT(*) AS fact_count, "
            "MAX(last_accessed) AS last_active "
            "FROM user_memories "
            "GROUP BY user_id "
            "ORDER BY last_active DESC "
            "LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()
        return ([dict(r) for r in rows], total)

    def get_user_memories(
        self, user_id: str, page: int = 1, per_page: int = 50,
    ) -> tuple[list[dict], int]:
        """获取某用户的记忆列表。"""
        offset = (page - 1) * per_page
        total_row = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM user_memories WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

        rows = self.conn.execute(
            "SELECT * FROM user_memories WHERE user_id = ? "
            "ORDER BY importance DESC, last_accessed DESC "
            "LIMIT ? OFFSET ?",
            (user_id, per_page, offset),
        ).fetchall()
        return ([dict(r) for r in rows], total)

    def delete_user_fact(self, user_id: str, fact_key: str) -> bool:
        """删除一条用户记忆。返回是否成功。"""
        cur = self.conn.execute(
            "DELETE FROM user_memories WHERE user_id = ? AND fact_key = ?",
            (user_id, fact_key),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def search_user_memories(
        self, query: str, top_n: int = 20,
    ) -> list[dict]:
        """跨用户搜索记忆 (按 fact_value LIKE 匹配)。"""
        rows = self.conn.execute(
            "SELECT * FROM user_memories WHERE fact_value LIKE ? "
            "ORDER BY importance DESC LIMIT ?",
            (f"%{query}%", top_n),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 知识库 ────────────────────────────────────────────

    def get_knowledge_sections(
        self, source: str = "", page: int = 1, per_page: int = 50,
    ) -> tuple[list[dict], int]:
        """分页获取知识库章节。source 为空则返回全部。"""
        offset = (page - 1) * per_page
        if source:
            total_row = self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_sections WHERE source = ?",
                (source,),
            ).fetchone()
            total = total_row["cnt"] if total_row else 0
            rows = self.conn.execute(
                "SELECT id, title, source, updated_at FROM knowledge_sections "
                "WHERE source = ? ORDER BY id LIMIT ? OFFSET ?",
                (source, per_page, offset),
            ).fetchall()
        else:
            total_row = self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_sections",
            ).fetchone()
            total = total_row["cnt"] if total_row else 0
            rows = self.conn.execute(
                "SELECT id, title, source, updated_at FROM knowledge_sections "
                "ORDER BY id LIMIT ? OFFSET ?",
                (per_page, offset),
            ).fetchall()
        return ([dict(r) for r in rows], total)

    def get_knowledge_section(self, section_id: int) -> dict | None:
        """获取单个知识库章节全文。"""
        row = self.conn.execute(
            "SELECT * FROM knowledge_sections WHERE id = ?", (section_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_knowledge_sources(self) -> list[str]:
        """获取知识库来源文件列表。"""
        rows = self.conn.execute(
            "SELECT DISTINCT source FROM knowledge_sections ORDER BY source",
        ).fetchall()
        return [r["source"] for r in rows]

    # ── LLM 配置 CRUD ─────────────────────────────────────

    @staticmethod
    def _row_to_llm_config(row: sqlite3.Row) -> LLMConfigRO:
        return LLMConfigRO(
            id=row["id"],
            name=row["name"] or "",
            provider=row["provider"] or "custom",
            provider_name=row["provider_name"] or "",
            api_key=row["api_key"] or "",
            base_url=row["base_url"] or "",
            model_name=row["model_name"] or "",
            is_active=bool(row["is_active"]),
            vlm_resize_max_dim=row["vlm_resize_max_dim"],
            token_budget_cap=row["token_budget_cap"],
            config_type=row["config_type"] or "",
        )

    def list_llm_configs(self) -> List[LLMConfigRO]:
        """列出全部 LLM/VLM 配置。"""
        rows = self.conn.execute(
            "SELECT * FROM llm_config ORDER BY id",
        ).fetchall()
        return [self._row_to_llm_config(r) for r in rows]

    def get_llm_config(self, config_id: int) -> Optional[LLMConfigRO]:
        """按 ID 查询 LLM 配置。"""
        row = self.conn.execute(
            "SELECT * FROM llm_config WHERE id = ?", (config_id,),
        ).fetchone()
        return self._row_to_llm_config(row) if row else None

    def add_llm_config(
        self,
        *,
        name: str,
        provider: str = "custom",
        provider_name: str = "",
        api_key: str = "",
        base_url: str = "",
        model_name: str = "",
        is_active: bool = False,
        vlm_resize_max_dim: Optional[int] = None,
        token_budget_cap: Optional[int] = None,
        config_type: str = "",
    ) -> int:
        """新增 LLM 配置。返回新 ID。"""
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO llm_config "
            "(name, provider, provider_name, api_key, base_url, model_name, "
            " is_active, vlm_resize_max_dim, token_budget_cap, config_type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name, provider, provider_name, api_key, base_url, model_name,
                int(is_active), vlm_resize_max_dim, token_budget_cap, config_type,
                now, now,
            ),
        )
        self.conn.commit()
        logger.info("新增 LLM 配置: id=%d name=%s", cur.lastrowid, name)
        return cur.lastrowid

    def update_llm_config(self, config_id: int, **fields) -> bool:
        """更新 LLM 配置。只更新提供的字段。"""
        allowed = {
            "name", "provider", "provider_name", "api_key", "base_url",
            "model_name", "is_active", "vlm_resize_max_dim", "token_budget_cap",
            "config_type",
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False

        if "is_active" in updates:
            updates["is_active"] = int(updates["is_active"])

        updates["updated_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [config_id]

        cur = self.conn.execute(
            f"UPDATE llm_config SET {set_clause} WHERE id = ?",
            values,
        )
        self.conn.commit()
        ok = cur.rowcount > 0
        if ok:
            logger.info("更新 LLM 配置: id=%d fields=%s", config_id, list(updates.keys()))
        return ok

    def delete_llm_config(self, config_id: int) -> bool:
        """删除 LLM 配置。返回是否成功。"""
        cur = self.conn.execute(
            "DELETE FROM llm_config WHERE id = ?", (config_id,),
        )
        self.conn.commit()
        ok = cur.rowcount > 0
        if ok:
            logger.info("删除 LLM 配置: id=%d", config_id)
        return ok

    # ── Token 用量记录 ────────────────────────────────────

    def record_token_usage(
        self,
        *,
        scenario: str = "",
        user_id: str = "",
        group_id: str = "",
        model: str = "",
        provider: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        latency_ms: int = 0,
    ) -> None:
        """记录一次 LLM 调用的 token 消耗。"""
        now = time.time()
        self.conn.execute(
            "INSERT INTO token_usage "
            "(timestamp, scenario, user_id, group_id, model, provider, "
            " input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now, scenario, user_id, group_id, model, provider,
                input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens, latency_ms,
            ),
        )
        self.conn.commit()

    def get_token_stats(
        self, period: str = "today",
    ) -> dict:
        """获取 token 消耗统计。

        Args:
            period: "today" | "week" | "month" | "all"

        Returns:
            {input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens,
             request_count, estimated_cost_cny, by_scenario: {...}}
        """
        now = time.time()
        if period == "today":
            since = now - 86400
        elif period == "week":
            since = now - 604800
        elif period == "month":
            since = now - 2592000
        else:
            since = 0

        row = self.conn.execute(
            "SELECT "
            "  COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "  COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "  COALESCE(SUM(cache_hit_tokens), 0) AS cache_hit_tokens, "
            "  COALESCE(SUM(cache_miss_tokens), 0) AS cache_miss_tokens, "
            "  COUNT(*) AS request_count "
            "FROM token_usage WHERE timestamp >= ?",
            (since,),
        ).fetchone()

        # 按场景分组
        by_scenario_rows = self.conn.execute(
            "SELECT scenario, "
            "  COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "  COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "  COUNT(*) AS request_count "
            "FROM token_usage WHERE timestamp >= ? "
            "GROUP BY scenario ORDER BY input_tokens DESC",
            (since,),
        ).fetchall()

        total_input = row["input_tokens"] if row else 0
        total_output = row["output_tokens"] if row else 0
        cache_hit = row["cache_hit_tokens"] if row else 0
        cache_miss = row["cache_miss_tokens"] if row else 0

        # 估算费用 (DeepSeek V4 Flash pricing)
        # cache hit: $0.014/M, cache miss: $0.14/M, output: $0.28/M
        cost_usd = (
            cache_hit / 1_000_000 * 0.014
            + cache_miss / 1_000_000 * 0.14
            + total_output / 1_000_000 * 0.28
        )

        return {
            "period": period,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
            "cache_hit_rate": (
                round(cache_hit / (cache_hit + cache_miss) * 100, 1)
                if (cache_hit + cache_miss) > 0 else 0
            ),
            "request_count": row["request_count"] if row else 0,
            "estimated_cost_cny": round(cost_usd * 7.2, 4),
            "by_scenario": {
                r["scenario"]: {
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "request_count": r["request_count"],
                }
                for r in by_scenario_rows
            },
        }

    def get_token_history(
        self, days: int = 7,
    ) -> list[dict]:
        """获取每日 token 消耗趋势。"""
        now = time.time()
        since = now - days * 86400
        rows = self.conn.execute(
            "SELECT "
            "  DATE(timestamp, 'unixepoch') AS day, "
            "  COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "  COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "  COUNT(*) AS request_count "
            "FROM token_usage WHERE timestamp >= ? "
            "GROUP BY day ORDER BY day",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_cache_rate(
        self, period: str = "today",
    ) -> dict:
        """获取缓存命中率统计 (轻量版, 面向状态面板)。

        Args:
            period: "today" | "week" | "hour"

        Returns:
            {hit_rate, hit_tokens, miss_tokens, total_cached_pct, period}
        """
        now = time.time()
        if period == "hour":
            since = now - 3600
        elif period == "week":
            since = now - 604800
        else:
            since = now - 86400

        row = self.conn.execute(
            "SELECT "
            "  COALESCE(SUM(cache_hit_tokens), 0) AS hit, "
            "  COALESCE(SUM(cache_miss_tokens), 0) AS miss, "
            "  COALESCE(SUM(input_tokens), 0) AS total_input "
            "FROM token_usage WHERE timestamp >= ?",
            (since,),
        ).fetchone()

        hit = row["hit"] if row else 0
        miss = row["miss"] if row else 0
        total_input = row["total_input"] if row else 0
        total_cacheable = hit + miss

        return {
            "period": period,
            "hit_tokens": hit,
            "miss_tokens": miss,
            "hit_rate": (
                round(hit / total_cacheable * 100, 1)
                if total_cacheable > 0 else 0
            ),
            "total_cached_pct": (
                round(hit / total_input * 100, 1)
                if total_input > 0 else 0
            ),
        }

    # ── 数据库状态 ────────────────────────────────────────

    def get_stats(self) -> dict:
        """获取数据库统计信息。"""
        mem_count = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM user_memories",
        ).fetchone()
        user_count = self.conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS cnt FROM user_memories",
        ).fetchone()
        kb_count = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM knowledge_sections",
        ).fetchone()
        sb_count = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM suspected_bots",
        ).fetchone()
        return {
            "db_size_kb": round(
                self._path.stat().st_size / 1024, 1,
            ) if self._path.exists() else 0,
            "user_count": user_count["cnt"] if user_count else 0,
            "memory_count": mem_count["cnt"] if mem_count else 0,
            "knowledge_sections": kb_count["cnt"] if kb_count else 0,
            "suspected_bots": sb_count["cnt"] if sb_count else 0,
        }


    # ── 疑似 Bot 管理 (管理员前端 API) ─────────────────

    def add_suspected_bot(
        self, user_id: str, user_name: str = "",
        group_id: str = "", score: float = 0.0,
        marked_by: str = "auto", notes: str = "",
    ) -> bool:
        """标记一个用户为疑似 Bot (自动检测或管理员手动)。

        已存在则更新分数和时间。
        """
        now = time.time()
        try:
            self.conn.execute(
                """INSERT INTO suspected_bots
                   (user_id, user_name, group_id, suspicion_score, marked_by, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                   user_name=excluded.user_name,
                   group_id=excluded.group_id,
                   suspicion_score=excluded.suspicion_score,
                   updated_at=excluded.updated_at""",
                (user_id, user_name, group_id, score, marked_by, notes, now, now),
            )
            self.conn.commit()
            logger.info(
                "suspected_bot: 标记 user=%s score=%.2f by=%s",
                user_id[:8], score, marked_by,
            )
            return True
        except Exception:
            logger.error("suspected_bot 写入失败", exc_info=True)
            return False

    def remove_suspected_bot(self, user_id: str) -> bool:
        """取消疑似 Bot 标记 (管理员手动修正)。"""
        try:
            self.conn.execute(
                "DELETE FROM suspected_bots WHERE user_id = ?",
                (user_id,),
            )
            self.conn.commit()
            logger.info("suspected_bot: 取消标记 user=%s", user_id[:8])
            return True
        except Exception:
            logger.error("suspected_bot 删除失败", exc_info=True)
            return False

    def update_suspected_bot(
        self, user_id: str, status: str = "", notes: str = "",
    ) -> bool:
        """更新疑似 Bot 状态 (管理员确认/修正)。"""
        now = time.time()
        try:
            parts = []
            params = []
            if status:
                parts.append("status = ?")
                params.append(status)
            if notes:
                parts.append("notes = ?")
                params.append(notes)
            parts.append("updated_at = ?")
            params.append(now)
            params.append(user_id)

            self.conn.execute(
                f"UPDATE suspected_bots SET {', '.join(parts)} WHERE user_id = ?",
                params,
            )
            self.conn.commit()
            return True
        except Exception:
            logger.error("suspected_bot 更新失败", exc_info=True)
            return False

    def list_suspected_bots(
        self, status: str = "", limit: int = 50,
    ) -> list[dict]:
        """列出疑似 Bot 列表 (供前端展示)。

        Args:
            status: 过滤状态 — ""=全部, "flagged"=待审核, "confirmed"=确认, "false_positive"=误判
            limit: 最多返回条数

        Returns:
            [{user_id, user_name, group_id, suspicion_score, marked_by,
              status, notes, created_at, updated_at}]
        """
        try:
            if status:
                rows = self.conn.execute(
                    """SELECT * FROM suspected_bots WHERE status = ?
                       ORDER BY suspicion_score DESC LIMIT ?""",
                    (status, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM suspected_bots ORDER BY suspicion_score DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            logger.error("suspected_bot 查询失败", exc_info=True)
            return []

    def get_suspected_bot(self, user_id: str) -> dict | None:
        """查询单个用户的疑似 Bot 标记。"""
        try:
            row = self.conn.execute(
                "SELECT * FROM suspected_bots WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    # ── 群聊总结 ──────────────────────────────────────────

    def get_latest_group_summary(self, group_id: int) -> dict | None:
        """获取某群最新一条总结。"""
        try:
            row = self.conn.execute(
                "SELECT * FROM group_summaries WHERE group_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (group_id,),
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def get_group_summary_history(
        self, group_id: int, limit: int = 20,
    ) -> list[dict]:
        """获取某群历史总结列表 (最新在前)。"""
        try:
            rows = self.conn.execute(
                "SELECT * FROM group_summaries WHERE group_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_summary_groups(self) -> list[dict]:
        """获取所有有总结的群 (去重)。"""
        try:
            rows = self.conn.execute(
                "SELECT DISTINCT group_id, MAX(created_at) as latest_summary "
                "FROM group_summaries GROUP BY group_id "
                "ORDER BY latest_summary DESC",
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


# ── 全局单例 ──────────────────────────────────────────────

_global_db: BotDatabase | None = None


def get_bot_db() -> BotDatabase:
    """获取全局 BotDatabase 单例。"""
    global _global_db
    if _global_db is None:
        _global_db = BotDatabase()
        _global_db.init()
    return _global_db
