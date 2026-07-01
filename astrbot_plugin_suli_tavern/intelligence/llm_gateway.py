"""统一 LLM 调用关口 — 预算熔断 + Token 追踪 + 审计日志 + 成本加权。

在调用前后提供:
  1. 预算检查 (pre-check): 统一入口，所有 LLM/VLM 调用必经
  2. Token 记录 (post-record): 统一 DB 写入 + 审计日志
  3. 成本核算: 按实际费用权重而非裸 token 数
  4. LLM_CALL 审计日志: 每次 LLM 调用一行 grep-able 日志

用法:
  from .llm_gateway import LLMGateway

  # Pre-call (estimated_tokens 用于准确的预算投影)
  budget_ok = LLMGateway.pre_check(
      bot_id, purpose="chat", model="gpt-5.5", provider="openai",
      estimated_tokens=3000,
  )
  if budget_ok == "hard_capped":
      return  # block
  if budget_ok == "soft_capped":
      # 预算紧张，跳过昂贵调用，只用廉价模型
      pass

  # ... LLM call ...

  # Post-call
  LLMGateway.record(
      bot_id=bot_id, model="gpt-5.5", provider="openai",
      input_tokens=1234, output_tokens=567,
      cache_hit_tokens=100, cache_miss_tokens=200,
      latency_ms=850, purpose="group_reply",
      group_id="711600211", user_id="123456",
  )
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# ── 预算缓存 (模块级, 5min TTL) ──────────────────────

_budget_cache: dict[str, dict] = {}  # {bot_id: {"ts": float, "result": str}}


class LLMGateway:
    """统一 LLM 调用关口。

    纯静态方法，无实例状态。所有状态在 DB 或模块级缓存中。
    """

    # ═══════════════════════════════════════════════════════════
    # B2: 模型成本权重 — 按实际 API 定价比例
    # 权重 1.0 = deepseek-v4-flash (基准)
    # ═══════════════════════════════════════════════════════════

    MODEL_COST_WEIGHTS: dict[str, float] = {
        # deepseek 系列 (极便宜)
        "deepseek-v4-flash": 1.0,
        "deepseek-v4-pro": 2.0,
        "deepseek-chat": 1.0,
        "deepseek-reasoner": 3.0,
        # GPT 系列 (贵，需成本加权)
        "gpt-5.5": 15.0,
        "gpt-5.4": 15.0,
        "gpt-5.4-mini": 8.0,
        "gpt-image-2": 20.0,
        "gpt-4o": 20.0,
        "gpt-4o-mini": 5.0,
        # Gemini 系列
        "gemini-3.1-flash-image": 4.0,
        "gemini-3.1-flash-lite-preview": 1.5,
        # Opus 系列 (最贵)
        "opus-4-8": 25.0,
        "claude-opus-4-8": 25.0,
    }

    @staticmethod
    def _cost_weight(model: str, provider: str = "") -> float:
        """返回模型相对于 deepseek-flash 的成本权重。

        权重 1.0 = deepseek-flash。GPT=15x, Opus=25x。
        未识别的模型默认权重 5.0 (保守偏高)。
        """
        if model in LLMGateway.MODEL_COST_WEIGHTS:
            return LLMGateway.MODEL_COST_WEIGHTS[model]
        # 按 provider 推断
        p = provider.lower()
        if "openai" in p or "gpt" in p:
            return 10.0
        if "claude" in p or "opus" in p or "anthropic" in p:
            return 25.0
        if "gemini" in p:
            return 3.0
        if "deepseek" in p:
            return 1.0
        return 5.0  # 未知 provider → 保守偏高

    @staticmethod
    def _weighted_tokens(input_tokens: int, output_tokens: int,
                         model: str = "", provider: str = "") -> float:
        """将裸 token 数换算为成本加权 token。

        GPT 1 token ≈ deepseek 15 token (按成本)。
        """
        w = LLMGateway._cost_weight(model, provider)
        return (input_tokens + output_tokens) * w

    # ═══════════════════════════════════════════════════════════
    # B1: 统一 Pre-check — 所有调用必经
    @staticmethod
    def pre_check(bot_id: str, *, purpose: str = "chat",
                  model: str = "", provider: str = "",
                  estimated_tokens: int = 3000) -> str:
        """调用前预算检查 — 所有 LLM/VLM 调用必须调用此方法。

        Args:
            bot_id: QQ 号
            purpose: 调用目的标识 (group_reply / auto_vlm / profile / memory / ...)
            model: 模型名 (用于成本加权 + per-model cap 检查)
            provider: provider 名
            estimated_tokens: 本次调用预估消耗 token 数 (默认 3000)

        Returns:
            "ok" — 预算充足
            "soft_capped" — 预算接近上限，跳过昂贵调用 (Opus/ReAct/GPT)
            "hard_capped" — 预算已耗尽，完全阻止 LLM 调用
        """
        if not bot_id:
            return "ok"

        now = time.time()
        # 缓存键含 bot + purpose, 不含 estimated_tokens (同 purpose 用相同估算)
        cache_key = f"{bot_id}:{purpose}"
        entry = _budget_cache.get(cache_key)
        if entry and (now - entry["ts"]) < 300:
            return entry["result"]

        try:
            from ..service.bot_db import get_bot_db

            db = get_bot_db()
            budget_cfg = db.get_token_budget_config()
            stats = db.get_token_stats(period="today")

            try:
                from ..service.bot_identity import get_bot_identity_service
                _svc_b = get_bot_identity_service()
                _bot = _svc_b.get_bot(str(bot_id))
                bot_key = _bot.character_card.lower() if _bot else "moon"
            except Exception:
                bot_key = "moon"
            hard_limit = int(budget_cfg.get(f"{bot_key}_hard_limit", 3_000_000))
            soft_limit = int(budget_cfg.get(f"{bot_key}_soft_limit", 2_400_000))

            # ── 修复 Bug 0: by_bot 是 list，不是 dict ──
            raw_input = 0
            raw_output = 0
            by_bot = stats.get("by_bot", [])
            if isinstance(by_bot, list):
                for entry_b in by_bot:
                    if isinstance(entry_b, dict) and str(entry_b.get("bot_id", "")) == str(bot_id):
                        raw_input = int(entry_b.get("input_tokens", 0) or 0)
                        raw_output = int(entry_b.get("output_tokens", 0) or 0)
                        break

            raw_used = raw_input + raw_output
            raw_projected = raw_used + estimated_tokens

            # ── Per-model 日配额检查 (优先级最高) ──
            model_cap_result = LLMGateway.check_model_cap(
                bot_id, model, provider, estimated_tokens,
            )

            # ── Hard cap: 裸 token 达到 hard_limit → 完全阻止 ──
            if model_cap_result == "hard_capped" or raw_projected >= hard_limit:
                result = "hard_capped"
            # ── Soft cap: 裸 token 达到 soft_limit → 仅允许廉价模型 ──
            elif raw_projected >= soft_limit or model_cap_result == "soft_capped":
                this_weight = LLMGateway._cost_weight(model, provider)
                if this_weight > 2.0:
                    logger.warning(
                        "LLMGateway: soft_capped — used=%d/%d model=%s weight=%.1f → block",
                        raw_used, soft_limit, model, this_weight,
                    )
                    result = "soft_capped"
                else:
                    logger.debug(
                        "LLMGateway: soft_capped but model=%s weight=%.1f ≤ 2.0 → allow",
                        model, this_weight,
                    )
                    result = "ok"
            else:
                result = "ok"

            logger.debug(
                "LLMGateway: pre_check bot=%s purpose=%s model=%s "
                "used=%d/%d est=%d model_cap=%s → %s",
                bot_id[:8], purpose, model,
                raw_used, hard_limit, estimated_tokens, model_cap_result, result,
            )
        except Exception:
            logger.warning(
                "LLMGateway: 预算检查失败, fail-closed → hard_capped (bot=%s)",
                bot_id[:8], exc_info=True,
            )
            result = "hard_capped"

        _budget_cache[cache_key] = {"ts": now, "result": result}
        return result

    # ═══════════════════════════════════════════════════════════
    # B3: Per-model token_budget_cap 检查
    @staticmethod
    def check_model_cap(bot_id: str, model: str, provider: str = "",
                        estimated_tokens: int = 0) -> str:
        """检查 per-model 的 token_budget_cap (llm_config 表的字段)。

        此前 token_budget_cap 字段存在但从不被读取 (侦察报告 §9)。
        现在接入统一关口。

        Returns:
            "ok" / "soft_capped" / "hard_capped"
        """
        _ = provider  # 保留接口兼容，当前仅按 model 匹配
        if not bot_id or not model:
            return "ok"

        try:
            from ..service.bot_db import get_bot_db
            db = get_bot_db()
            configs = db.list_llm_configs()
            model_cap = None
            for cfg in configs:
                if cfg.model_name == model or cfg.provider_name == model:
                    cap_val = getattr(cfg, "token_budget_cap", None)
                    if cap_val is not None:
                        model_cap = int(cap_val)
                        break

            if model_cap is None:
                return "ok"  # 无 per-model 限制

            # ── 修复: 直接查 DB 获取 per-model 今日消耗 ──
            # get_token_stats() 不返回 by_model，需独立查询
            import time as _time
            since = _time.time() - 86400
            row = db.conn.execute(
                "SELECT "
                "  COALESCE(SUM(input_tokens), 0) AS input_tokens, "
                "  COALESCE(SUM(output_tokens), 0) AS output_tokens "
                "FROM token_usage "
                "WHERE timestamp >= ? AND model = ? AND bot_id = ?",
                (since, model, bot_id),
            ).fetchone()

            used = 0
            if row:
                used = (int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0))

            if used + estimated_tokens >= model_cap:
                logger.warning(
                    "LLMGateway: model_cap reached model=%s used=%d cap=%d bot=%s",
                    model, used, model_cap, bot_id[:8],
                )
                return "hard_capped"
            if used + estimated_tokens >= model_cap * 0.8:
                return "soft_capped"
            return "ok"
        except Exception:
            logger.debug("LLMGateway: model_cap 检查失败, fallthrough", exc_info=True)
            return "ok"

    # ═══════════════════════════════════════════════════════════
    # Post-call: Token 记录 + 审计日志
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def record(
        bot_id: str,
        *,
        model: str = "",
        provider: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        latency_ms: int = 0,
        purpose: str = "",
        group_id: str = "",
        user_id: str = "",
        success: bool = True,
        error: str = "",
    ) -> None:
        """记录一次 LLM/VLM 调用: DB 写入 + 审计日志。

        应在每次 LLM/VLM API 调用完成后调用。
        即使调用失败也应记录 (success=False)。
        """
        total_tokens = input_tokens + output_tokens
        cost_weight = LLMGateway._cost_weight(model, provider)
        weighted = int(total_tokens * cost_weight)

        # ── DB 写入 ──
        try:
            from ..service.bot_db import get_bot_db
            db = get_bot_db()
            db.record_token_usage(
                scenario=purpose or "unknown",
                user_id=user_id or "",
                group_id=group_id or "",
                model=model or "",
                provider=provider or "?",
                bot_id=bot_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_hit_tokens=cache_hit_tokens,
                cache_miss_tokens=cache_miss_tokens,
                latency_ms=latency_ms,
            )
        except Exception:
            logger.debug("LLMGateway: DB 写入失败", exc_info=True)

        # ── 审计日志 (含成本权重) ──
        LLMGateway._log_llm_call(
            bot_id=bot_id,
            model=model,
            provider=provider,
            total_tokens=total_tokens,
            cost_weight=cost_weight,
            weighted_tokens=weighted,
            latency_ms=latency_ms,
            purpose=purpose,
            group_id=group_id,
            success=success,
            error=error,
        )

        # ── 使预算缓存失效 (token 计数变化了) ──
        # 清除所有该 bot 的缓存键
        keys_to_del = [k for k in _budget_cache if k.startswith(f"{bot_id}:")]
        for k in keys_to_del:
            _budget_cache.pop(k, None)

    @staticmethod
    def _log_llm_call(
        bot_id: str,
        model: str,
        provider: str,
        total_tokens: int,
        cost_weight: float,
        weighted_tokens: int,
        latency_ms: int,
        purpose: str,
        group_id: str,
        success: bool,
        error: str,
    ) -> None:
        """统一 LLM_CALL 审计日志格式。

        每次 LLM API 调用一行，grep "LLM_CALL" 即可审计所有模型调用。
        """
        _status = "OK" if success else f"ERR={error[:60]}"
        _parts = [
            f"bot={bot_id[:8]}",
            f"purpose={purpose}",
            f"model={model}",
            f"provider={provider}",
            f"tokens={total_tokens}",
            f"weight={cost_weight:.1f}",
            f"w_tokens={weighted_tokens}",
            f"latency={latency_ms}ms",
        ]
        if group_id:
            _parts.append(f"group={group_id}")
        _parts.append(_status)
        logger.info("LLM_CALL %s", " ".join(_parts))

    # ═══════════════════════════════════════════════════════════
    # 辅助: 从 TavernClient 读取最近 usage
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def record_from_tavern(
        tavern_client,  # TavernClient
        bot_id: str,
        *,
        model: str = "",
        provider: str = "",
        purpose: str = "",
        group_id: str = "",
        user_id: str = "",
        success: bool = True,
    ) -> None:
        """从 TavernClient 读取最近一次 token usage 并记录。

        兼容现有 _record_usage() 调用模式。
        """
        try:
            usage = tavern_client.get_last_usage()
            if not usage.get("input_tokens") and not usage.get("output_tokens"):
                return  # 无 usage 数据
        except Exception:
            return

        LLMGateway.record(
            bot_id=bot_id,
            model=model,
            provider=provider,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_hit_tokens=usage.get("cache_hit_tokens", 0),
            cache_miss_tokens=usage.get("cache_miss_tokens", 0),
            latency_ms=usage.get("latency_ms", 0),
            purpose=purpose,
            group_id=group_id,
            user_id=user_id,
            success=success,
        )

    # ═══════════════════════════════════════════════════════════
    # 已被移除: check_budget() (向后兼容别名)
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def check_budget(bot_id: str, *, purpose: str = "chat") -> str:
        """[已弃用] 请使用 pre_check()。保留为向后兼容。"""
        return LLMGateway.pre_check(bot_id, purpose=purpose)
