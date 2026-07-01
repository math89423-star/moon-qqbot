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
    vlm_resize_max_dim: int | None
    token_budget_cap: int | None
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
    bot_id        TEXT NOT NULL DEFAULT '',
    fact_key      TEXT NOT NULL,
    fact_value    TEXT NOT NULL,
    category      TEXT NOT NULL DEFAULT '其他',
    importance    REAL NOT NULL DEFAULT 0.5,
    created_at    REAL NOT NULL,
    last_accessed REAL NOT NULL,
    UNIQUE(user_id, bot_id, fact_key)
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

_MIGRATE_TOKEN_BOT_ID = """
ALTER TABLE token_usage ADD COLUMN bot_id TEXT NOT NULL DEFAULT ''
"""

_IDX_TOKEN_BOT_ID = """
CREATE INDEX IF NOT EXISTS idx_token_usage_bot_id
ON token_usage(bot_id, timestamp)
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

_TABLE_BOT_EXPERIENCES = """
CREATE TABLE IF NOT EXISTS bot_experiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    event TEXT NOT NULL,
    ts REAL NOT NULL,
    source_group INTEGER,
    participants TEXT DEFAULT '[]',
    valence_at_time REAL DEFAULT 0.0,
    importance REAL DEFAULT 1.0,
    message_range_start INTEGER DEFAULT 0,
    message_range_end INTEGER DEFAULT 0
)
"""

_IDX_BOT_EXPERIENCES = """
CREATE INDEX IF NOT EXISTS idx_bot_experiences_bot_ts
ON bot_experiences(bot_id, ts DESC)
"""

# ── Bot 间协调表 (ADR-001: 双实例拆分) ──
_TABLE_BOT_COORDINATION = """
CREATE TABLE IF NOT EXISTS bot_coordination (
    group_id          TEXT NOT NULL PRIMARY KEY,
    token_holder      TEXT NOT NULL DEFAULT '',
    token_acquired_at REAL NOT NULL DEFAULT 0,
    token_expires_at  REAL NOT NULL DEFAULT 0,
    last_reply_at     REAL NOT NULL DEFAULT 0,
    last_reply_bot    TEXT NOT NULL DEFAULT '',
    reply_target      TEXT NOT NULL DEFAULT '',
    extra             TEXT NOT NULL DEFAULT '{}'
)
"""

_IDX_BOT_COORDINATION_TOKEN = """
CREATE INDEX IF NOT EXISTS idx_bot_coordination_token
ON bot_coordination(token_holder, token_expires_at)
"""

_TABLE_BOT_IDENTITY = """
CREATE TABLE IF NOT EXISTS bot_identity (
    bot_id         TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    character_card TEXT NOT NULL DEFAULT '',
    nicknames      TEXT NOT NULL DEFAULT '[]',
    is_active      INTEGER NOT NULL DEFAULT 1,
    peer_bot_ids   TEXT NOT NULL DEFAULT '[]',
    metadata       TEXT NOT NULL DEFAULT '{}',
    created_at     REAL NOT NULL DEFAULT (julianday('now')),
    updated_at     REAL NOT NULL DEFAULT (julianday('now'))
)"""

_TABLE_BOT_IDENTITY_META = """
CREATE TABLE IF NOT EXISTS bot_identity_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)"""

# ── 默认配置值 ────────────────────────────────────────────

_DEFAULT_CONFIGS: dict[str, str] = {
    "admin_token": "",
    "active_llm_id": "",
    "active_vlm_id": "",
    "temperature_tavern_group": "0.8",
    "temperature_memory_extract": "0.2",
    "temperature_context_compress": "0.3",
    "temperature_cross_validation": "0.1",
}

# ── 迁移标记键 ────────────────────────────────────────────

_MIGRATED_JSON_KEY = "_migratedjson_memories"
_MIGRATED_KB_KEY = "_migrated_knowledge_base"
_MIGRATED_LLM_KEY = "_migrated_llm_configs"
_DELETED_PROVIDERS_KEY = "_deleted_synced_providers"  # JSON array of "provider|model_name"


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
        self._migrate_bot_id_column()    # 2026-06-30: per-bot 记忆隔离
        self._migratejson_memories()
        self._migrate_knowledge_base()
        self._migrate_llm_configs()
        self._sync_astrbot_providers()

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
            _TABLE_BOT_EXPERIENCES,
            _IDX_MEM_USER,
            _IDX_MEM_CATEGORY,
            _IDX_KB_SOURCE,
            _IDX_TOKEN_TIME,
            _IDX_TOKEN_SCENARIO,
            _IDX_TOKEN_BOT_ID,
            _MIGRATE_TOKEN_BOT_ID,
            _IDX_SUSPECT_BOT_USER,
            _IDX_SUSPECT_BOT_STATUS,
            _IDX_GROUP_SUMMARIES,
            _IDX_BOT_EXPERIENCES,
            _TABLE_BOT_COORDINATION,
            _IDX_BOT_COORDINATION_TOKEN,
            _TABLE_BOT_IDENTITY,
            _TABLE_BOT_IDENTITY_META,
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

    # ── bot_id 列迁移 (2026-06-30: per-bot 记忆隔离) ──────

    def _migrate_bot_id_column(self) -> None:
        """为 user_memories 表添加 bot_id 列 (如果不存在)。

        已有数据尝试从 bot_identity 推断归属，无法推断的归入主 bot。
        """
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(user_memories)").fetchall()}
        if "bot_id" in cols:
            return  # 已迁移

        logger.info("迁移: 为 user_memories 添加 bot_id 列...")
        self.conn.execute("ALTER TABLE user_memories ADD COLUMN bot_id TEXT NOT NULL DEFAULT ''")

        # 尝试从 bot_identity 表获取已知 bot QQ 号
        known_bots: list[str] = []
        try:
            rows = self.conn.execute(
                "SELECT bot_id FROM bot_identity"
            ).fetchall()
            known_bots = [r["bot_id"] for r in rows if r["bot_id"]]
        except Exception:
            pass

        main_bot = known_bots[0] if known_bots else "BOT_QQ_MAIN"

        # 现有数据默认归入主 bot
        self.conn.execute(
            "UPDATE user_memories SET bot_id = ? WHERE bot_id = ''",
            (main_bot,),
        )
        self.conn.commit()
        logger.info(
            "迁移完成: bot_id 列已添加, %d 条记录归入 %s",
            self.conn.execute("SELECT COUNT(*) FROM user_memories").fetchone()[0],
            main_bot,
        )

    # ── JSON → DB 迁移 ────────────────────────────────────

    def _migratejson_memories(self) -> None:
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
            tokensjson = json.dumps(
                sorted(sec.tokens) if sec.tokens else [],
                ensure_ascii=False,
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO knowledge_sections "
                "(title, content, source, tokens, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sec.title, sec.content, sec.source, tokensjson, now),
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

    def _sync_astrbot_providers(self) -> None:
        """将 AstrBot 注册的 provider 同步到 llm_config 表。

        每次启动运行，幂等：按 provider + model_name 去重。
        这样 WebUI 配置面板可以直接列出 AstrBot 中配置的模型。
        """
        cmd_config_path = "/AstrBot/data/cmd_config.json"
        if not os.path.exists(cmd_config_path):
            logger.info("AstrBot cmd_config.json 不存在，跳过 provider 同步")
            return

        try:
            with open(cmd_config_path, encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception:
            logger.warning("无法读取 AstrBot cmd_config.json", exc_info=True)
            return

        provider_entries = cfg.get("provider", [])
        if not provider_entries:
            logger.info("AstrBot provider 列表为空，跳过同步")
            return

        # 构建 source 查找表: provider_source_id → {key, api_base}
        source_map: dict[str, dict] = {}
        for src in cfg.get("provider_sources", []):
            sid = src.get("id") or src.get("provider", "")
            if sid and src.get("key"):
                source_map[str(sid)] = src

        # 已删除的 provider 集合 (用户明确删除的，不再同步)
        deleted_raw = self.get_config(_DELETED_PROVIDERS_KEY, "[]")
        deleted_providers: set[str] = set()
        try:
            deleted_list = json.loads(deleted_raw)
            if isinstance(deleted_list, list):
                deleted_providers = set(deleted_list)
        except Exception:
            pass

        # 已有配置的 (provider, model_name, config_type) 集合，用于去重。
        # config_type 必须包含在内: 同一 provider+model 可能同时有 LLM 和 VLM 两条，
        # 只用 (provider, model_name) 会导致 VLM 条目每次重启都被重复插入。
        existing = set()
        for row in self.conn.execute(
            "SELECT provider, model_name, config_type FROM llm_config"
        ).fetchall():
            existing.add((row["provider"], row["model_name"], row["config_type"]))

        now = time.time()
        synced = 0

        for prov in provider_entries:
            if not prov.get("enable", True):
                continue

            # provider 条目: id="deepseek/deepseek-v4-flash", model="deepseek-v4-flash"
            provider_id = str(prov.get("id") or "")
            model_name = str(prov.get("model") or "")
            provider_source_id = str(prov.get("provider_source_id") or "")
            modalities = prov.get("modalities", [])
            if not provider_id or not model_name:
                continue

            # 从 source 获取凭证
            source = source_map.get(provider_source_id, {})
            _key_raw = source.get("key", "")
            if isinstance(_key_raw, list):
                _key_raw = _key_raw[0] if _key_raw else ""
            api_key = str(_key_raw).strip()
            api_base = str(source.get("api_base", ""))
            if not api_key or not api_base:
                logger.debug("跳过无凭证的 provider: %s (source=%s)", provider_id, provider_source_id)
                continue

            # provider_name: 用 provider_source_id (deepseek/openai/anthropic)
            provider_name = provider_source_id

            # ── LLM 条目 (所有 text-capable provider) ──
            llm_key = (provider_id, model_name, "llm")
            llm_deleted_key = f"{provider_id}|{model_name}"
            if llm_key not in existing and llm_deleted_key not in deleted_providers:
                self.conn.execute(
                    "INSERT INTO llm_config "
                    "(name, provider, provider_name, api_key, base_url, model_name, "
                    " is_active, config_type, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1, 'llm', ?, ?)",
                    (f"{provider_id}/{model_name}", provider_id, provider_name,
                     api_key, api_base, model_name, now, now),
                )
                existing.add(llm_key)
                synced += 1

            # ── VLM 条目 (image-capable provider) ──
            if "image" in modalities:
                vlm_key = (provider_id, model_name, "vlm")
                vlm_deleted_key = f"{provider_id}|{model_name}_vlm"
                if vlm_key not in existing and vlm_deleted_key not in deleted_providers:
                    self.conn.execute(
                        "INSERT INTO llm_config "
                        "(name, provider, provider_name, api_key, base_url, model_name, "
                        " is_active, config_type, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 1, 'vlm', ?, ?)",
                        (f"{provider_id}/{model_name}(VLM)", provider_id, provider_name,
                         api_key, api_base, model_name, now, now),
                    )
                    existing.add(vlm_key)
                    synced += 1

        if synced:
            self.conn.commit()
            logger.info("从 AstrBot provider 同步了 %d 条 LLM/VLM 配置", synced)

        # 修复历史 bug: 之前 VLM 条目 model_name 与 LLM 相同，导致
        # (provider, model_name) 去重 key 冲突，每次重启都会插入重复 VLM。
        # 清理策略: 每组 (provider, model_name, config_type='vlm') 只保留
        # 最低 ID 的条目，更新槽位引用，删除其余。
        self._dedup_vlm_entries()

    def _dedup_vlm_entries(self) -> None:
        """删除重复的 VLM 条目（同一 provider+model_name 只保留最早的一条）。

        同时更新 bot_config 中的槽位引用，避免指向被删除的 ID。
        这是对 _sync_astrbot_providers 历史 bug 的一次性修复。
        """
        # 找出所有 config_type='vlm' 的条目，按 (provider, model_name) 分组
        rows = self.conn.execute(
            "SELECT id, provider, model_name FROM llm_config "
            "WHERE config_type = 'vlm' ORDER BY id"
        ).fetchall()

        groups: dict[tuple[str, str], list[int]] = {}
        for r in rows:
            key = (r["provider"], r["model_name"])
            groups.setdefault(key, []).append(r["id"])

        duplicate_ids: set[int] = set()
        id_remap: dict[int, int] = {}  # old_id → kept_id
        for _key, ids in groups.items():
            if len(ids) > 1:
                kept = ids[0]
                for dup_id in ids[1:]:
                    duplicate_ids.add(dup_id)
                    id_remap[dup_id] = kept

        if not duplicate_ids:
            return  # 无重复，无需清理

        logger.warning(
            "发现 %d 个重复 VLM 条目 (历史 bug 残留)，将保留最早条目并更新引用",
            len(duplicate_ids),
        )

        # 更新 bot_config 中所有引用被删除 ID 的槽位
        # 槽位 key 格式: bot:<QQ>:vlm_primary, bot:<QQ>:vlm_secondary, active_vlm_id
        for old_id, new_id in id_remap.items():
            old_val = str(old_id)
            new_val = str(new_id)
            # 更新所有包含该 ID 的配置键
            updated = self.conn.execute(
                "UPDATE bot_config SET value = ? WHERE value = ?",
                (new_val, old_val),
            ).rowcount
            if updated:
                logger.info("VLM 槽位引用更新: %d → %d (%d 处)", old_id, new_id, updated)

        # 删除重复条目
        placeholders = ",".join("?" * len(duplicate_ids))
        self.conn.execute(
            f"DELETE FROM llm_config WHERE id IN ({placeholders})",
            tuple(duplicate_ids),
        )
        self.conn.commit()
        logger.warning("已删除 %d 个重复 VLM 条目", len(duplicate_ids))

    # ── 配置 CRUD ─────────────────────────────────────────

    def get_config(self, key: str, default: str = "") -> str:
        """读取单个配置值。"""
        row = self.conn.execute(
            "SELECT value FROM bot_config WHERE key = ?", (key,),
        ).fetchone()
        return row["value"] if row else default

    def get_token_budget_config(self) -> dict:
        """读取 per-bot token 预算配置（返回 token 数，非 M）。"""
        def _read_m(key: str, default_m: float) -> int:
            raw = self.get_config(key, "")
            try:
                return int(float(raw) * 1_000_000) if raw else int(default_m * 1_000_000)
            except (ValueError, TypeError):
                return int(default_m * 1_000_000)

        return {
            "moon_hard_limit": _read_m("daily_token_limit_moon_m", 3.0),
            "moon_soft_limit": _read_m("daily_token_soft_limit_moon_m", 2.4),
            "_hard_limit": _read_m("daily_token_limit__m", 3.0),
            "_soft_limit": _read_m("daily_token_soft_limit__m", 2.4),
        }

    def set_token_budget_config(self, data: dict) -> None:
        """保存 per-bot token 预算配置（前端传 M 为单位）。"""
        for key in (
            "daily_token_limit_moon_m",
            "daily_token_soft_limit_moon_m",
            "daily_token_limit__m",
            "daily_token_soft_limit__m",
        ):
            val = data.get(key)
            if val is not None:
                self.set_config(key, str(float(val)))

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
        self, page: int = 1, per_page: int = 20, bot_id: str = "",
    ) -> tuple[list[dict], int]:
        """分页获取有记忆的用户列表 (可按 bot_id 过滤)。"""
        offset = (page - 1) * per_page
        if bot_id:
            total_row = self.conn.execute(
                "SELECT COUNT(DISTINCT user_id) AS cnt FROM user_memories WHERE bot_id = ?",
                (bot_id,),
            ).fetchone()
            total = total_row["cnt"] if total_row else 0
            rows = self.conn.execute(
                "SELECT user_id, user_name, COUNT(*) AS fact_count, "
                "MAX(last_accessed) AS last_active "
                "FROM user_memories WHERE bot_id = ? "
                "GROUP BY user_id "
                "ORDER BY last_active DESC "
                "LIMIT ? OFFSET ?",
                (bot_id, per_page, offset),
            ).fetchall()
        else:
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
        bot_id: str = "",
    ) -> tuple[list[dict], int]:
        """获取某用户的记忆列表 (可按 bot_id 过滤)。"""
        offset = (page - 1) * per_page
        if bot_id:
            total_row = self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM user_memories WHERE user_id = ? AND bot_id = ?",
                (user_id, bot_id),
            ).fetchone()
            total = total_row["cnt"] if total_row else 0
            rows = self.conn.execute(
                "SELECT * FROM user_memories WHERE user_id = ? AND bot_id = ? "
                "ORDER BY importance DESC, last_accessed DESC "
                "LIMIT ? OFFSET ?",
                (user_id, bot_id, per_page, offset),
            ).fetchall()
        else:
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

    def delete_user_fact(self, user_id: str, fact_key: str, bot_id: str = "") -> bool:
        """删除一条用户记忆 (可按 bot_id 限定范围)。返回是否成功。"""
        if bot_id:
            cur = self.conn.execute(
                "DELETE FROM user_memories WHERE user_id = ? AND fact_key = ? AND bot_id = ?",
                (user_id, fact_key, bot_id),
            )
        else:
            cur = self.conn.execute(
                "DELETE FROM user_memories WHERE user_id = ? AND fact_key = ?",
                (user_id, fact_key),
            )
        self.conn.commit()
        return cur.rowcount > 0

    def search_user_memories(
        self, query: str, top_n: int = 20, bot_id: str = "",
    ) -> list[dict]:
        """跨用户搜索记忆 (按 fact_value LIKE 匹配, 可按 bot_id 过滤)。"""
        if bot_id:
            rows = self.conn.execute(
                "SELECT * FROM user_memories WHERE fact_value LIKE ? AND bot_id = ? "
                "ORDER BY importance DESC LIMIT ?",
                (f"%{query}%", bot_id, top_n),
            ).fetchall()
        else:
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

    def list_llm_configs(self) -> list[LLMConfigRO]:
        """列出全部 LLM/VLM 配置。"""
        rows = self.conn.execute(
            "SELECT * FROM llm_config ORDER BY id",
        ).fetchall()
        return [self._row_to_llm_config(r) for r in rows]

    def get_llm_config(self, config_id: int) -> LLMConfigRO | None:
        """按 ID 查询 LLM 配置。"""
        row = self.conn.execute(
            "SELECT * FROM llm_config WHERE id = ?", (config_id,),
        ).fetchone()
        return self._row_to_llm_config(row) if row else None

    # ── cmd_config.json 同步 ─────────────────────────────
    # AstrBot 核心从 cmd_config.json 的 provider_sources 段读取 API key。
    # 暮恩前端保存到 bot_db 后必须同步写回 cmd_config.json，
    # 否则 AstrBot 核心看到的仍是旧 key → 401 Authentication Fails。

    _CMD_CONFIG_PATH = Path("data") / "cmd_config.json"

    def _sync_provider_to_cmd_config(
        self, provider: str, api_key: str, base_url: str = "",
    ) -> None:
        """将 provider 的 key/base_url 同步写入 cmd_config.json 的 provider_sources。

        仅更新已有条目；不存在的 provider 会新建最小条目。
        """
        if not provider:
            return
        try:
            cfg_path = self._CMD_CONFIG_PATH
            if not cfg_path.exists():
                logger.debug("cmd_config.json 不存在，跳过同步")
                return

            raw = cfg_path.read_text(encoding="utf-8")
            cfg = json.loads(raw)
            sources: list[dict] = cfg.get("provider_sources", [])

            # 查找匹配的 provider_source (按 id 或 provider 字段匹配)
            matched = None
            for src in sources:
                if src.get("id") == provider or src.get("provider") == provider:
                    matched = src
                    break

            if matched is not None:
                # 已有条目 → 更新 key 和 base_url
                matched["key"] = [api_key] if api_key else []
                if base_url:
                    matched["api_base"] = base_url
                logger.info("cmd_config.json: 已同步 provider=%s", provider)
            elif api_key:
                # 新 provider → 创建最小条目
                new_src = {
                    "provider": provider,
                    "type": "openai_chat_completion",
                    "provider_type": "chat_completion",
                    "key": [api_key],
                    "api_base": base_url or "",
                    "timeout": 120,
                    "proxy": "",
                    "custom_headers": {},
                    "id": provider,
                    "enable": True,
                }
                sources.append(new_src)
                cfg["provider_sources"] = sources
                logger.info("cmd_config.json: 新增 provider=%s", provider)

            cfg_path.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("cmd_config.json 同步失败: provider=%s error=%s", provider, type(e).__name__)

    def _remove_provider_from_cmd_config(self, provider: str) -> None:
        """从 cmd_config.json 的 provider_sources 中删除 provider (如果存在)。"""
        if not provider:
            return
        try:
            cfg_path = self._CMD_CONFIG_PATH
            if not cfg_path.exists():
                return

            raw = cfg_path.read_text(encoding="utf-8")
            cfg = json.loads(raw)
            sources: list[dict] = cfg.get("provider_sources", [])
            before = len(sources)
            cfg["provider_sources"] = [
                s for s in sources
                if s.get("id") != provider and s.get("provider") != provider
            ]
            if len(cfg["provider_sources"]) < before:
                cfg_path.write_text(
                    json.dumps(cfg, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info("cmd_config.json: 已删除 provider=%s", provider)
        except Exception:
            logger.warning("cmd_config.json 删除同步失败: provider=%s error=%s", provider, type(e).__name__)

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
        vlm_resize_max_dim: int | None = None,
        token_budget_cap: int | None = None,
        config_type: str = "",
    ) -> int:
        """新增 LLM 配置。返回新 ID。会自动同步到 cmd_config.json。"""
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
        # 同步到 cmd_config.json (AstrBot 核心从这里读 key)
        if api_key:
            self._sync_provider_to_cmd_config(provider, api_key, base_url)
        return cur.lastrowid

    def update_llm_config(self, config_id: int, **fields) -> bool:
        """更新 LLM 配置。只更新提供的字段。会自动同步到 cmd_config.json。"""
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
            # 同步到 cmd_config.json: 当 api_key/base_url/provider 变更时
            if "api_key" in updates or "base_url" in updates or "provider" in updates:
                row = self.conn.execute(
                    "SELECT provider, api_key, base_url FROM llm_config WHERE id = ?",
                    (config_id,),
                ).fetchone()
                if row:
                    self._sync_provider_to_cmd_config(
                        provider=row[0], api_key=row[1], base_url=row[2] or "",
                    )
        return ok

    def set_llm_config_active(self, config_id: int, active: bool) -> None:
        """设置 is_active 标志 (面板槽位分配/移除时同步)。"""
        self.conn.execute(
            "UPDATE llm_config SET is_active=?, updated_at=? WHERE id=?",
            (int(active), time.time(), config_id),
        )
        self.conn.commit()

    def is_config_assigned_to_any_slot(self, config_id: int) -> bool:
        """检查 config 是否被任一 bot 的任一槽位引用。"""
        cid_str = str(config_id)
        rows = self.conn.execute(
            "SELECT value FROM bot_config WHERE key LIKE 'bot:%:%'"
        ).fetchall()
        for (val,) in rows:
            if val == cid_str:
                return True
        return False

    def delete_llm_config(self, config_id: int) -> bool:
        """删除 LLM 配置。返回是否成功。会自动同步到 cmd_config.json。"""
        # 删除前先查出 provider + model_name + config_type
        row = self.conn.execute(
            "SELECT provider, model_name, config_type FROM llm_config WHERE id = ?", (config_id,),
        ).fetchone()
        deleted_provider = row["provider"] if row else ""
        if row:
            deleted_raw = self.get_config(_DELETED_PROVIDERS_KEY, "[]")
            try:
                deleted_list = json.loads(deleted_raw)
                if not isinstance(deleted_list, list):
                    deleted_list = []
            except Exception:
                deleted_list = []
            provider_key = f"{row['provider']}|{row['model_name']}"
            if row["config_type"] == "vlm":
                provider_key += "_vlm"
            if provider_key not in deleted_list:
                deleted_list.append(provider_key)
                self.set_config(_DELETED_PROVIDERS_KEY, json.dumps(deleted_list))
                logger.info("记录已删除 provider: %s", provider_key)

        cur = self.conn.execute(
            "DELETE FROM llm_config WHERE id = ?", (config_id,),
        )
        self.conn.commit()
        ok = cur.rowcount > 0
        if ok:
            logger.info("删除 LLM 配置: id=%d", config_id)
            # 同步: 检查是否还有其他配置使用同一 provider, 没有则从 cmd_config.json 移除
            if deleted_provider:
                remaining = self.conn.execute(
                    "SELECT COUNT(*) FROM llm_config WHERE provider = ?",
                    (deleted_provider,),
                ).fetchone()
                if remaining and remaining[0] == 0:
                    self._remove_provider_from_cmd_config(deleted_provider)
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
        bot_id: str = "",
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
            "(timestamp, scenario, user_id, group_id, model, provider, bot_id, "
            " input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now, scenario, user_id, group_id, model, provider, bot_id,
                input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens, latency_ms,
            ),
        )
        self.conn.commit()

    def get_token_stats(
        self, period: str = "today",
    ) -> dict:
        """获取 token 消耗统计（含 per-bot 拆分）。

        Args:
            period: "today" | "week" | "month" | "all"

        Returns:
            {input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens,
             request_count, estimated_cost_cny, by_scenario: {...}, by_bot: {...}}
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

        # 按 bot 分组 — per-bot 监控
        by_bot_rows = self.conn.execute(
            "SELECT bot_id, "
            "  COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "  COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "  COALESCE(SUM(cache_hit_tokens), 0) AS cache_hit_tokens, "
            "  COALESCE(SUM(cache_miss_tokens), 0) AS cache_miss_tokens, "
            "  COUNT(*) AS request_count "
            "FROM token_usage WHERE timestamp >= ? "
            "GROUP BY bot_id ORDER BY input_tokens DESC",
            (since,),
        ).fetchall()

        total_input = row["input_tokens"] if row else 0
        total_output = row["output_tokens"] if row else 0
        cache_hit = row["cache_hit_tokens"] if row else 0
        cache_miss = row["cache_miss_tokens"] if row else 0

        # ── 按 model 分组计算费用 (多模型差异化定价) ──
        cost_usd = self._compute_model_cost(since)

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
            "by_bot": [
                {
                    "bot_id": r["bot_id"] or "(未标记)",
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "cache_hit_tokens": r["cache_hit_tokens"],
                    "cache_miss_tokens": r["cache_miss_tokens"],
                    "request_count": r["request_count"],
                }
                for r in by_bot_rows
            ],
        }

    # ── 模型定价 (USD/1M tokens) ──────────────────────────
    # 基准: DeepSeek V4 Flash. 非 DeepSeek 模型用保守的近似价格.
    # 未列出的 model 按 provider 前缀推断, 未知 provider 默认 1.5x 基准.

    _PROVIDER_PRICE_MULT: dict[str, float] = {
        "deepseek": 1.0,
        "openai": 15.0,
        "claude": 25.0,
        "anthropic": 25.0,
        "gemini": 3.0,
    }

    _MODEL_PRICE_OVERRIDE: dict[str, float] = {
        # deepseek 系列 (实测价格)
        "deepseek-v4-flash": 1.0,
        "deepseek-v4-pro": 2.0,
        "deepseek-chat": 1.0,
        "deepseek-reasoner": 3.0,
        # GPT 系列 (近似, 含 image gen premium)
        "gpt-image-2": 20.0,
        "gpt-4o": 20.0,
        "gpt-4o-mini": 5.0,
        "gpt-5.4": 15.0,
        "gpt-5.4-mini": 8.0,
        "gpt-5.5": 15.0,
        # Opus 系列
        "claude-opus-4-8": 25.0,
        "opus-4-8": 25.0,
        # Gemini
        "gemini-3.1-flash-image": 4.0,
        "gemini-3.1-flash-lite-preview": 1.5,
    }

    @classmethod
    def _price_multiplier(cls, model: str, provider: str) -> float:
        """返回模型相对于 deepseek-flash 的定价倍率."""
        if model in cls._MODEL_PRICE_OVERRIDE:
            return cls._MODEL_PRICE_OVERRIDE[model]
        p = provider.lower()
        return cls._PROVIDER_PRICE_MULT.get(p, 1.5)

    def _compute_model_cost(self, since: float) -> float:
        """按 model 分组计算 USD 费用, 支持多模型差异化定价.

        处理两种情况:
        - DeepSeek 等上报 cache_hit/cache_miss → 精确分级计费
        - VLM/OpenAI 不上报缓存分解 → 输入全部按 cache_miss 计费
        """
        rows = self.conn.execute(
            "SELECT model, provider, "
            "  COALESCE(SUM(input_tokens), 0) AS it, "
            "  COALESCE(SUM(output_tokens), 0) AS ot, "
            "  COALESCE(SUM(cache_hit_tokens), 0) AS ch, "
            "  COALESCE(SUM(cache_miss_tokens), 0) AS cm "
            "FROM token_usage WHERE timestamp >= ? "
            "GROUP BY model, provider",
            (since,),
        ).fetchall()

        total_usd = 0.0
        for r in rows:
            model = r["model"] or "unknown"
            provider = r["provider"] or "unknown"
            ch = r["ch"]
            cm = r["cm"]
            ot = r["ot"]
            it = r["it"]
            mult = self._price_multiplier(model, provider)

            # 未报告缓存分解 → 全部输入按 cache_miss 计费 (OpenAI/VLM 常见)
            if ch + cm == 0:
                cm = it

            total_usd += (
                ch / 1_000_000 * 0.014 * mult
                + cm / 1_000_000 * 0.14 * mult
                + ot / 1_000_000 * 0.28 * mult
            )
        return total_usd

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


    # ── 群聊白名单 CRUD ─────────────────────────────────
    #
    # per-bot 白名单。JSON 结构从 {group_id: tier} 升级为
    # {bot_id: {group_id: tier}}。文件放在 shared_db/ 目录以确保
    # ADR-001 双实例容器间共享。

    _WHITELIST_PATH = Path("data") / "shared_db" / "group_chat_whitelist.json"

    def _read_whitelist_raw(self) -> dict:
        """读取原始白名单 JSON，自动处理旧格式迁移。

        返回 {bot_id: {group_id: tier}} 结构。
        旧格式 (list 或非嵌套 dict) 自动包装为 Moon bot 名下。
        若新路径 (shared_db/) 不存在但旧路径存在，自动迁移。
        """
        try:
            # 旧 → 新路径迁移
            _OLD_PATH = Path("data") / "group_chat_whitelist.json"
            if not self._WHITELIST_PATH.exists() and _OLD_PATH.exists():
                logger.info("检测到旧白名单文件，迁移到 shared_db/ 目录")
                self._WHITELIST_PATH.parent.mkdir(parents=True, exist_ok=True)
                old_data = json.loads(_OLD_PATH.read_text(encoding="utf-8"))
                if isinstance(old_data, list):
                    old_data = {str(g): "basic" for g in old_data}
                if isinstance(old_data, dict) and not isinstance(
                    next(iter(old_data.values()), None), dict
                ):
                    # 旧格式 → per-bot (暮恩名下)
                    old_data = {
                        "BOT_QQ_MAIN": {str(k): v for k, v in old_data.items()
                                       if v in ("basic", "full")}
                    }
                self._WHITELIST_PATH.write_text(
                    json.dumps(old_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                return old_data

            if not self._WHITELIST_PATH.exists():
                return {}
            data = json.loads(
                self._WHITELIST_PATH.read_text(encoding="utf-8")
            )
            if isinstance(data, list):
                # 旧格式: [123, 456] → 全部迁移为 Moon basic
                migrated = {str(g): "basic" for g in data}
                logger.info("白名单旧格式(list)→迁移到 Moon: %d 个群", len(data))
                return {"BOT_QQ_MAIN": migrated}
            if not isinstance(data, dict):
                return {}
            # 判断是否已为 per-bot 嵌套格式: 值是 dict → 新格式
            sample_val = next(iter(data.values()), None)
            if isinstance(sample_val, dict):
                return data  # 已是 per-bot 格式
            # 旧格式: {group_id: tier} → 包装为 Moon
            migrated = {
                int(k): v for k, v in data.items()
                if v in ("basic", "full")
            }
            logger.info("白名单旧格式(dict)→迁移到 Moon: %d 个群", len(migrated))
            return {"BOT_QQ_MAIN": {str(k): v for k, v in migrated.items()}}
        except (json.JSONDecodeError, ValueError, TypeError):
            logger.warning("白名单文件损坏，返回空列表", exc_info=True)
            return {}

    def _write_whitelist_raw(self, data: dict) -> None:
        """写入原始白名单 JSON (per-bot 嵌套格式)。"""
        self._WHITELIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._WHITELIST_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_whitelist(self, bot_id: str = "") -> dict[int, str]:
        """读取群聊白名单 {group_id: tier}。

        bot_id 为空时返回所有 bot 的合并结果 (向后兼容)，
        否则只返回指定 bot 的白名单。
        """
        raw = self._read_whitelist_raw()
        if bot_id:
            bot_entries = raw.get(str(bot_id), {})
            return {int(k): v for k, v in bot_entries.items() if v in ("basic", "full")}
        # 合并所有 bot
        result: dict[int, str] = {}
        for bot_entries in raw.values():
            if isinstance(bot_entries, dict):
                for k, v in bot_entries.items():
                    if v in ("basic", "full"):
                        result[int(k)] = v
        return result

    def get_all_bot_whitelists(self) -> dict[str, dict[int, str]]:
        """返回 per-bot 白名单全量: {bot_id: {group_id: tier}}。

        供前端展示用——展示每个 bot 各自的白名单。
        """
        raw = self._read_whitelist_raw()
        result: dict[str, dict[int, str]] = {}
        for bot_id, entries in raw.items():
            if isinstance(entries, dict):
                result[str(bot_id)] = {
                    int(k): v for k, v in entries.items()
                    if v in ("basic", "full")
                }
        return result

    def set_whitelist_entry(
        self, group_id: int, tier: str = "basic", bot_id: str = "",
    ) -> None:
        """添加/更新 per-bot 群聊白名单条目并持久化。

        bot_id 为空时默认写入 DEFAULT_BOT (BOT_QQ_MAIN)。
        """
        if tier not in ("basic", "full"):
            raise ValueError(f"无效的对话等级: {tier} (仅支持 basic/full)")
        bot_id = str(bot_id) if bot_id else "BOT_QQ_MAIN"
        raw = self._read_whitelist_raw()
        raw.setdefault(bot_id, {})[str(group_id)] = tier
        self._write_whitelist_raw(raw)
        logger.info("白名单更新: bot=%s group=%d tier=%s", bot_id, group_id, tier)

    def remove_whitelist_entry(self, group_id: int, bot_id: str = "") -> bool:
        """从 per-bot 白名单移除一个群。返回是否成功删除。

        bot_id 为空时默认从 DEFAULT_BOT (BOT_QQ_MAIN) 移除。
        """
        bot_id = str(bot_id) if bot_id else "BOT_QQ_MAIN"
        raw = self._read_whitelist_raw()
        bot_entries = raw.get(bot_id, {})
        gid_key = str(group_id)
        if gid_key not in bot_entries:
            return False
        del bot_entries[gid_key]
        if not bot_entries:
            del raw[bot_id]
        self._write_whitelist_raw(raw)
        logger.info("白名单删除: bot=%s group=%d", bot_id, group_id)
        return True

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

    # ── Bot 间协调 (ADR-001) ──

    def coordination_ensure_row(self, group_id: str) -> None:
        """确保某群在协调表中存在一行 (幂等)。"""
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO bot_coordination (group_id) VALUES (?)",
                (str(group_id),),
            )
            self.conn.commit()
        except Exception:
            pass

    def coordination_acquire_token(
        self, group_id: str, bot_id: str, ttl: float = 15.0, reply_target: str = "",
    ) -> bool:
        """原子获取发言权 token (SQLite UPDATE WHERE 过期/无持有者)。"""
        gid = str(group_id)
        now = time.time()
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO bot_coordination (group_id) VALUES (?)", (gid,),
            )
            result = self.conn.execute(
                "UPDATE bot_coordination "
                "SET token_holder = ?, token_acquired_at = ?, token_expires_at = ?, "
                "    last_reply_at = ?, last_reply_bot = ?, reply_target = ? "
                "WHERE group_id = ? "
                "  AND (token_holder = '' OR token_holder = ? OR token_expires_at < ?)",
                (bot_id, now, now + ttl, now, bot_id, reply_target,
                 gid, bot_id, now),
            )
            self.conn.commit()
            return result.rowcount > 0
        except Exception:
            return True  # 异常放行

    def coordination_release_token(self, group_id: str, bot_id: str) -> None:
        """释放发言权 token。"""
        try:
            self.conn.execute(
                "UPDATE bot_coordination SET token_holder = '', token_expires_at = 0 "
                "WHERE group_id = ? AND token_holder = ?",
                (str(group_id), bot_id),
            )
            self.conn.commit()
        except Exception:
            pass

    def coordination_is_peer_replying(self, group_id: str, my_bot_id: str) -> bool:
        """检查对方是否持有有效 token。"""
        now = time.time()
        try:
            row = self.conn.execute(
                "SELECT 1 FROM bot_coordination "
                "WHERE group_id = ? AND token_holder != '' AND token_holder != ? "
                "  AND token_expires_at > ?",
                (str(group_id), my_bot_id, now),
            ).fetchone()
            return row is not None
        except Exception:
            return False


    # ── Bot Identity CRUD ───────────────────────────────────

    def bot_identity_list(self, active_only: bool = False) -> list[dict]:
        """列出所有已注册 bot。"""
        try:
            if active_only:
                rows = self.conn.execute(
                    "SELECT * FROM bot_identity WHERE is_active = 1 ORDER BY created_at"
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM bot_identity ORDER BY created_at"
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def bot_identity_get(self, bot_id: str) -> dict | None:
        """获取单个 bot 身份。"""
        try:
            row = self.conn.execute(
                "SELECT * FROM bot_identity WHERE bot_id = ?", (str(bot_id),)
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def bot_identity_create(self, bot_id: str, name: str, character_card: str = "",
                            nicknames: str = "[]", peer_bot_ids: str = "[]",
                            metadata: str = "{}", is_active: bool = True) -> bool:
        """创建新 bot 身份 (bot_id 已存在时失败)。"""
        try:
            now = time.time()
            self.conn.execute(
                "INSERT INTO bot_identity (bot_id, name, character_card, nicknames, "
                "is_active, peer_bot_ids, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(bot_id), name, character_card, nicknames,
                 1 if is_active else 0, peer_bot_ids, metadata, now, now),
            )
            self.conn.commit()
            self._bump_identity_version()
            return True
        except sqlite3.IntegrityError:
            return False

    def bot_identity_update(self, bot_id: str, **kwargs) -> bool:
        """更新 bot 身份字段。kwargs 可选: name, character_card, nicknames,
        is_active, peer_bot_ids, metadata。"""
        if not kwargs:
            return False
        allowed = {"name", "character_card", "nicknames", "is_active",
                    "peer_bot_ids", "metadata"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        if "is_active" in updates:
            idx = list(updates.keys()).index("is_active")
            values[idx] = 1 if values[idx] else 0
        values.append(str(bot_id))
        try:
            now = time.time()
            self.conn.execute(
                f"UPDATE bot_identity SET {sets}, updated_at = ? WHERE bot_id = ?",
                values + [str(bot_id)] if "updated_at" not in updates else values,
            )
            # always set updated_at
            self.conn.execute(
                "UPDATE bot_identity SET updated_at = ? WHERE bot_id = ?",
                (now, str(bot_id)),
            )
            self.conn.commit()
            self._bump_identity_version()
            return True
        except Exception:
            return False

    def bot_identity_delete(self, bot_id: str) -> bool:
        """删除 bot 身份。"""
        try:
            self.conn.execute(
                "DELETE FROM bot_identity WHERE bot_id = ?", (str(bot_id),)
            )
            self.conn.commit()
            self._bump_identity_version()
            return True
        except Exception:
            return False

    def bot_identity_count(self) -> int:
        """返回已注册 bot 数量。"""
        try:
            row = self.conn.execute("SELECT COUNT(*) FROM bot_identity").fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    def _bump_identity_version(self) -> None:
        """递增 identity 缓存版本号，通知所有 BotIdentityService 消费者。"""
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO bot_identity_meta (key, value) VALUES ('version', "
                "CAST(COALESCE((SELECT value FROM bot_identity_meta WHERE key='version'), '0') AS INTEGER) + 1)"
            )
            self.conn.commit()
        except Exception:
            pass

    def bot_identity_version(self) -> int:
        """获取当前 identity 缓存版本号。"""
        try:
            row = self.conn.execute(
                "SELECT value FROM bot_identity_meta WHERE key = 'version'"
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

# ── 全局单例 ──────────────────────────────────────────────

_global_db: BotDatabase | None = None


def get_bot_db() -> BotDatabase:
    """获取全局 BotDatabase 单例。"""
    global _global_db
    if _global_db is None:
        _global_db = BotDatabase()
        _global_db.init()
    return _global_db
