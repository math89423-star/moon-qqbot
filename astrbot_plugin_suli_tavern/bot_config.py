"""Bot 配置服务 — 已迁移到 service/ 层。

此文件保留为 re-export shim，新代码请使用:
  from .service.bot_config import get_config_service, BotConfigService
"""
from .service.bot_config import *  # noqa: F403
