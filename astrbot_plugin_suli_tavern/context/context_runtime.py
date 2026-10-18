"""Context Runtime — 上下文运行时引擎。

核心职责:
  1. Persona Kernel 编译: 将完整角色卡拆分为 Always-On Kernel + Selective Retrieval
  2. Token Budget 调度: 根据对话类型动态分配各部分 token 预算
  3. Cache-Aware 组装: 确保静态前缀字节级一致
  4. Stale-Response 检测: 高活跃群的消息版本校验

设计原则 (来自 EverHome 方法论):
  - latency > exhaustive memory (QQ 群聊优先响应速度)
  - Persona Kernel ≤ 2.5K token (Always-On)
  - 动态部分按需检索 (RAG/Selective)
  - Memory Write 不阻塞主回复路径
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# §1 Token Budget 配置
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TokenBudget:
    """Token 预算分配 — 根据对话类型动态调整。

    总预算目标: ~8K-12K (QQ 群聊延迟敏感)
    """
    # Always-On (每轮必须)
    identity_kernel: int = 1500      # 核心身份: 谁、什么性格
    personality_kernel: int = 800    # 行为准则: 怎么说话、边界
    voice_kernel: int = 400          # 语言风格: 语气词、标点习惯

    # Semi-Stable (每几轮更新)
    relationship_snapshot: int = 300 # 当前用户关系快照

    # Dynamic (按需检索)
    recent_chat: int = 2000          # 最近对话历史
    retrieved_memory: int = 800      # 检索的相关记忆
    retrieved_canon: int = 500       # 检索的世界书/设定
    current_state: int = 200         # 当前状态 (时间/心情)

    @property
    def total_budget(self) -> int:
        """总 token 预算。"""
        return (
            self.identity_kernel
            + self.personality_kernel
            + self.voice_kernel
            + self.relationship_snapshot
            + self.recent_chat
            + self.retrieved_memory
            + self.retrieved_canon
            + self.current_state
        )


# ── 预设预算配置 (per 场景) ──

BUDGET_FAST_CHAT = TokenBudget(
    # 群聊闲聊: 最小化延迟
    identity_kernel=1200,
    personality_kernel=600,
    voice_kernel=300,
    relationship_snapshot=200,
    recent_chat=1500,
    retrieved_memory=400,
    retrieved_canon=200,
    current_state=150,
)

BUDGET_DEEP_CHAT = TokenBudget(
    # 深度对话: 允许更多上下文
    identity_kernel=1500,
    personality_kernel=800,
    voice_kernel=400,
    relationship_snapshot=300,
    recent_chat=2500,
    retrieved_memory=1200,
    retrieved_canon=800,
    current_state=200,
)

BUDGET_TOOL_CALL = TokenBudget(
    # 工具调用: 最小化 persona, 最大化工具相关上下文
    identity_kernel=800,
    personality_kernel=400,
    voice_kernel=200,
    relationship_snapshot=100,
    recent_chat=1500,
    retrieved_memory=600,
    retrieved_canon=400,
    current_state=150,
)


# ═══════════════════════════════════════════════════════════════
# §2 Persona Kernel — 角色卡运行时编译
# ═══════════════════════════════════════════════════════════════

@dataclass
class PersonaKernel:
    """Persona Kernel — 从完整角色卡编译出的轻量运行时上下文。

    目标: ≤ 2.5K token, 覆盖角色核心身份 + 行为 + 语言风格。
    剩余内容 (世界书/关系/剧情) 按需通过 RAG 检索注入。
    """
    # Always-On 核心 (每轮注入)
    identity: str = ""          # 身份: 谁、什么存在
    personality: str = ""       # 性格: 核心特质
    voice: str = ""             # 语言风格: 语气词、标点

    # 可选增强 (按需)
    behavioral_constitution: str = ""  # 行为边界/规则
    key_relationships: str = ""        # 关键关系 (仅相关时注入)

    # 元数据
    compiled_at: float = 0.0
    source_hash: str = ""       # 来源角色卡哈希 (用于缓存失效)

    @property
    def token_estimate(self) -> int:
        """粗略估算 token 数 (中文 ~1.5 chars/token)。"""
        total_chars = (
            len(self.identity)
            + len(self.personality)
            + len(self.voice)
            + len(self.behavioral_constitution)
            + len(self.key_relationships)
        )
        return int(total_chars / 1.5)

    def to_static_prompt(self) -> str:
        """组装为静态 system prompt (用于 message[0])。

        结构: [身份] [性格] [语言风格] [行为规则]
        这段内容必须字节级一致以命中前缀缓存。
        """
        parts = []
        if self.identity:
            parts.append(f"[身份核心]\n{self.identity}")
        if self.personality:
            parts.append(f"[性格核心]\n{self.personality}")
        if self.voice:
            parts.append(f"[语言风格]\n{self.voice}")
        if self.behavioral_constitution:
            parts.append(f"[行为规则]\n{self.behavioral_constitution}")
        return "\n\n".join(parts)


def compile_persona_kernel(
    character: dict,
    max_tokens: int = 2500,
) -> PersonaKernel:
    """从完整角色卡编译 Persona Kernel。

    策略:
      1. 提取 identity/personality/voice 核心段
      2. 如果超过 max_tokens, 按优先级截断
      3. 保留 behavioral_constitution 作为边界

    Args:
        character: 完整角色卡 dict (SillyTavern 格式)
        max_tokens: 目标 token 上限

    Returns:
        PersonaKernel 实例
    """
    system_prompt = character.get("system_prompt", "")
    name = character.get("name", "Assistant")

    # ── 策略: 按 section marker 拆分 ──
    # 角色卡通常有 [你是谁] [你的内在知识] [说话方式] 等标记
    identity = ""
    personality = ""
    voice = ""
    behavioral = ""

    # 简单启发式拆分 (基于常见标记)
    sections = _split_system_prompt(system_prompt)

    identity = sections.get("identity", system_prompt[:1500])  # fallback: 前 1500 字符
    personality = sections.get("personality", "")
    voice = sections.get("voice", "")
    behavioral = sections.get("behavioral", "")

    # ── Token 预算控制 ──
    kernel = PersonaKernel(
        identity=_truncate_to_tokens(identity, max_tokens // 2),
        personality=_truncate_to_tokens(personality, max_tokens // 4),
        voice=_truncate_to_tokens(voice, max_tokens // 8),
        behavioral_constitution=_truncate_to_tokens(behavioral, max_tokens // 8),
        compiled_at=time.time(),
    )

    logger.info(
        "Persona Kernel 编译完成: %s, ~%d tokens (目标: %d)",
        name, kernel.token_estimate, max_tokens,
    )
    return kernel


def _split_system_prompt(prompt: str) -> dict[str, str]:
    """按 section marker 拆分 system prompt。

    常见标记:
      [你是谁] / [身份] → identity
      [性格] / [你的特质] → personality
      [说话方式] / [语言风格] → voice
      [规则] / [边界] / [禁止] → behavioral
    """
    import re

    sections = {}
    current_key = "identity"  # 默认第一个段为 identity
    current_lines: list[str] = []

    for line in prompt.split("\n"):
        stripped = line.strip()
        # 检测 section marker
        if re.match(r"^\[.*?(身份|你是谁|identity).*?\]$", stripped, re.IGNORECASE):
            if current_lines:
                sections[current_key] = "\n".join(current_lines)
            current_key = "identity"
            current_lines = []
        elif re.match(r"^\[.*?(性格|特质|personality).*?\]$", stripped, re.IGNORECASE):
            if current_lines:
                sections[current_key] = "\n".join(current_lines)
            current_key = "personality"
            current_lines = []
        elif re.match(r"^\[.*?(说话|语言|voice|style).*?\]$", stripped, re.IGNORECASE):
            if current_lines:
                sections[current_key] = "\n".join(current_lines)
            current_key = "voice"
            current_lines = []
        elif re.match(r"^\[.*?(规则|边界|禁止|behavior|rule).*?\]$", stripped, re.IGNORECASE):
            if current_lines:
                sections[current_key] = "\n".join(current_lines)
            current_key = "behavioral"
            current_lines = []
        else:
            current_lines.append(line)

    # 保存最后一段
    if current_lines:
        sections[current_key] = "\n".join(current_lines)

    return sections


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """粗略截断到指定 token 数。

    中文约 1.5 chars/token, 英文约 4 chars/token.
    使用 2 chars/token 作为保守估计。
    """
    max_chars = max_tokens * 2
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


# ═══════════════════════════════════════════════════════════════
# §3 Context Runtime 组装器
# ═══════════════════════════════════════════════════════════════

@dataclass
class RuntimeContext:
    """运行时上下文 — 组装完毕待发送给 LLM 的最终产物。"""
    # 静态前缀 (message[0], 缓存友好)
    static_prefix: str = ""

    # 动态后缀 (message[1+], 每轮变化)
    dynamic_suffix: str = ""

    # 元数据
    budget_used: int = 0
    budget_total: int = 0
    cache_friendly: bool = True

    # 用于 stale-response 检测
    conversation_version: int = 0
    built_at: float = 0.0


class ContextRuntimeEngine:
    """上下文运行时引擎 — 协调 Persona Kernel + Token Budget + Cache Planning。

    核心流程:
      1. 编译/加载 Persona Kernel (缓存)
      2. 确定 Token Budget (按场景)
      3. 检索动态上下文 (记忆/世界书/历史)
      4. 组装 RuntimeContext (static + dynamic)
      5. 验证 cache 友好性
    """

    def __init__(self):
        self._kernel_cache: dict[str, PersonaKernel] = {}  # {character_hash: kernel}
        self._conversation_versions: dict[str, int] = {}   # {group_id: version}

    def build_runtime_context(
        self,
        character: dict,
        *,
        bot_id: str,
        user_id: str,
        group_id: str = "",
        budget: TokenBudget = BUDGET_FAST_CHAT,
        # 动态上下文
        recent_messages: list[dict] | None = None,
        memory_hints: str = "",
        world_book_entries: list[str] | None = None,
        current_mood: str = "",
        relationship_level: int = 0,
    ) -> RuntimeContext:
        """构建运行时上下文。

        Args:
            character: 完整角色卡
            bot_id: Bot QQ 号
            user_id: 触发用户 QQ 号
            group_id: 群号 (用于版本追踪)
            budget: Token 预算配置
            recent_messages: 最近对话历史
            memory_hints: 检索到的记忆提示
            world_book_entries: 检索到的世界书条目
            current_mood: 当前心情
            relationship_level: 好感度等级

        Returns:
            RuntimeContext 实例
        """
        start_time = time.time()

        # ── 1. 获取/编译 Persona Kernel ──
        kernel = self._get_or_compile_kernel(character, budget)

        # ── 2. 组装 Static Prefix (message[0]) ──
        static_parts = [kernel.to_static_prompt()]

        # 关键关系 (仅高好感度时注入)
        if relationship_level >= 3 and kernel.key_relationships:
            static_parts.append(f"[关键关系]\n{kernel.key_relationships}")

        static_prefix = "\n\n".join(filter(None, static_parts))

        # ── 3. 组装 Dynamic Suffix (message[1+]) ──
        dynamic_parts: list[str] = []

        # 当前状态
        state_lines = []
        if current_mood:
            state_lines.append(f"心情: {current_mood}")
        if relationship_level > 0:
            state_lines.append(f"好感度: {relationship_level}")
        if state_lines:
            dynamic_parts.append("[当前状态]\n" + "\n".join(state_lines))

        # 记忆
        if memory_hints:
            dynamic_parts.append(f"[相关记忆]\n{memory_hints[:budget.retrieved_memory * 2]}")

        # 世界书
        if world_book_entries:
            canon_text = "\n".join(world_book_entries)
            dynamic_parts.append(f"[世界设定]\n{canon_text[:budget.retrieved_canon * 2]}")

        # 最近对话
        if recent_messages:
            chat_lines = []
            for msg in recent_messages[-10:]:  # 最多 10 条
                name = msg.get("user_name", "???")
                text = msg.get("content", "")[:100]  # 截断长消息
                chat_lines.append(f"{name}: {text}")
            if chat_lines:
                dynamic_parts.append("[最近对话]\n" + "\n".join(chat_lines))

        dynamic_suffix = "\n\n".join(filter(None, dynamic_parts))

        # ── 4. 版本追踪 (stale-response 检测) ──
        conv_version = 0
        if group_id:
            conv_version = self._conversation_versions.get(group_id, 0) + 1
            self._conversation_versions[group_id] = conv_version

        # ── 5. 组装 RuntimeContext ──
        result = RuntimeContext(
            static_prefix=static_prefix,
            dynamic_suffix=dynamic_suffix,
            budget_used=len(static_prefix) // 2 + len(dynamic_suffix) // 2,  # 粗略 token 估算
            budget_total=budget.total_budget,
            cache_friendly=True,  # 静态前缀字节级一致
            conversation_version=conv_version,
            built_at=start_time,
        )

        elapsed_ms = (time.time() - start_time) * 1000
        logger.info(
            "Context Runtime: bot=%s group=%s budget=%d/%d tokens, "
            "static=%d chars, dynamic=%d chars, elapsed=%.1fms",
            bot_id, group_id, result.budget_used, result.budget_total,
            len(static_prefix), len(dynamic_suffix), elapsed_ms,
        )

        return result

    def _get_or_compile_kernel(
        self,
        character: dict,
        budget: TokenBudget,
    ) -> PersonaKernel:
        """获取缓存的 Persona Kernel, 或编译新的。"""
        # 简单哈希 (基于 system_prompt)
        sp = character.get("system_prompt", "")
        cache_hash = str(hash(sp[:500]))  # 取前 500 字符做 hash

        if cache_hash in self._kernel_cache:
            return self._kernel_cache[cache_hash]

        kernel = compile_persona_kernel(
            character,
            max_tokens=budget.identity_kernel + budget.personality_kernel + budget.voice_kernel,
        )
        self._kernel_cache[cache_hash] = kernel
        return kernel

    def check_stale_response(
        self,
        group_id: str,
        expected_version: int,
    ) -> bool:
        """检查响应是否过期。

        如果群聊在 LLM 思考期间收到新消息, version 会递增。
        返回 True 表示响应已过期, 应该丢弃或补充。

        Args:
            group_id: 群号
            expected_version: 发送请求时的版本号

        Returns:
            True = 响应过期
        """
        if not group_id:
            return False
        current = self._conversation_versions.get(group_id, 0)
        return current > expected_version

    def increment_conversation_version(self, group_id: str) -> int:
        """递增群聊版本号 (收到新消息时调用)。"""
        if not group_id:
            return 0
        current = self._conversation_versions.get(group_id, 0)
        new_version = current + 1
        self._conversation_versions[group_id] = new_version
        return new_version


# ═══════════════════════════════════════════════════════════════
# §4 单例 + 初始化
# ═══════════════════════════════════════════════════════════════

_engine: ContextRuntimeEngine | None = None


def get_context_runtime() -> ContextRuntimeEngine:
    """获取 Context Runtime 引擎单例。"""
    global _engine
    if _engine is None:
        _engine = ContextRuntimeEngine()
    return _engine
