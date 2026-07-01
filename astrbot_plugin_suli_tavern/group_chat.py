"""群聊自然对话调度器 — 已迁移到 transport/ 层。

此文件保留为 re-export shim，新代码请使用:
  from .transport.group_chat import (
      GroupChatScheduler,
      ProactiveChatScheduler,
      GroupChatContext,
      get_llm_semaphore,
      sanitize_qq_reply,
      filter_narration,
      is_duplicate,
      _get_recent_bot_replies,
      _setup_memory_ctx,
  )
"""
from .transport.group_chat import *  # noqa: F403
