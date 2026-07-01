"""对话连续性 Agent — 轻量 LLM 判断用户是否在持续与 bot 对话。

设计:
  - 规则做粗筛 (时间+消息窗口) → agent 做语义判断
  - 极轻量: flash 模型, ~200 input tokens, 1 output token
  - 三级决策: continue (延续) / fading (模糊) / end (结束)

用法:
  from .conversation_agent import ConversationAgent

  decision = await ConversationAgent.decide(
      tavern, thread_context, user_message, config,
  )
  # → "continue" | "fading" | "end"
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)

# ── Agent system prompt (极简, ~100 tokens) ──────────────────

_AGENT_SYSTEM_TEMPLATE = (
    "你是群聊对话分析器。判断用户的新消息是否在继续与「{bot_name}」对话。\n"
    "标准:\n"
    "- continue: 用户明显在继续跟{bot_name}说话 (追问、回应、接着聊同一话题)\n"
    "- fading: 不确定用户是在跟{bot_name}说还是跟群里其他人说 (模糊边界)\n"
    "- end: 用户明显转移了话题、在跟别人说话、或对话自然结束\n"
    "只回一个词: continue, fading, 或 end。"
)

# ── 用户消息截断长度 ───────────────────────────────────────

_MAX_USER_MSG_LEN = 100


class ConversationAgent:
    """对话连续性分析 Agent。

    纯静态方法，无内部状态。成本: flash 模型 ~200 input + 1 output token。
    """

    @staticmethod
    async def decide(
        tavern,       # duck-typed: .chat(messages, temperature, max_tokens)
        thread_context: list[dict],  # [{role, content}] — 最近几条 bot 与用户的交换
        user_name: str,
        user_message: str,
        time_since_reply: float,     # 秒
        msgs_since_reply: int,
        config: Config,
        bot_name: str = "暮恩",
    ) -> str:
        """判断用户是否在继续与 bot 对话。

        Args:
            tavern: TavernClient (需 .chat() 方法)
            thread_context: bot 与该用户最近的对话片段
            user_name: 用户名
            user_message: 用户最新消息内容
            time_since_reply: 距 bot 上次回复的秒数
            msgs_since_reply: 距 bot 上次回复的消息数
            config: Config
            bot_name: bot 的名称 (用于区分不同 bot 的对话分析)

        Returns:
            'continue' | 'fading' | 'end'
            异常时返回 'continue' (安全侧: 宁可多回应不少回应)
        """
        # 构建分析上下文
        context_lines = [f"--- 最近对话 ({bot_name} ↔ 该用户) ---"]
        for msg in thread_context[-6:]:  # 最多最近 3 轮交换
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if len(content) > 120:
                content = content[:117] + "..."
            label = bot_name if role == "assistant" else user_name
            context_lines.append(f"{label}: {content}")

        # 用户最新消息
        truncated_msg = user_message
        if len(truncated_msg) > _MAX_USER_MSG_LEN:
            truncated_msg = truncated_msg[:_MAX_USER_MSG_LEN - 3] + "..."

        context_lines.append("")
        context_lines.append(
            f"[现在] {user_name} 又说: 「{truncated_msg}」\n"
            f"(距{bot_name}上次回复 {time_since_reply:.0f}秒, "
            f"中间插了 {msgs_since_reply} 条消息)"
        )

        agent_messages = [
            {"role": "system", "content": _AGENT_SYSTEM_TEMPLATE.format(bot_name=bot_name)},
            {"role": "user", "content": "\n".join(context_lines)},
        ]

        try:
            result = await asyncio.wait_for(
                tavern.chat(
                    agent_messages,
                    temperature=0.1,   # 极低温度 — 这不是创意任务
                    max_tokens=5,
                ),
                timeout=10,
            )
            result = result.strip().lower()

            if "continue" in result:
                decision = "continue"
            elif "fading" in result:
                decision = "fading"
            elif "end" in result:
                decision = "end"
            else:
                logger.debug("Agent 输出无法解析: %r → 默认 continue", result[:30])
                decision = "continue"

            logger.debug(
                "对话连续性: %s (user=%s age=%.0fs msgs=%d raw=%r)",
                decision, user_name, time_since_reply, msgs_since_reply, result[:20],
            )
            return decision

        except TimeoutError:
            logger.warning("ConversationAgent.decide: LLM 调用超时 (10s), 默认 continue 不阻塞用户")
            return "continue"
        except Exception:
            logger.debug("ConversationAgent 调用异常，默认 continue", exc_info=True)
            return "continue"
