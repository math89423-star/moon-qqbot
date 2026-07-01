"""DEPRECATED re-export shim → 服务层已提取至 astrbot_plugin_suli_services。此 shim 将在 Phase 3 完成时移除。"""
from __future__ import annotations
import warnings
warnings.warn("vision 导入路径已过时, 请改用 astrbot_plugin_suli_services.vision", DeprecationWarning, stacklevel=2)
from astrbot_plugin_suli_services.vision import *  # noqa: E402, F401, F403
from astrbot_plugin_suli_services.vision import _download_image  # noqa: E402, F401 (re-export for main.py)
from astrbot_plugin_suli_services.vision import _reset_vlm_usage  # noqa: E402, F401 (re-export for main.py, import * 不导出 _ 前缀)
