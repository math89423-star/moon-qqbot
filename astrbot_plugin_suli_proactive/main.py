"""主动行为引擎 — AstrBot Star 插件入口 (双 Bot 通用)。

提供:
  - 私聊主动交互: 基于决策门控的定期主动消息调度
  - 群聊冷场破冰: 继承原有 proactive_speaker 能力
  - 消息监听: 用户活跃标记 + 休息指令检测

架构:
  本插件在 __init__ 时创建 ProactiveScheduler，在 initialize() 启动后台循环。
  消息监听通过 @astrbot_filter.event_message_type 注册，零侵入对接 AstrBot 事件流。
  双 bot 通用 — self_id gate 使用 _BOT_QQ_SET 白名单，per-bot 配置隔离。
"""

from __future__ import annotations

import logging
from typing import Any

from astrbot.api import AstrBotConfig  # noqa: TC002
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event import filter as astrbot_filter
from astrbot.api.star import Context, Star, register

from .candidate_pool import CandidatePool
from .config import Config
from .decision_gate import DecisionGate
from .reason_action import ReasonActionEngine
from .relationship import RelationshipManager
from .scheduler import ProactiveScheduler

logger = logging.getLogger(__name__)

# Bot QQ 号白名单 — 运行时从 dual_bot.get_bot_qq_set() 懒加载
# 不再硬编码 QQ 号。此常量仅作为模块加载时的 fallback。
_BOT_QQ_SET: set[str] = set()  # 运行时从 dual_bot 读取


@register("astrbot_plugin_suli_proactive", "暮恩", "主动行为引擎 (双Bot通用)", "1.0.0")
class MoonProactivePlugin(Star):
    """主动行为引擎 — 私聊/群聊主动交互调度 (双 Bot 通用)。

    提取自 private_companion proactive 核心模式, 全部参数化。
    零硬编码 persona 文本 — 所有角色内容来自 per-bot Config。
    self_id gate 使用 _BOT_QQ_SET 白名单, 双方各自独立调度。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self._astrbot_config = config if config is not None else getattr(context, "config", None)

        # 构建引擎组件链
        self._config = Config(**self._load_plugin_config())
        self._relationships = RelationshipManager(self._config)
        self._reason_engine = ReasonActionEngine(self._config)
        self._candidate_pool = CandidatePool(self._config)
        self._decision_gate = DecisionGate(
            self._config, self._relationships, self._reason_engine,
        )

        # 调度器
        self._scheduler = ProactiveScheduler(
            self._config,
            self._relationships,
            self._decision_gate,
            self._candidate_pool,
            available_abilities=self._detect_abilities(),
        )
        # 设置发送回调
        self._scheduler.set_send_callback(self._on_proactive_trigger)

        # 用于追踪观察到的用户
        self._observed_users: set[str] = set()

        # 注册到共享插件注册表 (供 suli_tavern 注入依赖)
        try:
            from astrbot_plugin_suli_tavern.plugin_registry import register_proactive_plugin
            register_proactive_plugin(self)
        except ImportError:
            logger.debug("plugin_registry 不可用 — suli_tavern 可能未加载")

    # ── 生命周期 ─────────────────────────────────────

    async def initialize(self) -> None:
        """插件启动 — 启动主动调度循环。"""
        if not self._config.enabled:
            logger.info("[MoonProactive] 总开关已关闭, 不启动调度器")
            return
        await self._scheduler.start()

    async def terminate(self) -> None:
        """插件停止 — 停止调度循环。"""
        await self._scheduler.stop()

    # ── 消息监听 ─────────────────────────────────────

    @astrbot_filter.event_message_type(astrbot_filter.EventMessageType.ALL, priority=5000)
    async def on_message_observe(self, event: AstrMessageEvent) -> None:
        """监听所有消息: 标记用户活跃 + 记录观测用户。

        priority=5000: 在逻辑插件之后、日志插件之前执行。
        """
        # ── Self-ID 身份门控: 拦截自回声 + 交叉回声 (fail-closed) ──
        try:
            _sid = str(getattr(event, "get_self_id", lambda: "")() or "")
        except Exception:
            _sid = ""
        if not _sid or _sid not in _BOT_QQ_SET:
            return
        try:
            user_id = str(event.get_sender_id())
            self._observed_users.add(user_id)
            self._relationships.mark_user_active(user_id)
        except Exception:
            logger.debug("消息观测异常", exc_info=True)

    @astrbot_filter.event_message_type(astrbot_filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent) -> None:
        """私聊消息: 检测休息指令 + 维持会话标记。"""
        # ── Self-ID 身份门控: 拦截自回声 + 交叉回声 (fail-closed) ──
        try:
            _sid = str(getattr(event, "get_self_id", lambda: "")() or "")
        except Exception:
            _sid = ""
        if not _sid or _sid not in _BOT_QQ_SET:
            return
        try:
            user_id = str(event.get_sender_id())
            text = str(getattr(event, "message_str", "") or "")

            # 休息指令: "休息吧" / "去睡觉" / "晚安" 等
            if self._detect_rest_command(text):
                self._relationships.set_user_rest(user_id)
                await event.send_text("好的，我先休息一阵子。需要我的时候随时喊我~")

        except Exception:
            logger.debug("私聊消息处理异常", exc_info=True)

    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """群聊消息: 更新群聊上下文中的用户活跃标记。

        此方法由 suli_tavern 的 group_chat.py 通过插件间接口调用。
        """
        try:
            user_id = str(event.get_sender_id())
            self._relationships.mark_user_active(user_id)
        except Exception:
            pass

    # ── 外部依赖注入 ────────────────────────────────

    def set_group_scheduler(self, group_scheduler: Any) -> None:
        """注入群聊调度器 (由 suli_tavern 在初始化时调用)。"""
        self._scheduler._group_scheduler = group_scheduler
        logger.info("群聊调度器已注入")

    def set_tavern(self, tavern: Any) -> None:
        """注入 TavernClient (由 suli_tavern 在初始化时调用)。

        TavernClient 提供 chat() 方法用于 LLM 调用。
        """
        self._scheduler.set_tavern(tavern)
        logger.info("TavernClient 已注入")

    # ── 公开 API ─────────────────────────────────────

    def get_relationship_manager(self) -> RelationshipManager:
        return self._relationships

    def get_candidate_pool(self) -> CandidatePool:
        return self._candidate_pool

    def offer_candidate(
        self, reason: str, action: str = "message",
        topic: str = "", motive: str = "", source: str = "external",
        score: float = 0.5,
    ) -> bool:
        """外部系统提供主动行为候选。"""
        from .candidate_pool import ProactiveCandidate
        return self._candidate_pool.offer(ProactiveCandidate(
            reason=reason, action=action, topic=topic,
            motive=motive, source=source, score=score,
        ))

    # ── 内部方法 ─────────────────────────────────────

    async def _on_proactive_trigger(self, candidate: Any) -> None:
        """主动行为触发回调 — 决策门控通过后调用。

        组装 prompt → LLM 生成 → 发送私聊消息。
        TavernClient 由 suli_tavern 通过 set_tavern() 注入。
        """
        tavern = self._scheduler._tavern
        if tavern is None:
            logger.warning("主动触发: TavernClient 未注入, 跳过")
            return

        # 确定目标用户
        target_id = self._resolve_target_user(candidate)
        if not target_id:
            logger.warning("主动触发: 无目标用户, 跳过")
            return

        # 构建 LLM 消息
        messages = self._build_proactive_messages(candidate, target_id)

        try:
            reply = await tavern.chat(
                messages,
                model="deepseek-v4-flash",
                temperature=0.85,
                max_tokens=256,
            )
            reply = (reply or "").strip()
            if not reply:
                logger.info("主动触发: LLM 返回空, 跳过 user=%s", target_id[:8])
                return

            await self._send_proactive_reply(target_id, reply)
            self._relationships.mark_proactive_sent(target_id)
            logger.info(
                "主动消息已发送: user=%s reason=%s action=%s reply=%.80s",
                target_id[:8], candidate.reason, candidate.action, reply[:80],
            )

        except Exception:
            logger.exception("主动 LLM/发送失败 user=%s", target_id[:8])

    def _resolve_target_user(self, candidate: Any) -> str:  # noqa: ARG002 (candidate 保留供后续 target_user 偏好使用)
        """解析主动消息的目标用户。"""
        # 优先: 正在发送状态的用户
        for uid, user in self._relationships.get_all_users().items():
            if user.proactive_sending:
                return uid
        # 回退: target_user_ids 中第一个
        if self._config.target_user_ids:
            return self._config.target_user_ids[0]
        return ""

    def _build_proactive_messages(self, candidate: Any, target_id: str) -> list[dict[str, str]]:  # noqa: ARG002
        """为主动消息构建 LLM 消息列表。

        组装简洁的 system prompt (角色名 + 动机 + 话题线索),
        引导 LLM 以角色身份自然发起对话。
        """
        char_name = getattr(self._config, "char_name", None) or "暮恩"
        motive = getattr(candidate, "motive", "") or "想和用户聊聊天"
        reason = getattr(candidate, "reason", "") or "check_in"
        topic = getattr(candidate, "topic", "") or ""

        topic_hint = f"\n话题线索: {topic}" if topic else ""

        system = (
            f"你是{char_name}——一个通过QQ群聊/私聊和用户交流的AI助手少女。"
            f"\n\n当前情境: {motive}"
            f"\n主动原因: {reason}"
            f"{topic_hint}"
            f"\n\n现在正是主动发起对话的时刻。请以{char_name}的身份"
            f"直接说出第一条消息——像真人聊天一样自然。"
            f"不要加'系统提示'、'根据指示'之类的元描述。"
            f"不要用Markdown。1-3句话就够了。"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "(该主动发起对话了——直接说出你的第一条消息吧)"},
        ]

    async def _send_proactive_reply(self, target_id: str, text: str) -> None:
        """发送主动私聊消息。"""
        try:
            from astrbot.api.message_components import Plain
            umo = f"qq:private:{target_id}"
            await self.context.send_message(umo, Plain(text))
        except Exception:
            logger.exception("主动消息发送失败 user=%s", target_id[:8])

    def _detect_rest_command(self, text: str) -> bool:
        """检测用户休息指令。"""
        patterns = ["休息吧", "去睡觉", "去休息", "晚安", "歇着", "退下"]
        text_lower = text.strip().lower()
        return any(p in text_lower for p in patterns)

    def _detect_abilities(self) -> set[str]:
        """检测可用的外部能力。"""
        abilities: set[str] = set()
        # 检查是否有图片生成能力 (后续对接 L-Port)
        # 检查是否有表情包系统 (已确认: meme_manager 可用)
        abilities.add("sticker")
        return abilities

    def _load_plugin_config(self) -> dict[str, Any]:
        """加载插件配置 — 合并 AstrBot 存储的配置和默认值。"""
        try:
            if hasattr(self._astrbot_config, "model_dump"):
                result = self._astrbot_config.model_dump()
                if isinstance(result, dict):
                    return result
            stored = getattr(self._astrbot_config, "as_dict", None)
            if callable(stored):
                result = stored()
                if isinstance(result, dict):
                    return result
        except Exception:
            pass
        return {}
