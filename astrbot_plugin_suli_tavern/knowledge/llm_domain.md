# 大模型领域通识

> 覆盖主流模型家族、核心架构概念、API 使用、Agent 模式、RAG、微调对齐、部署量化、评估基准与行业动向。帮助 bot 在群友讨论 LLM/AI 话题时能准确理解和回应。
> 最后更新: 2026-06

---

## 主流模型家族总览

### 闭源商业模型

| 模型 | 开发商 | 核心特点 | 典型场景 |
|------|--------|---------|---------|
| **GPT-5** | OpenAI | 多模态统一架构, 128K 上下文, 深度推理 | 通用对话/代码/创意写作 |
| **GPT-4o** | OpenAI | 原生多模态 (文本+视觉+音频), 极低延迟 | 实时语音/视觉理解 |
| **Claude 4.x** (Opus/Sonnet/Haiku) | Anthropic | Constitutional AI, 200K 上下文, 强调安全对齐 | 长文档分析/安全敏感场景 |
| **Gemini 2.5** | Google | TPU 原生训练, 1M+ 上下文, 多模态 | 超长上下文/搜索整合 |
| **DeepSeek-V3/R1** | DeepSeek | 开源权重, MoE 架构, 极高性价比 | 代码生成/数学推理 |
| **Qwen 3** | 阿里 | 开源多尺寸 (0.5B-72B), MoE, 中文优化 | 中文场景/企业部署 |
| **Grok-3** | xAI | 实时 X 平台数据接入, 长上下文 | 实时信息/社交媒体分析 |

### 开源模型族系

| 族系 | 代表模型 | 架构 | 亮点 |
|------|---------|------|------|
| **LLaMA** | LLaMA 4 (Meta) | Dense/MoE | 开源标杆, 生态最丰富 |
| **Mistral** | Mistral Large 2 / 3 | Dense/MoE | 欧洲最强, 多语言优秀 |
| **DeepSeek** | V3 / R1 | MoE (256 专家) | MLA 注意力, 训练成本极低 |
| **Qwen** | Qwen 3 (0.5B-72B) | Dense/MoE 混合 | 中文最强开源, 工具调用优秀 |
| **Yi** | Yi-Lightning / Yi-Large | Dense | 零一万物, 中英双语 |
| **Gemma** | Gemma 3 (Google) | Dense | 轻量级开源, 研究友好 |
| **Command R** | Command R+ (Cohere) | Dense | RAG 优化, 工具使用原生支持 |

### 开源模型能力梯队 (2026 中)

```
Tier 0 (接近 GPT-5/Claude 4.5):
  DeepSeek-R1, Qwen 3-72B, LLaMA 4-405B

Tier 1 (GPT-4 水平):
  Mistral Large 3, DeepSeek-V3, Qwen 3-32B

Tier 2 (性价比):
  Qwen 3-8B, LLaMA 4-8B, Gemma 3-12B

Tier 3 (端侧/边缘):
  Qwen 3-1.5B/0.5B, Gemma 3-1B, Phi-4-mini
```

---

## 核心架构概念

### Transformer 基础

大语言模型的核心架构，2017 年由 Google 在 "Attention Is All You Need" 中提出。

**关键组件**:
- **Self-Attention**: 每个 token 关注序列中所有其他 token，捕捉长距离依赖
- **Multi-Head Attention (MHA)**: 多组注意力并行，关注不同表示子空间
- **Feed-Forward Network (FFN)**: 位置独立的全连接层，存储知识
- **Layer Normalization**: 稳定训练，现在多用 Pre-LN (Pre-LayerNorm) 或 RMSNorm
- **Positional Encoding**: 注入位置信息，主流用 RoPE (旋转位置编码)

### 关键架构变体

| 架构 | 说明 | 代表模型 |
|------|------|---------|
| **MoE** (Mixture of Experts) | FFN 层拆成多个"专家"，每 token 只激活 ~10% 参数 | DeepSeek-V3, Qwen 3-MoE, Mixtral |
| **MLA** (Multi-head Latent Attention) | DeepSeek 提出，将 KV cache 压缩到低维潜空间，推理成本大幅降低 | DeepSeek-V2/V3/R1 |
| **GQA** (Grouped Query Attention) | 多个 Q head 共享一组 KV head，减少 KV cache 内存 | LLaMA 3/4, Mistral |
| **Mamba / SSM** | 状态空间模型，线性复杂度替代 Attention | Mamba, Jamba (混合) |
| **Linear Attention** | O(n) 复杂度注意力，适合超长序列 | RWKV, RetNet |

### Token 与分词

- **Token**: 模型处理的最小文本单元（不是字/词，是子词 subword）
- **Tokenizer**: BPE (GPT/LLaMA) vs SentencePiece (Gemma) vs WordPiece (BERT)
- **中文 token 效率**: 中文字均 ~2-3 tokens，英文 ~0.75 tokens/词 → 中文"贵" 2-3 倍
- **上下文窗口**: 从 4K (GPT-3) → 128K (GPT-4) → 1M+ (Gemini 2.5)，持续扩展中
- **KV Cache**: 推理时缓存的 Key-Value 状态，长上下文的内存瓶颈

### 推理参数速查

| 参数 | 作用 | 典型值 |
|------|------|--------|
| **temperature** | 控制随机性。0=确定, 1=平衡, >1=创意 | 对话 0.6-0.9, 代码 0-0.3, 创意写作 0.8-1.2 |
| **top_p** (nucleus sampling) | 只从累积概率 ≥ p 的 token 中采样 | 0.9-0.95 |
| **top_k** | 只从概率最高的 k 个 token 中采样 | 40-100 |
| **max_tokens** | 单次生成的最大 token 数 | 取决于上下文窗口剩余空间 |
| **presence_penalty** | 惩罚已出现过的 token，鼓励新话题 | -0.5 ~ 0.5 |
| **frequency_penalty** | 按频率惩罚重复 token | -0.5 ~ 0.5 |
| **stop** | 停止序列，遇到即终止生成 | `\n\n`, `<|end|>` 等 |

---

## API 与推理服务

### OpenAI 兼容协议 (事实标准)

绝大多数 LLM 服务商和本地推理框架都兼容 OpenAI Chat Completions API 格式：

```python
# 标准调用模式
from openai import AsyncOpenAI

client = AsyncOpenAI(
    base_url="https://api.deepseek.com/v1",  # 各厂商端点
    api_key="sk-xxx",
)

response = await client.chat.completions.create(
    model="deepseek-chat",
    messages=[
        {"role": "system", "content": "你是..."},
        {"role": "user", "content": "你好"},
    ],
    temperature=0.7,
    stream=True,  # SSE 流式输出
)
```

### 流式输出 (SSE)

- Server-Sent Events: 逐 token 返回，降低首字延迟
- 实现: `stream=True` → 异步迭代 `response`
- QQ bot 特殊处理: 消息不支持实时编辑，需等完整回复或分段发送
- "思考中"占位: Claude 的 thinking 或 DeepSeek-R1 的 reasoning_content 不在 QQ 逐字显示

### Function Calling / Tool Use

LLM 不直接执行操作，而是输出结构化的"调用意图"：

```json
{
  "name": "search_knowledge",
  "arguments": {"query": "LoRA 训练参数"}
}
```

- 主流模型均支持: GPT-4+, Claude, Gemini, DeepSeek, Qwen
- QQ bot 中的实现: AstrBot `@filter.llm_tool` 装饰器注册工具
- 工具调用循环: LLM → tool_call → 执行 → 结果注入 → LLM 继续 → 最终回复
- **需注意**: 工具调用可能陷入无限循环，需设 max_turns

### 速率限制 (Rate Limit)

| 概念 | 说明 |
|------|------|
| **RPM** (Requests Per Minute) | 每分钟请求数 |
| **TPM** (Tokens Per Minute) | 每分钟 token 消耗量 |
| **RPD** (Requests Per Day) | 每日请求配额 |
| **Tier 分级** | 消费越多，速率限制越宽松 |
| **429 错误** | 触发限流，需指数退避重试 |

---

## Agent 架构模式

### 核心范式

| 模式 | 流程 | 适用场景 |
|------|------|---------|
| **ReAct** | Thought → Action → Observation → 循环 | 需要多步推理+工具使用的任务 |
| **Plan-Execute** | 先制定完整计划 → 逐步执行 → 检查 | 复杂多步任务 |
| **Router** | 根据意图分类 → 路由到专门处理链 | 多意图分发 |
| **Reflexion** | 执行 → 自我评价 → 反思 → 重试 | 需要自我纠错的任务 |
| **Multi-Agent** | 多 Agent 协作：对话/投票/层级 | 需要多视角的复杂任务 |

### Tool Use 最佳实践

1. **工具描述要精确**: 功能/参数/返回值说明清楚，LLM 才能正确调用
2. **参数类型约束**: 用 JSON Schema 定义参数类型和必填项
3. **优雅降级**: 工具调用失败时有 fallback，不要让 LLM 卡在错误循环
4. **max_turns 保护**: 设置最大工具调用轮数 (洛普特当前: 5 轮)
5. **结果截断**: 工具返回结果控制在 context window 合理比例内

### MCP (Model Context Protocol)

Anthropic 提出的 Agent-工具标准化协议 (2024.11):
- **Host**: 运行 Agent 的应用 (如 AstrBot)
- **Client**: 连接到一个 MCP Server
- **Server**: 提供工具/资源/提示的标准化服务
- AstrBot 已支持 MCP 工具接入

---

## RAG 与向量检索

### RAG (Retrieval-Augmented Generation) 流程

```
用户提问 → Query 改写/扩展 → Embedding → 向量检索
                                             ↓
用户 ← LLM 生成 (含引用) ← 组装 Prompt ← Top-K 文档
```

### Embedding 模型

| 模型 | 维度 | 特点 |
|------|------|------|
| **BGE-M3** (BAAI) | 1024 | 多语言/多粒度, 支持稠密+稀疏, 本项目使用 |
| **text-embedding-3** (OpenAI) | 256-3072 | 可变维度, 商业最强 |
| **Jina Embeddings v3** | 1024 | 多语言, 任务特定 LoRA |
| **GTE-Qwen2** (阿里) | 2048 | 中文场景优秀 |

### 分块策略 (Chunking)

- **固定大小**: N 字符/ token 一块，简单但可能切断语义
- **语义分块**: 按段落/标题自然边界，本项目按 `##` 标题分节
- **递归分块**: 大块 → 小块 → 更小块，直到合适尺寸
- **句子窗口**: 检索句子 + 前后各 N 句上下文

### 检索优化

| 技术 | 说明 |
|------|------|
| **Hybrid Search** | 稠密向量 + 稀疏 (BM25) 混合检索 |
| **Re-ranking** | 粗检索 Top-K → 精排模型重排序 → 取 Top-N |
| **Query Rewriting** | LLM 改写用户问题，提高检索命中率 |
| **Multi-Query** | 生成多个查询变体，合并去重结果 |
| **Self-RAG** | LLM 自己判断是否需要检索 + 检索结果是否相关 |

---

## 微调与对齐

### 训练阶段总览

```
Pre-training (自监督) → SFT (指令微调) → RLHF/DPO (对齐)
     ↓                       ↓                    ↓
  基座模型               能听懂指令          符合人类偏好
```

### 微调方法对比

| 方法 | 全称 | 原理 | 资源需求 |
|------|------|------|---------|
| **Full Fine-tune** | 全参数微调 | 更新所有参数 | 最高 (数卡 A100) |
| **LoRA** | Low-Rank Adaptation | 在 Attention 旁路加低秩矩阵 | 低 (单卡 4090) |
| **QLoRA** | Quantized LoRA | 4-bit 量化 + LoRA | 最低 (单卡 24GB) |
| **Adapter** | 适配器层 | 在层间插入小型可训练模块 | 低 |
| **Prefix Tuning** | 前缀微调 | 在输入前加可学习 token | 极低 |

### LoRA 参数速览

| 参数 | 含义 | 典型值 |
|------|------|--------|
| **r** (rank) | 低秩矩阵维度 | 8-64, 越大表示能力越强但文件越大 |
| **alpha** | 缩放因子 | 通常 = r 或 2×r |
| **target_modules** | 应用到哪些层 | q_proj, v_proj (最小), q+v+o+f (全) |
| **dropout** | 防止过拟合 | 0-0.1 |

### RLHF / DPO / GRPO

- **RLHF** (Reinforcement Learning from Human Feedback): 训练奖励模型 → PPO 优化 → 对齐人类偏好
- **DPO** (Direct Preference Optimization): 跳过奖励模型，直接对比偏好对训练，更简单稳定
- **GRPO** (Group Relative Policy Optimization): DeepSeek-R1 使用，组内相对比较，无需外部奖励模型

### 幻觉问题 (Hallucination)

LLM 生成看似合理但事实错误的内容：
- **原因**: 训练数据的统计模式 ≠ 事实知识，模型倾向"编造"而非承认不知道
- **缓解**: RAG 注入事实、明确要求"不知道就说不知道"、引用来源、事后验证
- **检测**: 自我一致性 (多次生成交叉检查)、外部知识库比对

---

## 部署与量化

### 推理框架

| 框架 | 特点 | 适用场景 |
|------|------|---------|
| **vLLM** | PagedAttention, 高吞吐, 广泛支持 | 生产级 API 服务 |
| **llama.cpp** | C++ 重写, CPU/GPU 混合推理, GGUF 格式 | 本地/边缘部署 |
| **Ollama** | 基于 llama.cpp, 一键部署, 类 Docker 体验 | 个人开发/实验 |
| **SGLang** | RadixAttention, 结构化生成 | 高性能 API |
| **TGI** (HuggingFace) | 官方推理方案, 水印/兼容性好 | 企业部署 |
| **TensorRT-LLM** | NVIDIA 官方, 极致 GPU 优化 | NVIDIA 生态 |

### 量化格式

| 格式 | 精度 | 特点 |
|------|------|------|
| **GGUF** | 1.5-8 bit | llama.cpp 生态, 灵活混合精度, Q4_K_M 最常用 |
| **GPTQ** | 2-8 bit | GPU 优化, 需校正数据集 |
| **AWQ** | 4 bit | 激活感知量化, 比 GPTQ 更快 |
| **bitsandbytes** | 4/8 bit | HuggingFace 集成, QLoRA 训练 |
| **FP8** | 8 bit | H100/B200 原生支持, 业界新标准 |
| **FP16/BF16** | 16 bit | 半精度推理, 质量最高 |

### 量化与质量 (经验法则)

```
FP16/BF16 (baseline) → 质量无损
INT8 (8-bit)         → 几乎无损
Q4_K_M (4-bit)       → 轻微损失，日常可用
Q3_K_M (3-bit)       → 有感知损失，边际场景可用
Q2_K   (2-bit)       → 明显退化，一般不推荐
```

---

## 评估与基准

### 主流评测集

| 基准 | 评估维度 | 说明 |
|------|---------|------|
| **MMLU** | 多学科知识 (57 科) | 大学水平选择题 |
| **MMLU-Pro** | 更难版 MMLU | 10 选项 + 推理型 |
| **HumanEval** | 代码生成 | 164 道 Python 题, pass@k |
| **GSM8K** | 小学数学应用题 | 8.5K 道, 多步推理 |
| **MATH** | 竞赛数学 | 12.5K 题 |
| **HellaSwag** | 常识推理 | 完成句子选择 |
| **ARC** | 科学推理 | 小学科学题 |
| **LMSYS Arena** | 人类偏好 | 盲评 Elo 排名, 公认最难作弊 |
| **AlpacaEval** | 指令遵循 | GPT-4 作裁判打分 |
| **C-Eval / CMMLU** | 中文综合能力 | 中文版 MMLU |
| **LiveCodeBench** | 实时编程 | 持续更新的编程题, 防数据污染 |

### 解读评估分数的坑

1. **数据污染**: 训练集可能包含评测题，分数虚高
2. **评测协议差异**: Few-shot (5-shot) vs Zero-shot, CoT vs 直接答
3. **中文偏差**: 英文评测强的模型不一定中文好
4. **Arena Elo 的局限**: 反映"聊天风格偏好"而非事实准确性
5. **综合判断**: 单一分数不可靠，需要多看几个维度的评测

---

## Prompt Engineering 速查

### 基础技巧

| 技巧 | 说明 | 示例 |
|------|------|------|
| **Zero-shot** | 不给示例，直接提问 | "翻译成英文: 你好" |
| **Few-shot** | 给 2-5 个示例 | "输入: xxx → 输出: yyy" ×3 |
| **Chain-of-Thought** | "Let's think step by step" | 引导模型展示推理过程 |
| **角色设定** | "你是一个资深的 Python 工程师" | 设定专业身份 |
| **格式约束** | "用 JSON 格式返回" | 结构化输出 |
| **反面约束** | "不要使用序号，不要写'首先/其次'" | 禁止特定模式 |

### System Prompt 设计原则

1. **分层清晰**: 角色定位 → 行为准则 → 输出格式 → 边界约束
2. **正面指令优于反面禁止**: "请简洁回答" > "不要啰嗦"
3. **具体化**: "回复不超过 3 句话" > "回复短一点"
4. **优先级标注**: 用 "IMPORTANT:" / "CRITICAL:" 标记核心约束
5. **避免矛盾指令**: "要热情但不要话多" — 矛盾指令导致行为不稳定

### 常见坑

- System prompt 太长 → 末尾指令被忽略 (lost in middle 效应)
- 与 user message 中的指令冲突 → 行为不可预测
- 在多轮对话中 "漂移" → 越聊越偏离角色设定
- 注入 guard 与角色扮演的张力 → 过度安全教育可能破坏性格一致性

---

## 2025-2026 行业关键动态

### 2025 年

- **DeepSeek-R1 发布** (2025.1): 开源推理模型, 性能逼近 o1, 训练成本 ~$6M 震惊业界
- **GPT-4o 图像生成** (2025.3): 原生多模态图像生成, 质量超越 DALL-E 3
- **Gemini 2.5 Pro** (2025.3): 1M 上下文窗口, 推理能力显著提升
- **LLaMA 4** (2025.4): Meta 开源, Scout (17B MoE) 和 Maverick (400B MoE)
- **Claude 4 系列** (2025.5): Opus/Sonnet/Haiku, 200K 上下文, computer use 成熟
- **Qwen 3** (2025.5): 阿里全尺寸开源, MoE 架构, 中文能力显著提升
- **GPT-5** (2025.9): 统一架构, 推理+搜索+多模态深度整合

### 2026 上半年

- **DeepSeek 下一代**: MLA v2 架构改进, 更长上下文
- **开源模型能力持续逼近闭源**: Tier 0 开源模型数量增长
- **Agent 标准化**: MCP 协议广泛采纳, A2A (Agent-to-Agent) 协议出现
- **端侧模型爆发**: 手机上运行 7B 模型成为现实
- **多模态深度融合**: 不再是视觉+文本拼凑, 真正原生多模态理解

### 趋势关键词

- **推理时 Scaling**: 不是训更大模型, 而是推理时多算 (o1/R1 范式的 test-time compute)
- **Agent 化**: LLM 从"聊天工具"向"能主动执行任务的 Agent"转变
- **成本暴跌**: DeepSeek 证明了低成本训练顶级模型的可行性
- **开源闭源差距缩小**: 2024 年初差 12-18 个月, 2026 年中差约 3-6 个月
