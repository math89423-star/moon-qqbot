"""Cache Optimizer — LLM 上下文压缩 + 缓存命中优化。

从 token_controller/main.py 提取核心逻辑。

两个能力:
  1. ContextCompressor — 按 token 配额压缩上下文窗口 (智能裁剪旧历史)
  2. CacheOptimizer   — GPT/DeepSeek 缓存命中率优化 (稳定前缀/缓存键)

用法:
  from astrbot_plugin_suli_services.cache_optimizer import (
      ContextCompressor, CacheOptimizer, CacheAnchor,
  )

  # 上下文压缩
  limit = ContextCompressor.calc_context_limit(daily_token_limit=1_000_000)
  trimmed = ContextCompressor.trim_history(history, budget=limit)

  # 缓存优化
  anchor = CacheOptimizer.deepseek_anchor()
  extra_body = CacheOptimizer.gpt_cache_extra(group_id, provider_id, model)
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════
# 常量 (对齐 token_controller)
# ═════════════════════════════════════════════════════════════════

# 上下文窗口 = 每日配额的 0.5%
_CONTEXT_RATIO = 0.005
# 裁剪比例 — 保留 80% 的窗口大小
_TRIM_RATIO = 0.8
# 压缩阈值 — 当输入超过窗口 82% 时触发裁剪
_COMPRESS_THRESHOLD = 0.82
# 至少保留的最近轮数
_FALLBACK_TURNS = 3

# 稳定缓存锚点版本 (修改锚点内容时递增)
_ANCHOR_VERSION = "moon-v1"

# 支持 prompt_cache_retention 的 GPT 模型前缀
_GPT_CACHE_RETENTION_MODELS = (
    "gpt-4o", "gpt-4.1", "gpt-4.5",
    "gpt-5", "o3", "o4",
)


# ═════════════════════════════════════════════════════════════════
# ContextCompressor — 上下文窗口压缩
# ═════════════════════════════════════════════════════════════════

class ContextCompressor:
    """按 token 配额智能裁剪对话历史。

    核心逻辑:
      - 上下文窗口大小 = daily_token_limit x 0.5%
      - 当输入超过窗口 82% 时触发裁剪
      - 裁剪后保留 80% 窗口大小
      - 至少保留最近 3 轮对话
    """

    @staticmethod
    def calc_context_limit(daily_token_limit: int) -> int:
        """计算上下文窗口大小 (token 数)。

        Args:
            daily_token_limit: 每日 token 配额

        Returns:
            上下文窗口 token 上限 (最少 1)
        """
        if daily_token_limit <= 0:
            return 0
        return max(1, int(daily_token_limit * _CONTEXT_RATIO))

    @staticmethod
    def trim_budget(limit_tokens: int) -> int:
        """计算裁剪后的目标大小 (保留 80% 窗口)。"""
        return max(1, int(max(1, limit_tokens) * _TRIM_RATIO))

    @staticmethod
    def compress_threshold(limit_tokens: int) -> int:
        """计算触发压缩的阈值 (窗口的 82%)。"""
        return max(1, int(max(1, limit_tokens) * _COMPRESS_THRESHOLD))

    @staticmethod
    def expand_for_tokens(limit_tokens: int) -> int:
        """当输入必须内容已超阈值时，计算扩展后的窗口大小。

        避免过度压缩导致工具提示/系统提示被裁剪。
        """
        return max(
            1,
            math.ceil(max(1, limit_tokens) / _COMPRESS_THRESHOLD) + 128,
        )

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """快速估算文本 token 数 (保守估计: 1 字符 ≈ 0.5 token)。"""
        if not text:
            return 0
        # 中文约 1 char/token, 英文约 4 chars/token, 取保守平均
        return max(1, math.ceil(len(text) * 0.6))

    @classmethod
    def trim_history(
        cls,
        history: list[dict],
        budget: int,
        *,
        min_turns: int = _FALLBACK_TURNS,
    ) -> list[dict]:
        """裁剪对话历史以适应 token 预算。

        策略: 从最早的消息开始裁剪 (保留最近 N 轮)。

        Args:
            history: 对话历史 [{"role": "...", "content": "..."}, ...]
            budget: token 预算上限
            min_turns: 至少保留的最近轮数

        Returns:
            裁剪后的历史 (不修改原列表)
        """
        if budget <= 0 or not history:
            return list(history)

        # 估算总 token
        total = sum(cls.estimate_tokens(str(m.get("content", ""))) for m in history)
        if total <= budget:
            return list(history)

        # 从尾部保留，最少保留 min_turns 组 user+assistant 对
        # 一组 = user + assistant (2 条)
        min_messages = min_turns * 2
        kept = list(history)

        while len(kept) > min_messages:
            kept = kept[1:]  # 移除最早的消息
            new_total = sum(
                cls.estimate_tokens(str(m.get("content", ""))) for m in kept
            )
            if new_total <= budget:
                break

        return kept


# ═════════════════════════════════════════════════════════════════
# CacheOptimizer — GPT / DeepSeek 缓存命中优化
# ═════════════════════════════════════════════════════════════════

class CacheOptimizer:
    """LLM 缓存命中率优化。

    两个策略:
      GPT:      注入 prompt_cache_key + prompt_cache_retention
      DeepSeek: 在 system_prompt 开头注入稳定缓存锚点
    """

    @staticmethod
    def deepseek_anchor() -> str:
        """生成 DeepSeek 稳定缓存锚点。

        DeepSeek 默认开启上下文缓存 — 后续请求复用相同前缀时，
        API 会自动命中缓存并在 usage 中返回 prompt_cache_hit_tokens。
        这个锚点放在 system_prompt 最前面，确保每次请求的第一段文本相同。
        """
        lines = [
            "<moon_cache_anchor>",
            f"version={_ANCHOR_VERSION}",
            "purpose=stable_prefix_for_prompt_cache",
        ]
        lines.extend(
            f"anchor_{i:03d}=stable_cache_no_instruction" for i in range(5)
        )
        lines.append("</moon_cache_anchor>")
        return "\n".join(lines)

    @staticmethod
    def gpt_cache_key(
        group_id: str,
        provider_id: str,
        model: str,
    ) -> str:
        """生成 GPT prompt_cache_key。

        OpenAI Prompt Caching 要求: 相同 cache_key + 相同前缀 = 缓存命中。
        使用 SHA256 确保不同群/不同模型之间缓存隔离。
        """
        raw = f"{group_id}|{provider_id}|{model}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return f"lp-{digest}"

    @classmethod
    def gpt_cache_extra_body(
        cls,
        group_id: str,
        provider_id: str,
        model: str,
    ) -> dict[str, Any]:
        """生成 GPT custom_extra_body (prompt_cache_key + retention)。

        Args:
            group_id: 群 ID
            provider_id: AstrBot provider ID
            model: 模型名
        """
        cache_key = cls.gpt_cache_key(group_id, provider_id, model)
        extra: dict[str, Any] = {"prompt_cache_key": cache_key}
        if cls._supports_retention(model):
            extra["prompt_cache_retention"] = "24h"
        return extra

    @classmethod
    def is_deepseek_model(cls, model: str) -> bool:
        """判断是否为 DeepSeek 模型。"""
        lower = model.lower()
        return "deepseek" in lower

    @classmethod
    def is_gpt_model(cls, model: str) -> bool:
        """判断是否为 GPT 模型 (支持 prompt_cache_key)。"""
        lower = model.lower()
        return lower.startswith("gpt-") or (lower.startswith("o") and any(
            c.isdigit() for c in lower
        ))

    @classmethod
    def _supports_retention(cls, model: str) -> bool:
        """判断模型是否支持 prompt_cache_retention=24h。"""
        lower = model.lower()
        return any(lower.startswith(prefix) for prefix in _GPT_CACHE_RETENTION_MODELS)

    @classmethod
    def apply_deepseek_anchor(
        cls,
        system_prompt: str,
        *,
        enabled: bool = True,
    ) -> str:
        """在 system_prompt 开头注入 DeepSeek 缓存锚点。

        Args:
            system_prompt: 原始 system prompt
            enabled: 是否启用 (关闭时返回原样)
        """
        if not enabled:
            return system_prompt
        anchor = cls.deepseek_anchor()
        # 避免重复注入
        if "<moon_cache_anchor>" in system_prompt:
            return system_prompt
        return f"{anchor}\n\n{system_prompt}"

    @classmethod
    def apply_gpt_cache(
        cls,
        custom_extra_body: dict[str, Any] | None,
        group_id: str,
        provider_id: str,
        model: str,
        *,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """合并 GPT prompt_cache_key 到 custom_extra_body。

        Args:
            custom_extra_body: 现有的 extra_body (可能为 None)
            group_id: 群 ID
            provider_id: provider ID
            model: 模型名
            enabled: 是否启用
        """
        result = dict(custom_extra_body or {})
        if not enabled:
            return result
        cache_extra = cls.gpt_cache_extra_body(group_id, provider_id, model)
        result.update(cache_extra)
        return result

    @classmethod
    def apply(
        cls,
        system_prompt: str,
        custom_extra_body: dict[str, Any] | None,
        model: str,
        group_id: str = "",
        provider_id: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """一次调用同时应用 DeepSeek/GPT 缓存策略。

        自动识别模型类型:
          - DeepSeek → 注入稳定前缀到 system_prompt
          - GPT      → 注入 prompt_cache_key 到 extra_body
          - 其他     → 不做任何处理

        Returns:
            (modified_system_prompt, modified_extra_body)
        """
        extra = dict(custom_extra_body or {})

        if cls.is_deepseek_model(model):
            system_prompt = cls.apply_deepseek_anchor(system_prompt, enabled=True)
        elif cls.is_gpt_model(model):
            extra = cls.apply_gpt_cache(
                extra, group_id, provider_id, model, enabled=True,
            )

        return system_prompt, extra
