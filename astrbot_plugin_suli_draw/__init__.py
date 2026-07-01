"""AI 绘图客户端 — OpenAI-compatible Images API 封装。

用法:
  from astrbot_plugin_suli_draw import ImageGenClient, AuthError, RateLimitError, ...

  client = ImageGenClient(api_key="sk-xxx", model="gpt-image-2")
  results = await client.generate("a cat", size="1024x1536")
  for img in results:
      save(img.bytes_data, "output.png")
"""
