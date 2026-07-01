"""共享插件注册表 — 在 B 路线插件间实现依赖注入。

暮恩插件间的依赖注入不使用 AstrBot 的插件发现 API,
而是通过此模块级注册表进行:
  - proactive 插件注册自身 → suli_tavern 注入 GroupChatScheduler + TavernClient

这避免了循环导入, 且两个插件启动顺序无关 (注册在 __init__,
注入在 initialize() —— AstrBot 保证所有插件的 __init__ 先于 initialize() 执行)。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_proactive_plugin: Any = None


def register_proactive_plugin(plugin: Any) -> None:
    """注册主动行为插件实例 (由 suli_proactive 在 __init__ 时调用)。"""
    global _proactive_plugin
    _proactive_plugin = plugin
    logger.info("plugin_registry: 主动插件已注册")


def get_proactive_plugin() -> Any:
    """获取已注册的主动行为插件实例 (由 suli_tavern 在 initialize() 时调用)。"""
    return _proactive_plugin
