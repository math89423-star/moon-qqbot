"""astrbot_plugin_suli_social — 社会性生存守卫 (粟藜)

双层分类: 正则预筛选 (InputClassifier, 零 LLM) + LLM 精细分类。
SocialGuard: 感知群聊社会压力, 决定是否发言/使用工具。
"""

from .social_guard import (
    PressureLevel,
    SocialDecision,
    SocialGuard,
    SocialStance,
    get_social_guard,
)
from .types import InputClassification, InputNature

__all__ = [
    "SocialGuard",
    "SocialDecision",
    "SocialStance",
    "PressureLevel",
    "get_social_guard",
    "InputClassification",
    "InputNature",
]
