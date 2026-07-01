"""暮恩管线引擎 — 可组合异步步骤编排器。

纯抽象框架，零外部依赖。提供:

  - Pipeline       — 线性管线编排器
  - PipelineStep   — 异步步骤抽象基类
  - PipelineContext — 步骤间共享的 dict-based 上下文
  - PIPELINE_SILENCE — 短路标记常量

用法:
  from astrbot_plugin_suli_pipeline import Pipeline, PipelineStep, PipelineContext

  class MyStep(PipelineStep):
      name = "my_step"
      async def execute(self, ctx: PipelineContext) -> PipelineContext:
          ...
          return ctx

  pipeline = Pipeline("my_pipeline")
  pipeline.add_step(MyStep())
  result = await pipeline.run()
"""

from .pipeline import PIPELINE_SILENCE, Pipeline, PipelineContext, PipelineStep

__all__ = [
    "PIPELINE_SILENCE",
    "Pipeline",
    "PipelineContext",
    "PipelineStep",
]
