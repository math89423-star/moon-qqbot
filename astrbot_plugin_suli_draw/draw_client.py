"""图像生成 API 客户端 — 双后端自适应。

- gpt-image-2 → OpenAI Images API (/v1/images/generations, /v1/images/edits)
- gemini-*    → Chat Completions API (多模态输出, response_modalities=["IMAGE"])
               通过 vectorengine.ai 代理统一入口。

自然语言驱动: LLM 通过 function calling 传入 prompt 即可生图/以图生图。
"""

from __future__ import annotations

import base64
import builtins
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

# ── 异常类型 ───────────────────────────────────────────────


class ImageGenError(Exception):
    """图像生成基础异常。"""

    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class AuthError(ImageGenError):
    """认证失败 (401/403)。"""


class RateLimitError(ImageGenError):
    """频率限制 (429)。"""


class ContentModerationError(ImageGenError):
    """内容审核拦截。"""


class TimeoutError(ImageGenError):  # noqa: A001
    """请求超时 (内置 TimeoutError 的影子, 兼容 tools.py 导入)。"""


# ── 结果类型 ───────────────────────────────────────────────


@dataclass
class ImageResult:
    """单张生成结果。"""

    bytes_data: bytes
    revised_prompt: str = ""


# ── SSRF 防护 ─────────────────────────────────────────────
#
# ⚠️ 注意: allow_redirects=False + 拒绝 3xx 会阻断使用 CDN 重定向分发的图片源。
#   当前白名单内的 oss.filenest.top / vectorengine.ai 不走重定向，不受影响。
#   若后续接入新图片源 → 先确认其 CDN 策略，将域名加入 _SAFE_*_HOSTS 白名单。

# 已知安全的图片托管域名 (gpt-image-2 输出目标)
_SAFE_IMAGE_HOSTS: frozenset[str] = frozenset({"oss.filenest.top"})

# 已知安全的图片代理域名 (vectorengine 代理返回)
_SAFE_PROXY_HOSTS: frozenset[str] = frozenset({"vectorengine.ai", "api.vectorengine.ai"})


def _is_safe_image_url(url_str: str) -> bool:
    """验证图片 URL 是否安全可下载。

    阻止 SSRF: 拒绝私有/环回/链路本地 IP, 拒绝非 HTTP 协议。
    白名单: 已知托管域名跳过 DNS 解析。
    """
    try:
        parsed = urlparse(url_str)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname or ""
    if not hostname:
        return False
    # 已知安全域名: 跳过 DNS 解析
    if hostname in _SAFE_IMAGE_HOSTS | _SAFE_PROXY_HOSTS:
        return True
    # 检查是否是 IP 字面量 (无 DNS 解析)
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            logger.warning("ImageGen SSRF: 拒绝 IP 字面量 %s", hostname)
            return False
        return True
    except ValueError:
        pass  # 不是 IP, 需要 DNS 解析
    # DNS 解析 + 私有 IP 检查
    try:
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                addrs = socket.getaddrinfo(hostname, None, family)
            except socket.gaierror:
                continue
            for addr in addrs:
                resolved = addr[4][0]
                try:
                    ip = ipaddress.ip_address(resolved)
                except ValueError:
                    continue
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                    logger.warning(
                        "ImageGen SSRF: 拒绝 %s → %s (私有/保留 IP)", hostname, resolved,
                    )
                    return False
        return True
    except Exception:
        logger.warning("ImageGen SSRF: DNS 解析失败 %s", hostname)
        return False


# ── 图片提取 — 支持 base64 内联 + HTTP URL ───────────

_INLINE_IMG_RE = re.compile(r"data:image/\w+;base64,([A-Za-z0-9+/=]+)")
_MD_IMG_URL_RE = re.compile(r"!\[(?:image|图片)?\]\((https?://[^\)]+)\)")


def _extract_images(text: str) -> list[bytes]:
    """从文本中提取图片 — 优先内联 base64 (Gemini), 其次 HTTP URL (gpt-image-2)。"""
    results: list[bytes] = []
    # 1. 内联 base64
    for m in _INLINE_IMG_RE.finditer(text):
        try:
            results.append(base64.b64decode(m.group(1)))
        except Exception:
            logger.warning("ImageGen: 跳过无效 base64")
    # 2. HTTP URL (gpt-image-2 返回 URL 而非 base64)
    if not results:
        for m in _MD_IMG_URL_RE.finditer(text):
            url = m.group(1)
            if not _is_safe_image_url(url):
                logger.warning("ImageGen SSRF: 拒绝不安全 URL %s", url[:80])
                continue
            try:
                import requests as _rq
                resp = _rq.get(url, timeout=30, allow_redirects=False)
                if resp.status_code == 200:
                    results.append(resp.content)
                    logger.info("ImageGen: 下载 %s → %d bytes", url[:60], len(resp.content))
                elif resp.status_code in (301, 302, 303, 307, 308):
                    logger.warning("ImageGen: 拒绝重定向 %s → %s", url[:80], resp.headers.get("Location", "")[:80])
            except Exception:
                logger.warning("ImageGen: 下载失败 %s", url[:80])
    return results


# ── 客户端 ───────────────────────────────────────────────


@dataclass
class ImageGenClient:
    """双后端图像生成客户端。

    用法:
        client = ImageGenClient(api_key="sk-xxx", model="gemini-3.1-flash-image")
        results = await client.generate("一只猫")
        for r in results:
            save(r.bytes_data, "cat.png")
    """

    api_key: str
    base_url: str = "https://api.vectorengine.ai"
    model: str = "gpt-image-2"
    default_size: str = "1024x1536"
    default_quality: str = "medium"
    default_format: str = "png"
    timeout: int = 300

    # ── 统一入口: 全部走 chat/completions ──
    # vectorengine 代理不支持 /v1/images/generations,
    # gpt-image-2 返回 HTTP URL, gemini-* 返回内联 base64

    async def generate(
        self, prompt: str, size: str = "", n: int = 1, quality: str = "",  # noqa: ARG002
    ) -> list[ImageResult]:
        return await self._gen_chat(prompt, size or self.default_size, n)

    async def edit(
        self, prompt: str, image_url: str = "", mask_url: str = "",  # noqa: ARG002
        image_data: bytes | None = None, size: str = "", n: int = 1,
    ) -> list[ImageResult]:
        return await self._edit_chat(prompt, image_url, image_data, size or self.default_size, n)

    # ═══════════════════════════════════════════════════════
    # Chat Completions 多模态输出 (全模型统一)
    # ═══════════════════════════════════════════════════════

    def _size_to_aspect(self, size: str) -> str:
        """尺寸 → Gemini aspect_ratio (近似映射)。"""
        w, _, h = size.partition("x")
        try:
            ratio = int(w) / int(h)
        except (ValueError, ZeroDivisionError):
            return "3:4"
        if ratio > 1.4:
            return "16:9"
        if ratio > 1.1:
            return "4:3"
        if ratio > 0.8:
            return "1:1"
        if ratio > 0.6:
            return "3:4"
        return "9:16"

    async def _gen_chat(self, prompt: str, size: str, n: int) -> list[ImageResult]:
        logger.info("🎨 ImageGen [Chat] | model=%s size=%s prompt_len=%d", self.model, size, len(prompt))
        aspect = self._size_to_aspect(size)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }],
            "max_tokens": 4096,
            "image_config": {"aspect_ratio": aspect},
        }
        # response_modalities 让模型输出图片
        payload["response_modalities"] = ["IMAGE", "TEXT"]

        body = await self._post_json("/v1/chat/completions", payload)
        return self._parse_chat_images(body, n)

    async def _edit_chat(
        self, prompt: str, image_url: str,
        image_data: bytes | None, size: str, n: int,
    ) -> list[ImageResult]:
        logger.info(
            "🎨 ImageGen [Chat] edit | model=%s prompt_len=%d has_data=%s has_url=%s",
            self.model, len(prompt), bool(image_data), bool(image_url),
        )
        aspect = self._size_to_aspect(size)

        # 构建多模态消息: 参考图 + 编辑指令
        content: list[dict] = []
        if image_data:
            b64 = base64.b64encode(image_data).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        elif image_url:
            content.append({
                "type": "image_url",
                "image_url": {"url": image_url},
            })
        content.append({"type": "text", "text": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 4096,
            "image_config": {"aspect_ratio": aspect},
            "response_modalities": ["IMAGE", "TEXT"],
        }

        body = await self._post_json("/v1/chat/completions", payload)
        return self._parse_chat_images(body, n)

    def _parse_chat_images(self, body: dict, _n: int = 0) -> list[ImageResult]:
        """从 chat completions 响应中提取图片。

        Gemini 在 content 中返回 markdown 格式的内联图片:
        ![image](data:image/png;base64,iVBOR...)
        """
        choices = body.get("choices", [])
        if not choices:
            return []
        content_text = choices[0].get("message", {}).get("content", "")
        if not content_text:
            return []

        images = _extract_images(content_text)
        results = [ImageResult(bytes_data=img_bytes) for img_bytes in images]
        logger.info("🎨 ImageGen [Chat] 解析完成 | %d 张图", len(results))
        return results

    # ═══════════════════════════════════════════════════════
    # HTTP 工具
    # ═══════════════════════════════════════════════════════

    def _url(self, path: str) -> str:
        base = self.base_url.rstrip("/")
        # 兼容旧配置: base_url 可能包含 /v1 后缀 (如 api.vectorengine.ai/v1)
        # path 始终以 /v1/ 开头 → 去重避免 /v1/v1/chat/completions
        if path.startswith("/v1/") and base.endswith("/v1"):
            base = base[:-3]
        return f"{base}{path}"

    def _auth_headers(self, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self.api_key}"}
        if extra:
            h.update(extra)
        return h

    async def _post_json(self, path: str, payload: dict) -> dict:
        headers = self._auth_headers({"Content-Type": "application/json"})
        timeout_obj = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout_obj, trust_env=False) as s,
                s.post(self._url(path), json=payload, headers=headers) as resp,
            ):
                body = await resp.json()
                self._raise_for_status(resp.status, body)
        except aiohttp.ClientError as e:
            raise ImageGenError(f"网络错误: {e}") from e
        except builtins.TimeoutError as e:
            raise TimeoutError(f"{path} 请求超时 ({self.timeout}s)", status=408) from e
        return body

    def _raise_for_status(self, status: int, body: dict) -> None:
        if 200 <= status < 300:
            return
        msg = body.get("error", {}).get("message", str(body)) if isinstance(body, dict) else str(body)
        msg_str = str(msg)[:300]
        logger.error(
            "🎨 ImageGen API 返回 %s: %s (full body keys: %s)",
            status, msg_str, list(body.keys()) if isinstance(body, dict) else "N/A",
        )
        if status in (401, 403):
            raise AuthError(msg_str, status=status)
        if status == 429:
            raise RateLimitError(msg_str, status=status)
        if status == 400 and any(kw in msg_str.lower() for kw in ("content", "moderation", "safety")):
            raise ContentModerationError(msg_str, status=status)
        raise ImageGenError(msg_str, status=status)


# 顶层导出 — 兼容 tools.py 的 from ...astrbot_plugin_suli_draw.draw_client import DrawClient 用法
DrawClient = ImageGenClient
