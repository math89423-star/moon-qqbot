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
        "temperature_tavern_group": 0.8,
        "temperature_memory_extract": 0.2,
        "temperature_context_compress": 0.3,
        "temperature_cross_validation": 0.1,
    }
    # 已移除 (2026-06-30):
    #   temperature_bridge_chat — Bridge 插件已移除
    #   temperature_tavern_private — 私聊硬编码温度，未读配置

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

    # ── Per-Bot 开关 ──────────────────────────────────────

    def is_group_chat_enabled(self, bot_id: str) -> bool:
        """检查指定 bot 的群聊开关。默认 true。"""
        key = self._bot_key(bot_id, "group_chat_enabled")
        val = self._db.get_config(key, "true")
        return val.lower() == "true"

    def is_private_chat_enabled(self, bot_id: str) -> bool:
        """检查指定 bot 的私聊开关。默认 true。"""
        key = self._bot_key(bot_id, "private_chat_enabled")
        val = self._db.get_config(key, "true")
        return val.lower() == "true"

    def is_reasoning_enabled(self, bot_id: str) -> bool:
        """检查指定 bot 的自适应深度思考开关。默认 True (开启)。"""
        key = self._bot_key(bot_id, "reasoning_enabled")
        val = self._db.get_config(key, "true")
        return val.lower() == "true"

    def set_reasoning_enabled(self, bot_id: str, enabled: bool) -> None:
        """设置指定 bot 的自适应深度思考开关。"""
        key = self._bot_key(bot_id, "reasoning_enabled")
        self._db.set_config(key, "true" if enabled else "false")
        logger.info("Bot %s reasoning_enabled = %s", bot_id, enabled)

    def is_gate_thinking_enabled(self, bot_id: str) -> bool:
        """意图门 LLM 是否启用 thinking 模式。默认 False（Gate 是分类器不需要深度推理，前端可按 bot 开启）。"""
        key = self._bot_key(bot_id, "gate_thinking_enabled")
        val = self._db.get_config(key, "false")
        return val.lower() == "true"

    def set_gate_thinking_enabled(self, bot_id: str, enabled: bool) -> None:
        """设置指定 bot 的意图门 thinking 开关。"""
        key = self._bot_key(bot_id, "gate_thinking_enabled")
        self._db.set_config(key, "true" if enabled else "false")
        logger.info("Bot %s gate_thinking_enabled = %s", bot_id, enabled)

    def resolve_background_llm(
        self, bot_id: str, purpose: str = "background",
    ) -> dict:
        """解析背景任务 LLM 配置 — 统一入口。

        背景任务 (总结/建档/记忆提取) 默认走 llm_lite 槽位,
        thinking 跟随 gate_thinking_enabled 开关。

        Returns:
            {"model": str, "api_base": str, "api_key": str, "extra_params": dict|None}
            解析失败返回空 dict (调用方 fallback 到 tavern 默认模型)
        """
        slot = self.resolve_llm_slot(bot_id, "llm_lite") \
            or self.resolve_llm_slot(bot_id, "llm_primary")
        if not slot:
            logger.debug("resolve_background_llm: bot=%s 无 llm_lite 槽位", bot_id)
            return {}

        extra = None
        if self.is_gate_thinking_enabled(bot_id):
            extra = {"thinking": {"type": "enabled"}}

        logger.debug(
            "resolve_background_llm: bot=%s purpose=%s model=%s thinking=%s",
            bot_id, purpose, slot.model_name, "Y" if extra else "N",
        )
        return {
            "model": slot.model_name,
            "api_base": slot.normalized_base_url,
            "api_key": slot.api_key,
            "extra_params": extra,
        }

    # ── LLM/VLM 选择 ─────────────────────────────────────

    @staticmethod
    def _bot_key(bot_id: str, key: str) -> str:
        """生成 per-bot 配置键。空 bot_id 返回全局键。"""
        return f"bot:{bot_id}:{key}" if bot_id else key

    def get_active_llm_id(self, bot_id: str = "") -> int | None:
        """获取当前选用的 LLM config ID。

        Args:
            bot_id: 指定 bot 的 QQ 号。为空则使用全局配置（向后兼容）。
        """
        key = self._bot_key(bot_id, "active_llm_id")
        val = self._db.get_config(key, "")
        if not val and bot_id:
            # fallback: 全局配置
            val = self._db.get_config("active_llm_id", "")
        return int(val) if val else None

    def set_active_llm_id(self, config_id: int, bot_id: str = "") -> None:
        """设置当前选用的 LLM config ID。"""
        key = self._bot_key(bot_id, "active_llm_id")
        self._db.set_config(key, str(config_id))
        logger.info("活跃 LLM 切换: config_id=%d bot=%s", config_id, bot_id or "全局")

    def get_active_vlm_id(self, bot_id: str = "") -> int | None:
        """获取当前选用的 VLM config ID。"""
        key = self._bot_key(bot_id, "active_vlm_id")
        val = self._db.get_config(key, "")
        if not val and bot_id:
            val = self._db.get_config("active_vlm_id", "")
        return int(val) if val else None

    def set_active_vlm_id(self, config_id: int, bot_id: str = "") -> None:
        """设置当前选用的 VLM config ID。"""
        key = self._bot_key(bot_id, "active_vlm_id")
        self._db.set_config(key, str(config_id))
        logger.info("活跃 VLM 切换: config_id=%d bot=%s", config_id, bot_id or "全局")

    def resolve_active_llm(self, bot_id: str = "") -> LLMConfigRO | None:
        """解析活跃 LLM 配置。

        Args:
            bot_id: 指定 bot 的 QQ 号。为空则使用全局配置。
        """
        llm_id = self.get_active_llm_id(bot_id)
        if llm_id is None:
            return None

        try:
            cfg = self._db.get_llm_config(llm_id)
            if cfg and cfg.is_llm and cfg.is_active:
                return cfg
            return None
        except Exception:
            logger.warning("活跃 LLM config_id=%d 无法解析", llm_id, exc_info=True)
            return None

    def resolve_active_vlm(self, bot_id: str = "") -> LLMConfigRO | None:
        """解析活跃 VLM 配置。"""
        vlm_id = self.get_active_vlm_id(bot_id)
        if vlm_id is None:
            return None

        try:
            cfg = self._db.get_llm_config(vlm_id)
            if cfg and cfg.is_vlm and cfg.is_active:
                return cfg
            return None
        except Exception:
            logger.warning("活跃 VLM config_id=%d 无法解析", vlm_id, exc_info=True)
            return None

    # ── LLM/VLM 槽位 (多模型支持) ───────────────────────

    LLM_SLOTS = ("llm_lite", "llm_pro", "llm_gate")  # 日常闲聊 / 推理增强 / 意图闸
    VLM_SLOTS = ("vlm_primary", "vlm_secondary")

    # ── 向后兼容: 旧槽名 → 新槽名 ──
    _SLOT_ALIASES: dict[str, str] = {
        "llm_primary": "llm_lite",
        "llm_secondary": "llm_pro",
    }

    # ── Per-bot 槽位差异 ──
    # 每个 bot 的 LLM 槽位由 bot_identity.metadata.llm_slots 字段定义。
    # 未配置时默认返回全部 4 个槽位。

    def get_display_llm_slots(self, bot_id: str) -> tuple[str, ...]:
        """返回该 bot 应展示的 LLM 槽位列表 (从 BotIdentity 读取)。"""
        try:
            from .bot_identity import get_bot_identity_service
            svc = get_bot_identity_service()
            slots = svc.get_llm_slots(bot_id)
            if slots:
                return slots
        except Exception:
            pass
        return self.LLM_SLOTS

    def get_llm_slot(self, bot_id: str, slot: str) -> int | None:
        """获取指定 bot 的 LLM 槽位 config_id。"""
        key = self._bot_key(bot_id, slot)
        val = self._db.get_config(key, "")
        return int(val) if val else None

    def set_llm_slot(self, bot_id: str, slot: str, config_id: int) -> None:
        """设置指定 bot 的 LLM 槽位。分配时自动 is_active=1, 清空时检查解绑。"""
        key = self._bot_key(bot_id, slot)
        self._db.set_config(key, str(config_id))
        self._sync_is_active_for_config(config_id)
        logger.info("LLM 槽位: bot=%s slot=%s config_id=%d", bot_id, slot, config_id)

    def _sync_is_active_for_config(self, config_id: int) -> None:
        """同步 is_active: 有槽位引用→True, 无引用→False。"""
        if config_id <= 0:
            return
        active = self._db.is_config_assigned_to_any_slot(config_id)
        self._db.set_llm_config_active(config_id, active)

    def resolve_llm_slot(self, bot_id: str, slot: str) -> LLMConfigRO | None:
        """解析指定 bot 的 LLM 槽位 → 完整配置。

        支持向后兼容: 新槽名查不到时 fallback 到旧槽名 (llm_primary→llm_lite 等)。
        """
        cfg_id = self.get_llm_slot(bot_id, slot)
        # ── 向后兼容: 旧名 fallback ──
        if cfg_id is None:
            compat_slot = self._SLOT_ALIASES.get(slot)
            if compat_slot:
                cfg_id = self.get_llm_slot(bot_id, compat_slot)
                if cfg_id is not None:
                    logger.debug(
                        "LLM 槽位 fallback: bot=%s %s→%s (旧名兼容)",
                        bot_id, slot, compat_slot,
                    )
        if cfg_id is None:
            return None
        try:
            cfg = self._db.get_llm_config(cfg_id)
            if cfg and cfg.is_llm and cfg.is_active:
                return cfg
        except Exception:
            logger.warning("LLM 槽位解析失败: bot=%s slot=%s id=%d", bot_id, slot, cfg_id, exc_info=True)
        return None

    def get_vlm_slot(self, bot_id: str, slot: str) -> int | None:
        """获取指定 bot 的 VLM 槽位 config_id。"""
        key = self._bot_key(bot_id, slot)
        val = self._db.get_config(key, "")
        return int(val) if val else None

    def set_vlm_slot(self, bot_id: str, slot: str, config_id: int) -> None:
        """设置指定 bot 的 VLM 槽位。分配时自动 is_active=1, 清空时检查解绑。"""
        key = self._bot_key(bot_id, slot)
        self._db.set_config(key, str(config_id))
        self._sync_is_active_for_config(config_id)
        logger.info("VLM 槽位: bot=%s slot=%s config_id=%d", bot_id, slot, config_id)

    def resolve_vlm_slot(self, bot_id: str, slot: str) -> LLMConfigRO | None:
        """解析指定 bot 的 VLM 槽位 → 完整配置。

        ⚠️ 必须同时检查 is_vlm 和 is_active。
        此前只检查 is_vlm → inactive 模型 (如 gpt-5.4-mini id=33) 仍被当作 VLM 主槽烧钱。
        """
        cfg_id = self.get_vlm_slot(bot_id, slot)
        if cfg_id is None:
            return None
        try:
            cfg = self._db.get_llm_config(cfg_id)
            if cfg and cfg.is_vlm and cfg.is_active:
                return cfg
        except Exception:
            logger.warning("VLM 槽位解析失败: bot=%s slot=%s id=%d", bot_id, slot, cfg_id, exc_info=True)
        return None

    # ── 闸门判断槽位 + 仲裁槽位 (per-bot, 复用通用 get/set_llm_slot) ──

    def get_temperature(self, bot_id: str, scenario: str) -> float:
        """获取指定 bot 的某个场景 temperature。

        Args:
            bot_id: bot QQ 号
            scenario: 场景标识，如 'bridge_chat', 'tavern_private',
                      'tavern_group', 'memory_extract', 'context_compress',
                      'cross_validation'

        Returns:
            temperature 值 (float), DB 无值时 fallback 到默认值
        """
        key = self._bot_key(bot_id, f"temperature_{scenario}")
        val = self._db.get_config(key, "")
        if val:
            try:
                return float(val)
            except ValueError:
                pass
        default_key = f"temperature_{scenario}"
        return self._TEMP_DEFAULTS.get(default_key, 0.7)

    def set_temperature(self, bot_id: str, scenario: str, value: float) -> None:
        """设置指定 bot 的某个场景 temperature。"""
        key = self._bot_key(bot_id, f"temperature_{scenario}")
        clamped = max(0.0, min(2.0, value))
        self._db.set_config(key, str(clamped))
        logger.info("温度更新: bot=%s %s = %.2f", bot_id, scenario, clamped)

    def get_all_temperatures(self, bot_id: str) -> dict[str, float]:
        """获取指定 bot 的所有场景 temperature 快照。"""
        result: dict[str, float] = {}
        for scenario in [
            "bridge_chat", "tavern_private", "tavern_group",
            "memory_extract", "context_compress", "cross_validation",
        ]:
            result[scenario] = self.get_temperature(bot_id, scenario)
        return result

    def set_all_temperatures(self, bot_id: str, temps: dict[str, float]) -> None:
        """批量设置指定 bot 的 temperature。"""
        for scenario, value in temps.items():
            self.set_temperature(bot_id, scenario, value)
        logger.info("批量温度更新: bot=%s %s", bot_id, temps)

    # ── 对话参数 (Chat Params) ─────────────────────────────

    # 默认值 (与 config.py + llm_client.py 硬编码一致)
    _CHAT_PARAM_DEFAULTS: dict[str, object] = {
        # ── 群聊回复控制 ──
        "group_chat_max_tokens": 128,
        "group_chat_talkativeness": 0.5,
        # ── 上下文压缩 ──
        "group_chat_compress_threshold": 20,
        "group_chat_compress_keep_recent": 10,
        # ── 群聊触发策略 ──
        "group_chat_debounce_seconds": 10,
        "group_chat_batch_size": 12,
        "group_chat_cooldown_seconds": 20,
    }

    # 已移除 (2026-06-30):
    #   bridge_*, tavern_chat_max_tokens, group_chat_max_context, proactive_enabled,
    #   group_chat_nicknames (→ BotIdentity), model_router_* (→ LLM 槽位)

    # 元数据: label / description / type / min / max / step / group
    _CHAT_PARAM_META: dict[str, dict] = {
        "group_chat_max_tokens": {
            "label": "群聊回复最大 token",
            "desc": "闲聊基值128, 问答/指令自动384+, 高级推理512+",
            "type": "int", "min": 32, "max": 1024, "step": 16,
            "group": "回复控制",
        },
        "group_chat_talkativeness": {
            "label": "群聊活跃度",
            "desc": "未被点名时主动插话概率: 0=仅回应点名, 0.5=适中, 1=每次都回",
            "type": "float", "min": 0.0, "max": 1.0, "step": 0.1,
            "group": "回复控制",
        },
        "group_chat_compress_threshold": {
            "label": "压缩触发阈值 (条)",
            "desc": "群聊上下文超过此消息数时触发 LLM 压缩",
            "type": "int", "min": 10, "max": 50, "step": 2,
            "group": "上下文压缩",
        },
        "group_chat_compress_keep_recent": {
            "label": "压缩后保留 (条)",
            "desc": "上下文压缩后保留最近几条原文消息",
            "type": "int", "min": 5, "max": 30, "step": 1,
            "group": "上下文压缩",
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
    }

    def get_chat_param(self, bot_id: str, key: str) -> object:
        """读取指定 bot 的对话参数, DB 优先, fallback 到默认值。

        Returns:
            int | float | bool | str | list[str] — 根据元数据自动转换类型
        """
        default = self._CHAT_PARAM_DEFAULTS.get(key)
        db_key = self._bot_key(bot_id, f"chat_param_{key}")
        val = self._db.get_config(db_key, "")
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

    def set_chat_param(self, bot_id: str, key: str, value: object) -> None:
        """写入指定 bot 的对话参数到 bot_config 表。"""
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

        db_key = self._bot_key(bot_id, f"chat_param_{key}")
        self._db.set_config(db_key, val_str)
        logger.info("对话参数更新: bot=%s %s = %s", bot_id, key, val_str)

    def get_all_chat_params(self, bot_id: str) -> list[dict]:
        """获取指定 bot 的所有对话参数的当前值 + 元数据快照。

        Returns:
            [{key, value, label, desc, type, min, max, step, group}, ...]
        """
        result: list[dict] = []
        for key, meta in self._CHAT_PARAM_META.items():
            entry = dict(meta)
            entry["key"] = key
            entry["value"] = self.get_chat_param(bot_id, key)
            result.append(entry)
        return result

    # ── 统一工具层 (per-bot, per-tool) ─────────────────────
    # 工具统一注册表

    _TOOL_DEFAULTS: dict[str, object] = {
        "tool_calling_enabled": True,
        "tool_call_max_rounds": 10,
        "tool_call_timeout": 10.0,
        "tool_min_affinity": 1,
        # ── 每日配额 (per-bot 可调) ──
        "daily_image_limit": 3,
        "daily_vlm_limit": 5,
        "daily_tools_base_limit": 10,
        # ── 冷却时间 (per-bot 可调) ──
        "draw_cooldown_seconds": 180,
        "tool_cooldown_seconds": 60,
    }

    _TOOL_META: dict[str, dict] = {
        "tool_calling_enabled": {
            "label": "启用工具调用",
            "desc": "是否允许 LLM function calling 工具系统",
            "type": "bool",
            "group": "开关",
        },
        "tool_call_max_rounds": {
            "label": "最大轮数",
            "desc": "LLM 最多执行几轮工具调用 (不含最终文本合成轮)",
            "type": "int", "min": 2, "max": 20, "step": 1,
            "group": "控制",
        },
        "tool_call_timeout": {
            "label": "超时时间",
            "desc": "单个工具调用的超时秒数",
            "type": "float", "min": 2.0, "max": 30.0, "step": 1.0,
            "group": "控制",
        },
        "tool_min_affinity": {
            "label": "最低好感度",
            "desc": "用户使用工具所需的最低好感等级 (0=陌生 1=普通 2=熟悉 3=喜欢 4=亲密 5=珍视)",
            "type": "int", "min": -2, "max": 5, "step": 1,
            "group": "门控",
        },
        "daily_image_limit": {
            "label": "每日生图限额",
            "desc": "每个用户每天最多生成几张图片 (管理员不受限)",
            "type": "int", "min": 1, "max": 20, "step": 1,
            "group": "每日配额",
        },
        "daily_vlm_limit": {
            "label": "每日识图限额",
            "desc": "每个用户每天最多使用几次 VLM 识图 (管理员不受限)",
            "type": "int", "min": 1, "max": 30, "step": 1,
            "group": "每日配额",
        },
        "daily_tools_base_limit": {
            "label": "每日工具基础限额",
            "desc": "好感 Lv.1 用户每天最多调用几次工具 (更高好感等级自动×系数)",
            "type": "int", "min": 3, "max": 100, "step": 1,
            "group": "每日配额",
        },
        "draw_cooldown_seconds": {
            "label": "绘图冷却(秒)",
            "desc": "同一用户两次 AI 绘图之间的最小间隔秒数",
            "type": "int", "min": 30, "max": 600, "step": 10,
            "group": "冷却",
        },
        "tool_cooldown_seconds": {
            "label": "工具冷却(秒)",
            "desc": "同一用户两次工具调用之间的最小间隔秒数",
            "type": "int", "min": 10, "max": 300, "step": 10,
            "group": "冷却",
        },
    }

    # ── 统一工具注册表 ─────────────────────────────────────
    # (name, label, category, bot, desc)
    # bot: "moon" = 暮恩, "" = , "both" = 双 bot 共享

    _UNIFIED_TOOLS: tuple[dict, ...] = (
        # ═══ 暮恩专属 (tavern) — L-Port / ComfyUI 生态 ═══
        {"name": "check_lport_status",    "label": "L-Port 状态",     "category": "系统", "bot": "moon", "desc": "检查 L-Port 生图平台是否在线", "min_affinity": 2},
        {"name": "list_available_models", "label": "模型列表",         "category": "系统", "bot": "moon", "desc": "获取 ComfyUI 可用 AI 模型列表", "min_affinity": 2},
        {"name": "list_custom_nodes",     "label": "自定义节点",       "category": "系统", "bot": "moon", "desc": "获取 ComfyUI 自定义节点列表", "min_affinity": 2},
        {"name": "search_knowledge",      "label": "知识库搜索",       "category": "知识", "bot": "both", "desc": "搜索本地 ComfyUI/AI绘画知识库", "min_affinity": 1},
        {"name": "send_sticker",          "label": "发送表情",         "category": "社交", "bot": "both", "desc": "按情绪标签发送表情包到群聊", "min_affinity": 1},
        {"name": "remember_memory",       "label": "记住信息",         "category": "记忆", "bot": "both", "desc": "将用户提供的信息存入长期记忆", "min_affinity": 1},
        {"name": "get_memory",            "label": "查询记忆",         "category": "记忆", "bot": "both", "desc": "查询已记住的用户信息", "min_affinity": 1},
        # ═══ 专属 (companion) — QZone / 转发 ═══
        {"name": "pc_qzone_view_feed",    "label": "QQ空间动态",   "category": "QZone", "bot": "", "desc": "查看 QQ 空间动态 (可选点赞/评论)", "min_affinity": 1},
        {"name": "pc_qzone_publish_feed", "label": "发表空间说说",  "category": "QZone", "bot": "", "desc": "发布 QQ 空间说说", "min_affinity": 1},
        {"name": "pc_relay_message",      "label": "统一转发",      "category": "转发", "bot": "", "desc": "转发消息到群/私聊 (统一入口)", "min_affinity": 1},
        {"name": "pc_send_to_group",      "label": "发群消息",      "category": "转发", "bot": "", "desc": "发送消息到指定群聊", "min_affinity": 1},
        {"name": "pc_send_to_private_user","label": "发私聊消息",   "category": "转发", "bot": "", "desc": "发送消息到指定用户私聊", "min_affinity": 1},
        {"name": "pc_send_to_groups",     "label": "批量发群",     "category": "转发", "bot": "", "desc": "同一消息广播到多个群", "min_affinity": 1},
        {"name": "pc_send_to_private_users","label": "批量发私聊", "category": "转发", "bot": "", "desc": "同一消息广播到多个用户", "min_affinity": 1},
        {"name": "pc_schedule_group_relay","label": "预约转发",    "category": "转发", "bot": "", "desc": "目标出现时延迟转发消息", "min_affinity": 1},
        # ═══ 双 bot 共享 — 搜索 ═══
        # 注: recall_long_term_memory 已移除 — 暮恩侧无 schema/executor,
        #     侧无实现, 属悬空注册 (违反单一真相源)。记忆检索统一用 get_memory。
        {"name": "web_search",            "label": "联网搜索",     "category": "搜索", "bot": "both", "desc": "SearXNG 联网搜索", "min_affinity": 1},
        {"name": "pixiv_search",         "label": "Pixiv 搜图",    "category": "搜索", "bot": "both", "desc": "Pixiv 插画搜索 (需 refresh_token)", "min_affinity": 1},
        # ═══ 双 bot 共享 — 视觉+生图 ═══
        {"name": "describe_image",        "label": "识图",        "category": "视觉", "bot": "both", "desc": "VLM 图片内容解析", "min_affinity": 2},
        {"name": "generate_image",        "label": "AI 生图",     "category": "生图", "bot": "both", "desc": "AI 云端绘图 (Lv.3+ 日限3张)", "min_affinity": 3},
        {"name": "edit_image",            "label": "图片编辑",     "category": "生图", "bot": "both", "desc": "以图生图/原图编辑 (与生图共享额度)", "min_affinity": 3},
        # ═══ 双 bot 共享 — 群成员查询 ═══
        {"name": "pc_get_group_id_by_name","label": "群名查ID",    "category": "查询", "bot": "both", "desc": "根据群名称关键词查找群号", "min_affinity": 1},
        {"name": "pc_get_user_id_by_name","label": "昵称查QQ",     "category": "查询", "bot": "both", "desc": "根据昵称/别名/群名片解析 QQ 号", "min_affinity": 1},
        {"name": "pc_get_specified_group_members","label":"群成员查询","category":"查询","bot":"both","desc":"查询指定群的成员列表 (按关系筛选)","min_affinity":1},
    )

    # 好感等级显示名 (与 emotion 插件 affinity.py 对齐)
    AFFINITY_LABELS: dict[int, str] = {
        -2: "黑名单",
        -1: "疏远",
        0: "陌生",
        1: "普通",
        2: "熟悉",
        3: "喜欢",
        4: "亲密",
        5: "珍视",
    }

    def get_tool_setting(self, bot_id: str, key: str) -> object:
        """获取指定 bot 的单个全局工具配置，带类型转换 fallback。"""
        default = self._TOOL_DEFAULTS.get(key)
        db_key = self._bot_key(bot_id, f"tool_{key}")
        val = self._db.get_config(db_key, "")
        if not val:
            return default
        if isinstance(default, bool):
            return val.lower() == "true"
        elif isinstance(default, int):
            try:
                return int(val)
            except ValueError:
                return default
        elif isinstance(default, float):
            try:
                return float(val)
            except ValueError:
                return default
        return val

    def set_tool_setting(self, bot_id: str, key: str, value: object) -> None:
        """设置指定 bot 的单个全局工具配置。"""
        db_key = self._bot_key(bot_id, f"tool_{key}")
        if isinstance(value, bool):
            val_str = "true" if value else "false"
        else:
            val_str = str(value)
        self._db.set_config(db_key, val_str)
        logger.info("工具配置更新: bot=%s %s = %s", bot_id, key, val_str)

    def get_all_tool_settings(self, bot_id: str) -> list[dict]:
        """获取指定 bot 的所有全局工具配置快照 (含元数据，供前端渲染)。"""
        result: list[dict] = []
        for key, meta in self._TOOL_META.items():
            entry = dict(meta)
            entry["key"] = key
            entry["value"] = self.get_tool_setting(bot_id, key)
            result.append(entry)
        return result

    # ── 统一工具层: per-tool 启停 ──────────────────────────

    # 默认禁用的工具 (安全敏感 / 昂贵操作，需手动开启)
    _TOOLS_DEFAULT_DISABLED: frozenset[str] = frozenset({"describe_image"})

    def is_tool_enabled(self, bot_id: str, tool_name: str) -> bool:
        """检查指定 bot 的某个工具是否启用。

        默认全部启用，除 _TOOLS_DEFAULT_DISABLED 中的工具 (如 describe_image)。
        """
        default = "false" if tool_name in self._TOOLS_DEFAULT_DISABLED else "true"
        key = self._bot_key(bot_id, f"tool_{tool_name}_enabled")
        val = self._db.get_config(key, default)
        return val.lower() == "true"

    def set_tool_enabled(self, bot_id: str, tool_name: str, enabled: bool) -> None:
        """设置指定 bot 的某个工具启停。"""
        key = self._bot_key(bot_id, f"tool_{tool_name}_enabled")
        self._db.set_config(key, "true" if enabled else "false")
        logger.info("工具启停: bot=%s tool=%s enabled=%s", bot_id, tool_name, enabled)

    @staticmethod
    def _tool_belongs_to(tool_bot: str, bot_id: str) -> bool:
        """判断工具是否属于指定 bot。

        "both" = 所有 bot 共享。
        其他值 (如 "moon"/"") 匹配 bot 的 character_card 字段。
        bot_id 未知时 fallback 到 "moon"。
        """
        if tool_bot == "both":
            return True
        try:
            from .bot_identity import get_bot_identity_service
            svc = get_bot_identity_service()
            bot = svc.get_bot(bot_id)
            if bot:
                return tool_bot == bot.character_card.lower()
        except Exception:
            pass
        return tool_bot == "moon"  # fallback

    def get_disabled_tools(self, bot_id: str) -> set[str]:
        """获取指定 bot 已禁用的工具名集合 (供运行时过滤)。

        只考虑该 bot 拥有的工具 (含共享工具)。
        """
        disabled: set[str] = set()
        for tool in self._UNIFIED_TOOLS:
            if not self._tool_belongs_to(tool["bot"], bot_id):
                continue
            if not self.is_tool_enabled(bot_id, tool["name"]):
                disabled.add(tool["name"])
        return disabled

    def get_unified_tool_list(self, bot_id: str) -> list[dict]:
        """获取统一工具注册表 + 指定 bot 的 per-tool 配置状态。

        只返回该 bot 拥有的工具 (专属 + 共享)，过滤掉其他 bot 的专属工具。

        Returns:
            [{name, label, category, bot, desc, enabled, min_affinity}, ...]
        """
        result: list[dict] = []
        for tool in self._UNIFIED_TOOLS:
            if not self._tool_belongs_to(tool["bot"], bot_id):
                continue
            entry = dict(tool)
            entry["enabled"] = self.is_tool_enabled(bot_id, tool["name"])
            entry["min_affinity"] = self.get_tool_min_affinity(bot_id, tool["name"])
            result.append(entry)
        return result

    # ── per-tool 好感度门控 ───────────────────────────────

    def get_tool_min_affinity(self, bot_id: str, tool_name: str) -> int:
        """读取指定 bot 的单个工具最低好感度要求。

        优先级: DB per-bot 覆盖 > _UNIFIED_TOOLS 默认值 > 1
        """
        default: int = self._resolve_tool_field(tool_name, "min_affinity", 1)
        key = self._bot_key(bot_id, f"tool_affinity:{tool_name}")
        val = self._db.get_config(key, "")
        if not val:
            return default
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    def set_tool_min_affinity(self, bot_id: str, tool_name: str, level: int) -> None:
        """设置指定 bot 的单个工具最低好感度要求。"""
        key = self._bot_key(bot_id, f"tool_affinity:{tool_name}")
        self._db.set_config(key, str(int(level)))
        logger.info(
            "工具好感门控: bot=%s tool=%s min_affinity=%d", bot_id, tool_name, level,
        )

    def get_all_tool_affinities(self, bot_id: str) -> list[dict]:
        """获取指定 bot 所有工具的好感度配置 (兼容 WebUI 前端)。"""
        result: list[dict] = []
        for tool in self._UNIFIED_TOOLS:
            if not self._tool_belongs_to(tool["bot"], bot_id):
                continue
            result.append({
                "name": tool["name"],
                "label": tool.get("label", tool["name"]),
                "desc": tool.get("desc", ""),
                "min_affinity": self.get_tool_min_affinity(bot_id, tool["name"]),
                "default_affinity": tool.get("min_affinity", 1),
            })
        return result

    def _resolve_tool_field(self, tool_name: str, field: str, fallback: int = 1) -> int:
        """从 _UNIFIED_TOOLS 查找工具的默认字段值 (数值型)。

        当前 _UNIFIED_TOOLS 中 min_affinity 均为 int 字面量，
        isinstance(raw, int) 分支覆盖了所有实际情况。
        int(raw) + except 分支为防御性编程 —— 若日后配置从
        JSON/YAML 等外部源加载并意外将数值存为字符串，仍可正确解析。
        """
        for tool in self._UNIFIED_TOOLS:
            if tool.get("name") == tool_name:
                raw = tool.get(field, fallback)
                if isinstance(raw, int):
                    return raw
                try:
                    return int(raw)
                except (ValueError, TypeError):
                    return fallback
        return fallback

    # ── 每日限额 & 冷却 (便捷方法) ─────────────────────────

    def get_daily_image_limit(self, bot_id: str) -> int:
        """获取指定 bot 的每日生图限额 (per-bot 可配, 默认 3)。"""
        return int(self.get_tool_setting(bot_id, "daily_image_limit") or 3)

    def get_daily_vlm_limit(self, bot_id: str) -> int:
        """获取指定 bot 的每日 VLM 识图限额 (per-bot 可配, 默认 5)。"""
        return int(self.get_tool_setting(bot_id, "daily_vlm_limit") or 5)

    def get_daily_tools_base_limit(self, bot_id: str) -> int:
        """获取指定 bot 的每日工具基础限额 (per-bot 可配, 默认 10)。"""
        return int(self.get_tool_setting(bot_id, "daily_tools_base_limit") or 10)

    def get_draw_cooldown_seconds(self, bot_id: str) -> int:
        """获取指定 bot 的绘图冷却秒数 (per-bot 可配, 默认 180)。"""
        return int(self.get_tool_setting(bot_id, "draw_cooldown_seconds") or 180)

    def get_tool_cooldown_seconds(self, bot_id: str) -> int:
        """获取指定 bot 的工具冷却秒数 (per-bot 可配, 默认 60)。"""
        return int(self.get_tool_setting(bot_id, "tool_cooldown_seconds") or 60)

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
