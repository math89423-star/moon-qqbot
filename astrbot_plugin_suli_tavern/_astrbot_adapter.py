"""AstrBot → NoneBot API 适配层。

让 group_chat.py / sticker_sender.py 等少量框架耦合文件
无需改动即可在 AstrBot 下运行。

提供:
  - BotAdapter    — 模拟 nonebot.adapters.onebot.v11.Bot
  - EventAdapter  — 模拟 GroupMessageEvent
  - MessageSegment — 模拟 MessageSegment.text/image/at
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Image, Plain, Reply

logger = logging.getLogger(__name__)

# ── CQ 码解析 — 将 [CQ:at,qq=...] 纯文本转 At 段 ────
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)\]")


def _parse_cq_segments(text: str, *, allowed_qqs: frozenset[str] | None = None) -> list[Plain | At]:
    """将含 CQ 码的字符串拆分为 Plain + At 段列表。

    [CQ:at,qq=123456] → At(qq="123456")，其余文本 → Plain。

    安全: 未在 allowed_qqs 白名单中的 CQ 码会转义为纯文本，
    防止 LLM 被诱导输出 [CQ:at,...] 来 @骚扰陌生人。
    """
    segments: list[Plain | At] = []
    pos = 0
    for m in _CQ_AT_RE.finditer(text):
        qq = m.group(1)
        if m.start() > pos:
            segments.append(Plain(text[pos:m.start()]))
        if allowed_qqs is None or qq in allowed_qqs:
            segments.append(At(qq=qq))
        else:
            # 不在白名单 → 方括号转义为纯文本, 不触发 @
            segments.append(Plain(f"[CQ:at,qq={qq}]"))
        pos = m.end()
    if pos < len(text):
        segments.append(Plain(text[pos:]))
    return segments or [Plain(text)]


# ── MessageSegment 适配 ──────────────────────────

class MessageSegment:
    """兼容 NoneBot MessageSegment 的静态工厂。"""

    @staticmethod
    def text(content: str) -> Plain:
        return Plain(content)

    @staticmethod
    def image(data: bytes | str) -> Image:
        """支持 bytes (base64 编码) 或 str (文件路径/URL)。"""
        if isinstance(data, bytes):
            b64 = base64.b64encode(data).decode("ascii")
            return Image(file=f"base64://{b64}")
        return Image(file=data)

    @staticmethod
    def at(user_id: str | int) -> At:
        return At(qq=str(user_id))


# ── Reply 引用组件构造 ──────────────────────────────


def _make_reply_component(message_id: str | int) -> Any | None:
    """创建 AstrBot Reply 消息组件。多参数名尝试，兼容不同框架版本。

    返回 Reply 组件或 None（message_id 无效/创建失败）。
    """
    if not message_id:
        return None
    mid_str = str(message_id).strip()
    if not mid_str:
        return None
    candidates: list[Any] = [mid_str]
    try:
        candidates.append(int(mid_str))
    except (TypeError, ValueError):
        pass
    for value in candidates:
        for kwargs in ({"id": value}, {"message_id": value}, {"msg_id": value}):
            try:
                return Reply(**kwargs)
            except Exception:
                continue
        try:
            return Reply(value)
        except Exception:
            continue
    return None


def _extract_message_id(event: EventAdapter | AstrMessageEvent) -> str:
    """从 EventAdapter 或 AstrMessageEvent 中提取 message_id。

    用于 reply/quote 消息锚定 — 返回触发消息的 message_id 字符串。
    返回空字符串表示提取失败。
    """
    # EventAdapter 有 message_id property (int)
    if isinstance(event, EventAdapter):
        mid = getattr(event, "message_id", 0)
        return str(mid) if mid else ""

    # AstrMessageEvent: 尝试多个属性
    msg_obj = getattr(event, "message_obj", None)
    for attr in ("message_id", "id", "seq", "message_seq", "real_id"):
        val = getattr(msg_obj, attr, None) if msg_obj is not None else None
        if val is not None and str(val).strip():
            return str(val).strip()
    # 回退: event 自身
    for attr in ("message_id", "id"):
        val = getattr(event, attr, None)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""

class _Sender:
    """模拟 event.sender 对象。"""
    def __init__(self, card: str = "", nickname: str = ""):
        self.card = card
        self.nickname = nickname


class _MessageSegment:
    """模拟 OneBot 消息段 (用于 event.get_message() 返回)。"""
    def __init__(self, seg_type: str, data: dict[str, Any] | None = None):
        self.type = seg_type
        self.data = data or {}


class EventAdapter:
    """将 AstrMessageEvent 包装为 GroupMessageEvent 兼容接口。"""

    def __init__(self, event: AstrMessageEvent, group_id: int, self_id: str = ""):
        self._event = event
        self.group_id = group_id
        self._self_id = self_id
        self._parse_message()

    def _parse_message(self) -> None:
        """解析消息内容。"""
        self._plaintext = str(getattr(self._event, "message_str", "") or "")
        self._segments: list[_MessageSegment] = []
        try:
            components = self._event.get_messages()
            for comp in components:
                comp_type = str(getattr(comp, "type", "") or "")
                if comp_type == "at":
                    qq = str(getattr(comp, "qq", "") or "")
                    self._segments.append(_MessageSegment("at", {"qq": qq}))
                elif comp_type == "image":
                    url = getattr(comp, "url", None)
                    file = getattr(comp, "file", None)
                    self._segments.append(_MessageSegment(
                        "image",
                        {"url": str(url or ""), "file": str(file or "")},
                    ))
                elif comp_type == "reply":
                    mid = getattr(comp, "message_id", None) or getattr(comp, "id", None)
                    # 获取被引用消息的发送者 QQ (用于 _is_reply_to_bot 校验)
                    reply_qq = (
                        getattr(comp, "qq", None)
                        or getattr(comp, "user_id", None)
                    )
                    seg_data: dict[str, str] = {"id": str(mid or "")}
                    if reply_qq:
                        seg_data["qq"] = str(reply_qq)
                    self._segments.append(_MessageSegment("reply", seg_data))
                elif comp_type == "text":
                    text = getattr(comp, "text", None)
                    self._segments.append(_MessageSegment(
                        "text", {"text": str(text or "")},
                    ))
        except Exception:
            pass

    def get_user_id(self) -> str:
        try:
            return str(self._event.get_sender_id())
        except Exception:
            return ""

    def get_plaintext(self) -> str:
        return self._plaintext

    def get_message(self) -> list[_MessageSegment]:
        return self._segments

    @property
    def sender(self) -> _Sender:
        card = ""
        nickname = ""
        for name in ("get_sender_name", "get_sender_nickname"):
            func = getattr(self._event, name, None)
            if callable(func):
                try:
                    value = str(func() or "").strip()
                    if value:
                        card = value
                        nickname = value
                        break
                except Exception:
                    pass
        if not card:
            message_obj = getattr(self._event, "message_obj", None)
            sender_obj = getattr(message_obj, "sender", None) if message_obj is not None else None
            card = str(getattr(sender_obj, "card", "") or "").strip()
            nickname = str(getattr(sender_obj, "nickname", "") or "").strip()
            if not card and not nickname:
                nickname = str(getattr(sender_obj, "name", "") or "").strip()
        return _Sender(card=card or nickname, nickname=nickname or card)

    @property
    def message_id(self) -> int:
        try:
            return int(getattr(self._event, "message_id", 0) or 0)
        except (TypeError, ValueError):
            try:
                message_obj = getattr(self._event, "message_obj", None)
                return int(getattr(message_obj, "message_id", 0) or 0)
            except (TypeError, ValueError):
                return 0

    @property
    def raw_message(self) -> str:
        """兼容 OneBot raw_message — 返回消息原文 (含 CQ 码等价文本)。"""
        try:
            msg_obj = getattr(self._event, "message_obj", None)
            raw = getattr(msg_obj, "raw_message", None) if msg_obj is not None else None
            if raw:
                return str(raw)
        except Exception:
            pass
        return self._plaintext


# ── BotAdapter ────────────────────────────────────

class BotAdapter:
    """将 AstrBot 的发送能力包装为 NoneBot Bot 兼容接口。"""

    def __init__(self, event: AstrMessageEvent, self_id: str = ""):
        self._event = event
        self.self_id = self_id

    async def send(
        self,
        event: EventAdapter | AstrMessageEvent,
        message: Plain | Image | At | str,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """发送消息。兼容 NoneBot bot.send(event, message) 接口。

        kwargs 支持:
          - reply_message: bool — 是否引用回复触发消息。
            当 True 时，会在消息链开头插入 Reply 组件，锚定 event 的 message_id。
            适用于 mention/nickname/reply/thread_continuation 等有明确触发消息的场景。
            不适用于 batch/debounce/proactive 等无单一触发消息的场景。
        """
        target_event: AstrMessageEvent
        if isinstance(event, EventAdapter):
            target_event = event._event
        else:
            target_event = event

        # ── 引用回复: 当 reply_message=True 时，在消息链头插入 Reply 组件 ──
        _reply_component = None
        if kwargs.get("reply_message"):
            _mid = _extract_message_id(event)
            if _mid:
                _reply_component = _make_reply_component(_mid)

        # ── 构建基础消息链 ──
        _base_segments: list[Any] = []
        if isinstance(message, str):
            _base_segments = list(_parse_cq_segments(message))
        elif isinstance(message, (Plain, Image, At, Reply)):
            _base_segments = [message]
        elif isinstance(message, list):
            _base_segments = [
                Plain(seg) if isinstance(seg, str) else seg
                for seg in message
                if isinstance(seg, (str, Plain, Image, At, Reply))
            ]

        # ── 组装最终消息链 (Reply 在最前面) ──
        _final: list[Any] = []
        if _reply_component is not None:
            _final.append(_reply_component)
        _final.extend(_base_segments)

        if _final:
            await target_event.send(MessageChain(_final))
        return None

    async def call_action(self, action: str, **kwargs: Any) -> Any:
        """代理 OneBot API 调用。"""
        try:
            return await self._event.bot.api.call_action(action, **kwargs)
        except Exception:
            logger.debug("call_action %s 失败", action, exc_info=True)
            return None
