"""平台协议抽象层 (Platform Adapter ABC)。

定义平台无关的消息解析/构建/媒体API接口。
QQ 实现此接口，业务层只依赖此抽象。

目标架构:
  业务逻辑层 (main.py / group_chat.py / reply_postprocessor.py / ...)
      ↓ 只调用 get_adapter().xxx()
  PlatformAdapter (ABC)
      └── QQPlatformAdapter (CQ码 + OneBot API)

用法:
  from .platform_adapter import get_adapter, init_adapter
  from .adapters.qq_adapter import QQPlatformAdapter

  # 启动时初始化
  init_adapter(QQPlatformAdapter())

  # 业务代码
  adapter = get_adapter()
  user_ids = adapter.parse_at_targets(raw_text)
  at_str = adapter.build_at_mention(user_id)
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

# ── 模块级单例 ──────────────────────────────────────────

_adapter: PlatformAdapter | None = None


def init_adapter(adapter: PlatformAdapter) -> None:
    """初始化平台适配器 (启动时调用一次)。"""
    global _adapter
    _adapter = adapter


def get_adapter() -> PlatformAdapter:
    """获取当前平台适配器单例。"""
    if _adapter is None:
        raise RuntimeError(
            "PlatformAdapter 未初始化 — 请在启动时调用 init_adapter()"
        )
    return _adapter


# ── ABC 定义 ────────────────────────────────────────────


class PlatformAdapter(ABC):
    """平台协议抽象基类。

    隔离所有平台特有的:
      - 消息格式 (CQ码 / 引用格式)
      - API 调用  (OneBot API)
      - @提及语法 (@name → 平台特定@格式)

    业务层代码绝不直接接触任何协议细节——全部通过此接口。
    """

    # ── 协议标识 ─────────────────────────────────────────

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """平台名称 (如 'qq')。"""
        ...

    # ── 消息解析: 从平台原始文本提取结构化数据 ──────────

    @abstractmethod
    def parse_at_targets(self, raw_text: str) -> list[str]:
        """从消息原始文本提取所有 @目标的 user_id 列表。

        QQ: 从 [CQ:at,qq=XXXXX] 提取
        """
        ...

    @abstractmethod
    def parse_image_urls(self, raw_text: str) -> list[str]:
        """从消息原始文本提取所有图片 URL。

        QQ: 从 [CQ:image,...url=...] 提取
        """
        ...

    @abstractmethod
    def parse_image_file_ids(self, raw_text: str) -> list[str]:
        """从消息原始文本提取所有图片文件标识 (file_id)。

        QQ: 从 [CQ:image,...file=...] 提取，过滤本地路径/http。
        返回的 id 可直接用于下载 API。
        """
        ...

    @abstractmethod
    def parse_video_urls(self, raw_text: str) -> list[str]:
        """从消息原始文本提取所有视频 URL。

        QQ: 从 [CQ:video,...url=...] 提取
        """
        ...

    @abstractmethod
    def parse_voice_urls(self, raw_text: str) -> list[str]:
        """从消息原始文本提取所有语音 URL。

        QQ: 从 [CQ:record,...url=...] 提取
        """
        ...

    @abstractmethod
    def parse_forward_id(self, raw_seg: Any) -> str:
        """从平台原始消息段提取合并转发 ID。

        QQ: {"type": "forward", "data": {"id" | "resid" | "forward_id"}}
        返回空字符串表示该段不是合并转发。
        """
        ...

    @abstractmethod
    def parse_raw_seg(self, raw_seg: Any) -> dict[str, str]:
        """归一化平台原始消息段为统一结构。

        返回 keys: kind / text / url / file_id / sender_id / ref_id
        （缺失项为空字符串）。QQ OneBot {type, data} dict 与
        AstrBot 组件对象两种形态均兼容。
        kind 取值如: text / image / reply / forward / video /
        record / share / json / xml / at / face ……
        ref_id 仅 reply 段有值 (被引用消息 id)。
        """
        ...

    @abstractmethod
    def strip_quote_prefix(self, text: str) -> str:
        """剥离平台特有的引用回复前缀, 返回实际消息内容。

        QQ: [引用消息(发送者: 内容)] [At:QQ号] 实际消息 → 实际消息
        """
        ...

    @abstractmethod
    def has_quote_prefix(self, text: str) -> bool:
        """判断文本是否以平台特有的引用格式开头。"""
        ...

    @abstractmethod
    def get_at_mention_regex(self) -> re.Pattern:
        """返回匹配 @名字 的正则 (业务层用于从 LLM 输出中检测 @)。"""
        ...

    # ── 消息构建: 从结构化数据生成平台格式 ──────────────

    @abstractmethod
    def build_at_mention(self, user_id: str) -> str:
        """构建平台特定的 @提及 文本表示。

        QQ: [CQ:at,qq=123456]

        此文本在发送前会被框架解析为实际的 @组件。
        """
        ...

    # ── 平台代码剥离 ─────────────────────────────────────

    @abstractmethod
    def strip_platform_codes(self, text: str) -> str:
        """剥离平台特有的内联代码/标记，返回纯文本。

        QQ: 剥离所有 [CQ:...] 码 (at/image/reply/face/...)

        用于噪音检测等需要「只看人打的实际文字」的场景。
        """
        ...

    # ── 卡片/分享元数据提取 ──────────────────────────────

    @abstractmethod
    def extract_card_hints(self, raw_message_str: str) -> str:
        """从平台原始消息文本中提取分享卡片/JSON/XML 元数据。

        QQ: 从 [CQ:json|xml|share,...] CQ 码提取

        返回人类可读的摘要文本，用于注入 LLM 上下文。
        返回空字符串表示无卡片数据。
        """
        ...

    # ── 回复清理 ─────────────────────────────────────────

    @abstractmethod
    def sanitize_reply(self, text: str) -> str:
        """清理 LLM 回复中平台不支持的格式标记。

        QQ 不支持 Markdown，需清理 **粗体**/#标题/多余空行。

        注意: 此函数可能为空实现（平台支持富文本时）。
        """
        ...

    # ── 媒体 API ─────────────────────────────────────────

    @abstractmethod
    async def download_image(self, file_id: str, bot: Any = None) -> bytes | None:
        """通过平台 API 下载图片原始字节。

        QQ: OneBot get_image API (绕过 CDN rkey 鉴权)

        Args:
            file_id: 平台特定的文件标识 (QQ: file UUID)
            bot:     平台 Bot 实例 (用于 API 调用)

        Returns:
            图片字节数据，失败返回 None
        """
        ...

    # ── Prompt 描述: 供 LLM prompt 构建使用 ─────────────

    @abstractmethod
    def get_quote_format_description(self) -> str:
        """返回平台引用消息格式的描述文本 (注入 LLM system prompt)。

        QQ 示例:
          "[引用消息(发送者: 被引用内容)] [At:被引用者QQ] 实际消息"
        """
        ...

    @abstractmethod
    def get_at_mention_description(self) -> str:
        """返回平台 @提及 格式的描述文本 (注入 LLM system prompt)。

        QQ 示例:
          "[At:QQ号] 表示被 @的用户"
        """
        ...

    # ── 平台 API 调用 (底层) ─────────────────────────────

    @abstractmethod
    async def fetch_forward_detail(self, bot: Any, forward_id: str) -> dict | None:
        """拉取合并转发全文 (含版本兼容重试)。

        QQ: get_forward_msg 多参数名重试 (message_id/id/res_id/resid + int 变体)。
        返回 NapCat 原始数据 (含 messages 列表)，失败返回 None。
        """
        ...

    @abstractmethod
    async def fetch_replied_msg(self, bot: Any, message_id: int) -> dict | None:
        """拉取被引用消息全文。

        QQ: get_msg。返回 NapCat 原始数据，失败返回 None。
        """
        ...

    @abstractmethod
    async def call_platform_api(
        self, action: str, bot: Any, **kwargs: Any,
    ) -> Any:
        """调用平台底层 API。

        QQ: OneBot call_action (e.g. send_group_msg, get_forward_msg)

        此方法是 escape hatch — 当高层抽象不满足需求时使用。
        优先使用上述高层方法。
        """
        ...

    @abstractmethod
    async def send_platform_message(
        self,
        bot: Any,
        group_id: str | None = None,
        user_id: str | None = None,
        message: Any = None,
        **kwargs: Any,
    ) -> Any:
        """发送消息到平台。

        QQ: OneBot send_group_msg / send_private_msg
        """
        ...


# ── 模块级便利函数 ──────────────────────────────────────
# 业务代码可 import 这些函数，免去每次 call get_adapter()


def parse_at_targets(raw_text: str) -> list[str]:
    """从原始文本提取 @目标的 user_id 列表。"""
    return get_adapter().parse_at_targets(raw_text)


def parse_image_urls(raw_text: str) -> list[str]:
    """从原始文本提取图片 URL 列表。"""
    return get_adapter().parse_image_urls(raw_text)


def strip_quote_prefix(text: str) -> str:
    """剥离平台引用回复前缀。"""
    return get_adapter().strip_quote_prefix(text)


def has_quote_prefix(text: str) -> bool:
    """判断是否有平台引用回复前缀。"""
    return get_adapter().has_quote_prefix(text)


def build_at_mention(user_id: str) -> str:
    """构建平台 @提及 文本。"""
    return get_adapter().build_at_mention(user_id)


def strip_platform_codes(text: str) -> str:
    """剥离平台特有内联代码 (如 CQ 码)。"""
    return get_adapter().strip_platform_codes(text)


def sanitize_reply(text: str) -> str:
    """清理 LLM 回复中的平台不支持格式。"""
    return get_adapter().sanitize_reply(text)
