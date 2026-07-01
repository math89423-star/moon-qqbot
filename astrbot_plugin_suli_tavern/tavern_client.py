"""酒馆 API 客户端 — 已迁移到 service/ 层。

此文件保留为 re-export shim，新代码请使用:
  from .service.tavern_client import TavernClient, DEFAULT_CHARACTER, _scan_world_book
"""
from .service.tavern_client import *  # noqa: F403
