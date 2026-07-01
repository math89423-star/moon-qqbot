"""Bot 配置服务 — 封装 bot_db 的读写，提供类型化访问和默认值 fallback。

职责:
  - LLM/VLM 配置的运行时选择 (读/写)
  - 温度参数的集中管理 (取代硬编码)
  - Admin token 管理
  - 提供 get_xxx / set_xxx 类型化方法

所有读取操作都有 fallback: DB 无值 → 代码默认值
所有写入操作立即持久化到 SQLite
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .bot_db import get_bot_db

if TYPE_CHECKING:
    from .bot_db import BotDatabase, LLMConfigRO

logger = logging.getLogger(__name__)


class BotConfigService:
    """集中管理 bot 运行时配置。

    单例模式，持有 BotDatabase 引用。
    通过 get_* / set_* 方法提供类型化访问。
    """

    # ── 温度默认值 (与原来的硬编码值一致) ─────────────────

    _TEMP_DEFAULTS: dict[str, float] = {
        "temperature_bridge_chat": 0.7,
        "temperature_tavern_private": 0.9,
        "temperature_tavern_group": 0.7,
        "temperature_memory_extract": 0.2,
        "temperature_context_compress": 0.3,
        "temperature_cross_validation": 0.1,
    }

    def __init__(self, db: BotDatabase | None = None):
        self._db = db or get_bot_db()

    @property
    def db(self) -> BotDatabase:
        return self._db

    # ── Admin Token ───────────────────────────────────────

    @property
    def admin_token(self) -> str:
        return self._db.get_config("admin_token", "")

    def verify_token(self, token: str) -> bool:
        """验证 admin token。空 token 时拒绝所有请求。"""
        stored = self.admin_token
        if not stored or not token:
            return False
        # 常量时间比较防时序攻击
        return _constant_time_compare(stored, token)

    # ── LLM/VLM 选择 ─────────────────────────────────────

    def get_active_llm_id(self) -> int | None:
        """获取当前选用的 LLM config ID，无则返回 None。"""
        val = self._db.get_config("active_llm_id", "")
        return int(val) if val else None

    def set_active_llm_id(self, config_id: int) -> None:
        """设置当前选用的 LLM config ID。"""
        self._db.set_config("active_llm_id", str(config_id))
        logger.info("活跃 LLM 切换: config_id=%d", config_id)

    def get_active_vlm_id(self) -> int | None:
        """获取当前选用的 VLM config ID，无则返回 None。"""
        val = self._db.get_config("active_vlm_id", "")
        return int(val) if val else None

    def set_active_vlm_id(self, config_id: int) -> None:
        """设置当前选用的 VLM config ID。"""
        self._db.set_config("active_vlm_id", str(config_id))
        logger.info("活跃 VLM 切换: config_id=%d", config_id)

    def resolve_active_llm(self) -> LLMConfigRO | None:
        """解析当前活跃 LLM: 从 bot 自己的 llm_config 表查询。

        Returns:
            LLMConfigRO 或 None (无选择 / ID 已失效)
        """
        llm_id = self.get_active_llm_id()
        if llm_id is None:
            return None

        try:
            cfg = self._db.get_llm_config(llm_id)
            if cfg and cfg.is_llm:
                return cfg
            return None
        except Exception:
            logger.warning("活跃 LLM config_id=%d 无法解析", llm_id, exc_info=True)
            return None

    def resolve_active_vlm(self) -> LLMConfigRO | None:
        """解析当前活跃 VLM: 从 bot 自己的 llm_config 表查询。

        Returns:
            LLMConfigRO 或 None (无选择 / ID 已失效)
        """
        vlm_id = self.get_active_vlm_id()
        if vlm_id is None:
            return None

        try:
            cfg = self._db.get_llm_config(vlm_id)
            if cfg and cfg.is_vlm:
                return cfg
            return None
        except Exception:
            logger.warning("活跃 VLM config_id=%d 无法解析", vlm_id, exc_info=True)
            return None

    # ── 温度 ──────────────────────────────────────────────

    def get_temperature(self, scenario: str) -> float:
        """获取某个场景的 temperature。

        Args:
            scenario: 场景标识，如 'bridge_chat', 'tavern_private',
                      'tavern_group', 'memory_extract', 'context_compress',
                      'cross_validation'

        Returns:
            temperature 值 (float)
        """
        key = f"temperature_{scenario}"
        val = self._db.get_config(key, "")
        if val:
            try:
                return float(val)
            except ValueError:
                pass
        return self._TEMP_DEFAULTS.get(key, 0.7)

    def set_temperature(self, scenario: str, value: float) -> None:
        """设置某个场景的 temperature。"""
        key = f"temperature_{scenario}"
        clamped = max(0.0, min(2.0, value))
        self._db.set_config(key, str(clamped))
        logger.info("温度更新: %s = %.2f", scenario, clamped)

    def get_all_temperatures(self) -> dict[str, float]:
        """获取所有场景的 temperature 快照。"""
        result: dict[str, float] = {}
        for scenario in [
            "bridge_chat", "tavern_private", "tavern_group",
            "memory_extract", "context_compress", "cross_validation",
        ]:
            result[scenario] = self.get_temperature(scenario)
        return result

    def set_all_temperatures(self, temps: dict[str, float]) -> None:
        """批量设置 temperature。"""
        data: dict[str, str] = {}
        for scenario, value in temps.items():
            key = f"temperature_{scenario}"
            clamped = max(0.0, min(2.0, value))
            data[key] = str(clamped)
        self._db.set_many_configs(data)
        logger.info("批量温度更新: %s", temps)

    # ── 对话参数 (Chat Params) ─────────────────────────────

    # 默认值 (与 config.py + llm_client.py 硬编码一致)
    _CHAT_PARAM_DEFAULTS: dict[str, object] = {
        "bridge_chat_max_tokens": 2048,
        "group_chat_max_tokens": 96,
        "tavern_chat_max_tokens": 512,
        "bridge_chat_max_history": 30,
        "group_chat_compress_threshold": 20,
        "group_chat_compress_keep_recent": 10,
        "group_chat_debounce_seconds": 30,
        "group_chat_batch_size": 12,
        "group_chat_cooldown_seconds": 60,
        "group_chat_max_context": 30,
        "group_chat_nicknames": ["小暮", "暮暮", "洛宝", "暮恩", "moon"],
        "group_chat_talkativeness": 0.5,
        "proactive_enabled": False,
        "model_router_flash": "deepseek-v4-pro",
        "model_router_pro": "deepseek-v4-pro",
        "model_router_private_default_pro": True,
    }

    # 元数据: label / description / type / min / max / step / group
    _CHAT_PARAM_META: dict[str, dict] = {
        "bridge_chat_max_tokens": {
            "label": "Bridge Chat 最大 token",
            "desc": "/chat 对话的 LLM 单次回复最大 token 数",
            "type": "int", "min": 64, "max": 8192, "step": 64,
            "group": "回复控制",
        },
        "group_chat_max_tokens": {
            "label": "群聊回复最大 token",
            "desc": "群聊 LLM 单次回复最大 token 数 (闲聊基值96, 问答/指令自动192+)",
            "type": "int", "min": 32, "max": 1024, "step": 16,
            "group": "回复控制",
        },
        "tavern_chat_max_tokens": {
            "label": "私聊角色扮演最大 token",
            "desc": "私聊角色扮演 LLM 单次回复最大 token 数",
            "type": "int", "min": 64, "max": 2048, "step": 32,
            "group": "回复控制",
        },
        "bridge_chat_max_history": {
            "label": "Bridge Chat 最大历史",
            "desc": "Bridge Chat 会话最多保留多少条消息",
            "type": "int", "min": 4, "max": 100, "step": 2,
            "group": "上下文管理",
        },
        "group_chat_compress_threshold": {
            "label": "压缩触发阈值 (条)",
            "desc": "群聊上下文超过此消息数时触发 LLM 压缩",
            "type": "int", "min": 10, "max": 50, "step": 2,
            "group": "上下文管理",
        },
        "group_chat_compress_keep_recent": {
            "label": "压缩后保留 (条)",
            "desc": "上下文压缩后保留最近几条原文消息",
            "type": "int", "min": 5, "max": 30, "step": 1,
            "group": "上下文管理",
        },
        "group_chat_max_context": {
            "label": "最多上下文 (条)",
            "desc": "群聊上下文窗口中最多保留的消息条数 (硬上限)",
            "type": "int", "min": 5, "max": 100, "step": 5,
            "group": "上下文管理",
        },
        "group_chat_debounce_seconds": {
            "label": "Debounce 等待 (秒)",
            "desc": "群聊静默多少秒后触发 LLM 决策是否发言",
            "type": "int", "min": 5, "max": 300, "step": 5,
            "group": "群聊触发策略",
        },
        "group_chat_batch_size": {
            "label": "Batch 累积消息数",
            "desc": "累积多少条消息后立即触发 LLM 决策",
            "type": "int", "min": 2, "max": 50, "step": 1,
            "group": "群聊触发策略",
        },
        "group_chat_cooldown_seconds": {
            "label": "发言冷却 (秒)",
            "desc": "两次 bot 发言之间的最小间隔，防刷屏",
            "type": "int", "min": 10, "max": 600, "step": 10,
            "group": "群聊触发策略",
        },
        "group_chat_nicknames": {
            "label": "昵称唤醒列表",
            "desc": "消息中包含这些关键词时立即触发回复，逗号分隔",
            "type": "csv", "min": 0, "max": 0, "step": 0,
            "group": "其他",
        },
        "group_chat_talkativeness": {
            "label": "群聊活跃度 (Talkativeness)",
            "desc": "bot 未被点名时主动插话的概率: 0=仅回应点名(Shy), 0.5=适中(Default), 1=每次都回(Chatty)。点名/mention 无视此参数直接回复。SillyTavern 标准机制。",
            "type": "float", "min": 0.0, "max": 1.0, "step": 0.1,
            "group": "回复控制",
        },
        "proactive_enabled": {
            "label": "主动发言",
            "desc": "群聊冷场时 bot 是否主动开启话题 (需重启后生效)",
            "type": "bool", "min": 0, "max": 0, "step": 0,
            "group": "其他",
        },
        # ── 注意: reasoning_enabled 已从 chat params 移除，改用 per-bot 配置 ──
        "model_router_flash": {
            "label": "闲聊模型 (Flash)",
            "desc": "日常闲聊/水群使用的模型名，如 deepseek-v4-pro",
            "type": "str", "min": 0, "max": 0, "step": 0,
            "group": "模型路由",
        },
        "model_router_pro": {
            "label": "专业模型 (Pro)",
            "desc": "技术问答/推理场景使用的模型名，如 deepseek-v4-pro",
            "type": "str", "min": 0, "max": 0, "step": 0,
            "group": "模型路由",
        },
        # ── 注意: model_router_opus 已从 chat params 移除，改用 per-bot llm_opus 槽位 ──
        "model_router_private_default_pro": {
            "label": "私聊默认 Pro",
            "desc": "私聊是否默认使用专业模型 (关闭则私聊也用 Flash)",
            "type": "bool", "min": 0, "max": 0, "step": 0,
            "group": "模型路由",
        },
    }

    def get_chat_param(self, key: str) -> object:
        """读取对话参数, DB 优先, fallback 到默认值。

        Returns:
            int | float | bool | str | list[str] — 根据元数据自动转换类型
        """
        default = self._CHAT_PARAM_DEFAULTS.get(key)
        val = self._db.get_config(f"chat_param_{key}", "")
        meta = self._CHAT_PARAM_META.get(key, {})
        ptype = meta.get("type", "str")

        if val:
            try:
                if ptype == "int":
                    return int(val)
                if ptype == "float":
                    return float(val)
                if ptype == "bool":
                    return val.lower() in ("true", "1", "yes")
                if ptype == "csv":
                    return [s.strip() for s in val.split(",") if s.strip()]
            except (ValueError, TypeError):
                pass
        return default

    def set_chat_param(self, key: str, value: object) -> None:
        """写入对话参数到 bot_config 表。"""
        meta = self._CHAT_PARAM_META.get(key, {})
        ptype = meta.get("type", "str")

        if ptype == "csv":
            if isinstance(value, list):
                val_str = ", ".join(str(v) for v in value)
            else:
                val_str = str(value)
        elif ptype == "bool":
            val_str = "true" if value else "false"
        else:
            val_str = str(value)

        self._db.set_config(f"chat_param_{key}", val_str)
        logger.info("对话参数更新: %s = %s", key, val_str)

    def get_all_chat_params(self) -> list[dict]:
        """获取所有对话参数的当前值 + 元数据快照。

        Returns:
            [{key, value, label, desc, type, min, max, step, group}, ...]
        """
        result: list[dict] = []
        for key, meta in self._CHAT_PARAM_META.items():
            entry = dict(meta)
            entry["key"] = key
            entry["value"] = self.get_chat_param(key)
            result.append(entry)
        return result

    # ── 通用配置 ──────────────────────────────────────────

    def get(self, key: str, default: str = "") -> str:
        """读取任意配置键。"""
        return self._db.get_config(key, default)

    def set(self, key: str, value: str) -> None:
        """写入任意配置键。"""
        self._db.set_config(key, value)

    def get_all(self) -> dict[str, str]:
        """获取所有公开配置。"""
        return self._db.get_all_configs()


# ── 工具函数 ──────────────────────────────────────────────

def _constant_time_compare(a: str, b: str) -> bool:
    """常量时间字符串比较，防止时序攻击。"""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


# ── 全局单例 ──────────────────────────────────────────────

_global_config_service: BotConfigService | None = None


def get_config_service() -> BotConfigService:
    """获取全局 BotConfigService 单例。"""
    global _global_config_service
    if _global_config_service is None:
        _global_config_service = BotConfigService()
    return _global_config_service
