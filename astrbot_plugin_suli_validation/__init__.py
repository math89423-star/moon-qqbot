"""astrbot_plugin_suli_validation — 交叉验证编排器。

用法:
  from astrbot_plugin_suli_validation import CrossValidator, detect_challenge
"""

from __future__ import annotations

from .cross_validation import (
    CHALLENGE_TRIGGERS,
    TECH_MARKERS,
    CrossValidator,
    detect_challenge,
    extract_query,
)

__all__ = [
    "CHALLENGE_TRIGGERS",
    "TECH_MARKERS",
    "CrossValidator",
    "detect_challenge",
    "extract_query",
]
