"""astrbot_plugin_suli_intelligence — 通用 AI 智力基础设施。

7 个零依赖纯算法模块，可供任何 AstrBot 插件直接 import 使用:

  domains.py            — 知识领域检测 (关键词匹配, 零 LLM 成本)
  world_book.py         — World Info / Lorebook 触发系统 (完整三模式触发 + 计时效果)
  profile_agent.py      — 异步用户建档 Agent (Layer 2 后台任务)
  prompt_interceptor.py — 提示拦截管道 (变量求值 + 条件规则 + 阻尼平滑)
  group_summarizer.py   — 群聊定期总结 Agent (Layer 2 后台任务)
  prompt_cache.py       — Provider 感知的提示缓存优化
  fact_errors.py        — 事实错误记录数据库 (JSON 持久化)

用法:
  from astrbot_plugin_suli_intelligence import (
      # domains
      Domain, DOMAINS, detect_domains, get_domain_hints,
      get_domain_heat_boost, is_reasoning_needed, user_force_reasoning,
      REASONING_INSTRUCTION,
      # world_book
      WorldBookEntry, WorldBookBuffer, load_world_book,
      scan_world_book_static,
      # profile_agent
      ProfileAgent,
      # prompt_interceptor
      PromptInterceptor, InterceptorState, ToneVariables,
      # group_summarizer
      GroupSummarizer,
      # prompt_cache
      optimize_messages, get_cache_strategy, describe_cache_info,
      # fact_errors
      FactErrorDB, FactErrorEntry, get_fact_error_db,
  )
"""

from __future__ import annotations

# ── 领域检测 ──────────────────────────────────────────────
from .domains import (
    DOMAINS,
    REASONING_INSTRUCTION,
    Domain,
    detect_domains,
    get_domain_heat_boost,
    get_domain_hints,
    is_reasoning_needed,
    user_force_reasoning,
)

# ── 事实纠错 ──────────────────────────────────────────────
from .fact_errors import (
    FactErrorDB,
    FactErrorEntry,
    get_fact_error_db,
)

# ── 群聊摘要 ──────────────────────────────────────────────
from .group_summarizer import GroupSummarizer

# ── 用户画像 ──────────────────────────────────────────────
from .profile_agent import ProfileAgent

# ── 提示缓存 ──────────────────────────────────────────────
from .prompt_cache import (
    describe_cache_info,
    get_cache_strategy,
    optimize_messages,
)

# ── 提示拦截器 ────────────────────────────────────────────
from .prompt_interceptor import (
    DAMPING_CONFIDENCE_HIGH,
    DAMPING_CONFIDENCE_LOW,
    DAMPING_DEFAULT,
    MAX_DELTA_AROUSAL,
    MAX_DELTA_VALENCE,
    InterceptorState,
    PromptInterceptor,
    ToneVariables,
)

# ── 世界书 ────────────────────────────────────────────────
from .world_book import (
    DEFAULT_SCAN_DEPTH,
    WorldBookBuffer,
    WorldBookEntry,
    load_world_book,
    scan_world_book_static,
)

__all__ = [
    "DAMPING_CONFIDENCE_HIGH",
    "DAMPING_CONFIDENCE_LOW",
    "DAMPING_DEFAULT",
    "DEFAULT_SCAN_DEPTH",
    "DOMAINS",
    "MAX_DELTA_AROUSAL",
    "MAX_DELTA_VALENCE",
    "REASONING_INSTRUCTION",
    "Domain",
    "FactErrorDB",
    "FactErrorEntry",
    "GroupSummarizer",
    "InterceptorState",
    "ProfileAgent",
    "PromptInterceptor",
    "ToneVariables",
    "WorldBookBuffer",
    "WorldBookEntry",
    "describe_cache_info",
    "detect_domains",
    "get_cache_strategy",
    "get_domain_heat_boost",
    "get_domain_hints",
    "get_fact_error_db",
    "is_reasoning_needed",
    "load_world_book",
    "optimize_messages",
    "scan_world_book_static",
    "user_force_reasoning",
]
