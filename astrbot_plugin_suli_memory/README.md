# 暮恩 · 五层记忆

纯库插件。用户长期记忆 + Bot 自传体记忆的分层存储，per-bot 隔离。

## 五层结构

| 层 | 存储 | 主语 | 时效 | 触发 |
|---|---|---|---|---|
| AttentionSlot | 内存 (conversation_session) | bot | 秒~10分钟 | Gate 热度/槽位 |
| Episodic (情节归档) | `bot_episodes/{bot_id}/{group_id}.json` | bot | 分钟~周 | 槽过期自动归档，零 LLM |
| Daily Memory | `user_memories/{bot_id}/{user_id}.json` | 用户 | 天~周 | ProfileAgent 蒸馏 |
| Core Memory | `user_core/{bot_id}/{user_id}.json` | 用户 | 永久 | MemoryTier 升级 (≤30条/人) |
| Bot自传体经历 | `bot_experiences/{bot_id}/` + SQLite | bot 自己 | 永久 | 定时提取/蒸馏 (recent→core) |

- **编排**: `MemoryTierManager` 控制 daily→core 升级/过期，前端记忆管理走 `bot_db` 统一读写
- **隔离**: 全部 `{bot_id}/{user_id}.json` per-bot 路径，`get_memory_store(bot_id)` 找不到回 `None` (不跨 bot 回退)
- **注入**: `prompt_builder` 按 `[注意力槽 → 情节(≤2) → 日常 → 核心 → Bot经历]` 顺序注入 `message[1+]`

## API

- `remember_memory` / `get_memory` — 日常记忆读写
- `MemoryTierManager` — 记忆升级/降级/过期
- `EpisodicStore` — `archive(slot)` / `get_recent(group_id)` 情节归档
- `BotExperienceStore` — `get_experience_hints(max_tokens)` Bot 自传体

## 依赖

suli_emotion（好感度影响记忆权重/门控）

