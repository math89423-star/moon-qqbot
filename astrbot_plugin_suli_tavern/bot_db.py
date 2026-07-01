"""Bot 数据库 — 已迁移到 service/ 层。

此文件保留为 re-export shim，新代码请使用:
  from .service.bot_db import get_bot_db, BotDatabase, LLMConfigRO
"""
from .service.bot_db import *  # noqa: F403
