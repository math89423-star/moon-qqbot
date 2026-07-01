"""Prompt 缓存策略 — provider 感知的消息包装。

背景:
  DeepSeek 用自动前缀缓存（硬盘 KV cache），不需要显式标记。
  Anthropic Claude 需要显式 cache_control: { type: 'ephemeral' } 标记。
  OpenAI 对特定模型有自动前缀缓存。
  本地模型 (llama/ollama) 无缓存收益，跳过。

设计原则:
  - 所有 provider 都受益于「静态内容在前」的消息结构
  - 对 Anthropic 额外添加 cache_control 标记（兼容 SillyTavern 透传）
  - 对本地模型不做任何包装
  - 调用方无需关心底层差异 — 只需传 messages + provider → 得到优化后的 messages

用法:
  from .prompt_cache import optimize_messages

  messages = [{"role": "system", "content": "..."}, ...]
  messages = optimize_messages(messages, provider="claude")
"""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

# ── Provider → 策略映射 ────────────────────────────────────

# 需要显式 cache_control 标记的 provider
_CACHE_CONTROL_PROVIDERS = frozenset({"claude", "anthropic"})

# 自动前缀缓存 provider (不需要显式标记，但静态前缀放前面仍然关键)
_AUTO_CACHE_PROVIDERS = frozenset({"deepseek", "custom", "openai", "gemini"})

# VLM provider — 通常不缓存（图片 token 难以做前缀匹配）
_VLM_PROVIDERS_LOWER = frozenset({"gpt4v", "claude", "gemini", "nano_banana"})

# 本地 provider — 无缓存
_LOCAL_PROVIDERS = frozenset({"llama", "ollama"})


def get_cache_strategy(provider: str) -> str:
    """根据 provider 返回缓存策略标识。

    Returns:
        "cache_control" — 需要显式 cache_control 标记 (Anthropic)
        "auto_prefix"   — 自动前缀缓存, 静态前缀放前面即可 (DeepSeek/OpenAI)
        "vlm"           — 视觉模型, 缓存收益有限
        "none"          — 无需缓存 (本地模型)
    """
    p = provider.lower()
    if p in _CACHE_CONTROL_PROVIDERS:
        return "cache_control"
    if p in _AUTO_CACHE_PROVIDERS:
        return "auto_prefix"
    if p in _VLM_PROVIDERS_LOWER:
        return "vlm"
    if p in _LOCAL_PROVIDERS:
        return "none"
    # 未知 provider: 保守处理，不做包装（避免破坏兼容性）
    return "none"


def optimize_messages(
    messages: list[dict],
    provider: str,
    *,
    min_cacheable_chars: int = 256,
    debug_group_id: str = "",
) -> list[dict]:
    """对消息列表应用 provider 感知的缓存优化。

    核心优化:
      1. 对所有 provider: 不做重排（调用方负责静态前置）
      2. 对 Anthropic: 包装第一个 system message 为 content-parts 格式 +
         cache_control 标记
      3. 对 auto_prefix provider: 不做额外包装（自动缓存已处理前缀）
      4. 对本地/none: 原样返回

    注意: 此函数不改变消息顺序或内容——只添加缓存标记。
          调用方需要自己确保静态内容在消息列表最前面。

    Args:
        messages: 原始消息列表
        provider: LLM provider 标识
        min_cacheable_chars: 最少字符数才加 cache_control（太短无意义）
        debug_group_id: 非空时记录前缀哈希 (用于缓存稳定性诊断)

    Returns:
        优化后的消息列表（可能是原地修改的同一列表）
    """
    strategy = get_cache_strategy(provider)

    if strategy == "cache_control":
        return _apply_cache_control(messages, min_cacheable_chars)
    if strategy == "auto_prefix":
        return _ensure_static_prefix(messages, debug_group_id=debug_group_id)
    # vlm / none / unknown: 不做包装
    return messages


def _apply_cache_control(
    messages: list[dict],
    min_chars: int,
) -> list[dict]:
    """对 Anthropic 兼容 API: 包装第一个长 system message。

    Anthropic 格式: 将 content 从纯文本改为 content-parts 数组，
    其中前缀内容带 cache_control 标记。

    兼容 SillyTavern 透传: SillyTavern 1.12+ 的 Anthropic adapter
    会识别 content-parts 格式的 cache_control 并正确转换。
    """
    if not messages:
        return messages

    # 只标记第一条 system message（它是缓存收益最大的）
    for i, msg in enumerate(messages):
        if msg.get("role") != "system":
            continue

        content = msg.get("content", "")
        if not isinstance(content, str) or len(content) < min_chars:
            continue

        # 将纯文本 content 包装为 Anthropic content-parts 格式
        # 注意: 这种格式在 SillyTavern 透传时被正确识别
        msg["content"] = [
            {
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        break  # 只标记第一条

    return messages


def _ensure_static_prefix(
    messages: list[dict],
    *,
    debug_group_id: str = "",
) -> list[dict]:
    """对自动前缀缓存 provider: 验证静态内容在前 + 稳定性诊断。

    DeepSeek/OpenAI 的自动磁盘前缀缓存依赖每次请求的前缀字节完全一致。
    此函数不做消息重排（调用方负责），但记录前缀哈希用于稳定性验证。

    当 debug_group_id 非空时，记录第一条 system 消息的前缀哈希。
    相同 group/character 的连续请求应产生相同哈希——不同则说明前缀被污染。
    """
    if not messages:
        return messages

    # 提取第一条 system 消息的前 512 字符作为缓存锚点指纹
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) >= 64:
            prefix = content[:512]
            h = hashlib.sha256(prefix.encode()).hexdigest()[:16]
            if debug_group_id:
                logger.debug(
                    "PrefixCache fingerprint: group=%s hash=%s prefix_len=%d",
                    debug_group_id, h, len(content),
                )
        break

    return messages


# ── 缓存策略诊断 ──────────────────────────────────────────────

def describe_cache_info(provider: str) -> dict:
    """返回 provider 的缓存能力描述（供管理面板展示）。"""
    strategy = get_cache_strategy(provider)

    info = {
        "provider": provider,
        "strategy": strategy,
        "needs_markers": strategy == "cache_control",
        "auto_caches": strategy in ("cache_control", "auto_prefix"),
        "savings_rate": 0.0,
        "note": "",
    }

    if strategy == "cache_control":
        info["savings_rate"] = 0.90  # Anthropic: 缓存命中省 90%
        info["note"] = "需要 cache_control 标记 (已自动添加); 缓存命中省 90%"
    elif strategy == "auto_prefix":
        info["savings_rate"] = 0.90  # DeepSeek: 缓存命中省 90%
        info["note"] = "自动磁盘前缀缓存; 保持静态前缀在前即可; 缓存命中省 90%"
    elif strategy == "vlm":
        info["savings_rate"] = 0.0
        info["note"] = "视觉模型: 图片 token 难以缓存, 但文本前缀仍可能受益"
    elif strategy == "none":
        info["savings_rate"] = 0.0
        info["note"] = "本地模型或未知 provider: 无缓存收益"

    return info
