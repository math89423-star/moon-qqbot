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
from typing import TYPE_CHECKING, ClassVar

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

    # ── NSFW 隔离区 (Luna 专用, 默认关闭, fail-closed) ────
    # DB 键 (per-bot, 原则 16):
    #   bot:{id}:nsfw_enabled ("true"/"false", 默认 "false")
    #   bot:{id}:nsfw_groups (逗号分隔群号, 默认空 = 无群命中)
    #   bot:{id}:nsfw_users (逗号分隔 QQ, 默认空 = 仅超管)

    def is_nsfw_enabled(self, bot_id: str) -> bool:
        """NSFW 总开关。默认 False。"""
        key = self._bot_key(bot_id, "nsfw_enabled")
        val = self._db.get_config(key, "false")
        return val.lower() == "true"

    def set_nsfw_enabled(self, bot_id: str, enabled: bool) -> None:
        key = self._bot_key(bot_id, "nsfw_enabled")
        self._db.set_config(key, "true" if enabled else "false")
        logger.info("Bot %s nsfw_enabled = %s", bot_id, enabled)

    def get_nsfw_groups(self, bot_id: str) -> str:
        """NSFW 群白名单 (逗号分隔, 默认空)。"""
        key = self._bot_key(bot_id, "nsfw_groups")
        return self._db.get_config(key, "")

    def set_nsfw_groups(self, bot_id: str, groups: str) -> None:
        key = self._bot_key(bot_id, "nsfw_groups")
        self._db.set_config(key, groups or "")
        logger.info("Bot %s nsfw_groups = %s", bot_id, groups)

    def get_nsfw_users(self, bot_id: str) -> str:
        """NSFW 人白名单 (逗号分隔, 默认空 = 仅超管)。"""
        key = self._bot_key(bot_id, "nsfw_users")
        return self._db.get_config(key, "")

    def set_nsfw_users(self, bot_id: str, users: str) -> None:
        key = self._bot_key(bot_id, "nsfw_users")
        self._db.set_config(key, users or "")
        logger.info("Bot %s nsfw_users = %s", bot_id, users)

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

    # ── Per-Bot 思考档位 (前端可配, 作用最终回复路径) ──
    # auto = Gate 按意图动态决定 (默认, 现状); off = 强制关闭;
    # 其余 = 固定档位, 覆盖 Gate 注入 (MainDecision JSON 任务仍硬关, 不受影响)。
    THINKING_EFFORTS: tuple[str, ...] = (
        "auto", "off", "minimal", "low", "medium", "high", "xhigh", "max",
    )

    def get_thinking_effort(self, bot_id: str) -> str:
        """获取指定 bot 的思考档位。默认 auto, 非法值回 auto。"""
        key = self._bot_key(bot_id, "thinking_effort")
        val = (self._db.get_config(key, "auto") or "auto").strip().lower()
        return val if val in self.THINKING_EFFORTS else "auto"

    def set_thinking_effort(self, bot_id: str, effort: str) -> None:
        """设置指定 bot 的思考档位 (白名单校验, 非法值回 auto)。"""
        effort = (effort or "auto").strip().lower()
        if effort not in self.THINKING_EFFORTS:
            effort = "auto"
        key = self._bot_key(bot_id, "thinking_effort")
        self._db.set_config(key, effort)
        logger.info("Bot %s thinking_effort = %s", bot_id, effort)

    # 决策类调用 (MainDecision/人格化改写) 的档位应用:
    # auto/off → 硬关 thinking (P0 现状); 固定档 → reasoning_effort 直传,
    # 高档同步抬 max_tokens (CoT 计入输出预算, 小预算会被思考吃光)。
    DECISION_MAX_TOKENS: ClassVar[dict[str, int]] = {
        "xhigh": 16384, "max": 16384, "high": 12288,
    }

    # ── Per-Slot 思考档位 (前端槽位旁下拉, 档位跟着槽位/模型走) ──
    # 存储 bot:{id}:slot_effort:{slot}, 默认 "follow"(跟全局 thinking_effort)。
    SLOT_EFFORT_SLOTS: ClassVar[tuple[str, ...]] = (
        "llm_primary", "llm_lite", "llm_pro", "llm_gate", "shadow_agent",
    )
    SLOT_EFFORT_VALUES: ClassVar[tuple[str, ...]] = (
        "follow", "off", "minimal", "low", "medium", "high", "xhigh", "max",
    )

    def get_slot_effort(self, bot_id: str, slot: str, *, nsfw: bool = False) -> str:
        """获取指定 bot 某槽位的档位覆盖。默认 follow, 非法值回 follow。

        nsfw=True: 先查 NSFW 专属档位 (非 follow 才优先), 否则继续走
        SFW 槽位档位 — 未配置即完全跟随 SFW 链。
        """
        if nsfw:
            key = self._bot_key(bot_id, f"nsfw:slot_effort:{slot}")
            val = (self._db.get_config(key, "") or "").strip().lower()
            if val in self.SLOT_EFFORT_VALUES and val != "follow":
                return val
        key = self._bot_key(bot_id, f"slot_effort:{slot}")
        val = (self._db.get_config(key, "follow") or "follow").strip().lower()
        return val if val in self.SLOT_EFFORT_VALUES else "follow"

    def set_slot_effort(self, bot_id: str, slot: str, effort: str, *, nsfw: bool = False) -> None:
        """设置指定 bot 某槽位的档位覆盖 (白名单校验)。nsfw=True 写 NSFW 专属档。"""
        if slot not in self.SLOT_EFFORT_SLOTS:
            return
        effort = (effort or "follow").strip().lower()
        if effort not in self.SLOT_EFFORT_VALUES:
            effort = "follow"
        prefix = "nsfw:slot_effort:" if nsfw else "slot_effort:"
        key = self._bot_key(bot_id, f"{prefix}{slot}")
        self._db.set_config(key, effort)
        logger.info("Bot %s slot %s%s effort = %s", bot_id, "NSFW " if nsfw else "", slot, effort)

    def get_all_slot_efforts(self, bot_id: str, *, nsfw: bool = False) -> dict[str, str]:
        """获取指定 bot 全槽位档位快照 (前端下拉回显)。

        nsfw=True: 返回 NSFW 覆盖快照 (follow = 跟随 SFW / 未设置)。
        """
        if nsfw:
            result: dict[str, str] = {}
            for s in self.SLOT_EFFORT_SLOTS:
                val = (
                    self._db.get_config(self._bot_key(bot_id, f"nsfw:slot_effort:{s}"), "follow") or "follow"
                ).strip().lower()
                result[s] = val if val in self.SLOT_EFFORT_VALUES else "follow"
            return result
        return {s: self.get_slot_effort(bot_id, s) for s in self.SLOT_EFFORT_SLOTS}

    # ── 旧默认档位 default_effort (2026-10-15 已合并入 thinking_effort) ──
    # 数据迁移见一次性脚本说明；新代码一律走 thinking_effort + slot_effort。

    def set_all_slot_efforts(self, bot_id: str, efforts: dict, *, nsfw: bool = False) -> list[str]:
        """批量设置槽位档位。返回实际更新的槽位名。nsfw=True 写 NSFW 专属档。"""
        updated: list[str] = []
        for slot, effort in (efforts or {}).items():
            if slot in self.SLOT_EFFORT_SLOTS:
                self.set_slot_effort(bot_id, slot, str(effort or "follow"), nsfw=nsfw)
                updated.append(slot)
        return updated

    def get_effective_effort(self, bot_id: str, slot: str = "", *, nsfw: bool = False) -> str:
        """槽位实际生效档位: 槽覆盖 (非 follow) 优先, 否则全局默认档。

        nsfw=True 时生效链: NSFW 槽位档 → SFW 槽位档 → 全局默认档。

        返回值语义与全局一致: auto=动态/默认关, off=硬关, 其余=固定档。
        单一真相源：全局只有 thinking_effort 一个设置（前端标为“默认档”），
        槽位下拉 follow 即用它。2026-10-15 合并：旧 default_effort 已迁移
        （仅 shadow_agent 消费过，已按“显式槽覆盖优先、否则沿用”落盘），
        此处不再设例外分支。
        """
        if slot:
            fixed = self.get_slot_effort(bot_id, slot, nsfw=nsfw)
            if fixed != "follow":
                return fixed
        return self.get_thinking_effort(bot_id)

    def get_decision_extra(
        self, bot_id: str, slot: str = "", *, nsfw: bool = False,
    ) -> tuple[dict, int | None]:
        """决策类调用的 (extra_params, max_tokens_override)。

        slot: 实际命中的槽位名 (如 llm_lite/llm_pro), 档位按槽位解析。
        nsfw: 档位沿 NSFW 链解析 (NSFW 槽位档 → SFW 槽位档 → 全局)。

        Returns:
            (extra, max_tokens|None) — max 为 None 时调用方用默认值。
        """
        effort = self.get_effective_effort(bot_id, slot, nsfw=nsfw)
        if effort in ("auto", "off"):
            return {"thinking": {"type": "disabled"}}, None
        return {"reasoning_effort": effort}, self.DECISION_MAX_TOKENS.get(effort)

    def get_gate_extra(self, bot_id: str) -> dict:
        """Gate 类调用 (Slim/Full) 的 extra_params — 档位按 llm_gate 槽位解析。

        前端固定档位 (thinking_effort) 同样作用于 Gate；
        auto 时沿用 gate_thinking_enabled 开关 (默认关)。
        注: 高档 CoT 计入输出预算, tavern 地板逻辑会自动抬预算；
        但 Gate 超时短，高档可能超时 fail-open。
        """
        effort = self.get_effective_effort(bot_id or "", "llm_gate")
        if effort not in ("auto", "off"):
            return {"reasoning_effort": effort}
        if effort == "auto" and self.is_gate_thinking_enabled(bot_id or ""):
            return {"thinking": {"type": "enabled"}}
        return {"thinking": {"type": "disabled"}}

    # Gate 预算随档位 (CoT 计入输出)。2026-10-17 修宽: 补 minimal/low
    # (慢模型 CoT 起步 60~500 token; low 之前回落基础值 1024, 长输入偶发
    # 被思考吃光 → incomplete)。基础值(off 档)仍 1024 —— 关思考时绰绰有余。
    GATE_BUDGETS: ClassVar[dict[str, int]] = {
        "minimal": 1536, "low": 2048, "medium": 3072,
        "high": 4096, "xhigh": 8192, "max": 8192,
    }

    def get_gate_budget(self, bot_id: str, base: int) -> int:
        """Gate 调用 max_tokens: 基值与档位需求取大。"""
        effort = self.get_effective_effort(bot_id or "", "llm_gate")
        if effort == "auto" and self.is_gate_thinking_enabled(bot_id or ""):
            effort = "medium"
        return max(base, self.GATE_BUDGETS.get(effort, base))

    # ── ★ 影子 Agent 配置 (2026-07-02 压力测试加固) ──

    def is_shadow_agent_enabled(self, bot_id: str) -> bool:
        """检查指定 bot 的影子 Agent 是否启用。默认 true。"""
        key = self._bot_key(bot_id, "shadow_agent_enabled")
        val = self._db.get_config(key, "")
        if val == "":
            return True  # 默认启用
        return val.lower() == "true"

    def set_shadow_agent_enabled(self, bot_id: str, enabled: bool) -> None:
        """设置指定 bot 的影子 Agent 开关。"""
        key = self._bot_key(bot_id, "shadow_agent_enabled")
        self._db.set_config(key, "true" if enabled else "false")
        logger.info("Bot %s shadow_agent_enabled = %s", bot_id, enabled)

    def resolve_background_llm(
        self, bot_id: str, purpose: str = "background", *, nsfw: bool = False,
    ) -> dict:
        """解析背景任务 LLM 配置 — 统一入口。

        背景任务 (总结/建档/记忆提取) 默认走 llm_lite 槽位,
        影子 Agent (purpose="shadow_agent") 走自己的 shadow_agent 槽位,
        未配置时 fallback 到 llm_lite。
        thinking 跟随 gate_thinking_enabled 开关。
        nsfw=True: 槽位沿 NSFW 链解析 (未配专属覆盖 = 原行为)。

        Returns:
            {"model": str, "api_base": str, "api_key": str, "extra_params": dict|None}
            解析失败返回空 dict
        """
        # 影子 Agent: 优先走专属槽位
        if purpose == "shadow_agent":
            slot = self.resolve_llm_slot(bot_id, "shadow_agent", nsfw=nsfw)
            if slot:
                logger.debug(
                    "resolve_background_llm: bot=%s shadow_agent → %s",
                    bot_id, slot.model_name,
                )
                _shadow_effort = self.get_effective_effort(bot_id, "shadow_agent", nsfw=nsfw)
                return {
                    "model": slot.model_name,
                    "api_base": slot.normalized_base_url,
                    "api_key": slot.api_key,
                    # 固定档才注思考 (auto/off 保持现状 None)
                    "extra_params": (
                        {"reasoning_effort": _shadow_effort}
                        if _shadow_effort not in ("auto", "off") else None
                    ),
                }

        # 默认: llm_lite 槽位
        slot = self.resolve_llm_slot(bot_id, "llm_lite", nsfw=nsfw) \
            or self.resolve_llm_slot(bot_id, "llm_primary", nsfw=nsfw)
        if not slot:
            logger.debug("resolve_background_llm: bot=%s 无可用槽位", bot_id)
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
            # mimo-v2.5 等多用途模型 config_type=vlm，但也可当 LLM 用
            # 只要有 api_key 且 is_active 就接受
            if cfg and cfg.api_key and cfg.is_active:
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

    def get_first_available_llm(self) -> LLMConfigRO | None:
        """最终兜底: 从 llm_config 表直接取第一个活跃且有 API key 的 LLM。

        不依赖 active_llm_id — 用于 active_llm_id 未设置但槽位已配的场景。
        """
        try:
            configs = self._db.list_llm_configs()
            for cfg in configs:
                # mimo-v2.5 等多用途模型 config_type=vlm，但也可当 LLM 用
                if cfg.api_key and cfg.is_active:
                    return cfg
        except Exception:
            pass
        return None

    # ── LLM/VLM 槽位 (多模型支持) ───────────────────────

    # ── 链路 → 模型槽位 统一映射 (唯一真相源, 2026-10-15) ──
    # 前端标签与代码槽位曾各说各话 (闲聊/推理 vs slim/brain/mouth)，
    # 从此：链路名只进日志 slot= 前缀做区分，模型与档位只认下表。
    # | 链路                 | 模型槽位      | 档位槽位      | 档位策略                          |
    # |----------------------|---------------|---------------|-----------------------------------|
    # | gate (Slim/Stage1/2) | llm_gate      | llm_gate      | 跟槽位档位                        |
    # | brain (脑决策)       | llm_primary   | llm_primary   | 跟槽位档位                        |
    # | mouth (嘴渲染/连发)  | llm_primary*  | —             | 固定关思考 (responses 地板 minimal) |
    # | actor (二次人格化)   | llm_primary*  | —             | 固定关思考                        |
    # | reason (spec/ReAct)  | llm_pro       | llm_pro       | 跟槽位档位                        |
    # | chat (闲聊/后台/压缩)| llm_lite      | llm_lite      | 跟槽位档位                        |
    # | shadow (影子Agent)   | shadow_agent  | shadow_agent  | 跟槽位档位                        |
    # * mouth 连发走 llm_lite，actor/私聊回退走 llm_lite；私聊角色扮演用 llm_primary。
    # mouth/actor 固定关思考是 2026-10-13 超时教训：渲染跟高档会烧穿预算 60s 超时。
    # responses 网关无 "none" 档，关思考落地为 minimal —— 日志 effort=minimal 属正常地板。
    LLM_SLOTS = ("llm_primary", "llm_lite", "llm_pro", "llm_gate", "shadow_agent")
    VLM_SLOTS = ("vlm_primary", "vlm_secondary")

    # 退役槽位：llm_secondary 无任何消费方，llm_judge/llm_opus 功能已删 —— 前端不再展示。
    RETIRED_SLOTS: ClassVar[tuple[str, ...]] = ("llm_secondary", "llm_judge", "llm_opus")

    # ── 向后兼容: 首选槽名 → 回退槽名 (DB 只有旧键时仍能解析) ──
    _SLOT_ALIASES: dict[str, str] = {
        "llm_primary": "llm_lite",
        "llm_secondary": "llm_pro",
    }

    # ── Per-bot 槽位差异 ──
    # 每个 bot 的 LLM 槽位由 bot_identity.metadata.llm_slots 字段定义。
    # 未配置时默认返回全部 4 个槽位。

    def get_display_llm_slots(self, bot_id: str) -> tuple[str, ...]:
        """返回该 bot 应展示的 LLM 槽位列表 (从 BotIdentity 读取)。

        影子 Agent 槽位 (2026-07-02) 始终追加——无论 bot_identity 是否包含。
        """
        try:
            from .bot_identity import get_bot_identity_service
            svc = get_bot_identity_service()
            slots = svc.get_llm_slots(bot_id)
            if slots:
                return slots
        except Exception:
            slots = list(self.LLM_SLOTS)
        # 主链槽位 (brain/mouth/actor/私聊) 始终首位展示 —— 代码首选它，
        # 退役槽位 (secondary/judge/opus) 永不展示。
        slots = [s for s in slots if s not in self.RETIRED_SLOTS]
        if "llm_primary" not in slots:
            slots.insert(0, "llm_primary")
        # 影子 Agent 始终追加
        if "shadow_agent" not in slots:
            slots.append("shadow_agent")
        return tuple(slots)

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

    # ── NSFW 专属槽位 (2026-10-18) ────────────────────────
    # 背景: 上游模型审查 (如 longcat) 对成人向输入整轮打回, 成人区需要独立
    # 一套模型/档位, 不再和 SFW 抢同一槽位。未配置 = 跟随 SFW, 老行为零变化。
    # 键: bot:{id}:nsfw:{slot} (槽位), bot:{id}:nsfw:slot_effort:{slot} (档位),
    #     bot:{id}:nsfw:temperature_{scenario} (温度)。

    def get_nsfw_llm_slot(self, bot_id: str, slot: str) -> int | None:
        """NSFW 专属槽位 config_id。未配置返回 None (= 跟随 SFW)。"""
        if slot not in self.LLM_SLOTS:
            return None
        raw = self._db.get_config(self._bot_key(bot_id, f"nsfw:{slot}"), "")
        return int(raw) if raw else None

    def set_nsfw_llm_slot(self, bot_id: str, slot: str, config_id: int) -> None:
        """设置 NSFW 专属槽位。config_id <= 0 → 清除覆盖 (跟随 SFW)。

        与 set_llm_slot 同口径同步 is_active: 新旧 config 都重扫引用计数。
        """
        if slot not in self.LLM_SLOTS:
            return
        try:
            cid = int(config_id) if config_id else 0
        except (TypeError, ValueError):
            cid = 0
        key = self._bot_key(bot_id, f"nsfw:{slot}")
        old_raw = self._db.get_config(key, "")
        try:
            old_cid = int(old_raw) if old_raw else 0
        except (TypeError, ValueError):
            old_cid = 0
        new_val = str(cid) if cid > 0 else ""
        self._db.set_config(key, new_val)
        for _scan in {c for c in (old_cid, cid) if c > 0}:
            self._sync_is_active_for_config(_scan)
        logger.info(
            "NSFW 槽位: bot=%s slot=%s → %s",
            bot_id, slot, new_val or "(跟随 SFW)",
        )

    def _load_llm_cfg_by_id(self, bot_id: str, slot: str, cfg_id: int) -> LLMConfigRO | None:
        """按 config_id 解析可用 LLM 配置 (api_key + is_active 双检)。"""
        try:
            cfg = self._db.get_llm_config(cfg_id)
            # mimo-v2.5 等多用途模型 config_type=vlm，但也可当 LLM 用
            if cfg and cfg.api_key and cfg.is_active:
                return cfg
        except Exception:
            logger.warning("LLM 槽位解析失败: bot=%s slot=%s id=%d", bot_id, slot, cfg_id, exc_info=True)
        return None

    def resolve_llm_slot(
        self, bot_id: str, slot: str, *, nsfw: bool = False,
    ) -> LLMConfigRO | None:
        """解析指定 bot 的 LLM 槽位 → 完整配置。

        nsfw=True: 先查 NSFW 专属覆盖 (bot:{id}:nsfw:{slot})，未配置或不可用
        时回落 SFW 槽位 —— 没配专属覆盖的 bot 行为与旧版完全一致。
        支持向后兼容: 新槽名查不到时 fallback 到旧槽名 (llm_primary→llm_lite 等)。
        """
        if nsfw:
            nsfw_id = self.get_nsfw_llm_slot(bot_id, slot)
            if nsfw_id is not None:
                cfg = self._load_llm_cfg_by_id(bot_id, f"nsfw:{slot}", nsfw_id)
                if cfg is not None:
                    return cfg
                logger.debug(
                    "NSFW 槽位不可用, 回落 SFW: bot=%s slot=%s id=%d",
                    bot_id, slot, nsfw_id,
                )
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
        return self._load_llm_cfg_by_id(bot_id, slot, cfg_id)

    def resolve_llm_slot_ex(
        self, bot_id: str, slot: str, *, nsfw: bool = False,
    ) -> tuple[LLMConfigRO | None, bool]:
        """resolve_llm_slot 的带来源版本：返回 (配置, 是否命中 NSFW 专属覆盖)。

        调用方用它区分「nsfw=True 但回落 SFW」与「真的换了 NSFW 模型」，
        避免未配覆盖时误切换 base_url/模型来源。
        """
        if nsfw:
            nsfw_id = self.get_nsfw_llm_slot(bot_id, slot)
            if nsfw_id is not None:
                cfg = self._load_llm_cfg_by_id(bot_id, f"nsfw:{slot}", nsfw_id)
                if cfg is not None:
                    return cfg, True
        return self.resolve_llm_slot(bot_id, slot), False

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

    def _load_vlm_cfg_by_id(self, bot_id: str, slot: str, cfg_id: int) -> LLMConfigRO | None:
        """按 config_id 解析可用 VLM 配置 (is_vlm + is_active 双检)。"""
        try:
            cfg = self._db.get_llm_config(cfg_id)
            if cfg and cfg.is_vlm and cfg.is_active:
                return cfg
        except Exception:
            logger.warning("VLM 槽位解析失败: bot=%s slot=%s id=%d", bot_id, slot, cfg_id, exc_info=True)
        return None

    def get_nsfw_vlm_slot(self, bot_id: str, slot: str) -> int | None:
        """NSFW 专属 VLM 槽位 config_id。未配置返回 None (= 跟随 SFW)。"""
        if slot not in self.VLM_SLOTS:
            return None
        raw = self._db.get_config(self._bot_key(bot_id, f"nsfw:{slot}"), "")
        return int(raw) if raw else None

    def set_nsfw_vlm_slot(self, bot_id: str, slot: str, config_id: int) -> None:
        """设置 NSFW 专属 VLM 槽位。config_id <= 0 → 清除覆盖 (跟随 SFW)。

        与 set_nsfw_llm_slot 同口径同步 is_active: 新旧 config 都重扫引用计数。
        """
        if slot not in self.VLM_SLOTS:
            return
        try:
            cid = int(config_id) if config_id else 0
        except (TypeError, ValueError):
            cid = 0
        key = self._bot_key(bot_id, f"nsfw:{slot}")
        old_raw = self._db.get_config(key, "")
        try:
            old_cid = int(old_raw) if old_raw else 0
        except (TypeError, ValueError):
            old_cid = 0
        new_val = str(cid) if cid > 0 else ""
        self._db.set_config(key, new_val)
        for _scan in {c for c in (old_cid, cid) if c > 0}:
            self._sync_is_active_for_config(_scan)
        logger.info(
            "NSFW VLM 槽位: bot=%s slot=%s → %s",
            bot_id, slot, new_val or "(跟随 SFW)",
        )

    def resolve_vlm_slot(
        self, bot_id: str, slot: str, *, nsfw: bool = False,
    ) -> LLMConfigRO | None:
        """解析指定 bot 的 VLM 槽位 → 完整配置。

        nsfw=True: 先查 NSFW 专属覆盖 (bot:{id}:nsfw:{slot})，未配置或不可用
        时回落 SFW 槽位 —— 没配专属覆盖的 bot 行为与旧版完全一致。

        ⚠️ 必须同时检查 is_vlm 和 is_active。
        此前只检查 is_vlm → inactive 模型 (如 gpt-5.4-mini id=33) 仍被当作 VLM 主槽烧钱。
        """
        if nsfw:
            nsfw_id = self.get_nsfw_vlm_slot(bot_id, slot)
            if nsfw_id is not None:
                cfg = self._load_vlm_cfg_by_id(bot_id, f"nsfw:{slot}", nsfw_id)
                if cfg is not None:
                    return cfg
                logger.debug(
                    "NSFW VLM 槽位不可用, 回落 SFW: bot=%s slot=%s id=%d",
                    bot_id, slot, nsfw_id,
                )
        cfg_id = self.get_vlm_slot(bot_id, slot)
        if cfg_id is None:
            return None
        return self._load_vlm_cfg_by_id(bot_id, slot, cfg_id)

    def resolve_vlm_slot_ex(
        self, bot_id: str, slot: str, *, nsfw: bool = False,
    ) -> tuple[LLMConfigRO | None, bool]:
        """resolve_vlm_slot 的带来源版本：返回 (配置, 是否命中 NSFW 专属覆盖)。

        调用方用它区分「nsfw=True 但回落 SFW」与「真的换了 NSFW 模型」——
        未配覆盖时不得改动原有 VLM 来源 (零行为变化)。
        """
        if nsfw:
            nsfw_id = self.get_nsfw_vlm_slot(bot_id, slot)
            if nsfw_id is not None:
                cfg = self._load_vlm_cfg_by_id(bot_id, f"nsfw:{slot}", nsfw_id)
                if cfg is not None:
                    return cfg, True
        return self.resolve_vlm_slot(bot_id, slot), False

    # ── 闸门判断槽位 + 仲裁槽位 (per-bot, 复用通用 get/set_llm_slot) ──

    def get_temperature(self, bot_id: str, scenario: str, *, nsfw: bool = False) -> float:
        """获取指定 bot 的某个场景 temperature。

        nsfw=True: 先查 NSFW 专属覆盖, 无值回落 SFW 链再默认值。

        Args:
            bot_id: bot QQ 号
            scenario: 场景标识，如 'tavern_group', 'memory_extract',
                      'context_compress', 'cross_validation'

        Returns:
            temperature 值 (float), DB 无值时 fallback 到默认值
        """
        if nsfw:
            key = self._bot_key(bot_id, f"nsfw:temperature_{scenario}")
            val = self._db.get_config(key, "")
            if val:
                try:
                    return float(val)
                except ValueError:
                    pass
        key = self._bot_key(bot_id, f"temperature_{scenario}")
        val = self._db.get_config(key, "")
        if val:
            try:
                return float(val)
            except ValueError:
                pass
        default_key = f"temperature_{scenario}"
        return self._TEMP_DEFAULTS.get(default_key, 0.7)

    def set_temperature(
        self, bot_id: str, scenario: str, value: float | None, *, nsfw: bool = False,
    ) -> None:
        """设置指定 bot 的某个场景 temperature。

        nsfw=True 写 NSFW 专属键; value=None → 清除该覆盖 (恢复跟随 SFW)。
        """
        key = self._bot_key(
            bot_id,
            f"nsfw:temperature_{scenario}" if nsfw else f"temperature_{scenario}",
        )
        if value is None:
            if nsfw:
                self._db.set_config(key, "")
                logger.info("温度清除: bot=%s NSFW %s = (跟随 SFW)", bot_id, scenario)
            return
        clamped = max(0.0, min(2.0, float(value)))
        self._db.set_config(key, str(clamped))
        logger.info("温度更新: bot=%s %s%s = %.2f", bot_id, "NSFW " if nsfw else "", scenario, clamped)

    def get_nsfw_temperature_overrides(self, bot_id: str) -> dict[str, bool]:
        """哪些场景有 NSFW 温度覆盖 (前端显示「覆盖中 / 跟随 SFW」)。"""
        result: dict[str, bool] = {}
        for scenario in (
            "tavern_group", "memory_extract", "context_compress", "cross_validation",
        ):
            result[scenario] = bool(
                self._db.get_config(self._bot_key(bot_id, f"nsfw:temperature_{scenario}"), "")
            )
        return result

    def get_all_temperatures(self, bot_id: str, *, nsfw: bool = False) -> dict[str, float]:
        """获取指定 bot 的所有场景 temperature 快照。

        nsfw=True: 返回 NSFW 链生效值 (NSFW 覆盖 → SFW → 默认)。

        仅含 4 个有效场景 (2026-06-30 清理结论，2026-10-11 补齐代码)：
        bridge_chat (bridge 插件已下线)、tavern_private (私聊硬编码未读配置)
        已彻底移除，不再返回。
        """
        result: dict[str, float] = {}
        for scenario in [
            "tavern_group",
            "memory_extract", "context_compress", "cross_validation",
        ]:
            result[scenario] = self.get_temperature(bot_id, scenario, nsfw=nsfw)
        return result

    def set_all_temperatures(self, bot_id: str, temps: dict[str, float], *, nsfw: bool = False) -> None:
        """批量设置指定 bot 的 temperature。nsfw=True 写 NSFW 专属键。"""
        for scenario, value in temps.items():
            self.set_temperature(bot_id, scenario, value, nsfw=nsfw)
        logger.info("批量温度更新: bot=%s%s %s", bot_id, " (NSFW)" if nsfw else "", temps)

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
        # ── 第三方群聊Bot硬忽略 (2026-09-08, 幽幽子旁观者接话漏洞) ──
        "group_chat_known_bots": [],
        # ── per-bot 昵称表 (2026-10-15 恢复) ──
        # 2026-06-30 曾标注"→ BotIdentity"移除，但读取方 (_resolve_gate_nicknames /
        # _is_nickname_mentioned / VLM 昵称检测) 从未迁移，get_chat_param 对无 meta 的
        # 键恒返 None → 全员回退全局默认表，per-bot 的名单无法生效。
        # 恢复 csv 类型，DB 既有 per-bot 值即刻生效。
        "group_chat_nicknames": ["暮恩", "moon"],
    }

    # 已移除 (2026-06-30):
    #   bridge_*, tavern_chat_max_tokens, group_chat_max_context, proactive_enabled,
    #   model_router_* (→ LLM 槽位)

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
        "group_chat_known_bots": {
            "label": "第三方群聊Bot QQ（硬忽略）",
            "desc": "这些 QQ 发的消息永不触发回复（不进 Gate 零 token），逗号分隔。用于幽幽子这类群聊bot，防旁观者接话",
            "type": "csv",
            "group": "群聊触发策略",
        },
        "group_chat_nicknames": {
            "label": "群聊昵称表",
            "desc": "叫本 bot 的名字/昵称，逗号分隔。Gate 与昵称触发据此判是不是在叫我（per-bot，每个 bot 各用各的表）",
            "type": "csv",
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
                    import re as _re_csv
                    return [s.strip() for s in _re_csv.split(r"[,，、]", val) if s.strip()]
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

    # ── 绘图后端 (NovelAI / PixAI，各自独立 + 按 Bot 分配) ──
    # 写入口唯一在管理面板 webui/server.py (draw/config)；
    # 这里只读，供 tools.py 执行器做 per-bot 后端分发。
    #
    # 访问策略键 (2026-10-12 用户裁定: 计费后端仅主人 + 模型按身份分档):
    #   draw_novelai_model        主人档模型 (默认 nai-diffusion-5-full)
    #   draw_novelai_model_public 普通用户档模型 (默认 nai-diffusion-4-5-full)
    #   draw_metered_master_only  计费后端仅主人可用 (默认 "true")
    #   draw_metered_nsfw_allow   主人 NSFW 通路额外放行计费后端 (默认 "true")
    # 判定逻辑唯一在 service/draw_access.py，此处只负责读写原始值。

    _DRAW_VALID_SIZES = frozenset({
        "832x1216", "1216x832", "1024x1024", "512x768", "768x512",
    })

    def get_draw_provider(self, bot_id: str) -> str:
        """该 Bot 生图走哪家: novelai / pixai / none (默认 novelai)。"""
        v = self._db.get_config(f"draw_assign_{bot_id}", "novelai").strip()
        return v if v in ("novelai", "pixai", "none") else "novelai"

    def get_draw_novelai(self) -> dict:
        """NovelAI 独立配置快照 (token 可能为空，调用方判空)。

        模型两档 (2026-10-12 用户裁定: 主人默认 V5 full / 普通用户 V4.5 full):
          - model        主人档  `draw_novelai_model`        (默认 nai-diffusion-5-full)
          - model_public 公共档  `draw_novelai_model_public` (默认 nai-diffusion-4-5-full)
        生效档由 service/draw_access.resolve_route() 按身份选定并回填 model。
        """
        g = self._db.get_config
        try:
            steps = int(g("draw_novelai_steps", "23") or 23)
        except (TypeError, ValueError):
            steps = 23
        steps = max(1, min(28, steps))
        size = (g("draw_novelai_size", "832x1216") or "832x1216").strip()
        if size not in self._DRAW_VALID_SIZES:
            size = "832x1216"
        return {
            "token": (g("draw_novelai_token", "") or "").strip(),
            "model": (g("draw_novelai_model", "nai-diffusion-5-full") or "nai-diffusion-5-full").strip(),
            "model_public": (
                g("draw_novelai_model_public", "nai-diffusion-4-5-full") or "nai-diffusion-4-5-full"
            ).strip(),
            "size": size,
            "steps": steps,
        }

    def get_draw_pixai(self) -> dict:
        """PixAI 独立配置快照 (key 可能为空，调用方判空)。"""
        g = self._db.get_config
        aspect = (g("draw_pixai_aspect", "9:16") or "9:16").strip()
        if aspect not in ("1:1", "2:3", "3:2", "3:4", "4:3", "3:5", "5:3", "9:16", "16:9", "1:3", "3:1"):
            aspect = "9:16"
        size = (g("draw_pixai_size", "1k") or "1k").strip()
        if size not in ("1k", "1.5k"):
            size = "1k"
        return {
            "key": (g("draw_pixai_key", "") or "").strip(),
            "model": (g("draw_pixai_model", "1983308862240288769") or "1983308862240288769").strip(),
            "aspect": aspect,
            "size": size,
        }

    # ── 生图构思 spec (Phase-1)：走 LLM 推理位 (llm_pro) ──
    # 2026-10-12 裁定：构思本质是纯推理任务，不自立线路/档位/预算/温度/超时/agent开关；
    # 直接复用 llm_pro 槽位模型 + 槽位档位。绘图页独立 spec 参数已退役。

    _SPEC_ROUTES = ("vlm", "tavern")
    _SPEC_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")

    def get_draw_spec(self, bot_id: str = "") -> dict:
        """生图 spec 配置快照 — 走 LLM 推理位 (llm_pro)。
        模型取 llm_pro 槽位 (未配则回落 llm_lite)；档位取 llm_pro 生效档
        (auto 回落 high; off = 真关思考 2026-10-17，此前被静默映射成 high)；
        预算按决策档位表抬升；温度 0.7 / 超时 90 固定；agent 不先行，直接 llm_pro 直调。
        bot_id 为空时返回全局默认 (effort=high)，供面板兼容读取。
        """
        _effort = "high"
        try:
            _eff = str(self.get_effective_effort(bot_id, "llm_pro") or "high").strip().lower()
            if _eff == "off":
                # 2026-10-17: off 落地真关 —— 此前落 else→high, 面板设 off 等于没设
                # (与 spec 直调链的真关路径配套, 见 dispatcher._spec_via_vlm)。
                _effort = "off"
            elif _eff == "minimal":
                _effort = "low"
            elif _eff in ("low", "medium", "high", "xhigh", "max"):
                _effort = _eff
            elif _eff == "none":
                _effort = "none"
            else:
                _effort = "high"
        except Exception:
            _effort = "high"
        _tokens = self.DECISION_MAX_TOKENS.get(_effort, 4096)
        if _tokens is None:
            _tokens = 4096
        return {
            "route": "llm_pro",
            "effort": _effort,
            "tokens": _tokens,
            "temperature": 0.7,
            "timeout": 90,
            "agent_first": False,
        }

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
            "label": "每日识图限额 (已停用)",
            "desc": "2026-09-26 起识图不限额，此项保留仅为兼容，改动无效果",
            "type": "int", "min": 1, "max": 30, "step": 1,
            "group": "每日配额",
        },
        "daily_tools_base_limit": {
            "label": "每日工具基础限额 (已停用)",
            "desc": "2026-09-26 起除生图外工具不限额，此项保留仅为兼容，改动无效果",
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
    # bot: "loput" = 主 bot (tavern), "luna" = peer bot (companion), "both" = 双 bot 共享

    _UNIFIED_TOOLS: tuple[dict, ...] = (
        {"name": "search_knowledge",      "label": "知识库搜索",       "category": "知识", "bot": "both", "desc": "搜索本地 ComfyUI/AI绘画知识库", "min_affinity": 1},
        {"name": "send_sticker",          "label": "发送表情",         "category": "社交", "bot": "both", "desc": "按情绪标签发送表情包到群聊", "min_affinity": 1},
        {"name": "parse_forwarded_message","label": "读转发",          "category": "社交", "bot": "both", "desc": "读取合并转发缓存内容", "min_affinity": 1},
        {"name": "remember_memory",       "label": "记住信息",         "category": "记忆", "bot": "both", "desc": "将用户提供的信息存入长期记忆", "min_affinity": 1},
        {"name": "get_memory",            "label": "查询记忆",         "category": "记忆", "bot": "both", "desc": "查询已记住的用户信息", "min_affinity": 1},
        # ═══ 双 bot 共享 — 搜索 ═══
        # 注: recall_long_term_memory 已移除 — 主 bot 侧无 schema/executor,
        #     peer bot 侧无实现, 属悬空注册 (违反单一真相源)。记忆检索统一用 get_memory。
        {"name": "web_search",            "label": "联网搜索",     "category": "搜索", "bot": "both", "desc": "SearXNG 联网搜索", "min_affinity": 1},
        {"name": "pixiv_search",         "label": "Pixiv 搜图",    "category": "搜索", "bot": "both", "desc": "Pixiv 插画搜索 (需 refresh_token)", "min_affinity": 1},
        {"name": "execute_code",          "label": "代码执行",     "category": "计算", "bot": "both", "desc": "专属Docker小黑屋执行 Python/JS/Bash（字符画/计算）", "min_affinity": 1},
        # ═══ 双 bot 共享 — 视觉+生图 ═══
        {"name": "describe_image",        "label": "识图",        "category": "视觉", "bot": "both", "desc": "VLM 图片内容解析", "min_affinity": 2},
        {"name": "describe_pixiv_refs",   "label": "参考图解析",   "category": "视觉", "bot": "both", "desc": "Pixiv 参考图暂存解析（对着画）", "min_affinity": 2},
        {"name": "describe_video",        "label": "看视频",      "category": "视觉", "bot": "both", "desc": "VLM 视频内容解析 (抽帧)", "min_affinity": 2},
        {"name": "describe_voice",        "label": "听语音",      "category": "视觉", "bot": "both", "desc": "语音下载转写 (不支持则元数据)", "min_affinity": 2},
        {"name": "generate_image",        "label": "AI 生图",     "category": "生图", "bot": "both", "desc": "AI 云端绘图 (NovelAI Lv.2+日限5 / PixAI Lv.3+日限3)", "min_affinity": 2},
        {"name": "edit_image",            "label": "图片编辑",     "category": "生图", "bot": "both", "desc": "以图生图/原图编辑 (与生图共享额度)", "min_affinity": 2},
        {"name": "resolve_character_identity","label": "角色核验", "category": "生图", "bot": "both", "desc": "联网核验角色 danbooru tag（生图表单补全）", "min_affinity": 1},
        # ═══ 双 bot 共享 — 媒体 ═══
        {"name": "video_extract",          "label": "视频提取",       "category": "媒体", "bot": "both", "desc": "提取B站/抖音视频直接发到群聊 (≤20MB)", "min_affinity": 1},
        {"name": "dispatch_subagents",     "label": "子代理派发",     "category": "系统", "bot": "both", "desc": "Main Hybrid 专属：并发派发子代理", "min_affinity": 1},
        # 注: L4D2 13 工具由 astrbot_plugin_suli_l4d2 独立管理，不进统一表；
        # 注: L-Port 三件套已下线（后端无容器，见 2026-10-13 清理）。
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
    # 2026-10-02: 生图暂关 (绘图后端未就绪) — 面板可按 bot 重开 (DB 键覆盖本默认值)
    _TOOLS_DEFAULT_DISABLED: frozenset[str] = frozenset({
        "describe_image", "describe_video", "describe_voice",
        "generate_image", "edit_image",
    })

    def is_tool_enabled(self, bot_id: str, tool_name: str) -> bool:
        """检查指定 bot 的某个工具是否启用。

        默认全部启用，除 _TOOLS_DEFAULT_DISABLED 中的工具
        (describe_image / describe_video / describe_voice / generate_image / edit_image)。
        面板按 bot 设置的 DB 键覆盖本默认值。
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
        其他值 (如 "moon") 匹配 bot 的 character_card 字段。
        bot_id 未知时按共享 (both/default) 处理。
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
        return tool_bot in ("both", "default")  # fallback: both=共享, default=主 bot

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
