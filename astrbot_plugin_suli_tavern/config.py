"""L-Port 酒馆桥接插件 — 群聊自然对话配置。

AstrBot 插件配置 — 群聊自然对话参数。可通过环境变量覆盖默认值。
"""

from pydantic import BaseModel, Field


class Config(BaseModel):
    """群聊自然对话配置。所有字段均有默认值，按需覆盖。"""

    # 预置白名单: 启动时自动启用这些群 (种子值，不覆盖运行时变更)
    group_chat_whitelist: list[int] = Field(
        default_factory=list,
        description="启动时自动启用自然对话的群号列表 (运行时变更不会写回)",
    )

    # 静默触发: N 秒内无新消息则触发 LLM 决策
    group_chat_debounce_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="静默多少秒后触发 LLM 决策是否发言",
    )

    # 批量触发: 累积 M 条消息则立即触发
    group_chat_batch_size: int = Field(
        default=12,
        ge=2,
        le=50,
        description="累积多少条消息触发 LLM 决策",
    )

    # 昵称唤醒: 消息中包含这些关键词 → 视为 @提及 立即触发
    group_chat_nicknames: list[str] = Field(
        default_factory=list,
        description="消息中包含这些昵称时立即触发回复（为空时从角色卡 nicknames 字段读取）",
    )

    # 上下文窗口: 最多保留 N 条群聊消息
    group_chat_max_context: int = Field(
        default=30,
        ge=5,
        le=100,
        description="群聊上下文最多保留消息条数",
    )

    # 冷却: 两次发言最短间隔 (秒)，防止刷屏
    group_chat_cooldown_seconds: int = Field(
        default=60,
        ge=10,
        le=600,
        description="两次发言最短间隔 (秒)",
    )

    # 群聊回复最大 token (短于角色扮演的 512)
    group_chat_max_tokens: int = Field(
        default=96,
        ge=32,
        le=512,
        description="群聊回复最大 token 数 (闲聊场景基值, Intent Judge 检测到问答/指令时自动提升至 192+)",
    )

    # 群聊温度 (略低于角色扮演的 0.9，减少随机性)
    group_chat_temperature: float = Field(
        default=0.7,
        ge=0.1,
        le=2.0,
        description="群聊回复温度 (略低以减少发散, 配合 token 约束产生短回复)",
    )

    # ── 温度多样性 ──
    temperature_variation_enabled: bool = Field(
        default=True,
        description="基于上下文调制 LLM 温度 (意图/情绪/随机 jitter)",
    )
    temperature_variation_range: float = Field(
        default=0.15,
        ge=0.0,
        le=0.3,
        description="温度 jitter 振幅 (± 围绕基温)",
    )

    # 权限控制: 只有群主/管理员能开关
    group_chat_admin_only: bool = Field(
        default=True,
        description="是否仅群主/管理员能开关群聊自然对话",
    )

    # ── 上下文压缩 (P0) ──────────────────────────────

    # 超过此消息数触发 LLM 压缩 (替换直接截断)
    group_chat_compress_threshold: int = Field(
        default=20,
        ge=10,
        le=50,
        description="消息数超过此值触发上下文压缩",
    )

    # 压缩后保留最近 N 条原文消息
    group_chat_compress_keep_recent: int = Field(
        default=10,
        ge=5,
        le=30,
        description="压缩后保留的最近消息条数",
    )

    # 压缩 LLM 温度 (低温度保证摘要稳定)
    group_chat_compress_temperature: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="上下文压缩 LLM 温度",
    )

    # ── 热度状态机 (P1) ──────────────────────────────

    # 热度半衰期: 每秒衰减因子
    heat_half_life_seconds: float = Field(
        default=300.0,
        ge=60.0,
        le=3600.0,
        description="群聊热度半衰期 (秒)",
    )

    # 热度活跃阈值: 超过此值 bot 倾向参与
    heat_active_threshold: float = Field(
        default=2.0,
        ge=0.5,
        le=10.0,
        description="热度超过此值 bot 更愿意说话",
    )

    # 回复延迟: 模拟「打字中」的随机等待
    heat_reply_delay_min: float = Field(
        default=1.5,
        ge=0.0,
        le=10.0,
        description="回复前最小延迟 (秒)",
    )

    heat_reply_delay_max: float = Field(
        default=4.0,
        ge=0.0,
        le=15.0,
        description="回复前最大延迟 (秒)",
    )

    # ── 能量疲劳值 (P1.5, from Heartflow) ───────────────

    # 是否启用能量系统
    energy_enabled: bool = Field(
        default=True,
        description="是否启用 bot 能量疲劳值系统 (回复消耗/静默恢复/隔日补贴)",
    )

    # 每次回复消耗的能量 (0.1 = 10%)
    energy_decay_per_reply: float = Field(
        default=0.1,
        ge=0.01,
        le=0.5,
        description="每次回复消耗的能量比例 (0.1 = 最多连续10次回复后触底)",
    )

    # 每分钟自然恢复的能量 (0.004/min ≈ 0.02/5min ≈ 满格需 ~4h)
    energy_recovery_per_minute: float = Field(
        default=0.004,
        ge=0.001,
        le=0.05,
        description="每分钟自然恢复的能量 (默认 0.004, 约4小时从0回满)",
    )

    # 隔日重置补贴
    energy_daily_bonus: float = Field(
        default=0.2,
        ge=0.0,
        le=0.5,
        description="每日跨天时能量补贴 (模拟'睡了一觉')",
    )

    # ── Bot 自传体经历记忆 ────────────────────────────

    # 是否启用 bot 自传体经历记忆 (主语: bot 自己经历过什么)
    bot_experience_enabled: bool = Field(
        default=True,
        description="是否启用 bot 自传体经历记忆 (per-bot、跨群有效)",
    )

    # 同一群经历提取冷却 (秒)
    bot_experience_extract_cooldown: int = Field(
        default=1800,
        ge=300,
        le=86400,
        description="同一群经历提取最小间隔 (秒), 默认30分钟",
    )

    # 近期层最多保留条目
    bot_experience_max_recent: int = Field(
        default=100,
        ge=20,
        le=500,
        description="近期经历层最多保留多少条事件",
    )

    # 核心层最多保留条目
    bot_experience_max_core: int = Field(
        default=20,
        ge=5,
        le=50,
        description="核心经历层最多保留多少条自传片段",
    )

    # 近期积累多少条后触发向核心蒸馏
    bot_experience_distill_threshold: int = Field(
        default=30,
        ge=10,
        le=100,
        description="近期经历积累到此数量触发向核心蒸馏",
    )

    # 蒸馏冷却 (秒)
    bot_experience_distill_cooldown: int = Field(
        default=86400,
        ge=3600,
        le=604800,
        description="经历蒸馏最小间隔 (秒), 默认1天",
    )

    # ── 用户记忆 (P2) ─────────────────────────────────

    # 是否启用用户记忆提取
    user_memory_enabled: bool = Field(
        default=True,
        description="是否启用用户长期记忆",
    )

    # 同一用户记忆提取冷却 (秒)
    user_memory_extract_cooldown: int = Field(
        default=1800,
        ge=300,
        le=86400,
        description="同一用户记忆提取最小间隔 (30分钟)",
    )

    # 每用户最多记忆条数 (daily 层)
    user_memory_max_facts: int = Field(
        default=50,
        ge=10,
        le=200,
        description="每用户最多保留多少条 daily 记忆",
    )

    # 记忆衰减半衰期 (秒) — daily 层
    user_memory_decay_half_life: int = Field(
        default=604800,
        ge=86400,
        le=2592000,
        description="daily 记忆重要性衰减半衰期 (秒)，默认7天",
    )

    # 每次注入记忆最多几条 (daily 层 per user)
    user_memory_search_top_n: int = Field(
        default=5,
        ge=1,
        le=10,
        description="每次对话注入 daily 记忆最多几条 (per user)",
    )

    # ── 三层记忆: Core 层 ─────────────────────────────

    # 每用户 core 记忆上限
    memory_core_max_facts: int = Field(
        default=30,
        ge=5,
        le=100,
        description="每用户最多保留多少条 core 长期特征",
    )

    # daily 积累多少条后触发 core 蒸馏
    memory_core_distill_threshold: int = Field(
        default=10,
        ge=5,
        le=50,
        description="daily 记忆积累到此数量触发向 core 蒸馏",
    )

    # core 蒸馏冷却 (秒) — 防止频繁蒸馏
    memory_core_distill_cooldown: int = Field(
        default=86400,
        ge=3600,
        le=604800,
        description="同一用户 core 蒸馏最小间隔 (秒)，默认1天",
    )

    # ── 情感系统 (Phase F) ──────────────────────────────

    # 是否启用双轨情感系统 (短期情绪 + 长期好感)
    emotion_enabled: bool = Field(
        default=True,
        description="是否启用情感曲线系统 (双 bot 总开关)",
    )

    # peer bot 是否启用情感系统 — emotion_enabled 为 False 时此字段无效
    emotion_enabled_peer: bool = Field(
        default=True,
        description="对照 bot (peer_bot_qq) 是否启用情感系统",
    )

    # 愉悦度半衰期 (秒) — 向基线回归的速度
    emotion_valence_half_life: float = Field(
        default=1800.0,
        ge=300.0,
        le=7200.0,
        description="愉悦度半衰期 (秒)，默认30分钟",
    )

    # 唤醒度半衰期 (秒)
    emotion_arousal_half_life: float = Field(
        default=600.0,
        ge=120.0,
        le=3600.0,
        description="唤醒度半衰期 (秒)，默认10分钟",
    )

    # 愉悦度基线
    emotion_valence_baseline: float = Field(
        default=0.3,
        ge=-1.0,
        le=1.0,
        description="愉悦度基线 (默认轻微正向 +0.3)",
    )

    # ── 工具系统 (Phase B) ──────────────────────────────

    # 是否启用 function calling 工具
    tool_calling_enabled: bool = Field(
        default=True,
        description="是否启用 LLM function calling 工具系统",
    )

    # 工具调用最大轮数 (防止死循环)
    # 注意: 最后一轮强制 tool_choice="none" 保证文本合成, 实际工具轮数 = 此值
    tool_call_max_rounds: int = Field(
        default=10,
        ge=2,
        le=20,
        description="function calling 最多执行几轮工具调用 (不含最后一轮合成)",
    )

    # 单个工具调用超时 (秒)
    tool_call_timeout: float = Field(
        default=10.0,
        ge=2.0,
        le=30.0,
        description="单个工具调用超时秒数",
    )

    # L-Port API 基础地址
    lport_api_base_url: str = Field(
        default="http://127.0.0.1:5000",
        description="L-Port 后端 API 地址",
    )

    # ── 领域检测 (Phase A) ──────────────────────────────

    # 是否启用知识领域检测
    domain_detection_enabled: bool = Field(
        default=True,
        description="是否启用关键词领域检测",
    )

    # 领域分数超过此值 → 注入领域 system prompt
    domain_active_threshold: float = Field(
        default=2.0,
        ge=0.5,
        le=10.0,
        description="领域分数超过此值视为活跃话题",
    )

    # 领域分数半衰期 (比热度衰减快，话题切换更敏感)
    domain_half_life_seconds: float = Field(
        default=120.0,
        ge=30.0,
        le=600.0,
        description="领域分数半衰期 (秒)",
    )

    # 领域命中时触发门槛乘数 (越低越容易触发)
    domain_trigger_boost: float = Field(
        default=0.7,
        ge=0.3,
        le=1.0,
        description="领域活跃时触发门槛的乘数 (0.7 = 门槛降低30%)",
    )

    # ── 知识库 → 网页兜底 (Phase C) ───────────────────

    # 知识库无结果时是否自动尝试网页搜索
    kb_web_search_fallback: bool = Field(
        default=True,
        description="知识库无结果时是否自动 fallback 到网页搜索",
    )

    # 网页搜索最大返回结果数
    web_search_max_results: int = Field(
        default=10,
        ge=1,
        le=10,
        description="网页搜索最多返回多少条结果",
    )

    # ── 交叉验证 (Phase D) ────────────────────────────

    # 是否启用用户质疑时的交叉验证
    cross_validation_enabled: bool = Field(
        default=True,
        description="是否启用用户质疑 bot 回答时的交叉验证",
    )

    # 交叉验证 LLM 温度 (低温度保证裁判一致性)
    cross_validation_temperature: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="交叉验证裁判 LLM 温度",
    )

    # 交叉验证冷却 (秒)
    cross_validation_cooldown: int = Field(
        default=60,
        ge=10,
        le=600,
        description="同一群交叉验证最小间隔 (秒)",
    )

    # ── 主动对话 (Proactive) ────────────────────────────

    # 是否启用主动发言 (默认关，手动开启)
    proactive_enabled: bool = Field(
        default=False,
        description="是否启用主动发言（冷场破冰）",
    )

    # 群静默多久后考虑主动发言 (秒)
    proactive_silence_threshold: int = Field(
        default=900,
        ge=300,
        le=3600,
        description="群静默多少秒后考虑主动发言 (默认15分钟)",
    )

    # 主动发言后最少间隔 (秒)
    proactive_cooldown: int = Field(
        default=1800,
        ge=600,
        le=7200,
        description="两次主动发言最短间隔 (默认30分钟)",
    )

    # 满足条件时实际发言概率
    proactive_chance: float = Field(
        default=0.3,
        ge=0.05,
        le=1.0,
        description="满足所有条件时实际主动发言的概率 (默认30%)",
    )

    # ── 串行化触发合并 ─────────────────────────────

    # 触发信息在合并等待队列中的最大存活时间 (秒)
    trigger_timeout_seconds: int = Field(
        default=30,
        ge=5,
        le=120,
        description="合并等待中的触发信息超过此秒数后丢弃，防止响应过期群消息",
    )

    # ── 并发控制 ─────────────────────────────────────

    # 全局最大并发 LLM API 调用数 (群聊 + 私聊共享)
    max_concurrent_llm_calls: int = Field(
        default=5,
        ge=1,
        le=20,
        description="全局最大并发 LLM API 调用数",
    )

    # ── 模型路由 ─────────────────────────────────────

    # 各 tier 对应的模型名 (传给 SillyTavern)
    model_router_flash: str = Field(
        default="deepseek-v4-pro",
        description="闲聊默认模型 (Tier 1)",
    )
    model_router_pro: str = Field(
        default="deepseek-v4-pro",
        description="专业/推理模型 (Tier 2)",
    )
    # 私聊是否默认用 pro 模型
    model_router_private_default_pro: bool = Field(
        default=True,
        description="私聊是否默认使用 pro 模型 (关闭则私聊也用 flash)",
    )

    # ── Prompt Interceptor 管道 ────────────────────────

    # 是否启用 Prompt Interceptor (语气/风格变量计算 + 阻尼平滑)
    prompt_interceptor_enabled: bool = Field(
        default=True,
        description="是否启用 Prompt Interceptor 管道",
    )

    # Interceptor 阻尼力度 (0 = 无平滑, 1 = 完全平滑)
    prompt_interceptor_damping: float = Field(
        default=0.65,
        ge=0.0,
        le=1.0,
        description="Interceptor 阻尼平滑力度 (越高越平滑)",
    )

    # ── 统一意图门控 (3-Stage Intent Gate) ───────────────
    #     替代旧 reply_gate + mention_intent_gate + intent_judge 分散门控
    #     Stage 1: Relevance — "跟我有关吗？"
    #     Stage 2: Intent    — "用户想干嘛？走哪个模型？"
    #     Stage 3: Grace     — "等5秒，用户反悔了吗？"

    # 是否启用统一意图门控
    intent_gate_enabled: bool = Field(
        default=True,
        description="是否启用 3-Stage 统一意图门控 (替代旧分散门控)",
    )

    # Stage 1: Relevance Gate 超时 (秒)
    intent_gate_relevance_timeout: float = Field(
        default=5.0,
        ge=1.0,
        le=10.0,
        description="Stage 1 Relevance Gate LLM 超时秒数",
    )

    # Stage 2: Intent Gate 超时 (秒)
    intent_gate_intent_timeout: float = Field(
        default=15.0,
        ge=3.0,
        le=30.0,
        description="Stage 2 Intent Gate (合并 Full Gate) LLM 超时秒数",
    )

    # Stage 3: Grace Period 等待时长 (秒)
    intent_gate_grace_period_seconds: int = Field(
        default=5,
        ge=2,
        le=15,
        description="Stage 3 反悔窗口等待秒数 — 用户撤回/说'算了'则取消回复",
    )

    # ── [DEPRECATED] LLM 门控 (reply gate) — 已合并入 intent_gate ──
    reply_gate_enabled: bool = Field(
        default=True,
        description="[DEPRECATED] 已合并入 intent_gate_enabled，保留字段兼容旧配置",
    )
    reply_gate_temperature: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="[DEPRECATED] 已合并入 intent_gate",
    )

    # ── 活跃度调控 (talkativeness, from SillyTavern) ──────

    # 群聊活跃度: 控制 bot 在未被点名时主动插话的概率
    # 0.0 = Shy (仅回应点名), 0.5 = Default (适中), 1.0 = Chatty (每次都回)
    # 仅在 batch/debounce 等非点名触发时生效, 点名/mention 无视此参数
    group_chat_talkativeness: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="群聊活跃度 — bot 在未被点名时主动插话的概率 (SillyTavern talkativeness 机制)",
    )

    # ── [DEPRECATED] 提及意图门控 (mention intent gate) — 已合并入 intent_gate ──
    mention_intent_gate_enabled: bool = Field(
        default=True,
        description="[DEPRECATED] 已合并入 intent_gate_enabled，保留字段兼容旧配置",
    )

    # ── Pre-flight 上下文分析 ─────────────────────────────

    # 是否启用 Pre-flight 上下文分析 (复杂度评分 + 工具推荐 + 预收集)
    preflight_enabled: bool = Field(
        default=True,
        description="是否启用 Pre-flight 上下文分析",
    )

    # 复杂度超过此值才执行上下文预收集
    preflight_collect_threshold: float = Field(
        default=2.0,
        ge=0.5,
        le=8.0,
        description="Pre-flight 收集阈值，复杂度低于此值跳过收集",
    )

    # Pre-flight 单个工具调用冷却 (秒)
    preflight_tool_cooldown: int = Field(
        default=30,
        ge=10,
        le=300,
        description="同一工具 Pre-flight 调用最小间隔 (秒)",
    )

    # 是否将 Pre-flight 收集结果注入 prompt
    preflight_inject_context: bool = Field(
        default=True,
        description="是否将预收集的上下文注入 LLM prompt",
    )

    # ── 对话连续性 ────────────────────────────────────

    # 对话线程时间窗口: bot回复后多少秒内同一用户发言视为延续
    thread_window_seconds: int = Field(
        default=120,
        ge=30,
        le=600,
        description="bot回复后多少秒内同一用户的新消息视为对话延续",
    )

    # 对话线程消息窗口: bot回复后多少条消息内同一用户发言视为延续
    thread_window_messages: int = Field(
        default=8,
        ge=2,
        le=20,
        description="bot回复后多少条消息内同一用户的新消息视为对话延续",
    )

    # ── Profile Agent ──────────────────────────────────

    # 是否启用异步用户建档
    profile_agent_enabled: bool = Field(
        default=True,
        description="是否启用 Profile Agent 异步用户建档",
    )

    # 同一用户建档冷却 (分钟)
    profile_agent_cooldown_minutes: int = Field(
        default=30,
        ge=10,
        le=1440,
        description="同一用户两次建档最小间隔 (分钟)",
    )

    # ── 轻量建档 (Lightweight Profile) ─────────────────────

    # 是否启用轻量建档 (batch/debounce 触发时对活跃用户贴标签)
    lightweight_profile_enabled: bool = Field(
        default=True,
        description="是否启用批量轻量建档 (让每个活跃用户都有档案)",
    )

    # 轻量建档冷却 (分钟)
    lightweight_profile_cooldown_minutes: int = Field(
        default=30,
        ge=10,
        le=1440,
        description="同一用户两次轻量建档最小间隔 (分钟)",
    )

    # 单批轻量建档最多几个用户
    lightweight_profile_batch_max_users: int = Field(
        default=5,
        ge=1,
        le=20,
        description="单批轻量建档最多处理几个用户",
    )

    # ── 防恶意调教 (Grooming Guard) ────────────────────────

    # 是否启用恶意调教检测 (角色越狱/身份篡改/诱导违规)
    grooming_guard_enabled: bool = Field(
        default=True,
        description="是否启用防恶意调教检测",
    )

    # 自动黑名单阈值: 累计多少次恶意调教后锁定好感
    grooming_auto_blacklist_threshold: int = Field(
        default=5,
        ge=1,
        le=20,
        description="累计恶意调教多少次后自动黑名单 (Lv.-2)",
    )

    # 锁工具阈值: 累计多少次后禁止工具调用 (Lv.-1)
    grooming_lock_tools_threshold: int = Field(
        default=3,
        ge=1,
        le=20,
        description="累计恶意调教多少次后禁止工具调用 (Lv.-1)",
    )

    # ── 群聊总结 (Group Summarizer) ──────────────────────────

    # 是否启用群聊定期总结
    group_summary_enabled: bool = Field(
        default=True,
        description="是否启用群聊定期总结 (subagent 异步生成)",
    )

    # 消息数阈值: 累积多少条后触发总结
    group_summary_message_threshold: int = Field(
        default=100,
        ge=20,
        le=1000,
        description="累积多少条消息后触发群聊总结",
    )

    # 时间阈值: 距离上次总结多久后触发 (秒)
    group_summary_time_threshold: int = Field(
        default=10800,
        ge=1800,
        le=86400,
        description="距离上次总结多久后强制触发 (秒), 默认3小时",
    )

    # ── 防滥用闸 (Layer 0) ──────────────────────────────

    # 单用户令牌桶速率 (条/秒) — 超限消息静默丢弃
    abuse_user_rate_per_second: float = Field(
        default=0.5,
        ge=0.1,
        le=5.0,
        description="单用户每秒允许的消息数 (令牌桶补充速率)",
    )

    # 单用户突发容量 — 短时间内允许的突发消息数
    abuse_user_burst_per_minute: int = Field(
        default=3,
        ge=1,
        le=20,
        description="单用户令牌桶最大容量 (突发容忍)",
    )

    # 单用户每日 judge 调用配额
    abuse_daily_judge_quota: int = Field(
        default=50,
        ge=10,
        le=200,
        description="每用户每日最多消耗的 judge 调用次数",
    )

    # 单用户每日回复配额
    abuse_daily_reply_quota: int = Field(
        default=30,
        ge=5,
        le=100,
        description="每用户每日最多收到的 bot 回复次数",
    )

    # 单用户每日 advanced 模式配额
    abuse_daily_advanced_quota: int = Field(
        default=5,
        ge=1,
        le=20,
        description="每用户每日最多触发 advanced 模式的次数",
    )

    # 线程连续自回复深度上限 (超过强制冷却)
    abuse_thread_max_depth: int = Field(
        default=3,
        ge=2,
        le=10,
        description="同一线程 bot 连续回复多少次后强制冷却",
    )

    # 线程深度超限冷却 (秒)
    abuse_thread_depth_cooldown: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="线程深度超限后的冷却秒数 (指数退避基数)",
    )

    # 重复内容相似度阈值 (0-1)
    abuse_repeat_similarity_threshold: float = Field(
        default=0.80,
        ge=0.5,
        le=1.0,
        description="用户消息与最近消息相似度超过此值视为复读",
    )

    # Bot 行为检测开关
    abuse_bot_detection_enabled: bool = Field(
        default=True,
        description="是否启用 Bot 行为检测 + 人格化反击",
    )

    # ── Peer Play 闸门 (Layer 0 编排层) ─────────────────

    # 嫌疑分阈值 — 低于此值不触发任何 peer_play
    peer_play_suspicion_threshold: float = Field(
        default=0.7,
        ge=0.5,
        le=1.0,
        description="触发 peer_play 的最低嫌疑分 (≥0.7 降低误判真人风险)",
    )

    # ② 每对象冷却 (秒) — 同一 target N 分钟内最多一次 peer_play
    peer_play_cooldown_seconds: int = Field(
        default=1800,
        ge=300,
        le=7200,
        description="同一目标两次 peer_play 最短间隔 (秒), 默认30分钟",
    )

    # ③ 每对象次数上限
    peer_play_callout_lifetime_max: int = Field(
        default=1,
        ge=1,
        le=5,
        description="callout 对同一目标终生最多执行次数 (默认1次)",
    )

    peer_play_echo_daily_max: int = Field(
        default=2,
        ge=1,
        le=5,
        description="summon_echo 对同一目标每日最多执行次数",
    )

    peer_play_chore_daily_max: int = Field(
        default=1,
        ge=1,
        le=3,
        description="delegate_chore 对同一目标每日最多执行次数",
    )

    # ⑤ 触发后回复意愿下调因子 (0-1, 越小越不愿意回)
    peer_play_post_willingness_factor: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="peer_play 触发后对同一 target 的回复意愿乘数 (0.3=降至30%)",
    )

    # ── 同行隔离 (§4 安全隔离) ──────────────────────────

    # 是否启用外部 bot 内容隔离标记
    peer_isolation_enabled: bool = Field(
        default=True,
        description="是否在上下文中用隔离标记包裹外部 bot 的发言",
    )

    # ── 管理员 ─────────────────────────────────────────

    # 全局管理员 QQ 号列表
    # 请通过环境变量 BOT_ADMIN_QQ / BOT_QQ_MAIN / BOT_QQ_PEER 配置
    admin_qq_ids: list[int] = Field(
        default_factory=lambda: [0],
        description="全局管理员 QQ 号列表，请通过环境变量 BOT_ADMIN_QQ 配置",
    )

    # 唯一超级管理员 QQ 号 (角色扮演中称呼为「主人」，其他用户称呼为「小可爱」)
    super_admin_qq: int = Field(
        default=0,
        description="唯一超级管理员 QQ 号，请通过环境变量 BOT_ADMIN_QQ 配置。角色扮演中享有「主人」称呼特权",
    )

    # ★ 主人 QQ 号白名单 — 身份验证唯一真相源
    # display name / nickname 仅用于角色扮演语气，绝不用于鉴权。
    # 任何人自称"主人"但其 QQ 号不在此集合中 = 冒充。
    # 请通过环境变量 BOT_ADMIN_QQ 配置 (支持多个, 逗号分隔)。
    OWNER_QQ_WHITELIST: set[str] = Field(
        default_factory=lambda: set(),
        description="主人 QQ 号白名单。display name 可伪造，QQ 号不可。鉴权只用此集合。请通过环境变量 BOT_ADMIN_QQ 配置。",
    )

    # 同群对照 bot 的 QQ 号 — 其消息不应触发本 bot 的管线
    peer_bot_qq: int = Field(
        default=0,
        description="同群对照 bot 的 QQ 号，其消息不会触发本 bot 回复。请通过环境变量 BOT_QQ_PEER 配置。",
    )

    # ── 辅助方法 ──────────────────────────────────────

    def is_emotion_enabled(self, bot_id: str = "") -> bool:
        """检查指定 bot 的情感系统是否启用。

        emotion_enabled 是总开关; emotion_enabled_peer 控制 peer bot。
        """
        if not self.emotion_enabled:
            return False
        if bot_id and str(self.peer_bot_qq) == bot_id:
            return self.emotion_enabled_peer
        return True
