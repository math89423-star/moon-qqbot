"""知识库检索 — 已迁移到 service/ 层。

此文件保留为 re-export shim，新代码请使用:
  from .service.knowledge_base import KnowledgeBase, get_knowledge_base, tokenize
"""
from .service.knowledge_base import *  # noqa: F403
