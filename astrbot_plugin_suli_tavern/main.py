"""暮恩酒馆桥接 — AstrBot 版。

AstrBot 原生插件 — 保留全部自研基础设施：
  三层记忆 / Intent Gate (含 Grace) / Bot行为检测 / VLM 识图 / 工具调用

角色卡引擎基于 SillyTavern (酒馆)，AstrBot=大脑, ST=渲染器。
"""

from __future__ import annotations

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
from astrbot_plugin_suli_guards.dual_bot import get_bot_qq_set, is_peer_bot

# ── 反推提示词缓存 ────────────────────────────────────
_reverse_prompt_cache: dict[str, str] = {}


def get_reverse_prompt_cache(key: str) -> str:
    return _reverse_prompt_cache.pop(key, "")


def set_reverse_prompt_cache(key: str, desc: str) -> None:
    _reverse_prompt_cache[key] = desc
    logger.info("反推缓存已存储: key=%s, len=%d", key, len(desc))


# ── 合并转发消息缓存 ──────────────────────────────
# 委托给 service/forward_cache.py — 供 parse_forwarded_message 工具读取
from .service.forward_cache import cache_forward_content, get_cached_forward


# ── 模块级 monkey-patch: aiocqhttp reverse WS 强制 universal ──
# NapCat 默认 X-Client-Role: event → AstrBot 收得到发不出 (ApiNotAvailable)
# 尽早打补丁，确保插件 initialize() 之前就已生效
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

        # ── Patch 2: inject self_id into API calls ──
        # WebSocketReverseApi.call_action needs self_id to route to the
        # correct NapCat when there are 2+ clients. The framework's
        # _dispatch_send drops self_id from the event → inject it.
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )
        _orig_dispatch_send = AiocqhttpMessageEvent._dispatch_send

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
                await bot.send_group_msg(
                    group_id=session_id_int, message=messages, self_id=self_id,
                )
            elif not is_group and isinstance(session_id_int, int):
                await bot.send_private_msg(
                    user_id=session_id_int, message=messages, self_id=self_id,
                )
            elif hasattr(event, "__getitem__"):
                await bot.send(event=event, message=messages)
            else:
                raise ValueError(
                    f"无法发送消息：缺少有效的数字 session_id 或 event"
                )

        AiocqhttpMessageEvent._dispatch_send = _patched_dispatch_send  # type: ignore[method-assign]

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
    urls: list[str] = []
    # ★ 不解析 CQ:mface (QQ 原生表情) — 表情不是图片，不应触发 VLM
    # CQ:image = 用户发的真实图片, CQ:mface = QQ 内置表情/贴纸
    # §十三 坑13 同源模式: 协议标记不能污染触发决策
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
            if hasattr(comp, "url") and (comp.url or "").strip():
                urls.append(str(comp.url).strip())
            elif hasattr(comp, "file") and (comp.file or "").strip():
                urls.append(str(comp.file).strip())
    except Exception:
        pass
    return urls


def _extract_image_file_ids(event: AstrMessageEvent) -> list[str]:
    ids: list[str] = []
    try:
        components = event.get_messages()
        for comp in components:
            fid = getattr(comp, "file", None) or getattr(comp, "file_id", None)
            if fid and str(fid).strip():
                ids.append(str(fid).strip())
    except Exception:
        pass
    return ids


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
        for comp in components:
            if hasattr(comp, "type") and str(getattr(comp, "type", "")) == "reply":
                reply_msg_id = getattr(comp, "message_id", None) or getattr(comp, "id", None)
                if not reply_msg_id:
                    # ── 兜底: Reply 组件可能直接嵌套原始消息 (无需 API 调用) ──
                    _nested_message = getattr(comp, "message", None)
                    if isinstance(_nested_message, (list, tuple)):
                        for _seg in _nested_message:
                            if isinstance(_seg, dict) and _seg.get("type") == "image":
                                _seg_url = _seg.get("data", {}).get("url", "")
                                _seg_fid = _seg.get("data", {}).get("file", "")
                                if _seg_url:
                                    urls.append(_seg_url)
                                if _seg_fid:
                                    file_ids.append(_seg_fid)
                            elif hasattr(_seg, "type") and str(getattr(_seg, "type", "")) == "image":
                                _seg_url = getattr(_seg, "url", "") or ""
                                _seg_fid = getattr(_seg, "file", "") or ""
                                if _seg_url:
                                    urls.append(_seg_url)
                                if _seg_fid:
                                    file_ids.append(_seg_fid)
                            if not reply_sender_id:
                                _seg_sender = getattr(_seg, "sender_id", None) or (
                                    _seg.get("sender", {}).get("user_id", "") if isinstance(_seg, dict) else ""
                                )
                                reply_sender_id = str(_seg_sender or "")
                        if not reply_text_parts and isinstance(_nested_message, (list, tuple)):
                            _text_parts = []
                            for _seg in _nested_message:
                                _seg_type = _seg.get("type", "") if isinstance(_seg, dict) else str(getattr(_seg, "type", ""))
                                if _seg_type == "text":
                                    _t = _seg.get("data", {}).get("text", "") if isinstance(_seg, dict) else str(getattr(_seg, "text", ""))
                                    if _t:
                                        _text_parts.append(_t)
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
                        reply_data = await event.bot.api.call_action(
                            "get_msg", message_id=current_msg_id
                        )
                    except Exception:
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
                    for seg in reply_data.get("message", []):
                        if seg.get("type") == "image":
                            seg_url = seg.get("data", {}).get("url", "")
                            seg_fid = seg.get("data", {}).get("file", "")
                            if seg_url:
                                urls.append(seg_url)
                            if seg_fid:
                                file_ids.append(seg_fid)

                    # 检测当前消息是否也引用了其他消息 → 继续上溯
                    next_msg_id = None
                    for seg in reply_data.get("message", []):
                        if seg.get("type") == "reply":
                            _nested_id = seg.get("data", {}).get("id", "")
                            if _nested_id:
                                try:
                                    next_msg_id = int(_nested_id)
                                except (ValueError, TypeError):
                                    pass
                                break
                    current_msg_id = next_msg_id  # None → 循环终止

                break  # 只处理第一个 reply 组件
    except Exception:
        pass
    reply_text = "\n\n".join(reply_text_parts) if reply_text_parts else ""
    return urls, file_ids, reply_text, reply_sender_id


def _extract_card_info(event: AstrMessageEvent) -> str:
    raw = getattr(event, "message_str", "") or ""
    parts: list[str] = []
    for m in _re.finditer(r"\[CQ:(?:json|xml|share),[^\]]*?data=([^\]]+)", raw):
        data = m.group(1)
        if data:
            parts.append(data[:500])
    return "\n".join(parts)


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
        """调用 get_forward_msg 展开转发内容，多参数名重试。"""
        safe_id = str(fid or "").strip()
        if not safe_id:
            return ["[转发消息 ID 为空]"]
        fwd_data = None
        # 尝试多种 NapCat 接受的参数名（不同版本兼容）
        for action, kwargs in [
            ("get_forward_msg", {"message_id": safe_id}),
            ("get_forward_msg", {"id": safe_id}),
            ("get_forward_msg", {"res_id": safe_id}),
            ("get_forward_msg", {"resid": safe_id}),
        ]:
            try:
                fwd_data = await event.bot.api.call_action(action, **kwargs)
                if fwd_data:
                    break
            except Exception:
                continue
        # int 变体 (某些 NapCat 版本只接受 int)
        if not fwd_data and safe_id.isdigit():
            try:
                fwd_data = await event.bot.api.call_action(
                    "get_forward_msg", message_id=int(safe_id),
                )
            except Exception:
                pass

        if not fwd_data:
            logger.info("get_forward_msg 未能获取消息内容, id=%s", safe_id)
            return ["[转发消息内容获取失败 — API 可能不支持]"]

        messages = fwd_data.get("messages", [])
        logger.info("合并转发解析成功: id=%s, 消息数=%d", safe_id, len(messages))
        result: list[str] = []
        for msg in messages[:20]:
            sender = msg.get("sender", {})
            name = sender.get("card", sender.get("nickname", "?"))
            text = str(msg.get("raw_message", ""))[:200]
            result.append(f"[转发] {name}: {text}")
        if len(messages) > 20:
            result.append(f"[转发] ... 还有 {len(messages) - 20} 条消息已省略")
        return result

    def _forward_id_from_raw_seg(seg: Any) -> str:
        """从 raw message_obj 的消息段 dict 中提取 forward id。"""
        if not isinstance(seg, dict):
            return ""
        if seg.get("type") != "forward":
            return ""
        seg_data = seg.get("data", {})
        if isinstance(seg_data, dict):
            for key in ("id", "resid", "forward_id"):
                val = seg_data.get(key)
                if val:
                    return str(val).strip()
        return ""

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
            if hasattr(comp, "type") and str(getattr(comp, "type", "")) == "reply":
                reply_msg_id = (
                    getattr(comp, "message_id", None)
                    or getattr(comp, "id", None)
                )
                if not reply_msg_id:
                    continue
                try:
                    reply_data = await event.bot.api.call_action(
                        "get_msg", message_id=int(reply_msg_id)
                    )
                    if not reply_data:
                        continue
                    # 检查引用消息中是否包含 forward 消息段
                    for seg in reply_data.get("message", []):
                        fid = _forward_id_from_raw_seg(seg)
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
                    fid = _forward_id_from_raw_seg(seg)
                    if fid:
                        logger.info(
                            "检测到合并转发消息 (message_obj 兜底), id=%s", fid,
                        )
                        _matched_forward_id = fid
                        parts = await _expand_forward(fid)
                        break

        # ── 场景 4: 用户提及"转发"但未在任何消息段中找到 → 查缓存 ──
        # 典型场景: 用户先发了一条合并转发 (无 @/昵称, bot 不回复)，
        # 然后发 "@暮恩 看看这个转发" → 转发内容在之前的消息中。
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
            if str(getattr(comp, "type", "") or "") == "reply":
                return True
    except Exception:
        pass
    return False


def _message_targets_bot(content: str, config: Any) -> bool:
    if not content:
        return False
    nicknames = getattr(config, "bot_nicknames", ["暮恩", "小暮", "moon", "绿蛇"])
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
                    compress_messages, temperature=compress_temp, max_tokens=150,
                )
        else:
            result = await tavern.chat(
                compress_messages, temperature=compress_temp, max_tokens=150,
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
class MoonTavernPlugin(Star):
    """暮恩 · 无限之蛇 — AstrBot 版。"""

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
                Path(__file__).resolve().parent.parent.parent.parent
                / "data" / "shared_db" / "group_chat_whitelist.json"
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
                """尝试解析任一 bot 的 vlm_primary 槽位。"""
                for bot in _identity_svc.list_bots(active_only=True):
                    bot_id = bot.bot_id
                    cfg = _config_svc.resolve_vlm_slot(bot_id, "vlm_primary")
                    if cfg and cfg.api_key:
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
        # 槽位用途标签
        SLOT_LABELS = {
            "llm_lite": "闲聊", "llm_pro": "推理",
            "llm_gate": "意图闸", "llm_judge": "仲裁",
            "vlm_primary": "识图", "vlm_secondary": "绘图VLM",
        }

        def _mask(s: str) -> str:
            return (s[:8] + "..." + s[-4:]) if len(s) > 16 else ("***" if s else "(空)")

        for bot in _bots:
            bot_id = bot.bot_id
            label = bot.name
            # per-bot LLM 槽位 (不同 bot 有不同槽位: 无仲裁)
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
                    logger.warning(
                        "  VLM %-12s [%-4s] → ⚠️ 未配置", slot, purpose,
                    )

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
        return str(value or "").strip()

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
        if not self_id or self_id not in get_bot_qq_set():
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
            content = content + "\n" + card_info if content else card_info

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
            _has_nickname = _message_targets_bot(content, self._lport_config)
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
                    vlm_ok, _ = check_daily_vlm_limit(user_id, admin_qq=self._lport_config.super_admin_qq)
                    if not vlm_ok:
                        _vlm_allowed = False

        if image_urls and _vlm_allowed:
            if len(image_urls) > MAX_VLM_IMAGES:
                await event.send(MessageChain([Plain(
                    f"✨ 一下子发这么多图暮恩看不过来啦…最多只能仔细看 {MAX_VLM_IMAGES} 张～"
                )]))
                image_urls = image_urls[:MAX_VLM_IMAGES]

            # ── VLM 延迟到意图门之后 ──
            # 此前 VLM 在这里直接调用 describe_images_from_urls(),
            # 完全绕过意图门授权。现在改为标记延迟 VLM 请求,
            # 由 _evaluate_and_reply 在 Gate 通过后执行。
            event._moon_deferred_vlm = {
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
            logger.info(
                "群 %s: VLM 延迟 — %d 张图片待 Gate 授权后处理 user=%s",
                group_id, len(image_urls), user_id,
            )
            # 先用纯文本 URL 标记，Gate 授权后再替换为 VLM 描述
            _url_suffix = f" URL: {image_urls[0]}" if image_urls else ""
            img_count = len(image_urls)
            img_tag = f"[图片×{img_count}{_url_suffix}]" if img_count > 1 else f"[图片{_url_suffix}]"
            content = content + " " + img_tag if content else img_tag
            # ────────────────────────────────────────────

        elif image_urls:
            # ── 即使 _vlm_allowed=False (无 @/回复/昵称),
            # 也传递图片数据给 Gate。Gate 端的裸图守卫会正确处理:
            #   裸图 + 弱信号 (batch/debounce/proactive) → 静默等待, 不调 VLM
            #   裸图 + 强信号 (mention/reply/nickname/thread_continuation) → 放行 Gate 评估
            #   有文字指令 → Gate 判断是否授权 describe_image
            # 不设 _moon_deferred_vlm 的后果: 跨消息查找会在 ctx.messages 中
            # 扫到当前消息自己的 [图片 URL: ...], 构造伪 deferred_vlm → 裸图守卫被绕过。
            event._moon_deferred_vlm = {
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
            logger.info(
                "群 %s: VLM 延迟 (非授权路径) — %d 张图片待 Gate 判定 user=%s",
                group_id, len(image_urls), user_id,
            )
            _url_suffix = f" URL: {image_urls[0]}" if image_urls else ""
            img_count = len(image_urls)
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
            bot = BotAdapter(event, self_id)
            wrapped_event = EventAdapter(event, int(group_id), self_id)
            await self.group_chat_ctl.on_message(bot, wrapped_event, user_id, user_name, content)
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
        if not self_id or self_id not in get_bot_qq_set():
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
                    vlm_ok2, _ = check_daily_vlm_limit(user_id)
                    if not vlm_ok2:
                        _vlm_ok = False

        if image_urls and _vlm_ok:
            # ── 私聊 VLM 接预算检查 ──
            _llm_gateway = None
            try:
                from .intelligence.llm_gateway import LLMGateway as _LLMGW
                from .service.bot_config import get_config_service as _get_cfg_svc2
                _llm_gateway = _LLMGW
                _vlm_cfg = _get_cfg_svc2().resolve_vlm_slot(self_id, "vlm_primary")
                _vlm_model = _vlm_cfg.model_name if _vlm_cfg else ""
                _vlm_prov = _vlm_cfg.provider if _vlm_cfg else ""
                _vlm_budget = _LLMGW.pre_check(
                    self_id, purpose="auto_vlm_private",
                    model=_vlm_model, provider=_vlm_prov,
                )
                if _vlm_budget == "hard_capped":
                    await event.send(MessageChain([Plain("今日 token 额度已用完，暂时无法看图。请明天再来。")]))
                    event.stop_event()
                    return
            except Exception:
                logger.debug("私聊 VLM 预算检查失败, fallthrough", exc_info=True)
            # ────────────────────────────────────────────
            if len(image_urls) > MAX_VLM_IMAGES:
                await event.send(MessageChain([Plain(
                    f"✨ 一下子发这么多图暮恩看不过来啦…最多只能仔细看 {MAX_VLM_IMAGES} 张～"
                )]))
                image_urls = image_urls[:MAX_VLM_IMAGES]
            _reset_vlm_usage()
            descriptions = await describe_images_from_urls(image_urls, file_ids=image_file_ids, user_query=str(content or ""))
            # ── VLM 用量写入统一统计 ──
            _vlm_usage = get_last_vlm_usage()
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
                        record_vlm_usage(user_id, admin_qq=self._lport_config.super_admin_qq)
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
        await event.send(MessageChain([Plain("❌ /role 命令已禁用。请直接发送消息与暮恩对话。")]))
        return

    async def _on_role_command_disabled(self, event: AstrMessageEvent):
        # 保留旧入口供参考, 实际不可达
        # ── Self-ID 身份门控: 仅处理双 bot 的事件 (fail-closed) ──
        if not self._self_id(event) or self._self_id(event) not in get_bot_qq_set():
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
            f"✨ {char_name} · 无限之蛇\n"
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
            "当前角色卡: v3.0「无限之蛇」· L-Port专属AI助手"
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

        await _maybe_compress_role_history(session, self.tavern, _bot_self_id)

        # 根据 self_id 解析角色卡 (双 Bot: 暮恩 vs )
        _bot_self_id = self._self_id(event)
        _char = get_character_for_self_id(_bot_self_id)

        tavern_messages: list[dict[str, str]] = [
            {"role": "system", "content": _char.get("system_prompt", "")},
        ]
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
                await event.send(MessageChain([Plain("今日 token 额度已用完，暂时无法私聊。请明天再来。")]))
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
                    "✨ 今天已经聊了很多了呢…明天再来找暮恩玩吧 (´・ω・`)"
                )]))
                session.history.pop()
                return
            # ────────────────────────────────────────────

            # ── 安全硬线: 正则预筛选 (零 LLM 成本) ──
            # 私聊绕过意图闸, 用正则兜底拦截明显的性化/敌意内容
            try:
                from astrbot_plugin_suli_social.input_classifier import InputClassifier
                _cls = InputClassifier()
                _cls_result = _cls.prescreen(content, is_addressed_to_me=True)
                if (
                    not _cls_result.needs_llm
                    and _cls_result.nature.value in ("sexualized", "hostile")
                ):
                    logger.warning(
                        "私聊 %s: 安全硬线拦截 nature=%s conf=%.2f",
                        user_id[:8], _cls_result.nature.value, _cls_result.confidence,
                    )
                    await event.send(MessageChain([Plain(
                        "❌ 消息包含不当内容，无法回复。"
                    )]))
                    session.history.pop()
                    return
            except ImportError:
                pass
            except Exception:
                logger.debug("私聊安全硬线异常, fail-open", exc_info=True)
            # ────────────────────────────────────────────

            sem = get_llm_semaphore(_bot_self_id)
            if sem:
                async with sem:
                    reply = await self.tavern.chat(tavern_messages)
            else:
                reply = await self.tavern.chat(tavern_messages)

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
            session.add_assistant(reply)
            await event.send(MessageChain([Plain(reply)]))
        except Exception:
            logger.exception("角色扮演失败: user=%s", user_id)
            session.history.pop()
            await event.send(MessageChain([Plain("❌ 对话出错了…等一下再试试吧 (´・ω・`)")]))

# ── Bot 身份查询 ── 从 BotIdentityService 动态读取, 不再硬编码 QQ 号。
# 向后兼容: get_bot_qq_set() 从 dual_bot.py (Phase 2) 导入已有懒加载机制。


def _bot_tag(bot_id: str) -> str:
    """返回 bot 日志标签, 如 [暮恩]。从 BotIdentityService 查询。"""
    try:
        from .service.bot_identity import get_bot_identity_service
        svc = get_bot_identity_service()
        bot = svc.get_bot(bot_id)
        if bot:
            return f"[{bot.name}]"
    except Exception:
        pass
    return f"[{bot_id[:8]}]"
