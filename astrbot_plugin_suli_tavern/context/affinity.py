"""DEPRECATED re-export shim → 长期好感已提取至 astrbot_plugin_suli_emotion。Phase 3 完成时移除。"""
from __future__ import annotations

import warnings

from astrbot_plugin_suli_emotion import *  # noqa: F403

warnings.warn(
    "context.affinity 导入路径已过时, 请改用 astrbot_plugin_suli_emotion",
    DeprecationWarning,
    stacklevel=2,
)
