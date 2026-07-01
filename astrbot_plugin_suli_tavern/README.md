# 粟藜 · 角色扮演与群聊主控

AstrBot 主插件，提供完整的 QQ 机器人角色扮演和群聊对话能力。

## 功能

- **角色扮演** — 基于 SillyTavern 兼容角色卡的私聊对话，支持多轮上下文
- **群聊自然对话** — @提及 / 昵称唤醒 / 话题感知 多模式触发
- **意图门控** — 3 阶段判断（相关度 → 意图 → 优雅处理），决定是否回复
- **工具调用** — LLM function calling，支持表情包、搜索、生图、识图、知识库检索等
- **管理面板** — 独立 WebUI (localhost:6190)，配置 LLM/VLM、管理记忆、白名单等

## 配置

通过管理面板 `http://localhost:6190` 或环境变量：

| 变量 | 说明 |
|------|------|
| `BOT_QQ_MAIN` | 主 bot QQ 号 |
| `BOT_QQ_ALT` | 副 bot QQ 号（可选） |
| `BOT_CHAR_MAIN` | 主 bot 角色卡文件名（不含 .json） |
| `BOT_CHAR_ALT` | 副 bot 角色卡文件名 |

## 角色卡

在 `characters/` 目录放置 JSON 文件，兼容 SillyTavern 角色卡格式。参考 `characters/example_character.json`。

## 依赖插件

依赖以下库插件（需同时安装）：suli_gate, suli_guards, suli_routing, suli_intelligence, suli_memory, suli_emotion, suli_pipeline, suli_context, suli_proactive, suli_social
