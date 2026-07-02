"""粟藜视频服务层 — B站视频链接解析 + 下载。

纯库插件，供其他 AstrBot 插件 import 使用。

用法:
  from astrbot_plugin_suli_video.bilibili import get_bilibili_info, download_bilibili_video

  # 获取元数据 (不下文件)
  info = await get_bilibili_info("https://www.bilibili.com/video/BV1xx411c7mD")
  print(info["title"], info["duration"], info["formats"])

  # 下载视频
  path = await download_bilibili_video(url, target_dir="/tmp", max_mb=20)
  if path:
      print(f"已下载: {path}")
"""

from .bilibili import download_bilibili_video, get_bilibili_info

__all__ = [
    "download_bilibili_video",
    "get_bilibili_info",
]
