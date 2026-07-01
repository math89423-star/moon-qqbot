"""DEPRECATED re-export shim → 三层记忆蒸馏已提取至 astrbot_plugin_suli_memory。Phase 3 完成时移除。"""
from __future__ import annotations

import warnings

from astrbot_plugin_suli_memory import *  # noqa: F403

warnings.warn(
    "context.memory_tiers 导入路径已过时, 请改用 astrbot_plugin_suli_memory",
    DeprecationWarning,
    stacklevel=2,
)
