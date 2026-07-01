"""上下文生命周期 — 记忆提取蒸馏 + 压缩 + 记忆上下文设置。

从 group_chat.py 提取，用于管理群聊上下文信息的生命周期:
  记忆提取 → 核心蒸馏 → 上下文压缩

用法:
  from .context_lifecycle import setup_memory_ctx, extract_and_distill, maybe_compress
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def setup_memory_ctx(ctx, trigger_user_id: str, bot_id: str = "") -> None:
    """设置记忆工具上下文 — 让 remember_memory/get_memory 知道是谁在说话。

    Args:
        bot_id: per-bot 隔离 — 当前 bot 的 QQ 号
    """
    from ..tools import set_memory_context

    user_names: dict[str, str] = {}
    trigger_name = ""
    for msg in ctx.messages[-30:]:
        uid = str(msg.get("user_id", ""))
        name = str(msg.get("user_name", ""))
        if uid and name and not uid.startswith("bot_"):
            user_names[name] = uid
            if uid == trigger_user_id:
                trigger_name = name
    set_memory_context(bot_id, {
        "trigger_user_id": trigger_user_id,
        "trigger_user_name": trigger_name,
        "all_user_names": user_names,
    })


async def extract_and_distill(
    ctx,
    trigger_user_id: str,
    *,
    memory_store,
    tier_manager=None,
) -> None:
    """异步提取 daily 记忆 + 条件蒸馏 core 特征。

    在每次 LLM 回复后异步执行, 不阻塞回复管线。
    """
    try:
        await memory_store.extract_async(ctx, trigger_user_id)
    except Exception:
        logger.warning("daily 记忆提取失败", exc_info=True)

    # 条件蒸馏: daily → core
    if tier_manager is not None:
        try:
            await tier_manager.maybe_distill(trigger_user_id)
        except Exception:
            logger.debug("core 蒸馏触发失败", exc_info=True)


async def maybe_compress(
    ctx,
    *,
    config,
    chat_param_fn,
    tavern,
    resolve_provider_fn,
    record_usage_fn,
    llm_semaphore,
    sanitize_fn,
) -> None:
    """超过阈值时触发 LLM 压缩，替代直接截断。

    将所有依赖显式注入，零 self 耦合。
    """
    threshold = chat_param_fn("group_chat_compress_threshold", "group_chat_compress_threshold")
    if len(ctx.messages) <= threshold:
        return

    keep = chat_param_fn("group_chat_compress_keep_recent", "group_chat_compress_keep_recent")
    old_messages = ctx.messages[:-keep] if len(ctx.messages) > keep else []

    if not old_messages:
        return

    logger.info(
        "群 %d: 触发上下文压缩 (%d 条 → 摘要)",
        ctx.group_id, len(old_messages),
    )

    new_summary = await _compress_summary(
        old_messages,
        existing_summary=ctx.summary,
        group_id=ctx.group_id,
        tavern=tavern,
        config=config,
        resolve_provider_fn=resolve_provider_fn,
        record_usage_fn=record_usage_fn,
        llm_semaphore=llm_semaphore,
        sanitize_fn=sanitize_fn,
    )
    if new_summary:
        ctx.summary = new_summary
        ctx.messages = ctx.messages[-keep:]
        ctx.summary_timestamp = old_messages[-1]["timestamp"]
        logger.info(
            "群 %d: 压缩完成, 摘要 %d 字, 保留 %d 条",
            ctx.group_id, len(new_summary), len(ctx.messages),
        )


async def _compress_summary(
    old_messages: list[dict],
    *,
    existing_summary: str = "",
    group_id: int = 0,
    tavern,
    config,
    resolve_provider_fn,
    record_usage_fn,
    llm_semaphore,
    sanitize_fn,
) -> str:
    """调用 LLM 将旧消息列表压缩为摘要。"""
    lines = []
    for msg in old_messages[-30:]:  # 最多压缩 30 条
        name = msg["user_name"]
        text = sanitize_fn(msg["content"])
        if len(text) > 150:
            text = text[:147] + "..."
        lines.append(f"{name}: {text}")

    existing_hint = (
        f"\n之前的摘要: {existing_summary}\n请合并更新。"
        if existing_summary else ""
    )

    compress_messages = [
        {
            "role": "system",
            "content": (
                "你是群聊记录总结助手。将聊天记录压缩为简洁摘要（200字内），"
                "保留: 谁说了什么重要的事、讨论了什么话题、值得记住的细节。"
                "用连贯的叙述句，不要编号列表。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请总结以下群聊记录:\n"
                + "\n".join(lines)
                + existing_hint
            ),
        },
    ]

    try:
        from ..service.bot_config import get_config_service
        compress_temp = get_config_service().get_temperature("context_compress")
    except Exception:
        compress_temp = config.group_chat_compress_temperature

    try:
        if llm_semaphore is not None:
            async with llm_semaphore:
                result = await asyncio.wait_for(
                    tavern.chat(
                        compress_messages,
                        temperature=compress_temp,
                        max_tokens=256,
                        provider=resolve_provider_fn(),
                        api_base="",
                        api_key="",
                    ),
                    timeout=30,
                )
        else:
            result = await asyncio.wait_for(
                tavern.chat(
                    compress_messages,
                    temperature=compress_temp,
                    max_tokens=256,
                    provider=resolve_provider_fn(),
                    api_base="",
                    api_key="",
                ),
                timeout=30,
            )
        record_usage_fn(scenario="compress", group_id=str(group_id))
        return result.strip() if result else ""
    except TimeoutError:
        logger.warning("上下文压缩 LLM 调用超时 (30s), 跳过本次压缩")
        return existing_summary
    except Exception:
        logger.exception("群聊上下文压缩失败")
        return existing_summary
