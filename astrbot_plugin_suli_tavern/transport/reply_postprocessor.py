"""回复后处理器 — Markdown 清理 + @提及转换 + 反臃肿 + 重复检测。

从 group_chat.py 提取的纯函数模块，无状态，零 class 依赖。

用法:
  from .reply_postprocessor import (
      sanitize_qq_reply, resolve_at_mentions, filter_narration,
      get_recent_bot_replies, is_duplicate,
  )
"""

from __future__ import annotations

import difflib
import logging
import re

logger = logging.getLogger(__name__)

# ── Markdown 清理 ───────────────────────────────────────


def sanitize_qq_reply(text: str) -> str:
    """清理 LLM 回复中的平台不支持格式 (委托到平台适配器)。

    此函数保留作为向后兼容别名，实际逻辑在 QQPlatformAdapter.sanitize_reply()。
    """
    try:
        from ..platform_adapter import get_adapter
        return get_adapter().sanitize_reply(text)
    except RuntimeError:
        pass  # 适配器未初始化 → 内联处理

    # ── Fallback: 内联 Markdown 清理 (适配器未初始化时) ──
    if not text:
        return text

    # 0. 清理 LLM 假引用
    text = re.sub(
        r"\[引用的消息内容[：:]\s*[^\]]*\]\s*",
        "", text,
    )
    text = re.sub(
        r"\[引用的消息内容\]\s*",
        "", text,
    )

    # 1. 代码块 (先处理，避免内部 markdown 被误处理)
    text = re.sub(r"```[^\n]*\n.*?```", lambda m: m.group(0).replace("`", ""), text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # 2. 粗体+斜体
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    # 3. 粗体
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    # 4. 斜体 (注意不要匹配颜文字中的 * )
    text = re.sub(r"(?<!\*)\*(.+?)\*(?!\*)", r"\1", text)

    # 5. Markdown 标题 (# ## ### 等)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # 6. 水平线
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # 7. 所有连续换行 → 单换行 (QQ消息不留空行)
    text = re.sub(r"\n{2,}", "\n", text)

    # 8. 行首行尾空格
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = text.strip()

    return text


# ── @提及转换: 纯文本 @name → 平台特定 @格式 ───────────
# @name 模式: @后跟非空白/非标点的名字 (1-20字)
# 正则从平台适配器获取 (QQ: _AT_NAME_RE, 微信: 可能不同)
_AT_NAME_RE: re.Pattern | None = None  # 懒加载


def _get_at_name_re() -> re.Pattern:
    """获取 @名字 匹配正则 (优先从平台适配器，fallback 内置)。"""
    global _AT_NAME_RE
    if _AT_NAME_RE is not None:
        return _AT_NAME_RE
    try:
        from ..platform_adapter import get_adapter
        _AT_NAME_RE = get_adapter().get_at_mention_regex()
        return _AT_NAME_RE
    except RuntimeError:
        pass
    # Fallback: 内置正则 (QQ 平台)
    _AT_NAME_RE = re.compile(
        r'@([^\s'
        r'，。！？、：；""''（）()'    # 中英文标点
        r'\[\]【】'                    # 方括号 (避免匹配 CQ 码)
        r'\n@'                         # 换行 + @ 自身
        r']{1,20})'
    )
    return _AT_NAME_RE

# peer bot 的常见昵称 — 用于将 @peer_bot 等映射到 peer_bot_qq
_PEER_BOT_NAMES: tuple[str, ...] = ()  # 由调用方从 BotIdentityService 动态获取

# 主人的关系称呼 — 人格里叫主人「主人」/「好孩子」, 模型会照写 "@主人"。
# 这些是称呼不是昵称, 要指回主人 QQ (2026-10-18 事故: 群里出现字面 "@主人")。
_MASTER_ALIASES: tuple[str, ...] = ("主人", "好孩子")


def resolve_at_mentions(
    text: str,
    ctx_messages: list[dict],
    bot_name: str = "暮恩",
    peer_bot_qq: str = "",
    peer_bot_names: tuple[str, ...] = _PEER_BOT_NAMES,
    trigger_uid: str = "",
    trigger_name: str = "",
    master_uids: tuple[str, ...] = (),
) -> str:
    """将 LLM 输出中的 @昵称 纯文本转换为平台特定 @格式 (委托到适配器)。

    LLM 在 prompt 中被要求用 @名字 标明说话对象, 但输出的 @名字
    只是纯文本, 不会触发真正的 @提醒。此函数从最近上下文
    建立 name→user_id 映射, 将 @name 替换为平台真正的 @格式。

    处理逻辑:
      1. 从 ctx_messages 提取 name→user_id 映射 (最近消息优先)
      2. 显式添加 trigger_uid→trigger_name 映射 (确保触发者名字被找到)
      3. 扫描文本中的 @name 模式, 最长匹配优先
      4. 查询映射: @bot自己 → 跳过, @peer → peer_bot_qq, @群友 → user_id
      5. ★防幻觉: 解析出的 user_id 必须在最近消息参与者集合中出现过
      6. ★限量: 最多 3 个 @提及 (防止 LLM 滥 @)
      7. 无法识别的名字保留原文 (避免破坏非 @ 语境)
      8. 最终 @格式 由 get_adapter().build_at_mention() 生成

    ★ 2026-06-27 防幻觉守卫:
      - 构建 recent_participant_ids 集合 (最近 80 条消息中实际发言的 user_id)
      - 解析出的 user_id 若不在参与者集合中 → 跳过 (LLM 可能幻觉了不存在的用户)
      - 例外: trigger_uid 和 peer_bot_qq 始终允许 (前者是真实触发者, 后者是已知对照 bot)
    """
    if not text or "@" not in text:
        return text

    # ── 获取平台适配器的 @构建方法 ──
    try:
        from ..platform_adapter import get_adapter
        _build_at = get_adapter().build_at_mention
    except RuntimeError:
        # Fallback: QQ 格式 (适配器未初始化时)
        _build_at = lambda qq: f"[CQ:at,qq={qq}] "  # noqa: E731

    # ── 构建 name → qq 映射 ──
    name_to_qq: dict[str, str] = {}
    recent_participant_qqs: set[str] = set()  # ★ 防幻觉: 最近实际发言的 QQ 集合
    for msg in reversed(ctx_messages[-80:]):
        uid = str(msg.get("user_id", ""))
        name = str(msg.get("user_name", ""))
        if uid and name and not uid.startswith("bot_"):
            if name not in name_to_qq:
                name_to_qq[name] = uid
            recent_participant_qqs.add(uid)  # ★ 记录实际参与者

    # ── 显式添加触发者映射: 确保 LLM @触发者名字 时一定能解析 ──
    #     触发者可能不在最近 80 条消息中 (长时间对话/合并触发),
    #     但如果有 trigger_uid + trigger_name 就一定能找到。
    if trigger_uid and trigger_name:
        name_to_qq[trigger_name] = trigger_uid

    # ── 添加 peer bot 昵称映射 ──
    if peer_bot_qq:
        for pn in peer_bot_names:
            name_to_qq[pn] = peer_bot_qq

    # ── 关系称呼别名 → 主人 QQ (2026-10-18 事故: 群里冒出字面 "@主人") ──
    # 人格里称主人「主人」/「好孩子」, 模型照写 "@主人"; 名字表里没有这个昵称 →
    # 按 §7 原样留字面 "@主人" 发进群: @ 不到人 + 出戏。
    # 这里把关系称呼指回主人 QQ, 仍受 §5 防幻觉守卫约束 (主人不在最近参与者/白名单
    # 内就不换), fail-safe。
    for _mq in (master_uids or ()):
        _mq = str(_mq or "").strip()
        if not _mq:
            continue
        for _alias in _MASTER_ALIASES:
            name_to_qq.setdefault(_alias, _mq)

    # ── ★ 始终允许的 QQ 白名单 (不受参与者检查限制) ──
    _always_allowed_qqs: set[str] = set()
    if trigger_uid:
        _always_allowed_qqs.add(trigger_uid)
    if peer_bot_qq:
        _always_allowed_qqs.add(peer_bot_qq)

    # ── bot 自己的名字 → 不映射 (不能 @自己) ──
    bot_skip_names = {
        bot_name,
        bot_name.lower(),
    }

    # ── ★ 最大 @提及数: 防止 LLM 滥 @ 多人 ──
    _MAX_AT_MENTIONS = 3

    # ── 扫描并替换 ──
    replacements: list[tuple[int, int, str]] = []  # (start, end, replacement)
    skipped_hallucination = 0

    for m in _get_at_name_re().finditer(text):
        name = m.group(1)

        # 跳过 bot 自己
        if name in bot_skip_names or name.lower() in bot_skip_names:
            continue

        # ★ 限量检查: 超过上限后不再转换
        if len(replacements) >= _MAX_AT_MENTIONS:
            break

        # 查找 QQ 号
        qq = name_to_qq.get(name)
        if qq:
            # ★ 防幻觉守卫: 解析出的 QQ 必须是最近参与者或白名单成员
            if qq not in recent_participant_qqs and qq not in _always_allowed_qqs:
                skipped_hallucination += 1
                logger.debug(
                    "resolve_at_mentions: 跳过幻觉 @%s → QQ=%s (不在最近参与者中)",
                    name, qq[:8],
                )
                continue

            # 用平台适配器的 @格式 替换 @name
            # 注意: @码 后面不跟原名字, 避免渲染后名字重复
            replacements.append((m.start(), m.end(), _build_at(qq)))

    # ── 应用替换 (从后往前, 避免位置偏移) ──
    for start, end, repl in reversed(replacements):
        text = text[:start] + repl + text[end:]

    if skipped_hallucination:
        logger.info(
            "resolve_at_mentions: 拦截 %d 个幻觉 @提及",
            skipped_hallucination,
        )

    return text


# ── 反臃肿过滤器 (Anti-Bloat) ─────────────────────────

# ── 动作容器: （）内一律保留 (2026-10 设计裁定) ─────────────
# 裁定原文: 「动作描写允许, 让 agent 自由发挥, 尺度尽量大并放开, 同时文笔细腻」。
# 因此 （）是动作/神态/身体反应的容器, 属于沉浸资产, 不再是噪音。
# 旁白清理只作用于括号【外】的第三人称小说腔 —— 括号内原文一字不动。
#
# 历史包袱备忘: 旧版 `_STAGE_DIRECTION_RE` 曾删除全部（）内容,
# 与 hormone.py:872「允许用（）写动作」及 NSFW 卡「（）必须用」正面冲突,
# 导致成人区动作描写 100% 蒸发。该常量已废弃, 不要再恢复。

# 动作容器 = 括号内【含中文】。这道判据把动作与颜文字彻底分开:
#   （手在抖） / (笑到呛住)  → 中文 → 动作容器, 保护
#   (・ω・) / (￣ω￣)        → 无中文 → 颜文字, 交给密度控制
# 未含中文的括号不享受保护, 否则 density 规则再也裁不到真颜文字。
_BRACKET_RE = re.compile(r"[（(][^)）]*[一-鿿][^)）]*[)）]")


def _protect_brackets(text: str) -> tuple[str, list[str]]:
    """抽取（）内容并占位, 使其不被后续旁白/自称规则误伤。"""
    store: list[str] = []

    def _sub(m: re.Match) -> str:
        store.append(m.group(0))
        return f"\x00{len(store) - 1}\x00"

    return _BRACKET_RE.sub(_sub, text), store


def _restore_brackets(text: str, store: list[str]) -> str:
    for i, seg in enumerate(store):
        text = text.replace(f"\x00{i}\x00", seg)
    return text


# 星号动作 → （）容器: 统一动作承载格式。
# 若放任不管, sanitize_qq_reply 会剥掉星号只留裸文本, 使动作退化成第三人称旁白。
# 仅转换含中文的 *...* (排除 *^_^* 之类颜文字)。
_STAR_ACTION_RE = re.compile(r"(?<!\*)\*([^*\n]*[一-鿿][^*\n]*)\*(?!\*)")

# 第三人称自称 → 第一人称。per-bot: 角色名由调用方传入。
_SELF_REF_VERBS = (
    "觉得|认为|感觉|知道|想|猜|表示|建议|推荐|告诉|"
    "来说|想说|回答|解释|补充|确认|提醒|问一下|说一句"
)


def _self_reference_re(char_name: str) -> re.Pattern:
    """构造第三人称自称正则。匿名时退化为通用词首兜底。"""
    name = re.escape(char_name) if char_name else r"\w+"
    return re.compile(rf"{name}({_SELF_REF_VERBS})")


# 小说式旁白 — 只保留【带第三人称主语】的模式。
# 无主语的特征碎片 (竖瞳/猫耳/鳞/白大褂/捋发…) 已解禁, 不再清理:
# 它们既是角色特征又是动作素材, 删掉会制造残句 (见 2026-10 审计)。
_GENERIC_NARRATION = [
    r"她微微(?:一)?笑",
    r"她温柔地[笑说了道]",
    r"她[轻轻]声[地道说]",
    r"她眨[了]?眨(?:眼|眼睛|眸子)",
    r"她托着腮",
    r"她[歪着][了]?头",
    r"她叹了口气",
    r"她[无奈宠溺]地[笑摇了]",
    r"他微微(?:一)?笑",
    r"他温柔地[笑说了道]",
    r"他叹了口气",
    r"他[无奈宠溺]地[笑摇了]",
    r"少女托着腮",
]


def _narration_phrases(char_name: str) -> list[str]:
    """通用第三人称旁白 + 该角色专属的「名字+动作」旁白。"""
    pats = list(_GENERIC_NARRATION)
    if char_name:
        n = re.escape(char_name)
        pats.extend([
            rf"{n}眨[了]?眨(?:眼|眼睛|竖瞳|眸子|猫瞳)",
            rf"{n}[微微温柔]?[一地]?笑",
            rf"{n}[轻轻无奈]地",
            rf"{n}[歪着][了]?头",
        ])
    return pats

# ── 表达密度控制 ──────────────────────────────────────

# 颜文字/颜表情模式 (kaomoji) — 匹配常见的日式颜文字
# 格式: (・ω・) (｡･ω･｡) (๑•̀ㅂ•́)و✧ (￣ω￣) 等
# 同时也匹配独立装饰符号: ♪ ✨ ～ 等 (LLM 惯性输出)
# ★ 含中文的括号【不是】颜文字 —— 那是动作容器（手在抖）/（停了很久）。
#   命中中文即豁免, 与括号保护机制互为双保险。
_KAOMOJI_RE = re.compile(
    r"[(（][^)）一-鿿]{2,10}[)）]"  # 括号内2-10个非中文字符的颜文字
    r"|[♪♫✨✿❀◕ω⊙≧≦◡ﾟ▽`´]{2,}"  # 连续2个以上装饰符号 (单个可能是标点)
)

# 独立颜文字 (不含文字的纯表情回复可以保留)
_STANDALONE_KAOMOJI_RE = re.compile(
    r"^[(（][^)）一-鿿]{2,10}[)）]\s*$"
)

# 硬上限: 600 字 + 8 句
# 2026-06-27: 200→600 — 工具检索报告 (>400字) 被旧上限腰斩 (TRAPS §十九)
#   markdown 表格的 \n 被当作"自然断点" → 报告在表格中截断。
#   600 字覆盖检索报告/画图描述/多工具回复的合理长度，
#   闲聊自然远低于此上限 (50-150字)，不受影响。
_HARD_CHAR_LIMIT = 600
_HARD_SENTENCE_LIMIT = 20

# ── System prompt 泄露守卫 ──────────────────────────
# system prompt 独有短语 — 这些短语绝对不会出现在正常对话中。
# LLM 上下文过载时会混淆 system prompt 和输出内容 → 把指令当自己的话说出。
# 检测到任一标记 → 从第一次命中处截断。
_SYSTEM_PROMPT_LEAK_MARKERS = [
    # ── 人格标头 & 指令段落 ──
    "追问原则",
    "非人感渗漏",
    "你不是来刷存在感的",
    "群聊铁律",
    "猫式挑逗:",
    "称呼的温度计",
    "温度由什么决定",
    "禁止用括号旁白",
    "人设方向指令",
    "[指令边界",
    "[能力诚实",
    "[图片理解铁律",
    "[你是谁]",
    "[爱莉面",
    "[侵蚀面",
    "[你的样子",
    "[你的心理状态",
    "[说话方式",
    "以上是你的全部角色设定",
    # ── thread_summary / 内部纪要格式 (这些是给系统看的, 不能对用户说) ──
    "未涉及：",        # prompt 教的 "未涉及:中国区支付方式"
    "未涉及:",         # 半角变体
    "给出了每个模型的地址",  # 元描述: LLM 描述自己做了什么, 不是给用户的结果
    "最后根据主人偏好给出选择建议",  # 同上
    "是给系统看的元数据",  # thread_summary prompt 原文
    "标签只供系统提取",   # 同上
    "不要对用户提及它",   # 同上
    "这行标签只供系统",   # 同上
    "<thread_summary>",  # ★ XML 标签泄漏兜底 — 源头应在 tools.py / group_chat.py 剥离
    "</thread_summary>",  # ★ 闭合标签同上
]


def _strip_system_prompt_leak(text: str) -> tuple[str, bool]:
    """检测并截断 LLM 回复中的 system prompt 复述。

    LLM 在上下文过载时可能混淆 system prompt 和输出内容，
    把指令文字当作自己的回复说出。此函数检测已知的 system prompt
    特有短语，在第一次命中处截断。

    Returns:
        (cleaned_text, was_cleaned)
    """
    if not text:
        return text, False

    best_pos = len(text)
    matched = ""
    for marker in _SYSTEM_PROMPT_LEAK_MARKERS:
        pos = text.find(marker)
        if pos != -1 and pos < best_pos:
            best_pos = pos
            matched = marker

    if best_pos < len(text):
        cleaned = text[:best_pos].rstrip()
        logger.info(
            "System prompt 泄露: 位置 %d 命中「%s」→ %d→%d 字",
            best_pos, matched, len(text), len(cleaned),
        )
        return cleaned, True

    return text, False


def _detect_ascii_art(text: str) -> bool:
    """检测文本是否包含 ASCII art (字符画)。

    判断标准:
    1. 包含 3 行以上含有大量非字母数字字符 (如 / \\ _ | ^ 等) 的行
    2. 这些行的字符中，非字母数字字符占比 > 60%
    """
    lines = text.split("\n")
    art_chars = set("/\\_|^~<>()[]{}#@*+=-:;,.!?")
    art_line_count = 0

    for line in lines:
        if not line.strip():
            continue
        # 计算非空白字符中，艺术字符的占比
        non_space = [c for c in line if c != " "]
        if not non_space:
            continue
        art_count = sum(1 for c in non_space if c in art_chars)
        if art_count / len(non_space) > 0.6 and len(non_space) >= 3:
            art_line_count += 1

    return art_line_count >= 3


def filter_narration(
    text: str,
    *,
    char_name: str = "",
    is_nsfw: bool = False,
) -> tuple[str, int]:
    """反臃肿后处理: 清理旁白/自称/舞台指示 + 表达密度控制。

    动作容器（）：**原样保留**。旁白清理只作用于括号外的第三人称小说腔。
    这是安全网 — system prompt 已禁止这些模式，此函数兜底清理漏网之鱼。

    Args:
        text: 待清理的回复原文
        char_name: 当前角色名 (per-bot: 第三人称自称/专属旁白按角色匹配)
        is_nsfw: 是否成人区 (保留位, 当前不改变清理策略)

    Returns:
        (cleaned_text, change_count): 清理后的文本和修改次数
    """
    if not text:
        return text, 0

    changes = 0

    # ── 0.5 ASCII art 检测: 跳过空格压缩等会破坏格式的处理 ──
    _is_ascii_art = _detect_ascii_art(text)

    # ── 0. System prompt 泄露守卫 (P0 — 优先于所有其他处理) ──
    text, was_leak = _strip_system_prompt_leak(text)
    if was_leak:
        changes += 1

    # ── 0.6 动作容器保护: （）内一字不动 ──
    #     必须在所有清理之前执行, 否则动作/神态描写会被下面的规则吃掉。
    #     ASCII art 例外: 字符画里的括号多为构图元素, 且可能跨行, 不参与保护。
    _br_store: list[str] = []
    if not _is_ascii_art:
        text, _br_store = _protect_brackets(text)

        # 星号动作 → （）容器: 统一承载格式。
        # 若不转换, sanitize_qq_reply 会剥掉星号留下裸文本, 动作退化成旁白。
        text, n = _STAR_ACTION_RE.subn(r"（\1）", text)
        changes += n

    # 1. 第三人称自称 → 第一人称 (保留语义，只改称呼)
    text, n = _self_reference_re(char_name).subn(r"我\1", text)
    changes += n

    # 2. 小说式旁白短语 (仅带第三人称主语的模式)
    #    连带吞掉紧随的顿逗, 避免删除后留下 "。，" 这类标点残渣。
    for pat in _narration_phrases(char_name):
        text, n = re.subn(pat + r"[，,、]?", "", text)
        changes += n

    # ── 3. 表达密度控制: 颜文字泛滥 ──
    # 非纯表情回复中 >2 个颜文字 → 保留前 2 个
    # ★ 必须在括号还原【之前】执行: 此刻动作容器还是占位符,
    #   不会被 _KAOMOJI_RE 误判成颜文字。
    #   （手在抖）（停了很久）（没动）这类短动作段正好落在
    #   "括号内 2-10 字符" 的陷阱里, 还原后再裁剪会把它们整段吃掉。
    if not _STANDALONE_KAOMOJI_RE.match(text):
        kaomoji_matches = list(_KAOMOJI_RE.finditer(text))
        if len(kaomoji_matches) > 2:
            # 从后往前删除多余的，只保留前 2 个
            for m in reversed(kaomoji_matches[2:]):
                text = text[:m.start()] + text[m.end():]
            changes += len(kaomoji_matches) - 2
            logger.debug("反臃肿: 裁剪 %d 个多余颜文字", len(kaomoji_matches) - 2)

    # ── 4. 还原动作容器（此后长度/句数统计按真实字数走） ──
    text = _restore_brackets(text, _br_store)

    # ── 6. 表达密度控制: 长度软裁剪 (600字) ──
    # 原则: 宁可放行偏长的回复，也不生硬截断——半句话比长回复更糟。
    # 2026-06-27: 分隔符优先级调整 — \n\n (段落) 优先于 \n (行),
    #   防止 markdown 表格内的 \n 被当作自然断点 (TRAPS §十九)。
    #   中文句末标点 (。？！) 仍是最优先的自然断点。
    _orig_len = len(text)
    if _orig_len > _HARD_CHAR_LIMIT:
        truncated = text[:_HARD_CHAR_LIMIT]
        # 在截断区内找最后一个自然句子结尾
        best_cut = -1
        for sep in ("。", "？", "！", "\n\n", "\n"):
            pos = truncated.rfind(sep)
            if pos > best_cut:
                best_cut = pos
                # \n\n 匹配后跳过后续的 \n (避免 \n 覆盖更好的段落断点)
                if sep == "\n\n":
                    break  # 段落断点优先级最高，不再检查 \n
        # 只在前40%-100%区间有自然断点时裁剪；否则放行原回复
        # (阈值从 60% 降到 40% — 给工具报告更多宽容度)
        if best_cut > _HARD_CHAR_LIMIT * 0.4:
            text = truncated[:best_cut + 1].strip()
            changes += 1
            logger.info("反臃肿: 长度软裁剪 %d→%d 字", _orig_len, len(text))
        else:
            logger.debug(
                "反臃肿: 长度超标 %d 字但无自然断点 (best_cut=%d) → 放行不截断",
                _orig_len, best_cut,
            )

    # 句数裁剪 (>8句 → 保留前8句)
    # 2026-06-27: 分隔符去掉 \n — markdown 每行一个 \n，把结构化回复切成
    #   15+ "句"（---、🔥 Title、每行链接都计数），句数上限形同虚设。
    #   单 \n 是排版/换行，不是句子边界。仅保留 。！？ 作为句子分隔。
    sentences = re.split(r"[。！？]+|\n{2,}", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) > _HARD_SENTENCE_LIMIT:
        text = "。".join(sentences[:_HARD_SENTENCE_LIMIT]) + "。"
        changes += 1
        logger.info("反臃肿: 句数裁剪 %d→%d 句", len(sentences), _HARD_SENTENCE_LIMIT)

    # 清理残留: 多余空格、连续标点、空白括号
    # ★ ASCII art 保护: 跳过空格压缩，保留字符画对齐
    if not _is_ascii_art:
        text = re.sub(r" {2,}", " ", text)
    text = re.sub(r",,+", "，", text)
    text = re.sub(r"。。+", "。", text)
    # 旁白删除后残留的混合标点连缀 (如 "。，" → "。")
    text = re.sub(r"([，。、；：！？])[，。、；：！？]+", r"\1", text)
    # 句首残留标点 (旁白移除后可能留下)
    text = re.sub(r"^[，。、；：！？\s]+", "", text)
    # 删除空的 *()（）* 残留
    text = re.sub(r"\*\s*\*", "", text)
    text = re.sub(r"[（(]\s*[）)]", "", text)

    text = text.strip()

    if changes:
        logger.debug(
            "反臃肿过滤器: %d 处修改 (%d 字)",
            changes, len(text),
        )

    return text, changes


def get_recent_bot_replies(
    messages: list[dict], char_name: str, count: int = 5,
) -> list[str]:
    """从消息列表提取最近 bot 的发言内容 (用于重复检测)。"""
    replies: list[str] = []
    bot_id = f"bot_{char_name}"
    for msg in reversed(messages):
        if str(msg.get("user_id", "")) == bot_id:
            replies.append(str(msg.get("content", "")))
            if len(replies) >= count:
                break
    replies.reverse()
    return replies


def is_duplicate(
    text: str,
    recent_replies: list[str],
    threshold: float = 0.65,
) -> bool:
    """检测回复是否与最近的 bot 发言高度重复。

    Args:
        text: 待发送的回复
        recent_replies: 最近的 bot 发言列表 (按时间升序)
        threshold: 相似度阈值 (0-1)，超过即视为重复

    Returns:
        True 如果与任何最近回复高度重复
    """
    if not text or not recent_replies:
        return False

    for prev in recent_replies[-3:]:  # 最近 3 条
        if not prev or len(prev) < 3:
            continue
        # 长度相差超过 50% 跳过 (不太可能是重复)
        len_ratio = min(len(text), len(prev)) / max(len(text), len(prev))
        if len_ratio < 0.5:
            continue
        ratio = difflib.SequenceMatcher(None, text, prev).ratio()
        if ratio >= threshold:
            logger.debug(
                "重复检测命中: ratio=%.2f vs 「%s」",
                ratio, prev[:50],
            )
            return True

    return False


# ── 碎片化发送 (拟真节奏) — 单一真相源 ─────────────────────
# 群聊与私聊共用同一套切分规则: 按句末标点切, 短碎片概率合并, 上限由调用方给
# (荷尔蒙曲线给: 起意=1 收着点 / 升温=3 / 决堤=4)。禁止在别处再写一套分段。

_FRAG_SPLIT_RE = re.compile(r"(?<=[。！？…!?；;])\s*|\n+")
_FRAG_NO_SPLIT = ("```", "[CQ:", "http://", "https://")


def fragment_reply(
    reply: str,
    *,
    max_frags: int = 3,
    merge_short_p: float = 0.8,
    merge_long_p: float = 0.4,
    target_len: int = 220,
) -> list[str]:
    """把回复切成 ≤max_frags 条碎片消息。纯函数。

    含代码块/CQ码/链接 或 过短(<16字)/超长(>600字) → 不拆, 原样单条。
    """
    import random
    text = reply.strip()
    if max_frags <= 1:
        return [text]
    if not text or len(text) < 16 or len(text) > 600:
        return [text]
    if any(k in text for k in _FRAG_NO_SPLIT):
        return [text]
    parts = [p.strip() for p in _FRAG_SPLIT_RE.split(text) if p.strip()]
    if len(parts) < 2:
        return [text]
    frags: list[str] = []
    buf = parts[0]
    for nxt in parts[1:]:
        merge_p = merge_short_p if len(buf) < 20 else merge_long_p
        if len(buf) + len(nxt) <= target_len and random.random() < merge_p:
            buf += nxt
        else:
            frags.append(buf)
            buf = nxt
        if len(frags) >= max_frags - 1:
            break
    frags.append(buf)
    while len(frags) > max_frags:
        frags[-2] += frags[-1]
        frags.pop()
    return frags or [text]


def typing_delay_ms(fragment: str) -> float:
    """模拟打字耗时: 中文 0.18s/字 + 随机抖动, 单条上限 1.8s。"""
    import random
    cjk = sum(1 for ch in fragment if "\u4e00" <= ch <= "\u9fff")
    base = 0.55 + cjk * 0.18 * random.uniform(0.7, 1.3)
    return min(base, 1.8)
