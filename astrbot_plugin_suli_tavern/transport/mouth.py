"""Mouth（嘴）— 人格渲染：plan → 进群正文。

分工：脑定"说什么"（reply_plan），嘴定"怎么说"。
只做渲染：路由判断与工具调用一律不归嘴（send_sticker 待 v2 接管）。
输入：完整 persona（调用方供给：线上走人格体系，探针读角色卡）
      + plan + 近史 + 世界书 + 亲密氛围。
输出：纯自然文本，无 JSON。超长硬截。
"""

import re as _re

_MOUTH_SUFFIX = """[嘴渲染指令 — 本段固定]
你是{bot_name}本人，用平时说话的方式回话。plan 只列要点，由你展开成自然正文：
- one_liner 一两句（100字内收尾）；short 两到四句（300字内收尾）；thread 可展开（600字内收尾）。
- 只输出正文：无 JSON、无字段名、无旁白、无 markdown、无开场白。
- 正文第一句即入正题，不解释、不复述 plan。
- 记忆诚实：手里只有近期片段时，不许断言式否认自己说过的话（"我没说过/压根没这回事"一律禁用），只能说"没翻到/不记得了"。[Recent] 里有的才算说过，没有的不认也不否认。
- 亲密寄存器（仅当本轮是亲密/NSFW时生效，日常不理）：自称只用「我」，叫对方「亲爱的」；不许自称人家，不许让对方叫你人家；用户没叫错称呼时不许纠正名字；不许编造"X分钟前/刚才"这类时间，Recent里没有就不写时间。"""

_MOUTH_CAP = 900


# ── 脉络沉淀指令 (2026-10-15 开)：只进动态段，不碰静态缓存 ──
# 嘴是用户可见正文的唯一产口，episodic 的货源就指着它每轮带一条脉络句。
# 提取由调用方经 _extract_thread_summary 落槽（工具链同款），此处只负责让模型输出标签。
_MOUTH_TS_SUFFIX = """[脉络沉淀 — 附加在最后一行]
正文写完后，另起一行附加 <thread_summary>...</thread_summary>：一句话（≤60字）说明本轮你为用户做了什么，第一人称「我」，像人话不要系统腔。
铁律：标签是附加的——只写标签没有正文是严重 bug，每条回复必须有用户可见的正文。"""


def render_mouth_static(persona_text: str, bot_name: str = "") -> str:
    """静态段 = 完整 persona + 渲染指令后缀。同 bot 字节稳定，可进前缀缓存。"""
    return (
        str(persona_text or "").strip()
        + "\n\n"
        + _MOUTH_SUFFIX.replace("{bot_name}", str(bot_name or ""))
    )


def build_mouth_user_block(
    *,
    speech_act: str = "",
    key_points: list[str] | None = None,
    length_hint: str = "short",
    tone_hint: str = "",
    trigger_text: str = "",
    recent_lines: list[str] | None = None,
    identity_line: str = "",
    wb_block: str = "",
    hormone_line: str = "",
    want_thread_summary: bool = False,
) -> str:
    """组装嘴动态段：plan 要点 + 触发 + 近史 + 背景。与脑解耦，只收扁平字段。"""
    points = [str(x).strip() for x in (key_points or []) if str(x).strip()][:3]
    recent = list(recent_lines) if recent_lines else []
    parts = [
        "[嘴输入] 以下 plan 是终审结论（说什么），由你展开成人格正文，只输出正文。",
        f"speech_act={speech_act or 'chat'} length_hint={length_hint or 'short'} "
        f"tone_hint={tone_hint.strip() or '自然'}",
    ]
    if points:
        parts.append("要点：\n" + "\n".join(f"{i}. {_p}" for i, _p in enumerate(points, 1)))
    if trigger_text.strip():
        parts.append(f"[触发] {trigger_text.strip()[:200]}")
    if identity_line.strip():
        parts.append(identity_line.strip())
    if wb_block.strip():
        parts.append(wb_block.strip())
    if hormone_line.strip():
        parts.append(hormone_line.strip())
    if recent:
        parts.append("[Recent]\n" + "\n".join(recent))
    else:
        parts.append("[Recent]\n(空)")
    if want_thread_summary:
        parts.append(_MOUTH_TS_SUFFIX)
    return "\n".join(parts)


def parse_mouth(raw: str, cap: int = _MOUTH_CAP) -> str:
    """嘴输出清洗：去围栏/去引号/拒 JSON/静默标记/硬截断。空结果返回 ""（调用方兜底）。"""
    _t = str(raw or "").strip()
    if "```" in _t:
        _m = _re.search(r"```(?:json|text)?\s*(.*?)\s*```", _t, _re.DOTALL)
        if _m:
            _t = _m.group(1).strip()
    _t = _t.strip().strip('"').strip("'").strip("「」").strip("【】").strip()
    if _t in ("", "(空回复)", "[静默]"):
        return ""
    if _t.startswith("{"):
        return ""
    if len(_t) > cap:
        return _t[:cap]
    return _t


_FU_CAP = 2
_FU_LEN = 200


def parse_follow_ups(raw: object) -> list[str]:
    """连发补充解析：行首 + 优先，无标记行备选，JSON 与空行丢弃，最多 2 条各 200 字。"""
    _marked: list[str] = []
    _plain: list[str] = []
    for _ln in str(raw or "").split("\n"):
        _s = _ln.strip()
        if _s == "" or _s.startswith(("{", "[")):
            continue
        if _s.startswith("+"):
            _s = _s.strip("+").strip().strip('"').strip("'").strip()
            if _s != "":
                _marked.append(_s[:_FU_LEN])
        else:
            _plain.append(_s[:_FU_LEN])
    return (_marked + _plain)[:_FU_CAP]
