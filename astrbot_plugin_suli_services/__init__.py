"""暮恩基础服务层 — VLM 识图 + 联网搜索 + 知识库 + 缓存优化 + Pixiv 搜图。

纯库插件，供其他 AstrBot 插件 import 使用。

提供:
  - vision 模块:           VLM 图片描述 + 意图检测
  - web_search 模块:       SearXNG 搜索 + DuckDuckGo 降级
  - knowledge_base 模块:   BGE-M3 向量检索 + 关键词回退
  - cache_optimizer 模块:  LLM 上下文压缩 + GPT/DeepSeek 缓存命中优化
  - pixiv_search 模块:     Pixiv 插画搜索 (pixivpy3 + refresh_token 认证)

用法:
  from astrbot_plugin_suli_services import web_search, format_web_results
  from astrbot_plugin_suli_services.vision import describe_image_from_url
  from astrbot_plugin_suli_services import KnowledgeBase, get_knowledge_base
  from astrbot_plugin_suli_services.cache_optimizer import ContextCompressor, CacheOptimizer
  from astrbot_plugin_suli_services import search_pixiv, format_pixiv_results
"""

from .cache_optimizer import CacheOptimizer, ContextCompressor
from .knowledge_base import KnowledgeBase, Section, add_web_result, get_knowledge_base, init_knowledge_dir, tokenize
from .pixiv_search import format_pixiv_results, mark_illust_shown, search_pixiv
from .web_search import format_web_results, web_search

__all__ = [
    "add_web_result",
    "CacheOptimizer",
    "ContextCompressor",
    "KnowledgeBase",
    "Section",
    "format_pixiv_results",
    "format_web_results",
    "get_knowledge_base",
    "init_knowledge_dir",
    "search_pixiv",
    "tokenize",
    "web_search",
]
