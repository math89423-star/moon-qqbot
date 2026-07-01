"""Per-bot 疲劳/精力状态管理器 — -1~+1 单轴，0 基线，指数衰减。

五大属性之五: 疲劳值 (Fatigue) — bot 自身的精力水平。
与心情值 (Mood) 的 valence 同量纲 (-1~+1)，但时间尺度更长:
  - 心情值: 分钟级波动，30min 半衰 (valence)
  - 疲劳值: 小时级波动，2h 半衰

设计:
  - -1.0 = 精力耗尽，话少、不主动、语气慵懒
  -  0.0 = 正常基线，自然回归点
  - +1.0 = 精力充沛，话多、主动、语气活跃
  - 每轮互动消耗 0.02~0.05
  - 正向互动回复 0.01~0.03
  - 负向互动加速消耗
  - 空闲时指数衰减回 0 (半衰期 2h)

用法:
  from .persona_state import (
      get_fatigue, save_fatigue,
      tick_fatigue, get_fatigue_prompt,
  )
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

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
    if self_id not in _locks:
        _locks[self_id] = asyncio.Lock()
    return _locks[self_id]


def _store_path_for(self_id: str) -> Path:
    return _STORE_DIR / f"fatigue_{self_id}.json"


# ── 疲劳值参数 ──────────────────────────────────────
_FATIGUE_HALF_LIFE = 7200.0   # 半衰期 2h (比心情 30min 慢 4x)
_FATIGUE_BASELINE = 0.0       # 自然回归点
_FATIGUE_CLAMP_MIN = -1.0
_FATIGUE_CLAMP_MAX = 1.0

# ── 单次互动消耗/恢复 ──────────────────────────────
# 每轮聊天后的疲劳变化 (delta，未经互动质量调制)

# 基础消耗: 每次回复都消耗精力
_REPLY_COST = -0.025

# 互动质量调制:
_QUALITY_MOD: dict[str, float] = {
    # InteractionQuality.value → fatigue delta
    "good":     +0.030,   # 好互动 → 真正恢复精力 (净 +0.005)
    "normal":   -0.010,   # 正常 → 轻微消耗
    "bad":      -0.040,   # 坏互动 → 加速消耗
    "brief":    -0.005,   # 简短互动 → 几乎不消耗
    "awkward":  -0.030,   # 尴尬互动 → 较多消耗
    "relief":   +0.030,   # 释然 → 真正恢复 (净 +0.005)
}

# 主动发言额外消耗
_ACTIVE_COST = -0.015
# 被冷落额外消耗
_MISSED_COST = -0.020


# ═══════════════════════════════════════════════════════════════
# FatigueState — per-bot 疲劳状态
# ═══════════════════════════════════════════════════════════════


class FatigueState:
    """per-bot 疲劳状态 — -1~+1 单轴。

    对标 MoodState.valence 的量纲 (-1~+1)，但时间尺度更长 (2h vs 30min)。
    持久化到 JSON，重启后按离线时长补衰减。
    """

    def __init__(self, value: float = 0.0, updated_at: float | None = None) -> None:
        self.value = max(_FATIGUE_CLAMP_MIN, min(_FATIGUE_CLAMP_MAX, value))
        self.updated_at = updated_at or time.time()

    # ── 衰减 ──────────────────────────────────────────

    def decay(self, now: float | None = None) -> None:
        """指数衰减到基线 0 (读时触发)。"""
        if now is None:
            now = time.time()
        elapsed = now - self.updated_at
        if elapsed <= 0:
            return
        decay = 0.5 ** (elapsed / _FATIGUE_HALF_LIFE)
        self.value = _FATIGUE_BASELINE + (self.value - _FATIGUE_BASELINE) * decay
        # 接近基线时归零 (避免 -0.001 之类永不平息的微扰)
        if abs(self.value) < 0.005:
            self.value = 0.0
        self.updated_at = now

    # ── 应用 delta ────────────────────────────────────

    def apply_delta(self, delta: float, now: float | None = None) -> None:
        """累积 delta 到疲劳值，钳制到 [-1, +1]."""
        if now is None:
            now = time.time()
        self.value = max(_FATIGUE_CLAMP_MIN, min(_FATIGUE_CLAMP_MAX, self.value + delta))
        self.updated_at = now

    # ── 标签 ──────────────────────────────────────────

    @property
    def label(self) -> str:
        v = self.value
        if v > 0.35:
            return "精力充沛"
        if v > 0.10:
            return "状态不错"
        if v > -0.10:
            return "正常"
        if v > -0.35:
            return "有点累"
        if v > -0.60:
            return "疲惫"
        return "筋疲力尽"

    def to_prompt_hint(self) -> str:
        """生成叙事化疲劳提示。中性 (> -0.15) 时不注入，节省 token。"""
        if self.value > -0.15:
            return ""  # 正常状态不注入
        v = self.value
        if v > -0.30:
            return (
                "[当前精力]\n"
                "有点累了，回复可以简短一些，不用太热情。但别让对方觉得你在敷衍。"
            )
        if v > -0.50:
            return (
                "[当前精力]\n"
                "你现在挺累的。话少一点、句子短一点——但还是要回应。"
                "语气可以慵懒一点，像加班到晚上十点还在回消息。"
            )
        if v > -0.70:
            return (
                "[当前精力]\n"
                "筋疲力尽、不想说话。如果可以，用最少的字把对方打发掉。"
                "不要主动展开话题。语气慢悠悠的，句号比感叹号多。"
            )
        return (
            "[当前精力]\n"
            "累到不想动了。能用表情包/颜文字解决的就别打字。"
            "真的需要回复时，控制在 15 字以内。不想装热情。"
        )

    # ── 持久化 ──────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "value": round(self.value, 4),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FatigueState":
        return cls(
            value=float(data.get("value", 0.0)),
            updated_at=float(data.get("updated_at", time.time())),
        )


# ═══════════════════════════════════════════════════════════════
# 模块级 per-bot 字典
# ═══════════════════════════════════════════════════════════════

_fatigues: dict[str, FatigueState] = {}


def _load_from_disk(self_id: str) -> FatigueState:
    store_path = _store_path_for(self_id)
    if not store_path.exists():
        logger.info("bot %s 疲劳值: 无存档，从基线 0 开始", self_id)
        return FatigueState()
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
        fs = FatigueState.from_dict(data)
        # ★ 关键: 按离线时长衰减
        fs.decay()
        logger.info(
            "bot %s 疲劳值: 已加载 %.2f label=%s (离线衰减后)",
            self_id, fs.value, fs.label,
        )
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        logger.warning("bot %s 疲劳值: 文件损坏，从基线 0 开始: %s", self_id, e)
        return FatigueState()
    else:
        return fs
        return FatigueState()


def get_fatigue(self_id: str) -> FatigueState:
    """获取 per-bot 疲劳状态 (惰性加载 + 读时衰减)。"""
    if self_id not in _fatigues:
        _fatigues[self_id] = _load_from_disk(self_id)
    else:
        _fatigues[self_id].decay()
    return _fatigues[self_id]


async def save_fatigue(self_id: str) -> None:
    """持久化指定 bot 的疲劳值。"""
    if self_id not in _fatigues:
        return
    fs = _fatigues[self_id]
    store_path = _store_path_for(self_id)
    async with _get_lock(self_id):
        store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(fs.to_dict(), ensure_ascii=False, indent=2)
        try:
            tmp = store_path.with_suffix(".tmp")
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: tmp.write_text(payload, encoding="utf-8"),
            )
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: tmp.replace(store_path),
            )
        except Exception:
            logger.exception("bot %s 疲劳值保存失败", self_id)


async def tick_fatigue(
    self_id: str,
    *,
    quality: str = "normal",      # "good" | "normal" | "bad" | "brief" | "awkward" | "relief"
    is_active: bool = False,      # bot 主动发言 (消耗更大)
    was_missed: bool = False,     # bot 发言后没人接话 (额外消耗)
) -> None:
    """每轮互动后推进疲劳值。

    delta = 基础消耗 + 质量调制 + 主动/被冷落惩罚

    Args:
        self_id: bot QQ 号
        quality: 互动质量 ("good"/"normal"/"bad"/"brief"/"awkward"/"relief")
        is_active: bot 是否主动发言
        was_missed: 发言后是否被冷落
    """
    fs = get_fatigue(self_id)
    fs.decay()  # 先衰减到当前时刻

    # 基础消耗
    delta = _REPLY_COST

    # 质量调制
    delta += _QUALITY_MOD.get(quality, -0.010)

    # 主动发言
    if is_active:
        delta += _ACTIVE_COST

    # 被冷落
    if was_missed:
        delta += _MISSED_COST

    fs.apply_delta(delta)
    await save_fatigue(self_id)

    _old = fs.value - delta
    logger.debug(
        "bot %s 疲劳值: %.3f → %.3f (Δ=%.3f, quality=%s, active=%s, missed=%s)",
        self_id, _old, fs.value, delta, quality, is_active, was_missed,
    )


def get_fatigue_prompt(self_id: str) -> str:
    """获取叙事化疲劳提示文本。

    空字符串 = 正常状态，无需注入。
    非空 = 如 "[当前精力]\n有点累了，回复可以简短一些..."
    """
    fs = get_fatigue(self_id)
    return fs.to_prompt_hint()
