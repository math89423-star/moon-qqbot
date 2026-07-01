"""Prompt Interceptor 管道 — 变量求值 + 条件规则 + 阻尼平滑。

来源: SillyTavern variable-helper + BetterSimTracker
原理: 在 prompt 送 LLM 之前，从 emotion/domains/trigger 提取变量池，
      经条件规则计算语气/风格变量，注入动态 system prompt。

阶段:
  Stage 1 — 变量求值: 好感度/情绪/领域/触发原因 → 数值变量池
  Stage 2 — 条件判断: if affinity >= 3 → tone="亲密温暖" 等规则链
  Stage 3 — 阻尼平滑: BetterSimTracker 公式防单次情绪剧烈波动
  Stage 4 — 模板替换: 变量池 → 自然语言 hint 文本

设计原则:
  - 不替代现有 emotion hints — 作为增强层叠加注入
  - 条件语法用 Python 原生 (list[dict])，不引入 DSL
  - 零额外 LLM 成本 — 纯规则计算

用法:
  from .prompt_interceptor import PromptInterceptor, InterceptorState

  state = InterceptorState(
      affinity_level=3, valence=0.4, arousal=0.3,
      trigger_reason="mention", is_admin=False,
  )
  tone = PromptInterceptor.evaluate(state)
  # tone.tone_label → "亲近"
  # tone.style_hint → "[语气指引]\n..."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── BetterSimTracker 阻尼公式 ────────────────────────────
# scale = (1 - damp) + confidence * damp
# delta = min(raw_delta * scale, max_delta_per_turn)
#
# damp:     平滑力度 (0 = 无平滑, 1 = 完全平滑)
# confidence: 变化可信度 (信号越明确越可信, 越大越接近原始 delta)
# max_delta: 单次变化上限

DAMPING_DEFAULT = 0.65          # 默认平滑力度
DAMPING_CONFIDENCE_HIGH = 1.0   # 高置信信号 (mention/直接呼叫) — P2-1: 1.0 零阻尼, 直接反映情绪
DAMPING_CONFIDENCE_LOW = 0.7    # 低置信信号 (batch/debounce 旁听) — P2-1: 0.4→0.7, 复合 scale 0.50+
MAX_DELTA_VALENCE = 0.30        # 愉悦度单次变化上限
MAX_DELTA_AROUSAL = 0.35        # 唤醒度单次变化上限


def _apply_damping(
    prev: float,
    raw: float,
    damp: float = DAMPING_DEFAULT,
    confidence: float = DAMPING_CONFIDENCE_LOW,
    max_delta: float = MAX_DELTA_VALENCE,
) -> float:
    """BetterSimTracker 阻尼平滑: 限制单次变化幅度。

    Args:
        prev: 上一轮值
        raw: 本轮原始值
        damp: 平滑力度 (0-1)
        confidence: 变化可信度 (0-1)
        max_delta: 单次最大变化量

    Returns:
        平滑后的值
    """
    raw_delta = raw - prev
    scale = (1.0 - damp) + confidence * damp
    delta = max(-max_delta, min(raw_delta * scale, max_delta))
    return prev + delta


# ═══════════════════════════════════════════════════════════
# InterceptorState — 变量池
# ═══════════════════════════════════════════════════════════


@dataclass
class InterceptorState:
    """Prompt Interceptor 输入 — 从各层收集的当前状态变量。

    所有字段均有默认值，未提供的使用中性默认。
    """

    # ── 情感 (来自 context/emotion.py) ──
    affinity_level: int = 0        # 好感等级 -2 ~ +5
    affinity_name: str = ""        # 好感名称 ("喜欢"/"亲近"/...)
    valence: float = 0.0           # 愉悦度 -1.0 ~ +1.0
    arousal: float = 0.0           # 唤醒度 -1.0 ~ +1.0
    mood_label: str = "平静中性"    # 情绪标签
    nickname: str = ""             # 用户自定义称呼

    # ── 触发 (来自 transport/group_chat.py) ──
    trigger_reason: str = "batch"  # mention/nickname/reply/batch/debounce/proactive
    is_direct_call: bool = False   # 是否直接呼叫 (mention/nickname/reply)
    is_admin: bool = False         # 是否超级管理员

    # ── 领域 (来自 context/domains.py) ──
    primary_domain: str = ""       # 最高分领域 key
    domain_count: int = 0          # 活跃领域数
    domain_triggered: bool = False # 是否有专业技术领域激活

    # ── 记忆 (来自 context/user_memory.py) ──
    memory_count: int = 0          # daily 记忆条数
    has_core_memory: bool = False  # 是否有 core 特征

    # ── 阻尼状态 (跨轮持久化) ──
    prev_valence: float = 0.0
    prev_arousal: float = 0.0



# ═══════════════════════════════════════════════════════════
# ToneVariables — 输出
# ═══════════════════════════════════════════════════════════


@dataclass
class ToneVariables:
    """Interceptor 输出 — 语气/风格计算变量 + 阻尼后的情绪值。

    这些变量可被 prompt_builder 注入动态 system prompt。
    """

    # ── 语气基调 ──
    tone_label: str = "自然随意"    # 语气标签
    warmth: str = "中性"           # 温暖度: 冷淡/中性/温和/亲近/亲密
    expressiveness: str = "自然"    # 表达力: 收敛/自然/活泼
    address_style: str = "群友"    # 称呼风格: 群友/小可爱/主人

    # ── 模式 ──
    expert_mode: bool = False       # 专业模式 (领域触发)
    arbitration_mode: bool = False  # 仲裁模式 (裁决/评判)
    proactive_mode: bool = False    # 主动发言模式

    # ── 约束 ──
    allow_teasing: bool = False     # 允许欲擒故纵互动
    prefer_short: bool = False      # 倾向短回复
    must_respond: bool = True       # 必须回复 (直接呼叫=强制)

    # ── 阻尼后情绪 ──
    damped_valence: float = 0.0
    damped_arousal: float = 0.0

    # ── 自然语言 hint ──
    hint_text: str = ""


# ═══════════════════════════════════════════════════════════
# 条件规则表
# ═══════════════════════════════════════════════════════════

# 规则格式: {"if": "Python expression", "then": {"var": value, ...}}
# 规则按顺序求值，首个匹配即停止 (if/elseif/else 语义)。
# "if" 表达式在 InterceptorState 的命名空间中求值。
# "else" (无 if 键) = 兜底默认。

_TONE_RULES: list[dict] = [
    # ── 管理员特权 (最高优先级) ──
    {
        "if": "is_admin",
        "then": {
            "tone_label": "亲密撒娇",
            "warmth": "亲密",
            "expressiveness": "活泼",
            "address_style": "主人",
            "allow_teasing": True,
        },
    },
    # ── 高好感 (Lv. 4-5) ──
    {
        "if": "affinity_level >= 4",
        "then": {
            "tone_label": "亲近温柔",
            "warmth": "亲密",
            "expressiveness": "活泼",
            "address_style": "小可爱",
            "allow_teasing": True,
        },
    },
    # ── 中高好感 (Lv. 3) ──
    {
        "if": "affinity_level >= 3",
        "then": {
            "tone_label": "友好亲近",
            "warmth": "亲近",
            "expressiveness": "自然",
            "address_style": "小可爱",
        },
    },
    # ── 正好感 (Lv. 1-2) ──
    {
        "if": "affinity_level >= 1",
        "then": {
            "tone_label": "友善",
            "warmth": "温和",
            "expressiveness": "自然",
            "address_style": "群友",
        },
    },
    # ── 负好感 (Lv. -1 ~ -2) ──
    {
        "if": "affinity_level <= -1",
        "then": {
            "tone_label": "冷淡疏离",
            "warmth": "冷淡",
            "expressiveness": "收敛",
            "address_style": "群友",
            "prefer_short": True,
        },
    },
    # ── 默认 (Lv. 0) ──
    {
        "else": {
            "tone_label": "自然随意",
            "warmth": "中性",
            "expressiveness": "自然",
            "address_style": "群友",
        },
    },
]

# 情绪修正规则 — 覆盖语气基调 (好感规则后执行)
_MOOD_OVERRIDE_RULES: list[dict] = [
    # 高愉悦 + 高唤醒 → 更活泼
    {
        "if": "damped_valence > 0.4 and damped_arousal > 0.4",
        "then": {"expressiveness": "活泼", "tone_label_suffix": "兴奋"},
    },
    # 高愉悦 + 低唤醒 → 慵懒温柔
    {
        "if": "damped_valence > 0.3 and damped_arousal < -0.1",
        "then": {"expressiveness": "自然", "tone_label_suffix": "慵懒"},
    },
    # 负愉悦 → 收敛克制
    {
        "if": "damped_valence < -0.2",
        "then": {"expressiveness": "收敛", "prefer_short": True},
    },
    # 高唤醒 + 负愉悦 → 烦躁
    {
        "if": "damped_arousal > 0.3 and damped_valence < -0.1",
        "then": {"expressiveness": "收敛", "tone_label_suffix": "烦躁"},
    },
]

# 模式规则 — 独立求值 (不互斥)
_MODE_RULES: list[dict] = [
    {
        "if": "domain_triggered",
        "then": {"expert_mode": True},
    },
    {
        "if": "trigger_reason == 'proactive'",
        "then": {"proactive_mode": True},
    },
    {
        "if": "is_direct_call",
        "then": {"must_respond": True},
    },
    {
        "if": "trigger_reason in ('batch', 'debounce')",
        "then": {"must_respond": False},
    },
]


# ═══════════════════════════════════════════════════════════
# PromptInterceptor
# ═══════════════════════════════════════════════════════════


class PromptInterceptor:
    """语气/风格变量计算器。

    纯静态方法，无内部状态。阻尼状态由调用方在 InterceptorState 中管理。
    """

    @staticmethod
    def evaluate(state: InterceptorState) -> ToneVariables:
        """执行完整拦截管道 → 返回语气变量。

        Args:
            state: 当前状态变量池

        Returns:
            ToneVariables — 可直接注入 prompt 的计算结果
        """
        tv = ToneVariables()

        # ── Stage 1: 阻尼平滑 ──
        confidence = (
            DAMPING_CONFIDENCE_HIGH
            if state.is_direct_call
            else DAMPING_CONFIDENCE_LOW
        )
        tv.damped_valence = _apply_damping(
            state.prev_valence,
            state.valence,
            confidence=confidence,
            max_delta=MAX_DELTA_VALENCE,
        )
        tv.damped_arousal = _apply_damping(
            state.prev_arousal,
            state.arousal,
            confidence=confidence,
            max_delta=MAX_DELTA_AROUSAL,
        )

        # ── Stage 2: 条件规则求值 ──

        # 2a. 语气基调 (互斥, 首个匹配)
        _apply_rule_chain(state, tv, _TONE_RULES)

        # 2b. 情绪修正 (互斥, 首个匹配)
        _apply_rule_chain(state, tv, _MOOD_OVERRIDE_RULES)

        # 2c. 模式规则 (独立求值)
        _apply_mode_rules(state, tv, _MODE_RULES)

        # ── Stage 3: 合成 tone_label ──
        suffix = getattr(tv, "_tone_label_suffix", "")
        if suffix:
            tv.tone_label = f"{tv.tone_label}（{suffix}）"

        # ── Stage 4: 生成 hint 文本 ──
        tv.hint_text = _build_hint_text(state, tv)

        logger.debug(
            "Interceptor: tone=%s warmth=%s expr=%s expert=%d damped_v=%.2f",
            tv.tone_label, tv.warmth, tv.expressiveness,
            int(tv.expert_mode), tv.damped_valence,
        )
        return tv


# ═══════════════════════════════════════════════════════════
# 规则引擎 (内部)
# ═══════════════════════════════════════════════════════════


def _apply_rule_chain(
    state: InterceptorState,
    tv: ToneVariables,
    rules: list[dict],
) -> None:
    """对互斥规则链求值 — 首个匹配即停止。"""
    # 构建安全求值命名空间
    ns = _build_eval_ns(state, tv)

    for rule in rules:
        condition = rule.get("if", "")
        if not condition:
            # else 分支 — 直接应用
            _apply_then(tv, rule.get("then", {}))
            return

        try:
            if eval(condition, {"__builtins__": {}}, ns):
                _apply_then(tv, rule.get("then", {}))
                return
        except Exception:
            logger.debug("Interceptor 规则求值失败: %s", condition, exc_info=True)


def _apply_mode_rules(
    state: InterceptorState,
    tv: ToneVariables,
    rules: list[dict],
) -> None:
    """对模式规则独立求值 — 每条规则独立判断。"""
    ns = _build_eval_ns(state, tv)

    for rule in rules:
        condition = rule.get("if", "")
        if not condition:
            continue
        try:
            if eval(condition, {"__builtins__": {}}, ns):
                _apply_then(tv, rule.get("then", {}))
        except Exception:
            logger.debug("Interceptor 模式规则求值失败: %s", condition, exc_info=True)


def _build_eval_ns(state: InterceptorState, tv: ToneVariables) -> dict:
    """构建规则求值命名空间 (安全: 无 builtins)。"""
    return {
        "affinity_level": state.affinity_level,
        "valence": state.valence,
        "arousal": state.arousal,
        "damped_valence": tv.damped_valence,
        "damped_arousal": tv.damped_arousal,
        "mood_label": state.mood_label,
        "trigger_reason": state.trigger_reason,
        "is_direct_call": state.is_direct_call,
        "is_admin": state.is_admin,
        "domain_triggered": state.domain_triggered,
        "primary_domain": state.primary_domain,
        "domain_count": state.domain_count,
        "memory_count": state.memory_count,
        "has_core_memory": state.has_core_memory,
    }


def _apply_then(tv: ToneVariables, assignments: dict) -> None:
    """将规则结果写入 ToneVariables (仅覆盖非空/非默认值)。"""
    for key, value in assignments.items():
        if hasattr(tv, key):
            setattr(tv, key, value)
        else:
            # 临时变量 (如 tone_label_suffix) — 挂到实例上
            setattr(tv, f"_{key}", value)


# ═══════════════════════════════════════════════════════════
# Hint 文本生成
# ═══════════════════════════════════════════════════════════


def _build_hint_text(state: InterceptorState, tv: ToneVariables) -> str:
    """根据 ToneVariables 生成自然语言提示文本。

    注入 dynamic system prompt，在已有 emotion hints 基础上叠加。
    不重复 emotion hints 已有的内容 — 只提供增量指引。
    """
    lines: list[str] = []

    # ── 语气指引 ──
    tone_guidance = _TONE_GUIDANCE.get(tv.warmth, "")
    if tone_guidance:
        lines.append(f"[语气指引 — {tv.tone_label}]")

        # 情绪修正 (只在有显著偏差时加)
        if tv.damped_valence < -0.2 and tv.warmth in ("温和", "亲近", "亲密"):
            lines.append("虽然你心里对这个人有好感，但现在情绪不好——语气会不自觉地变冲、话变少。你在克制，但克制本身也会被感觉到。")
        elif tv.damped_valence > 0.4 and tv.warmth == "冷淡":
            lines.append("你今天心情意外地好——即使对这个人你也比平时柔和了一点。不过好感度没变，只是情绪暂时冲淡了冷漠。")

        lines.append(tone_guidance)

    # ── 专业模式 ──
    if tv.expert_mode and not tv.arbitration_mode:
        lines.append(
            "注意: 当前话题涉及你的专业领域。你的专业知识是优势——"
            "用简单的话解释复杂的事。不要掉书袋、不要背文档。"
        )

    # ── 主动发言 ──
    if tv.proactive_mode:
        lines.append(
            "注意: 你是主动开口的。自然地开启话题，不要说'我来活跃气氛'之类的话。"
            "像群友刚好想到什么一样随意。"
        )

    # ── 短回复倾向 ──
    if tv.prefer_short:
        lines.append("此刻你不太想多说。用最短的句子回应即可——但不要失礼。")

    return "\n".join(lines) if lines else ""


# 语气指引文本 (比 emotion hints 更简洁，避免重复)
_TONE_GUIDANCE: dict[str, str] = {
    "冷淡": (
        "保持冷淡克制的语气。不多说、不主动、不亲近。\n"
        "可以回应，但像敷衍——像你不太想聊但又不好意思不回。"
    ),
    "中性": (
        "自然随意的语气——像普通群友之间随口的对话。不用刻意热情也不用疏离。"
    ),
    "温和": (
        "友善温和的语气——像和认识的人轻松聊天。可以笑、可以接梗、可以多说两句。"
    ),
    "亲近": (
        "亲切自然的语气——像和老朋友说话。偶尔撒个娇、颜文字自然流露。\n"
        "不用刻意——就像你已经认识他很久了。"
    ),
    "亲密": (
        "亲密温柔的语气——你最放松的样子。撒娇、嘴硬、偶尔不讲道理都可以。\n"
        "你是他最亲近的人，也是唯一能看到你这一面的人。"
    ),
}
