"""astrbot_plugin_suli_memory — 三层记忆蒸馏系统 + bot 自传体经历记忆 + 情节记忆归档。

4 个模块，零外部框架依赖 (tokenize 惰性加载 + 内置回退):

  user_memory.py      — UserMemoryStore: 会话级记忆 (daily) — JSON 持久化 + 关键词检索 + 衰减 + LLM 提取
  memory_tiers.py     — CoreMemoryStore + MemoryTierManager: 人格级记忆 (core) + 三层编排
  bot_experience.py   — BotExperienceStore: bot 自传体经历记忆 (recent + core) — per-bot、跨群有效
  episodic_store.py   — EpisodicStore: 情节记忆归档 — 槽过期自动保存 thread_summary, 零新增 LLM 调用

用法:
  from astrbot_plugin_suli_memory import (
      UserMemoryStore, init_memory_store, get_memory_store,
      CoreMemoryStore, MemoryTierManager, init_tier_manager, get_tier_manager,
      BotExperienceStore, init_experience_store, get_experience_store,
      EpisodicStore, init_episodic_store, get_episodic_store,
  )
"""

from __future__ import annotations

# ── bot 自传体经历记忆 (主语: bot) ──────────────────────────
from .bot_experience import (
    BotExperienceStore,
    get_experience_store,
    init_experience_store,
)

# ── 情节记忆归档 (槽过期 → 长期回忆) ────────────────────────
from .episodic_store import (
    EpisodicStore,
    get_episodic_store,
    init_episodic_store,
)

# ── 记忆编排 (daily + core 蒸馏) ───────────────────────────
from .memory_tiers import (
    CoreMemoryStore,
    MemoryTierManager,
    get_tier_manager,
    init_tier_manager,
)

# ── 用户记忆 (daily 层) ────────────────────────────────────
from .user_memory import (
    UserMemoryStore,
    get_memory_store,
    init_memory_store,
)

__all__ = [
    "BotExperienceStore",
    "CoreMemoryStore",
    "EpisodicStore",
    "MemoryTierManager",
    "UserMemoryStore",
    "get_episodic_store",
    "get_experience_store",
    "get_memory_store",
    "get_tier_manager",
    "init_episodic_store",
    "init_experience_store",
    "init_memory_store",
    "init_tier_manager",
]
