"""表情包发送 — 已迁移到 service/ 层，对接 meme_manager 共享图库。

此文件保留为 re-export shim，新代码请使用:
  from .service.sticker_sender import (
      set_sticker_context, clear_sticker_context,
      send_sticker_by_tag, send_sticker_direct,
      get_available_tags, get_category_summary,
  )
"""
from .service.sticker_sender import *  # noqa: F403
