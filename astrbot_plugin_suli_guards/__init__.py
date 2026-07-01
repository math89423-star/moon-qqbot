"""暮恩守卫系统 — AstrBot 纯库插件。

提供 6 个可导入的模块:

  - InjectionGuard    — 预 LLM 注入拦截 (57 条统一模式)
  - HeuristicDetector — 启发式载荷检测 (编码解码 + 多维度评分)
  - AbuseGuard        — 滥用检测 (频率/重复/配额/线程深度)
  - GroomingGuard     — 恶意调教处置
  - PeerIsolation     — 外部 bot 内容隔离
  - BotDetector       — Bot 行为检测 (滚动嫌疑分 + social_play)

用法:
  from astrbot_plugin_suli_guards import InjectionGuard, HeuristicDetector
"""

from .abuse_guard import AbuseGuard
from .bot_detector import BotDetector, generate_social_play_hint, init_bot_detector
from .grooming_guard import GroomingGuard
from .heuristic_detector import HeuristicDetector
from .injection_guard import InjectionGuard
from .peer_isolation import PeerIsolation
from .types import (
    AbuseGuardConfig,
    AbuseVerdict,
    InjectionVerdict,
    MemoryPermanentStore,
    MemoryStore,
    PermanentStore,
)

__all__ = [
    "AbuseGuard",
    "AbuseGuardConfig",
    "AbuseVerdict",
    "BotDetector",
    "GroomingGuard",
    "HeuristicDetector",
    "InjectionGuard",
    "InjectionVerdict",
    "MemoryPermanentStore",
    "MemoryStore",
    "PeerIsolation",
    "PermanentStore",
    "generate_social_play_hint",
    "init_bot_detector",
]
