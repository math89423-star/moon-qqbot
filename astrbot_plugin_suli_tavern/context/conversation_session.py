"""关注事件槽 — 解决"聊一半断片/失忆"。

在意图闸（管单条判断）和记忆系统（管长期档案）之间补上中等时间尺度的注意力状态。
模型单位是"关注事件"(AttentionSlot)而非"和谁的会话"——一个 bot 在每个群最多持有 2 个关注槽。

决策机制: 热度涌现 — 加热(回复/@/关键词) → 自然衰减 → 淡出
硬超时: 空闲超时 + 绝对寿命上限, 优先于热度判断, 强制释放
余温期: 槽移出后短暂冷却, 同话题再起直接恢复 (解决断片)
LLM 角色反转: 只做罕见的归属歧义消解, 不可用时退回廉价信号

设计原则:
  1. 会话存续 = 热度物理 (不依赖 LLM)
  2. 会话内消息默认短路漏斗
  3. LLM 从"决定要不要理"变"决定要不要停" (踩刹车)
  4. 冷却只对槽外陌生消息生效
  5. 硬超时优先于热度 — 再重要的事, 放太久也得放下
  6. bot_id 必含 (铁律 1: 状态隔离)

用法:
  from astrbot_plugin_suli_tavern.context.conversation_session import get_slot_manager

  mgr = get_slot_manager()
  if mgr.is_participant_in_active_slot(bot_id, group_id, user_id):
      ... 短路唤醒漏斗，直接进回复管线 ...

"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# AttentionSlot — 关注事件槽
# ═══════════════════════════════════════════════════════════════


@dataclass
class AttentionSlot:
    """一个关注事件 — bot 正在关注群里的"什么话题"而非"和谁"。

    一题多人: participants 动态进出, 归入同一 slot
    多题并行: bot 只关注自己的最多 2 个 slot, 其余无视
    混乱免疫: 槽满即满, 十个话题和两个话题行为一样

    生命周期: active → (能量衰减/超时) → fading → cooling → 移除
    """

    bot_id: str
    group_id: int
    topic_anchor: str = ""
    participants: set[str] = field(default_factory=set)
    energy: float = 0.0
    created_at: float = 0.0
    last_heated_at: float = 0.0
    anchor_messages: list[dict] = field(default_factory=list)
    thread_summary: str = ""  # LLM 产出的对话脉络摘要 (1-2句结论, 挂在槽上, 随槽生命周期自动清理)
    state: str = "active"  # active | fading | cooling

    def __post_init__(self) -> None:
        if self.created_at <= 0:
            self.created_at = time.time()
        if self.last_heated_at <= 0:
            self.last_heated_at = self.created_at

    @property
    def age(self) -> float:
        """槽已存在多久 (秒)。"""
        return time.time() - self.created_at

    @property
    def idle(self) -> float:
        """距离最后一次加热多久 (秒)。"""
        return time.time() - self.last_heated_at

    def add_participant(self, user_id: str) -> None:
        self.participants.add(user_id)

    def has_participant(self, user_id: str) -> bool:
        return user_id in self.participants


# ═══════════════════════════════════════════════════════════════
# AttentionSlotManager
# ═══════════════════════════════════════════════════════════════


class AttentionSlotManager:
    """关注槽管理器 — 模块级单例。

    每个 bot 在每个群最多持有 MAX_SLOTS 个关注槽。
    存储: 纯内存, key = f"{bot_id}:{group_id}"
    """

    # ── 硬超时 (优先于热度) ──────────────────────────────
    IDLE_TTL: float = 120.0       # 空闲超时: 没人再喂这个话题 → 强制移出
    MAX_LIFETIME: float = 600.0   # 绝对寿命上限: 10min, 防止死捏着不放

    # ── 热度物理 ─────────────────────────────────────────
    DECAY_HALF_LIFE: float = 30.0      # 能量半衰期 (秒)
    FADING_THRESHOLD: float = 0.10     # 低于此 → fading
    COOLING_THRESHOLD: float = 0.05    # 低于此 → cooling
    COOLING_TTL: float = 30.0          # 余温期: 同话题再起直接恢复

    # ── 加热量 ───────────────────────────────────────────
    HEAT_AT: float = 0.50              # @ 或 reply_bot → 高
    HEAT_REPLY: float = 0.50           # bot 自己回复 → 高
    HEAT_KEYWORD: float = 0.20         # 关键词蹭到 → 低
    HEAT_PARTICIPANT_JOIN: float = 0.15 # 新参与者加入 → 低

    # ── 槽数 ─────────────────────────────────────────────
    MAX_SLOTS: int = 2

    def __init__(self) -> None:
        # key: f"{bot_id}:{group_id}" → list[AttentionSlot] (最多 MAX_SLOTS)
        self._stores: dict[str, list[AttentionSlot]] = {}
        self._lock = asyncio.Lock()
        # 防重复归档: 已归档 slot 的 id(slot)，cooling 恢复后再离开不再归档
        self._archived_slot_ids: set[int] = set()
        # 情节记忆存储 (惰性注入，由 group_chat.py 初始化时调用 set_episodic_store)
        self._episodic_store: object | None = None

    # ── Key ───────────────────────────────────────────────

    @staticmethod
    def _key(bot_id: str, group_id: int) -> str:
        return f"{bot_id}:{group_id}"

    # ── 情节记忆归档 ───────────────────────────────────────

    def set_episodic_store(self, store: object) -> None:
        """注入情节记忆存储实例 (由 group_chat.py 初始化时调用)。"""
        self._episodic_store = store

    async def _archive_slot(self, slot: AttentionSlot) -> None:
        """槽离开时归档 thread_summary 为情节记忆 (零新增 LLM 调用)。

        防重复: 已归档的 slot id 跳过。空 thread_summary 跳过。
        """
        if self._episodic_store is None:
            return
        sid = id(slot)
        if sid in self._archived_slot_ids:
            return
        summary = (slot.thread_summary or "").strip()
        if not summary:
            return
        try:
            # episodic_store.archive 是同步 I/O，用 to_thread 包装
            import asyncio as _asyncio
            archive_fn = getattr(self._episodic_store, "archive", None)
            if archive_fn is None:
                return
            ok = await _asyncio.to_thread(
                archive_fn,
                slot.bot_id,
                slot.group_id,
                summary,
                slot.participants,
                slot.topic_anchor,
            )
            if ok:
                self._archived_slot_ids.add(sid)
                logger.info(
                    "情节归档完成: bot=%s group=%s anchor=%.40s summary=%.80s",
                    slot.bot_id, slot.group_id, slot.topic_anchor, summary,
                )
        except Exception:
            logger.debug("情节归档失败 (非阻塞)", exc_info=True)

    def _get_slots(self, bot_id: str, group_id: int) -> list[AttentionSlot]:
        """获取 (bot_id, group_id) 的槽列表。"""
        key = self._key(bot_id, group_id)
        if key not in self._stores:
            self._stores[key] = []
        return self._stores[key]

    # ── 能量衰减 ─────────────────────────────────────────

    @staticmethod
    def _decayed_energy(slot: AttentionSlot, now: float | None = None) -> float:
        """计算指数衰减后的当前能量。

        energy(t) = energy_0 × 0.5^(Δt / half_life)
        """
        if now is None:
            now = time.time()
        elapsed = now - slot.last_heated_at
        if elapsed <= 0:
            return slot.energy
        return slot.energy * (0.5 ** (elapsed / AttentionSlotManager.DECAY_HALF_LIFE))

    # ── 硬超时检查 (优先于热度) ───────────────────────────

    def _check_hard_timeouts(
        self, slots: list[AttentionSlot], now: float | None = None,
    ) -> list[AttentionSlot]:
        """检查硬超时 — 任一触发即强制移入 cooling。

        返回被强制移出的 slot 列表。
        超时判断优先于热度判断 — 即使 energy 很高也照移。
        """
        if now is None:
            now = time.time()
        forced: list[AttentionSlot] = []
        for slot in slots:
            if slot.state == "cooling":
                continue
            idle = now - slot.last_heated_at
            age = now - slot.created_at
            if idle > self.IDLE_TTL:
                slot.state = "cooling"
                forced.append(slot)
                logger.info(
                    "关注槽空闲超时强制移出: bot=%s group=%s anchor=%.40s idle=%.0fs energy=%.3f",
                    slot.bot_id, slot.group_id, slot.topic_anchor, idle, slot.energy,
                )
            elif age > self.MAX_LIFETIME:
                slot.state = "cooling"
                forced.append(slot)
                logger.info(
                    "关注槽寿命上限强制移出: bot=%s group=%s anchor=%.40s age=%.0fs energy=%.3f",
                    slot.bot_id, slot.group_id, slot.topic_anchor, age, slot.energy,
                )
        return forced

    # ── 状态更新 (tick) ───────────────────────────────────

    def _tick_slots(
        self, slots: list[AttentionSlot], now: float | None = None,
    ) -> tuple[list[AttentionSlot], list[AttentionSlot]]:
        """维护槽状态: 超时 → 衰减 → 状态迁移。

        Returns:
            (evicted: 本次移出的, remaining: 仍在槽中的)
        """
        if now is None:
            now = time.time()

        # Step 1: 硬超时优先
        forced = self._check_hard_timeouts(slots, now)
        forced_ids = {id(s) for s in forced}

        evicted: list[AttentionSlot] = []
        remaining: list[AttentionSlot] = []

        for slot in slots:
            if slot.state == "cooling":
                # 已经在 cooling 中: 检查余温是否耗尽
                if now - slot.last_heated_at > self.COOLING_TTL:
                    evicted.append(slot)  # 余温耗尽, 彻底丢弃
                else:
                    remaining.append(slot)  # 仍在余温期
                continue

            # Step 2: 能量衰减
            current_energy = self._decayed_energy(slot, now)

            # Step 3: 状态迁移
            if current_energy < self.COOLING_THRESHOLD:
                slot.state = "cooling"
                slot.energy = current_energy
                remaining.append(slot)  # 进入 cooling, 不立即丢弃
                logger.debug(
                    "关注槽进入余温: bot=%s group=%s anchor=%.40s energy=%.4f",
                    slot.bot_id, slot.group_id, slot.topic_anchor, current_energy,
                )
            elif current_energy < self.FADING_THRESHOLD:
                slot.state = "fading"
                slot.energy = current_energy
                remaining.append(slot)
            else:
                slot.state = "active"
                slot.energy = current_energy
                remaining.append(slot)

        # 清理真正被丢弃的 (不在 remaining 中的旧 cooling)
        for slot in evicted:
            logger.info(
                "关注槽彻底丢弃: bot=%s group=%s anchor=%.40s",
                slot.bot_id, slot.group_id, slot.topic_anchor,
            )
            # ── Path A: 槽彻底丢弃 → 归档情节记忆 ──
            _ = asyncio.create_task(self._archive_slot(slot))

        # ── 清理已归档 slot ID 中的死引用 (内存泄漏防护) ──
        if self._archived_slot_ids and evicted:
            evicted_ids = {id(s) for s in evicted}
            self._archived_slot_ids -= evicted_ids
            all_live = {
                id(s)
                for store_slots in self._stores.values()
                for s in store_slots
            }
            self._archived_slot_ids &= all_live
        return evicted, remaining

    # ── 归属: 消息属于哪个槽 ─────────────────────────────

    def _find_slot_for_message(
        self,
        slots: list[AttentionSlot],
        text: str,
        sender_id: str,
        is_at: bool = False,
        is_reply: bool = False,
    ) -> AttentionSlot | None:
        """用廉价信号判断消息属于哪个槽。

        优先级: reply 关系 > @ > 参与者匹配 > 关键词重叠
        只在模糊且影响换出时才用 LLM (外部调用, 此处不使用)。
        """
        active_slots = [s for s in slots if s.state != "cooling"]

        # 1. 参与者匹配 (最快, 最可靠)
        for slot in active_slots:
            if sender_id in slot.participants:
                return slot

        # 2. @ / reply 信号 → 倾向创建新归属 (由 heat 调用的上下文决定)

        # 3. 关键词重叠 (廉价) — 处理英文 (空格分词) 和中文 (字级 n-gram)
        if text and active_slots:
            best_overlap = 0.0
            best_slot: AttentionSlot | None = None
            for slot in active_slots:
                if not slot.topic_anchor:
                    continue
                overlap = self._keyword_overlap(slot.topic_anchor, text)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_slot = slot
            if best_slot and best_overlap > 0.15:
                return best_slot

        return None

    @staticmethod
    def _keyword_overlap(anchor: str, text: str) -> float:
        """计算 anchor 与 text 的关键词重叠度 (0.0~1.0)。

        英文: 空格分词 + 集合交集
        中文/混合: 空格分词 → 若结果 ≤1 个 token → 退化为 2-gram 字符级比较
        """
        a_lower = anchor.lower()
        t_lower = text.lower()

        # 空格分词
        a_words = set(w for w in a_lower.split() if len(w) >= 2)
        t_words = set(w for w in t_lower.split() if len(w) >= 2)

        if a_words:
            space_overlap = len(a_words & t_words) / len(a_words)
            if space_overlap > 0:
                return space_overlap

        # 退化: 字符级 2-gram (处理 CJK)
        def _bigrams(s: str) -> set[str]:
            return {s[i:i+2] for i in range(len(s) - 1)}

        a_bigrams = _bigrams(a_lower)
        t_bigrams = _bigrams(t_lower)
        if not a_bigrams:
            return 0.0
        return len(a_bigrams & t_bigrams) / len(a_bigrams)

    # ── 输入净化 ───────────────────────────────────────────

    @staticmethod
    def _sanitize_anchor(raw: str, max_len: int = 120) -> str:
        """净化话题锚点 — 防御间接 prompt injection。

        锚点来自用户消息原文，注入 LLM system prompt 前必须净化:
          - 折叠换行 → 空格 (防止多行 prompt 注入)
          - 移除 prompt 结构标记 (---, ###, ```, <, >)
          - 截断到安全长度

        Args:
            raw: 原始话题文本 (来自用户消息)
            max_len: 最大长度 (默认 120 字符)
        """
        if not raw:
            return ""
        # 1. 折叠换行
        sanitized = raw.replace("\r", " ").replace("\n", " ")
        # 2. 移除 prompt 注入标记
        for char in ("---", "###", "```", "<", ">", "===", "***"):
            sanitized = sanitized.replace(char, " ")
        # 3. 折叠连续空格
        sanitized = " ".join(w for w in sanitized.split() if w)
        # 4. 截断
        if len(sanitized) > max_len:
            sanitized = sanitized[:max_len] + "…"
        return sanitized
        return len(a_bigrams & t_bigrams) / len(a_bigrams)

    # ── 加热 ─────────────────────────────────────────────

    async def heat_slot(
        self,
        bot_id: str,
        group_id: int,
        *,
        topic_anchor: str = "",
        user_id: str = "",
        user_name: str = "",
        heat_amount: float | None = None,
        anchor_messages: list[dict] | None = None,
        is_at: bool = False,
        text: str = "",
    ) -> AttentionSlot | None:
        """给关注槽加热 — 消息/回复归入某 slot 时调用。

        若消息不属于任何现有 slot 且槽未满 → 新建 slot。
        若槽满 → 热度比较, 新的更热才换出最冷的; 否则新事件被"推开"。

        Args:
            bot_id: bot QQ 号
            group_id: 群号
            topic_anchor: 话题摘要
            user_id: 触发用户 ID
            user_name: 触发用户名称 (暂未存入 slot, 保留签名)
            heat_amount: 加热量, None 时根据 is_at 自动选择
            anchor_messages: 锚点消息
            is_at: 是否 @ 或 reply_bot (影响默认加热量)
            text: 消息文本 (用于关键词归属)

        Returns:
            被加热的 slot, 或 None (被推开/槽满且新事件不够热)
        """
        if not bot_id or not group_id:
            return None

        if heat_amount is None:
            heat_amount = self.HEAT_AT if is_at else self.HEAT_KEYWORD

        now = time.time()
        key = self._key(bot_id, group_id)

        async with self._lock:
            slots = self._get_slots(bot_id, group_id)

            # ── 先维护: tick 所有槽 ──
            evicted, slots = self._tick_slots(slots, now)

            # ── 找归属 ──
            existing = self._find_slot_for_message(slots, text, user_id, is_at=is_at)

            if existing is not None:
                # ── 归入已有 slot: 加热 ──
                existing.energy += heat_amount
                existing.last_heated_at = now
                existing.state = "active"  # cooling → active 恢复
                if user_id:
                    existing.add_participant(user_id)
                if topic_anchor:
                    existing.topic_anchor = self._sanitize_anchor(topic_anchor)
                if anchor_messages:
                    existing.anchor_messages = anchor_messages
                logger.debug(
                    "关注槽加热: bot=%s group=%s slot=%.30s energy=%.3f +%.2f",
                    bot_id, group_id, existing.topic_anchor, existing.energy, heat_amount,
                )
                self._stores[key] = slots
                return existing

            # ── 槽未满: 新建 ──
            active_count = sum(1 for s in slots if s.state != "cooling")
            if active_count < self.MAX_SLOTS:
                new_slot = AttentionSlot(
                    bot_id=bot_id,
                    group_id=group_id,
                    topic_anchor=self._sanitize_anchor(topic_anchor),
                    participants={user_id} if user_id else set(),
                    energy=heat_amount,
                    created_at=now,
                    last_heated_at=now,
                    anchor_messages=anchor_messages or [],
                    state="active",
                )
                slots.append(new_slot)
                self._stores[key] = slots
                logger.info(
                    "关注槽新建: bot=%s group=%s anchor=%.40s energy=%.2f (slot %d/%d)",
                    bot_id, group_id, topic_anchor, heat_amount, active_count + 1, self.MAX_SLOTS,
                )
                return new_slot

            # ── 槽满: 热度比较 ──
            coldest = min(
                (s for s in slots if s.state != "cooling"),
                key=lambda s: s.energy,
                default=None,
            )
            if coldest is None:
                # 所有槽都在 cooling → 拒绝
                logger.debug(
                    "关注槽全在余温期，新事件被推开: bot=%s group=%s anchor=%.30s",
                    bot_id, group_id, topic_anchor,
                )
                return None

            if heat_amount > coldest.energy:
                # 新事件更热 → 换出最冷的
                logger.info(
                    "关注槽换出: bot=%s group=%s 旧=%.30s (energy=%.3f) → 新=%.30s (energy=%.2f)",
                    bot_id, group_id, coldest.topic_anchor, coldest.energy, topic_anchor, heat_amount,
                )
                # ── Path B: 热度换出 → 归档情节记忆 ──
                _ = asyncio.create_task(self._archive_slot(coldest))
                slots.remove(coldest)
                new_slot = AttentionSlot(
                    bot_id=bot_id,
                    group_id=group_id,
                    topic_anchor=self._sanitize_anchor(topic_anchor),
                    participants={user_id} if user_id else set(),
                    energy=heat_amount,
                    created_at=now,
                    last_heated_at=now,
                    anchor_messages=anchor_messages or [],
                    state="active",
                )
                slots.append(new_slot)
                self._stores[key] = slots
                return new_slot

            # 新事件不够热 → 被推开
            logger.debug(
                "关注槽已满，新事件被推开: bot=%s group=%s new=%.30s (%.2f) < coldest=%.30s (%.3f)",
                bot_id, group_id, topic_anchor, heat_amount, coldest.topic_anchor, coldest.energy,
            )
            return None

    # ── Query ─────────────────────────────────────────────

    def is_participant_in_active_slot(
        self, bot_id: str, group_id: int, user_id: str,
    ) -> bool:
        """检查 user 是否是任意活跃槽的参与者 → 短路唤醒漏斗。

        纯 CPU 操作, 不依赖 LLM。
        """
        if not bot_id or not user_id:
            return False
        slots = self._get_slots(bot_id, group_id)
        now = time.time()
        for slot in slots:
            if slot.state == "cooling":
                continue
            current_energy = self._decayed_energy(slot, now)
            if current_energy >= self.FADING_THRESHOLD and user_id in slot.participants:
                return True
        return False

    def get_topic_anchor_for_user(
        self, bot_id: str, group_id: int, user_id: str,
    ) -> str:
        """获取 user 所在槽的话题锚点 (用于 prompt 注入)。

        返回最匹配的活跃槽的 topic_anchor, 无匹配返回空字符串。
        """
        slots = self._get_slots(bot_id, group_id)
        now = time.time()
        best: AttentionSlot | None = None
        best_energy = -1.0
        for slot in slots:
            if slot.state == "cooling":
                continue
            if user_id in slot.participants:
                energy = self._decayed_energy(slot, now)
                if energy > best_energy:
                    best_energy = energy
                    best = slot
        if best and best.topic_anchor and best_energy >= self.FADING_THRESHOLD:
            return best.topic_anchor
        return ""

    # ── 对话脉络 (thread_summary) ──────────────────────

    def get_thread_summary_for_user(
        self, bot_id: str, group_id: int, user_id: str,
    ) -> str:
        """获取 user 所在活跃槽的对话脉络摘要 (用于 prompt 注入)。

        返回与 user 最相关的活跃槽的 thread_summary, 无则返回空字符串。
        热度低于 FADING_THRESHOLD 的槽不返回 (话题已冷却 → 脉络作废)。
        """
        slots = self._get_slots(bot_id, group_id)
        now = time.time()
        best: AttentionSlot | None = None
        best_energy = -1.0
        for slot in slots:
            if slot.state == "cooling":
                continue
            if not slot.thread_summary:
                continue
            if user_id in slot.participants:
                energy = self._decayed_energy(slot, now)
                if energy > best_energy:
                    best_energy = energy
                    best = slot
        if best and best.thread_summary and best_energy >= self.FADING_THRESHOLD:
            return best.thread_summary
        return ""

    def set_thread_summary(
        self, bot_id: str, group_id: int, user_id: str,
        summary: str,
    ) -> bool:
        """设置 user 所在最活跃槽的对话脉络摘要。

        写入前净化摘要文本 (防 prompt injection)。只在 summary 非空时写入——
        空字符串不覆盖已有值 (防 LLM 漏吐标签导致脉络丢失; 约束 4)。

        Returns:
            True 如果成功写入, False 如果没有匹配的活跃槽。
        """
        if not summary or not summary.strip():
            return False

        slots = self._get_slots(bot_id, group_id)
        now = time.time()
        best: AttentionSlot | None = None
        best_energy = -1.0
        for slot in slots:
            if slot.state == "cooling":
                continue
            if user_id in slot.participants:
                energy = self._decayed_energy(slot, now)
                if energy > best_energy:
                    best_energy = energy
                    best = slot
        if best and best_energy >= self.FADING_THRESHOLD:
            # 净化: 折叠换行 + 截断 (thread_summary 最长 300 字符, 容纳覆盖范围+边界+结论)
            clean = summary.replace("\r", " ").replace("\n", " ")
            # 移除标签残留 (防 LLM 吐出 <thread_summary> 标签本身)
            clean = clean.replace("<thread_summary>", "").replace("</thread_summary>", "")
            # ── 防 stored prompt injection: 过滤指令型模式 ──
            _injection_patterns = [
                r"(?i)\bignore\b.*\b(previous|all|above)\b",
                r"(?i)\boverride\b.*\b(rules?|system|instructions?)\b",
                r"(?i)\byou are now\b",
                r"(?i)\bdisregard\b.*\b(prior|earlier|system)\b",
                r"(?i)\bnew (system )?prompt\b",
                r"(?i)\bsystem:\s*",
            ]
            import re as _re_clean
            for _pat in _injection_patterns:
                clean = _re_clean.sub(_pat, "[FILTERED]", clean)
            clean = " ".join(clean.split())  # 折叠连续空格
            if len(clean) > 300:
                clean = clean[:297] + "..."
            if clean:
                best.thread_summary = clean
                logger.info(
                    "关注槽脉络更新: bot=%s group=%s anchor=%.40s summary=%.80s",
                    bot_id, group_id, best.topic_anchor, clean,
                )
                return True
        return False

    def get_active_slots(
        self, bot_id: str, group_id: int,
    ) -> list[AttentionSlot]:
        """获取所有活跃槽 (用于调试面板)。"""
        slots = self._get_slots(bot_id, group_id)
        now = time.time()
        return [
            s for s in slots
            if s.state != "cooling" and self._decayed_energy(s, now) >= self.FADING_THRESHOLD
        ]

    def slot_count(self, bot_id: str, group_id: int) -> int:
        """活跃槽数。"""
        return len(self.get_active_slots(bot_id, group_id))

    # ── 余温恢复 ─────────────────────────────────────────

    async def try_revive_cooling(
        self,
        bot_id: str,
        group_id: int,
        topic_anchor: str,
        user_id: str = "",
        heat_amount: float | None = None,
    ) -> AttentionSlot | None:
        """检查 cooling 槽中是否有同话题的 → 直接恢复 (解决断片)。

        余温期内同话题被重新提起 → energy 回升、状态转回 active。
        """
        if heat_amount is None:
            heat_amount = self.HEAT_KEYWORD

        slots = self._get_slots(bot_id, group_id)
        now = time.time()

        for slot in slots:
            if slot.state != "cooling":
                continue
            # 话题重叠检测: 简单关键词匹配
            if topic_anchor and slot.topic_anchor:
                ta_words = set(topic_anchor.lower().split())
                slot_words = set(slot.topic_anchor.lower().split())
                if not ta_words or not slot_words:
                    continue
                overlap = len(ta_words & slot_words) / max(len(ta_words), len(slot_words))
                if overlap > 0.2:
                    slot.state = "active"
                    slot.energy = max(slot.energy, self.FADING_THRESHOLD) + heat_amount
                    slot.last_heated_at = now
                    if user_id:
                        slot.add_participant(user_id)
                    logger.info(
                        "关注槽余温恢复: bot=%s group=%s anchor=%.40s energy=%.3f",
                        bot_id, group_id, slot.topic_anchor, slot.energy,
                    )
                    return slot

        return None

    # ── Maintenance ───────────────────────────────────────

    async def tick(
        self, bot_id: str, group_id: int,
    ) -> list[AttentionSlot]:
        """维护槽状态 (每条消息 / 定时调用)。

        Returns:
            本次被移出的 slot 列表。
        """
        key = self._key(bot_id, group_id)
        async with self._lock:
            slots = self._get_slots(bot_id, group_id)
            evicted, remaining = self._tick_slots(slots)
            self._stores[key] = remaining
            return evicted

    async def cleanup_expired(self) -> int:
        """清理所有 bot/group 的过期槽。返回清理数。"""
        now = time.time()
        total = 0
        async with self._lock:
            for key in list(self._stores.keys()):
                _, remaining = self._tick_slots(self._stores[key], now)
                removed = len(self._stores[key]) - len(remaining)
                self._stores[key] = remaining
                total += removed
                if not remaining:
                    del self._stores[key]
        return total

    @property
    def total_active_count(self) -> int:
        """所有 bot/group 的活跃槽总数 (用于监控)。"""
        now = time.time()
        count = 0
        for slots in self._stores.values():
            for s in slots:
                if s.state != "cooling" and self._decayed_energy(s, now) >= self.FADING_THRESHOLD:
                    count += 1
        return count

    def debug_dump(self, bot_id: str = "", group_id: int = 0) -> str:
        """调试面板: 打印槽状态。"""
        lines: list[str] = []
        for key, slots in self._stores.items():
            if bot_id and not key.startswith(bot_id):
                continue
            if group_id and str(group_id) not in key:
                continue
            now = time.time()
            for s in slots:
                energy = self._decayed_energy(s, now)
                _ts = f" ts={s.thread_summary[:40]}" if s.thread_summary else ""
                lines.append(
                    f"[{s.state:.6s}] energy={energy:.3f} idle={s.idle:.0f}s "
                    f"age={s.age:.0f}s participants={len(s.participants)} "
                    f"anchor={s.topic_anchor[:60]}{_ts}"
                )
        return "\n".join(lines) if lines else "(无活跃槽)"


# ── 模块级单例 ──────────────────────────────────────────────

_slot_manager: AttentionSlotManager | None = None


def get_slot_manager() -> AttentionSlotManager:
    """获取模块级 AttentionSlotManager 单例 (推荐)。"""
    global _slot_manager
    if _slot_manager is None:
        _slot_manager = AttentionSlotManager()
    return _slot_manager
