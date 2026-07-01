"""管线引擎 — 可组合的异步步骤编排器。

设计原则:
  - 每个 Step 是独立的异步可调用单元，接收 PipelineContext，返回 PipelineContext
  - Step 可按需启用/禁用，可标记 required (失败中断) 或 optional (失败跳过)
  - 管线执行是线性的：step_1 → step_2 → ... → step_N
  - Step 可设置短路条件 (返回特定标记时提前终止管线)
  - 无状态 — Pipeline 是纯函数，状态在 PipelineContext 中流转

用法:
  from .pipeline import Pipeline, PipelineStep, PipelineContext

  pipeline = Pipeline("reply_pipeline", steps=[
      PreFlightStep(),
      ContextCollectionStep(),
      ModelRoutingStep(),
      PromptBuildStep(),
      LLMCallStep(),
      PostProcessStep(),
  ])
  result = await pipeline.run(ctx)
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── 管线短路标记 ──────────────────────────────────────────

# Step 返回此值表示「不回复」(如静默、重复、冷却)
PIPELINE_SILENCE = "__pipeline_silence__"


@dataclass
class PipelineContext:
    """管线上下文 — 在 Step 之间流转的共享状态。

    设计: dict-based 而非强类型，因为不同管线有不同的上下文字段。
    常用 key:
      - "group_id": int
      - "trigger_reason": str
      - "trigger_user_id": str
      - "messages": list[dict]       (LLM messages)
      - "reply": str                  (最终回复)
      - "model_route": ModelRoute     (路由结果)
      - "preflight": ContextPreflight (Pre-flight 分析)
      - "collected_context": dict     (收集的上下文)
    """

    data: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    step_log: list[str] = field(default_factory=list)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value):
        self.data[key] = value
        return self

    def __contains__(self, key: str) -> bool:
        return key in self.data

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at


class PipelineStep(abc.ABC):
    """管线步骤基类。

    子类只需实现 async execute(ctx) → PipelineContext。
    覆盖 name, enabled, required 属性来定制行为。
    """

    name: str = ""
    enabled: bool = True
    required: bool = True  # False = 失败时记录日志并跳过，不中断管线

    @abc.abstractmethod
    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行本步骤。返回修改后的 ctx (通常就地修改)。"""
        ...

    def should_run(self, ctx: PipelineContext) -> bool:
        """运行前检查 — 子类可覆盖以条件跳过。"""
        return self.enabled

    def __repr__(self):
        status = "✓" if self.enabled else "✗"
        req = "!" if self.required else "~"
        return f"<{status}{req} {self.name or self.__class__.__name__}>"


class Pipeline:
    """线性管线 — 按序执行 Step 列表。

    Step 可以:
      - 返回 ctx → 继续下一个 step
      - raise Exception → required 则中断, optional 则跳过
      - 在 ctx 中设置 reply 且后续 step 检查短路
    """

    def __init__(self, name: str, steps: list[PipelineStep] | None = None):
        self.name = name
        self._steps: list[PipelineStep] = steps or []

    def add_step(self, step: PipelineStep, after: str = "", before: str = "") -> None:
        """插入 step。after/before 指定位置 (按 name 匹配)，都空则追加到末尾。"""
        if after:
            for i, s in enumerate(self._steps):
                if s.name == after:
                    self._steps.insert(i + 1, step)
                    return
            raise ValueError(f"Step '{after}' not found in pipeline '{self.name}'")
        if before:
            for i, s in enumerate(self._steps):
                if s.name == before:
                    self._steps.insert(i, step)
                    return
            raise ValueError(f"Step '{before}' not found in pipeline '{self.name}'")
        self._steps.append(step)

    def remove_step(self, name: str) -> None:
        """按 name 移除 step。"""
        self._steps = [s for s in self._steps if s.name != name]

    def get_step(self, name: str) -> PipelineStep | None:
        """按 name 查找 step。"""
        for s in self._steps:
            if s.name == name:
                return s
        return None

    async def run(
        self, ctx: PipelineContext | None = None,
    ) -> PipelineContext:
        """执行管线。

        Args:
            ctx: 初始上下文 (None 则创建空上下文)

        Returns:
            执行后的上下文 (ctx.data["reply"] 包含最终回复或 PIPELINE_SILENCE)
        """
        if ctx is None:
            ctx = PipelineContext()

        enabled_steps = [s for s in self._steps if s.should_run(ctx)]
        logger.debug(
            "管线 '%s': 开始执行 %d/%d steps",
            self.name, len(enabled_steps), len(self._steps),
        )

        for step in self._steps:
            if not step.should_run(ctx):
                ctx.step_log.append(f"{step.name}: skip (disabled)")
                continue

            step_start = time.time()
            try:
                ctx = await step.execute(ctx)
                elapsed_ms = (time.time() - step_start) * 1000
                ctx.step_log.append(f"{step.name}: ok ({elapsed_ms:.0f}ms)")

                # 短路检查: 如果 ctx 中已有 SILENCE 标记，提前终止
                if ctx.get("reply") == PIPELINE_SILENCE:
                    logger.debug(
                        "管线 '%s': 在 '%s' 收到静默信号，终止",
                        self.name, step.name,
                    )
                    break

            except Exception:
                elapsed_ms = (time.time() - step_start) * 1000
                logger.exception(
                    "管线 '%s': Step '%s' 失败 (%dms)",
                    self.name, step.name or step.__class__.__name__,
                    int(elapsed_ms),
                )
                ctx.step_log.append(
                    f"{step.name}: FAIL ({elapsed_ms:.0f}ms)",
                )
                if step.required:
                    logger.error(
                        "管线 '%s': required step '%s' 失败，中断",
                        self.name, step.name,
                    )
                    break
                # optional step 失败 → 继续

        logger.info(
            "管线 '%s': 完成 (%dms) → %s",
            self.name, int(ctx.elapsed * 1000),
            ", ".join(ctx.step_log),
        )
        return ctx

    @property
    def steps(self) -> list[PipelineStep]:
        return list(self._steps)
