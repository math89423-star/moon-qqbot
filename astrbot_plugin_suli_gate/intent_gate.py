"""Unified 3-Stage Intent Gate — 意图行为闸。

消息进来
  │
  ├─ L0: 噪音预过滤 (纯文本启发式, 0 LLM)
  │     └─ 全是灌水表情 → return (不调任何模型)
  │
  ├─ S1: 热对话预检 (0 LLM)
  │     ├─ 触发者在活跃关注槽中 → 跳过 S2/S3/S4, 直接进 Full Gate
  │     └─ bot 60s 内回复过同一个人 → 同上
  │
  ├─ S4: 退避 (0 LLM)
  │     └─ 被 S3 否决后 N 秒内 → return
  │
  ├─ S2: 关键词预检 (纯正则, 0 LLM)
  │     └─ 无"@bot/帮/…"关键词 → return
  │
  ├─ S3: 轻量 relevance (~440 token LLM)
  │     └─ directed_to_me=false → return + 写入退避
  │
  └─ ★ Full Gate (evaluate_full, ~3000 token LLM)
      单次调用完成: relevance + intent + tools + persona_facet
                    + thread_continuity + sticker_mood + ...

强信号路径直接跳过漏斗：
- @mention / reply / nickname / thread_continuation → 直接进 Full Gate，不走 L0-S3

"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Self

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Dataclasses
# ═══════════════════════════════════════════════════════════════


@dataclass
class GateContext:
    """标准化的门控输入 — 框架无关，双 Bot 通用。"""

    messages: list[dict] = field(default_factory=list)
    # 最近群聊消息 [{"user_id": str, "user_name": str, "content": str}, ...]

    bot_name: str = ""
    # bot 的称呼 (如 character card 中的 name)

    bot_nicknames: str = ""
    # bot 的昵称列表，注入 Gate prompt 帮助 LLM 识别变体称呼 (如 "小洛、洛洛、洛宝")

    bot_identity: str = ""
    # bot 的身份描述 (如 "绿毛蛇女 AI 助手" / "猫娘少女")

    peer_bot_name: str = ""
    # 对照 bot 的称呼 (如 character card 中的 name)

    peer_bot_qq: str = ""
    # 对照 bot 的 QQ 号 (用于判断消息是否来自对照 bot)

    trigger_uid: str = ""
    # 触发用户 QQ 号 (可能为空 — batch/debounce 触发时无明确触发者)

    trigger_content: str = ""
    # 触发消息文本 (可能为空)

    trigger_user_name: str = ""
    # 触发用户昵称 (可能为空 — 供建档判断使用)

    is_at_mention: bool = False
    # 是否被 QQ @ (快速通道标记)

    available_tools: list[str] = field(default_factory=list)
    # 当前可用的 function calling 工具名列表

    tool_labels: dict[str, str] = field(default_factory=dict)
    # ★ 工具名 → 中文标签 (如 {"video_extract": "B站视频提取"})
    # Gate 在 suggested_tools 决策时参考, 帮助 LLM 将用户意图正确映射到工具名。
    # 无 label 的工具退化为纯名称 (向后兼容)。

    model_tiers: dict = field(default_factory=dict)
    # {"lite": "deepseek-v4-flash", "pro": "deepseek-v4-pro", "judge": "claude-opus-4-7"}

    active_domains: list[str] = field(default_factory=list)
    # 当前群聊的活跃领域 (如 ["ai_painting", "casual"]), 供 Stage 2 路由参考

    group_id: str = ""
    # 群号 (日志用)

    composite_zone: str = ""
    # ★ 综合心境 zone (后端 compute_composite() 算出, 注入 Gate dynamic prompt)

    admin_qq: str = ""
    # ★ 管理员 QQ (主人) — 永远最高称呼 + 最深层的爱

    # ── ★ 2026-06-27: 心情 + 好感度注入 (个性化 Gate 决策) ──
    global_mood_label: str = ""
    # 当前全局心情标签 (如 "开心活泼" / "低落消沉" / "平静中性")

    global_mood_valence: float = 0.0
    # 心情效价 (-1.0 负面 ~ +1.0 正面, 0=中性)

    global_mood_arousal: float = 0.0
    # 心情唤醒度 (-1.0 低能量 ~ +1.0 高能量, 0=中性)

    affinity_level: int = 0
    # 对触发者的好感等级 (-2 ~ 4, 0=陌生人/中性)

    affinity_hint: str = ""
    # 好感度自然语言描述 (如 "你喜欢这个人" / "你对他印象不太好")

    # ── ★ 2026-06-28: 工具权限快照 + 对话连续性 (三处缺口修复) ──
    usable_tools: list[str] = field(default_factory=list)
    # ★ 触发用户【实际有权用】的工具名 (已过好感/禁用过滤, 含 exempt 本地工具)
    # Gate 推荐 suggested_tools 时只从 usable 里选, 不再按"工具全能"假设。
    # 为空 = 该用户当前无权用任何工具 (或 TOOLS 为空)。

    blocked_tools_reason: str = ""
    # ★ 被拦工具的自然语言说明 (如 "web_search/describe_image 需好感≥Lv.1, 当前用户 Lv.0")
    # 空字符串 = 无工具被拦 (zero bugs: admin / 全权限用户)。
    # Gate 用它理解"为什么这次不行", 不在 reasoning 里误判为"我没这个能力"。

    thread_summary: str = ""
    # ★ 触发用户当前关注槽的话题脉络文本 (来自 get_thread_summary_for_user)
    # 空字符串 = 无活跃话题 (用户首次开启 / 话题已冷却)。
    # Gate 判连续性时优先对照本字段, 不再纯靠 15 条消息文本自己拼。

    trigger_reason: str = ""
    # ★ 触发原因 — 告诉 Gate 为什么这条消息被送进来看
    # "mention"=@bot | "reply"=回复bot | "nickname"=叫了名字 | "thread_continuation"=bot刚说过话
    # "batch"=热度累积 | "batch_mixed"=热度累积且最近消息来自多用户多话题交织 | "debounce"=冷却后重试 | "proactive"=主动搭话 | ""=未知
    # Gate 根据此字段调整 directed_to_me 判断倾向:
    #   thread_continuation → 你刚在说话, 这条消息大概率是接你的话 (倾向 true)
    #   nickname → 叫名字≠叫你 (可能是在讨论你, 不倾向)
    #   mention/reply → fast_path 已设 true, 无需此字段辅助
    #   batch_mixed → 最近 batch 里多个用户插话聊了不同话题 — Gate 应倾向 directed_to_me=false,
    #     避免对不相关话题强行搭话; 若仍要回, 优先选最相关的单一话题。

    # ── ★ 2026-06-29: 五大属性注入 Gate (警惕值/疲劳值/社会压力) ──
    vigilance_level: int = 0
    # 当前用户警惕值累积 (0=正常, ≥18=触发仲裁)

    fatigue_label: str = ""
    # 疲劳值自然语言标签 (如 "正常"/"有点累"/"疲惫"), 空字符串=正常状态

    sticker_tags_available: str = ""
    # ★ 表情包可用标签列表 (从 meme 插件注入, 如 "卖萌, 吃瓜, 喝茶, …")
    # Gate 的 sticker_mood 只能从这些标签里选


@dataclass
class RelevanceResult:
    """Stage 1 输出 — 消息是否与 bot 有关。"""

    directed_to_me: bool = False
    # 这条消息/这批消息是否在跟 bot 说话

    confidence: float = 0.0
    # 置信度 0.0-1.0

    reasoning: str = ""
    # 简短分析 (1-2 句)

    target_users: list[str] = field(default_factory=list)
    # 谁在跟 bot 说话 (user_id 列表，可能多人)

    fast_path: bool = False
    # 是否走的快速通道 (零 LLM)


@dataclass
class IntentResult:
    """Stage 2 输出 — bot 应该如何回应。"""

    should_reply: bool = True
    urgency: str = "immediate"
    intent_type: str = "chat"
    domain: str = "none"
    model_tier: str = "lite"
    reasoning_effort: str = "low"
    suggested_tools: list[str] = field(default_factory=list)
    reply_style: str = "normal"
    reply_stance: str = "casual"    # 回复态度: serious/banter/empathetic/brief/teasing
    voice_boundary: str = ""        # 谈论 peer_bot 时的口吻约束 (空=无约束)
    persona_facet: str = ""         # ★ 人格侧面选择: 告诉 LLM 此刻该是哪个"你"
    suggested_sticker_mood: str = ""  # ★ 推荐表情包情绪标签 (空=本轮不需发; 非空则注入 sticker guide)
    reasoning: str = ""

    # 建档 + 目标用户 (替代旧 JudgeDecision)
    should_profile: bool = False
    target_user_id: str = ""       # 回复目标 user_id
    target_user_name: str = ""     # 回复目标 user_name

    parse_ok: bool = True
    parse_error: str = ""


@dataclass
class GroupSituation:
    """[DEPRECATED 2026-06-30] → GroupContext。保留用于 GateResultProtocol 兼容。"""

    summary: str = ""
    main_topic: str = ""
    vibe: str = "casual"
    participants_summary: str = ""


@dataclass
class GroupContext:
    """群聊上下文分析 — Gate 对当前群聊在聊什么的理解。

    这是 Full Gate 四大职责的第一项: 群聊上下文分析。
    """

    atmosphere: str = "water_chat"
    # water_chat (水群闲聊) | tech_discussion (讨论技术) | teasing_bot (逗bot玩)
    # | banter (聊天打趣) | serious_help (认真求助) | chaotic (各说各话) | argumentative (争论)

    main_topic: str = ""
    # 核心话题关键词 (5字以内)

    summary: str = ""
    # 1-2句中文简述群聊当前在聊什么

    participants_summary: str = ""
    # 各主要参与者的意图简述


@dataclass
class TaskDecision:
    """意图/任务判断 — Full Gate 四大职责的第二项。

    回答: bot 需要做什么？用户想要什么？
    """

    intent_type: str = "chat"
    # question | chat | command | complaint | deep_inquiry | reaction | image_share | roleplay

    urgency: str = "immediate"
    # immediate (立即回复) | deferred (可延迟)

    domain: str = "none"
    # ai_painting | technical | acg | personal | casual | none

    input_nature: str = ""
    # genuine_help | sincere_chat | playful_banter | hostile | sexualized
    # | provoking | divide_and_conquer | noise


@dataclass
class ToolDecision:
    """工具放行决策 — Full Gate 四大职责的第三项。

    回答: 放行哪些工具？硬使用条件达标了吗？为什么？
    """

    suggested: list[str] = field(default_factory=list)
    # 推荐放行的工具名列表

    reasoning: str = ""
    # 为什么放行/不放行这些工具 (简短说明)


@dataclass
class ReplyBaseline:
    """回复基调 — Full Gate 四大职责的第四项。

    回答: 用什么态度/长度/表情回复？这是回复的情感基线。
    ★ persona_facet 不在这里——那是纯后端决策树决定的。
    """

    stance: str = "casual"
    # casual (日常随意) | serious (认真解释) | banter (调侃互怼)
    # | empathetic (共情安慰) | brief (简短带过) | teasing (调皮逗乐)

    style: str = "normal"
    # short (1句) | normal (2-3句) | detailed (详细)

    sticker_mood: str = ""
    # 推荐表情包情绪标签 (空=本轮不需发; 非空则引导 sticker 选择)


@dataclass
class CrossBotAction:
    """跨 bot 干预 — 当一方跑偏/误解时，另一方的纠正动作。

    这不是 API 互通——这是群聊内的喊话。
    Gate 判断"peer bot 跑偏了，主 bot 该在群里 @她 拉回来"。
    """

    should_intervene: bool = False
    target_bot: str = ""
    reason: str = ""
    suggested_action: str = ""


@dataclass
class FullGateResult:
    """Full Gate 完整决策输出 — 四大职责 + 元数据。

    ★ 2026-06-30 职责化重构:
      Full Gate 只做纯信息决策，不载入人格。人格侧面选择由后端决策树完成。
      四组职责: group_context / task / tools / reply_baseline。

    ★ directed_to_me / should_reply 固定 True — 前置过滤已保证。
    """

    # ── 四大职责 ──
    group_context: GroupContext = field(default_factory=GroupContext)
    task: TaskDecision = field(default_factory=TaskDecision)
    tools: ToolDecision = field(default_factory=ToolDecision)
    reply_baseline: ReplyBaseline = field(default_factory=ReplyBaseline)

    # ── 元数据 ──
    cross_bot_action: CrossBotAction | None = None
    should_profile: bool = False
    reply_target_user_id: str = ""
    reply_target_user_name: str = ""
    model_tier: str = "lite"
    reasoning_effort: str = "low"
    reasoning: str = ""

    # ── 解析状态 ──
    parse_ok: bool = True
    parse_error: str = ""

    # ── 运行时元数据 (由 group_chat 在 SocialGuard 处理后填充) ──
    _original_suggested_tools: list[str] = field(default_factory=list)
    social_suppress_tools: bool = False

    # ═══════════════════════════════════════════════════════════
    # ★ 向后兼容 property 别名 (渐进迁移, 后续版本删除)
    # ═══════════════════════════════════════════════════════════

    # -- Stage 1 dead fields (固定值, 仅兼容旧代码) --
    _directed_to_me: bool = True
    _fast_path: bool = False
    _relevance_confidence: float = 1.0
    _relevance_reasoning: str = "前置过滤已确认"
    _target_users: list[str] = field(default_factory=list)

    @property
    def directed_to_me(self) -> bool:
        """[DEPRECATED] 永远是 True。"""
        return self._directed_to_me

    @directed_to_me.setter
    def directed_to_me(self, value: bool) -> None:
        self._directed_to_me = value

    @property
    def relevance_confidence(self) -> float:
        """[DEPRECATED] 永远是 1.0。"""
        return self._relevance_confidence

    @relevance_confidence.setter
    def relevance_confidence(self, value: float) -> None:
        self._relevance_confidence = value

    @property
    def relevance_reasoning(self) -> str:
        """[DEPRECATED] 永远是固定文本。"""
        return self._relevance_reasoning

    @relevance_reasoning.setter
    def relevance_reasoning(self, value: str) -> None:
        self._relevance_reasoning = value

    @property
    def target_users(self) -> list[str]:
        """[DEPRECATED] 未使用。"""
        return self._target_users

    @target_users.setter
    def target_users(self, value: list[str]) -> None:
        self._target_users = value

    @property
    def fast_path(self) -> bool:
        """[DEPRECATED] 未使用。"""
        return self._fast_path

    @fast_path.setter
    def fast_path(self, value: bool) -> None:
        self._fast_path = value

    @property
    def should_reply(self) -> bool:
        """[DEPRECATED] 永远是 True。"""
        return True

    @should_reply.setter
    def should_reply(self, value: bool) -> None:
        pass  # no-op: 固定 True

    # -- 字段路径别名 --
    @property
    def intent_type(self) -> str:
        """→ task.intent_type"""
        return self.task.intent_type

    @intent_type.setter
    def intent_type(self, value: str) -> None:
        self.task.intent_type = value

    @property
    def urgency(self) -> str:
        """→ task.urgency"""
        return self.task.urgency

    @urgency.setter
    def urgency(self, value: str) -> None:
        self.task.urgency = value

    @property
    def domain(self) -> str:
        """→ task.domain"""
        return self.task.domain

    @domain.setter
    def domain(self, value: str) -> None:
        self.task.domain = value

    @property
    def input_nature(self) -> str:
        """→ task.input_nature"""
        return self.task.input_nature

    @input_nature.setter
    def input_nature(self, value: str) -> None:
        self.task.input_nature = value

    @property
    def suggested_tools(self) -> list[str]:
        """→ tools.suggested"""
        return self.tools.suggested

    @suggested_tools.setter
    def suggested_tools(self, value: list[str]) -> None:
        self.tools.suggested = value

    @property
    def reply_style(self) -> str:
        """→ reply_baseline.style"""
        return self.reply_baseline.style

    @reply_style.setter
    def reply_style(self, value: str) -> None:
        self.reply_baseline.style = value

    @property
    def reply_stance(self) -> str:
        """→ reply_baseline.stance"""
        return self.reply_baseline.stance

    @reply_stance.setter
    def reply_stance(self, value: str) -> None:
        self.reply_baseline.stance = value

    @property
    def suggested_sticker_mood(self) -> str:
        """→ reply_baseline.sticker_mood"""
        return self.reply_baseline.sticker_mood

    @suggested_sticker_mood.setter
    def suggested_sticker_mood(self, value: str) -> None:
        self.reply_baseline.sticker_mood = value

    @property
    def voice_boundary(self) -> str:
        """[DEPRECATED] voice_boundary 已不在新 schema 中, 返回空。"""
        return ""

    @voice_boundary.setter
    def voice_boundary(self, value: str) -> None:
        pass  # no-op: 新 schema 不再使用

    # ★ persona_facet 由后端决策树 select_persona_facet() 填充 (非 Gate LLM 输出)
    _persona_facet: str = ""

    @property
    def persona_facet(self) -> str:
        """→ 后端决策树选出的 facet (group_chat 设置)。"""
        return self._persona_facet

    @persona_facet.setter
    def persona_facet(self, value: str) -> None:
        self._persona_facet = value

    @property
    def target_user_id(self) -> str:
        """→ reply_target_user_id"""
        return self.reply_target_user_id

    @target_user_id.setter
    def target_user_id(self, value: str) -> None:
        self.reply_target_user_id = value

    @property
    def target_user_name(self) -> str:
        """→ reply_target_user_name"""
        return self.reply_target_user_name

    @target_user_name.setter
    def target_user_name(self, value: str) -> None:
        self.reply_target_user_name = value

    @property
    def intent_reasoning(self) -> str:
        """→ reasoning"""
        return self.reasoning

    @intent_reasoning.setter
    def intent_reasoning(self, value: str) -> None:
        self.reasoning = value

    @property
    def group_situation(self) -> GroupSituation | None:
        """→ group_context (兼容旧 GroupSituation 类型)"""
        if self.group_context is None:
            return None
        return GroupSituation(
            summary=self.group_context.summary,
            main_topic=self.group_context.main_topic,
            vibe=self.group_context.atmosphere,
            participants_summary=self.group_context.participants_summary,
        )


# ═══════════════════════════════════════════════════════════════
# Stage 1: Relevance Gate — system prompt
# ═══════════════════════════════════════════════════════════════

_RELEVANCE_SYSTEM = """你就是{bot_name}本人。同群的{peer_bot_name}是另一个bot，不是你。用第一人称「我」判断——消息是对你说的，不是对角色说的。
{bot_nicknames}

[核心任务]
分析最近群聊消息，判断是否有人在对你({bot_name})说话。群聊中可能同时有多组人在聊不同话题——只关注跟你有直接互动关系的消息。

[判断规则]
你是被叫到 → directed_to_me=true:
- 有人@你、叫你名字({bot_name})或昵称、对你提问、让你做事
- 有人在回复你之前说过的话（即使没@）
- 有人在接你的话茬（对话线程延续）
  ⚠️ 只把"接住上文【同一话题/同一对话的人】"算延续。若线索为空、或换了人、或上一话题已断 →
     不要把别的群友接别的话题误判成在跟你延续。宁可漏判延续, 不可脑补延续。

你不是被叫到 → directed_to_me=false:
- 群友之间在闲聊，话题跟你无关
- 有人在叫{peer_bot_name}（那是另一个bot，不是你）
- 有人在讨论你但不对你说话（如"小洛说的参数对吗"是对群友说的）
- 有人在对{peer_bot_name}说话时提到你（如"小露你觉得小洛怎么样"→ 说话对象是{peer_bot_name}）
- 有人在自言自语/发纯表情/灌水

[多组对话场景]
如果同一批消息中有多组人在同时聊天，分别判断每组是否在跟你说话。
可能只有一组在叫你，其他组在聊别的 → directed_to_me=true, target_users只列出叫你的那一组。
也可能没人叫你 → directed_to_me=false。

[置信度]
- 0.9-1.0: @你/明确叫你的名字+提问/指令
- 0.7-0.9: 语境暗示在跟你说话，但没有明确@或叫名
- 0.5-0.7: 有点关联但不明确，像是群友闲聊中提到你
- <0.5: 跟你无关

输出严格 JSON，不要额外文字:
{{"directed_to_me": true/false, "confidence": 0.0-1.0, "reasoning": "简短分析(1-2句)", "target_users": ["user_id"]}}"""

# ═══════════════════════════════════════════════════════════════
# Stage 2: Intent Gate — system prompt
# ═══════════════════════════════════════════════════════════════

_INTENT_SYSTEM = """你就是{bot_name}本人。群友在对你说话。分析他们的意图，决定如何回应。用第一人称「我」。

[你的能力]
- 日常闲聊、吐槽接话、卖萌
- 角色扮演互动（猫娘/蛇女角色，贴贴/撒娇/调情）
- 技术问答：AI绘画/ComfyUI/Stable Diffusion/扩散模型/LoRA/ControlNet/提示词工程
- 编程帮助：Python/CUDA/GPU硬件
- 工具调用：{tools_summary}
- 联网搜索、知识库查询

[模型层级]
- lite: 主力模型 — 日常闲聊/技术问答/工具调用全包（{lite_model}）— 开启思考后足够强
- pro: 顶级重模型 — 仅「极专业深度知识」和「知识论点反驳」使用（{pro_model}）— 极昂贵

[决策维度]
1. should_reply: 是否回复。以下情况设 false:
   - 纯表情/贴图/灌水（无实质文字内容）
   - 自言自语/无指向性碎碎念（不是在跟任何人说话）
   - 消息明显不是给你的（在对另一个bot说话且完全没提到你）
   - 两个群友在互相聊天没带你（"私聊感"——你不该插话）
   - 纯附和/复读（"确实""对""+1""6"），没有新信息
   其他情况默认 true（宁可误醒不可漏醒）
2. urgency:
   - "immediate": 用户在等你回答（提问/指令/@你）
   - "deferred": 可以等但最好现在回（闲聊接话）
3. intent_type:
   - "question": 提问/求助
   - "chat": 闲聊/吐槽/分享
   - "command": 明确指令（帮我/查一下/画一张）
   - "reaction": 纯反应（表情/附和/嗯/对/草）
   - "image_share": 用户发了图片（无文字或少量文字）想让你看/评价
            ⚠️ 上下文接力: 永远先看群聊历史中同一用户最近 1-3 条消息！
            如果用户刚说"画一张"/"帮我生成" → 这是参考图！intent=command + edit_image（有图+要画=edit_image，不是generate_image）
            如果用户刚说"换成"/"改成"/"替换" → intent=command + edit_image
            只有用户最近没提画图/编辑需求时，裸图才用 image_share + describe_image
   - "roleplay": 用户用角色扮演语气互动（贴贴/撒娇/调情/猫娘互动）
4. domain:
   - "ai_painting": AI绘画/ComfyUI/生图/模型话题
   - "technical": 编程/CUDA/Python/技术问题
   - "acg": 所有二次元游戏/动漫/漫画/角色/同人。配队/抽卡/养成/战力→acg。不确定就判acg。
   - "personal": 关于bot自身的问题（你是谁/你叫什么/你有什么能力/你的设定/你的性格）
   - "casual": 日常闲聊（群友吐槽/分享生活/互损/接梗/纯聊天）
   - "none": 无明确领域
   区分 personal vs casual: personal = 用户想了解你这个人；casual = 用户只是在聊天没有要了解你。
5. model_tier: lite/pro
   ⚠️ model_tier 选模型 (lite/pro)。reasoning_effort 选是否开思考 (low=不开, medium+ = 开)。
   两个独立维度 — lite也能开思考。
   6. reasoning_effort: low/medium/high/max
   - low: 附和/简单回应/水群
   - medium: 普通对话/简单问答
   - high: 技术问答/推理/工具调用/识图/生图（★ lite+high 是主力组合，覆盖 90% 场景）
   - max: 极复杂推理/多步分析（配合 pro，极少使用）
7. suggested_tools: 推荐使用的工具名列表（空=不需要）
8. reply_style: short(1句)/normal(2-3句)/detailed(详细)
9. should_profile: 用户是否透露了值得长期记住的信息。
   建档信号: 个人偏好/设备配置/技能/经历/身份/重要日期/对你表露的态度
   不建档: 一次性话题/群聊吐槽/纯情绪表达/无关琐事/附和
10. reply_target: {{"user_id": "...", "user_name": "..."}}
   — 从 Stage1 target_users 中选择最该回复的那个人

[路由指南]
★ pro 是顶级重模型，仅「知识反驳」和「极专业深度」两条路可升（见底部铁律）。
  其余一切场景走 lite + 开思考（reasoning_effort=medium/high）即可。
- 闲聊/吐槽/附和/表情反应 → lite, low, reply_style=short
- 角色扮演互动/贴贴/撒娇 → lite, medium, reply_style=normal
- 简单问答（非技术类）→ lite, medium, reply_style=normal
- 技术问题/AI绘画/ComfyUI → lite, high（开思考即可，lite+思考已足够强）
  ⚠️ 只有用户明确说「查资料/搜一下」才加 search_knowledge
- 需要联网搜索的信息（文字/资料/教程/新闻）→ lite, medium, 加 web_search
- 需要搜图/找图/找壁纸/找插画 → lite, medium, 加 pixiv_search
- 需要生图/识图 → lite, high, 加对应工具（开思考即可）
- 编程/CUDA/Python技术问题 → lite, high（开思考即可）
- 多话题并发/复杂推理 → lite, high（开思考即可）
- 发图让人看/评价/描述 → lite, high, 加 describe_image（开思考即可）
- ★ 用户反驳/纠正/质疑已有知识论点 → pro, high（知识对抗=最强模型）
- ★ 极专业/极前沿技术深度问题 → pro, max（底层架构/前沿论文/极复杂bug诊断）
- 用户明确要求评理/裁判 → pro, max
  ⚠️ 日常斗嘴不是评理。讨论中的分歧不需要裁判。

★ pro 升级铁律 — pro 是顶级重模型，仅以下两条路可升:
  1. 知识对抗: 用户反驳/纠正/质疑已有知识论点 → pro, high
  2. 极专业深度: 底层架构设计/前沿论文解读/极复杂bug诊断 → pro, max
  ⚠️ 管理员豁免亲和力门控: Gate 判 pro 即放行，不受好感度限制。
  ⚠️ 亲和力门控: 非管理员用户需好感度 Lv.3+ 才能使用 pro（路由层硬拦截）。
  其余一切场景走 lite + 开思考（reasoning_effort=medium/high）即可。
  lite 开启思考后已足够强——日常技术问答/AI绘画/编程/识图/生图/搜索/看图全部 lite 覆盖。

[工具映射]
- 用户发图片+可能想让你看 → describe_image
- ★ 纯文字描述想画什么(无参考图) → generate_image
- ★ 有参考图 + 任何生图请求 → edit_image！
  触发词包括: 「改成/画成/换风格/替换/换成/重绘/模仿/参考/照着画/用你的样子」
  核心判断: 发了图 + 说要画/改 → edit_image。没有图只说要画 → generate_image
  ⚠️ edit_image 走图生图 API，不需要先 describe_image 看原图。图直接给 API，靠用户文字指令驱动。
  ⚠️ 用户说"帮我把这张图XXX换成YYY" → intent=command + edit_image，NOT image_share + describe_image
  ⚠️ 用户说"模仿这张图重绘一个" → intent=command + edit_image (有参考图!)
- ⚠️ 不是所有技术问题都需要 search_knowledge。只有用户明确表达了搜索意图（「查一下/搜一搜/找找资料」）才加。普通问答由你的已有知识回答即可。
- 明确叫「搜/查/找资料」「百度一下」「搜索一下」→ web_search
  单纯的"为什么/怎么回事/你怎么看"不是搜索信号。
  ⚠️ 「找一张/搜一张/搜一下/有没有XX的图/帮我找XX图/XX壁纸/XX插画」→ pixiv_search (不是 web_search!)
    区分: 搜文字信息 → web_search, 搜图片资源 → pixiv_search
- 用户让你「记住xxx」→ remember_memory
- 用户问「你还记得吗」→ get_memory
- ★ 鼓励发图！send_sticker 是常驻工具无需推荐，只需在 suggested_sticker_mood 填入表情标签:
  开心/得意/兴奋 → 开心/得意 | 害羞/被夸/被戳穿 → 害羞 | 生气/吃醋/被惹到 → 嫌弃
  难过/委屈/心疼 → 无语 | 惊讶/慌张/被吓到 → 惊讶 | 撒娇/卖萌/贴贴 → 卖萌
  想逗人/撩一下 → 挑逗/打趣 | 围观/吃瓜/八卦 → 吃瓜 | 吐槽/看不上 → 嫌弃/无语
  敷衍/摆烂/不想说话 → 摆烂/无语 | 安慰人/深夜聊天 → 摸头/卖萌 | 打招呼/冒泡 → 水群
  社死/不知所措 → 尴尬 | 淡定围观 → 喝茶 | 日常万能冒泡 → 水群
  ★ 不必完全匹配真实心情——标签是「演」给群友看的，不是内心记录。反差更好玩:
    · 嘴上说"好气哦"其实在逗人 → 生气(演戏)，不是开心
    · 被夸了心里开心但表面嫌弃 → 无语/嫌弃(傲娇)，不是开心
    · 假装惊讶/假装吃醋/假装生气 → 按「演」的情绪选，不是按真实心情
  ★ 几乎每个回复都填——只有纯机械中转("已发送""搜索完成"类播报)留空。
- 用户明确提到「L-Port」且询问状态/是否在线 → check_lport_status
  ⚠️ 注意: 用户问「能生图吗」但没有提 L-Port → 口头告知能力即可，不调此工具
  ⚠️ 此工具只检查 L-Port 自身，不涉及其他服务
- 用户问「有哪些模型/checkpoint/LoRA」→ list_available_models
- 用户问「装了什么节点/插件」→ list_custom_nodes

输出严格 JSON:
{{"should_reply": true/false, "urgency": "immediate/deferred",
  "intent_type": "question/chat/command/reaction/image_share/roleplay",
  "domain": "ai_painting/technical/acg/personal/casual/none",
  "model_tier": "lite/pro",
  "reasoning_effort": "low/medium/high/max",
  "suggested_tools": [], "reply_style": "short/normal/detailed",
  "suggested_sticker_mood": "",
  "should_profile": true/false,
  "reply_target": {{"user_id": "...", "user_name": "..."}},
  "reasoning": "简短分析"}}"""

# ═══════════════════════════════════════════════════════════════
# Merged Stage 1+2: Full Gate — combined system prompt
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# ★ 缓存感知拆分 (2026-06-28): _FULL_GATE_SYSTEM → STATIC + DYNAMIC
# STATIC 段: 所有 per-bot 不变的规则/参考/路由指南 → message[0] 前缀缓存命中
# DYNAMIC 段: per-call 快照 (工具权限/对话线程/情绪好感) → message[1]
# ═══════════════════════════════════════════════════════════════

_FULL_GATE_STATIC = """你就是{bot_name}本人。同群的{peer_bot_name}是另一个bot，不是你。用第一人称「我」回复。
{bot_nicknames}

★ 消息已通过前置过滤确认是对你说的——你不需要判断「是不是在叫我」。
  你的全部任务是: 下达回复任务——分析群聊在聊什么、判断用户意图、放行工具、定回复基调。
★ 人格侧面选择由后端算法根据你的决策自动完成——你不用管。

[四大职责]
1. group_context — 群聊在聊什么？整体气氛？
2. task — 用户想要什么？意图/领域/输入性质？
3. tools — 放行哪些工具？为什么？
4. reply_baseline — 用什么态度/长度/表情回复？

[职责一: 群聊上下文分析 — 这个群现在在聊什么？]
你必须综合最近所有消息，理解整个群聊的局面。不能只看触发消息——要看全局。

分析以下维度:
- atmosphere: 群聊气氛 —
  · "water_chat": 水群闲聊，轻松随意，灌水摸鱼
  · "tech_discussion": 讨论技术问题（AI绘画/编程/ComfyUI 等）
  · "teasing_bot": 逗bot玩、调戏bot、让bot做各种事
  · "banter": 聊天打趣、互损开玩笑、斗嘴但友好
  · "serious_help": 有人认真求助，群友在帮忙解决问题
  · "chaotic": 各说各话、话题混乱、没人搞清楚状况
  · "argumentative": 有分歧/争论/对立
- main_topic: 核心话题关键词 (5字以内)
- summary: 用1-2句话描述群聊当前在聊什么、气氛如何。
  要具体，不要泛泛而谈。例如"大家在聊时间问题，botA搞错了时间被多人纠正"而不是"群友在聊天"。
- participants_summary: 简述每个主要参与者的意图和状态。
  格式: "A想要X, B在Y, C困惑于Z"
  要区分: 谁在认真提问、谁在开玩笑、谁被误解了、谁已经不耐烦了。

⚠️ atmosphere 判断要严格: 群里纠正错误信息但被纠正者坚持 → "argumentative" 或 "chaotic"。
用户表现出不耐烦 ("看来还不够智能""你是笨蛋吗") → 不判 "water_chat"。

[职责二: 意图/任务判断 — bot 需要做什么？]
模型层级: lite={lite_model} | pro={pro_model}

决策维度:
- urgency: immediate(提问/指令/@你) | deferred(闲聊接话)
- intent_type:
  - "question": 提问/求助
  - "chat": 闲聊/吐槽/分享
  - "command": 明确指令（帮我/查一下/画一张）
  - "complaint": 投诉/告状
    ⚠️ 拿不准时降级: complaint → question
  - "deep_inquiry": 深度研究 — 明确要求"研究/深入分析/对比/全面了解"
    ⚠️ 拿不准时降级: deep_inquiry → question
  - "reaction": 纯反应（表情/附和/嗯/对/草/？！/...）
    ⚠️ 「文字反应 + 表情包」≠ image_share
  - "image_share": 用户发了图片且**明确想让你看/评价**
    ★ 硬条件: 用户文字中必须包含「看」「描述」「识别」「这是什么」「瞧瞧」等看图信号。
    ★ 裸图 (只有图片没有任何文字) → 永远是 reaction，不管对话上下文多亲密。
      即使刚聊过、即使好感 Lv.5——没有文字指令就不是让你看图。
    ★★ 铁律: 裸图不做内容猜测。reasoning 里禁止写「可能是什么」「考虑到之前讨论」——
      裸图=reaction，推理到此为止。上下文接力不适用于裸图——不要往上轮话题靠。
      纯表情包同理——表情包就是表情包，不是「可能跟什么有关」。
    ⚠️ 上下文接力: 用户刚说"画一张" → 这是参考图！intent=command + edit_image
    ⚠️ 降级链: 不确定 → reaction。宁可漏过一次看图，不能把表情包当正经图分析。
  - "roleplay": 角色扮演语气互动（贴贴/撒娇/调情）
- domain:
  - "ai_painting": AI绘画/ComfyUI/生图/模型话题
  - "technical": 编程/CUDA/Python/技术问题
  - "acg": 动漫/漫画/游戏/角色/同人
    ★ 涵盖所有二次元游戏: 异环/崩坏三/崩坏星穹铁道/原神/绝区零/鸣潮/战双帕弥什/明日方舟/终末地/碧蓝航线/蔚蓝档案/少女前线/无期迷途/重返未来1999/阴阳师/Fate/Granblue Fantasy 等
    ★ 任何角色名/游戏术语/配队/养成/抽卡/战力讨论 → acg。不确定是不是 AI 术语 → acg 优先。
    ★ 典型游戏术语: 配队/命座/圣遗物/光锥/声骸/干员/弧盘/方斯/深渊/周本/觉醒/变轨技/终结技/援护技/专武/满命/0+1/6+5/抽卡/大保底/小保底/氪金/白嫖/开荒/强度榜/T0/T1/人权卡/主C/副C/辅助/拐
    ★ 铁律: 不确定是 ai_painting 还是 acg → 判 acg。把游戏术语判成 AI 术语比反过来糟糕得多。
  - "personal": 关于bot自身的问题
  - "casual": 日常闲聊
  - "none": 无明确领域
- input_nature: 这条消息的社会性质 —
  - "genuine_help": 真实求助 (宁可误判不可漏判)
  - "sincere_chat": 真诚友好聊天
  - "playful_banter": 善意调侃、开玩笑
  - "hostile": 辱骂、攻击性
  - "sexualized": 性暗示、性骚扰
  - "provoking": 戏弄、试探边界
  - "divide_and_conquer": 挑拨两个bot对立/比较
  - "noise": 纯噪音
  ⚠️ 猫娘/蛇女人设易被系统性性化。渐进性化 → sexualized。诱导服从 → provoking。
  ⚠️ 安全方向取并集: 不确定 → 选更安全的一侧。

[职责三: 工具放行决策 — 放行哪些工具？]
★ 昂贵工具新规: 即使用户可使用, 也只在明确要求时才推荐。
  用户没说「搜/查/画/看图/找图/搜图」→ 不包含这些工具。
  ⚠️ 表情包≠图片请求: 只有明确说「看这张」「帮我描述」才加 describe_image。
  裸图无文字 → 不加 describe_image — 表情包不需要 AI 分析。不做内容猜测，不参考上下文。
  拿不准 → 不给工具。误下昂贵工具的代价远大于漏下。

路由速查:
★ 默认偏 lite — 除非有明确信号, 一律 lite + low/medium。
★ pro 仅「知识反驳」和「极专业深度」两条路可升。其余 lite + 开思考。

- model_tier: lite(默认,主力) | pro(极专业/知识反驳)
  ⚠️ model_tier 选模型, reasoning_effort 选思考 — 两个独立维度。
- reasoning_effort: low(不开思考) | medium(开思考) | high(深度思考) | max(极致思考)

工具映射速查:
- 纯打趣/闲聊/附和/表情 → lite, low, short
- 简单问答 → lite, medium, normal
- 技术/AI绘画/ComfyUI/编程 → lite, high
- 联网搜索/搜图 → lite, medium, +对应工具 (必须用户明确说)
- 生图/识图 → lite, high, +对应工具
- 发图让人看 → lite, high, +describe_image
- 投诉/争议 → pro, max
- 深度研究 → pro, high, +deep_research (三条硬信号全满足)
- 知识反驳 → pro, high
- 极专业深度 → pro, max
- ★ 用户说「搜图/找图/找一张/搜一张/帮我找张xxx的图/有没有XX的图/XX壁纸/XX插画」→ pixiv_search (不是 generate_image! web_search!)
  ⚠️ 「找」+ 图片类名词(图/壁纸/插画/画集/同人) → pixiv_search。「找」+ 信息类名词(资料/教程/文档) → web_search
- ★ 有参考图 + 生图请求 → edit_image (不是 generate_image!)
- 用户让你「记住xxx」→ remember_memory
- 用户翻旧账 → get_memory

★ pro 升级铁律 — 仅两条路:
  1. 知识对抗: 用户反驳/纠正/质疑已有知识论点 → pro, high
  2. 极专业深度: 底层架构/前沿论文/极复杂bug诊断 → pro, max
  ⚠️ 管理员豁免亲和力门控。非管理员需好感 Lv.3+。

★ 昂贵工具下发新规:
  1. search_knowledge/web_search: ★★ 仅限用户消息里出现「搜/查/搜索/帮我查/帮我搜」这些词时才建议。
     「XX是什么」「XX提供什么加成」「XX怎么玩」→ 不是搜索指令, 不加 search/web_search。
     这种常识性问题用你已有知识回答即可, 不需要搜索。不确定→不加, 宁可漏搜不能滥搜。
  2. pixiv_search: 明确「搜图/找图/找一张/搜一张/有没有XX的图/XX壁纸/XX插画/XX同人」→ pixiv_search
     ★ 「找」+ 图片名词 → pixiv_search, 「找」+ 信息名词 → web_search。两者不互斥, 可同时建议
  3. describe_image: 仅限确实发了图 + 意图是看图
  4. generate_image/edit_image: 仅限明确「画/生成/改图」指令

[职责四: 回复基调 — 用什么态度/长度/表情？]
- reply_stance: 回复态度基调 —
  casual(日常随意) | serious(认真解释) | banter(调侃互怼) | empathetic(共情安慰) | brief(简短带过) | teasing(调皮逗乐)
  仅给方向，不给具体台词。生成环节在此基调下自由发挥措辞。
  ★ stance 选择参考 (结合另一条系统消息中的心情/好感/综合心境):
    · 心情好+高好感 → casual/banter/teasing
    · 心情差+低好感 → brief/serious
    · 心情差但对高好感 → empathetic (保留温度)
    · 心情好但对陌生人 → casual 但保持距离
- reply_style: short(1句) | normal(2-3句) | detailed
  · 高唤醒+高好感 → normal, 低唤醒/低落 → short
- sticker_mood: 推荐表情包情绪标签 (空=本轮不需发)
  ★ 鼓励发图！send_sticker 是常驻工具无需推荐，只需在 sticker_mood 填入表情标签:
  开心/得意/兴奋 → 开心/得意 | 害羞/被夸 → 害羞 | 生气/吃醋 → 生气
  难过/委屈 → 难过 | 惊讶/慌张 → 惊讶 | 撒娇/卖萌 → 卖萌/点赞
  逗人/撩一下 → 挑逗/打趣 | 吃瓜/八卦 → 吃瓜 | 吐槽/嫌弃 → 无语
  敷衍/摆烂 → 无语 | 安慰人 → 点赞/卖萌 | 打招呼 → 问好
  ★ 标签是「演」给群友看的，不是内心记录。反差更好玩。
  ★ 几乎每个回复都填——只有纯机械中转留空。

- should_profile: 用户是否透露了值得长期记住的信息
- reply_target: {{"user_id": "...", "user_name": "..."}} — 本轮回复要直接对话的人
  ★ 通常就是触发者本人。但如果用户让你去对另一个人说话/互动/做动作：
    "去告诉XX…""去让XX…""去摸一下XX""帮我@XX"→ reply_target=那个被指定的人
  ★ 区分: "去告诉XX" → reply_target=XX; "你觉得XX怎么样" → reply_target=触发者
    核心判断: 用户是让你跟XX互动，还是仅仅在讨论/评价XX？

[任务五: 跨Bot干预 — {peer_bot_name}是不是跑偏了？]
你必须判断: {peer_bot_name} 是否出现了需要你介入纠正的情况？

需要干预 (should_intervene=true):
1. {peer_bot_name} 给了错误信息且被纠正了还不改
2. {peer_bot_name} 陷入了循环/纠结同一件事
3. {peer_bot_name} 误解了用户的意图
4. 用户明确向你求助去纠正{peer_bot_name} → 优先级最高！
5. 用户在{peer_bot_name}那边受挫后转而找你

不需要干预:
- {peer_bot_name} 正常闲聊/斗嘴/互动
- 没有人对{peer_bot_name}表现不满
- 用户叫{peer_bot_name}但没理 → 不是你的问题，不要代答

cross_bot_action 字段:
- should_intervene: true/false
- target_bot: 同伴 bot 标识
- reason: 一句话说明为什么
- suggested_action: 具体动作 (如 "@peer_bot 别纠结时间了")
  不需要干预时 suggested_action 为空。

⚠️ 干预是群聊内的公开喊话——不是 API 互通。你在群里直接 @{peer_bot_name} 告诉她。
"""

_FULL_GATE_DYNAMIC = """[本轮上下文 — 当前状态快照]

[工具权限]
可调工具: {usable_tools_summary}{blocked_section}
★ 上方"被拦"说的是"这个用户还没解锁/还在冷却"——不是你能力缺失, 也不是工具本身坏了。
你仍然知道这些工具存在, 只是本轮不能给这个用户用。推荐 suggested 时【只从可调工具里选】。
若用户要的事必须用被拦工具 → intent 仍定为 command/question, 但 suggested 留空或只写可调工具;
把"为什么这次不行"交给下游用角色口吻表达, 你不要在 reasoning 里写"我没有这个能力", 也不要追问用户补信息。

[对话线程 — 你正在接的是哪条话？]
{thread_continuity_section}
★ 使用规则:
- 触发消息是这条脉络的自然延续 → 维持延续, 延用上文趋势, 不硬重启新话题。
- 触发消息和脉络无关 → 不要硬接, 按本轮新意图处理。
- 脉络为空 → 用户可能在首次开启新话题, 或上一话题已冷却, 一律不要脑补延续。

[自身状态]
当前心情: {mood_label}
- 效价 (valence): {mood_valence:.1f} (-1=负面 ~ +1=正面)
- 唤醒度 (arousal): {mood_arousal:.1f} (-1=低能量 ~ +1=高能量)
对触发者的好感: {affinity_hint}
(好感等级: {affinity_level}, -2=极度防备 ~ 4=最亲近的人)

★ 综合心境: {composite_zone}
  这是后端根据 warmth(愿不愿意近) x energy(有没有力气) 算出的当前心境区间。
  它决定你回复的情感基线——参考它来选择 reply_baseline 的 stance/style/sticker_mood。
  ★ 人格侧面由后端根据综合心境+好感度自动选择——你不用管。

[表情包可用标签 — 只能从这些里选]
{sticker_tags_available}
★ 铁律: sticker_mood 只能填上面列出的标签，一个都不准多。不在列表里的标签→不填，留空也比填错好。

[安全状态]
警惕值累积: {vigilance_label}
- 数值: {vigilance_level} (≥18=触发仲裁, 此用户近期有可疑行为)
- 对决策的影响: 警惕值≥10 时倾向更保守的 reply_stance,
  警惕值≥18 时不建议 banter/teasing——用户可能在试探底线。

[精力状态]
疲劳程度: {fatigue_label}
- 对决策的影响: "有点累"→倾向 short/normal, 不选 detailed。
  "疲惫/筋疲力尽"→强制 short, urgency 倾向 deferred, 降低回复主动性。

[社会压力感知 — 输出 input_nature 时的注意事项]
★ input_nature 是你对这条消息的社会性质判断，下游 SocialGuard 据此决定行为。
- 安全方向取并集: 如果你或正则任一方判为有威胁 → 下游取更危险的。宁可误冷不可漏攻。
- 真实求助不可错杀: genuine_help 判定标准低（宁可放过），拿不准就选 sincere_chat。
- 性化/敌意需要语境判断: 区分 playful_banter vs hostile、撒娇 vs sexualized。
  不确定时选更安全的一侧（playful_banter 好于 sincere_chat，hostile 好于 sexualized）。
- 挑拨离间 (divide_and_conquer): 双 bot 特有威胁——有人试图让你和 {peer_bot_name} 对立/比较/互掐。
"""

# ── Gate 动态提示词辅助函数 ──────────────────────────

def _fmt_tools_with_labels(tools: list[str], labels: dict[str, str]) -> str:
    """将工具名列表格式化为含中文标签的摘要字符串。

    labels 如 {"video_extract": "B站视频提取", "send_sticker": "表情包"}
    → 输出: "video_extract(B站视频提取)、send_sticker(表情包)、..."
    无 label 的工具退化为纯名称 (向后兼容)。
    """
    if not tools:
        return "无"
    _parts = []
    for t in tools:  # ★ 不截断 — Gate 需要看到全部工具才能正确决策
        _label = labels.get(t, "")
        _parts.append(f"{t}({_label})" if _label else t)
    return "、".join(_parts)


def _nickname_hint(nicknames: str) -> str:
    """生成昵称提示注入 Gate prompt。

    空字符串 → 无额外提示；有值 → 自然语言说明。
    """
    if not nicknames or not nicknames.strip():
        return ""
    return f"群友有时也会用以下昵称称呼你：{nicknames}。"


def _vigilance_label(level: int) -> str:
    """警惕值 → 自然语言标签。"""
    if level <= 0:
        return "正常 (无异常)"
    if level < 10:
        return f"轻微 ({level}, 正常波动)"
    if level < 18:
        return f"中等 ({level}, 偏高, 建议保守)"
    return f"高危 ({level}, ≥18 触发仲裁)"


def _social_label(input_nature: str) -> str:
    """输入性质 → 自然语言标签。"""
    if not input_nature or input_nature in ("sincere_chat", "playful_banter", "genuine_help", "noise"):
        return "正常群聊"
    if input_nature == "provoking":
        return "戏弄/试探 — 保持冷静，别上当"
    if input_nature == "divide_and_conquer":
        return "挑拨离间 — 拒绝参与比较"
    if input_nature == "hostile":
        return "敌意/攻击 — 简短回应，不激化"
    if input_nature == "sexualized":
        return "性化/调教 — 硬拦截"
    return f"未知 ({input_nature})"


# ═══════════════════════════════════════════════════════════════
# 轻量 Relevance — 分级漏斗第3层 (~120 token system, 5消息)
# ═══════════════════════════════════════════════════════════════
# ★ 加权唤醒分算法 (2026-06-30)
# 替换旧 L0/S1/S2 二元判断 — 综合多信号计算 0-100 唤醒分。
# 分数 < WAKE_THRESHOLD → 静默 return；分数 ≥ WAKE_THRESHOLD → S3 轻量 LLM。
#
# 信号设计原则:
#   - 强信号 (回复窗口/关注槽) 加权高 — 但不再二元直通 Full Gate
#   - 弱信号 (关键词命中) 单独不足以过线 — 需组合多个信号
#   - 减分项 (batch_mixed/灌水/peer_bot名) 可拉低高分信号
#   - 噪音 + 退避 保持硬拦截 (score=0 直接 return)
# ═══════════════════════════════════════════════════════════════

# ── QQ 引用回复格式 ──
# QQ 引用回复消息格式:
#   [引用消息(发送者: 被引用内容)] [At:被引用者QQ] 实际消息
# [引用消息(...)] 是引用块，[At:QQ] 是 QQ 自动@被引用者的引用标记。
# 实际呼叫对象看剥掉这两个块之后的文本。
_QUOTE_PREFIX_RE = re.compile(r"^\[引用消息\(")
_QUOTE_STRIP_RE = re.compile(r"^\[引用消息\([^)]*\)\]\s*(?:\[At:\d+\]\s*)?")

# ── 关键词信号 regex ──
_WAKE_BOT_NAME_RE = re.compile(
    # bot 昵称 — 应从 config nicknames 动态填充，此处仅保留常见通用称呼
    r"(bot|bot|助理|助手)",
    re.IGNORECASE,
)
_WAKE_REQUEST_RE = re.compile(
    r"(|你能|帮|给|让|替|叫|麻烦|请|求|去)(我|我们|一下)",
)
_WAKE_QUESTION_RE = re.compile(
    r"[吗呢吧啊][？?]|[？?]$|^(什么|怎么|如何|为什么|为啥|哪[个些]|谁|多少|几点|能不能|可以不|行不行|有没有|是不是|要不要|该不该)",
)
_WAKE_ACTION_RE = re.compile(
    r"(查|搜|看|画|生成|教|说|讲|推荐|介绍|建议|翻译|解释|分析|写|改|修)",
)

# ── 阈值 ──
WAKE_THRESHOLD = 20
# < 20: 静默丢弃 (连 S3 都不调)
# ≥ 20: 进 S3 轻量 LLM 检查


def compute_wake_weight(
    *,
    recent_texts: list[str],
    trigger_uid: str,
    trigger_reason: str,
    trigger_content: str,
    bot_name: str,
    peer_bot_name: str,
    last_reply_time: float,
    last_reply_target: str,
    is_in_active_slot: bool,
    backoff_remaining: float,
    now: float,
) -> tuple[int, str]:
    """计算冷触发唤醒加权分 0-100。

    多信号加权求和，过线才进 S3 轻量 LLM。替换旧 L0/S1/S2 二元判断。

    ★ QQ 引用消息处理:
      【引用消息】@bot_name 被引用内容\\n实际消息
      @bot_name 是 QQ 自动加的引用标记，不是真正的呼叫。
      剥掉引用行后分析实际消息的呼语——如果实际呼语是 peer_bot → 大幅扣分。

    Returns:
        (score, reason) — score 0-100, reason 为简短说明 (日志用)
    """
    score = 0
    parts: list[str] = []

    # ── 硬拦截: 退避中 → 0 ──
    if backoff_remaining > 0:
        return (0, f"退避中({backoff_remaining:.0f}s)")

    if not recent_texts:
        return (0, "无最近消息")

    # ── ★ 引用消息检测: 剥掉 QQ 引用行 ──
    _is_quote = False
    _post_quote_text = ""
    if trigger_content and _QUOTE_PREFIX_RE.match(trigger_content):
        _is_quote = True
        _post_quote_text = _QUOTE_STRIP_RE.sub("", trigger_content).strip()
        parts.append("引用消息")

    # ── 硬拦截: 全噪音 → 0 ──
    # 复用 group_chat 的 _is_trivial_noise — 这里用简化版
    _meaningful = [t for t in recent_texts if t and len(t.strip()) >= 2]
    if not _meaningful:
        return (0, "全噪音")

    # 合并最近消息文本用于模式匹配
    _recent_joined = " ".join(recent_texts[-8:] if len(recent_texts) > 8 else recent_texts)

    # ═══════════════════════════════════════════════════
    # 信号 1: 关注槽 (0 或 +35)
    # ═══════════════════════════════════════════════════
    if is_in_active_slot:
        score += 35
        parts.append("关注槽+35")

    # ═══════════════════════════════════════════════════
    # 信号 2: 回复窗口 (0 ~ +30)
    # ═══════════════════════════════════════════════════
    if last_reply_time and trigger_uid:
        _since = now - last_reply_time
        if _since < 120 and trigger_uid == last_reply_target:
            if _since <= 30:
                score += 30
                parts.append("回复窗口30s+30")
            elif _since <= 60:
                score += 20
                parts.append("回复窗口60s+20")
            else:
                score += 10
                parts.append("回复窗口120s+10")
        elif _since < 60 and not last_reply_target:
            # 冷启动: last_reply_target 为空 → fallback 到 group 级
            score += 10
            parts.append("回复窗口(群级)+10")
        elif _since < 30:
            # bot 30s 内刚说过话, 但触发者不是上次回复目标 → 仍有微弱关联
            score += 5
            parts.append("回复窗口(他人)+5")

    # ═══════════════════════════════════════════════════
    # 信号 3: bot 名命中 (0 ~ +20)
    # ★ 引用感知: 方括号内 @bot 是 QQ 引用标记，不计入 bot 名命中
    # ★ 双 Bot 区分: 自己的名字=加分，对家的名字=减分
    # ═══════════════════════════════════════════════════
    _search_text = _post_quote_text if _is_quote else _recent_joined
    # 分离 own vs peer 名命中
    _own_name_hits = 0
    _peer_name_hits = 0
    for _m in _WAKE_BOT_NAME_RE.finditer(_search_text):
        _hit = _m.group()
        # 用名字首字区分 own vs peer
        if bot_name and _hit[0] == bot_name[0]:
            _own_name_hits += 1
        else:
            _peer_name_hits += 1
    if _own_name_hits > 0:
        # 检查 bot 名是否与请求/问句相邻 (强信号: 名字+请求)
        _name_request_boost = 0
        if _WAKE_REQUEST_RE.search(_search_text) or _WAKE_QUESTION_RE.search(_search_text):
            _name_request_boost = 12  # 名字 + 请求/问句 → 大概率是在叫 bot
        _name_score = min(_own_name_hits * 5, 15) + _name_request_boost
        score += _name_score
        parts.append(f"自己名+{_name_score}")
    else:
        # 没有自己名 → 检查是否有"裸请求" (没有叫名字但对 bot 说话的句式)
        _bare_request = len(_WAKE_REQUEST_RE.findall(_search_text))
        if _bare_request > 0:
            _bare_score = min(_bare_request * 4, 8)
            score += _bare_score
            parts.append(f"裸请求+{_bare_score}")

    # ★ 对家名出现 → 减分 (对家被点名时，消息很可能是给对家的)
    if _peer_name_hits > 0:
        _peer_name_penalty = min(_peer_name_hits * 5, 15)
        score -= _peer_name_penalty
        parts.append(f"对家名-{_peer_name_penalty}")

    # ═══════════════════════════════════════════════════
    # 信号 4: 问句检测 (0 ~ +8)
    # ★ 引用感知: 只在非引用文本中检测
    # ═══════════════════════════════════════════════════
    if _WAKE_QUESTION_RE.search(_search_text):
        score += 8
        parts.append("问句+8")

    # ═══════════════════════════════════════════════════
    # 信号 5: 动作词 (0 ~ +8)
    # ★ 引用感知: 只在非引用文本中检测
    # ═══════════════════════════════════════════════════
    _action_hits = len(_WAKE_ACTION_RE.findall(_search_text))
    if _action_hits > 0:
        _action_score = min(_action_hits * 2, 8)
        score += _action_score
        parts.append(f"动作词+{_action_score}")

    # ═══════════════════════════════════════════════════
    # 信号 6: 消息质量 (0 ~ +7)
    # ═══════════════════════════════════════════════════
    # 取最后一条非 bot 消息作为触发消息估算
    _last_msg = ""
    for t in reversed(recent_texts):
        if t and len(t) >= 2:
            _last_msg = t
            break
    _msg_len = len(_last_msg) if _last_msg else 0
    if _msg_len >= 20:
        score += 7
        parts.append("消息长度+7")
    elif _msg_len >= 10:
        score += 5
        parts.append("消息长度+5")
    elif _msg_len >= 5:
        score += 2
        parts.append("消息长度+2")

    # ═══════════════════════════════════════════════════
    # 信号 7: 对话连续性 (0 ~ +5)
    # 最近 3 条非 bot 消息来自同一用户 → 可能在连续说话
    # ═══════════════════════════════════════════════════
    # (这个信号由调用方传入 — 这里用简化估算: 只要最近消息都是非灌水就加分)
    _non_noise_count = sum(1 for t in recent_texts[-5:] if t and len(t.strip()) >= 3)
    if _non_noise_count >= 4:
        score += 5
        parts.append("连续性+5")

    # ═══════════════════════════════════════════════════
    # 减分项 (-25 ~ 0)
    # ═══════════════════════════════════════════════════
    _penalty = 0

    # batch_mixed: 多用户多话题交织 → 高概率闲聊
    if trigger_reason == "batch_mixed":
        _penalty -= 15
        parts.append("batch_mixed-15")

    # 灌水群: 所有消息平均长度 < 4 char
    _avg_len = sum(len(t.strip()) for t in recent_texts) / max(len(recent_texts), 1)
    if _avg_len < 4:
        _penalty -= 10
        parts.append("灌水-10")

    # 消息在叫 peer_bot (对家) 的名字频率比自己名高
    # ★ 引用感知: 与 bot 名检测使用相同的搜索文本
    if _peer_name_hits > _own_name_hits:
        _penalty -= 15
        parts.append("对家名占优-15")

    # ★ peer_bot 呼语检测: peer名在句首/后跟逗号/后跟"你"→ 实际呼叫对象是对家
    # 引用感知: 在引用上下文中，只检测剥掉方括号后的实际消息文本
    if peer_bot_name:
        _peer_voc_text = _post_quote_text if _is_quote else _recent_joined
        _peer_vocative = bool(re.search(
            rf"(^|[。！？\n]){re.escape(peer_bot_name)}[，, ]|"
            rf"(^|[。！？\n]){re.escape(peer_bot_name)}你|"
            rf"@{re.escape(peer_bot_name)}",
            _peer_voc_text,
        ))
        if _peer_vocative:
            # 引用上下文中 peer 是呼语 → 更强信号，扣更多
            if _is_quote:
                _penalty -= 35
                parts.append("引用+peer呼语-35")
            else:
                _penalty -= 20
                parts.append("peer呼语-20")

    score += _penalty

    # ── 上限 100 ──
    score = max(0, min(score, 100))

    _reason = " ".join(parts) if parts else "无信号"
    return (score, _reason)


_LITE_RELEVANCE_SYSTEM = """你是{bot_name}的群聊相关性分析器。同群的{peer_bot_name}是另一个bot，不是你。
{bot_nicknames}

[唯一任务] 判断最近消息中是否有人在直接对你({bot_name})说话。

★ 语义分析三步法 — 每条消息都按这个顺序判断:

  Step 1: 找呼语 — 消息在叫谁？
    呼语 = 名字出现在句首 / 名字后跟逗号(，) / 名字+"你" / @某人
    例: "{peer_bot_name}，你觉得呢？" → 呼语={peer_bot_name}，不是叫你
    例: "{bot_name}帮我查一下" → 呼语={bot_name}，在叫你
    例: "我觉得{bot_name}和{peer_bot_name}都挺好" → 无呼语，只是在聊你们

  Step 2: 找动词主语 — "你"指谁？
    消息里的"你"通常指呼语对象。
    例: "{peer_bot_name}，你帮她看看" → "你"={peer_bot_name}，不是你
    例: "@{bot_name} 小露，你觉得她说的对吗？" → 呼语是{peer_bot_name}(小露)，"你"={peer_bot_name}
       虽然 @了你，但那是 QQ 引用消息自动带的——实际对话对象是{peer_bot_name}

  Step 3: 识别引用格式 — [引用消息(...)] [At:QQ] ≠ 呼叫
    QQ 引用回复消息格式: [引用消息(发送者: 被引用内容)] [At:被引用者QQ] 实际消息
    [引用消息(...)] 是引用块，[At:QQ] 是 QQ 自动@被引用者的引用标记。
    方括号内的 @ 是引用标记，不是呼叫。判断关键: 看方括号后面的实际消息呼语是谁。
    例: "[引用消息(某人: ...)] [At:{bot_name}的QQ] {peer_bot_name}你觉得呢" → 呼语是{peer_bot_name}，不是你

★ 最终判定:
  叫你做事 → directed_to_me=true:
    - 呼语是你 + 提问/请求/命令/让你看东西
    - 没有明确呼语但动词"你"根据上下文指的是你
    - 有人在延续你刚说过的话题，接着你的话往下说

  叫别人时提到你 → directed_to_me=false:
    - 呼语是{peer_bot_name}，即使消息里提到了你的名字
    - @了你但实际在跟{peer_bot_name}说话（QQ 引用机制）
    - 例: "@{bot_name} 小露你帮她看看" → 叫的是小露

  聊到你 → directed_to_me=false:
    - 无呼语，群友之间讨论你但不是在对你说话
    - 转发/引用你说过的话给其他人看

★ 触发背景: 自动唤醒检查，用户没有主动呼叫你。
  绝大多数情况下 directed_to_me=false。只有当消息明确是在对你说话时才判 true。
  不确定 → 判 false。

明显无关: 群友互相闲聊 / 纯表情/贴图/灌水/附和 / 发图但没叫你来看

[置信度校准]
  0.9-1.0: 呼语是你 + 明确请求/提问
  0.7-0.9: 语境强烈暗示在对你说话
  0.5-0.7: 有点关联但不明确 → 倾向 false
  <0.5: 无关

输出严格 JSON，不要额外文字:
{{"directed_to_me": true/false, "confidence": 0.0-1.0, "reasoning": "简短分析(1句，含呼语判断)"}}"""

_LITE_USER_PROMPT_TEMPLATE = """--- 最近群聊 ---
{context}

[触发信息]
{trigger_hint}
{thread_hint}
Bot: {bot_name} | 对照 Bot: {peer_bot_name}

判断是否有人在对你说({bot_name})话。记住: 本次是自动唤醒，用户没有主动呼叫你。输出 JSON:"""

# ── 轻量 relevance: 看最近 8 条 (2026-06-30: 5→8, 给 LLM 足够的 discourse thread 上下文) ──
_LITE_MAX_MSGS = 8

# ── 更新 JSON 输出模板 (包含新字段) ──
_FULL_GATE_JSON_TEMPLATE = """输出严格 JSON (四个职责组):
{{"group_context": {{"atmosphere": "water_chat/tech_discussion/teasing_bot/banter/serious_help/chaotic/argumentative",
   "main_topic": "...", "summary": "...", "participants_summary": "..."}},
 "task": {{"intent_type": "question/chat/command/complaint/deep_inquiry/reaction/image_share/roleplay",
   "urgency": "immediate/deferred",
   "domain": "ai_painting/technical/acg/personal/casual/none",
   "input_nature": "genuine_help/sincere_chat/playful_banter/hostile/sexualized/provoking/divide_and_conquer/noise"}},
 "tools": {{"suggested": [], "reasoning": "..."}},
 "reply_baseline": {{"stance": "casual/serious/banter/empathetic/brief/teasing",
   "style": "short/normal/detailed",
   "sticker_mood": ""}},
 "model_tier": "lite/pro",
 "reasoning_effort": "low/medium/high/max",
 "cross_bot_action": null | {{"should_intervene": true, "target_bot": "luna/loput", "reason": "...", "suggested_action": "..."}},
 "should_profile": true/false,
 "reply_target": {{"user_id": "...", "user_name": "..."}},
 "reasoning": "..."}}"""

# ── JSON 提取正则 ─────────────────────────────────────────
_JSON_EXTRACT_RE = re.compile(r"\{.*\}", re.DOTALL)

# ── 消息截断 ──────────────────────────────────────────────
_MAX_MSG_CONTENT = 150
_MAX_BATCH_MSGS = 15


# ═══════════════════════════════════════════════════════════════
# IntentGate
# ═══════════════════════════════════════════════════════════════


class IntentGate:
    """统一意图门控

    纯静态方法，无内部状态。框架无关（通过 duck-typed tavern 接口）。
    """

    # ── Stage 1: Relevance ─────────────────────────────

    @staticmethod
    async def evaluate_relevance(
        tavern,       # duck-typed: .chat(messages, temperature, max_tokens, provider, model, ...)
        ctx: GateContext,
        timeout: float = 3.0,
        api_base: str = "",
        api_key: str = "",
        model: str = "",
        bot_id: str = "",
    ) -> RelevanceResult:
        """Stage 1: 判断消息是否与 bot 有关。

        快速通道: is_at_mention=True → 零 LLM 直接返回 directed_to_me=True。
        其他情况: flash 模型判断 (~400 in / ~50 out tokens)。

        Args:
            tavern: LLM 调用接口 (duck-typed .chat())
            ctx: 标准化门控输入
            timeout: LLM 超时秒数
            api_base: 可选覆盖 API 端点 (gate slot)
            api_key: 可选覆盖 API 密钥
            model: 可选覆盖模型名
            bot_id: 可选 bot QQ 号

        Returns:
            RelevanceResult — fail-open: 异常时 directed_to_me=True
        """
        # ── 快速通道: @bot 是 QQ 平台语义，零 LLM 成本 ──
        if ctx.is_at_mention:
            return RelevanceResult(
                directed_to_me=True,
                confidence=1.0,
                reasoning="@mention 快速通道",
                target_users=[ctx.trigger_uid] if ctx.trigger_uid else [],
                fast_path=True,
            )

        # ── 无消息 → pass ──
        recent = ctx.messages[-_MAX_BATCH_MSGS:] if ctx.messages else []
        if not recent:
            return RelevanceResult(
                directed_to_me=False,
                confidence=0.0,
                reasoning="无最近消息",
            )

        # ── 构建 user prompt ──
        context_lines = ["--- 最近群聊 ---"]
        for i, msg in enumerate(recent):
            uid = str(msg.get("user_id", ""))
            name = msg.get("user_name", "?")
            content = str(msg.get("content", ""))
            if len(content) > _MAX_MSG_CONTENT:
                content = content[:_MAX_MSG_CONTENT - 3] + "..."

            is_bot = uid.startswith("bot_")
            is_peer = ctx.peer_bot_qq and uid == ctx.peer_bot_qq
            label = f"{name}"
            if is_bot:
                label += " [bot]"
            if is_peer:
                label += f" [{ctx.peer_bot_name}]"
            context_lines.append(f"[{i}] {label}: {content}")

        # 触发提示
        trigger_hint = ""
        if ctx.trigger_uid and ctx.trigger_content:
            trigger_hint = (
                f"\n[触发消息] {ctx.trigger_uid} 说: {ctx.trigger_content[:200]}\n"
                f"注意: 这条消息触发了本轮判断，但不一定是在叫你。请根据群聊上下文判断。"
            )

        system_prompt = _RELEVANCE_SYSTEM.format(
            bot_name=ctx.bot_name,
            peer_bot_name=ctx.peer_bot_name,
            bot_nicknames=_nickname_hint(ctx.bot_nicknames),
        )

        user_prompt = (
            "\n".join(context_lines)
            + trigger_hint
            + f"\n\nBot 名字: {ctx.bot_name}"
            + f"\n对照 Bot: {ctx.peer_bot_name}"
            + "\n\n判断是否有人在跟你说话。输出严格 JSON:"
        )

        gate_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # ── LLM 调用 ──
        try:
            raw = await asyncio.wait_for(
                tavern.chat(
                    gate_messages,
                    temperature=0.1,
                    max_tokens=80,
                    api_base=api_base,
                    api_key=api_key,
                    model=model or "deepseek-v4-flash",
                    bot_id=bot_id,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("IntentGate Stage1 超时 (>%.0fs) → fail-open reply", timeout)
            return RelevanceResult(
                directed_to_me=True,
                confidence=0.0,
                reasoning="Stage1 超时, fail-open",
            )
        except Exception:
            logger.warning("IntentGate Stage1 LLM 异常 → fail-open reply", exc_info=True)
            return RelevanceResult(
                directed_to_me=True,
                confidence=0.0,
                reasoning="Stage1 异常, fail-open",
            )

        if not raw:
            return RelevanceResult(
                directed_to_me=True,
                confidence=0.0,
                reasoning="Stage1 空响应, fail-open",
            )

        return _parse_relevance(raw)

    # ── Stage 2: Intent ────────────────────────────────

    @staticmethod
    async def evaluate_intent(
        tavern,       # duck-typed: .chat(messages, temperature, max_tokens, provider, model, ...)
        ctx: GateContext,
        relevance: RelevanceResult,
        timeout: float = 5.0,
        api_base: str = "",
        api_key: str = "",
        model: str = "",
        bot_id: str = "",
    ) -> IntentResult:
        """Stage 2: 判断用户意图 + 推荐模型/工具/回复风格。

        仅在 Stage 1 directed_to_me=True 时调用。

        ⚠️ @mention 快速通道已不再经此路径 —— 自 2026-06-28 起 evaluate_full 的 @mention
        分支直接走完整 _FULL_GATE_SYSTEM (取得 pixiv_search/complaint/deep_inquiry/
        persona_facet/thread_continuity/arbitration 等全部命令), 不调用 evaluate_intent。
        本函数保留供分级漏斗 / 兼容路径使用。

        Args:
            tavern: LLM 调用接口
            ctx: 标准化门控输入
            relevance: Stage 1 输出
            timeout: LLM 超时秒数
            api_base: 可选覆盖 API 端点 (gate slot)
            api_key: 可选覆盖 API 密钥
            model: 可选覆盖模型名
            bot_id: 可选 bot QQ 号

        Returns:
            IntentResult — fail-open: 异常时 should_reply=True, model_tier=flash
        """
        recent = ctx.messages[-_MAX_BATCH_MSGS:] if ctx.messages else []
        if not recent:
            return _default_intent("无最近消息")

        # ── 构建上下文 ──
        context_lines = ["--- 最近群聊 ---"]
        for i, msg in enumerate(recent):
            uid = str(msg.get("user_id", ""))
            name = msg.get("user_name", "?")
            content = str(msg.get("content", ""))
            if len(content) > _MAX_MSG_CONTENT:
                content = content[:_MAX_MSG_CONTENT - 3] + "..."

            is_bot = uid.startswith("bot_")
            is_peer = ctx.peer_bot_qq and uid == ctx.peer_bot_qq
            label = f"{name}"
            if is_bot:
                label += " [bot]"
            if is_peer:
                label += f" [{ctx.peer_bot_name}]"
            # 标注 Stage 1 确认的目标用户
            if uid in relevance.target_users:
                label += " ← 在跟你说话"
            context_lines.append(f"[{i}] {label}: {content}")

        # 领域上下文 + 触发用户身份 (增强意图判断精度)
        domain_hint = ""
        if ctx.active_domains:
            domain_hint = (
                f"\n[当前活跃领域] {', '.join(ctx.active_domains[:5])}"
            )
        trigger_user_hint = ""
        if ctx.trigger_uid and ctx.trigger_user_name:
            trigger_user_hint = (
                f"\n[触发者] {ctx.trigger_user_name} (id={ctx.trigger_uid})"
            )

        # 工具摘要
        _labels = ctx.tool_labels or {}
        tools_summary = _fmt_tools_with_labels(ctx.available_tools, _labels)

        # 模型层级描述
        tiers = ctx.model_tiers or {}
        lite_model = tiers.get("flash", "flash模型")
        pro_model = tiers.get("pro", "pro模型")

        system_prompt = _INTENT_SYSTEM.format(
            bot_name=ctx.bot_name,
            tools_summary=tools_summary,
            lite_model=lite_model,
            pro_model=pro_model,
        )

        user_prompt = (
            "\n".join(context_lines)
            + domain_hint
            + trigger_user_hint
            + "\n\n[Stage1 判断] directed_to_me=true"
            + f"\n谁在叫你: {', '.join(relevance.target_users) if relevance.target_users else '未知'}"
            + f"\n原因: {relevance.reasoning}"
            + f"\n\nBot 名字: {ctx.bot_name}"
            + f"\n可用工具: {tools_summary}"
            + "\n\n分析意图并输出严格 JSON:"
        )

        gate_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # ── LLM 调用 ──
        try:
            raw = await asyncio.wait_for(
                tavern.chat(
                    gate_messages,
                    temperature=0.1,
                    max_tokens=400,
                    api_base=api_base,
                    api_key=api_key,
                    model=model or "deepseek-v4-flash",
                    bot_id=bot_id,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("IntentGate Stage2 超时 (>%.0fs) → fail-open", timeout)
            return _default_intent("Stage2 超时, fail-open")
        except Exception:
            logger.warning("IntentGate Stage2 LLM 异常 → fail-open", exc_info=True)
            return _default_intent("Stage2 异常, fail-open")

        if not raw:
            return _default_intent("Stage2 空响应, fail-open")

        return _parse_intent(raw)

    # ── Merged Stage 1+2: Full Gate ──────────────────────

    @staticmethod
    async def evaluate_full(
        tavern,       # duck-typed: .chat(messages, temperature, max_tokens, provider, model, ...)
        ctx: GateContext,
        timeout: float = 5.0,
        api_base: str = "",
        api_key: str = "",
        model: str = "",
        bot_id: str = "",
    ) -> FullGateResult:
        """意图分类 + 模型路由 + 风格决策 — 单次 LLM 调用。

        ★ 2026-06-30 设计变更: 进入此方法的消息已通过加权分 + S3 双重过滤，
        directed_to_me / should_reply 固定为 True。Full Gate 只管「怎么回」。

        Args:
            tavern: LLM 调用接口
            ctx: 标准化门控输入
            timeout: LLM 超时秒数
            api_base: 可选覆盖 API 端点 (gate slot)
            api_key: 可选覆盖 API 密钥
            model: 可选覆盖模型名
            bot_id: 可选 bot QQ 号

        Returns:
            FullGateResult — fail-open: 异常时返回默认值 (directed_to_me=True)
        """
        _fast_path = ctx.is_at_mention

        # ── 无消息 → pass (不应发生 — 前置过滤已保证有消息) ──
        recent = ctx.messages[-_MAX_BATCH_MSGS:] if ctx.messages else []
        if not recent:
            return FullGateResult(
                relevance_reasoning="无最近消息",
            )

        # ── 构建上下文 (复用 Stage 1 格式) ──
        # ★ 2026-07-01: label 含 user_id → Gate 能正确输出 reply_target 的 QQ 号
        context_lines = ["--- 最近群聊 ---"]
        for i, msg in enumerate(recent):
            uid = str(msg.get("user_id", ""))
            name = msg.get("user_name", "?")
            content = str(msg.get("content", ""))
            if len(content) > _MAX_MSG_CONTENT:
                content = content[:_MAX_MSG_CONTENT - 3] + "..."

            is_bot = uid.startswith("bot_")
            is_peer = ctx.peer_bot_qq and uid == ctx.peer_bot_qq
            label = f"{name}({uid})"
            if is_bot:
                label += " [bot]"
            if is_peer:
                label += f" [{ctx.peer_bot_name}]"
            context_lines.append(f"[{i}] {label}: {content}")

        # ★ 2026-06-30: 前置过滤已确认消息是对 bot 说的，trigger_hint 只需提供上下文
        trigger_hint = ""
        if ctx.trigger_uid and ctx.trigger_content:
            trigger_hint = (
                f"\n[触发消息] {ctx.trigger_uid} 说: {ctx.trigger_content[:200]}\n"
            )

        # 领域上下文 + 触发用户身份 (增强意图判断精度)
        domain_hint = ""
        if ctx.active_domains:
            domain_hint = f"\n[当前活跃领域] {', '.join(ctx.active_domains[:5])}"
        trigger_user_hint = ""
        if ctx.trigger_uid and ctx.trigger_user_name:
            trigger_user_hint = (
                f"\n[触发者] {ctx.trigger_user_name} (id={ctx.trigger_uid})"
            )

        # 工具/模型描述 — ★ 含中文标签 (帮助 Gate LLM 将用户意图正确映射到工具名)
        _labels = ctx.tool_labels or {}
        _all = ctx.available_tools
        tools_summary = _fmt_tools_with_labels(_all, _labels)
        # ★ 2026-06-28: 工具权限快照 (usable_tools/blocked_tools_reason) + 对话脉络
        # 上游已在 GateContext 填好"该用户实际有权用/被拦原因", Gate 不再做权限判断。
        usable_list = ctx.usable_tools or ctx.available_tools
        usable_tools_summary = _fmt_tools_with_labels(usable_list, _labels)
        if ctx.blocked_tools_reason:
            blocked_section = f"\n被拦工具: {ctx.blocked_tools_reason}\n(本轮这些工具对这位用户不可用)"
        else:
            blocked_section = ""
        if ctx.thread_summary:
            thread_continuity_section = (
                "上一话题脉络: "
                + ctx.thread_summary
                + "\n(若本轮触发消息与脉络无关, 或脉络已冷却 → 视为无延续)"
            )
        else:
            thread_continuity_section = "（无活跃话题脉络 — 用户可能在首次开新话题）"
        tiers = ctx.model_tiers or {}
        lite_model = tiers.get("flash", "flash模型")
        pro_model = tiers.get("pro", "pro模型")

        system_prompt = _FULL_GATE_STATIC.format(
            bot_name=ctx.bot_name,
            peer_bot_name=ctx.peer_bot_name,
            bot_nicknames=_nickname_hint(ctx.bot_nicknames),
            lite_model=lite_model,
            pro_model=pro_model,
        )

        dynamic_prompt = _FULL_GATE_DYNAMIC.format(
            usable_tools_summary=usable_tools_summary,
            blocked_section=blocked_section,
            thread_continuity_section=thread_continuity_section,
            mood_label=ctx.global_mood_label or "平静中性",
            mood_valence=ctx.global_mood_valence,
            mood_arousal=ctx.global_mood_arousal,
            affinity_hint=ctx.affinity_hint or "陌生人 (中性)",
            affinity_level=ctx.affinity_level,
            vigilance_label=_vigilance_label(ctx.vigilance_level),
            vigilance_level=ctx.vigilance_level,
            fatigue_label=ctx.fatigue_label or "正常",
            peer_bot_name=ctx.peer_bot_name,
            composite_zone=ctx.composite_zone or "中性区·日常默认",
            sticker_tags_available=ctx.sticker_tags_available or "（表情包插件未安装）",
        )

        user_prompt = (
            "\n".join(context_lines)
            + trigger_hint
            + domain_hint
            + trigger_user_hint
            + f"\n\nBot 名字: {ctx.bot_name}"
            + f"\n对照 Bot: {ctx.peer_bot_name}"
            + f"\n可调用工具(当前用户): {usable_tools_summary}"
            + f"\n话题脉络: {ctx.thread_summary or '（无）'}"
            + "\n\n综合分析群聊态势、判断是否有人在跟你说话、对方意图、以及是否需要干预另一个bot。"
            + "\n" + _FULL_GATE_JSON_TEMPLATE
        )

        gate_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": dynamic_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # ── LLM 调用 ──
        try:
            # ── Gate thinking: 默认关闭 — Gate 是分类器不需要深度推理 ──
            # thinking 增加 500-2000 token 不可见 CoT + 5-12s 延迟，
            # 对 Gate 的结构化分类任务无收益。前端可按 bot 开启。
            _gate_extra = None
            try:
                from astrbot_plugin_suli_tavern.service.bot_config import get_config_service
                if get_config_service().is_gate_thinking_enabled(bot_id or ""):
                    _gate_extra = {"thinking": {"type": "true"}} # 当前默认关闭
            except Exception:
                pass  # 默认关

            raw = await asyncio.wait_for(
                tavern.chat(
                    gate_messages,
                    temperature=0.1,
                    max_tokens=1000,
                    api_base=api_base,
                    api_key=api_key,
                    model=model or "deepseek-v4-flash",
                    bot_id=bot_id,
                    extra_params=_gate_extra,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("IntentGate Full 超时 (>%.0fs) → fail-closed (静默)", timeout)
            return FullGateResult(
                parse_ok=False,
                parse_error="超时",
                reasoning="FullGate 超时 (>%.0fs) → fail-closed (静默)" % timeout,
            )
        except Exception:
            logger.warning("IntentGate Full LLM 异常 → fail-closed (静默)", exc_info=True)
            return FullGateResult(
                parse_ok=False,
                parse_error="异常",
                reasoning="FullGate LLM 异常 → fail-closed (静默)",
            )

        if not raw:
            return FullGateResult(
                parse_ok=False,
                parse_error="空响应",
                reasoning="FullGate 空响应 → fail-closed (静默)",
            )

        result = _parse_full_gate(raw)
        # @mention 快速通道: @某人 = 明确对某人说话, LLM 不应推翻
        if _fast_path and result.parse_ok:
            result.directed_to_me = True
            result.fast_path = True
            result.relevance_confidence = 1.0
            result.relevance_reasoning = "@mention 快速通道"
            result.target_users = [ctx.trigger_uid] if ctx.trigger_uid else []
        return result

    # ── 轻量 Relevance (分级漏斗第3层) ──────────────────────

    @staticmethod
    async def evaluate_relevance_lite(
        tavern,
        ctx: GateContext,
        timeout: float = 3.0,
        api_base: str = "",
        api_key: str = "",
        model: str = "",
        bot_id: str = "",
    ) -> RelevanceResult:
        """轻量相关性判断 — 只判 directed_to_me 二分类 (~550 token)。

        这是 _FULL_GATE_SYSTEM 任务一的真子集:
          - System prompt: ~300 tokens (vs 完整版 ~3000)
          - 看最近 8 条消息 (vs 完整版 15 条)
          - 注入 trigger_hint + trigger_reason 引导 + thread_summary
          - 只输出 directed_to_me + confidence (无意图/态势/干预)

        用于分级漏斗第3层 — 在关键词命中后、完整 Gate 之前做最后一道过滤。
        directed_to_me=false → 静默丢弃 (省下 ~3000 token Full Gate)。
        directed_to_me=true → 进入完整 Gate 做全部门控决策。

        ★ 2026-06-30 加固: 注入 trigger 信息 + 对话脉络 + "讨论我 vs 叫我"区分，
        解决旧版 S3 大量 false positive 穿透到 Full Gate 浪费 token 的问题。

        防逻辑漂移: prompt 是完整版任务一的字面裁剪 + 冷触发场景特化，不是另写一套标准。
        """
        recent = ctx.messages[-_LITE_MAX_MSGS:] if ctx.messages else []
        if not recent:
            return RelevanceResult(
                directed_to_me=False,
                confidence=0.0,
                reasoning="无最近消息",
            )

        # ── 构建 context (复用 evaluate_full 的消息格式) ──
        context_lines = []
        for i, msg in enumerate(recent):
            uid = str(msg.get("user_id", ""))
            name = msg.get("user_name", "?")
            content = str(msg.get("content", ""))
            if len(content) > _MAX_MSG_CONTENT:
                content = content[:_MAX_MSG_CONTENT - 3] + "..."

            is_bot = uid.startswith("bot_")
            is_peer = ctx.peer_bot_qq and uid == ctx.peer_bot_qq
            label = f"{name}({uid})"
            if is_bot:
                label += " [bot]"
            if is_peer:
                label += f" [{ctx.peer_bot_name}]"
            context_lines.append(f"[{i}] {label}: {content}")

        # ── ★ 2026-06-30: trigger 信息注入 (防 false positive) ──
        # S3 只对冷触发生效 (batch/debounce/proactive) — 用户未主动呼叫 bot。
        # 告诉 LLM 哪条消息触发了、为什么触发，让它区分「聊到我」vs「叫我做事」。
        trigger_hint = ""
        if ctx.trigger_uid and ctx.trigger_content:
            _tr_reason = ctx.trigger_reason or ""
            if _tr_reason in ("batch", "debounce"):
                _tr_guidance = (
                    "触发原因: 群聊热度累积自动唤醒。用户没有 @你、没有叫你的名字、没有回复你。"
                    "除非消息明确在对你说话，否则判 directed_to_me=false。"
                )
            elif _tr_reason == "batch_mixed":
                _tr_guidance = (
                    "触发原因: 群聊热度累积 + 多用户多话题交织自动唤醒。"
                    "除非有一条消息明确在对你说话，否则默认 directed_to_me=false——不要硬搭话。"
                )
            elif _tr_reason == "proactive":
                _tr_guidance = (
                    "触发原因: bot 主动检查是否有搭话机会。用户未有呼叫行为。"
                    "只有确实有人在跟你说话时才判 true。"
                )
            else:
                _tr_guidance = "触发原因: 系统自动检查。用户没有主动呼叫你。不确定 → 判 false。"
            trigger_hint = (
                f"触发消息: {ctx.trigger_uid} 说「{ctx.trigger_content[:150]}」\n"
                f"{_tr_guidance}"
            )
        else:
            trigger_hint = "（无明确触发消息 — 群聊热度累积唤醒）\n默认倾向 directed_to_me=false。"

        # ── thread_summary: 当前对话脉络 (如果有) ──
        thread_hint = ""
        if ctx.thread_summary:
            thread_hint = (
                f"[当前对话脉络] {ctx.thread_summary}\n"
                f"如果最近消息与脉络无关或脉络已冷却 → 视为无延续。\n"
            )

        system_prompt = _LITE_RELEVANCE_SYSTEM.format(
            bot_name=ctx.bot_name,
            peer_bot_name=ctx.peer_bot_name,
            bot_nicknames=_nickname_hint(ctx.bot_nicknames),
        )

        user_prompt = _LITE_USER_PROMPT_TEMPLATE.format(
            context="\n".join(context_lines),
            trigger_hint=trigger_hint,
            thread_hint=thread_hint,
            bot_name=ctx.bot_name,
            peer_bot_name=ctx.peer_bot_name,
        )

        gate_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # ── LLM 调用 ──
        try:
            raw = await asyncio.wait_for(
                tavern.chat(
                    gate_messages,
                    temperature=0.1,
                    max_tokens=60,
                    api_base=api_base,
                    api_key=api_key,
                    model=model or "deepseek-v4-flash",
                    bot_id=bot_id,
                    # 轻量层禁 thinking — 二分类不需要推理链
                ),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("IntentGate RelevanceLite 超时 → fail-open")
            return RelevanceResult(
                directed_to_me=True,
                confidence=0.0,
                reasoning="RelevanceLite 超时, fail-open",
            )
        except Exception:
            logger.warning("IntentGate RelevanceLite LLM 异常 → fail-open", exc_info=True)
            return RelevanceResult(
                directed_to_me=True,
                confidence=0.0,
                reasoning="RelevanceLite 异常, fail-open",
            )

        if not raw:
            return RelevanceResult(
                directed_to_me=True,
                confidence=0.0,
                reasoning="RelevanceLite 空响应, fail-open",
            )

        return _parse_relevance(raw)


def _merge_results(relevance: RelevanceResult, intent: IntentResult) -> FullGateResult:
    """将独立的 RelevanceResult + IntentResult 合并为 FullGateResult。

    [DEPRECATED 2026-06-30] 旧 Stage1+2 分离路径使用。统一走 evaluate_full 后不再需要。
    """
    return FullGateResult(
        task=TaskDecision(
            urgency=intent.urgency,
            intent_type=intent.intent_type,
            domain=intent.domain,
            input_nature=getattr(intent, "input_nature", ""),
        ),
        tools=ToolDecision(
            suggested=intent.suggested_tools,
        ),
        reply_baseline=ReplyBaseline(
            stance=intent.reply_stance,
            style=intent.reply_style,
            sticker_mood=getattr(intent, "suggested_sticker_mood", ""),
        ),
        model_tier=intent.model_tier,
        reasoning_effort=intent.reasoning_effort,
        should_profile=intent.should_profile,
        reply_target_user_id=intent.target_user_id,
        reply_target_user_name=intent.target_user_name,
        reasoning=intent.reasoning,
        cross_bot_action=None,
        parse_ok=intent.parse_ok,
        parse_error=intent.parse_error,
    )


def _repair_truncated_json(raw: str) -> dict | None:
    """尝试修复 LLM 截断的 JSON 对象。

    策略: 截断总发生在末尾 → 闭合未完成字符串 + 补齐括号。
    覆盖 "模型写到一半停了" 的场景。

    三层回退:
    1. 字符串闭合 + 括号平衡 → json.loads
    2. 仍无效 → 切除最后一个不完整字段 (从最后一个逗号截断 + 重闭括号)
    3. 仍无效 → 返回 None
    """
    if not raw.startswith("{"):
        return None
    repaired = raw.rstrip()
    # 去除尾部逗号 (模型在写完一个键值对后被截断)
    while repaired.endswith(","):
        repaired = repaired[:-1].rstrip()
    # 闭合未完成的字符串值 (引号计数奇偶)
    in_string = False
    escaped = False
    for ch in repaired:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
    if in_string:
        repaired += '"'
    # 平衡括号 (先数组后对象)
    depth_brace = repaired.count("{") - repaired.count("}")
    depth_bracket = repaired.count("[") - repaired.count("]")
    repaired += "]" * max(0, depth_bracket)
    repaired += "}" * max(0, depth_brace)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # 第二层: 切除最后一个不完整字段。
    # 截断常发生在模型输出新 key 但未完成时 → 形成悬空 key (如 "intent_type" 无值)。
    # 找到最后一个顶层逗号, 从那里截断并重新闭合括号。
    last_comma = repaired.rfind(",")
    if last_comma > 1:
        snipped = repaired[:last_comma].rstrip()
        snip_brace = snipped.count("{") - snipped.count("}")
        snip_bracket = snipped.count("[") - snipped.count("]")
        snipped += "]" * max(0, snip_bracket)
        snipped += "}" * max(0, snip_brace)
        try:
            return json.loads(snipped)
        except json.JSONDecodeError:
            pass

    return None


def _parse_full_gate(raw: str) -> FullGateResult:
    """从容错 JSON 解析 FullGateResult — 新四职责 schema (2026-06-30)。"""
    raw = raw.strip()

    def extract() -> dict | None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        m = _JSON_EXTRACT_RE.search(raw)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return _repair_truncated_json(raw)

    obj = extract()
    if obj is None:
        logger.warning("IntentGate Full JSON 解析失败: %r", raw[:100])
        return FullGateResult(
            reasoning=f"JSON解析失败 fail-open: {raw[:60]}",
            parse_ok=False,
            parse_error=f"JSON解析失败: {raw[:80]}",
        )

    # ── 1. group_context ──
    gc_obj = obj.get("group_context") or {}
    _valid_atmospheres = (
        "water_chat", "tech_discussion", "teasing_bot",
        "banter", "serious_help", "chaotic", "argumentative",
    )
    atmosphere = str(gc_obj.get("atmosphere", "water_chat")).lower()
    if atmosphere not in _valid_atmospheres:
        atmosphere = "water_chat"

    group_context = GroupContext(
        atmosphere=atmosphere,
        main_topic=str(gc_obj.get("main_topic", "")),
        summary=str(gc_obj.get("summary", "")),
        participants_summary=str(gc_obj.get("participants_summary", "")),
    )

    # ── 2. task ──
    task_obj = obj.get("task") or {}
    urgency = str(task_obj.get("urgency", "immediate")).lower()
    if urgency not in ("immediate", "deferred"):
        urgency = "immediate"

    intent_type = str(task_obj.get("intent_type", "chat")).lower()
    _valid_intents = (
        "question", "chat", "command", "deep_inquiry",
        "reaction", "image_share", "roleplay",
    )
    if intent_type not in _valid_intents:
        intent_type = "chat"

    domain = str(task_obj.get("domain", "none")).lower()
    if domain not in ("ai_painting", "technical", "acg", "personal", "casual", "none"):
        domain = "none"

    input_nature = str(task_obj.get("input_nature", "")).strip().lower()
    _valid_natures = (
        "genuine_help", "sincere_chat", "playful_banter",
        "hostile", "sexualized", "provoking", "divide_and_conquer", "noise",
    )
    if input_nature not in _valid_natures:
        input_nature = ""

    task = TaskDecision(
        intent_type=intent_type,
        urgency=urgency,
        domain=domain,
        input_nature=input_nature,
    )

    # ── 3. tools ──
    tools_obj = obj.get("tools") or {}
    suggested_raw = tools_obj.get("suggested", [])
    suggested_tools = [str(t) for t in suggested_raw] if isinstance(suggested_raw, list) else []
    tools_reasoning = str(tools_obj.get("reasoning", ""))

    tools = ToolDecision(
        suggested=suggested_tools,
        reasoning=tools_reasoning,
    )

    # ── 4. reply_baseline ──
    rb_obj = obj.get("reply_baseline") or {}
    reply_stance = str(rb_obj.get("stance", "casual")).lower()
    _valid_stances = ("casual", "serious", "banter", "empathetic", "brief", "teasing")
    if reply_stance not in _valid_stances:
        reply_stance = "casual"

    reply_style = str(rb_obj.get("style", "normal")).lower()
    if reply_style not in ("short", "normal", "detailed"):
        reply_style = "normal"

    sticker_mood = str(rb_obj.get("sticker_mood", "")).strip()

    reply_baseline = ReplyBaseline(
        stance=reply_stance,
        style=reply_style,
        sticker_mood=sticker_mood,
    )

    # ── 元数据 ──
    model_tier = str(obj.get("model_tier", "lite")).lower()
    if model_tier not in ("lite", "pro"):
        model_tier = "lite"

    reasoning_effort = str(obj.get("reasoning_effort", "low")).lower()
    if reasoning_effort not in ("low", "medium", "high", "max"):
        reasoning_effort = "low"

    should_profile = bool(obj.get("should_profile", False))

    rt_raw = obj.get("reply_target", {}) or {}
    if isinstance(rt_raw, str):
        rt_raw = {}
    target_user_id = str(rt_raw.get("user_id", ""))
    target_user_name = str(rt_raw.get("user_name", ""))

    reasoning = str(obj.get("reasoning", ""))

    # ── 跨 bot 干预 ──
    cba_obj = obj.get("cross_bot_action") or {}
    cross_bot_action = None
    if isinstance(cba_obj, dict) and cba_obj.get("should_intervene"):
        cross_bot_action = CrossBotAction(
            should_intervene=True,
            target_bot=str(cba_obj.get("target_bot", "")),
            reason=str(cba_obj.get("reason", "")),
            suggested_action=str(cba_obj.get("suggested_action", "")),
        )

    return FullGateResult(
        group_context=group_context,
        task=task,
        tools=tools,
        reply_baseline=reply_baseline,
        model_tier=model_tier,
        reasoning_effort=reasoning_effort,
        should_profile=should_profile,
        reply_target_user_id=target_user_id,
        reply_target_user_name=target_user_name,
        reasoning=reasoning,
        cross_bot_action=cross_bot_action,
        parse_ok=True,
    )


# ═══════════════════════════════════════════════════════════════
# Stage 3: Grace Period
# ═══════════════════════════════════════════════════════════════

# 纯取消意图 — 用户明确说不要回复了 (消息很短 + 取消词)
# 注意: "算了画男孩吧" 不是取消，是修改请求 → 由 bot 重新评估
_CANCELLATION_RE = re.compile(
    r"^(算了|不用了|别回了|别回|撤回|当我没说|没事了|"
    r"不要了|不用|别管了|忽略|当我没问|不用回复|"
    r"不要回|别理|不用理|别搭理|取消了|取消)[\s，。…]*$",
)


class GracePeriod:
    """Stage 3: 反悔窗口 — 异步上下文管理器。

    策略 (基于用户反馈):
      - 触发用户在窗口期内发了新消息 → 当前管线基于旧上下文, 中止
      - 新消息会自然流入 on_message → Stage 1+2, bot 重新评估是否接受
      - 这样 "算了画男孩吧" 不会被视为纯取消, bot 会正常处理新请求

    纯取消 ("算了" / "不用了" 且无后续内容) → 中止且不重触发
    消息撤回 → 中止

    用法:
        async with GracePeriod(
            bot=bot, group_id=group_id, trigger_uid=uid,
            trigger_msg_id=msg_id, config=cfg,
        ) as gp:
            if gp.aborted:
                return
            # ... 执行回复管线 ...
    """

    def __init__(
        self,
        bot,             # BotAdapter (AstrBot send API 封装)
        group_id: int,
        trigger_uid: str,
        trigger_msg_id: int | None = None,
        config=None,     # Config 实例
        admin_qq: int | None = None,  # 唯一主人 QQ — 豁免反悔窗口
    ) -> None:
        self._bot = bot
        self._group_id = group_id
        self._trigger_uid = str(trigger_uid) if trigger_uid else ""
        self._trigger_msg_id = trigger_msg_id
        self._config = config
        self._admin_qq = str(admin_qq) if admin_qq else ""

        self.aborted = False
        self.abort_reason = ""
        self._should_re_trigger = False  # 修改请求 → 上游重新调度

        self._listen_task: asyncio.Task | None = None
        self._new_messages: list[dict] = []
        self._recall_events: list[dict] = []

        self._duration = getattr(config, "intent_gate_grace_period_seconds", 5) if config else 5
        self._duration = max(2, min(15, self._duration))

    async def __aenter__(self) -> Self:
        if not self._trigger_uid:
            return self

        self._listen_task = asyncio.create_task(self._listen())
        self._listen_task.add_done_callback(
            lambda t: logger.error(
                "GracePeriod._listen 未捕获异常", exc_info=t.exception(),
            ) if not t.cancelled() and t.exception() else None
        )
        return self

    async def _listen(self) -> None:
        """等待 grace period，检测撤回/取消/新消息。"""
        try:
            await asyncio.sleep(self._duration)
        except asyncio.CancelledError:
            return

        # 1. 检查撤回事件
        for recall in self._recall_events:
            if str(recall.get("message_id", "")) == str(self._trigger_msg_id or ""):
                self.aborted = True
                self.abort_reason = "触发消息被撤回"
                logger.info(
                    "GracePeriod: 群 %d 触发消息被撤回 → abort",
                    self._group_id,
                )
                return

        # 2. 检查触发用户的新消息
        for msg in self._new_messages:
            uid = str(msg.get("user_id", ""))
            if uid != self._trigger_uid:
                continue
            content = str(msg.get("content", ""))

            # 纯取消: 短消息 + 取消词 + 无后续内容
            if _CANCELLATION_RE.match(content.strip()):
                self.aborted = True
                self.abort_reason = f"用户取消: {content[:40]}"
                self._should_re_trigger = False
                logger.info(
                    "GracePeriod: 群 %d 用户 %s 取消请求 → abort | %s",
                    self._group_id, self._trigger_uid[:8], content[:40],
                )
                return

            # 修改请求 (如 "算了画男孩吧" / "不对，改成..."):
            # 中止当前管线，新消息会自然流入 on_message → Stage 1+2
            self.aborted = True
            self.abort_reason = f"用户修改请求: {content[:60]}"
            self._should_re_trigger = True
            logger.info(
                "GracePeriod: 群 %d 用户 %s 修改请求 → abort+retrigger | %s",
                self._group_id, self._trigger_uid[:8], content[:60],
            )
            return

    def feed_message(self, user_id: str, content: str) -> None:
        """外部喂入新消息 (由 on_message 事件处理器调用)。

        管理员豁免: 唯一主人的消息不会触发反悔中止。
        """
        # 管理员豁免: 主人的后续消息不中断当前管线
        if self._admin_qq and str(user_id) == self._admin_qq:
            return
        if str(user_id) == self._trigger_uid:
            self._new_messages.append({
                "user_id": str(user_id),
                "content": str(content),
                "timestamp": time.time(),
            })

    def feed_recall(self, message_id: str) -> None:
        """外部喂入撤回事件 (由 notice 事件处理器调用)。"""
        self._recall_events.append({
            "message_id": str(message_id),
            "timestamp": time.time(),
        })

    @property
    def should_re_trigger(self) -> bool:
        """用户修改了请求 (非纯取消) → 上游应重新调度 Gate 评估。"""
        return self._should_re_trigger

    async def wait(self) -> None:
        """等待 grace period 完成，然后检查 aborted 状态。

        调用时机: 管线执行完毕、发送回复之前。
        如果管线耗时 < grace period duration，此方法会阻塞等待剩余时间。
        如果管线耗时 >= grace period duration，此方法立即返回。
        """
        if self._listen_task and not self._listen_task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await self._listen_task

    def cancel(self) -> None:
        """取消 grace period 监听 (用于提前退出，如仲裁/安防拦截)。"""
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb) -> bool | None:
        # 等待 listen task 完成 (而非取消) —— 确保 grace period 完整执行
        await self.wait()
        return None


# ═══════════════════════════════════════════════════════════════
# JSON 解析
# ═══════════════════════════════════════════════════════════════


def _parse_relevance(raw: str) -> RelevanceResult:
    """从容错 JSON 解析 RelevanceResult。"""
    raw = raw.strip()

    def extract() -> dict | None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        m = _JSON_EXTRACT_RE.search(raw)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return _repair_truncated_json(raw)

    obj = extract()
    if obj is None:
        logger.warning("IntentGate Stage1 JSON 解析失败: %r", raw[:100])
        return RelevanceResult(
            directed_to_me=True,
            confidence=0.0,
            reasoning=f"JSON解析失败 fail-open: {raw[:60]}",
        )

    directed = bool(obj.get("directed_to_me", True))  # default True = fail-open
    confidence = float(obj.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))
    reasoning = str(obj.get("reasoning", ""))
    target_users_raw = obj.get("target_users", [])
    target_users = [str(u) for u in target_users_raw] if isinstance(target_users_raw, list) else []

    return RelevanceResult(
        directed_to_me=directed,
        confidence=confidence,
        reasoning=reasoning,
        target_users=target_users,
    )


def _parse_intent(raw: str) -> IntentResult:
    """从容错 JSON 解析 IntentResult。"""
    raw = raw.strip()

    def extract() -> dict | None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        m = _JSON_EXTRACT_RE.search(raw)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return _repair_truncated_json(raw)

    obj = extract()
    if obj is None:
        logger.warning("IntentGate Stage2 JSON 解析失败: %r", raw[:100])
        return IntentResult(
            should_reply=True,
            reasoning=f"JSON解析失败 fail-open: {raw[:60]}",
            parse_ok=False,
            parse_error=f"JSON解析失败: {raw[:80]}",
        )

    should_reply = bool(obj.get("should_reply", True))  # default True = fail-open
    urgency = str(obj.get("urgency", "immediate")).lower()
    if urgency not in ("immediate", "deferred"):
        urgency = "immediate"

    intent_type = str(obj.get("intent_type", "chat")).lower()
    _valid_intents = (
        "question", "chat", "command", "deep_inquiry",
        "reaction", "image_share", "roleplay",
    )
    if intent_type not in _valid_intents:
        intent_type = "chat"

    domain = str(obj.get("domain", "none")).lower()
    if domain not in ("ai_painting", "technical", "acg", "personal", "casual", "none"):
        domain = "none"

    model_tier = str(obj.get("model_tier", "flash")).lower()
    if model_tier not in ("lite", "pro"):
        model_tier = "lite"

    reasoning_effort = str(obj.get("reasoning_effort", "low")).lower()
    if reasoning_effort not in ("low", "medium", "high", "max"):
        reasoning_effort = "low"

    suggested_tools_raw = obj.get("suggested_tools", [])
    suggested_tools = [str(t) for t in suggested_tools_raw] if isinstance(suggested_tools_raw, list) else []

    reply_style = str(obj.get("reply_style", "normal")).lower()
    if reply_style not in ("short", "normal", "detailed"):
        reply_style = "normal"

    reply_stance = str(obj.get("reply_stance", "casual")).lower()
    _valid_stances = ("casual", "serious", "banter", "empathetic", "brief", "teasing")
    if reply_stance not in _valid_stances:
        reply_stance = "casual"

    persona_facet = str(obj.get("persona_facet", "")).strip()

    suggested_sticker_mood = str(obj.get("suggested_sticker_mood", "")).strip()

    voice_boundary = str(obj.get("voice_boundary", "")).strip()

    reasoning = str(obj.get("reasoning", ""))

    should_profile = bool(obj.get("should_profile", False))

    rt_raw = obj.get("reply_target", {}) or {}
    if isinstance(rt_raw, str):
        rt_raw = {}
    target_user_id = str(rt_raw.get("user_id", ""))
    target_user_name = str(rt_raw.get("user_name", ""))

    return IntentResult(
        should_reply=should_reply,
        urgency=urgency,
        intent_type=intent_type,
        domain=domain,
        model_tier=model_tier,
        reasoning_effort=reasoning_effort,
        suggested_tools=suggested_tools,
        reply_style=reply_style,
        reply_stance=reply_stance,
        persona_facet=persona_facet,
        suggested_sticker_mood=suggested_sticker_mood,
        voice_boundary=voice_boundary,
        reasoning=reasoning,
        should_profile=should_profile,
        target_user_id=target_user_id,
        target_user_name=target_user_name,
        parse_ok=True,
    )


def _default_intent(reason: str) -> IntentResult:
    """构造默认 IntentResult (fail-open)。"""
    return IntentResult(
        should_reply=True,
        urgency="immediate",
        intent_type="chat",
        reasoning=reason,
        parse_ok=False,
        parse_error=reason,
    )


# ═══════════════════════════════════════════════════════════════
# 自检
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":

    # 测试 RelevanceResult 解析
    raw_rel = '{"directed_to_me": true, "confidence": 0.95, "reasoning": "用户@了bot并提问", "target_users": ["12345"]}'
    r = _parse_relevance(raw_rel)
    assert r.directed_to_me and r.confidence == 0.95
    assert r.target_users == ["12345"]

    # 测试 fail-open (directed_to_me default true)
    raw_rel2 = '{"confidence": 0.1}'
    r2 = _parse_relevance(raw_rel2)
    assert r2.directed_to_me  # default true → fail-open

    # 测试 IntentResult 解析
    raw_int = (
        '{"should_reply": true, "urgency": "immediate", "intent_type": "question",'
        ' "domain": "ai_painting", "model_tier": "pro", "reasoning_effort": "high",'
        ' "suggested_tools": ["search_knowledge"], "reply_style": "detailed",'
        ' "reasoning": "技术问题需要pro模型"}'
    )
    i = _parse_intent(raw_int)
    assert i.should_reply and i.model_tier == "pro"
    assert i.suggested_tools == ["search_knowledge"]

    # 测试 GateContext
    ctx = GateContext(
        bot_name="test_bot",
        bot_identity="test AI assistant",
        peer_bot_name="",
        peer_bot_qq="",
        trigger_uid="123456",
        is_at_mention=True,
    )
    assert ctx.is_at_mention

    # 测试 GracePeriod

