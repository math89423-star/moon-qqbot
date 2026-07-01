"""暮恩短期情绪 (Mood) — 效价-唤醒双维模型。

从 emotion.py 拆分出的独立模块。

用法:
  from .mood import MoodState
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# ═══════════════════════════════════════════════════════════

# 情绪阻尼默认参数
_DAMP_DEFAULT = 0.65          # 默认平滑力度
_MAX_DELTA_VALENCE = 0.30     # 愉悦度单次变化上限
_MAX_DELTA_AROUSAL = 0.35     # 唤醒度单次变化上限
MAX_EVENT_HISTORY = 50         # 情绪事件历史最大保留条数


def _apply_damping_simple(
    prev: float,
    raw: float,
    damp: float = _DAMP_DEFAULT,
    confidence: float = 0.5,
    max_delta: float = _MAX_DELTA_VALENCE,
) -> float:
    """BetterSimTracker 阻尼平滑: 限制单次变化幅度。

    scale = (1 - damp) + confidence × damp
    delta = clamp(raw_delta × scale, -max_delta, +max_delta)

    Args:
        prev: 上一轮值
        raw: 本轮原始值 (prev + delta)
        damp: 平滑力度 (0 = 无平滑, 1 = 完全平滑)
        confidence: 变化可信度 (0~1, 越大越接近原始 delta)
        max_delta: 单次最大变化量

    Returns:
        平滑后的值
    """
    raw_delta = raw - prev
    scale = (1.0 - damp) + confidence * damp
    delta = max(-max_delta, min(raw_delta * scale, max_delta))
    return prev + delta


# ═══════════════════════════════════════════════════════════
# MoodState — 短期情绪 (Valence × Arousal)
# ═══════════════════════════════════════════════════════════


@dataclass
class MoodState:
    """短期情绪 — Valence × Arousal 连续空间。

    valence:  -1.0 (很不开心) ~ +1.0 (很开心), 基线 +0.3
    arousal:  -1.0 (很慵懒)   ~ +1.0 (很兴奋),  基线  0.0
    """

    valence: float = 0.3
    arousal: float = 0.0
    updated_at: float = field(default_factory=time.time)
    last_event: str = ""
    event_history: list[dict] = field(default_factory=list)

    # 参数 (可外部覆盖)
    valence_half_life: float = 1800.0
    arousal_half_life: float = 600.0
    valence_baseline: float = 0.3
    arousal_baseline: float = 0.0

    # ── 衰减 ──────────────────────────────────────────

    def decay(self, now: float | None = None) -> None:
        if now is None:
            now = time.time()
        elapsed = now - self.updated_at
        if elapsed <= 0:
            return
        v_decay = 0.5 ** (elapsed / self.valence_half_life)
        self.valence = self.valence_baseline + (self.valence - self.valence_baseline) * v_decay
        a_decay = 0.5 ** (elapsed / self.arousal_half_life)
        self.arousal = self.arousal_baseline + (self.arousal - self.arousal_baseline) * a_decay
        self.updated_at = now

    # ── 应用事件 ──────────────────────────────────────

    def apply_event(
        self,
        delta_valence: float,
        delta_arousal: float,
        event_name: str,
        now: float | None = None,
        damp: bool = True,
    ) -> None:
        """应用情绪事件 delta，可选 BetterSimTracker 阻尼平滑。

        Args:
            delta_valence: 愉悦度变化量
            delta_arousal: 唤醒度变化量
            event_name: 事件名称 (日志用)
            now: 时间戳
            damp: 是否启用 BetterSimTracker 阻尼 (限制单次波动幅度)
        """
        if now is None:
            now = time.time()

        if damp:
            # BetterSimTracker 阻尼平滑: 防止单次情绪剧烈波动
            # scale = (1-damp) + confidence*damp
            # 情绪事件的可信度与 delta 绝对值成正比 — 大波动更高置信
            raw_v = self.valence + delta_valence
            raw_a = self.arousal + delta_arousal
            v_confidence = min(0.95, 0.4 + abs(delta_valence) * 0.7)
            a_confidence = min(0.95, 0.4 + abs(delta_arousal) * 0.7)
            damped_v = _apply_damping_simple(
                self.valence, raw_v, confidence=v_confidence, max_delta=0.30,
            )
            damped_a = _apply_damping_simple(
                self.arousal, raw_a, confidence=a_confidence, max_delta=0.35,
            )
            delta_valence = damped_v - self.valence
            delta_arousal = damped_a - self.arousal

        self.valence = max(-1.0, min(1.0, self.valence + delta_valence))
        self.arousal = max(-1.0, min(1.0, self.arousal + delta_arousal))
        self.updated_at = now
        self.last_event = event_name
        self.event_history.append({
            "type": "mood",
            "event": event_name,
            "time": now,
            "d_valence": delta_valence,
            "d_arousal": delta_arousal,
        })
        if len(self.event_history) > MAX_EVENT_HISTORY:
            self.event_history = self.event_history[-MAX_EVENT_HISTORY:]

    # ── 情绪标签 ──────────────────────────────────────

    @property
    def label(self) -> str:
        v, a = self.valence, self.arousal
        if v > 0.4 and a > 0.3:
            return "开心活泼"
        if v > 0.2 and a > 0.3:
            return "兴奋好奇"
        if v > 0.3 and -0.3 <= a <= 0.3:
            return "温柔平和"
        if v > 0.1 and a < -0.3:
            return "慵懒满足"
        if v < -0.2 and a > 0.2:
            return "烦躁委屈"
        if v < -0.3 and a < -0.2:
            return "低落消沉"
        return "平静中性"

    def to_prompt_hint(self) -> str:
        """生成短期情绪提示（中性时不注入，节省 token）。"""
        label = self.label
        if label == "平静中性":
            return ""

        lines = [f"你此刻的情绪: {label}"]

        tone_map = {
            "开心活泼": "语气轻快跳跃，多用颜文字。句子短促有力。",
            "兴奋好奇": "很感兴趣，可以多追问、多感叹。",
            "温柔平和": "语气温和自然，像和朋友轻声聊天。",
            "慵懒满足": "语气慢悠悠，句子偏短。有点懒但很安心。",
            "烦躁委屈": "可能有点带刺、嘴硬，但不要真的伤人。",
            "低落消沉": "话少、字少、颜文字消失。不想说太多但还是会回应。",
        }
        if label in tone_map:
            lines.append(tone_map[label])
        return "[此刻情绪]\n" + "\n".join(lines)


# ═══════════════════════════════════════════════════════════
