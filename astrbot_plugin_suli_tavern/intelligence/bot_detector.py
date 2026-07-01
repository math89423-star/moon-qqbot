"""DEPRECATED re-export shim → 守卫已提取至 astrbot_plugin_suli_guards。

⚠️ 此文件仅保向后兼容。请更新 import 为:
    from astrbot_plugin_suli_guards import BotDetector, BotSuspicion, generate_social_play_hint

此 shim 将在 Phase 3 完成时移除。

迁移说明:
  旧代码:  from ..intelligence.bot_detector import init_bot_detector
          init_bot_detector()

  新代码:  from astrbot_plugin_suli_guards import BotDetector, init_bot_detector
          BotDetector.init_store(get_bot_db())  # 注入持久化
          init_bot_detector()                    # 恢复状态
"""

from __future__ import annotations

import warnings

warnings.warn(
    "bot_detector 导入路径已过时, 请改用 astrbot_plugin_suli_guards",
    DeprecationWarning,
    stacklevel=2,
)

from astrbot_plugin_suli_guards.bot_detector import (  # noqa: E402, F401
    BotDetector,
    BotSuspicion,
    generate_social_play_hint,
    init_bot_detector,
)

__all__ = [
    "BotDetector",
    "BotSuspicion",
    "generate_social_play_hint",
    "init_bot_detector",
]
