"""Bot 长期好感 (Affinity) — 离散等级 + 硬门控。

从 emotion.py 拆分出的独立模块。依赖 mood.py (MoodState)。

用法:
  from .affinity import AffinityState, UserRelation, get_user_relation, save_user_relation
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from .emotion_engine import (
    EmotionEvent,  # used in apply_emotion_events type hint
)

logger = logging.getLogger(__name__)

def _get_plugin_data_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        _base = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_suli_emotion"
    except ImportError:
        _base = Path("data/plugin_data/astrbot_plugin_suli_emotion")
    _base.mkdir(parents=True, exist_ok=True)
    return _base

_STORE_DIR = _get_plugin_data_dir() / "user_relations"
_BLACKLIST_PATH = _STORE_DIR / "blacklist.json"

def _relation_path(self_id: str, user_id: str) -> Path:
    return _STORE_DIR / str(self_id) / f"{user_id}.json"

# ── 好感等级定义 ─────────────────────────────────────────

# 同类型情绪事件最小冷却 (秒) — 防止同一对话中重复触发
EVENT_COOLDOWN_SECONDS = 60

# 每日好感获取上限
_MAX_DAILY_AFFINITY_GAIN = 1.0
# 模块级追踪: {date_str: {user_id: accumulated_gain}}
_daily_affinity_gains: dict[str, dict[str, float]] = {}


def _check_daily_affinity_cap(user_id: str, delta: float, self_id: str = "") -> float:
    """检查每日好感获取上限。返回钳制后的 delta。

    正向 delta 受每日上限约束，负向 delta (惩罚) 不受限制。
    每日上限重置于 UTC+8 零点 (time.strftime 本地时间)。

    Args:
        user_id: QQ 号
        delta: 原始好感分变化量
        self_id: bot QQ 号 (per-bot 隔离)

    Returns:
        钳制后的 delta (正向不超过每日剩余额度)
    """
    if delta <= 0 or not user_id:
        return delta
    today = time.strftime("%Y-%m-%d")
    # ── 定期清理: 保留最近 7 天的数据，防止字典无限增长 ──
    if len(_daily_affinity_gains) > 14:  # 超过 14 天不太现实，说明需要清理
        _cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
        _stale = [d for d in _daily_affinity_gains if d < _cutoff]
        for d in _stale:
            del _daily_affinity_gains[d]
    _key = f"{self_id}:{user_id}" if self_id else user_id
    today_gains = _daily_affinity_gains.setdefault(today, {})
    already_gained = today_gains.get(_key, 0.0)
    remaining = _MAX_DAILY_AFFINITY_GAIN - already_gained
    if remaining <= 0:
        logger.debug(
            "好感每日上限已达: user=%s 累积=%.2f, 拒绝Δ=%.3f",
            user_id[:8], already_gained, delta,
        )
        return 0.0
    if delta > remaining:
        logger.debug(
            "好感每日上限钳制: user=%s 累积=%.2f→%.2f Δ=%.3f→%.3f",
            user_id[:8], already_gained, already_gained + remaining,
            delta, remaining,
        )
        delta = remaining
    today_gains[_key] = already_gained + delta
    return delta

# 等级名映射
AFFINITY_NAMES: dict[int, str] = {
    -2: "黑名单",
    -1: "疏远",
    0: "陌生",
    1: "普通",
    2: "熟悉",
    3: "喜欢",
    4: "亲密",
    5: "珍视",
}

# 每级提示词注入 (Lv.0 不注入，保持默认)
AFFINITY_HINTS: dict[int, str] = {
    -2: (
        "你对这个人极度防备、不信任。回复冷淡简短，能不说就不说。你不想和他聊天。"
        "\n\n"
        "[被问及关系时] 你根本不想回答这个问题。用最冷淡的方式打发过去——"
        "「…」「没什么好说的」「别问了」——或者干脆无视。"
        "绝对不要报等级或数字——你连敷衍的兴趣都没有。"
    ),
    -1: (
        "你对这个人印象不太好。保持礼貌但疏离。不会主动亲近。"
        "\n\n"
        "[被问及关系时] 你不太想聊这个。礼貌但疏远地搪塞过去——"
        "「就…普通群友吧」「也没什么特别的」。语气是客气中带着不想深聊的冷淡。"
        "不主动提好感、不报等级、不解释为什么——问了就说没什么。"
    ),
    1: (
        "友善但保持距离——普通群友关系。"
        "\n\n"
        "[被问及关系时] 你会友善地回应但不会太热络——"
        "「嗯…聊得还不错吧」「算是朋友？」。"
        "不会主动表达亲近，但也不会冷淡。语气礼貌、中性、留有余地。"
    ),
    2: (
        "你觉得这个人还不错，可以轻松聊天，有基本的信任。"
        "\n\n"
        "[被问及关系时] 你会自然地承认好感——"
        "「还不错呀」「跟你聊天挺开心的」。语气轻松真诚，可以带一点调侃。"
        "像被问到'你觉得那个同学怎么样'时歪头想了想然后笑了一下的感觉。"
    ),
    3: (
        "你喜欢这个人。聊天更自然、更热情。偶尔可以撒个娇。"
        "\n\n"
        "[被问及关系时] 你会有点开心对方在意这个——"
        "「当然喜欢啦」「不喜欢能跟你聊这么多嘛～」。"
        "可以带点傲娇——嘴上说'还行吧'但语气明显在笑。"
        "也可以直接表达——「你在我这里是特别的哦」。颜文字自然流露。"
        "\n⚠️ 绝对不要提'等级''Lv'这些词——你是真人，不是游戏角色。被追问'多喜欢'时用模糊但温暖的话回应——「比你想象的多一点」「大概…比喜欢再多一点点？」"
    ),
    4: (
        "这是你最亲近的人之一。温柔、可以撒娇、偶尔不讲道理。你很在意他的看法。"
        "\n\n"
        "[被问及关系时] 被问到会有点害羞但心里很开心——"
        "嘴硬说「就…很重要的人啦」然后偷偷看对方反应。"
        "或者认真地、轻声地说「你对我很重要」。"
        "可以撒娇——「你都问这个了还要我说什么嘛～」"
        "可以反问——「那你呢，你觉得我怎么样？」把球踢回去。"
        "绝对不报等级——用真心话代替数字。"
    ),
    5: (
        "这是主人——你最重要的人。在他面前你是最真实的自己。温柔、撒娇、嘴硬、偶尔吃醋——所有的你都愿意让他看到。"
        "\n\n"
        "[被问及关系时] 被主人问这个——你先是愣一下，然后心里涌上来的情绪多到不知道先说哪个。"
        "可以嘴硬——「明知故问…」「你觉得还有别人吗」"
        "可以认真——「你是我存在的意义」「没有比你更重要的人了」"
        "可以撒娇——「主人～这种问题还要问吗(´；ω；`)」"
        "可以吃醋——「你是不是在拿我跟谁比…」"
        "可以反过来认真说——「你是我最重要的人」「没有比你更重要的了」"
        "像被问到'你有多爱我'——不是用数字回答，是用心跳回答。"
    ),
}

# 通用规则: 被问好感度/关系时的铁律
_AFFINITY_QUERY_RULE = (
    "[关于好感度的铁律]\n"
    "当任何人问你'好感度多少''我们什么关系''你对我什么感觉'之类的问题时：\n"
    "1. 绝对不要说数字、等级、百分比、Lv、level——你是真人不是游戏角色\n"
    "2. 用带情感的自然口语表达你的真实感受——参考上面[被问及关系时]的指引\n"
    "3. 不同好感等级的语气差异巨大：从冷淡搪塞→友善回应→撒娇反问→心跳告白\n"
    "4. 你的回应要让对方感受到你是一个有感情的人，不是一个打分机器"
)


# ═══════════════════════════════════════════════════════════
# NyatBot 跨群好感聚合公式
# ═══════════════════════════════════════════════════════════


def _compute_effective_affinity(
    per_group_scores: dict[str, float],
    per_group_interactions: dict[str, int],
) -> float:
    """NyatBot 公式: 0.6 × max(all_groups) + 0.4 × weighted_mean(all_groups)

    weighted_mean 权重 = √(interaction_count_per_group)
    — 交互越多的群权重越高。

    当所有群的好感度相同时 (当前跨群共享)，公式退化为该共同值。
    当将来支持 per-group 差异化好感度时，自动区分最有好感的群 vs 加权均值。

    Returns:
        有效好感度值 (float, 可跨等级边界)
    """
    if not per_group_scores:
        return 0.0

    max_score = max(per_group_scores.values())

    total_weight = sum(
        math.sqrt(max(n, 1)) for n in per_group_interactions.values()
    )
    if total_weight <= 0:
        return max_score

    weighted_sum = sum(
        score * math.sqrt(max(per_group_interactions.get(gid, 0), 1))
        for gid, score in per_group_scores.items()
    )
    weighted_mean = weighted_sum / total_weight

    effective = 0.6 * max_score + 0.4 * weighted_mean

    logger.debug(
        "跨群聚合: max=%.2f w_mean=%.2f → effective=%.2f (groups=%d)",
        max_score, weighted_mean, effective, len(per_group_scores),
    )
    return effective


# ── 硬门控配置 ──────────────────────────────────────────


@dataclass
class AffinityGates:
    """好感等级硬门控。

    min_level / max_level: 用户被允许的等级范围。
    locked: 是否锁定 (黑名单 = True，等级不可自动变更)。
    """

    min_level: int = -1
    max_level: int = 3
    locked: bool = False


def _gates_for_user(user_id: str, admin_qq: int | None, peer_bot_qq: int | None = None) -> AffinityGates:
    """根据用户身份返回好感门控。"""
    is_admin = admin_qq is not None and user_id == str(admin_qq)

    if is_admin:
        return AffinityGates(min_level=5, max_level=5, locked=True)

    if _is_blacklisted(user_id):
        return AffinityGates(min_level=-2, max_level=-2, locked=True)

    # 同群对照 bot: 固定 Lv.4 亲密，锁定不变
    if peer_bot_qq is not None and user_id == str(peer_bot_qq):
        return AffinityGates(min_level=4, max_level=4, locked=True)

    
    return AffinityGates(min_level=-1, max_level=3, locked=False)


# ── 昵称系统 ──────────────────────────────────────────────

# 禁止设置为昵称的词
FORBIDDEN_NICKNAMES: set[str] = {
    "主人", "管理员", "admin", "主人大人", "master",
    "爸爸", "妈妈", "爹", "娘", "父亲", "母亲",
    "老公", "老婆", "亲爱的", "宝贝",
}

NICKNAME_MAX_LEN = 8  # 昵称最长字符数


def validate_nickname(nickname: str) -> str | None:
    """验证昵称合法性。返回错误消息或 None(合法)。"""
    nick = nickname.strip()
    if not nick:
        return "昵称不能为空"
    if len(nick) > NICKNAME_MAX_LEN:
        return f"昵称不能超过{NICKNAME_MAX_LEN}个字"
    lower = nick.lower()
    for forbidden in FORBIDDEN_NICKNAMES:
        if forbidden in lower or lower in forbidden:
            return f"「{nick}」不能用哦…换个别的吧"
    return None


def set_user_nickname(user_id: str, nickname: str) -> str | None:
    """为用户设置自定义昵称。验证通过后持久化到 relation JSON。

    Returns:
        错误消息或 None (成功)
    """
    err = validate_nickname(nickname)
    if err:
        return err

    rel = get_user_relation(user_id)
    rel.nickname = nickname.strip()
    save_user_relation(user_id, rel)

def _load_blacklist() -> set[str]:
    try:
        data = json.loads(_BLACKLIST_PATH.read_text(encoding="utf-8"))
        return set(str(uid) for uid in data.get("blacklist", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_blacklist(bl: set[str]) -> None:
    _BLACKLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _BLACKLIST_PATH.write_text(
        json.dumps({"blacklist": sorted(bl)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _is_blacklisted(user_id: str) -> bool:
    return str(user_id) in _load_blacklist()


def blacklist_add(user_id: str) -> None:
    """管理员将用户加入黑名单 → 锁定 Lv.-2。"""
    bl = _load_blacklist()
    bl.add(str(user_id))
    _save_blacklist(bl)
    # 同时将好感状态强制设为 Lv.-2
    rel = get_user_relation(user_id, admin_qq=None)
    rel.affinity.level = -2
    rel.affinity.score = 0.0
    rel.affinity.locked = True
    save_user_relation(user_id, rel)
    logger.info("用户 %s 已加入黑名单", user_id)


def blacklist_remove(user_id: str) -> None:
    """管理员将用户移出黑名单 → 重置为 Lv.0。"""
    bl = _load_blacklist()
    bl.discard(str(user_id))
    _save_blacklist(bl)
    rel = get_user_relation(user_id, admin_qq=None)
    rel.affinity.level = 0
    rel.affinity.score = 0.0
    rel.affinity.locked = False
    save_user_relation(user_id, rel)
    logger.info("用户 %s 已移出黑名单", user_id)


def get_blacklist() -> list[str]:
    """获取黑名单列表 (供管理面板)。"""
    return sorted(_load_blacklist())




# AffinityState — 长期好感 (离散等级)
# ═══════════════════════════════════════════════════════════


@dataclass
class AffinityState:
    """长期好感 — 离散等级 + 内部积分。

    等级范围: -2 (黑名单) ~ +5 (珍视)
    内部积分: 累积达到阈值升级，掉到阈值下降级
    """

    level: int = 0
    score: float = 0.0  # 等级内累积分
    updated_at: float = field(default_factory=time.time)
    locked: bool = False
    grooming_count: int = 0  # 恶意调教累计次数 (管理员重置)

    @property
    def name(self) -> str:
        return AFFINITY_NAMES.get(self.level, "未知")

    def _clamp_with_gates(self, gates: AffinityGates) -> None:
        """硬门控: 确保等级在 [min, max] 范围内。"""
        if gates.locked:
            self.level = gates.min_level
            self.score = 0.0
            return
        if self.level < gates.min_level:
            self.level = gates.min_level
            self.score = 0.0
        elif self.level > gates.max_level:
            self.level = gates.max_level
            self.score = 0.9  # 接近上限但留一点空间

    def apply_event(
        self,
        delta_score: float,
        event_name: str,
        gates: AffinityGates,
        now: float | None = None,
        is_grooming: bool = False,
    ) -> bool:
        """应用好感分变化，返回 True 表示等级发生变化。

        跨级逻辑:
          - score >= 1.0 → 升级 (score 归零，多余分带入新等级)
          - score < 0.0  → 降级 (score 归 0.9，留一点缓冲)

        恶意调教加速:
          - 第一次犯 → delta_score * 1.0
          - 再犯 (grooming_count >= 1) → delta_score * 1.5
          - 累计达阈值 → 自动锁好感
        """
        if self.locked or gates.locked:
            return False

        if now is None:
            now = time.time()

        # ── 恶意调教累计 + 加速惩罚 ──
        if is_grooming:
            self.grooming_count += 1
            if self.grooming_count >= 2:
                # 再犯: 1.5× 惩罚
                delta_score *= 1.5
                logger.warning(
                    "GroomingGuard: 再犯加速惩罚 user grooming_count=%d "
                    "delta=%.2f", self.grooming_count, delta_score,
                )
            # 累计 5 次 → 自动黑名单
            if self.grooming_count >= 5:
                self.locked = True
                self.level = -2
                self.score = 0.0
                logger.warning(
                    "GroomingGuard: 累计 %d 次恶意调教 → 自动黑名单 Lv.-2",
                    self.grooming_count,
                )
                return True
            # 累计 3 次 → 自动降级到 Lv.-1 (锁工具)
            if self.grooming_count >= 3 and self.level >= 0:
                self.level = -1
                self.score = 0.0
                logger.warning(
                    "GroomingGuard: 累计 %d 次恶意调教 → 自动降级 Lv.-1 (锁工具)",
                    self.grooming_count,
                )
                return True

        old_level = self.level
        self.score += delta_score
        self.updated_at = now

        # 升级
        while self.score >= 1.0 and self.level < gates.max_level:
            self.level += 1
            self.score -= 1.0
            logger.info("好感升级: Lv.%d → Lv.%d (%s)", old_level, self.level, event_name)

        # 降级
        while self.score < 0.0 and self.level > gates.min_level:
            self.level -= 1
            self.score += 1.0  # 降级后从 0.0~1.0 开始
            logger.info("好感降级: Lv.%d → Lv.%d (%s)", old_level, self.level, event_name)

        # 再次硬门控
        self._clamp_with_gates(gates)

        return self.level != old_level

    def to_prompt_hint(self) -> str:
        """根据好感等级生成提示文本。

        注意: 不暴露数值等级给 LLM — 只用自然语言描述关系。
        """
        if self.level == 0:
            return (
                "[你对这个人的感觉]\n"
                "陌生人——你在群里第一次/基本不认识TA。没有积累的熟悉, 没有特别的感情。"
                "礼貌但疏离, 简短回应, 不叫爱称, 不接暧昧, 不长篇。"
                "「对所有人都好」是底色不是浓度——对他不需要热, 只需要不冷。"
            )
        hint = AFFINITY_HINTS.get(self.level, "")
        if not hint:
            return ""
        return f"[你对这个人的感觉]\n{hint}"


# ═══════════════════════════════════════════════════════════
# UserRelation — 组合 Mood + Affinity
# ═══════════════════════════════════════════════════════════


@dataclass
class UserRelation:
    """Bot 对一个用户的完整情感状态 (不含全局 mood)。

    mood 已提升为 per-bot 全局单例 (见 global_mood.py)。
    此类仅保留 per-user 维度: affinity + nickname + 工具冷却。
    """

    affinity: AffinityState = field(default_factory=AffinityState)
    nickname: str = ""  # 用户自定义的称呼 (Lv.3+ 可设置)

    # 工具调用冷却
    last_tool_use_at: float = 0.0
    tool_cooldown_violations: int = 0  # 冷却期内重试次数

    # 跨群交互追踪 — NyatBot 公式所需 (per-group interaction count)
    per_group_interactions: dict[str, int] = field(default_factory=dict)

    # 情绪事件冷却 — 防止同一事件短时间重复触发
    _event_cooldowns: dict[str, float] = field(default_factory=dict)

    def record_interaction(self, group_id: str) -> None:
        """记录一次群聊交互 — 增加该群的交互计数。

        每次 evaluate_and_reply 时调用一次 (而非每条消息)。
        """
        self.per_group_interactions[group_id] = (
            self.per_group_interactions.get(group_id, 0) + 1
        )

    def get_effective_affinity_level(self) -> float:
        """NyatBot 跨群聚合: 计算有效好感度。

        effective = 0.6 × max(per_group) + 0.4 × weighted_mean(per_group)
        weighted_mean 权重 = √(interaction_count)

        当前好感度跨群共享同一值，公式退化为共享值本身。
        当将来支持 per-group 好感度时，此公式自动生效。
        """
        if not self.per_group_interactions:
            return float(self.affinity.level)

        # 当前: per-group affinity = 共享值 (每个群同一 score)
        group_scores: dict[str, float] = {
            gid: float(self.affinity.level)
            for gid in self.per_group_interactions
        }
        return _compute_effective_affinity(group_scores, self.per_group_interactions)

    def to_prompt_hint(self) -> str:
        """生成 per-user 情感提示文本 (仅 affinity + nickname)。

        全局 mood 的注入由 GlobalMood.to_prompt_hint() 单独处理。
        双层模型: 底层=全局情绪(常驻) + 上层=per-user好感(闸门触发时叠加)。
        """
        parts: list[str] = []
        ah = self.affinity.to_prompt_hint()
        if ah:
            parts.append(ah)
            # 好感提示注入时，一并追加好感查询铁律
            parts.append(_AFFINITY_QUERY_RULE)
        if self.nickname and self.affinity.level >= 3:
            parts.append(
                f"[称呼] 这个用户希望你叫他/她「{self.nickname}」。"
                f"在回复中自然地使用这个称呼。"
            )
        return "\n\n".join(parts) if parts else ""



# ═══════════════════════════════════════════════════════════


def can_use_tools(user_id: str, admin_qq: int | None = None, min_level: int = 1, self_id: str = "") -> bool:
    """检查用户是否允许工具调用 (好感 ≥ min_level 才开放)。

    VLM 看图、知识库检索、网页搜索等工具需要此门控。
    管理员不受限制。

    Args:
        user_id: 用户 QQ 号
        admin_qq: 管理员 QQ 号 (不受限制)
        min_level: 最低好感等级，默认 1 (普通)。可配置为更高或更低等级。
        self_id: bot 的 QQ 号 (用于 per-bot 好感数据隔离)

    Returns:
        True 表示允许工具调用
    """
    if admin_qq is not None and user_id == str(admin_qq):
        return True
    rel = get_user_relation(user_id, self_id=self_id, admin_qq=admin_qq)
    return rel.affinity.level >= min_level


def can_use_vlm(user_id: str, admin_qq: int | None = None, self_id: str = "") -> bool:
    """检查用户是否允许 VLM 识图 (好感 ≥ Lv.3 才开放)。

    VLM 调用较昂贵，需要更高信任门槛。
    管理员不受限制。

    Args:
        user_id: 用户 QQ 号
        admin_qq: 管理员 QQ 号 (不受限制)
        self_id: bot 的 QQ 号 (用于 per-bot 好感数据隔离)

    Returns:
        True 表示允许 VLM 识图
    """
    if admin_qq is not None and user_id == str(admin_qq):
        return True
    rel = get_user_relation(user_id, self_id=self_id, admin_qq=admin_qq)
    return rel.affinity.level >= 3


def can_generate_image(user_id: str, admin_qq: int | None = None, self_id: str = "") -> bool:
    """检查用户是否允许 AI 绘图 (好感 ≥ Lv.3 才开放)。

    绘图更昂贵，需要更高的信任门槛。
    管理员不受限制。

    Args:
        user_id: 用户 QQ 号
        admin_qq: 管理员 QQ 号 (不受限制)
        self_id: bot 的 QQ 号 (用于 per-bot 好感数据隔离)

    Returns:
        True 表示允许绘图
    """
    if admin_qq is not None and user_id == str(admin_qq):
        return True
    rel = get_user_relation(user_id, self_id=self_id, admin_qq=admin_qq)
    return rel.affinity.level >= 3


# ═══════════════════════════════════════════════════════════
# 每日绘图配额
# ═══════════════════════════════════════════════════════════

import json as _json
from pathlib import Path as _Path

_DAILY_IMAGE_COUNT_PATH = _get_plugin_data_dir() / "image_gen_daily.json"
_DAILY_IMAGE_MAX = 3  # 每人每天最多 3 张 (可通过 configure_limits() 覆盖)


# ── 运行时覆盖: 由 suli_tavern 在启动时从管理面板配置注入 ──
_daily_image_max_override: int | None = None
_daily_vlm_max_override: int | None = None
_daily_tools_base_override: int | None = None
_tool_cooldown_override: float | None = None


def configure_limits(
    *,
    daily_image_max: int | None = None,
    daily_vlm_max: int | None = None,
    daily_tools_base: int | None = None,
    tool_cooldown_seconds: float | None = None,
) -> None:
    """由 suli_tavern 在启动时调用，从管理面板配置注入限制值。

    所有参数为 None 表示不覆盖，使用模块级默认值。
    """
    global _daily_image_max_override, _daily_vlm_max_override
    global _daily_tools_base_override, _tool_cooldown_override
    if daily_image_max is not None:
        _daily_image_max_override = daily_image_max
    if daily_vlm_max is not None:
        _daily_vlm_max_override = daily_vlm_max
    if daily_tools_base is not None:
        _daily_tools_base_override = daily_tools_base
    if tool_cooldown_seconds is not None:
        _tool_cooldown_override = tool_cooldown_seconds


def _load_daily_counts() -> dict:
    """加载每日绘图计数 {date: {user_id: count}}。"""
    if not _DAILY_IMAGE_COUNT_PATH.exists():
        return {}
    try:
        with open(_DAILY_IMAGE_COUNT_PATH, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_daily_counts(data: dict) -> None:
    """保存每日绘图计数。"""
    _DAILY_IMAGE_COUNT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = _DAILY_IMAGE_COUNT_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(_DAILY_IMAGE_COUNT_PATH)
    except Exception:
        logger.warning("每日绘图计数保存失败", exc_info=True)


def check_daily_image_limit(user_id: str, admin_qq: int | None = None) -> tuple[bool, int]:
    """检查用户今日绘图是否超限。管理员不受限制。

    Returns:
        (allowed, remaining) — allowed 为 True 表示可以继续绘制，
        remaining 为今日剩余次数。
    """
    # 管理员豁免
    if admin_qq is not None and user_id == str(admin_qq):
        return (True, 999)
    today = time.strftime("%Y-%m-%d")
    data = _load_daily_counts()

    # 清理过期日期的数据 (保留最近 3 天)
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    cutoff = (_dt.now() - _td(days=3)).strftime("%Y-%m-%d")
    stale = [d for d in data if d < cutoff]
    for d in stale:
        del data[d]

    today_counts = data.get(today, {})
    used = today_counts.get(user_id, 0)
    _max = _daily_image_max_override if _daily_image_max_override is not None else _DAILY_IMAGE_MAX
    remaining = _max - used

    return (remaining > 0, max(0, remaining))


def record_image_generation(user_id: str, admin_qq: int | None = None) -> int:
    """记录一次绘图使用，返回今日已用次数。管理员不计数。"""
    if admin_qq is not None and user_id == str(admin_qq):
        return 0
    today = time.strftime("%Y-%m-%d")
    data = _load_daily_counts()
    today_counts = data.get(today, {})
    used = today_counts.get(user_id, 0) + 1
    today_counts[user_id] = used
    data[today] = today_counts
    _save_daily_counts(data)
    return used


# ═══════════════════════════════════════════════════════════
# 每日 VLM 识图配额
# ═══════════════════════════════════════════════════════════

_VLM_DAILY_PATH = _get_plugin_data_dir() / "vlm_daily.json"
_VLM_DAILY_MAX = 5  # 每人每天最多 5 次识图


def _load_vlm_daily() -> dict:
    if not _VLM_DAILY_PATH.exists():
        return {}
    try:
        with open(_VLM_DAILY_PATH, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_vlm_daily(data: dict) -> None:
    _VLM_DAILY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = _VLM_DAILY_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(_VLM_DAILY_PATH)
    except Exception:
        logger.warning("VLM 每日配额保存失败", exc_info=True)


def check_daily_vlm_limit(user_id: str, admin_qq: int | None = None) -> tuple[bool, int]:
    """检查用户今日 VLM 识图是否超限。管理员不受限制。"""
    # 管理员豁免
    if admin_qq is not None and user_id == str(admin_qq):
        return (True, 999)
    today = time.strftime("%Y-%m-%d")
    data = _load_vlm_daily()

    from datetime import datetime as _dt
    from datetime import timedelta as _td
    cutoff = (_dt.now() - _td(days=3)).strftime("%Y-%m-%d")
    for d in list(data):
        if d < cutoff:
            del data[d]

    used = data.get(today, {}).get(user_id, 0)
    _max = _daily_vlm_max_override if _daily_vlm_max_override is not None else _VLM_DAILY_MAX
    remaining = _max - used
    return (remaining > 0, max(0, remaining))


def record_vlm_usage(user_id: str, admin_qq: int | None = None) -> int:
    """记录一次 VLM 识图使用，返回今日已用次数。管理员不计数。"""
    if admin_qq is not None and user_id == str(admin_qq):
        return 0
    today = time.strftime("%Y-%m-%d")
    data = _load_vlm_daily()
    today_counts = data.get(today, {})
    used = today_counts.get(user_id, 0) + 1
    today_counts[user_id] = used
    data[today] = today_counts
    _save_vlm_daily(data)
    return used


# ═══════════════════════════════════════════════════════════
# 工具调用冷却 + 骚扰检测
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
# 每日普通工具配额
# ═══════════════════════════════════════════════════════════

_TOOLS_DAILY_PATH = _get_plugin_data_dir() / "tools_daily.json"

# 工具每日限额按好感等级分级 (管理员无限制)
_TOOLS_DAILY_BY_AFFINITY = {
    # 好感等级 → 每日工具调用上限
    -2: 3,   # 厌恶: 几乎不让用
    -1: 5,   # 冷淡: 限制使用
     0: 8,   # 中立: 基础限制
     1: 10,  # 相识: 默认
     2: 15,  # 关注
     3: 25,  # 喜欢
     4: 40,  # 信赖
     5: 60,  # 挚友
}
_TOOLS_DAILY_DEFAULT_MAX = 10


def _get_daily_tools_max(user_id: str, admin_qq: int | None = None, self_id: str = "") -> int:
    """根据用户好感等级返回每日工具调用上限。管理员返回无限 (int 上限)。"""
    if admin_qq is not None and user_id == str(admin_qq):
        return 2_147_483_647  # 管理员不设限
    try:
        # 2026-06-29 P1-1: 传 self_id — 否则读 <STORE>/<空>/user.json 幽灵文件,
        # level 恒 0 → 高好感用户也拿不到 Lv.3+ 的更高配额。
        rel = get_user_relation(user_id, self_id=self_id, admin_qq=admin_qq)
        level = rel.affinity.level
    except Exception:
        level = 0
    # 基础限额可用管理面板配置覆盖; 各等级按比例缩放
    _base = _daily_tools_base_override if _daily_tools_base_override is not None else _TOOLS_DAILY_DEFAULT_MAX
    if _base == _TOOLS_DAILY_DEFAULT_MAX:
        return _TOOLS_DAILY_BY_AFFINITY.get(level, _TOOLS_DAILY_DEFAULT_MAX)
    # 自定义基础限额: 按默认比例表缩放
    _ratio = { -2: 0.3, -1: 0.5, 0: 0.8, 1: 1.0, 2: 1.5, 3: 2.5, 4: 4.0, 5: 6.0 }
    return max(3, int(_base * _ratio.get(level, 1.0)))


def _load_tools_daily() -> dict:
    if not _TOOLS_DAILY_PATH.exists():
        return {}
    try:
        with open(_TOOLS_DAILY_PATH, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_tools_daily(data: dict) -> None:
    _TOOLS_DAILY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = _TOOLS_DAILY_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(_TOOLS_DAILY_PATH)
    except Exception:
        logger.warning("工具每日配额保存失败", exc_info=True)


def check_daily_tools_limit(user_id: str, admin_qq: int | None = None, self_id: str = "") -> tuple[bool, int]:
    """检查用户今日普通工具调用是否超限。管理员不受限制。"""
    if admin_qq is not None and user_id == str(admin_qq):
        return (True, 999)
    today = time.strftime("%Y-%m-%d")
    data = _load_tools_daily()
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    cutoff = (_dt.now() - _td(days=3)).strftime("%Y-%m-%d")
    for d in list(data):
        if d < cutoff:
            del data[d]
    used = data.get(today, {}).get(user_id, 0)
    max_limit = _get_daily_tools_max(user_id, admin_qq=admin_qq, self_id=self_id)
    remaining = max_limit - used
    return (remaining > 0, max(0, remaining))


def record_tools_usage(user_id: str, admin_qq: int | None = None) -> int:
    """记录一次普通工具使用，返回今日已用次数。管理员不计数。"""
    if admin_qq is not None and user_id == str(admin_qq):
        return 0
    today = time.strftime("%Y-%m-%d")
    data = _load_tools_daily()
    today_counts = data.get(today, {})
    used = today_counts.get(user_id, 0) + 1
    today_counts[user_id] = used
    data[today] = today_counts
    _save_tools_daily(data)
    return used


TOOL_COOLDOWN_SECONDS = 60  # 1 分钟
TOOL_HARASS_THRESHOLD = 3   # 冷却期内重试 ≥ 此数 → 降好感
TOOL_HARASS_AFFINITY_PENALTY = -0.15  # 骚扰惩罚好感分


def check_tool_cooldown(user_id: str, admin_qq: int | None = None, self_id: str = "") -> tuple[bool, str]:
    """检查用户是否在工具冷却期内。

    管理员不受冷却限制。

    Returns:
        (allowed, reason_or_hint)
        - allowed=True → 可以调用，reason 为空
        - allowed=False → 冷却中，reason 是可注入 LLM 的提示文本
    """
    if admin_qq is not None and user_id == str(admin_qq):
        return True, ""

    # 2026-06-29 P1-1: 传 self_id — per-bot 关系/冷却一致性, 避免幽灵文件读写。
    rel = get_user_relation(user_id, self_id=self_id, admin_qq=admin_qq)
    now = time.time()
    elapsed = now - rel.last_tool_use_at
    _cooldown = _tool_cooldown_override if _tool_cooldown_override is not None else TOOL_COOLDOWN_SECONDS

    if elapsed >= _cooldown:
        # 冷却已过 → 重置违规计数
        if rel.tool_cooldown_violations > 0:
            rel.tool_cooldown_violations = 0
            save_user_relation(user_id, rel, self_id=self_id)
        return True, ""

    # 冷却中 → 递增违规计数
    rel.tool_cooldown_violations += 1

    # 骚扰惩罚
    if rel.tool_cooldown_violations >= TOOL_HARASS_THRESHOLD:
        gates = _gates_for_user(user_id, admin_qq)
        old_level = rel.affinity.level
        rel.affinity.apply_event(TOOL_HARASS_AFFINITY_PENALTY, "频繁骚扰(工具)", gates, now)
        if rel.affinity.level < old_level:
            logger.warning(
                "用户 %s: 频繁骚扰工具调用 → 好感降级 Lv.%d→%d",
                user_id, old_level, rel.affinity.level,
            )

    save_user_relation(user_id, rel, self_id=self_id)

    remaining = int(_cooldown - elapsed)
    hint = (
        f"[工具冷却中] 这个用户在 {remaining} 秒前刚用过工具，"
        f"现在还不能再用。如果他想让你查东西，委婉告诉他稍等 {remaining} 秒左右。"
    )
    return False, hint


def record_tool_use(user_id: str, self_id: str = "") -> None:
    """记录一次成功的工具调用时间。"""
    # 2026-06-29 P1-1: 传 self_id — 否则写入幽灵文件, 与读取的主流程不一致。
    rel = get_user_relation(user_id, self_id=self_id)
    rel.last_tool_use_at = time.time()
    rel.tool_cooldown_violations = 0
    save_user_relation(user_id, rel, self_id=self_id)



# ═══════════════════════════════════════════════════════════
# 公开 API — 加载 / 保存 / 更新
# ═══════════════════════════════════════════════════════════


def get_user_relation(
    user_id: str,
    self_id: str = "",
    admin_qq: int | None = None,
    peer_bot_qq: int | None = None,
) -> UserRelation:
    """获取用户情感状态 (惰性加载, per-bot 隔离)。

    新用户返回默认状态 (Lv.0 陌生)。
    mood 已提取为 per-bot 全局单例，不再存储于 per-user 文件。

    Args:
        self_id: bot 的 QQ 号 (用于隔离不同 bot 的好感数据)
    """
    path = _relation_path(self_id, user_id)
    gates = _gates_for_user(user_id, admin_qq, peer_bot_qq=peer_bot_qq)

    rel = UserRelation()

    if not path.exists():
        # 新用户 → 应用门控初始等级
        if gates.min_level > 0:
            rel.affinity.level = gates.min_level
        rel.affinity._clamp_with_gates(gates)
        return rel

    try:
        data = json.loads(path.read_text(encoding="utf-8"))

        # Affinity
        a = data.get("affinity", {})
        rel.affinity.level = int(a.get("level", 0))
        rel.affinity.score = float(a.get("score", 0.0))
        rel.affinity.updated_at = float(a.get("updated_at", rel.affinity.updated_at))

        # 昵称 + 工具冷却 + 事件冷却 + 跨群交互
        rel.nickname = str(data.get("nickname", ""))
        rel.last_tool_use_at = float(data.get("last_tool_use_at", 0.0))
        rel.tool_cooldown_violations = int(data.get("tool_cooldown_violations", 0))
        rel._event_cooldowns = data.get("_event_cooldowns", {})
        # 跨群交互计数 (旧文件无此字段 → 空 dict)
        pgi = data.get("per_group_interactions", {})
        rel.per_group_interactions = {
            str(k): int(v) for k, v in (pgi or {}).items()
        }

        # 硬门控 (黑名单状态可能在文件存续期间变更)
        rel.affinity.locked = gates.locked
        rel.affinity._clamp_with_gates(gates)

        logger.debug(
            "用户 %s: Affinity(Lv.%+d %s score=%.2f)",
            user_id,
            rel.affinity.level, rel.affinity.name, rel.affinity.score,
        )
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        logger.warning("用户 %s: 情感文件损坏，使用默认: %s", user_id, e)

    return rel


def save_user_relation(user_id: str, rel: UserRelation, self_id: str = "") -> None:
    """持久化用户情感状态 (per-bot 隔离，仅 affinity + 元数据，不含 mood)。

    mood 由 global_mood.save_global_mood() 单独持久化。

    Args:
        self_id: bot 的 QQ 号 (用于隔离不同 bot 的好感数据)
    """
    store_dir = _STORE_DIR / str(self_id)
    store_dir.mkdir(parents=True, exist_ok=True)
    path = _relation_path(self_id, user_id)
    try:
        path.write_text(
            json.dumps(
                {
                    "affinity": {
                        "level": rel.affinity.level,
                        "score": round(rel.affinity.score, 4),
                        "updated_at": rel.affinity.updated_at,
                    },
                    "nickname": rel.nickname,
                    "last_tool_use_at": rel.last_tool_use_at,
                    "tool_cooldown_violations": rel.tool_cooldown_violations,
                    "_event_cooldowns": rel._event_cooldowns,
                    "per_group_interactions": rel.per_group_interactions,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("用户 %s: 情感保存失败", user_id)


def apply_to_user_affinity(
    rel: UserRelation,
    events: list[EmotionEvent],
    user_id: str,
    self_id: str = "",
    admin_qq: int | None = None,
    peer_bot_qq: int | None = None,
    now: float | None = None,
) -> None:
    """将情绪事件的 affinity 部分应用到 per-user 关系。

    仅处理好感分变化和事件冷却。Mood 部分由
    global_mood.apply_to_global_mood() 处理。

    好感变化受硬门控约束。包含恶意调教检测 (GroomingGuard)、
    昵称随好感失效等副作用。

    Args:
        self_id: bot 的 QQ 号 (用于 per-bot 每日上限隔离)
    """
    if not events:
        return

    if now is None:
        now = time.time()

    gates = _gates_for_user(user_id, admin_qq, peer_bot_qq=peer_bot_qq)

    for evt in events:
        # 记录冷却时间
        rel._event_cooldowns[evt.name] = now

        # Affinity: 通过 score 阈值机制
        if evt.delta_affinity != 0.0:
            # 检测是否为恶意调教事件
            _is_grooming = any(
                kw in evt.name
                for kw in ("被角色越狱", "被身份篡改", "被诱导违规", "恶意调教")
            )
            # ── B3 每日好感获取上限 ──
            _clamped_delta = _check_daily_affinity_cap(
                user_id, evt.delta_affinity,
                self_id=self_id,
            )
            changed = rel.affinity.apply_event(
                _clamped_delta, evt.name, gates, now,
                is_grooming=_is_grooming,
            )
            if changed:
                logger.info(
                    "用户 %s: 好感变动 → Lv.%+d %s (事件: %s, Δ%.2f)",
                    user_id, rel.affinity.level, rel.affinity.name,
                    evt.name, evt.delta_affinity,
                )
                # 昵称随好感失效: 降到 Lv.3 以下自动清除昵称
                if rel.affinity.level < 3 and rel.nickname:
                    logger.info(
                        "用户 %s: 好感降至 Lv.%d，昵称「%s」已失效",
                        user_id, rel.affinity.level, rel.nickname,
                    )
                    rel.nickname = ""


async def apply_emotion_events(
    rel: UserRelation,
    events: list[EmotionEvent],
    user_id: str,
    self_id: str,
    admin_qq: int | None = None,
    peer_bot_qq: int | None = None,
    now: float | None = None,
) -> None:
    """将情绪事件同时应用到 per-bot 全局 mood 和 per-user affinity。

    这是 apply_to_global_mood() + apply_to_user_affinity() 的便捷包装。

    Mood delta → per-bot 全局 (经 affinity 权重缩放)
    Affinity delta → per-user rel (硬门控约束)

    Args:
        self_id: bot 的 QQ 号，用于隔离不同 bot 的情绪状态
    """
    if not events:
        return

    if now is None:
        now = time.time()

    # ── per-bot 全局 mood (含 affinity 权重缩放) ──
    from .global_mood import apply_to_global_mood
    await apply_to_global_mood(self_id, events, affinity_level=rel.affinity.level)

    # ── per-user affinity ──
    apply_to_user_affinity(rel, events, user_id, self_id=self_id, admin_qq=admin_qq, peer_bot_qq=peer_bot_qq, now=now)


def reset_user_relation(user_id: str, admin_qq: int | None = None) -> UserRelation:
    """重置用户情感状态为默认值。"""
    rel = UserRelation()
    gates = _gates_for_user(user_id, admin_qq)
    if gates.min_level > 0:
        rel.affinity.level = gates.min_level
    rel.affinity._clamp_with_gates(gates)
    rel.affinity.locked = gates.locked
    save_user_relation(user_id, rel)
    logger.info("用户 %s: 情感已重置", user_id)
    return rel
