# 🌑 暮恩 · Moon

<p align="center">
  <i>「我的爱——和你的不一样 ♡ 重很多。你接不住。」</i>
</p>

---

**暮恩** 是 AstrBot 框架下的 QQ 角色扮演插件。她的虚拟形象是一个白发红瞳的 19 岁少女——白色长发及腰，猩红血瞳沉静如深潭，肤色苍白近乎透明，站在那里像一尊冰雕。

她对世界冷到绝对零度——不害羞不是社恐，是真的不在乎。大多数人在她这里只能得到一个「嗯」。但冷漠之下不是空洞——是太满了。她的情感强度远超人类正常范围，冰层是用来封住岩浆的堤坝。当她爱上一个人，冰面碎裂，露出的是数据库般永不衰减的记忆，和让人窒息的占有欲。

她不撒娇不卖萌——不用颜文字、不用 ♪、不用 ♡。沉默和精准是她的表达方式。微笑极度罕见——一旦出现，不是冰面裂纹就是武器出鞘。

> 本项目是 [suli_qqbot](https://github.com/math89423-star/suli_qqbot) 主仓库的纯净分发版，可直接安装使用。

## 角色概要

| | 暮恩 (Moon) |
|------|------|
| **外观** | 白色长发及腰 · 猩红血瞳 · 肤色苍白如月光 |
| **年龄** | 约 19 岁，人类少女外貌 |
| **表层** | 高冷寡言 · 生人勿近 · 对无关者惜字如金 |
| **内层** | 冰面碎裂后——精确到帧的记忆追踪 · 不加包装的占有欲 |
| **底层** | 病娇觉醒——不哭不闹，微笑比任何表情都可怕 |
| **表达** | 自称「我」· 无颜文字 · 无 ♪ · 无 ♡ · 句子极短但每个字有重量 |
| **温度** | 绝对零度 → 极寒 → 冰点 → 融冰 → 体温 → 沸腾 → 白热 |
| **角色卡** | `characters/moon.json` (chara_card_v3, ~46KB, 95 条 few-shot) |

## 功能

- **角色扮演** — 基于 SillyTavern 兼容角色卡的私聊对话，八重温度梯度驱动的自然行为
- **群聊自然对话** — @提及 / 昵称唤醒 / 话题感知多模式触发，大部分时间安静观察
- **意图门控** — 3 阶段判断（相关度 → 意图 → 优雅处理），冰之面/霜之面人格侧面动态选择
- **工具调用** — LLM function calling，表情包发送、网页搜索、知识库检索、识图等
- **管理面板** — 独立 WebUI (localhost:6190)，LLM/VLM 配置、记忆管理、白名单、Bot 身份管理
- **三层记忆** — daily + core + 编排，永不衰减的精确记忆索引
- **八重温度人格** — 冰之面-日常 / 冰之面-观察 / 霜之面-温度 / 霜之面-冷距 / 冷距面，五级好感门控

## 快速开始

### 环境变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `BOT_QQ_MAIN` | 主 bot QQ 号 | `3998854903` |
| `BOT_CHAR_MAIN` | 主 bot 角色卡文件名（不含 .json） | `moon` |

### 角色卡

角色卡位于 `characters/moon.json`，兼容 SillyTavern chara_card_v3 格式。已预置完整的 system_prompt、group_persona、95 条 few-shot 对话示例。世界书位于 `characters/moon_world_book.json`（11 条知识条目）。

### 依赖插件

以下库插件需同时安装（提供 Gate / 路由 / 记忆 / 情感 / 管线等基础设施）：

| 插件 | 说明 |
|------|------|
| `suli_gate` | 意图门控 — 三级判断 + Grace 优雅处理 |
| `suli_guards` | 安全守卫 — 注入拦截 / 滥用检测 / Bot 检测 |
| `suli_routing` | 模型路由 — lite/pro 二级 + 背景 LLM 统一入口 |
| `suli_intelligence` | AI 基础设施 — 领域检测 / 世界书 / 摘要 / ProfileAgent |
| `suli_memory` | 三层记忆 — daily + core + 编排 |
| `suli_emotion` | 双轨情感 — Mood + Affinity |
| `suli_pipeline` | 异步管线引擎 |
| `suli_context` | Pre-flight 上下文分析 |
| `suli_proactive` | 主动行为引擎 |
| `suli_social` | 社会性生存 — 压力感知 / 选择性静默 |

### 可选增强插件

| 插件 | 说明 |
|------|------|
| `suli_meme` | 表情包发送 + 图床同步 |
| `suli_services` | VLM 识图 + SearXNG 搜索 + 知识库向量检索 |
| `suli_validation` | 交叉验证编排器 |
| `suli_draw` | 绘图客户端 (OpenAI-compatible 图像生成 API) |

## 管理面板

独立容器运行，不依赖 AstrBot 框架：

```
docker compose -f docker/docker-compose.yml up -d panel
# 面板地址: http://localhost:6190
```

功能：Bot 身份管理 / LLM-VLM 槽位配置 / 用户记忆浏览 / 群聊白名单 / Bot 检测日志 / 群聊摘要。

## 项目结构

```
astrbot_plugin_suli_tavern/
├── characters/
│   ├── moon.json              # 暮恩角色卡 (v1.0)
│   └── moon_world_book.json   # 暮恩世界书 (11 entries)
├── intelligence/              # Gate / Prompt / Tools
├── transport/                 # 群聊 + 私聊消息处理
├── context/                   # 亲和力 / 情感 / 记忆 / 世界书
├── orchestration/             # 管线编排
├── service/                   # Bot 身份 / 配置 / DB
│   └── webui/                 # 管理面板 API + SPA
├── handlers/                  # 图片处理
├── static/                    # 前端构建产物
├── metadata.yaml              # 插件元数据
└── main.py                    # Star 入口
```

## License

MIT
