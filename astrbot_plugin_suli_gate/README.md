# 粟藜 · 三层意图门控

纯库插件。在 LLM 调用前判断消息是否需要回复、回复什么风格。

## 三层结构

1. **相关度** — 这条消息跟我有关吗？
2. **意图分类** — 闲聊 / 提问 / 指令 / 表情反应 / 玩梗
3. **处理策略** — 路由模型 tier、推荐工具、回复风格、情绪标签

## 产物

`GateResult` 包含：intent_type, model_tier, reasoning_effort, suggested_tools, reply_style, sticker_mood, urgency 等。

## 依赖

suli_intelligence（领域检测）, suli_context（上下文预分析）

