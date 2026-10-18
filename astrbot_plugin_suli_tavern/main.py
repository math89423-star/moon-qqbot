"""暮恩酒馆桥接 — AstrBot 版。

AstrBot 原生插件 — 保留全部自研基础设施：
  三层记忆 / Intent Gate (含 Grace) / Bot行为检测 / VLM 识图 / 工具调用

角色卡引擎基于 SillyTavern (酒馆)，AstrBot=大脑, ST=渲染器。
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import re as _re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_PLUGINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Forward, Plain
from astrbot.api.star import Context, Star, register

from ._astrbot_adapter import BotAdapter, EventAdapter
from .platform_adapter import get_adapter as _get_platform_adapter, init_adapter as _init_platform_adapter
from .adapters.qq_adapter import QQPlatformAdapter
from .config import Config
from .transport.group_chat import (
    GroupChatScheduler,
    _url_to_file_id,
    get_llm_semaphore,
    sanitize_qq_reply,
)
from .transport.proactive_speaker import ProactiveChatScheduler
from .service.vision import _download_image
from .tavern_client import CHARACTERS, DEFAULT_CHARACTER, TavernClient, get_character_for_self_id
from .tools import (
    cache_qq_image,
    set_force_reply_bypass,
)
from .vision import (
    MAX_VLM_IMAGES,
    _reset_vlm_usage,
    describe_images_from_urls,
    detect_image_intent,
    detect_reverse_prompt_intent,
    get_last_vlm_usage,
    has_active_vlm,
)

# ── Bot QQ 集合 (从 dual_bot 共享模块导入 — 单一真相源) ──
from astrbot_plugin_suli_guards.dual_bot import get_known_bot_ids, is_peer_bot

# ── 反推提示词缓存 ────────────────────────────────────
_reverse_prompt_cache: dict[str, str] = {}

# ── 刷屏日志降噪（无害 verbose 默认不显示，出问题开 debug 再看）──
# tavern 成功/preflight/通道预热已在各处改成 debug；
# 这里再把第三方（httpx/httpcore/uvicorn.access）的 INFO 直接压到 WARNING。
import logging as _logging  # noqa: E402, I001
for _noisy in ("httpx", "httpcore", "uvicorn.access", "mcp.server.streamable_http_manager"):
    try:  # noqa: SIM105
        _logging.getLogger(_noisy).setLevel(_logging.WARNING)
    except Exception:
        pass
del _noisy


def get_reverse_prompt_cache(key: str) -> str:
    return _reverse_prompt_cache.pop(key, "")


def set_reverse_prompt_cache(key: str, desc: str) -> None:
    _reverse_prompt_cache[key] = desc
    logger.info("反推缓存已存储: key=%s, len=%d", key, len(desc))


# ── 角色显示名 (per-bot) ──────────────────────────────
def _bot_display_name(self_id: str, fallback: str = "人家") -> str:
    """bot QQ → 角色显示名。单一真相源 = 角色卡 (原则 24)。

    用户可见文本里禁止写死任何一个 bot 的名字 —— 同一份代码服务多 bot,
    写死某个名字会让其他 bot 自称错名字。未知时返回中性称呼。
    """
    try:
        from .service.tavern_client import get_character_for_self_id

        card = get_character_for_self_id(str(self_id or ""))
        name = str((card or {}).get("name") or "").strip()
        return name or fallback
    except Exception:
        logger.debug("角色显示名解析失败 self_id=%s", self_id, exc_info=True)
        return fallback


# ── 合并转发消息缓存 ──────────────────────────────
# 委托给 service/forward_cache.py — 供 parse_forwarded_message 工具读取
from .service.forward_cache import cache_forward_content, get_cached_forward


# ── 模块级 monkey-patch: aiocqhttp reverse WS 强制 universal ──
# NapCat 默认 X-Client-Role: event → AstrBot 收得到发不出 (ApiNotAvailable)
# 尽早打补丁，确保插件 initialize() 之前就已生效
# ── 发送层加固安装器 (Patch 2 + Patch 3) ────────────────────
# Patch 2: WebSocketReverseApi.call_action 需要 self_id 才能路由到正确的 NapCat;
#          框架的 _dispatch_send 把 self_id 丢了 → 注入。
# Patch 3: 回执异常自愈 — ActionFailed retcode=1200 + EventChecker/1006514「网络连接异常」
#          实测 (13:35:09 群 100000003) NapCat 侧「发送 ->」正常, 异常来自 QQ 内核
#          回执层 → 属于「已送达但回执失败」。按致命错处理会丢上下文/自反馈/连发补充。
def _install_send_healer() -> None:
    # ── Patch 2: inject self_id into API calls ──
    # WebSocketReverseApi.call_action needs self_id to route to the
    # correct NapCat when there are 2+ clients. The framework's
    # _dispatch_send drops self_id from the event → inject it.
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )

    @classmethod
    async def _patched_dispatch_send(
        cls, bot, event, is_group, session_id, messages,
    ) -> None:
        session_id_int = (
            int(session_id) if session_id and str(session_id).isdigit() else None
        )
        # Inject self_id from the event so call_action routes correctly
        self_id = None
        if hasattr(event, "__getitem__"):
            try:
                self_id = str(event["self_id"])
            except (KeyError, TypeError):
                pass
        if not self_id:
            self_id = "*"  # wildcard: matches any registered API client
        if is_group and isinstance(session_id_int, int):
            await _send_healed(
                bot, bot.send_group_msg,
                {"group_id": session_id_int, "message": messages, "self_id": self_id},
                is_group=True, session_id=session_id_int, self_id=self_id,
            )
        elif not is_group and isinstance(session_id_int, int):
            await _send_healed(
                bot, bot.send_private_msg,
                {"user_id": session_id_int, "message": messages, "self_id": self_id},
                is_group=False, session_id=session_id_int, self_id=self_id,
            )
        elif hasattr(event, "__getitem__"):
            await _send_healed(
                bot, bot.send,
                {"event": event, "message": messages},
                is_group=False, session_id=None, self_id=self_id,
            )
        else:
            raise ValueError(
                "无法发送消息：缺少有效的数字 session_id 或 event"
            )

    AiocqhttpMessageEvent._dispatch_send = _patched_dispatch_send  # type: ignore[method-assign]

    # ── Patch 3: 发送回执异常自愈 (2026-10-10) ──
    # 症状: ActionFailed retcode=1200 + EventChecker Failed / result 1006514「网络连接异常」。
    # 实测 (13:35:09 群 100000003): NapCat 侧日志显示「发送 -> 群聊」正常,
    # 异常来自 QQ 内核的事件回执层 —— 即「已送达但回执失败」。
    # 若按致命错误处理, 后果是: 该轮回复不进上下文 / 不记自反馈 / 丢掉连发补充。
    # 策略: 先查历史确认是否已送达 (避免重发造成重复), 未送达才去引用重发一次;
    #       只有「1200 + flaky 特征」才自愈, 其他 ActionFailed (参数错/群不存在) 照旧抛出。
    from aiocqhttp.exceptions import ActionFailed as _ActionFailed
    try:
        from aiocqhttp.exceptions import ApiNotAvailable as _ApiNotAvailable
    except ImportError:  # 老版本 aiocqhttp 无此异常类时降级为通用兜底
        _ApiNotAvailable = None  # type: ignore[assignment]  # noqa: N806

    _flaky_hints = ("1006514", "EventChecker", "网络连接异常",
                      "Timeout: NTEvent", "NodeIKernelMsgService/sendMsg",
                      "onMsgInfoListUpdate", "Timeout")

    def _is_flaky_send_error(exc: Exception) -> bool:
        if getattr(exc, "retcode", None) != 1200:
            return False
        blob = f"{getattr(exc, 'message', '')}{getattr(exc, 'wording', '')}{exc}"
        return any(h in blob for h in _flaky_hints)

    def _first_text(msgs: list) -> str:
        for seg in msgs or []:
            _ps = _parse_raw_seg(seg)
            if _ps["kind"] == "text" and _ps["text"]:
                return _ps["text"][:16]
        return ""

    async def _delivered_already(
        bot, *, is_group: bool, session_id: int, head: str, self_id: str,
    ) -> bool | None:
        """回执异常时查历史确认是否其实已发出。True=已送达 / False=没发出去 / None=查不到。"""
        if not head:
            return None
        try:
            try:
                _ad = _get_platform_adapter()
            except RuntimeError:
                _ad = QQPlatformAdapter()
            if is_group:
                res = await _ad.call_platform_api(
                    "get_group_msg_history", bot, group_id=session_id, count=5, self_id=self_id,
                )
            else:
                res = await _ad.call_platform_api(
                    "get_friend_msg_history", bot, user_id=session_id, count=5, self_id=self_id,
                )
        except Exception:
            logger.debug("回执异常后查历史失败 (无法确认是否已送达)", exc_info=True)
            return None
        for m in (res or {}).get("messages", [])[-3:]:
            _hist_parts: list[str] = []
            for s in (m.get("message") or []):
                _ps = _parse_raw_seg(s)
                if _ps["kind"] == "text" and _ps["text"]:
                    _hist_parts.append(_ps["text"])
            txt = "".join(_hist_parts)
            if head and head in txt:
                return True
        return False

    async def _send_healed(bot, call, kwargs: dict, *, is_group: bool, session_id, self_id: str):
        """发送 + 回执异常自愈。返回原始结果 / None (已尽力)。"""
        _where = f"{'群' if is_group else '私聊'} {session_id}"
        try:
            return await call(**kwargs)
        except _ActionFailed as exc:
            if not _is_flaky_send_error(exc):
                raise
            logger.warning("QQ 回执异常 (%s), 先确认是否已送达: %.120s", _where, str(exc))
            head = _first_text(kwargs.get("message") or [])
            await asyncio.sleep(0.8)
            confirmed = (
                await _delivered_already(
                    bot, is_group=is_group, session_id=session_id,
                    head=head, self_id=self_id,
                )
                if session_id else None
            )
            if confirmed:
                logger.info("QQ 回执异常但消息已送达 (%s) → 不重发, 上下文照记", _where)
                return None
            # 未送达 / 无法确认 → 去掉引用段重发一次 (陈旧 reply id 是最常见触发源)
            _msgs = [
                m for m in (kwargs.get("message") or [])
                if not (isinstance(m, dict) and m.get("type") == "reply")
            ]
            try:
                logger.warning("QQ 重发一次 (%s) 已剥离引用段 (%d 段)", _where, len(_msgs))
                return await call(**{**kwargs, "message": _msgs})
            except _ActionFailed as exc2:
                logger.error(
                    "QQ 重发仍失败 (%s): %.120s | 若持续出现请检查 napcat 登录态与网络",
                    _where, str(exc2),
                )
                return None
            except Exception:
                logger.error("QQ 重发异常 (%s)", _where, exc_info=True)
                return None
        except Exception as exc0:
            # ApiNotAvailable：反向 WS 客户端没了（NapCat 断连/重启/掉登录态），
            # 消息肯定没发出去。等 1.5s 重试一次（瞬断可自愈），仍失败只记一行
            # ERROR，不抛长 traceback。上游按“已尽力”继续，避免整轮崩。
            if _ApiNotAvailable is not None and isinstance(exc0, _ApiNotAvailable):
                await asyncio.sleep(1.5)
                try:
                    return await call(**kwargs)
                except Exception as exc_retry:
                    logger.error(
                        "QQ 发送通道不可用 (%s): %.120s | 去 NapCat WebUI 查登录态/连接",
                        _where, str(exc_retry),
                    )
                    return None
            raise



def _patch_aiocqhttp_wsr() -> None:
    try:
        from aiocqhttp import CQHttp as _CQHttp

        # ── Patch 1a: resilient _add_wsr_api_client (handle missing X-Self-ID) ──
        _orig_add_wsr_api = _CQHttp._add_wsr_api_client

        def _resilient_add_wsr_api(self) -> None:
            from quart import websocket
            try:
                self_id = websocket.headers["X-Self-ID"]
            except KeyError:
                # NapCat may not send X-Self-ID → infer from event client
                self.logger.warning(
                    "X-Self-ID header missing, using wildcard self_id='*'"
                )
                self_id = "*"
            self._wsr_api_clients[self_id] = websocket._get_current_object()

        _CQHttp._add_wsr_api_client = _resilient_add_wsr_api  # type: ignore[method-assign]

        # ── Patch 1b: force universal WS mode ──
        async def _universal_wsr(self) -> None:
            if self._access_token:
                from flask import abort
                from quart import websocket

                auth = websocket.headers.get("Authorization", "")
                m = __import__("re").fullmatch(
                    r"(?:[Tt]oken|[Bb]earer) (?P<token>\S+)", auth
                )
                if not m:
                    self.logger.warning("authorization header is missed")
                    abort(401)
                token_given = m.group("token").strip()
                if token_given != self._access_token:
                    self.logger.warning("authorization header is invalid")
                    abort(403)
            await self._handle_wsr_universal()

        _CQHttp._handle_wsr = _universal_wsr  # type: ignore[method-assign]

        _install_send_healer()

        logger.info("aiocqhttp monkey-patch 已应用 (模块级): resilient API + universal WS + self_id routing")
    except Exception:
        pass  # 非运行时环境 (ruff/pyright) 静默跳过


_patch_aiocqhttp_wsr()


# ── 会话管理 (内存, 用户级) ──────────────────────────

SESSION_TTL = 1800
MAX_HISTORY = 20
_PRIVATE_COMPRESS_THRESHOLD = 15
_PRIVATE_COMPRESS_KEEP_RECENT = 6


@dataclass
class RoleSession:
    user_id: str
    history: list[dict[str, str]] = field(default_factory=list)
    last_active: float = field(default_factory=time.time)
    summary: str = ""
    summary_timestamp: float = 0.0

    def add_user(self, content: str) -> None:
        self.history.append({"role": "user", "content": content})
        self.last_active = time.time()
        self._trim()

    def add_assistant(self, content: str) -> None:
        self.history.append({"role": "assistant", "content": content})
        self.last_active = time.time()
        self._trim()

    def _trim(self) -> None:
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-max(4, MAX_HISTORY):]

    def clear_summary(self) -> None:
        self.summary = ""
        self.summary_timestamp = 0.0

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > SESSION_TTL


# ── 消息解析辅助 ─────────────────────────────────────

def _parse_cq_image_urls(raw: str) -> list[str]:
    """从 CQ 码文本提取图片 URL — 委托到平台适配器。"""
    # ★ 不解析 CQ:mface (QQ 原生表情) — 表情不是图片，不应触发 VLM
    # CQ:image = 用户发的真实图片, CQ:mface = QQ 内置表情/贴纸
    # §十三 坑13 同源模式: 协议标记不能污染触发决策
    try:
        return _get_platform_adapter().parse_image_urls(raw)
    except RuntimeError:
        # 适配器未初始化 → 内联解析 (fallback)
        urls: list[str] = []
        for m in _re.finditer(r"\[CQ:image,[^\]]*url=([^,\]]+)", raw):
            url = m.group(1)
            if url:
                urls.append(url)
        return urls


def _extract_image_urls(event: AstrMessageEvent) -> list[str]:
    """从消息中提取图片 URL (兼容 CQ 码 + AstrBot Image 组件)。"""
    urls: list[str] = []
    raw = getattr(event, "message_str", "") or ""
    urls.extend(_parse_cq_image_urls(raw))
    try:
        components = event.get_messages()
        for comp in components:
            _url = str(getattr(comp, "url", "") or "").strip()
            if _url:
                # 仅 http(s) 视为有效 URL，本地路径不算
                if _url.startswith("http://") or _url.startswith("https://"):
                    urls.append(_url)
                elif _url.startswith("/") or _url.startswith("./") or _url.startswith("file://"):
                    # 本地路径不是 URL，跳过（由 file_id 路径处理）
                    pass
                else:
                    urls.append(_url)
            else:
                _f = str(getattr(comp, "file", "") or "").strip()
                if _f and (_f.startswith("http://") or _f.startswith("https://")):
                    urls.append(_f)
                elif _f and not (_f.startswith("/") or _f.startswith("./") or _f.startswith("file://")):
                    # file 字段可能是 file_id 而非 URL，不加入 urls，留给 file_ids
                    pass
    except Exception:
        pass
    return urls


def _extract_image_file_ids(event: AstrMessageEvent) -> list[str]:
    ids: list[str] = []
    try:
        components = event.get_messages()
        for comp in components:
            # 优先取 file_id (OneBot 原始标识)，file 可能是本地路径需过滤
            fid = getattr(comp, "file_id", None) or getattr(comp, "file", None)
            if fid and str(fid).strip():
                _s = str(fid).strip()
                # 本地路径不是有效 file_id，跳过以免触发 get_image 1200
                if _s.startswith("/") or _s.startswith("./") or _s.startswith("file://"):
                    continue
                ids.append(_s)
    except Exception:
        pass
    # CQ 码兜底：从 message_str 解析 file= 字段 (委托适配器，原则 36)
    if not ids:
        try:
            raw = getattr(event, "message_str", "") or ""
            try:
                ids.extend(_get_platform_adapter().parse_image_file_ids(raw))
            except RuntimeError:
                for m in __import__("re").finditer(r"\[CQ:image,[^\]]*file=([^,\]]+)", raw):
                    _f = m.group(1).strip()
                    if _f and not (_f.startswith("/") or _f.startswith("http")):
                        ids.append(_f)
        except Exception:
            pass
    return ids


def _extract_media_urls(event: AstrMessageEvent, kind: str) -> list[str]:
    """从消息提取视频/语音 URL (kind: video/voice)。

    双路径: message_str CQ 码 (经适配器) + AstrBot 组件 url/file。
    """
    urls: list[str] = []
    raw = getattr(event, "message_str", "") or ""
    try:
        adapter = _get_platform_adapter()
        if kind == "video":
            urls.extend(adapter.parse_video_urls(raw))
        elif kind == "voice":
            urls.extend(adapter.parse_voice_urls(raw))
    except RuntimeError:
        pass
    try:
        components = event.get_messages()
        want = ("video",) if kind == "video" else ("record", "voice")
        for comp in components:
            if _type_str(getattr(comp, "type", "")) not in want:
                continue
            for attr in ("url", "file"):
                val = str(getattr(comp, attr, "") or "").strip()
                if val and (val.startswith("http://") or val.startswith("https://")):
                    if val not in urls:
                        urls.append(val)
    except Exception:
        pass
    return urls


def _type_str(t: object) -> str:
    """组件类型归一化: ComponentType.Reply→reply, 原始小写串原样。

    AstrBot 组件的 type 是枚举 (str() == "ComponentType.Reply")，
    直接 == "reply" 永远 False —— 引用/图片判断曾因此整体失效。
    """
    try:
        s = str(t or "")
        if "." in s:
            s = s.rsplit(".", 1)[-1]
        return s.lower()
    except Exception:
        return ""


def _parse_forward_id(seg: Any) -> str:
    """forward id 提取 — 委托平台适配器 (原则 36/39)。

    未初始化时直连 QQ 实现类 (同进程 tavern 拥有，无反向依赖)。
    """
    try:
        return _get_platform_adapter().parse_forward_id(seg) or ""
    except RuntimeError:
        try:
            return QQPlatformAdapter().parse_forward_id(seg) or ""
        except Exception:
            return ""


def _parse_raw_seg(seg: Any) -> dict[str, str]:
    """OneBot 原始段归一化 — 委托平台适配器 (原则 36/39)。

    返回 {kind/text/url/file_id/sender_id/ref_id}。
    未初始化时直连 QQ 实现类。
    """
    try:
        return _get_platform_adapter().parse_raw_seg(seg)
    except RuntimeError:
        try:
            return QQPlatformAdapter().parse_raw_seg(seg)
        except Exception:
            return {"kind": "", "text": "", "url": "",
                    "file_id": "", "sender_id": "", "ref_id": ""}


async def _fetch_forward_detail(bot: Any, fid: str) -> dict | None:
    """合并转发全文拉取 — 委托平台适配器 (含版本兼容重试)。"""
    try:
        return await _get_platform_adapter().fetch_forward_detail(bot, fid)
    except RuntimeError:
        try:
            return await QQPlatformAdapter().fetch_forward_detail(bot, fid)
        except Exception:
            return None


async def _fetch_replied_msg(bot: Any, message_id: int) -> dict | None:
    """被引用消息全文拉取 — 委托平台适配器。"""
    try:
        return await _get_platform_adapter().fetch_replied_msg(bot, message_id)
    except RuntimeError:
        try:
            return await QQPlatformAdapter().fetch_replied_msg(bot, message_id)
        except Exception:
            return None


def _comp_kind(comp: object) -> str:
    """AstrBot 消息组件的类型归一化 (reply/image/text/...)。"""
    try:
        return _type_str(getattr(comp, "type", ""))
    except Exception:
        return ""


async def _extract_reply_image_urls(event: AstrMessageEvent) -> tuple[list[str], list[str], str, str]:
    """提取引用消息中的图片 URL + 文本 + 发送者 (上溯最多 3 层)。

    Returns:
        (urls, file_ids, reply_text, reply_sender_id)
        reply_sender_id 用于判断引用图片是否由 bot 自己发出——避免 VLM 循环。
    """
    MAX_TRACE_DEPTH = 3
    urls: list[str] = []
    file_ids: list[str] = []
    reply_text_parts: list[str] = []
    reply_sender_id = ""
    try:
        components = event.get_messages()
        try:
            from astrbot.api import logger as _lg
            _lg.debug(
                "引用取图: 组件类型=%s",
                [str(getattr(c, "type", type(c).__name__)) for c in (components or [])],
            )
        except Exception:
            pass
        for comp in components:
            if _comp_kind(comp) == "reply":
                reply_msg_id = getattr(comp, "message_id", None) or getattr(comp, "id", None)
                try:
                    from astrbot.api import logger as _lg2
                    _lg2.debug("引用取图: reply_msg_id=%r", reply_msg_id)
                except Exception:
                    pass
                if not reply_msg_id:
                    # ── 兜底: Reply 组件可能直接嵌套原始消息 (无需 API 调用) ──
                    # 段结构归一化委托适配器 (原则 36)，dict/对象两种形态兼容。
                    _nested_message = getattr(comp, "message", None)
                    if isinstance(_nested_message, (list, tuple)):
                        for _seg in _nested_message:
                            _ps = _parse_raw_seg(_seg)
                            if _ps["kind"] == "image":
                                if _ps["url"]:
                                    urls.append(_ps["url"])
                                if _ps["file_id"]:
                                    file_ids.append(_ps["file_id"])
                            if not reply_sender_id and _ps["sender_id"]:
                                reply_sender_id = _ps["sender_id"]
                        if not reply_text_parts and isinstance(_nested_message, (list, tuple)):
                            _text_parts = []
                            for _s in _nested_message:
                                _pt = _parse_raw_seg(_s)
                                if _pt["kind"] == "text" and _pt["text"]:
                                    _text_parts.append(_pt["text"])
                            if _text_parts:
                                reply_text_parts.append("[引用 Lv.1]\n" + " ".join(_text_parts)[:500])
                    break

                # ── 多层引用上溯: 沿引用链逐层获取 (max 3 层) ──
                current_msg_id = int(reply_msg_id)
                trace_depth = 0
                while current_msg_id and trace_depth < MAX_TRACE_DEPTH:
                    trace_depth += 1
                    _level_label = f"[引用 Lv.{trace_depth}]"
                    try:
                        reply_data = await _fetch_replied_msg(event.bot, current_msg_id)
                    except Exception as _ge:
                        try:
                            from astrbot.api import logger as _lg3
                            _lg3.debug("引用取图: get_msg 失败 id=%s err=%s", current_msg_id, _ge)
                        except Exception:
                            pass
                        break

                    if not reply_data:
                        break

                    # 提取文本 (首层取 raw_message 全文, 后续层级同理)
                    _msg_text = str(reply_data.get("raw_message", ""))[:500]
                    if _msg_text:
                        reply_text_parts.append(f"{_level_label}\n{_msg_text}")

                    # 提取发送者 (仅首层: 用于 VLM 循环判断)
                    if trace_depth == 1:
                        reply_sender = reply_data.get("sender") or {}
                        reply_sender_id = str(reply_sender.get("user_id", ""))

                    # 提取图片
                    try:
                        from astrbot.api import logger as _lg4
                        _lg4.debug(
                            "引用取图: get_msg 成功段类型=%s",
                            [str(s.get("type", "?")) for s in (reply_data.get("message", []) or [])],
                        )
                    except Exception:
                        pass
                    for seg in reply_data.get("message", []):
                        _ps = _parse_raw_seg(seg)
                        if _ps["kind"] == "image":
                            if _ps["url"]:
                                urls.append(_ps["url"])
                            if _ps["file_id"]:
                                file_ids.append(_ps["file_id"])

                    # 检测当前消息是否也引用了其他消息 → 继续上溯
                    next_msg_id = None
                    for seg in reply_data.get("message", []):
                        _ps2 = _parse_raw_seg(seg)
                        if _ps2["kind"] == "reply":
                            _nested_id = _ps2["ref_id"]
                            if _nested_id:
                                try:
                                    next_msg_id = int(_nested_id)
                                except (ValueError, TypeError):
                                    pass
                                break
                    current_msg_id = next_msg_id  # None → 循环终止

                break  # 只处理第一个 reply 组件
    except Exception as _outer:
        try:
            from astrbot.api import logger as _lg5
            _lg5.debug("引用取图: 外层异常 %s", _outer)
        except Exception:
            pass
    reply_text = "\n\n".join(reply_text_parts) if reply_text_parts else ""
    return urls, file_ids, reply_text, reply_sender_id


def _extract_card_info(event: AstrMessageEvent) -> str:
    """从 CQ 分享/JSON/XML 卡片中提取元数据。

    两条提取路径:
      Path 1 — message_str 中的 [CQ:share/json/xml,...] 正则匹配 (文本消息中的卡片)
      Path 2 — message_obj.message 原始 OneBot 段: {"type":"json"|"share","data":{...}}
              (QQ 分享卡作为独立 ComponentType.Json 到达时 message_str 为空，len=0)

    关键设计: URL 提取到最前面 ([分享链接] 前缀)，确保即使上下文截断
    (Gate 150 字符 / prompt_builder 197 字符) URL 也在前 40 字符内可见。
    否则长分享卡 JSON (title + content + image URL) 轻松 300-500 字符，
    URL 埋在 JSON 深处 → 两层截断全裁掉 → LLM 看不到链接。
    """
    parts: list[str] = []

    # ── Path 1: 平台协议层卡片提取 (委托到适配器) ──
    raw = getattr(event, "message_str", "") or ""
    card_hints = _get_platform_adapter().extract_card_hints(raw)
    if card_hints:
        parts.append(card_hints)

    # ── Path 2: AstrBot message components (JSON/share 组件 message_str 为空) ──
    if not parts:
        try:
            components = event.get_messages()
            for comp in components:
                comp_type = str(getattr(comp, "type", ""))
                # 只处理 Share / Json 组件 (Plain/Image/Reply/At 不记录)
                if comp_type == "ComponentType.Share":
                    url = str(getattr(comp, "url", "") or "")
                    if url.startswith("http"):
                        parts.append(f"[分享链接] {url}")
                    parts.append(str({
                        "url": url,
                        "title": getattr(comp, "title", ""),
                        "content": getattr(comp, "content", ""),
                    })[:500])
                    break
                # Json 组件: data 字段是已解析的 dict
                if comp_type == "ComponentType.Json":
                    json_data = getattr(comp, "data", None)
                    if isinstance(json_data, dict):
                        logger.info(
                            "%s [CARD DEBUG] Json data keys=%s full=%s",
                            _bot_tag("?"), list(json_data.keys())[:20],
                            _json.dumps(json_data, ensure_ascii=False)[:2000],
                        )
                        _extract_url_from_json_dict(json_data, parts)
                        if not parts:
                            parts.append(_json.dumps(json_data, ensure_ascii=False)[:500])
                    break
        except Exception as e:
            logger.info("%s [CARD DEBUG] get_messages 异常: %s", _bot_tag("?"), e)
        # ── Path 2b: 兜底 — message_obj.message 原始 OneBot 段 (某些适配器版本) ──
        if not parts:
            msg_obj = getattr(event, "message_obj", None)
            raw_message: Any = getattr(msg_obj, "message", None) if msg_obj is not None else None
            if isinstance(raw_message, list):
                for seg in raw_message:
                    _ps = _parse_raw_seg(seg)
                    if _ps["kind"] == "share":
                        if _ps["url"] and _ps["url"].startswith("http"):
                            parts.append(f"[分享链接] {_ps['url']}")
                        if _ps["text"]:
                            parts.append(_ps["text"])
                    elif _ps["kind"] == "json":
                        if _ps["text"]:
                            _append_card_data(_ps["text"], parts)
                    if parts:
                        break

    return "\n".join(parts)


def _extract_url_from_json_dict(obj: dict, parts: list[str]) -> None:
    """从已解析的 JSON dict 中提取 URL (AstrBot Json 组件 data 字段)。"""
    url = ""
    # 常见 URL 字段路径 (按优先级)
    url = (
        obj.get("url", "")
        or obj.get("jumpUrl", "")
        or obj.get("share_url", "")
        or obj.get("link", "")
    )
    # structmsg: meta.news.jumpUrl / meta.music.jumpUrl
    if not url:
        meta = obj.get("meta", {})
        if isinstance(meta, dict):
            for section in ("news", "music", "detail_1", "detail"):
                section_data = meta.get(section, {})
                if isinstance(section_data, dict):
                    url = section_data.get("jumpUrl", "") or section_data.get("url", "")
                    if url:
                        break
    # QQ 小程序卡片: 递归搜整个 JSON 找第一个 http URL (兜底)
    if not url or not (isinstance(url, str) and url.startswith("http")):
        url = _find_first_url(obj)
    if url and isinstance(url, str) and url.startswith("http"):
        parts.append(f"[分享链接] {url}")


def _find_first_url(obj: Any, _depth: int = 0) -> str:
    """递归搜索 JSON 结构中第一个 http URL。最大深度 6，防栈溢出。"""
    if _depth > 6:
        return ""
    if isinstance(obj, dict):
        # 优先查常见 URL 字段名 (含 QQ 小程序特有 qqdocurl)
        for key in ("url", "jumpUrl", "share_url", "link", "src", "href",
                     "redirect_url", "qqdocurl"):
            val = obj.get(key, "")
            if isinstance(val, str) and val.startswith("http"):
                return val
        # 遍历所有值: 字符串直接匹配 → 容器递归
        for val in obj.values():
            if isinstance(val, str) and val.startswith("http"):
                return val
            if isinstance(val, (dict, list)):
                found = _find_first_url(val, _depth + 1)
                if found:
                    return found
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, str) and item.startswith("http"):
                return item
            if isinstance(item, (dict, list)):
                found = _find_first_url(item, _depth + 1)
                if found:
                    return found
    return ""


def _append_card_data(data_str: str, parts: list[str]) -> None:
    """解析卡片 JSON 数据，提取 URL 到最前面，其余内容追加。

    QQ 分享卡有多种 JSON 结构:
      - share 类型: {"url": "https://...", "title": "...", ...}
      - structmsg (B站/小程序): {"app":"com.tencent.structmsg","meta":{"news":{"jumpUrl":"..."}}}
      - 其他嵌套格式: URL 可能在任意深度

    策略: 先按常见路径提取 → 失败则正则搜任意 http URL。
    """
    url = ""
    try:
        obj = _json.loads(data_str)
        # 常见 URL 字段路径 (按优先级)
        if isinstance(obj, dict):
            url = (
                obj.get("url", "")
                or obj.get("jumpUrl", "")
                or obj.get("share_url", "")
                or obj.get("link", "")
            )
            # structmsg: meta.news.jumpUrl / meta.music.jumpUrl
            if not url:
                meta = obj.get("meta", {})
                if isinstance(meta, dict):
                    for section in ("news", "music", "detail_1", "detail"):
                        section_data = meta.get(section, {})
                        if isinstance(section_data, dict):
                            url = section_data.get("jumpUrl", "") or section_data.get("url", "")
                            if url:
                                break
    except (_json.JSONDecodeError, TypeError):
        pass

    # JSON 解析未找到 → 正则兜底: 从原始字符串中搜 http URL
    if not url or not (isinstance(url, str) and url.startswith("http")):
        m = _re.search(r'https?://[^\s"\'\\,\}\]\)>]+', data_str)
        if m:
            url = m.group(0).rstrip(".")

    if url and isinstance(url, str) and url.startswith("http"):
        parts.append(f"[分享链接] {url}")
    parts.append(data_str[:500])


async def _extract_forward_content(event: AstrMessageEvent) -> str:
    """提取合并转发消息内容。

    支持四种场景：
    1. 直接收到合并转发消息 (Forward 组件在当前消息链中)
    2. 用户引用回复合并转发消息 (Reply 指向含 Forward 的消息)
    3. 从 raw message_obj 检测 forward 段 (AstrBot 组件解析可能遗漏的变体)
    4. 缓存回退: 用户提及"转发"但未找到 → 查历史缓存 (最近 10 分钟)

    AstrBot 框架将 NapCat 的 forward 消息段映射为 Forward 组件。
    使用 isinstance() 判断类型，避免 str(enum) 全限定名陷阱。
    """
    parts: list[str] = []
    sender_id = str(event.get_sender_id())
    _group_id_raw = getattr(event, "group_id", 0)
    cache_key = str(_group_id_raw) if _group_id_raw else f"private:{sender_id}"
    _matched_forward_id = ""  # 用于后续缓存

    async def _expand_forward(fid: str) -> list[str]:
        """调用 get_forward_msg 展开转发内容 (经平台适配器，含版本兼容重试)。"""
        safe_id = str(fid or "").strip()
        if not safe_id:
            return ["[转发消息 ID 为空]"]
        fwd_data = await _fetch_forward_detail(event.bot, safe_id)

        if not fwd_data:
            logger.info("get_forward_msg 未能获取消息内容, id=%s", safe_id)
            return ["[转发消息内容获取失败 — API 可能不支持]"]

        messages = fwd_data.get("messages", [])
        logger.info("合并转发解析成功: id=%s, 消息数=%d", safe_id, len(messages))
        result: list[str] = []
        nested_imgs: list[str] = []
        for msg in messages[:20]:
            sender = msg.get("sender", {})
            name = sender.get("card", sender.get("nickname", "?"))
            text = str(msg.get("raw_message", ""))[:200]
            result.append(f"[转发] {name}: {text}")
            # 嵌套图片: 逐条内段扫描 (上限 3 张，不断链)
            if len(nested_imgs) < 3:
                _segs = msg.get("message", [])
                if isinstance(_segs, list):
                    for _s in _segs:
                        _pi = _parse_raw_seg(_s)
                        if _pi["kind"] == "image" and _pi["url"] and _pi["url"] not in nested_imgs:
                            nested_imgs.append(_pi["url"])
                            result.append(f"[转发内图片] {_pi['url']}")
                            if len(nested_imgs) >= 3:
                                break
        if len(messages) > 20:
            result.append(f"[转发] ... 还有 {len(messages) - 20} 条消息已省略")
        return result

    try:
        components = event.get_messages()
        for comp in components:
            # ── 场景 1: 当前消息链中直接有 Forward 组件 ──
            if isinstance(comp, Forward):
                fid = getattr(comp, "id", None)
                if fid:
                    logger.info("检测到合并转发消息 (直接), id=%s", fid)
                    _matched_forward_id = str(fid)
                    parts = await _expand_forward(fid)
                    break

            # ── 场景 2: 用户引用回复了合并转发消息 ──
            if _comp_kind(comp) == "reply":
                reply_msg_id = (
                    getattr(comp, "message_id", None)
                    or getattr(comp, "id", None)
                )
                if not reply_msg_id:
                    continue
                try:
                    reply_data = await _fetch_replied_msg(event.bot, int(reply_msg_id))
                    if not reply_data:
                        continue
                    # 检查引用消息中是否包含 forward 消息段
                    for seg in reply_data.get("message", []):
                        fid = _parse_forward_id(seg)
                        if fid:
                            logger.info(
                                "检测到合并转发消息 (引用), reply_msg_id=%s forward_id=%s",
                                reply_msg_id, fid,
                            )
                            _matched_forward_id = fid
                            parts = await _expand_forward(fid)
                            break
                except Exception:
                    pass
                break  # 只处理第一个 reply

        # ── 场景 3: raw message_obj 直接检测 forward 段 ──
        # AstrBot 组件解析可能遗漏某些 forward 变体（如 inline payload），
        # 兜底检查 OneBot 原始消息段列表。
        if not parts:
            msg_obj = getattr(event, "message_obj", None)
            raw_message: Any = getattr(msg_obj, "message", None) if msg_obj is not None else None
            if isinstance(raw_message, list):
                for seg in raw_message:
                    fid = _parse_forward_id(seg)
                    if fid:
                        logger.info(
                            "检测到合并转发消息 (message_obj 兜底), id=%s", fid,
                        )
                        _matched_forward_id = fid
                        parts = await _expand_forward(fid)
                        break

        # ── 场景 4: 用户提及"转发"但未在任何消息段中找到 → 查缓存 ──
        # 典型场景: 用户先发了一条合并转发 (无 @/昵称, bot 不回复)，
        # 然后发 "@bot 看看这个转发" → 转发内容在之前的消息中。
        if not parts:
            raw_text = str(getattr(event, "message_str", "") or "")
            if any(kw in raw_text for kw in ("转发", "合并", "聊天记录")):
                cached = get_cached_forward(cache_key, sender_id)
                if cached:
                    logger.info(
                        "合并转发从缓存恢复: key=%s sender=%s len=%d",
                        cache_key, sender_id[:8], len(cached),
                    )
                    parts = [cached]

    except Exception:
        logger.info("合并转发提取失败", exc_info=True)

    # ── 缓存提取成功的转发内容（供场景 4 使用）──
    result = "\n".join(parts)
    if result and result != "[转发消息内容获取失败 — API 可能不支持]":
        cache_forward_content(cache_key, sender_id, _matched_forward_id, result)

    return result


def _is_bot_at_mentioned(event: AstrMessageEvent, self_id: str) -> bool:
    try:
        components = event.get_messages()
        for comp in components:
            if hasattr(comp, "qq") and str(getattr(comp, "qq", "")) == self_id:
                return True
    except Exception:
        pass
    return False


def _is_bot_replied(event: AstrMessageEvent) -> bool:
    try:
        components = event.get_messages()
        for comp in components:
            if _comp_kind(comp) == "reply":
                return True
    except Exception:
        pass
    return False


def _message_targets_bot(content: str, config: Any, nicknames: object = None) -> bool:
    """昵称命中检查 (per-bot 昵称表，2026-10-15 修串味)。

    旧代码只读共享 config 的 bot_nicknames (无此字段时恒回退单一默认表)，
    多 bot 部署时昵称串味：识别不出自己的昵称，还会对别的 bot 昵称误触发。
    调用方传入该 bot 的昵称表 (DB per-bot group_chat_nicknames)；不传时才走全局。
    """
    if not content:
        return False
    if nicknames is None:
        nicknames = getattr(config, "bot_nicknames", None) or getattr(config, "group_chat_nicknames", None)
    if isinstance(nicknames, str):
        nicknames = [p.strip() for p in _re.split(r"[,，、]", nicknames) if p.strip()]
    if not nicknames:
        nicknames = []
    content_lower = content.lower()
    return any(n.lower() in content_lower for n in nicknames if n)


def _detect_redraw_intent(content: str) -> bool:
    if not content:
        return False
    patterns = [
        r"(?:重新?画|重绘|改.*图|修.*图|编辑.*图|换.*图|再.*画|重新.*生成)",
        r"(?:画.*成|改成|换成|变成).*",
    ]
    return any(_re.search(p, content) for p in patterns)


async def _classify_image_intent(content: str, image_count: int, *,
                                  bot_mentioned: bool = False,
                                  has_human_reply_images: bool = False) -> bool:
    """判断是否应触发 VLM 描述图片。

    ⚠️ 加固 (第一批 A2):
      - 移除无条件 VLM 触发: 此前 has_human_reply_images 直接 return True，
        任何人引用含图消息就能烧 VLM。这是 20 额度漏洞的根因之一。
      - 新逻辑: 即使有引用图片，也必须有关键词信号确认用户真想看图。
        @bot + 引用图片 + 关键词 → 三重信号才触发。
        仅引用图片无关键词 → 不触发 (安全默认)。

    设计原则:
      - @bot + 引用人类图片 + 关键词 → 三重确认，触发
      - 仅引用图片无关键词 → 不触发 (可能是讨论其他话题)
      - 引用 bot 自己发的图 → 不自动触发 (避免 VLM 自产自消循环)
      - 其余情况 → 文本关键词检测
    """
    _has_explicit_intent = detect_image_intent(content)

    # @bot + 引用人类图片 + 明确关键词 → 三重确认
    if bot_mentioned and has_human_reply_images and _has_explicit_intent:
        logger.info("VLM 触发: @提及 + 引用人类图片 + 关键词, image_count=%d", image_count)
        return True

    # 仅引用人类图片、无关键词 → 不触发 (⚠️ 此前的无条件放行已移除)
    if has_human_reply_images and not _has_explicit_intent:
        logger.info(
            "VLM 跳过: 引用人类图片但无关键词信号, image_count=%d content=%.60s",
            image_count, content,
        )
        return False

    # 纯关键词触发 (无引用图片，消息中直接含图)
    return _has_explicit_intent


async def _maybe_compress_role_history(
    session: RoleSession, tavern: TavernClient, bot_id: str = "",
) -> None:
    if len(session.history) <= _PRIVATE_COMPRESS_THRESHOLD:
        return
    keep = _PRIVATE_COMPRESS_KEEP_RECENT
    old_messages = session.history[:-keep] if len(session.history) > keep else []
    if not old_messages:
        return
    lines: list[str] = []
    for msg in old_messages[-20:]:
        content = str(msg.get("content", ""))[:150]
        prefix = "AI: " if msg.get("role") == "assistant" else "用户: "
        lines.append(f"{prefix}{content}")
    existing_hint = (
        f"\n之前的摘要: {session.summary}\n请合并更新。"
        if session.summary else ""
    )
    compress_messages = [
        {
            "role": "system",
            "content": (
                "你是对话摘要助手。将角色扮演对话压缩为简洁摘要（150字内），"
                "保留: 角色关系、重要事件、情感变化、关键承诺。用连贯叙述句。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请总结以下角色扮演对话:\n" + "\n".join(lines) + existing_hint
            ),
        },
    ]
    try:
        from .bot_config import get_config_service
        compress_temp = get_config_service().get_temperature("context_compress")
    except Exception:
        compress_temp = 0.3
    try:
        sem = get_llm_semaphore(bot_id) if bot_id else None
        if sem:
            async with sem:
                result = await tavern.chat(
                    compress_messages, temperature=compress_temp, max_tokens=768,
                    slot="context_compress",
                )
        else:
            result = await tavern.chat(
                compress_messages, temperature=compress_temp, max_tokens=768,
                slot="context_compress",
            )
        new_summary = result.strip()
        if new_summary:
            session.summary = new_summary
            session.history = session.history[-keep:]
            logger.info(
                "私聊压缩完成: user=%s len=%d keep=%d",
                session.user_id, len(new_summary), len(session.history),
            )
    except Exception:
        logger.exception("私聊上下文压缩失败: %s", session.user_id)


# ── AstrBot Star 插件 ────────────────────────────────

PLUGIN_NAME = "astrbot_plugin_suli_tavern"


@register(
    PLUGIN_NAME,
    "math89423",
    "暮恩：角色扮演 + 群聊自然对话 + L-Port 生图 + 三层记忆 + 意图门控 + VLM 识图",
    "1.0.0",
)
class LoputTavernPlugin(Star):
    """暮恩 (Moon) — AstrBot 版。开源项目 moon-qqbot。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self._lport_config = Config()

        # ── 酒馆客户端 ──
        self.tavern: Optional[TavernClient] = None
        self._init_error: str = ""
        try:
            self.tavern = TavernClient()
            # 显示活跃 LLM 配置 (B 路线: 直连 API, 不经过酒馆)
            try:
                from .service.bot_config import get_config_service
                active = get_config_service().resolve_active_llm()
                backend_info = f"{active.name} ({active.model_name})" if active else "未配置"
            except Exception:
                backend_info = "未知"
            logger.info("暮恩插件初始化成功, LLM=%s", backend_info)
        except Exception as e:
            self._init_error = f"LLM 客户端初始化失败: {e!s}"
            logger.error(self._init_error)

        # ── 群聊调度器 ──
        self.group_chat_ctl: Optional[GroupChatScheduler] = None
        if self.tavern is not None:
            WHITELIST_FILE = (
                Path(__file__).resolve().parent.parent
                / "AstrBot" / "data" / "shared_db" / "group_chat_whitelist.json"
            )
            try:
                self.group_chat_ctl = GroupChatScheduler(
                    tavern=self.tavern,
                    characters=CHARACTERS,
                    config=self._lport_config,
                    whitelist_path=WHITELIST_FILE,
                )
                logger.info("群聊调度器初始化成功")
            except Exception as e:
                logger.error("群聊调度器初始化失败: %s", e)

        # ── 主动对话调度器 ──
        self.proactive_ctl: Optional[ProactiveChatScheduler] = None
        if self.group_chat_ctl is not None:
            self.proactive_ctl = ProactiveChatScheduler(
                self.group_chat_ctl, self._lport_config,
            )

        # ── 会话管理 ──
        self._sessions: dict[str, RoleSession] = {}

    # ── AstrBot 生命周期 ──────────────────────────

    async def initialize(self) -> None:
        from .bot_config import get_config_service
        from .bot_db import get_bot_db

        # ── 知识库目录注入 (必须在 get_bot_db() 前: 迁移依赖此路径) ──
        from astrbot_plugin_suli_services.knowledge_base import init_knowledge_dir
        init_knowledge_dir(Path(__file__).parent / "knowledge")

        _bot_db = get_bot_db()
        _config_svc = get_config_service()
        logger.info("Bot 本地数据库已初始化: %s", _bot_db._path)

        # ── Bot 身份注册中心 ──
        from .service.bot_identity import get_bot_identity_service
        _identity_svc = get_bot_identity_service()
        _identity_svc.init_db(_bot_db)
        _bot_count = _bot_db.bot_identity_count()
        logger.info("BotIdentity 注册中心已初始化: %d bots", _bot_count)

        # ── 平台适配器初始化 ──
        _init_platform_adapter(QQPlatformAdapter())
        logger.info("PlatformAdapter 已初始化: %s", _get_platform_adapter().platform_name)

        # ── 知识库迁移修复: 之前因路径未注入导致静默失败, 重置 flag 重跑 ──
        try:
            _bot_db.conn.execute(
                "DELETE FROM bot_config WHERE key = '_migrated_knowledge_base'"
            )
            _bot_db.conn.commit()
            _bot_db._migrate_knowledge_base()
        except Exception:
            logger.warning("知识库迁移重跑失败 (非致命)", exc_info=True)

        # ── VLM 配置注入 (多槽位: vlm_primary=识图, vlm_secondary=绘图) ──
        try:
            from astrbot_plugin_suli_services.vision import init_vlm_provider

            def _resolve_vlm_for_any_bot() -> dict | None:
                """尝试解析任一 bot 的 vlm_primary 槽位。

                ⚠️ 单槽全局 provider：多 bot 槽位分化时会取首个有 key 的 bot 的配置
                （今日双 bot 同槽无事）。归属打到日志，串了能看见。
                """
                for bot in _identity_svc.list_bots(active_only=True):
                    bot_id = bot.bot_id
                    cfg = _config_svc.resolve_vlm_slot(bot_id, "vlm_primary")
                    if cfg and cfg.api_key:
                        logger.info(
                            "VLM 槽位归属: 使用 bot=%s 的 vlm_primary (model=%s)",
                            bot_id, cfg.model_name,
                        )
                        return {
                            "api_base": cfg.normalized_base_url,
                            "api_key": cfg.api_key,
                            "model_name": cfg.model_name,
                            "provider": cfg.provider,
                        }
                # fallback: 旧全局配置
                cfg = _config_svc.resolve_active_vlm()
                if cfg and cfg.api_key:
                    return {
                        "api_base": cfg.normalized_base_url,
                        "api_key": cfg.api_key,
                        "model_name": cfg.model_name,
                        "provider": cfg.provider,
                    }
                return None

            init_vlm_provider(_resolve_vlm_for_any_bot)
            logger.info("VLM 配置提供器已注入 (多槽位)")
        except Exception:
            logger.warning("VLM 配置注入失败 (非致命)", exc_info=True)

        # ── 迁移: 修复面板创建的 config 默认 is_active=0 的历史遗留 ──
        # 任何已分配到槽位的 config 都应标记为 active
        try:
            _fixed = 0
            _all_slots = list(_config_svc.LLM_SLOTS) + list(_config_svc.VLM_SLOTS)
            for _bot in _identity_svc.list_bots(active_only=False):
                _bot_id = _bot.bot_id
                for _slot in _all_slots:
                    _cid = _bot_db.get_config(f"bot:{_bot_id}:{_slot}", "")
                    if _cid:
                        _row = _bot_db.conn.execute(
                            "SELECT is_active FROM llm_config WHERE id=?", (int(_cid),)
                        ).fetchone()
                        if _row and not _row[0]:
                            _bot_db.conn.execute(
                                "UPDATE llm_config SET is_active=1 WHERE id=?",
                                (int(_cid),),
                            )
                            _fixed += 1
                            logger.info(
                                "迁移: config id=%s (bot=%s slot=%s) is_active=0→1",
                                _cid, _bot_id, _slot,
                            )
            if _fixed:
                _bot_db.conn.commit()
                logger.info("is_active 迁移完成: %d 个 config 已修复", _fixed)
        except Exception:
            logger.warning("is_active 迁移失败 (非致命)", exc_info=True)

        # ── 启动时模型配置审计: 打印所有 bot 的全部槽位 + 绘图配置 ──
        self._audit_model_configs(_config_svc, _bot_db)

        if self.proactive_ctl is not None and self._lport_config.proactive_enabled:
            await self.proactive_ctl.start()
            logger.info("主动对话调度器已启动")

        # ── 向主动行为插件注入依赖 ──
        await self._inject_proactive_deps()

        # ── 绘图分配检查 (仅配置层, 不发网络): 按 Bot 分配 NovelAI / PixAI。
        # 启动即告警, 免得用户触发了才发现 Key 没配。
        async def _check_draw_assignment():
            await asyncio.sleep(5)  # 等其他初始化完成
            try:
                from .service.bot_config import get_config_service as _get_cfg_svc
                try:
                    from .service.draw_access import METERED_PROVIDERS as _MP
                except Exception:
                    _MP = frozenset()
                try:
                    _bots = list(_identity_svc.list_bots(active_only=True))
                except Exception:
                    _bots = []
                _svc = _get_cfg_svc()
                for _bot in _bots or []:
                    _owner = str(getattr(_bot, "bot_id", "") or "")
                    try:
                        _provider = _svc.get_draw_provider(_owner)
                        _ncfg = _svc.get_draw_novelai()
                        _pcfg = _svc.get_draw_pixai()
                    except Exception:
                        _provider, _ncfg, _pcfg = "none", {}, {}
                    if _provider == "novelai" and (_ncfg.get("token") or "").strip():
                        logger.info(
                            "绘图分配: bot=%s provider=novelai model(主人)=%s "
                            "model(普通)=%s 计费后端=%s(仅主人/主人NSFW通路)",
                            _owner, _ncfg.get("model"), _ncfg.get("model_public"),
                            ",".join(sorted(_MP)) or "-",
                        )
                    elif _provider == "pixai" and (_pcfg.get("key") or "").strip():
                        logger.info(
                            "绘图分配: bot=%s provider=pixai model=%s "
                            "(计费后端: 主人直用; 非主人自动回退 novelai — 降级只进日志)",
                            _owner, _pcfg.get("model"),
                        )
                    else:
                        logger.warning(
                            "绘图分配: bot=%s provider=%s 但 Key 未配置 "
                            "→ generate_image 必回“未配置”。请去管理面板 :6190 绘图页补 Key。",
                            _owner, _provider,
                        )
            except Exception:
                logger.debug("绘图分配检查异常", exc_info=True)
        asyncio.create_task(_check_draw_assignment())

    async def terminate(self) -> None:
        """插件卸载时清理资源。"""
        if self.proactive_ctl:
            await self.proactive_ctl.stop()
        logger.info("暮恩插件已卸载")

    # ── 依赖注入 ──────────────────────────────────

    @staticmethod
    def _audit_model_configs(config_svc, bot_db) -> None:
        """启动时打印所有 bot 的全部模型槽位 + 绘图配置。"""
        try:
            from .service.bot_identity import get_bot_identity_service
            _svc = get_bot_identity_service()
            _bots = _svc.list_bots(active_only=True)
        except Exception:
            _bots = []
        VLM_SLOTS = ("vlm_primary", "vlm_secondary")
        # 已退役的可选槽位: 绘图已改走面板分配 (NovelAI/PixAI)，
        # vlm_secondary 无消费方，未配置属正常，不打 WARN。
        OPTIONAL_VLM_SLOTS = frozenset({"vlm_secondary"})
        # 槽位用途标签
        SLOT_LABELS = {
            "llm_lite": "闲聊", "llm_pro": "推理",
            "llm_gate": "意图闸", "llm_judge": "仲裁",
            "shadow_agent": "影子Agent",
            "vlm_primary": "识图", "vlm_secondary": "绘图VLM",
        }

        def _mask(s: str) -> str:
            return (s[:8] + "..." + s[-4:]) if len(s) > 16 else ("***" if s else "(空)")

        for bot in _bots:
            bot_id = bot.bot_id
            label = bot.name
            # per-bot LLM 槽位 (不同 bot 有不同槽位: peer bot 无仲裁)
            llm_slots = config_svc.get_display_llm_slots(bot_id)
            logger.info(
                "═══ %s (%s) — %d LLM + %d VLM ═══",
                label, bot_id, len(llm_slots), len(VLM_SLOTS),
            )

            # LLM 槽位 (含 Gate)
            for slot in llm_slots:
                cfg = config_svc.resolve_llm_slot(bot_id, slot)
                purpose = SLOT_LABELS.get(slot, slot)
                if cfg and cfg.api_key:
                    logger.info(
                        "  LLM %-12s [%-4s] → model=%-24s url=%s key=%s",
                        slot, purpose, cfg.model_name or "?",
                        cfg.normalized_base_url or "?",
                        _mask(cfg.api_key),
                    )
                else:
                    logger.warning(
                        "  LLM %-12s [%-4s] → ⚠️ 未配置", slot, purpose,
                    )

            # VLM 槽位
            for slot in VLM_SLOTS:
                cfg = config_svc.resolve_vlm_slot(bot_id, slot)
                purpose = SLOT_LABELS.get(slot, slot)
                if cfg and cfg.api_key:
                    logger.info(
                        "  VLM %-12s [%-4s] → model=%-24s url=%s key=%s",
                        slot, purpose, cfg.model_name or "?",
                        cfg.normalized_base_url or "?",
                        _mask(cfg.api_key),
                    )
                else:
                    if slot in OPTIONAL_VLM_SLOTS:
                        logger.info(
                            "  VLM %-12s [%-4s] → (空, 已退役：绘图走面板 NovelAI/PixAI 分配)",
                            slot, purpose,
                        )
                    else:
                        logger.warning(
                            "  VLM %-12s [%-4s] → ⚠️ 未配置", slot, purpose,
                        )

            # ★ 影子 Agent (2026-07-02) — 已作为正式 LLM 槽位，会在上方的循环中打印

    async def _inject_proactive_deps(self) -> None:
        """向主动行为插件注入 GroupChatScheduler + TavernClient。

        通过共享 plugin_registry 获取 proactive 插件实例,
        注入群聊调度器和 LLM 客户端。
        导入失败时静默降级 (proactive 插件可能未加载)。
        """
        try:
            from .plugin_registry import get_proactive_plugin
        except ImportError:
            logger.debug("plugin_registry 不可用, 跳过 proactive 依赖注入")
            return

        plugin = get_proactive_plugin()
        if plugin is None:
            logger.debug("主动插件未注册, 跳过依赖注入")
            return

        try:
            if self.group_chat_ctl is not None:
                plugin.set_group_scheduler(self.group_chat_ctl)
            plugin.set_tavern(self.tavern)
            logger.info("已向主动插件注入 GroupChatScheduler + TavernClient")
        except Exception:
            logger.exception("主动插件依赖注入失败")

    # ── 会话管理 ──────────────────────────────────

    def _get_session(self, user_id: str) -> RoleSession:
        expired = [uid for uid, s in self._sessions.items() if s.is_expired]
        for uid in expired:
            del self._sessions[uid]
        if expired:
            logger.info("清理了 %d 个过期角色会话", len(expired))
        if user_id not in self._sessions:
            self._sessions[user_id] = RoleSession(user_id)
        return self._sessions[user_id]

    # ── 事件数据提取 ──────────────────────────────

    @staticmethod
    def _extract_group_id(event: AstrMessageEvent) -> str:
        raw = getattr(event, "message_obj", None)
        for key in ("group_id", "group", "group_no", "group_uin"):
            value = getattr(raw, key, None) if raw is not None else None
            if value and str(value).strip().isdigit():
                return str(value).strip()
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        match = _re.search(r":GroupMessage:(\d+)", umo)
        if match:
            return match.group(1)
        session_id = str(getattr(event, "session_id", "") or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if session_id.isdigit() and session_id != sender_id:
            return session_id
        return ""

    @staticmethod
    def _sender_role(event: AstrMessageEvent) -> str:
        """QQ 群头衔：owner=群主 admin=管理员 member=群员 (OneBot sender.role)。"""
        try:
            message_obj = getattr(event, "message_obj", None)
            sender = getattr(message_obj, "sender", None) if message_obj is not None else None
            role = str(getattr(sender, "role", "") or "").strip().lower()
            if role in ("owner", "admin", "member"):
                return role
        except Exception:
            pass
        return "member"

    @staticmethod
    def _sender_name(event: AstrMessageEvent, user_id: str = "") -> str:
        for name in ("get_sender_name", "get_sender_nickname"):
            func = getattr(event, name, None)
            if callable(func):
                try:
                    value = str(func() or "").strip()
                    if value:
                        return value
                except Exception:
                    pass
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None) if message_obj is not None else None
        for attr in ("card", "nickname", "name"):
            value = getattr(sender, attr, None) if sender is not None else None
            if value and str(value).strip():
                return str(value).strip()
        return user_id or "群友"

    @staticmethod
    def _self_id(event: AstrMessageEvent) -> str:
        for name in ("get_self_id", "get_bot_id"):
            func = getattr(event, name, None)
            if callable(func):
                try:
                    value = str(func() or "").strip()
                    if value:
                        return value
                except Exception:
                    pass
        message_obj = getattr(event, "message_obj", None)
        value = getattr(message_obj, "self_id", None) if message_obj is not None else None
        raw_id = str(value or "").strip()

        return raw_id

    @staticmethod
    def _is_group_chat_enabled(bot_id: str) -> bool:
        """检查指定 bot 的群聊开关是否打开。"""
        try:
            from .service.bot_config import get_config_service
            return get_config_service().is_group_chat_enabled(bot_id)
        except Exception:
            logger.warning("群聊开关检查异常, fail-closed → 禁止 (bot=%s)", bot_id, exc_info=True)
            return False  # C1: 配置服务不可用时默认禁止, fail-closed

    @staticmethod
    def _is_private_chat_enabled(bot_id: str) -> bool:
        """检查指定 bot 的私聊开关是否打开。"""
        try:
            from .service.bot_config import get_config_service
            return get_config_service().is_private_chat_enabled(bot_id)
        except Exception:
            logger.debug("私聊开关检查异常, fail-closed → 禁止 (bot=%s)", bot_id, exc_info=True)
            return False  # C1: 配置服务不可用时默认禁止, fail-closed

    # ── 群聊消息处理 ──────────────────────────────

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听所有群消息，送入调度器做自然对话决策。"""
        if self.group_chat_ctl is None:
            self_id = self._self_id(event)
            logger.warning("%s group_chat_ctl 未初始化，跳过消息处理", _bot_tag(self_id))
            event.stop_event()
            return

        group_id = self._extract_group_id(event)
        user_id = str(event.get_sender_id())
        self_id = self._self_id(event)

        # ── Self-ID 身份门控: 仅处理双 bot 的事件 (fail-closed) ──
        #   ADR-001 双实例下每个 AstrBot 只连自己的 NapCat,
        #   此处为 defense-in-depth —— 异常事件直接拒绝。
        if not self_id or self_id not in get_known_bot_ids():
            return

        if not group_id or not self.group_chat_ctl.is_group_enabled(int(group_id)):
            _txt_len = len(str(getattr(event, "message_str", "") or ""))
            logger.info("%s 白名单拦截: group=%s sender=%s msg_len=%d", _bot_tag(self_id), group_id, user_id[:8], _txt_len)
            event.stop_event()
            return

        content = str(getattr(event, "message_str", "") or "").strip()
        # ── 群聊开关门控: 该 bot 的群聊功能是否已开启 ──
        if not self._is_group_chat_enabled(self_id):
            logger.info(
                "%s 群聊开关门控拦截: bot=%s 群聊功能未开启 group=%s", _bot_tag(self_id),
                self_id, group_id,
            )
            event.stop_event()
            return

        # ── 入口日志: 确认消息已进入暮恩管线 ──
        _sender = self._sender_name(event, user_id)
        logger.info(
            "%s 群消息入口: group=%s sender=%s/%s len=%d", _bot_tag(self_id),
            group_id, user_id[:8], _sender, len(content),
        )

        card_info = _extract_card_info(event)
        if card_info:
            _was_empty = not content
            content = content + "\n" + card_info if content else card_info
            if _was_empty:
                logger.info(
                    "%s 分享卡提取 (消息文本为空, 从 OneBot 段恢复): %s",
                    _bot_tag(self_id), card_info[:120],
                )

        forward_info = await _extract_forward_content(event)
        if forward_info:
            content = content + "\n" + forward_info if content else forward_info

        image_urls = _extract_image_urls(event)
        image_file_ids = _extract_image_file_ids(event)
        reply_image_urls, reply_file_ids, reply_text, reply_sender_id = await _extract_reply_image_urls(event)
        if reply_image_urls:
            image_urls = image_urls + reply_image_urls
            image_file_ids = image_file_ids + reply_file_ids
        if reply_text:
            content = content + "\n[引用的消息内容]\n" + reply_text if content else "[引用的消息内容]\n" + reply_text
        # ── 视频/语音: 提取 URL 并打标 (URL 置前防截断，与图片 img_tag 同策略) ──
        # 完整描述走 describe_video / describe_voice 工具 (Gate 授权后按需调用)，
        # 此处只保证 LLM 看得见、不断链。
        video_urls = _extract_media_urls(event, "video")
        voice_urls = _extract_media_urls(event, "voice")
        for _vu in video_urls[:2]:
            _vtag = f"[视频 URL: {_vu}]"
            content = content + "\n" + _vtag if content else _vtag
        for _au in voice_urls[:2]:
            _atag = f"[语音 URL: {_au}]"
            content = content + "\n" + _atag if content else _atag
        if video_urls or voice_urls:
            try:
                event._loput_media = {"videos": list(video_urls), "voices": list(voice_urls)}
            except Exception:
                pass
        # ── 待确认生图单提示 (仅短句：确认回复一般很短，长句不注防污染) ──
        try:
            from .service.draw_confirm import pending_note as _pending_note
            _mt0 = (content or "").strip()
            if _mt0 and len(_mt0) <= 12:
                _pnote = _pending_note(self_id, user_id)
                if _pnote:
                    content = content + "\n" + _pnote if content else _pnote
        except Exception:
            pass
        # 引用图片是否来自人类 (非 bot): 避免 VLM 自产自消循环
        _reply_imgs_from_human = bool(reply_image_urls) and not is_peer_bot(self_id, reply_sender_id)

        if len(content) > 800:
            if _is_bot_at_mentioned(event, self_id) or forward_info:
                # @bot 的超长消息 / 含转发内容的消息 → 截断后交由 LLM 自行回应
                # 转发内容已缓存至 forward_cache，LLM 可通过 parse_forwarded_message 工具读取全文
                _trunc_note = (
                    "\n[消息过长已截断]"
                    if not forward_info
                    else "\n[转发内容已缓存，可通过 parse_forwarded_message 工具读取全文]"
                )
                content = content[:800] + _trunc_note
                logger.info(
                    "群 %s: 超长消息截断 user=%s len=%d forward=%s → 交由 LLM",
                    group_id, user_id, len(content), bool(forward_info),
                )
            else:
                logger.info("群 %s: 拒绝超长消息 user=%s len=%d", group_id, user_id, len(content))
                event.stop_event()
                return

        _vlm_available = has_active_vlm()
        _needs_pre_download = _is_bot_at_mentioned(event, self_id) or _is_bot_replied(event)
        if _needs_pre_download:
            for url, fid in zip(image_urls, image_file_ids):
                try:
                    data = await _download_image(url, file_id=fid)
                    if data:
                        cache_qq_image(url, data)
                except Exception:
                    pass

        _vlm_allowed = False
        if _vlm_available and image_urls:
            _bot_mentioned = _is_bot_at_mentioned(event, self_id)
            _bot_replied = _is_bot_replied(event)
            try:
                from .service.bot_config import get_config_service as _gcs_nn
                _nn = _gcs_nn().get_chat_param(self_id, "group_chat_nicknames")
            except Exception:
                _nn = None
            _has_nickname = _message_targets_bot(content, self._lport_config, _nn)
            _has_image_intent = detect_image_intent(content)

            if _bot_mentioned or _bot_replied:
                _vlm_allowed = await _classify_image_intent(
                    content, len(image_urls),
                    bot_mentioned=_bot_mentioned,
                    has_human_reply_images=_reply_imgs_from_human,
                )
            elif _has_nickname and _has_image_intent:
                _vlm_allowed = await _classify_image_intent(content, len(image_urls))

        _redraw_intent = False
        if image_urls and _detect_redraw_intent(content) and _is_bot_at_mentioned(event, self_id):
            set_force_reply_bypass(self_id)
            _vlm_allowed = False
            _redraw_intent = True

        if _vlm_allowed and self._lport_config.emotion_enabled:
            from .emotion import can_use_vlm, check_daily_vlm_limit, check_tool_cooldown
            if not can_use_vlm(user_id, admin_qq=self._lport_config.super_admin_qq, self_id=self_id):
                _vlm_allowed = False
            elif _vlm_allowed:
                cool_ok, _ = check_tool_cooldown(user_id, admin_qq=self._lport_config.super_admin_qq, self_id=self_id)
                if not cool_ok:
                    _vlm_allowed = False
                    # per-bot 个性化 VLM 冷却消息
                    _vlm_busy_msg = (
                        _identity_svc.get_bot(str(self_id)).get_metadata("vlm_busy_msg")
                        if _identity_svc and _identity_svc.get_bot(str(self_id))
                        else None
                    ) or "✨ 刚刚才看过图呢…先让我歇一会儿嘛，过几分钟再发给我看 (´・ω・`)"
                    await event.send(MessageChain([Plain(_vlm_busy_msg)]))
                else:
                    vlm_ok, _ = check_daily_vlm_limit(
                        user_id, admin_qq=self._lport_config.super_admin_qq, self_id=self_id)
                    if not vlm_ok:
                        _vlm_allowed = False

        _has_image = bool(image_urls or image_file_ids)
        if _has_image and _vlm_allowed:
            if len(image_urls) > MAX_VLM_IMAGES:
                await event.send(MessageChain([Plain(
                    f"✨ 一下子发这么多图{_bot_display_name(self_id)}看不过来啦…最多只能仔细看 {MAX_VLM_IMAGES} 张～"
                )]))
                image_urls = image_urls[:MAX_VLM_IMAGES]

            # ── VLM 延迟到意图门之后 ──
            # 此前 VLM 在这里直接调用 describe_images_from_urls(),
            # 完全绕过意图门授权。现在改为标记延迟 VLM 请求,
            # 由 Actor 链（_handle_actor_batch）在 Gate 通过后执行。
            event._loput_deferred_vlm = {
                "urls": list(image_urls),
                "file_ids": list(image_file_ids) if image_file_ids else [],
                "user_id": user_id,
                "group_id": group_id,
                "redraw_intent": _redraw_intent,
                "reverse_prompt_intent": detect_reverse_prompt_intent(content),
                "user_query": str(content or ""),
            }
            # ★ 保存 URL→file_id 映射供跨消息查找恢复
            # group_chat.py 可能在 basic tier 提前 return 跳过缓存填充,
            # 必须在 main.py 源头写入, 确保后续"看看这张图"能恢复 file_id
            for _j, _u in enumerate(image_urls):
                if _j < len(image_file_ids) and image_file_ids[_j]:
                    _url_to_file_id[_u] = image_file_ids[_j]
                    if len(_url_to_file_id) > 200:
                        _oldest = next(iter(_url_to_file_id))
                        del _url_to_file_id[_oldest]
            # file_id 仅有而 url 为空时，亦存 file_id 占位供后续 get_image
            if not image_urls and image_file_ids:
                for _fid in image_file_ids:
                    _url_to_file_id[_fid] = _fid
            logger.info(
                "群 %s: VLM 延迟 — %d 张图片待 Gate 授权后处理 user=%s",
                group_id, len(image_urls or image_file_ids), user_id,
            )
            # 先用纯文本 URL 标记，Gate 授权后再替换为 VLM 描述
            _url_suffix = f" URL: {image_urls[0]}" if image_urls else (f" file:{image_file_ids[0][:12]}" if image_file_ids else "")
            img_count = len(image_urls or image_file_ids)
            img_tag = f"[图片×{img_count}{_url_suffix}]" if img_count > 1 else f"[图片{_url_suffix}]"
            content = content + " " + img_tag if content else img_tag
            # ────────────────────────────────────────────

        elif _has_image:
            # ── 即使 _vlm_allowed=False (无 @/回复/昵称),
            # 也传递图片数据给 Gate。Gate 端的裸图守卫会正确处理:
            #   裸图 + 弱信号 (batch/debounce/proactive) → 静默等待, 不调 VLM
            #   裸图 + 强信号 (mention/reply/nickname/thread_continuation) → 放行 Gate 评估
            #   有文字指令 → Gate 判断是否授权 describe_image
            # 不设 _loput_deferred_vlm 的后果: 跨消息查找会在 ctx.messages 中
            # 扫到当前消息自己的 [图片 URL: ...], 构造伪 deferred_vlm → 裸图守卫被绕过。
            event._loput_deferred_vlm = {
                "urls": list(image_urls),
                "file_ids": list(image_file_ids) if image_file_ids else [],
                "user_id": user_id,
                "group_id": group_id,
                "redraw_intent": False,
                "reverse_prompt_intent": detect_reverse_prompt_intent(content),
                "user_query": str(content or ""),
            }
            # ★ 保存 URL→file_id 映射 — 必须在此写入 (与授权路径相同原因)
            for _j, _u in enumerate(image_urls):
                if _j < len(image_file_ids) and image_file_ids[_j]:
                    _url_to_file_id[_u] = image_file_ids[_j]
                    if len(_url_to_file_id) > 200:
                        _oldest = next(iter(_url_to_file_id))
                        del _url_to_file_id[_oldest]
            if not image_urls and image_file_ids:
                for _fid in image_file_ids:
                    _url_to_file_id[_fid] = _fid
            logger.info(
                "群 %s: VLM 延迟 (非授权路径) — %d 张图片待 Gate 判定 user=%s",
                group_id, len(image_urls or image_file_ids), user_id,
            )
            _url_suffix = f" URL: {image_urls[0]}" if image_urls else (f" file:{image_file_ids[0][:12]}" if image_file_ids else "")
            img_count = len(image_urls or image_file_ids)
            img_tag = f"[图片×{img_count}{_url_suffix}]" if img_count > 1 else f"[图片{_url_suffix}]"
            content = content + " " + img_tag if content else img_tag

        if not content:
            event.stop_event()
            return
        if user_id == self_id:
            event.stop_event()
            return
        # / 开头消息: 不 stop_event — 放行给框架命令处理器
        if content.startswith("/"):
            return

        user_name = self._sender_name(event, user_id)
        try:
            group_role = self._sender_role(event)
        except Exception:
            group_role = "member"

        try:
            bot = BotAdapter(event, self_id)
            wrapped_event = EventAdapter(event, int(group_id), self_id)
            await self.group_chat_ctl.on_message(bot, wrapped_event, user_id, user_name, content, group_role=group_role)
        except Exception:
            logger.exception("群聊调度器异常: group=%s user=%s", group_id, user_id)
        # ★ plugin-return-vs-stop-event:
        # on_message 内的静默检测 / 各种 early return 只退出了插件函数，
        # 框架不知道事件已被处理，会继续走自己的 LLM 管线生成回复。
        # → 必须在入口 handler 统一 stop_event，无论 on_message 成功/异常。
        event.stop_event()

    # ── 私聊消息处理 ──────────────────────────────

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """监听所有私聊消息，自动走角色扮演。"""
        if self.tavern is None:
            event.stop_event()
            return

        self_id = self._self_id(event)

        # ── Self-ID 身份门控: 仅处理双 bot 的事件 (fail-closed) ──
        if not self_id or self_id not in get_known_bot_ids():
            return
        # ── 私聊开关门控: 该 bot 的私聊功能是否已开启 ──
        if self_id and not self._is_private_chat_enabled(self_id):
            event.stop_event()
            return

        user_id = str(event.get_sender_id())
        content = str(getattr(event, "message_str", "") or "").strip()

        card_info = _extract_card_info(event)
        if card_info:
            content = content + "\n" + card_info if content else card_info

        forward_info = await _extract_forward_content(event)
        if forward_info:
            content = content + "\n" + forward_info if content else forward_info

        image_urls = _extract_image_urls(event)
        image_file_ids = _extract_image_file_ids(event)
        reply_image_urls, reply_file_ids, reply_text, _reply_sender = await _extract_reply_image_urls(event)
        if reply_image_urls:
            image_urls = image_urls + reply_image_urls
            image_file_ids = image_file_ids + reply_file_ids
        if reply_text:
            content = content + "\n[引用的消息内容]\n" + reply_text if content else "[引用的消息内容]\n" + reply_text
        # ── 视频/语音: 提取 URL 并打标 (与群聊入口同策略) ──
        video_urls = _extract_media_urls(event, "video")
        voice_urls = _extract_media_urls(event, "voice")
        for _vu in video_urls[:2]:
            _vtag = f"[视频 URL: {_vu}]"
            content = content + "\n" + _vtag if content else _vtag
        for _au in voice_urls[:2]:
            _atag = f"[语音 URL: {_au}]"
            content = content + "\n" + _atag if content else _atag
        if video_urls or voice_urls:
            try:
                event._loput_media = {"videos": list(video_urls), "voices": list(voice_urls)}
            except Exception:
                pass
        # ── 待确认生图单提示 (仅短句，与群聊入口同策略) ──
        try:
            from .service.draw_confirm import pending_note as _pending_note2
            _mt1 = (content or "").strip()
            if _mt1 and len(_mt1) <= 12:
                _pnote1 = _pending_note2(self_id, user_id)
                if _pnote1:
                    content = content + "\n" + _pnote1 if content else _pnote1
        except Exception:
            pass

        if len(content) > 800:
            # 私聊超长消息 → 截断后交由 LLM 自行回应
            content = content[:800] + "\n[消息过长已截断]"
            logger.info("私聊 %s: 超长消息截断 len=%d → 交由 LLM", user_id[:8], len(content))

        for url, fid in zip(image_urls, image_file_ids):
            try:
                data = await _download_image(url, file_id=fid)
                if data:
                    cache_qq_image(url, data)
            except Exception:
                pass

        _vlm_available = has_active_vlm()
        _vlm_ok = _vlm_available and detect_image_intent(content)

        _redraw_intent = False
        if image_urls and _detect_redraw_intent(content):
            set_force_reply_bypass(self_id)
            _vlm_ok = False
            _redraw_intent = True

        if _vlm_ok and self._lport_config.emotion_enabled:
            from .emotion import can_use_vlm, check_daily_vlm_limit, check_tool_cooldown
            if not can_use_vlm(user_id, admin_qq=self._lport_config.super_admin_qq, self_id=self_id):
                _vlm_ok = False
            elif _vlm_ok:
                cool_ok, _ = check_tool_cooldown(user_id, admin_qq=self._lport_config.super_admin_qq, self_id=self_id)
                if not cool_ok:
                    _vlm_ok = False
                    # per-bot 个性化 VLM 冷却消息
                    _vlm_busy_msg = (
                        _identity_svc.get_bot(str(self_id)).get_metadata("vlm_busy_msg")
                        if _identity_svc and _identity_svc.get_bot(str(self_id))
                        else None
                    ) or "✨ 刚刚才看过图呢…先让我歇一会儿嘛，过几分钟再发给我看 (´・ω・`)"
                    await event.send(MessageChain([Plain(_vlm_busy_msg)]))
                else:
                    vlm_ok2, _ = check_daily_vlm_limit(user_id, self_id=self_id)
                    if not vlm_ok2:
                        _vlm_ok = False

        if image_urls and _vlm_ok:
            # ── 私聊 VLM 接预算检查 ──
            _llm_gateway = None
            # 私聊 NSFW 区: VLM 走 NSFW 专属槽链 (未配 = 跟随 SFW, 零行为变化)
            _nsfw_vlm_ctx = False
            try:
                from .service.nsfw_zone import is_nsfw_zone_private as _izp_vlm
                _nsfw_vlm_ctx = bool(_izp_vlm(self_id, user_id))
            except Exception:
                _nsfw_vlm_ctx = False
            _vlm_override = None
            try:
                from .intelligence.llm_gateway import LLMGateway as _LLMGW
                from .service.bot_config import get_config_service as _get_cfg_svc2
                _llm_gateway = _LLMGW
                _vlm_cfg = _get_cfg_svc2().resolve_vlm_slot(self_id, "vlm_primary", nsfw=_nsfw_vlm_ctx)
                _vlm_model = _vlm_cfg.model_name if _vlm_cfg else ""
                _vlm_prov = _vlm_cfg.provider if _vlm_cfg else ""
                _vlm_budget = _LLMGW.pre_check(
                    self_id, purpose="auto_vlm_private",
                    model=_vlm_model, provider=_vlm_prov,
                )
                if _vlm_budget == "hard_capped":
                    from .transport.group_chat import _budget_line as _bl_vlm

                    await event.send(MessageChain([Plain(
                        _bl_vlm(_bot_display_name(self_id))
                    )]))
                    event.stop_event()
                    return
            except Exception:
                logger.debug("私聊 VLM 预算检查失败, fallthrough", exc_info=True)
            # ────────────────────────────────────────────
            if len(image_urls) > MAX_VLM_IMAGES:
                await event.send(MessageChain([Plain(
                    f"✨ 一下子发这么多图{_bot_display_name(self_id)}看不过来啦…最多只能仔细看 {MAX_VLM_IMAGES} 张～"
                )]))
                image_urls = image_urls[:MAX_VLM_IMAGES]
            _reset_vlm_usage(self._self_id(event))
            # NSFW 专属 VLM 覆盖: 仅当真实命中 NSFW 覆盖时切换来源 (未配 = 全局原路径)
            if _nsfw_vlm_ctx:
                try:
                    from .service.bot_config import get_config_service as _get_cfg_svc3
                    _nv_cfg, _nv_hit = _get_cfg_svc3().resolve_vlm_slot_ex(
                        self_id, "vlm_primary", nsfw=True)
                    if _nv_hit and _nv_cfg is not None and getattr(_nv_cfg, "api_key", ""):
                        _vlm_override = {
                            "api_base": _nv_cfg.normalized_base_url,
                            "api_key": _nv_cfg.api_key,
                            "model_name": _nv_cfg.model_name,
                        }
                except Exception:
                    _vlm_override = None
            descriptions = await describe_images_from_urls(
                image_urls, file_ids=image_file_ids, user_query=str(content or ""),
                vlm_override=_vlm_override,
            )
            # ── VLM 用量写入统一统计 ──
            _vlm_usage = get_last_vlm_usage(self_id)
            if _vlm_usage and (_vlm_usage.get("input_tokens") or _vlm_usage.get("output_tokens")):
                try:
                    if _llm_gateway is not None:
                        _llm_gateway.record(
                        bot_id=self_id,
                        model=_vlm_usage.get("model", "?"),
                        provider=_vlm_usage.get("provider", "?"),
                        input_tokens=_vlm_usage.get("input_tokens", 0),
                        output_tokens=_vlm_usage.get("output_tokens", 0),
                        purpose="auto_vlm_private",
                        user_id=user_id,
                    )
                except Exception:
                    logger.debug("LLMGateway VLM 用量记录失败", exc_info=True)
            # ────────────────────────────────────────────
            if descriptions:
                _url_tag = f" [图片URL: {image_urls[0]}]" if image_urls else ""
                img_text = " [用户发送了图片: " + "；".join(descriptions) + "]" + _url_tag
                if detect_reverse_prompt_intent(content):
                    _vlm_desc_only = descriptions[0]
                    _m = _re.search(r"【描述】\s*\n?(.*?)(?=【备注】|$)", _vlm_desc_only, _re.DOTALL)
                    if _m:
                        _vlm_desc_only = _m.group(1).strip()
                    _bid = self._self_id(event)
                    set_reverse_prompt_cache(f"{_bid}:u{user_id}", _vlm_desc_only)
                    img_text += "\n\n[指令] 基于上述图片描述生成 Danbooru 标签式正/负向提示词"
                content = content + img_text if content else img_text.strip()
                if self._lport_config.emotion_enabled:
                    try:
                        from .emotion import record_tool_use, record_vlm_usage
                        record_tool_use(user_id, self_id=self._self_id(event))
                        record_vlm_usage(
                            user_id, admin_qq=self._lport_config.super_admin_qq, self_id=self._self_id(event))
                    except Exception:
                        pass
        elif image_urls:
            _url_suffix = f" URL: {image_urls[0]}" if image_urls else ""
            img_count = len(image_urls)
            img_tag = f"[图片×{img_count}{_url_suffix}]" if img_count > 1 else f"[图片{_url_suffix}]"
            content = content + " " + img_tag if content else img_tag

        if not content:
            return

        user_name = self._sender_name(event, user_id)
        await self._cmd_roleplay(event, user_id, content, user_name)

    # ── /role 命令 ──────────────────────────────────

    @filter.command("role")
    async def on_role_command(self, event: AstrMessageEvent):
        # ── /role 命令已禁用 ──
        await event.send(MessageChain([Plain(
            f"❌ /role 命令已禁用。请直接发送消息与{_bot_display_name(event.get_self_id())}对话。"
        )]))
        return

    async def _on_role_command_disabled(self, event: AstrMessageEvent):
        # 保留旧入口供参考, 实际不可达
        # ── Self-ID 身份门控: 仅处理双 bot 的事件 (fail-closed) ──
        if not self._self_id(event) or self._self_id(event) not in get_known_bot_ids():
            return
        if self._init_error or self.tavern is None:
            await event.send(MessageChain([Plain(f"❌ {self._init_error}")]))
            return

        user_id = str(event.get_sender_id())
        args = str(getattr(event, "message_str", "") or "").strip()

        if not args:
            await self._send_help(event)
            return

        user_name = self._sender_name(event, user_id)
        parts = args.split(maxsplit=1)
        sub = parts[0].lower()

        if sub in ("reset", "重置", "清除", "clear"):
            self._sessions.pop(user_id, None)
            group_id = self._extract_group_id(event)
            if group_id and self.group_chat_ctl:
                await self.group_chat_ctl.clear_context(int(group_id))
            await event.send(MessageChain([Plain("✅ 角色会话已重置。")]))
        elif sub in ("help", "帮助", "?"):
            await self._send_help(event)
        elif sub == "group":
            sub_args = parts[1] if len(parts) > 1 else ""
            await self._cmd_group(event, user_id, sub_args)
        else:
            await self._cmd_roleplay(event, user_id, args, user_name)

    # ── 命令实现 ───────────────────────────────────

    async def _send_help(self, event: AstrMessageEvent):
        char_name = DEFAULT_CHARACTER["name"]
        help_text = (
            f"✨ {char_name}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"  /role <消息>        与 {char_name} 对话\n"
            "  /role reset         重置会话\n"
            "  /role help          显示帮助\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💬 私聊自动角色扮演: 直接发消息即可\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📢 群聊自然对话 (仅群聊)\n"
            "  /role group status  查看状态\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🔬 支持多轮对话，30分钟无操作自动清除会话\n"
            f"当前角色卡: {char_name}"
        )
        await event.send(MessageChain([Plain(help_text)]))

    async def _cmd_group(self, event: AstrMessageEvent, user_id: str, args: str):
        if self.group_chat_ctl is None:
            await event.send(MessageChain([Plain(f"❌ {self._init_error}")]))
            return

        group_id = self._extract_group_id(event)
        if not group_id:
            await event.send(MessageChain([Plain("⚠️ 该命令只能在群聊中使用")]))
            return

        cfg = self._lport_config
        if cfg.group_chat_admin_only:
            message_obj = getattr(event, "message_obj", None)
            sender = getattr(message_obj, "sender", None) if message_obj is not None else None
            role = getattr(sender, "role", "member") if sender is not None else "member"
            if role not in ("owner", "admin"):
                await event.send(MessageChain([Plain("❌ 只有群主/管理员才能管理群聊自然对话")]))
                return

        sub = args.strip().lower()
        gid = int(group_id)

        if sub in ("on", "开启", "启用", "开", "off", "关闭", "禁用", "关"):
            await event.send(MessageChain([Plain(
                "⚠️ 群聊自然对话的启用/关闭请通过 Web 管理面板操作。"
            )]))
        elif sub in ("status", "状态", "查看"):
            enabled = self.group_chat_ctl.is_group_enabled(gid)
            ctx = self.group_chat_ctl.get_context(gid)
            if ctx:
                msg_count = len(ctx.messages)
                last_active_ago = int(time.time() - ctx.last_active)
                cooldown_remain = max(
                    0,
                    cfg.group_chat_cooldown_seconds
                    - int(time.time() - ctx.last_reply_time),
                )
            else:
                msg_count = 0
                last_active_ago = 0
                cooldown_remain = 0
            await event.send(MessageChain([Plain(
                f"📊 群 {group_id} 自然对话状态\n"
                f"状态: {'✅ 已启用' if enabled else '❌ 未启用'}\n"
                f"已缓存消息: {msg_count} 条\n"
                f"距上次活跃: {last_active_ago} 秒\n"
                f"冷却剩余: {cooldown_remain} 秒\n"
                f"冷却间隔: {cfg.group_chat_cooldown_seconds} 秒"
            )]))
        else:
            await event.send(MessageChain([Plain("未知子命令。可用: status")]))

    async def _cmd_roleplay(
        self,
        event: AstrMessageEvent,
        user_id: str,
        content: str,
        user_name: str,
    ):
        session = self._get_session(user_id)
        if not content:
            return

        # 根据 self_id 解析角色卡 (双 Bot: 主 bot vs 对照 bot)
        # ★ 必须先于 _maybe_compress_role_history —— 后者要 self_id。
        #   原顺序是「1901 读 / 1904 写」→ UnboundLocalError,
        #   私聊从 520e53a 起一直崩 (2026-10-10 修)。
        _bot_self_id = self._self_id(event)

        await _maybe_compress_role_history(session, self.tavern, _bot_self_id)

        _char = get_character_for_self_id(_bot_self_id)

        # ── NSFW Zone (私聊): 命中则切换成人向卡 ──
        # 与群聊同源: 文件名为 {角色卡名}_nsfw.json, 门控 fail-closed。
        # 私聊没有群, 走 is_nsfw_zone_private (独立开关 nsfw_private, 不影响群内 NSFW)。
        _nsfw_active = False
        try:
            from .service.nsfw_zone import is_nsfw_zone_private

            if is_nsfw_zone_private(_bot_self_id, user_id):
                from .service.tavern_client import (
                    _card_name_for_qq,
                    load_nsfw_character_card,
                )

                _card_name = _card_name_for_qq(_bot_self_id)
                if _card_name:
                    # 文件读取走线程池 — async 路径禁同步 I/O (铁律 3)
                    _nd = await asyncio.to_thread(
                        load_nsfw_character_card, _card_name
                    )
                    if _nd:
                        _char = _nd
                        _nsfw_active = True
                        logger.info("私聊 NSFW: 角色卡已切换 → %s_nsfw", _card_name)
        except Exception:
            logger.debug("私聊 NSFW 判定异常, fail-closed 走常规卡", exc_info=True)
            _nsfw_active = False

        # ── 静态段组装: system_prompt + few-shot + 输出自检 ──
        # 私聊原先只取 system_prompt —— mes_example / post_history_instructions
        # 两个字段从来不进 prompt (TavernClient.build_messages 零调用者, 是死代码)。
        # 后果: 私聊没有任何 few-shot, 文风完全靠 system_prompt 里的写法条裸奔;
        # 而群聊走 _build_static_system, 是 persona + few-shot + 自检三件套。
        # 两条路径文风必然分裂 —— 2026-10-10 补齐, 格式对齐群聊, 只换抬头措辞。
        # 占位符换成定值 (「你」), 保证 message[0] 跨调用字节级稳定 (原则 29/30)。
        _char_name_p = _char.get("name", "")
        # ★ 占位符替换也要作用在 system_prompt 上:
        #   基础卡 system_prompt 里有 5 处 {{user}} 从未被替换 ——
        #   模型一直收到字面量占位符原文。(2026-10-10 修)
        _sys_text = (
            (_char.get("system_prompt", "") or "")
            .replace("{{char}}", _char_name_p)
            .replace("{{user}}", "对方")
        )
        _mes_p = (_char.get("mes_example", "") or "").strip()
        if _mes_p:
            _mes_p = _mes_p.replace("{{char}}", _char_name_p).replace("{{user}}", "你")
            _sys_text += (
                f"\n[对话示例 — 以下是你和对方之间真实发生过的对话]\n{_mes_p}\n"
            )
        _post_p = (_char.get("post_history_instructions", "") or "").strip()
        if _post_p:
            _post_p = _post_p.replace("{{user}}", "你").replace("{{char}}", _char_name_p)
            _sys_text += f"\n[回复前自检 — 逐条确认你没有犯以下错误]\n{_post_p}\n"

        tavern_messages: list[dict[str, str]] = [
            {"role": "system", "content": _sys_text},
        ]

        # ── 世界书注入 (动态段 — message[0] 保持字节级稳定, 前缀缓存不受影响) ──
        # 扫描范围 = 会话历史 + 本条消息 (世界书自己的 scan_depth 会截最近 N 条)
        try:
            from .service.tavern_client import _scan_world_book

            _wb_entries = _scan_world_book(
                [*session.history, {"role": "user", "content": content}],
                bot_id=_bot_self_id,
                nsfw=_nsfw_active,
            )
            if _wb_entries:
                tavern_messages.append({
                    "role": "system",
                    "content": (
                        "[附加背景 — 这些信息在刚才的对话中被触发，"
                        "你应该自然地融入回复中]\n\n" + "\n\n".join(_wb_entries)
                    ),
                })
        except Exception:
            logger.debug("私聊世界书注入异常 (跳过)", exc_info=True)

        # ── 荷尔蒙声线注入 (动态段) ──
        # 此前私聊只用了 knobs()/observe_*，从未调 hint() —— 六分区语气
        # (起意/撩拨/升温/临界/决堤/余韵)、taboo、solicit、拟声规则在私聊
        # 全部空转, 而私聊正是 1v1 成人向主战场 (2026-10-12 通路审计 P0-5)。
        # 必须是 append (message[1+]) —— hint 每轮都变, 进 message[0] 会废掉前缀缓存。
        try:
            from .service import hormone_bridge as _hb_hint_mod

            _priv_hint = _hb_hint_mod.hint(_bot_self_id, user_id, private=True)
            if _priv_hint:
                tavern_messages.append({"role": "system", "content": _priv_hint})
        except Exception:
            logger.debug("私聊荷尔蒙 hint 注入异常 (跳过)", exc_info=True)

        if session.summary:
            tavern_messages.append({
                "role": "system",
                "content": f"[对话历史摘要] {session.summary}",
            })
        tavern_messages.extend(session.history)
        tavern_messages.append({"role": "user", "content": content})
        session.add_user(content)

        try:
            # ── 私聊角色扮演接入统一成本关口 ──
            from .intelligence.llm_gateway import LLMGateway
            from .service.bot_config import get_config_service as _get_cfg_svc

            _char_cfg = _get_cfg_svc().resolve_llm_slot(_bot_self_id, "llm_primary")
            _priv_model = _char_cfg.model_name if _char_cfg else ""
            _priv_provider = _char_cfg.provider if _char_cfg else ""
            _budget_ok = LLMGateway.pre_check(
                _bot_self_id,
                purpose="private_roleplay",
                model=_priv_model,
                provider=_priv_provider,
            )
            if _budget_ok == "hard_capped":
                from .transport.group_chat import _budget_line as _bl_priv

                await event.send(MessageChain([Plain(
                    _bl_priv(_bot_display_name(_bot_self_id))
                )]))
                session.history.pop()
                return

            # per-user 每日私聊配额 (防止单人无限刷)
            _today = time.strftime("%Y-%m-%d")
            _priv_quota = getattr(self, "_private_daily_quota", None)
            if _priv_quota is None:
                self._private_daily_quota: dict[str, dict[str, int]] = {}
                _priv_quota = self._private_daily_quota
            _priv_day = _priv_quota.get(_today, {})
            _priv_count = _priv_day.get(user_id, 0)
            _priv_max = 50  # 每人每天最多 50 轮私聊对话
            if _priv_count >= _priv_max and user_id != str(self._lport_config.super_admin_qq):
                await event.send(MessageChain([Plain(
                    f"✨ 今天已经聊了很多了呢…明天再来找{_bot_display_name(_bot_self_id)}玩吧 (´・ω・`)"
                )]))
                session.history.pop()
                return
            # ────────────────────────────────────────────

            # ── 安全硬线: 正则预筛选 (零 LLM 成本) ──
            # 私聊绕过意图闸, 用正则兜底拦截明显的性化/敌意内容。
            # NSFW Zone 内传入 nsfw_zone=True → 成人合意类降级交 LLM,
            # 「解除伦理约束 / 未成年相关」仍硬拦 (见 InputClassifier)。
            try:
                from astrbot_plugin_suli_social.input_classifier import InputClassifier
                _cls = InputClassifier()
                _cls_result = _cls.prescreen(
                    content, is_addressed_to_me=True, nsfw_zone=_nsfw_active
                )
                if (
                    not _cls_result.needs_llm
                    and _cls_result.nature.value in ("sexualized", "hostile")
                ):
                    logger.warning(
                        "私聊 %s: 安全硬线拦截 nature=%s conf=%.2f",
                        user_id[:8], _cls_result.nature.value, _cls_result.confidence,
                    )
                    # 角色化硬线话术: 从角色卡 safety_replies 池抽 (2026-10-12)。
                    # 此前是客服腔「❌ 消息包含不当内容，无法回复。」—— 而硬线
                    # 拦截恰恰发生在沉浸场景进行中, 一句客服腔直接击穿角色。
                    _hard_reply = ""
                    try:
                        from astrbot_plugin_suli_guards.injection_guard import (
                            InjectionGuard as _IG_hl,
                        )

                        _hard_reply = _IG_hl._pick_safety_reply(
                            bot_id=str(_bot_self_id),
                        )
                    except Exception:
                        _hard_reply = "这个不行。"
                    await event.send(MessageChain([Plain(_hard_reply)]))
                    session.history.pop()
                    return
            except ImportError:
                pass
            except Exception:
                logger.debug("私聊安全硬线异常, fail-open", exc_info=True)
            # ────────────────────────────────────────────

            sem = get_llm_semaphore(_bot_self_id)
            # ── 荷尔蒙喂入 (私聊): 用户文本 = 刺激源 (门控不过则内部直接返回) ──
            try:
                from .service import hormone_bridge as _hb

                _hb.observe_user(_bot_self_id, user_id, str(content or ""), private=True)
            except Exception:
                logger.debug("荷尔蒙喂入失败 (私聊, 不影响回复)", exc_info=True)
            # ── 2026-08-17 方案 A: 私聊角色扮演回归 tavern.chat 直连。
            #    早期经外置 agent 中间层转发会把 persona 注入稀释, 角色扮演失真
            #    — 与群聊同源修复。私聊是纯 1v1 角色扮演 (无工具需求),
            #    直接走 tavern_client (完整 system_prompt + 会话历史)。
            # ── NSFW 专属槽位 (2026-10-18): 命中专属覆盖 → 本次显式用其模型/凭证;
            #    未配 = 空 kwargs 走原回退链, SFW/未配置行为零变化。
            _priv_chat_kwargs: dict = {}
            if _nsfw_active:
                try:
                    _nsfw_cfg, _nsfw_hit = _get_cfg_svc().resolve_llm_slot_ex(
                        _bot_self_id, "llm_primary", nsfw=True,
                    )
                    if _nsfw_hit and _nsfw_cfg:
                        _priv_chat_kwargs = {
                            "model": _nsfw_cfg.model_name,
                            "api_base": _nsfw_cfg.normalized_base_url,
                            "api_key": _nsfw_cfg.api_key,
                            "bot_id": _bot_self_id,
                        }
                        _priv_model = _nsfw_cfg.model_name
                        _priv_provider = _nsfw_cfg.provider
                        logger.info("私聊 NSFW: 用专属槽位模型 %s (bot=%s)", _priv_model, _bot_self_id)
                except Exception:
                    logger.debug("私聊 NSFW 槽位解析异常, 走原回退链", exc_info=True)
            if sem:
                async with sem:
                    reply = await self.tavern.chat(tavern_messages, slot="private_roleplay", **_priv_chat_kwargs)
            else:
                reply = await self.tavern.chat(tavern_messages, slot="private_roleplay", **_priv_chat_kwargs)

            # ── 记录用量 + 更新配额 ──
            LLMGateway.record_from_tavern(
                self.tavern,
                bot_id=_bot_self_id,
                model=_priv_model,
                provider=_priv_provider,
                purpose="private_roleplay",
                user_id=user_id,
            )
            _priv_day[user_id] = _priv_count + 1
            _priv_quota[_today] = _priv_day
            # ────────────────────────────

            reply = sanitize_qq_reply(reply)
            # ── 旁白/自称过滤: 与群聊同源, per-bot 传角色名 ──
            #    此前私聊只做 markdown 清理 —— 泄露守卫 / 旁白过滤 / 长度控制全缺,
            #    而私聊正是 NSFW 主场。这里补齐兜底。
            try:
                from .transport.reply_postprocessor import (
                    filter_narration as _filter_nar,
                )

                if isinstance(reply, str) and reply:
                    reply, _nar_n = _filter_nar(reply, char_name=_char_name_p)
                    if _nar_n:
                        logger.info(
                            "私聊 %s: 反臃肿过滤 %d 处修改",
                            user_id[:8], _nar_n,
                        )
            except Exception:
                logger.debug("私聊反臃肿过滤失败", exc_info=True)
            session.add_assistant(reply)
            # ── 荷尔蒙驱动的分段发送 (私聊也有"打字打不完"的节奏) ──
            #    门控不过 / shadow / 不在 NSFW Zone → 中性旋钮 = 单条 (旧行为)。
            _hb_k = {"frag_max": 1, "gap_s": 1.5, "live": False}
            try:
                from .service import hormone_bridge as _hb2

                _hb_k = _hb2.knobs(_bot_self_id, user_id, private=True)
            except Exception:
                logger.debug("荷尔蒙旋钮获取失败 (私聊), 单条发送", exc_info=True)
            _frags = [reply]
            # ★ 只有 live 才分段: 私聊接线前就是单条, 中性值不得改变它
            _hb_frag = int(_hb_k.get("frag_max", 1) or 1) if _hb_k.get("live") else 1
            if _hb_frag > 1:
                try:
                    from .transport.reply_postprocessor import fragment_reply as _frag_fn

                    _frags = _frag_fn(reply, max_frags=_hb_frag)
                except Exception:
                    logger.debug("私聊分段失败, 退回单条", exc_info=True)
                    _frags = [reply]
            for _fi, _frag in enumerate(_frags):
                if _fi:
                    await asyncio.sleep(float(_hb_k.get("gap_s", 1.5)))
                await event.send(MessageChain([Plain(_frag)]))
            # 自反馈: 自己刚说出去的露骨内容也算蓄积 (有上限)
            try:
                from .service import hormone_bridge as _hb3

                _hb3.observe_self(_bot_self_id, user_id, reply, private=True)
                _hb3.note_burst(_bot_self_id, user_id, max(0, len(_frags) - 1))
            except Exception:
                logger.debug("荷尔蒙自反馈失败 (私聊)", exc_info=True)
        except Exception:
            logger.exception("角色扮演失败: user=%s", user_id)
            session.history.pop()
            # 角色化兜底 (2026-10-12): 此前是「❌ 对话出错了…(´・ω・`)」
            # —— 系统腔 + 与角色卡声线无关的颜文字。
            from .transport.group_chat import _fallback_line as _fl_err

            await event.send(MessageChain([Plain(
                _fl_err(_bot_display_name(_bot_self_id))
            )]))

# ── Bot 身份查询 ── 从 BotIdentityService 动态读取, 不再硬编码 QQ 号。
# 向后兼容: get_known_bot_ids() 从 dual_bot.py (Phase 2) 导入已有懒加载机制。

def _bot_tag(bot_id: str) -> str:
    """返回 bot 日志标签。从 BotIdentityService 查询。"""
    try:
        from .service.bot_identity import get_bot_identity_service
        svc = get_bot_identity_service()
        bot = svc.get_bot(bot_id)
        if bot:
            return f"[{bot.name}]"
    except Exception:
        pass
    return f"[{bot_id[:8]}]"
