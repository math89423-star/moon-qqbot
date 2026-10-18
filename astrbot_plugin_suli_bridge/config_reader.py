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
    """已禁用: 前端 6190 是唯一真相源，禁止从 L-Port 读取配置。

    保留类定义仅为向后兼容 — 所有查询返回空/None 并记录警告。
    """

    def __init__(self, db_path: str):
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "LPortConfigReader 已禁用: 前端 6190 为唯一真相源，拒绝读取 %s", db_path
        )
        # 保留 file: URI 但不再实际使用 — 后续查询直接返回空
        self._db_uri = f"file:{db_path}?mode=ro"
        self._disabled = True

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

    # ── 查询接口 — 已禁用 (前端唯一真相源) ──────────────
    def list_all(self) -> List[LLMConfigRO]:
        """已禁用: 前端为唯一来源，返回空列表。"""
        import logging as _logging
        _logging.getLogger(__name__).warning("LPortConfigReader.list_all 已禁用: 请通过前端 6190 配置")
        return []

    def get_by_id(self, config_id: int) -> Optional[LLMConfigRO]:
        import logging as _logging
        _logging.getLogger(__name__).warning("LPortConfigReader.get_by_id 已禁用")
        return None

    def get_active_llm(self) -> Optional[LLMConfigRO]:
        import logging as _logging
        _logging.getLogger(__name__).warning("LPortConfigReader.get_active_llm 已禁用")
        return None

    def get_active_vlm(self) -> Optional[LLMConfigRO]:
        import logging as _logging
        _logging.getLogger(__name__).warning("LPortConfigReader.get_active_vlm 已禁用")
        return None

    def list_llm(self) -> List[LLMConfigRO]:
        import logging as _logging
        _logging.getLogger(__name__).warning("LPortConfigReader.list_llm 已禁用")
        return []
