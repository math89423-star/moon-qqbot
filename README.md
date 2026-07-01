# astrbot-moon

基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) + [NapCat](https://napcat.napneko.icu/) 的 QQ 机器人插件套件，一键部署，开箱即用。

## 功能特性

- **AI 角色扮演** — 支持私聊和群聊，自动角色卡加载
- **自然群聊对话** — @机器人或喊名字即可触发智能回复
- **三层记忆系统** — 短期对话记忆 → 核心记忆蒸馏 → 长期经验积累
- **双轨情感系统** — 短期情绪波动 + 长期好感度追踪
- **意图门控** — 判断该不该说话、说什么、什么时候说
- **安全守卫** — 注入拦截、滥用检测、Bot 检测
- **工具调用** — 联网搜索、AI 绘图、AI 识图、知识库检索
- **表情包系统** — AI 表情包发送 + 图床同步 + 类别管理
- **Web 管理面板** — LLM 配置、记忆管理、群聊设置一站式管理
- **模型路由** — 自适应 Lite/Pro 双级模型切换

## 快速开始

### 前置条件

- Python 3.10+
- Git
- 一个 QQ 小号（用于机器人登录）

### Windows 用户

```powershell
git clone https://github.com/math89423-star/moon-qqbot.git
cd moon-qqbot
scripts\deploy.bat
```

启动：

```powershell
scripts\start.bat
```

### Linux / macOS 用户

```bash
git clone https://github.com/math89423-star/moon-qqbot.git
cd moon-qqbot
bash scripts/deploy.sh
```

启动：

```bash
bash scripts/start.sh
```

脚本会自动完成：
1. 创建 Python 虚拟环境
2. 安装项目依赖
3. 安装 AstrBot 框架
4. 部署全部 17 个插件
5. 复制角色卡
6. 引导安装 NapCat

### 启动

```bash
bash scripts/start.sh       # Linux / macOS
# 或
scripts\start.bat           # Windows
```

启动后访问 `http://localhost:5190` 进入管理面板。

### 配置 LLM

在管理面板的「机器人配置」中设置 LLM 接口，支持所有 OpenAI 兼容 API：

| 槽位 | 用途 |
|------|------|
| LLM Lite | 日常聊天，推荐使用快速便宜的模型 |
| LLM Pro | 复杂推理，推荐使用强力模型 |
| LLM Gate | 意图判断，轻量模型即可 |
| VLM Primary | 图片识别，需要支持视觉的模型 |

### 配置示例

使用 DeepSeek:

```
Provider: custom
API Base: https://api.deepseek.com/v1
API Key: sk-your-key
Model: deepseek-chat
```

使用 OpenAI:

```
Provider: custom
API Base: https://api.openai.com/v1
API Key: sk-your-key
Model: gpt-4o
```

## 插件列表

### 核心插件

| 插件 | 说明 |
|------|------|
| `astrbot_plugin_suli_tavern` | 主插件 — 角色扮演 + 群聊 + 管理面板 |
| `astrbot_plugin_suli_gate` | 三层意图门控 |
| `astrbot_plugin_suli_guards` | 安全守卫 — 注入 / 滥用 / Bot 检测 |
| `astrbot_plugin_suli_routing` | 自适应模型路由 |
| `astrbot_plugin_suli_intelligence` | AI 基础设施 — 领域检测 / 世界书 / 群聊摘要 |
| `astrbot_plugin_suli_memory` | 三层记忆蒸馏 |
| `astrbot_plugin_suli_emotion` | 双轨情感系统 |
| `astrbot_plugin_suli_pipeline` | 异步管线引擎 |
| `astrbot_plugin_suli_context` | 上下文预分析 |
| `astrbot_plugin_suli_proactive` | 主动行为引擎 |

### 增强插件

| 插件 | 说明 |
|------|------|
| `astrbot_plugin_suli_meme` | AI 表情包发送 + 图床同步 |
| `astrbot_plugin_suli_services` | VLM 识图 + 联网搜索 + 知识库 |
| `astrbot_plugin_suli_validation` | 回答交叉验证 |
| `astrbot_plugin_suli_social` | 社会压力与选择性静默 |
| `astrbot_plugin_suli_draw` | AI 绘图客户端 |
| `astrbot_plugin_remove_blank_lines` | 回复空行清理 |
| `astrbot_plugin_suli_bridge` | LLM 配置桥接 |

## 管理面板

访问 `http://localhost:5190`：

- **仪表盘** — 运行状态总览
- **机器人配置** — LLM/VLM 槽位、温度、工具开关
- **用户记忆** — 搜索、查看、删除用户记忆
- **知识库** — 知识章节管理
- **群聊设置** — 白名单、群级对话参数
- **Bot 检测** — 可疑 Bot 列表
- **群聊总结** — 群聊话题摘要
- **表情包管理** — 上传、删除、分类

## 角色卡

角色卡使用 SillyTavern v3 兼容格式。默认角色卡为 `characters/moon.json`（暮恩）。

### 自定义角色卡

1. 在 `characters/` 目录创建 `your_char.json`
2. 设置环境变量 `BOT_CHAR_MAIN=your_char`
3. 重启机器人

### 角色卡格式

```json
{
  "spec": "chara_card_v3",
  "spec_version": "3.0",
  "data": {
    "name": "角色名",
    "description": "角色外观与背景",
    "personality": "性格描述",
    "scenario": "对话场景",
    "first_mes": "开场白",
    "mes_example": "对话示例",
    "system_prompt": "系统提示词"
  }
}
```

高级用法：创建 `characters/your_char_persona_v2.txt`，系统提示词将从该文件读取（便于版本管理）。

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `BOT_QQ_MAIN` | 是 | 机器人 QQ 号 |

## 架构

```
NapCat (QQ 协议)
    │  OneBot WebSocket
    ▼
AstrBot (机器人框架)
    │  加载插件
    ▼
astrbot-moon 插件套件
    ├── suli_tavern     ← 主控（事件、命令、管线编排）
    ├── suli_gate       ← 意图判断
    ├── suli_guards     ← 安全防护
    ├── suli_routing    ← 模型路由
    ├── suli_intelligence ← 领域/世界书/摘要
    ├── suli_memory     ← 记忆存储
    ├── suli_emotion    ← 情感追踪
    ├── suli_pipeline   ← 异步管线
    └── ... 其余增强插件
            │
            ▼
    OpenAI 兼容 API (LLM / VLM)
```

## 常见问题

### 插件未加载

确认插件目录已正确部署到 `AstrBot/data/plugins/`。运行 `deploy.sh` 可自动处理。

### WebSocket 连接错误

检查 NapCat 是否已启动并登录。确认 AstrBot 的 `api_config.json` 中 aiocqhttp 平台配置的 WebSocket 地址指向 NapCat。

### LLM 调用失败

在管理面板检查 LLM 配置：API Key 是否正确、API Base 是否以 `/v1` 结尾、模型名称是否有误。

### 角色卡未生效

确认 `characters/` 目录下有 JSON 角色卡文件，且文件名与 `BOT_CHAR_MAIN` 环境变量一致。未设置时自动扫描目录。

## 依赖

- Python 3.10+
- AstrBot v4.25.0+
- openai >= 2.0
- aiohttp
- aiosqlite

## License

MIT
