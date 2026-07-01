"""LLM 对话客户端 — 基于 bot 自有 LLM 配置, 带会话记忆。

特性:
  - 从 bot DB (llm_config 表) 取配置, 用 openai AsyncOpenAI 调 API
  - 多用户会话隔离 (内存字典, keyed by user_id)
  - 单用户最大历史 30 条消息, 超出自动截断
  - 支持 /chat switch 切换模型 (软切换)
  - 会话超时: 30 分钟无活动自动清理
"""

from __future__ import annotations

import time
import logging
from typing import Dict, List, Optional, TYPE_CHECKING

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from .bot_db import BotDatabase, LLMConfigRO

logger = logging.getLogger(__name__)

# ── 系统提示词 ──────────────────────────────────────────
DEFAULT_SYSTEM_PROMPT = (
    "你是暮恩, 一个通过 QQ 提供服务的 AI 助手。\n"
    "当前机器人正处于功能开发阶段，更多能力敬请期待。\n"
    "你有以下能力:\n"
    "- 回答各类知识问题\n"
    "- 帮助用户构思 AI 绘画的 prompt 和创意\n"
    "- 讨论动漫、游戏、艺术相关话题\n"
    "- 提供 Pixiv 搜图建议和标签参考\n"
    "请用中文回复, 保持简洁友好, 不要使用 emoji。对于 AI 绘画相关的问题, "
    "可以主动提供详细的 prompt 建议和风格参考。"
)

# ── 会话配置 ──────────────────────────────────────────
_DEFAULT_MAX_HISTORY = 30  # 单用户最大消息条数 (user + assistant 对)
SESSION_TTL = 1800  # 会话超时 (秒), 30 分钟无活动自动清理


class Conversation:
    """单个用户的会话状态"""

    def __init__(self, user_id: str, system_prompt: str = DEFAULT_SYSTEM_PROMPT,
                 max_history: int = _DEFAULT_MAX_HISTORY):
        self.user_id = user_id
        self.system_prompt = system_prompt
        self.messages: List[Dict[str, str]] = []
        self.last_active = time.time()
        self.model_name: str = ""  # 当前使用的模型名, 用于展示
        self._max_history = max_history

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})
        self.last_active = time.time()
        self._trim()

    def add_assistant(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})
        self.last_active = time.time()
        self._trim()

    def build_messages(self) -> List[Dict[str, str]]:
        """构建完整消息列表 (system + history)"""
        return [{"role": "system", "content": self.system_prompt}] + self.messages

    def _trim(self) -> None:
        """超过 max_history 条时, 保留最近的消息 (最少保留 system prompt 外的 4 条)"""
        if len(self.messages) > self._max_history:
            # 保留最后 max_history 条, 但至少 4 条
            keep = max(4, self._max_history)
            self.messages = self.messages[-keep:]

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > SESSION_TTL

    @property
    def message_count(self) -> int:
        return len(self.messages)


class LLMChatService:
    """LLM 对话服务 — 管理多用户会话与 API 调用。

    用法:
        from .bot_db import get_bot_db
        chat = LLMChatService(get_bot_db())

        # 使用当前激活的 LLM
        reply = await chat.chat(user_id="12345", message="你好")

        # 切换到指定配置 (仅影响该 service 实例)
        chat.switch_model(3)

        # 重置某用户的会话
        chat.reset("12345")
    """

    def __init__(self, db: BotDatabase, default_config_id: Optional[int] = None):
        self._db = db
        self._conversations: Dict[str, Conversation] = {}
        self._client: Optional[AsyncOpenAI] = None
        self._active_config: Optional[LLMConfigRO] = None
        self._override_config_id: Optional[int] = None

        # 初始化: 优先用指定的 config_id, 否则用当前激活配置
        init_id = default_config_id
        if not init_id:
            from .bot_config import get_config_service
            init_id = get_config_service().get_active_llm_id()

        cfg = db.get_llm_config(init_id) if init_id else None

        if cfg:
            self._set_config(cfg)
        else:
            logger.warning("LLMChatService: 未找到可用的 LLM 配置, 请先在管理面板中设置")

    # ── 参数读取 ──────────────────────────────────────

    @staticmethod
    def _get_bridge_param(key: str, default: object) -> object:
        """从 BotConfigService 读取参数, fallback 到硬编码默认值。"""
        try:
            from .bot_config import get_config_service
            return get_config_service().get_chat_param(key)
        except Exception:
            return default

    # ── 配置管理 ──────────────────────────────────────────

    def _set_config(self, cfg: LLMConfigRO) -> None:
        """更新内部客户端与活跃配置"""
        self._active_config = cfg
        self._client = AsyncOpenAI(
            api_key=cfg.api_key,
            base_url=cfg.normalized_base_url,
        )
        logger.info(
            "LLMChatService: 已加载配置 id=%d name=%s model=%s base_url=%s",
            cfg.id, cfg.name, cfg.model_name, cfg.normalized_base_url,
        )

    @property
    def active_config(self) -> Optional[LLMConfigRO]:
        return self._active_config

    def switch_model(self, config_id: int) -> bool:
        """切换到指定 LLM 配置 (仅影响运行时)。

        Returns:
            True 表示切换成功, False 表示配置不存在或不可用。
        """
        cfg = self._db.get_llm_config(config_id)
        if not cfg:
            logger.warning("switch_model: config id=%d 不存在", config_id)
            return False
        if not cfg.is_llm:
            logger.warning("switch_model: config id=%d 不是 LLM (provider=%s)", config_id, cfg.provider)
            return False
        if not cfg.api_key and cfg.provider not in ("llama", "ollama"):
            logger.warning("switch_model: config id=%d 缺少 api_key", config_id)
            return False

        self._set_config(cfg)
        self._override_config_id = config_id
        return True

    # ── 会话管理 ──────────────────────────────────────────

    def _get_or_create_conv(self, user_id: str) -> Conversation:
        """获取或创建用户会话, 同时清理过期会话"""
        self._cleanup_expired()
        conv = self._conversations.get(user_id)
        if conv is None:
            max_hist = int(self._get_bridge_param("bridge_chat_max_history", 30) or 30)
            conv = Conversation(user_id, max_history=max_hist)
            self._conversations[user_id] = conv
        return conv

    def _cleanup_expired(self) -> int:
        """清理过期会话, 返回清理数量"""
        expired = [
            uid for uid, conv in self._conversations.items() if conv.is_expired
        ]
        for uid in expired:
            del self._conversations[uid]
        if expired:
            logger.info("LLMChatService: 清理了 %d 个过期会话", len(expired))
        return len(expired)

    def reset(self, user_id: str) -> None:
        """重置指定用户的会话历史"""
        self._conversations.pop(user_id, None)

    def get_history(self, user_id: str) -> List[Dict[str, str]]:
        """获取用户的对话历史 (不含 system prompt)"""
        conv = self._conversations.get(user_id)
        return list(conv.messages) if conv else []

    # ── 核心: 对话 ───────────────────────────────────────

    async def chat(self, user_id: str, message: str) -> str:
        """发送消息并获取 LLM 回复。

        Args:
            user_id: 用户唯一标识 (QQ 号)
            message: 用户消息文本

        Returns:
            LLM 回复文本

        Raises:
            RuntimeError: 无可用 LLM 配置
            Exception: API 调用失败
        """
        if not self._client or not self._active_config:
            raise RuntimeError("无可用 LLM 配置, 请在管理面板中设置")

        conv = self._get_or_create_conv(user_id)
        conv.add_user(message)

        try:
            try:
                from .bot_config import get_config_service
                temperature = get_config_service().get_temperature("bridge_chat")
            except Exception:
                temperature = 0.7  # fallback 默认值

            max_tok = int(self._get_bridge_param("bridge_chat_max_tokens", 2048) or 2048)
            response = await self._client.chat.completions.create(
                model=self._active_config.model_name,
                messages=conv.build_messages(),
                temperature=temperature,
                max_tokens=max_tok,
            )
            reply = response.choices[0].message.content or "(空回复)"
            conv.add_assistant(reply)
            conv.model_name = self._active_config.model_name
            return reply

        except Exception as e:
            # 失败时移除刚添加的 user 消息, 避免污染历史
            if conv.messages and conv.messages[-1]["role"] == "user":
                conv.messages.pop()
            logger.error("LLM API 调用失败 user=%s: %s", user_id, str(e))
            raise

    async def chat_stream(self, user_id: str, message: str):
        """流式对话 — 生成器, 逐块产出文本。

        Usage:
            async for chunk in chat.chat_stream(uid, msg):
                accumulated += chunk
        """
        if not self._client or not self._active_config:
            raise RuntimeError("无可用 LLM 配置")

        conv = self._get_or_create_conv(user_id)
        conv.add_user(message)

        full_reply = ""
        try:
            max_tok = int(self._get_bridge_param("bridge_chat_max_tokens", 2048) or 2048)
            stream = await self._client.chat.completions.create(
                model=self._active_config.model_name,
                messages=conv.build_messages(),
                temperature=0.7,
                max_tokens=max_tok,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    full_reply += delta.content
                    yield delta.content

            conv.add_assistant(full_reply)
            conv.model_name = self._active_config.model_name

        except Exception as e:
            if conv.messages and conv.messages[-1]["role"] == "user":
                conv.messages.pop()
            logger.error("LLM 流式调用失败 user=%s: %s", user_id, str(e))
            raise
