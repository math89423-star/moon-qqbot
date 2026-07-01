# 粟藜 · 三层记忆蒸馏

纯库插件。用户长期记忆的分层存储与检索。

## 三层结构

1. **Daily Memory** — 近期对话细节，高保真低延迟
2. **Core Memory** — 蒸馏后的关键特征，长期保留
3. **Memory Tier** — 编排层，控制记忆的存取优先级和生命周期

## API

- `remember_memory` — 存入记忆
- `get_memory` — 检索记忆
- `MemoryTierManager` — 记忆升级/降级/过期管理

## 依赖

suli_emotion（好感度影响记忆权重）

