"""承诺守卫 — 「说到必须做到」确定性兜底。

背景 (2026-10-12 放鸽子事故):
  Main 判 REPLY_ONLY 但 immediate_reply 里写「这就去给你查/画」→
  系统收不到任务 (TaskLedger 无记录, Dispatcher 全程无日志) → 用户白等。
  绘图已有硬兜底 (brain_live 绘图路由兜底), 但搜/查/找/
  写等其他动词没有同款保护。

本模块是纯函数层 (零 LLM, 零 I/O), 供 transport/brain_live
在返回终审体前调一次 apply_promise_guard。transport 只做 action 映射
(升级 / 改写), 所有正则与决策关在这里。

精度优先于召回: 只认「第一人称 + 未来动作 + 任务动词」同现, 「我要睡觉/
吃饭/出门」这类自我陈述、完成态 (已经查到/画好了)、否定 (没查到/画不了)
一律不命中, 宁可漏网 (走原回复) 不可误伤 (造垃圾任务)。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ── 未来承诺: 承诺架 (A) + 任务动词 (B) ──────────────────────────
# A+B 同现才算承诺。单有 A (如光一句「等我一下」) 也算 — 它本身就是后文预告。
_PROMISE_RES: tuple[re.Pattern[str], ...] = (
    # 这就/马上/立刻 + 去来帮给
    re.compile(r"(这就|马上|立刻|现在就|待会就)(去|来|帮你|给你|替你|开)"),
    # 我去/我来/我这就去 + (给你/帮你) + 任务动词
    re.compile(r"我(去|来|这就去|马上就去)(给你|帮你|替你)?(查|搜|找|画|写|翻|问|跑|算|做|整|核|确认|看|试|弄|发)"),
    # (帮你/给你/替你) + 任务动词 — 主语省略式承诺
    re.compile(r"(帮你|给你|替你)(查|搜|找|画|写|翻|问|跑|算|做|整|核实|确认|看|试)"),
    # 等我/稍等 + (一下/片刻) — 纯后文预告, 无动词也算
    re.compile(r"(等我|稍等|稍候|等一下|等下)(一下|下下|一会|会儿|片刻)?"),
    # 包在我身上 / 交给我 / 我来 + 任务动词
    re.compile(r"(包在我身上|交给我(吧|就行|就好)?|我来(查|搜|找|画|写|翻|问|搞|弄))"),
    # 我要 + (去)(给你/帮你) + 任务动词 — 「我要去给你查一下」
    re.compile(r"我要(去)?(给你|帮你|替你)?(查|搜|找|画|写|翻|问|跑|算)"),
    # (待会/一会) + (给你/帮你/就) + 任务动词
    re.compile(r"(待会|待会儿|一会|一会儿)(给你|帮你|就)(查|搜|找|画|写|发)"),
    # 未来态收尾: 马上好/马上来/马上发/这就来
    re.compile(r"(马上|这就)(好|来|发|到|回你)"),
)

# ── 排除: 形似承诺但不是 (必须在 _PROMISE_RES 之前检查) ──────────
_EXCLUDE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"等下[次回个]"),  # 「等下次再说」— 下次，不是后文
)

# ── 反例: 命中承诺但同时含这些 → 不是承诺 (完成态/否定/自我陈述) ──
_COMPLETION_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(已经|早就|刚.*完|查到|找到|画好|写好|发了|发过|做完|搜到)"),
    re.compile(r"(没|不|无法|查不到|找不到|画不了|做不到|发不出)"),
    re.compile(r"(我要(睡觉|吃饭|出门|洗澡|上课|上班|下班|休息|睡了))"),
)

# ── 改写: 动词感知 (拒绝回填病句) ───────────────────────────────
# 查询类动词 → 「先X一下」(我去查 → 我先查一下)；
# 创作类动词 → 「先构思一下」(这就去画 → 先构思一下)；
# 纯拖延词 → 删除或弱化为「行」。
# 其余动词 (做/整/弄/跑/算/发…) 不做字面替换 —— 走后面的分句丢弃兜底。
_SEARCH_VERBS = "查搜找翻看问"
_CREATE_VERBS = "画写"

_SUB_SEARCH = (  # (查|搜|…) 前的不同承诺架, 统一收成「先X一下」
    # 注: 尾巴吞掉可选的「一下」, 否则「我去查一下」会变成「我先查一下一下」
    re.compile(r"(?:这就去(?:给你)?|我去(?:给你)?|我来|我要(?:去)?(?:给你)?|马上(?:去|来)?(?:给你|帮你)?)(查|搜|找|翻|看|问)(?:一下)?"),
)
_SUB_CREATE = (
    re.compile(r"(?:这就去(?:给你)?|我去(?:给你)?|我来|马上(?:去)?(?:给你)?)(画|写)(?:一张|一个|一幅|一下)?"),
)
_SUB_STALL = (  # 纯拖延，无实质动作
    re.compile(r"(等我|稍等|稍候)(一下|下下|一会|会儿|片刻)?[，,]?"),
    re.compile(r"(马上|这就)(好|来|发|到|回你)[，,]?"),
    re.compile(r"包在我身上[，,]?"),
    re.compile(r"交给我(吧|就行|就好)?[，,]?"),
)

_CLAUSE_SPLIT_RE = re.compile(r"([，。！？!?\n]+)")


def contains_future_promise(text: str) -> tuple[bool, str]:
    """回复文本是否含未来承诺措辞。

    Returns:
        (hit, matched): hit=True 表示「说了后文」; matched 为命中的原文片段
        (供日志, 最长 30 字)。
    """
    s = str(text or "").strip()
    if not s:
        return False, ""
    if any(r.search(s) for r in _EXCLUDE_RES):
        return False, ""
    if any(r.search(s) for r in _COMPLETION_RES):
        return False, ""
    for r in _PROMISE_RES:
        m = r.search(s)
        if m:
            return True, m.group(0)[:30]
    return False, ""


def _drop_promise_clauses(text: str) -> str:
    """丢弃含承诺的分句, 保留其余部分。分句间按中文标点切分。

    例: 「行，我这就去给你跑一下」→「行」。全丢光返回 "" (调用方用兜底话术)。
    """
    parts = _CLAUSE_SPLIT_RE.split(str(text or ""))
    kept: list[str] = []
    for part in parts:
        if not part or _CLAUSE_SPLIT_RE.fullmatch(part):
            continue
        hit, _ = contains_future_promise(part)
        if not hit and part.strip():
            kept.append(part.strip())
    out = "，".join(kept)
    out = re.sub(r"[，,]{2,}", "，", out).strip()
    return re.sub(r"^[，,、；：\s]+", "", out)


def sanitize_promise_reply(text: str) -> str:
    """无任务可派时把承诺改写成中性承接, 不让空头支票进群。

    两步走: ① 动词感知替换 (查询→先X一下 / 创作→先构思一下 / 拖延→删);
    ② 仍含承诺的分句整体丢弃。保证输出要么无承诺, 要么 "" (调用方用兜底话术)。
    不引入「任务/派发/系统」等工程词。
    """
    s = str(text or "")
    if not s:
        return s
    out = s
    for r in _SUB_SEARCH:
        out = r.sub(lambda m: f"先{m.group(1)}一下", out)
    for r in _SUB_CREATE:
        out = r.sub("先构思一下", out)
    for r in _SUB_STALL:
        out = r.sub("", out)
    out = re.sub(r"[，,]{2,}", "，", out).strip()
    out = re.sub(r"^[，,、；：\s]+", "", out)
    hit, _ = contains_future_promise(out)
    if hit:
        out = _drop_promise_clauses(out)
    return out


def should_upgrade_to_delegate(batch_text: str) -> tuple[bool, str]:
    """承诺 + batch 任务味浓 → 是否强制升级 REPLY_AND_DELEGATE。

    只看 batch (用户说了什么), 不看 reply (bot 说了什么):
    reply 含任务词反而是污染源 (「我去给你查个好玩的」也能推出搜索族),
    但 objective 用的是 batch 原文 —— batch 无任务味时升级只会造垃圾任务。

    Returns:
        (upgrade, reason): reason 供日志 (inferred=xxx / task_verb=xxx / draw)。
    """
    batch = str(batch_text or "")
    # 2026-10-18 重开单事故：batch 里可能带 [已完成/待确认生图单] 系统注
    # （自带「生图」词）→ 关键词网误判任务味 → 强制升级派单。剥注后再判。
    try:
        from .draw_confirm import strip_draw_notes as _sdn_pg

        batch = _sdn_pg(batch)
    except Exception:
        pass
    if not batch.strip():
        return False, ""
    try:
        from ..intelligence.tool_registry import infer_task_tools as _itt
        from ..intelligence.tool_registry import looks_like_draw_request as _is_draw
    except Exception:
        try:
            from astrbot_plugin_suli_tavern.intelligence.tool_registry import infer_task_tools as _itt
            from astrbot_plugin_suli_tavern.intelligence.tool_registry import looks_like_draw_request as _is_draw
        except Exception:
            _itt = None  # type: ignore[assignment]
            _is_draw = None  # type: ignore[assignment]
    if _itt is not None:
        try:
            _inferred = set(_itt(batch))
            if _inferred:
                return True, f"inferred={sorted(_inferred)[:3]}"
        except Exception:
            pass
    if _is_draw is not None:
        try:
            if _is_draw(batch):
                return True, "draw"
        except Exception:
            pass
    # 通用任务动词网: batch 里明确要做事 (兜住 infer 表之外的 搜/查/算/跑)
    if re.search(r"(搜|查|找|写|跑|算|翻|核实|确认|总结|整理|对比|列出)", batch):
        # 但 batch 只是闲聊感叹 (无动作对象) 不算: 「查什么查」这类顶嘴
        if re.search(r"(帮我|给我|替我|查一下|搜一下|找一下|总结|整理|列出|跑一下|算一下)", batch):
            return True, "task_verb"
    return False, ""


def apply_promise_guard(
    *,
    response_mode: str,
    immediate_reply: str,
    follow_ups: list[str],
    batch_text: str,
) -> tuple[str, str, list[str], str]:
    """承诺守卫编排 (transport 层的唯一入口)。

    transport 只做 action 映射, 检测/决策/改写全在这里:

    - response_mode 非 REPLY_ONLY → ("none", 原样返回)
    - 无承诺 → ("none", 原样返回)
    - 承诺 + batch 任务味浓 → ("upgrade", 原样返回, info 供日志)
    - 承诺 + 无任务味 → ("sanitize", 改写后 reply/follow_ups, info 供日志);
      改写掏空 ("" ) 时原样返回 "" —— 调用方用角色化兜底话术补上,
      本模块不碰角色名 (service 层不拥有 persona)。

    Returns:
        (action, fixed_reply, fixed_follow_ups, info)。
    """
    imm = str(immediate_reply or "")
    fus = [str(f) for f in (follow_ups or [])]
    if response_mode != "REPLY_ONLY":
        return "none", imm, fus, ""
    hit, phrase = contains_future_promise(imm + " " + " ".join(fus))
    if not hit:
        return "none", imm, fus, ""
    up, reason = should_upgrade_to_delegate(batch_text)
    if up:
        return "upgrade", imm, fus, f"{reason} phrase={phrase}"
    fixed = sanitize_promise_reply(imm)
    fixed_fus = []
    for f in fus:
        ff = sanitize_promise_reply(f)
        if ff.strip() and ff.strip() not in ("，", ",", "。"):
            fixed_fus.append(ff)
    return "sanitize", fixed, fixed_fus, f"phrase={phrase}"
