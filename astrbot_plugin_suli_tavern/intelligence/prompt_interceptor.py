"""DEPRECATED re-export shim → 提示拦截器已提取至 astrbot_plugin_suli_intelligence。Phase 3 完成时移除。"""
from __future__ import annotations

import warnings

from astrbot_plugin_suli_intelligence import *  # noqa: F403

warnings.warn(
    "intelligence.prompt_interceptor 导入路径已过时, 请改用 astrbot_plugin_suli_intelligence",
    DeprecationWarning,
    stacklevel=2,
)
