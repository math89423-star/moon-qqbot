# 扩散模型与二次元生图知识库

> 覆盖三大模型族系：SDXL 族系、Anima 族系、FLUX 族系。
> 含模型谱系、提示词体系、采样参数、LoRA 生态、选型指南。
> 最后更新: 2026-06

---

# 第一章：SDXL 族系及其分支

SDXL (Stable Diffusion XL) 是 Stability AI 发布的 3.5B 参数 U-Net 架构模型，原生分辨率 1024×1024，使用双 CLIP 文本编码器 (CLIP-L + CLIP-G)。几乎所有主流二次元模型都是 SDXL 的微调或衍生。

## 族系谱系

```
SDXL 1.0 (Stability AI, 2023)
├── Kohaku-XL Beta 5 ──→ Illustrious XL (Onoma AI, 韩国)
│   ├── Illustrious v0.1 (早期版本)
│   │   ├── NoobAI-XL (Laxhar, 基于 v0.1 early-release)
│   │   └── RouWei (基于 v0.1, ~13M图片微调)
│   ├── Illustrious v2.0 Stable (2025.4)
│   ├── Illustrious v3.0 / v3.1
│   └── Illustrious v3.5 vpred
│   └── WAI-NSFW-Illustrious (WAI0731)
│       ├── v7 → v13 → v130 → v160 (持续迭代)
│       └── WAI x Janku + NovaMoon-PM ILL XL (融合版)
├── Pony Diffusion V6 XL (AstraliteHeart / LyliaEngine)
│   └── 独立分支: 专注 MLP/furry/动画/插画, 自建评分数据集
└── Animagine XL (Cagliostro Lab)
    ├── v3.1 → v4.0 (2025.2)
    └── 纯二次元路线，8.4M 图像训练
```

## Illustrious XL 族系 — "二次元之王"

**开发者**: Onama AI Research (韩国)
**基础**: SDXL → Kohaku-XL Beta 5 → Illustrious
**训练数据**: 最高 2000 万张 Danbooru 图片 (持续更新至 2023 年)
**核心特点**: Danbooru 标签体系，原生高分辨率支持 (最高 1024×1536，可扩展至 3744×3744)

### 版本差异

| 版本 | 发布时间 | 特点 |
|------|---------|------|
| v0.1 | 2024 早期 | 原始基座，NoobAI 和 RouWei 的起点 |
| v2.0 Stable | 2025.4 | 稳定版，社区广泛使用 |
| v3.0/v3.1 | 2025 | 角色一致性大幅提升 |
| v3.5 vpred | 2025.5 | V-Pred 变体，色彩更浓对比度更高 |

### Illustrious 提示词体系

**正向前缀**:
```
masterpiece, best quality, newest, highres, absurdres
```

**专属美学标签**:
- `very awa` — Illustrious 独有的美学增强标签，提升画面观感
- `very aesthetic` — 同类效果，不同模型响应略有差异

**画师标签**: 不加前缀，直接写画师名。如 `kantoku, wlop, ask`

**负向提示词**:
```
worst quality, low quality, bad anatomy, bad hands, watermark, text, signature, jpeg artifacts
```

### Illustrious 采样参数

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| Steps | 20-30 | DPM++ 2M SDE 表现最优 |
| CFG | 3.5-7 | 推荐 5，高 CFG 易过饱和 |
| 采样器 | Euler a / DPM++ 2M SDE | Euler a 更灵活，DPM 更精细 |
| 分辨率 | 768×1344 至 1344×768 | 32 的倍数 |
| CLIP Skip | 不需要 / 2 | 动漫模型通常不需要 |

### Illustrious 的优势

- 角色识别: Danbooru 标签体系下角色精确度业界最高
- 画师风格: 识别数万画师风格，数据清洗干净
- 细节表现: 手脚面部大幅优于旧模型
- LoRA 生态: NoobAI/Illustrious 共享数千 LoRA

---

## NoobAI-XL — "更好的 Illustrious"

**开发者**: Laxhar (LAXHAR)
**基础**: Illustrious-xl-early-release-v0
**训练数据**: 完整 Danbooru (~13M) + e621 数据集
**社区共识**: "NoobAI is basically just better Illustrious"

### 版本

- **NoobAI-XL v1.0** (2024.10): Epsilon 版本，使用传统 CFG
- **NoobAI V-Pred 1.0 / V-Pred IV**: 高对比度、更鲜艳色彩，暗部更深
- **推荐采样器**: Euler a (Epsilon) / Euler a (V-Pred)

### NoobAI 独有优势

- **e621 训练**: 对 furry/anthro 内容支持远超纯 Illustrious
- **双标签兼容**: 同时理解 Danbooru 标签和部分 Pony 系 score 标签
- **风格范围广**: 动漫 → 半写实 → 赛博朋克，LoRA 依赖更低
- **LoRA 跨兼容**: NoobAI LoRA 可直接用于 Illustrious，反之亦然

### NoobAI 采样参数

| 参数 | Epsilon | V-Pred |
|------|---------|--------|
| CFG | 4-9 (推荐 7) | 3-5 |
| Steps | 20-30 | 20-30 |
| 采样器 | Euler a | Euler a |
| 分辨率 | 与 Illustrious 相同 | 与 Illustrious 相同 |

---

## WAI-NSFW-Illustrious — 实战派

**开发者**: WAI0731
**基础**: Illustrious 各版本微调/融合

### 版本迭代

| 版本 | 特点 |
|------|------|
| v7-v13 | 早期版本，逐步优化 |
| v130 | 色彩更饱和，需 CFG 3-5 避免过饱和 |
| v160 | 2025 年最新，平衡性最佳 |

### WAI 采样注意

- WAI v130+: CFG 3-5（比 Illustrious 低，高 CFG 会色彩爆炸）
- 与 Illustrious/NoobAI LoRA 完全兼容
- 适合作为融合基底和 LoRA 训练基础

---

## RouWei — 新兴高质量竞争者

**开发者**: Minthybasis
**基础**: Illustrious v0.1 大规模微调 (~13M 图片，含 ~4M 自然文本标注）
**核心卖点**: 解决了 Illustrious/NoobAI 的标签泄漏和偏差问题

### 核心优势

- **最佳 prompt 遵循度**: 标签无泄漏、无偏差，ASR (Average Semantic Recall) 业界最高
- **识别 50,000+ 画师风格**: 数据清洗干净，热门风格无水印
- **自然文本支持**: v0.8+ 版理解自然语言，配合 ToriiGate short mode 最佳
- **画师标签需 "by" 前缀**: 如 `by kantoku, by wlop`。用 BREAK 分隔或放在末尾

### RouWei 采样参数

| 参数 | Epsilon | V-Pred |
|------|---------|--------|
| CFG | 4-9 (最佳 7) | 3-5 |
| 采样器 | Euler a | Euler a |
| Steps | 20-28 | 20-28 |
| 分辨率 | ~1MP (1024×1024, 1216×832) | 同左 |

### RouWei 提示特点

- 质量标签极简: 只需 `masterpiece, best quality`（正向）+ `low quality, worst quality`（负向）
- 不要在负向里放 `greyscale, monochrome` 等非必要标签
- 亮度/色彩控制标签可用: `low brightness, high saturation, hdr, sdr, soft colors`

---

## Pony Diffusion V6 XL — 独立分支

**开发者**: AstraliteHeart / PurpleSmartAI / LyliaEngine
**基础**: SDXL 1.0（非 Illustrious 衍生）
**训练数据**: ~2.6M 人工美学评分图片（9 分制）

### 评分标签体系

Pony 使用独特的美学评分标签。由于训练问题，单独 `score_9` 效果弱，必须用完整链:

```
score_9, score_8_up, score_7_up, score_6_up, score_5_up, score_4_up
```

- `score_9` = 最高品质
- `score_8_up` = 高分+写实质感
- `score_7_up` = 二次元质感
- 负向用: `score_4, score_5, score_6`

### 源标签与分级

| 标签 | 作用 |
|------|------|
| `source_anime` | 动漫风格 |
| `source_pony` | MLP/pony 风格 |
| `source_furry` | furry/anthro 风格 |
| `source_cartoon` | 卡通风格 |
| `rating_safe` | SFW |
| `rating_questionable` | 轻度 |
| `rating_explicit` | 明确成人内容 |

### Pony 采样参数

| 参数 | 推荐值 |
|------|--------|
| CFG | 5-7 |
| Steps | 25-40 |
| 采样器 | Euler a / Euler |
| 分辨率 | 1024×1024 (SDXL 标准) |
| CLIP Skip | 2 (关键! 不设会导致画质下降) |

### Pony 特点

- **自然语言友好**: 50% 训练数据有详细自然语言标注
- **画师标签无效**: 训练时删除了画师数据库，用风格描述替代
- **LoRA 生态独立**: Pony LoRA 不能直接用于 Illustrious 系
- **适合**: 不喜欢标签体系的新手、西方卡通风格、MLP/furry 内容

---

## Animagine XL 4.0 — 纯二次元

**开发者**: Cagliostro Lab
**基础**: SDXL 1.0
**最新**: v4.0 (2025.2)，知识截止 2025.1.7

### 特点

- **8.4M 纯二次元图片训练**
- **稳定输出**: 简单 prompt 即可获得一致高质量结果
- **4.0 改进**: 更精确比例、减少噪点/伪影、修复低饱和度问题
- **ViT 美学分类器**: `very aesthetic` 标签触发更好的构图
- **负向推荐**:
```
nsfw, lowres, (bad), text, error, fewer, extra, missing, worst quality,
jpeg artifacts, low quality, watermark, unfinished, displeasing, oldest,
early, chromatic aberration, signature, extra digits, artistic error,
username, scan, [abstract]
```

### 当前地位

已被 Illustrious 和 NoobAI 超越，但仍然是纯二次元稳定出图的好选择。社区趋势是 Illustrious/NoobAI 成为主力。

---

## SDXL 族系选型速查

| 需求 | 推荐模型 | CFG | Steps | 采样器 |
|------|---------|-----|-------|--------|
| 角色精确复现 | Illustrious v3.1 | 5 | 20-30 | DPM++ 2M SDE |
| 最广风格+最少LoRA | NoobAI-XL | 5-7 | 20-30 | Euler a |
| 色彩浓郁+对比强 | NoobAI V-Pred | 3-5 | 20-30 | Euler a |
| prompt 遵循度最高 | RouWei | 7 | 20-28 | Euler a |
| NSFW/ furry | NoobAI / WAI | 4-6 | 20-30 | Euler a |
| 新手友好/自然语言 | Pony V6 XL | 5-7 | 25-40 | Euler a |
| 纯二次元稳定出图 | Animagine 4.0 | 5-7 | 25-30 | Euler a |
| LoRA 训练基底 | NoobAI-XL | — | — | — |

---

# 第二章：Anima 族系 (2026.5 发布)

> **Anima 是 2026 年二次元生图最重要的新模型族。** 它不是 SDXL 的衍生，而是基于全新 DiT (Diffusion Transformer) 架构的独立族系。

## 架构与技术

**开发者**: CircleStone Labs × Comfy Org
**架构基础**: NVIDIA Cosmos-Predict2-2B (DiT 主干)
**核心创新**: 用 Transformer 替代 U-Net 做降噪，图像以 token 形式通过注意力机制处理

### 核心技术栈

| 组件 | 详情 |
|------|------|
| 参数规模 | 2B (DiT Backbone) |
| 文本编码器 | Qwen3 0.6B (LLM 级理解，非 CLIP) |
| VAE | Qwen-Image VAE (基于 Wan 2.1 微调，16 通道 vs SDXL 4 通道) |
| 训练数据 | 数百万动漫图片 + 80 万非动漫艺术图，无合成数据 |
| 文件大小 | ~4GB (vs SDXL 的 ~6.5GB) |
| 许可证 | CircleStone Labs 非商用许可 |
| 知识截止 | 2025 年 9 月 |

### DiT vs U-Net 本质差异

- **U-Net (SDXL 系)**: 卷积降噪，逐步去噪潜空间张量。数学上更"平滑"，但每个像素独立处理
- **DiT (Anima)**: 注意力降噪，图像 token 通过自注意力/交叉注意力交互。全局一致性更好，但计算量更大
- **结果差异**: Anima 的全局一致性（背景+角色+光影的统一感）天然优于 SDXL 系，但 SDXL 系的逐像素精细控制更强（ControlNet 依赖这种特性）

## 模型族成员

### 官方模型

| 模型 | 发布日期 | 说明 |
|------|---------|------|
| Anima Preview 0.3 | 2026.2 | 早期预览版，中分辨率，有伪影 |
| Anima-Base v1.0 | 2026.5.14 | 预训练基座，无美学微调，灵活性最高 |
| Anima-Turbo | 待发布 | 加速推理版，预计 8-16 步 |

### 社区衍生模型

| 模型 | 开发者 | 特点 |
|------|--------|------|
| WAI-Anima v1 | WAI0731 (2026.4.15) | 美学调优，标签响应更好，角色一致性提升 |
| JANIMA v1.0 | Civitai 社区 | 更锐利的细节和更好的解剖结构 |
| CottonAnima | — | 柔和风格变体 |
| Kirazuri | — | 特定风格优化 |
| Hexer Minimal Toon Anima V1 | — | 极简卡通风格 |
| Cat Tower | — | 猫耳角色特化 |

### LoRA 生态

- **官方支持**: sd-scripts (kohya-ss) 训练脚本已适配 + AnimaLoraToolkit
- **已有 LoRA**: Nijijourney 风格、Alice 家族风格、各类角色 LoRA
- **数量**: 远少于 Illustrious（数百 vs 数千），但快速增长中
- **编码器升级**: 社区发布 Qwen 3.5 4B 编码器替代默认 Qwen3 0.6B，提升标签保真度

## Anima 提示词系统

### 质量标签 (两套可混用)

| 人工评分系 | Pony 美学系 |
|-----------|-----------|
| `masterpiece` | `score_9` |
| `best quality` | `score_8` |
| `good quality` | `score_7` |
| `normal quality` | `score_6`...`score_1` |

**建议正向前缀**:
```
masterpiece, best quality, score_7, safe,
```

### 安全标签

`safe`, `sensitive`, `nsfw`, `explicit`

### 年代标签

`year 2025, year 2024, ... , newest, recent, mid, early, old`

### 画师标签

**核心规则: 使用 `@` 前缀**

- `@big chungus`, `@kantoku`, `@wlop`
- 不加 `@` 则画师风格效果极弱
- 多画师混合注意权重控制

### 混合提示词（自然语言 + 标签）

Anima 的 LLM 文本编码器天然理解自然语言，这是它相比 SDXL CLIP 的核心优势:

```
masterpiece, best quality, score_7, safe,
A girl is reading a book indoors and red hair and long hair and ponytail,
warm afternoon light from window, dust particles in sunbeam
```

**关键原则**:
- **短 prompt 会导致崩坏**: Anima-Base 是无美学微调的基座模型——必须写足够详细的 prompt 或使用质量/画师标签
- **自然语言描述空间关系**: Anima 比 SDXL 系更擅长理解复杂空间描述
- **多角色区分**: 用自然语言描述不同角色的属性差异

### 负向提示词（极简主义）

```
worst quality, low quality, score_1, score_2, score_3, artist name
```

> ⚠️ **核心警告**: 请勿从 Illustrious/SDXL 复制大段负面提示词到 Anima。Anima 对负面提示词的敏感度不同。`bad hands, extra fingers, watermark, text` 这种长列表不应该默认添加——只在看到特定问题时才针对性加。

## Anima 采样参数

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| Steps | 30-50 | DiT 需要比 U-Net 更多步 |
| CFG | 4-6 (推荐 5) | DiT 对 CFG 的响应曲线与 U-Net 不同 |
| 默认采样器 | er_sde | 中性风格，平涂色彩，线条锐利 |
| 替代采样器 | euler_a | 更柔和，偏 2.5D |
| 替代采样器2 | dpmpp_2m_sde_gpu | 更多变化和创意 |
| 分辨率 | 512² – 1536² | 832×1216 或 1024×1024 最佳 |

### Anima 速度特性

- 单图生成: 275s (M1 Max) — 比 SDXL (~217s) 慢约 25%
- 但复杂场景稳定: SDXL 在动态场景下变慢（~337s），Anima 几乎不变
- **SPEED 加速**: ComfyUI-SPEED 节点使用频谱渐进扩散，可达 ~1.8× 加速
- **低显存**: 4GB vs SDXL 6.5GB，适合消费级 GPU

## Anima LoRA 训练

### 核心原则

| 原则 | 说明 |
|------|------|
| 不训练 LLM Adapter | `llm_adapter_lr=0`，TE 与 DiT 之间的 6 层 Transformer 极易退化 |
| 低学习率 | rank 32 推荐 2e-5 起步 |
| 轻触即止 | 基座模型已含大量视觉概念，无需对抗美学微调/RLHF |

### 关键发现: 甜区是 ep150-180，不是 12000 步

社区大规模测试 (lilting.ch, 2026) 明确结论:

| 每图曝光量 | Epochs (53图×4 repeats) | 方向命中率 |
|-----------|------------------------|-----------|
| 400 | ep100 | 0% (太浅) |
| **600** | **ep150** | **100% (甜区)** |
| **720** | **ep180** | **100% (平台)** |
| 800 | ep200 | 0% (崩坏/ghosting) |

**官方 "12000+ 步" 建议导致灾难性遗忘** — 方向控制变差而非变好。真正的甜区是每图 600-720 次曝光。

### 推荐训练配置

```yaml
resolution: 1024
repeats: 4
lora_rank: 32
lora_alpha: 32.0
epochs: 150
batch_size: 1
grad_accum: 4
learning_rate: 2.0e-5
mixed_precision: bf16
xformers: false          # 关键! 避免 torch cu130 冲突
flip_augment: false      # 水平翻转导致侧马尾方向丢失
cache_latents: true
cache_text_encoder_outputs: true
```

### 标注最佳实践

- **触发词**: 单个小写词 (如 `kanachan`)
- **发色/瞳色吸收进触发词**: 从标注中移除颜色相关描述，防止色偏
- **方向信息放自然语言**: 标签如 `left side ponytail` 对 Anima 不承载方向信息
- **质量前缀**: `masterpiece, best quality, score_7, safe`
- **无年份标签**: 训练时不加年份，避免锁定时代风格
- **去权重**: 删除所有 `(tag:1.5)` 强调 — 训练用平权标签

## Anima 的优势与局限

### 优势

- **氛围感背景**: 绘画级光影、景深、戏剧性构图 — SDXL 系无法匹敌
- **动态场景**: 自然的姿态和物理感（跑步、风吹、流水）
- **默认细节**: 眼睛/脸部/手部无需额外修图节点
- **色彩光影**: 真实的光泽、环境光包裹、皮肤半透明的次表面散射感
- **显存友好**: 4GB vs SDXL 6.5GB
- **自然语言理解**: LLM 文本编码器，理解复杂空间关系和自然描述

### 局限

- **角色一致性不如 Illustrious**: 同一角色跨场景的一致性弱（SDXL 系的逐像素控制在这里是优势）
- **短 prompt 出怪图**: 基座无美学微调，必须写详细 prompt
- **多风格融合不稳定**: >2 个风格标签混用可能崩
- **新角色识别弱**: 知识截止 2025.9
- **ControlNet 缺失**: 这是 Anima vs Illustrious 的最大功能差距
- **非商用许可**: 商用场景必须选 Illustrious/NoobAI
- **文字渲染弱**: 只能处理单个词/短词组
- **方向偏见**: 内置方向偏见（如侧马尾默认朝左），LoRA 难以完全覆盖

---

# 第三章：FLUX 族系

> FLUX 是 Black Forest Labs 开发的 DiT (Diffusion Transformer) 模型族，与 Anima 同属 Transformer 架构阵营。12B 参数（FLUX.1）是当前最大的开源生图模型之一。

## 族系谱系

```
FLUX 家族 (Black Forest Labs)
├── FLUX.1 [pro] — 商业 API，不开放权重
├── FLUX.1 [dev] — 开放权重，非商用，12B 参数 ← 社区主力
├── FLUX.1 [schnell] — Apache 2.0，4 步蒸馏 ← 商用友好
└── FLUX.2 Klein — 小型化家族
    ├── Klein 4B — Apache 2.0，消费级 GPU
    └── Klein 9B — Apache 2.0，性能与体积的平衡点
```

## 核心技术差异

| 特性 | FLUX.1 dev | FLUX.2 Klein 9B | SDXL (对比) |
|------|-----------|-----------------|-------------|
| 架构 | 12B DiT + 双编码器 | 9B DiT + Qwen3 编码器 | 3.5B U-Net + 双 CLIP |
| 文本编码器 | CLIP + T5-XXL | Qwen3 (LLM 级) | CLIP-L + CLIP-G |
| 提示词理解 | 自然语言（T5 读句子） | 自然语言（LLM 理解更佳） | Danbooru 标签最佳 |
| 许可证 | 非商用 | **Apache 2.0** | OpenRAIL-M |
| 最低显存 | ~24GB (bf16) | ~13GB (4B) | ~8GB |
| 原生分辨率 | 1024×1024 | 768×1152 / 1056×1584 | 1024×1024 |

## FLUX 提示词体系 — 与 SDXL 完全不同

### 核心规则

1. **无负向提示词**: FLUX 不支持负向。描述你想要什么，别描述不想要什么
2. **无 SD 风格权重**: `(word:1.5)` 或 `++` 语法无效
3. **自然语言 >> 标签**: T5-XXL / Qwen3 是 LLM 级编码器，读句子不读标签
4. **长提示词更好**: 100+ token 的详细描述效果优于短标签

### 提示词结构公式

```
[主体] + [动作/姿态] + [风格/媒介] + [环境/背景] + [光照] + [摄影/技术参数]
```

**好例子** (自然语言):
```
A woman in a red silk dress standing barefoot on a sandy beach at sunset,
warm golden light behind her, shallow depth of field with soft bokeh
across the water, 35mm film still, cinematic lighting
```

**坏例子** (关键词列表):
```
woman, red dress, beach, sunset, bokeh, 8k, masterpiece, best quality
```

### FLUX vs SDXL 提示词对比

| 提示词风格 | 适合 FLUX | 适合 SDXL |
|-----------|----------|----------|
| 自然语言描述段落 | ✅ 最佳 | ❌ 效果差 |
| Danbooru 标签列表 | ❌ 效果差 | ✅ 最佳 |
| 自然语言 + 标签混合 | ⚠️ 可用 | ⚠️ 可用 |
| 光照/摄影术语 | ✅ 极佳 | ⚠️ 可用 |

## FLUX.1 dev — 社区主力

### 采样参数

| 参数 | FLUX.1 dev |
|------|-----------|
| Steps | 30-40 (distilled，不可太低) |
| CFG (Distilled) | **3.5** (固定值，不是可调参数) |
| 采样器 | Euler |
| Clip Skip | 1 |
| 分辨率 | 1024×1024 / 768×1152 |
| 高清放大 | 4x-AnimeSharp, denoise 0.20, steps 10 |

### 动漫 LoRA

FLUX.1 dev 的动漫能力完全依赖 LoRA:

| LoRA | 触发词 | 推荐强度 |
|------|--------|---------|
| MJanime_Flux_LoRa_v3 | `Anime style` | 0.9-1.3 |
| Canopus-LoRA-Flux-Anime | `Anime` | 0.8-1.0 |
| Retro Anime Flux | 无(描述风格即可) | 0.8-1.2 |
| FLUX.1 Dev LoRA 新海诚 | 无 | 1.0 |
| Seedream 4.0 Meets Flux | `S33DR34M` | 0.5-1.0 |

### FLUX.1 动漫的定位

- **并非二次元主力**: FLUX.1 本质上是通用模型，动漫靠 LoRA "外挂"
- **场景/光影极佳**: 自然语言描述环境光影的能力远超 SDXL 系
- **角色控制弱**: 没有 Danbooru 标签体系，角色精确度不如 Illustrious
- **用于背景+氛围**: 生成背景场景后搭配 SDXL 系角色合成

## FLUX.2 Klein — 速度革命

### 核心突破

| 特性 | FLUX.2 Klein 9B |
|------|----------------|
| Steps | **4 步** (distilled) + 1 upscale pass |
| CFG | **1.0** (远低于 FLUX.1 的 3.5) |
| 文本编码器 | Qwen3 (比 T5-XXL 更轻量更准确) |
| 许可证 | **Apache 2.0** (商用友好!) |

### 动漫 LoRA

| LoRA | 特点 |
|------|------|
| KleinAnime Flux2-9B v1.0 | 完整微调，非 LoRA。Danbooru-top10k 训练，计划扩展至 50k |
| Neurocore Anime Cyberpunk | 触发词 `in the style of cknc,` |
| Dark Anime (ChronoKnight) | 无触发词，直接描述暗黑动漫风格 |
| Manga style (Naoki Urasawa) | 浦泽直树漫画风格 |
| Painterly Fantasy | 油画风格奇幻 |

### Klein 动漫采样

| 参数 | 推荐值 |
|------|--------|
| Steps | 4 + 1 upscale |
| CFG | 1.0 |
| 采样器 | Euler |
| 分辨率 | 768×1152 / 1056×1584 |
| 高清放大 | NMKD_SIAX, denoise 0.35 |
| LoRA Strength | 0.8-1.0 |
| 触发词 | 很多 Klein LoRA 不需要触发词 |

### Klein 的优势

- **4 步出图**: 比 FLUX.1 快 ~8 倍，比 SDXL 快 ~3 倍
- **Apache 2.0**: 商用无限制，游戏/产品开发首选
- **Qwen3 编码器**: 中文理解力远超 T5-XXL，中英混合 prompt 效果好
- **低显存**: 4B 版仅需 ~13GB，RTX 3090/4070 可用

---

# 三族系全景对比

## 架构层级

| 维度 | SDXL 族系 | Anima 族系 | FLUX 族系 |
|------|----------|-----------|----------|
| 架构 | U-Net (3.5B) | DiT (2B) | DiT (4B-12B) |
| 文本编码器 | CLIP (0.3B) | Qwen3 LLM (0.6B) | CLIP+T5 / Qwen3 LLM |
| 成熟度 | 🏆 最成熟 | 快速增长 | 中等 |
| LoRA 生态 | 🏆 数千个 | 数百个 | 数百个 |
| ControlNet | 🏆 完整支持 | ❌ 官方缺失 | ⚠️ 有限 |

## 二次元质量

| 维度 | SDXL 族系 | Anima 族系 | FLUX 族系 |
|------|----------|-----------|----------|
| 角色精确度 | 🏆 Illustrious | ⚠️ 弱于 SDXL | ❌ 弱(靠 LoRA) |
| 背景/氛围 | ⚠️ 还行 | 🏆 最佳 | 🏆 优秀 |
| 手/面部 | ⚠️ 需修图节点 | 🏆 默认就好 | ⚠️ 看 LoRA |
| 多角色 | ⚠️ 标签分隔 | 🏆 自然语言区分 | 🏆 自然语言区分 |
| 风格丰富度 | 🏆 最丰富 | 🏆 优秀 | ⚠️ 受 LoRA 限 |

## 实用选型矩阵

| 你的需求 | 推荐模型 | 理由 |
|---------|---------|------|
| 角色立绘/多角度一致 | Illustrious + LoRA | 角色一致性业界最佳 |
| 氛围感插画/壁纸 | Anima | 背景和光影无可匹敌 |
| 快速出图+商用 | FLUX.2 Klein 9B | 4 步 + Apache 2.0 |
| 二次元新手 | Pony V6 XL | 自然语言+简单标签 |
| prompt 精确控制 | RouWei | ASR 业界最高 |
| 最广风格无 LoRA | NoobAI-XL | e621+Danbooru 双料训练 |
| 动漫角色场景合成 | Anima (场景) + Illustrious (角色) | 两个模型分工 |
| 低显存 (4-6GB) | Anima (FP8) 或 FLUX.2 Klein 4B | 小模型友好 |
| LoRA 训练发布 | NoobAI-XL 或 WAI-Anima | 社区兼容性最好 |
| 中英混合 prompt | FLUX.2 Klein (Qwen3) | Qwen3 中文理解强 |

---

# 采样器速查

| 采样器 | 架构 | 特点 | 适用 |
|--------|------|------|------|
| Euler a | U-Net | 经典，带随机性 | SDXL 快速探索 |
| Euler | U-Net/DiT | 确定性，收敛稳定 | FLUX / 需要复现结果 |
| DPM++ 2M Karras | U-Net | 速度快质量高 | SD1.5/SDXL 最通用 |
| DPM++ 2M SDE Karras | U-Net | 细节更丰富 | SDXL 追求表现力 |
| DDIM | U-Net | 低步数可用 | SDXL 快速测试 |
| UniPC | U-Net | 收敛极快 | SDXL 速度优先 |
| er_sde | DiT | 平涂色彩，线条锐利 | **Anima 默认** |
| DEIS Simple | DiT | 简单快速 | FLUX 动漫 LoRA |

---

# 常见问题排查

## 画面崩坏 / 肢体扭曲
- 降低 CFG（过高 CFG 是最主要原因）
- 增加步数（30+）
- Anima: 确保 prompt 足够详细，短 prompt 必崩
- FLUX: 检查 CFG 是否设为蒸馏固定值（dev=3.5, Klein=1.0）
- 检查分辨率是否在模型训练范围内

## 色彩过饱和
- 降低 CFG（WAI v130+ 建议 3-5；Pony 建议 5-7）
- Anima: 尝试 CFG 3-4
- 不要叠加多个美学标签

## 画面发灰 / 色彩暗淡
- 检查 VAE 是否正确加载
- SDXL: CFG < 3 会发灰
- Anima/FLUX: 检查分辨率，过低分辨率导致模糊
- SDXL 动漫: 尝试 `very awa` 或 `very aesthetic` 标签

## 角色不像 / 特征错误
- LoRA 权重调参 (推荐 0.7-0.85)
- 检查触发词精确性（必须匹配 LoRA 说明页）
- Anima: 角色一致性弱于 Illustrious，降低预期或用 WAI-Anima
- 确认角色在模型训练截止日期之前

## LoRA 污染 / 特征泄漏
- 多 LoRA 叠加总权重 ≤ 2.0 (SDXL) / 1.5 (Anima)
- 冲突 LoRA（两个不同角色）会互相污染
- 使用 LoRA Block Weight 限制作用范围
- LyCORIS (LoHa/LoKr) 比普通 LoRA 参数更多效果更好

## Anima 专有问题
- **短 prompt 崩**: 基座无美学微调 — 必须详细 prompt 或加质量/画师标签
- **多风格崩**: 最多 1-2 风格标签
- **文字乱码**: Anima 文字渲染弱，不要要求生成复杂文字
- **角色不认识**: 知识截止 2025.9，新角色需要 LoRA
- **方向偏见**: 内置方向偏见（侧马尾默认左），标注中加强自然语言方向描述

## FLUX 专有问题
- **负向不生效**: FLUX 不支持负向提示词，改用正向描述排除
- **CFG 概念不同**: FLUX 的 CFG 不是可调参数，是蒸馏固定值
- **低步数糊**: FLUX.1 dev 至少 30 步，Klein 至少 4 步
- **T5 编码器慢**: FLUX.1 dev 的 T5-XXL 编码是速度瓶颈，Klein 用 Qwen3 解决
