"""只读读取 L-Port SQLite 中的 LLM/VLM 配置。

⚠️ 已弃用: 此模块仅保留用于 bot_db._migrate_llm_configs() 的一次性迁移。
   运行时 LLM 配置请从 ..suli_tavern.bot_db 导入 LLMConfigRO 并使用
   BotDatabase.list_llm_configs() / get_llm_config() 等方法。

安全约束:
  - SQLite URI mode=ro: 操作系统级只读, INSERT/UPDATE/DELETE 直接报错
  - 纯 stdlib sqlite3, 零 ORM 依赖
  - api_key 仅在内存中, 调用方负责不外泄

与 L-Port 的权威定义保持同步:
  - VLM_PROVIDERS / LOCAL_PROVIDERS 来自 llm_config_service.py:9-11
  - 表结构来自 config_model.py:7-23
"""

from __future__ import annotations

import sqlite3
import os
from dataclasses import dataclass
from typing import Optional, List


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

    @property
    def is_vlm(self) -> bool:
        return self.provider in VLM_PROVIDERS

    @property
    def is_local(self) -> bool:
        return self.provider in LOCAL_PROVIDERS

    @property
    def is_llm(self) -> bool:
        """云端 LLM (排除 VLM 和本地)"""
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
        if url.endswith("/v1"):
            url = url[: -len("/v1")]
        return url.rstrip("/") + "/v1"


class LPortConfigReader:
    """L-Port 配置只读访问器。

    用法:
        reader = LPortConfigReader("/path/to/lport_desktop.db")
        deepseek = reader.get_active_llm()       # 当前激活的云端 LLM
        vlm = reader.get_active_vlm()            # 当前激活的 VLM
        all_cfgs = reader.list_all()             # 全部配置
        one = reader.get_by_id(3)                # 按 ID 查询
    """

    def __init__(self, db_path: str):
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"L-Port 数据库不存在: {db_path}")
        # file: URI + mode=ro → OS 级只读, 写操作触发 OperationalError
        self._db_uri = f"file:{db_path}?mode=ro"

    # ── 内部 ──────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_config(row: sqlite3.Row) -> LLMConfigRO:
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
        )

    # ── 查询接口 ──────────────────────────────────────────

    def list_all(self) -> List[LLMConfigRO]:
        """列出全部 LLM/VLM 配置"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM llm_config ORDER BY id").fetchall()
            return [self._row_to_config(r) for r in rows]

    def get_by_id(self, config_id: int) -> Optional[LLMConfigRO]:
        """按 ID 查询"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM llm_config WHERE id = ?", (config_id,)
            ).fetchone()
            return self._row_to_config(row) if row else None

    def get_active_llm(self) -> Optional[LLMConfigRO]:
        """获取当前激活的云端 LLM 配置。

        过滤逻辑与 L-Port settings_llm.py:18 一致:
        包含 provider 不在 VLM_PROVIDERS 中的, 或 provider 在 LOCAL_PROVIDERS 中的。
        即: 排除纯云端 VLM (gpt4v/claude/gemini/nano_banana)。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM llm_config WHERE is_active = 1 ORDER BY id"
            ).fetchall()
            for r in rows:
                cfg = self._row_to_config(r)
                if cfg.provider not in VLM_PROVIDERS or cfg.provider in LOCAL_PROVIDERS:
                    return cfg
            return None

    def get_active_vlm(self) -> Optional[LLMConfigRO]:
        """获取当前激活的 VLM 配置"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM llm_config WHERE is_active = 1 ORDER BY id"
            ).fetchall()
            for r in rows:
                cfg = self._row_to_config(r)
                if cfg.provider in VLM_PROVIDERS and cfg.provider not in LOCAL_PROVIDERS:
                    return cfg
            return None

    def list_llm(self) -> List[LLMConfigRO]:
        """列出所有云端 LLM 配置 (非 VLM)"""
        all_cfgs = self.list_all()
        return [c for c in all_cfgs if c.provider not in VLM_PROVIDERS or c.provider in LOCAL_PROVIDERS]
