"""自适应模型路由 — Lite / Pro 二级切换。

根据上下文信号自动选择模型 tier:
  - LITE  (deepseek-v4-pro): 日常闲聊、水群、简单附和 (~85%)
  - PRO   (deepseek-v4-pro): 技术问答、推理需求、私聊深度对话 (~15%)

升级信号:
  lite → pro: 技术领域激活 / 用户要求推理 / 私聊 / 工具调用 / 交叉验证纠错

用法:
  from astrbot_plugin_suli_routing import ModelRouter, ModelTier

  tier = ModelRouter.decide_tier(
      trigger_reason="batch",
      active_domains=ctx.active_domains,
      user_message=latest_user_msg,
      user_id=trigger_uid,
      admin_qq=cfg.super_admin_qq,
      challenge_verdict=...,
      tools_enabled=True,
  )
  route = ModelRouter.resolve(tier, default_provider="deepseek")
  # route.model → "deepseek-v4-pro", route.provider → "deepseek"
"""

from __future__ import annotations

import logging
import re

from .types import CredentialProvider, DomainAwareness, ModelRoute, ModelTier

logger = logging.getLogger(__name__)

# ── 问句检测正则 (用于 decide_tier 信号 E) ──────────────
_QUESTION_SIGNAL_RE = re.compile(
    r"[吗呢吧啊]？|[吗呢吧啊]\?|"
    r"？$|\?$|"
    r"^(什么|怎么|如何|为什么|为啥|哪[个些]|谁|多少|几点|"
    r"能不能|可以不|行不行|有没有|是不是|要不要|该不该|"
    r"帮我|帮我查|帮我找|帮我看|推荐|建议|介绍)"
)

# ── 默认模型名映射 ──────────────────────────────────────
# 可被 CredentialProvider.get_config_model() 覆盖
# JUDGE 需要切到对应 provider

_DEFAULT_TIER_MODELS: dict[ModelTier, str] = {
    ModelTier.LITE: "deepseek-v4-pro",
    ModelTier.PRO:   "deepseek-v4-pro",
}


# ── 可注入依赖 (消费方在启动时注入) ──────────────────────

_domain: DomainAwareness | None = None
_credentials: CredentialProvider | None = None


def init_domain_awareness(impl: DomainAwareness) -> None:
    """注入领域感知实现 (is_reasoning_needed + user_force_reasoning)。

    不注入时 domain 升级信号静默跳过 — FLASH 降级，不会崩溃。
    """
    global _domain
    _domain = impl


def init_credential_provider(impl: CredentialProvider) -> None:
    """注入凭证提供者 (bot_db + bot_config 桥接)。

    不注入时 resolve() 使用默认模型名，api_base/api_key 为空。
    """
    global _credentials
    _credentials = impl


class ModelRouter:
    """自适应模型路由器。

    纯静态方法，无状态，可安全地在 async 上下文中调用。
    """

    @staticmethod
    def decide_tier(
        *,
        trigger_reason: str = "",
        active_domains: dict[str, float] | None = None,
        user_message: str = "",
        user_id: str = "",
        admin_qq: int = 0,
        challenge_verdict: str | None = None,
        tools_enabled: bool = False,
        # ── Pre-flight 增强信号 ──
        context_complexity: float = 0.0,
        tool_chain_depth: int = 0,
        has_unresolved_images: bool = False,
        user_affinity_level: int = 0,
        active_domain_count: int = 0,
    ) -> ModelTier:
        """根据上下文信号决定使用哪个模型 tier。

        Args:
            trigger_reason: 触发原因 — "mention"|"nickname"|"reply"|"batch"
                            |"debounce"|"proactive"|"private"
            active_domains: 当前活跃领域分数 (群聊 ctx.active_domains)
            user_message: 触发用户的最新消息内容
            user_id: 触发用户 QQ 号
            admin_qq: 管理员 QQ 号
            challenge_verdict: 交叉验证结果 — "bot_wrong"|"bot_right"|"deadlock"|None
            tools_enabled: 本轮是否启用工具调用
            context_complexity: Pre-flight 上下文复杂度 (0.0-10.0)
            tool_chain_depth: 推荐工具数量
            has_unresolved_images: 是否有未解析的图片
            user_affinity_level: 触发用户好感等级
            active_domain_count: 活跃领域数

        Returns:
            ModelTier — 该用哪个等级的模型
        """

        _ = tools_enabled  # 预留给未来工具感知路由 (工具链深度已覆盖)

        _is_admin = bool(admin_qq and str(user_id) == str(admin_qq))
        tier: ModelTier = ModelTier.LITE  # 默认

        # ── deadlock → 至少用 PRO 纠错 ──────
        if challenge_verdict == "deadlock":
            logger.info("模型路由: deadlock 升级到 PRO")
            tier = ModelTier.PRO

        # ── 交叉验证 bot_wrong — 用 pro 纠正
        elif challenge_verdict == "bot_wrong":
            logger.info("模型路由: bot_wrong 升级到 PRO")
            tier = ModelTier.PRO

        # 技术话题激活 → 需要深度推理 (注入 domain 感知)
        elif active_domains and _domain is not None and _domain.is_reasoning_needed(active_domains):
            logger.debug("模型路由: 技术话题 → PRO")
            tier = ModelTier.PRO

        # 用户显式要求思考/分析 (注入 domain 感知)
        elif user_message and _domain is not None and _domain.user_force_reasoning(user_message):
            logger.debug("模型路由: 用户要求推理 → PRO")
            tier = ModelTier.PRO

        # ── 🆕 上下文复杂度驱动升级 ──────────────────────

        # 信号 A: 上下文复杂度 >= 4.0 — 需要更深的推理
        elif context_complexity >= 4.0:
            logger.info(
                "模型路由: 上下文复杂度 %.1f >= 4.0 → PRO",
                context_complexity,
            )
            tier = ModelTier.PRO

        # 信号 B: 有未解析图片 — 图片理解需要更强能力
        elif has_unresolved_images:
            logger.info("模型路由: 有待解析图片 → PRO")
            tier = ModelTier.PRO

        # 信号 C: 工具链深度 >= 2 — 多工具编排需要更好的规划
        elif tool_chain_depth >= 2:
            logger.info("模型路由: 工具链深度 %d >= 2 → PRO", tool_chain_depth)
            tier = ModelTier.PRO

        # 信号 D: 问句 + 中等复杂度 — 可能需要推理才能回答好
        elif (
            user_message
            and _QUESTION_SIGNAL_RE.search(user_message)
            and context_complexity >= 3.0
        ):
            logger.info(
                "模型路由: 含问句 + 复杂度 %.1f → PRO",
                context_complexity,
            )
            tier = ModelTier.PRO

        # 信号 E: 多话题并发 — 需要更好的上下文切换能力
        elif active_domain_count >= 2:
            logger.info(
                "模型路由: 多话题并发 (%d domains) → PRO",
                active_domain_count,
            )
            tier = ModelTier.PRO

        # ── Tier 1: LITE — 默认闲聊 ────────────────────
        # tier 已是 LITE

        # ═══════════════════════════════════════════════════════════
        # 后处理: 亲和力门控 (管理员豁免)
        # ═══════════════════════════════════════════════════════════

        # ── PRO 亲和力门控: 非管理员 + 好感度 < 3 → 强制降级 LITE ──
        #    管理员豁免此门控 — 管理员不受好感度限制，Gate 判 pro 即放行
        #    pro 永远由 Gate 判定，路由层不做任何人的自动升级
        if tier == ModelTier.PRO and not _is_admin and user_affinity_level < 3:
            logger.info(
                "模型路由: PRO 降级 LITE (好感度 Lv.%d < 3, 非管理员)",
                user_affinity_level,
            )
            tier = ModelTier.LITE

        return tier

    @staticmethod
    def resolve(
        tier: ModelTier,
        default_provider: str = "",
    ) -> ModelRoute:
        """将 tier 解析为具体的 model + provider + api_base/api_key。

        模型名优先级: CredentialProvider.get_config_model() → 代码默认值
        PRO 自动附加 reasoning_effort=max
        直连路径: 通过 CredentialProvider.find_llm_config() 解析 api_base/api_key

        Args:
            tier: 模型等级
            default_provider: 当前活跃 LLM 的 provider (如 "deepseek")

        Returns:
            ModelRoute — 包含 model, provider, extra_params, api_base, api_key
        """
        # 尝试从 CredentialProvider 读取自定义模型名
        model = _read_model_name(tier)

        # provider + reasoning_effort (THINKING tier 枚举未上线, 移除引用)
        if tier == ModelTier.PRO:
            provider = default_provider or "deepseek"
            extra = {"reasoning_effort": "max"}     # 技术/推理场景: 深度思考
        else:
            provider = default_provider or "deepseek"
            extra = {}                               # FLASH 闲聊: 不开推理, 省 token 提速

        # ── API 凭证解析: 通过注入的 CredentialProvider ──
        api_base = ""
        api_key = ""
        llm_cfg = _find_llm_config(model)
        if llm_cfg:
            api_base = llm_cfg.get("base_url", "")
            api_key = llm_cfg.get("api_key", "")
            logger.debug(
                "模型路由: %s → 直连 %s (%s)",
                model, api_base, llm_cfg.get("name", "?"),
            )
        elif _credentials is not None:
            # 回退: 使用活跃 LLM 配置 (CredentialProvider 注入)
            try:
                active = _credentials.resolve_active_llm()
                if active is not None:
                    api_base = getattr(active, "normalized_base_url", "")
                    api_key = getattr(active, "api_key", "")
                    logger.debug(
                        "模型路由: %s → 回退活跃配置 %s (%s)",
                        model, api_base, getattr(active, "name", "?"),
                    )
            except Exception:
                logger.debug("模型路由: %s → 无直连凭证 (活跃配置解析失败)", model)
        else:
            logger.debug("模型路由: %s → 无直连凭证 (未注入 CredentialProvider)", model)

        return ModelRoute(
            tier=tier, model=model, provider=provider,
            extra_params=extra, api_base=api_base, api_key=api_key,
        )


# ── 内部辅助 ──────────────────────────────────────────────


def _read_model_name(tier: ModelTier) -> str:
    """从 CredentialProvider 读取自定义模型名，fallback 到默认值。"""
    config_keys = {
        ModelTier.LITE: "model_router_lite",
        ModelTier.PRO:   "model_router_pro",
    }
    key = config_keys.get(tier, "")
    if key and _credentials is not None:
        try:
            val = _credentials.get_config_model(key, "")
            if val and isinstance(val, str) and val.strip():
                return val.strip()
        except Exception:
            pass
    return _DEFAULT_TIER_MODELS.get(tier, "deepseek-v4-pro")


def _find_llm_config(model_name: str) -> dict[str, str] | None:
    """从注入的 CredentialProvider 查找匹配的 LLM 配置。

    返回:
        {"name", "provider", "model_name", "api_key", "base_url"} 或 None
    """
    if not model_name or _credentials is None:
        return None
    try:
        return _credentials.find_llm_config(model_name)
    except Exception:
        logger.debug("find_llm_config 失败", exc_info=True)
        return None
