"""Aesthetic skill (本地确定性版) — SuliAnimeCreator 三 skill 口径移植。

来源 (跨仓复用，不另起标准)：
- SuliAnimeCreator/skills/image/visual-aesthetics/SKILL.md
  §4 风格分级：设定级头 + 质量尾只用具体词；§2 光影三要素 (方向+光比+色温一次写全)；
  §1 冷暖结合；Mage 四要素顺序 subject/setting/light/framing。
- SuliAnimeCreator/skills/image/illustration-prompt/SKILL.md
  8-block 顺序；Vague ban；顺序即权重 (风格锚最前)；质量尾模型方言豁免。
- SuliAnimeCreator/skills/image/composition-camera/SKILL.md
  景别六选一不叠写；构图法每次>=2；防裁切；机位块。
- SuliAnimeCreator/skills/image/production-image-models/references/novelai.md
  Add Quality Tags 开关已追加的不手写重复。
- argon 终极质量词串指南 (NovelAI V5 & Tsubaki.3, 2026-10-16 接入)：
  V5 标准尾 very aesthetic/masterpiece/high complexity/no text 四件套 (Add Quality Tags 已追加的不重复)；
  复杂度四选一默认 high (泛用高质量档)；depthness/location 功能词保留；
  Tsubaki 自然语言质量尾 (Track2 tags_to_nl 用)。
- L-Port-Comfy/app/backend/prompts/draw_prompt.md
  画质词放最前、主体→风格→构图→光影→背景顺序、negative 思路 (本模块只管正向，
  NovelAI 通道 negative 由后端/客户端管，不在这里加)。
- anime-illust-divergent-director（二次元插画发散导演，2026-10-15 接入）：
  三向发散 A 氛围留白 / B 大透视张力 / C 超现实符号（本模块按场景关键词自动选型，
  只加结构性 tag，不加叙事道具——道具由 LLM spec 定，防幻觉加戏）；
  70/25/5 色彩配比（主色已定时补对撞轮廓光）；微表演只收已有表情词的细化映射；
  双轨编译 Track1 tags 矩阵（即本函数输出）+ Track2 自然语言（tags_to_nl，FLUX 类底座用）。

设计：零 LLM、零网络，纯字符串后处理。放在 spec 落单前最后一步，
LLM  spec 的措辞不动 (保前缀缓存)，缺的美学槽由本模块确定性补齐。
幂等：跑两遍结果不变（所有补齐先判存在）。
"""

from __future__ import annotations

import re

# ── 风格头 (visual-aesthetics §4，设定级头；禁 2D tv anime screencap) ──
_STYLE_HEADER = (
    "high-quality 2D anime illustration, anime key visual, "
    "official anime art, cel-shaded anime"
)

# ── 质量尾 (具体词，illustration-prompt 避雷 + visual-aesthetics §4) ──
_QUALITY_TAIL = "sharp lineart, highly detailed background, film grain"

# ── V5 复杂度四选一 (argon 精华：默认 high，泛用高质量档) ──
_COMPLEXITY_TAGS = (
    "low complexity",
    "medium complexity",
    "high complexity",
    "ultra complexity",
)

# 豁免的模型方言功能词 (Vague ban 不管它们)
_QUALITY_DIALECT = ("score_9", "masterpiece", "best quality", "absurdres")

# ── Vague ban (illustration-prompt)：自然语言形容词禁入，模型方言除外 ──
_VAGUE_RES = tuple(
    re.compile(r"\b" + w + r"\b", re.IGNORECASE)
    for w in (
        "cinematic", "epic", "stunning", "spectacular", "perfect",
        "premium", "high quality", "texture-rich",
    )
)
_RES_TOKENS = re.compile(r"\b(4K|8K|HD)\b", re.IGNORECASE)

# ── 机位/景别 (composition-camera 景别六选一) ──
_SHOT_TAGS = (
    "extreme close-up", "close-up", "head-and-shoulders portrait",
    "portrait, head and shoulders", "bust shot cropped at mid-torso",
    "upper body", "cowboy shot", "full body", "wide shot",
    "extreme wide shot", "lower body focus",
)

# ── 构图法信号词 (每次>=2，本模块只保底1个+景别=2要素) ──
_COMPO_TAGS = (
    "rule of thirds", "triangular composition", "rule of space",
    "negative space", "leading lines", "symmetrical composition",
    "centered composition", "diagonal composition", "golden ratio",
    "frame in frame", "depth of field", "three-layer depth",
)

# ── 光影信号词 (方向/光比/色温；visual-aesthetics §2) ──
_LIGHT_DIR = (
    "key light from upper left", "key light from upper right",
    "front light", "side light", "backlight", "rim light",
    "top light", "soft light", "hard light", "studio lighting",
    "natural light", "volumetric lighting", "stage spotlight",
)
_LIGHT_RATIO = (
    "high contrast", "low contrast", "soft shadows", "hard shadows",
)
_LIGHT_TEMP = (
    "warm 3200K", "warm lighting", "cool lighting", "golden hour",
    "daylight", "neutral color temperature",
)

# ── 三向发散选型 (divergent-director Step 1，关键词 deterministic) ──
# A 氛围留白：夜/雨/雪/黄昏/室内静物/壁纸循环类场景
_SCHEME_A_KEYS = (
    "night", "rain", "snow", "sunset", "dusk", "evening", "indoor",
    "cafe", "window", "bedroom", "bath", "candle", "moon", "starry",
    "wallpaper", "serene", "melancholic", "nostalgic",
)
# B 大透视张力：战斗/奔跑/跳跃/飞行/爆发/乐器演奏类动态场景
_SCHEME_B_KEYS = (
    "battle", "fight", "running", "jumping", "flying", "dancing",
    "playing guitar", "playing piano", "playing violin", "concert",
    "explosion", "action", "dynamic", "leaping", "spinning",
    "sports", "racing", "sword", "magic circle",
)
# C 超现实符号：梦境/镜面/漂浮/几何/星空异象类场景（其余默认不进 C）
_SCHEME_C_KEYS = (
    "dream", "mirror", "floating", "surreal", "nebula", "galaxy",
    "geometric", "fractal", "underwater", "crystal", "stained glass",
)

# 方案结构性点缀（只加机位/光影/构成类 tag，不加叙事道具防幻觉）
_ACCENT_A = ("volumetric lighting", "light particles", "atmospheric perspective")
_ACCENT_B = ("dynamic angle", "foreshortening", "motion lines")
_ACCENT_C = ("symmetrical composition", "vibrant contrast", "intricate details")

# 微表演细化映射（已有表情词 → 更具体的 tag；无表情词不加，不编情绪）
_MICRO_MAP = (
    ("smirk", "smug expression"),
    ("blush", "blush stickers"),
    ("crying", "tears streaming down"),
    ("laughing", "open-mouth smile"),
    ("angry", "furrowed brows"),
    ("surprised", "wide eyes"),
    ("sleepy", "half-closed eyes"),
    ("wink", "one eye closed"),
)

# 70/25/5 对撞轮廓光（主色已定时补对侧 accents，不改变主导色）
_RIM_WARM = "warm rim light"
_RIM_COOL = "cool rim light"


def pick_scheme(prompt_en: str) -> str:
    """按场景关键词选发散方案 A/B/C（默认 A，最稳）。"""
    _low = (prompt_en or "").lower()
    if any(k in _low for k in _SCHEME_C_KEYS):
        return "C"
    if any(k in _low for k in _SCHEME_B_KEYS):
        return "B"
    return "A"


def diverge_schemes(objective_cn: str = "") -> list[dict]:
    """三向发散方案简介（确定性模板，供确认单/面板展示二选一时用，不进 prompt）。"""
    _obj = (objective_cn or "").strip()[:40] or "本单"
    return [
        {"scheme": "A", "name": "极致光影与情绪留白",
         "blurb": f"{_obj}走氛围流：环境叙事+丁达尔效应+冷暖对撞，静谧宿命感，适合壁纸循环"},
        {"scheme": "B", "name": "大透视与作画张力",
         "blurb": f"{_obj}走张力流：极端机位+前景虚化+发衣飞舞，瞬间爆发定格"},
        {"scheme": "C", "name": "超现实意象与符号解构",
         "blurb": f"{_obj}走构成流：心理空间外化+强平面构成，设计感拉满"},
    ]


def _tags_of(prompt_en: str) -> list[str]:
    return [t.strip() for t in (prompt_en or "").split(",") if t.strip()]


# ── 双轨 Track2：tags → 自然语言（FLUX/多模态底座用，80-120 词帧序）──
_NL_DROP = (
    "masterpiece", "best quality", "absurdres", "score_9", "score_8_up",
    "score_7_up", "very aesthetic", "highly detailed",
)


def tags_to_nl(prompt_en: str) -> str:
    """Tag 矩阵转结构化英文白描（确定性）：构图画风 → 主体神态服饰材质 →
    光影色彩碰撞 → 前后景纵深粒子。质量词 folding 进首句，不单列。
    """
    _tags = [t for t in _tags_of(prompt_en) if t.lower() not in _NL_DROP]
    if not _tags:
        return ""

    def _grab(*words: str) -> list[str]:
        return [t for t in _tags if any(w in t.lower() for w in words)]

    _who = _grab("girl", "boy", "woman", "man", "catgirl", "solo", "duo")
    _wear = _grab("dress", "uniform", "shirt", "jacket", "skirt", "armor", "kimono",
                  "maid", "swimsuit", "coat", "hoodie", "sweater", "hair", "eyes",
                  "ahoge", "twintails", "ponytail", "blush", "smile", "expression")
    _cam = _grab("angle", "shot", "view", "fisheye", "close-up", "full body",
                 "wide", "portrait", "foreshortening", "perspective", "rule of",
                 "composition", "symmetrical", "centered", "depth of field",
                 "from below", "from above", "looking at viewer", "looking afar")
    _light = _grab("light", "shadow", "contrast", "glow", "sunlight", "backlight",
                   "rim", "volumetric", "lens flare", "particles", "night", "sunset")
    _used: set[str] = set()
    for _grp in (_who, _wear, _cam, _light):
        for _x in _grp:
            _used.add(_x)
    # 首 tag 未被归类多半是角色名/专名 → 并入主体，不扔背景堆
    if _tags and _tags[0] not in _used:
        _who = [_tags[0]] + _who
        _used.add(_tags[0])
    _rest = [_x for _x in _tags if _x not in _used]

    def _ph(ts: list[str]) -> str:
        return ", ".join(x.replace("_", " ") for x in ts[:14])

    _sents = [
        "High-quality 2D anime key visual"
        + (f" featuring {_ph(_who)}" if _who else ""),
        f"Subject details: {_ph(_wear)}." if _wear else "",
        f"Framing and camera: {_ph(_cam)}." if _cam else "",
        ((f"Light and palette: {_ph(_light)}"
          + (f"; background elements: {_ph(_rest)}" if _rest else "") + ".")
         if (_light or _rest) else ""),
        # Tsubaki 精华 (argon 建议)：自然语言质量尾放末尾，不堆 tag 垃圾场。
        "Clean anime illustration, high detail on costume and fabric folds, "
        "crisp lineart, cinematic color grading, high detail, masterpiece.",
    ]
    return re.sub(r"\s{2,}", " ", " ".join(s for s in _sents if s)).strip()


def _has_any(hay: str, needles: tuple[str, ...]) -> bool:
    _h = hay.lower()
    return any(n.lower() in _h for n in needles)


def _strip_vague(text: str) -> str:
    out = text
    for _rx in _VAGUE_RES:
        out = _rx.sub("", out)
    out = _RES_TOKENS.sub("", out)
    # 清理堆出来的空逗号/多空格 (循环到稳定，防 ", , ," 一遍压不完)
    for _ in range(3):
        _n = re.sub(r"(,\s*){2,}", ",", out)
        _n = re.sub(r"\s{2,}", " ", _n)
        if _n == out:
            break
        out = _n
    return out.strip(" , ")


def enhance_prompt_en(prompt_en: str, orientation: str = "portrait") -> str:
    """美学后处理：风格头置顶 + 缺槽补齐 + Vague 清洗 + 质量尾兜底。

    输入：spec/翻译层产出的英文逗号 prompt (角色 tag 已在最前)。
    输出：同一格式，可直接送 NovelAI/PixAI。幂等：已有的不重复加。
    """
    _p = (prompt_en or "").strip()
    if not _p:
        return ""
    _p = _strip_vague(_p)
    _low = _p.lower()

    # 1) 风格锚最前 (顺序即权重)：缺才顶到最前，不动角色 tag 相对顺序。
    # 角色 tag 本就该在最前 (draw_confirm 同人铁律)，风格头紧跟其后。
    if "cel-shaded anime" not in _low and "anime key visual" not in _low:
        # 找到首个逗号后插入，避免盖住开头的 danbooru 角色 tag。
        if "," in _p:
            _head, _rest = _p.split(",", 1)
            _p = f"{_head.strip()}, {_STYLE_HEADER}, {_rest.strip()}"
        else:
            _p = f"{_p}, {_STYLE_HEADER}"
        _low = _p.lower()

    # 2) 机位/景别保底 (composition-camera)：缺景别按方向给默认。
    if not _has_any(_low, _SHOT_TAGS):
        _default_shot = (
            "full body" if "full body" in _low or orientation == "portrait" and "full-body" in _low
            else ("bust shot cropped at mid-torso, eye-level, straight-on")
        )
        # 全身单在 spec 里常已写 full body 文案而非 tag，先判文本再补 tag。
        if "full body" not in _low and "full-body" not in _low:
            if orientation == "landscape":
                _default_shot = "wide shot"
            _p = f"{_p}, {_default_shot}"
        _low = _p.lower()

    # 3) 构图法保底 (>=2 中的第二个)：缺则按方向补一个。
    if not _has_any(_low, _COMPO_TAGS):
        _p = f"{_p}, {'rule of thirds' if orientation == 'portrait' else 'three-layer depth'}"
        _low = _p.lower()

    # 4) 光影三要素一次写全 (方向+光比+色温)：缺哪项补哪项。
    _adds: list[str] = []
    if not _has_any(_low, _LIGHT_DIR):
        _adds.append("key light from upper left")
    if not _has_any(_low, _LIGHT_RATIO):
        _adds.append("high contrast")
    if not _has_any(_low, _LIGHT_TEMP):
        _adds.append("warm 3200K")
    # 冷暖结合：主观偏暖时已含 warm；偏冷未写反色不强求 (零幻觉优先)。
    if _adds:
        _p = f"{_p}, {', '.join(_adds)}"
        _low = _p.lower()

    # 4b) 70/25/5 对撞轮廓光 + 方案点缀 (divergent-director 蓝图层，结构性 tag only)。
    _scheme = pick_scheme(_p)
    _accents: list[str] = []
    if _RIM_WARM not in _low and _RIM_COOL not in _low and "rim light" not in _low:
        if "cool" in _low and "warm" not in _low:
            _accents.append(_RIM_WARM)
        elif "warm" in _low:
            _accents.append(_RIM_COOL)
    _scheme_tags = {"A": _ACCENT_A, "B": _ACCENT_B, "C": _ACCENT_C}[_scheme]
    # 幂等：本方案点缀已有任意一个就不再加（防多跑多堆）。
    if not any(_t in _low for _t in _scheme_tags):
        for _t in _scheme_tags:
            if _t not in _low:
                _accents.append(_t)
                break  # 每单只点缀 1 个，不堆
    # 微表演：已有表情词才细化；已有任一细化词不再加（幂等）。
    if not any(_dst in _low for _, _dst in _MICRO_MAP):
        for _src, _dst in _MICRO_MAP:
            if _src in _low and _dst not in _low:
                _accents.append(_dst)
                break  # 只细化 1 处
    if _accents:
        _p = f"{_p}, {', '.join(_accents)}"
        _low = _p.lower()

    # 5) 质量尾兜底 (具体词)：方言词保留，NovelAI 开关词不重复。
    if "sharp lineart" not in _low:
        _p = f"{_p}, {_QUALITY_TAIL}"
    # masterpiece/best quality 缺才补 (NovelAI 开关可能已加，但 API 侧不重复计算) 。
    if "masterpiece" not in _low:
        _p = f"{_p}, masterpiece"
        _low = _p.lower()
    if "best quality" not in _low:
        _p = f"{_p}, best quality"
        _low = _p.lower()

    # 5b) V5 精华 (argon 建议)：复杂度四选一默认 high；very aesthetic 缺才补。
    # Add Quality Tags 开关已追加的不重复；no text/amazing quality 只保留不强加
    # (用户要画文字时强加 no text 会误伤，此处不默认加)。
    if not _has_any(_low, _COMPLEXITY_TAGS):
        _p = f"{_p}, high complexity"
        _low = _p.lower()
    if "very aesthetic" not in _low:
        _p = f"{_p}, very aesthetic"
        _low = _p.lower()

    return re.sub(r"\s{2,}", " ", _p.strip(" , "))
