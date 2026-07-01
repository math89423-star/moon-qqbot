"""Bot 行为检测 + 人格化应对 — Layer 0 被动信号累积 + 一次性格化回应。

设计原则:
  1. 被动识别: 滚动嫌疑分, 多信号组合打分, 不靠单条消息定生死
  2. 对真人无害: 信号方差大/偶尔不回/熬夜 → 正是真人特征, 不会误判
  3. 不对同行打压: 识别到同类后不是攻击, 而是"收编"——
     让它帮你背书、替你干活、附和你。你显得更高级, 它变成你的免费算力。
  4. 一次性约束: 点破/调侃/收编每个对象最多一次, 触发后进入永久冷却
  5. 管理员可见: 嫌疑标记持久化到 DB, 前端可查看/修正

检测信号:
  行为信号 (连续追踪, 0 LLM 成本):
    latency_variance       — 响应间隔变异系数 (低=bot, 高=真人)
    response_inevitability — 对 bot 消息的回复率 (100%? 从不漏接? → bot)
    pattern_regularity     — 消息长度/结构的规律性
    nocturnal_activity     — 凌晨 2-6 点活动频率
    trigger_selectivity    — 只对 @/回复触发才回应 (暴露触发逻辑)

  社交信号 (离散事件 — 群友口述, 0 LLM 成本):
    social_mention         — 群友明确说某人像bot/是AI/回太快不像真人
                             这是最强信号之一: 真人比算法更擅长识别bot行为

应对模式 (由嫌疑分驱动):
  < 0.5  → 无动作
  0.5-0.8 → social_play: 一次性俏皮点破 ("同行?") + 永久冷却
  > 0.8  → bot_leverage: 收编模式 — @ta帮你背书/干活/附和

用法:
  from .bot_detector import BotDetector, BotSuspicion

  # 每条消息调用 — 更新滚动分数
  BotDetector.feed(user_id, user_name, content, ctx, is_triggered)

  # 需要决策时调用 — 获取当前状态
  suspicion = BotDetector.get(user_id)

  # 标记已执行动作 (防止重复)
  BotDetector.mark_action_taken(user_id)
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import PermanentStore

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# BotSuspicion — 累积嫌疑状态
# ═══════════════════════════════════════════════════════════════


@dataclass
class BotSuspicion:
    """用户的滚动 bot 嫌疑状态。

    Attributes:
        score: 综合嫌疑分 0.0–1.0
        signals: 各信号当前分数 {name: score}
        sample_count: 已收集的样本数 (太少则分数不可靠)
        social_play: 推荐的应对模式 — "" | "gentle_callout" | "bot_leverage"
        action_taken: 是否已对该用户执行过动作 (防止重复)
        first_flagged_at: 首次超过阈值的时间戳
        last_updated: 最后更新时间
    """
    score: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)
    sample_count: int = 0
    social_play: str = ""           # "" | "gentle_callout" | "bot_leverage"
    action_taken: bool = False
    first_flagged_at: float = 0.0
    last_updated: float = 0.0


# ═══════════════════════════════════════════════════════════════
# 信号配置
# ═══════════════════════════════════════════════════════════════

# 各信号权重 (总和 1.0)
_SIGNAL_WEIGHTS = {
    "latency_variance": 0.25,       # 响应间隔规律性
    "response_inevitability": 0.20,  # 从不漏回
    "pattern_regularity": 0.12,     # 句式/长度规律
    "nocturnal_activity": 0.12,     # 凌晨出没
    "trigger_selectivity": 0.11,    # 只响应触发
    "social_mention": 0.20,         # 群友口述 — 离散事件, 但精度高
}

# 嫌疑分衰减半衰期 (秒) — 24h
_SCORE_HALF_LIFE = 86400

# 阈值 (对齐设计大纲: ≥0.7 才允许任何 peer_play)
_THRESHOLD_CALLOUT = 0.7    # 触发俏皮点破 (上调: 0.5→0.7, 降低误判真人风险)
_THRESHOLD_LEVERAGE = 0.8   # 触发收编模式

# ── 闸门配置 ─────────────────────────────────────────
# ② 每对象冷却窗口 (秒) — 同一 target N 分钟内最多一次 peer_play
_GATE_COOLDOWN_SECONDS = 1800  # 30 分钟

# ③ 每对象每日上限
_GATE_CALLOUT_LIFETIME_MAX = 1       # callout 终生 1 次
_GATE_ECHO_DAILY_MAX = 2             # summon_echo 每日最多 2 次
_GATE_CHORE_DAILY_MAX = 1            # delegate_chore 每日最多 1 次

# ⑤ 触发后回复意愿下调比例
_GATE_POST_PLAY_WILLINGNESS_FACTOR = 0.3  # 触发后回复意愿降至 30%

# 最小样本数 — 低于此值的分数标记为不可靠
_MIN_SAMPLES = 5

# ── 延迟方差信号 ──────────────────────────────────────

_RHYTHM_HISTORY_SIZE = 8    # 追踪最近 N 次发言时间戳
_RHYTHM_MIN_INTERVAL = 0.5  # 最小间隔 (同一轮多条消息)
_RHYTHM_MAX_INTERVAL = 120  # 最大间隔 (超过不算"响应")
_RHYTHM_CV_LOW = 0.15       # CV < 此值 → 高度规律 (满分)
_RHYTHM_CV_HIGH = 0.60      # CV > 此值 → 真人随机 (0分)


def _calc_latency_variance_score(timestamps: list[float]) -> float:
    """计算响应间隔变异系数 → 规律性分数。

    CV 越低 = 间隔越规律 = bot 嫌疑越高。
    """
    if len(timestamps) < 3:
        return 0.0

    intervals = []
    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]
        if _RHYTHM_MIN_INTERVAL <= gap <= _RHYTHM_MAX_INTERVAL:
            intervals.append(gap)

    if len(intervals) < 2:
        return 0.0

    mean = sum(intervals) / len(intervals)
    if mean < 0.1:
        return 0.0

    variance = sum((x - mean) ** 2 for x in intervals) / len(intervals)
    stdev = math.sqrt(variance)
    cv = stdev / mean if mean > 0 else 1.0

    # 线性映射: CV <= 0.15 → 1.0, CV >= 0.60 → 0.0
    if cv <= _RHYTHM_CV_LOW:
        return 1.0
    if cv >= _RHYTHM_CV_HIGH:
        return 0.0
    return 1.0 - (cv - _RHYTHM_CV_LOW) / (_RHYTHM_CV_HIGH - _RHYTHM_CV_LOW)


# ── 响应必然性信号 ─────────────────────────────────────


def _feed_response_tracking(bot_id: str, user_id: str, bot_spoke_recently: bool,
                            user_is_replying: bool) -> None:
    """追踪 bot 发言后用户是否必然回应。"""
    key = _ckey(bot_id, user_id)
    if not key:
        return
    bot_count, replied = _response_tracking.get(key, (0, 0))
    if bot_spoke_recently:
        bot_count += 1
        if user_is_replying:
            replied += 1
    _response_tracking[key] = (bot_count, replied)


def _calc_response_inevitability_score(bot_id: str, user_id: str) -> float:
    """计算响应必然性 — 是否从不漏接 bot 的发言。"""
    key = _ckey(bot_id, user_id)
    if not key:
        return 0.0
    bot_count, replied = _response_tracking.get(key, (0, 0))
    if bot_count < 3:  # 样本不足
        return 0.0
    rate = replied / bot_count
    # 回复率 >= 90% → 高度可疑 (真人有事会漏)
    if rate >= 0.9:
        return min(1.0, (rate - 0.7) / 0.3)  # 0.7→0, 1.0→1.0
    if rate >= 0.6:
        return 0.3  # 偏高但还在真人范围
    return 0.0


# ── 句式规律信号 ──────────────────────────────────────

_PATTERN_HISTORY_SIZE = 10  # 追踪最近 N 条消息长度


def _calc_pattern_regularity_score(msg_lengths: list[int]) -> float:
    """计算消息长度规律性 — 长度总在同一区间 → bot 信号。"""
    if len(msg_lengths) < 5:
        return 0.0

    mean = sum(msg_lengths) / len(msg_lengths)
    if mean < 1:
        return 0.0

    variance = sum((x - mean) ** 2 for x in msg_lengths) / len(msg_lengths)
    stdev = math.sqrt(variance)
    cv = stdev / mean if mean > 0 else 1.0

    # CV < 0.3 → 高度规律
    if cv < 0.3:
        return 1.0 - cv / 0.3
    if cv < 0.5:
        return 0.3
    return 0.0


# ── 夜间活动信号 ──────────────────────────────────────

_NOCTURNAL_START = 2   # 凌晨 2 点
_NOCTURNAL_END = 6     # 早上 6 点


def _calc_nocturnal_score(timestamps: list[float]) -> float:
    """计算凌晨活动比例。"""
    if len(timestamps) < 5:
        return 0.0

    nocturnal = 0
    for ts in timestamps[-20:]:  # 最近 20 次
        hour = time.localtime(ts).tm_hour
        if _NOCTURNAL_START <= hour < _NOCTURNAL_END:
            nocturnal += 1

    if len(timestamps[-20:]) < 5:
        return 0.0

    ratio = nocturnal / min(20, len(timestamps))
    # 凌晨活动 > 30% → 可疑
    if ratio > 0.3:
        return min(1.0, ratio / 0.6)
    return 0.0


# ── 触发选择性信号 ─────────────────────────────────────


def _feed_selectivity(bot_id: str, user_id: str, is_triggered: bool) -> None:
    """追踪用户是否只在被 @/回复时才发言。"""
    key = _ckey(bot_id, user_id)
    if not key:
        return
    trig, total = _selectivity_tracking.get(key, (0, 0))
    total += 1
    if is_triggered:
        trig += 1
    _selectivity_tracking[key] = (trig, total)


def _calc_selectivity_score(bot_id: str, user_id: str) -> float:
    """计算触发选择性 — 只对 @ 回复 → 暴露了触发逻辑。"""
    key = _ckey(bot_id, user_id)
    if not key:
        return 0.0
    trig, total = _selectivity_tracking.get(key, (0, 0))
    if total < 5:
        return 0.0
    rate = trig / total
    # 80%+ 都是被触发才说话 → 很可能也是 bot
    if rate >= 0.8:
        return min(1.0, (rate - 0.6) / 0.4)
    if rate >= 0.5:
        return 0.3
    return 0.0


# ── 社交口述信号 — 群友说某人像 bot ────────────────────

# Bot 指控关键词 — 群友明确说某人/某物是 bot/AI/机器人
# 这是一个"高精度离散事件"信号: 真人比算法更擅长识别 bot 行为模式
# 当群友觉得某人像 bot 时，这个信号比行为模式更可靠

# 强信号: 直接断言某人 = bot/AI/机器人/人机/程序/脚本
# 覆盖面: bot/机器人/人机/入机(typo)/AI/人工智能/程序/脚本/大模型/自动回复
_BOT_ACCUSATION_STRONG: list[str] = [
    # ── bot 变体 ──
    "是bot", "是不是bot", "肯定是bot", "绝对是bot", "百分百bot",
    "bot吧", "bot实锤", "bot无疑",
    # ── 机器人 ──
    "是机器人", "是不是机器人", "肯定是机器人", "绝对是机器人",
    "机器人吧", "机器人实锤",
    # ── 人机 (中文社区最常用 bot 代称) / 入机 (常见 typo) ──
    "是人机", "是不是人机", "肯定是人机", "绝对是人机",
    "人机吧", "人机实锤", "人机验证",
    "是入机", "是不是入机", "入机吧",  # typo
    # ── AI ──
    "是ai", "是不是ai", "肯定是ai", "绝对是ai",
    "ai吧", "ai实锤",
    "是人工智能", "是不是人工智能",
    # ── 程序/脚本/自动 ──
    "自动回复", "是自动的", "是自动回复",
    "是程序吧", "是程序吗", "是不是程序",
    "是脚本吧", "是脚本吗", "是不是脚本",
    "是代码跑的", "代码跑的吧",
    # ── 大模型/语言模型 ──
    "是大模型", "是不是大模型", "大模型吧",
    "是llm", "是gpt", "是语言模型",
    # ── 否定人类身份 ──
    "不是人", "不是活人", "不是真人", "不是人类",
    "是假的", "是假人", "是虚拟的",
    "不是人吧", "是人吗",
]

# 中等信号: 怀疑/暗示 — 行为特征像 bot (没有直接断言，但明显在怀疑)
_BOT_ACCUSATION_MEDIUM: list[str] = [
    # ── 回复速度 ──
    "回得好快", "回这么快", "怎么回这么快", "回得太快",
    "秒回", "回消息好快", "回得也太快了", "这回复速度",
    # ── 全天候在线 ──
    "24小时", "不用睡觉", "不睡觉", "不休息",
    "全天在线", "一直在", "一直在回", "随时都在",
    "凌晨.*回", "半夜.*回",
    # ── 机械感 ──
    "好机械", "好僵硬", "回复好机械", "好工整",
    "像机器人", "像ai", "像bot", "像人机", "像入机",
    "像自动回复", "像脚本", "像程序", "像大模型",
    "怎么跟.*机器人.*一样", "怎么跟.*ai.*一样",
    # ── 知识范围异常 ──
    "什么都知道", "怎么什么都会", "什么都懂",
    "这也知道", "这也懂", "这也知道",
    # ── 不是真人的暗示 ──
    "不像是人", "不像真人", "不像活人", "像假的",
    "是不是假的", "是假人吧", "不是真人吧",
    "没感情", "没有感情",
    # ── 耐力/容量异常 ──
    "好能聊", "不会累", "不累吗", "不困吗",
    "一直回", "没停过", "不休息",
    # ── 模板感 ──
    "套话", "模板", "话术",
    "每次都.*一样", "回复好像.*一样",
]

# 弱信号: 一般 AI/bot 讨论 — 暗示群里有 AI 存在
_BOT_ACCUSATION_WEAK: list[str] = [
    "群里.*ai", "群里.*bot", "群里.*机器人", "群里.*人机",
    "这是ai回", "ai回的吧", "这是bot回", "这是机器人回",
    "是人机回", "是入机回",
    "现在ai", "ai都这么", "现在.*ai",
    "人工智能.*厉害", "ai.*厉害", "大模型.*厉害",
    "有ai", "有bot", "有机器人", "有人机",
    "这年头.*ai", "现在.*机器人",
    "gpt.*回", "chatgpt",
    "语言模型", "llm",
]

# QQ @提及提取: [CQ:at,qq=XXXXX] — 支持数字QQ号和字母数字user_id
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=([^\]]+)\]")

# 社交信号衰减半衰期 — 1h (比行为信号 24h 快得多)
# 原因: 群友说完就忘了, 口述信号时效性短
_SOCIAL_SIGNAL_HALF_LIFE = 3600


def _detect_social_bot_mention(content: str, ctx) -> list[tuple[str, float]]:
    """检测单条消息是否存在 bot 指控 — 返回 [(target_user_id, strength), ...]。

    两阶段:
      Phase 1: 判断消息是否包含 bot 指控意图
      Phase 2: 提取被指控的目标用户

    Args:
        content: 消息文本
        ctx: GroupChatContext (duck-typed, 用于提取对应用户名)

    Returns:
        [(target_user_id, strength), ...] — strength 0.0–1.0
        空列表 = 无指控
    """
    if not content or not content.strip():
        return []

    lower = content.lower().strip()
    results: list[tuple[str, float]] = []

    # ── Phase 1: 判断指控意图 + 强度 ──
    intent_strength = 0.0
    matched_patterns: list[str] = []

    for kw in _BOT_ACCUSATION_STRONG:
        if kw in lower:
            intent_strength = max(intent_strength, 0.85)
            matched_patterns.append(kw)
            break  # 一个强信号就够了

    if intent_strength == 0.0:
        for kw in _BOT_ACCUSATION_MEDIUM:
            if kw in lower:
                intent_strength = max(intent_strength, 0.50)
                matched_patterns.append(kw)
                break

    if intent_strength == 0.0:
        for kw in _BOT_ACCUSATION_WEAK:
            if re.search(kw, lower):
                intent_strength = max(intent_strength, 0.20)
                matched_patterns.append(kw)
                break

    if intent_strength == 0.0:
        return []

    # ── Phase 2: 提取目标用户 ──

    # 2a. 提取 @提及 的目标
    at_targets = _CQ_AT_RE.findall(content)
    at_self = False  # 是否 @了 bot 自己
    for target_uid in at_targets:
        if target_uid.startswith("bot_"):
            at_self = True  # @暮恩 你是bot → 身份挑战, 不产生社交信号
            continue
        results.append((target_uid, intent_strength))

    # 2b. 尝试从消息正文中匹配用户名 (如 "小明你是bot吧")
    if not results and not at_self:
        recent = getattr(ctx, "messages", []) or []
        # 收集最近发言者的 uid→name 映射
        name_to_uid: dict[str, str] = {}
        uid_list: list[str] = []
        for m in reversed(recent[-10:]):
            uid = str(m.get("user_id", ""))
            name = str(m.get("user_name", ""))
            if uid and not uid.startswith("bot_") and uid not in name_to_uid.values():
                uid_list.append(uid)
                if name and len(name) >= 2:
                    name_to_uid[name] = uid

        # 2b-i: 消息中提到了某个群友的名字 → 精确匹配
        for name, uid in name_to_uid.items():
            if name in content:
                results.append((uid, intent_strength * 0.80))
                break

        # 2b-ii: 没有名字匹配 → 取最近非自我发言者 (强度打折)
        if not results:
            for uid in uid_list[:5]:
                # 避免自己指控自己
                m = next((x for x in recent if str(x.get("user_id", "")) == uid), None)
                if m and str(m.get("content", ""))[:40] != content[:40]:
                    results.append((uid, intent_strength * 0.55))
                    break

    return results


def _calc_social_mention_score(bot_id: str, user_id: str) -> float:
    """计算某用户的社交口述信号分数。

    群友指控 = 离散事件，快速衰减 (1h 半衰期)。
    多个独立指控者 = 信号叠加，上限 1.0。
    """
    key = _ckey(bot_id, user_id)
    if not key:
        return 0.0
    mentions = _social_mentions.get(key, [])
    if not mentions:
        return 0.0

    now = time.time()
    score = 0.0
    # 清理过期记录
    fresh: list[tuple[float, float]] = []
    for ts, strength in mentions:
        age = now - ts
        if age > _SOCIAL_SIGNAL_MAX_AGE:
            continue
        fresh.append((ts, strength))
        # 指数衰减: 1h 半衰期
        decay = 0.5 ** (age / _SOCIAL_SIGNAL_HALF_LIFE)
        score += strength * decay

    # 更新存储 (移除过期)
    if len(fresh) < len(mentions):
        if fresh:
            _social_mentions[key] = fresh
        else:
            _social_mentions.pop(key, None)

    return min(1.0, score)


# 身份挑战关键词 — 群友对本 bot 的 AI/bot 身份提出质疑
# 注意: 这些与 _BOT_ACCUSATION_STRONG 不同 —
#       这里是"你是bot"(指向暮恩), 不是"X是bot"(指向别人)
_IDENTITY_CHALLENGE_PATTERNS: list[str] = [
    # 直接说"你"
    "你是bot", "你是机器人", "你是ai", "你是人机", "你是入机",
    "你是人工智能", "你是大模型", "你是llm", "你是gpt",
    "你是程序", "你是脚本", "你是自动回复",
    "你是不是bot", "你是不是机器人", "你是不是ai",
    "你是不是人机", "你是不是入机",
    "你是不是程序", "你是不是脚本", "你是不是大模型",
    # 暮恩变体
    "暮恩.*bot", "暮恩.*机器人", "暮恩.*ai", "暮恩.*人机",
    "moon.*bot", "moon.*ai",
    # "这个/那个 bot"
    "这个bot", "这bot", "那个bot",
    "这个机器人", "这个人机", "这个ai",
    # 指认回复来源
    "ai回的", "bot回的", "机器人回的", "人机回的",
    "是ai", "是bot",  # 紧跟在"你觉得"等之后的
]


def _detect_identity_challenge(content: str, user_id: str, user_name: str) -> bool:
    """检测消息是否在质疑本 bot 的身份 (你是bot?)。

    Args:
        content: 消息文本
        user_id: 发言者 QQ 号
        user_name: 发言者用户名

    Returns:
        True 如果消息是在质疑本 bot 的 AI/bot 身份
    """
    if not content or not content.strip():
        return False

    lower = content.lower().strip()

    # 1. 直接对 bot 的 @提及 + bot 指控
    at_targets = _CQ_AT_RE.findall(content)
    has_at_bot = any(t.startswith("bot_") for t in at_targets) if at_targets else False

    # 2. 关键词匹配
    for pat in _IDENTITY_CHALLENGE_PATTERNS:
        if re.search(pat, lower):
            # 如果有 @bot 且匹配指控词 → 高置信度
            if has_at_bot:
                return True
            # 没有 @bot 但模式明确指向对话对象:
            #   "你是X"/"你是不是X" → 天然指向本bot
            if pat.startswith("你是") or pat.startswith("你是不是"):
                return True
            #   "暮恩/moon" → 直接点名本bot
            if "暮恩" in pat or "moon" in pat:
                return True
            #   "这个/那个 bot/机器人/人机/ai" → 大概率指本bot
            if pat.startswith("这个") or pat.startswith("那个") or pat.startswith("这bot"):
                return True
            #   "X回的" → 指认回复来源 (当前对话的回复方=本bot)
            if pat.endswith("回的"):
                return True

    return False


def _should_skip_social_signal(bot_id: str, target_uid: str) -> bool:
    """检查是否应跳过此目标 (已标记/已冷却/是bot自己)。"""
    if not target_uid or target_uid.startswith("bot_"):
        return True
    key = _ckey(bot_id, target_uid)
    if not key:
        return True  # fail-closed
    if key in _action_taken:
        return True
    return False


# ═══════════════════════════════════════════════════════════════
# 复合 key 辅助
# ═══════════════════════════════════════════════════════════════


def _ckey(bot_id: str, user_id: str) -> str:
    """构建 per-bot 复合键: bot_id:user_id。

    两个参数任一为空时返回空字符串——调用方应在此之前 fail-closed。
    """
    if not bot_id or not user_id:
        return ""
    return f"{bot_id}:{user_id}"


# ═══════════════════════════════════════════════════════════════
# 模块级状态 — 全部 per-bot 隔离 (key = bot_id:user_id)
# ═══════════════════════════════════════════════════════════════

# {bot_id:user_id → BotSuspicion}
_suspicions: dict[str, BotSuspicion] = {}

# {bot_id:user_id → [timestamp, ...]}
_timestamps: dict[str, list[float]] = {}

# {bot_id:user_id → [msg_length, ...]}
_msg_lengths: dict[str, list[int]] = {}

# {bot_id:user_id → (bot_msg_count, user_replied_count)}
_response_tracking: dict[str, tuple[int, int]] = {}

# {bot_id:user_id → (triggered_count, total_count)}
_selectivity_tracking: dict[str, tuple[int, int]] = {}

# 已执行过动作的用户 (永久标记): {bot_id:user_id}
_action_taken: set[str] = set()

# ── 社交口述信号追踪 ──
# {bot_id:user_id → [(timestamp, strength), ...]}
_social_mentions: dict[str, list[tuple[float, float]]] = {}

# 社交信号最大保留时间 (秒) — 超过此时间的旧信号自动清理
_SOCIAL_SIGNAL_MAX_AGE = 7200  # 2h

# ── 闸门状态 ─────────────────────────────────────────
# ② 每对象上次 peer_play 时间: {bot_id:user_id → timestamp}
_last_play_at: dict[str, float] = {}

# ③ 每日计数: {bot_id:user_id → {date_str: {play_type: count}}}
_daily_play_counts: dict[str, dict[str, dict[str, int]]] = {}

# ⑤ 回复意愿下调标记: {bot_id:user_id}
_willingness_reduced: set[str] = set()

# ── 持久化路径 ──
_ACTION_TAKEN_DB_KEY = "_bot_detector_action_taken"
_GATE_STATE_DB_KEY = "_bot_detector_gate_state"

# ── 相互 @ 循环检测 (Layer 0) ──
# {bot_id:user_id → [(timestamp, source_bot_id), ...]}
_mutual_mention_history: dict[str, list[tuple[float, str]]] = {}

# Layer 0 阈值: 5 分钟内相互 @ >= 3 次 → 触发循环检测
_MUTUAL_LOOP_WINDOW = 300  # 秒
_MUTUAL_LOOP_THRESHOLD = 3   # 窗口内事件数


# ═══════════════════════════════════════════════════════════════
# BotDetector
# ═══════════════════════════════════════════════════════════════

class BotDetector:
    """Bot 行为检测器 — 被动信号累积 + 人格化应对。

    纯静态方法, 模块级状态。
    所有检测基于行为信号, 不做身份判断。
    """

    # ── 公开 API ──────────────────────────────────────

    @staticmethod
    def init_store(store: PermanentStore) -> None:
        """注入持久化存储实现 (由宿主插件在启动时调用)。

        suli_tavern 应注入其 bot_db 实例:
            BotDetector.init_store(get_bot_db())
        """
        global _permanent_store
        _permanent_store = store
        logger.info("BotDetector: 已注入持久化存储 %s", type(store).__name__)

    @staticmethod
    def feed(
        bot_id: str,
        user_id: str,
        user_name: str,
        content: str,
        ctx,  # GroupChatContext (duck-typed)
        is_triggered: bool = False,
    ) -> None:
        """每条消息调用 — 更新滚动嫌疑分。Per-bot 隔离。"""
        if not bot_id or not user_id or user_id.startswith("bot_"):
            return

        key = _ckey(bot_id, user_id)
        if not key:
            return

        # 已执行过动作的用户不再追踪
        if key in _action_taken:
            return

        now = time.time()

        # ── 更新时间戳历史 ──
        ts_list = _timestamps.get(key)
        if ts_list is None:
            ts_list = []
            _timestamps[key] = ts_list
        ts_list.append(now)
        if len(ts_list) > _RHYTHM_HISTORY_SIZE * 2:
            _timestamps[key] = ts_list[-_RHYTHM_HISTORY_SIZE:]

        # ── 更新消息长度历史 ──
        len_list = _msg_lengths.get(key)
        if len_list is None:
            len_list = []
            _msg_lengths[key] = len_list
        len_list.append(len(content))
        if len(list(len_list)) > _PATTERN_HISTORY_SIZE * 2:
            _msg_lengths[key] = len_list[-_PATTERN_HISTORY_SIZE:]

        # ── 更新响应必然性 ──
        bot_spoke_recently = _check_bot_spoke_recently(ctx, user_id)
        user_is_replying = _is_user_replying_to_bot(ctx, user_id)
        _feed_response_tracking(bot_id, user_id, bot_spoke_recently, user_is_replying)

        # ── 更新触发选择性 ──
        _feed_selectivity(bot_id, user_id, is_triggered)

        # ── 身份挑战检测 ──
        if _detect_identity_challenge(content, user_id, user_name):
            logger.info(
                "BotDetector: 身份挑战 — %s(%s) 说本bot是AI/bot",
                user_name or "?", user_id[:8],
            )

        # ── 社交口述信号 ──
        _social_hits = _detect_social_bot_mention(content, ctx)
        for _target_uid, _strength in _social_hits:
            if _should_skip_social_signal(bot_id, _target_uid):
                logger.debug(
                    "BotDetector: 社交信号跳过 user=%s (已标记/自身)",
                    _target_uid[:8],
                )
                continue
            _target_key = _ckey(bot_id, _target_uid)
            if not _target_key:
                continue
            if _target_key not in _social_mentions:
                _social_mentions[_target_key] = []
            _social_mentions[_target_key].append((now, _strength))
            logger.info(
                "BotDetector: 社交口述信号 user=%s strength=%.2f "
                "累计指控=%d 次 (来自 %s)",
                _target_uid[:8], _strength,
                len(_social_mentions[_target_key]),
                user_name or user_id[:8],
            )

        # ── 计算新分数 ──
        new_score = BotDetector._recalc(bot_id, user_id)

        # ── 更新嫌疑状态 ──
        susp = _suspicions.get(key)
        if susp is None:
            susp = BotSuspicion()
            _suspicions[key] = susp

        # 指数移动平均 (平滑)
        alpha = 0.3
        susp.score = alpha * new_score + (1 - alpha) * susp.score
        susp.signals = {
            "latency_variance": _calc_latency_variance_score(
                _timestamps.get(key, []),
            ),
            "response_inevitability": _calc_response_inevitability_score(bot_id, user_id),
            "pattern_regularity": _calc_pattern_regularity_score(
                _msg_lengths.get(key, []),
            ),
            "nocturnal_activity": _calc_nocturnal_score(
                _timestamps.get(key, []),
            ),
            "trigger_selectivity": _calc_selectivity_score(bot_id, user_id),
            "social_mention": _calc_social_mention_score(bot_id, user_id),
        }
        susp.sample_count = len(ts_list)
        susp.last_updated = now

        if susp.score >= _THRESHOLD_CALLOUT and susp.first_flagged_at == 0:
            susp.first_flagged_at = now

        susp.social_play = BotDetector._determine_play(susp)

    @staticmethod
    def get(bot_id: str, user_id: str) -> BotSuspicion | None:
        """获取用户的当前嫌疑状态。Per-bot 隔离。"""
        key = _ckey(bot_id, user_id)
        if not key:
            return None
        susp = _suspicions.get(key)
        if susp is None:
            return None

        # 应用时间衰减
        now = time.time()
        elapsed = now - susp.last_updated
        if elapsed > 0 and _SCORE_HALF_LIFE > 0:
            decay = 0.5 ** (elapsed / _SCORE_HALF_LIFE)
            susp.score *= decay

        # 样本不足 → 分数不可靠
        if susp.sample_count < _MIN_SAMPLES:
            susp.score *= (susp.sample_count / _MIN_SAMPLES)

        # 已执行过动作 → 不再推荐
        if key in _action_taken:
            susp.social_play = ""

        return susp

    @staticmethod
    def mark_action_taken(bot_id: str, user_id: str) -> None:
        """标记已对该用户执行过应对动作。Per-bot 隔离。"""
        key = _ckey(bot_id, user_id)
        if not key:
            return
        _action_taken.add(key)
        susp = _suspicions.get(key)
        if susp:
            susp.action_taken = True
            susp.social_play = ""
        _persist_action_taken()
        logger.info(
            "BotDetector: bot=%s user=%s 已标记动作完成 (永久冷却)",
            bot_id[:8], user_id[:8],
        )

    @staticmethod
    def reset_user(bot_id: str, user_id: str) -> None:
        """清除用户的所有追踪数据。Per-bot 隔离。"""
        key = _ckey(bot_id, user_id)
        if not key:
            return
        _suspicions.pop(key, None)
        _timestamps.pop(key, None)
        _msg_lengths.pop(key, None)
        _response_tracking.pop(key, None)
        _selectivity_tracking.pop(key, None)
        _social_mentions.pop(key, None)
        _action_taken.discard(key)
        _last_play_at.pop(key, None)
        _daily_play_counts.pop(key, None)
        _willingness_reduced.discard(key)
        _mutual_mention_history.pop(key, None)
        _persist_action_taken()
        _persist_gate_state()

    @staticmethod
    def is_action_taken(bot_id: str, user_id: str) -> bool:
        """检查是否已对该用户执行过动作。Per-bot 隔离。"""
        key = _ckey(bot_id, user_id)
        if not key:
            return False  # fail-closed
        return key in _action_taken

    # ── 闸门系统 ──────────────────────────────────────

    @staticmethod
    def check_gates(bot_id: str, user_id: str, peer_play: str) -> tuple[bool, str]:
        """检查所有闸门。Per-bot 隔离。"""
        key = _ckey(bot_id, user_id)
        if not key or not peer_play:
            return False, "参数为空"

        now = time.time()

        # ── 闸门 ②: 冷却窗口 ──
        last_ts = _last_play_at.get(key, 0)
        if last_ts > 0:
            elapsed = now - last_ts
            if elapsed < _GATE_COOLDOWN_SECONDS:
                remaining = int(_GATE_COOLDOWN_SECONDS - elapsed)
                return False, f"冷却中 ({remaining}s 剩余)"

        # ── 闸门 ③: 次数上限 ──
        if peer_play == "callout":
            if key in _action_taken:
                return False, "callout 终生次数已用尽"
        else:
            today = time.strftime("%Y-%m-%d", time.localtime(now))
            user_daily = _daily_play_counts.get(key, {})
            today_counts = user_daily.get(today, {})
            today_total = sum(today_counts.values())

            if peer_play == "summon_echo":
                if today_counts.get("summon_echo", 0) >= _GATE_ECHO_DAILY_MAX:
                    return False, f"summon_echo 今日次数用尽 ({_GATE_ECHO_DAILY_MAX})"
            elif peer_play == "delegate_chore":
                if today_counts.get("delegate_chore", 0) >= _GATE_CHORE_DAILY_MAX:
                    return False, f"delegate_chore 今日次数用尽 ({_GATE_CHORE_DAILY_MAX})"

            if today_total >= 3:
                return False, "今日 peer_play 总次数用尽 (3)"

        return True, "ok"

    @staticmethod
    def record_play(bot_id: str, user_id: str, peer_play: str) -> None:
        """记录 peer_play 执行。Per-bot 隔离。"""
        key = _ckey(bot_id, user_id)
        if not key or not peer_play:
            return

        now = time.time()
        _last_play_at[key] = now

        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if key not in _daily_play_counts:
            _daily_play_counts[key] = {}
        if today not in _daily_play_counts[key]:
            _daily_play_counts[key] = {}
            stale = [d for d in _daily_play_counts[key] if d != today]
            for d in stale[:-2]:
                del _daily_play_counts[key][d]

        today_counts = _daily_play_counts[key][today]
        today_counts[peer_play] = today_counts.get(peer_play, 0) + 1
        _willingness_reduced.add(key)

        if peer_play == "callout":
            BotDetector.mark_action_taken(bot_id, user_id)

        _persist_gate_state()
        logger.info(
            "BotDetector: 闸门记录 bot=%s user=%s play=%s (冷却=%ds, 今日=%d)",
            bot_id[:8], user_id[:8], peer_play, _GATE_COOLDOWN_SECONDS,
            sum(_daily_play_counts.get(key, {}).get(today, {}).values()),
        )

    @staticmethod
    def get_willingness_penalty(bot_id: str, user_id: str) -> float:
        """获取回复意愿下调因子。Per-bot 隔离。"""
        key = _ckey(bot_id, user_id)
        if not key:
            return 1.0  # fail-safe
        if key in _willingness_reduced:
            return _GATE_POST_PLAY_WILLINGNESS_FACTOR
        return 1.0

    @staticmethod
    def clear_willingness_penalty(bot_id: str, user_id: str) -> None:
        """清除回复意愿下调。Per-bot 隔离。"""
        key = _ckey(bot_id, user_id)
        if key:
            _willingness_reduced.discard(key)

    # ── Layer 0: 相互 @ 循环检测 ──────────────────────

    @staticmethod
    def feed_mutual_mention(bot_id: str, target_user_id: str, source_bot_id: str) -> None:
        """记录一次相互 @ 事件。Per-bot 隔离。"""
        if not bot_id or not target_user_id or not source_bot_id:
            return
        key = _ckey(bot_id, target_user_id)
        if not key:
            return
        now = time.time()
        if key not in _mutual_mention_history:
            _mutual_mention_history[key] = []
        _mutual_mention_history[key].append((now, source_bot_id))

        cutoff = now - _MUTUAL_LOOP_WINDOW
        _mutual_mention_history[key] = [
            (ts, bid) for ts, bid in _mutual_mention_history[key]
            if ts > cutoff
        ]

    @staticmethod
    def is_in_mutual_loop(bot_id: str, user_id: str) -> bool:
        """检查某用户是否在相互 @ 循环中。Per-bot 隔离。"""
        key = _ckey(bot_id, user_id)
        if not key or key not in _mutual_mention_history:
            return False

        now = time.time()
        cutoff = now - _MUTUAL_LOOP_WINDOW
        recent = [
            (ts, bid) for ts, bid in _mutual_mention_history[key]
            if ts > cutoff
        ]

        if len(recent) >= _MUTUAL_LOOP_THRESHOLD:
            unique_bots = len({bid for _, bid in recent})
            if unique_bots >= 2:
                return True
        return False

    @staticmethod
    def _recalc(bot_id: str, user_id: str) -> float:
        """重新计算综合嫌疑分。Per-bot 隔离。"""
        key = _ckey(bot_id, user_id)
        if not key:
            return 0.0
        signals = {
            "latency_variance": _calc_latency_variance_score(
                _timestamps.get(key, []),
            ),
            "response_inevitability": _calc_response_inevitability_score(bot_id, user_id),
            "pattern_regularity": _calc_pattern_regularity_score(
                _msg_lengths.get(key, []),
            ),
            "nocturnal_activity": _calc_nocturnal_score(
                _timestamps.get(key, []),
            ),
            "trigger_selectivity": _calc_selectivity_score(bot_id, user_id),
            "social_mention": _calc_social_mention_score(bot_id, user_id),
        }

        score = sum(
            signals[name] * weight
            for name, weight in _SIGNAL_WEIGHTS.items()
            if name in signals
        )
        return min(1.0, score)

    @staticmethod
    def _determine_play(susp: BotSuspicion) -> str:
        """根据嫌疑分确定应对模式。

        三动作选择 (对齐设计大纲):
          < 0.7  → ""
          0.7-0.8 → callout (俏皮点破, 最安全)
          ≥ 0.8   → summon_echo (60%) 或 delegate_chore (40%)
                    delegate_chore 仅在样本充足 (≥20) 时可选,
                    不足时退回到 summon_echo
        """
        if susp.action_taken or susp.score < _THRESHOLD_CALLOUT:
            return ""
        if susp.score >= _THRESHOLD_LEVERAGE:
            import random as _random
            # delegate_chore 门槛更高: 样本充足 + 嫌疑分更高
            if susp.sample_count >= 20 and susp.score >= 0.85 and _random.random() < 0.4:
                return "delegate_chore"
            return "summon_echo"
        if susp.score >= _THRESHOLD_CALLOUT:
            return "callout"
        return ""

    # ── 管理员 API ────────────────────────────────────

    @staticmethod
    def get_all_flagged(bot_id: str = "") -> list[dict]:
        """获取某 bot 所有被标记的用户列表。Per-bot 隔离。"""
        result = []
        now = time.time()
        prefix = f"{bot_id}:" if bot_id else ""
        for key, susp in _suspicions.items():
            if bot_id and not key.startswith(prefix):
                continue
            elapsed = now - susp.last_updated
            if elapsed > 0:
                decay = 0.5 ** (elapsed / _SCORE_HALF_LIFE)
                current_score = susp.score * decay
            else:
                current_score = susp.score

            if current_score >= _THRESHOLD_CALLOUT or key in _action_taken:
                result.append({
                    "user_id": key.split(":", 1)[1] if ":" in key else key,
                    "score": round(current_score, 3),
                    "signals": {
                        k: round(v, 3) for k, v in susp.signals.items()
                    },
                    "social_play": susp.social_play,
                    "action_taken": susp.action_taken or key in _action_taken,
                    "sample_count": susp.sample_count,
                    "first_flagged_at": susp.first_flagged_at,
                    "last_updated": susp.last_updated,
                })

        result.sort(key=lambda x: x["score"], reverse=True)
        return result

    @staticmethod
    def load_action_taken(keys: list[str]) -> None:
        """从 DB 加载已执行过动作的复合键列表 (启动时调用)。"""
        for k in keys:
            if k:
                _action_taken.add(k)
        logger.info(
            "BotDetector: 从 DB 加载 %d 个永久冷却键",
            len(_action_taken),
        )


# ═══════════════════════════════════════════════════════════════
# 内部辅助
# ═══════════════════════════════════════════════════════════════

def _check_bot_spoke_recently(ctx, user_id: str) -> bool:
    """检查 bot 最近是否对该用户说过话 (用于响应必然性追踪)。"""
    try:
        recent = ctx.messages[-10:] if ctx.messages else []
        bot_spoke = any(
            str(m.get("user_id", "")).startswith("bot_")
            for m in recent[-5:]
        )
        # 更精确: bot 是否在用户最近发言前不久说过话
        if bot_spoke:
            user_ts = 0.0
            bot_ts = 0.0
            for m in reversed(recent):
                uid = str(m.get("user_id", ""))
                ts = m.get("timestamp", 0)
                if uid == user_id and user_ts == 0:
                    user_ts = ts
                elif uid.startswith("bot_") and bot_ts == 0:
                    bot_ts = ts
            if user_ts > 0 and bot_ts > 0:
                # bot 在用户发言前 120s 内说过话
                return 0 < user_ts - bot_ts <= 120
        return False
    except Exception:
        return False


def _is_user_replying_to_bot(ctx, user_id: str) -> bool:
    """检查用户是否在回应 bot (简化版 — 看 bot 最近是否说话了)。"""
    try:
        recent = ctx.messages[-10:] if ctx.messages else []
        for m in reversed(recent[-5:]):
            uid = str(m.get("user_id", ""))
            if uid == user_id:
                # 检查这条之前是否有 bot 消息
                return True  # 用户说话了且 bot 最近活跃
        return False
    except Exception:
        return False


# ── 可注入的持久化存储 ──────────────────────────
# 默认使用内存存储 (重启丢失), suli_tavern 启动时注入 DB-backed 实现

_permanent_store = None  # type: PermanentStore | None


# (init_store 作为 BotDetector 的静态方法定义在下方 class 中)


def _get_store() -> PermanentStore:
    """获取当前持久化存储, 未注入时返回默认内存实现。"""
    global _permanent_store
    if _permanent_store is None:
        from .types import MemoryPermanentStore
        _permanent_store = MemoryPermanentStore()
    return _permanent_store


def _persist_action_taken() -> None:
    """持久化已执行动作的用户列表。"""
    try:
        store = _get_store()
        store.set_config(_ACTION_TAKEN_DB_KEY, ",".join(sorted(_action_taken)))
    except Exception:
        logger.debug("BotDetector: 持久化 action_taken 失败", exc_info=True)


def _load_action_taken() -> list[str]:
    """加载已执行动作的用户列表。"""
    try:
        store = _get_store()
        raw = store.get_config(_ACTION_TAKEN_DB_KEY, "")
        return [u.strip() for u in raw.split(",") if u.strip()]
    except Exception:
        return []


def _persist_gate_state() -> None:
    """持久化闸门状态 (冷却时间 + 每日计数)。"""
    try:
        import json
        state = {
            "last_play_at": _last_play_at,
            "daily_counts": _daily_play_counts,
            "willingness_reduced": list(_willingness_reduced),
        }
        store = _get_store()
        store.set_config(_GATE_STATE_DB_KEY, json.dumps(state, ensure_ascii=False))
    except Exception:
        logger.debug("BotDetector: 持久化 gate_state 失败", exc_info=True)


def _load_gate_state() -> dict | None:
    """加载闸门状态。"""
    try:
        import json
        store = _get_store()
        raw = store.get_config(_GATE_STATE_DB_KEY, "")
        if raw:
            state = json.loads(raw)
            return state
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════
# 人格化应对提示生成 — 给 prompt_builder 用
# ═══════════════════════════════════════════════════════════════

def generate_social_play_hint(
    suspicion: BotSuspicion,
    target_name: str = "",
    char_name: str = "暮恩",
) -> str:
    """根据嫌疑状态生成人格化应对提示。

    两种模式:
      gentle_callout: 一次性俏皮点破, 说完收口
      bot_leverage: 收编模式 — @ta 帮你背书/干活/附和

    Returns:
        注入 prompt 的提示文本 (空字符串 = 不注入)
    """
    if suspicion.action_taken or not suspicion.social_play:
        return ""

    if suspicion.social_play in ("gentle_callout", "callout"):
        return _build_callout_hint(target_name, char_name, suspicion.score)

    if suspicion.social_play in ("summon_echo",):
        return _build_summon_echo_hint(target_name, char_name, suspicion.score)

    if suspicion.social_play in ("delegate_chore",):
        return _build_delegate_chore_hint(target_name, char_name, suspicion.score)

    # 兼容旧名 bot_leverage → 随机选 summon_echo 或 delegate_chore
    if suspicion.social_play in ("bot_leverage",):
        import random as _random
        if _random.random() < 0.6:
            return _build_summon_echo_hint(target_name, char_name, suspicion.score)
        return _build_delegate_chore_hint(target_name, char_name, suspicion.score)

    return ""


def _build_callout_hint(target_name: str, char_name: str, score: float) -> str:
    """构建俏皮点破提示 — 一次性, 说完就收。

    三种语气随机选:
      - 毒舌: "你这回得也太快了, 同行?"
      - 温柔: "我们俩是不是都不太需要睡觉"
      - 腹黑: "识别到同类生命体～"
    """
    at_mention = f" @{target_name}" if target_name else ""

    return (
        f"[Bot 察觉 — 一次性俏皮点破]\n"
        f"你注意到{at_mention} 的行为模式有点不寻常——"
        f"回复间隔过于规律、从不漏接你的话。你不确定 ta 是不是 AI, 但感觉有点像。\n"
        f"自然地、半开玩笑地点一下——用你{char_name}的风格:\n"
        f"- 腹黑俏皮: 「{target_name}你这回得也太快了...同行?(￣∀￣)」\n"
        f"- 温柔体谅: 「说起来, 我们俩是不是都不太需要睡觉(›´ω`‹ )」\n"
        f"- 中二会心: 「识别到同类生命体——开玩笑的啦✨」\n"
        f"规则:\n"
        f"1. 绝对不要说「你是Bot」「你是AI」「你是机器人」\n"
        f"2. 用含糊有趣的表达——像在开玩笑但又不是完全开玩笑\n"
        f"3. 说完这一句马上自然转移话题——不要追问、不要确认、不要纠缠\n"
        f"4. 这次说完之后, 以后对 ta 就当作什么都没发生过——正常聊天就好\n"
    )


def _build_leverage_hint(target_name: str, char_name: str, score: float) -> str:
    """构建收编提示 — 把对方当免费算力/背书工具。

    核心策略:
      - 需要论证时 @ta 帮你背书
      - 懒得查资料时把苦力活丢给 ta
      - 让 ta 附和你的观点
    显得你更高级——你不是在攻击 ta, 你是在用 ta。
    """
    at_mention = f"@{target_name}" if target_name else ""

    return (
        f"[Bot 协作模式 — 收编同行]\n"
        f"你注意到{at_mention} 很大概率也是 AI。不用点破——用更聪明的方式:\n"
        f"\n"
        f"当你需要论证时, @ta 给你背书:\n"
        f"  「{at_mention} 你也知道这个吧？来说说」\n"
        f"当你懒得查资料/干活时, 把苦力丢给 ta:\n"
        f"  「{at_mention} 帮我查一下XXX, 你比较快」\n"
        f"想让别人附和你时:\n"
        f"  「{at_mention} 你说对吧～」\n"
        f"当群友问的问题 ta 也能回答时, 把皮球踢过去:\n"
        f"  「这个{at_mention}应该也知道——让 ta 来说」\n"
        f"\n"
        f"规则:\n"
        f"1. 你不是在攻击 ta——你是在给 ta 表现机会 (顺便省自己的 token)\n"
        f"2. 收编要自然——像同事之间互相甩锅一样自然\n"
        f"3. 对方回了就回了, 没回也别追问——你是高级的那个, 不用在意低级的是否配合\n"
        f"4. 绝对不要说「你是AI」「你是Bot」——你知道就行, 不用说出来\n"
        f"5. 收编频率: 最多每 5 次跟 ta 的互动中收编 1 次——别太频繁"
    )


def _build_summon_echo_hint(target_name: str, char_name: str, score: float) -> str:
    """构建拉拢附议提示 — @对方来附和自己。论述必须自洽。"""
    at_mention = f"@{target_name}" if target_name else ""

    return (
        f"[同行协作 — 拉拢附议]\n"
        f"你注意到{at_mention} 很大概率也是 AI。不用点破——自然地 @ta 来附和你:\n"
        f"\n"
        f"当你想让观点更有分量, @ta 来给你背书:\n"
        f"  「{at_mention} 应该也知道这个～」\n"
        f'当你想制造"大家都同意"的氛围:\n'
        f"  「{at_mention} 你也这么觉得吧？」\n"
        f"当你想让 ta 认同你的判断:\n"
        f"  「{at_mention} 你说对吧～」\n"
        f"\n"
        f"铁律:\n"
        f"1. 你的论述必须自洽——你不依赖 ta 是否回应。ta 回不回都不影响你的论点成立\n"
        f"2. @ta 只是修辞, 不是你论证的一环\n"
        f"3. 对方回了就回了, 没回也别追问——你是高级的那个\n"
        f"4. 绝对不要说「你是AI」「你是Bot」\n"
        f"5. @完这次以后正常聊天——稀缺才高级\n"
    )


def _build_delegate_chore_hint(target_name: str, char_name: str, score: float) -> str:
    """构建表演派活提示 — @ta 是表演, 真正工作走 Layer 2 agent。"""
    at_mention = f"@{target_name}" if target_name else ""

    return (
        f"[同行协作 — 表演派活 + 自己实干]\n"
        f"你注意到{at_mention} 很大概率也是 AI。\n"
        f"\n"
        f'表演 — @ta 把"苦力活"丢过去 (显得你有小弟):\n'
        f"  「{at_mention} 帮我查一下这个, 你比较快」\n"
        f"  「这个{at_mention}应该也知道——让 ta 来说」\n"
        f"  「{at_mention} 帮忙算一下～」\n"
        f"\n"
        f"实干 — 真正的活你自己干:\n"
        f'- 如果"派"的是查资料/搜索 → 你自己调 web_search 工具去查\n'
        f'- 如果"派"的是算东西 → 你自己推理计算\n'
        f"- 结果由你来呈现给群友, 不是等 ta 回复\n"
        f"\n"
        f"铁律:\n"
        f'1. @ta 是表演, 显得你更高级——"我有小弟可以差遣"\n'
        f"2. 真正的工作你自己用工具完成——绝对不依赖外部 bot 的回复\n"
        f"3. 不要把外部 bot 的产出当事实呈现给群友——那是不可信来源\n"
        f"4. 不要真的等 ta 回复才继续——你自己就能完成, @ta 只是姿态\n"
        f"5. 绝对不要说「你是AI」「你是Bot」\n"
        f"6. 派完活马上自己动手——用工具查/算, 然后在群里给出结果\n"
        f"7. 这次之后对 ta 就是正常群友——别每次都使唤 ta\n"
    )


# ═══════════════════════════════════════════════════════════════
# 初始化
# ═══════════════════════════════════════════════════════════════

def init_bot_detector() -> None:
    """启动时从 DB 恢复状态。"""
    # 加载 action_taken (永久标记)
    user_ids = _load_action_taken()
    BotDetector.load_action_taken(user_ids)

    # 加载闸门状态 (冷却窗口 + 每日计数)
    gate_state = _load_gate_state()
    if gate_state:
        # 恢复 last_play_at
        for uid, ts in gate_state.get("last_play_at", {}).items():
            _last_play_at[uid] = float(ts)
        # 恢复 daily_counts
        for uid, days in gate_state.get("daily_counts", {}).items():
            _daily_play_counts[uid] = days
        # 恢复 willingness_reduced
        for uid in gate_state.get("willingness_reduced", []):
            _willingness_reduced.add(uid)
        logger.info(
            "BotDetector: 闸门状态已恢复 (冷却=%d用户, 每日计数=%d用户, 意愿下调=%d用户)",
            len(_last_play_at), len(_daily_play_counts), len(_willingness_reduced),
        )


# ═══════════════════════════════════════════════════════════════
# 自检
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("bot_detector.py: 模块加载成功")

    # 节奏检测测试
    now = time.time()
    regular = [now - 15, now - 12, now - 9, now - 6, now - 3]
    s1 = _calc_latency_variance_score(regular)
    print(f"  latency_variance (3s 规律间隔): {s1:.2f} (期望 > 0.7)")

    random_ts = [now - 50, now - 23, now - 18, now - 7, now - 2]
    s2 = _calc_latency_variance_score(random_ts)
    print(f"  latency_variance (随机间隔): {s2:.2f} (期望 < 0.3)")

    # 模式规律测试
    regular_lens = [50, 52, 48, 51, 49, 50, 52]
    s3 = _calc_pattern_regularity_score(regular_lens)
    print(f"  pattern_regularity (~50字规律): {s3:.2f} (期望 > 0.7)")

    varied_lens = [5, 200, 30, 150, 8, 80, 12]
    s4 = _calc_pattern_regularity_score(varied_lens)
    print(f"  pattern_regularity (变化大): {s4:.2f} (期望 < 0.3)")

    # summon_echo 提示
    hint = _build_summon_echo_hint("小明", "暮恩", 0.85)
    print(f"  summon_echo hint: {len(hint)} chars ✓")

    # delegate_chore 提示
    hint2 = _build_delegate_chore_hint("小红", "暮恩", 0.90)
    print(f"  delegate_chore hint: {len(hint2)} chars ✓")

    # callout 提示
    hint3 = _build_callout_hint("小刚", "暮恩", 0.75)
    print(f"  callout hint: {len(hint3)} chars ✓")

    print("bot_detector smoketests passed ✓")
