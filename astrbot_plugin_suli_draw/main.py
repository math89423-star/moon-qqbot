"""astrbot_plugin_suli_draw — AstrBot Star 入口 (纯库插件)

此插件是纯 Python 库，供 astrbot_plugin_suli_tavern import 使用。
此 main.py 仅为满足 AstrBot v4.25+ 的插件加载器要求，
使其被加载为 Python 包后可供其他插件导入。
"""
from astrbot.api.star import register, Star, Context


@register("astrbot_plugin_suli_draw", "粟藜", "AI 绘图客户端 (纯库)", "1.0.0")
class PluginMain(Star):
    async def initialize(self) -> None:
        pass
