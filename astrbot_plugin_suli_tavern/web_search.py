"""联网搜索 — 已迁移到 service/ 层。

此文件保留为 re-export shim，新代码请使用:
  from .service.web_search import web_search, format_web_results
"""
from .service.web_search import *  # noqa: F403
