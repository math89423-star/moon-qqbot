"""模型路由核心类型 — 零依赖，可独立使用。

定义 ModelTier / ModelRoute 以及依赖注入协议。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol


class ModelTier(enum.Enum):
    """模型等级 — Gate 选模型 (正交于 reasoning_effort 开思考)。

    model_tier 选槽位, reasoning_effort 选是否开思考 — 两个独立维度。
    所有 tier 都能开思考能力。
    """
    LITE = 1   # 日常闲聊 — llm_lite (便宜快速)
    PRO = 2    # 推理增强 — llm_pro (强模型)
    # JUDGE removed (2026-06-30): 争议仲裁功能已删除，跨bot联动在单bot场景下不可用


@dataclass
class ModelRoute:
    """解析后的模型路由结果。

    Attributes:
        tier: 模型等级
        model: 传给 API 的 model 参数
        provider: provider 标识 (决定 chat_completion_source 或直连路由)
        extra_params: 传给 API 的额外参数 (如 reasoning_effort)
        api_base: 三方代理 base_url (非空时绕过酒馆直连)
        api_key: 三方代理 api_key
    """
    tier: ModelTier
    model: str
    provider: str
    extra_params: dict = field(default_factory=dict)
    api_base: str = ""
    api_key: str = ""


class DomainAwareness(Protocol):
    """领域感知 — 决定当前话题是否需要深度推理。

    由消费方 (suli_tavern) 注入实现。
    不注入时 domain 升级信号静默跳过 (安全降级)。
    """

    def is_reasoning_needed(
        self, active_domains: dict[str, float], threshold: float = 2.0,
    ) -> bool: ...

    def user_force_reasoning(self, message: str) -> bool: ...


class CredentialProvider(Protocol):
    """凭证提供者 — 解析模型名→api_base/api_key。

    由消费方 (suli_tavern) 注入 bot_db + bot_config 实现。
    不注入时 resolve() 回退到默认模型名，api_base/api_key 为空。
    """

    def get_config_model(self, key: str, default: str = "") -> str: ...

    def find_llm_config(self, model_name: str) -> dict[str, Any] | None: ...

    def resolve_active_llm(self) -> Any | None: ...
