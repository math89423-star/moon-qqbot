"""World Info / Lorebook 触发系统 — 完整三模式触发 + 计时效果。

来源: SillyTavern World Book
原理: 每个 World Book 条目有关键词 + 触发模式 + 计时效果，
      每条消息到达时扫描更新状态，在 prompt 构建时注入激活条目。

触发模式:
  - keyword:   消息包含任一关键词 → 激活 (ANY 逻辑)
  - constant:  始终激活 (全局设定)
  - vectorized: (远期) 语义相似度触发 — 当前为占位，退化为 constant

计时效果 (以消息数为单位):
  - sticky N:  激活后持续 N 条消息 (消息计数递减，到期移除)
  - cooldown N: 激活后冷却 N 条消息 (期间不可再次触发)
  - delay N:    需积累 N 条消息后才可首次触发

说话人匹配: 用显式 speaker_ids 字段 — 空列表 = 所有说话人

用法:
  from .world_book import load_world_book, WorldBookBuffer

  entries = load_world_book("path/to/world_book.json")
  buffer = WorldBookBuffer(entries)
  buffer.feed_message(user_id="123", user_name="小明", content="ComfyUI节点怎么连")
  active = buffer.get_active_content()
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────

# 默认扫描深度: 检查最近 N 条消息的关键词
DEFAULT_SCAN_DEPTH = 8

# 有效触发模式
VALID_TRIGGERS = frozenset({"keyword", "constant", "vectorized"})


# ── 条目数据结构 ──────────────────────────────────────────

@dataclass
class WorldBookEntry:
    """单条 World Book / Lorebook 条目。

    对齐 SillyTavern World Info entry 格式，
    说话人匹配用显式字段而非 \\x01 前缀。
    """

    id: str
    keys: list[str]                # 触发关键词 (keyword 模式)
    content: str                   # 注入 prompt 的文本
    trigger: str = "keyword"       # keyword | constant | vectorized
    comment: str = ""              # 人类可读注释

    # ── 计时效果 (以消息条数为单位, 0 = 不启用) ──
    sticky: int = 0                # 激活后持续 N 条消息
    cooldown: int = 0              # 激活后冷却 N 条消息
    delay: int = 0                 # 积累 N 条消息后才可首次触发

    # ── 过滤 ──
    speaker_ids: list[str] = field(default_factory=list)  # 空 = 所有说话人
    probability: float = 1.0       # 触发概率 0.0-1.0
    case_sensitive: bool = False   # 关键词是否区分大小写

    # ── 约束 ──
    inclusion_group: str = ""      # 同组内只激活一条 (随机选)
    priority: int = 0              # 排序优先级 (越高越靠前注入)
    scan_depth_override: int = 0   # 覆盖全局扫描深度 (0 = 使用默认)

    def matches_keyword(self, text: str) -> bool:
        """检查文本是否包含任一触发关键词 (ANY 逻辑)。"""
        if not self.keys or not text:
            return False
        search_text = text if self.case_sensitive else text.lower()
        for key in self.keys:
            search_key = key if self.case_sensitive else key.lower()
            if search_key in search_text:
                return True
        return False

    def matches_speaker(self, user_id: str) -> bool:
        """检查说话人是否匹配。空列表 = 所有说话人。"""
        if not self.speaker_ids:
            return True
        return user_id in self.speaker_ids


# ── JSON 加载 ────────────────────────────────────────────

def load_world_book(json_path: str | Path) -> list[WorldBookEntry]:
    """从 JSON 文件加载 World Book 条目列表。

    兼容旧格式 (仅有 id/keys/content/comment)，
    新字段缺失时使用默认值。
    """
    path = Path(json_path)
    if not path.exists():
        logger.warning("World Book 文件不存在: %s", path)
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("World Book JSON 解析失败: %s — %s", path, e)
        return []

    raw_entries = data.get("entries", [])
    entries: list[WorldBookEntry] = []

    for raw in raw_entries:
        try:
            entry = WorldBookEntry(
                id=raw.get("id", ""),
                keys=raw.get("keys", []),
                content=raw.get("content", ""),
                trigger=raw.get("trigger", "keyword"),
                comment=raw.get("comment", ""),
                sticky=raw.get("sticky", 0),
                cooldown=raw.get("cooldown", 0),
                delay=raw.get("delay", 0),
                speaker_ids=raw.get("speaker_ids", []),
                probability=raw.get("probability", 1.0),
                case_sensitive=raw.get("case_sensitive", False),
                inclusion_group=raw.get("inclusion_group", ""),
                priority=raw.get("priority", 0),
                scan_depth_override=raw.get("scan_depth_override", 0),
            )
            if entry.id and entry.content:
                entries.append(entry)
            else:
                logger.warning("World Book 条目缺少 id 或 content，跳过: %r", raw.get("id", "?"))
        except Exception:
            logger.warning("World Book 条目解析失败: %r", raw.get("id", "?"), exc_info=True)

    # 分类统计
    keyword_count = sum(1 for e in entries if e.trigger == "keyword")
    constant_count = sum(1 for e in entries if e.trigger == "constant")
    has_sticky = sum(1 for e in entries if e.sticky > 0)
    has_cooldown = sum(1 for e in entries if e.cooldown > 0)

    logger.info(
        "World Book 加载完成: %d 条 (keyword=%d constant=%d, sticky=%d cooldown=%d)",
        len(entries), keyword_count, constant_count, has_sticky, has_cooldown,
    )
    return entries


# ── WorldBookBuffer: 有状态追踪器 ──────────────────────────

@dataclass
class WorldBookBuffer:
    """Per-group World Book 状态追踪器。

    维护激活条目集合 + 冷却队列 + 消息计数器。
    每次消息到达时调用 feed_message() 更新状态。
    """

    entries: list[WorldBookEntry]
    scan_depth: int = DEFAULT_SCAN_DEPTH

    # ── 内部状态 ──
    _active: dict[str, int] = field(default_factory=dict)    # entry_id → sticky_remaining
    _cooldowns: dict[str, int] = field(default_factory=dict)  # entry_id → cooldown_remaining
    _message_count: int = 0                                    # 总消息数 (delay 用)
    _recent_texts: list[str] = field(default_factory=list)     # 最近消息文本 (关键词扫描用)

    # ── 公共接口 ───────────────────────────────────────

    def feed_message(
        self,
        user_id: str = "",
        user_name: str = "",
        content: str = "",
    ) -> None:
        """处理一条新消息: 推进计时器 + 扫描关键词 + 激活条目。

        应在消息入库后、prompt 构建前调用。
        """
        self._message_count += 1

        # 1. 推进计时器: sticky 递减, cooldown 递减
        self._tick_timers()

        # 2. 记录消息文本 (用于关键词扫描)
        text = f"{user_name}: {content}" if user_name else content
        self._recent_texts.append(text)
        # 保持最近 scan_depth 条
        if len(self._recent_texts) > self.scan_depth:
            self._recent_texts = self._recent_texts[-self.scan_depth:]

        # 3. 扫描并激活新条目
        self._scan_and_activate(user_id, content)

    def get_active_content(self) -> list[str]:
        """返回当前所有激活条目的 content 列表 (按 priority 降序)。

        用于注入 prompt。
        """
        if not self._active:
            return []

        # 按 priority 降序排列
        active_ids = set(self._active.keys())
        sorted_entries = sorted(
            [e for e in self.entries if e.id in active_ids],
            key=lambda e: e.priority,
            reverse=True,
        )

        result: list[str] = []
        for entry in sorted_entries:
            sticky_rem = self._active.get(entry.id, 0)
            logger.debug(
                "WB 注入: %s (sticky=%d, priority=%d)",
                entry.id, sticky_rem, entry.priority,
            )
            result.append(entry.content)

        return result

    def reset(self) -> None:
        """重置所有状态 (群聊上下文过期时调用)。"""
        self._active.clear()
        self._cooldowns.clear()
        self._message_count = 0
        self._recent_texts.clear()

    # ── 内部方法 ───────────────────────────────────────

    def _tick_timers(self) -> None:
        """推进所有计时器 (每条消息调用一次)。"""

        # Sticky 递减 — 到期条目从 active 移除
        expired: list[str] = []
        for eid in list(self._active):
            self._active[eid] -= 1
            if self._active[eid] <= 0:
                expired.append(eid)
        for eid in expired:
            del self._active[eid]
            logger.debug("WB sticky 到期: %s", eid)

        # Cooldown 递减
        for eid in list(self._cooldowns):
            self._cooldowns[eid] -= 1
            if self._cooldowns[eid] <= 0:
                del self._cooldowns[eid]

    def _scan_and_activate(self, user_id: str, content: str) -> None:
        """扫描条目关键词，激活命中条目。"""

        # 构建扫描文本: 最近 N 条消息内容
        scan_text = " ".join(self._recent_texts)

        # 按 inclusion_group 去重: 同组只激活一条
        activated_groups: set[str] = set()

        for entry in self.entries:
            eid = entry.id

            # 已在 active → 跳过 (sticky 期间不重新激活)
            if eid in self._active:
                continue

            # 已在 cooldown → 跳过
            if eid in self._cooldowns:
                continue

            # ── 触发模式分发 ──
            if entry.trigger == "constant":
                # 始终激活 — 但也要检查 cooldown/delay
                if not self._can_activate(entry, user_id):
                    continue
                self._activate(entry, activated_groups)

            elif entry.trigger == "keyword":
                # 关键词匹配
                if not entry.matches_keyword(scan_text):
                    continue
                if not entry.matches_speaker(user_id):
                    continue
                if not self._can_activate(entry, user_id):
                    continue
                self._activate(entry, activated_groups)
                logger.debug("WB 关键词命中: %s → %s", entry.keys[:3], eid)

            elif entry.trigger == "vectorized":
                # (远期) 语义相似度触发 — 当前退化为 constant
                # TODO: 接入 embedding 服务实现语义匹配
                if not self._can_activate(entry, user_id):
                    continue
                self._activate(entry, activated_groups)

    def _can_activate(self, entry: WorldBookEntry, _user_id: str = "") -> bool:
        """检查条目是否可以激活 (cooldown / delay / probability)。"""

        # Delay: 消息数不足
        if entry.delay > 0 and self._message_count < entry.delay:
            return False

        # Probability: 概率过滤
        if entry.probability < 1.0:
            if random.random() > entry.probability:
                logger.debug("WB 概率过滤: %s (p=%.2f)", entry.id, entry.probability)
                return False

        return True

    def _activate(
        self,
        entry: WorldBookEntry,
        activated_groups: set[str],
    ) -> None:
        """激活条目: 加入 active + 设置 cooldown + 记录 inclusion_group。"""

        # Inclusion group 约束: 同组只激活一条
        if entry.inclusion_group:
            if entry.inclusion_group in activated_groups:
                logger.debug(
                    "WB inclusion_group 冲突: %s (group=%s)",
                    entry.id, entry.inclusion_group,
                )
                return
            activated_groups.add(entry.inclusion_group)

        # 激活
        if entry.sticky > 0:
            self._active[entry.id] = entry.sticky
        else:
            # sticky=0: 仅当前轮有效 (用 1 表示，下一轮 tick 移除)
            self._active[entry.id] = 1

        # 冷却
        if entry.cooldown > 0:
            self._cooldowns[entry.id] = entry.cooldown

        logger.debug("WB 激活: %s (sticky=%d, cooldown=%d)", entry.id, entry.sticky, entry.cooldown)


# ── 兼容旧版 scan (供 tavern_client 私聊/角色扮演使用) ────

_WORLD_BOOK_CACHE: list[WorldBookEntry] | None = None
_WORLD_BOOK_CACHE_PATH: str = ""


def _get_cached_entries(wb_path: str) -> list[WorldBookEntry]:
    """获取缓存的 World Book 条目 (避免重复加载)。"""
    global _WORLD_BOOK_CACHE, _WORLD_BOOK_CACHE_PATH
    if _WORLD_BOOK_CACHE is not None and wb_path == _WORLD_BOOK_CACHE_PATH:
        return _WORLD_BOOK_CACHE
    _WORLD_BOOK_CACHE = load_world_book(wb_path)
    _WORLD_BOOK_CACHE_PATH = wb_path
    return _WORLD_BOOK_CACHE


def scan_world_book_static(
    messages: list[dict[str, str]],
    entries: list[WorldBookEntry] | None = None,
    scan_depth: int = DEFAULT_SCAN_DEPTH,
) -> list[str]:
    """无状态扫描 — 兼容旧版 _scan_world_book 接口。

    用于私聊/角色扮演等不需要状态追踪的场景。
    对最近 N 条消息做关键词子串匹配 (ANY 逻辑)。

    Args:
        messages: LLM messages 列表
        entries: World Book 条目 (None 时返回空)
        scan_depth: 扫描最近 N 条消息

    Returns:
        命中条目的 content 列表
    """
    if not entries:
        return []

    # 只扫描 user + assistant 消息
    scan_msgs = [m for m in messages if m.get("role") in ("user", "assistant")]
    scan_msgs = scan_msgs[-scan_depth:]
    if not scan_msgs:
        return []

    combined = " ".join(
        m.get("content", "") for m in scan_msgs
    ).lower()

    activated: list[str] = []
    seen_ids: set[str] = set()

    for entry in entries:
        if entry.id in seen_ids:
            continue
        if entry.trigger not in ("keyword", "constant"):
            continue

        if entry.trigger == "constant":
            activated.append(entry.content)
            seen_ids.add(entry.id)
            continue

        # keyword 模式
        if not entry.keys:
            continue
        for key in entry.keys:
            if key.lower() in combined:
                activated.append(entry.content)
                seen_ids.add(entry.id)
                break

    if activated:
        logger.debug("WB 静态扫描命中 %d 条: %s", len(activated), sorted(seen_ids))
    return activated
