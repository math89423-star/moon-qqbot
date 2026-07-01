"""DEPRECATED re-export shim → 服务层已提取至 astrbot_plugin_suli_services。此 shim 将在 Phase 3 完成时移除。"""
from __future__ import annotations
import warnings
warnings.warn("web_search 导入路径已过时, 请改用 astrbot_plugin_suli_services", DeprecationWarning, stacklevel=2)
from astrbot_plugin_suli_services.web_search import *  # noqa: E402, F401, F403
