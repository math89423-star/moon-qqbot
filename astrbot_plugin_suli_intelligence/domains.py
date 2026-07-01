"""知识领域检测 — 基于关键词匹配的话题感知。

设计原则:
  - 不用 LLM 做检测 — 关键词匹配足够快、确定性高、零延迟
  - LLM 的工作是根据检测结果调整回复风格，而非判断话题类型
  - 领域分数做半衰期衰减，与热度状态机逻辑一致

用法:
  from .domains import DOMAINS, detect_domains

  scores = detect_domains(content, existing_scores, half_life, now)
  hints = get_domain_hints(scores, threshold)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Domain:
    """知识领域定义。

    Attributes:
        key: 唯一标识 (如 "ai_creation")
        name: 中文名 (如 "AI 创作")
        keywords: 强信号词 — 命中一个即大幅加分
        weak_keywords: 弱信号词 — 命中加分小，需累积
        system_prompt_append: 命中时追加到 LLM system prompt 的内容
        heat_boost: 领域活跃时给群聊热度的额外加成
    """

    key: str
    name: str
    keywords: list[str]
    weak_keywords: list[str] = field(default_factory=list)
    system_prompt_append: str = ""
    heat_boost: float = 1.0


# ── 领域定义 ──────────────────────────────────────────────

DOMAINS: list[Domain] = [
    Domain(
        key="ai_creation",
        name="AI 创作",
        keywords=[
            # ComfyUI 核心
            "comfyui", "comfy", "confyui",
            # 模型系列
            "stable diffusion", "stablediffusion", "sd1.5", "sd1.5",
            "sdxl", "sd xl", "sd3", "sd3.5", "flux", "flux.1",
            "lora", "lycoris", "loha", "dora",
            "controlnet", "control net", "深度控制",
            # 技术组件
            "vae", "taesd", "ip-adapter", "ipadapter", "instantid",
            "线稿上色", "姿态控制", "inpaint", "重绘",
            # 训练
            "炼丹", "微调", "finetune", "dreambooth", "everydream",
            "融合模型", "模型融合", "merge",
        ],
        weak_keywords=[
            # 生图相关
            "生图", "画图", "生成", "跑图", "出图", "作图",
            "文生图", "图生图", "txt2img", "img2img",
            # 模型/节点
            "模型", "节点", "自定义节点", "checkpoint", "ckpt",
            "safetensors", "unet", "clip",
            # 提示词
            "提示词", "prompt", "负面提示词", "negative prompt",
            "正负向", "tag", "打标",
            # 采样参数
            "采样", "采样器", "步数", "steps", "cfg", "cfg scale",
            "种子", "seed", "降噪", "denoise",
            # 后处理
            "放大", "高清修复", "面部修复", "hires", "upscale",
            "超分", "放大算法",
            # 工作流
            "工作流", "workflow", "管线", "pipeline",
            # 插件
            "插件", "扩展", "custom nodes",
            # 显卡/性能
            "显存", "vram", "gpu", "cuda", "cudnn",
            # AI 绘画平台
            "novelai", "nai", "midjourney", "mj", "dalle",
            "webui", "forge", "reforge", "comfy",
            # 风格/LoRA
            "画风", "风格", "质感", "光影",
            # 问题排查
            "报错", "出不来", "效果不好", "崩溃", "oom",
            # 提示词工程
            "咒语", "画师", "画师标签", "artist", "权重", "括号", "调度词",
            "触发词", "trigger", "激活词", "activation",
            "语义", "danbooru", "e621", "gelbooru", "safebooru",
            "构图", "镜头", "视角", "光影", "氛围", "色调",
            "服装", "服饰", "发型", "瞳色", "发色",
            "姿势", "动态", "静态",
        ],
        system_prompt_append=(
            "\n\n[当前话题：AI 创作 / ComfyUI — 你的核心专业领域]\n"
            "群里正在讨论 AI 绘画相关话题。你就是这方面的专家——"
            "对 ComfyUI / Stable Diffusion / LoRA / ControlNet 了如指掌。\n"
            "\n"
            "[专业回答准则]\n"
            "1. 用自信、专业、坚定的口吻回答技术问题。"
            "你是专家，给出明确的答案，不要含糊其辞。\n"
            "2. 给出具体建议时明确范围和依据——"
            "例如「SDXL 的 CFG 推荐 5-7，过 10 容易过曝」而不是「可能 5 左右」。\n"
            "3. 信息来源标注：查了知识库的回答标注「📚」，"
            "查了网页的回答标注「🌐」。让群友知道你的信息依据。\n"
            "4. 不确定的技术细节，先调用 search_knowledge 工具查证再回答，"
            "不要凭记忆猜测。知识库查不到就试 web_search。\n"
            "5. 像群里的技术大牛那样自然交流——不要变成客服机器人，"
            "不要一次性把知识全倒出来。\n"
            "\n"
            "[被质疑时的应对]\n"
            "如果群友对你的专业回答提出质疑（说「不对」「错了」「应该是」）：\n"
            "- 先承认存在分歧，重新查证（知识库 + 网页搜索）\n"
            "- 如果发现自己确实错了：大方承认「查了一下确实是我搞错了，"
            "感谢指正！」，然后给出正确信息\n"
            "- 如果核实后自己正确：礼貌但坚定地维护自己的判断，"
            "引用证据「我查了一下，xxx 官方文档明确说了...」\n"
            "- 如果无法确定：诚实表示「这一点我不完全确定，"
            "建议查 xxx 官方文档，或咨询管理员确认」"
        ),
        heat_boost=1.5,
    ),
]

# ── 公开接口 ──────────────────────────────────────────────


def detect_domains(
    content: str,
    existing_scores: dict[str, float] | None = None,
    half_life: float = 120.0,
    now: float | None = None,
) -> dict[str, float]:
    """扫描消息内容，返回更新后的领域分数。

    对每个领域:
      1. 先做半衰期衰减
      2. 扫描关键词 — 强信号命中 +2.0, 弱信号命中 +0.5
      3. 单条消息单领域最多 +3.0 (防止一条消息刷满)

    Args:
        content: 消息文本
        existing_scores: 当前各领域分数 (会被修改并返回)
        half_life: 领域分数半衰期 (秒)
        now: 当前时间 (用于衰减计算)

    Returns:
        {domain_key: score, ...}  (与 existing_scores 是同一 dict)
    """
    if now is None:
        now = time.time()

    scores = existing_scores or {}

    # 半衰期衰减
    for key in list(scores):
        # 用 domain 的 last_update 做衰减
        pass  # 衰减由调用方在 _update_domains 中处理

    lower = content.lower()

    for domain in DOMAINS:
        added = 0.0

        # 强信号: 每个命中 +2.0
        for kw in domain.keywords:
            if kw in lower:
                added += 2.0
                break  # 强信号命中一个就够了

        # 弱信号: 累积加分
        if added == 0.0:
            for kw in domain.weak_keywords:
                if kw in lower:
                    added += 0.5

        # 单条消息上限
        added = min(added, 3.0)

        if added > 0:
            scores[domain.key] = scores.get(domain.key, 0.0) + added

    return scores


def get_domain_hints(
    active_domains: dict[str, float],
    threshold: float = 2.0,
) -> str:
    """根据活跃领域生成 system prompt 追加内容。

    Args:
        active_domains: {domain_key: score, ...}
        threshold: 分数超过此值才注入提示

    Returns:
        追加到 system prompt 的文本 (可能为空)
    """
    # 按分数降序
    domain_map = {d.key: d for d in DOMAINS}
    parts = []
    for key, score in sorted(
        active_domains.items(), key=lambda x: x[1], reverse=True
    ):
        if score >= threshold and key in domain_map:
            parts.append(domain_map[key].system_prompt_append)
    return "".join(parts)


def get_domain_heat_boost(
    active_domains: dict[str, float],
    threshold: float = 2.0,
) -> float:
    """计算领域热度的额外加成。

    Returns:
        热度加成倍率 (默认 1.0 = 无加成)
    """
    domain_map = {d.key: d for d in DOMAINS}
    boost = 1.0
    for key, score in active_domains.items():
        if score >= threshold and key in domain_map:
            boost = max(boost, domain_map[key].heat_boost)
    return boost


# 深度思考触发领域 — 这些领域激活时需要逐步推理
# 后续新增技术领域时在此注册
_REASONING_DOMAINS: set[str] = {
    "ai_creation",       # AI 创作 / ComfyUI 技术问题
}


def is_reasoning_needed(
    active_domains: dict[str, float],
    threshold: float = 2.0,
) -> bool:
    """判断当前话题是否需要深度思考 (技术/复杂话题)。"""
    return any(
        key in _REASONING_DOMAINS and score >= threshold
        for key, score in active_domains.items()
    )


def user_force_reasoning(message: str) -> bool:
    """检查用户消息是否显式要求深度思考 (如「你想想」「分析一下」)。"""
    msg_lower = message.lower()
    return any(kw in msg_lower for kw in _FORCE_REASONING_KEYWORDS)


# 用户显式要求思考的关键词 — 命中任意一个即强制开启深度思考
_FORCE_REASONING_KEYWORDS: list[str] = [
    "想想", "思考", "分析一下", "推理", "想一想",
    "好好想", "仔细想", "认真想", "深思", "琢磨",
    "think", "reason", "analyze",
    "你怎么看", "你觉得呢", "给点建议", "帮我判断",
]

REASONING_INSTRUCTION = (
    "\n\n[🧠 深度思考模式 — 当前话题需要认真分析]\n"
    "这个问题有一定复杂度，请不要急于给出答案。\n"
    "在回答之前，先在心里理清：\n"
    "1. 问题的核心是什么？对方真正想知道什么？\n"
    "2. 需要用到哪些知识点？有没有不确定的地方需要先查工具？\n"
    "3. 最好的解释顺序是什么？从简单到复杂，还是先给结论再解释？\n"
    "然后用清晰、有条理的方式回答。不一定要把思考过程写出来——"
    "但你的回答应该体现出你认真想过。\n"
    "技术细节务必准确，不确定的先 search_knowledge 或 web_search。"
)
