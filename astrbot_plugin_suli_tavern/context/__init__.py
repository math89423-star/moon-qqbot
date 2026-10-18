"""Context 层 — 上下文运行时 + 异步写入 + 预算调度。

组件:
  - ContextRuntimeEngine: 上下文运行时引擎 (Persona Kernel + Token Budget + Cache Planning)
  - AsyncMemoryWriter: 异步记忆写入管理器 (Read-Write 分离)
  - BudgetScheduler: 上下文预算调度器 (动态 token 分配)
  - IntentRouter: 意图路由器 (Fast/Think/Agent 三级路径)

设计原则:
  - latency > exhaustive memory (QQ 群聊)
  - Persona Kernel ≤ 2.5K token (Always-On)
  - Memory Write 不阻塞主回复
  - 静态前缀字节级一致 (缓存友好)
"""

from .context_runtime import (
    BUDGET_DEEP_CHAT,
    BUDGET_FAST_CHAT,
    BUDGET_TOOL_CALL,
    ContextRuntimeEngine,
    PersonaKernel,
    RuntimeContext,
    TokenBudget,
    compile_persona_kernel,
    get_context_runtime,
)
from .async_memory_writer import (
    AsyncMemoryWriter,
    MemoryWriteTask,
    get_memory_writer,
)
from .budget_scheduler import (
    BudgetScheduler,
    get_budget_scheduler,
)
from ..intelligence.intent_router import (
    IntentRouter,
    RouteDecision,
    RouteType,
    get_intent_router,
)

__all__ = [
    # Context Runtime
    "ContextRuntimeEngine",
    "PersonaKernel",
    "RuntimeContext",
    "TokenBudget",
    "compile_persona_kernel",
    "get_context_runtime",
    "BUDGET_FAST_CHAT",
    "BUDGET_DEEP_CHAT",
    "BUDGET_TOOL_CALL",
    # Async Memory Writer
    "AsyncMemoryWriter",
    "MemoryWriteTask",
    "get_memory_writer",
    # Budget Scheduler
    "BudgetScheduler",
    "get_budget_scheduler",
    # Intent Router
    "IntentRouter",
    "RouteDecision",
    "RouteType",
    "get_intent_router",
]
