# 提示词工程：从入门到精通

> 覆盖提示词体系总览、Danbooru标签体系、质量标签、画师标签、权重语法、各模型专属格式、负面提示词分层、结构化模板、常见误区。
> 最后更新: 2026-06

---

## 提示词体系总览：tag 式 vs 自然语言式 vs 混合式

AIGC 生图的提示词有三种基本体系，不同模型适用不同的体系——**选错体系比写错内容更致命**。

### tag 式（Danbooru 标签体系）

用逗号分隔的独立标签描述画面，每个标签是一个概念单元。这是 SD 1.5 / SDXL / Illustrious / NoobAI / Pony 的原生语言。

```
1girl, solo, long hair, blue eyes, school uniform, standing, hallway, soft lighting, masterpiece, best quality
```

核心特点：
- 标签之间独立，顺序有影响但不绝对
- 模型在 Danbooru 标签上训练，对标签响应精准
- 不适合写完整句子——"a girl with long blue hair" 能工作但不如 "1girl, long hair, blue hair" 精确

### 自然语言式

用完整英文句子描述画面，像给人类画师的 brief。这是 FLUX / SD3 / Anima 的原生语言。

```
A young woman with long blue hair and gentle green eyes, sitting by an arched stone window under moonlight, wearing an elegant layered purple gown, soft pink-purple rim light illuminating her silhouette, calm serene atmosphere
```

核心特点：
- 不需要标签化，自然描写即可
- 对空间关系、光影氛围的描述理解远超 tag 式
- 不适合写孤立的 `1girl, solo` 这种标签——会降低自然语言部分的质量

### 混合式（Anima 专属）

Anima 模型族使用 Qwen3 LLM 作为文本编码器，同时理解标签和自然语言。最佳实践是 **@artist 标签 + 自然语言描述** 混用。

```
@ask, @mika picaso, 1girl, solo, sitting by an arched window, gradient pink to purple long hair, pointed elf ears, masterpiece, highres
```

**关键**：Anima 下的 `@artist1, @artist2` 不是冲突而是**风格融合**——模型会尝试混合两位画师的风格。所以评价 Anima prompt 时说"两个画师标签冲突"属于**知识错误**。

### 选择速查表

| 模型 | 推荐体系 | 备注 |
|------|---------|------|
| Illustrious / NoobAI / WAI | tag 式 | Danbooru 标签是原生语言 |
| Pony Diffusion V6 | tag 式 + 来源标签 | 必须加 `score_9, source_anime` 等 |
| Anima 系列 | 混合式 | `@artist` + 自然语言描述 |
| FLUX / FLUX.2 | 纯自然语言 | 不要用 tag，不要用负向 |
| SD3 / SD3.5 | 自然语言优先 | 可少量 tag 辅助 |

---

## Danbooru 标签体系详解

Danbooru 是二次元生图最重要的标签来源。理解其标签分类和语法规则是写好 tag 式 prompt 的基础。

### 标签分类

| 类别 | 示例 | 说明 |
|------|------|------|
| **人物数量** | `1girl`, `2boys`, `solo`, `multiple girls` | 决定画面几人 |
| **角色名** | `hatsune_miku`, `frieren_(sousou_no_frieren)` | 触发模型对该角色的知识 |
| **身体特征** | `long_hair`, `blue_eyes`, `ahoge`, `pointed_ears` | 角色外观 |
| **服装** | `school_uniform`, `kimono`, `armor`, `barefoot` | 穿着 |
| **姿势** | `sitting`, `standing`, `looking_at_viewer`, `arms_up` | 动作和面向 |
| **表情** | `smile`, `half-closed_eyes`, `blush`, `open_mouth` | 情绪表达 |
| **背景/场景** | `night_sky`, `window`, `cityscape`, `underwater` | 环境 |
| **风格/渲染** | `cel_shading`, `watercolor_(medium)`, `thick_outlines` | 艺术风格 |
| **meta/质量** | `masterpiece`, `absurdres`, `official_art` | 质量控制和来源 |
| **版权/IP** | `genshin_impact`, `honkai_star_rail` | 触发 IP 美术风格 |

### 标签语法规则

1. **下划线代替空格**：`long hair` → `long_hair`，`school uniform` → `school_uniform`。模型在 Danbooru 标签上训练，下划线是标签内部分界符，空格是标签之间的分界符
2. **括号表示歧义消解**：`watercolor_(medium)` 区分"水彩颜料"和"水彩画风"，`frieren_(sousou_no_frieren)` 指定具体作品的芙莉莲
3. **标签顺序**：越靠前的标签权重越高。通用建议：质量 meta → 人物数量 → 角色 → 身体特征 → 服装 → 姿势/表情 → 背景 → 风格

### 中文标点 vs 英文标点的实际影响

用户 prompt 中常见 `、`（中文顿号）替代 `,`（英文逗号）。**实际影响很小**——绝大部分模型的 tokenizer 将 `、` 和 `,` 视为等价分隔符。这不是需要批评的问题。`one hand resting on cheek、the other draped over knee` 在 tokenize 后与用 `, ` 几乎一致。

---

## 质量标签的真实作用

质量标签是生图社区最被滥用的部分。理解其真实作用机制。

### 通用质量标签

| 标签 | 实际作用 | 作用范围 |
|------|---------|---------|
| `masterpiece` | 提升画面完成度和细节密度 | SDXL 系及衍生模型 |
| `best quality` | 与 masterpiece 近似，效果有重叠 | SDXL 系 |
| `absurdres` | 提示模型产出高分辨率级细节 | 所有模型微弱响应 |
| `highres` | 类似 absurdres 但更弱 | 部分模型 |
| `newest` | 数据集中较新的风格偏好 | Illustrious 系 |
| `safe` | 排除 NSFW 内容倾向 | 大多数模型弱响应 |

### 质量标签的边际效益递减

**第一条 `masterpiece` 有显著效果，第二条质量标签效果减半，第三条几乎无感知差异。**

```
masterpiece                                    ← 效果显著
masterpiece, best quality                       ← 微幅提升
masterpiece, best quality, highres, absurdres   ← 几乎无额外效果，浪费 token
```

实用建议：**`masterpiece, best quality` 两个字就够了**。`score_7, score_8, score_9` 只在 Pony 模型下有效（Pony 的训练集用评分标签做质量分级），在其他模型下等同于无效 token。

### `score_7` / `score_8` / `score_9` 的模型限定

NovelAI 系评分标签：
- **仅在 Pony Diffusion 和 NovelAI 官方模型下有效**
- 在其他 Illustrious/NoobAI 模型上基本无影响——不是"有害"而是"占位无用"
- `safe` 标签在 Pony 系下的含义与普通模型不同：Pony 的 `safe` 是强过滤信号

### `year 2025` / `newest` 的实际含义

这些标签影响模型对"最新画风"的倾向，属于弱信号。同时使用 `year 2025, newest` 不构成冲突——它们是同向增强，不是矛盾。批评它们"重复"属于**过度挑剔**，因为不同模型对这两个标签的响应机制不完全重叠。

---

## 画师标签系统：跨模型差异

画师标签是最强的风格控制手段——比任何"风格描述词"都有效。但使用规则跨模型差异巨大。

### Anima 系列：`@` 前缀

Anima 的 Qwen3 编码器使用 `@` 前缀标记画师：
```
@ask, @mika picaso, @wlop, @krenz cushart
```

关键规则：
- **多画师 = 风格融合，不是冲突**：Anima 会尝试融合多个 `@artist` 的风格。`@ask, @mika picaso` 的效果是混合两者的色彩/构图/笔触倾向
- `@` 前缀是 Anima 的特有语法，裸写 `ask, mika picaso` 在 Anima 下召回率下降
- 推荐 2-3 个画师混合，超过 5 个风格会稀释得不伦不类

### Illustrious / NoobAI：裸写画师名

这些 Danbooru 体系的模型直接使用画师标签名（不含 `@`）：
```
kantoku, wlop, ask_(artist), mika_pikazo
```

关键规则：
- 画师名是标准 Danbooru tag，带括号消歧
- 多画师混合有效，但不同画师的风格差异太大会导致画面不稳定
- 推荐 1-2 个互相兼容的画师组合

### Pony Diffusion：评分标签 + 来源标签

Pony 有独特的三层标签系统：
```
score_9, score_8_up, source_anime, rating_safe, 1girl, ...
```
- `score_9` / `score_8_up` 等评分标签是 Pony 的**强制需求**——不写画面质量会显著下降
- `source_anime` / `source_cartoon` / `source_furry` 等来源标签影响风格基调

---

## 提示词权重语法

不同平台支持的权重语法有差异，给建议前先确认用户在用什么。

### ComfyUI 原生

ComfyUI 默认不支持 `(word:1.5)` 格式——CLIPTextEncode 节点直接编码所有文本。需要安装 SD Prompt Reader 等自定义节点才支持权重语法。

### WebUI / Forge / reForge

| 语法 | 效果 | 示例 |
|------|------|------|
| `(word)` | 权重 ×1.1 | `(blue eyes)` |
| `((word))` | 权重 ×1.21 (1.1²) | `((masterpiece))` |
| `(word:1.5)` | 精确权重 | `(flower crown:1.3)` |
| `[word]` | 权重 ×0.9 (降权) | `[blurry background]` |
| `{word}` | 权重 ×1.05 (增强) | `{detailed eyes}` |

### BREAK 关键字

在 WebUI 中使用 `BREAK` 分隔不同区域，每个区域被独立编码后 concat——用于避免不同主体之间的概念污染：
```
1girl, blue eyes, blonde hair BREAK white dress, standing BREAK cherry blossom background
```

### 通用建议

给群友推荐权重语法前，先问一句"你用的是 WebUI 还是 ComfyUI"——在 ComfyUI 原生环境下推荐 `(word:1.5)` 是无用建议。

---

## 各模型专属提示词模板

### Illustrious / NoobAI / WAI 系

```
masterpiece, best quality, newest, 1girl, solo, [角色特征], [服装], [姿势], [表情], [背景], [风格tag]
```

负向：
```
worst quality, low quality, bad anatomy, bad hands, watermark, text, signature, jpeg artifacts, extra fingers, missing fingers
```

提示：
- 角色 tag 可以写 Danbooru 角色名（如 `hatsune_miku`）触发模型角色知识
- 自然语言短句（如 `sitting elegantly against an arched window`）也能工作，但不如标签精确
- 不要混入中文——Danbooru 体系模型不训练中文

### Anima 系

```
@ask, @mika picaso, 1girl, solo, masterpiece, highres, [自然语言场景描述], [氛围/光影描述]
```

负向：
```
nsfw, lowres, bad anatomy, bad hands, extra fingers, blurry, watermark, text
```

提示：
- **Anima 对自然语言的理解远超 tag 式模型**。写 `soft pink-purple moonlight streaming through the arched window creating rim light on her hair and shoulders` 这种长句在 Anima 上效果极好——这不是"冗余"，这是充分利用 LLM 编码器的优势
- 中文描述在 Anima 下也能部分理解（Qwen3 是中英双语），但不推荐——效果不如英文
- `year 2025` 和 `newest` 在 Anima 下推荐保留——Anima 训练时用了年代标签做风格控制
- `safe` 标签在 Anima 下推荐保留——防止模型向 NSFW 漂移

### FLUX / FLUX.2

```
[纯自然语言段落描述]
```

**负向：不写负向提示词**。FLUX 的 CFG 机制与传统 SD 不同，写负向反而可能干扰。

提示：
- FLUX 不使用 tag 式——写了 `1girl, solo` 这些标签反而浪费 token
- 自然语言的空间关系和光影描述是 FLUX 的强项
- FLUX 不需要质量标签（`masterpiece` 无效）

### Pony Diffusion V6

```
score_9, score_8_up, score_7_up, source_anime, rating_safe, 1girl, solo, [Danbooru标签...]
```

负向：
```
score_4, score_3, low quality, bad anatomy, watermark, signature
```

提示：
- `score_9` 系列标签是 Pony 的**强制项**——去掉后画面质量断崖式下跌
- `source_anime` / `rating_safe` 也是强信号

---

## 负面提示词分层体系

负面提示词不应该是一个万能模板。分层构建：

### 第一层：通用基础（所有 tag 式模型必加）

```
lowres, bad anatomy, bad hands, extra fingers, missing fingers, watermark, text, signature, jpeg artifacts
```

### 第二层：质量护城河（推荐加）

```
worst quality, low quality, normal quality, blurry, deformed, disfigured, ugly, mutated hands, poorly drawn hands, poorly drawn face
```

### 第三层：风格约束（按需加）

```
monochrome, sepia, sketch, 3d, realistic, photo, nsfw
```

### 第四层：激进控制（仅特定场景）

```
extra limbs, fused fingers, too many fingers, long neck, elongated body, bad proportions, gross proportions, missing arms, missing legs, extra arms, extra legs
```

### 各模型负向模板速查

| 模型 | 负向推荐 | 注意事项 |
|------|---------|---------|
| Illustrious/NoobAI | 基础层 + 质量层 | 不要过度——Illustrious 对负向敏感 |
| Anima | 基础层（精简版） | `nsfw` 建议加 |
| FLUX | 不写 | FLUX 的 CFG 机制不同，负向干扰 |
| Pony | `score_4, score_3` + 基础层 | Pony 评分负向很重要 |

---

## 提示词结构通用模板

将提示词组织为结构化的信息流，而非随机堆砌：

```
[质量激活] masterpiece, best quality, highres

[主体定义] 1girl, solo, [角色特征: 发色/瞳色/体型/种族]

[外观细节] [服装] + [配饰] + [发型]

[姿势/表情] [动作] + [面向/视线] + [情绪/微表情]

[构图/光影] [景别: medium shot/full body/close-up] + [光源方向/颜色] + [构图规则]

[环境/氛围] [场景地点] + [时间/天气] + [氛围关键词]

[风格绑定] [画师标签] + [渲染风格] + [色彩倾向]

[负向提示词] [按需分层]
```

### 实际应用示例（Anima 混合式）

```
@ask, @mika picaso, masterpiece, highres, year 2025, safe, 1girl, solo, gradient pink to purple long hair, pointed elf ears, layered purple and white gown with silver embroidery, sitting elegantly against an arched stone window, soft pink-purple moonlight creating rim light on hair, deep blue star-filled night sky outside, calm serene twilight atmosphere, medium shot with rule-of-thirds composition, cel shading anime style, purple-white-pink palette with silver accents
```

这个 prompt 的结构：`@画师 + 质量 + 年代 + 安全标签 → 主体 → 外观 → 姿势/场景 → 光影(自然语言长句) → 构图 → 风格 → 色彩`

---

## 常见误区与反模式

### 误区 1：质量标签越多越好

**错。** 质量标签边际效益递减严重。2-3 个足够。10 个质量标签不会让画面"更好 10 倍"——它们在 token 层面互相稀释。

### 误区 2：画师标签不能混用

**看模型。** Anima 系的多 `@artist` 是**风格融合**，不是冲突。SDXL 系的混用也是常见的风格 blending 手段。只有风格反差极大的画师（如新海诚 + 武内崇）混用时才会互相打架，风格相近的画师混用往往产生更好的结果。

### 误区 3：自然语言描述是"冗余"

**看模型。** 在 FLUX/Anima/SD3 上，自然语言描述**不是冗余**——它是模型理解画面意图的核心通道。"把自然语言改成 tag"的建议只适用于 Illustrious/NoobAI 等纯 Danbooru 体系模型。对一个 Anima prompt 说"你的描述太啰嗦了应该精简成 tag"是一个**专业知识错误**。

### 误区 4：中英混写一定不好

**分情况。** 
- 在 Danbooru 体系模型（Illustrious/NoobAI）中，中文 token 模型无法理解，浪费 token——但量不大时实际影响很小
- 在 Anima（Qwen3 编码器）中，中文描述能部分理解，影响不大
- **不要在没有看到中文的情况下说用户写了中文**——这是严重的幻觉。先看清楚用户的 prompt 再评价

### 误区 5：所有平台默认支持权重语法

**错。** ComfyUI 原生 CLIPTextEncode 不支持 `(word:1.5)` 语法。如果不问用户用的是什么工具就直接推荐权重括号，建议可能无效。

### 误区 6：评价 prompt 前的必要步骤

在评价任何 prompt 之前，先确认：
1. 对方用什么模型？（决定标签体系）
2. 对方用什么工具/平台？（决定语法支持）
3. 对方的期望风格是什么？（决定评判标准）

**没有这些信息就开始评价，就是在蒙。** 好的评价是"你这个 prompt 如果是给 Illustrious 跑的话...如果是 Anima 的话就..."，而不是用一套标准硬套所有场景。
