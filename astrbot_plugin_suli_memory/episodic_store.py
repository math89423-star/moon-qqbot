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


# ── 展示/召回用关键词 (规则抽词，零 LLM；与 _tokenize 的 n-gram 汤区分) ──
# _tokenize 的单字+bigram+trigram 是给召回做模糊匹配的，直接取"最长 token"
# 当展示关键词必翻车 (如"你自由/个星穹/道花火"——trigram 随机切片，2026-10-15 实测)。
# 此处按 书名号标题 > 英文词 > 中文内容词 抽可读词；召回侧照常用。

_TITLE_RE = _re.compile(r"《([^》]{2,20})》")
_TITLE_SPLIT_RE = _re.compile(r"[：:·・／/\s]+")
_FRAG_SPLIT_RE = _re.compile(
    r"[，。！？；：、\"\"''「」『』（）()【】〈〉…—·\s"
    r"\u2600-\u27BF\u2B00-\u2BFF\uFE00-\uFE0F"
    r"\U0001F000-\U0001FAFF\U0001FC00-\U0001FFFF"
    r"❤️♥✨♪♡❤\*#@~_\-/\\|+=\^<>$%&]+"
)
# 内部分词只切最稳的虚词 (地/得/量/极/过会碎地图/记得/质量/积极/过去，不在此切，长句走动词兜底)
# 2026-10-15 补吧吗呢啊：爱吧昵称→爱/昵称，不然整块当关键词（“爱吧昵称”事故）
_INNER_SPLIT_RE = _re.compile(r"[的之和与或跟及在对为是将被把由从向往给让使令经因如若但却还并及等比同张个些种样最更太很第着讨论吧吗呢啊呀么]")
_VERB_SPLIT_RE = _re.compile(r"[问说看听说打吃喝玩聊谈讲读写真画做用查搜找教请帮让叫替求追播讨论高质极量得下过论讨]")
_STRIP_CHARS = "为给把被在对和与跟向往从由将就才都也还要会能可应该得地着的了过之其每各另别再所而已于吧呢吗啊呀么这那你我他她它是没不很太更最第张个些种样一"
_STOPWORDS = frozenset({
    "什么", "怎么", "这样", "那样", "这么", "那么", "这个", "那个", "这些", "那些",
    "这里", "那里", "哪里", "可以", "应该", "需要", "觉得", "认为", "知道", "看到",
    "还是", "或者", "以及", "因为", "所以", "虽然", "然后", "但是", "如果", "一个",
    "一张", "一些", "有点", "非常", "真的", "就是", "不是", "没有", "进行", "通过",
    "作为", "主人绘制一张",
})
_EN_STOPWORDS = frozenset({
    "the", "a", "an", "to", "of", "and", "or", "for", "with", "is", "are",
    "it", "this", "that", "you", "your", "can", "please", "me", "my", "we",
})


def _extract_keywords(topic_anchor: str, max_keywords: int = 5) -> list[str]:
    """从话题锚点提取可读关键词 (纯规则，零 LLM)。

    书名号标题优先 (崩坏：星穹铁道→崩坏/星穹铁道)，再取英文词，
    中文按标点分片→虚词剥边→内部分词，留 2-6 字内容词，按出现序去重截断。
    """
    if not topic_anchor:
        return []
    kws: list[str] = []

    def _push(w: str) -> None:
        w = (w or "").strip()
        # 只收中日韩+字母数字下划线，表情/符号碎片直接丢（“❤️你躺”事故）
        if not w or not _re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9_]+", w):
            return
        if len(w) >= 2 and w not in _STOPWORDS and w not in kws:
            kws.append(w)

    for title in _TITLE_RE.findall(topic_anchor):
        title = title.strip()
        if len(title) <= 8:
            _push(title)
        for part in _TITLE_SPLIT_RE.split(title):
            _push(part)
    text = _TITLE_RE.sub(" ", topic_anchor)
    for m in _re.finditer(r"[a-z][a-z0-9_]+", text.lower()):
        if len(m.group()) >= 2 and m.group() not in _EN_STOPWORDS:
            _push(m.group())
    for frag in _FRAG_SPLIT_RE.split(text):
        frag = frag.strip().strip(_STRIP_CHARS)
        if not frag:
            continue
        if _re.fullmatch(r"[A-Za-z0-9_]+", frag):
            # 纯英文碎片走英文通道 (小写+停用词过滤，防 "Can" 漏网)
            _low = frag.lower()
            if len(_low) >= 2 and _low not in _EN_STOPWORDS:
                _push(_low)
            continue
        for seg in _INNER_SPLIT_RE.split(frag):
            seg = seg.strip().strip(_STRIP_CHARS)
            if 2 <= len(seg) <= 6:
                _push(seg)
            elif len(seg) > 6:
                # 长句兜底：先按黏着动词再切，仍超长则抽中日韩/字母 runs，不再盲切首尾4字
                # (盲切会产出“爱吧昵称”“❤️你躺”这类不可读词，2026-10-15 实测)
                for sub in _VERB_SPLIT_RE.split(seg):
                    sub = sub.strip().strip(_STRIP_CHARS)
                    if 2 <= len(sub) <= 6 and _re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9_]+", sub):
                        _push(sub)
                    elif len(sub) > 6:
                        for run in _re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z][A-Za-z0-9_]{1,15}", sub):
                            _push(run)
                            if len(kws) >= max_keywords:
                                break
                    if len(kws) >= max_keywords:
                        break
    return kws[:max_keywords]


def _sanitize_llm_keywords(words: list[str] | None, max_keywords: int = 5) -> list[str]:
    """清洗 LLM 预抽关键词：只要中日韩+字母数字、2-8 字、去重截断。

    LLM 偶发带解释/序号/emoji（如“1.昵称❤️”），这里只收干净词；
    洗后为空则调用方回退规则抽词。
    """
    if not words:
        return []
    out: list[str] = []
    for w in words:
        w = (str(w or "")).strip().strip("\"'“”‘’。、，, |.。:：;；!！?？")
        if not w or not _re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9_]+", w):
            continue
        if 2 <= len(w) <= 8 and w not in _STOPWORDS and w not in out:
            out.append(w)
        if len(out) >= max_keywords:
            break
    return out


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
        topic_keywords: list[str] | None = None,
    ) -> bool:
        """归档一条情节记录。空 summary 跳过。

        topic_keywords 由调用方（tavern 侧 LLM）预抽传入；缺省/洗后为空
        则回退本模块规则抽词（零 LLM）。记忆层保持纯规则，不直连 LLM。
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
            # 关键词优先用调用方 LLM 预抽（干净），缺省/洗后为空回退规则。
            # 规则只看 summary+anchor（summary 优先），不再只看原话。
            "topic_keywords": _sanitize_llm_keywords(topic_keywords)
            or _extract_keywords(f"{summary} {topic_anchor or ''}"),
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
