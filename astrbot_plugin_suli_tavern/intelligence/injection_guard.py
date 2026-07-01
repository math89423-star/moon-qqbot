"""DEPRECATED re-export shim → 守卫已提取至 astrbot_plugin_suli_guards。

⚠️ 此文件仅保向后兼容。请更新 import 为:
    from astrbot_plugin_suli_guards import InjectionGuard, InjectionVerdict

此 shim 将在 Phase 3 完成时移除。
"""

from __future__ import annotations

import logging
import warnings

warnings.warn(
    "injection_guard 导入路径已过时, 请改用 astrbot_plugin_suli_guards",
    DeprecationWarning,
    stacklevel=2,
)
logger = logging.getLogger(__name__)

from astrbot_plugin_suli_guards.injection_guard import (  # noqa: E402, F401
    InjectionGuard,
)
from astrbot_plugin_suli_guards.types import InjectionVerdict  # noqa: E402, F401

__all__ = ["InjectionGuard", "InjectionVerdict"]
