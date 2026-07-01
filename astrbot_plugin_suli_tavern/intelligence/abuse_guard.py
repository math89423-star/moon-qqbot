"""DEPRECATED re-export shim → 守卫已提取至 astrbot_plugin_suli_guards。

⚠️ 此文件仅保向后兼容。请更新 import 为:
    from astrbot_plugin_suli_guards import AbuseGuard, AbuseVerdict, AbuseGuardConfig

此 shim 将在 Phase 3 完成时移除。

迁移说明:
  旧 API:  AbuseGuard.check_rate(user_id, config)  # config: Config
  新 API:  AbuseGuard.check_rate(user_id, config)  # config: AbuseGuardConfig

  如需兼容旧 Config 对象，请手动构造 AbuseGuardConfig:
    cfg = AbuseGuardConfig(
        rate_capacity=config.abuse_user_burst_per_minute,
        rate_refill_per_second=config.abuse_user_rate_per_second,
        ...
    )
"""

from __future__ import annotations

import logging
import warnings

warnings.warn(
    "abuse_guard 导入路径已过时, 请改用 astrbot_plugin_suli_guards",
    DeprecationWarning,
    stacklevel=2,
)
logger = logging.getLogger(__name__)

from astrbot_plugin_suli_guards.abuse_guard import (  # noqa: E402, F401
    AbuseGuard,
    check_quotas,
    check_repeat,
    check_thread_depth,
    check_token_bucket,
    record_advanced_quota,
    record_reply_quota,
    reset_thread_depth_counter,
)
from astrbot_plugin_suli_guards.types import (  # noqa: E402, F401
    AbuseGuardConfig,
    AbuseVerdict,
)

__all__ = [
    "AbuseGuard",
    "AbuseGuardConfig",
    "AbuseVerdict",
    "check_quotas",
    "check_repeat",
    "check_thread_depth",
    "check_token_bucket",
    "record_advanced_quota",
    "record_reply_quota",
    "reset_thread_depth_counter",
]
