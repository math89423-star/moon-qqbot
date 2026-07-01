"""暮恩自适应模型路由 — Lite / Pro 二级切换。

纯库插件，供其他 AstrBot 插件 import 使用。

提供:
  - ModelTier:     模型等级枚举 (LITE / PRO)
  - ModelRoute:    路由结果 dataclass (model + provider + api_base + api_key)
  - ModelRouter:   静态路由器 (decide_tier + resolve)
  - init_domain_awareness():    注入领域感知 (is_reasoning_needed + user_force_reasoning)
  - init_credential_provider(): 注入凭证提供者 (bot_db + bot_config 桥接)

用法:
  from astrbot_plugin_suli_routing import ModelRouter, ModelTier, ModelRoute
  from astrbot_plugin_suli_routing import init_domain_awareness, init_credential_provider

  # 启动时注入依赖
  init_domain_awareness(my_domain_impl)
  init_credential_provider(my_credential_impl)

  # 运行时路由
  tier = ModelRouter.decide_tier(
      trigger_reason="mention",
      active_domains=ctx.active_domains,
      user_message=msg,
      ...
  )
  route = ModelRouter.resolve(tier, default_provider="deepseek")
"""

from .router import ModelRouter, init_credential_provider, init_domain_awareness
from .types import CredentialProvider, DomainAwareness, ModelRoute, ModelTier

__all__ = [
    "CredentialProvider",
    "DomainAwareness",
    "ModelRoute",
    "ModelRouter",
    "ModelTier",
    "init_credential_provider",
    "init_domain_awareness",
]
