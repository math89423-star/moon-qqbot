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
    """清理 LLM 回复中的 Markdown 标记（QQ 不渲染）。

    即使 system prompt 已禁止 Markdown，LLM 偶尔仍会输出，
    此函数作为最后防线在发送前强制清理。

    清理内容:
      - **粗体** / *斜体* / ***粗斜体*** → 纯文本
      - # 标题 → 纯文本
      - `代码` / ```代码块``` → 纯文本
      - [引用的消息内容: ...] 假引用格式 (LLM 从输入中学来的)
      - 多余空行 (>1连续) → 单个换行
      - 行首行尾空格
    """
    if not text:
        return text

    # 0. 清理 LLM 输出的假引用格式 — LLM 从输入 context 中学了
    #    "[引用的消息内容]" 这个标注，在输出中也模仿写出。
    #    这不是真正的 QQ 引用，只是纯文本，用户点不了。
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


# ── @提及转换: 纯文本 @name → QQ [CQ:at,qq=...] ───────────

# @name 模式: @后跟非空白/非标点的名字 (1-20字)
_AT_NAME_RE = re.compile(
    r'@([^\s'
    r'，。！？、：；""''（）()'    # 中英文标点
    r'\[\]【】'                    # 方括号 (避免匹配 CQ 码)
    r'\n@'                         # 换行 + @ 自身
    r']{1,20})'
)

# peer bot 的常见昵称 — 用于将 @peer_bot 等映射到 peer_bot_qq
_PEER_BOT_NAMES: tuple[str, ...] = ()  # 由调用方从 BotIdentityService 动态获取


def resolve_at_mentions(
    text: str,
    ctx_messages: list[dict],
    bot_name: str = "暮恩",
    peer_bot_qq: str = "",
    peer_bot_names: tuple[str, ...] = _PEER_BOT_NAMES,
    trigger_uid: str = "",
    trigger_name: str = "",
) -> str:
    """将 LLM 输出中的 @昵称 纯文本转换为 QQ [CQ:at,qq=...] CQ码。

    LLM 在 prompt 中被要求用 @名字 标明说话对象, 但输出的 @名字
    只是纯文本, 在 QQ 中不会触发真正的 @提醒。此函数从最近上下文
    建立 name→QQ 映射, 将 @name 替换为真实的 CQ @码。

    处理逻辑:
      1. 从 ctx_messages 提取 name→qq 映射 (最近消息优先)
      2. 显式添加 trigger_uid→trigger_name 映射 (确保触发者名字被找到)
      3. 扫描文本中的 @name 模式, 最长匹配优先
      4. 查询映射: @bot自己 → 跳过, @peer → peer_bot_qq, @群友 → QQ号
      5. ★防幻觉: 解析出的 QQ 必须在最近消息参与者集合中出现过
      6. ★限量: 最多 3 个 @提及 (防止 LLM 滥 @)
      7. 无法识别的名字保留原文 (避免破坏非 @ 语境)

    ★ 2026-06-27 防幻觉守卫:
      - 构建 recent_participant_qqs 集合 (最近 80 条消息中实际发言的 QQ)
      - 解析出的 @QQ 若不在参与者集合中 → 跳过 (LLM 可能幻觉了不存在的用户)
      - 例外: trigger_uid 和 peer_bot_qq 始终允许 (前者是真实触发者, 后者是已知对照 bot)
    """
    if not text or "@" not in text:
        return text

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

    for m in _AT_NAME_RE.finditer(text):
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

            # 用 [CQ:at,qq=...] + 空格替换 @name
            # 注意: CQ 码后面不跟原名字, 避免 QQ 渲染后名字重复
            replacements.append((m.start(), m.end(), f"[CQ:at,qq={qq}] "))

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

# 第三人称自称 → 第一人称 (由调用方根据角色卡注入 bot_name 前缀模式)
# 匹配 "[任意名字]觉得/认为/感觉..." 等第三人称自称，替换为"我觉得/认为..."
# 调用方应通过 filter_narration 的 bot_name 参数注入角色名模式
_SELF_REFERENCE_RE = re.compile(
    r"\w+(觉得|认为|感觉|知道|想|猜|表示|建议|推荐|告诉|"
    r"来说|想说|回答|解释|补充|确认|提醒|问一下|说一句)"
)

# 长动作描述 *...* (内容 >3 字)
_LONG_ACTION_RE = re.compile(r"\*[^*]{3,}\*")

# 小说式旁白 — 这些短语在 QQ 聊天中绝对不会出现
# 注意: 用 (?:a|b) 而非 [ab] 匹配多字符替代，避免部分匹配残留碎片
_NARRATION_PHRASES = [
    r"她微微(?:一)?笑",
    r"她温柔地[笑说了道]",
    r"她[轻轻]声[地道说]",
    r"她眨[了]?眨(?:眼|眼睛|竖瞳|眸子)",
    r"她托着腮",
    r"她[歪着][了]?头",
    r"她叹了口气",
    r"她[无奈宠溺]地[笑摇了]",
    r"\w+眨[了]?眨(?:眼|眼睛|竖瞳|眸子)",
    r"\w+[微微温柔]?[一地]?笑",
    r"\w+[轻轻无奈]地",
    r"少女托着腮",
    r"翡翠绿的眸[子了]",
    r"竖瞳微微(?:一)?(?:收|闪)?缩",
    r"蛇瞳微微(?:一)?(?:收|闪|缩)?缩",
    r"光核微微(?:闪|发)?烁",
    r"白大褂[的衣角袖口]",
    r"[捋理][了]?[捋理]?(?:及腰|长|绿)?发",
]

# 舞台指示括号 — 中文括号里的描述性文字，不是真实聊天用括
_STAGE_DIRECTION_RE = re.compile(
    r"[（(]\s*(?:大笑|认真|思考|温柔|轻声|叹气|无奈|苦笑|"
    r"微笑|惊讶|疑惑|恍然|沉默|小声|偷偷|暗暗|"
    r"认真地说|温柔地说|轻声说|小声说|无奈地笑|"
    r"若有所思|恍然大悟|一本正经)\s*[）)]"
)

# ── 表达密度控制 ──────────────────────────────────────

# 颜文字/颜表情模式 (kaomoji) — 匹配常见的日式颜文字
# 格式: (・ω・) (｡･ω･｡) (๑•̀ㅂ•́)و✧ (￣ω￣) 等
# 同时也匹配独立装饰符号: ♪ ✨ ～ 等 (LLM 惯性输出)
_KAOMOJI_RE = re.compile(
    r"[(（][^)）]{2,10}[)）]"  # 括号内2-10个字符的颜文字
    r"|[♪♫✨✿❀◕ω⊙≧≦◡ﾟ▽`´]{2,}"  # 连续2个以上装饰符号 (单个可能是标点)
)

# 独立颜文字 (不含文字的纯表情回复可以保留)
_STANDALONE_KAOMOJI_RE = re.compile(
    r"^[(（][^)）]{2,10}[)）]\s*$"
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


def filter_narration(text: str) -> tuple[str, int]:
    """反臃肿后处理: 清理 LLM 回复中的旁白/自称/舞台指示 + 表达密度控制。

    这是安全网 — system prompt 已禁止这些模式，
    此函数兜底清理漏网之鱼。

    Returns:
        (cleaned_text, change_count): 清理后的文本和修改次数
    """
    if not text:
        return text, 0

    changes = 0

    # ── 0. System prompt 泄露守卫 (P0 — 优先于所有其他处理) ──
    text, was_leak = _strip_system_prompt_leak(text)
    if was_leak:
        changes += 1

    # 1. 第三人称自称 → 第一人称 (保留语义，只改称呼)
    text, n = _SELF_REFERENCE_RE.subn(r"我\1", text)
    changes += n

    # 2. 长动作描述 *...*
    text, n = _LONG_ACTION_RE.subn("", text)
    changes += n

    # 3. 小说式旁白短语
    for pat in _NARRATION_PHRASES:
        text, n = re.subn(pat, "", text)
        changes += n

    # 4. 舞台指示括号
    text, n = _STAGE_DIRECTION_RE.subn("", text)
    changes += n

    # ── 5. 表达密度控制: 颜文字泛滥 ──
    # 非纯表情回复中 >2 个颜文字 → 保留前 2 个
    if not _STANDALONE_KAOMOJI_RE.match(text):
        kaomoji_matches = list(_KAOMOJI_RE.finditer(text))
        if len(kaomoji_matches) > 2:
            # 从后往前删除多余的，只保留前 2 个
            for m in reversed(kaomoji_matches[2:]):
                text = text[:m.start()] + text[m.end():]
            changes += len(kaomoji_matches) - 2
            logger.debug("反臃肿: 裁剪 %d 个多余颜文字", len(kaomoji_matches) - 2)

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
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r",,+", "，", text)
    text = re.sub(r"。。+", "。", text)
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
