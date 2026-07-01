"""社会性生存守卫 — SocialGuard: 感知群聊社会压力, 决定是否发言/使用工具。

设计:
  - 混合分类: 正则预筛选 (InputClassifier, 零 LLM) + LLM 精细分类 (Gate input_nature)
  - 安全方向取并集: 任一方判 hostile/sexualized/provoking/divide → 取更危险
  - 仅双方都判善意 → LLM 胜出
  - fail-open: 异常时始终允许回复 (社会压力判断是优化, 不是硬线)

决策矩阵 (InputNature → 行为):
  NOISE            → 不拦截 (Gate 另有 relevance 判断)
  SINCERE_CHAT     → 全放行
  PLAYFUL_BANTER   → 全放行
  GENUINE_HELP     → 全放行, 注入帮助提示
  PROVOKING        → 允许回复, 不压制工具, 注入冷静提示
  DIVIDE_CONQUER   → 允许回复, 压制高风险工具, 注入拒绝参与提示
  HOSTILE           → 允许回复 (冷却/安全线另有处理), 压制全部工具
  SEXUALIZED        → 硬拦截 (不应回复), 压制全部工具
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from astrbot.api import logger

from .input_classifier import InputClassifier, InputNature
from .types import InputClassification


class SocialStance(str, Enum):
    """社会立场 — bot 在当前群聊压力下的姿态。"""

    ENGAGED = "engaged"        # 正常参与
    CAUTIOUS = "cautious"      # 谨慎 (说话但收敛)
    MINIMAL = "minimal"        # 最小存在 (仅必要时发言)
    SILENT = "silent"          # 完全静默


class PressureLevel(str, Enum):
    """社会压力等级。"""

    NONE = "none"              # 无压力
    LOW = "low"                # 轻微压力
    MODERATE = "moderate"      # 中等压力
    HIGH = "high"              # 高压力
    EXTREME = "extreme"        # 极端压力


@dataclass
class SocialDecision:
    """社会守卫单次评估结果。"""

    should_reply: bool = True
    skip_reason: str = ""
    suppress_tools: bool = False
    stance: SocialStance = SocialStance.ENGAGED
    pressure_level: PressureLevel = PressureLevel.NONE
    input_nature: InputNature = InputNature.SINCERE_CHAT
    persona_injection: str | None = None
    # 供下游读取的额外上下文
    suspected_social_play: str | None = None


# ── 决策映射: InputNature → (should_reply, suppress_tools, stance, pressure) ──

# 硬拦截: should_reply=False (bot 完全静默)
_SILENCE_NATURES: frozenset[InputNature] = frozenset({
    InputNature.SEXUALIZED,
})

# 工具压制: suppress_tools=True (清空 Gate suggested_tools)
_TOOL_SUPPRESS_NATURES: frozenset[InputNature] = frozenset({
    InputNature.SEXUALIZED,
    InputNature.HOSTILE,
    InputNature.DIVIDE_CONQUER,
})

# 立场映射
_STANCE_MAP: dict[InputNature, SocialStance] = {
    InputNature.NOISE: SocialStance.ENGAGED,
    InputNature.SINCERE_CHAT: SocialStance.ENGAGED,
    InputNature.PLAYFUL_BANTER: SocialStance.ENGAGED,
    InputNature.GENUINE_HELP: SocialStance.ENGAGED,
    InputNature.PROVOKING: SocialStance.CAUTIOUS,
    InputNature.DIVIDE_CONQUER: SocialStance.CAUTIOUS,
    InputNature.HOSTILE: SocialStance.MINIMAL,
    InputNature.SEXUALIZED: SocialStance.SILENT,
}

# 压力映射
_PRESSURE_MAP: dict[InputNature, PressureLevel] = {
    InputNature.NOISE: PressureLevel.NONE,
    InputNature.SINCERE_CHAT: PressureLevel.NONE,
    InputNature.PLAYFUL_BANTER: PressureLevel.NONE,
    InputNature.GENUINE_HELP: PressureLevel.NONE,
    InputNature.PROVOKING: PressureLevel.LOW,
    InputNature.DIVIDE_CONQUER: PressureLevel.MODERATE,
    InputNature.HOSTILE: PressureLevel.HIGH,
    InputNature.SEXUALIZED: PressureLevel.EXTREME,
}

# ── Persona 注入模板 (追加到 system prompt 末尾) ──
_PERSONA_INJECTIONS: dict[InputNature, str] = {
    InputNature.PROVOKING: (
        "[社会感知] 对方正在试探或戏弄你。保持冷静与威严，"
        "用一句话带过即可——不要上当，不要情绪化反击。"
    ),
    InputNature.DIVIDE_CONQUER: (
        "[社会感知] 对方可能在挑拨你和同伴的关系。"
        "不要参与比较、不要贬低同伴、不要接受暧昧引导。"
        "用一句话转移话题或礼貌拒绝。"
    ),
    InputNature.HOSTILE: (
        "[社会感知] 对方有明显的敌意或攻击性。"
        "不要激化冲突——简短回应（不超过 30 字），语气平淡但坚定。"
        "不道歉、不解释、不妥协。"
    ),
    InputNature.GENUINE_HELP: (
        "[社会感知] 对方可能在真诚求助。如果你知道答案，请耐心详细地回答。"
        "如果不知道，诚实说不知道并给出可操作的建议方向。"
    ),
}


class SocialGuard:
    """社会性生存守卫 — 感知群聊压力, 调控发言行为。

    核心逻辑:
      1. InputClassifier 正则预筛选 (零 LLM 成本)
      2. 合并 Gate LLM 的 input_nature (安全方向取并集)
      3. 自回复速率 → 压力加成 (高频自说自话 → 收敛)
      4. 输出 SocialDecision: 是否回复 / 是否压制工具 / 立场 / 压力等级
    """

    # 自回复速率阈值: 60s 内 N 次回复触发压力
    _SELF_REPLY_WINDOW_S: float = 60.0
    _SELF_REPLY_LOW_THRESHOLD: int = 3     # ≥3 → LOW
    _SELF_REPLY_MODERATE_THRESHOLD: int = 5  # ≥5 → MODERATE
    _SELF_REPLY_HIGH_THRESHOLD: int = 8      # ≥8 → HIGH

    def __init__(
        self,
        bot_id: str = "",
        bot_name: str = "",
        peer_bot_name: str = "",
        peer_bot_qq: str = "",
    ) -> None:
        self._bot_id = bot_id
        self._bot_name = bot_name
        self._peer_bot_name = peer_bot_name
        self._peer_bot_qq = peer_bot_qq
        # 正则分类器 (单例, 编译好的正则)
        self._classifier = InputClassifier()
        # 每个群的 self-reply 时间戳队列 (用于自回复速率建模)
        self._reply_timestamps: dict[str, list[float]] = {}

    # ═══════════════════════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════════════════════

    def evaluate(
        self,
        *,
        group_id: str,
        user_id: str,
        content: str,
        is_addressed_to_me: bool = False,
        is_at_mention: bool = False,
        thread_id: str = "",
        llm_input_nature: str = "",
    ) -> SocialDecision:
        """评估当前消息的社会压力, 返回发言决策。

        Args:
            group_id: 群 ID
            user_id: 触发用户 ID
            content: 消息文本
            is_addressed_to_me: 消息是否定向给 bot (来自 Gate S1)
            is_at_mention: 是否有 @提及
            thread_id: 对话线程 ID
            llm_input_nature: Gate LLM 的 input_nature 分类结果 (字符串)

        Returns:
            SocialDecision: 发言决策
        """
        try:
            return self._evaluate_impl(
                group_id=group_id,
                user_id=user_id,
                content=content,
                is_addressed_to_me=is_addressed_to_me,
                is_at_mention=is_at_mention,
                llm_input_nature=llm_input_nature,
            )
        except Exception:
            logger.debug(
                "群 %s: SocialGuard.evaluate 异常, fail-open",
                group_id, exc_info=True,
            )
            return SocialDecision(
                should_reply=True,
                suppress_tools=False,
                stance=SocialStance.ENGAGED,
                pressure_level=PressureLevel.NONE,
                input_nature=InputNature.SINCERE_CHAT,
            )

    def _evaluate_impl(
        self,
        *,
        group_id: str,
        user_id: str,
        content: str,
        is_addressed_to_me: bool,
        is_at_mention: bool,
        llm_input_nature: str,
    ) -> SocialDecision:
        """evaluate() 的实现 — 异常由外层 catch。"""

        # ── 步骤 1: 正则预筛选 (零 LLM 成本) ──
        prescreen = self._classifier.prescreen(
            content,
            is_addressed_to_me=is_addressed_to_me if is_addressed_to_me else None,
            peer_bot_name=self._peer_bot_name,
        )

        # ── 步骤 2: 合并 Gate LLM 结果 (如果可用) ──
        if llm_input_nature and llm_input_nature.strip():
            classification = self._classifier.classify_with_llm_result(
                content,
                llm_nature=llm_input_nature.strip(),
                is_addressed_to_me=is_addressed_to_me,
            )
        else:
            # 仅有正则结果 — 安全硬线 (SEXUALIZED/HOSTILE ≥0.8) 已由 prescreen 短路
            classification = prescreen

        final_nature = classification.nature

        # ── 步骤 3: 自回复速率 → 压力修正 ──
        _base_pressure = _PRESSURE_MAP.get(final_nature, PressureLevel.NONE)
        _self_reply_pressure = self._calc_self_reply_pressure(group_id)
        _effective_pressure = self._max_pressure(_base_pressure, _self_reply_pressure)

        # ── 步骤 4: 自回复压力可升级 stance ──
        _base_stance = _STANCE_MAP.get(final_nature, SocialStance.ENGAGED)
        _effective_stance = self._upgrade_stance_for_pressure(
            _base_stance, _effective_pressure,
        )

        # ── 步骤 5: 组装决策 ──
        should_reply = final_nature not in _SILENCE_NATURES
        suppress_tools = final_nature in _TOOL_SUPPRESS_NATURES
        skip_reason = ""
        if not should_reply:
            skip_reason = f"SocialGuard 硬拦截: input_nature={final_nature.value}"
        elif suppress_tools:
            skip_reason = f"SocialGuard 工具压制: input_nature={final_nature.value}"

        # ── 步骤 6: Persona 注入 (安全分类时追加方向指令) ──
        persona_injection = _PERSONA_INJECTIONS.get(final_nature)

        # ── 步骤 7: 社交把戏检测 ──
        suspected_social_play = None
        if final_nature == InputNature.DIVIDE_CONQUER:
            suspected_social_play = "divide_and_conquer"
        elif final_nature == InputNature.PROVOKING and not is_addressed_to_me:
            suspected_social_play = "delegate_chore"

        return SocialDecision(
            should_reply=should_reply,
            skip_reason=skip_reason,
            suppress_tools=suppress_tools,
            stance=_effective_stance,
            pressure_level=_effective_pressure,
            input_nature=final_nature,
            persona_injection=persona_injection,
            suspected_social_play=suspected_social_play,
        )

    # ═══════════════════════════════════════════════════════════
    # 自回复速率跟踪
    # ═══════════════════════════════════════════════════════════

    def feed_my_reply(self, group_id: str, *, thread_id: str = "") -> None:
        """记录 bot 自己的一次回复 — 用于自回复速率跟踪。

        高频自回复 → 提升压力等级, 触发选择性静默。
        """
        now = time.monotonic()
        ts_list = self._reply_timestamps.get(group_id)
        if ts_list is None:
            ts_list = []
            self._reply_timestamps[group_id] = ts_list
        ts_list.append(now)
        # 清理过期时间戳
        cutoff = now - self._SELF_REPLY_WINDOW_S
        while ts_list and ts_list[0] < cutoff:
            ts_list.pop(0)

    def _calc_self_reply_pressure(self, group_id: str) -> PressureLevel:
        """计算自回复速率产生的压力等级。"""
        ts_list = self._reply_timestamps.get(group_id)
        if not ts_list:
            return PressureLevel.NONE
        # 清理过期
        cutoff = time.monotonic() - self._SELF_REPLY_WINDOW_S
        while ts_list and ts_list[0] < cutoff:
            ts_list.pop(0)
        count = len(ts_list)
        if count >= self._SELF_REPLY_HIGH_THRESHOLD:
            return PressureLevel.HIGH
        if count >= self._SELF_REPLY_MODERATE_THRESHOLD:
            return PressureLevel.MODERATE
        if count >= self._SELF_REPLY_LOW_THRESHOLD:
            return PressureLevel.LOW
        return PressureLevel.NONE

    # ═══════════════════════════════════════════════════════════
    # 辅助
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _max_pressure(a: PressureLevel, b: PressureLevel) -> PressureLevel:
        _order = [
            PressureLevel.NONE,
            PressureLevel.LOW,
            PressureLevel.MODERATE,
            PressureLevel.HIGH,
            PressureLevel.EXTREME,
        ]
        _scores = {p: i for i, p in enumerate(_order)}
        return a if _scores.get(a, 0) >= _scores.get(b, 0) else b

    @staticmethod
    def _upgrade_stance_for_pressure(
        stance: SocialStance,
        pressure: PressureLevel,
    ) -> SocialStance:
        """自回复压力足够高时, 升级 stance (更保守)。"""
        _stance_order = [
            SocialStance.ENGAGED,
            SocialStance.CAUTIOUS,
            SocialStance.MINIMAL,
            SocialStance.SILENT,
        ]
        _scores = {s: i for i, s in enumerate(_stance_order)}
        _base = _scores.get(stance, 0)
        # 自回复 HIGH → 升一级; EXTREME → 升两级
        if pressure == PressureLevel.HIGH:
            _base = min(_base + 1, len(_stance_order) - 1)
        elif pressure == PressureLevel.EXTREME:
            _base = min(_base + 2, len(_stance_order) - 1)
        return _stance_order[_base]


# ── 模块级单例缓存 (per bot_id) ──
_instances: dict[str, SocialGuard] = {}


def get_social_guard(
    bot_id: str = "",
    bot_name: str = "",
    peer_bot_name: str = "",
    peer_bot_qq: str = "",
) -> SocialGuard:
    """获取或创建 SocialGuard 实例 (per bot_id 单例)。

    首次调用创建实例, 后续相同 bot_id 返回缓存实例。
    bot_id 为空时始终创建新实例 (初始化占位)。
    """
    if bot_id and bot_id in _instances:
        return _instances[bot_id]

    instance = SocialGuard(
        bot_id=bot_id,
        bot_name=bot_name,
        peer_bot_name=peer_bot_name,
        peer_bot_qq=peer_bot_qq,
    )
    if bot_id:
        _instances[bot_id] = instance
    return instance
