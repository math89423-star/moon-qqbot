# QQ Bot 开发与运维知识库

> 覆盖 QQ bot 框架生态、OneBot 协议、AstrBot 框架、NapCat 部署、消息格式、风控与安全。
> 帮助 bot 理解自身运行机制，并在群友询问 bot 开发相关问题时给出专业回答。
> 最后更新: 2026-06

---

## QQ Bot 框架生态

### 协议层次

```
QQ 客户端 (手Q/PC) ←→ QQ 服务端 ←→ Bot 协议实现 ←→ Bot 框架 ←→ 插件
                                              │
                          NapCat / LLOneBot ──┤  (PC 协议, NT 内核)
                          Lagrange          ──┤  (NTQQ 协议, Go)
                          Mirai             ──┤  (Android 协议, Kotlin)
                          Icalingua++       ──┤  (NTQQ 协议, Node)
```

### 主流协议实现对比

| 项目 | 语言 | 协议 | 状态 | 说明 |
|------|------|------|------|------|
| **NapCat** | TypeScript | PC NTQQ (Windows) | ✅ 活跃 | 本项目使用, 最稳定 |
| **LLOneBot** | TypeScript | PC NTQQ | ✅ 活跃 | NapCat 的前身/同源 |
| **Lagrange** | Go | NTQQ (签名服务) | 🟡 维护 | 纯 Go, 需签名服务 |
| **Mirai** | Kotlin | Android QQ | 🟡 低活跃 | 老牌, Android 协议 |
| **go-cqhttp** | Go | Android QQ | ❌ 已停更 | 2023 年停止维护 |

### Bot 框架层

| 框架 | 语言 | 协议标准 | 说明 |
|------|------|---------|------|
| **AstrBot** | Python | OneBot v11/v12 | 本项目使用, 插件化 + LLM 原生 |
| **NoneBot2** | Python | OneBot v11/v12 | 成熟生态, 异步优先 |
| **Koishi** | TypeScript | OneBot v11/v12 | 跨平台, 插件市场丰富 |
| **ZeroBot** | Go | OneBot v11 | 轻量高性能 |

---

## OneBot 协议

### 概述

OneBot 是 QQ bot 与框架之间的通信标准。Bot 协议实现 (NapCat) 作为 OneBot Server，Bot 框架 (AstrBot) 作为 OneBot Client。

### 通信方式

| 方式 | 说明 | 使用场景 |
|------|------|---------|
| **正向 WebSocket** | 框架主动连接 bot 协议实现 | 框架和协议在同一网络 |
| **反向 WebSocket** | bot 协议实现主动连接框架 | Docker 环境推荐, NapCat→AstrBot |
| **HTTP POST** | 事件通过 HTTP 推送 | 简单部署, 不支持流式 |
| **HTTP GET** | 框架主动轮询 | 不推荐 |

### OneBot v11 核心 API

```python
# 发送消息
send_msg(group_id=711600211, message="你好")

# 发送私聊
send_private_msg(user_id=123456, message="你好")

# 获取消息
get_msg(message_id=12345)

# 撤回消息
delete_msg(message_id=12345)

# 获取群信息
get_group_info(group_id=711600211)

# 获取群成员
get_group_member_list(group_id=711600211)

# 获取登录信息
get_login_info()
```

### CQ 码 (消息中嵌入的特殊格式)

| CQ 码 | 含义 | 示例 |
|------|------|------|
| `[CQ:at,qq=123]` | @某人 | `[CQ:at,qq=3581173900]` (洛普特) |
| `[CQ:image,file=xxx]` | 图片 | 文件路径/base64/URL |
| `[CQ:reply,id=123]` | 引用回复 | 回复某条消息 |
| `[CQ:face,id=123]` | QQ 表情 | 系统自带表情 |
| `[CQ:record,file=xxx]` | 语音 | 语音消息 |
| `[CQ:forward,id=xxx]` | 合并转发 | 多条消息合并转发 |
| `[CQ:json,data=xxx]` | JSON 卡片 | 结构化消息卡片 |

---

## AstrBot 框架核心

### 架构概览

```
NapCat (QQ协议) → WebSocket → AstrBot Core → Star 插件系统
                                  │
                              Provider 管理
                              (LLM 后端配置)
                                  │
                              Pipeline 管线
                              (消息→意图→生成→回复)
```

### Star 插件 (本项目使用的插件类型)

```python
from astrbot.api.star import Star, Context

class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    # 注册命令
    @filter.command("draw")
    async def draw(self, message: AstrMessageEvent):
        ...

    # 注册 LLM 工具
    @filter.llm_tool("search_knowledge")
    async def search_knowledge(self, query: str) -> str:
        ...

    # 监听群聊消息
    @filter.on_llm_message()
    async def on_group_message(self, message: AstrMessageEvent):
        ...
```

### 关键概念

| 概念 | 说明 |
|------|------|
| **Provider** | LLM 后端配置 (API endpoint + key + model) |
| **Persona** | 人格/系统提示词，可绑定到特定 Provider |
| **Pipeline** | 消息处理管线 (可自定义阶段和顺序) |
| **LLM Tool** | 注册给 LLM 的可用工具，`@filter.llm_tool` 装饰 |
| **Context** | 插件上下文，提供 send_message / get_providers 等能力 |

### Per-Bot 配置

AstrBot 支持同一实例管理多个 QQ 号，每个 QQ 号可独立配置:
- **Provider 绑定**: 每个 bot 使用不同的 LLM 后端
- **Persona 绑定**: 每个 bot 有独立的人格/system prompt
- **插件开关**: 每个 bot 可选择启用哪些插件
- **工具注册**: 工具可限定哪些 bot 可用

本项目: 洛普特 + 露娜 双 bot 一个 AstrBot 实例管理。

---

## NapCat 部署实战

### Docker Compose 部署 (本项目方案)

```yaml
# astrbot.yml
services:
  napcat:
    image: napcat/napcat:latest
    environment:
      - NAPCAT_UID=${NAPCAT_UID}
      - NAPCAT_GID=${NAPCAT_GID}
    volumes:
      - ./napcat-data:/app/.napcat
      - ./napcat-config:/app/napcat/config
    ports:
      - "6199:6199"  # WebSocket

  astrbot:
    image: astrbot/astrbot:latest
    volumes:
      - ./plugins:/AstrBot/data/plugins
    ports:
      - "6185:6185"  # WebUI
```

### 网络模式与踩坑

1. **容器互联**: NapCat 和 AstrBot 必须在同一 Docker 网络，用服务名通信
2. **反向 WS**: NapCat 配置 `ws://astrbot:6199/ws` (容器内用服务名)
3. **宿主 127.0.0.1 不可达**: 容器内 `127.0.0.1` 是容器自己，不是宿主机。SillyTavern 在宿主机 127.0.0.1:8000 时容器无法访问，需用 `host.docker.internal` 或改用 Docker 网络。
4. **UID/GID 映射**: NapCat 需要与宿主文件权限匹配，否则无法写入配置

### NapCat 登录流程

1. 首次启动 NapCat → 输出二维码 URL
2. 用手机 QQ 扫码登录 (需要同一网络下的手机上 QQ)
3. 登录成功 → 生成 token，后续启动自动登录
4. 如果 token 过期 → 需重新扫码或输入验证码

### 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 扫码登录反复掉 | 账号风控 | 等几个小时再试，或在同 IP 登录手 Q 养号 |
| WebSocket 连接断开 | 网络波动 / NapCat 重启 | AstrBot 有自动重连机制 |
| 消息发送失败 120 | 账号被风控 | 降低发送频率，检查账号状态 |
| 图片发不出去 | 图片过大或格式不支持 | QQ 限制 ~10MB, 支持 jpg/png/gif |
| 合并转发生成错误 | 消息格式问题 | Check 转发消息的构造逻辑 |

---

## 消息格式深入

### AstrBot 消息事件结构

```python
class AstrMessageEvent:
    message: str              # 纯文本 (所有 CQ 码已移除)
    raw_message: str          # 含 CQ 码的原始消息
    sender: Sender            # 发送者 (user_id, nickname, card)
    group_id: int | None      # 群号 (私聊为 None)
    message_id: int           # 消息 ID
    is_private: bool          # 是否私聊
    is_group: bool            # 是否群聊
    images: list[str]         # 图片 URL 列表 (已提取)
    at_list: list[int]        # 被 @ 的 QQ 号列表
```

### 消息链路 (洛普特)

```
QQ 消息 → NapCat 接收 → WebSocket → AstrBot
    → suli_tavern/main.py on_llm_message()
    → transport/group_chat.py process_group_message()
    → intelligence/intent_gate.py (意图门控)
    → intelligence/judge.py (裁判决策)
    → orchestration/pipeline.py (装配 prompt)
    → ST 渲染 / 直连 LLM
    → 回复 → NapCat → QQ
```

### 消息长度限制

- QQ 单条消息: ~4500 字符 (含 CQ 码)
- 长回复策略: 分段发送 (每段 ~4000 字符 + "续"提示)
- 图片消息: 单独发送，配合简短文字说明

---

## 频率限制与风控

### QQ 频率限制 (经验值)

| 操作 | 限制 |
|------|------|
| 群消息发送 | ~30-60 条/分钟 (超过可能被限速) |
| 私聊消息发送 | ~20-30 条/分钟 |
| 图片上传 | ~10 张/分钟 |
| 合并转发 | ~5 次/分钟 |
| 加好友请求 | ~5 次/天 |
| 群邀请 | 极低频率，容易风控 |

### 风控信号识别

- **消息发送返回 120**: 被限制发送
- **消息延迟送达**: 可能被限速中
- **账号需重新登录**: 被风控踢下线
- **群聊中不显示**: 被群屏蔽或账号被限制
- **私聊消息对方收不到**: 被对方屏蔽或风控

### 风控规避经验

1. **养号期**: 新号上线前 3-7 天只接收不发送，模拟正常用户行为
2. **发送间隔**: 群消息间隔 ≥2 秒，连续发送不超过 5 条
3. **内容多样性**: 避免重复发送相似内容
4. **避免敏感词**: 政治/色情/暴恐关键词直接导致风控升级
5. **登录 IP 稳定**: 频繁切换 IP 触发异地登录保护
6. **主号比小号稳**: 长期使用的号码比新注册的耐风控

---

## 安全实践

### Token / Key 管理

```
铁律:
1. API Key 绝不硬编码在代码中
2. 存储位置: 环境变量 / AstrBot WebUI 配置 / L-Port DB (只读)
3. 日志不得输出完整 Key — 掩码 (前4后4)
4. QQ 消息中绝不展示完整 Key
5. 配置备份不包含 Key
```

### 输入安全

- **Prompt Injection 防护**: InjectionGuard 59 条规则 (见 `group_chat.py`)
- **不要原样反射用户输入**: 用户发敏感内容 → bot 不跟风不重复
- **CQ 码过滤**: 防止用户通过伪造 CQ 码进行操作
- **URL 安全**: 不自动访问用户发送的链接，防止 SSRF

### 权限控制

- **管理员**: 群主/管理员 → 可执行敏感操作 (如 `/reload`, `/shutdown`)
- **普通群友**: 只能使用常规命令和对话
- **黑名单**: 恶意用户可被静默忽略
- **白名单**: 测试阶段可仅对白名单用户开放

---

## 本项目部署拓扑

```
宿主机 (Windows/WSL2)
│
├── Docker: astrbot-compare/
│   ├── NapCat (主号 3581173900)  → 电动糕手群 (711600211)
│   ├── NapCat (露娜 3969478803)  → 同一群聊
│   └── AstrBot Core (WebUI :6185, WS :6199)
│       ├── suli_tavern     (洛普特主插件)
│       ├── suli_bridge     (L-Port LLM 桥接)
│       ├── astrbot_plugin_suli_draw       (生图插件)
│       ├── private_companion (露娜陪伴)
│       └── 社区插件         (Heartflow/meme/self_evolution)
│
└── SillyTavern (宿主机 127.0.0.1:8000)
    └── 仅离线编辑角色卡, 在线时由 AstrBot 直连 LLM
```

### 关键路径

| 路径 | 说明 |
|------|------|
| `plugins/` | 所有插件源码 (git 跟踪, Docker bind-mount) |
| `config/` | 插件配置 (git 跟踪) |
| `~/astrbot-compare/runtime/` | 运行时数据 (Docker volume, 不入 git) |
| `docker/` | Docker 部署编排 (docker-compose.yml + Dockerfile) |
