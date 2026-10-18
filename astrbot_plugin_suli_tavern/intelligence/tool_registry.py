"""工具元数据统一注册表 — Gate/后台任务/tavern 共用的工具分类真相源。

此前 exempt 集 / 强制查询白名单 / 工具族分散在 group_chat.py 三处常量 +
任务执行器注入逻辑，新增查询工具需要改多处。本模块集中声明，
调用方懒加载 import（原则 38: 非 suli_tavern 插件走 lazy import + fallback）。

新增数据查询类工具时: 在 MANDATORY_QUERY_TOOLS 加一行即可同时作用于
执行器 must_tools 和 tavern fallback 防幻觉，无需再改其他文件。
"""

from __future__ import annotations

import re

# ── 门控豁免工具: 零成本本地操作, 不查好感度/不受二元门控全关/不触发执行器分流 ──
GATE_EXEMPT_TOOLS: frozenset[str] = frozenset({
    "send_sticker",
    "parse_forwarded_message",
})

# ── 强制查询白名单: 答案只存在于工具返回值, 不调用必然靠记忆/猜测编答 ──
# 执行器路径 → must_tools 注入 [必须先调用]; tavern fallback 路径 → 系统指令强制。
MANDATORY_QUERY_TOOLS: frozenset[str] = frozenset({
    # L4D2 数据监控 (只读)
    "l4d2_server_status",
    "l4d2_player_score",
    "l4d2_player_stats",
    "l4d2_leaderboard",
    # 其他只读查询工具 (按需扩展)
})

# ── 工具族: 已清空（2026-10-13 搜索族拆分）──
# 搜索三件套不再自动全族放行（每次文字搜索烧 3 工具位）。
# Gate 按需单建议：文字二选一 web_search/search_knowledge，图搜才加 pixiv_search。
# family_of 保留兼容，恒返回单元素。
TOOL_FAMILIES: tuple[tuple[str, ...], ...] = ()


def is_exempt(tool_name: str) -> bool:
    """是否豁免工具 (零成本本地操作)。"""
    return tool_name in GATE_EXEMPT_TOOLS


def is_mandatory_query(tool_name: str) -> bool:
    """是否强制查询工具 (答案只在工具返回值里)。"""
    return tool_name in MANDATORY_QUERY_TOOLS


def filter_mandatory(
    suggested: list[str] | tuple | set | None,
) -> list[str]:
    """从 Gate 建议中过滤出强制查询工具 (去重, 保持建议顺序)。"""
    if not suggested:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in suggested:
        if t in MANDATORY_QUERY_TOOLS and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def strip_exempt(
    suggested: list[str] | tuple | set | None,
) -> set[str]:
    """去掉豁免工具后的实质工具集 (执行器分流判定用)。"""
    return set(suggested or []) - GATE_EXEMPT_TOOLS


def build_task_allowlist(
    suggested: list[str] | tuple | set | None,
    must: list[str] | tuple | set | None = None,
) -> frozenset[str] | None:
    """构建单任务工具 allowlist (C2 真过滤)。

    与 dispatcher prompt D14 同源 (capability_scope)：agent 在协议层只能
    看见/调用这些工具。must 工具恒在 (答案只存在于其返回值里)。
    无 scope 信息时返回 None = 全量 (fail-open，保持今日行为；
    有 scope 才收紧，渐进式生效)。
    """
    scope = {str(t).strip() for t in (suggested or []) if str(t).strip()}
    for t in must or []:
        if str(t).strip():
            scope.add(str(t).strip())
    if not scope:
        return None
    return frozenset(scope)


# ── 任务目标关键词 → 工具 (scope 为用户级宽集合时的收紧网) ──
#
# D14 的 capability_scope 是“用户可用”宽集合 (affinity/开关过滤后仍 20+)，
# 直呼跳 Slim 时 Gate 建议缺席，agent 面对全量表容易漂移无为
# (2026-10-11 生图任务 60s 零调用教训)。brief 目标含这些词即收紧。
# 保守：仅覆盖高频明确意图；无命中返回空集 (调用方回退宽 scope)。
_KEYWORD_TOOLS: tuple[tuple[tuple[str, ...], frozenset[str]], ...] = (
    (("画一张", "画一个", "画一只", "画一位", "画幅", "绘制", "生成一张", "生成一个", "生图", "绘图", "绘画", "插画", "二创图"), frozenset({"generate_image"})),
    (("改图", "编辑图片", "重绘", "以图生图", "参考这张"), frozenset({"edit_image"})),
    (("对着画", "按这个画", "照着画", "参考着画", "按刚搜"), frozenset({"describe_pixiv_refs", "pixiv_search", "generate_image"})),
    (("搜", "查", "找图", "最新", "乐子", "新闻", "动态", "热搜", "这几天",
      "最近发生", "有什么新", "出了什么事", "怎么了"),
     frozenset({"web_search", "search_knowledge", "pixiv_search"})),
    (("记住", "记得"), frozenset({"remember_memory", "get_memory"})),
)


def infer_task_tools(objective: str) -> set[str]:
    """从任务目标文本推断任务级工具集。无命中返回空集。"""
    text = str(objective or "")
    out: set[str] = set()
    for keywords, tools in _KEYWORD_TOOLS:
        if any(k in text for k in keywords):
            out |= set(tools)
    return out


# ── 生图意图补网 (2026-10-12 假完成事故) ─────────────────────────
# 事故链: 「帮我画一张…二创图」→ _KEYWORD_TOOLS 只认「画一张」（不认「画一张」
# 之外的「生成一张/二创图/来一张」）→ infer 空集 → allowlist 保持宽集合 20 个
# → agent 空转 170s 后编出「…人家画好了」的成品描述 (零工具调用、零出图)。
# 这张网只看「创作动词 + 图像名词」同现，命中即把生图工具并入推断 —— 之后
# 任务会走 spec 直算 (不进 agent)，从结构上消灭这类幻觉。
_DRAW_CREATION_RE = re.compile(
    r"(?:帮我|给我|替我|请|再)?\s*(?:"
    # ① 创作动词 + 图像名词 (动词与名词可隔着定语): 画一张…二创图 / 生成一张图 / 出个图
    # 画后接这些字是名词复合 (画风/画质/画师/画面…)，不是创作动词
    # (2026-10-14 找画混淆事故：「找一张画风好看的同人图」)
    # 2026-10-15 花火事故补丁：「绘制一张《崩坏：星穹铁道》花火的同人图」
    # 动词到名词跨过长定语，间隙 8→16；「绘制」显式消费两字，避免「绘」后剩「制」占间隙。
    r"(?:画(?!风|质|师|面|像|作|家|廊|展|报|册|集|卷|页|框|轴)|繪|绘制|绘|生成|制作|做|搞|弄|整|来|出)\s*"
    r"(?:一张|一幅|一个|一份|一只|一位|个|张|幅|版|下|一下)?\s*"
    r"[^\s，。！？!?、]{0,16}?"
    r"(?:图|插画|插圖|立绘|壁纸|头像|表情包|插画集)"
    r"|"
    # ② 只有「画 + 量词」也够 (宾语是主题不是图): 画个鲸鱼娘 / 给我画只猫 / 画张头像
    r"(?:画|繪|绘|绘制)\s*(?:一张|一幅|一个|一只|一位|一条|一头|一匹|个|张|幅|版|只|条|头|匹)"
    r")"
)
# 强名词 (本身就是生图意图); 后接「功能/能力」时是在问有没有这功能, 不算请求。
_DRAW_NOUN_RE = re.compile(
    r"(?:生图|绘图|绘画|插画|插圖|立绘|同人图|二创图|二創圖|二创插画)(?!功能|能力)"
)


# ── 索取动词网 (2026-10-14 找画混淆事故) ──────────────────────
# 「去帮我找一张流萤的同人图」被强名词网（同人图）误判进生图管线：
# 守卫升级 + allowlist 双双放行画图，Brain 再把 brief 写成「生成一张」。
# 索取 ≠ 创作：撞上强名词也不算生图 —— 除非创作 pattern 本身已命中
# （创作优先：“求你帮我画一张”照样是画）。
# 2026-10-15 花火事故补丁：「追求极高质量美图」里的「求」不是索取。
# 「追求/要求/需求/请求」先剥掉再判索取，避免质量诉求误杀绘图意图。
_SEARCH_VERB_RE = re.compile(r"(找|搜|查|寻|求|翻|看有没有|有没有)")
_SEARCH_EXCLUDE_WORDS = ("追求", "要求", "需求", "请求")


def _strip_search_exclude(text: str) -> str:
    """剥掉非索取语境里的「求」：追求/要求/需求/请求。"""
    t = str(text or "")
    for w in _SEARCH_EXCLUDE_WORDS:
        if w in t:
            t = t.replace(w, "XX")
    return t


def looks_like_search_request(text: str) -> bool:
    """文本是否像「索取现成的东西」(找/搜/求/翻 + 对象) 且无创作意图。

    用途: dispatcher 原文回查 —— brief 是 Brain 的转述，转述可能丢字
    (2026-10-14 实测「找一张」→「生成一张」)。原文索取主导时，用原文
    一票否决画图强制意图。创作 pattern 优先：有创作动词的一律不算索取。
    """
    t = str(text or "")
    if not t:
        return False
    if _DRAW_CREATION_RE.search(t):
        return False
    return bool(_SEARCH_VERB_RE.search(_strip_search_exclude(t)))


def looks_like_draw_request(text: str) -> bool:
    """文本是否像「要一张图」的请求 (供 allowlist 收紧 + must 硬约束)。

    保守优先: 只认「创作动词 + 图像名词」同现，或强名词 (生图/绘图/插画/立绘/
    同人图/二创图)。看/描述/发图类请求 (VLM、引用图) 一律不命中 —— 误判会让
    agent 被强制调 generate_image 去画一张本来只是让人读的图。
    索取类请求 (找/搜/求/翻 + 图) 同理不命中 —— 同上，误判会把找图派成画图。
    """
    t = str(text or "")
    if not t:
        return False
    if _DRAW_CREATION_RE.search(t):
        return True
    if _SEARCH_VERB_RE.search(_strip_search_exclude(t)):
        return False
    if _DRAW_NOUN_RE.search(t):
        return True
    return False


def family_of(tool_name: str) -> tuple[str, ...]:
    """返回工具所在族 (无族返回单元素元组)。"""
    for family in TOOL_FAMILIES:
        if tool_name in family:
            return family
    return (tool_name,)


# ── Tool Profile 已删除 (2026-10-13 死代码清理) ──
# TOOL_PROFILES / select_profile / profile_hash 全仓库零调用，且 main-v1 引用的
# memory_recall/dispatch_task/task_status/cancel_task 在 TOOLS 中不存在。
# Dispatcher 实际走 build_task_allowlist + infer_task_tools + looks_like_draw_request。
