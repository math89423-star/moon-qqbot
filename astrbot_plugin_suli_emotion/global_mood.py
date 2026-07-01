"""全局情绪 — per-bot GlobalMood (Valence x Arousal).

每个 bot 拥有独立的内在状态，通过 self_id (QQ号) 隔离。
群友 1 惹怒了 bot A → bot A 会感受到火气，bot B 不受影响。

核心公式: 最终态度 = 全局情绪(per-bot, 易变, 会衰减) x 局部好感(per-user, 稳定)

用法:
  from .global_mood import (
      get_global_mood, save_global_mood,
      apply_to_global_mood, affinity_mood_weight,
  )
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .mood import MAX_EVENT_HISTORY, MoodState

logger = logging.getLogger(__name__)


def _get_plugin_data_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        _base = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_suli_emotion"
    except ImportError:
        _base = Path("data/plugin_data/astrbot_plugin_suli_emotion")
    _base.mkdir(parents=True, exist_ok=True)
    return _base
_STORE_DIR = _get_plugin_data_dir()
_locks: dict[str, asyncio.Lock] = {}


def _get_lock(self_id: str) -> asyncio.Lock:
    """获取 per-bot 异步锁 (避免双 bot 互相阻塞文件 I/O)."""
    if self_id not in _locks:
        _locks[self_id] = asyncio.Lock()
    return _locks[self_id]


def _store_path_for(self_id: str) -> Path:
    """每个 bot 独立的情绪持久化文件."""
    return _STORE_DIR / f"global_mood_{self_id}.json"

# ═══════════════════════════════════════════════════════════
# affinity → mood 权重 (防御优先版)
# ═══════════════════════════════════════════════════════════


def affinity_mood_weight(affinity_level: int) -> float:
    """好感等级 → 对全局情绪的影响权重。

    防御优先: 单调递增，陌生捣乱者推不动全局 mood。
    代价: 牺牲"被讨厌的人激怒"的真实性（真实人类是 U 形——亲近和讨厌的人
    都能强烈影响你，中性陌生人最弱）。

    
      权重单调递增而非 U 形，是有意为之的工程权衡。
      若日后真实性压倒防御需求，可给 Lv.-2 一个略高权重（如 0.05→0.2），
      但需同时评估滥用面。
    """
    _weights = {
        -2: 0.05,  # 黑名单: 几乎影响不了你 (防御)
        -1: 0.15,  # 疏远
         0: 0.25,  # 陌生
         1: 0.40,  # 普通
         2: 0.55,  # 熟悉
         3: 0.70,  # 喜欢
         4: 0.85,  # 亲密
         5: 1.00,  # 珍视: 完全影响力
    }
    return _weights.get(affinity_level, 0.25)


# ═══════════════════════════════════════════════════════════
# GlobalMood — per-bot 全局单例
# ═══════════════════════════════════════════════════════════


@dataclass
class GlobalMood:
    """per-bot 情绪 — bot 此刻的整体心情。

    持久化到 data/global_mood_{self_id}.json，含时间戳。
    重启时读回并按离线时长继续衰减，保证平滑过渡——
    崩溃前烦躁的 bot 重启后依然略带烦躁，而非瞬间归零。

    每个 bot (按 self_id=QQ号) 拥有独立的 GlobalMood 实例。
    """

    mood: MoodState = field(default_factory=MoodState)
    _self_id: str = ""  # 所属 bot 的 QQ 号

    # PromptInterceptor 阻尼状态 (跨轮平滑，从旧 UserRelation._prev_* 迁入)
    # 初始化为基线值 (0.3) 而非 0.0，消除首轮吸收基线差的浪费
    _prev_valence: float = 0.3
    _prev_arousal: float = 0.3

    # ── 衰减 ──────────────────────────────────────────

    def decay(self, now: float | None = None) -> None:
        """衰减到当前时刻 (读时触发)。"""
        self.mood.decay(now)

    # ── 应用事件 ──────────────────────────────────────

    def apply_event(
        self,
        delta_valence: float,
        delta_arousal: float,
        event_name: str,
        affinity_level: int = 0,
        now: float | None = None,
    ) -> None:
        """应用情绪事件到全局 mood，经 affinity 权重缩放。

        Args:
            delta_valence: 原始 valence 变化量
            delta_arousal: 原始 arousal 变化量
            event_name: 事件名称
            affinity_level: 触发用户的 affinity 等级 (用于权重缩放)
            now: 时间戳
        """
        weight = affinity_mood_weight(affinity_level)
        scaled_v = delta_valence * weight
        scaled_a = delta_arousal * weight

        if abs(weight - 1.0) > 0.01:
            logger.debug(
                "全局 mood: %s Δv=%.3f→%.3f Δa=%.3f→%.3f (weight=%.2f Lv.%+d)",
                event_name, delta_valence, scaled_v,
                delta_arousal, scaled_a, weight, affinity_level,
            )

        self.mood.apply_event(scaled_v, scaled_a, event_name, now)

    # ── 便捷属性 ──────────────────────────────────────

    @property
    def valence(self) -> float:
        return self.mood.valence

    @property
    def arousal(self) -> float:
        return self.mood.arousal

    @property
    def label(self) -> str:
        return self.mood.label

    def to_prompt_hint(self) -> str:
        """生成全局情绪提示文本 (底层常驻注入)。

        中性时不注入，节省 token。
        """
        return self.mood.to_prompt_hint()

    # ── 持久化 ──────────────────────────────────────────

    async def save(self) -> None:
        """持久化 per-bot mood 到磁盘 (含时间戳，供重启恢复)。

        使用 asyncio.Lock + run_in_executor 避免阻塞事件循环。
        """
        store_path = _store_path_for(self._self_id)
        async with _get_lock(self._self_id):
            store_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {
                    "self_id": self._self_id,
                    "valence": round(self.mood.valence, 4),
                    "arousal": round(self.mood.arousal, 4),
                    "updated_at": self.mood.updated_at,
                    "last_event": self.mood.last_event,
                    "event_history": self.mood.event_history[-MAX_EVENT_HISTORY:],
                    "_prev_valence": round(self._prev_valence, 4),
                    "_prev_arousal": round(self._prev_arousal, 4),
                },
                ensure_ascii=False,
                indent=2,
            )
            try:
                tmp = store_path.with_suffix(".tmp")
                # 文件 I/O 委托给线程池，不阻塞事件循环
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: tmp.write_text(payload, encoding="utf-8"),
                )
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: tmp.replace(store_path),
                )
            except Exception:
                logger.exception("bot %s 全局 mood 保存失败", self._self_id)

    @classmethod
    def load(cls, self_id: str) -> "GlobalMood":
        """从磁盘加载 per-bot mood，按离线时长衰减。

        若文件不存在或损坏，返回基线状态的新实例。
        这是"重启≠归零"的关键实现——读回时间戳，补衰减。
        """
        store_path = _store_path_for(self_id)
        if not store_path.exists():
            logger.info("bot %s 全局 mood: 无存档，从基线开始 (V=0.30 A=0.00)", self_id)
            return cls(_self_id=self_id)

        try:
            data = json.loads(store_path.read_text(encoding="utf-8"))
            mood = MoodState(
                valence=float(data.get("valence", 0.3)),
                arousal=float(data.get("arousal", 0.0)),
                updated_at=float(data.get("updated_at", time.time())),
                last_event=str(data.get("last_event", "")),
                event_history=data.get("event_history", [])[-MAX_EVENT_HISTORY:],
            )
            gm = cls(mood=mood, _self_id=self_id)
            gm._prev_valence = float(data.get("_prev_valence", 0.0))
            gm._prev_arousal = float(data.get("_prev_arousal", 0.0))

            # ★ 关键: 按离线时长衰减
            gm.decay()

            logger.info(
                "bot %s 全局 mood: 已加载 V=%.2f A=%.2f label=%s (离线衰减后)",
                self_id, gm.valence, gm.arousal, gm.label,
            )
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning("bot %s 全局 mood: 文件损坏，从基线开始: %s", self_id, e)
        else:
            return gm
        return cls(_self_id=self_id)


# ═══════════════════════════════════════════════════════════
# 模块级 per-bot 字典
# ═══════════════════════════════════════════════════════════

_moods: dict[str, GlobalMood] = {}


def get_global_mood(self_id: str) -> GlobalMood:
    """获取 per-bot mood 实例 (惰性加载 + 读时衰减)。

    首次调用从磁盘加载 (data/global_mood_{self_id}.json)，
    后续返回内存实例。每次读取前自动衰减到当前时刻。
    """
    if self_id not in _moods:
        _moods[self_id] = GlobalMood.load(self_id)
    else:
        _moods[self_id].decay()
    return _moods[self_id]


async def save_global_mood(self_id: str) -> None:
    """持久化指定 bot 的 mood."""
    if self_id in _moods:
        await _moods[self_id].save()


async def apply_to_global_mood(
    self_id: str,
    events: list,  # list[EmotionEvent] — 避免循环导入，不写类型注解
    affinity_level: int = 0,
) -> None:
    """将情绪事件的 mood 部分应用到 per-bot 实例 (经 affinity 权重缩放)。

    与 apply_to_user_affinity() 配对使用——情绪事件发生后，
    mood delta → per-bot 全局，affinity delta → per-user。

    Args:
        self_id: bot 的 QQ 号
        events: EmotionEvent 列表
        affinity_level: 触发用户的 affinity 等级
    """
    if not events:
        return

    gm = get_global_mood(self_id)
    now = time.time()

    for evt in events:
        if evt.delta_valence != 0.0 or evt.delta_arousal != 0.0:
            gm.apply_event(
                evt.delta_valence,
                evt.delta_arousal,
                evt.name,
                affinity_level=affinity_level,
                now=now,
            )

    # 混合事件记录
    if len(events) > 1:
        names = "+".join(e.name for e in events)
        gm.mood.last_event = names

    await gm.save()
