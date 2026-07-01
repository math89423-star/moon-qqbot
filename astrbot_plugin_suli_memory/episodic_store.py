"""情节记忆存储 — 槽过期时归档 thread_summary，零新增 LLM 调用。

纯被动归档: 注意力槽离开时自动保存 thread_summary，不做蒸馏/去重。
检索: 关键词召回 (复用 _tokenize), top_n 封顶防噪音。

路径: bot_episodes/{bot_id}/{group_id}.json
格式: {bot_id, group_id, entries: [{summary, participants, topic_keywords, created_at}]}

用法:
  from astrbot_plugin_suli_memory.episodic_store import EpisodicStore

  store = EpisodicStore(data_dir="/data/plugin_data/astrbot_plugin_suli_memory")
  await store.archive(bot_id, group_id, summary, participants, topic_anchor)
  results = store.query(bot_id, group_id, "最近在聊什么", top_n=2)
"""

from __future__ import annotations

import json
import logging
import os
import re as _re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── tokenize (复用 user_memory.py 的实现) ──


def _tokenize(text: str) -> set[str]:
    """中文+英文多粒度分词。优先使用 suli_services，不可用时回退内置实现。"""
    try:
        from astrbot_plugin_suli_services.knowledge_base import tokenize as _ext_tokenize  # type: ignore[assignment]
        return _ext_tokenize(text)
    except ImportError:
        pass
    tokens: set[str] = set()
    text_lower = text.lower()
    for m in _re.finditer(r"[a-z][a-z0-9_]+", text_lower):
        tokens.add(m.group())
    for i in range(len(text) - 1):
        if not (text[i].isspace() or text[i + 1].isspace()):
            tokens.add(text[i:i + 2])
    return tokens


def _extract_keywords(topic_anchor: str, max_keywords: int = 5) -> list[str]:
    """从话题锚点提取关键词 (纯规则，零 LLM)。

    取 _tokenize 后最长的几个 token 作为关键词。
    """
    if not topic_anchor:
        return []
    tokens = _tokenize(topic_anchor)
    sorted_tokens = sorted(tokens, key=len, reverse=True)
    return sorted_tokens[:max_keywords]


class EpisodicStore:
    """情节记忆存储 — 槽过期归档 + 关键词检索。

    纯 JSON 存储, 每 (bot_id, group_id) 一个文件。
    封顶 50 条 / 群, FIFO 淘汰。
    """

    MAX_ENTRIES = 50

    def __init__(self, data_dir: str = "") -> None:
        if not data_dir:
            # 默认路径 — 与其他记忆数据平级
            data_dir = os.path.join(
                os.path.dirname(__file__) if "__file__" in dir() else ".",
                "..", "..", "..",
                "data", "plugin_data", "astrbot_plugin_suli_memory",
            )
        self._base_dir = os.path.join(data_dir, "bot_episodes")

    # ── 路径 ──────────────────────────────────────────────

    def _file_path(self, bot_id: str, group_id: int) -> str:
        return os.path.join(self._base_dir, bot_id, f"{group_id}.json")

    def _load(self, bot_id: str, group_id: int) -> dict:
        path = self._file_path(bot_id, group_id)
        if not os.path.exists(path):
            return {
                "bot_id": bot_id,
                "group_id": group_id,
                "entries": [],
                "last_updated": 0.0,
            }
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {
                    "bot_id": bot_id,
                    "group_id": group_id,
                    "entries": [],
                    "last_updated": 0.0,
                }
            data.setdefault("entries", [])
            data.setdefault("last_updated", 0.0)
            return data
        except (json.JSONDecodeError, OSError):
            logger.warning("情节记忆文件损坏，重新初始化: %s", path)
            return {
                "bot_id": bot_id,
                "group_id": group_id,
                "entries": [],
                "last_updated": 0.0,
            }

    def _save(self, data: dict) -> None:
        bot_id = data.get("bot_id", "")
        group_id = data.get("group_id", 0)
        path = self._file_path(bot_id, group_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data["last_updated"] = time.time()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── 归档 ──────────────────────────────────────────────

    def archive(
        self,
        bot_id: str,
        group_id: int,
        summary: str,
        participants: set[str] | list[str] | None = None,
        topic_anchor: str = "",
    ) -> bool:
        """归档一条情节记录。空 summary 跳过。

        Returns:
            True 如果成功归档，False 如果跳过 (空 summary)。
        """
        summary = (summary or "").strip()
        if not summary:
            return False

        data = self._load(bot_id, group_id)
        entries: list[dict] = data["entries"]

        # 去重: 相同 summary 已在最近 5 条中 → 跳过
        recent_summaries = {e.get("summary", "") for e in entries[-5:]}
        if summary in recent_summaries:
            logger.debug("情节归档跳过(重复): bot=%s group=%s summary=%.60s", bot_id, group_id, summary)
            return False

        participants_list: list[str] = []
        if participants:
            participants_list = sorted(
                str(p) for p in participants
                if str(p).isdigit()  # 只保留 QQ 号
            )

        entry = {
            "summary": summary,
            "participants": participants_list,
            "topic_keywords": _extract_keywords(topic_anchor or summary),
            "created_at": time.time(),
        }
        entries.append(entry)

        # FIFO 淘汰
        while len(entries) > self.MAX_ENTRIES:
            removed = entries.pop(0)
            logger.debug("情节归档 FIFO 淘汰: bot=%s group=%s summary=%.40s", bot_id, group_id, removed.get("summary", ""))

        self._save(data)
        logger.info(
            "情节归档: bot=%s group=%s summary=%.80s participants=%s keywords=%s total=%d",
            bot_id, group_id, summary, participants_list, entry["topic_keywords"], len(entries),
        )
        return True

    # ── 检索 ──────────────────────────────────────────────

    def query(
        self,
        bot_id: str,
        group_id: int,
        context_text: str = "",
        top_n: int = 2,
    ) -> list[dict]:
        """检索与当前上下文相关的情节记录。

        关键词交集评分: 对每条 entry 的 summary + topic_keywords
        做 tokenize，与 context_text 的 tokens 取交集 → 按交集大小排序。

        context_text 为空时返回最近 top_n 条。
        """
        data = self._load(bot_id, group_id)
        entries: list[dict] = data.get("entries", [])
        if not entries:
            return []

        query_tokens = _tokenize(context_text) if context_text else set()

        if not query_tokens:
            # 无上下文 → 返回最近 N 条
            return list(reversed(entries[-top_n:]))

        scored: list[tuple[int, float, dict]] = []  # (overlap, recency, entry)
        for i, entry in enumerate(entries):
            target_text = entry.get("summary", "")
            keywords = entry.get("topic_keywords", [])
            if keywords:
                target_text += " " + " ".join(keywords)
            target_tokens = _tokenize(target_text)
            overlap = len(query_tokens & target_tokens)
            if overlap > 0:
                recency = i / max(len(entries), 1)  # 越新越大
                scored.append((overlap, recency, entry))

        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [entry for _, _, entry in scored[:top_n]]

    # ── 归档计数 (供监控) ─────────────────────────────────

    def count(self, bot_id: str, group_id: int) -> int:
        """返回某群的归档条目数。"""
        data = self._load(bot_id, group_id)
        return len(data.get("entries", []))

    def total_counts(self, bot_id: str = "") -> dict[int, int]:
        """返回某 bot 所有群的归档条目数。bot_id 为空时返回全部。"""
        result: dict[int, int] = {}
        base = self._base_dir
        if not os.path.isdir(base):
            return result
        for bot_dir in os.listdir(base):
            if bot_id and bot_dir != bot_id:
                continue
            bot_path = os.path.join(base, bot_dir)
            if not os.path.isdir(bot_path):
                continue
            for fname in os.listdir(bot_path):
                if fname.endswith(".json"):
                    try:
                        gid = int(fname.replace(".json", ""))
                        result[gid] = self.count(bot_dir, gid)
                    except ValueError:
                        pass
        return result


# ── 模块级单例 ──────────────────────────────────────────

_episodic_stores: dict[str, EpisodicStore] = {}


def get_episodic_store(bot_id: str = "") -> EpisodicStore | None:
    """获取 per-bot 情节记忆存储实例。"""
    if not bot_id:
        return None
    return _episodic_stores.get(bot_id)


def init_episodic_store(bot_id: str, data_dir: str = "") -> EpisodicStore:
    """初始化 per-bot 情节记忆存储实例。"""
    store = EpisodicStore(data_dir=data_dir)
    _episodic_stores[bot_id] = store
    logger.info("情节记忆存储初始化: bot=%s dir=%s", bot_id, store._base_dir)
    return store
