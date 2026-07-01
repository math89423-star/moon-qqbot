# 粟藜 · 异步消息管线引擎

纯库插件。可组合的异步步骤编排器，用于消息处理流水线。

## 用法

```python
from astrbot_plugin_suli_pipeline import Pipeline

pipeline = Pipeline()
pipeline.add_step(step1).add_step(step2)
result = await pipeline.run(context)
```

## 特性

- 步骤可组合、可复用
- 异步非阻塞
- 支持条件跳过和提前终止

