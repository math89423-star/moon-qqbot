"""管理面板独立容器入口 — 不依赖 AstrBot 框架, 仅需 aiohttp + sqlite3。

用法:
    python panel_main.py --port 5190 --host 0.0.0.0

依赖: aiohttp (Web 服务器), sqlite3 (标准库)
DB:   data/shared_db/none_qqbot.db (需挂载)
SPA:  static/ (同目录下的 Vue 3 构建产物)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

# 确保插件模块可导入 (panel_main.py 在 astrbot_plugin_suli_tavern/ 下)
_PLUGIN_DIR = Path(__file__).resolve().parent
_PARENT_DIR = _PLUGIN_DIR.parent
if str(_PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(_PARENT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Panel] %(levelname)s: %(message)s",
)
logger = logging.getLogger("moon-panel")


async def main() -> None:
    parser = argparse.ArgumentParser(description="暮恩管理面板 — 独立容器")
    parser.add_argument("--port", type=int, default=5190, help="监听端口 (默认 5190)")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    args = parser.parse_args()

    # 延迟导入 — 确保 sys.path 已就绪
    from astrbot_plugin_suli_tavern.service.bot_db import get_bot_db
    from astrbot_plugin_suli_tavern.service.bot_config import BotConfigService
    from astrbot_plugin_suli_tavern.webui.server import ConfigWebUI

    # 初始化 DB (路径: data/shared_db/none_qqbot.db — 由容器 WORKDIR + volume 决定)
    db = get_bot_db()
    logger.info("DB 已连接: %d 条 llm_config", len(db.list_llm_configs()))

    # ── Bot 身份注册中心 ──
    from astrbot_plugin_suli_tavern.service.bot_identity import get_bot_identity_service
    _identity_svc = get_bot_identity_service()
    _identity_svc.init_db(db)
    logger.info("BotIdentity 注册中心已初始化: %d bots", db.bot_identity_count())

    config_svc = BotConfigService()

    # 启动 WebUI (不注入 group_chat_handler — 面板独立于 bot 运行时)
    webui = ConfigWebUI(config_svc, port=args.port, host=args.host)
    await webui.start()
    logger.info("管理面板已就绪: http://localhost:%d", args.port)

    stop_event = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)
    except NotImplementedError:
        pass
    await stop_event.wait()

    logger.info("正在关闭...")
    await webui.stop()
    logger.info("管理面板已关闭")


if __name__ == "__main__":
    asyncio.run(main())
