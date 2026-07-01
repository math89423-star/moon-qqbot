"""astrbot_plugin_suli_validation — AstrBot Star 入口 (纯库插件)

此插件是纯 Python 库，供 astrbot_plugin_suli_tavern import 使用。
此 main.py 仅为满足 AstrBot v4.25+ 的插件加载器要求，
使其被加载为 Python 包后可供其他插件导入。
"""
import sys, os
_PLUGINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from astrbot.api.star import register, Star, Context


@register("astrbot_plugin_suli_validation", "L-Port", "暮恩子插件 (纯库)", "1.0.0")
class PluginMain(Star):
    async def initialize(self) -> None:
        pass
