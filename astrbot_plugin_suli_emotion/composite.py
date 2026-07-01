"""综合量化值 — warmth x energy 二维心境算法。

纯信号计算，零外部依赖。供 Gate (决策) 和 PromptBuilder (facet 选择) 共同使用。

算法:
  warmth = affinity_norm x 0.65 + valence x 0.35   — 「愿不愿意近」
  energy = arousal x 0.5 + (1 - fatigue_norm) x 0.5 — 「有没有力气」

  映射到 7 个心境 zone，决定回复基调。

用法:
  from astrbot_plugin_suli_emotion.composite import compute_composite, CompositeResult
  cr = compute_composite(valence=0.3, arousal=0.1, affinity_level=2, fatigue_value=-0.1)
  # cr.zone → "moderate", cr.zone_label → "温和区·日常积极"
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Zone 映射表 ──────────────────────────────────────────────

# (zone_key, label, warmth_min, warmth_max, energy_min, energy_max, extra_condition)
# extra_condition: 额外条件函数 (affinity_level) -> bool
_ZONE_DEFS: list[tuple[str, str, float, float, float, float, str | None]] = [
    # zone_key       label                  warmth      energy       extra
    #                                    min    max   min    max
    ("warm_active",   "暖活区·高能亲近",     0.55, 1.0,  0.30, 1.0,  None),
    ("warm_calm",     "温润区·亲近低能",     0.55, 1.0, -1.0,  0.30, None),
    ("interested",    "兴致区·好心情",       0.20, 0.55, 0.301, 1.0,  None),
    ("moderate",      "温和区·日常积极",     0.20, 0.55,-1.0,  0.30,  None),
    ("neutral",       "中性区·日常默认",     -0.20,0.20,-1.0,  1.0,  None),
    ("cold_gap",      "寒隙区·在意的人让你冷了",-1.0,-0.20,-1.0, 1.0, "aff>=2"),
    ("cold_distance",  "冷距区·疏离防御",     -1.0,-0.20,-1.0,  1.0,  "aff<2"),
]


@dataclass
class CompositeResult:
    """综合量化结果 — 两维度 + zone 映射。"""

    warmth: float = 0.0
    """温暖度: ~ -0.35 (极度疏远) ~ +1.0 (极度亲近)"""

    energy: float = 0.0
    """能量度: ~ -1.0 (筋疲力尽) ~ +1.0 (精力充沛)"""

    zone: str = "neutral"
    """心境 zone key: warm_active|warm_calm|interested|moderate|neutral|cold_gap|cold_distance"""

    zone_label: str = "中性区·日常默认"
    """心境 zone 中文标签"""

    affinity_level: int = 0
    """好感等级 (透传, 供下游 facet 决策用)"""

    @property
    def is_warm(self) -> bool:
        """是否在温暖区间 (暖活/温润)。"""
        return self.zone in ("warm_active", "warm_calm")

    @property
    def is_cold(self) -> bool:
        """是否在冷距区间 (寒隙/冷距)。"""
        return self.zone in ("cold_gap", "cold_distance")

    @property
    def is_high_energy(self) -> bool:
        """是否高能量。"""
        return self.energy > 0.3


def compute_composite(
    *,
    valence: float = 0.0,
    arousal: float = 0.0,
    affinity_level: int = 0,
    fatigue_value: float = 0.0,
) -> CompositeResult:
    """计算综合量化值。

    Args:
        valence: 心情效价 (-1.0 ~ +1.0, 基线 +0.3)
        arousal: 心情唤醒度 (-1.0 ~ +1.0, 基线 0.0)
        affinity_level: 好感等级 (-2 ~ +5)
        fatigue_value: 疲劳值 (-1.0 筋疲力尽 ~ +1.0 精力充沛, 基线 0.0)

    Returns:
        CompositeResult: warmth, energy, zone, zone_label
    """
    # ── 归一化 ──
    # 好感度: 负值→负贡献, 正值→正贡献, 0=中性
    if affinity_level >= 0:
        affinity_norm = affinity_level / 5.0   # 0→0.0, 3→0.6, 5→1.0
    else:
        affinity_norm = affinity_level / 2.0   # -2→-1.0, -1→-0.5
    fatigue_norm = (fatigue_value + 1.0) / 2.0  # -1→0.0, 0→0.5, +1→1.0

    # ── 两维度 ──
    warmth = affinity_norm * 0.65 + valence * 0.35
    energy = arousal * 0.5 + (1.0 - fatigue_norm) * 0.5

    # clamp
    warmth = max(-1.0, min(1.0, warmth))
    energy = max(-1.0, min(1.0, energy))

    # ── zone 匹配 (按定义顺序, 先匹配先得) ──
    zone = "neutral"
    zone_label = "中性区·日常默认"
    for zkey, zlabel, wmin, wmax, emin, emax, extra in _ZONE_DEFS:
        if not (wmin <= warmth <= wmax):
            continue
        if not (emin <= energy <= emax):
            continue
        if extra == "aff>=2":
            if affinity_level < 2:
                continue
        elif extra == "aff<2":
            if affinity_level >= 2:
                continue
        zone = zkey
        zone_label = zlabel
        break

    return CompositeResult(
        warmth=round(warmth, 4),
        energy=round(energy, 4),
        zone=zone,
        zone_label=zone_label,
        affinity_level=affinity_level,
    )
