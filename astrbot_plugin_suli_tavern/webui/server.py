"""统一 WebUI 服务器 — 管理面板 API + SPA 静态文件服务。

设计:
  - 基于 aiohttp, 运行在插件 asyncio 事件循环中 (无需独立进程)
  - 提供 Vue 3 SPA 静态文件 + /api/admin/* REST API
  - 保留 /api/config/* 向后兼容 (别名到 /api/admin/*)
  - 直接读写 BotConfigService / BotDatabase (none_qqbot.db)
  - 端口可配置, 默认 6190

用法:
    from .webui.server import ConfigWebUI
    webui = ConfigWebUI(config_service, port=6190)
    await webui.start()
    # ... bot running ...
    await webui.stop()
"""

from __future__ import annotations

import json
import logging
import time as time_module
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from ..service.bot_config import BotConfigService

logger = logging.getLogger(__name__)

_API_PREFIX = "/api/admin"
_API_LEGACY_PREFIX = "/api/config"

# SPA 构建产物目录
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# ── MIME 类型映射 ────────────────────────────────────────────

_MIME_TYPES = {
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".json": "application/json",
    ".html": "text/html",
}


# ── JSON 响应工具 ───────────────────────────────────────────


def _json(data, status=200):
    return web.Response(
        text=json.dumps(data, ensure_ascii=False, default=str),
        content_type="application/json",
        charset="utf-8",
        status=status,
    )


def _json_error(message: str, status=400):
    return _json({"error": True, "message": message}, status=status)


# ── 认证中间件 ───────────────────────────────────────────────


@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.Response:
    """对所有 /api/ 路由进行 Bearer token 认证。

    白名单:
      - 非 API 路由: 直接放行
      - /api/*/login: 登录端点无需认证
      - 本地访问 (127.0.0.1 / localhost / ::1 / 192.168.x.x): 跳过认证
    其余 /api/ 路由需 Authorization: Bearer <admin_token>。
    """
    path = request.path

    # 非 API 路由 — 不检查
    if not path.startswith("/api/"):
        return await handler(request)

    # 登录端点白名单
    if path in (_API_PREFIX + "/login", _API_LEGACY_PREFIX + "/login") and request.method == "POST":
        return await handler(request)

    # 本地/局域网访问 — 跳过认证 (开发 & 自托管场景)
    peer = request.transport.get_extra_info("peername")
    if peer:
        host = peer[0]
        if host in ("127.0.0.1", "::1", "localhost") or host.startswith("192.168."):
            return await handler(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return _json_error("未授权: 缺少 Bearer token", 401)

    token = auth_header[7:]
    config_service: BotConfigService = request.app["config_service"]

    if not token or not config_service.verify_token(token):
        return _json_error("未授权: token 无效", 401)

    return await handler(request)


# ── 服务器 ──────────────────────────────────────────────────


class ConfigWebUI:
    """统一管理 WebUI 服务器 — SPA + REST API。

    属性:
        config_service: BotConfigService 单例
        port: 监听端口 (默认 6190)
        group_chat_handler: 可选的 GroupChatScheduler 引用 (用于白名单实时同步)
    """

    # Bot 身份数据统一从 BotIdentityService 读取。
    # 以下仅为向后兼容保留的 fallback 值。
    _DEFAULT_BOT_FALLBACK = ""

    @property
    def DEFAULT_BOT(self) -> str:
        """返回默认 bot 的 QQ 号 (第一个活跃 bot)。"""
        try:
            from ..service.bot_identity import get_bot_identity_service
            svc = get_bot_identity_service()
            bots = svc.list_bots(active_only=True)
            if bots:
                return bots[0].bot_id
        except Exception:
            pass
        return self._DEFAULT_BOT_FALLBACK

    def _get_bot_meta(self, bot_id: str) -> dict:
        """获取 bot 的元数据 (name/icon/color)。兼容旧 BOT_META 访问。"""
        try:
            from ..service.bot_identity import get_bot_identity_service
            svc = get_bot_identity_service()
            bot = svc.get_bot(bot_id)
            if bot:
                return {"name": bot.name, "icon": bot.icon, "color": bot.color}
        except Exception:
            pass
        return {}

    @classmethod
    def _bot_key(cls, bot_id: str, key: str) -> str:
        return f"bot:{bot_id}:{key}"

    def _get_bot_config(self, bot_id: str, key: str, default: str = "") -> str:
        return self.config_service.db.get_config(self._bot_key(bot_id, key), default)

    def _set_bot_config(self, bot_id: str, key: str, value: str) -> None:
        self.config_service.db.set_config(self._bot_key(bot_id, key), value)

    def _resolve_bot_id(self, request: web.Request, data: dict | None = None) -> str:
        if data and data.get("bot_id"):
            return str(data["bot_id"]).strip()
        q = request.query.get("bot_id", "").strip()
        return q or self.DEFAULT_BOT

    def __init__(
        self,
        config_service: BotConfigService,
        port: int = 6190,
        host: str = "0.0.0.0",
        group_chat_handler=None,
    ):
        self.config_service = config_service
        self.port = port
        self.host = host
        self.group_chat_handler = group_chat_handler
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    # ── 启动 / 停止 ─────────────────────────────────────

    async def start(self) -> None:
        self._app = web.Application(middlewares=[auth_middleware])
        self._app["config_service"] = self.config_service
        self._setup_routes()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        logger.info(
            "🔧 管理面板已就绪: http://%s:%d",
            self.host if self.host != "0.0.0.0" else "localhost",
            self.port,
        )

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            self._app = None
            logger.info("管理面板已关闭")

    # ── 路由注册 ─────────────────────────────────────────

    def _setup_routes(self) -> None:
        assert self._app is not None
        app: web.Application = self._app

        # ── 静态文件 & SPA ──────────────────────────
        app.router.add_get("/", self._serve_spa)
        app.router.add_get("/assets/{filename:.*}", self._serve_static)
        # 图标文件 (favicon 等)
        app.router.add_get("/icons.svg", self._serve_static_root)

        # ── 认证 ─────────────────────────────────────
        app.router.add_post(_API_PREFIX + "/login", self._login)
        app.router.add_post(_API_LEGACY_PREFIX + "/login", self._login)

        # ── Bot 管理 ─────────────────────────────────
        app.router.add_get(_API_PREFIX + "/bots", self._list_bots)
        app.router.add_post(_API_PREFIX + "/bots", self._create_bot)
        app.router.add_put(_API_PREFIX + r"/bots/{bot_id}", self._update_bot)
        app.router.add_delete(_API_PREFIX + r"/bots/{bot_id}", self._delete_bot)
        app.router.add_get(_API_PREFIX + "/characters", self._list_characters)
        app.router.add_get(_API_PREFIX + r"/characters/{name}", self._get_character)
        app.router.add_put(_API_PREFIX + r"/characters/{name}", self._update_character)
        app.router.add_post(_API_PREFIX + "/characters", self._create_character)
        app.router.add_delete(_API_PREFIX + r"/characters/{name}", self._delete_character)
        app.router.add_get(_API_PREFIX + "/bot-settings", self._get_bot_settings)
        app.router.add_post(_API_PREFIX + "/bot-settings", self._set_bot_settings)
        # 向后兼容
        app.router.add_get(_API_LEGACY_PREFIX + "/bot-list", self._list_bots)
        app.router.add_get(_API_LEGACY_PREFIX + "/bot-settings", self._get_bot_settings)
        app.router.add_post(_API_LEGACY_PREFIX + "/bot-settings", self._set_bot_settings)

        # ── LLM 配置 ─────────────────────────────────
        app.router.add_get(_API_PREFIX + "/llm/list", self._list_llm)
        app.router.add_get(_API_PREFIX + "/llm/active", self._get_active_llm)
        app.router.add_post(_API_PREFIX + "/llm/activate", self._activate_llm)
        app.router.add_post(_API_PREFIX + "/llm", self._create_llm)
        app.router.add_put(_API_PREFIX + r"/llm/{config_id:\d+}", self._update_llm)
        app.router.add_delete(_API_PREFIX + r"/llm/{config_id:\d+}", self._delete_llm)
        # 向后兼容
        app.router.add_get(_API_LEGACY_PREFIX + "/llm/list", self._list_llm)
        app.router.add_post(_API_LEGACY_PREFIX + "/llm/activate", self._activate_llm)

        # ── VLM 配置 ─────────────────────────────────
        app.router.add_get(_API_PREFIX + "/vlm/list", self._list_vlm)
        app.router.add_post(_API_PREFIX + "/vlm/activate", self._activate_vlm)
        app.router.add_post(_API_PREFIX + "/vlm", self._create_llm)
        app.router.add_put(_API_PREFIX + r"/vlm/{config_id:\d+}", self._update_llm)
        app.router.add_delete(_API_PREFIX + r"/vlm/{config_id:\d+}", self._delete_llm)
        # 向后兼容
        app.router.add_get(_API_LEGACY_PREFIX + "/vlm/list", self._list_vlm)
        app.router.add_post(_API_LEGACY_PREFIX + "/vlm/activate", self._activate_vlm)

        # ── 温度 ─────────────────────────────────────
        app.router.add_get(_API_PREFIX + "/temperature", self._get_temperatures)
        app.router.add_put(_API_PREFIX + "/temperature", self._set_temperatures)
        app.router.add_get(_API_LEGACY_PREFIX + "/temperatures", self._get_temperatures)
        app.router.add_post(_API_LEGACY_PREFIX + "/temperatures", self._set_temperatures)

        # ── 对话参数 ─────────────────────────────────
        app.router.add_get(_API_PREFIX + "/chat-params", self._get_chat_params)
        app.router.add_put(_API_PREFIX + "/chat-params", self._set_chat_params)
        app.router.add_get(_API_LEGACY_PREFIX + "/chat-params", self._get_chat_params)
        app.router.add_post(_API_LEGACY_PREFIX + "/chat-params", self._set_chat_params)

        # ── 工具设置 ─────────────────────────────────
        app.router.add_get(_API_PREFIX + "/tool-settings", self._get_tool_settings)
        app.router.add_post(_API_PREFIX + "/tool-settings", self._set_tool_settings)
        app.router.add_get(_API_LEGACY_PREFIX + "/tool-settings", self._get_tool_settings)
        app.router.add_post(_API_LEGACY_PREFIX + "/tool-settings", self._set_tool_settings)

        # ── Token 统计 ───────────────────────────────
        app.router.add_get(_API_PREFIX + "/token-stats", self._get_token_stats)
        app.router.add_get(_API_PREFIX + "/token-history", self._get_token_history)
        app.router.add_get(_API_PREFIX + "/token-budget", self._get_token_budget)
        app.router.add_post(_API_PREFIX + "/token-budget", self._set_token_budget)
        app.router.add_get(_API_LEGACY_PREFIX + "/token-stats", self._get_token_stats)

        # ── 数据库统计 ───────────────────────────────
        app.router.add_get(_API_PREFIX + "/status", self._get_status)
        app.router.add_get(_API_LEGACY_PREFIX + "/db-stats", self._get_db_stats)

        # ── 用户记忆 ─────────────────────────────────
        app.router.add_get(_API_PREFIX + "/memory/users", self._list_memory_users)
        app.router.add_get(_API_PREFIX + "/memory/search", self._search_memories)
        app.router.add_get(_API_PREFIX + "/memory/{user_id:[^/]+}", self._get_user_memories)
        app.router.add_delete(
            _API_PREFIX + "/memory/{user_id:[^/]+}/{fact_key:.+}",
            self._delete_user_fact,
        )

        # ── 知识库 ───────────────────────────────────
        app.router.add_get(_API_PREFIX + "/knowledge", self._list_knowledge)
        app.router.add_get(_API_PREFIX + r"/knowledge/{section_id:\d+}", self._get_knowledge_section)

        # ── 群聊白名单 ───────────────────────────────
        app.router.add_get(_API_PREFIX + "/whitelist", self._get_whitelist)
        app.router.add_post(_API_PREFIX + "/whitelist", self._add_whitelist)
        app.router.add_put(_API_PREFIX + r"/whitelist/{group_id:\d+}", self._update_whitelist)
        app.router.add_delete(_API_PREFIX + r"/whitelist/{group_id:\d+}", self._delete_whitelist)

        # ── Bot 检测 ─────────────────────────────────
        app.router.add_get(_API_PREFIX + "/bot-detect/list", self._list_suspected_bots)
        app.router.add_get(_API_PREFIX + "/bot-detect/live", self._get_live_detections)
        app.router.add_get(_API_PREFIX + "/bot-detect/{user_id:[^/]+}", self._get_suspected_bot)
        app.router.add_put(_API_PREFIX + "/bot-detect/{user_id:[^/]+}", self._update_suspected_bot)
        app.router.add_post(_API_PREFIX + "/bot-detect/{user_id:[^/]+}/reset", self._reset_suspected_bot)
        app.router.add_delete(_API_PREFIX + "/bot-detect/{user_id:[^/]+}", self._delete_suspected_bot)

        # ── 群聊总结 ─────────────────────────────────
        app.router.add_get(_API_PREFIX + "/summary", self._list_summary_groups)
        app.router.add_get(_API_PREFIX + r"/summary/{group_id:\d+}", self._get_group_summary)
        app.router.add_get(
            _API_PREFIX + r"/summary/{group_id:\d+}/history",
            self._get_group_summary_history,
        )

        # ── 插件发现 ──────────────────────────────
        app.router.add_get(_API_PREFIX + "/plugins", self._list_plugins)

        # ── 头像上传 ──────────────────────────────
        app.router.add_post(_API_PREFIX + "/avatars", self._upload_avatar)
        app.router.add_get("/avatars/{filename}", self._serve_avatar)

        # ── 表情包管理 ──────────────────────────────
        app.router.add_get(_API_PREFIX + "/memes/categories", self._list_meme_categories)
        app.router.add_get(_API_PREFIX + "/memes", self._list_memes)
        app.router.add_post(_API_PREFIX + "/memes/category/clear", self._clear_meme_category)
        app.router.add_post(_API_PREFIX + "/memes/category/delete", self._delete_meme_category)
        app.router.add_post(_API_PREFIX + "/memes/category/update-desc", self._update_meme_category_desc)
        app.router.add_get(_API_PREFIX + "/memes/sync/status", self._get_meme_sync_status)
        app.router.add_post(_API_PREFIX + "/memes/upload", self._upload_meme)
        app.router.add_post(_API_PREFIX + "/memes/delete", self._delete_meme)
        # 表情图片服务 (需放在 SPA fallback 之前)
        app.router.add_get("/memes/img/{category}/{filename:.+}", self._serve_meme_image)

        # ── 通用配置 ─────────────────────────────────
        app.router.add_get(_API_PREFIX + "/config", self._get_all_config)
        app.router.add_put(_API_PREFIX + "/config", self._set_config)

        # ── SPA 客户端路由 fallback ──────────────────
        app.router.add_get("/{route:.*}", self._serve_spa_fallback)

    # ═══════════════════════════════════════════════════════
    # 静态文件
    # ═══════════════════════════════════════════════════════

    async def _serve_spa(self, request: web.Request) -> web.Response:
        """提供 SPA 入口 index.html (no-cache 防旧版本残留)。"""
        index_path = _STATIC_DIR / "index.html"
        if not index_path.exists():
            return web.Response(
                text="SPA 未构建。请运行: cd frontend && npm run build",
                status=503,
            )
        return web.Response(
            text=index_path.read_text(encoding="utf-8"),
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    async def _serve_static(self, request: web.Request) -> web.Response:
        """提供 /assets/* 静态文件 (含 no-cache 头，防浏览器缓存旧版本)。"""
        filename = request.match_info.get("filename", "")
        # 路径遍历防护: 拒绝 ".." 和绝对路径
        if ".." in filename or filename.startswith("/"):
            return _json_error("Not found", 404)
        allowed_dir = (_STATIC_DIR / "assets").resolve()
        file_path = (allowed_dir / filename).resolve()
        # 确保解析后的路径在 allowed_dir 内 (兼容 Windows \ vs /)
        import os as _os
        _allowed = str(allowed_dir).replace("\\", "/").rstrip("/") + "/"
        _actual = str(file_path).replace("\\", "/")
        if not _actual.startswith(_allowed) and _actual != str(allowed_dir).replace("\\", "/"):
            return _json_error("Not found", 404)
        if not file_path.exists() or not file_path.is_file():
            return _json_error("Not found", 404)
        content_type = _MIME_TYPES.get(file_path.suffix, "application/octet-stream")
        return web.Response(
            body=file_path.read_bytes(),
            content_type=content_type,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    async def _serve_static_root(self, request: web.Request) -> web.Response:
        """提供根目录静态文件 (icons.svg 等)。"""
        path = request.path.lstrip("/")
        # 路径遍历防护
        if ".." in path:
            return await self._serve_spa(request)
        allowed_dir = _STATIC_DIR.resolve()
        file_path = (allowed_dir / path).resolve()
        # 兼容 Windows \ vs /
        _allowed = str(allowed_dir).replace("\\", "/").rstrip("/") + "/"
        _actual = str(file_path).replace("\\", "/")
        if not _actual.startswith(_allowed) and _actual != str(allowed_dir).replace("\\", "/"):
            return await self._serve_spa(request)
        if not file_path.exists() or not file_path.is_file():
            return await self._serve_spa(request)
        content_type = _MIME_TYPES.get(file_path.suffix, "application/octet-stream")
        return web.Response(
            body=file_path.read_bytes(),
            content_type=content_type,
        )

    async def _serve_spa_fallback(self, request: web.Request) -> web.Response:
        """SPA 客户端路由 fallback — 非 API 路径返回 index.html。"""
        # 只对非 API 路径生效
        if request.path.startswith("/api/"):
            return _json_error("Not found", 404)
        return await self._serve_spa(request)

    # ═══════════════════════════════════════════════════════
    # 认证
    # ═══════════════════════════════════════════════════════

    async def _login(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            token = (data.get("token") or "").strip()
        except Exception:
            return _json_error("请求格式错误", 400)
        if not token:
            return _json_error("缺少 token", 400)

        # 本地访问 + token="local" → 免密登录
        if token == "local":
            peer = request.transport.get_extra_info("peername")
            if peer and peer[0] in ("127.0.0.1", "::1", "localhost"):
                return _json({"ok": True})

        if self.config_service.verify_token(token):
            return _json({"ok": True})
        return _json_error("认证失败", 401)

    # ═══════════════════════════════════════════════════════
    # Bot 管理
    # ═══════════════════════════════════════════════════════

    async def _list_bots(self, request: web.Request) -> web.Response:
        """返回所有已注册 bot 的列表 (从 bot_identity 表读取)。"""
        try:
            from ..service.bot_identity import get_bot_identity_service
            svc = get_bot_identity_service()
            bots = svc.list_bots(active_only=False)
            return _json({
                "bots": [
                    {
                        "id": b.bot_id, "name": b.name,
                        "icon": b.icon, "color": b.color,
                        "avatar": b.metadata.get("avatar", ""),
                        "character_card": b.character_card,
                        "nicknames": b.nicknames,
                        "is_active": b.is_active,
                        "peer_bot_ids": b.peer_bot_ids,
                        "role_description": b.role_description,
                        "rejection_style": b.rejection_style if b.rejection_style else {},
                        "llm_slots": list(b.llm_slots) if b.llm_slots else [],
                    }
                    for b in bots
                ],
                "default_bot": self.DEFAULT_BOT,
            })
        except Exception:
            logger.error("获取 bot 列表失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _get_bot_settings(self, request: web.Request) -> web.Response:
        try:
            bot_id = self._resolve_bot_id(request)
            settings = {
                "bot_id": bot_id,
                "bot_name": self._get_bot_meta(bot_id).get("name", bot_id),
                "group_chat_enabled": self._get_bot_config(bot_id, "group_chat_enabled", "true") == "true",
                "private_chat_enabled": self._get_bot_config(bot_id, "private_chat_enabled", "true") == "true",
                "reasoning_enabled": self._get_bot_config(bot_id, "reasoning_enabled", "true") == "true",
                "shadow_agent_enabled": self._get_bot_config(bot_id, "shadow_agent_enabled", "true") == "true",
                "shadow_agent_model": self._get_bot_config(bot_id, "shadow_agent_model", ""),
                "owner_qq": self._get_bot_config(bot_id, "owner_qq", str(self.config_service.config.super_admin_qq if hasattr(self.config_service, 'config') else "")),
                "llm_slots": {},
                "vlm_slots": {},
            }
            for slot in self.config_service.get_display_llm_slots(bot_id):
                raw = self._get_bot_config(bot_id, slot, "")
                settings["llm_slots"][slot] = int(raw) if raw else None
            for slot in self.config_service.VLM_SLOTS:
                raw = self._get_bot_config(bot_id, slot, "")
                settings["vlm_slots"][slot] = int(raw) if raw else None
            return _json(settings)
        except Exception:
            logger.error("获取 bot 设置失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _set_bot_settings(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            bot_id = self._resolve_bot_id(request, data)
            updated = []
            for key in ("group_chat_enabled", "private_chat_enabled", "reasoning_enabled", "shadow_agent_enabled"):
                if key in data:
                    val = "true" if data[key] else "false"
                    self._set_bot_config(bot_id, key, val)
                    updated.append(key)
            if "shadow_agent_model" in data:
                self._set_bot_config(bot_id, "shadow_agent_model", str(data["shadow_agent_model"] or ""))
                updated.append("shadow_agent_model")
            if "owner_qq" in data:
                self._set_bot_config(bot_id, "owner_qq", str(data["owner_qq"] or ""))
                updated.append("owner_qq")
            return _json({"ok": True, "updated": updated, "bot_id": bot_id})
        except Exception:
            logger.error("设置 bot 设置失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ── Bot 身份 CRUD ──────────────────────────────

    async def _create_bot(self, request: web.Request) -> web.Response:
        """创建新 bot 身份。"""
        try:
            data = await request.json()
            from ..service.bot_identity import BotIdentity, get_bot_identity_service
            svc = get_bot_identity_service()
            identity = BotIdentity(
                bot_id=str(data["bot_id"]).strip(),
                name=data.get("name", "").strip(),
                character_card=data.get("character_card", "").strip(),
                nicknames=data.get("nicknames", []),
                is_active=data.get("is_active", True),
                peer_bot_ids=data.get("peer_bot_ids", []),
                icon=data.get("icon", "🤖"),
                color=data.get("color", "#666666"),
                role_description=data.get("role_description", "").strip(),
                rejection_style=data.get("rejection_style", {}),
                llm_slots=tuple(data.get("llm_slots", ())),
                metadata=data.get("metadata", {}),
            )
            # 头像 URL (可来自上传或外部链接)
            if data.get("avatar"):
                identity.metadata["avatar"] = data["avatar"]
            if not identity.bot_id or not identity.name:
                return _json_error("bot_id 和 name 为必填项", 400)
            ok = svc.create_bot(identity)
            if not ok:
                return _json_error("Bot 已存在或创建失败", 409)
            logger.info("Bot 已创建: %s (%s)", identity.bot_id, identity.name)
            return _json({"ok": True, "bot_id": identity.bot_id}, 201)
        except Exception:
            logger.error("创建 bot 失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _update_bot(self, request: web.Request) -> web.Response:
        """更新 bot 身份。"""
        try:
            bot_id = request.match_info["bot_id"]
            data = await request.json()
            from ..service.bot_identity import BotIdentity, get_bot_identity_service
            svc = get_bot_identity_service()
            existing = svc.get_bot(bot_id)
            if not existing:
                return _json_error("Bot 不存在", 404)
            # Merge fields
            existing.name = data.get("name", existing.name)
            existing.character_card = data.get("character_card", existing.character_card)
            existing.nicknames = data.get("nicknames", existing.nicknames)
            if "is_active" in data:
                existing.is_active = data["is_active"]
            existing.peer_bot_ids = data.get("peer_bot_ids", existing.peer_bot_ids)
            existing.icon = data.get("icon", existing.icon)
            existing.color = data.get("color", existing.color)
            existing.role_description = data.get("role_description", existing.role_description)
            if "rejection_style" in data:
                existing.rejection_style = data["rejection_style"]
            if "llm_slots" in data:
                existing.llm_slots = tuple(data["llm_slots"])
            if "metadata" in data:
                existing.metadata = data["metadata"]
            # 头像 URL
            if data.get("avatar") is not None:
                existing.metadata["avatar"] = data["avatar"]
            # 将已更新的字段同步回 metadata JSON（直接赋值覆盖旧值）
            existing.metadata["icon"] = existing.icon
            existing.metadata["color"] = existing.color
            existing.metadata["role_description"] = existing.role_description
            existing.metadata["rejection_style"] = existing.rejection_style
            if existing.llm_slots:
                existing.metadata["llm_slots"] = list(existing.llm_slots)
            ok = svc.update_bot(existing)
            if not ok:
                return _json_error("更新失败", 500)
            logger.info("Bot 已更新: %s (%s)", bot_id, existing.name)
            return _json({"ok": True})
        except Exception:
            logger.error("更新 bot 失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _delete_bot(self, request: web.Request) -> web.Response:
        """删除 bot 身份。"""
        try:
            bot_id = request.match_info["bot_id"]
            from ..service.bot_identity import get_bot_identity_service
            svc = get_bot_identity_service()
            existing = svc.get_bot(bot_id)
            if not existing:
                return _json_error("Bot 不存在", 404)
            svc.delete_bot(bot_id)
            logger.info("Bot 已删除: %s (%s)", bot_id, existing.name)
            return _json({"ok": True})
        except Exception:
            logger.error("删除 bot 失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _list_characters(self, request: web.Request) -> web.Response:
        """列出 characters/ 目录下可用的角色卡文件。"""
        try:
            from pathlib import Path as _Path
            char_dir = _Path(__file__).resolve().parent.parent / "characters"
            chars: list[dict] = []
            if char_dir.exists():
                for f in sorted(char_dir.glob("*.json")):
                    if f.stem.endswith("_world_book") or f.stem.startswith("example"):
                        continue
                    # 尝试读取角色卡名
                    display_name = f.stem
                    try:
                        import json as _json_lib
                        with open(f, encoding="utf-8") as _fh:
                            card = _json_lib.load(_fh)
                        display_name = card.get("data", {}).get("name", f.stem)
                    except Exception:
                        pass
                    chars.append({"name": f.stem, "display_name": display_name})
            return _json({"characters": chars})
        except Exception:
            logger.error("获取角色卡列表失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ── 角色卡 CRUD ────────────────────────────────

    async def _get_character(self, request: web.Request) -> web.Response:
        """读取单个角色卡完整 JSON: GET /api/admin/characters/{name}"""
        try:
            name = request.match_info.get("name", "")
            if not name or "/" in name or "\\" in name:
                return _json_error("无效的角色卡名", 400)
            char_dir = Path(__file__).resolve().parent.parent / "characters"
            path = char_dir / f"{name}.json"
            if not path.exists():
                return _json_error(f"角色卡 '{name}' 不存在", 404)
            with open(path, encoding="utf-8") as f:
                card = json.load(f)
            return _json({"name": name, "card": card})
        except Exception:
            logger.error("读取角色卡失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _update_character(self, request: web.Request) -> web.Response:
        """更新角色卡 JSON 文件: PUT /api/admin/characters/{name}"""
        try:
            name = request.match_info.get("name", "")
            if not name or "/" in name or "\\" in name:
                return _json_error("无效的角色卡名", 400)
            char_dir = Path(__file__).resolve().parent.parent / "characters"
            path = char_dir / f"{name}.json"
            if not path.exists():
                return _json_error(f"角色卡 '{name}' 不存在", 404)

            data = await request.json()
            card = data.get("card", data)  # 支持 {card: {...}} 或直接 {...}

            # 基本校验
            if not isinstance(card, dict):
                return _json_error("请求体必须是 JSON 对象", 400)
            card_data = card.get("data", {})
            if not card_data.get("name"):
                return _json_error("角色卡必须有 data.name 字段", 400)

            # 原子写入: tmp file → rename
            tmp_path = path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(card, f, ensure_ascii=False, indent=2)
                f.write("\n")
            tmp_path.replace(path)

            logger.info("角色卡已更新: %s", name)
            return _json({"ok": True, "name": name})
        except Exception:
            logger.error("更新角色卡失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _create_character(self, request: web.Request) -> web.Response:
        """创建新角色卡: POST /api/admin/characters"""
        try:
            data = await request.json()
            card_name = (data.get("name") or "").strip()
            if not card_name:
                return _json_error("缺少 name 字段", 400)
            if "/" in card_name or "\\" in card_name:
                return _json_error("无效的角色卡名", 400)

            char_dir = Path(__file__).resolve().parent.parent / "characters"
            path = char_dir / f"{card_name}.json"
            if path.exists():
                return _json_error(f"角色卡 '{card_name}' 已存在", 409)

            # 如果提供了完整 JSON → 使用；否则创建最小骨架
            full_card = data.get("card") or data.get("data")
            if full_card and isinstance(full_card, dict):
                if "data" in full_card and "spec" in full_card:
                    card = full_card
                else:
                    card = {
                        "spec": "chara_card_v3",
                        "spec_version": "3.0",
                        "data": full_card,
                    }
            else:
                display_name = data.get("display_name", card_name)
                card = {
                    "spec": "chara_card_v3",
                    "spec_version": "3.0",
                    "data": {
                        "name": display_name,
                        "description": "",
                        "personality": "",
                        "scenario": "",
                        "talkativeness": "0.5",
                        "first_mes": "",
                        "mes_example": "",
                        "system_prompt": "",
                        "group_persona": "",
                        "group_mes_example": "",
                        "post_history_instructions": "",
                        "role_description": "",
                        "kaomoji_rule": "",
                        "sticker_guide": "",
                        "companion_rules": "",
                        "tags": [],
                        "creator_notes": "",
                        "character_version": "1.0.0",
                    },
                }

            char_dir.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(card, f, ensure_ascii=False, indent=2)
                f.write("\n")

            logger.info("角色卡已创建: %s", card_name)
            return _json({"ok": True, "name": card_name}, status=201)
        except Exception:
            logger.error("创建角色卡失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _delete_character(self, request: web.Request) -> web.Response:
        """删除角色卡文件: DELETE /api/admin/characters/{name}"""
        try:
            name = request.match_info.get("name", "")
            if not name or "/" in name or "\\" in name:
                return _json_error("无效的角色卡名", 400)

            char_dir = Path(__file__).resolve().parent.parent / "characters"
            path = char_dir / f"{name}.json"
            if not path.exists():
                return _json_error(f"角色卡 '{name}' 不存在", 404)

            # 检查是否有 bot 使用此角色卡
            try:
                from ..service.bot_identity import get_bot_identity_service
                svc = get_bot_identity_service()
                using_bots = [
                    b.name for b in svc.list_bots(active_only=False)
                    if b.character_card.lower() == name.lower()
                ]
                if using_bots:
                    return _json_error(
                        f"角色卡正被以下 Bot 使用: {', '.join(using_bots)}。请先解除关联后再删除。",
                        400,
                    )
            except ImportError:
                pass  # 服务不可用时跳过检查

            path.unlink()
            logger.info("角色卡已删除: %s", name)
            return _json({"ok": True, "name": name})
        except Exception:
            logger.error("删除角色卡失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # LLM 配置
    # ═══════════════════════════════════════════════════════

    def _llm_config_to_dict(self, c) -> dict:
        masked = (
            c.api_key[:4] + "****" + c.api_key[-4:]
            if len(c.api_key) > 8
            else ("****" if c.api_key else "")
        )
        return {
            "id": c.id,
            "name": c.name,
            "provider": c.provider,
            "provider_name": c.provider_name,
            "model_name": c.model_name,
            "base_url": c.base_url,
            "is_active": c.is_active,
            "is_vlm": c.is_vlm,
            "is_llm": c.is_llm,
            "config_type": c.config_type,
            "api_key_preview": masked,
            "api_key_masked": masked,   # 前端字段名
        }

    async def _list_llm(self, request: web.Request) -> web.Response:
        try:
            bot_id = self._resolve_bot_id(request)
            configs = self.config_service.db.list_llm_configs()
            slots = {}
            for slot in self.config_service.get_display_llm_slots(bot_id):
                raw = self._get_bot_config(bot_id, slot, "")
                slots[slot] = int(raw) if raw else None
            result = [self._llm_config_to_dict(c) for c in configs if c.is_llm]
            return _json({
                "configs": result,
                "slots": slots,
                "bot_id": bot_id,
                "bot_name": self._get_bot_meta(bot_id).get("name", bot_id),
            })
        except Exception:
            logger.error("列出 LLM 配置失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _get_active_llm(self, request: web.Request) -> web.Response:
        """获取当前活跃 LLM/VLM 配置 (SPA 兼容)。"""
        try:
            bot_id = self._resolve_bot_id(request)
            llm_id = self.config_service.get_active_llm_id(bot_id)
            vlm_id = self.config_service.get_active_vlm_id(bot_id)
            result: dict[str, object] = {"active_llm_id": llm_id, "active_vlm_id": vlm_id}
            if llm_id:
                cfg = self.config_service.db.get_llm_config(llm_id)
                if cfg:
                    result["llm"] = self._llm_config_to_dict(cfg)
            if vlm_id:
                cfg = self.config_service.db.get_llm_config(vlm_id)
                if cfg:
                    result["vlm"] = self._llm_config_to_dict(cfg)
            return _json(result)
        except Exception:
            logger.error("获取活跃 LLM 失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _activate_llm(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            bot_id = self._resolve_bot_id(request, data)
            config_id = data.get("config_id")
            slot = data.get("slot", "llm_primary")
            llm_type = data.get("llm_type", "llm")  # SPA sends llm_type

            if config_id is None:
                return _json_error("缺少 config_id")
            cid = int(config_id)
            if cid == 0:
                if slot in self.config_service.LLM_SLOTS or slot in self.config_service.VLM_SLOTS:
                    # 先取旧 config_id 用于同步 is_active
                    old_cid = self.config_service.get_llm_slot(bot_id, slot) if slot in self.config_service.LLM_SLOTS else self.config_service.get_vlm_slot(bot_id, slot)
                    self._set_bot_config(bot_id, slot, "")
                    if old_cid and old_cid > 0:
                        self.config_service._sync_is_active_for_config(old_cid)
                else:
                    # 兼容旧 SPA activateLLM
                    key = "active_llm_id" if llm_type == "llm" else "active_vlm_id"
                    self._set_bot_config(bot_id, key, "")
                return _json({"ok": True, "slot": slot, "cleared": True, "bot_id": bot_id})

            cfg = self.config_service.db.get_llm_config(cid)
            if not cfg:
                return _json_error("配置不存在")

            # 判断是 LLM 还是 VLM 槽位
            if slot in self.config_service.LLM_SLOTS:
                if not cfg.is_llm:
                    return _json_error("该配置不是 LLM")
                self.config_service.set_llm_slot(bot_id, slot, cid)
            elif slot in self.config_service.VLM_SLOTS:
                if not cfg.is_vlm:
                    return _json_error("该配置不是 VLM")
                self.config_service.set_vlm_slot(bot_id, slot, cid)
            else:
                # 兼容旧 SPA: llm_type + config_id
                self.config_service.set_llm_slot(bot_id, "llm_primary", cid)

            return _json({"ok": True, "slot": slot, "config_id": cid, "bot_id": bot_id})
        except Exception:
            logger.error("设置 LLM 槽位失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _create_llm(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            name = data.get("name", "").strip()
            if not name:
                return _json_error("名称为必填项")
            config_id = self.config_service.db.add_llm_config(
                name=name,
                provider=data.get("provider", "custom"),
                provider_name=data.get("provider_name", ""),
                api_key=data.get("api_key", ""),
                base_url=data.get("base_url", ""),
                model_name=data.get("model_name", ""),
                config_type=data.get("config_type", ""),
                is_active=True,  # 面板创建的配置默认启用
            )
            return _json({"ok": True, "id": config_id})
        except Exception:
            logger.error("创建 LLM 配置失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _update_llm(self, request: web.Request) -> web.Response:
        try:
            config_id = int(request.match_info["config_id"])
            data = await request.json()
            fields = {}
            for k in ("name", "provider", "provider_name", "api_key", "base_url", "model_name", "config_type"):
                if k in data and data[k] is not None:
                    fields[k] = data[k]
            ok = self.config_service.db.update_llm_config(config_id, **fields)
            return _json({"ok": ok})
        except Exception:
            logger.error("更新 LLM 配置失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _delete_llm(self, request: web.Request) -> web.Response:
        try:
            config_id = int(request.match_info["config_id"])
            ok = self.config_service.db.delete_llm_config(config_id)
            return _json({"ok": ok})
        except Exception:
            logger.error("删除 LLM 配置失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # VLM 配置
    # ═══════════════════════════════════════════════════════

    async def _list_vlm(self, request: web.Request) -> web.Response:
        try:
            bot_id = self._resolve_bot_id(request)
            configs = self.config_service.db.list_llm_configs()
            vlm_configs = [c for c in configs if c.is_vlm]
            slots = {}
            for slot in self.config_service.VLM_SLOTS:
                raw = self._get_bot_config(bot_id, slot, "")
                slots[slot] = int(raw) if raw else None
            result = [self._llm_config_to_dict(c) for c in vlm_configs]
            return _json({
                "configs": result,
                "slots": slots,
                "bot_id": bot_id,
                "bot_name": self._get_bot_meta(bot_id).get("name", bot_id),
            })
        except Exception:
            logger.error("列出 VLM 配置失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _activate_vlm(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            bot_id = self._resolve_bot_id(request, data)
            config_id = data.get("config_id")
            slot = data.get("slot", "vlm_primary")
            if config_id is None:
                return _json_error("缺少 config_id")
            cid = int(config_id)
            if cid == 0:
                old_cid = self.config_service.get_vlm_slot(bot_id, slot)
                self._set_bot_config(bot_id, slot, "")
                if old_cid and old_cid > 0:
                    self.config_service._sync_is_active_for_config(old_cid)
                return _json({"ok": True, "slot": slot, "cleared": True, "bot_id": bot_id})
            cfg = self.config_service.db.get_llm_config(cid)
            if not cfg or not cfg.is_vlm:
                return _json_error("该配置不是 VLM")
            self.config_service.set_vlm_slot(bot_id, slot, cid)
            return _json({"ok": True, "slot": slot, "config_id": cid, "bot_id": bot_id})
        except Exception:
            logger.error("设置 VLM 槽位失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # 温度
    # ═══════════════════════════════════════════════════════

    async def _get_temperatures(self, request: web.Request) -> web.Response:
        try:
            bot_id = self._resolve_bot_id(request)
            temps = self.config_service.get_all_temperatures(bot_id)
            return _json({"temperatures": temps, "bot_id": bot_id})
        except Exception:
            logger.error("获取温度失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _set_temperatures(self, request: web.Request) -> web.Response:
        try:
            bot_id = self._resolve_bot_id(request)
            data = await request.json()
            # 支持两种格式: 直接的 dict 或 {"temperatures": {...}}
            if "temperatures" in data:
                data = data["temperatures"]
            if not isinstance(data, dict):
                return _json_error("请求格式错误，需要 JSON 对象")
            valid_keys = {
                "tavern_group", "memory_extract",
                "context_compress", "cross_validation",
            }
            temps = {k: float(v) for k, v in data.items() if k in valid_keys}
            if not temps:
                return _json_error("没有合法的温度参数")
            self.config_service.set_all_temperatures(bot_id, temps)
            return _json({"ok": True, "temperatures": self.config_service.get_all_temperatures(bot_id), "bot_id": bot_id})
        except Exception:
            logger.error("设置温度失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # 对话参数
    # ═══════════════════════════════════════════════════════

    async def _get_chat_params(self, request: web.Request) -> web.Response:
        try:
            bot_id = self._resolve_bot_id(request)
            params = self.config_service.get_all_chat_params(bot_id)
            return _json({"params": params, "bot_id": bot_id})
        except Exception:
            logger.error("获取对话参数失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _set_chat_params(self, request: web.Request) -> web.Response:
        try:
            bot_id = self._resolve_bot_id(request)
            data = await request.json()
            if "params" in data:
                data = data["params"]
            if not isinstance(data, dict):
                return _json_error("请求格式错误，需要 JSON 对象")
            updated = 0
            for key, value in data.items():
                if key in self.config_service._CHAT_PARAM_META:
                    self.config_service.set_chat_param(bot_id, key, value)
                    updated += 1
            return _json({"ok": True, "updated": updated, "bot_id": bot_id})
        except Exception:
            logger.error("设置对话参数失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # 工具设置
    # ═══════════════════════════════════════════════════════

    async def _get_tool_settings(self, request: web.Request) -> web.Response:
        try:
            bot_id = self._resolve_bot_id(request)
            settings = self.config_service.get_all_tool_settings(bot_id)
            tools = self.config_service.get_unified_tool_list(bot_id)
            return _json({"tool_settings": settings, "tools": tools, "bot_id": bot_id})
        except Exception:
            logger.error("获取工具设置失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _set_tool_settings(self, request: web.Request) -> web.Response:
        try:
            bot_id = self._resolve_bot_id(request)
            data = await request.json()
            updated_global = 0
            updated_tools = 0

            # ── 全局工具设置 ──
            global_data = data.get("tool_settings", None)
            if global_data and isinstance(global_data, dict):
                valid_keys = set(self.config_service._TOOL_META.keys())
                for key, value in global_data.items():
                    if key in valid_keys:
                        self.config_service.set_tool_setting(bot_id, key, value)
                        updated_global += 1

            # ── per-tool 启停 ──
            tools_data = data.get("tools", None)
            if tools_data and isinstance(tools_data, dict):
                valid_names = {t["name"] for t in self.config_service._UNIFIED_TOOLS}
                for tool_name, tool_info in tools_data.items():
                    if tool_name in valid_names:
                        if isinstance(tool_info, dict):
                            # 新格式: {name: {enabled: bool, min_affinity: int}}
                            if "enabled" in tool_info:
                                _enabled = tool_info["enabled"]
                                if isinstance(_enabled, bool):
                                    pass
                                elif isinstance(_enabled, str):
                                    _enabled = _enabled.lower() in ("true", "1", "yes")
                                else:
                                    _enabled = bool(_enabled)
                                self.config_service.set_tool_enabled(
                                    bot_id, tool_name, _enabled,
                                )
                                updated_tools += 1
                            if "min_affinity" in tool_info:
                                try:
                                    self.config_service.set_tool_min_affinity(
                                        bot_id, tool_name, int(tool_info["min_affinity"]),
                                    )
                                    updated_tools += 1
                                except (ValueError, TypeError):
                                    pass
                        else:
                            # 旧格式: {name: bool} (兼容)
                            if isinstance(tool_info, bool):
                                pass
                            elif isinstance(tool_info, str):
                                tool_info = tool_info.lower() in ("true", "1", "yes")
                            else:
                                tool_info = bool(tool_info)
                            self.config_service.set_tool_enabled(bot_id, tool_name, tool_info)
                            updated_tools += 1

            return _json({
                "ok": True,
                "updated_global": updated_global,
                "updated_tools": updated_tools,
                "tool_settings": self.config_service.get_all_tool_settings(bot_id),
                "tools": self.config_service.get_unified_tool_list(bot_id),
                "bot_id": bot_id,
            })
        except Exception:
            logger.error("设置工具设置失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # Token 统计
    # ═══════════════════════════════════════════════════════

    async def _get_token_stats(self, request: web.Request) -> web.Response:
        try:
            period = request.query.get("period", "today")
            stats = self.config_service.db.get_token_stats(period)
            cache = self.config_service.db.get_cache_rate(period)
            stats["cache_hit_rate"] = cache.get("hit_rate", 0) if isinstance(cache, dict) else 0
            return _json(stats)
        except Exception:
            logger.error("获取 token 统计失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _get_token_history(self, request: web.Request) -> web.Response:
        try:
            days = int(request.query.get("days", "7"))
            history = self.config_service.db.get_token_history(days=days)
            return _json(history)
        except Exception:
            logger.error("获取 token 历史失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _get_token_budget(self, request: web.Request) -> web.Response:
        """读取 per-bot token 预算配置 + VLM 缓存统计（返回 M 为单位）。"""
        try:
            cfg = self.config_service.db.get_token_budget_config()
            result = {
                "loput_hard_limit_m": round(cfg["loput_hard_limit"] / 1_000_000, 1),
                "loput_soft_limit_m": round(cfg["loput_soft_limit"] / 1_000_000, 1),
                "luna_hard_limit_m": round(cfg["luna_hard_limit"] / 1_000_000, 1),
                "luna_soft_limit_m": round(cfg["luna_soft_limit"] / 1_000_000, 1),
            }
            # VLM 缓存统计 (跨 bot 共享，SHA-256 字节级匹配)
            try:
                from astrbot_plugin_suli_services.vision import get_vlm_cache_stats
                result["vlm_cache"] = get_vlm_cache_stats()
            except Exception:
                pass  # VLM 模块不可用时静默跳过
            return _json(result)
        except Exception:
            logger.error("获取 token 预算配置失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _set_token_budget(self, request: web.Request) -> web.Response:
        """保存 per-bot token 预算配置（前端传 M 为单位）。"""
        try:
            data = await request.json()
            self.config_service.db.set_token_budget_config(data)
            return _json({"ok": True})
        except Exception:
            logger.error("保存 token 预算配置失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # 状态 & DB 统计
    # ═══════════════════════════════════════════════════════

    def _build_group_chat_status(self, bot_id: str) -> dict:
        """从白名单 + group_chat_enabled 组装群聊状态。

        面板是独立容器，无 bot 运行时连接，数据来自 DB/JSON 持久层。
        """
        try:
            whitelist = self.config_service.db.get_whitelist(bot_id)
            group_key = f"bot:{bot_id}:group_chat_enabled" if bot_id else "group_chat_enabled"
            enabled = self.config_service.db.get_config(group_key, "") == "true"
            active_groups = list(whitelist.keys()) if enabled else []
            return {
                "group_count": len(active_groups),
                "active_groups": active_groups,
                "enabled": enabled,
            }
        except Exception:
            return {"group_count": 0, "active_groups": [], "enabled": False}

    async def _get_status(self, request: web.Request) -> web.Response:
        try:
            db_stats = self.config_service.db.get_stats()
            bot_id = self._resolve_bot_id(request)
            # LLM/VLM 活跃 ID: 优先旧 key (active_llm_id/active_vlm_id),
            # 未设置时回退到槽位系统 (llm_lite / vlm_primary)
            llm_id = self.config_service.get_active_llm_id(bot_id)
            if llm_id is None:
                llm_id = self.config_service.get_llm_slot(bot_id, "llm_lite")
            vlm_id = self.config_service.get_active_vlm_id(bot_id)
            if vlm_id is None:
                vlm_id = self.config_service.get_vlm_slot(bot_id, "vlm_primary")
            group_chat = self._build_group_chat_status(bot_id)
            return _json({
                "timestamp": int(time_module.time()),
                "db": db_stats,
                "llm": {"active_llm_id": llm_id, "active_vlm_id": vlm_id},
                "group_chat": group_chat,
            })
        except Exception:
            logger.error("获取状态失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _get_db_stats(self, request: web.Request) -> web.Response:
        try:
            stats = self.config_service.db.get_stats()
            return _json({"stats": stats})
        except Exception:
            logger.error("获取数据库统计失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # 用户记忆
    # ═══════════════════════════════════════════════════════

    async def _list_memory_users(self, request: web.Request) -> web.Response:
        try:
            page = int(request.query.get("page", "1"))
            per_page = int(request.query.get("per_page", "20"))
            bot_id = request.query.get("bot_id", "").strip()
            users, total = self.config_service.db.get_memory_users(
                page=page, per_page=per_page, bot_id=bot_id,
            )
            return _json({"users": users, "total": total, "page": page, "per_page": per_page})
        except Exception:
            logger.error("列出记忆用户失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _search_memories(self, request: web.Request) -> web.Response:
        try:
            q = request.query.get("q", "")
            top_n = int(request.query.get("top_n", "20"))
            bot_id = request.query.get("bot_id", "").strip()
            results = self.config_service.db.search_user_memories(q, top_n=top_n, bot_id=bot_id)
            return _json({"results": results, "query": q, "count": len(results)})
        except Exception:
            logger.error("搜索记忆失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _get_user_memories(self, request: web.Request) -> web.Response:
        try:
            user_id = request.match_info["user_id"]
            page = int(request.query.get("page", "1"))
            per_page = int(request.query.get("per_page", "50"))
            bot_id = request.query.get("bot_id", "").strip()
            facts, total = self.config_service.db.get_user_memories(
                user_id, page=page, per_page=per_page, bot_id=bot_id,
            )
            return _json({"facts": facts, "total": total, "page": page, "per_page": per_page})
        except Exception:
            logger.error("获取用户记忆失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _delete_user_fact(self, request: web.Request) -> web.Response:
        try:
            user_id = request.match_info["user_id"]
            fact_key = request.match_info["fact_key"]
            bot_id = request.query.get("bot_id", "").strip()
            ok = self.config_service.db.delete_user_fact(user_id, fact_key, bot_id=bot_id)
            return _json({"ok": ok})
        except Exception:
            logger.error("删除用户记忆失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # 知识库
    # ═══════════════════════════════════════════════════════

    async def _list_knowledge(self, request: web.Request) -> web.Response:
        try:
            source = request.query.get("source", "")
            page = int(request.query.get("page", "1"))
            per_page = int(request.query.get("per_page", "50"))
            sections, total = self.config_service.db.get_knowledge_sections(
                source=source, page=page, per_page=per_page,
            )
            sources = self.config_service.db.get_knowledge_sources()
            return _json({
                "sections": sections,
                "total": total,
                "page": page,
                "per_page": per_page,
                "sources": sources,
            })
        except Exception:
            logger.error("列出知识库失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _get_knowledge_section(self, request: web.Request) -> web.Response:
        try:
            section_id = int(request.match_info["section_id"])
            section = self.config_service.db.get_knowledge_section(section_id)
            if section is None:
                return _json_error("章节不存在", 404)
            return _json({"section": section})
        except Exception:
            logger.error("获取知识库章节失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # 群聊白名单 (per-bot)
    # ═══════════════════════════════════════════════════════

    async def _get_whitelist(self, request: web.Request) -> web.Response:
        """GET /api/admin/whitelist?bot_id=... → {whitelist: [...], all_bots: {...}}

        支持 per-bot 过滤: bot_id=<main_bot_qq> 只返回该 bot 的。
        不传 bot_id 返回 all_bots 合并视图 (每群一行, 标记哪些 bot 启用了它)。
        """
        try:
            bot_id = request.query.get("bot_id", "").strip()
            if bot_id:
                wl = self.config_service.db.get_whitelist(bot_id=bot_id)
                entries = [
                    {"group_id": gid, "tier": tier}
                    for gid, tier in sorted(wl.items())
                ]
                return _json({"whitelist": entries, "bot_id": bot_id})
            # 合并视图: 返回 each bot 的白名单
            all_bots = self.config_service.db.get_all_bot_whitelists()
            return _json({"whitelist": [], "all_bots": all_bots})
        except Exception:
            logger.error("获取白名单失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _add_whitelist(self, request: web.Request) -> web.Response:
        """POST /api/admin/whitelist — body: {group_id, tier, bot_id?}"""
        try:
            data = await request.json()
            group_id = int(data.get("group_id", 0))
            tier = data.get("tier", "basic")
            bot_id = str(data.get("bot_id", "") or "").strip()
            if not group_id:
                return _json_error("缺少 group_id")
            if not bot_id:
                return _json_error("缺少 bot_id")
            self.config_service.db.set_whitelist_entry(group_id, tier, bot_id=bot_id)
            # 同步到调度器内存 (仅当 bot_id 匹配当前运行时)
            if self.group_chat_handler:
                try:
                    current_bot = getattr(self.group_chat_handler, '_current_bot_id', '')
                    if not current_bot or current_bot == bot_id:
                        self.group_chat_handler.set_group_tier(group_id, tier)
                except Exception:
                    logger.warning("同步白名单到调度器失败", exc_info=True)
            return _json({"ok": True, "group_id": group_id, "tier": tier, "bot_id": bot_id})
        except Exception:
            logger.error("添加白名单失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _update_whitelist(self, request: web.Request) -> web.Response:
        """PUT /api/admin/whitelist/{group_id} — body: {tier, bot_id?}"""
        try:
            group_id = int(request.match_info["group_id"])
            data = await request.json()
            tier = data.get("tier", "basic")
            bot_id = str(data.get("bot_id", "") or "").strip()
            if not bot_id:
                return _json_error("缺少 bot_id")
            self.config_service.db.set_whitelist_entry(group_id, tier, bot_id=bot_id)
            if self.group_chat_handler:
                try:
                    current_bot = getattr(self.group_chat_handler, '_current_bot_id', '')
                    if not current_bot or current_bot == bot_id:
                        self.group_chat_handler.set_group_tier(group_id, tier)
                except Exception:
                    logger.warning("同步白名单到调度器失败", exc_info=True)
            return _json({"ok": True, "group_id": group_id, "tier": tier, "bot_id": bot_id})
        except Exception:
            logger.error("更新白名单失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _delete_whitelist(self, request: web.Request) -> web.Response:
        """DELETE /api/admin/whitelist/{group_id}?bot_id=..."""
        try:
            group_id = int(request.match_info["group_id"])
            bot_id = request.query.get("bot_id", "").strip()
            if not bot_id:
                return _json_error("缺少 bot_id")
            ok = self.config_service.db.remove_whitelist_entry(group_id, bot_id=bot_id)
            if ok and self.group_chat_handler:
                try:
                    current_bot = getattr(self.group_chat_handler, '_current_bot_id', '')
                    if not current_bot or current_bot == bot_id:
                        self.group_chat_handler.disable_group(group_id)
                except Exception:
                    logger.warning("同步白名单删除到调度器失败", exc_info=True)
            return _json({"ok": ok})
        except Exception:
            logger.error("删除白名单失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # Bot 检测
    # ═══════════════════════════════════════════════════════

    async def _list_suspected_bots(self, request: web.Request) -> web.Response:
        try:
            status = request.query.get("status", "")
            limit = int(request.query.get("limit", "50"))
            bots = self.config_service.db.list_suspected_bots(status=status, limit=limit)
            return _json({"bots": bots})
        except Exception:
            logger.error("列出疑似 Bot 失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _get_live_detections(self, request: web.Request) -> web.Response:
        try:
            from astrbot_plugin_suli_guards.bot_detector import BotDetector
            live = BotDetector.get_all_flagged()
            total = len(live)
            action_count = sum(1 for item in live if item.get("action_taken"))
            return _json({
                "live": live,
                "total_tracked": total,
                "action_taken_count": action_count,
            })
        except ImportError:
            return _json({"live": [], "total_tracked": 0, "action_taken_count": 0})
        except Exception:
            logger.error("获取实时检测状态失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _get_suspected_bot(self, request: web.Request) -> web.Response:
        try:
            user_id = request.match_info["user_id"]
            bot = self.config_service.db.get_suspected_bot(user_id)
            if bot is None:
                return _json_error("未找到该用户", 404)
            return _json({"bot": bot})
        except Exception:
            logger.error("获取疑似 Bot 详情失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _update_suspected_bot(self, request: web.Request) -> web.Response:
        try:
            user_id = request.match_info["user_id"]
            data = await request.json()
            fields = {}
            if "status" in data:
                fields["status"] = data["status"]
            if "notes" in data:
                fields["notes"] = data["notes"]
            ok = self.config_service.db.update_suspected_bot(user_id, **fields)
            return _json({"ok": ok, "bot": self.config_service.db.get_suspected_bot(user_id)})
        except Exception:
            logger.error("更新疑似 Bot 失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _reset_suspected_bot(self, request: web.Request) -> web.Response:
        try:
            user_id = request.match_info["user_id"]
            self.config_service.db.remove_suspected_bot(user_id)
            return _json({"ok": True})
        except Exception:
            logger.error("重置疑似 Bot 失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _delete_suspected_bot(self, request: web.Request) -> web.Response:
        try:
            user_id = request.match_info["user_id"]
            ok = self.config_service.db.remove_suspected_bot(user_id)
            return _json({"ok": ok})
        except Exception:
            logger.error("删除疑似 Bot 记录失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # 群聊总结
    # ═══════════════════════════════════════════════════════

    async def _list_summary_groups(self, request: web.Request) -> web.Response:
        try:
            groups = self.config_service.db.get_summary_groups()
            return _json({"groups": groups, "count": len(groups)})
        except Exception:
            logger.error("列出摘要群失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _get_group_summary(self, request: web.Request) -> web.Response:
        try:
            group_id = int(request.match_info["group_id"])
            summary = self.config_service.db.get_latest_group_summary(group_id)
            if summary is None:
                return _json({"group_id": group_id, "summary_text": None})
            return _json(summary)
        except Exception:
            logger.error("获取群总结失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _get_group_summary_history(self, request: web.Request) -> web.Response:
        try:
            group_id = int(request.match_info["group_id"])
            limit = int(request.query.get("limit", "20"))
            summaries = self.config_service.db.get_group_summary_history(group_id, limit=limit)
            return _json({
                "group_id": group_id,
                "summaries": summaries,
                "count": len(summaries),
            })
        except Exception:
            logger.error("获取群总结历史失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # 通用配置
    # ═══════════════════════════════════════════════════════

    async def _get_all_config(self, request: web.Request) -> web.Response:
        try:
            config = self.config_service.get_all()
            return _json({"config": config})
        except Exception:
            logger.error("获取配置失败", exc_info=True)
            return _json_error("Internal server error", 500)

    async def _set_config(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            kv = data.get("data", data)
            if not isinstance(kv, dict):
                return _json_error("请求格式错误")
            for k, v in kv.items():
                self.config_service.set(k, str(v))
            return _json({"ok": True})
        except Exception:
            logger.error("设置配置失败", exc_info=True)
            return _json_error("Internal server error", 500)

    # ═══════════════════════════════════════════════════════
    # 插件发现 — 动态探测已安装增强插件及其管理页面
    # ═══════════════════════════════════════════════════════

    async def _list_plugins(self, request: web.Request) -> web.Response:
        """返回已安装的增强插件清单及其前端管理页面信息。

        SPA 启动时调用此接口，根据返回结果动态注册路由和导航项。
        核心插件 (tavern/gate/guards/routing/emotion/memory/pipeline/context/proactive)
        不在列表中——它们不提供独立管理页面。

        增强插件通过约定的 webui/templates/<name>.html 提供管理页面，
        面板服务器负责路由到这些独立 HTML 文件。
        """
        try:
            from pathlib import Path
            # 面板容器无 AstrBot 框架，用相对路径 (CWD=/AstrBot)
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path
                data_plugins = Path(get_astrbot_data_path()) / "plugins"
            except ModuleNotFoundError:
                data_plugins = Path("data/plugins")

            plugins: list[dict] = []

            # suli_meme — 表情包管理
            # 检测顺序: (1) AstrBot data/plugins (2) 兄弟目录 (纯净包分发版)
            meme_dir = data_plugins / "astrbot_plugin_suli_meme"
            if not meme_dir.is_dir():
                # 回退: 检查与 tavern 同级的 meme 插件目录
                sibling = Path(__file__).resolve().parent.parent.parent / "astrbot_plugin_suli_meme"
                if sibling.is_dir():
                    meme_dir = sibling
            if meme_dir.is_dir():
                plugins.append({
                    "id": "suli_meme",
                    "name": "表情包管理",
                    "route": "/memes",
                    "icon": "smile",
                    "type": "enhanced",
                    "has_page": True,
                    "description": "表情包类别管理、浏览、同步",
                })

            return _json({"plugins": plugins})
        except Exception:
            logger.error("获取插件列表失败", exc_info=True)
            return _json_error("内部错误", 500)

    # ═══════════════════════════════════════════════════════
    # 头像上传
    # ═══════════════════════════════════════════════════════

    _AVATAR_DIR = Path(__file__).resolve().parent.parent.parent.parent / "avatars"
    _AVATAR_MAX_SIZE = 512 * 1024  # 512 KB
    _AVATAR_ALLOWED_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

    async def _serve_avatar(self, request: web.Request) -> web.Response:
        """提供上传的头像图片文件。"""
        try:
            filename = request.match_info["filename"]
            if ".." in filename or "/" in filename:
                raise web.HTTPNotFound()
            self._AVATAR_DIR.mkdir(parents=True, exist_ok=True)
            file_path = self._AVATAR_DIR / filename
            if not file_path.is_file():
                raise web.HTTPNotFound()
            ext = file_path.suffix.lower()
            mime_map = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".gif": "image/gif",
                ".webp": "image/webp",
            }
            return web.Response(
                body=file_path.read_bytes(),
                content_type=mime_map.get(ext, "application/octet-stream"),
            )
        except web.HTTPNotFound:
            raise
        except Exception:
            logger.error("提供头像图片失败", exc_info=True)
            raise web.HTTPNotFound()

    async def _upload_avatar(self, request: web.Request) -> web.Response:
        """上传 bot 头像图片。返回可访问的 URL。"""
        try:
            reader = await request.multipart()
            field = await reader.next()
            if not field:
                return _json_error("未收到文件", 400)
            # 读入内存
            data = await field.read()
            if len(data) > self._AVATAR_MAX_SIZE:
                return _json_error(f"文件过大 (最大 {self._AVATAR_MAX_SIZE // 1024} KB)", 413)
            # 确定文件名和扩展名
            original_name = field.filename or "avatar.png"
            ext = Path(original_name).suffix.lower()
            if ext not in self._AVATAR_ALLOWED_EXTS:
                return _json_error(f"不支持的格式: {ext}，支持 {', '.join(sorted(self._AVATAR_ALLOWED_EXTS))}", 400)
            import uuid
            safe_name = f"{uuid.uuid4().hex[:12]}{ext}"
            self._AVATAR_DIR.mkdir(parents=True, exist_ok=True)
            (self._AVATAR_DIR / safe_name).write_bytes(data)
            url = f"/avatars/{safe_name}"
            logger.info("头像已上传: %s (%d bytes)", safe_name, len(data))
            return _json({"url": url, "filename": safe_name, "size": len(data)})
        except Exception:
            logger.error("头像上传失败", exc_info=True)
            return _json_error("上传失败，请重试", 500)

    async def _serve_meme_image(self, request: web.Request) -> web.Response:
        """提供表情图片文件 — 从 plugin_data 目录读取。"""
        try:
            from pathlib import Path
            category = request.match_info["category"]
            filename = request.match_info["filename"]
            # 安全检查
            if ".." in category or ".." in filename or "/" in category:
                raise web.HTTPNotFound()
            from astrbot_plugin_suli_meme.config import MEMES_DIR as _MEMES_DIR
            file_path = Path(_MEMES_DIR) / category / filename
            if not file_path.is_file():
                raise web.HTTPNotFound()
            ext = file_path.suffix.lower()
            mime_map = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".gif": "image/gif",
                ".webp": "image/webp",
            }
            return web.Response(
                body=file_path.read_bytes(),
                content_type=mime_map.get(ext, "application/octet-stream"),
            )
        except web.HTTPNotFound:
            raise
        except Exception:
            logger.error("提供表情图片失败", exc_info=True)
            raise web.HTTPNotFound()

    # ═══════════════════════════════════════════════════════
    # 表情包管理
    # ═══════════════════════════════════════════════════════

    async def _list_meme_categories(self, request: web.Request) -> web.Response:
        """列出表情类别及其图片数量 — 直接读文件系统，不依赖 AstrBot。"""
        try:
            import os
            from pathlib import Path
            from astrbot_plugin_suli_meme.backend.category_manager import CategoryManager
            from astrbot_plugin_suli_meme.config import MEMES_DATA_PATH, MEMES_DIR
            cm = CategoryManager()
            descriptions = cm.get_descriptions()
            memes_dir = Path(MEMES_DIR)
            categories: dict[str, dict] = {}
            if memes_dir.is_dir():
                for entry in sorted(memes_dir.iterdir()):
                    if entry.is_dir() and not entry.name.startswith("."):
                        count = len([f for f in entry.iterdir() if f.is_file()])
                        categories[entry.name] = {
                            "name": entry.name, "count": count,
                            "description": descriptions.get(entry.name, ""),
                        }
            return _json({"categories": sorted(categories.values(), key=lambda c: c["name"])})
        except Exception:
            logger.error("列出表情类别失败", exc_info=True)
            return _json_error("内部错误", 500)

    async def _list_memes(self, request: web.Request) -> web.Response:
        """列出表情图片，支持按类别过滤 — 直接读文件系统。"""
        try:
            from pathlib import Path
            from astrbot_plugin_suli_meme.config import MEMES_DIR
            cat_filter = request.query.get("category", "").strip()
            memes_dir = Path(MEMES_DIR)
            items: list[dict] = []
            search_dir = memes_dir / cat_filter if cat_filter else memes_dir
            if search_dir.is_dir():
                for f in sorted(search_dir.iterdir()):
                    if f.is_file() and not f.name.startswith("."):
                        items.append({
                            "file": f"{cat_filter}/{f.name}" if cat_filter else f.name,
                            "name": f.stem,
                        })
            return _json({"items": items, "total": len(items)})
        except Exception:
            logger.error("列出表情失败", exc_info=True)
            return _json_error("内部错误", 500)

    async def _clear_meme_category(self, request: web.Request) -> web.Response:
        """清空指定类别的所有表情图片。"""
        try:
            data = await request.json()
            category = str(data.get("category", "")).strip()
            if not category:
                return _json_error("缺少 category 参数")
            # 安全检查: 类别名不能含路径分隔符
            if "/" in category or "\\" in category or ".." in category:
                return _json_error("无效的类别名")
            from astrbot_plugin_suli_meme.backend.models import clear_category_emojis
            clear_category_emojis(category)
            return _json({"ok": True, "category": category})
        except Exception:
            logger.error("清空表情类别失败", exc_info=True)
            return _json_error("内部错误", 500)

    async def _delete_meme_category(self, request: web.Request) -> web.Response:
        """删除整个表情类别（目录 + 配置）。"""
        try:
            data = await request.json()
            category = str(data.get("category", "")).strip()
            if not category:
                return _json_error("缺少 category 参数")
            if "/" in category or "\\" in category or ".." in category:
                return _json_error("无效的类别名")
            from astrbot_plugin_suli_meme.backend.category_manager import CategoryManager
            from astrbot_plugin_suli_meme.config import MEMES_DATA_PATH, MEMES_DIR
            cm = CategoryManager()
            cm.delete_category(category)
            return _json({"ok": True, "category": category})
        except Exception:
            logger.error("删除表情类别失败", exc_info=True)
            return _json_error("内部错误", 500)

    async def _update_meme_category_desc(self, request: web.Request) -> web.Response:
        """更新表情类别描述。"""
        try:
            data = await request.json()
            category = str(data.get("category", "")).strip()
            description = str(data.get("description", ""))
            if not category:
                return _json_error("缺少 category 参数")
            from astrbot_plugin_suli_meme.backend.category_manager import CategoryManager
            from astrbot_plugin_suli_meme.config import MEMES_DATA_PATH, MEMES_DIR
            cm = CategoryManager()
            cm.update_description(category, description)
            return _json({"ok": True, "category": category})
        except Exception:
            logger.error("更新类别描述失败", exc_info=True)
            return _json_error("内部错误", 500)

    async def _get_meme_sync_status(self, request: web.Request) -> web.Response:
        """获取表情包配置与文件系统同步状态。"""
        try:
            from astrbot_plugin_suli_meme.config import MEMES_DATA_PATH, MEMES_DIR
            from astrbot_plugin_suli_meme.backend.category_manager import CategoryManager
            cm = CategoryManager()
            missing_in_config, deleted_categories = cm.get_sync_status()
            return _json({
                "missing_in_config": missing_in_config,
                "deleted_categories": deleted_categories,
            })
        except Exception:
            logger.error("获取同步状态失败", exc_info=True)
            return _json_error("内部错误", 500)

    async def _upload_meme(self, request: web.Request) -> web.Response:
        """上传单张表情图片到指定类别。"""
        try:
            # aiohttp multipart: 读取表单数据
            reader = await request.multipart()
            category = None
            file_field = None
            file_name = None
            file_content = None

            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == "category":
                    category = (await part.text()).strip()
                elif part.name == "file":
                    file_name = part.filename
                    file_content = await part.read()

            if not category:
                return _json_error("缺少 category 参数")
            if not file_content:
                return _json_error("未选择文件或文件为空")
            if "/" in category or "\\" in category or ".." in category:
                return _json_error("无效的类别名")

            # 安全检查
            if not file_name:
                file_name = "uploaded.png"
            if ".." in file_name or "/" in file_name or "\\" in file_name:
                return _json_error("无效的文件名")

            from pathlib import Path
            from astrbot_plugin_suli_meme.config import MEMES_DIR

            # 确保类别目录存在
            cat_path = Path(MEMES_DIR) / category
            cat_path.mkdir(parents=True, exist_ok=True)

            # 处理重复文件名
            dest = cat_path / file_name
            stem = dest.stem
            suffix = dest.suffix.lower()
            counter = 1
            while dest.exists():
                dest = cat_path / f"{stem}_{counter}{suffix}"
                counter += 1
                if counter > 1000:
                    return _json_error("文件名冲突过多，请重命名后重试")

            dest.write_bytes(file_content)

            # 确保 CategoryManager 记录此类别（目录已存在则 update_description 无副作用）
            from astrbot_plugin_suli_meme.backend.category_manager import CategoryManager
            cm = CategoryManager()
            try:
                cm.update_description(category, cm.get_descriptions().get(category, ""))
            except Exception:
                pass  # 类别描述文件更新失败不阻塞上传

            logger.info("表情上传成功: %s/%s (%d bytes)", category, dest.name, len(file_content))
            return _json({
                "ok": True,
                "category": category,
                "filename": dest.name,
                "size": len(file_content),
            })
        except Exception:
            logger.error("上传表情失败", exc_info=True)
            return _json_error("上传失败: 内部错误", 500)

    async def _delete_meme(self, request: web.Request) -> web.Response:
        """删除单张表情图片。"""
        try:
            data = await request.json()
            category = str(data.get("category", "")).strip()
            filename = str(data.get("filename", "")).strip()

            if not category or not filename:
                return _json_error("缺少 category 或 filename 参数")
            if "/" in category or "\\" in category or ".." in category:
                return _json_error("无效的类别名")
            if ".." in filename or "/" in filename or "\\" in filename:
                return _json_error("无效的文件名")

            from pathlib import Path
            from astrbot_plugin_suli_meme.config import MEMES_DIR

            file_path = Path(MEMES_DIR) / category / filename
            if not file_path.is_file():
                return _json_error("文件不存在", 404)

            file_path.unlink()
            logger.info("表情删除成功: %s/%s", category, filename)

            # 如果类别目录为空，可选清理目录
            cat_dir = Path(MEMES_DIR) / category
            if cat_dir.is_dir():
                remaining = [f for f in cat_dir.iterdir() if f.is_file()]
                if not remaining:
                    cat_dir.rmdir()

            return _json({"ok": True, "category": category, "filename": filename})
        except Exception:
            logger.error("删除表情失败", exc_info=True)
            return _json_error("删除失败: 内部错误", 500)
