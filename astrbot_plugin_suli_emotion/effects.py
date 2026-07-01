"""Persona Effects — 长期人格效应引擎。

从 self_evolution/engine/persona_sim_rules.py 提取核心逻辑。

与 suli_emotion 现有模块的关系:
  - mood.py (MoodState): 短期情绪 — 效价/唤醒双维模型, 分钟级衰减
  - effects.py (本模块): 长期效应 — 持续叙事驱动的 Effect 系统, 小时级衰减
  - emotion_engine.py (EmotionEngine): 事件检测 — 关键词→情绪事件

Effect 系统的核心创新 (vs 单纯的情绪模型):
  1. 每个 Effect 携带叙事语义: source_detail (来源), decay_style, recovery_style
  2. 状态阈值 + 近期互动事件 → 联合触发 Effect
  3. Prompt 注入不再是数字 ("精力=80"), 而是叙事 ("刚主动搭话但被冷落, 还有点堵着")

用法:
  from astrbot_plugin_suli_emotion.effects import (
      EffectEngine, PersonaState, Effect, snapshot_to_prompt,
  )

  engine = EffectEngine()
  snapshot = engine.tick(state, recent_events)
  prompt = snapshot_to_prompt(snapshot)
"""

from __future__ import annotations

import time
from copy import copy
from dataclasses import dataclass, field
from enum import Enum

# ═════════════════════════════════════════════════════════════════
# Types
# ═════════════════════════════════════════════════════════════════

class EffectType(Enum):
    BUFF = "buff"
    DEBUFF = "debuff"
    NEUTRAL = "neutral"


class InteractionQuality(Enum):
    GOOD = "good"
    NORMAL = "normal"
    BAD = "bad"
    BRIEF = "brief"
    AWKWARD = "awkward"
    RELIEF = "relief"


class InteractionMode(Enum):
    ACTIVE = "active"
    PASSIVE = "passive"


class InteractionOutcome(Enum):
    CONNECTED = "connected"
    MISSED = "missed"


@dataclass
class PersonaState:
    """四个核心数值 (0-100)."""
    energy: float = 80.0
    mood: float = 70.0
    social_need: float = 50.0
    satiety: float = 80.0
    last_tick_at: float = field(default_factory=time.time)
    last_interaction_at: float = field(default_factory=time.time)


@dataclass
class Effect:
    """持久人格效应。

    每个 Effect 携带叙事语义:
      - source_detail: 触发来源描述 (如 "主动搭话但被冷落，期望落空")
      - decay_style: 衰减风格
      - recovery_style: 恢复条件
      - prompt_hint: 注入到 LLM prompt 的情绪提示短句
    """
    effect_id: str
    effect_type: EffectType
    name: str
    intensity: int = 1
    started_at: float = 0.0
    expires_at: float = 0.0
    prompt_hint: str = ""
    tags: list[str] = field(default_factory=list)
    source_detail: str = ""
    decay_style: str = "gradual"
    recovery_style: str = "passive"

    def is_active(self, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        return self.expires_at <= 0 or now < self.expires_at


@dataclass
class InteractionEvent:
    """近期互动事件快照。"""
    quality: InteractionQuality = InteractionQuality.NORMAL
    mode: InteractionMode = InteractionMode.PASSIVE
    outcome: InteractionOutcome = InteractionOutcome.CONNECTED
    summary: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class PersonaSnapshot:
    """tick() 返回的快照 — 当前状态 + 活跃 Effect + 近期事件。"""
    state: PersonaState
    active_effects: list[Effect]
    recent_events: list[InteractionEvent]
    snapshot_at: float = field(default_factory=time.time)


# ═════════════════════════════════════════════════════════════════
# Default Effects — 12 条基础效应定义
# ═════════════════════════════════════════════════════════════════

_DEFAULT_EFFECTS: dict[str, Effect] = {
    "low_energy": Effect(
        effect_id="low_energy", effect_type=EffectType.DEBUFF,
        name="疲惫", intensity=2, prompt_hint="有点累，懒得动",
        tags=["energy"], source_detail="体力自然消耗，未及时休息",
        decay_style="slow", recovery_style="rest",
    ),
    "low_mood": Effect(
        effect_id="low_mood", effect_type=EffectType.DEBUFF,
        name="低落", intensity=2, prompt_hint="心情不太好",
        tags=["mood"], source_detail="持续的情绪下行",
        decay_style="gradual", recovery_style="positive_event",
    ),
    "lonely": Effect(
        effect_id="lonely", effect_type=EffectType.DEBUFF,
        name="孤独", intensity=2, prompt_hint="想和人聊聊天",
        tags=["social"], source_detail="长时间没有社交互动",
        decay_style="slow", recovery_style="social_connection",
    ),
    "hungry": Effect(
        effect_id="hungry", effect_type=EffectType.DEBUFF,
        name="饿了", intensity=1, prompt_hint="有点饿",
        tags=["satiety"], source_detail="饱腹感持续下降",
        decay_style="steady", recovery_style="eating",
    ),
    "irritated": Effect(
        effect_id="irritated", effect_type=EffectType.DEBUFF,
        name="烦躁", intensity=3, prompt_hint="有点烦躁",
        tags=["mood"], source_detail="遭遇不愉快互动或持续压力",
        decay_style="responsive", recovery_style="calm_environment",
    ),
    "wronged": Effect(
        effect_id="wronged", effect_type=EffectType.DEBUFF,
        name="委屈", intensity=3, prompt_hint="心里有点委屈",
        tags=["mood"], source_detail="被忽视或遭受尴尬/负面互动",
        decay_style="sticky", recovery_style="being_heard",
    ),
    "tired": Effect(
        effect_id="tired", effect_type=EffectType.DEBUFF,
        name="困倦", intensity=2, prompt_hint="好困",
        tags=["energy"], source_detail="精力持续不足且未得到充分休息",
        decay_style="accelerating", recovery_style="sleep",
    ),
    "curious": Effect(
        effect_id="curious", effect_type=EffectType.BUFF,
        name="好奇", intensity=1, prompt_hint="有点好奇",
        tags=["mood"], source_detail="近期有多次互动接触",
        decay_style="quick", recovery_style="satisfying_interaction",
    ),
    "relieved": Effect(
        effect_id="relieved", effect_type=EffectType.BUFF,
        name="轻松", intensity=1, prompt_hint="感觉轻松",
        tags=["mood"], source_detail="在低落之后被正面互动接住",
        decay_style="gentle", recovery_style="sustained_positive",
    ),
    "satisfied": Effect(
        effect_id="satisfied", effect_type=EffectType.BUFF,
        name="满足", intensity=2, prompt_hint="心情不错",
        tags=["mood"], source_detail="互动质量好，预期被满足",
        decay_style="moderate", recovery_style="continued_positive",
    ),
    "sleepy": Effect(
        effect_id="sleepy", effect_type=EffectType.DEBUFF,
        name="想睡", intensity=2, prompt_hint="困了",
        tags=["energy"], source_detail="体力透支，早该休息了",
        decay_style="accelerating", recovery_style="sleep",
    ),
    "thriving": Effect(
        effect_id="thriving", effect_type=EffectType.BUFF,
        name="神清气爽", intensity=2, prompt_hint="状态很好",
        tags=["energy", "mood"], source_detail="精力和心情同时处于高位",
        decay_style="slow", recovery_style="maintaining_balance",
    ),
}


# ═════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════

def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _make_effect(effect_id: str, now: float, dur: float) -> Effect:
    """创建 Effect 副本并设置时间戳。"""
    e = copy(_DEFAULT_EFFECTS[effect_id])
    e.started_at = now
    e.expires_at = now + dur
    return e


# ═════════════════════════════════════════════════════════════════
# EffectEngine
# ═════════════════════════════════════════════════════════════════

class EffectEngine:
    """长期人格效应引擎 — 纯函数式, 无状态, 无 I/O。"""

    def __init__(self, effect_duration_hours: float = 2.0) -> None:
        self._effect_duration = effect_duration_hours * 3600.0

    # ── State Evolution ─────────────────────────────────

    @staticmethod
    def calc_state_delta(
        state: PersonaState,
        elapsed_hours: float,
        *,
        interaction_recent: bool = False,
    ) -> PersonaState:
        """根据时间流逝计算状态变化 (纯函数)。"""
        if elapsed_hours <= 0:
            return state

        energy_d = -elapsed_hours * 1.5
        if interaction_recent:
            energy_d += elapsed_hours * 0.5
        elif elapsed_hours > 3.0:
            energy_d += (elapsed_hours - 3.0) * 1.0

        mood_d = -elapsed_hours * 0.8
        if interaction_recent:
            mood_d += elapsed_hours * 1.0

        social_d = elapsed_hours * 2.0
        if interaction_recent:
            social_d -= elapsed_hours * 1.5

        satiety_d = -elapsed_hours * 1.0

        return PersonaState(
            energy=_clamp(state.energy + energy_d),
            mood=_clamp(state.mood + mood_d),
            social_need=_clamp(state.social_need + social_d),
            satiety=_clamp(state.satiety + satiety_d),
            last_tick_at=time.time(),
            last_interaction_at=state.last_interaction_at,
        )

    # ── Effect Trigger Rules ────────────────────────────

    def _trigger_state_effects(
        self,
        state: PersonaState,
        active: set[str],
        recent_bad: bool,
        interaction_gap_h: float,
        now: float,
        dur: float,
    ) -> list[Effect]:
        """状态阈值触发规则。"""
        triggered: list[Effect] = []

        if state.energy < 30 and "low_energy" not in active:
            e = _make_effect("low_energy", now, dur)
            e.prompt_hint = "提不起劲，什么都懒得做"
            e.source_detail = f"体力跌至{state.energy:.0f}，自然消耗累积"
            triggered.append(e)

        if state.mood < 30 and "low_mood" not in active:
            e = _make_effect("low_mood", now, dur)
            e.prompt_hint = "心情有点低落，什么都提不起兴趣"
            e.source_detail = f"心情持续低迷，当前{state.mood:.0f}"
            triggered.append(e)

        if state.social_need > 70 and interaction_gap_h > 2.0 and "lonely" not in active:
            e = _make_effect("lonely", now, dur)
            e.prompt_hint = "好久没和人聊了，有点想找人说说笑"
            e.source_detail = f"超过{interaction_gap_h:.1f}h无互动，社交需求积累"
            triggered.append(e)

        if state.satiety < 30 and "hungry" not in active:
            e = _make_effect("hungry", now, dur)
            e.source_detail = f"饱腹感降至{state.satiety:.0f}"
            triggered.append(e)

        if state.mood < 20 and "irritated" not in active:
            e = _make_effect("irritated", now, dur)
            e.prompt_hint = "最近有点烦躁，对什么都提不起耐心"
            e.source_detail = "心情极低引发的烦躁"
            triggered.append(e)
        elif state.mood < 30 and recent_bad and "irritated" not in active:
            e = _make_effect("irritated", now, dur)
            e.prompt_hint = "受了点委屈，现在有点不耐烦"
            e.source_detail = "负面互动引发的烦躁感"
            triggered.append(e)

        if state.energy < 20 and interaction_gap_h > 3.0 and "sleepy" not in active:
            e = _make_effect("sleepy", now, dur)
            e.prompt_hint = "有点困了，脑子转不动"
            e.source_detail = "精力低下合并长期未休息"
            triggered.append(e)

        if state.energy > 80 and state.mood > 80 and "thriving" not in active:
            e = _make_effect("thriving", now, dur)
            e.prompt_hint = "状态很好，什么都挺顺的"
            e.source_detail = f"精力{state.energy:.0f}、心情{state.mood:.0f}双高"
            triggered.append(e)

        return triggered

    def _trigger_interaction_effects(
        self,
        active: set[str],
        recent_bad: bool,
        recent_good: bool,
        interaction_gap_h: float,
        recent_events: list[InteractionEvent],
        now: float,
        dur: float,
    ) -> list[Effect]:
        """互动事件触发规则。"""
        triggered: list[Effect] = []

        if recent_bad and "wronged" not in active:
            e = _make_effect("wronged", now, dur)
            e.prompt_hint = "刚才的互动有点受挫，心里不太舒服"
            bad_ev = next(
                (ev for ev in recent_events[-3:]
                 if ev.quality == InteractionQuality.BAD), None
            )
            if bad_ev:
                if bad_ev.outcome == InteractionOutcome.MISSED:
                    e.source_detail = "主动搭话但被冷落，期望落空"
                elif (bad_ev.outcome == InteractionOutcome.CONNECTED
                      and bad_ev.mode == InteractionMode.PASSIVE):
                    e.source_detail = "被动接受负面互动，有苦说不出"
                else:
                    e.source_detail = "遭遇负面互动，体验受挫"
            else:
                e.source_detail = "负面互动导致的委屈感"
            triggered.append(e)

        if recent_good and "low_mood" in active and "relieved" not in active:
            e = _make_effect("relieved", now, dur)
            e.prompt_hint = "心情终于好起来了，轻松了不少"
            e.source_detail = "低落之后被正面互动接住"
            triggered.append(e)

        if interaction_gap_h < 1.0 and "curious" not in active:
            e = _make_effect("curious", now, dur)
            e.prompt_hint = "最近聊得挺多的，对什么都有点好奇"
            e.source_detail = "短时间内有多次互动，接触面扩大"
            triggered.append(e)

        if recent_good and "satisfied" not in active:
            e = _make_effect("satisfied", now, dur)
            e.prompt_hint = "刚才的互动挺愉快的，心里挺满足"
            e.source_detail = "高质量互动带来的满足感"
            triggered.append(e)

        if "low_energy" in active and interaction_gap_h > 4.0 and "tired" not in active:
            e = _make_effect("tired", now, dur)
            e.prompt_hint = "一直没怎么休息，有点撑不住了"
            e.source_detail = "精力持续透支，支撑已达极限"
            triggered.append(e)

        return triggered

    def eval_effects(
        self,
        state: PersonaState,
        active_effect_ids: set[str],
        recent_events: list[InteractionEvent],
        now: float | None = None,
    ) -> list[Effect]:
        """根据状态 + 近期事件组合条件, 判断应触发哪些 Effect。

        分两组:
          1. 状态阈值触发 (能量/心情/社交/饱腹 → 疲惫/低落/孤独/饿了/烦躁/想睡/神清气爽)
          2. 互动事件触发 (负面互动→委屈, 正面互动→满足/轻松, 高频→好奇)
        """
        if now is None:
            now = time.time()

        dur = self._effect_duration
        active = active_effect_ids

        recent_bad = any(
            e.quality == InteractionQuality.BAD for e in recent_events[-3:]
        )
        recent_good = any(
            e.quality == InteractionQuality.GOOD for e in recent_events[-3:]
        )
        interaction_gap_h = (
            (now - state.last_interaction_at) / 3600.0
            if state.last_interaction_at > 0 else 999.0
        )

        state_effects = self._trigger_state_effects(
            state, active, recent_bad, interaction_gap_h, now, dur,
        )
        interaction_effects = self._trigger_interaction_effects(
            active, recent_bad, recent_good, interaction_gap_h,
            recent_events, now, dur,
        )

        return state_effects + interaction_effects

    # ── Interaction Application ─────────────────────────

    @staticmethod
    def apply_interaction(
        state: PersonaState,
        quality: InteractionQuality = InteractionQuality.NORMAL,
        mode: InteractionMode = InteractionMode.PASSIVE,
        outcome: InteractionOutcome = InteractionOutcome.CONNECTED,
    ) -> PersonaState:
        """用户互动后刷新状态 (纯函数)。"""
        now = time.time()

        if quality == InteractionQuality.GOOD:
            mood_d, social_d, energy_d = 15.0, -20.0, -2.0
        elif quality == InteractionQuality.BAD:
            mood_d, social_d, energy_d = -10.0, -10.0, -5.0
        elif quality == InteractionQuality.BRIEF:
            mood_d, social_d, energy_d = 3.0, -5.0, 0.0
        elif quality == InteractionQuality.AWKWARD:
            mood_d, social_d, energy_d = -5.0, -5.0, -2.0
        elif quality == InteractionQuality.RELIEF:
            mood_d, social_d, energy_d = 20.0, -10.0, 3.0
        else:  # NORMAL
            mood_d, social_d, energy_d = 5.0, -15.0, -3.0

        if mode == InteractionMode.ACTIVE:
            energy_d -= 2.0
        if outcome == InteractionOutcome.MISSED:
            mood_d -= 5.0

        return PersonaState(
            energy=_clamp(state.energy + energy_d),
            mood=_clamp(state.mood + mood_d),
            social_need=_clamp(state.social_need + social_d),
            satiety=_clamp(state.satiety - 2.0),
            last_tick_at=state.last_tick_at,
            last_interaction_at=now,
        )

    # ── Main Tick ───────────────────────────────────────

    def tick(
        self,
        state: PersonaState,
        recent_events: list[InteractionEvent] | None = None,
        interaction_quality: InteractionQuality | None = None,
        interaction_mode: InteractionMode = InteractionMode.PASSIVE,
        interaction_outcome: InteractionOutcome = InteractionOutcome.CONNECTED,
        now: float | None = None,
    ) -> PersonaSnapshot:
        """主入口 — 推进一轮人格时间。

        1. 应用互动效果 (如有)
        2. 计算时间流逝
        3. 触发新 Effect
        4. 返回完整快照
        """
        if now is None:
            now = time.time()

        recent_events = recent_events or []

        if interaction_quality is not None:
            state = self.apply_interaction(
                state, interaction_quality, interaction_mode, interaction_outcome,
            )
            recent_events.append(InteractionEvent(
                quality=interaction_quality,
                mode=interaction_mode,
                outcome=interaction_outcome,
                summary=(
                    f"{interaction_quality.value} x "
                    f"{interaction_mode.value} x "
                    f"{interaction_outcome.value}"
                ),
                timestamp=now,
            ))

        elapsed_h = (
            (now - state.last_tick_at) / 3600.0 if state.last_tick_at > 0 else 0
        )
        interaction_recent = any(
            (now - e.timestamp) < 3600 for e in recent_events[-5:]
        )
        state = self.calc_state_delta(
            state, elapsed_h, interaction_recent=interaction_recent,
        )

        new_effects = self.eval_effects(
            state, set(), recent_events, now,
        )

        return PersonaSnapshot(
            state=state,
            active_effects=new_effects,
            recent_events=recent_events[-10:],
            snapshot_at=now,
        )


# ═════════════════════════════════════════════════════════════════
# Prompt Generation — 叙事化注入
# ═════════════════════════════════════════════════════════════════

def snapshot_to_prompt(snapshot: PersonaSnapshot) -> str:
    """将 PersonaSnapshot 转为 LLM prompt 注入文本。

    核心创新: 不说"精力=30, 情绪=20", 而是说:
      "刚主动说了话但被冷落，还有点堵着"
    """
    effects_sorted = sorted(
        snapshot.active_effects, key=lambda x: x.intensity, reverse=True,
    )
    parts = [e.prompt_hint for e in effects_sorted[:3] if e.prompt_hint]

    if not parts:
        s = snapshot.state
        lines = []
        if s.energy < 40:
            lines.append(f"精力略低({s.energy:.0f})")
        if s.mood < 40:
            lines.append(f"心情略低({s.mood:.0f})")
        if s.social_need > 60:
            lines.append("社交需求较高")
        if s.satiety < 30:
            lines.append("有点饿了")
        if lines:
            parts.append("; ".join(lines))

    if not parts:
        return ""

    return f"[当前心境]\n{' | '.join(parts)}"


def snapshot_to_debug(snapshot: PersonaSnapshot) -> str:
    """调试用: 完整状态 + 所有 Effect 的详细信息。"""
    s = snapshot.state
    lines = [
        f"energy={s.energy:.0f} mood={s.mood:.0f} "
        f"social={s.social_need:.0f} satiety={s.satiety:.0f}",
    ]
    for eff in snapshot.active_effects:
        lines.append(
            f"  [{eff.effect_type.value}] {eff.name} "
            f"(intensity={eff.intensity}, decay={eff.decay_style}, "
            f"recovery={eff.recovery_style})"
        )
        if eff.source_detail:
            lines.append(f"    source: {eff.source_detail}")
        if eff.prompt_hint:
            lines.append(f"    hint: {eff.prompt_hint}")
    return "\n".join(lines)
