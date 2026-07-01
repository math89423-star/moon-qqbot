"""DEPRECATED re-export shim → 管线引擎已提取至 astrbot_plugin_suli_pipeline。

⚠️ 此文件仅保向后兼容。请更新 import 为:
    from astrbot_plugin_suli_pipeline import Pipeline, PipelineStep, PipelineContext, PIPELINE_SILENCE

此 shim 将在 Phase 3 完成时移除。
"""

from __future__ import annotations

import warnings

warnings.warn(
    "pipeline 导入路径已过时, 请改用 astrbot_plugin_suli_pipeline",
    DeprecationWarning,
    stacklevel=2,
)

from astrbot_plugin_suli_pipeline.pipeline import (  # noqa: E402, F401
    PIPELINE_SILENCE,
    Pipeline,
    PipelineContext,
    PipelineStep,
)

__all__ = ["PIPELINE_SILENCE", "Pipeline", "PipelineContext", "PipelineStep"]
