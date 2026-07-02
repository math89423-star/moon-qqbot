"""Group Summarizer — 群聊定期总结 Agent (Layer 2 Task Agent)。

设计:
  - 每 N 条消息 或 每 M 分钟触发一次 (取较早者)
  - 取最近 200 条消息 → flash 模型生成 3-5 句摘要
  - 输出写入 JSON 文件 + SQLite 表
  - 完全异步 fire-and-forget，不阻塞回复管线
  - 冷却: 每群每小时最多 1 次

用法:
  from .group_summarizer import GroupSummarizer

  asyncio.create_task(
      GroupSummarizer.summarize(tavern, ctx, config)
  )
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 存储目录

def _get_plugin_data_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        _base = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_suli_intelligence"
    except ImportError:
        _base = Path("data/plugin_data/astrbot_plugin_suli_intelligence")
    _base.mkdir(parents=True, exist_ok=True)
    return _base
_SUMMARY_DIR = _get_plugin_data_dir() / "group_summaries"

# 冷却 (秒) — 每群每小时最多 1 次
_SUMMARY_COOLDOWN = 3600

# 模块级冷却: {"{bot_id}:{group_id}": last_summary_timestamp} — per-bot 隔离
_last_summary_at: dict[str, float] = {}

# ── System prompt ──────────────────────────────────────────────

_SUMMARY_SYSTEM = """你是群聊观察员。根据最近一批群聊消息，用 3-5 句话总结这段时间的话题和氛围。

[关注点]
- 主要话题: 大家在聊什么 (技术/闲聊/生图/游戏...)
- 关键讨论: 有没有深入的技术讨论或争议
- 氛围: 热闹/冷清/欢乐/严肃
- 活跃用户: 有没有特别活跃或有趣的人 (只提特征，不点名)

[规则]
- 只基于提供的消息，不要脑补
- 3-5 句中文字，简洁
- 不要评价 bot 自己
- 不要用"今天""最近"等时间词，用"这段时间"
- 如果消息太少 (<5条) 或全是无意义内容，输出 "无" """

_MAX_MSGS = 200      # 最多取最近 N 条
_MAX_MSG_LEN = 100   # 每条消息截断长度


class GroupSummarizer:
    """群聊总结 Agent — 纯静态方法，模块级冷却。"""

    @staticmethod
    async def summarize(
        tavern,  # duck-typed: .chat(messages, temperature, max_tokens)
        ctx,     # GroupChatContext (duck-typed)
        config,  # Config
        bot_id: str = "",
    ) -> str | None:
        """生成一次群聊总结。

        Args:
            bot_id: bot QQ 号 (per-bot 隔离冷却)

        Returns:
            摘要文本，或 None (冷却中/无内容/失败)
        """
        enabled = getattr(config, "group_summary_enabled", True)
        if not enabled:
            return None

        group_id = getattr(ctx, "group_id", 0)
        if not group_id:
            return None

        # ── 冷却检查 (per-bot) ──
        _cd_key = f"{bot_id}:{group_id}" if bot_id else str(group_id)
        now = time.time()
        last = _last_summary_at.get(_cd_key, 0)
        if now - last < _SUMMARY_COOLDOWN:
            logger.debug("GroupSummarizer: bot=%s 群 %d 冷却中 (%.0fs)", bot_id, group_id, now - last)
            return None

        _last_summary_at[_cd_key] = now

        # ── 收集消息 ──
        messages = getattr(ctx, "messages", []) or []
        if not messages:
            return None

        recent = messages[-_MAX_MSGS:]
        # 过滤 bot 自身消息 + 空消息
        user_msgs = []
        for m in recent:
            uid = str(m.get("user_id", ""))
            if uid.startswith("bot_"):
                continue
            content = str(m.get("content", "")).strip()
            if not content:
                continue
            if len(content) > _MAX_MSG_LEN:
                content = content[:_MAX_MSG_LEN - 3] + "..."
            name = str(m.get("user_name", uid))
            user_msgs.append(f"{name}: {content}")

        if len(user_msgs) < 5:
            return None  # 太少，不值得总结

        msg_range_start = len(messages) - len(recent)
        msg_range_end = len(messages) - 1

        # ── 构建 prompt ──
        prompt = (
            f"--- 群聊最近 {len(user_msgs)} 条消息 ---\n"
            + "\n".join(user_msgs)
            + "\n\n请总结这段时间的群聊。3-5 句话。"
        )

        # ── LLM 调用 (timeout=30s) ──
        _bg_llm = {}
        try:
            from astrbot_plugin_suli_tavern.service.bot_config import get_config_service
            _bg_llm = get_config_service().resolve_background_llm(bot_id, "group_summary")
        except Exception:
            logger.warning("GroupSummarizer: resolve_background_llm 失败, fallback 默认", exc_info=True)

        try:
            raw = await asyncio.wait_for(
                tavern.chat(
                    [
                        {"role": "system", "content": _SUMMARY_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=200,
                    model=_bg_llm.get("model", "deepseek-v4-flash"),
                    api_base=_bg_llm.get("api_base", ""),
                    api_key=_bg_llm.get("api_key", ""),
                    extra_params=_bg_llm.get("extra_params"),
                ),
                timeout=30,
            )
        except TimeoutError:
            logger.warning("GroupSummarizer.summarize: 群 %d LLM 调用超时 (30s)", group_id)
            return None
        except Exception:
            logger.warning("GroupSummarizer: 群 %d LLM 调用失败", group_id, exc_info=True)
            return None

        if not raw or raw.strip() == "无":
            return None

        summary_text = raw.strip()
        logger.info(
            "GroupSummarizer: 群 %d 生成摘要 (%d 字)",
            group_id, len(summary_text),
        )

        # ── 持久化 ──
        await _save_summary(
            group_id, summary_text,
            msg_range_start, msg_range_end, now,
        )

        # ── 重置消息计数器 ──
        if hasattr(ctx, "message_count_since_last_summary"):
            ctx.message_count_since_last_summary = 0
        if hasattr(ctx, "last_summary_at"):
            ctx.last_summary_at = now

        return summary_text


async def _save_summary(
    group_id: int,
    summary_text: str,
    msg_start: int,
    msg_end: int,
    timestamp: float,
) -> None:
    """写入 JSON 文件 + SQLite。"""

    # 1. JSON 文件
    try:
        _SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
        path = _SUMMARY_DIR / f"{group_id}.json"

        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass

        entries = existing.get("summaries", [])
        entries.append({
            "timestamp": timestamp,
            "text": summary_text,
            "message_range": [msg_start, msg_end],
        })

        # 最多保留 50 条历史摘要
        if len(entries) > 50:
            entries = entries[-50:]

        existing["group_id"] = group_id
        existing["summaries"] = entries
        existing["last_updated"] = timestamp

        path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.debug("GroupSummarizer: JSON 写入失败", exc_info=True)

    # 2. SQLite
    try:
        from astrbot_plugin_suli_tavern.service.bot_db import get_bot_db
        db = get_bot_db()
        db.conn.execute(
            "INSERT INTO group_summaries "
            "(group_id, summary_text, message_range_start, message_range_end, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (group_id, summary_text, msg_start, msg_end, timestamp),
        )
        db.conn.commit()
    except Exception:
        logger.debug("GroupSummarizer: SQLite 写入失败", exc_info=True)
