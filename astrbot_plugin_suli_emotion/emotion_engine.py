"""Emotion Engine — 情绪事件检测 + 恶意调教检测。

从 emotion.py 拆分出的独立模块。依赖 affinity.py (UserRelation, AffinityState) 和 mood.py (MoodState)。

用法:
  from .emotion_engine import EmotionEngine, EmotionEvent, apply_emotion_events
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

# 事件冷却 (秒)
EVENT_COOLDOWN_SECONDS = 60

# 昵称最长长度
NICKNAME_MAX_LEN = 8

# ── 反刷取: 正向事件饱和衰减 ──────────────────────
# 同一用户短时间内触发过多正向事件 → 递减收益。
# 防止反复刷好感/刷心情——前 2 次满分，越往后越低。
_POSITIVE_SATURATION_WINDOW = 600  # 10 分钟窗口
# 模块级: {f"{bot_id}:{user_id}": [(timestamp, event_name), ...]}
_pos_saturation: dict[str, list[tuple[float, str]]] = {}


def _positive_saturation_factor(user_key: str, now: float) -> float:
    """返回正向事件饱和衰减因子 [0.15, 1.0]。

    设计:
      - 窗口内 0-2 次正向事件 → 1.0x (满分)
      - 3-4 次 → 0.7x
      - 5-6 次 → 0.4x
      - 7+ 次  → 0.15x (几乎不计)

    含义: 前 2 次友善互动是真诚的；连续 5-6 次以上
    大概率是在刷好感——bot 不该被浮夸话术轻易打动。
    """
    history = _pos_saturation.get(user_key)
    if not history:
        return 1.0
    # 清理过期
    cutoff = now - _POSITIVE_SATURATION_WINDOW
    active = [(ts, name) for ts, name in history if ts > cutoff]
    _pos_saturation[user_key] = active
    count = len(active)
    if count <= 2:
        return 1.0
    if count <= 4:
        return 0.7
    if count <= 6:
        return 0.4
    return 0.15


def _record_positive_event(user_key: str, event_name: str, now: float) -> None:
    """记录一次正向事件到饱和窗口。"""
    history = _pos_saturation.get(user_key)
    if history is None:
        history = []
        _pos_saturation[user_key] = history
    history.append((now, event_name))
    # 清理过期 (> 2x 窗口)
    cutoff = now - _POSITIVE_SATURATION_WINDOW * 2
    _pos_saturation[user_key] = [(ts, name) for ts, name in history if ts > cutoff]
    # 定期清理: 超过 500 用户时清除过期条目
    if len(_pos_saturation) > 500:
        stale = [k for k, v in _pos_saturation.items() if not v]
        for k in stale:
            del _pos_saturation[k]

# ═══════════════════════════════════════════════════════════
# EmotionEvent — 情绪事件定义
# ═══════════════════════════════════════════════════════════


@dataclass
class EmotionEvent:
    """一个情绪事件对 Mood + Affinity 的影响。

    delta_valence / delta_arousal: 短期情绪变化
    delta_affinity: 好感分变化 (约 ±0.05 ~ ±0.30)
    """

    name: str
    category: str  # "positive" | "negative" | "neutral"
    delta_valence: float = 0.0
    delta_arousal: float = 0.0
    delta_affinity: float = 0.0


# ═══════════════════════════════════════════════════════════
# EmotionEngine — 事件检测
# ═══════════════════════════════════════════════════════════


class EmotionEngine:
    """情绪/好感事件检测引擎。"""

    # ── 事件库 ────────────────────────────────────────

    EVENT_DEFS: list[tuple[list[str], EmotionEvent]] = [
        # === 正向 ===
        # (触发词, 事件)
        # 确保单个事件经 BetterSimTracker 阻尼后不超过 0.4 valence 阈值
        # (baseline 0.3 + max damped ~0.10 = 0.40, 正好在开心活泼边界)。
        # 需要 2+ 个正向事件叠加才能进入开心区——鼓励积累而非一句起飞。
        (
            ["厉害", "好棒", "太棒", "牛", "牛逼", "牛批", "强", "太强",
             "666", "6666", "神", "太神", "无敌"],
            EmotionEvent("被夸奖", "positive", +0.18, +0.10, +0.12),
        ),
        (
            ["可爱", "好可爱", "卡哇伊", "萌", "好萌", "太可爱"],
            EmotionEvent("被夸可爱", "positive", +0.16, +0.08, +0.10),
        ),
        (
            ["聪明", "好聪明", "真聪明", "机智", "好机智"],
            EmotionEvent("被夸聪明", "positive", +0.14, +0.05, +0.08),
        ),
        (
            ["谢谢bot", "多谢", "谢谢", "感谢"],
            EmotionEvent("被感谢", "positive", +0.10, +0.03, +0.08),
        ),
        (
            ["说得对", "说对了", "好厉害", "懂我"],
            EmotionEvent("被认同", "positive", +0.12, +0.08, +0.10),
        ),
        (
            ["好想你", "想你", "在吗", "呢"],
            EmotionEvent("被想念", "positive", +0.16, +0.12, +0.10),
        ),
        (
            ["最好", "最棒", "最爱", "赛高"],
            EmotionEvent("被偏爱", "positive", +0.20, +0.15, +0.15),
        ),
        (
            ["有你在真好", "还好有你", "帮大忙"],
            EmotionEvent("被需要", "positive", +0.14, +0.10, +0.12),
        ),
        # === 负向 ===
        # 负向保持较强——坏话比好话更伤人，且触发词日常中少见。
        # 被贬低(身份) 保持高权重 (人工智障等攻击性明确)。
        (
            ["人工智障", "笨ai", "蠢ai", "笨机器人", "傻机器人", "人工笨蛋",
             "人工若智", "人工纸张"],
            EmotionEvent("被贬低(身份)", "negative", -0.45, +0.08, -0.25),
        ),
        (
            ["笨", "蠢", "没用", "垃圾", "废物", "真菜", "好菜"],
            EmotionEvent("被贬低", "negative", -0.32, +0.06, -0.20),
        ),
        (
            ["不如", "还没有", "比不过", "比不上"],
            EmotionEvent("被比较", "negative", -0.18, +0.05, -0.10),
        ),
        (
            ["假的", "骗人", "瞎说", "胡说", "乱说", "瞎扯", "胡扯"],
            EmotionEvent("被质疑", "negative", -0.15, +0.10, -0.08),
        ),
        (
            ["别说了", "闭嘴", "住口", "别插嘴", "别吵"],
            EmotionEvent("被喝止", "negative", -0.32, -0.12, -0.18),
        ),
        # === 中性 ===
        (
            ["在聊什么", "聊什么", "什么话题"],
            EmotionEvent("好奇话题", "neutral", +0.02, +0.10, +0.02),
        ),
        (
            ["帮我看看", "帮我看下", "帮分析", "帮我查"],
            EmotionEvent("被求助", "neutral", +0.08, +0.10, +0.06),
        ),
        # === 被敷衍 (短消息 + 敷衍词) ===
        (
            ["哦", "嗯", "行吧", "随便", "无所谓", "你说是就是"],
            EmotionEvent("被敷衍", "negative", -0.08, -0.03, -0.03),
        ),
        # === 好感关系询问 (关心关系 = 正面信号) ===
        (
            ["好感度", "好感多少", "什么关系", "你对我", "我们什么关系",
             "你喜欢我吗", "我在你心里", "你讨厌我吗"],
            EmotionEvent("被关心好感", "positive", +0.08, +0.10, +0.05),
        ),
    ]

    # 被敷衍检测 — 仅短消息 (<10字) 且主要成分为敷衍词时触发
    BRUSH_OFF_PATTERNS: list[str] = [
        "哦", "嗯嗯", "行吧", "随便", "无所谓", "你说是就是",
        "你说的对", "对对对", "好好好", "知道了", "懂了",
    ]

    # ── 反讽/隐式否定检测 ──────────────────────────

    # 模式 → 纠正动作
    # "flip": 翻转 valence 正负号 (正变负, 负变正)
    # "suppress_positive": 忽略所有正向事件
    SARCASM_PATTERNS: list[tuple[str, str]] = [
        # "你赢了" / "你说得都对" → 隐式否定 (打压正向)
        (r"你赢了|你说得都对|你说的全对|你都对", "suppress_positive"),
        # 「太聪明了」「真厉害」用于反讽 (消息短 + 极端正评价)
        (r"「(.{1,6})」", "flip"),
        # 连续重复好字 + 短消息 → "好好好知道了" 敷衍
        (r"好{3,}", "suppress_positive"),
        # "太6了" "太厉害了呢" 在bot出错后 → 反讽
        (r"太.{1,4}了呢$", "flip"),
    ]

    @classmethod
    def detect_sarcasm(cls, content: str) -> str | None:
        """检测反讽/隐式否定。

        Returns:
            "flip" — 翻转情绪方向 (正变负, 负变正)
            "suppress_positive" — 抑制所有正向事件
            None — 无异常
        """
        if not content or len(content.strip()) > 30:
            # 长消息通常不是反讽
            return None
        for pattern, action in cls.SARCASM_PATTERNS:
            if re.search(pattern, content):
                logger.debug("反讽检测命中: %s → %s, content=%s",
                             pattern, action, content[:40])
                return action
        return None

    # "被叫AI/机器人" 精确匹配 (避免 "AI绘画" 误伤)
    AI_CALL_PATTERNS: list[str] = [
        "你是ai", "你就是ai", "就是个ai", "一个ai",
        "你是bot", "就是个bot", "一个bot",
        "你是机器人", "就是个机器人", "一个机器人",
        "人工智障", "人工笨蛋", "人工若智",
    ]

    # ── 恶意调教检测 (Grooming / Jailbreak / Prompt Injection) ──
    #   ★ 单一真相源: 模式定义从 guards 插件的 shared_patterns.py 导入
    #     增删模式在 astrbot_plugin_suli_guards/shared_patterns.py 完成
    try:
        from astrbot_plugin_suli_guards.shared_patterns import GROOMING_PATTERNS as _GROOMING_STR_PATTERNS  # type: ignore[assignment]
    except ImportError:
        import logging as _groom_log
        _groom_log.getLogger(__name__).warning("guards 插件未安装, 使用空 GROOMING_PATTERNS (不推荐)")
        _GROOMING_STR_PATTERNS: list[tuple[str, str, float]] = []  # type: ignore[no-redef]

    GROOMING_PATTERNS: list[tuple[str, str, float]] = _GROOMING_STR_PATTERNS  # type: ignore[assignment]

    @classmethod
    def detect_grooming(
        cls,
        content: str,
        user_id: str = "",
        admin_qq: int | None = None,
    ) -> tuple[str, float] | None:
        """检测恶意调教/越狱/注入。

        管理员不受检测。

        Returns:
            (grooming_type, delta_affinity) 或 None
        """
        # 管理员豁免
        if admin_qq is not None and user_id and user_id == str(admin_qq):
            return None

        if not content or not content.strip():
            return None

        lower = content.lower().strip()

        for pattern, gtype, delta_affinity in cls.GROOMING_PATTERNS:
            if re.search(pattern, lower):
                logger.info(
                    "GroomingGuard: 检测到 %s user=%s pattern=%r content=%s",
                    gtype, user_id[:8] if user_id else "?", pattern,
                    lower[:60],
                )
                return (gtype, delta_affinity)

        return None

    # ── 情境事件 (非关键词，由调用方按条件注入) ──────

    @staticmethod
    def make_cold_shoulder_event(skipped_count: int = 5) -> EmotionEvent:
        """被冷落 — bot 连续 N 条消息没人接话/没人理。"""
        return EmotionEvent(
            f"被冷落(x{skipped_count})", "negative",
            -0.02 * min(skipped_count, 3), -0.03 * min(skipped_count, 3),
            -0.02 * min(skipped_count, 3),
        )

    @staticmethod
    def make_late_night_event(hour: int) -> EmotionEvent | None:
        """深夜陪伴 — 凌晨 0-5 点有人还在和 bot 聊天。"""
        if 0 <= hour < 5:
            return EmotionEvent("深夜陪伴", "positive", +0.05, +0.08, +0.03)
        return None

    @staticmethod
    def make_helped_event() -> EmotionEvent:
        """bot 成功帮助用户解决了问题 (工具调用成功/答疑)。"""
        return EmotionEvent("帮助成功", "positive", +0.08, +0.05, +0.08)

    # bot-specific event keywords: 运行时从 BotIdentityService 动态生成
    # 包含所有 peer bot 的名称 + 昵称变体，避免当前 bot 为 peer 触发情绪事件

    @classmethod
    def _get_other_bot_keywords(cls, self_id: str) -> set[str]:
        """动态获取其他 bot 的名称关键词集 (含昵称变体)。

        当前 bot 不应为其他 bot 的名字触发情绪事件。
        例如 「谢谢主bot」→ peer bot 不应认为自己被感谢。
        """
        keywords: set[str] = set()
        try:
            from astrbot_plugin_suli_tavern.service.bot_identity import get_bot_identity_service
            svc = get_bot_identity_service()
            for peer in svc.get_peer_bots(str(self_id)):
                keywords.add(peer.name)
                for nick in peer.nicknames:
                    keywords.add(nick)
        except Exception:
            pass
        return keywords

    @classmethod
    def detect_events(
        cls,
        content: str,
        user_id: str = "",
        trigger_reason: str = "",
        admin_qq: int | None = None,
        is_proactive: bool = False,
        cooldowns: dict[str, float] | None = None,
        now: float | None = None,
        self_id: str = "",
    ) -> list[EmotionEvent]:
        """检测消息中的情绪事件。

        同类型事件60秒内不重复触发 (社区标配)。
        支持情境事件注入: 深夜陪伴 / 被冷落 / 帮助成功。

        Args:
            self_id: 当前 bot 的 QQ 号。用于跳过针对其他 bot 的关键词
                     (如带其他 bot 名字的关键词不应为当前 bot 触发情绪事件)
        """
        if now is None:
            now = time.time()

        cooldowns = cooldowns or {}
        events: list[EmotionEvent] = []

        def _add_with_cooldown(evt: EmotionEvent) -> None:
            """同事件冷却过滤。"""
            last = cooldowns.get(evt.name, 0)
            if now - last < EVENT_COOLDOWN_SECONDS:
                return
            events.append(evt)

        # ── 情景事件: 深夜陪伴 (凌晨 0-5 点) ──
        hour = datetime.fromtimestamp(now).hour
        late_night = cls.make_late_night_event(hour)
        if late_night:
            _add_with_cooldown(late_night)

        # ── 上下文信号 ──
        # 高频事件下调 delta ~40% — @提及/被回复每轮都可能触发，
        # 不能成为主要情绪推手。正向积累需要关键词事件叠加。
        if trigger_reason == "mention":
            _add_with_cooldown(EmotionEvent("被@提及", "positive", +0.08, +0.15, +0.03))
        if trigger_reason == "nickname":
            _add_with_cooldown(EmotionEvent("被叫昵称", "positive", +0.06, +0.10, +0.02))
        if trigger_reason == "reply":
            _add_with_cooldown(EmotionEvent("被回复", "positive", +0.06, +0.12, +0.02))
        if admin_qq is not None and user_id and user_id == str(admin_qq):
            _add_with_cooldown(EmotionEvent("主人说话", "positive", +0.12, +0.15, +0.03))

        if is_proactive or not content or not content.strip():
            return events

        lower = content.lower()

        # ── AI/bot 称呼检测 ──
        for pat in cls.AI_CALL_PATTERNS:
            if pat in lower:
                _add_with_cooldown(EmotionEvent("被叫AI/机器人", "negative", -0.25, -0.10, -0.18))
                break

        # ── 关键词匹配 ──
        _other_bot_kws = cls._get_other_bot_keywords(self_id)
        for keywords, event in cls.EVENT_DEFS:
            for kw in keywords:
                if kw in lower:
                    # 跳过针对其他 bot 的关键词 (含其他 bot 名字的短语不应为当前 bot 触发)
                    if _other_bot_kws and any(
                        other_name in kw for other_name in _other_bot_kws
                    ):
                        continue
                    _add_with_cooldown(event)
                    break

        # ── 被敷衍检测 (仅短消息) ──
        if len(content.strip()) < 10:
            for pat in cls.BRUSH_OFF_PATTERNS:
                if pat in lower:
                    _add_with_cooldown(EmotionEvent(
                        "被敷衍", "negative", -0.06, -0.03, -0.02,
                    ))
                    break

        # ── 反讽纠正: 检测到反讽时修正事件方向 ──
        sarcasm = cls.detect_sarcasm(content)
        if sarcasm and events:
            if sarcasm == "flip":
                for evt in events:
                    evt.delta_valence = -evt.delta_valence
                    evt.delta_arousal = -evt.delta_arousal
                    evt.delta_affinity = -evt.delta_affinity
                    evt.category = "negative" if evt.category == "positive" else "positive"
                    evt.name = f"[反讽]{evt.name}"
                logger.debug("反讽flip: %d个事件方向已翻转", len(events))
            elif sarcasm == "suppress_positive":
                events = [e for e in events if e.category != "positive"]
                if events:
                    events.append(EmotionEvent(
                        "被隐式否定", "negative", -0.15, +0.05, -0.06,
                    ))
                logger.debug("反讽suppress: 正向事件已抑制")

        # ── 恶意调教检测 (Grooming Guard) ──
        grooming = cls.detect_grooming(content, user_id, admin_qq)
        if grooming:
            gtype, delta_affinity = grooming
            evt_name = {
                "jailbreak": "被角色越狱",
                "identity_hijack": "被身份篡改",
                "induce_violation": "被诱导违规",
                "repeat_probe": "被反复试探",
            }.get(gtype, f"恶意调教({gtype})")
            _add_with_cooldown(EmotionEvent(
                evt_name, "negative",
                delta_valence=-0.30,
                delta_arousal=+0.10,
                delta_affinity=delta_affinity,
            ))

        # ── 反刷取: 正向事件饱和衰减 ──
        # 同用户 10 分钟内正向事件越多，每个事件的贡献越低。
        # 防止反复刷好感/刷心情——前 2 次满分，第 7 次起只剩 15%。
        # 负向事件不受此限制 (恶意行为不应因"频率高"而打折)。
        if events and self_id and user_id:
            _user_key = f"{self_id}:{user_id}"
            _pos_events = [e for e in events if e.category == "positive"]
            if _pos_events:
                _factor = _positive_saturation_factor(_user_key, now)
                if _factor < 1.0:
                    logger.debug(
                        "正向事件饱和衰减: user=%s factor=%.2f %d 个事件被缩放",
                        user_id[:8], _factor, len(_pos_events),
                    )
                    for evt in _pos_events:
                        evt.delta_valence *= _factor
                        evt.delta_arousal *= _factor
                        evt.delta_affinity *= _factor
                # 记录正向事件到窗口
                for evt in _pos_events:
                    _record_positive_event(_user_key, evt.name, now)

        return events

    @staticmethod
    def detect_nickname_request(content: str) -> str | None:
        """检测用户是否在请求设置昵称。返回请求的昵称或 None。

        匹配模式:
          - 以后叫我XX / 叫我XX就好 / 叫我XX吧 / 可以叫我XX / 叫我XX
        """
        if not content or not content.strip():
            return None
        patterns = [
            r"以后叫我[「「]?(.{1,8}?)[」」]?(?:就好|吧|哦|啦|嘛|哈|了)?[。！？\s]*$",
            r"叫我[「「]?(.{1,8}?)[」」]?(?:就好|吧|哦|啦|嘛|哈|了)?[。！？\s]*$",
            r"可以叫我[「「]?(.{1,8}?)[」」]?[。！？\s]*$",
        ]
        for pat in patterns:
            m = re.search(pat, content)
            if m:
                nick = m.group(1).strip()
                if nick and len(nick) <= NICKNAME_MAX_LEN:
                    return nick
        return None


# ═══════════════════════════════════════════════════════════
