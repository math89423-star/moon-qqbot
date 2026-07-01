"""L-Port 桥接插件 (AstrBot 版) — /chat 多轮 LLM 对话。

AstrBot 插件 — L-Port LLM 配置桥接。核心业务逻辑 (config_reader / llm_client) 框架无关。
"""

from __future__ import annotations

import logging
import traceback
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

from .bot_db import get_bot_db
from .bot_config import get_config_service
from .llm_client import LLMChatService

logger = logging.getLogger(__name__)

PLUGIN_NAME = "astrbot_plugin_suli_bridge"


@register(
    PLUGIN_NAME,
    "math89423",
    "L-Port LLM 桥接: /chat 多轮对话 + 模型切换",
    "1.0.0",
)
class LPortBridgePlugin(Star):
    """L-Port LLM 桥接 — AstrBot 版。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)

        self._bot_db = get_bot_db()
        self._config_svc = get_config_service()
        self.chat_service: Optional[LLMChatService] = None
        self._init_error: str = ""

        try:
            self.chat_service = LLMChatService(self._bot_db)
            logger.info("L-Port 桥接插件初始化成功")
        except Exception as e:
            self._init_error = f"LLM 对话服务初始化失败: {e!s}"
            logger.error(self._init_error)

    # ── /chat 命令 ──────────────────────────────────

    @staticmethod
    def _self_id(event: AstrMessageEvent) -> str:
        """提取事件的 self_id (bot QQ 号)。"""
        for name in ("get_self_id", "get_bot_id"):
            func = getattr(event, name, None)
            if callable(func):
                try:
                    value = str(func() or "").strip()
                    if value:
                        return value
                except Exception:
                    pass
        message_obj = getattr(event, "message_obj", None)
        value = getattr(message_obj, "self_id", None) if message_obj is not None else None
        return str(value or "").strip()

    @filter.command("chat")
    async def on_chat_command(self, event: AstrMessageEvent):
        # ── /chat 命令已禁用 ──
        await event.send(Plain("❌ /chat 命令已禁用。请直接发送消息与暮恩对话。"))
        return

    async def _on_chat_command_disabled(self, event: AstrMessageEvent):
        # 保留旧入口供参考, 实际不可达
        # ── Self-ID 身份门控: 仅处理暮恩的事件 (fail-closed) ──
        if not self._self_id(event) or self._self_id(event) != "BOT_QQ_MAIN":
            return
        if self._init_error:
            await event.send(Plain(f"❌ {self._init_error}"))
            return

        user_id = str(event.get_sender_id())
        raw_msg = str(getattr(event, "message_str", "") or "").strip()
        args = raw_msg

        if not args:
            await self._send_help(event)
            return

        parts = args.split(maxsplit=1)
        sub = parts[0].lower()

        if sub in ("reset", "重置", "清除", "clear"):
            self.chat_service.reset(user_id)
            await event.send(Plain("✅ 会话已重置, 上下文已清除。"))
        elif sub in ("model", "模型", "当前模型"):
            await self._cmd_model(event)
        elif sub in ("list", "列表", "模型列表"):
            await self._cmd_list(event)
        elif sub in ("switch", "切换"):
            target = parts[1] if len(parts) > 1 else ""
            await self._cmd_switch(event, target)
        elif sub in ("help", "帮助", "?"):
            await self._send_help(event)
        else:
            await self._cmd_dialogue(event, user_id, args)

    # ── 子命令 ──────────────────────────────────────

    async def _send_help(self, event: AstrMessageEvent):
        cfg_info = ""
        if self.chat_service and self.chat_service.active_config:
            c = self.chat_service.active_config
            cfg_info = f"\n当前模型: {c.name} ({c.model_name})"

        help_text = (
            "🤖 L-Port AI 助手命令:{}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "  /chat <消息>       与 AI 对话\n"
            "  /chat reset        重置会话\n"
            "  /chat model        查看当前模型\n"
            "  /chat list         列出可用模型\n"
            "  /chat switch <id>  切换模型\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 支持多轮对话, 30分钟无操作自动清除会话"
        ).format(cfg_info)
        await event.send(Plain(help_text))

    async def _cmd_model(self, event: AstrMessageEvent):
        if not self.chat_service.active_config:
            await event.send(Plain("⚠️ 当前无可用 LLM 配置"))
            return

        c = self.chat_service.active_config
        key_preview = ""
        if c.api_key:
            k = c.api_key
            key_preview = f"{k[:4]}****{k[-4:]}" if len(k) > 8 else "****"

        msg = (
            f"📡 当前 LLM 配置\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"  名称: {c.name}\n"
            f"  ID: {c.id}\n"
            f"  Provider: {c.provider}\n"
            f"  模型: {c.model_name}\n"
            f"  端点: {c.normalized_base_url}\n"
            f"  API Key: {key_preview}\n"
        )
        if c.token_budget_cap:
            msg += f"  Token 上限: {c.token_budget_cap}\n"
        await event.send(Plain(msg))

    async def _cmd_list(self, event: AstrMessageEvent):
        all_configs = self._bot_db.list_llm_configs()
        configs = [c for c in all_configs if c.is_llm]
        if not configs:
            await event.send(Plain("⚠️ 未找到可用的 LLM 配置"))
            return

        active_id = self.chat_service.active_config.id if self.chat_service.active_config else None

        lines = ["📋 可用 LLM 配置:"]
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        for c in configs:
            mark = "✅" if c.id == active_id else "  "
            key_status = "有密钥" if c.api_key else "无密钥"
            lines.append(
                f"  [{c.id}] {mark} {c.name}\n"
                f"       provider={c.provider}  model={c.model_name}  {key_status}"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 使用 /chat switch <id> 切换模型")
        await event.send(Plain("\n".join(lines)))

    async def _cmd_switch(self, event: AstrMessageEvent, target: str):
        if not target:
            await event.send(Plain("⚠️ 用法: /chat switch <id>\n请先 /chat list 查看可用配置"))
            return

        try:
            config_id = int(target)
        except ValueError:
            await event.send(Plain(f"⚠️ 无效 ID: {target} (需要数字)"))
            return

        cfg = self._bot_db.get_llm_config(config_id)
        if not cfg:
            await event.send(Plain(f"❌ 配置 ID {config_id} 不存在"))
            return
        if not cfg.is_llm:
            await event.send(Plain(
                f"⚠️ 配置 [{config_id}] {cfg.name} 是 VLM/本地模型，不是云端 LLM, 无法用于对话"
            ))
            return

        ok = self.chat_service.switch_model(config_id)
        if ok:
            await event.send(Plain(
                f"✅ 已切换到: {self.chat_service.active_config.name}\n"
                f"   模型: {self.chat_service.active_config.model_name}"
            ))
        else:
            await event.send(Plain(f"❌ 切换失败, 请检查配置 [{config_id}] 是否完整"))

    async def _cmd_dialogue(self, event: AstrMessageEvent, user_id: str, message: str):
        if not message.strip():
            await self._send_help(event)
            return

        try:
            reply = await self.chat_service.chat(user_id, message)

            max_len = 400
            if len(reply) <= max_len:
                await event.send(Plain(reply))
            else:
                paragraphs = reply.split("\n")
                buffer = ""
                for para in paragraphs:
                    if len(buffer) + len(para) + 1 > max_len and buffer:
                        await event.send(Plain(buffer.strip()))
                        buffer = para + "\n"
                    else:
                        buffer += para + "\n"
                if buffer.strip():
                    await event.send(Plain(buffer.strip()))

        except RuntimeError as e:
            await event.send(Plain(f"❌ {e!s}"))
        except Exception as e:
            logger.error("对话失败 user=%s: %s\n%s", user_id, str(e), traceback.format_exc())
            await event.send(Plain(f"⚠️ AI 回复失败, 请稍后重试。\n({str(e)[:100]})"))
