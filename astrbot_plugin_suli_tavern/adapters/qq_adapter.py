"""QQ 平台适配器 — CQ码 + OneBot API 实现。

将 OneBot V11 协议细节 (CQ码 / 引用格式 / API 调用) 封装在此，
业务层通过 platform_adapter.PlatformAdapter 接口调用，不接触协议细节。

集中管理的 QQ 特有模式:
  - CQ:at / CQ:image URL 解析
  - QQ 引用回复格式 [引用消息(...)] [At:数字]
  - @提及构建 [CQ:at,qq=...]
  - Markdown 清理 (QQ 不渲染)
  - OneBot get_image / send_group_msg API
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any

from ..platform_adapter import PlatformAdapter

logger = logging.getLogger(__name__)


class QQPlatformAdapter(PlatformAdapter):
    """QQ / OneBot V11 平台的协议适配器。

    集中管理所有 CQ 码和 OneBot API 调用，是 QQ 协议细节的唯一入口。
    """

    # ── 协议常量 ─────────────────────────────────────────

    @property
    def platform_name(self) -> str:
        return "qq"

    # ── CQ 码正则 (编译一次，复用) ──────────────────────

    # [CQ:at,qq=XXXXX] — 支持数字 QQ 号和字母数字 user_id
    CQ_AT_RE: re.Pattern = re.compile(r"\[CQ:at,qq=([^\]]+)\]")

    # [CQ:image,...url=...] — 提取图片 URL
    CQ_IMAGE_URL_RE: re.Pattern = re.compile(
        r"\[CQ:image,[^\]]*url=([^,\]]+)"
    )

    # [CQ:image,...file=...] — 提取图片文件标识 (file_id)
    CQ_IMAGE_FILE_RE: re.Pattern = re.compile(
        r"\[CQ:image,[^\]]*file=([^,\]]+)"
    )

    # [CQ:video,...url=...] — 提取视频 URL
    CQ_VIDEO_URL_RE: re.Pattern = re.compile(
        r"\[CQ:video,[^\]]*url=([^,\]]+)"
    )

    # [CQ:record,...url=...] — 提取语音 URL
    CQ_RECORD_URL_RE: re.Pattern = re.compile(
        r"\[CQ:record,[^\]]*url=([^,\]]+)"
    )

    # forward 段 id 键名 (不同 NapCat 版本)
    FORWARD_ID_KEYS: tuple = ("id", "resid", "forward_id")

    # QQ 引用回复前缀
    # 格式: [引用消息(发送者: 被引用内容)] [At:被引用者QQ] 实际消息
    QUOTE_PREFIX_RE: re.Pattern = re.compile(r"^\[引用消息\(")
    QUOTE_STRIP_RE: re.Pattern = re.compile(
        r"^\[引用消息\([^)]*\)\]\s*(?:\[At:\d+\]\s*)?"
    )

    # @名字 检测 (LLM 输出中的 @昵称)
    AT_NAME_RE: re.Pattern = re.compile(
        r'@([^\s'
        r'，。！？、：；""''（）()'
        r'\[\]【】'
        r'\n@'
        r']{1,20})'
    )

    # ── Markdown 清理正则 (QQ 不渲染) ───────────────────

    # LLM 假引用格式
    FAKE_QUOTE_RE_1 = re.compile(
        r"\[引用的消息内容[：:]\s*[^\]]*\]\s*"
    )
    FAKE_QUOTE_RE_2 = re.compile(r"\[引用的消息内容\]\s*")

    # Markdown 语法
    MD_CODE_BLOCK_RE = re.compile(
        r"```[^\n]*\n.*?```", re.DOTALL
    )
    MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
    MD_BOLD_ITALIC_RE = re.compile(r"\*\*\*(.+?)\*\*\*")
    MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    MD_ITALIC_RE = re.compile(r"(?<!\*)\*(.+?)\*(?!\*)")
    MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
    MD_HRULE_RE = re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE)

    # ── 消息解析实现 ─────────────────────────────────────

    def parse_at_targets(self, raw_text: str) -> list[str]:
        """从 [CQ:at,qq=XXXXX] 提取所有被 @ 的 QQ 号。"""
        if not raw_text:
            return []
        return self.CQ_AT_RE.findall(raw_text)

    def parse_image_urls(self, raw_text: str) -> list[str]:
        """从 [CQ:image,...url=...] 提取图片 URL。"""
        if not raw_text:
            return []
        urls: list[str] = []
        for m in self.CQ_IMAGE_URL_RE.finditer(raw_text):
            url = m.group(1)
            if url:
                urls.append(url)
        return urls

    def parse_image_file_ids(self, raw_text: str) -> list[str]:
        """从 [CQ:image,...file=...] 提取图片文件标识，过滤本地路径/http。"""
        if not raw_text:
            return []
        ids: list[str] = []
        for m in self.CQ_IMAGE_FILE_RE.finditer(raw_text):
            fid = (m.group(1) or "").strip()
            if fid and not fid.startswith(("/", "./", "file://", "http")):
                ids.append(fid)
        return ids

    def parse_video_urls(self, raw_text: str) -> list[str]:
        """从 [CQ:video,...url=...] 提取视频 URL。"""
        if not raw_text:
            return []
        urls: list[str] = []
        for m in self.CQ_VIDEO_URL_RE.finditer(raw_text):
            url = m.group(1)
            if url:
                urls.append(url)
        return urls

    def parse_voice_urls(self, raw_text: str) -> list[str]:
        """从 [CQ:record,...url=...] 提取语音 URL。"""
        if not raw_text:
            return []
        urls: list[str] = []
        for m in self.CQ_RECORD_URL_RE.finditer(raw_text):
            url = m.group(1)
            if url:
                urls.append(url)
        return urls

    @staticmethod
    def _norm_seg_kind(t: Any) -> str:
        """段类型归一化: ComponentType.Reply→reply，原始小写串原样。"""
        try:
            s = str(t or "")
            if "." in s:
                s = s.rsplit(".", 1)[-1]
            return s.lower()
        except Exception:
            return ""

    def parse_forward_id(self, raw_seg: Any) -> str:
        """从 OneBot 原始段提取 forward id (id/resid/forward_id)。"""
        if not isinstance(raw_seg, dict):
            return ""
        if self._norm_seg_kind(raw_seg.get("type")) != "forward":
            return ""
        seg_data = raw_seg.get("data", {})
        if isinstance(seg_data, dict):
            for key in self.FORWARD_ID_KEYS:
                val = seg_data.get(key)
                if val:
                    return str(val).strip()
        return ""

    def parse_raw_seg(self, raw_seg: Any) -> dict[str, str]:
        """归一化 OneBot 原始段 / AstrBot 组件对象为统一结构。"""
        norm: dict[str, str] = {
            "kind": "", "text": "", "url": "",
            "file_id": "", "sender_id": "", "ref_id": "",
        }
        if isinstance(raw_seg, dict):
            # OneBot 原始段: {"type": ..., "data": {...}, "sender": {...}}
            norm["kind"] = self._norm_seg_kind(raw_seg.get("type"))
            data = raw_seg.get("data", {})
            if not isinstance(data, dict):
                data = {}
            sender = raw_seg.get("sender", {})
            if isinstance(sender, dict):
                norm["sender_id"] = str(sender.get("user_id", "") or "")
            if norm["kind"] == "text":
                norm["text"] = str(data.get("text", "") or "")
            elif norm["kind"] in ("image", "video", "record"):
                norm["url"] = str(data.get("url", "") or "")
                norm["file_id"] = str(data.get("file", "") or "")
            elif norm["kind"] == "reply":
                norm["ref_id"] = str(data.get("id", "") or "")
            elif norm["kind"] == "json":
                norm["text"] = str(data.get("data", "") or "")
            elif norm["kind"] == "share":
                norm["url"] = str(data.get("url", "") or "")
                norm["text"] = str(data)[:500]
            elif norm["kind"] in ("xml", "at", "face"):
                norm["text"] = str(data)[:200]
            return norm
        # AstrBot 组件对象形态
        kind = self._norm_seg_kind(getattr(raw_seg, "type", ""))
        norm["kind"] = kind
        norm["url"] = str(getattr(raw_seg, "url", "") or "")
        norm["file_id"] = str(
            getattr(raw_seg, "file_id", None)
            or getattr(raw_seg, "file", "") or ""
        )
        norm["text"] = str(getattr(raw_seg, "text", "") or "")
        norm["sender_id"] = str(getattr(raw_seg, "sender_id", "") or "")
        ref = getattr(raw_seg, "message_id", None) or getattr(raw_seg, "id", None)
        if kind == "reply" and ref is not None:
            norm["ref_id"] = str(ref)
        return norm

    def strip_quote_prefix(self, text: str) -> str:
        """剥离 QQ 引用回复前缀。"""
        if not text:
            return text
        return self.QUOTE_STRIP_RE.sub("", text).strip()

    def has_quote_prefix(self, text: str) -> bool:
        """判断是否以 QQ 引用格式开头。"""
        if not text:
            return False
        return bool(self.QUOTE_PREFIX_RE.match(text))

    def get_at_mention_regex(self) -> re.Pattern:
        """返回 @名字 匹配正则。"""
        return self.AT_NAME_RE

    # ── 消息构建实现 ─────────────────────────────────────

    def build_at_mention(self, user_id: str) -> str:
        """构建 QQ [CQ:at,qq=...] 格式。"""
        return f"[CQ:at,qq={user_id}]"

    # ── 卡片/分享元数据提取实现 ──────────────────────────

    # CQ share/json/xml 卡片正则
    CQ_CARD_RE: re.Pattern = re.compile(
        r"\[CQ:(?:json|xml|share),[^\]]*?data=([^\]]+)"
    )

    def extract_card_hints(self, raw_message_str: str) -> str:
        """从 QQ CQ 码文本中提取分享/JSON/XML 卡片元数据。

        [CQ:json,data=...] / [CQ:share,...] / [CQ:xml,data=...]
        → 人类可读摘要文本 (URL 前置确保截断安全)
        """
        if not raw_message_str:
            return ""

        parts: list[str] = []
        for m in self.CQ_CARD_RE.finditer(raw_message_str):
            data = m.group(1)
            if data:
                # 委托到 _append_card_data (从 main.py 迁移的辅助逻辑)
                self._append_card_data(data, parts)

        return "\n".join(parts)

    @staticmethod
    def _append_card_data(data: str, parts: list[str]) -> None:
        """解析 CQ 卡片 data 字段并追加到 parts。

        data 字段是 JSON 字符串 (可能含 HTML 实体编码)。
        关键: URL 放在最前面 ([分享链接] 前缀)，确保上下文截断时可见。
        """
        import json as _json

        text = data
        # HTML 实体还原
        for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                             ("&quot;", '"'), ("&#39;", "'"), ("&#x27;", "'"),
                             ("&apos;", "'")]:
            text = text.replace(entity, char)

        # 提取 URL 优先展示
        url = ""
        try:
            obj = _json.loads(text)
            if isinstance(obj, dict):
                url = (
                    obj.get("url", "")
                    or obj.get("jumpUrl", "")
                    or obj.get("share_url", "")
                    or obj.get("link", "")
                )
                if not url:
                    meta = obj.get("meta", {})
                    if isinstance(meta, dict):
                        for section in ("news", "music", "detail_1", "detail"):
                            sd = meta.get(section, {})
                            if isinstance(sd, dict):
                                url = sd.get("jumpUrl", "") or sd.get("url", "")
                                if url:
                                    break
        except (_json.JSONDecodeError, TypeError):
            pass

        # JSON 解析未找到 → 正则兜底: 从原始字符串中搜 http URL
        if not url or not (isinstance(url, str) and url.startswith("http")):
            m = re.search(r'https?://[^\s"\'\\,\}\]\)>]+', text)
            if m:
                url = m.group(0).rstrip(".")

        if url and isinstance(url, str) and url.startswith("http"):
            parts.append(f"[分享链接] {url}")

        # 摘要 (截断到 500 字符)
        text_stripped = text.strip()
        summary = text_stripped[:500] + ("..." if len(text_stripped) > 500 else "")
        if summary:
            parts.append(summary)

    # ── 平台代码剥离实现 ─────────────────────────────────

    def strip_platform_codes(self, text: str) -> str:
        """剥离所有 [CQ:...] 码 (at/image/reply/face/mface/...)。

        用于噪音检测——只看人打的实际文字，不含协议标记。
        """
        if not text:
            return text
        return re.sub(r"\[CQ:[^\]]+\]", "", text).strip()

    # ── 回复清理实现 ─────────────────────────────────────

    def sanitize_reply(self, text: str) -> str:
        """清理 LLM 回复中的 Markdown (QQ 不渲染)。

        即使 system prompt 已禁止 Markdown，LLM 偶尔仍会输出，
        此函数作为最后防线在发送前强制清理。

        清理内容:
          - **粗体** / *斜体* / ***粗斜体*** → 纯文本
          - # 标题 → 纯文本
          - `代码` / ```代码块``` → 纯文本
          - [引用的消息内容: ...] 假引用格式
          - 多余空行 (>1连续) → 单个换行
          - 行首行尾空格
        """
        if not text:
            return text

        # 0. 清理 LLM 假引用
        text = self.FAKE_QUOTE_RE_1.sub("", text)
        text = self.FAKE_QUOTE_RE_2.sub("", text)

        # 1. 代码块 (先处理)
        text = self.MD_CODE_BLOCK_RE.sub(
            lambda m: m.group(0).replace("`", ""), text
        )
        text = self.MD_INLINE_CODE_RE.sub(r"\1", text)

        # 2. 粗体+斜体
        text = self.MD_BOLD_ITALIC_RE.sub(r"\1", text)
        # 3. 粗体
        text = self.MD_BOLD_RE.sub(r"\1", text)
        # 4. 斜体
        text = self.MD_ITALIC_RE.sub(r"\1", text)

        # 5. 标题
        text = self.MD_HEADING_RE.sub("", text)

        # 6. 水平线
        text = self.MD_HRULE_RE.sub("", text)

        # 7. 连续换行 → 单换行
        text = re.sub(r"\n{2,}", "\n", text)

        # 8. 行尾空格
        text = "\n".join(line.rstrip() for line in text.split("\n"))
        text = text.strip()

        return text

    # ── 媒体 API 实现 ─────────────────────────────────────

    async def download_image(
        self, file_id: str, bot: Any = None,
    ) -> bytes | None:
        """通过 OneBot get_image API 下载图片原始字节。

        优先使用 OneBot API (绕过 QQ CDN rkey 鉴权)，
        bot 为 None 或 API 失败时返回 None (调用方应 fallback HTTP)。
        """
        if bot is None or not file_id:
            return None

        try:
            resp = await bot.call_action("get_image", file=file_id)
            file_data: bytes | None = None

            if isinstance(resp, dict):
                raw = (
                    resp.get("file")
                    or resp.get("data")
                    or resp.get("image")
                    or b""
                )
                if isinstance(raw, str):
                    # NapCat/LLOneBot: file=base64 string
                    try:
                        file_data = base64.b64decode(raw)
                    except Exception:
                        logger.debug(
                            "get_image: file 字段不是有效 base64, len=%d",
                            len(raw),
                        )
                        file_data = None
                elif isinstance(raw, bytes):
                    file_data = raw
            elif isinstance(resp, bytes):
                file_data = resp
            elif isinstance(resp, str):
                try:
                    file_data = base64.b64decode(resp)
                except Exception:
                    file_data = None

            if file_data and len(file_data) > 512:
                logger.debug(
                    "OneBot get_image 成功: %d bytes", len(file_data)
                )
                return file_data

            logger.debug(
                "get_image 数据过小或无效: %d bytes",
                len(file_data) if file_data else 0,
            )
        except Exception:
            logger.debug("get_image API 失败", exc_info=True)

        return None

    @staticmethod
    async def _api_call(bot: Any, action: str, **kwargs: Any) -> Any:
        """兼容两种 bot 形态取 call_action: 原始 API 客户端 (bot.call_action)
        与 AstrBot 平台包装 (bot.api.call_action)。"""
        call = getattr(bot, "call_action", None)
        if not callable(call):
            call = getattr(getattr(bot, "api", None), "call_action", None)
        if not callable(call):
            return None
        try:
            return await call(action, **kwargs)
        except Exception:
            logger.debug("OneBot %s 调用失败 (QQ)", action, exc_info=True)
            return None

    async def fetch_forward_detail(self, bot: Any, forward_id: str) -> dict | None:
        """调用 get_forward_msg 展开转发全文 (多参数名重试，兼容不同 NapCat 版本)。"""
        safe_id = str(forward_id or "").strip()
        if not safe_id or bot is None:
            return None
        for kwargs in [
            {"message_id": safe_id},
            {"id": safe_id},
            {"res_id": safe_id},
            {"resid": safe_id},
        ]:
            data = await self._api_call(bot, "get_forward_msg", **kwargs)
            if isinstance(data, dict) and data:
                return data
        # int 变体 (某些 NapCat 版本只接受 int)
        if safe_id.isdigit():
            data = await self._api_call(bot, "get_forward_msg", message_id=int(safe_id))
            if isinstance(data, dict) and data:
                return data
        logger.debug("get_forward_msg 未能获取内容, id=%s", safe_id)
        return None

    async def fetch_replied_msg(self, bot: Any, message_id: int) -> dict | None:
        """调用 get_msg 拉取被引用消息全文。"""
        if not message_id or bot is None:
            return None
        data = await self._api_call(bot, "get_msg", message_id=int(message_id))
        return data if isinstance(data, dict) else None

    # ── Prompt 描述实现 ───────────────────────────────────

    def get_quote_format_description(self) -> str:
        """返回 QQ 引用消息格式描述 (注入 LLM prompt)。"""
        return (
            "[引用消息(发送者: 被引用内容)] [At:被引用者QQ] 实际消息\n"
            "[引用消息(...)] 是引用块，[At:QQ] 是 QQ 自动@被引用者的引用标记。"
        )

    def get_at_mention_description(self) -> str:
        """返回 QQ @提及 格式描述。"""
        return "[At:QQ号] 表示被 @的用户"

    # ── 平台 API 实现 ─────────────────────────────────────

    async def call_platform_api(
        self, action: str, bot: Any, **kwargs: Any,
    ) -> Any:
        """调用 OneBot API。

        用于 escape-hatch 场景：高层抽象不满足时直接调 OneBot API。
        例如: get_msg, get_forward_msg, send_group_msg (video) 等。
        """
        try:
            return await bot.call_action(action, **kwargs)
        except Exception:
            logger.debug(
                "call_platform_api %s 失败 (QQ)", action, exc_info=True
            )
            return None

    async def send_platform_message(
        self,
        bot: Any,
        group_id: str | None = None,
        user_id: str | None = None,
        message: Any = None,
        self_id: str = "*",
        **kwargs: Any,
    ) -> Any:
        """通过 OneBot API 发送消息。

        group_id 非空 → send_group_msg
        user_id 非空  → send_private_msg
        """
        if group_id and str(group_id).isdigit():
            return await bot.send_group_msg(
                group_id=int(group_id),
                message=message,
                self_id=self_id,
            )
        elif user_id and str(user_id).isdigit():
            return await bot.send_private_msg(
                user_id=int(user_id),
                message=message,
                self_id=self_id,
            )
        return None
