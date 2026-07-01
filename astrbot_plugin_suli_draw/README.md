# 粟藜 · AI 生图与编辑

纯库插件。OpenAI 兼容图像生成 API 客户端。

## 功能

- **文本生图** — 自然语言描述生成图片
- **图片编辑** — 以图生图 / 局部修改
- **多模型支持** — 通过管理面板配置不同 provider

## 用法

```python
from astrbot_plugin_suli_draw import ImageGenClient

client = ImageGenClient(api_key="sk-xxx", model="gpt-image-2")
results = await client.generate("a cat", size="1024x1536")
```

## 依赖

suli_routing（使用 VLM/生图槽位配置）

