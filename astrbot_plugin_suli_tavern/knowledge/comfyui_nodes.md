# ComfyUI 核心节点与调参指南

> 覆盖 ComfyUI 必备节点、参数作用、调参建议、显存管理、ControlNet 图生图/重绘、超分辨率完整流程与工作流优化。
> 最后更新: 2026-06

---

## 基础概念：ComfyUI 节点系统

ComfyUI 是基于节点的图像生成工具，每个节点负责一个独立功能，通过连线传递数据。
核心数据流：`LoadCheckpoint → CLIPTextEncode → KSampler → VAEDecode → SaveImage`

### 节点连线颜色含义
- **蓝色线**: 模型/权重对象 (MODEL)
- **红色线**: CLIP/文本编码 (CONDITIONING)
- **黄色线**: latent 潜空间张量 (LATENT)
- **绿色线**: VAE 输入/输出 (VAE)
- **紫色线**: 图像张量 (IMAGE)
- **橙色线**: 遮罩 (MASK)

### 必备节点套装 (2025-2026)

**ComfyUI Manager**: 第一个要装的节点。一键安装/更新/卸载其他所有节点。内置工作流导入导出。

**ComfyUI-Easy-Use** (yolain): 全能效率包。整合了 SD1.x/SDXL/Flux/SD3 的全能 Loader、预采样配置节点、通配符和 LoRA 支持、XY Plot 参数网格测试、背景移除（RMBG-1.4）、GPU 显存强制清理。

**Efficiency Nodes**: 减少节点数量的效率套件。Efficient Loader 一个节点加载 checkpoint + VAE + LoRA；KSampler (Efficient) 含实时预览和种子管理；支持 HiRes-Fix、Noise Control、Tiled Upscaler 等脚本节点。

---

## GPU 显存管理与型号速查

> 显存 (VRAM) 是 ComfyUI 的硬上限——模型放不进显存就跑不了，跟 GPU 算力无关。12GB 是 2025-2026 的实用入门线，16GB 是甜区，8GB 已过时。

### NVIDIA 消费级 GPU VRAM 速查表

| 系列 | 型号 | VRAM | 位宽 | CUDA 架构 | 定位 |
|------|------|------|------|-----------|------|
| RTX 30 | 3060 | 12 GB | 192-bit | Ampere 8.6 | 入门甜区 |
| RTX 30 | 3090 | 24 GB | 384-bit | Ampere 8.6 | 二手性价比王 |
| RTX 40 | 4060 | 8 GB | **128-bit** | Ada 8.9 | ❌ 不推荐 |
| RTX 40 | 4060 Ti 16GB | 16 GB | **128-bit** | Ada 8.9 | VRAM 大但慢 |
| RTX 40 | 4070 | 12 GB | 192-bit | Ada 8.9 | 入门可行 |
| RTX 40 | 4070 Ti Super | 16 GB | **256-bit** | Ada 8.9 | ⭐ 最佳性价比 |
| RTX 40 | 4080 Super | 16 GB | 256-bit | Ada 8.9 | 算力更强 |
| RTX 40 | 4090 | 24 GB | 384-bit | Ada 8.9 | 全场景无妥协 |
| RTX 50 | 5060 Ti | 16 GB | 128-bit | Blackwell 12.0 | 入门预算首选 |
| RTX 50 | 5070 Ti | 16 GB | 256-bit | Blackwell 12.0 | 高端甜区 |
| RTX 50 | 5080 | 16 GB | 256-bit | Blackwell 12.0 | 发烧友 |
| RTX 50 | 5090 | **32 GB** | 512-bit | Blackwell 12.0 | 旗舰无限制 |

### 内存位宽为什么重要

位宽决定 VRAM 带宽 = 位宽 × 显存频率。带宽直接决定生图速度：
- 128-bit (4060/4060Ti): ~288 GB/s — 同样16GB，比256-bit卡慢 50%+
- 256-bit (4070TiS/4080/5070Ti): ~672 GB/s — 甜区
- 384-bit (3090/4090): ~1008 GB/s — 高性能
- 512-bit (5090): ~1792 GB/s — 旗舰

**关键结论**: RTX 4060 Ti 16GB 有足够 VRAM 容量但位宽窄，跑 FLUX 比 4070 Ti Super 慢 2-3 倍。预算允许则避开 128-bit 卡。

### RTX 50 系列新增：NVFP4 硬件加速

Blackwell 架构引入原生 FP4 Tensor Core，对 ComfyUI 是质的飞跃：
- NVFP4 将 FLUX Dev 显存从 **26 GB (BF16) → 14 GB**，降幅 46%
- 速度比 FP8 快 ~84%（5090 实测: 7.73 vs 4.21 it/s）
- RTX 5060 Ti 16GB 跑 NVFP4 FLUX 成为可能
- **⚠️ RTX 40 系列没有 FP4 硬件**，强开 NVFP4 反而比 FP8 慢 2×——40 系请用 FP8 Scaled

### 各模型族 VRAM 需求估算（含 ControlNet/LoRA 叠加）

| 场景 | 最低 VRAM | 推荐 VRAM | 舒适 VRAM |
|------|-----------|-----------|-----------|
| SD 1.5 (512×512) | 4 GB | 6 GB | 8 GB |
| SD 1.5 + ControlNet | 6 GB | 8 GB | 12 GB |
| SDXL (1024×1024) | 8 GB | 12 GB | 16 GB |
| SDXL + 1 ControlNet | 10 GB | 14 GB | 16 GB |
| SDXL + LoRA + ControlNet | 12 GB | 16 GB | 20 GB |
| Anima (1024×1024) | 6 GB | 10 GB | 12 GB |
| FLUX.1 schnell (FP8) | 10 GB | 12 GB | 16 GB |
| FLUX.1 dev (FP8) | 12 GB | 16 GB | 20 GB |
| FLUX.1 dev (FP16/BF16) | 24 GB | 24 GB | 32 GB |
| FLUX + LoRA | 14 GB | 18 GB | 24 GB |
| Ultimate SD Upscale (2x→4k) | +2-4 GB 额外 | 16 GB | 24 GB |
| 视频 (SVD) | 16 GB | 24 GB | 32 GB |

### OOM 风险预判：帮用户判断他的卡能跑什么

**核心预判逻辑**:
1. 查用户 GPU 的 VRAM 容量（上表）
2. 查用户要跑的模型/工作流（上表）
3. 留 1-2 GB 系统余量（浏览器/桌面占 VRAM）
4. 如果需求 VRAM > 实际 VRAM - 1GB → 高风险 OOM

**常见风险判断**:
- RTX 4060 8GB 用户问 FLUX → ❌ 跑不了。建议换 SDXL 或升级显卡
- RTX 4070 12GB 用户问 FLUX dev → ⚠️ 可以跑但需 FP8 量化 + Tiled VAE。生成慢（~30s+/张）
- RTX 3060 12GB 用户问 SDXL + ControlNet → ⚠️ 勉强够，注意关其他 GPU 程序
- RTX 4060 Ti 16GB 用户问 FLUX dev → ✅ 可以跑，显存够，但速度因 128-bit 位宽较慢
- RTX 4090 24GB → ✅ 通吃一切
- RTX 5090 32GB → ✅ 旗舰，训练也够

### 分级显存优化策略

**第一级（少损失，先做）**:
- 开 Tiled VAE: tile_size 256-320，解码阶段省 2-4 GB
- FP8 量化模型（40 系）/ NVFP4（50 系）：省 30-46% 显存，画质损失极小
- xFormers / Flash Attention: 省 ~20% 计算显存，ComfyUI 内置
- Batch size = 1: 不批量生成

**第二级（有些代价）**:
- GGUF Q8 量化: 省 30% 显存，画质几乎无损
- `--lowvram` 启动参数: 空闲模型卸载到系统内存，慢但防 OOM
- 不要同时加载多个 LoRA

**第三级（最后手段）**:
- GGUF Q4 量化: 省 60% 显存，画质有明显损失
- 降低生成分辨率（SDXL 1024→768）
- 减少 ControlNet 叠加数量

### 系统级建议

- **系统内存至少 32 GB**：显存不够时 ComfyUI 会 offload 到 RAM，内存不够直接 OOM
- **更新显卡驱动**: 545+ 驱动支持 CUDA 12.x，老驱动（<535）某些优化算子不工作
- **nvidia-smi 监控**: 终端运行 `watch -n 1 nvidia-smi` 实时看显存占用
- **关闭浏览器硬件加速**: Chrome/Edge 的 GPU 进程可能占用 1-2 GB VRAM
- **NVMe SSD**: 模型加载到 VRAM 更快，频繁切换模型时体验好

---

## KSampler：采样器核心参数

KSampler 是 ComfyUI 最重要的节点，控制降噪过程。

### 采样器 (Sampler) 选择

| 采样器 | 特点 | 适用场景 |
|--------|------|---------|
| DPM++ 2M Karras | 速度快，质量高，最通用 | SD1.5/SDXL 日常出图 |
| DPM++ 2M SDE Karras | 比 2M 多点随机性，细节更丰富 | 追求细节表现力时 |
| Euler a | 经典，有随机性，每步变化大 | 快速预览/探索构图 |
| Euler | 确定性，收敛稳定 | 需要稳定输出时 |
| DDIM | 确定性，步数少也能出图 | 快速测试 |
| UniPC | 收敛极快，10步可用 | 追求速度最大化 |
| LCM | 专为 LCM 模型设计，4-8步 | LCM/Turbo 模型 |

### 步数 (Steps)

- **SD1.5**: 18-24 步（DPM++ 2M Karras），低于 15 步细节不足，高于 30 步收益递减
- **SDXL**: 22-28 步，比 SD1.5 需要稍多步数
- **Flux**: 20-30 步，对步数不太敏感
- **LCM/Turbo 模型**: 4-8 步即可，更多步反而破坏效果
- **一般原则**: 步数过少→细节模糊；步数过多→过拟合/颜色偏移，且边际收益递减

### CFG (Classifier-Free Guidance)

CFG 控制 prompt 对生成结果的影响力。数值越高，模型越"听话"，但也越容易过饱和/失真。

- **SD1.5**: 5-7（推荐 6）
- **SDXL**: 4.5-6.5（推荐 5）
- **Illustrious/NoobAI**: 3.5-7（推荐 5）
- **Flux**: 1-4（Flux 对 CFG 敏感，低CFG就能出好图）
- **Pony 系列**: 5-8（推荐 7）

### CFG 调参技巧

**动态 CFG (Anima Dynamic CFG)**:
- `cfg_start`: 5.5–7.0 (初始阶段高CFG建立结构)
- `cfg_end`: 3.0–4.0 (末尾降低CFG增加自然感)
- `schedule`: cosine (最平滑的过渡曲线)
- `rescale`: 0.5–0.7 (防止过饱和)
- 原理：采样初期需要强引导建立构图，后期降低引导让细节更自然

**预CFG节点 (Pre-CFG Nodes)**:
- Automatic CFG: 在生成过程中智能调整CFG
- 支持空uncond: 跳过某些timestep的负面预测，加速~50%
- 可与ConditioningSetTimestepRange配合使用

---

## VAE：变分自编码器

VAE 负责 latent 空间 ↔ 像素空间的编解码。

### 主流VAE选择

- **SD1.5**: 用原版 VAE（kl-f8-anime2 适合二次元）
- **SDXL**: 用 SDXL 专属 VAE（sdxl_vae）
- **动漫模型**: kl-f8-anime2（色彩更鲜艳）
- **ClearVAE**: 减少噪点，画面更干净
- **FLUX**: FLUX 自带 VAE，一般不需要换

### VAE 常见问题

- **VAE 报错 "cuDNN error"**: 通常是显存不足或 VAE 与模型不匹配
- **画面发灰/色彩暗淡**: VAE 没加载正确，或用错了 VAE
- **Tiled VAE**: 大图解码时必开，分块解码节省显存，256/320 tile size 是比较好的平衡点

---

## CLIP Text Encode：提示词编码

### CLIP 参数

- **CLIP Skip (CLIP Set Last Layer)**: 跳过 CLIP 最后 N 层。SD1.5 常用 -2（即跳过最后2层），SDXL 通常不需要。
  - 跳层越多 → 生成结果越偏离 prompt → 风格化更强，但控制力下降
  - SD1.5 推荐: -1 或 -2
  - 动漫模型（Illustrious/NoobAI）: 通常不需要 CLIP Skip

### CLIP 分块机制 (SDXL)

- SDXL 的 CLIP 每 75 token 一个 chunk
- 超长 prompt 会被分成多个 chunk，每个 chunk 独立编码后合并
- 用 BREAK 关键字（WebUI）或 conditioning concat（ComfyUI）手动分离不同语义块
- 实践：artist 标签放单独 chunk 效果更好

---

## LoRA / LyCORIS：低秩适配器

### LoRA 权重解释

```
LoRA 权重格式: (model_strength, clip_strength)
- model_strength: 控制对图像视觉效果的影响
- clip_strength: 控制对 prompt 理解的调整
```

### 调参指南

| 场景 | model_strength | clip_strength | 说明 |
|------|---------------|---------------|------|
| 人物 LoRA | 0.7-0.9 | 0.5-0.7 | 人物特征需要较强引导 |
| 风格 LoRA | 0.4-0.7 | 0.3-0.5 | 风格迁移宜轻不宜重 |
| 服装/物品 LoRA | 0.6-0.8 | 0.5-0.6 | 保持灵活性的同时确保特征 |
| 概念 LoRA | 0.5-0.8 | 0.7-0.9 | 概念依赖 CLIP 理解 |

### LoRA 叠加

- 多 LoRA 叠加时建议总权重 ≤ 2.0（model_strength 总和）
- 相互冲突的 LoRA（如两个不同人物）会互相污染
- 使用 LoRA Block Weight 插件可以指定 LoRA 作用于特定层
- LyCORIS（LoHa/LoKr/DyLoRA）比普通 LoRA 参数更多，效果通常更好，但文件更大

### 常见 LoRA 问题

- **LoRA 过强导致画面崩坏**: 降低 model_strength 到 0.5 以下
- **多个 LoRA 互相污染**: 用 LoRA Block Weight 限制作用范围
- **触发词不生效**: 查 LoRA 说明页的训练触发词，必须精确匹配

---

## ControlNet 深度使用：图生图与重绘全指南

> ControlNet 是精确控制图像生成的核心工具。不仅能做线稿上色/姿态控制，更是图生图重绘 (Inpaint) 和风格迁移的主力。

### 核心 ControlNet 类型与参数

| ControlNet | 作用 | 最佳参数 |
|-----------|------|---------|
| Canny | 边缘检测 → 控制构图 | strength 0.5-0.8, low_threshold 100, high_threshold 200 |
| Depth | 深度图 → 控制空间关系 | strength 0.6-0.9 |
| OpenPose | 骨骼姿态 → 控制人物动作 | strength 0.7-1.0 |
| Scribble | 涂鸦 → 自由构图引导 | strength 0.6-0.9 |
| Tile | 分块重绘/高清修复（不是超分模型！） | strength 0.8-0.9, 预处理器设 None |
| Lineart | 线稿控制 | strength 0.5-0.8 |
| IP-Adapter | 图像提示 → 风格/构图参考 | weight 0.5-0.9 |
| Reference | 参考图引导 | style_fidelity 0.5-0.8 |
| Inpaint | 局部重绘专用，保持未遮罩区域不变 | strength 0.8-1.2 |

### 图生图 (Img2Img) 完整工作流

图生图的核心是在已有图像的基础上用降噪重新生成，降噪强度 (denoise) 是关键参数。

**标准图生图节点链**:
```
Load Image → VAE Encode → KSampler (denoise: 0.4-0.75)
                         ↑
                      Prompt (描述目标改动)
```

**降噪强度选择**（图生图核心决策）:
| denoise | 效果 | 适用场景 |
|---------|------|---------|
| 0.1-0.25 | 极轻微修改，基本保留原图 | 调色、去噪、微调细节 |
| 0.25-0.4 | 保留构图和结构，小范围修改 | 换发色、服装微调、背景微调 |
| 0.4-0.6 | 保留大致构图，细节重绘 | 风格迁移、换衣服、表情修改 |
| 0.6-0.75 | 构图参考原图，大量新细节 | 重绘渲染、大幅度风格化 |
| 0.75-0.9 | 仅保留模糊构图印象 | 接近从零生成 |
| 1.0 | 完全忽略原图 | = 文生图 |

**图生图叠加 ControlNet 的高级用法**:
- `Load Image → Canny → ControlNet Apply (strength 0.5-0.7)` + KSampler denoise 0.5-0.7 → 锁定构图同时允许风格变化
- `Load Image → Depth → ControlNet Apply (strength 0.6-0.8)` → 保持空间关系，适合场景图生图
- `Load Image → Lineart → ControlNet Apply (strength 0.5-0.7)` → 保持线稿结构，适合线稿上色

### 重绘 (Inpaint) 专业方法

Inpaint 是图生图的特化形式：只重绘遮罩区域，其余完全不变。

**核心节点**: `VAE Encode (for Inpainting)` — **必须用这个，不能用普通 VAE Encode！**
- 普通 VAE Encode → 非遮罩区域会漂移变色
- VAE Encode for Inpainting → 非遮罩区域像素级保持原样

**标准 Inpaint 工作流**:
```
Load Image ─┬─→ VAE Encode (for Inpainting) ──→ KSampler
             │       ↑
             ├─→ (mask 输入: 白色=重绘区, 黑色=保留区)
             │
             └─→ Inpaint Preprocessor → ControlNet Loader (inpaint model) → ControlNet Apply
                                                                                ↓
Prompt (描述重绘目标) ──→ CLIP Text Encode ──────────────────────────→ KSampler

KSampler 设置:
  - denoise: 0.45-0.6（局部重绘推荐范围；0.45 微修，0.6 大幅度改动）
  - sampler: DPM++ 2M Karras 或 Euler ancestral
  - steps: 20-30
```

**Inpaint 遮罩技巧**:
| 问题 | 解决方案 |
|------|---------|
| 重绘边缘有接缝 | 遮罩外扩 4-8 px，用 `Grow Mask` 或模糊遮罩边缘 |
| 背景被污染 | 降低 denoise 到 0.4，或在 prompt 中描述保留的背景 |
| 重绘太生硬 | 遮罩边缘 feather 2-6 px，降低 ControlNet Inpaint strength 到 0.8 |
| 需要精确遮罩 | 用 Segment Anything (SAM) 节点自动抠图 |

**Inpaint ControlNet 双重控制**（最稳定方案）:
```
Inpaint ControlNet (strength 0.8-1.0) + Canny/Depth ControlNet (strength 0.4-0.6)
→ Inpaint 锁定未遮罩区域 + Canny/Depth 锁定全局结构
→ 遮罩内外都不飘
```

### ControlNet 叠加策略

**叠加原则**:
- 总 ControlNet strength 建议 ≤ 2.5（各 CN strength 之和）
- 结构类 CN (Canny/Depth) + 风格类 CN (IP-Adapter) 互补
- Tile 只用于放大和细节增强，不要跟其他结构 CN 同时开

**常用叠加组合**:
| 场景 | 组合 | 参数 |
|------|------|------|
| 人物动作 + 风格参考 | OpenPose (0.8) + IP-Adapter (0.5) | KSampler denoise 0.7 |
| 线稿上色 + 固定构图 | Lineart (0.7) + Canny (0.4) | KSampler denoise 0.6 |
| 局部重绘 + 全局结构锁 | Inpaint (1.0) + Depth (0.5) | KSampler denoise 0.5 |
| 高清放大 + 细节增强 | Tile (0.8) + Ultimate SD Upscale | denoise 0.25-0.35 |

### ControlNet Advanced 高级技巧

- **Timestep Keyframes**: 控制 ControlNet 在采样过程中的强度变化（如开头用Canny建立结构，后期降低Canny让模型自由发挥）
- **Soft Weights**: base_multiplier = "我的 prompt 更重要"，uncond_multiplier = "ControlNet 更重要"
- **Sliding Context**: AnimateDiff 兼容的滑动上下文窗口

### IP-Adapter 权重类型 (13种预设)

- **style transfer**: 风格迁移（推荐权重 0.6-0.8）
- **composition**: 构图参考
- **strong style transfer**: 强风格迁移
- **style and composition**: 同时控制风格+构图
- **FACEID / FULL FACE**: 人脸一致性（配合 InstantID 更好）

---

## 超分辨率完整指南：插值/超分/分块放大

> 超分 (Super Resolution) 不只是"点一下放大"——插值算法选择、AI超分模型选择、分阶段放大策略、潜空间 vs 像素空间路线，每一步都影响最终画质。

### 插值算法对比

插值 (Interpolation) 是最基础的放大方式——数学计算新像素，不靠 AI。ComfyUI 的 `Upscale Latent` 和 `Image Scale` 节点提供多种插值算法。

| 算法 | 原理 | 速度 | 效果 | 适用 |
|------|------|------|------|------|
| nearest-exact | 最近邻，直接复制最近像素 | ⚡最快 | 锯齿/马赛克，无细节生成 | 像素艺术、需要保持硬边的场景 |
| bilinear | 双线性，4邻域加权平均 | ⚡快 | 平滑但模糊，丢失高频细节 | 快速预览、中间步骤 |
| bicubic | 双三次，16邻域样条插值 | ⚡中等 | 比 bilinear 清晰，保持边缘更好 | 通用插值首选 |
| Lanczos | sinc 函数窗，6-lobe 采样 | 🐢较慢 | 锐度最高，但可能产生振铃伪影 | 摄影/写实、追求锐度 |
| area | 区域平均 | ⚡中等 | 缩小图像时最平滑 | 缩小而非放大 |

**选择建议**:
- 潜空间放大 → 用 `bicubic`（最平衡）或 `nearest-exact`（Anima 推荐）
- 像素空间 → 不要用插值，直接用 AI 超分模型
- Lanczos 锐度好但二次元线条可能出白边——动漫图放大优先用 AI 模型

### 潜空间放大 vs 像素空间放大

这是超分的两条完全不同的技术路线。

| 维度 | 潜空间放大 (Latent) | 像素空间放大 (Pixel) |
|------|---------------------|---------------------|
| **数据域** | VAE 压缩域 (latent 8×8 压缩块) | RGB 像素图像 |
| **操作** | 插值放大 latent → 再 KSampler 降噪 | 先 VAE Decode → AI 超分模型放大 |
| **速度** | 较快（不需要解码/重编码） | 较慢（需 decode→upscale→re-encode） |
| **显存** | 较低（latent 占空间小） | 较高（全分辨率像素图加载） |
| **画质** | 细节依赖 KSampler 降噪补充 | AI 超分模型生成细节质量高 |
| **最大倍率** | 1.5-2x 安全（再大容易崩） | 4x-8x 安全（分块放大） |
| **适用** | HiRes Fix 内部放大 | 最终出图放大 |

**最佳实践：混合路线**:
```
1st pass: 文生图 1024×1024
2nd pass: Latent Upscale 1.5x → KSampler (denoise 0.35-0.5) → 1536×1536
3rd pass: VAE Decode → RealESRGAN 2x → 3072×3072 最终输出
```

### AI 超分模型选择

AI 超分模型通过神经网络生成新细节，远优于传统插值。

| 模型 | 速度 | 质量 | 显存 | 最佳用途 |
|------|------|------|------|---------|
| R-ESRGAN 4x+ | 中等 | ⭐⭐⭐⭐ | 中等 | 通用，最流行 |
| R-ESRGAN 4x+ Anime6B | 中等 | ⭐⭐⭐⭐⭐ | 中等 | **二次元动漫首选** — 线条清晰、色彩保真 |
| 4x-UltraSharp | 快 | ⭐⭐⭐ | 低 | 快速锐化，写实/通用 |
| 4x-AnimeSharp | 快 | ⭐⭐⭐ | 低 | 二次元快速放大 |
| 4x_NMKD-Superscale | 中等 | ⭐⭐⭐⭐ | 中高 | 写实/游戏纹理 |
| SwinIR_4x | 慢 | ⭐⭐⭐⭐⭐ | 高 | **高质量插画** — 自然平滑、少伪影 |
| DAT_4x | 慢 | ⭐⭐⭐⭐⭐ | 高 | Transformer 超分，细节还原好 |
| LDSR (Latent Diffusion SR) | 很慢 | ⭐⭐⭐⭐⭐ | 很高 | 扩散超分，质量最高但成本大 |
| 8x_NMKD-Superscale | 快 | ⭐⭐⭐ | 中高 | 8x 一步到位，画质一般 |

**动漫/二次元专用推荐**:
1. **预算低/追求速度**: `4x-AnimeSharp` — 速度快，线条清晰
2. **标准出图**: `R-ESRGAN 4x+ Anime6B` — 日系二次元最通用选择
3. **高质量插画**: `SwinIR_4x` — 自然平滑，不产生过锐伪影
4. **混合二次元+写实**: `4x-UltraSharp` + `R-ESRGAN 4x+ Anime6B` 两张对比选优

### Ultimate SD Upscale + Tile ControlNet 完整配置

这是最高质量的放大方案：先 AI 模型放大，再用扩散模型分块重绘增强细节。

**推荐工作流**:
```
VAE Decode → ImageScale (RealESRGAN 2x) → Ultimate SD Upscale
                                              ├── tile_width: 1024 (SDXL) / 512 (SD1.5)
                                              ├── tile_height: 1024 / 512
                                              ├── force_uniform_tiles: true (避免接缝)
                                              ├── tiling_strategy: "padded" (最佳接缝融合)
                                              ├── denoise: 0.25-0.35 (只加细节不改内容)
                                              ├── steps: 20
                                              ├── sampler: DPM++ 2M Karras
                                              └── ControlNet Tile (strength 0.8-0.9, 预处理器: None)
```

**各参数详尽说明**:

- **Tile Size (分块大小)**: SDXL 模型用 1024×1024（模型原生分辨率），SD1.5 用 512×512。每个块单独过扩散模型增强。块太小 → 接缝可见，块太大 → 显存超限。
- **denoise**: 最关键参数。0.2 = 几乎不改内容/只润色细节，0.35 = 明显增加细节但可能改变小元素。推荐从 0.25 开始试。
- **force_uniform_tiles**: 必须开 True，否则边缘块尺寸不一致会产生网格伪影。
- **tiling_strategy: padded**: 每个块四周有 padding 重叠区，融合时消除接缝。比 "simple" 效果好很多。
- **ControlNet Tile strength 0.8-0.9**: Tile 模型告诉 KSampler "保持这个区域的图像结构"，防止 denoise 把内容改飞。
- **预处理器设 None**: Tile ControlNet 不需要预处理器——它的输入就是待增强的图像块本身。

**常见问题排查**:
| 问题 | 原因 | 解决 |
|------|------|------|
| 可见网格/接缝 | tile 太小或 padding 不够 | 增大 tile size + 用 padded 策略 |
| 放大后模糊 | denoise 太低，细节没加进去 | 提升 denoise 到 0.3-0.4 |
| 内容变化太大 | denoise 太高，扩散把内容改了 | 降低 denoise 到 0.2-0.25 + 提高 Tile CN strength |
| OOM | 全分辨率像素图显存放不下 | 降低 tile size 到 768 或 512 |
| 颜色偏移 | Tile CN 未平衡 prompt 引导 | 设 Control Mode 为 "My prompt is more important" |

### 分阶段放大策略

不要一次 8x — 分步放大效果更好。

| 策略 | 步骤 | 适用 |
|------|------|------|
| 一步 2x | 1024 → Latent 2x → KSampler (0.4) | 简单快速 |
| 两步 1.5x | 1024 → Latent 1.5x → KSampler (0.4) → Latent 1.5x → KSampler (0.35) | 比一步2x画质好 |
| 混合 4x | 1024 → VAE Decode → RealESRGAN 2x → Ultimate SD Upscale + Tile (denoise 0.25) | 高质量最终输出 |
| 极限放大 | 1024 → Latent 1.5x (denoise 0.4) → VAE Decode → RealESRGAN 2x → Ultimate SD Upscale 2x (denoise 0.25) → 最终 6144×6144 | 海报/打印级 |

**倍率选择原则**:
- Latent Upscale 每次 ≤ 2x，推荐 1.5x
- AI 超分模型 ≤ 4x（模型原生倍率），2x 最安全
- 中间用 denoise 0.35-0.5 走 KSampler 补充细节
- 最终阶段用 Ultimate SD Upscale 做最后的润色

### 二次元动漫超分专属建议

- **不要用 Lanczos**: Lanczos 锐化在二次元线条上会产生白边/振铃
- **首选 Anime6B**: ESRGAN 4x+ Anime6B 专门为日系二次元训练，线条处理最好
- **二次元 denoise 偏低**: 动漫平涂区域多，denoise 高了容易破坏干净的色块，0.2-0.35 更安全
- **线条修复**: 如果原图线条模糊，用 `4x-AnimeSharp` 做第一级放大（锐化线条），再用 `Anime6B` 做第二级
- **VAE 搭配**: 二次元放大用 `kl-f8-anime2` VAE，色彩更鲜艳

---

## 常用辅助节点

### 图像后处理

- **FaceDetailer**: 自动检测人脸区域，局部重绘修复。detection_confidence 设 0.5，denoise 设 0.3-0.5
- **ImageCASharpening+**: 高通锐化。strength 0.5-0.8，过高产生噪点
- **ImageDesaturate+**: 去色/降饱和度
- **ImagePosterize+**: 色调分离效果。strength 0.3-0.5 做轻微风格化

### 标签/提示词

- **Danbooru Tags Upsampler**: 把简短 prompt 自动扩展为详细 danbooru 标签列表。用 DART v2 模型，推荐 total_tag_length: long
- **WD14 Tagger**: 从图像反推 danbooru 标签，用于图生图参考

### 流程控制

- **Fast Groups Bypasser (rgthree)**: 一键开关整个节点组，方便切换工作流分支
- **Image Comparer (rgthree)**: A/B 滑动对比两张图
- **XY Plot (Efficiency)**: 参数网格测试，自动生成不同参数组合的对比图

---

## 性能优化

### 显存管理

- **FP8 模型**: 使用 FLUX_FP8 等量化模型，大幅降低显存占用，画质损失极小
- **GGUF 量化模型**: Q8_0 推荐，显存减半，画质几乎无损
- **Tiled VAE**: 大图解码时必开，tile_size 256-320，可节省 2-4GB 显存
- **--disable-cuda-malloc --gpu-only**: FP8 模型建议加这两个启动参数
- **空 uncond 技巧**: 配合 ConditioningSetTimestepRange，约省 50% 负面预测计算量

### 速度优化

- **Pruna Optimization Nodes**: 模型编译 + 智能缓存，Flux 上 2.9x-5.6x 加速
- **TeaCache**: 跨步缓存中间计算结果
- **LCM/Turbo LoRA**: 4-8 步出图，牺牲一定画质换速度
- **Batch Size > 1**: 同时生成多张图，充分利用 GPU 并行能力
- **Torch Compile**: PyTorch 2.0+ 的 torch.compile，首次编译慢但后续快 10-30%

---

## 出图异常诊断与修复

> 出图异常是最常见的问题。按现象分类，每类给出从最常见到最罕见的排查顺序。
> 诊断铁律：先查 VAE → 再查 CFG → 再查分辨率/步数 → 最后查硬件/驱动。

### 画面发灰/色彩暗淡/发白

这是最常被问的问题——画面像蒙了一层灰雾，色彩不鲜艳。

**诊断与修复**（按可能性从高到低）:

1. **VAE 没加载**: 90% 是这个原因。检查 `Load VAE` 节点是否连到了 `VAE Decode`。没有 VAE → latent 直接解码 → 发灰。
2. **VAE 不匹配**: SD1.5 模型用了 SDXL VAE，反之亦然。查模型说明页推荐哪个 VAE。
3. **CFG 过低**: SD1.5 CFG < 3 / SDXL CFG < 2 时模型引导太弱，画面失去对比度。提高到 5-7。
4. **二次元模型专用 VAE**: 动漫模型推荐 `kl-f8-anime2`（色彩更鲜艳）或 `ClearVAE`（更深色/更干净）。

### 纯黑/纯灰/噪点满屏（雪花片）

出图不是图像而是噪声或全黑——diffusion 过程根本没收敛。

**诊断与修复**:

1. **VAE 错误/损坏**: 检查 VAE 文件是否完整。SDXL 在 FP16 推理时必用 `sdxl-vae-fp16-fix`（`madebyollin/sdxl-vae-fp16-fix`），否则 NaN → 黑图。
2. **模型文件路径放错**: Checkpoint 必须放 `models/checkpoints/`，不能放 `models/unet/`（unet 目录是纯扩散模型，不含 VAE/CLIP）。
3. **CFG 极低或为 0**: CFG=0 等于不引导 → 纯噪声。至少设 3 以上。
4. **macOS MPS 后端 bug**: macOS 27 beta + MPS 推理 SDXL → 纯噪声。换成 CPU 或降级 macOS。
5. **Seed = -1 且模型炸了**: 极小概率，换个 seed 排除。

### 色块/彩纹/彩色马赛克

图像中有不自然的彩色方块、条纹、数码感伪影。

**诊断与修复**:

1. **VAE 不对**: 加载了错误系列的 VAE（SD1.5 VAE 用于 SDXL 模型等）。SDXL 和 SD1.5 的 latent 通道数不同。
2. **潜空间操作不当**: SDXL 的 VAE 对 latent 变换（缩放/裁剪/normalize）比 SD1.5 敏感数十倍。**避免在 latent 空间做 scale/crop**——先 VAE Decode 到像素空间再操作。
3. **多次 VAE 编解码累积误差**: 每次 Encode→Decode 都会引入浮点误差。多次 inpaint→save→load→inpaint 循环后，画面逐渐出现彩色条纹。解决：减少编解码循环，或用 `VAE Decode (Tiled)` 降低单次误差。
4. **FP8/GGUF 量化过度**: Q4 级别量化会产生色块伪影。换 Q8 或 FP8。
5. **Multiple CUDA 版本冲突**: 系统装了两个 CUDA Toolkit → 卸载多余版本，只留一个。

### 过曝（高光死白、大面积白色）

画面某些区域亮度极高，像过度曝光。

**诊断与修复**:

1. **CFG 过高（最常见）**: SDXL CFG > 8 → 过饱和/过曝。降到 5-7。FLUX CFG > 5 → 过曝，降到 1-4。
2. **CFG Rescale 没开**: ComfyUI 在 KSampler 中设 `cfg_rescale` 为 0.7-0.8，可自动抑制过曝。
3. **Dynamic Thresholding**: 安装 `sd-dynamic-thresholding` 节点，将 latent 的极端值钳制在合理范围。适合高 CFG 场景。
4. **LoRA 强度过高**: 多个 LoRA 叠加（总 model_strength > 1.5）可能导致过曝。降低 LoRA 权重。
5. **FP16 VAE 不兼容**: SDXL FP16 推理时若没用 fp16-fix VAE，会产生 NaN 过曝。换 fp16-fix VAE。

### 模糊/油腻感/细节丢失

画面像涂了层凡士林，边缘不锐利。

**诊断与修复**:

1. **步数不够（最常见）**: SDXL < 15 步 → 细节不足。提高至 22-28。
2. **采样器选择不当**: 避免 `LMS` 和 `Heun`（过度平滑）。用 `DPM++ 2M Karras` 或 `DPM++ 2M SDE Karras`。
3. **CFG 过低**: CFG < 3 → 模型引导弱，画面 soft。调到 5-7。
4. **FP8/INT8 量化画质损失**: 尤其是 SD3.5 的 INT8 量化会模糊高频细节 → 换 FP8(E5M2) 格式。
5. **CLIP Skip 过高**: CLIP Skip -3 或更高 → prompt 控制力急剧下降。回到 -1 或 -2。
6. **Detail Daemon 节点**: 安装 `ComfyUI-Detail-Daemon`，设 `detail_amount: 0.1-0.25`，在采样过程中注入细节噪声。

### 人体异常：多肢体、融脸、缺眼睛

这是扩散模型的经典问题——小分辨率下人体结构崩坏。

**诊断与修复**:

1. **分辨率太低（根本原因）**: 人物全身图至少需要 1024×1024 (SDXL) 或 768×768 (SD1.5)。512×512 全身图必崩。
2. **负面提示词缺少人体标签**: 加 `bad anatomy, bad hands, extra limbs, missing fingers, fused fingers, poorly drawn face, deformed`。
3. **未用高清修复**: 低分辨率生成全身图 → HiRes Fix（1.5x-2x, denoise 0.35-0.5）修复面部/手部。
4. **FaceDetailer / HandDetailer**: 生成后自动检测人脸/手部区域做局部重绘修复。denoise 0.3-0.45。
5. **ADetailer 替代方案**: 在 ComfyUI 中用 `FaceDetailer` 节点（Impact Pack），设 bbox_detection_confidence 0.5。

### 双人/多人互相污染

描述两个角色时，角色 A 的特征（发色/服装/体型）跑到角色 B 身上。

**诊断与修复**:

1. **Prompt 未分词分组**: SDXL 的 CLIP 每 75 token 一个 chunk——两个角色的描述可能被分到同一个 chunk 互相污染。用 `ConditioningConcat` 或 `ConditioningAverage` 单独编码每个角色的 prompt。
2. **区域提示词 (Regional Prompting)**: 用 Attention Mask / Regional Prompter 节点为不同空间区域注入不同的 prompt。
3. **降低总 prompt 长度**: 超过 150 token 后 token 注意力稀释严重。精简每个角色的描述词。
4. **施加角色分隔**: negative prompt 中加 `merged, fusion, mixed hair color, shared clothing`。

### 背景乱码/前景正常

人物画得不错但背景莫名其妙——彩色斑点、建筑碎片、扭曲纹理。

**诊断与修复**:

1. **负面提示词缺少背景标签**: 加 `simple background, messy background, distorted background, blurry background` 抑制劣质背景。
2. **分辨率/宽高比不匹配**: SDXL 非 64 倍数的分辨率会导致 latent 对齐异常。用 1024×1024 / 1024×768 / 896×1152 等标准尺寸。
3. **CFG 不够高**: 低 CFG 时背景脱离 prompt 控制。提高到 5-6。
4. **描述背景**: positive prompt 中明确描述想要的背景而非留空——留空等于让模型随机发挥。

### 颜色偏移：绿紫偏色/棕色调

画面整体偏某种颜色（绿色、品红色、棕色），像加了一层滤镜。

**诊断与修复**:

1. **VAE 错误（经典症状）**: 绿色/品红色偏色 = VAE 与模型不兼容的铁证。换 `vae-ft-mse-840000`（SD1.5）或 `sdxl-vae-fp16-fix`。
2. **SDXL 动漫模型专用 VAE**: 用 `SDXL_anime_natural_vae`（防绿偏）或 `SDXL_anime_clear_vae`（更深红）。
3. **FLUX inpaint 棕色调 bug**: FLUX 连续 inpaint 后有已知的渐进式棕色调 bug（2025 Forge 版已知问题）。回退到早期版本。
4. **颜色配置文件问题**: 输入图像非 sRGB（如 Adobe RGB / CMYK）→ VAE 编码错误。ComfyUI 加载图像前确保是 sRGB。
5. **ZTSNR 噪声调度问题**: 部分动漫模型（NovelAI V3）训练用了 Zero Terminal SNR，需搭配支持 ZTSNR 的调度器。

### Inpaint 重绘异常：边缘生硬/背景漂移/贴图感

**诊断与修复**:

1. **没用 VAE Encode (for Inpainting)**: 这是最核心的——普通 VAE Encode 在 inpaint 中必然导致非遮罩区漂移。**必须用专用节点。**
2. **遮罩边缘生硬/接缝**: 遮罩 feather 2-6 px，或遮罩外扩 4-8 px 给过渡区。
3. **背景漂移**: 降低 denoise 到 0.4-0.5 + 在 prompt 中描述保留的背景 + 叠加 Depth ControlNet (strength 0.4-0.6)。
4. **重绘太生硬/贴图感**: 降低 Inpaint ControlNet strength 到 0.8 + denoise 降到 0.4 + 遮罩 feather 增大。

### 生成结果千篇一律/无变化

每次出图姿势构图几乎一样，换 seed 也没用。

**诊断与修复**:

1. **采样器选错**: Euler/DDIM 等确定性采样器在相同 seed 下输出完全一致。换 `DPM++ 2M SDE Karras` 或 `Euler a`（带随机性）。
2. **CFG 太低**: 低 CFG 时模型倾向输出训练数据的"平均"——画面单调。提高到 6-7。
3. **Prompt 太短/太泛**: "a girl" → 模型倾向输出最安全的答案。加具体描述词：pose、背景、光照、情绪。

---

## 软件/系统报错排查

### Out of Memory (OOM)

- **先判断用户显卡型号和 VRAM**，对照上方 GPU 速查表判断是否真的跑不动
- 降低分辨率或 batch size
- 开 Tiled VAE（最重要的一步）
- 换 GGUF/FP8 量化模型
- 加 `--lowvram` 启动参数（会变慢，但能跑）
- 检查是否有其他程序占用显存 (`nvidia-smi`)
- 浏览器关闭硬件加速（可能占 1-2 GB VRAM）

### 特定 GPU 型号 OOM 常见原因

- RTX 4060 8GB 跑 SDXL → 正常现象，8GB 就是不够。降分辨率到 768×768 或换 SD1.5
- RTX 3060 12GB 加多个 ControlNet → 每个 CN 占额外 1-2GB，减到 1 个
- RTX 4070 12GB 跑 FLUX → 必须 FP8 量化 + Tiled VAE，否则必 OOM
- RTX 4060 Ti 16GB 跑 FLUX FP16 → 16GB 也不够 FP16 FLUX。换 FP8

### "cuDNN error" / "CUDA error"

- 更新显卡驱动和 CUDA 版本
- 检查 PyTorch 版本是否匹配 CUDA (torch 2.x 需要 CUDA 11.8+)
- FP8 模型尝试加 --disable-cuda-malloc

### 节点连接报红

- 检查数据类型是否匹配 (LATENT 不能连 IMAGE)
- 某些节点有隐藏的输入要求（如 ControlNet 需要 image 输入）
- 检查节点版本是否过旧，用 Manager 更新
