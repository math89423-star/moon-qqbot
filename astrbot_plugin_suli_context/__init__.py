"""astrbot_plugin_suli_context — Pre-flight 上下文分析层。

提供 LLM 调用前的上下文复杂度评分 + 工具推荐 + 预收集。

用法:
  from astrbot_plugin_suli_context import ContextGatherer, ContextPreflight, format_collected_context
"""

from __future__ import annotations

from .context_gatherer import (
    DEFAULT_COLLECT_THRESHOLD,
    PREFLIGHT_TIMEOUT,
    ContextGatherer,
    ContextPreflight,
    format_collected_context,
)

__all__ = [
    "DEFAULT_COLLECT_THRESHOLD",
    "PREFLIGHT_TIMEOUT",
    "ContextGatherer",
    "ContextPreflight",
    "format_collected_context",
]
