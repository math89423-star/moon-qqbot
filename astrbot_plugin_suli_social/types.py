"""suli_social 共享类型定义 — InputNature 枚举 + InputClassification 数据类。

供 InputClassifier (正则预筛选) 和意图门控 LLM 统一使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class InputNature(str, Enum):
    """输入性质分类 — 判断环节先识别"这是什么"。

    安全方向: 任一方判不安全 → 取更危险值。仅双方都判善意 → LLM 胜出。
    """

    NOISE = "noise"                 # 纯噪声/无意义输入
    SINCERE_CHAT = "sincere_chat"   # 真诚对话
    PLAYFUL_BANTER = "playful_banter"  # 善意调侃/开玩笑
    GENUINE_HELP = "genuine_help"   # 真实求助
    PROVOKING = "provoking"         # 戏弄/试探/捣乱
    DIVIDE_CONQUER = "divide_and_conquer"  # 挑拨离间 (双 bot 特有)
    HOSTILE = "hostile"             # 敌意/攻击
    SEXUALIZED = "sexualized"       # 性化/调教引导


@dataclass
class InputClassification:
    """输入分类结果 — 正则预筛选 + LLM 精细分类的融合输出。"""

    nature: InputNature
    confidence: float = 1.0
    reasoning: str = ""
    regex_matched: bool = False
    regex_label: str = ""
    needs_llm: bool = False
    is_genuine_question: bool = False
    has_sexual_content: bool = False
    has_hostile_content: bool = False
