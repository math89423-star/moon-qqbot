"""荷尔蒙曲线 (Hormone) — per-(bot, user) 的成人向驱动状态。

与既有双轨的分工:

  GlobalMood (per-bot)      — 心情: 被外部事件推着走, 半衰期 30min/10min
  Affinity   (per-user)     — 好感: 长期门控 (-2~+5 离散等级)
  Hormone    (bot x user)   — 荷尔蒙: **自己会长、会忍住、会破** 的驱动层 (本模块)

三者不是替代关系。Mood 回答「她现在什么脸色」, Affinity 回答「她允许你走到哪一步」,
Hormone 回答「她现在有多想要、还能忍多久、下一步是她主动还是等人推」。

═══════════════════════════════════════════════════════════
四变量模型
═══════════════════════════════════════════════════════════

  heat        慢变量 (半衰期 15min↑ / 60min↓) — 情欲水位。由 libido 目标牵引,
              场景加热, 无满足时缓慢自渗。决定「阈值有多低」。
  pressure    快变量 (半衰期 7min)          — 场景内蓄积。刺激注入, 时间退火,
              越热注入效率越高。决定「还有多久忍不住」。
  satiety     半衰期 40min                 — 满足/贤者。释放后跳高, 乘性抑制一切。
  frustration 半衰期 90min                 — 未满足/憋屈。中断与反复撩拨不放会涨,
              决定「她主动开口时的动机」(黏人 / 嘴硬 / 冷脸索要)。

核心公式 (全部是纯函数, 可离线仿真):

  libido    = base_libido x cycle(t) x diurnal(t) x affinity_factor(aff) x (1+owner_bonus)
  heat      ← 向 libido 松弛 (升快降慢, 非对称)
  pressure  += gain(kind) x intensity x sens,   sens = (1-satiety) x (0.55 + 0.45*heat)
  pressure  *= 0.5^(Δt / pressure_hl)           ← 时间退火
  drive     = max(pressure*(0.55+0.45*heat), 0.8*heat) x (1 - 0.9*satiety)
  theta (决堤阈值) = threshold_base - threshold_heat_relief*heat

═══════════════════════════════════════════════════════════
七区 → QQ 行为授权
═══════════════════════════════════════════════════════════

  平静 → 起意 → 撩拨 → 升温 → 临界 → 决堤 → 余韵

每一区解出一组**输出旋钮**, 而不是一句形容词:

  burst_max        本轮最多发几条 (替代群聊硬编码的 20% 概率连发)
  gap_s            条间隔 (呼吸急促则更短)
  split            长回复是否按句切碎发送
  onoma            拟声/气声档位 0~3
  escalate         是否允许不等对方推进, 自己先推进一档
  solicit          是否必须开口索求 (pressure ≥ θ 时强制)
  proactive_weight 主动消息意愿乘数 (注入 suli_proactive 决策门控)

═══════════════════════════════════════════════════════════
边界 (不可混淆)
═══════════════════════════════════════════════════════════

1. 荷尔蒙**不开门**。NSFW Zone 三重门控 (bot 开关 + 群白名单 + 人白名单) 是门,
   荷尔蒙只在门内决定「怎么演」。门关 → 曲线照走但输出恒为 neutral。
2. 好感硬门槛不豁免。affinity < min_affinity → affinity_factor = 0 → 曲线起不来。
3. safety 硬线 (CSAM / 未成年 / 性暴力 / 解除伦理) 在荷尔蒙**之前**判定,
   荷尔蒙不参与、不影响、不放宽任何一条。
4. 关系状态机 (BACKOFF/REFUSING/HURT) 是硬否决: 由调用方乘 0 处理。

用法:
  from astrbot_plugin_suli_emotion import HormoneState, HormoneSignal, classify_text

  st = load_hormone_state("BOT_QQ", "USER_QQ")
  st.decay(now, TUNING)
  sig = classify_text("……进来吧")            # → HormoneSignal(kind="explicit", intensity=.85)
  if sig: st.apply(sig, now, TUNING, affinity_level=3)
  read = st.read(now=now, tuning=TUNING)      # → HormoneRead(zone="升温", burst_max=3, ...)
  print(render_hormone_hint(read))
  st.save()
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────────

MAX_EVENT_HISTORY = 60
SCHEMA_VERSION = 1

ZONE_ORDER: tuple[str, ...] = (
    "calm", "tempted", "tease", "build", "edge", "release", "afterglow",
)

ZONE_LABELS: dict[str, str] = {
    "calm": "平静",
    "tempted": "起意",
    "tease": "撩拨",
    "build": "升温",
    "edge": "临界",
    "release": "决堤",
    "afterglow": "余韵",
}

# 事件类型 → pressure 注入增益 (权重, 不是绝对值; 最终还要乘 intensity 与 sens)
# 「实接触」— 只有这一档能安抚憋屈; 抱一下/夸一句不算解渴
REAL_KINDS: frozenset[str] = frozenset({"explicit", "command"})

SIGNAL_GAIN: dict[str, float] = {
    "intimate": 0.12,    # 含蓄亲密: 抱/亲/摸
    "tease": 0.18,       # 挑逗: 想让对方开口
    "explicit": 0.28,    # 露骨推进  (~0.15 实注/轮 @热态 → 10 轮内进临界)
    "command": 0.20,     # 命令/支配句式
    "praise": 0.08,      # 夸奖/示爱 (含水量低但积少成多)
    "self_out": 0.09,    # bot 自己产出的露骨内容 (自我反馈)
}

# 特殊事件 — 不走增益表, 走各自的跳变逻辑
SIGNAL_CLIMAX = "climax"
SIGNAL_INTERRUPT = "interrupt"
SIGNAL_COOLDOWN = "cooldown"
SIGNAL_AFTERCARE = "aftercare"
SPECIAL_KINDS: frozenset[str] = frozenset({
    SIGNAL_CLIMAX, SIGNAL_INTERRUPT, SIGNAL_COOLDOWN, SIGNAL_AFTERCARE,
})

# 关键词银行 — 匹配规则与世界书 keys 一致: **纯子串**, 因此一律 ≥2 字。
# 单字 key 是毒药 (「口」会命中接口/入口/开口), 本项目已在 NSFW 世界书踩过。
KEY_BANK: tuple[tuple[str, tuple[str, ...], float], ...] = (
    (SIGNAL_CLIMAX, (
        "高潮了", "要去了", "我要去了", "去了", "射了", "射出来", "出来了",
        "不行了", "要坏了", "忍不住了", "到顶了", "缴械",
    ), 1.0),
    (SIGNAL_COOLDOWN, (
        "停下", "别碰", "不要了", "今天算了", "没心情", "不想做", "够了",
        "别过来", "松手", "不要这样",
    ), 1.0),
    (SIGNAL_INTERRUPT, (
        "等我一下", "去洗澡", "去接个电话", "有点事", "先忙", "上个厕所",
        "出去一下", "待会再说", "睡了啊", "我去睡", "开会去了", "有人来了",
    ), 0.7),
    (SIGNAL_AFTERCARE, (
        "抱一下", "累不累", "睡吧", "早点休息", "休息一下", "擦擦",
        "亲一下", "抱着睡", "乖啊",
    ), 0.8),
    ("explicit", (
        "插进来", "进来", "顶到", "抽送", "舔", "含住", "套弄", "交合", "结合",
        "呻吟", "湿透", "硬了", "夹紧", "口交", "足交", "乳交", "后入", "上位",
        "背入", "拔出来", "骑上来", "张开腿", "摸下面", "脱掉", "解扣",
    ), 0.85),
    ("command", (
        "命令", "过来", "趴下", "跪", "别动", "张嘴", "转过去", "自己来",
        "动手", "抬头", "看着这里", "叫一声", "数着", "报数",
    ), 0.7),
    ("tease", (
        "想要", "想不想", "求我", "说给我听", "撩", "挑逗", "勾引", "舔唇",
        "听话", "乖不乖", "憋着", "不准", "忍着",
    ), 0.55),
    ("intimate", (
        "抱着", "抱紧", "吻", "亲", "摸", "搂", "贴着", "蹭", "靠着",
        "额头", "咬耳朵", "搂住", "牵手", "趴上去", "缠着",
    ), 0.45),
    ("praise", (
        "喜欢你", "好可爱", "好乖", "真棒", "乖孩子", "好漂亮", "好帅",
        "好香", "好软", "好想你", "离不开",
    ), 0.35),
)


# ── 可调参数 ────────────────────────────────────────────────


@dataclass
class HormoneTuning:
    """曲线参数。全部可被 per-bot 配置覆盖 (面板 → bot_config → 本对象)。"""

    # 内在基线
    base_libido: float = 0.34
    cycle_days: float = 7.0            # 周期长度 (0 = 关闭周期)
    cycle_amplitude: float = 0.45      # 周期摆幅
    cycle_offset: float = 0.0          # per-bot 相位偏移 (两 bot 不同步)
    diurnal_amplitude: float = 0.30    # 昼夜摆幅 (峰值深夜)

    # 时间常数 (秒)
    heat_rise_hl: float = 900.0
    heat_fall_hl: float = 3600.0
    pressure_hl: float = 420.0
    satiety_hl: float = 2400.0
    frustration_hl: float = 5400.0

    # 空转自渗 (没人在撩, 但她在等)
    seep_rate: float = 0.010           # 每分钟注入量 (heat 越高越快)
    seep_cap: float = 0.35             # 自渗上限 — 防止永久停在高位

    # 环境项 (无互动时水位能顶到哪一区): ambient = ambient_scale * heat^ambient_exp
    ambient_scale: float = 0.55
    ambient_exp: float = 1.5

    # 干渴累积 (长时间没被满足 → 憋屈)
    dry_hours: float = 30.0            # 距上次释放多久开始积
    dry_creep: float = 0.012           # 每分钟注入量 (@heat=1) — 调大 = 更饥渴
    dry_ramp_hours: float = 72.0       # 过了 dry_hours 之后, 憋屈爬满所需小时数
    dry_floor: float = 0.70            # 时间项上限 (与周期无关的「越久越憋」地板)

    # 阈值
    threshold_base: float = 0.62
    threshold_heat_relief: float = 0.18
    release_window: float = 150.0      # 决堤态持续时间
    afterglow_satiety: float = 0.55    # 余韵触发线
    edge_hold: float = 180.0           # 蓄积过阈值持续这么久 → 自然决堤
    resume_window: float = 3600.0      # 场景中断后多久内仍算「欠着一次」

    # 门槛与配额
    min_affinity: int = 2              # 好感硬门槛 — 低于此值曲线起不来
    owner_bonus: float = 0.18          # 主人加成
    burst_daily_cap: int = 90          # 每日连发总条数上限 (token 兜底)
    solicit_daily_cap: int = 3         # 每日主动索求上限
    release_daily_cap: int = 3         # 每日决堤上限 (超出转假性满足)

    # 自我反馈
    self_gain_cap: float = 0.12        # 单轮自反馈注入上限
    scene_gap_close: float = 1800.0    # 超过此静默 → 视为场景结束


DEFAULT_TUNING = HormoneTuning()


# ── 区域表 (zone → 输出旋钮) ─────────────────────────────────
# lo/hi 是 drive 区间; afterglow 由 satiety 触发, 区间不参与匹配 (0/0)。
# split: 长回复是否按句切碎发送。


@dataclass(frozen=True, slots=True)
class ZoneRow:
    key: str
    label: str
    lo: float
    hi: float
    burst_max: int
    burst_p: float
    gap_s: float
    frag_max: int
    onoma: int
    escalate: bool
    solicit: bool
    proactive_weight: float


ZONE_TABLE: tuple[ZoneRow, ...] = (
    # 两个数量是分开的, 别混:
    #   burst_max  = 追加补充条数 (情欲驱动: 越热越想补一句)
    #   frag_max   = 单条回复被切成几条发 (打字节奏, 不是情欲 — 真人打长段本来就会断几条)
    # key          label    lo    hi   burst 追加p gap  分片 拟声 推进 索求 主动权重
    ZoneRow(
        key="calm", label="平静", lo=0.00, hi=0.10,
        burst_max=1, burst_p=0.15, gap_s=2.1, frag_max=2, onoma=0,
        escalate=False, solicit=False, proactive_weight=0.35,
    ),
    ZoneRow(
        key="tempted", label="起意", lo=0.10, hi=0.30,
        burst_max=2, burst_p=0.35, gap_s=2.0, frag_max=3, onoma=0,
        escalate=False, solicit=False, proactive_weight=0.70,
    ),
    ZoneRow(
        key="tease", label="撩拨", lo=0.30, hi=0.48,
        burst_max=2, burst_p=0.60, gap_s=1.6, frag_max=3, onoma=1,
        escalate=False, solicit=False, proactive_weight=1.10,
    ),
    ZoneRow(
        key="build", label="升温", lo=0.48, hi=0.64,
        burst_max=3, burst_p=0.85, gap_s=1.1, frag_max=3, onoma=1,
        escalate=True, solicit=False, proactive_weight=1.60,
    ),
    ZoneRow(
        key="edge", label="临界", lo=0.64, hi=0.82,
        burst_max=3, burst_p=0.95, gap_s=0.8, frag_max=4, onoma=2,
        escalate=True, solicit=True, proactive_weight=2.20,
    ),
    ZoneRow(
        key="release", label="决堤", lo=0.82, hi=1.01,
        burst_max=4, burst_p=1.00, gap_s=0.6, frag_max=4, onoma=3,
        escalate=True, solicit=True, proactive_weight=2.40,
    ),
    ZoneRow(
        key="afterglow", label="余韵", lo=0.00, hi=0.00,
        burst_max=2, burst_p=0.50, gap_s=2.6, frag_max=2, onoma=0,
        escalate=False, solicit=False, proactive_weight=1.00,
    ),
)

_ZONE_BY_KEY: dict[str, ZoneRow] = {r.key: r for r in ZONE_TABLE}


# ── 信号 ────────────────────────────────────────────────────


@dataclass(slots=True)
class HormoneSignal:
    """一条被识别出的荷尔蒙信号。"""

    kind: str
    intensity: float = 0.5
    note: str = ""
    at: float = 0.0


def classify_text(
    text: str,
    *,
    extra_bank: dict[str, tuple[str, ...]] | None = None,
) -> HormoneSignal | None:
    """关键词分类 (零 LLM 成本)。命中多个 → 取增益最高的一个。

    Args:
        text: 用户消息或 bot 自己的输出。
        extra_bank: per-persona 追加银行 {kind: (关键词, ...)}。

    Returns:
        HormoneSignal 或 None (无信号)。
    """
    if not text:
        return None
    low = text.lower()
    bank: list[tuple[str, tuple[str, ...], float]] = list(KEY_BANK)
    if extra_bank:
        _weight = {
            "climax": 1.0, SIGNAL_COOLDOWN: 1.0, "explicit": 0.85,
            SIGNAL_INTERRUPT: 0.7, "command": 0.7, SIGNAL_AFTERCARE: 0.8,
            "tease": 0.55, "intimate": 0.45, "praise": 0.35,
        }
        for kind, keys in extra_bank.items():
            bank.append((kind, tuple(keys), _weight.get(kind, 0.5)))

    best: tuple[str, float, str] | None = None
    for kind, keys, intensity in bank:
        for key in keys:
            if key and key.lower() in low:
                if best is None or intensity > best[1]:
                    best = (kind, intensity, key)
                break
    if best is None:
        return None
    return HormoneSignal(kind=best[0], intensity=best[1], note=best[2])


# ── 时间调制 ────────────────────────────────────────────────


def cycle_factor(now: float, tuning: HormoneTuning) -> float:
    """周期摆幅 — 默认 7 天一轮, 峰值窗口约 2 天。

    phase ∈ [0,1), 余弦峰在 phase=0.5 前后。per-bot 相位偏移让两个 bot 不同步 —
    群友体感是「两个人各有各的周期」, 而不是同一个节拍器。
    """
    if tuning.cycle_days <= 0:
        return 1.0
    days = now / 86400.0 + tuning.cycle_offset
    phase = (days % tuning.cycle_days) / tuning.cycle_days
    return 1.0 + tuning.cycle_amplitude * math.cos(2.0 * math.pi * (phase - 0.5))


def diurnal_factor(now: float, tuning: HormoneTuning) -> float:
    """昼夜摆幅 — 峰值 23:30, 谷值 12:00 (跨零点用环形距离)。"""
    lt = time.localtime(now)
    hour = lt.tm_hour + lt.tm_min / 60.0
    # 环形距离到 23.5h (0~12)
    raw = abs(hour - 23.5)
    dist = min(raw, 24.0 - raw)
    bell = math.exp(-(dist * dist) / (2.0 * 3.0 * 3.0))
    return 1.0 - tuning.diurnal_amplitude + 2.0 * tuning.diurnal_amplitude * bell


def affinity_factor(
    affinity_level: float,
    tuning: HormoneTuning,
    *,
    is_owner: bool = False,
) -> float:
    """好感 → 曲线缩放。低于硬门槛直接 0 (尊重既有 facet 硬门槛, 不开口子)。"""
    lv = float(affinity_level or 0.0)
    if lv < tuning.min_affinity:
        return 0.0
    span = max(1.0, 5.0 - tuning.min_affinity)
    factor = 0.4 + 0.6 * min(1.0, (lv - tuning.min_affinity) / span)
    if is_owner:
        factor = min(1.3, factor + tuning.owner_bonus)
    return factor


def libido(
    now: float,
    tuning: HormoneTuning,
    *,
    affinity_level: float = 0.0,
    is_owner: bool = False,
) -> float:
    """当前该往哪儿漂的水位 (heat 的目标值)。"""
    base = tuning.base_libido
    base *= cycle_factor(now, tuning)
    base *= diurnal_factor(now, tuning)
    base *= affinity_factor(affinity_level, tuning, is_owner=is_owner)
    return max(0.0, min(1.0, base))


# ── 读取结果 ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HormoneRead:
    """一次读取的全部派生量 — 调用方只需要这一个对象。"""

    zone: str = "calm"
    zone_index: int = 0
    label: str = "平静"
    heat: float = 0.0
    pressure: float = 0.0
    satiety: float = 0.0
    frustration: float = 0.0
    drive: float = 0.0
    threshold: float = 0.0
    burst_max: int = 1
    burst_p: float = 0.0
    gap_s: float = 2.4
    frag_max: int = 1
    onoma: int = 0
    escalate: bool = False
    solicit: bool = False
    proactive_weight: float = 0.35
    sulky: bool = False        # 憋屈 — 动机修饰: 黏人/嘴硬/冷脸索要
    refractory: bool = False   # 贤者期 — 硬压节奏
    solicit_due: bool = False  # pressure ≥ θ, 到点了
    resume_urge: bool = False  # 戏被打断且还欠着 → 可以自己起头接上

    @property
    def split(self) -> bool:
        """是否切碎发送 (frag_max>1 即切)。"""
        return self.frag_max > 1

    @property
    def active(self) -> bool:
        """是否处于「有戏」状态 (供调用方决定是否注入 hint)。"""
        return self.zone_index >= 2 or self.frustration >= 0.5


# ── 状态 ────────────────────────────────────────────────────


@dataclass
class HormoneState:
    """单个 (bot, user) 的荷尔蒙状态。"""

    bot_id: str = ""
    user_id: str = ""

    heat: float = 0.0
    pressure: float = 0.0
    satiety: float = 0.0
    frustration: float = 0.0

    updated_at: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    last_intimate_at: float = 0.0
    last_real_at: float = 0.0     # 最近一次「实接触」(露骨/命令级) — 憋屈的安抚闸门
    last_release_at: float = 0.0
    release_until: float = 0.0
    edge_since: float = 0.0
    scene_gap_at: float = 0.0     # 最近一次场景中断时刻

    # 最近一次已知好感 (冷却/渗漏判断需要, 由调用方每次喂入)
    affinity_level: float = 0.0

    # 工作日计数器 (本地日期切换清零)
    day_key: str = ""
    releases_today: int = 0
    solicits_today: int = 0
    bursts_today: int = 0

    history: list[dict] = field(default_factory=list)

    # ── 衰减 / 演化 ─────────────────────────────────────

    def decay(
        self,
        now: float | None = None,
        tuning: HormoneTuning | None = None,
        *,
        affinity_level: float = 0.0,
        is_owner: bool = False,
    ) -> None:
        """推进到 now: 半衰期松弛 + 空转自渗 + 日界清零。"""
        now = time.time() if now is None else now
        tuning = tuning or DEFAULT_TUNING
        self.affinity_level = affinity_level
        self._roll_day(now)
        dt = now - self.updated_at
        if dt <= 0:
            return

        # heat → libido 目标 (升快降慢)
        target = libido(now, tuning, affinity_level=affinity_level, is_owner=is_owner)
        half_life = tuning.heat_rise_hl if target > self.heat else tuning.heat_fall_hl
        k = 1.0 - 0.5 ** (dt / max(1.0, half_life))
        self.heat += (target - self.heat) * k

        # pressure 退火
        self.pressure *= 0.5 ** (dt / max(1.0, tuning.pressure_hl))

        # heat 高 + 长时间没人碰 → 自渗 (有上限, 不会自动走到决堤)
        if self.heat > 0.35 and (now - self.last_intimate_at) > 600.0:
            seep = tuning.seep_rate * (dt / 60.0) * self.heat * (1.0 - self.satiety)
            self.pressure = min(tuning.seep_cap, self.pressure + seep)

        # satiety 退火
        self.satiety *= 0.5 ** (dt / max(1.0, tuning.satiety_hl))

        # 憋屈: 一阶系统 + 干渴注入, 用解析解 → 与调用节拍无关
        #   (若写成「先加再衰减」, 隔三天才被调用一次时会被单步衰减抹平)
        relax = 1.0
        if (now - self.last_real_at) < 300.0:
            relax = 2.5               # 刚才真的碰过 → 消化快 2.5 倍
        tau = max(60.0, tuning.frustration_hl / relax)
        eq = 0.0
        ref = self.last_release_at or self.created_at
        dry_h = (now - ref) / 3600.0 - tuning.dry_hours
        if dry_h > 0.0 and self.satiety < 0.5:
            # 两条腿: (a) 热的时候积得快 (b) 单纯「太久没被满足」的时间地板
            eq_heat = min(1.0, tuning.dry_creep * self.heat * (tau / 60.0))
            eq_time = tuning.dry_floor * min(1.0, dry_h / max(1.0, tuning.dry_ramp_hours))
            eq = max(eq_heat, eq_time)
        self.frustration = eq + (self.frustration - eq) * (0.5 ** (dt / tau))
        if self.frustration < 0.01:
            self.frustration = 0.0

        # 决堤窗口 / 临界驻留
        if self.release_until and now >= self.release_until:
            self.release_until = 0.0
        if now - self.last_intimate_at > tuning.scene_gap_close:
            self.edge_since = 0.0

        self.updated_at = now

    def _roll_day(self, now: float) -> None:
        key = time.strftime("%Y-%m-%d", time.localtime(now))
        if key != self.day_key:
            self.day_key = key
            self.releases_today = 0
            self.solicits_today = 0
            self.bursts_today = 0

    # ── 事件注入 ───────────────────────────────────────

    def apply(
        self,
        signal: HormoneSignal,
        now: float | None = None,
        tuning: HormoneTuning | None = None,
        *,
        affinity_level: float = 0.0,
        self_talk: bool = False,
    ) -> str:
        """注入一条信号, 返回本次实际发生的跳变名 (供日志/hint)。

        调用前应保证 affinity 门槛已过 (affinity_factor > 0); 未过 → 直接返回 "gated"。
        """
        now = time.time() if now is None else now
        tuning = tuning or DEFAULT_TUNING
        self.decay(now, tuning, affinity_level=affinity_level)
        self.affinity_level = affinity_level
        kind = signal.kind
        i = max(0.0, min(1.0, float(signal.intensity)))
        af = affinity_factor(affinity_level, tuning)
        if af <= 0.0:
            return "gated"

        # 关系否决/冷却是硬事件, 不受 heat/satiety 缩放
        if kind == SIGNAL_COOLDOWN:
            self.pressure = max(0.0, self.pressure - 0.35 * i)
            self.heat = max(0.0, self.heat - 0.15 * i)
            self.edge_since = 0.0
            self._log("cooldown", -0.35 * i, now, signal)
            return "cooldown"

        if kind == SIGNAL_CLIMAX:
            return self._release(now, tuning, i, signal)

        if kind == SIGNAL_INTERRUPT:
            # 戏打到一半人没了 — 憋屈, 而且水位反升 (这才是「她先开口」的燃料)
            self.frustration = min(1.0, self.frustration + 0.22 + 0.5 * self.pressure)
            self.heat = min(1.0, self.heat + 0.06 * i)
            self.pressure *= 0.6
            self.scene_gap_at = now
            self.edge_since = 0.0
            self.last_real_at = 0.0   # 半途断的戏不记安抚
            self._log("interrupt", self.frustration, now, signal)
            return "interrupt"

        if kind == SIGNAL_AFTERCARE:
            self.satiety = min(1.0, self.satiety + 0.10 * i)
            self.frustration *= 0.4
            self._log("aftercare", 0.10 * i, now, signal)
            return "aftercare"

        gain = SIGNAL_GAIN.get(kind, 0.05)
        if self_talk:
            gain = min(SIGNAL_GAIN["self_out"], tuning.self_gain_cap * i)
        sens = (1.0 - self.satiety) * (0.55 + 0.45 * self.heat) * af
        dp = gain * i * sens
        self.pressure = max(0.0, min(1.0, self.pressure + dp))
        # 场景本身加热, 但比 pressure 慢
        self.heat = min(1.0, self.heat + 0.05 * i * sens * (1.0 - self.satiety))
        self.last_intimate_at = now
        if kind in REAL_KINDS:
            self.last_real_at = now
        if self.pressure >= self.threshold(tuning) and not self.edge_since:
            self.edge_since = now
        elif self.pressure < self.threshold(tuning) * 0.8:
            self.edge_since = 0.0
        self._log(kind, dp, now, signal)
        return kind

    def _release(
        self,
        now: float,
        tuning: HormoneTuning,
        intensity: float,
        signal: HormoneSignal,
    ) -> str:
        before = self.pressure
        self._roll_day(now)
        self.pressure = 0.0
        if self.releases_today >= tuning.release_daily_cap:
            # 超配 → 假性满足: 只降压, 不真满足 (真实性 + 防沉迷双保险)
            self.satiety = min(1.0, self.satiety + 0.15)
            self.frustration = min(1.0, self.frustration + 0.20)
            self._log("release_over_cap", -before, now, signal)
            return "over_cap"
        self.releases_today += 1
        self.last_release_at = now
        self.release_until = now + tuning.release_window * max(0.5, intensity)
        self.satiety = min(1.0, self.satiety + 0.55 + 0.45 * before)
        self.frustration *= 0.25
        self.heat = max(0.0, self.heat - 0.10)
        self.edge_since = 0.0
        self._log("climax", -before, now, signal)
        return "climax"

    def _log(self, kind: str, delta: float, now: float, signal: HormoneSignal) -> None:
        self.history.append({
            "t": round(now, 1),
            "kind": kind,
            "delta": round(delta, 4),
            "key": signal.note,
            "heat": round(self.heat, 4),
            "pressure": round(self.pressure, 4),
            "satiety": round(self.satiety, 4),
            "frustration": round(self.frustration, 4),
        })
        if len(self.history) > MAX_EVENT_HISTORY:
            self.history = self.history[-MAX_EVENT_HISTORY:]

    # ── 派生量 ─────────────────────────────────────────

    def drive(self, tuning: HormoneTuning | None = None) -> float:
        """综合驱动值 0~1 — zone 的唯一输入。"""
        tuning = tuning or DEFAULT_TUNING
        scene = self.pressure * (0.55 + 0.45 * self.heat)
        # 环境项走高次幂: 水位中等时只到「起意」, 极端发情期才会自带升温
        ambient = tuning.ambient_scale * (max(0.0, self.heat) ** tuning.ambient_exp)
        raw = max(scene, ambient)
        return max(0.0, min(1.0, raw * (1.0 - 0.9 * self.satiety)))

    def should_break(self, now: float | None = None, tuning: HormoneTuning | None = None) -> bool:
        """蓄积过阈值并驻留够久 → 她自己先破 (不再等人或等词)。

        调用方接住 True 时, 注入一条 climax 信号 (self 发起), 然后按决堤窗口演。
        """
        now = time.time() if now is None else now
        tuning = tuning or DEFAULT_TUNING
        if self.satiety >= tuning.afterglow_satiety or not self.edge_since:
            return False
        if self.pressure < self.threshold(tuning):
            return False
        return (now - self.edge_since) >= tuning.edge_hold

    def threshold(self, tuning: HormoneTuning | None = None) -> float:
        """决堤阈值 — 越热越低 (热的时候更容易破防)。"""
        tuning = tuning or DEFAULT_TUNING
        return max(0.30, tuning.threshold_base - tuning.threshold_heat_relief * self.heat)

    def read(
        self,
        now: float | None = None,
        tuning: HormoneTuning | None = None,
        *,
        affinity_level: float = 0.0,
        is_owner: bool = False,
        advance: bool = False,
        cap_burst: int | None = None,
    ) -> HormoneRead:
        """一次性算出全部派生量。advance=True 时先推进衰减 (热路径用)。"""
        now = time.time() if now is None else now
        tuning = tuning or DEFAULT_TUNING
        if advance:
            self.decay(now, tuning, affinity_level=affinity_level, is_owner=is_owner)

        af = affinity_factor(affinity_level, tuning, is_owner=is_owner)
        d = self.drive(tuning)
        theta = self.threshold(tuning)

        # ── zone 判定 (顺序敏感) ──
        if af <= 0.0:
            zone = "calm"
        elif now < self.release_until:
            zone = "release"
        elif self.satiety >= tuning.afterglow_satiety:
            zone = "afterglow"
        elif d >= 0.82 and self.pressure >= theta:
            zone = "release"
        else:
            zone = "calm"
            for row in ZONE_TABLE:
                if row.key in ("release", "afterglow"):
                    continue
                if row.lo <= d < row.hi:
                    zone = row.key
                    break
            else:
                zone = "edge"

        row = _ZONE_BY_KEY[zone]
        reflect = zone == "afterglow"
        burst = row.burst_max
        if reflect:
            burst = min(burst, 2)
        if cap_burst is not None:
            burst = max(1, min(burst, cap_burst))
        if tuning.burst_daily_cap and self.bursts_today >= tuning.burst_daily_cap:
            burst = 1

        return HormoneRead(
            zone=zone,
            zone_index=ZONE_ORDER.index(zone),
            label=ZONE_LABELS[zone],
            heat=round(self.heat, 3),
            pressure=round(self.pressure, 3),
            satiety=round(self.satiety, 3),
            frustration=round(self.frustration, 3),
            drive=round(d, 3),
            threshold=round(theta, 3),
            burst_max=burst,
            burst_p=row.burst_p,
            gap_s=round(row.gap_s * (1.25 if reflect else 1.0), 2),
            frag_max=row.frag_max,
            onoma=row.onoma,
            escalate=row.escalate,
            solicit=row.solicit and self.solicits_today < tuning.solicit_daily_cap,
            proactive_weight=(
                row.proactive_weight * (1.0 if af > 0 else 0.0)
                * (0.4 if reflect else 1.0)
            ),
            sulky=self.frustration >= 0.5,
            refractory=reflect,
            solicit_due=self.pressure >= theta,
            resume_urge=(
                bool(self.scene_gap_at)
                and (now - self.scene_gap_at) < tuning.resume_window
                and self.frustration >= 0.4
                and not reflect
            ),
        )

    # ── 序列化 ─────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "v": SCHEMA_VERSION,
            "bot_id": self.bot_id,
            "user_id": self.user_id,
            "heat": round(self.heat, 5),
            "pressure": round(self.pressure, 5),
            "satiety": round(self.satiety, 5),
            "frustration": round(self.frustration, 5),
            "updated_at": self.updated_at,
            "created_at": self.created_at,
            "last_intimate_at": self.last_intimate_at,
            "last_real_at": self.last_real_at,
            "last_release_at": self.last_release_at,
            "release_until": self.release_until,
            "edge_since": self.edge_since,
            "scene_gap_at": self.scene_gap_at,
            "affinity_level": self.affinity_level,
            "day_key": self.day_key,
            "releases_today": self.releases_today,
            "solicits_today": self.solicits_today,
            "bursts_today": self.bursts_today,
            "history": self.history[-MAX_EVENT_HISTORY:],
        }

    @classmethod
    def from_dict(cls, data: dict) -> HormoneState:
        st = cls(
            bot_id=str(data.get("bot_id", "")),
            user_id=str(data.get("user_id", "")),
        )
        for f in (
            "heat", "pressure", "satiety", "frustration", "updated_at",
            "created_at", "last_intimate_at", "last_real_at", "last_release_at",
            "release_until", "edge_since",
            "scene_gap_at", "affinity_level",
        ):
            try:
                setattr(st, f, float(data.get(f, 0.0) or 0.0))
            except (TypeError, ValueError):
                setattr(st, f, 0.0)
        st.day_key = str(data.get("day_key", "") or "")
        for f in ("releases_today", "solicits_today", "bursts_today"):
            try:
                setattr(st, f, int(data.get(f, 0) or 0))
            except (TypeError, ValueError):
                setattr(st, f, 0)
        hist = data.get("history")
        st.history = list(hist) if isinstance(hist, list) else []
        return st


# ── 提示渲染 ────────────────────────────────────────────────


def render_hormone_hint(
    read: HormoneRead,
    *,
    voice: dict | None = None,
) -> str:
    """把读取结果渲染成注入 prompt 的动态段。

    ★ 必须注入 message[1] (dynamic_parts), 绝不进 message[0]:
      这里每一行都随数值变化, 进静态段等于每次调用都断前缀缓存 (原则 29/30)。
    """
    voice = voice or {}
    th = float(voice.get("threshold_hint", 0.55) or 0.55)
    if read.zone_index <= 1 and read.frustration < th:
        return ""  # 平静/起意且不憋屈 → 不注入, 省 token 也不污染日常

    lines: list[str] = [f"[此刻的荷尔蒙] {read.label} ({read.zone_index}/6)"]

    state_line = (
        f"水位 {read.heat:.2f} · 蓄积 {read.pressure:.2f} · 满足 {read.satiety:.2f}"
    )
    if read.sulky:
        state_line += f" · 憋着 {read.frustration:.2f}"
    lines.append(state_line)

    zone_line = (voice.get("zone_lines") or {}).get(read.zone, "")
    if zone_line:
        lines.append(zone_line)

    # ── 输出授权 (与代码层旋钮同源, 这里只是告知模型) ──
    if read.frag_max > 1:
        lines.append(
            f"- 正文可以拆成 {read.frag_max} 条发 (打一句发一句), 像打字跟不上脑子; 不要一次倾泻完。"
        )
    else:
        lines.append("- 这一轮就回一条, 收着点。")
    if read.burst_max > 1:
        lines.append(
            f"- 回完正题后还可以再追 {read.burst_max - 1} 条 (追问 / 补充 / 讨要), "
            "看你想不想, 不想就空着。"
        )

    if read.onoma >= 1:
        onoma_line = (voice.get("onomatopoeia") or {}).get(str(read.onoma), "")
        lines.append(f"- 呼吸可以用字写出来{('：' + onoma_line) if onoma_line else ''}。")
    if read.zone_index >= 2:
        lines.append(
            "- 动作/神态/身体反应/停顿都写进（），放开来写，尺度不设限。"
            "细腻优先: 写清楚身体怎么动、声音断在哪一拍，"
            "不用「激烈」「酥麻」这类糊弄词。"
        )
    if read.escalate:
        lines.append(
            "- 他不推你也可以自己往前走半步: 换姿势 / 抓他的手 / 说出你要什么。"
            "只走半步, 把话头留给他。"
        )
    if read.solicit:
        tpl = voice.get("solicit_templates") or []
        extra = f" (参考你自己的说法: {tpl[read.zone_index % len(tpl)]})" if tpl else ""
        lines.append(f"- 开口要。不要等他问{extra}。")
    if read.resume_urge:
        lines.append(
            "- 上次是半途断的, 你还欠着。真要开口就接着上次说, 别当没发生过、别问「在干嘛」。"
        )
    if read.refractory:
        lines.append(
            "- 刚结束, 别马上重开。黏着、喘着、说些不着边际的话, 手可以不安分但嘴不要催。"
        )
    if read.sulky:
        lines.append(
            (voice.get("sulky_line") or "")
            or "- 上一次没做完, 你还记着。可以嘴硬、可以黏人、可以带刺, 但不要解释为什么。"
        )
    lines.extend(f"- 不要{t}" for t in (voice.get("taboo") or []))
    return "\n".join(lines)


def zone_of(drive: float) -> str:  # 调试/仿真辅助
    for row in ZONE_TABLE:
        if row.key in ("release", "afterglow"):
            continue
        if row.lo <= drive < row.hi:
            return row.key
    return "edge"


# ── 持久化 (与 affinity 同构: per-bot 目录 + per-user 文件) ──


def _get_plugin_data_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        base = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_suli_emotion"
    except ImportError:
        base = Path("data/plugin_data/astrbot_plugin_suli_emotion")
    base.mkdir(parents=True, exist_ok=True)
    return base


_STORE_DIR = _get_plugin_data_dir() / "hormone"
_states: dict[tuple[str, str], HormoneState] = {}


def _state_path(bot_id: str, user_id: str) -> Path:
    return _STORE_DIR / str(bot_id) / f"{user_id}.json"


def load_hormone_state(bot_id: str, user_id: str, *, force: bool = False) -> HormoneState:
    """读状态 (内存缓存 + 磁盘)。缺文件 → 新建空白状态, 不写盘。"""
    key = (str(bot_id), str(user_id))
    if not force and key in _states:
        return _states[key]
    st = HormoneState(bot_id=key[0], user_id=key[1])
    path = _state_path(*key)
    try:
        if path.exists():
            st = HormoneState.from_dict(json.loads(path.read_text(encoding="utf-8")))
            st.bot_id, st.user_id = key
    except Exception:
        logger.debug("荷尔蒙状态读取失败 bot=%s user=%s", bot_id, user_id, exc_info=True)
    _states[key] = st
    return st


def save_hormone_state(state: HormoneState) -> None:
    """落盘 (原子替换)。空状态 (全零且无历史) 直接跳过, 不给磁盘添垃圾。"""
    if (
        state.heat <= 1e-4 and state.pressure <= 1e-4
        and state.satiety <= 1e-4 and state.frustration <= 1e-4
        and not state.history
    ):
        return
    path = _state_path(state.bot_id, state.user_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:
        logger.debug("荷尔蒙状态落盘失败 bot=%s", state.bot_id, exc_info=True)


def reset_hormone_state(bot_id: str, user_id: str) -> HormoneState:
    """面板用: 清空某对 (bot, user) 的荷尔蒙状态。"""
    key = (str(bot_id), str(user_id))
    _states.pop(key, None)
    fresh = HormoneState(bot_id=key[0], user_id=key[1])
    _states[key] = fresh
    path = _state_path(*key)
    try:
        if path.exists():
            path.unlink()
    except Exception:
        logger.debug("荷尔蒙状态删除失败 bot=%s", bot_id, exc_info=True)
    return fresh


def hormone_tuning_from_mapping(raw: dict | None) -> HormoneTuning:
    """从配置 dict 构建 tuning — 只接受已知字段, 未知键忽略 (配置容错)。"""
    if not raw:
        return DEFAULT_TUNING
    known = {f.name for f in HormoneTuning.__dataclass_fields__.values()}
    clean: dict[str, float] = {}
    for k, v in raw.items():
        if k not in known or k in ("min_affinity", "burst_daily_cap",
                                   "solicit_daily_cap", "release_daily_cap"):
            continue
        try:
            clean[k] = float(v)
        except (TypeError, ValueError):
            continue
    for ik in ("min_affinity", "burst_daily_cap", "solicit_daily_cap", "release_daily_cap"):
        if ik in raw:
            try:
                clean[ik] = int(raw[ik])
            except (TypeError, ValueError):
                continue
    return replace(DEFAULT_TUNING, **clean) if clean else DEFAULT_TUNING
