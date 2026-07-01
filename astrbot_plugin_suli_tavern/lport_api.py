"""L-Port API 客户端 — 已迁移到 service/ 层。

此文件保留为 re-export shim，新代码请使用:
  from .service.lport_api import LPortClient
"""
from .service.lport_api import *  # noqa: F403
