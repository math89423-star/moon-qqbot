"""astrbot_plugin_suli_emotion — 双轨情感系统 (mood/affinity 已拆分)。

5 个模块，集群内部耦合，零外部框架依赖:

  mood.py            — MoodState: 短期情绪 (Valence/Arousal 双维模型 + BetterSimTracker 阻尼)
  global_mood.py     — GlobalMood: per-bot 全局情绪单例 (自持久化 + 读时衰减 + affinity 权重缩放)
  emotion_engine.py  — EmotionEngine: 情绪事件检测 (17种事件 + 反讽纠正 + 恶意调教检测)
  affinity.py        — AffinityState + UserRelation: 长期好感 (-2~+5 离散等级) + 工具门控 + NyatBot 跨群聚合
  effects.py         — EffectEngine: 长期人格效应 (12条 Effect + 叙事驱动 Prompt 注入)

核心公式: 最终态度 = 全局情绪(per-bot, 易变, 会衰减) x 局部好感(per-user, 稳定)

用法:
  from astrbot_plugin_suli_emotion import (
      MoodState,
      EmotionEngine, EmotionEvent,
      AffinityState, UserRelation,
      GlobalMood, get_global_mood, save_global_mood,
      apply_to_global_mood, affinity_mood_weight,
      get_user_relation, save_user_relation, apply_emotion_events,
      can_use_tools, can_use_vlm, set_user_nickname,
      CompositeResult, compute_composite,
  )
"""

from __future__ import annotations

# ── 长期好感 + 门控 ───────────────────────────────────────
from .affinity import (
    AFFINITY_HINTS,
    AFFINITY_NAMES,
    FORBIDDEN_NICKNAMES,
    NICKNAME_MAX_LEN,
    TOOL_COOLDOWN_SECONDS,
    TOOL_HARASS_AFFINITY_PENALTY,
    TOOL_HARASS_THRESHOLD,
    AffinityGates,
    AffinityState,
    UserRelation,
    apply_emotion_events,
    apply_to_user_affinity,
    blacklist_add,
    blacklist_remove,
    can_generate_image,
    can_use_tools,
    can_use_vlm,
    check_daily_image_limit,
    check_daily_tools_limit,
    check_daily_vlm_limit,
    check_tool_cooldown,
    get_blacklist,
    get_user_relation,
    record_image_generation,
    record_tool_use,
    record_tools_usage,
    record_vlm_usage,
    reset_user_relation,
    save_user_relation,
    set_user_nickname,
    validate_nickname,
)

# ── 综合量化值 (warmth x energy 二维心境) ────────────────
from .composite import CompositeResult, compute_composite

# ── 长期人格效应 ──────────────────────────────────────────
from .effects import (
    Effect,
    EffectEngine,
    EffectType,
    InteractionEvent,
    InteractionMode,
    InteractionOutcome,
    InteractionQuality,
    PersonaSnapshot,
    PersonaState,
    snapshot_to_debug,
    snapshot_to_prompt,
)

# ── 情绪事件检测 ──────────────────────────────────────────
from .emotion_engine import EmotionEngine, EmotionEvent

# ── 全局情绪单例 ──────────────────────────────────────────
from .global_mood import (
    GlobalMood,
    affinity_mood_weight,
    apply_to_global_mood,
    get_global_mood,
    save_global_mood,
)

# ── 短期情绪 ──────────────────────────────────────────────
from .mood import MAX_EVENT_HISTORY, MoodState

# ── 疲劳/精力状态管理器 (-1~+1 单轴) ────────────────
from .persona_state import (
    FatigueState,
    get_fatigue,
    get_fatigue_prompt,
    save_fatigue,
    tick_fatigue,
)

__all__ = [
    "AFFINITY_HINTS",
    "AFFINITY_NAMES",
    "FORBIDDEN_NICKNAMES",
    "MAX_EVENT_HISTORY",
    "NICKNAME_MAX_LEN",
    "TOOL_COOLDOWN_SECONDS",
    "TOOL_HARASS_AFFINITY_PENALTY",
    "TOOL_HARASS_THRESHOLD",
    "AffinityGates",
    "AffinityState",
    "CompositeResult",
    "Effect",
    "EffectEngine",
    "EffectType",
    "EmotionEngine",
    "EmotionEvent",
    "FatigueState",
    "GlobalMood",
    "InteractionEvent",
    "InteractionMode",
    "InteractionOutcome",
    "InteractionQuality",
    "MoodState",
    "PersonaSnapshot",
    "PersonaState",
    "UserRelation",
    "affinity_mood_weight",
    "apply_emotion_events",
    "apply_to_global_mood",
    "apply_to_user_affinity",
    "blacklist_add",
    "blacklist_remove",
    "can_generate_image",
    "can_use_tools",
    "can_use_vlm",
    "check_daily_image_limit",
    "check_daily_tools_limit",
    "check_daily_vlm_limit",
    "check_tool_cooldown",
    "compute_composite",
    "get_blacklist",
    "get_fatigue",
    "get_fatigue_prompt",
    "get_global_mood",
    "get_user_relation",
    "record_image_generation",
    "record_tool_use",
    "record_tools_usage",
    "record_vlm_usage",
    "reset_user_relation",
    "save_fatigue",
    "save_global_mood",
    "save_user_relation",
    "set_user_nickname",
    "snapshot_to_debug",
    "snapshot_to_prompt",
    "tick_fatigue",
    "validate_nickname",
]
