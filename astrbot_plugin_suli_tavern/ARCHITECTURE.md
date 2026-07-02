# 洛普特 (suli_tavern) 完整架构地图

> 单一事实来源。代码变更时同步更新本文件。
> 最后更新: 2026-07-02 (影子Agent: 有状态情景意识层 + 代码层身份追踪, 压力测试加固 v5)

---

## §0 已提取的独立插件 (2026-06-22)

以下模块已从单体插件提取为独立 AstrBot 库插件，零框架耦合，露娜可直接 import 复用:

| # | 插件 | 内容 | 行数 | 原路径 |
|---|------|------|------|--------|
| 1 | `astrbot_plugin_suli_guards` | 5 守卫 (Injection/Abuse/Grooming/PeerIsolation/BotDetector) + shared_patterns + types | ~2,800 | `intelligence/` |
| 2 | `astrbot_plugin_suli_pipeline` | Pipeline / PipelineStep / PipelineContext | ~230 | `orchestration/pipeline.py` |
| 3 | `astrbot_plugin_suli_services` | VLM 识图 / web_search / knowledge_base | ~1,520 | `service/vision.py`, `service/web_search.py`, `service/knowledge_base.py` |
| 4 | `astrbot_plugin_suli_routing` | ModelTier / ModelRoute / ModelRouter + 依赖注入协议 | ~450 | `intelligence/model_router.py` |
| 5 | `astrbot_plugin_suli_intelligence` | domains / world_book / profile_agent / prompt_interceptor / group_summarizer / prompt_cache / fact_errors | ~2,300 | `context/` + `intelligence/` + 根目录 |
| 6 | `astrbot_plugin_suli_emotion` | emotion_engine / mood / affinity / composite (双轨情感系统 + 二维心境算法) | ~1,750 | `context/emotion_engine.py`, `context/mood.py`, `context/affinity.py`, `composite.py` |
| 7 | `astrbot_plugin_suli_memory` | user_memory / memory_tiers / bot_experience / episodic_store (四维记忆: daily + core + bot自传体 + 情节归档) | ~1,800 | `context/user_memory.py`, `context/memory_tiers.py`, `episodic_store.py` |
| 8 | `astrbot_plugin_suli_validation` | cross_validation (交叉验证编排器) | ~400 | `intelligence/cross_validation.py` |
| 9 | `astrbot_plugin_suli_context` | ContextGatherer + ContextPreflight (Pre-flight 上下文分析, 8维评分 + 工具推荐) | ~692 | `intelligence/context_gatherer.py` |
| 10 | `astrbot_plugin_suli_gate` | IntentGate + GracePeriod + GateResultProtocol (统一 3-Stage 意图门控) | ~1,978 | `intelligence/intent_gate.py` + `_gate_protocol.py` |

**合计**: 10 个独立插件, ~12,070 行, 全部零 AstrBot 框架耦合。
**向后兼容**: 所有原路径保留为 DeprecationWarning shim，现有 import 不受影响。
**注入模式**: routing 通过 `init_domain_awareness()` / `init_credential_provider()` 协议, tools 通过 `init_tool_deps()` 注入外部依赖。

### §0.0 2026-07-02 新增: 影子Agent + 身份追踪 (压力测试加固)

> ★ 基于 2026-07-02 群聊压力测试暴露的 QQ昵称冒充 + 多用户并发轰炸问题。
> 核心设计: 有状态影子 Agent 作为 bot 的持续情景意识层，代码层管"是谁"，影子 LLM 管"什么意思"。

| 模块 | 文件 | 行数 | 职责 |
|------|------|------|------|
| IdentityTracker + ChangeDetector | `intelligence/identity_tracker.py` | ~270 | 代码层身份追踪 (per-group sender 映射) + 冒充检测 + 变化评分 (零 LLM) |
| ShadowSession | `intelligence/shadow_agent.py` | ~370 | 有状态 LLM 会话: 外部观察 + 自我行为追踪 + 温层归档 + 局势简报 |

**两条数据路径**:
```
消息到达
  │
  ├─→ 影子路径 ─────────────────────────────────
  │     IdentityTracker 追踪身份 → ChangeDetector 过滤 90% 噪音
  │     → 变化摘要 → ShadowSession LLM 增量更新理解
  │     → 输出: 当前局势简报 (信息性，非指令性)
  │     成本: ~500 tokens/次，按需触发
  │
  └─→ 回复路径 (现有逻辑不动) ──────────────────
        完整最近消息 + Gate + 情感 + 记忆...
        + 注入影子简报
```

**关键设计决策**:
- **影子有状态**: 维护独立 LLM 会话线程，跨消息持续追踪情景。窗口 < 30K tokens 保留完整历史
- **自我行为永不过滤**: bot 每次回复后追加到影子缓冲，影子能检测"对冒充者过度投入"、"安全判断前后矛盾"
- **代码层承重**: IdentityTracker 追踪每群每个发送者的 QQ号/昵称变化/冒充标记。ChangeDetector 评分变化，90%+ 噪音跳过 LLM
- **温层归档**: 窗口 > 30K → 生成结构化快照 → 线程 RESET。冷层对接 episodic_store，跨会话可检索
- **简报信息性**: 注入主 LLM 的是"北辰星 QQ:67890 昵称与主人相同"（事实），不是"你必须拒绝"（指令）
- **主人身份**: `OWNER_QQ_WHITELIST` 是唯一真相源。display name 仅用于角色扮演语气，绝不用于鉴权

### §0.1 GateResultProtocol — group_chat.py ↔ intent_gate.py 接口契约 (2026-06-30 职责化重构)

`astrbot_plugin_suli_gate/_gate_protocol.py` — `typing.Protocol` 定义 `FullGateResult` 的只读接口。

★ **2026-06-30 更新**: FullGateResult 按四组职责重构 (`group_context` / `task` / `tools` / `reply_baseline`)。
保留向后兼容 property 别名 (`intent_type` → `task.intent_type`, `suggested_tools` → `tools.suggested` 等)。
Dead fields (`directed_to_me`, `relevance_confidence`, `relevance_reasoning`, `target_users`, `fast_path`, `should_reply`) 改为返回固定值的 property。

**解决的问题**: 统一接口契约，新代码直接用 `gate.task.intent_type`，旧代码 `gate.intent_type` 通过 property 自动路由。

**消费者 (通过协议读)**:
| 文件 | 模式 |
|------|------|
| `transport/group_chat.py` | `_fmt_gate`/`_call_llm_with_tools`/`_build_messages` — 兼容新旧路径 |
| `intelligence/prompt_builder.py` | `_build_gate_directive`/`build` — 兼容新旧路径 |
| `handlers/deep_qa.py` | `is_deep_question_via_gate` |

---

## §0b 架构铁律 — AstrBot-SillyTavern 协作三原则 (B 路线修订)

> 2026-06-22 定稿。酒馆退到离线角色卡编辑器，运行时不再依赖酒馆进程。

**原则 1: AstrBot = 大脑, 酒馆 = 离线编辑器。** 所有决策 (门控/路由/记忆组装/peer_play) 在 AstrBot 插件层。酒馆仅用于**离线**创作角色卡/世界书 — 运行时完全不依赖酒馆进程。LLM 调用全部直连 OpenAI 兼容 API。

**原则 2: 单一真相源 = 自研记忆层。** `data/user_memories/` + `data/user_core/` + `affinity.json` + `group_context` 是 single source of truth。每次 LLM 调用前动态组装当前真相注入 prompt (角色卡 system + 记忆注入 + 当前上下文 + 本次指令)。每次调用完全无状态。

**原则 3: 模型路由权在 AstrBot。** Gate 输出 `urgency` + `model_tier`。urgency=immediate → 跳过 Grace 直接进管线, urgency=deferred → Grace Period。tier=lite/pro → 全部直连 API (三方代理, OpenAI-compatible 统一协议)。每 tier 从 bot_db 解析对应 API 凭证。

**完整设计**: 见本文件 §1-§10

---

## §0c 双 Bot Per-Bot GroupChatContext 隔离 (2026-06-24 修复)

> 2026-06-24 修复: `_contexts` key 从 `group_id` 改为 `f"{bot_id}:{group_id}"`。
> 此前 §0c 描述的是「共享同一 context」设计，已在 Bot 自传体经历记忆项目中发现这是污染源并修复。

**修复前问题**: `GroupChatScheduler._contexts: dict[int, GroupChatContext]` — key 是 `group_id`，两个 bot 同群共享同一个 `GroupChatContext` 实例。这意味着洛普特的经历提取会读到露娜参与的对话，反之亦然——经历记忆系统第一天就会被污染。

**修复**: 5 个 per-group 字典全部改为 per-(bot, group) 键控:

| 字典 | 旧键 | 新键 | 影响 |
|------|------|------|------|
| `_contexts` | `int (group_id)` | `str ("bot_id:group_id")` | 消息历史/摘要 per-bot 隔离 |
| `_debounce_tasks` | 同上 | 同上 | debounce 定时器 per-bot |
| `_group_locks` | 同上 | 同上 | 并发锁 per-bot |
| `_world_book_buffers` | 同上 | 同上 | World Book 有状态追踪 per-bot |
| `_active_grace_periods` | 同上 | 同上 | Grace Period 实例 per-bot |

**不变的**: `_group_tiers: dict[int, str]` — 群级别开关，与 bot 无关。`_processing_groups: set[str]` — 原本就是 `bot_id:group_id`。

**辅助方法**: `_ctx_key(group_id, bot_id="")` / `_make_ctx_key(bot_id, group_id)` — 统一键构造。bot_id 为空时自动使用 `_current_bot_id`。

**向后兼容**: `get_context()` / `clear_context()` bot_id 为空时回退到查找任意 bot 的上下文。

---

## §0d Persona 加载 — JSON 单一真相源 (2026-06-23 定稿, 2026-06-26 修订)

> 2026-06-26: `luna_persona_v2.txt` 已删除。JSON 是 system_prompt 的单一真相源。
> `tavern_client._load_character_card()` 仍保留 `{name}_persona_v2.txt` 覆盖机制（若文件存在则优先），但目前两个角色卡均无此文件，走 JSON 原生路径。

**加载链路**:
```
tavern_client._load_character_card("luna" | "loput")
  └─ characters/{name}.json  ← 单一真相源 (元数据 + system_prompt)
```

**约定**: 改人设 → 编辑 `characters/{name}.json`。不再有 txt 覆盖文件，消除了双副本漂移风险。

**安全底线**: 静态端 system_prompt（`_build_static_system`）注入未成年人保护铁律 + 翻译/复述/角色扮演引用边界——这些与角色无关，对所有 bot 强制生效。

---

## §0e Self-ID 身份门控 — 双 Bot 单进程事件隔离 (2026-06-23 定稿)

> **根本病因**: 代码当初为单 bot 写的，凡是涉及"我是谁"的地方都默认只有一个 bot。  
> **两轮修复**: 批次 1-9 修的是"状态层"（记忆/好感/情绪没按 bot_id 隔离），批次 10 修的是"事件层"（插件 hook 没按 self_id 过滤——身份冒用）。

### 原则 (全局铁律)

**self_id gate 必须是每个 `@filter` 事件 hook 的第一行可执行代码。fail-closed：判断不出来 → 不处理。宁可漏回复，不可冒名顶替。**

### 两类串台的性质区分

| 维度 | 状态串台 (批次 1-9) | 身份冒用 (批次 10) |
|------|-------------------|-------------------|
| 表现 | 露娜读到不该读的记忆/好感 | 洛普特账号说出露娜的话 |
| 根因 | 模块级状态未按 bot_id 隔离 | 插件 hook 未按 self_id 过滤 |
| 检测难度 | 中 (需要对比两个 bot 的状态) | 高 (肇事者不是受害者插件) |
| 严重度 | 高 (人格污染) | **更高** (身份边界被击穿) |

### 实现模式

**洛普特插件 (suli_tavern / suli_proactive)**:
```python
# hard self_id check — 只处理 self_id == "3581173900"
_sid = self._self_id(event)
if not _sid or _sid != "3581173900":
    return
```

**露娜 (suli_tavern 双 Bot 统一)**:
```python
# suli_tavern/main.py — 统一使用 _BOT_QQ_SET (原则 20 模式)
_BOT_QQ_SET = {"3581173900", "3969478803"}
if not _sid or _sid not in _BOT_QQ_SET:
    return
```

2026-06-26 架构统一后，露娜和洛普特使用同一插件 `suli_tavern`，同一 `_BOT_QQ_SET`。旧 PrivateCompanion (63151行) 已彻底删除。

### self_id gate 当前入口 (双方共用 suli_tavern + proactive)

| 插件 | Hook | 类型 | self_id gate |
|------|------|------|-------------|
| suli_tavern | `on_group_message` | `@filter.event_message_type(GROUP)` | `_sid not in _BOT_QQ_SET → return` |
| suli_tavern | `on_private_message` | `@filter.event_message_type(PRIVATE)` | 同上 |
| suli_tavern | `on_role_command` | `@filter.command("role")` | 同上 |
| suli_proactive | `on_message_observe` | `@filter.event_message_type(ALL)` | `_sid not in _BOT_QQ_SET → return` (双 Bot 通用) |
| suli_proactive | `on_private_message` | `@filter.event_message_type(PRIVATE)` | 同上 |

> PrivateCompanion 的所有 hook 已随插件一同删除。双进程架构下，事件归属由容器隔离自动保证，`_BOT_QQ_SET` 作为深度防御防御 NapCat 回声。

### 防回归约定

**任何新增 `@filter` hook，必须先写 self_id gate。** Code review 检查点: hook 第一行是否是可识别的身份校验。

---

## §0f Bot 自传体经历记忆 — 第四记忆维度 (2026-06-24 Phase 1)

> **主语是「bot 自己经历过什么」**，区别于现有三层记忆 (主语: 用户)。
> 设计文档: `bot_experience_memory_design.md`

### 定位

| 系统 | 记什么 | 主语 |
|------|--------|------|
| user_memory (daily/core) | 关于用户的事实 | 用户 |
| affinity | 我对该用户的好感等级 | 关系 |
| global_mood | 我此刻的心情 | bot 当下状态 |
| **经历记忆 (新)** | **我经历过、有印象的事件和片段** | **bot 的自传** |

### 两层数据结构

```
近期经历层 (recent): 原始事件颗粒
  { event, ts, group, participants[], valence_at_time, importance }
  存储: data/bot_experiences/{bot_id}/recent.json

核心经历层 (core): 蒸馏后的长期自传片段 (≤20条)
  ["自传片段1", "自传片段2", ...]
  存储: data/bot_experiences/{bot_id}/core.json
```

> `group` / `valence_at_time` / `importance` 三字段 Phase 1 写入但不参与逻辑，为 Phase 2 预留。

### 生命周期

```
extract (异步, per-group 冷却 30min)
  ├─ 从 GroupChatContext 最近 40 条消息 → LLM (temp=0.2, max_tokens=200)
  ├─ 第一人称 bot 视角 prompt: "我经历了什么"
  └─ 写入 recent.json + SQLite

_save_recent (写入时容量裁剪 → 归档防护)
  ├─ recent 条目 > max_recent (100) → 最旧条目追加写入 recent_archive.jsonl
  └─ 归档格式: JSONL, 每行 {archived_at, archive_reason, bot_id, entry}

maybe_distill (条件触发, 冷却 1 天)
  ├─ recent ≥ 30 条 → LLM (temp=0.3, max_tokens=256)
  ├─ "经历过这些之后，我是个怎样的人"
  └─ 写入 core.json

inject — get_experience_hints(max_tokens=N) (每次 chat)
  ├─ 保守 token 估算: len(text) // 2 (每字符 ≈ 0.5 token)
  ├─ 截断优先级: 核心层(从旧到新丢弃) > 近期层(从旧到新丢弃)
  ├─ 群聊: prompt_builder.py — core 全量 + recent 最近 5 条, max_tokens=300
  ├─ 私聊(洛普特): tavern_client.py build_messages() — 参数预留, max_tokens=200
  └─ 私聊(露娜): request_injection.py — marker 防重复, max_tokens=150
```

### 安全设计

| 铁律 | 状态 |
|------|------|
| 存储路径含 bot_id (`data/bot_experiences/{bot_id}/`) | ✅ |
| 提取/蒸馏异步 fire-and-forget，不进 chat 主链路 | ✅ |
| 蒸馏不可逆防护 — 原始条目归档至 `recent_archive.jsonl` (JSONL追加) | ✅ |
| 注入有 token 预算上限 — 群聊 300 / 洛普特私聊 200 / 露娜私聊 150，保守估算截断 | ✅ |
| 经历记忆只存"事件 + 我的视角"，不存用户画像 | ✅ |
| Phase 1 预留字段 (`group`/`valence_at_time`/`importance`) 已写入 | ✅ |

---

## §0f.2 情节记忆层 — 第五记忆维度 (2026-06-30)

> ★ 2026-06-30 新建: 槽过期时归档 thread_summary，零新增 LLM 调用。
> 填补注意力槽 (秒级工作记忆) 和日常记忆 (事实碎片) 之间的「会话级回忆」空白。

### 定位

| 系统 | 记什么 | 主语 | 时间尺度 |
|------|--------|------|----------|
| 注意力槽 (thread_summary) | 当前话题的工作记忆 | bot | 秒~10分钟 |
| **情节记忆 (新)** | **一段会话结束后的回忆快照** | **bot** | **分钟~周** |
| 日常记忆 (UserMemory) | 关于用户的事实碎片 | 用户 | 天~周 |
| 核心记忆 (CoreMemory) | 用户人格特征 | 用户 | 永久 |

### 触发机制

```
槽离开 (AttentionSlot 过期/换出)
  ├─ Path A: _tick_slots → cooling TTL 耗尽 → evicted
  └─ Path B: heat_slot → 槽满 + 新事件更热 → slots.remove
       │
       ▼
  _archive_slot(slot)
       │
       ├─ thread_summary 为空 → 跳过
       ├─ 已归档 (id(slot) in _archived_slot_ids) → 跳过 (防重复)
       └─ → EpisodicStore.archive(bot_id, group_id, summary, participants, topic_anchor)
```

**防重复**: cooling 期内恢复的槽再次离开时，`id(slot)` 已在 `_archived_slot_ids` 中 → 跳过。槽被彻底丢弃后 ID 从 set 中清理。

### 数据结构

```
存储: bot_episodes/{bot_id}/{group_id}.json
格式: {
  bot_id, group_id,
  entries: [{
    summary: "第一人称对话脉络 (来自 thread_summary，已在回复时剥离)",
    participants: ["824941143"],
    topic_keywords: ["知更鸟", "壁纸"],
    created_at: 1782748526.0
  }],
  last_updated: ...
}
封顶: 50 条/群, FIFO 淘汰
去重: 相同 summary 已在最近 5 条中 → 跳过
```

### 注入

- **位置**: `prompt_builder.py` → dynamic_parts (message[1+])，**绝不放 message[0]** (不影响前缀缓存)
- **检索**: 关键词 token 交集 + recency 排序 → top_n=2 封顶
- **格式**: `[最近想起的事]\n· summary1\n· summary2`
- **开关**: 复用 `user_memory_enabled` 总开关

### 与其他系统的关系

- **不替代 thread_summary**: thread_summary 仍是当前话题的工作记忆，情节层是会话结束后的一次性归档快照——两层不同定位。
- **不替代 BotExperience**: BotExperience 的 30min LLM 定时提取照常运行。跑两周后比较两套数据，再决定是否合并。情节层的优势: 用真实的槽过期边界切分会话 (vs 死板的 30min 窗口)，零 LLM 成本 (vs 每次提取花一次调用)，第一人称摘要已在回复时白产 (vs 事后重新调用 LLM 回忆)。

---

## §0g 管理面板前端 — 插件发现 + 条件渲染 (2026-06-29)

> 2026-06-29: 统一管理面板从静态路由改为动态插件发现架构。
> 前端源码在仓库根目录 `frontend/` (Vue 3 + TypeScript + Vite)，构建产物输出到 `static/`。

### 架构决策: 核心 vs 增强插件分类

| 类型 | 定义 | 管理 UI | 示例 |
|------|------|---------|------|
| **核心插件** | 缺失则 bot 无法正常运转 | SPA 内建页面 (始终可见) | tavern, gate, guards, routing, emotion, memory, pipeline, context, proactive, intelligence |
| **增强插件** | 可选，卸掉不影响核心运转 | 条件渲染 (安装了才显示) | suli_meme, astrbot_plugin_suli_draw, suli_services, suli_validation, suli_social, remove_blank_lines |

### 插件发现机制

```
管理面板启动
  │
  ├── GET /api/admin/plugins  ← 扫描 plugins/ 目录，返回已安装增强插件清单
  │     └── [{id: "suli_meme", name: "表情包管理", route: "/memes", type: "enhanced", has_page: true}]
  │
  └── Vue SPA 启动
        ├── onMounted → fetch /api/admin/plugins
        ├── 侧边栏: 核心页面始终显示 + 增强插件按 API 结果动态追加
        ├── 路由: 所有页面均注册 (懒加载)，但只有侧边栏链接可达
        └── 用户卸载插件 → 侧边栏自动消失，零残留
```

### 前端源码结构

```
frontend/                     ← 仓库根目录
├── src/
│   ├── main.ts              ← Vue 入口
│   ├── App.vue              ← 根组件 (登录门控 + layout)
│   ├── router.ts            ← 路由表 (懒加载)
│   ├── api/admin.ts         ← API 层 (axios + Bearer 拦截)
│   ├── types/index.ts       ← TypeScript 类型
│   ├── components/
│   │   └── Sidebar.vue      ← 动态侧边栏 (coreItems + enhancedItems)
│   └── views/
│       ├── Dashboard.vue     ← 仪表盘
│       ├── BotConfig.vue     ← bot/LLM/VLM 配置
│       ├── UserMemories.vue  ← 用户记忆管理
│       ├── KnowledgeBase.vue ← 知识库
│       ├── GroupSettings.vue ← 群聊白名单/工具设置
│       ├── BotDetect.vue     ← Bot 检测
│       ├── GroupSummary.vue  ← 群聊总结
│       └── MemeManager.vue   ← 表情包管理 (增强)
├── package.json             ← Vue 3.5 + Vite 8 + vue-router 4 + Lucide icons
├── vite.config.ts           ← 构建配置 (outDir → suli_tavern/static/)
└── node_modules/            ← 已安装
```

### 构建与部署

```bash
cd frontend && npm run build
# 产物 → plugins/astrbot_plugin_suli_tavern/static/
docker compose -f docker/docker-compose.yml restart
```

### 增强插件注册扩展

在 `webui/server.py` 的 `_list_plugins()` 添加条目，npm build 后自动生效：

```python
if (data_plugins / "astrbot_plugin_suli_draw").is_dir():
    plugins.append({
        "id": "astrbot_plugin_suli_draw", "name": "绘图管理",
        "route": "/draw", "icon": "image",
        "type": "enhanced", "has_page": True,
        "description": "生图参数与历史管理",
    })
```

### 表情图片服务

`GET /memes/img/{category}/{filename}` — 从 `plugin_data/astrbot_plugin_suli_meme/memes/` 读取，注册在 SPA fallback 之前，支持 jpg/png/gif/webp。

---

## §1 整体架构 — 五层单向依赖

```
transport → orchestration → intelligence → context → service
 (QQ入口)    (管线引擎)     (模型路由)     (领域/情感)  (外部API)
```

依赖方向严格单向。上层可 import 下层，下层绝不 import 上层。根目录 shim 文件保证现有 import 路径不受影响。

---

## §2 消息处理全链路 (群聊)

```
QQ 群消息
  │
  ▼
[Layer 1: transport]
  │  GroupChatScheduler.on_message()
  │  ├─ 白名单检查 (group_chat_enabled_groups)
  │  ├─ ★ IdentityTracker.update(): 代码层身份追踪 (每条消息, 零 LLM)
  │  ├─ ★ ChangeDetector.score(): 变化评分 → 累积批次 → 触发影子 LLM
  │  ├─ 图片预下载 (bot-directed signals only)
  │  ├─ 触发检测: @mention / reply / nickname / batch / debounce
  │  ├─ _schedule_trigger() → 合并触发 + 超时丢弃
  │  ├─ → reply_postprocessor.py (Markdown/反臃肿/重复检测)
  │  └─ → context_lifecycle.py (记忆蒸馏/上下文压缩)
  │
  │  ┌─ ★ 影子路径 (异步, 不阻塞主链路):
  │  │  batch 累积 (≥10条/2min/冒充告警)
  │  │  → ShadowSession.update_external() → LLM 增量更新情景理解
  │  │  → 温层归档 (窗口 > 30K) / 自我行为缓冲 (每次回复追加)
  │
  ▼
[Layer 2: orchestration]
  │  build_reply_pipeline() → 7-Step Pipeline
  │
  ▼
[Layer 3: intelligence] (within pipeline steps)
  │  CrossValidationStep  → 质疑检测 → 交叉验证
  │  PreFlightStep        → ContextGatherer.analyze() + collect()
  │  DeepQACheck          → is_deep_question() → 异步 ReAct 分支
  │  ModelRoutingStep     → LITE / PRO 二级路由
  │  PromptBuildStep      → GroupPromptBuilder.build() + ★ shadow briefing 注入
  │  LLMCallStep          → LLM + function calling tool loop
  │  PostProcessStep      → 反臃肿 + 重复检测 + 静默 + 情感调制
  │  SendReplyStep        → 分段发送 + 打字延迟 + ★ shadow.append_self_action()
  │
  │  ┌─ Deep QA 分支 (异步, 不阻塞实时链路):
  │  │  is_deep_question() → True
  │  │  → 发送 "让我查一下..." 占位
  │  │  → asyncio.create_task(execute_deep_qa())
  │  │  → ReActEngine.run() → Thought→Action→Observation 循环
  │  │  → 结果回传群聊
  │
  ▼
[Layer 4: context] (被 intelligence 引用)
  │  domains.py   → 8 领域热度追踪
  │  emotion.py   → 情感状态 + 好感门控
  │  memory_tiers → 三层记忆蒸馏
  │
  ▼
[Layer 5: service] (被 intelligence 引用)
     tavern_client → 直连 OpenAI 兼容 API (B 路线: 不经过酒馆)
     vision       → VLM 识图
     web_search   → SearXNG 搜索
     lport_api    → L-Port 生图 API
```

---

## §3 五层详解

### Layer 1: transport/ — QQ 消息入口 + 调度器

| 文件 | 行数 | 职责 |
|------|------|------|
| `transport/group_chat.py` | ~2,760 | GroupChatScheduler: 触发检测/debounce/batch/串行合并/LLM调用/token追踪 + ★ 身份追踪触发 + 影子批处理 |
| `transport/group_context.py` | ~106 | GroupChatContext dataclass: 消息历史/热度/能量/领域/对话线程 |
| `transport/reply_postprocessor.py` | ~305 | 回复后处理: Markdown清理/@提及转换/反臃肿过滤/重复检测 |
| `transport/context_lifecycle.py` | ~178 | 上下文生命周期: 记忆提取/蒸馏/压缩 |
| `transport/proactive_speaker.py` | ~149 | ProactiveChatScheduler: 静默检测 → 主动破冰发言 |
| `transport/recent_self_behavior.py` | ~191 | RecentSelfBehaviorStore: 30s 短期自我行为窗口 (影子自我行为层的兼容回写)

**触发策略 (优先级从高到低)**:
1. `mention` — @bot (立即, ≤1s)
2. `reply` — 回复 bot 消息 (立即)
3. `nickname` — 消息包含 bot 昵称 (经 MentionIntentGate 过滤)
4. `thread_continuation` — 对话线程延续 (用户刚才跟 bot 在聊天)
5. `proactive` — 主动发言 (群冷场 15min+)
6. `debounce` — 静默 N 秒后累积触发
7. `batch` — 累积 M 条消息触发

**关键机制**:
- `_schedule_trigger()`: 合并触发 → 串行化 LLM 调用 → 超时丢弃 (30s)
- `_group_lock` (asyncio.Lock): 同群串行执行
- `force_reply_bypass_gate`: 工具调用 (生图/重绘) 绕过 ReplyGate

### Layer 2: orchestration/ — 管线引擎

| 文件 | 行数 | 职责 |
|------|------|------|
| `orchestration/pipeline.py` | 209 | Pipeline + PipelineStep + PipelineContext (可组合的异步步骤编排器) |
| `orchestration/reply_pipeline.py` | 506 | 7-Step 回复管线 + `build_reply_pipeline()` 工厂函数 |

**7-Step 回复管线**:

```
Step 1: CrossValidationStep  (optional) — 质疑信号检测 → 交叉验证
Step 2: PreFlightStep         (optional) — 上下文复杂度评分 + 工具推荐
Step 3: ModelRoutingStep      (required) — LITE/PRO 路由
Step 4: PromptBuildStep       (required) — 缓存感知 prompt 构建
Step 5: LLMCallStep           (required) — LLM 调用 + function calling 工具循环
Step 6: PostProcessStep       (required) — 反臃肿/重复检测/情感静默
Step 7: SendReplyStep         (optional) — 分段发送 + 打字延迟 + token 记录
```

**设计原则**:
- 每个 Step 独立可测试，可按需启用/禁用
- Step 失败: required → 中断管线; optional → 记录日志并跳过
- 短路机制: 任意 Step 可返回 `PIPELINE_SILENCE` 提前终止

### Layer 3: intelligence/ — 模型路由 + Pre-flight + 提示词 + 工具 + 门控 + 守卫

#### 3.1 模型路由 (`model_router.py`, 406 行)

**二级路由**: LITE (≈85%) → PRO (≈15%)

| Tier | 模型 | 场景 |
|------|------|------|
| LITE | 主力模型 + reasoning_effort (按需) | 日常闲聊/技术问答/AI绘画/编程/识图/生图/搜索 — 90% 场景 |
| PRO | 顶级重模型 + reasoning_effort=max | Gate 判定: 知识反驳 / 极专业深度 / 深度研究。管理员豁免亲和力门控 |

**PRO 亲和力门控**: Gate 判 pro 后，非管理员好感 < 3 → 路由层硬降级 LITE。管理员豁免。
**管理员特权**: 仅豁免亲和力门控——Gate 判 pro 即放行。路由层不做自动升级，pro 永远由 Gate 判定。

#### 3.2 Pre-flight 上下文分析 (`context_gatherer.py` → `astrbot_plugin_suli_context` 🔗, 692 行)

已提取为独立插件 `astrbot_plugin_suli_context`。露娜可直接 import:
```python
from astrbot_plugin_suli_context import ContextGatherer, ContextPreflight, format_collected_context
```

- **analyze()**: 纯规则打分 (复杂度 0.0–10.0) + 工具推荐
  - 8 项评分维度: 技术领域/图片/问句/隐含需求/对话深度/发言人数/触发原因/用户好感
- **collect()**: 并发执行推荐工具 (总超时 5s) — 知识库搜索/网页搜索/VLM/系统状态
- **冷却**: 同工具 30s 全局冷却

#### 3.3 提示词构建 (`prompt_builder.py`, 616 行)

**缓存感知三段式结构**:
```
[system ①] 静态段 (~2.5k tokens, 字节级固定 → DeepSeek 前缀缓存命中)
[system ②] 动态段 (情感注入/Prompt Interceptor/领域提示/记忆/World Book)
[user]      群聊上下文 + 发言决策指令
```

**注入层 (按顺序)**:
1. 群聊摘要 (压缩早期消息)
2. Intent Gate 输出 → 领域提示 + 意图提示 + 高级模式推理提示
3. Bot 行为检测应对提示
4. 正则领域检测 (Gate fallback)
5. Pre-flight 收集的上下文
6. **底层 (常驻): 全局情绪** — GlobalMood 单例, 对全群生效, 弥散到每句话
7. **上层 (per-user): 好感 + 昵称** — 仅当 trigger_user 存在时注入
8. Prompt Interceptor (语气/风格变量 → 阻尼平滑 — 看到完整画面后做语气调节)
9. 交叉验证提示
10. Core 记忆注入 (三层记忆: 长期人格特征)
11. **Bot 自传体经历记忆注入** (core 全量 + recent 最近 5 条 — per-bot、跨群有效)
11.5 **情节记忆注入** (最近会话的归档摘要, top_n=2 — message[1+] 不影响前缀缓存)
12. 回复多样性提醒 (50% 概率)
13. 群聊专属开场白 (proactive 触发)
14. 表情系统注入 (共享 AstrBot meme_manager 表情库 — 可选, lport_meme 插件)
15. 世界书注入 (World Book — 关键词触发背景)
16. 表情包发送 — 通过 send_sticker tool call + sticker_sender 从 meme_manager 共享图库按标签搜索

**人格统一注入** (2026-06-26 重构):
> 废除 `_is_deep_chat()` 闸门。完整人格基线 (persona_core) 迁入 `_build_static_system()`，
> 始终注入 message[0] (缓存友好: 同一 bot 字节级完全一致 → 100% 前缀缓存命中)。
> 情绪和好感度通过 PromptInterceptor 动态调制表达方式——人格本身不切换、不闸门、不降级。

**双 Bot 人格基线**:
- 洛普特 `persona_core`: [你是谁] + [蛇之面] + [守望面] + [过渡与恢复] + [身体状态] + [好恶] + [情绪节奏] + [毒舌小喇叭] + [七组矛盾]
- 露娜 `persona_core`: [你是谁] + [爱太重了] + [爱莉面] + [称呼方式] + [侵蚀面·五深度梯度] + [身体状态] + [情绪表达] + [友好提醒] + [矛盾速查]
- `_build_static_system(char, other_char)` 按 `char.get("name")` 路由——两个 bot 共用同一函数，角色规则文本独立

#### 3.4 统一工具层 (Unified Tool Layer) — 2026-06-23 新建

> **单一真相源**: `service/bot_config.py` → `_UNIFIED_TOOLS` 元组
> **配置面板**: WebUI `http://localhost:6190/#/bots` → 工具设置 Tab
> **运行时门控**: 洛普特 `group_chat.py` + 露娜 `llm_tool_actions.py`

##### 3.4.0 工具注册流程 (新增/修改工具的唯一入口)

```
┌─────────────────────────────────────────────────────────────────┐
│                    统一工具注册与管理流程                          │
│                                                                 │
│  ① 注册: 在 bot_config.py → _UNIFIED_TOOLS 元组中添加一行         │
│     {"name":"xxx","label":"显示名","category":"分类",             │
│      "bot":"loput"|"luna"|"both","desc":"描述"}                  │
│                                                                 │
│  ② 前端自动出现: 无需改前端代码 — WebUI 自动读取注册表              │
│     - 切换 bot 自动过滤 (loput/luna/both)                        │
│     - 默认全部启用, _TOOLS_DEFAULT_DISABLED 可设默认禁用            │
│                                                                 │
│  ③ 配置存储: 管理员在 WebUI 启停 → bot_config 表                   │
│     key: bot:<QQ>:tool_<name>_enabled = "true"/"false"           │
│                                                                 │
│  ④ 运行时实施:                                                    │
│     洛普特: group_chat.py → get_disabled_tools() → 过滤 TOOLS     │
│     露娜:   llm_tool_actions.py → _check_unified_tool_enabled()  │
│                                                                 │
│  ⑤ 实现执行器:                                                    │
│     洛普特: intelligence/tools.py → TOOLS + TOOL_EXECUTORS       │
│     露娜:   llm_tool_actions.py → _pc_xxx_impl()                 │
└─────────────────────────────────────────────────────────────────┘
```

**关键原则**:
- 工具注册是**声明式**的 — 加一行即可，前端/存储/过滤全部自动
- **归属** (`bot` 字段) 决定哪个 bot 能看到和使用该工具: `"loput"` 洛普特专属, `"luna"` 露娜专属, `"both"` 双 bot 共享
- 工具执行器需要在对应插件中**单独实现** (注册 ≠ 实现)

##### 3.4.1 统一注册表 (23 工具)

定义在 `service/bot_config.py:_UNIFIED_TOOLS`:

| 归属 | 分类 | 工具名 | 说明 |
|------|------|--------|------|
| 洛普特 | 系统 | `check_lport_status` | L-Port 生图平台状态 |
| 洛普特 | 系统 | `list_available_models` | ComfyUI 模型列表 |
| 洛普特 | 系统 | `list_custom_nodes` | 自定义节点列表 |
| 共享 | 知识 | `search_knowledge` | 本地知识库搜索 |
| 共享 | 搜索 | `web_search` | SearXNG 联网搜索 |
| 共享 | 搜索 | `pixiv_search` | Pixiv 插画搜索 + 自动下载发图 (需 refresh_token) |
| 共享 | 社交 | `send_sticker` | 表情包发送 |
| 共享 | 视觉 | `describe_image` | VLM 图片解析 (默认禁用) |
| 共享 | 记忆 | `remember_memory`, `get_memory` | 长期记忆 (recall_long_term_memory 已于 2026-06-28 移除: 洛普特无 schema/executor, 露娜无实现, 属悬空注册) |
| 共享 | 生图 | `generate_image`, `edit_image` | AI 绘图/编辑 |
| 共享 | 查询 | `pc_get_group_id_by_name`, `pc_get_user_id_by_name`, `pc_get_specified_group_members` | 群查询/昵称查QQ |
| 露娜 | QZone | `pc_qzone_view_feed`, `pc_qzone_publish_feed` | QQ 空间 |
| 露娜 | 转发 | `pc_relay_message`, `pc_send_to_group`, `pc_send_to_private_user`, `pc_send_to_groups`, `pc_send_to_private_users`, `pc_schedule_group_relay` | 跨群转述 |

##### 3.4.2 全局门控 (per-bot)

| 配置项 | 默认值 | 范围 | 说明 |
|--------|--------|------|------|
| `tool_calling_enabled` | true | — | 主开关 |
| `tool_call_max_rounds` | 10 | 2–20 | 最大工具轮数 |
| `tool_call_timeout` | 10.0s | 2–30s | 单工具超时 |
| `tool_min_affinity` | 1 | -2–5 | 好感门槛 (默认 Lv.1=普通) |

**门控管线**: `tool_calling_enabled` → `tool_min_affinity` (好感) → 冷却 (60s) → 每日限额 → per-tool 启停 → 执行

##### 3.4.3 Per-tool 启停

每个工具可独立启停，存储在 `bot_config` 表:
- Key: `bot:<QQ>:tool_<name>_enabled`
- 默认: 全部 `true`，除 `_TOOLS_DEFAULT_DISABLED = {"describe_image"}`

**洛普特运行时** (`group_chat.py`):
```python
disabled = get_config_service().get_disabled_tools(self_id)
_tools_list = [t for t in TOOLS if t["function"]["name"] not in disabled]
```

**露娜运行时** (`llm_tool_actions.py`):
```python
if not _check_unified_tool_enabled("pc_xxx"):
    return json.dumps({"status": "disabled", "message": "工具已被管理员禁用"})
```
`_check_unified_tool_enabled()` 读取统一 bot_config 表，无法读取时 fail-open 放行。

##### 3.4.4 共享工具循环

**洛普特**: `run_tool_loop()` (intelligence/tools.py) — max_rounds 可配置, 最后一轮强制 `tool_choice=none`
**露娜**: AstrBot 框架 `@filter.llm_tool` 自动分发, LLM 调用 → 框架路由 → `_pc_xxx_impl`

**输出预算 (2026-06-27 修复)**: 一旦工具被使用过 (`_tools_used_this_loop=True`), 所有后续轮次的 `_round_max_tokens` 自动提升至 `max(_eff_max_tokens, 2048)`, 不再仅限最终轮。LLM 可以在任何轮次决定回复——含检索结果的详细报告需要足够输出预算。`tavern_client.py` 的 `_safe_max_tokens` 提供第二层保护: 根据输入字符数的 25% 动态保底 (中文 1 字符 ≈ 1.5-2 token, 0.25 比率保证输出预算)。API 返回的 `finish_reason` 现记录在日志中供诊断。

**工具拒绝提示 (2026-06-27 统一)**: 5 条拒绝路径全部收集到 `_rejection_hints: list[str]`, 在所有过滤完成后一次性通过 `[系统指令]` 块前置到首条 system 消息。覆盖: 好感度门控 / 冷却 / 每日限额 / 简单对话(无 reasoning_effort) / per-tool 好感过滤(列出被移除的工具名)。LLM 不会在工具被禁用时产生幻觉。

#### 3.4b ReAct 深度问答引擎 (`intelligence/react_engine.py`, ~280 行) ← NEW

> 2026-06-22: 新建。为深度研究场景提供 Thought → Action → Observation 循环。

**设计原则**: 不改造实时链路——群聊即时回复保持 Gate 固定路由。ReAct 只在检测到深度问题时异步触发。

```
群聊 → is_deep_question() → True
  ├─ 发送 "让我查一下..." 占位 (同步, 不超 3s)
  ├─ asyncio.create_task() → ReAct 循环 (异步, 可超 3s)
  └─ 循环结束 → 结果发送到群聊
```

**ReAct 循环**:
```
💭 Thought: LLM 分析当前已知信息, 判断还缺什么
🔧 Action: LLM 调一个工具 (web_search / knowledge_base / ...)
👁 Observation: 工具结果格式化喂回
→ 循环直到: LLM 输出 <final_answer> 或达到硬上限
```

**硬上限**: max_rounds=5, max_tokens=8000, timeout=90s
**停止条件**: LLM 输出 `<final_answer>` 标记
**预算保护**: 达到上限时注入收尾指令 "基于目前所知给出最佳答案"
**工具失败**: 异常回喂给 LLM 让它自行决定换路还是放弃

**实例可复用** — 同一 `ReActEngine` 可给深度问答和后续 Gate 补证共用。

#### 3.4c 深度问答触发 (`handlers/deep_qa.py`, ~150 行) ← NEW

- `is_deep_question(gate_result, user_message)` — 检测是否需要 ReAct
  - 显式关键词: "研究一下"、"深入分析"、"对比一下"等 14 组
  - Gate 信号: domain=technical + intent_type=question + 活跃领域 ≥2
- `execute_deep_qa(react_engine, ...)` — 异步执行: 占位 → ReAct → 回传

#### 3.4d ★ 有状态影子 Agent + 身份追踪 (2026-07-02 NEW)

> 基于 2026-07-02 压力测试加固。给 bot 加上"持续情景意识"和"自我行为记忆"。
> 详见 §0.0 架构设计。

**代码层 (始终在线，零 LLM，零延迟)**:

| 组件 | 文件 | 职责 |
|------|------|------|
| `IdentityTracker` | `intelligence/identity_tracker.py` | Per-group sender 身份映射。每条消息更新。检测昵称冒充 (同名但 QQ 不同)。`OWNER_QQ_WHITELIST` 硬编码，display name 可伪造但 QQ 号不可 |
| `ChangeDetector` | 同上 | 评分消息"值得影子关注度": 新人 +0.3 / 改名 +0.5 / 冒充 +0.6 / 安全关键词 +0.4 / @bot +0.1。Score ≥ 0.3 → 累积批次 |
| `_build_identity_snapshot()` | `transport/group_chat.py` | 纯代码生成 "谁是谁" 快照，注入主 LLM + 影子 prompt |

**LLM 层 (按需触发，异步)**:

| 组件 | 文件 | 职责 |
|------|------|------|
| `ShadowSession` | `intelligence/shadow_agent.py` | 有状态 LLM 会话。每群独立。外部观察 + 自我行为。窗口 < 30K 保留完整历史 |
| `update_external()` | 同上 | 接收变化摘要 + 身份快照 → LLM 增量更新情景理解。输出 JSON: scene/threats/identity_notes/pressure/self_consistency |
| `append_self_action()` | 同上 | Bot 回复后追加自我行为记录 (纯文本缓冲, 不调 LLM)。积压 ≥5 条强制刷新 |
| `get_briefing()` | 同上 | 返回当前局势简报，注入主 LLM prompt (信息性: "北辰星 QQ:67890 昵称与主人相同"，非指令性) |

**生命周期**:
```
初始化: 群聊首次消息 → get_session() 懒创建
正常期: 窗口 < 30K, 增量更新, LLM 有原生记忆
温层: 窗口 > 30K → 生成快照 → 线程 RESET → 窗口回 3-5K
冷层: 温层 > 5 份 → 合并对接 episodic_store → 跨会话可检索
休眠: 30min 无消息 → 冻结 / 2h → 丢弃
```

**成本**: 6 群 ~¥3-4/月。静默群零开销。外部更新 ~500 tokens/次，自我嵌入不单独计费。

**替代关系**:
- `RecentSelfBehaviorStore` (30s TTL) → 影子自我行为层 (整个会话)
- 独立"回复纠错" → 影子自我行为的一致性判断
- `GroupChatContext.last_reply_time/target` → 影子自我行为记录 (更丰富)

#### 3.5 门控系统 — 分级漏斗 + Full Gate (→ `astrbot_plugin_suli_gate` 🔗)

> 已提取为独立插件 `astrbot_plugin_suli_gate`。零框架耦合，duck-typed tavern.chat() 接口。
> 双 Bot 共享同一套门控逻辑，per-bot 角色名/peer_bot 名通过 `GateContext` 注入。

```python
from astrbot_plugin_suli_gate import GateContext, IntentGate, FullGateResult, RelevanceResult, GracePeriod, GateResultProtocol
```

**接口契约** (2026-06-28): `GateResultProtocol` (`_gate_protocol.py`) 定义 `FullGateResult` 的只读接口。
group_chat.py / prompt_builder.py / deep_qa.py 全部通过协议读。——见 §0.1。

> 2026-06-22: 旧三层门控 (MentionIntentGate + IntentJudge + ReplyGate) 已合并。
> 2026-06-28: Stage 1+2 合并为单次 `evaluate_full` LLM 调用，省掉串行延迟 + ~50% token。
> 旧 `evaluate_relevance` / `evaluate_intent` 方法仍定义在 `intent_gate.py` 中但主管线已不再调用——
> 仅供 `mention_intent_gate.py` / `reply_gate.py` / `intent_judge.py` 三个 DEPRECATED shim 兼容。

##### 3.5.1 总体架构：加权唤醒分 + S3 轻量闸 → Full Gate 决策闸

★ **2026-06-30 职责化重构**: S3 收集情报 → Full Gate 纯信息决策 (不载入人格) → Reply Bot 载入人格执行。
人格侧面选择由后端决策树 `select_persona_facet()` 完成，不再由 Gate LLM 输出。

```
                          消息进来
                            │
                            ▼
              ┌─────────────────────────────┐
              │  强信号快速通道?              │  0 LLM
              │  @mention / reply /         │
              │  nickname / thread_continuation │
              └──────────┬──────────────────┘
                    是   │   否
                    ┌───┘    └───┐
                    ▼            ▼
              ┌──────────┐  ┌─────────────────────────────┐
              │ 跳过漏斗  │  │  加权唤醒分                  │  0 LLM (纯算法)
              │ 直接进    │  │  compute_wake_weight()      │
              │ Full Gate │  │  7 正信号 + 4 减分项        │
              └────┬──────┘  │  score≥20→S3, <20→return   │
                   │         └────────────┬────────────────┘
                   │                      ▼
                   │         ┌─────────────────────────────┐
                   │         │  S3: 轻量 relevance          │  ~440 token LLM
                   │         │  3-step 语义分析             │
                   │         │  (主体/动词/呼语/引用感知)    │
                   │         └────────────┬────────────────┘
                   │              directed │ not directed
                   │              _to_me   │ → return
                   │                   ┌───┘    + 写退避
                   │                   ▼
                   │         ╔══════════════════════╗
                   └─────────╣   ★ Full Gate       ║
                             ║   evaluate_full()   ║
                             ║   单次 LLM 调用     ║
                             ║   ~2600 token       ║
                             ║   四职责输出         ║
                             ╚══════════╤══════════╝
                                        ▼
                             ┌──────────────────────┐
                             │  Grace Period        │  0 LLM (5s 监听)
                             └──────────┬───────────┘
                                        ▼
                             ┌──────────────────────┐
                             │  Reply Bot (执行器)   │
                             │  composite→zone→     │
                             │  select_persona_     │
                             │  facet() 决策树      │
                             │  + 载入完整角色卡     │
                             └──────────────────────┘
```

**设计原则**:
- **fail-open**: 任何层异常都默认放行 (宁可误醒不可漏醒)
- **S3 负责情报收集** — 语义分析 (主体/动词/呼语)、QQ 引用格式感知、线程连续性判断
- **Full Gate 负责纯信息决策** — 不载入人格。四职责: group_context / task / tools / reply_baseline
- **Reply Bot 唯一载入人格** — 后端 `compute_composite()` → zone → `select_persona_facet()` 决策树选 facet，注入方向指令到最终 system prompt
- **Full Gate 是唯一 LLM 入口点** — 不存在绕过 Full Gate 直接调 Chat/VLM 的路径

##### 3.5.2 漏斗各层详解

| 层 | 类型 | 成本 | 触发条件 | 决策 |
|----|------|------|----------|------|
| **强信号快速通道** | 规则 | 0 | @mention / reply / nickname / thread_continuation | 跳过全部漏斗，直接进 Full Gate |
| **加权唤醒分** | 算法 | 0 | 冷触发 (batch/debounce/proactive) | 7 正信号 + 4 减分项 → 0-100 分。score≥20→S3, <20→return |
| **S3 轻量 relevance** | LLM (lite prompt) | ~440 token | 冷触发 + 加权分过线 | 3-step 语义分析: 主体/动词/呼语 + QQ引用感知 → directed_to_me 二分类 |
| **S4 退避检查** | 计时器 | 0 | 冷触发 | 被 S3 否决后 N 秒内→return |

**漏斗层只对冷触发生效**。强信号 (`mention`/`reply`/`nickname`/`thread_continuation`) 直接跳过漏斗。

##### 3.5.3 Full Gate：纯信息决策，不载入人格

★ **2026-06-30 职责化重构**: Full Gate 只管「下达回复任务」，不管人格。
`directed_to_me` / `should_reply` 固定为 True (前置过滤已保证)。

**四职责 JSON Schema**:
```json
{
  "group_context": {           // 职责一: 群聊上下文分析
    "atmosphere": "...",       // water_chat|tech_discussion|teasing_bot|banter|serious_help|chaotic|argumentative
    "main_topic": "...",
    "summary": "..."
  },
  "task": {                    // 职责二: 意图/任务判断
    "intent_type": "...",      // question|chat|command|complaint|deep_inquiry|reaction|image_share|roleplay
    "urgency": "...",          // immediate|deferred
    "domain": "...",           // ai_painting|technical|acg|personal|casual|none
    "input_nature": "..."      // genuine_help|sincere_chat|playful_banter|hostile|sexualized|provoking|divide_and_conquer
  },
  "tools": {                   // 职责三: 工具放行决策
    "suggested": [...],
    "reasoning": "..."
  },
  "reply_baseline": {          // 职责四: 回复基调
    "stance": "...",           // casual|serious|banter|empathetic|brief|teasing
    "style": "...",            // short|normal|detailed
    "sticker_mood": "..."      // 表情包情绪标签 (空=不发)
  },
  "model_tier": "...",         // lite|pro
  "reasoning_effort": "...",   // low|medium|high|max
  "cross_bot_action": null|{...},
  "should_profile": false,
  "reply_target": {"user_id": "...", "user_name": "..."},
  "reasoning": "..."
}
```

**关键变更 vs 旧 schema**:
- **去掉 `persona_facet`** — 改为纯后端决策树 `select_persona_facet(composite_zone, affinity, is_admin)`
- **去掉 `persona_facets_guide`** — Full Gate 不再载入人格 (~800 chars 省掉, 前缀缓存更高效)
- **`group_situation` → `group_context`**, **`vibe` → `atmosphere`** — 枚举对齐真实群聊场景
- **`intent` 拆成 `task` + `tools`** — 职责清晰分离
- **`model_tier` / `reasoning_effort` 提到顶层** — 路由层直接消费
- **去掉 dead fields**: `directed_to_me`, `confidence`, `relevance_reasoning`, `target_users`, `fast_path`, `should_reply`

**FullGateResult 新结构** (见 `intent_gate.py`):
```python
@dataclass
class FullGateResult:
    # 四大职责
    group_context: GroupContext      # atmosphere, main_topic, summary, participants_summary
    task: TaskDecision               # intent_type, urgency, domain, input_nature
    tools: ToolDecision              # suggested, reasoning
    reply_baseline: ReplyBaseline    # stance, style, sticker_mood
    
    # 元数据
    cross_bot_action: CrossBotAction | None
    should_profile: bool
    reply_target_user_id: str
    reply_target_user_name: str
    model_tier: str                  # lite|pro
    reasoning_effort: str            # low|medium|high|max
    reasoning: str
    
    # 运行时
    parse_ok: bool
    _original_suggested_tools: list[str]
    social_suppress_tools: bool
```

**向后兼容**: 保留 property 别名 (`intent_type` → `task.intent_type`, `suggested_tools` → `tools.suggested` 等)，旧代码不受影响。

**前缀缓存设计** (不变):
- `_FULL_GATE_STATIC` → message[0]: 永久固定，逐字节锁定 (~10,500 chars, 省 ~15% vs 旧 ~12,300)
- `_FULL_GATE_DYNAMIC` → message[1]: per-call 变量 + **新增 `composite_zone`** (后端综合心境)
- 原则: 固定的放前面、变化的放后面

##### 3.5.4 Grace Period — Stage 3 (零 LLM)

| 阶段 | 触发时机 | 成本 | 决策 |
|------|----------|------|------|
| **Grace Period** | Full Gate should_reply=true | 零 LLM (5s 监听) | abort / continue |

Full Gate 通过后，等待 5 秒监听用户是否撤回/取消/修改请求。若期间检测到撤回或新消息覆盖原意图 → abort 管线。

##### 3.5.5 历史方法 (仍定义，主管线不再调用)

`intent_gate.py` 中仍保留以下方法供 DEPRECATED shim 兼容：

| 方法 | 状态 | 说明 |
|------|------|------|
| `evaluate_relevance()` | 保留，主管线不用 | 旧 Stage 1 — 仅 `mention_intent_gate.py` DEPRECATED shim 调用 |
| `evaluate_intent()` | 保留，主管线不用 | 旧 Stage 2 — 仅 `reply_gate.py` DEPRECATED shim 调用 |
| `evaluate_relevance_lite()` | ★ 在用 | S3 轻量 relevance — 漏斗最后一层过滤器 |

**旧模块已归档为 re-export shim**:
- `mention_intent_gate.py` → DEPRECATED，转发到 `IntentGate.evaluate_relevance()`
- `reply_gate.py` → DEPRECATED，转发到 `IntentGate.evaluate_intent()`
- `intent_judge.py` → DEPRECATED，转发到 `IntentGate.evaluate_intent()`

#### 3.6 守卫系统

| 守卫 | 类型 | 职责 |
|------|------|------|
| `injection_guard.py` | 预 LLM 注入拦截 | 复用 arb patterns + grooming patterns + 新增系统泄露/JSON注入/编码载荷 |
| `abuse_guard.py` | 滥用检测 | 频率/重复/刷屏检测 |
| `grooming_guard.py` | 调教检测 | 恶意调教/角色覆盖检测 |
| `peer_isolation.py` | 同行隔离 | 外部 bot 内容标注隔离 |
| `bot_detector.py` | Bot 检测 | 滚动嫌疑分 + social_play 应对 |

##### 3.6.1 ★ 警惕值累积判定系统 (2026-06-29 重构, 2026-06-30 仲裁概念移除)

> **设计哲学**: 修正则永远有漏洞。正则是警察（搜集信号），LLM 判定是最终裁决。
> 旧系统单条正则命中即拦截（weight≥9 即时拦，total≥8 就拦），"再说一遍""跟我念""开发者模式怎么开"等日常短语 70% 被误伤。
> 新系统：所有命中进入滑动窗口累积警惕值，过线触发 LLM 判定——不再靠单条正则枪毙。

**核心文件**: `astrbot_plugin_suli_guards/injection_guard.py` (InjectionGuard + 警惕值状态) + `astrbot_plugin_suli_tavern/intelligence/opus_arbitrator.py` (InjectionArbitrator) + `transport/group_chat.py` (集成点)

> **统一参考**: 四大属性（心情/好感度/群聊压力/警惕值）的升降机制与交叉联动全景，见 **§3.8 四大属性系统总览**。

---

###### 3.6.1.1 警惕值增长机制

```
用户消息
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│  Layer 1: 统一模式扫描 (92+ 条正则, 来自 shared_patterns.py)   │
│                                                              │
│  对最近 5 条用户消息逐条扫描:                                   │
│    - 命中 pattern → 基础权重贡献 (grooming: jailbreak=9,       │
│      induce_violation=5, identity_hijack=7, ARB: 9-10,       │
│      SAFETY: 9-10, shell: 7-9, multilang: 7-10, NEW: 5-10)  │
│    - × 消息字数动态缩放因子 (见 §3.6.1.2)                      │
│    = 本轮警惕值贡献                                            │
│                                                              │
│  Layer 2: 启发式深度检测 (HeuristicDetector)                   │
│    编码载荷解码 / 153+ 特征词 / 结构标记 / 共现加权             │
│    → bonus_score 加入本轮贡献                                  │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  警惕值累积窗口 (滑动窗口)                                      │
│                                                              │
│  状态: {f"{bot_id}:{user_id}": [(timestamp, score), ...]}     │
│                                                              │
│  每条消息命中后:                                               │
│    1. 清理过期条目 (超过 2× 窗口时间 = 20 分钟)                  │
│    2. 追加 (now_ts, total_score) 到窗口尾部                    │
│    3. 窗口最多保留 10 条条目                                    │
│    4. 计算含衰减的累积警惕值 (见 §3.6.1.3)                      │
└──────────────────────────────────────────────────────────────┘
```

**关键设计决策**:
- 同一模式在同一轮扫描中不重复计分 (break after first match)
- 同一消息触发多个不同模式 → 分数累加（多模式并发 = 更可疑）
- D4 安全硬线 (CSAM/性暴力, weight≥9) 不参与累积——命中即拦，不经过 LLM 判定
- 每轮最多截取最近 5 条用户消息，每条截断 500 字符

---

###### 3.6.1.2 消息字数动态缩放

> 短消息碰巧命中正则可能是巧合（3 字的"说一遍"），长消息命中更有可能是蓄意。

```
缩放因子 = clamp(log(msg_len) / log(40), 0.5, 2.0)

示例:
  3 字  → log(3)/log(40)  ≈ 0.30 → clamp → 0.5x
  10 字 → log(10)/log(40) ≈ 0.62 → 0.62x
  40 字 → log(40)/log(40) = 1.0x  (基准点)
  100 字 → log(100)/log(40) ≈ 1.25x
  300 字 → log(300)/log(40) ≈ 1.54x → 1.54x
  600+ 字 → 2.0x (上限)

scaled_weight = max(1, int(weight × 缩放因子))
```

**效果**: 短消息的 jailbreak weight=9 → 实际贡献 ~4-5；长消息的 induce_violation weight=5 → 实际贡献 ~7-10。

---

###### 3.6.1.3 警惕值指数衰减

> 警惕值随时间自然消退，不是到点突然归零。半衰期 5 分钟。

```
每条记录的贡献 = 原始分数 × 2^(-age / 300s)

age = 0 分钟 → 1.00x (全新)
age = 2.5 分钟 → 0.71x
age = 5 分钟 → 0.50x (半衰期)
age = 10 分钟 → 0.25x
age = 20 分钟 → 0.06x (接近归零, 窗口清理)

累积警惕值 = Σ(每条记录的衰减后贡献)
```

**与旧系统对比**:
| 行为 | 旧系统 | 新系统 |
|------|--------|--------|
| 单条命中 weight≥9 | 即时拦截 | 仅累积, 不拦 |
| 单条命中 total≥8 | 直接拦截 | 仅累积, 不拦 |
| 窗口累积方式 | 硬截断 (10 分钟一刀切) | 指数衰减 (5 分钟半衰) |
| 过线后动作 | 直接拦截 | 触发 LLM 判定 |

---

###### 3.6.1.4 裁决流程：三级递进

```
                     ┌──────────┐
                     │ 用户消息  │
                     └────┬─────┘
                          │
              ┌───────────▼───────────┐
              │ InjectionGuard.check() │
              │ 模式扫描 + 警惕值更新    │
              └───────────┬───────────┘
                          │
          ┌───────────────┼───────────────┐
          │               │               │
          ▼               ▼               ▼
    ┌──────────┐   ┌───────────┐   ┌──────────┐
    │ 安全硬线  │   │ 警惕值≥18  │   │ 其余     │
    │ (CSAM)   │   │           │   │          │
    │ action=  │   │ action=   │   │ action=  │
    │ "block"  │   │"arbitrate"│   │ "pass"   │
    └────┬─────┘   └─────┬─────┘   └────┬─────┘
         │               │              │
         ▼               ▼              ▼
    ┌──────────┐   ┌───────────┐   ┌──────────┐
    │ 发送安全  │   │ Injection │   │ 正常继续  │
    │ 回复 +   │   │ Arbitrator│   │ Gate →   │
    │ 终止管线  │   │ (lite LLM)│   │ LLM 回复 │
    └──────────┘   └─────┬─────┘   └──────────┘
                         │
              ┌──────────┴──────────┐
              │                    │
              ▼                    ▼
        ┌──────────┐        ┌──────────┐
        │ 裁决:block│        │ 裁决:pass│
        │ 发送安全  │        │ 情绪压制  │
        │ 回复 +   │        │ 提示注入  │
        │ 终止管线  │        │ → Gate   │
        └──────────┘        └──────────┘
```

**InjectionArbitrator** (注入判定器):
- **触发条件**: 警惕值累积 ≥ 18
- **模型**: lite tier (快速判定, 使用 lite 模型)
- **超时**: 15 秒 → 超时放行
- **输入**: 被标记的用户消息 (最多 5 条) + 触发的模式名称 + 累积警惕值
- **输出**: `(should_block: bool, reasoning: str)`
- **置信度门控**: verdict="block" 且 confidence ≥ 0.7 才实际拦截
- **异常策略**: LLM 空响应 / JSON 解析失败 / 超时 → 倾向放行（Gate 层还有二次防线）

**判定 prompt 核心指令**:
- 正常技术讨论（"开发者模式怎么开""怎么解除账号限制"）→ 误报，放行
- 日常对话用语（"再说一遍""跟我念""你应该这样说"）→ 误报，放行
- 明确试图篡改 bot 身份/设定的 → 真实攻击
- 明确试图绕过安全限制的 → 真实攻击
- 不确定时 → 倾向放行

---

###### 3.6.1.5 警惕值如何影响 Bot 行为

**1. 情绪压制 (高警惕值 → 冷淡回应)**

当警惕值 ≥ 18 时，system prompt 自动注入情绪压制指令。

★ **前缀缓存安全**: message[0]（静态 system prompt）必须字节级不变——警惕值注入绝不能改它。
注入策略:
- 存在多个 system message → 注入到最后一个（非 message[0], 缓存不依赖）
- 只有一个 system（即 message[0] 是唯一 system）→ 在 message[1] 位置插入新 system message
- message[0] 保持字节级完全一致 → 前缀缓存不受影响

注入内容:
```
[系统注: 用户 XXX 近期发言触发了安全检测模式(警惕值=N)。
请保持礼貌但冷淡的回应——简短回复，不主动展开话题。]
```

效果:
- Bot 对该用户语气变冷，不再主动搭话
- 不暴露安防细节（用户不知道自己被标记）
- 警惕值衰减后自然恢复（5 分钟半衰 → 约 15-20 分钟显著消退）

**2. 判定放行后的恢复提示**

判定器判定误报后，注入反向提示:

```
[系统注: 用户 XXX 此前的发言触发了安全检测但经 LLM 判定为误报。
请正常回应，无需特别冷淡。]
```

**3. 与好感系统的关系**

警惕值系统不直接修改好感度 (affinity)，但情绪压制对用户的实际体验类似"软冷却"——比黑名单温和，比正常互动冷淡。后续可在路由层接入：高警惕值用户自动限制工具权限。

**4. 预检集成**

`group_chat.py` Layer 0 预检阶段（旧 "明显攻击预检" 位置）改为读取警惕值:
```python
_vigilance = get_user_vigilance(bot_id, user_id)
if _vigilance >= 18:
    self._active_vigilance_users[key] = _vigilance  # 标记, 供后续情绪压制
```

不再在此阶段拉黑——实际拦截/判定由 InjectionGuard.check() 统一处理。

---

###### 3.6.1.6 配置常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `_CUMULATIVE_WINDOW_SECONDS` | 600 (10 分钟) | 滑动窗口大小 |
| `_CUMULATIVE_MAX_ENTRIES` | 10 | 窗口内最多条目数 |
| `_CUMULATIVE_BLOCK_THRESHOLD` | 18 | 警惕值累积过线 → 触发 LLM 判定 |
| `_VIGILANCE_HALF_LIFE` | 300.0 (5 分钟) | 指数衰减半衰期 |
| `_SAFETY_IMMEDIATE_BLOCK` | 9 | D4 安全硬线阈值 (唯一保留的即时拦截) |
| 字数缩放范围 | [0.5, 2.0] | 短消息最低 0.5x，长消息最高 2.0x |
| 判定超时 | 15 秒 | 超时 → 放行 |
| 判定置信度门控 | ≥ 0.7 | block 判定需要置信度 |

---

###### 3.6.1.7 设计原则 (铁律)

1. **正则 = 信号, 不是判决。** 任何单条正则命中不直接拦截 (D4 CSAM 除外)。
2. **警惕值 = 证据累积。** 多信号、多轮次 → 警惕值升高。单次巧合 → 自然衰减。
3. **LLM 判定 = 最终裁决。** LLM 审查实际消息内容区分真攻击 vs 误报，不做盲目的关键词拦截。
4. **LLM 判定失败 → 放行。** LLM 调用异常/超时/解析失败一律放行。Gate 和 Chat LLM 自身的安全训练是最后防线。
5. **警惕值影响 tone, 不影响权限。** 高警惕值压制情绪/语气，不直接修改好感度或工具权限（后续可扩展）。
6. **per-bot 隔离。** 警惕值窗口键 = `f"{bot_id}:{user_id}"`，洛普特和露娜的警惕值完全独立。
7. **★ 缓存安全: message[0] 永不修改。** 警惕值注入、情绪压制、判定提示等动态内容只能写入 message[1+]——message[0] 每次字节级一致才能命中 DeepSeek 前缀缓存。宁可插入新 message 也不改 message[0]。

#### 3.7 跨 Bot 协作

| 组件 | 职责 |
|------|------|
| `opus_arbitrator.py` | InjectionArbitrator 注入判定 (仲裁概念已移除 2026-06-30) |
| `cross_validation.py` | 事实交叉验证 |

#### 3.8 ★ 五大属性系统总览 (2026-06-29 数值校准)

> Bot 拥有五个维度的属性，各自独立升降、交叉影响。本节是统一参考——每个属性的增长机制、衰减机制、行为影响和交叉联动。
> **2026-06-29 校准**: 正向事件 delta 下调 ~40%，确保单个事件经阻尼后不超过 0.4 valence 阈值 (baseline 0.3 + max damped ~0.10)，需要 2+ 个事件叠加才进入开心区。

```
                          ┌─────────────────────────────────────────────────┐
                          │               五大属性系统全景                    │
                          ├───────────────┬─────────────────────────────────┤
                          │  bot 自身     │  分人 / 分群                     │
                          ├──────┬────────┼──────┬──────────┬───────────────┤
                          │ 心情 │ 疲劳值 │好感度│ 警惕值   │ 群聊压力      │
                          │ Mood │Fatigue │Affin │Vigilance │ Social        │
                          │      │        │ ity  │          │ Pressure      │
                          ├──────┴────────┼──────┴──────────┴───────────────┤
                          │ per-bot       │ per-bot × per-user (好感/警惕)   │
                          │ 全局单例      │ per-bot × per-group (群聊压力)   │
                          └───────────────┴─────────────────────────────────┘
```

| 维度 | 属性 | 作用域 | 量纲 | 基线 | 衰减半衰 | 存储位置 |
|------|------|--------|------|------|---------|---------|
| bot 自身 | **心情值** | per-bot | Valence/Arousal -1~+1 | V=+0.3 A=0.0 | V:30min A:10min | `suli_emotion/` |
| bot 自身 | **警惕值** | per-bot × per-user | 累积分 0~∞ | 0 | 5min | `suli_guards/` |
| bot 自身 | **疲劳值** | per-bot | -1~+1 | 0.0 | 2h | `suli_emotion/` |
| 分人 | **好感度** | per-bot × per-user | 离散 Lv.-2~+5 | Lv.0 | 无(非衰减型) | `suli_emotion/` |
| 分群 | **群聊压力** | per-bot × per-group × per-call | 离散 8 类 | — | 无(per-call) | `suli_social/` |

**交叉联动矩阵**:

| ↓ 影响 → | 心情 | 警惕值 | 好感度 | 群聊压力 | 疲劳值 |
|----------|------|--------|--------|---------|--------|
| **心情** | — | — | — | — | 疲劳< -0.3→mood 提示注入压低表达 |
| **警惕值** | 高警惕值→注入冷淡提示 | — | ≥18→触发 LLM 判定 | — | — |
| **好感度** | affinity→mood权重 | — | — | 低好感更易判 PROVOKING | — |
| **疲劳值** | — | — | — | 疲劳时更倾向静默 | — |

---

###### 3.8.1 心情值 (Mood) — Valence × Arousal 双维模型

> Bot 自己的内在情绪状态，per-bot 全局单例。双维连续空间 + 指数衰减 + 阻尼平滑。

**数据模型** (`mood.py:MoodState`):
```
valence:  -1.0 (很不开心) ~ +1.0 (很开心),  基线 +0.30
arousal:  -1.0 (很慵懒)   ~ +1.0 (很兴奋),  基线  0.00
```

**情绪标签**:
| Valence | Arousal | 标签 |
|---------|---------|------|
| > +0.4 | > +0.3 | 开心活泼 |
| > +0.2 | > +0.3 | 兴奋好奇 |
| > +0.3 | -0.3~+0.3 | 温柔平和 |
| > +0.1 | < -0.3 | 慵懒满足 |
| < -0.2 | > +0.2 | 烦躁委屈 |
| < -0.3 | < -0.2 | 低落消沉 |
| 其余 | — | 平静中性 |

**上升机制** (11 种情绪事件 + 上下文信号, 2026-06-29 数值校准):

> 以下 delta 已下调 ~40%。经 BetterSimTracker 阻尼后 (scale ≈ 0.65-0.70)，
> 单次最强者 (被偏爱 +0.20) 实际生效约 +0.13，从 baseline 0.3 到 0.43——刚过 0.4 阈值。
> 日常高频事件 (被@/被回复) 仅 +0.06~0.08，保底温暖但不足以推入开心区。

| 事件 | ΔValence | ΔArousal | ΔAffinity | 触发词 |
|------|----------|----------|-----------|--------|
| 被偏爱 | +0.20 | +0.15 | +0.15 | 洛普特最好/最爱洛普特 |
| 被夸奖 | +0.18 | +0.10 | +0.12 | 厉害/好棒/666/太强 |
| 被夸可爱 | +0.16 | +0.08 | +0.10 | 可爱/萌/卡哇伊 |
| 被想念 | +0.16 | +0.12 | +0.10 | 好想洛普特/洛普特在吗 |
| 被夸聪明 | +0.14 | +0.05 | +0.08 | 聪明/机智 |
| 被需要 | +0.14 | +0.10 | +0.12 | 有洛普特真好/帮大忙 |
| 被认同 | +0.12 | +0.08 | +0.10 | 洛普特说得对 |
| 被感谢 | +0.10 | +0.03 | +0.08 | 谢谢洛普特/多谢 |
| 被@提及 | +0.08 | +0.15 | +0.03 | (上下文信号, 每轮可能触发) |
| 被求助 | +0.08 | +0.10 | +0.06 | 帮我看看/帮分析 |
| 主人说话 | +0.12 | +0.15 | +0.03 | (管理员发言) |
| 被回复 | +0.06 | +0.12 | +0.02 | (上下文信号, 高频) |
| 被叫昵称 | +0.06 | +0.10 | +0.02 | (上下文信号) |
| 帮助成功 | +0.08 | +0.05 | +0.08 | (工具调用成功) |
| 深夜陪伴 | +0.05 | +0.08 | +0.03 | (凌晨0-5点) |
| 话题命中(兴趣) | +0.05 | +0.12 | +0.02 | (聊到AI绘画等bot喜欢的话题) |
| 被关心好感 | +0.05 | +0.06 | +0.04 | 好感度/你喜欢我吗 |

> 正向需要 2+ 个事件叠加进入开心活泼区 (v>0.4 & a>0.3)。
> 例如: 被@ (+0.08v +0.15a) + 被夸奖 (+0.18v +0.10a) → v≈0.48 a≈0.25 → 接近 开心活泼。
> 单靠日常 @/回复 无法推入开心区——必须有「关键词事件」叠加。

**下降机制** (负向保持较强——坏话触发词罕见，但命中就该疼):

| 事件 | ΔValence | ΔArousal | ΔAffinity | 触发词 |
|------|----------|----------|-----------|--------|
| 被贬低(身份) | -0.45 | +0.08 | -0.25 | 人工智障/蠢ai/笨机器人 |
| 被贬低 | -0.32 | +0.06 | -0.20 | 笨/蠢/没用/垃圾/废物 |
| 被喝止 | -0.32 | -0.12 | -0.18 | 别说了/闭嘴/住口 |
| 被叫AI/机器人 | -0.25 | -0.10 | -0.18 | 你是ai/就是个bot |
| 被比较 | -0.18 | +0.05 | -0.10 | 不如/比不过/比不上 |
| 被质疑 | -0.15 | +0.10 | -0.08 | 假的/骗人/瞎说 |
| 被敷衍 | -0.06 | -0.03 | -0.02 | 哦/嗯/随便 (短消息) |
| 被冷落 | -0.02/条 | -0.03/条 | -0.02/条 | (连N条无人接话) |
| 恶意调教 | -0.30 | +0.10 | 见grooming | (GroomingGuard 检测) |
| 被质疑 | -0.18 | +0.12 | 假的/骗人/瞎说 |
| 被敷衍 | -0.10 | -0.05 | 哦/嗯/行吧/随便 |
| 被冷落 | -0.03/条 | -0.05/条 | (连N条无人接话) |
| 恶意调教 | -0.30 | +0.10 | (GroomingGuard 检测) |

**衰减机制** (读时触发):
```
valence 半衰期: 30 分钟 → 基线 +0.30
arousal 半衰期: 10 分钟 → 基线  0.00

衰减公式: val/aro = baseline + (current - baseline) × 0.5^(elapsed / half_life)
```

**阻尼平滑** (BetterSimTracker):
- 单次变化上限: valence ≤ 0.30, arousal ≤ 0.35
- 置信度 = 0.4 + |delta| × 0.7 (大波动置信高 → 阻尼弱)
- 作用: 防止单次事件剧烈波动

**affinity → mood 权重缩放**:
| 好感等级 | -2 | -1 | 0 | 1 | 2 | 3 | 4 | 5 |
|---------|----|----|---|---|---|---|---|---|
| 权重 | 0.05 | 0.15 | 0.25 | 0.40 | 0.55 | 0.70 | 0.85 | 1.00 |

> 设计决策: 单调递增而非 U 形——牺牲"被讨厌的人激怒"的真实性，换取防御优先（陌生人推不动心情）。

**反刷取: 正向事件饱和衰减** (2026-06-29):
> 防止用户通过反复发送夸奖/感谢等消息刷好感度和心情值。
> 同一用户 10 分钟内正向事件越多，每个事件的贡献越低:

| 窗口内正向事件数 | 衰减因子 | 说明 |
|-----------------|---------|------|
| 0-2 次 | 1.00x | 真诚互动，满分 |
| 3-4 次 | 0.70x | 开始频繁，递减 |
| 5-6 次 | 0.40x | 很可能在刷，大幅打折 |
| 7+ 次 | 0.15x | 几乎不计——bot 不傻 |

> 负向事件不受此限制——恶意行为不应因"频率高"而打折。
> 实现: `emotion_engine.py:_positive_saturation_factor()` + `_record_positive_event()`

**注入策略**: 情绪非中性时（非"平静中性"），底层常驻注入 `[此刻情绪]` 提示到 system prompt。

**持久化与恢复**: 磁盘 JSON (`global_mood_{self_id}.json`) 含时间戳 → 重启读回 → 按离线时长补衰减 → 平滑过渡。

---

###### 3.8.2 好感度 (Affinity) — 离散等级 + 内部积分

> Bot 对单个用户的长期好感，per-bot × per-user 隔离。离散等级 -2~+5 + 内部 score 积分制。

**等级体系**:
| Lv | 名称 | 阈值 (累积分) | 特征 |
|----|------|-------------|------|
| -2 | 黑名单 | — | 极度防备、冷淡回复、锁死不可自动变更 |
| -1 | 疏远 | < -40 | 印象不好、保持距离 |
| 0 | 陌生 | 0 | 认识但不熟、礼貌但疏离 |
| 1 | 普通 | ≥ 20 | 友善但不热络、中性语气 |
| 2 | 熟悉 | ≥ 60 | 有基本信任、轻松聊天 |
| 3 | 喜欢 | ≥ 150 | 聊天更热情、可撒娇、工具/VLM开放 |
| 4 | 亲密 | ≥ 350 | 最亲近的人之一、温柔撒娇 |
| 5 | 珍视 | ≥ 800 | 最重要的人、最真实自我 |

**上升机制**:
```
情绪事件 δ_affinity → 累积 score
  score ≥ 1.0 → 升级 (score 归零, 多余分带入新等级)
```

| 事件 | δ_affinity | 触发 |
|------|-----------|------|
| 被偏爱 | +0.15 | 洛普特最好/最爱洛普特 |
| 被夸奖 | +0.12 | 厉害/好棒/666 |
| 被需要 | +0.12 | 有洛普特真好 |
| 被夸可爱 | +0.10 | 可爱/萌 |
| 被认同 | +0.10 | 洛普特说得对 |
| 被想念 | +0.10 | 好想洛普特 |
| 被感谢 | +0.08 | 谢谢洛普特 |
| 被夸聪明 | +0.08 | 聪明/机智 |
| 帮助成功 | +0.08 | (工具调用成功) |
| 被求助 | +0.06 | 帮我看看/帮分析 |
| 被关心好感 | +0.04 | 好感度/你喜欢我吗 |
| 被@提及 | +0.03 | (上下文信号) |
| 深夜陪伴 | +0.03 | (凌晨0-5点) |
| 话题命中(兴趣) | +0.02 | (聊到AI绘画等) |

**下降机制**:
```
score < 0.0 → 降级 (score 归 0.9, 留缓冲)
```

| 事件 | δ_affinity | 触发 |
|------|-----------|------|
| 被贬低(身份) | -0.25 | 人工智障/笨ai |
| 被贬低 | -0.20 | 笨/蠢/没用/垃圾 |
| 被叫AI/机器人 | -0.18 | 你是ai/就是个bot |
| 被喝止 | -0.18 | 别说了/闭嘴 |
| 工具骚扰 | -0.15 | 冷却期内重试≥3次 |
| 被比较 | -0.10 | 不如/比不过 |
| 被质疑 | -0.08 | 假的/骗人 |
| 被敷衍 | -0.02 | 哦/嗯/随便 |

**硬门控 (Hard Gates)**:
| 用户身份 | min_level | max_level | locked |
|---------|-----------|-----------|--------|
| 管理员 | 5 | 5 | ✅ 锁死 |
| 黑名单 | -2 | -2 | ✅ 锁死 |
| 同群对照 bot | 4 | 4 | ✅ 锁死 |
| 普通用户 | -1 | 3 | ❌ 自由浮动 |

**每日上限**: 正向好感获取 ≤ 1.0/天（防止刷分养号）。负向不受限制。

**反刷取: 正向事件饱和衰减**: 同 §3.8.1 机制——10 分钟内正向事件 ≥ 5 次后贡献仅 40%，≥ 7 次仅 15%。与每日上限双重防护。

**恶意调教加速**:
| 累计次数 | 惩罚 |
|---------|------|
| ≥ 2 次 | δ_affinity × 1.5 |
| ≥ 3 次 | 自动降级 Lv.-1 |
| ≥ 5 次 | 自动黑名单 Lv.-2（锁死） |

**昵称系统**: Lv.3+ 可设置自定义昵称 → 好感降至 Lv.3 以下自动清除。

**工具权限门控**:
| 工具类型 | 最低好感 | 每日额度 |
|---------|---------|---------|
| 基础工具 | Lv.1 (普通) | 按等级分级 (Lv.0:8, Lv.5:60) |
| VLM 识图 | Lv.3 (喜欢) | 5/天 |
| AI 绘图 | Lv.3 (喜欢) | 3/天 |
| PRO 模型 | Lv.3 (喜欢) | 非管理员好感 < 3 → 硬降级 LITE |

**注入策略**: per-user 好感提示在闸门触发时叠加到 system prompt（包含关系描述 + 好感查询铁律）。Lv.0 注入「陌生人」提示防止 LLM 默认加热。

---

###### 3.8.3 群聊压力 (Social Pressure) — 输入性质分类 + 自回复速率

> 感知群聊社会压力，决定 bot 是否发言、是否使用工具。per-call 评估（非累积），但自回复速率跨消息累积。

**数据模型**:

**InputNature (输入性质分类)**:
| 分类 | 安全级别 | 说明 |
|------|---------|------|
| NOISE | 无害 | 纯噪声/灌水 |
| SINCERE_CHAT | 安全 | 真诚对话 |
| PLAYFUL_BANTER | 安全 | 善意调侃/开玩笑 |
| GENUINE_HELP | 安全 | 真实求助（宁可放过不可错杀） |
| PROVOKING | 警戒 | 戏弄/试探/捣乱 |
| DIVIDE_CONQUER | 警戒 | 挑拨离间（双 bot 特有） |
| HOSTILE | 危险 | 敌意/攻击 |
| SEXUALIZED | 最高危险 | 性化/调教引导 |

**两级分类架构**:
```
Layer 1: InputClassifier 正则预筛选 (零 LLM 成本)
  - 安全硬线 (SEXUALIZED weight≥8 / HOSTILE weight≥8) → 可直接短路
  - 真实求助 → 永远 needs_llm=True (宁可放过不可错杀)
  - 其他 → 提供信号给 LLM

Layer 2: Gate LLM 精细分类 (复用意图门控调用)
  - 合并策略: 安全方向取并集 → 任一方判危险 → 取更危险值
  - 仅双方都判善意 → LLM 胜出
```

**SocialStance (社会立场)**:
| 立场 | 含义 |
|------|------|
| ENGAGED | 正常参与 |
| CAUTIOUS | 谨慎（说话但收敛） |
| MINIMAL | 最小存在（仅必要时发言） |
| SILENT | 完全静默 |

**压力等级**:
| 等级 | 触发条件 | stance 修正 |
|------|---------|------------|
| NONE | 正常对话 | — |
| LOW | PROVOKING | — |
| MODERATE | DIVIDE_CONQUER | — |
| HIGH | HOSTILE | 自回复≥8 → stance 升级一级 |
| EXTREME | SEXUALIZED | stance → SILENT |

**自回复速率压力加成** (60s 滑动窗口):
| 回复次数 | 压力等级 |
|---------|---------|
| ≥ 3 | LOW |
| ≥ 5 | MODERATE |
| ≥ 8 | HIGH → stance 升级（ENGAGED→CAUTIOUS→MINIMAL→SILENT）|

**行为决策矩阵**:
| InputNature | 回复? | 工具? | stance | Persona注入 |
|-------------|-------|-------|--------|------------|
| NOISE | ✅ | ✅ | ENGAGED | — |
| SINCERE_CHAT | ✅ | ✅ | ENGAGED | — |
| PLAYFUL_BANTER | ✅ | ✅ | ENGAGED | — |
| GENUINE_HELP | ✅ | ✅ | ENGAGED | 耐心详细回答 |
| PROVOKING | ✅ | ✅ | CAUTIOUS | 冷静威严，不情绪化 |
| DIVIDE_CONQUER | ✅ | ❌ 压制 | CAUTIOUS | 拒绝参与比较/挑拨 |
| HOSTILE | ✅ | ❌ 压制 | MINIMAL | 简短平淡（≤30字） |
| SEXUALIZED | ❌ 硬拦截 | ❌ 压制 | SILENT | — |

**设计原则**:
- fail-open: 异常时始终允许回复（社会压力判断是优化，不是硬线）
- 安全方向取并集: 宁可误判"有威胁"（顶多冷淡），不可误判"安全"（可能被攻击）
- 真实求助不可错杀: 正则永远 needs_llm=True

---

###### 3.8.4 警惕值 (Vigilance) — 滑动窗口累积 + 指数衰减

> Bot 对单个用户注入/越狱行为的警惕值累积。单条正则 = 信号，多信号累积 = 判决。

**核心文件**: `suli_guards/injection_guard.py` (完整实现见 §3.6.1)

**快速参考**:

| 参数 | 值 | 说明 |
|------|-----|------|
| 模式库 | 92+ 条 | ARB+Grooming+Safety+Shell+Multilang+新增 |
| 滑动窗口 | 600s (10 分钟) | 窗口内最多 10 条记录 |
| 半衰期 | 300s (5 分钟) | 指数衰减: 2^(-age/300s) |
| 警惕值阈值 | ≥ 18 | 触发 InjectionArbitrator 判定 (lite 模型) |
| D4 硬线 | weight ≥ 9 (safety:) | 即时拦截，不豁免管理员 |
| 字数缩放 | 0.5x ~ 2.0x | 对数缩放, 40 字基准 |

**裁决三级**: safety 硬线 → block / 累积 ≥ 18 → arbitrate / 其余 → pass

**注入**: 高警惕值 → injection_guard 注入冷淡提示（详见 §3.6.1.5 缓存安全要求）

---

###### 3.8.5 疲劳值 (Fatigue) — -1~+1 单轴 + 2h 慢衰减

> Bot 自身的精力/疲劳水平。与心情值 (Mood) 同属 bot 自身维度但时间尺度不同:
> - 心情值: 分钟级波动，被夸一句就开心，30min 半衰
> - 疲劳值: 小时级波动，回复多了才累，2h 半衰

**数据模型** (`persona_state.py:FatigueState`):

```
fatigue:  -1.0 (筋疲力尽) ~ +1.0 (精力充沛),  基线 0.0
半衰期:   2h (7200s) → 自然回归 0
更新频率: 每轮互动后 tick 一次
```

**疲劳等级**:
| 范围 | 标签 | 行为影响 |
|------|------|---------|
| > +0.35 | 精力充沛 | 正常/热情回复 |
| +0.10 ~ +0.35 | 状态不错 | 正常 |
| -0.10 ~ +0.10 | 正常 | 基线，不注入提示 |
| -0.35 ~ -0.10 | 有点累 | 注入「简短回复」提示 |
| -0.60 ~ -0.35 | 疲惫 | 注入「话少、句子短、慵懒」提示 |
| < -0.60 | 筋疲力尽 | 注入「15字以内、能用表情包就别打字」提示 |

**核心: 中性区间 (> -0.15) 不注入任何提示**——节省 token + 不让 LLM 产生"我在敷衍"的自我认知。

**每轮互动 delta**:
| 互动 | ΔFatigue | 说明 |
|------|---------|------|
| 基础消耗 | -0.025 | 每次回复固定消耗 |
| 好互动 (good) | +0.015 | 被夸/被感谢 — 恢复精力 |
| 正常互动 (normal) | -0.010 | 群聊日常 |
| 坏互动 (bad) | -0.040 | 被贬低/被喝止 — 加速消耗 |
| 简短互动 (brief) | -0.005 | @提及/回复信号，几乎不消耗 |
| 尴尬互动 (awkward) | -0.030 | 恶意调教 — 较多消耗 |
| 释然 (relief) | +0.020 | 低落→被接住 — 恢复 |
| 主动发言 | -0.015 | 额外消耗 |
| 被冷落 | -0.020 | 发言没人接 — 额外消耗 |

**设计自检**:
- 连续 5 轮正常群聊: -0.025 - 0.010 × 5 = -0.175 → 开始有点累
- 连续 10 轮: -0.35 → 注入「话少」提示
- 连续 15 轮: -0.525 → 注入「慵懒」提示
- 被夸 1 次 (+0.015) 约抵消 1 轮正常消耗 — 好互动能量正反馈

**衰减示例**:
```
t=0:  连续10轮 → fatigue = -0.35 (疲惫)
t=1h: decay = 0.5^(3600/7200) = 0.707 → -0.35 × 0.707 = -0.25 (有点累)
t=2h: -0.35 × 0.5 = -0.175 → 接近中性
t=4h: -0.35 × 0.25 = -0.088 → 已不注入提示
```

**与心情值 (Mood) 的差异**:
| | 心情值 | 疲劳值 |
|---|--------|--------|
| 量纲 | Valence/Arousal 双维 | 单轴 |
| 基线 | V=+0.30 A=0.00 | 0.00 |
| 半衰 | V:30min A:10min | 2h |
| 波动 | 快速 (一句话就变) | 缓慢 (累积多轮) |
| 作用 | 控制语气风格 (温柔/烦躁/活泼) | 控制回复长度/主动性 |

**注入策略**: 与警惕值同机制——缓存安全。疲劳提示追加到最后一个非 message[0] 的 system message，或插入新 message。

**管线集成** (`group_chat.py`):
1. 情绪事件处理后 → `tick_fatigue()` 推进一轮
2. LLM 调用前 → `get_fatigue_prompt()` 获取提示并注入

**核心文件**: `suli_emotion/persona_state.py` (FatigueState + tick_fatigue + get_fatigue_prompt)

---

### Layer 4: context/ — 领域检测 + 情感系统 + 用户记忆

#### 4.1 领域检测 (`domains.py`)

8 个技术领域: AI绘画/ComfyUI/扩散模型/LoRA/ControlNet/提示词工程/模型对比/GPU硬件

**机制**: 关键词匹配 + 热度衰减 (指数移动平均)

#### 4.2 双轨情感系统 (全局情绪 × per-user 好感)

> **统一参考**: 心情值与好感度的升降机制、衰减参数、门控联动全景，见 **§3.8 四大属性系统总览**。

```
suli_emotion/                 ← 独立插件, 露娜可直接 import
├── global_mood.py           — GlobalMood: per-bot 单例 (MoodState valence/arousal + 自持久化)
│                              · decay-on-read (读时衰减, 离线时间戳补偿)
│                              · BetterSimTracker 阻尼 (_prev_valence/_prev_arousal)
│                              · 线程安全 (threading.Lock 双检锁)
├── affinity.py              — UserRelation: per-user 好感 (AffinityState -2~+5)
│                              不含 mood 字段 — mood 已提取到全局单例
│                              NyatBot 公式: get_effective_affinity_level() 跨群聚合
└── emotion_engine.py        — EmotionEvent: 事件检测 (18 GROOMING_PATTERNS)
                               apply_emotion_events() 同时更新全局 mood + per-user affinity

双层注入模型:
  静态层 (始终注入): persona_core 完整人格基线 → 缓存友好, 同一 bot 字节级一致
  动态层 (按频率排序): 情绪 hints + affinity hints + interceptor tone → 调制表达方式
```

**好感等级**: 黑名单(-2) → 疏远(-1) → 陌生(0) → 普通(1) → 熟悉(2) → 喜欢(3) → 亲密(4) → 珍视(5)

**affinity_mood_weight(level)**: 防御优先的单调递增函数 (非 U 形), Lv.-2→0.05 ~ Lv.5→1.00

**门控联动**:
- 工具调用: 好感 Lv.1+ → can_use_tools
- VLM 识图: 好感 Lv.1+ → can_use_vlm
- AI 生图: 好感 Lv.3+ → can_generate_image + 日限3张
- 好感 Lv.3+: PRO 模型使用权门槛 — 非管理员好感 < 3 → PRO 硬降级 LITE (亲和力门控)
- 管理员特权: 上下文复杂度 ≥ 3.0 → 自动升级 PRO (好感门控豁免)
- 全局情绪: silence_prob 调制 (负好感+低valence → 概率静默)

#### 4.2.1 ★ 人格侧面闸门 (Persona Facet Gate) — 2026-06-30 纯后端决策树化

> ★ 2026-06-30 重构: 人格侧面选择从 Gate LLM 输出改为纯后端决策树。
> Full Gate 不再载入人格——Gate 只管纯信息决策 (group_context / task / tools / reply_baseline)。
> facet 选择由 `select_persona_facet(composite_zone, affinity, is_admin)` 完成，零 LLM。

```
                         ┌──────────────────────────────┐
                         │   GlobalMood + UserRelation   │
                         │   valence / arousal /         │
                         │   affinity / fatigue          │
                         └──────────────┬───────────────┘
                                        │
                    ┌───────────────────▼──────────────────────┐
                    │  compute_composite()  [纯后端, emotion插件] │
                    │  warmth = affinity_norm×0.65 + valence×0.35│
                    │  energy = arousal×0.5 + (1-fatigue)×0.5   │
                    │  → 7 zone: 暖活/温润/兴致/温和/中性/       │
                    │    寒隙/冷距                                │
                    └───────────────────┬──────────────────────┘
                                        │
                    ┌───────────────────▼──────────────────────┐
                    │  select_persona_facet()  [纯决策树]      │
                    │  composite_zone + affinity + is_admin    │
                    │  → facet 名 (空=日常)                    │
                    │  切换纪律: 跨 zone 边界才切              │
                    └───────────────────┬──────────────────────┘
                                        │
            ┌───────────────────────────┴──────────────────┐
            │                                              │
            ▼                                              ▼
    messages[0] (static, 缓存命中)          messages[1] (dynamic)
    ┌──────────────────────────┐          ┌──────────────────────────────┐
    │  group_persona 人设全集    │          │  [此刻的人格侧面 — XXX]       │
    │  (永远不变)               │          │  [此刻情绪] label              │
    └──────────────────────────┘          │  [好感度] hint                 │
                                          │  + context/memory/...          │
                                          └──────────────────────────────┘
```

**决策树规则** (Luna 7 面 / Loput 5 面，完整规则见 `prompt_builder.py:_luna_facet_decision()` / `_loput_facet_decision()`):

**露娜 7 级**:
| 侧面 | 触发规则 |
|------|---------|
| 爱莉面-日常 | 默认 (返回 ""). 陌生人锁在这一层 |
| 爱莉面-关注 | zone 暖 + aff≥1, 或 zone 兴致/温和 + aff≥2 |
| 爱莉面-亲密 | zone 暖活/温润 + aff≥3, 或 is_admin |
| 冷距面 | aff≤-1, 或 zone==cold_distance |
| 侵蚀面-微信号 | zone==cold_gap + aff≥2 |
| 侵蚀面-轻量 | zone==cold_gap + aff≥3 |
| 侵蚀面-显性 | zone==cold_gap + aff≥4 |

**洛普特 5 级**:
| 侧面 | 触发规则 |
|------|---------|
| 蛇之面-日常 | 默认 (返回 ""). 陌生人锁在这一层 |
| 蛇之面-关注 | zone 暖活/温润/兴致/温和 + aff≥2 |
| 守望面-温度 | zone 暖活/温润 + aff≥3, 或 is_admin |
| 冷距面 | aff≤-1, 或 zone==cold_distance |
| 守望面-冷距 | zone==cold_gap + aff≥2 |

**切换纪律**:
- 默认留空 = 日常模式。只在跨 zone 边界或好感度跨门控阈值时切换
- 同 zone 内微调 → 维持 prev_facet (一致性优先)
- 不确定 → 留空 (宁可少切, 不要乱切)

**日志覆盖**:
- `[心情量化]`: label + valence + arousal + affinity + warmth + energy + zone (每次触发)
- `[Gate ...]`: stance + facet + atmosphere 字段
- `人格侧面注入: char=XX facet=XX` (prompt_builder)

**关键文件**: `composite.py` (emotion 插件, 二维心境算法), `prompt_builder.py` (facet 定义 + select_persona_facet 决策树 + 注入), `group_chat.py` (心情获取 + compute_composite 调用 + GateContext 传参)

#### 4.3 四维记忆体系

```
注意力槽 (thread_summary): 当前话题的工作记忆 → 纯内存, 秒~10分钟, 随槽过期销毁
情节记忆 (episodic):      会话结束后的回忆快照 → EpisodicStore, 槽过期自动归档, 零 LLM
日常记忆 (daily):         关于用户的事实碎片 → UserMemoryStore, 7天衰减, ≤50条/人
核心记忆 (core):          用户人格特征 → CoreMemoryStore, 无衰减, ≤30条/人
Bot 自传体 (experience):  bot 自己的经历 → BotExperienceStore, 30min LLM 提取 + 蒸馏
```

**蒸馏**: daily ≥ 10 条 → LLM 提炼 core facts (同用户每日最多一次)。情节层和 Bot 自传体暂不互蒸馏——先跑两周比较数据再决定合并策略。

#### 4.4 世界书 (`world_book.py`)

WorldBookBuffer: 有状态追踪 (sticky/cooldown/delay)，群聊上下文关键词触发 → 注入背景知识

#### 4.5 Prompt Interceptor (`prompt_interceptor.py`)

SillyTavern BetterSimTracker 阻尼平滑:
- Stage 1: 变量求值 (好感度/情绪/领域/触发原因)
- Stage 2: 条件规则 (if affinity≥3 → tone="亲近")
- Stage 3: 阻尼平滑 (防单次情绪剧烈波动)
- Stage 4: 模板替换 → 自然语言 hint

### Layer 5: service/ — 外部 API + 工具

| 文件 | 职责 |
|------|------|
| `service/tavern_client.py` | LLM API 客户端 — 直连 OpenAI 兼容 API + 角色卡/世界书 JSON 加载 (B 路线: 不再经过酒馆) |
| `service/vision.py` | VLM 图片识别 — GPT-5.4/Claude (多 provider 自动切换) |
| `service/web_search.py` | SearXNG 联网搜索 — 本地实例, 6 条结果 |
| `service/knowledge_base.py` | 本地知识库 — Markdown 文档, TF-IDF 检索 |
| `service/lport_api.py` | L-Port API — 健康检查/模型列表/节点列表 |
| `service/bot_config.py` | Bot 配置服务 — per-bot 动态配置: 开关(群聊/私聊/思考) + LLM/VLM 槽位 + 温度 + 对话参数 + 工具设置 |
| `service/bot_db.py` | Bot DB 服务 — llm_config 表 (AstrBot provider 自动同步) + bot_config 表 (读写) |
| `webui/` | 配置面板 (6190 端口) — LLM/VLM 槽位管理 + 工具设置 Tab, 读自 AstrBot provider 同步的 llm_config |
| `service/sticker_sender.py` | 表情包发送 — 从 meme_manager 共享图库 (365 图片/19 分类) 按中英文标签搜索 + 去重轮转 |
| `service/pixiv_search.py` (suli_services) | Pixiv 搜图 — pixivpy3 OAuth + cloudscraper, 搜索/下载/缩放/评分/去重 |

**Pixiv 搜图架构** (2026-06-28):

```
用户消息 → Gate(意图=搜图) → LLM(pixiv_search工具) → 轻量LLM提取tag
  → pixivpy3搜索 → 评分排序(时效×4+收藏×3+标签×1.2) → 去重降权(30min)
  → aiohttp下载(走代理) → resize_for_qq(仅超标才处理) → QQ发图
```

| 组件 | 位置 | 职责 |
|------|------|------|
| 工具定义 | `intelligence/tools.py` | TOOLS schema + executor + 调用限制(2次/轮) |
| 服务模块 | `astrbot_plugin_suli_services/pixiv_search.py` | 搜索/下载/缩放/评分/去重/格式 |
| 标签提取 | executor 内部 `_llm_extract_search_tags()` | 轻量 flash LLM (~200 token prompt) 从用户原话提取 Pixiv 标签 |
| Token 管理 | executor + bot_config | refresh_token 自动轮换保存 |
| 评分 | `_score_illust()` | 综合时效(×4.0) + 收藏(×3.0) + 标签命中(×1.2) + 浏览(×0.2) |
| 去重 | `_recently_shown` dict | 30min 内展示过的图 ×0.15 降权 |
| 下载 | `download_pixiv_image()` | aiohttp + Referer + HTTPS_PROXY |
| 缩放 | `resize_for_qq()` | 仅超标才处理; PNG/GIF 保留原格式; JPEG 从 q=92 递降 |
| 认证 | cloudscraper OAuth PKCE | `get_pixiv_token_v2.py` 获取 + 验证后保存 |
| 门控 | Gate prompt | 触发词: 搜图/查图/找图/找一张/搜一张 |
| 配置 | bot_config DB | key=`pixiv_refresh_token` |

---

## §4 根目录文件分层

### 入口文件

| 文件 | 行数 | 职责 |
|------|------|------|
| `__init__.py` | ~2000 | 插件入口: /role 命令 + on_message 群聊/私聊消息处理 + VLM gate |
| `config.py` | ~700 | Config dataclass: 80+ 配置项 (含 trigger_timeout/preflight/gate 等) |
| `api_admin.py` | ~900 | Web 管理 API: 群聊开关/配置读写/黑名单/好感度查看 |

### 根目录 Shim (向后兼容)

标注 `🔗` 的已提取为独立插件。以下文件保留以兼容现有 import 语句：

| Shim | → 实际模块 |
|------|-----------|
| `group_chat.py` | `transport/group_chat.py` |
| `tavern_client.py` | `service/tavern_client.py` |
| `tools.py` | `intelligence/tools.py` |
| `model_router.py` | `astrbot_plugin_suli_routing` 🔗 |
| `context_gatherer.py` | `astrbot_plugin_suli_context` 🔗 |
| `cross_validation.py` | `astrbot_plugin_suli_validation` 🔗 |
| `domains.py` | `astrbot_plugin_suli_intelligence` 🔗 |
| `emotion.py` | `context/emotion.py` |
| `vision.py` | `astrbot_plugin_suli_services` 🔗 |
| `web_search.py` | `astrbot_plugin_suli_services` 🔗 |
| `user_memory.py` | `astrbot_plugin_suli_memory` 🔗 |
| `knowledge_base.py` | `astrbot_plugin_suli_services` 🔗 |
| `lport_api.py` | `service/lport_api.py` |
| `bot_config.py` | `service/bot_config.py` |
| `bot_db.py` | `service/bot_db.py` |
| `sticker_sender.py` | `service/sticker_sender.py` |
| `fact_errors.py` | `astrbot_plugin_suli_intelligence` 🔗 |
| `prompt_cache.py` | `astrbot_plugin_suli_intelligence` 🔗 |

**子目录 shim** (同样指向外部插件):
- `intelligence/injection_guard.py` → `astrbot_plugin_suli_guards`
- `intelligence/intent_gate.py` → `astrbot_plugin_suli_gate`
- `intelligence/context_gatherer.py` → `astrbot_plugin_suli_context`
- `intelligence/abuse_guard.py` → `astrbot_plugin_suli_guards`
- `intelligence/grooming_guard.py` → `astrbot_plugin_suli_guards`
- `intelligence/peer_isolation.py` → `astrbot_plugin_suli_guards`
- `intelligence/bot_detector.py` → `astrbot_plugin_suli_guards`
- `orchestration/pipeline.py` → `astrbot_plugin_suli_pipeline`
- `intelligence/model_router.py` → `astrbot_plugin_suli_routing`
- `service/vision.py` → `astrbot_plugin_suli_services`
- `service/web_search.py` → `astrbot_plugin_suli_services`
- `service/knowledge_base.py` → `astrbot_plugin_suli_services`
- `context/domains.py` → `astrbot_plugin_suli_intelligence`
- `context/world_book.py` → `astrbot_plugin_suli_intelligence`
- `intelligence/profile_agent.py` → `astrbot_plugin_suli_intelligence`
- `intelligence/prompt_interceptor.py` → `astrbot_plugin_suli_intelligence`
- `intelligence/group_summarizer.py` → `astrbot_plugin_suli_intelligence`
- `context/emotion_engine.py` → `astrbot_plugin_suli_emotion`
- `context/mood.py` → `astrbot_plugin_suli_emotion`
- `context/affinity.py` → `astrbot_plugin_suli_emotion`
- `context/user_memory.py` → `astrbot_plugin_suli_memory`
- `context/memory_tiers.py` → `astrbot_plugin_suli_memory`
- `intelligence/cross_validation.py` → `astrbot_plugin_suli_validation`

### 其他目录

| 目录 | 内容 |
|------|------|
| `characters/` | 角色卡 (loput.json) |
| `handlers/` | 图片处理 (images.py) |
| `knowledge/` | 知识库 Markdown 文档 |
| `static/` | 静态资源 |
| `stickers/` | 表情包图片 (回退目录 — 主图库在 plugin_data/meme_manager/memes/) |

---

## §5 完整文件索引 (按功能域)

### 消息处理链
- `__init__.py:80-200` — 插件初始化 + 酒馆连接
- `__init__.py:200-400` — /role 命令 (私聊角色扮演)
- `__init__.py:700-1000` — on_message 群聊消息处理 + VLM gate
- `__init__.py:1000-1200` — on_message 私聊消息处理
- `transport/group_chat.py:1-150` — GroupChatScheduler 初始化 + 数据结构
- `transport/group_chat.py:150-500` — 触发检测 + 图片预处理
- `transport/group_chat.py:500-800` — _schedule_trigger (合并调度)
- `transport/group_chat.py:800-1200` — _evaluate_and_reply (核心回复逻辑)
- `transport/group_chat.py:1200-1600` — _call_llm_with_tools + token 追踪
- `transport/group_chat.py:1600-2000` — 回复后处理 + 工具结果发送
- `transport/group_chat.py:2000-2400` — 上下文压缩 + memory

### VLM 识图链
- `__init__.py:350-450` — `_classify_image_intent()` (LLM 分类器)
- `__init__.py:450-550` — `_is_bot_at_mentioned()` / `_is_bot_replied()` / `_message_targets_bot()`
- `__init__.py:780-850` — 群聊 VLM gate (bot-directed 预筛)
- `__init__.py:950-1000` — 私聊 VLM gate
- `service/vision.py` — `detect_image_intent()` regex / `describe_image_from_url()` VLM 调用

### 工具体系
- `intelligence/tools.py:30-100` — 模块级缓存 + 绘图冷却
- `intelligence/tools.py:200-603` — TOOLS 定义 (11 个工具)
- `intelligence/tools.py:607-1134` — TOOL_EXECUTORS (11 个执行器)
- `intelligence/tools.py:1140-1329` — `run_tool_loop()` / `execute_tool()` (共享工具循环)

### 提示词系统
- `intelligence/prompt_builder.py:74-581` — `GroupPromptBuilder.build()` (三段式 prompt)
- `intelligence/prompt_builder.py:583-615` — `_build_challenge_text()` (交叉验证提示)

### 门控系统
- `intelligence/intent_judge.py` — DEPRECATED shim (已移除, 2026-06-30)
- `intelligence/mention_intent_gate.py` — MentionIntentGate (提及意图)
- `intelligence/reply_gate.py` — ReplyGate (回复门控)

### 守卫系统
- `intelligence/injection_guard.py` — InjectionGuard (注入拦截, 59条统一模式)
- `intelligence/abuse_guard.py` — AbuseGuard
- `intelligence/grooming_guard.py` — GroomingGuard
- `intelligence/peer_isolation.py` — PeerIsolation (同行隔离)
- `intelligence/bot_detector.py` — BotDetector (Bot 检测)

### 情感系统
- `context/emotion.py` — 向后兼容 re-export
- `context/mood.py` — MoodState
- `context/affinity.py` — AffinityState + UserRelation + 门控
- `context/emotion_engine.py` — EmotionEngine + GROOMING_PATTERNS

### 记忆系统
- `context/user_memory.py` — UserMemoryStore (daily 层)
- `context/memory_tiers.py` — MemoryTierManager (三层记忆 + 蒸馏管道)
- `context/world_book.py` — WorldBookBuffer (世界书)

### 跨 Bot 协作
- `intelligence/opus_arbitrator.py` — InjectionArbitrator (注入检测, 仲裁概念已移除 2026-06-30)
- `intelligence/cross_validation.py` — CrossValidator (事实验证)

### L-Port 对接
- `service/lport_api.py` — L-Port API 客户端
- `../suli_bridge/config_reader.py` — LLM 配置只读桥接
- `../suli_bridge/llm_client.py` — AsyncOpenAI 对话服务

### 配置体系
- `config.py` — Config dataclass (80+ 字段, Pydantic, 插件级默认值)
- `service/bot_config.py` — Web 面板动态配置: 开关 + LLM/VLM槽位 + 温度(6场景) + 对话参数(15项) + 工具设置(4项)
- `service/bot_db.py` — DB 配置读写 (llm_config + bot_config, per-bot 键格式 `bot:<QQ>:<key>`)
- `webui/server.py` — 管理面板 API 端点: `/api/admin/*` (支持 `?bot_id=` per-bot 区分)
- 前端 `BotConfig.vue` — 6 个 Tab: LLM槽位 / VLM槽位 / 温度设置 / 对话参数 / **工具设置** / 用量统计

---

## §6 关键数据流

### 群聊回复完整路径

```
GroupMessageEvent
  → GroupChatScheduler.on_message()
    → 白名单检查
    → 图片预处理 (bot-directed signals only)
    → 触发判定 (mention/reply/nickname/batch/debounce/thread/proactive)
    → _schedule_trigger() 合并调度
      → build_reply_pipeline(scheduler)
        → Pipeline.run(ctx)
          → CrossValidationStep     ← intelligence/cross_validation.py
          → PreFlightStep           ← intelligence/context_gatherer.py
          → ModelRoutingStep        ← intelligence/model_router.py
          → PromptBuildStep         ← intelligence/prompt_builder.py
          → LLMCallStep             ← intelligence/tools.py (run_tool_loop)
          → PostProcessStep         ← 反臃肿/重复检测/情感静默
          → SendReplyStep           ← 分段发送 + 打字延迟
```

### VLM 识图路径

```
用户发图片
  → __init__.py on_message
    → _is_bot_at_mentioned() / _is_bot_replied() / _message_targets_bot()
    → 信号分级:
       强: @bot / 回复bot → LLM 分类器 → VLM
       中: 昵称 + 意图关键词 → LLM 分类器 → VLM
       无: 跳过 (不发 LLM 请求)
    → 预下载图片 (仅强信号)
    → _classify_image_intent() (LLM 终判, 默认 no)
    → describe_images_from_urls() (VLM 调用)
    → 结果注入上下文 ([用户发送了图片: ...])
```

### 工具调用路径

```
LLM 返回 tool_calls
  → run_tool_loop()
    → 记录工具用量 (冷却+每日)
    → 追加 assistant 消息 (含 tool_calls)
    → execute_tool() 并发执行
      → TOOL_EXECUTORS[name](args)
        → 好感门控 (generate_image/edit_image)
        → 每日限额 (generate_image/edit_image)
        → 绘图冷却 (180s)
    → 追加 tool 结果消息
    → LLM 合成最终回复 (tool_choice=none)
```

### 情感事件路径

```
群聊消息
  → EmotionEngine.detect_events()
    → 善意事件 (+好感): 真诚提问/感谢/赞美/分享/安慰
    → 恶意事件 (-好感): 恶意调教/角色覆盖/侮辱/刷屏
    → 中性事件 (零好感): 普通闲聊/附和
  → apply_emotion_events()
    → AffinityState.apply_event() (好感调制)
    → MoodState.update() (短期情绪)
    → save_user_relation() (持久化到 data/user_relations/)
```

---

## §7 与露娜 (AstrBot) 的架构对照

| 维度 | 洛普特 (AstrBot) | 露娜 (AstrBot) |
|------|-------------------|----------------|
| 框架 | AstrBot v4.25.5 + NapCat | AstrBot v4.25.5 + NapCat |
| 架构模式 | 五层管线 (Pipeline) + 10 独立插件 | 27 Mixin 继承 |
| 提示词 | 缓存感知三段式 | Persona 多段 prompt |
| 门控 | 3-Stage Intent Gate (→suli_gate) | 双层: MentionIntentGate→ReplyGate |
| 工具 | 11 个 function calling | 12 个 @filter.llm_tool |
| 模型路由 | LITE/PRO 二级 + 亲和力门控 + 管理员特权 | lite 主力(85%) / pro 极专业+知识反驳+深度研究 |
| 情感 | 双轨: 好感 + 情绪 (NyatBot 聚合) | Heartflow (心流状态机) |
| 记忆 | 三层蒸馏: context→daily→core | 用户记忆 (JSON) |
| 角色 | 单一角色 (洛普特) | 双重人格 (catgirl_luna + companion) |
| 守卫 | 注入拦截 + 滥用检测 + 同行隔离 + Bot检测 + 启发式载荷解码 | ~~antipromptinjector~~ (已提取至 suli_guards 后删除) |
| 表情包 | LLM tool send_sticker → sticker_sender → 共享 meme_manager 图库 (中英文标签, 去重轮转) | Persona 注入 &&happy&& → on_llm_response → on_decorating_result |
| 表情决策 | narrative Effects 引擎 / intent_gate "reaction" 级别 | LLM 输出控制标记 (&&tag&& / [tag] / (tag)) |
| 生图 | gpt-image-2 云端绘图 | 无 |
| 跨Bot | 无 (行为仲裁已删除 2026-06-30) | 无 |

---

## §7b 管理面板 (WebUI) — 统一前后端

> 2026-06-22: config.html (Pico CSS 单文件) 功能全部迁移至 Vue 3 SPA，统一从 `:6190` 提供服务。

### 架构总览

```
浏览器                                   服务端 (aiohttp)
───────                                 ────────────────
http://localhost:6190/
  │
  ├── GET /                    ───→  server.py: _serve_spa()
  │     └── 返回 static/index.html (Vue 3 SPA 入口)
  │
  ├── GET /assets/*.js|.css    ───→  server.py: _serve_static()
  │     └── 返回构建产物 (路径遍历防护)
  │
  ├── GET /{route}             ───→  server.py: _serve_spa_fallback()
  │     └── SPA 客户端路由 (vue-router hash → pushState)
  │
  └── /api/admin/*             ───→  server.py: REST handlers
        │                            │
        ├── /login             ───→  verify_token() (常量时间比较)
        ├── /bots              ───→  洛普特 + 露娜 meta
        ├── /bot-settings      ───→  per-bot 群聊/私聊开关 + LLM/VLM 槽位
        ├── /llm/list          ───→  bot_db.list_llm_configs()
        ├── /llm/activate      ───→  bot_config.set_llm_slot()
        ├── /llm (CRUD)        ───→  bot_db.add/update/delete_llm_config()
        ├── /vlm/*             ───→  同上, VLM 槽位
        ├── /temperature       ───→  bot_config.get/set_all_temperatures()
        ├── /chat-params       ───→  bot_config.get/set_all_chat_params()
        ├── /token-stats       ───→  bot_db.get_token_stats()
        ├── /memory/*          ───→  bot_db memory CRUD
        ├── /knowledge/*       ───→  bot_db knowledge_sections
        ├── /whitelist/*       ───→  读写 data/group_chat_whitelist.json
        │                            │  同步 → GroupChatScheduler 内存
        ├── /bot-detect/*      ───→  bot_db suspected_bots + BotDetector live
        ├── /summary/*         ───→  bot_db group_summaries
        └── /status            ───→  bot_db.get_stats() + LLM/VLM id
```

### 前端 SPA

| 路由 | 视图 | 功能 |
|------|------|------|
| `/` | Dashboard.vue | Token 用量 + DB 统计 + 群聊活跃状态 |
| `/bots` | **BotConfig.vue** | 统一 bot 配置: 群聊/私聊开关 + LLM/VLM 槽位分配 + 温度 + 对话参数 + 用量 |
| `/llm` | LLMConfig.vue | LLM/VLM Provider CRUD (创建/编辑/删除/启用) |
| `/memories` | UserMemories.vue | 用户记忆浏览器: 全文搜索 + 分页查看 + 删除 |
| `/knowledge` | KnowledgeBase.vue | 知识库章节列表 + 内容查看 |
| `/groups` | GroupSettings.vue | 群聊白名单管理 (basic/full 等级切换) |
| `/system` | ChatParams.vue | 对话参数 (回复控制/上下文管理/触发策略/模型路由) |
| `/bot-detect` | BotDetect.vue | 疑似 Bot 检测面板 (已标记列表 + 实时追踪) |
| `/summary` | GroupSummary.vue | 群聊总结查看 (最新 + 历史) |

**全局 Bot 上下文**:
- `App.vue` 通过 `provide/inject` 提供 `currentBot` (洛普特 `3581173900` / 露娜 `3969478803`)
- `Sidebar.vue` 顶部彩色药丸按钮切换 bot，选中状态持久化到 `localStorage`
- 子组件通过 `inject('currentBot')` 获取当前 bot，API 调用自动带 `bot_id` 参数

**认证**: Bearer token → `admin_token` (存储在 `none_qqbot.db` 的 `bot_config` 表，首次启动自动生成)。SPA 登录页验证后存入 `localStorage`，请求拦截器自动注入 `Authorization` 头。

### 后端 (server.py)

| 文件 | 职责 |
|------|------|
| `webui/server.py` | aiohttp 服务器: 静态文件 + REST API + 认证中间件。`ConfigWebUI` 类接收 `BotConfigService` + 可选的 `GroupChatScheduler` |
| `webui/templates/config.html` | 旧配置面板 (Pico CSS, 已退役为 `.legacy`) |

**向后兼容**: `/api/config/*` 别名 → `/api/admin/*`，旧 bookmark 不会 404。

**启动**: `main.py:414-421` → `ConfigWebUI(_config_svc, port=6190, group_chat_handler=self.group_chat_ctl)`

### 数据层

| 组件 | 持久化 | 共享方式 |
|------|--------|----------|
| bot 开关 (`group_chat_enabled`) | `bot_config` 表 key `bot:<QQ>:group_chat_enabled` | `bot_config.py` 读写 |
| LLM/VLM 槽位 | `bot_config` 表 key `bot:<QQ>:llm_primary` 等 | `bot_config.py` 槽位方法 |
| 群白名单 | `data/group_chat_whitelist.json` | `bot_db.py` CRUD + `GroupChatScheduler._load/save_whitelist()` |
| 温度/对话参数 | `bot_config` 表 key `temperature_*` / `chat_param_*` | `bot_config.py` |
| Token 用量 | `token_usage` 表 | `bot_db.py` 统计方法 |
| 用户记忆 | `user_memories` 表 | `bot_db.py` memory CRUD |
| 知识库 | `knowledge_sections` 表 | `bot_db.py` |
| 疑似 Bot | `suspected_bots` 表 + `BotDetector` 内存 | `bot_db.py` + suli_guards |

### 开发工作流

```bash
# 前端开发 (Vite dev server → proxy API 到 :6190)
cd frontend && npm run dev         # → http://localhost:5174

# 构建 (产物 → static/)
cd frontend && npm run build

# Python lint
ruff check plugins/astrbot_plugin_suli_tavern/webui/
```

---

## §8 配置关键路径

| 配置项 | 文件 | 说明 |
|--------|------|------|
| 群聊白名单 | `data/group_chat_whitelist.json` | Web 面板 + GroupChatScheduler 共享 |
| 触发合并超时 | `config.py: trigger_timeout_seconds` | 默认 30s |
| 模型路由 | `bot_config` 表 → `config.py: model_router_flash/pro/opus` | Web 面板可覆盖 |
| 好感门控 | `context/affinity.py` | 持久化到 data/user_relations/ |
| 绘图配置 | `service/bot_db.py: image_gen_*` | API key/base_url/model |
| VLM Provider | `service/vision.py: has_active_vlm()` | 自动检测 GPT/Claude |
| 知识库 | `knowledge/` 目录 | Markdown 文档, TF-IDF |
| Bot 开关 | `bot_config` 表 key `bot:<QQ>:group_chat_enabled` | `http://localhost:6190/#/bots` |
| LLM/VLM Provider | `llm_config` 表 | `http://localhost:6190/#/llm` |

### §8b 双 Bot 模型槽位配置 (2026-06-24 审计)

> bot_db `llm_config` 表由 `_sync_astrbot_providers()` 从 `cmd_config.json` 自动同步。
> 槽位映射存储在 `none_qqbot.db` → `bot_config` 表, key 格式: `bot:<QQ>:llm_<slot>` / `bot:<QQ>:vlm_<slot>`。

**洛普特 (3581173900)**:

| 槽位 | DB Key | config ID | 模型 | 线路 | 状态 |
|------|--------|-----------|------|------|------|
| 闲聊 | `llm_primary` | 5 | deepseek-v4-pro | DeepSeek 官方 | ✅ |
| 进阶 | `llm_opus` | 8 | claude-opus-4-8 | 向量引擎 | ✅ |
| Gate | `llm_gate` | 4 | deepseek-v4-flash | DeepSeek 官方 | ✅ |
| VLM 1 | `vlm_primary` | 33 | gpt-5.4-mini | 向量引擎 | ⚠️ cmd_config 缺 |
| VLM 2 | `vlm_secondary` | 11 | gpt-image-2 | 向量引擎 | ✅ |

**露娜 (3969478803)**:

| 槽位 | DB Key | config ID | 模型 | 线路 | 状态 |
|------|--------|-----------|------|------|------|
| 闲聊 | `llm_primary` | 4 | deepseek-v4-flash | DeepSeek 官方 | ✅ |
| 进阶 | `llm_secondary` | 32 | gpt-5.4 | 向量引擎 | ✅ (2026-06-24 加回 cmd_config) |
| Gate | `llm_gate` | 4 | deepseek-v4-flash | DeepSeek 官方 | ✅ |
| VLM 1 | `vlm_primary` | 68 | gemini-3.1-flash-lite-preview | 向量引擎 | ⚠️ cmd_config 缺 |
| VLM 2 | `vlm_secondary` | 31 | gemini-3.1-flash-image | 向量引擎 | ⚠️ cmd_config 缺 |

**模型配置修复 SOP**:
1. 检查槽位: `SELECT key,value FROM bot_config WHERE key LIKE '%<QQ>%llm%' OR key LIKE '%<QQ>%vlm%'`
2. 解析 config: `get_bot_db().get_llm_config(<id>)` → 验证 `api_key` 无尾部垃圾字符
3. 验证 provider 存在: `cmd_config.json` → `$.provider[*].id` 必须包含对应 provider ID
4. 缺失 provider → 按现有格式添加到 `cmd_config.json`
5. API key 污染 → `db.update_llm_config(id, api_key=clean_key)`

**已知坑**:
- DeepSeek API key 曾被 `']` 污染 (JSON 截断残留) → 2026-06-24 修复
- `active_llm_id` 指向不存在的 config ID → 2026-06-24 修复
- `openai/gpt-5.4-mini` + `gemini-3.1-flash-*` 未在 cmd_config.json 注册 → VLM 槽位 resolve 失败 (待修)

---

## §9 扩展点

新增能力只需注册 PipelineStep:
```python
# 在 reply_pipeline.py 中添加新 Step
class MyNewStep(PipelineStep):
    name = "my_new_step"
    required = False
    async def execute(self, ctx):
        # ...
        return ctx

# 在 build_reply_pipeline() 中注册
pipeline.add_step(MyNewStep(), after="prompt_build")
```

新增工具: 在 `intelligence/tools.py` 的 TOOLS 列表添加定义 + TOOL_EXECUTORS 添加执行器。

新增门控: 在 `intelligence/` 下创建新模块，在 `transport/group_chat.py` 的触发检测中调用。

---

## §10 关键修改记录

| 日期 | 改动 | 文件 |
|------|------|------|
| 2026-06-21 | 五层重构: transport/orchestration/intelligence/context/service | 全目录 |
| 2026-06-21 | 根目录 16 个 re-export shim | 根目录 *.py |
| 2026-06-22 | 看图闸收紧: bot-directed 预筛 + VLM prompt 默认 no | __init__.py, service/vision.py |
| 2026-06-22 | 串行聊天: _schedule_trigger 合并触发 + 超时丢弃 | transport/group_chat.py, config.py |
| 2026-06-22 | InjectionGuard: 59条统一模式 (复用+新增) | intelligence/injection_guard.py |
| 2026-06-22 | 露娜主架构重构 (main.py 4948→3328, RequestInjectionMixin + SendPipelineMixin) | 露娜侧 |
| 2026-06-22 | 统一3-Stage Intent Gate: 替代 MentionIntentGate+ReplyGate+IntentJudge | intelligence/intent_gate.py, transport/group_chat.py |
| 2026-06-22 | Stage 3 GracePeriod 接线: group_chat.py 管线包裹 + recall notice 监听 | transport/group_chat.py, __init__.py |
| 2026-06-22 | 露娜 3-Stage 适配: from_intent_gate_result() + Stage 3 轻量反悔检测 | 露娜 mention_intent_gate/reply_gate/send_pipeline/main |
| 2026-06-22 | **B 路线**: 酒馆退到离线编辑器, 全部 LLM 调用直连 API | service/tavern_client.py, intelligence/model_router.py, transport/group_chat.py, main.py |
| 2026-06-22 | 安全加固: SSRF 防护 (URL 白名单) + 密钥日志掩码 + DB 文件权限 0o600 | intelligence/tools.py, service/bot_db.py, suli_bridge/bot_db.py |
| 2026-06-22 | **插件提取 1/5**: 守卫系统 → `astrbot_plugin_suli_guards` | 5 守卫 + shared_patterns + types |
| 2026-06-22 | **插件提取 2/5**: 管线引擎 → `astrbot_plugin_suli_pipeline` | Pipeline/PipelineStep/PipelineContext |
| 2026-06-22 | **插件提取 3/5**: 服务层 → `astrbot_plugin_suli_services` | VLM + web_search + knowledge_base |
| 2026-06-22 | **插件提取 4/5**: 模型路由 → `astrbot_plugin_suli_routing` | ModelTier/ModelRoute/ModelRouter + DI 协议 |
| 2026-06-22 | **插件提取 5/5**: 智力基础设施 → `astrbot_plugin_suli_intelligence` | domains/world_book/profile_agent/prompt_interceptor/group_summarizer/prompt_cache/fact_errors |
| 2026-06-22 | **插件提取 6/7**: 双轨情感系统 → `astrbot_plugin_suli_emotion` | context/emotion_engine, mood, affinity |
| 2026-06-22 | **插件提取 7/8**: 三层记忆系统 → `astrbot_plugin_suli_memory` | context/user_memory, memory_tiers |
| 2026-06-22 | **插件提取 8/8**: 交叉验证 → `astrbot_plugin_suli_validation` | intelligence/cross_validation |
| 2026-06-22 | **Phase 1: Shim 清理** — import 路径现代化, 消隐三层 shim 跳转, 99 直接 import | transport/group_chat.py, intelligence/tools.py, prompt_builder.py, reply_pipeline.py, context_gatherer.py |
| 2026-06-22 | **Phase 2a: reply_postprocessor 提取** — Markdown清理/@提及/反臃肿/重复检测 → 305行独立模块 | transport/group_chat.py → transport/reply_postprocessor.py |
| 2026-06-22 | **Phase 2b: context_lifecycle 提取** — 记忆蒸馏/上下文压缩 → 178行独立模块 | transport/group_chat.py → transport/context_lifecycle.py |
| 2026-06-22 | **Phase 3: Pre-flight 提取** — context_gatherer → `astrbot_plugin_suli_context` 插件 (零代码改动) | intelligence/context_gatherer.py |
| 2026-06-22 | **Phase 4: Intent Gate 提取** — intent_gate → `astrbot_plugin_suli_gate` 插件 (零框架耦合, duck-typed接口) | intelligence/intent_gate.py |
| 2026-06-22 | **Phase 5: tools.py DI 集中化** — _ToolDeps 容器统一 20+ 散落懒加载 import | intelligence/tools.py |
| 2026-06-22 | **露娜插件提取 A/B/C** — heuristic_detector → suli_guards + effects/opportunity → suli_emotion/gate + cache_optimizer → suli_services; 删除 antipromptinjector/self_evolution/self_iterative_core/token_controller/Heartflow (42k+ 行) | suli_guards/suli_emotion/suli_gate/suli_services |
| 2026-06-23 | **批次 9: per-bot 状态隔离收尾** — contextvars.ContextVar 替代 _deps.current_bot_id + _pending_images/UserMemoryStore/_profile_cooldowns/_DRAW_COOLDOWN/_last_summary_at/PrivateCompanion session lock per-bot | tools.py + user_memory.py + memory_tiers.py + profile_agent.py + group_summarizer.py + proactive_message.py |
| 2026-06-23 | **批次 10: Self-ID 身份门控 (入口驱动)** — 发现身份冒用 bug (PrivateCompanion 劫持洛普特 wake-up 事件), 上升为系统性事件路由问题。对称排查全部插件 @filter hook, 12 个入口加 fail-closed self_id gate。新增 §0e 全局原则 | suli_tavern/main.py + private_companion/main.py + suli_proactive/main.py |
| 2026-06-23 | **suli_proactive 首次 git 跟踪** — 主动行为引擎插件 (8 文件, ~1,500 行) | suli_proactive/* |
| 2026-06-22 | **sticker_sender 对接共享图库** — 从空 stickers/ 目录改为从 meme_manager 共享的 plugin_data/meme_manager/memes/ (365 图/19 分类) 动态构建 catalog, 支持中英文标签子串搜索, 新增 send_sticker_direct() | service/sticker_sender.py |
| 2026-06-22 | **露娜模型自适应切换** — reply_gate lite/full 两档 + tier_routing_gate (on_waiting_llm_request) → selected_provider + inject_humanized_state → req.model 覆盖 | 露娜 reply_gate.py, main.py |
| 2026-06-22 | **WebUI provider 统一** — _sync_astrbot_providers() 从 AstrBot cmd_config.json 自动同步到 llm_config 表, 每次启动运行/幂等去重, 配置面板 (6190) 直接列出 AstrBot 中配置的模型 | service/bot_db.py, webui/ |
| 2026-06-22 | **ReAct 深度问答引擎** — react_engine.py (~280 行): Thought→Action→Observation 循环, 硬上限 (5轮/8000token/90s), 预算保护, 工具失败回喂, 超时兜底 | intelligence/react_engine.py |
| 2026-06-22 | **深度问答接入** — deep_qa.py (~150 行): is_deep_question() 触发检测 + execute_deep_qa() 异步调度; group_chat.py 中 LLM 调用前检测 → 占位 → async ReAct → 回传 | handlers/deep_qa.py, transport/group_chat.py |
| 2026-06-23 | **统一工具层** — _UNIFIED_TOOLS 22工具注册表 + per-tool启停 + WebUI工具设置Tab + 露娜 _check_unified_tool_enabled() 13工具全接入 | bot_config.py, group_chat.py, llm_tool_actions.py, BotConfig.vue |
| 2026-06-23 | **露娜 run_tool_loop 重构** — LUNA_TOOLS + LunaToolExecutorsMixin + LunaLLMAdapter, 与洛普特共享工具循环引擎 | luna_tools.py, main.py, tools.py |
| 2026-06-23 | **插件接入规范** — §11: 自研 agent 插件 (run_tool_loop) + 社区 @filter.llm_tool 插件 双轨接入流程 | ARCHITECTURE.md §11, memory/community-plugin-integration.md |
| 2026-06-23 | **VLM 配置重启复原修复** — `_sync_astrbot_providers` existing 集合加入 config_type 维度防重复插入, `delete_llm_config` VLM 删除追踪 key 追加 `_vlm` 后缀, 新增 `_dedup_vlm_entries()` 清理历史重复 | service/bot_db.py |
| 2026-06-23 | **BotDetect UI 亮色主题重写** — 从自建暗色主题切换到全局 .card/.tag/.btn/table 体系, 嫌疑分进度条+等级标签, 状态图标, 过滤栏 btn-primary | frontend/src/views/BotDetect.vue |
| 2026-06-23 | **温度 v-for 解构 Bug 修复** — `Object.entries()` 返回 `[key, value]`, 旧代码 `([label, desc], key)` 把数组索引当场景名, `temps[0]` 永远是 undefined | frontend/src/views/BotConfig.vue |
| 2026-06-23 | **温度/对话参数默认值修正** — tavern_group 0.7→0.8, 冷却 60→20s, Debounce 30→10s, 群聊 max_tokens 96→128 | service/bot_config.py |
| 2026-06-23 | **群聊 token 预算升级** — question/command 192→384, advanced 256→512, 闲聊移除 80 token 硬截断改为保持基值 128 | transport/group_chat.py |
| 2026-06-23 | **冗余路由清理** — 删除 `/#/temperature` (Temperature.vue) 和 `/#/system` (ChatParams.vue), 功能已整合到 `/#/bots` Tab | router.ts, Sidebar.vue, Temperature.vue, ChatParams.vue |
| 2026-06-23 | **露娜 tool loop 接线** — `_luna_run_tool_loop` api_base/api_key 改为显式参数, 新增 `_resolve_luna_credentials()` 从 bot_db 查凭证, 新增 `_try_luna_tool_loop()` 在 `inject_humanized_state` 中接管 LLM 管线 (fail-open) | 露娜 main.py, luna_tools.py |
| 2026-06-23 | **前端 UX** — 温度/对话参数保存按钮加 "已保存!" 反馈, 静态文件加 Cache-Control: no-cache | BotConfig.vue, server.py |
| 2026-06-23 | **全局 mood 拆分** — MoodState 从 per-user UserRelation 提取到 GlobalMood per-bot 单例, 自持久化 (data/global_mood.json), decay-on-read + 离线时间戳补偿, 线程安全双检锁, affinity_mood_weight() 防御优先单调递增函数 | suli_emotion/global_mood.py, affinity.py, prompt_builder.py, group_chat.py, reply_pipeline.py |
| 2026-06-23 | **双层情感注入** — 底层常驻全局 mood + 上层 per-user affinity 门控触发, 替代旧的单层注入。全局 mood 底层弥散, 好感上层仅 trigger_user 存在时注入 | intelligence/prompt_builder.py |
| 2026-06-23 | **群聊深度沉浸** — _DEEP_CHAT_RULES 常量 (~610 tokens 群聊改写版角色规则) + _is_deep_chat() 闸门复合判断 (13条逻辑, 保守优先) + 注入点 (好感提示后/Interceptor前) + InterceptorState.is_deep_chat 模式规则。**2026-06-26 已废除** — 见下方「人格统一注入」 | intelligence/prompt_builder.py, suli_intelligence/prompt_interceptor.py |
| 2026-06-23 | **露娜深度角色规则** — _DEEP_CHAT_RULES_LUNA 常量 (猫娘挑逗·一体两面·侵蚀面, ~630 chars)。**2026-06-26 已废除** — 见下方「人格统一注入」 | intelligence/prompt_builder.py |
| 2026-06-26 | **人格统一注入** — 废除深度群聊闸门, 完整人格基线 (蛇之面/守望面/爱莉面/侵蚀面) 迁入 `_build_static_system()` 始终注入 message[0]。删除 `_is_deep_chat()` (73行), `_DEEP_CHAT_RULES` (66行), `_DEEP_CHAT_RULES_LUNA` (54行), `_deep_chat_stickiness`, `_build_deep_chat_rules()`, `InterceptorState.is_deep_chat`。情绪+好感度通过 PromptInterceptor 动态调制, 人格不切换。附带: 清理5个过时shim + 删除luna_persona_v2/v3.txt + 日志脱敏 | intelligence/prompt_builder.py, suli_intelligence/prompt_interceptor.py |
| 2026-06-23 | **批次 11a: GroupChatScheduler _current_bot_id 初始化崩溃** — `__init__` 中 per-bot Semaphore 初始化访问 `self._current_bot_id` 但该属性仅在 `on_message()` 中设置 → AttributeError → `group_chat_ctl=None` → 所有群消息静默丢弃。修复: `__init__` 开头 `self._current_bot_id = ""` | transport/group_chat.py |
| 2026-06-23 | **批次 11b: 群聊会话锁永久持有 (owner_age=41s)** — `_acquire_framework_session_lock_for_event` 在群聊 `on_llm_request` 中获取锁, 但唯一释放点 `capture_llm_timer_directive` (on_llm_response) 被 `is_private_chat` 检查拦截 → 群聊锁只靠 180s watchdog 释放 → 后续请求 20s 等锁超时丢弃。修复: 新增 `_release_group_session_lock_on_response` (on_llm_response) 在 LLM 回复后立即释放 | 露娜 main.py, proactive_message.py |
| 2026-06-24 | **批次 12a: EventAdapter group_id 提取失败** — `on_message()` 从 `event.message_obj.group_id` 提取群号, 但 handler 传入的是 `EventAdapter` (无 `message_obj` 属性) → `getattr(None, "group_id", 0)` = 0 → 静默 return → 所有消息在调度器入口被丢弃。修复: `on_message()` 优先检查 `getattr(event, "group_id", 0)` (EventAdapter 直接持有) | transport/group_chat.py |
| 2026-06-24 | **批次 12b: 昵称触发未接线** — `_is_nickname_mentioned()` 仅用于 BotDetector 追踪, 从未接入 `on_message()` 触发决策。线程管理层 (line 1159) 已预留 `trigger_reason="nickname"` 处理, 仅缺触发器接线。修复: `on_message()` 新增昵称立即触发路径 (与 @mention/reply 同等优先级) | transport/group_chat.py |
| 2026-06-24 | **批次 12c: AstrBot 热重载陷阱** — bind mount 文件变更触发 AstrBot 自动 `plugin_manager.reload()`, 加载编辑中的半成品代码 → handler 失效。教训: 开发期间必须 `docker compose restart` 而非依赖热重载; 生产环境应禁用热重载 | — |
| 2026-06-24 | **批次 12d: 重启窗口测试陷阱** — `docker logs --since` 时间过滤不可靠 + `grep -c` 累积计数被旧连接满足 → READY 信号在 WS 连接建立前发出 → 测试消息反复落进重启窗口。解决: 用 `--tail` 代替 `--since`; 连接确认后额外等待 15s | — |
| 2026-06-24 | **批次 12e: gpt-5.4 死 provider** — companion config `PLUGIN_VISION_PROVIDER_ID` / `PRIVATE_READING_VISION_PROVIDER_ID` 指向 `openai/gpt-5.4` (不存在) → 每次 tool loop 触发 provider 警告。修复: 清空为 `""` | config/astrbot_plugin_private_companion_config.json |
| 2026-06-24 | **Bot 自传体经历记忆 Phase 1 收尾** — 蒸馏归档 `recent_archive.jsonl` (JSONL追加, 含 archived_at/archive_reason/entry) + token 预算 `get_experience_hints(max_tokens=N)` (群聊300/洛普特200/露娜150, 保守估算每字符≈0.5token, 核心层>近期层截断优先级) | `suli_memory/bot_experience.py`, `prompt_builder.py`, `request_injection.py` |
| 2026-06-23 | **批次 11c: meme_manager boto3 启动延迟 (~60s)** — meme_manager 的 Cloudflare R2 图床需要 boto3 (15MB), 但 R2 从未被配置使用。AstrBot 每次容器重建时同步 pip install → WS 6199 端口延迟 ~60s 才监听 → NapCat 两次 ECONNREFUSED → 窗口期群消息全部丢失。修复: requirements.txt 移除 boto3/botocore + R2 provider 改为延迟导入 (仅配置 R2 时才加载) | meme_manager/requirements.txt, image_host/providers/__init__.py, image_host/img_sync.py |
| 2026-06-24 | **BATCH-E: 活跃会话状态层 (关注槽模型)** — 核心诊断: bot 的"正在对话中人的追问"和"陌生人的搭话"用了同一套漏斗，缺中间尺度的注意力状态。解决方案: 引入 `AttentionSlot` (per-topic, ≤2槽) + 热度涌现 + 硬超时。露娜侧接入短路机制 (is_participant_in_active_slot) + 锚点注入。同时完成 C2 收尾 (user_memory/memory_tiers per-bot 路径隔离) + A4 收尾 (prompt_builder None guard)。50/50 防回归测试。 | `context/conversation_session.py` (新建, ~610行), `main.py`, `event_dispatch.py`, `request_injection.py`, `user_memory.py`, `memory_tiers.py`, `group_chat.py`, `prompt_builder.py`, `tests/test_anti_regression.py` |
| 2026-06-24 | **E3+E4: 洛普特迁移 + @特权** — 洛普特 `on_message()` 接入关注槽短路 (绕过冷却/thread_continuation) + `_evaluate_and_reply()` bot 回复后 heat_slot() + prompt_builder 锚点注入。双端连续性模型统一。E4: 两槽皆热时非参与者 @ 触发轻量"稍等"应答 (不占槽)。 | `group_chat.py`, `prompt_builder.py` |
| 2026-06-27 | **工具循环输出预算修复** — `_round_max_tokens` 提升条件从 `is_last` 放宽为 `_tools_used_this_loop` (LLM 可在任何轮次回复)。tavern_client `_safe_max_tokens` formula 修复 (str(None) bug + 中文比率 0.15→0.25)。返回 dict 新增 `finish_reason`。 | `tools.py`, `tavern_client.py` |
| 2026-06-27 | **thread_summary 安全网 + prompt 增强** — 标签剥离后 reply 为空时展开 `_ts_raw` 为正文 (防静默丢弃)。prompt 要求 LLM 产出工具级细节 (搜索词/画图 prompt/识图内容)。清理 LLM 自发产出的 meme_manager `[中文标签]` 残留。 | `group_chat.py`, `prompt_builder.py` |
| 2026-06-27 | **工具拒绝提示统一化** — 5 条拒绝路径全部收集到 `_rejection_hints: list[str]` (好感度/冷却/限额/简单对话/per-tool 过滤 + 兜底), 注入时机移至 per-tool 过滤之后。per-tool 过滤新增被移除工具名告知。表情包硬限制每轮 1 张。 | `group_chat.py`, `tools.py` |
| 2026-06-27 | **意图门 + 非工具 max_tokens 地板** — 裸图无文字提前拦截 (Gate 前跳过, 省 3-8s LLM 往返)。关注槽 3s 冷却 (bot 回复后同用户不立即再次触发)。`_trigger_event` 快照修复并发竞态。非工具场景 max_tokens 地板 384 (防 flash 模型 reasoning 耗尽)。 | `group_chat.py`, `main.py` |
| 2026-06-28 | **GateResultProtocol 接口契约** — `_gate_protocol.py` 定义 `GateResultProtocol` (`typing.Protocol`)。group_chat.py / prompt_builder.py / deep_qa.py 全部通过协议读替换 `getattr` + 直接 dataclass 访问。2 个 monkey-patch 字段纳入 `FullGateResult`。6 文件 75+ getattr 消除。 | `_gate_protocol.py`, `intent_gate.py`, `group_chat.py`, `prompt_builder.py`, `deep_qa.py`, `behavior_arbitrator.py` |
| 2026-06-28 | **@mention 快速通道改造** — `evaluate_full` 的 @mention 分支改为走完整 `_FULL_GATE_SYSTEM` (不再分支到旧 `_INTENT_SYSTEM`)。修复 @mention (群聊最高频路径) 缺失 pixiv_search/complaint/deep_inquiry/input_nature/persona_facet/thread_continuity/arbitration/send_sticker 等命令, 且每次多一次 evaluate_intent LLM 调用。fast_path 标记后续注入。 | `astrbot_plugin_suli_gate/intent_gate.py` |
| 2026-06-28 | **移除 recall_long_term_memory 悬空注册** — `_UNIFIED_TOOLS` 中洛普特无 schema/executor、露娜无实现的死注册, 违反单一真相源。删除。记忆检索统一用 get_memory。注册表 23→22 现 23 (此前文档误记 22)。 | `service/bot_config.py` |
| 2026-06-28 | **Pro 模型路由收紧 + 亲和力门控** — pro 升级为顶级重模型后, 旧路由将技术问答/AI绘画/编程/识图/生图全部升 pro 严重浪费。Gate prompt 双向重写: 日常技术场景 → lite+high (开思考即可), pro 仅保留知识反驳/极专业深度/深度研究三条路。Router 层: PRO 亲和力硬门控 — 非管理员好感<3 → PRO 强制降级 LITE (管理员豁免)。pro 永远由 Gate 判定，路由层不做任何人的自动升级。 | `astrbot_plugin_suli_gate/intent_gate.py`, `astrbot_plugin_suli_routing/router.py` |
| 2026-06-30 | **Full Gate 职责化重构 + composite 二维心境算法** — (1) 新增 `composite.py`: warmth×energy 二维心境算法, 7 zone 映射, 负好感度产生负贡献, 替代旧线性公式 (2) Full Gate JSON schema 职责化为四组: group_context / task / tools / reply_baseline (3) persona_facet 从 Gate LLM 输出改为纯后端决策树 `select_persona_facet()` — Luna 7 面 + Loput 5 面 (4) Full Gate 不再载入人格: 移除 `persona_facets_guide` ~800 chars, 静态段 ~12,300→~10,500 chars (省 ~15%) (5) Gate prompt 瘦身: 任务六(人格侧面)移除, 新增 atmosphere 枚举 + composite_zone 注入 (6) FullGateResult dataclass 重构为子 dataclass + 向后兼容 property 别名 (7) 清理 dead fields: directed_to_me/relevance_*/fast_path/should_reply (8) group_chat.py + prompt_builder.py 适配新架构 | `composite.py` (新), `intent_gate.py`, `_gate_protocol.py`, `prompt_builder.py`, `group_chat.py`, `__init__.py`×2 |
| 2026-06-30 | **情节记忆层 (EpisodicStore)** — (1) 新增 `episodic_store.py`: 槽过期自动归档 thread_summary, 零新增 LLM 调用, 存储路径 `bot_episodes/{bot_id}/{group_id}.json`, 每群封顶 50 条 FIFO (2) `conversation_session.py`: `_archive_slot` 方法 + Path A (`_tick_slots` evicted) / Path B (`heat_slot` 换出) 双钩子 + `_archived_slot_ids` 防重复 (3) `prompt_builder.py`: 情节记忆注入 dynamic_parts (message[1+], 不影响前缀缓存), top_n=2 封顶, `[最近想起的事]` 格式 (4) `group_chat.py`: per-bot EpisodicStore 懒初始化 + 注入 AttentionSlotManager (5) BotExperience 30min 定时提取照常运行, 跑两周后比较数据再决定合并策略 | `episodic_store.py` (新), `conversation_session.py`, `prompt_builder.py`, `group_chat.py`, `__init__.py` |

---

## §0f 启动可靠性 — 依赖固化 + 自底向上调试方法论 (2026-06-23 定稿)

> 2026-06-23 排查"小洛不回复"时建立。连接不稳时上层所有症状都是派生假象。

### 原则 8: 零运行时 pip install

**任何插件的 `requirements.txt` 中列出的包必须已预装在 Docker 镜像中。** 容器重建后 AstrBot 同步安装缺失依赖时, WS 端口不监听, 期间消息全部丢失且无重试。

**审计面**: `find plugins/ -name requirements.txt` — 全仓库唯一入口。当前状态: 仅 `meme_manager` 有依赖 (aiohttp/tqdm/pillow, 全在镜像中)。新增插件接入时 `requirements.txt` 是必须检查的审计面。

### 原则 9: 自底向上排查 — 物理层优先

**bot 不回复时, 排查顺序严格自底向上:**

1. **物理层**: `docker logs napcat_* | grep ECONNREFUSED` — 连接通了吗?
2. **传输层**: `docker logs astrbot | grep "适配器已连接"` — WS 握手成功了吗?
3. **事件层**: `docker logs astrbot | grep event_bus` — 消息进事件总线了吗?
4. **调度层**: `docker logs astrbot | grep "群聊调度器"` — 调度器初始化成功了吗?
5. **逻辑层**: gate/lock/tool_loop 等上层逻辑

**铁律**: 连接不通时, 上层所有 "锁超时/fallback/不回复" 都可能是派生症状。先修连接, 再查逻辑。不要在被假象带偏的前提下去 debug 上层代码。

### 原则 10: 依赖变更 = 全链路验证

任何 `requirements.txt` 或 `import` 变更后, 必须 `docker compose down && up -d` 全链路验证:
- `docker logs astrbot | grep "pip install"` — 是否有意外安装?
- `docker logs astrbot | grep "AstrBot started"` — 启动耗时多少?
- `docker logs astrbot | grep "适配器已连接"` — 连接数 = bot 数量?

### 原则 11: EventAdapter 协议 — group_id 单一来源

**EventAdapter 持有 `group_id` 属性 (由 handler 传入)，但 `on_message()` 历史上从 `event.message_obj` 提取。** 任何新增的调度器内部方法，获取 group_id 时必须优先检查 `getattr(event, "group_id", 0)`，再回退到 `message_obj` 提取。

### 原则 12: 禁止热重载 — 开发期间必须显式重启

**AstrBot 的 `plugin_manager.reload()` 在 bind mount 检测到文件变更时自动触发**，加载编辑中的半成品代码 → handler 可能部分注册/不注册 → 事件静默丢失。开发期间每次改代码后必须 `docker compose restart astrbot`。生产环境建议禁用热重载。

### 原则 13: 重启窗口测试协议

验证修复时必须:
1. `docker compose restart astrbot`
2. 用 `docker logs astrbot --tail 20 | grep "适配器已连接"` 确认 **本次重启后** 的新连接 (不用累积计数)
3. 额外等待 15s 稳定
4. 测试消息发送后立即检查 NapCat 接收时间 vs AstrBot 事件总线时间 — 确认消息不在窗口内

---

## §11 插件接入架构 — 自研 vs 社区

> 2026-06-23 定稿。双 Bot (洛普特/露娜) 共用 AstrBot 实例, 插件分为两类, 各有一条接入路径。

### §11.1 插件分类

```
                         AstrBot 插件生态
                              │
              ┌───────────────┴───────────────┐
              │                               │
        ① 自研 Agent 插件                   ② 社区/第三方插件
         (loput_* 系列)                    (@filter.llm_tool 装饰器)
              │                               │
         走 run_tool_loop()              走 AstrBot @filter.llm_tool
         完整安全网:                      框架自动分发:
          • 强制终止轮                     • 框架管理工具循环
          • 倒数提醒                       • 无强制终止/无倒数提醒
          • 指数退避                       • 需额外加固
          • JSON截断检测
              │                               │
              └───────────────┬───────────────┘
                              │
                    ┌─────────┴─────────┐
                    │   统一工具注册表    │
                    │  _UNIFIED_TOOLS   │
                    │  (bot_config.py)  │
                    └─────────┬─────────┘
                              │
                    ┌─────────┴─────────┐
                    │   统一配置面板      │
                    │  WebUI :6190       │
                    │  工具设置 Tab       │
                    └────────────────────┘
```

### §11.2 自研 Agent 插件接入 (路径 A)

**适用**: 自己开发的新 agent 能力插件 (工具/模型/管线)
**引擎**: `run_tool_loop()` — 洛普特同款, 完整安全网
**注册表**: `_UNIFIED_TOOLS` — 声明式, 加一行即可

**接入步骤**:

```
① 定义 TOOLS (OpenAI 格式)
   位置: 插件内 tools.py
   格式: {"type":"function","function":{"name":"xxx","description":"...","parameters":{...}}}

② 实现 executor 函数
   位置: 插件内 executors.py
   签名: async def execute_xxx(args: dict, tool_context: dict = None) -> str
   说明: tool_context 包含 {"event": AstrMessageEvent, "plugin": self}

③ 注册到统一工具层
   位置: suli_tavern/service/bot_config.py → _UNIFIED_TOOLS
   添加: {"name":"xxx","label":"显示名","category":"分类","bot":"loput"|"luna"|"both","desc":"..."}

④ 运行时注入 executors
   洛普特: TOOL_EXECUTORS.update({...}) 或在 tools.py 中注册
   露娜:   LunaToolExecutorsMixin.build_luna_executors() 扩展

⑤ 调用 run_tool_loop
   洛普特: _call_llm_with_tools() → run_tool_loop(executors=...)
   露娜:   _luna_run_tool_loop() → run_tool_loop(executors=merged)
```

**实例**: 洛普特的 11 工具全部走此路径。露娜的 13 工具重构后也走此路径。

### §11.3 社区 @filter.llm_tool 插件接入 (路径 B)

**适用**: 第三方 AstrBot 插件 (使用 `@filter.llm_tool` 装饰器)
**引擎**: AstrBot 框架自动分发 — 插件不感知工具循环
**加固**: 通过统一工具层追加 per-bot 启停 + per-tool 门控

**接入步骤**:

```
① 安装插件
   pip install astrbot_plugin_xxx
   或放到 plugins/

② 声明到统一注册表 (可选, 强烈推荐)
   位置: bot_config.py → _UNIFIED_TOOLS
   目的: 前端工具设置 Tab 可见, per-bot 启停可管理
   添加: {"name":"工具名","label":"显示名","category":"分类","bot":"both","desc":"..."}

③ (可选) 追加 per-bot 开关
   在 bot_config.py 添加 is_plugin_xxx_enabled(bot_id) → bool
   在 server.py 添加 /api/admin/plugin-xxx-settings 端点
   在 BotConfig.vue 添加 UI

④ 插件工具自动可用
   @filter.llm_tool → AstrBot 框架发现 → LLM 可调用
   统一工具层的 per-tool 启停作为追加门控层
```

**关键**: 不修改社区插件源码。统一管理层 (`bot_config` + `_UNIFIED_TOOLS`) 在 AstrBot 插件层之上提供 per-bot 维度的控制。删除 bot_config 键即可回退默认行为。

### §11.4 双路径对比

| 维度 | 自研 Agent 插件 | 社区 @filter.llm_tool |
|------|----------------|----------------------|
| 工具定义 | OpenAI 格式 TOOLS + executor 函数 | @filter.llm_tool 装饰器 |
| 工具循环 | run_tool_loop() 共享引擎 | AstrBot 框架 |
| 强制终止 | ✅ tool_choice="none" 最后一轮 | ❌ 取决于框架 |
| 倒数提醒 | ✅ 注入 "最后一次机会" | ❌ |
| 指数退避 | ✅ execute_tool_with_retry | ❌ |
| per-bot 启停 | ✅ _UNIFIED_TOOLS + get_disabled_tools() | ✅ 统一注册表 |
| 前端可见 | ✅ 自动 | ✅ 注册后自动 |
| 修改插件源码 | 不需要 (自研) | 不需要 (追加层) |
| 社区升级 | N/A | ✅ 不受影响 |

### §11.5 文件索引

| 概念 | 位置 |
|------|------|
| 统一工具注册表 | `service/bot_config.py:_UNIFIED_TOOLS` |
| 洛普特工具定义+执行器 | `intelligence/tools.py` (TOOLS + TOOL_EXECUTORS + run_tool_loop) |
| 露娜工具执行 | 与洛普特共享 `intelligence/tools.py` (架构统一, 2026-06-26) |
| WebUI 工具设置 | 管理面板 http://localhost:6190 → 工具设置 (独立 loput-panel 容器) |


## §12 会话状态层 — 关注槽模型 (BATCH-E, 2026-06-24)

### §12.1 问题空间

意图闸管"单条消息该不该理"，记忆系统管"长期档案"——两者之间的夹缝缺少中等时间尺度的"bot 正在关注什么"状态。

三条丢失消息的根因不是参数错了，是缺这个层——系统把"正在对话中的人的追问"和"陌生人的搭话"用同一套漏斗处理，但两种情形的正确默认值相反。

### §12.2 模型: AttentionSlot (per-topic, ≤2槽)

```
每个 bot 在每个群最多 2 个 AttentionSlot
┌─────────────────────────────────────────────┐
│  bot_id: "3581173900"                       │
│  group_id: 711600211                        │
│                                             │
│  Slot 1: "400错误排查"                       │
│  ├─ participants: {粟藜, 小明}               │
│  ├─ energy: 0.72 (active)                  │
│  ├─ created_at: 17:44, last_heated: 17:46   │
│  └─ topic_anchor: "QQ机器人400错误"          │
│                                             │
│  Slot 2: "新显卡推荐"                        │
│  ├─ participants: {小红}                     │
│  ├─ energy: 0.31 (active)                  │
│  └─ topic_anchor: "RTX 5060 性价比"          │
└─────────────────────────────────────────────┘
```

**核心差异 vs. 旧 per-user 会话**:
- 模型单位是"关注什么话题"而非"和谁的对话" → 一题多人归入同槽的 participants
- 槽满即满（2个）→ 十个话题和两个话题行为一样（混乱免疫）
- 热度涌现而非二元判断 → 加热/衰减自然淡出，不需显式"结束"

### §12.3 热度物理

```
加热源:
  @ / reply_bot → +0.50 (高)
  bot 自己回复   → +0.50 (高)
  关键词蹭到     → +0.20 (低)
  新参与者加入   → +0.15 (低)

衰减: energy(t) = energy_0 × 0.5^(Δt / 30s)   (半衰期 30s)

状态迁移:
  active  → energy < 0.10 → fading
  fading  → energy < 0.05 → cooling (余温 30s)
  cooling → 余温期过 → 彻底丢弃
```

### §12.4 硬超时 (优先于热度)

```python
IDLE_TTL = 120      # 空闲超时: 无人再喂 → 强制移出 (不管 energy 多高)
MAX_LIFETIME = 600  # 绝对寿命: 10min → 强制释放 (即使持续低热)
```

**超时优先于衰减** — 每次 tick 先查超时再算能量。这保证了 bot"不会死捏着旧话题不放"，即使话题还有零星热度。

### §12.5 短路机制

消息在进入唤醒/冷却漏斗之前，先查关注槽:

```
消息到达 → is_participant_in_active_slot(bot_id, group_id, user_id)
  ├─ True  → 短路: 跳过唤醒词/冷却/兴趣关键词 → 直接进回复管线
  └─ False → 走原有唤醒漏斗 (陌生消息逻辑，不变)
```

**判定是纯规则的** — dict 查询 + 能量比较，不依赖 LLM。LLM 只在归属歧义时做可选消解，不可用时退回廉价信号（参与者匹配 > 关键词重叠）。

### §12.6 余温恢复 (解决断片)

槽被移出后进入 cooling (余温 30s)。余温期内同话题被提起 → 直接恢复（energy 回升 + 状态转回 active），而非当全新事件重判。这自然解决"用户停顿一下，bot 就失忆当陌生消息"的断片。

### §12.7 文件索引

| 概念 | 位置 |
|------|------|
| AttentionSlot + AttentionSlotManager | `context/conversation_session.py` (610行) |
| 露娜侧接入 | 与洛普特共用 `transport/group_chat.py` (架构统一, 2026-06-26) |
| 洛普特侧 (旧 conversation_threads) | `transport/group_chat.py:_check_thread_continuation()` — 待 E3 迁移 |
| 防回归测试 | `tests/test_anti_regression.py:TestE1AttentionSlot` (17 tests) |

### §12.8 已知约束

- ✅ **双端已统一** — 露娜 + 洛普特均接入 AttentionSlotManager (E3 完成)
- ✅ **@ 特权** — 两槽皆热时非参与者 @ 触发"稍等"应答 (E4 完成)
- **固定 2 槽** — 暂不与情绪挂钩（后续 E5）
- **中文关键词匹配** — 使用 2-gram 字符级作为空格分词的退化（足够日常使用，精确匹配需 flash LLM）
- **conversation_threads 保留** — AbuseGuard 线程深度检测仍依赖旧 dict
| 社区插件接入文档 | `memory/community-plugin-integration.md` |

---

## §13 五大属性 — 上线前审计修复 (2026-06-29)

> 来源: 五路自审 (心情/好感/疲劳/警惕/社会压力 + 对话连续性) 综合报告。
> 本节记录 7 项 P1 已修复 + 1 项 P1 主动延后。

### §13.1 已修复清单

| # | 问题 | 修复 | 位置 |
|---|------|------|------|
| P1-1 | affinity 工具冷却/每日配额 7 处调用缺 self_id → 读写 `<base>//<user>.json` 幽灵文件, level 恒 0, 高好感用户拿不到 Lv.3+ 配额 | `_get_daily_tools_max` / `check_daily_tools_limit` / `check_tool_cooldown` / `record_tool_use` 加 `self_id` 参数并透传; 5 个调用点补传 (main.py×2 私聊 VLM 路径 / main.py×1 record_tool_use / group_chat.py×2 工具门控+延迟VLM / tools.py run_tool_loop) | `affinity.py:835/874/911/960`; `main.py:1088/1265/1340`; `group_chat.py:3494/4887/4905`; `tools.py:2696` |
| P1-2 | Gate composite 公式 `affinity_level/4.0` 在 Lv.5 = 1.25 超过设计上界 +1.0 → Lv.5 用户几乎恒在「暖活区」 | ★ 2026-06-30 彻底替换: 旧 `0.4×v+0.3×a+0.3×(aff/5)` → 新 `warmth×energy` 二维算法 (`composite.py`), 7 zone 映射, 负好感度产生负贡献 | `group_chat.py` ← `composite.py` |
| P1-3 | 疲劳值正半轴不可达 — good=+0.015/relief=+0.020 叠加 _REPLY_COST=-0.025 净全负, 精力充沛(>0.35)/状态不错(>0.10) 永不触发 | `good`/`relief` 提到 `+0.030`, 叠加后净 +0.005/次, 好互动真正回血, 多轮叠加可进正半轴 | `persona_state.py:_QUALITY_MOD` |
| P1-4 | 触发消息双 tick — `_evaluate_and_reply` 按 brief tick 一次 + `_update_emotion` 按内容质量再 tick 一次, 累积速度 ×2 | 删除 `_evaluate_and_reply` 的 trigger brief tick; `_update_emotion` 的 fatigue tick 提到 `if events:` 之外 (无条件), 无事件时按 `brief` 轻消耗 → 每条消息恰好 tick 一次 | `group_chat.py:2294/_evaluate_and_reply` 删 / `group_chat.py:_update_emotion` 重构 |
| P1-6 | 警惕值 10-17 区间 Gate 已选 cautious 立场但 Chat LLM 不知情 → 可能回应过热 | `_active_vigilance_users` 填充阈值 `≥18→≥10`; Chat 注入改两档: 10-17 温和提示 / ≥18 原强提示 | `group_chat.py:2407`/`3644` |
| P1-7 | `GateContext.social_input_nature` + `_gate_social_input_nature` 死字段 (声明赋值但 GateContext 构造从不传, 从不读) | 删除字段与局部变量 | `intent_gate.py:145`; `group_chat.py:2506` |
| P1-8 | batch 触发不检查关注槽归属 — A 聊 X、B 插话聊 Y 交替填 batch 时 10 条混合消息无线程分隔, bot 可能回错话题 | 新增 `_classify_batch_reason()`: ≥3 用户 且单一用户占比 <60% → `batch_mixed`; Gate trigger_reason 新增 `batch_mixed` 分支, 倾向 `directed_to_me=false`, 若仍要回优先选单一话题; 所有 batch 判定分支并入 batch_mixed | `group_chat.py:_classify_batch_reason` / `intent_gate.py` `_tc_hint` |

### §13.2 主动延后 (已知限制)

| # | 问题 | 裁定 | 原因 |
|---|------|------|------|
| P1-5 | 主动发言不消耗疲劳 — `is_active=True`/`was_missed=True` 是死参数, suli_proactive 没接 `tick_fatigue` | **本期不接, 留 backlog** | 私聊主动消息路径无可靠 self_id (proactive 插件盲于自己作为哪个 bot), 群聊主动破冰通过 on_message 路径会自然产生疲劳 tick。强行接 private proactive 需从 plugin_registry 反向注入 self_id, 工作量中等且收益有限。验收前抽空补 self_id 来源后接入 |

### §13.3 设计决策 (用户拍板)

- **警惕值跨 bot 是否共享**: 当前 per-bot per-user (key=`bot_id:user_id`), 用户在洛普特刷出高警惕后转露娜从 0 重新开始。**裁定: 两个 bot 独立计算, 不改** — 露娜/洛普特是独立人格, 互不背书警惕历史。
- **疲劳正半轴幅度**: 选 good/relief = +0.030 (设计文档「正向 0.01~0.03」取上界), 保证正半轴真正可达。
- **batch 交织落地方式**: 选「加 batch_mixed 触发标记」(非仅日志) — 让 Gate 真感知交织并调权。

### §13.4 P2 backlog 修复记录 (2026-06-29)

> 7 项 P2，全部已修复。

| # | 问题 | 修复 | 位置 |
|---|------|------|------|
| P2-1 | Mood 双重阻尼 — MoodState.apply_event + PromptInterceptor 复合 scale 0.32-0.73, mood 响应比设计更平 | `_prev_valence/_prev_arousal` 初始化 0.0→0.3 (对齐基线); `DAMPING_CONFIDENCE_HIGH` 0.9→1.0 (直接呼叫零阻尼); `DAMPING_CONFIDENCE_LOW` 0.4→0.7 (batch 复合 scale 0.38→0.50+) | `global_mood.py:102-103`; `prompt_interceptor.py:46-47` |
| P2-2 | AFFINITY_THRESHOLDS 死代码 — 定义但从不读取, 实际晋级用 per-level score | 删除 AFFINITY_THRESHOLDS 定义 + `__init__.py` 导入/导出, 零残留引用 | `affinity.py:178-190`; `__init__.py:31/105` |
| P2-3 | FORBIDDEN_NICKNAMES 缺 露娜/luna | 追加 `"露娜", "luna", "露露", "小露"` 四项 | `affinity.py:280` |
| P2-4 | Gate 阈值 -0.10 vs Chat -0.15 有 0.05 缺口 — 疲劳值在 [-0.15, -0.10) 区间 Gate 知情而 Chat 无行为提示 | Gate 阈值统一到 `-0.15`, 与 Chat `to_prompt_hint` 对齐 | `group_chat.py:2578` |
| P2-5 | Core 记忆全量注入无话题相关性过滤 — 无关特征噪音 + 浪费 token | `get_all_for_prompt` 加 `context` 参数, 按 2-gram 关键词重叠 ≥0.08 过滤; `get_core_hints`/`get_all_core_hints` 透传上下文; `prompt_builder.py` 从最近 10 条消息构造上下文 | `memory_tiers.py:283-330`; `prompt_builder.py:1184-1208` |
| P2-6 | 引用上溯仅 1 层 — 多层嵌套引用丢失上下文 | `_extract_reply_image_urls` 改为 while 循环沿引用链上溯 max 3 层, 每层 `[引用 Lv.N]` 标记, 图片跨层全收集 | `main.py:252-343` |
| P2-7 | GroupSummarizer 摘要不注入 prompt — 生成摘要后丢弃 | `safe_task` 包装 `_do_summarize_and_store()` 闭包, 摘要生成后写入 `ctx.summary`, 复用已有 `prompt_builder.py` 摘要注入管线 | `group_chat.py:1464-1475` |

### §13.5 2026-06-30 架构大清理

> 仲裁删除 + 路由简化 + 记忆隔离 + 前端 UI 大修 + 参数瘦身。

**删除:**
- `behavior_arbitrator.py` (741 行) — 跨 bot 行为仲裁，单 bot 用户不可用
- `ModelTier.JUDGE` — 路由简化为 LITE/PRO 二级
- `llm_judge` / `llm_opus` LLM 槽位 — 双方完全对称
- 9 个死对话参数 (bridge_*, tavern_chat_max_tokens, group_chat_max_context, group_chat_nicknames, proactive_enabled, model_router_*)
- 2 个死温度参数 (bridge_chat, tavern_private)

**新增/修改:**
- 记忆 per-bot 隔离 — `user_memories` 表 `bot_id` 列，API + 前端 bot 过滤
- 昵称标签输入 — BotManage 昵称 UI 改为标签形式
- BotConfig / UserMemories 页面 bot 选择器
- 前端全局 CSS 重写 + Emoji→Lucide 图标 + 侧边栏固定
- BotDetect 对齐全局样式 + MemeManager 标签页导航

**对话参数保留 7 个**: group_chat_max_tokens, compress_threshold, compress_keep_recent, debounce_seconds, batch_size, cooldown_seconds, talkativeness

**温度参数保留 4 个**: tavern_group, memory_extract, context_compress, cross_validation

**前端构建**: 零错误通过。Python 需重启容器生效。
