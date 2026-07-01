"""VLM 图片解析 — 已迁移到 service/ 层。

此文件保留为 re-export shim，新代码请使用:
  from .service.vision import has_active_vlm, describe_image_from_url, describe_images_from_urls, detect_image_intent, detect_reverse_prompt_intent, MAX_VLM_IMAGES
"""
from .service.vision import *  # noqa: F403
from .service.vision import _reset_vlm_usage  # noqa: F401 (import * 不导出 _ 前缀)
