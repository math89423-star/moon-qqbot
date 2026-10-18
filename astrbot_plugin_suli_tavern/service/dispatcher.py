"""Async Dispatcher + Runtime Pool (P0-3).

- Dispatcher.enqueue(brief) → TaskLedger 记录 + 投 Worker Pool
- Runtime Pool: N Worker Process 各 one active task, cancel=kill+replace
- Ack 400ms grace: dispatch 后 400ms 再发 ack，若 250ms 内完成则 cancel ack
- TaskResult → TaskResultEvent 投回 Actor

P0-2 阶段仅提供 enqueue 接口，P0-3 补齐 Pool 实现。
当前实现为 simp pool：线程池模拟 worker process（后续替换为多子进程）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass

from astrbot.api import logger

from ..transport.task_events import TaskResultEvent
from .task_ledger import ledger_call


# ── 生图 Phase-1 spec 死合同（正文在 draw_confirm，dispatcher 重导出供单测/旧引用）──
# NSFW 变体 (2026-10-16 拒单事故): 中性 system + 露骨 objective → 模型拒出规格,
# 用户侧却看到「你没说画什么」。zone 已验时换 _DRAW_SPEC_SYSTEM_NSFW。
from ..service.draw_confirm import _DRAW_SPEC_SYSTEM  # noqa: E402,F401
from ..service.draw_confirm import _DRAW_SPEC_SYSTEM_NSFW
# 2026-10-18 重开单事故：生图单系统注（[已完成/待确认生图单]）自带「生图」词，
# 关键词判定一律先剥注 —— 否则没要图的短消息会被推断出绘图意图。
from ..service.draw_confirm import strip_draw_notes as _sdn  # noqa: E402


_TOOL_TEXT_KEYS = ("result", "output", "content", "text", "observation", "summary")


def _events_had_tool(events: object, keyword: str) -> bool:
    """事件流里是否出现过某工具调用 (找图兜底判定用, 纯函数)。
    扫 tool/name/type/kind 四个键, 大小写不敏感。"""
    try:
        kw = str(keyword or "").strip().lower()
        if not kw:
            return False
        for _e in (events or []):
            if not isinstance(_e, dict):
                continue
            _hay = (f"{_e.get('tool', '')} {_e.get('name', '')} "
                    f"{_e.get('type', '')} {_e.get('kind', '')}").lower()
            if kw in _hay:
                return True
    except Exception:
        pass
    return False


def _collect_tool_texts(events: object) -> list[str]:
    """从全部事件里捞工具结果正文（超时打捞用）。

    2026-10-14 鸣潮乐子事故：agent 空转 50 步超时，搜索结果全在前面的事件里，
    打捞只看最后 10 个（全是 deliberation）→ 交白卷。只收结果字段，不收调用参数。
    """
    out: list[str] = []
    try:
        for ev in (events or [])[:200]:
            if not isinstance(ev, dict):
                continue
            _t = str(ev.get("type", ev.get("kind", "")) or "")
            _name = str(ev.get("name", ev.get("tool", "")) or "")
            _is_tool = "tool" in _t.lower() or "tool" in _name.lower()
            _has_result_key = any(
                isinstance(ev.get(_k), str) and str(ev.get(_k)).strip()
                for _k in ("result", "output", "observation")
            )
            if not (_is_tool or _has_result_key):
                continue
            for _k in _TOOL_TEXT_KEYS:
                _v = ev.get(_k)
                if isinstance(_v, str) and _v.strip():
                    out.append(_v.strip()[:500])
                    break
    except Exception:
        pass
    return out


def classify_task_result(http_result: dict | None, objective: str = "") -> dict:
    """诚实分类执行结果 — 传输成功 ≠ 业务成功。

    返回 {status, summary, facts, evidence, uncertainties, error_code, error}。
    error 为原始失败明细（供二次人格化如实上报，禁止编造替代结论）。
    - ok=false / finish_reason=error / error 非空 / 空文本无证据 → failed/partical，
      绝不合成 "已完成"。
    - 仅 ok 真值 + 正常结束 + 非空文本/证据 → completed。
    - 自由文本拆行只做展示切分，不伪造 facts 证据。
    """
    _obj = (objective or "").strip() or "任务"
    if not isinstance(http_result, dict):
        return {
            "status": "failed",
            "summary": f"{_obj}未能执行（无可用执行服务），请稍后重试。",
            "facts": (),
            "evidence": (),
            "uncertainties": ("执行服务无响应",),
            "error_code": "INFRASTRUCTURE",
            "error": "执行服务无响应",
        }
    _err = http_result.get("error")
    _ok = http_result.get("ok", True)
    _finish = str(http_result.get("finish_reason") or "")
    _text = str(http_result.get("text") or "").strip()
    # 证据：仅保留工具相关事件原文
    _evidence: list[str] = []
    try:
        for ev in (http_result.get("events", []) or [])[-10:]:
            if isinstance(ev, dict):
                _t = str(ev.get("type", ev.get("kind", "")))
                if "tool" in _t.lower():
                    _evidence.append(str(ev)[:300])
    except Exception:
        pass
    _failed_finish = _finish.lower() in ("error", "failed", "timeout", "cancelled")
    # 超时但有产出 (文本/工具证据/工具结果正文) → partial 有多少回多少，不判死。
    # 工具结果扫全量事件：空转超时时结果全在前面，只看最后 10 个等于交白卷。
    _tool_texts = _collect_tool_texts(http_result.get("events")) if _finish.lower() == "timeout" else []
    if _finish.lower() == "timeout" and (_text or _evidence or _tool_texts):
        _detail = str(_err)[:200] if _err else "执行超时"
        if _text:
            _summary = _text[:2000]
        elif _tool_texts:
            _joined = "\n".join(_tool_texts)[:2000]
            _summary = f"{_obj}超时，先把已查到的给你：\n{_joined}"
        else:
            _summary = f"{_obj}超时，仅收回 {len(_evidence)} 条工具证据。"
        return {
            "status": "partial",
            "summary": _summary,
            "facts": tuple(s.strip() for s in _summary.split("\n") if s.strip())[:10],
            "evidence": tuple(_evidence[:3]),
            "uncertainties": (_detail,),
            "error_code": "TIMEOUT_PARTIAL",
            "error": _detail,
        }
    if _err or _ok is False or _failed_finish:
        _detail = str(_err)[:200] if _err else f"执行结束状态异常（finish={_finish or 'unknown'}）"
        return {
            "status": "failed",
            "summary": f"{_obj}执行失败：{_detail}。",
            "facts": (),
            "evidence": tuple(_evidence[:3]),
            "uncertainties": (_detail,),
            "error_code": "EXECUTION_FAILED",
            "error": _detail,
        }
    if not _text and not _evidence:
        return {
            "status": "failed",
            "summary": f"{_obj}没有返回可用结果（空响应），请稍后重试。",
            "facts": (),
            "evidence": (),
            "uncertainties": ("空响应",),
            "error_code": "EMPTY_RESULT",
            "error": "执行器返回空响应（无文本、无工具证据）",
        }
    _summary = _text[:2000] if _text else f"{_obj}返回了部分工具证据，但无文本结论。"
    _facts = tuple(s.strip() for s in _summary.split("\n") if s.strip())[:10]
    return {
        "status": "completed",
        "summary": _summary,
        "facts": _facts,
        "evidence": tuple(_evidence[:3]),
        "uncertainties": (),
        "error_code": None,
        "error": "",
    }


# ── Phase-1 spec 工具选择 (2026-10-12 生图确认链事故) ──────────
# 事故: _tool_one = sorted(_narrow)[0] 按字母序选, narrow={generate,edit}
# 时恒定选中 edit_image ('e' < 'g') —— 纯文字请求也被 stage 成以图生图单,
# 用户确认后 Phase-2 直执 edit_image, 无底图 →「没找到可改的参考图」。
# (实测:「帮我画一张…二创图」→ allow=[edit,generate] → pending=edit →
# confirmed=True → Phase-2 edit 失败, 连走两次。)
# 规则: edit 必须有底图证据才选, 否则一律 generate (文生图是默认)。
# 单候选时尊重 Gate 原判 (附图改图等走 agent/工具路补图, 不在这里动)。
_EDIT_HINT_WORDS = (
    "重绘", "以图生图", "img2img", "i2i", "换装", "换衣", "换衣服",
    "改图", "编辑图片", "改一下", "改成", "照着改",
)
_PURE_TEXT_WORDS = (
    "纯文字", "直接画", "不用参考", "不要参考", "无参考", "凭空画",
    "文字生图", "纯文生图", "文本生图",
)
_IMG_REF_WORDS = (
    "这张", "这幅", "那张", "那幅", "附件", "发的图", "发的照片",
    "引用", "上张", "刚才那张", "刚那张", "原图",
)


def _pick_spec_tool(
    narrow, *, objective: str = "", facts=None,
    bot_id: str = "", requester_id: str = "",
) -> str:
    """Phase-1 复述单的工具选择。双候选时按证据选, 不按字母序。"""
    cands = set(narrow or ()) & {"generate_image", "edit_image"}
    if len(cands) == 1:
        return next(iter(cands))
    if not cands:
        return "generate_image"
    text = str(objective or "") + "\n" + "\n".join(
        str(x or "") for x in (facts or [])
    )
    if any(w in text for w in _PURE_TEXT_WORDS):
        return "generate_image"
    if any(w in text for w in _EDIT_HINT_WORDS):
        try:
            from ..intelligence.tools import get_last_draw_image as _last_img
            if _last_img(bot_id, requester_id):
                return "edit_image"
        except Exception:
            pass
        if any(w in text for w in _IMG_REF_WORDS):
            return "edit_image"
        return "generate_image"
    return "generate_image"


def _parse_draw_spec(text: str) -> dict | None:
    """DrawSpec JSON 提取（统一表单）。

    返回完整 JSON dict（含 prompt/prompt_en/orientation/size + theme/character_*/style/composition_shot/lighting/background/missing）。
    旧 4 字段仍必填；新槽缺失视为“未填”，由宿主补全/核验，不在这里编数。
    调用方一律经 `fill_spec_from_json` 落表，不直接读本函数返回值拼单。
    """
    import json as _js
    import re as _re

    s = str(text or "").strip()
    if not s:
        logger.warning("spec 解析失败: 空文本 out_len=0")
        return None
    _m = _re.search(r"```(?:json)?\s*(\{.*\})\s*```", s, _re.DOTALL)
    _cand = _m.group(1) if _m else s
    if not _cand.lstrip().startswith("{"):
        _i, _j = _cand.find("{"), _cand.rfind("}")
        if _i < 0 or _j <= _i:
            logger.warning("spec 解析失败: 无JSON花括号 raw=%.800s", s)
            return None
        _cand = _cand[_i:_j + 1]
    try:
        _d = _js.loads(_cand)
    except Exception as _je:
        logger.warning("spec 解析失败: JSON报错 %s raw=%.800s", str(_je)[:200], s)
        return None
    if not isinstance(_d, dict):
        logger.warning("spec 解析失败: 非dict raw=%.800s", s)
        return None
    _p = str(_d.get("prompt", "") or "").strip()
    _pe = str(_d.get("prompt_en", "") or "").strip()
    _o = str(_d.get("orientation", "") or "").strip().lower()
    if not _p or not _pe:
        logger.warning(
            "spec 解析失败: 缺prompt字段 keys=%s raw=%.800s",
            list(_d.keys())[:10], s,
        )
        return None
    _o = "landscape" if _o in ("landscape", "horizontal", "wide", "横图", "横") else "portrait"
    _d["prompt"] = _p
    _d["prompt_en"] = _pe
    _d["orientation"] = _o
    if "size" not in _d:
        _d["size"] = ""
    return _d


def _spec_tuple_from_dict(d: dict) -> tuple:
    """兼容垫片：旧 4 元组解包 (prompt, prompt_en, orientation, size_raw)。新代码优先直接读 dict。”"""
    return (
        str(d.get("prompt", "") or ""),
        str(d.get("prompt_en", "") or ""),
        str(d.get("orientation", "") or "portrait"),
        str(d.get("size", "") or ""),
    )


# 同模型 400 拒收过的档位（参数性拒绝，非瞬时故障）：本进程内永久跳过，
# 每笔 spec 省掉必败的试探调用。网关放开新档时随进程重启自动清空重探。
_EFFORT_400_BLOCK: dict[str, set] = {}


async def _spec_via_vlm(
    bot_id: str, content: str, effort0: str, tokens: int, temp: float, timeout_s: int,
    *, nsfw: bool = False,
) -> tuple | None:
    """LLM 推理位直调：chat 槽走裸调档位链，spark/responses 槽走 tavern 双协议。失败回 None。"""
    def _is_resp(slot) -> bool:
        if slot is None:
            return False
        _b = str(slot.normalized_base_url or "")
        return "/responses" in _b or "muse-spark" in str(slot.model_name or "").lower()

    try:
        from .bot_config import get_config_service as _gcs3

        _svc = _gcs3()
        # 推理位优先，顺延 lite/primary；协议不过滤，分支里再按槽类型走。
        _slot = None
        _slot_name = "-"
        for _cand in ("llm_pro", "llm_lite", "llm_primary"):
            _cand_slot = None
            with contextlib.suppress(Exception):
                _cand_slot = _svc.resolve_llm_slot(bot_id, _cand, nsfw=nsfw)
            if _cand_slot is not None and _cand_slot.api_key:
                _slot = _cand_slot
                _slot_name = _cand
                break
        if _slot is None:
            logger.debug("直接 spec 无可用推理槽 bot=%s", bot_id)
            return None
        _base = str(_slot.normalized_base_url or "")
        _sys = _DRAW_SPEC_SYSTEM_NSFW if nsfw else _DRAW_SPEC_SYSTEM
        _msgs = [
            {"role": "system", "content": _sys},
            {"role": "user", "content": content},
        ]
        # ── spark/responses 槽：走 tavern（双协议 + reasoning.effort 翻译）──
        if _is_resp(_slot):
            from .tavern_client import TavernClient as _TC
            _tc = _TC()
            try:
                _order_r = ["minimal", "low", "medium", "high", "xhigh", "max"]
                if str(effort0) in ("none", "off"):
                    _chain_r: list = ["minimal"]
                elif effort0 in _order_r:
                    _chain_r = [e for e in reversed(_order_r) if _order_r.index(e) <= _order_r.index(effort0)]
                else:
                    _chain_r = ["max", "xhigh", "high", "medium", "low", "minimal"]
                _text = ""
                _landed = "-"
                for _eff in _chain_r:
                    try:
                        _txt = await _tc.chat(
                            messages=_msgs,
                            model=_slot.model_name,
                            temperature=temp,
                            max_tokens=tokens,
                            api_base=_base,
                            api_key=_slot.api_key,
                            bot_id=bot_id,
                            extra_params={"reasoning_effort": _eff},
                            slot=f"{_slot_name}:spec",
                        )
                    except Exception as _ee:
                        logger.debug("spec responses effort=%s 失败 %s", _eff, str(_ee)[:120])
                        continue
                    if _txt.strip() and _txt.strip() != "(空回复)":
                        _text = _txt
                        _landed = str(_eff)
                        break
                logger.info(
                    "spec 直调落档 route=llm_pro slot=%s effort=%s out_len=%d raw=%.1000s",
                    _slot_name, _landed, len(_text), _text,
                )
                return _parse_draw_spec(_text)
            finally:
                with contextlib.suppress(Exception):
                    await _tc.close()
        from openai import AsyncOpenAI

        _cli = AsyncOpenAI(api_key=_slot.api_key, base_url=_base.rstrip("/"), timeout=timeout_s)
        _heads = {
            "User-Agent": "moon-qqbot/1.0",
            "x-opencode-session": "moon-qqbot-vlm",
        }
        _order = ["minimal", "low", "medium", "high", "xhigh", "max"]
        # ── 真关思考 (2026-10-17): off/none 此前打滑 —— none→无参裸调 (longcat 仍思考开),
        # off→掉进 max 链 (越关越猛)。现单发走 opencode 方言:
        # longcat→thinking.disabled, opencode 其余→reasoning_effort=none, 其他基底裸调。
        if str(effort0) in ("none", "off"):
            _off_body: dict = {}
            try:
                from .tavern_client import _apply_opencode_thinking_dialect as _dialect_off
                _off_body = _dialect_off(_base, None, _slot.model_name) or {}
            except Exception:
                _off_body = {}
            _text = ""
            _landed = "-"
            _off_tries: list[dict] = ([{"extra_body": _off_body}] if _off_body else []) + [{}]
            for _try_body in _off_tries:
                try:
                    _resp = await _cli.chat.completions.create(
                        model=_slot.model_name,
                        messages=_msgs,
                        max_tokens=tokens,
                        temperature=temp,
                        extra_headers=_heads,
                        **_try_body,
                    )
                    with contextlib.suppress(Exception):
                        _text = _resp.choices[0].message.content or ""
                    if _text.strip():
                        _landed = str(effort0) if _try_body else "bare"
                        break
                except Exception as _oe:
                    logger.debug("spec 关思考尝试(%s)失败: %s",
                                 "方言" if _try_body else "裸调", str(_oe)[:120])
            logger.info(
                "spec 直调落档 route=llm_pro slot=%s effort=%s out_len=%d raw=%.1000s",
                _slot_name, _landed, len(_text), _text,
            )
            return _parse_draw_spec(_text)
        elif effort0 in _order:
            _chain = [e for e in reversed(_order) if _order.index(e) <= _order.index(effort0)]
        else:
            _chain = ["max", "xhigh", "high", "medium", "low"]
        # 末尾兜底一次无参裸调：部分模型（如 mimo 系）完全拒收 reasoning_effort，
        # 有档全灭时裸调是最后活路（实测 mimo-v2.6-flash 有档全 400、裸调 200）。
        if None not in _chain:
            _chain.append(None)
        _text = ""
        _landed = "-"
        _blocked = _EFFORT_400_BLOCK.get(str(_slot.model_name), set())
        for _eff in _chain:
            if _eff in _blocked:
                logger.debug("spec effort=%s 已知被拒，跳过", _eff)
                continue
            try:
                _kw: dict = {}
                if _eff is not None:
                    _kw["reasoning_effort"] = _eff
                _resp = await _cli.chat.completions.create(
                    model=_slot.model_name,
                    messages=_msgs,
                    max_tokens=tokens,
                    temperature=temp,
                    extra_headers=_heads,
                    **_kw,
                )
            except Exception as _ee:
                # 400 = 参数性拒绝（该模型无此档），记黑名单；超时/网络等瞬时故障不记
                if getattr(_ee, "status_code", None) == 400:
                    _EFFORT_400_BLOCK.setdefault(str(_slot.model_name), set()).add(_eff)
                logger.debug("spec effort=%s 失败 %s", _eff, str(_ee)[:120])
                continue
            with contextlib.suppress(Exception):
                _text = _resp.choices[0].message.content or ""
            if _text.strip():
                _landed = str(_eff or "none")
                break
        logger.info(
            "spec 直调落档 route=llm_pro slot=%s effort=%s out_len=%d raw=%.1000s",
            _slot_name, _landed, len(_text), _text,
        )
        return _parse_draw_spec(_text)
    except Exception:
        logger.debug("直接 spec 失败 bot=%s", bot_id, exc_info=True)
        return None


# ── 自主配图单场景守卫 (2026-10-14)：同一场景 TTL 窗内只画 1 张 ──
# key 与 NSFW 场景锁同口径 (bot + 群/私聊 + 人)；窗口取 scene_ttl（默认 30min）。
# 登记在放行时（执行前）：失败也占窗，防失败循环烧后端；窗过自动解。
_AUTO_ILLUST_TS: dict[str, float] = {}


def _auto_scene_guard(bot_id: str, group_str: str, requester_id: str, *, private: bool) -> bool:
    """单场景 1 张守卫：True=本窗可画（已登记），False=本窗已画过。"""
    try:
        from ..service.nsfw_zone import scene_ttl as _sttl

        _win = float(_sttl())
    except Exception:
        _win = 1800.0
    if _win <= 0:
        _win = 1800.0
    _side = "p" if private else "g"
    _key = f"{bot_id}:{_side}:{group_str or requester_id}:{requester_id}"
    _now = time.time()
    if _now - _AUTO_ILLUST_TS.get(_key, 0.0) < _win:
        return False
    _AUTO_ILLUST_TS[_key] = _now
    return True


async def _auto_illustrate_run(scheduler, bot_id: str, requester_id: str, group_str: str,
                               prompt_cn: str, facts: list, *, private: bool) -> dict:
    """自主配图执行：后端分发 → spec 直算 → 预设顶入 → transient 单 → 直执。

    不 stage 落单（不污染确认流）。返回执行结果 dict。
    """
    try:
        from ..intelligence.tools import _resolve_draw_backend as _res_bk2
        _prov, _nc, _pc = _res_bk2(bot_id, requester_id, group_str, private=private)
    except Exception:
        return {"ok": False, "finish_reason": "error", "text": "",
                "events": [], "error": "后端分发异常"}
    if _prov == "novelai":
        if not ((_nc.get("token") or "").strip()):
            return {"ok": False, "finish_reason": "error", "text": "",
                    "events": [], "error": "NovelAI 未配置"}
        _panel = _nc.get("size") or "832x1216"
        _model = _nc.get("model") or "nai-diffusion-5-full"
    elif _prov == "pixai":
        if not ((_pc.get("key") or "").strip()):
            return {"ok": False, "finish_reason": "error", "text": "",
                    "events": [], "error": "PixAI 未配置"}
        _panel = f"{_pc.get('aspect') or '9:16'} {_pc.get('size') or '1k'}"
        _model = _pc.get("model") or "1983308862240288769"
    else:
        return {"ok": False, "finish_reason": "error", "text": "",
                "events": [], "error": "无可用绘图后端"}
    try:
        _char = scheduler._resolve_character(bot_id) or {}
        _cname = str((_char.get("name") or "") if isinstance(_char, dict) else "")
    except Exception:
        _cname = ""
    try:
        from ..service.draw_confirm import get_chara_draw_preset as _preset_fn
        _pre = _preset_fn(_cname) or {}
    except Exception:
        _pre = {}
    _objective = f"NSFW自主配图（{_cname or '本机角色'}）：{prompt_cn}"[:200]
    # 本函数只在 zone 三验通过后调用 (见 dispatch 内 _auto_zone) → 恒为成人向。
    _spec = await _direct_draw_spec(bot_id, _objective, facts or [], nsfw=True)
    if not isinstance(_spec, dict):
        return {"ok": False, "finish_reason": "error", "text": "",
                "events": [], "error": "spec 直算失败"}
    _en = str(_spec.get("prompt_en") or _spec.get("prompt") or "")
    # 外观统一交给 apply_local_char_preset（与用户点名下单路同一函数, 原则 24）。
    # 旧写法是这里手写前缀拼接 —— 只有自主路有兜底, 用户点名那一路没有, 于是
    # 主人点「画小洛」时被上游编成了猫娘 (2026-10-16 自画错事故)。
    _pre_feat = str((_pre or {}).get("features") or "")
    _so = str(_spec.get("orientation") or "portrait")
    if _so not in ("portrait", "landscape"):
        _so = "portrait"
    try:
        from ..intelligence.tools import _orient_of_size as _oof_a
        from ..intelligence.tools import _resolve_draw_size as _rsz_a
        _ss, _cl = _rsz_a(_prov, _so, _panel, str(_spec.get("size") or ""))
        _so = _oof_a(_prov, _ss, _so)
    except Exception:
        _ss = ""
    if not _ss:
        _ss = _panel
    try:
        from ..service.draw_confirm import DrawSpec as _DSpec
        _view = _DSpec(
            prompt=str(_spec.get("prompt") or _objective), prompt_en=_en,
            orientation=_so, size_label=_ss, model=_model, provider=_prov,
            tool="generate_image", image_url="",
            identity_status="original",
            character_name=str((_pre or {}).get("name") or _cname),
            character_features=_pre_feat,
        )
    except Exception as _e:
        return {"ok": False, "finish_reason": "error", "text": "",
                "events": [], "error": f"表单组装失败：{_e}"[:200]}
    try:
        from ..service.draw_confirm import apply_local_char_preset as _preset_fix_a
        _pre_stripped = _preset_fix_a(_view)
        if _pre_stripped:
            logger.info("Dispatcher 自主配图外观收口 name=%s 剔除=[%s]",
                        _view.character_name, _pre_stripped)
    except Exception:
        logger.debug("自主配图外观收口跳过", exc_info=True)
    return await _phase2_direct_draw(bot_id, requester_id, _view, _objective, nsfw_bypass=True)


def _fetch_task_raw_text(scheduler, bot_id, group_id, origin_message_id, requester_id) -> str:
    """取触发本次任务的用户原文（ground truth）。

    先按 origin_message_id 精确匹配，找不到回退到该触发用户最近一条。
    读不到回空串（调用方 fail-open，不因此误杀正常单）。
    """
    try:
        _ctx = scheduler._contexts.get(
            scheduler._make_ctx_key(bot_id, int(group_id)))
    except Exception:
        return ""
    if _ctx is None:
        return ""
    try:
        _oid = str(origin_message_id or "")
        _msgs = getattr(_ctx, "messages", None) or []
        if _oid:
            for _m in reversed(_msgs[-30:]):
                if not isinstance(_m, dict):
                    continue
                if (str(_m.get("message_id", "") or "") == _oid
                        or str(_m.get("event_id", "") or "") == _oid):
                    return str(_m.get("content", "") or "")
        for _m in reversed(_msgs[-30:]):
            if not isinstance(_m, dict):
                continue
            if str(_m.get("user_id", "") or "") == str(requester_id or ""):
                return str(_m.get("content", "") or "")
    except Exception:
        return ""
    return ""


# ── 绘图意图硬门 (2026-10-16 误开单事故) ──────────────────────────
# 事故: 主人收到图后回「不错不错」→ Gate/Main 仍把它裹成一张生图任务
# (objective=「不错不错」, allow=[generate_image, edit_image]) → spec 按合同返回空 prompt
# → 解析 None → 用户收到一句「这单的构图我没接住，再把画面点一句」，
# 而图早在半分钟前就发完了。
# ★ 注意不要在 dispatcher 里另造一套意图网: 宽口径的"要不要画"由
#   tool_registry.looks_like_draw_request 管(放行用), 这里只做**否决**——
#   纯夸赞/纯附和的那几个字不构成绘图请求。铁律 7: 触发原文没有绘图意图、
#   又没有待确认单时, 不许调 spec、不许出声。
def _is_plain_chatter(task_brief: dict) -> bool:
    """触发原文是不是纯夸赞/附和（→ 不该开单，也不该出任何绘图话术）。"""
    try:
        from .draw_confirm import looks_like_plain_praise as _praise

        _t = _sdn(str(task_brief.get("trigger_text", "") or ""))
        _o = _sdn(str(task_brief.get("objective", "") or ""))
        return bool(_praise(_t) or _praise(_o))
    except Exception:
        return False  # fail-open: 判不出来就走旧行为，不因新门丢单


def _draw_ask_subject_text(scheduler, bot_id, *, failed: bool = False) -> str:
    """生图缺主体反问候选（定稿话术）。

    对用户只说要什么、不报内部失败（spec 空/构思卡住是内部细节，不泄）。
    缺什么就问什么：人物/场景/风格说一个就行。

    ★ failed=True 是 2026-10-16 拒单事故补的分支：原来的兜底把「构思那一步没成」
    也说成「你没说画什么」—— 用户明明把画面描述得很细，读起来像在装傻。
    两种情形共用一句话术是错的：没给主体 → 问主体；构思没成 → 认下这单没接住。
    """
    try:
        _name = str(scheduler._resolve_character(bot_id).get("name", ""))
    except Exception:
        _name = ""
    if "暮恩" in _name:
        if failed:
            return ("还没画——这单的构图我没接住。"
                    "你把画面再点一句：谁、在哪、什么姿势。点完我重开一单。")
        return ("还没画——你只说了画一张图，没说画什么。"
                "人物/场景/风格说一个，我给你开单。")
    if failed:
        return "还没画——这单的构图我没接住。把画面再点一句，我重开一单。"
    return ("还没画——你还没说画什么。"
            "人物/场景/风格说一个就行，说完我就开画。")


async def _direct_draw_spec(bot_id: str, objective: str, facts: list,
                            *, nsfw: bool = False) -> dict | None:
    """确定性直调：走 LLM 推理位 (llm_pro) 槽位模型 + 槽位档位。

    绘图页独立 spec 参数已退役；配置全部来自 get_draw_spec(bot_id)。
    按档位链自动降档。失败回 None。返回统一表单 JSON dict（见 _parse_draw_spec）。

    nsfw=True（调用方已验 zone）时换成人向 system，否则中性 system 会让
    模型对着露骨 objective 直接拒答（2026-10-16 事故）。
    """
    # 2026-10-18 重开单事故：objective 里若混着生图单系统注（自带「生图/知更鸟」
    # 等词），先剥掉再进圣经先行与 spec 生成，否则身份会被误挂到别人身上。
    objective = _sdn(objective)
    try:
        from .bot_config import get_config_service

        _cfg = get_config_service().get_draw_spec(bot_id)
    except Exception:
        return None
    _effort0 = str(_cfg.get("effort", "high") or "high")
    try:
        _tokens = int(_cfg.get("tokens", 4096) or 4096)
    except (TypeError, ValueError):
        _tokens = 4096
    try:
        _temp = float(_cfg.get("temperature", 0.7) or 0.7)
    except (TypeError, ValueError):
        _temp = 0.7
    try:
        _timeout = int(_cfg.get("timeout", 90) or 90)
    except (TypeError, ValueError):
        _timeout = 90
    _content = "任务目标：" + (objective or "")
    _fl = [str(x).strip() for x in (facts or []) if str(x).strip()][:5]
    if _fl:
        _content += "\n背景事实：\n" + "\n".join(f"- {x}" for x in _fl)
    # 圣经先行 (2026-10-17): 热门角色本地秒查 — spec 出生就带对身份,
    # 不靠事后补顶 (爱弥斯案: spec LLM 把 爱弥斯 猜成 Aimisi, 搜画全错)。
    try:
        from .character_bible import lookup as _bible_spec
        _sbhit = _bible_spec(f"{objective or ''}\n" + "\n".join(_fl))
        if _sbhit and str(_sbhit.get("danbooru") or "").strip():
            _sbtag = str(_sbhit["danbooru"]).strip()
            _sbft = str(_sbhit.get("features") or "").strip()
            _content += (
                f"\n[身份已核验] 本地圣经命中：{str(_sbhit.get('name') or '').strip() or '该角色'} → {_sbtag}"
                + (f"（官设特征：{_sbft}）" if _sbft else "")
                + "。prompt_en 必须以该 danbooru tag 起手（原样，含下划线/括号），"
                  "character_danbooru 填该 tag，character_features 用上述官设特征，"
                  "身份已定不必再推断。"
            )
            logger.info("spec 圣经先行: %s → %s", str(objective or "")[:40], _sbtag)
    except Exception:
        logger.debug("spec 圣经先行跳过", exc_info=True)
    # 先行 tavern 专线（经 group_chat 注册的 provider，tavern_client 同时吃
    # chat 与 responses 两协议；推理位是 spark 时只有这条路能走）。
    try:
        from ..intelligence.tools import run_spec_via_tavern as _via_tavern
        _text = await _via_tavern(bot_id, _content, _effort0, _tokens, _temp, nsfw=nsfw)
    except Exception:
        _text = ""
    _obj = _parse_draw_spec(_text)
    if _obj is not None:
        logger.info("spec 直调落档 route=llm_pro slot=tavern effort=%s nsfw=%s", _effort0, nsfw)
        return _obj
    # 回落裸调（chat 协议槽；spark 槽会被 chat_ok 跳过，见 _spec_via_vlm）
    return await _spec_via_vlm(bot_id, _content, _effort0, _tokens, _temp, _timeout, nsfw=nsfw)


async def _phase2_direct_draw(bot_id: str, requester_id: str, pending_spec, objective: str,
                               *, nsfw_bypass: bool = False) -> dict:
    """Phase-2 直执：已确认复述单 → 直接调 generate_image/edit_image 执行器。

    不经 agent（mimo 线屡现空聊/180s 卡死，must 只是 prompt 无强制力），
    不烧 LLM。图字节走 _pending_images 侧通道（调用方 _flush_draw_images
    直发 QQ）。返回执行结果 dict，直接喂 classify_task_result。

    统一表单硬门 (2026-10-13)：表单未填满一律拦截，不调执行器、不扣费。
    """
    try:
        from ..service.draw_confirm import require_ready_to_draw as _req_ready
        _ok, _reason = _req_ready(pending_spec)
        if not _ok:
            logger.warning("Dispatcher Phase-2 被表单门拦截 task requester=%s reason=%s",
                           str(requester_id)[:8], _reason[:120])
            return {"ok": False, "finish_reason": "error", "text": _reason,
                    "events": [], "error": _reason[:200]}
    except Exception:
        pass
    # 中央顶 tag (2026-10-16 昔涟事故): 圣经已定但 prompt_en 是 LLM 直写,
    # tag 常丢失 → 画错人/错发色。stage 已顶过, 这里是直单/老单的第二道。
    try:
        from ..service.draw_confirm import ensure_prompt_head as _ensure_head2
        _ensure_head2(pending_spec)
    except Exception:
        pass
    _tool = str(getattr(pending_spec, "tool", "") or "generate_image")
    if _tool not in ("generate_image", "edit_image"):
        _tool = "generate_image"
    _prompt = str(
        getattr(pending_spec, "prompt_en", "")
        or getattr(pending_spec, "prompt", "")
        or objective
        or ""
    )
    _ori = str(getattr(pending_spec, "orientation", "") or "portrait")
    _size = str(getattr(pending_spec, "size_label", "") or "")
    _img = str(getattr(pending_spec, "image_url", "") or "")
    if _tool == "edit_image" and not _img:
        # 纠偏 (2026-10-12): 被错 stage 的纯文字单 —— 无底图、无上次成图。
        # 直接调 edit 必回「没找到可改的参考图」。转文生图, 并把单子改写成
        # generate, 否则确认门里 spec.tool 对不上又会打回重述。
        _has_base = False
        try:
            from ..intelligence.tools import get_last_draw_image as _last_img
            _has_base = bool(_last_img(bot_id, requester_id))
        except Exception:
            _has_base = False
        if not _has_base:
            _tool = "generate_image"
            try:
                from ..service.draw_confirm import DrawSpec as _DS2
                from ..service.draw_confirm import stage_order as _stage2
                _stage2(bot_id, requester_id, _DS2(
                    prompt=str(getattr(pending_spec, "prompt", "") or ""),
                    prompt_en=str(getattr(pending_spec, "prompt_en", "") or ""),
                    orientation=_ori, size_label=_size,
                    model=str(getattr(pending_spec, "model", "") or ""),
                    provider=str(getattr(pending_spec, "provider", "") or ""),
                    tool="generate_image", image_url="",
                ), restages=int(getattr(pending_spec, "restages", 0) or 0))
            except Exception:
                pass
            logger.info("Dispatcher Phase-2 工具纠偏 edit_image→generate_image (无底图, 纯文字单)")
    try:
        from ..intelligence.tools import execute_edit_image as _edit_ex
        from ..intelligence.tools import execute_generate_image as _gen_ex
        _ex = _gen_ex if _tool == "generate_image" else _edit_ex
        _tctx = {"bot_id": bot_id, "trigger_user_id": requester_id}
        if nsfw_bypass:
            # 自主配图（NSFW）：dispatcher 已三验 zone，执行器侧跳过每日限额+用量记录。
            _tctx["nsfw_zone_verified"] = True
        _text = await _ex(
            {"prompt": _prompt, "orientation": _ori, "size": _size,
             "image_url": _img, "confirmed": True,
             "use_pixiv_refs": bool(getattr(pending_spec, "use_pixiv_refs", False))},
            _tctx,
        )
    except Exception as _de:
        logger.warning("Dispatcher Phase-2 直执异常 tool=%s %s", _tool, _de)
        return {"ok": False, "finish_reason": "error", "text": "",
                "events": [], "error": str(_de)[:200]}
    _text_s = str(_text or "").strip()
    if _text_s.startswith("❌"):
        return {"ok": False, "finish_reason": "error", "text": _text_s,
                "events": [], "error": _text_s[:200]}
    logger.info("Dispatcher Phase-2 直执 tool=%s out_len=%d", _tool, len(_text_s))
    return {
        "ok": True, "finish_reason": "completed",
        "text": _text_s[:2000] or f"{_tool} 执行完成",
        "events": [{"type": "tool_call", "name": _tool, "result": "ok"}],
        "error": "",
    }


# ── 轻重分流：纯本地确定性工具直执，不进 agent 循环 ──
# 轻：知识库检索 / 转发明细 / 角色身份核验 —— 确定性执行器，秒级回。
# 重：联网搜索 / 生图 / VLM 识图 / 代码 / 创作 —— 走本地执行器多轮。
_LIGHT_LOCAL_TOOLS = frozenset({"search_knowledge", "parse_forwarded_message", "resolve_character_identity"})


def is_light_local_task(narrow: object, pending_spec: object = None) -> bool:
    """纯判定：收紧集非空、全员本地工具、无待确认生图单 → 本地直执。"""
    if isinstance(narrow, (set, frozenset)) and narrow and pending_spec is None:
        return set(narrow) <= _LIGHT_LOCAL_TOOLS
    return False


def light_tool_params(name: str, objective: str) -> dict:
    """轻任务执行器入参：检索/核验类取 objective 作 query，转发明细无参查缓存。"""
    if name == "parse_forwarded_message":
        return {}
    return {"query": objective[:200]}


# SUBAGENT tier：轻集合 + 单发型外部检索。pixiv/video 因直发图片与特殊入参暂留重档。
_SUBAGENT_TOOLS = frozenset(set(_LIGHT_LOCAL_TOOLS) | {"web_search"})


def classify_task_weight(narrow: object, pending_spec: object = None) -> str:
    """任务定性：light（本地直执）/ subagent（单发委派）/ heavy（本地执行器多轮）。

    - 待确认生图单由既有 Phase-1/Phase-2 分支处理，这里一律按 heavy 走原路。
    - narrow 非空全员轻工具 → light。
    - narrow 落在 SUBAGENT 集且含重成员 → subagent。
    - 其余（空集/多族混合/生图/代码/创作）→ heavy。
    """
    if is_light_local_task(narrow, pending_spec):
        return "light"
    if isinstance(narrow, (set, frozenset)) and narrow and pending_spec is None:
        s = set(narrow)
        if s <= _SUBAGENT_TOOLS and len(s - _LIGHT_LOCAL_TOOLS) > 0:
            return "subagent"
    return "heavy"


def build_subagent_subtasks(narrow: object, objective: str) -> list[dict]:
    """SUBAGENT tier 子任务表：family 路由 + 入参，顺序确定（按工具名字序）。

    family 缺映射的一律按 retrieval 走原生执行器（本 tier 成员均为单发型检索类）。
    """
    from ..transport.subagent import family_of as _fam_of
    if isinstance(narrow, (set, frozenset)) and narrow:
        names = sorted(narrow)
    else:
        return []
    out: list[dict] = []
    for i, name in enumerate(names):
        fam = _fam_of(name)
        if fam == "generic":
            fam = "retrieval"
        out.append({
            "id": f"sub-{i}",
            "family": fam,
            "tool": name,
            "task": objective[:200],
            "args": light_tool_params(name, objective),
        })
    return out


@dataclass
class DispatchHandle:
    task_id: str
    ack_task: asyncio.Task | None = None
    worker_task: asyncio.Task | None = None
    queue_time: float = 0.0  # D10: 入队到真正执行（拿到槽位）的等待


# 事件键去重：同 (bot,群,触发人,源消息,目标摘要) 30 秒内只开一单，防回声与连击重复入队。
_dispatch_dedup_ts: dict[str, float] = {}
_DISPATCH_DEDUP_WINDOW = 30.0


def _dispatch_event_key(bot_id: object, group_id: object, requester_id: object,
                        origin_message_id: object, objective: object) -> str:
    import hashlib as _hl
    _obj = str(objective or "")[:200]
    _digest = _hl.sha1(_obj.encode("utf-8")).hexdigest()[:12]
    return f"{bot_id}:{group_id}:{requester_id}:{origin_message_id}:{_digest}"


async def _ledger_update_retry(ledger: object, task_id: str, status: str,
                               artifacts: list | None = None,
                               result_ref: str | None = None) -> object | None:
    """账本写重试一次：首次失败睡 0.2 秒再试，仍失败上抛由调用方记失败。"""
    try:
        return await ledger_call(ledger, "update_status", task_id, status, artifacts, result_ref)
    except Exception:
        await asyncio.sleep(0.2)
        return await ledger_call(ledger, "update_status", task_id, status, artifacts, result_ref)


# 超时暂离：同 (bot,群,人) 连续超时达标转暂离，成功一次清零。
_timeout_strikes: dict[str, int] = {}
_away_until: dict[str, float] = {}
AWAY_AFTER_TIMEOUTS = 2
AWAY_COOLDOWN = 600.0


def _timeout_key(bot_id: object, group_id: object, requester_id: object) -> str:
    return f"{bot_id}:{group_id}:{requester_id}"


def _note_timeout_finish(bot_id: object, group_id: object, requester_id: object,
                         finish: object) -> bool:
    """记录一次任务收尾：超时记一次，其余清零；连续达标返回暂离 True。"""
    import time as _t
    key = _timeout_key(bot_id, group_id, requester_id)
    if str(finish or "").lower() == "timeout":
        _timeout_strikes[key] = int(_timeout_strikes.get(key, 0)) + 1
    else:
        _timeout_strikes[key] = 0
        _away_until.pop(key, None)
        return False
    if int(_timeout_strikes.get(key, 0)) >= AWAY_AFTER_TIMEOUTS:
        _away_until[key] = _t.time() + AWAY_COOLDOWN
        return True
    return False


class Dispatcher:
    # D10: 有界执行槽 - 与 blueprint 初始建议 2-4 一致，默认 4。
    MAX_CONCURRENT_TASKS = 4

    def __init__(self, actor_registry, task_ledger, scheduler) -> None:
        self._actor_registry = actor_registry
        self._ledger = task_ledger
        self._scheduler = scheduler
        self._exec_sem = asyncio.Semaphore(type(self).MAX_CONCURRENT_TASKS)
        self._handles: dict[str, DispatchHandle] = {}
        # D09: task_id → origin 原始事件（用于结果引用 origin 而非 last_event），有界。
        self._origin_events: dict[str, object] = {}

    def _stash_origin_event(self, task_id: str, origin_event: object | None) -> None:
        if origin_event is None:
            return
        self._origin_events[task_id] = origin_event
        if len(self._origin_events) > 200:
            _old = next(iter(self._origin_events))
            del self._origin_events[_old]

    async def _resolve_session_id(
        self, task_id: str, task_brief: dict, bot_id: str, group_id: str, requester_id: str,
    ) -> str:
        """D12: continuation_of 合法时恢复同一任务会话，否则新会话。

        合法 = 引用存在 + 同 bot/group/请求者。非法引用拒绝并记日志，
        绝不跨用户/跨群恢复会话。
        """
        _cont = task_brief.get("continuation_of")
        _cont = str(_cont).strip() if isinstance(_cont, str) else ""
        if not _cont:
            return f"task-{task_id}"
        try:
            prev = await ledger_call(self._ledger, "get", _cont)
        except Exception:
            prev = None
        if (prev is not None and prev.bot_id == bot_id
                and str(prev.group_id) == str(group_id)
                and prev.requester_id == requester_id):
            logger.info("Dispatcher 续办 task=%s continuation_of=%s 会话恢复", task_id, _cont)
            return f"task-{prev.task_id}"
        logger.warning("Dispatcher 非法 continuation_of task=%s ref=%s 已拒绝（新会话）", task_id, _cont[:40])
        return f"task-{task_id}"

    async def dispatch(
        self, task_brief: dict, bot_id: str, group_id: str, requester_id: str,
        origin_message_id: str, immediate_reply: str, origin_event: object | None = None,
    ) -> str:
        """入队并调度，返回 task_id。"""
        # Peer 触发硬门（2026-10-15 抢单案）：bot 账号（含自己）永远不能当任务触发者。
        # 一条 @主 bot 的消息曾被双方 dispatcher 同时入队，peer 的嘲讽又触发了第三单，
        # 三单全灭还刷屏。context 旁观可以，开单不行。
        try:
            from astrbot_plugin_suli_guards.dual_bot import get_known_bot_ids as _kb_ids
            _bot_qqs = {str(x).strip() for x in (_kb_ids() or []) if str(x or "").strip()}
        except Exception:
            _bot_qqs = set()
        if str(requester_id or "").strip() and str(requester_id).strip() in _bot_qqs:
            logger.warning(
                "Dispatcher 拒收 peer/bot 触发 task bot=%s requester=%s objective=%s",
                bot_id, requester_id, str((task_brief or {}).get("objective", ""))[:60],
            )
            return ""
        _dkey = _dispatch_event_key(bot_id, group_id, requester_id, origin_message_id,
                                    (task_brief or {}).get("objective", ""))
        _now = time.time()
        if _now - _dispatch_dedup_ts.get(_dkey, 0.0) < _DISPATCH_DEDUP_WINDOW:
            logger.info("Dispatcher 事件键去重 task drop key=%s", _dkey)
            return ""
        _dispatch_dedup_ts[_dkey] = _now
        if len(_dispatch_dedup_ts) > 500:
            _old = next(iter(_dispatch_dedup_ts))
            del _dispatch_dedup_ts[_old]
        rec = await ledger_call(self._ledger, "create", bot_id, group_id, requester_id, origin_message_id, task_brief)
        task_id = rec.task_id
        self._stash_origin_event(task_id, origin_event)
        session_id = await self._resolve_session_id(task_id, task_brief, bot_id, str(group_id), requester_id)
        # D10: 排队期间标记 queued（不提前标 running），拿到槽位后才转 running。
        try:
            await ledger_call(self._ledger, "update_status", task_id, "queued")
        except Exception:
            await ledger_call(self._ledger, "update_status", task_id, "running")
        logger.info("Dispatcher 入队 task=%s group=%s brief=%s", task_id, group_id, task_brief.get("objective", "")[:60])

        # ── NSFW 主动绘图 (自主配图) 不发 ack (2026-10-17 操作者裁定) ──
        # 亲密戏里"记下来就去画/这就去画"这类过渡话术全在断戏；图照出，只闭嘴。
        # 点单路 (用户明确要画) 不受影响：那里 ack 是应答，不是画外音。
        try:
            from .draw_confirm import draw_ack_suppressed as _ack_off
            if _ack_off(auto_illustrate=bool((task_brief or {}).get("auto_illustrate"))):
                if immediate_reply:
                    logger.info("Dispatcher ack 抑制 (NSFW 自主配图不发 ack) task=%s", task_id)
                immediate_reply = ""
        except Exception:
            logger.debug("ack 抑制判定异常 (保持原行为)", exc_info=True)

        # Ack 400ms grace（仅 DELEGATE_ONLY 有 immediate_reply）
        ack_task = None
        if immediate_reply:
            async def _ack():
                await asyncio.sleep(0.4)
                # 250ms 内已完成则 cancel（由 worker 提前 cancel）
                if rec.status in ("completed", "failed", "partial"):
                    return
                # outbox 幂等：同任务 ack 只占一位，重放即去重
                try:
                    from .send_outbox import claim as _ob_claim, mark_sent as _ob_sent
                    from .send_outbox import idempotency_key as _ob_key
                    _obk = _ob_key("ack", bot_id, group_id, task_id)
                    if not _ob_claim(_obk):
                        logger.info("Dispatcher ack 去重 task=%s", task_id)
                        return
                except Exception:
                    _obk = ""
                try:
                    ctx = self._scheduler._contexts.get(self._scheduler._make_ctx_key(bot_id, int(group_id)))
                    if ctx and ctx.last_bot and ctx.last_event and immediate_reply:
                        await ctx.last_bot.send(ctx.last_event, immediate_reply)
                        try:
                            if _obk:
                                _ob_sent(_obk)
                        except Exception:
                            pass
                        logger.info("Dispatcher ack 已发 task=%s", task_id)
                except Exception:
                    pass

            ack_task = asyncio.create_task(_ack())
        self._handles[task_id] = DispatchHandle(task_id=task_id, ack_task=ack_task)

        # 投 worker — P0-3 真后台任务（单 worker 异步，不阻塞 Actor）
        async def _worker():
            t0 = time.time()
            # D10: 有界执行槽 — 排队等待计入 queue_time，拿到槽位才转 running。
            # 准入段在外层 try 之前：取消直接 return，不抛异常穿任务链。
            _slot_held = False
            _queued_at = time.time()
            await self._exec_sem.acquire()
            _slot_held = True
            _queue_time = time.time() - _queued_at
            h = self._handles.get(task_id)
            if h is not None:
                h.queue_time = _queue_time
            _admit_cancelled = False
            with contextlib.suppress(Exception):
                _pre = await ledger_call(self._ledger, "get", task_id)
                _admit_cancelled = bool(_pre is not None and _pre.status == "cancelled")
            if _admit_cancelled:
                # 排队期间被取消 → 不启动执行，直接释放槽位返回。
                with contextlib.suppress(ValueError):
                    self._exec_sem.release()
                return
            # receipt 先行：落单即记运行回执，写失败重试一次，仍失败记失败不覆盖
            _receipt = {"kind": "receipt", "task_id": task_id,
                        "objective": str((task_brief or {}).get("objective", ""))[:120],
                        "ts": time.time()}
            try:
                await _ledger_update_retry(self._ledger, task_id, "running", [_receipt], None)
            except Exception:
                with contextlib.suppress(Exception):
                    await ledger_call(self._ledger, "update_status", task_id, "failed")
                with contextlib.suppress(ValueError):
                    self._exec_sem.release()
                return
            if _queue_time > 1.0:
                logger.info("Dispatcher 排队 task=%s queue_time=%.1fs", task_id, _queue_time)
            try:
                # 构造 subagent prompt（裁定：TaskBrief 由 Main 生成，执行器只做执行）
                prompt_parts = [f"任务目标：{task_brief.get('objective','')}"]
                if task_brief.get("context_facts"):
                    prompt_parts.append("背景事实：\n" + "\n".join(f"- {x}" for x in task_brief["context_facts"][:5]))
                if task_brief.get("constraints"):
                    prompt_parts.append("约束：\n" + "\n".join(f"- {x}" for x in task_brief["constraints"][:5]))
                if task_brief.get("required_outputs"):
                    prompt_parts.append("需要产出：\n" + "\n".join(f"- {x}" for x in task_brief["required_outputs"][:5]))
                if task_brief.get("success_criteria"):
                    prompt_parts.append("成功标准：\n" + "\n".join(f"- {x}" for x in task_brief["success_criteria"][:5]))
                # D14: 本次真正可用的能力集合（宿主签发，不信任模型自填）。
                # 注: 此处只收 scope，窄集 prompt 在 allowlist 确定后追加（见下方），
                # 避免 agent 看到宽集后试窄外工具漂移。
                _scope = [str(x) for x in (task_brief.get("capability_scope") or []) if str(x).strip()][:20]
                prompt_parts.append("请直接执行并返回结构化结果，勿加人格化措辞。")
                # _task_prompt 在 allowlist 确定后组装（见下方窄集 prompt），此处不提前 join。

                # Ack 250ms 内完成则取消（按裁定 grace）
                # 此处 worker 启动后，ack 任务已在 sleep(0.4)，若此处 0.25s 内完成会 cancel

                # 调用执行器 — in-process 多轮工具循环（不依赖外部 runtime）
                # C2 真过滤: 任务 allowlist → 工具列表裁剪 + 拦调用 (与 prompt D14 同源)。
                # 无 scope 时 fail-open (全量，今日行为)；finally 必弹栈防泄漏。
                # scope 是用户级宽集合时，用目标关键词收紧 (直呼跳 Slim 无 Gate 建议时生效)。
                from ..intelligence.tool_registry import build_task_allowlist, infer_task_tools
                from ..intelligence.tool_registry import looks_like_draw_request
                from .task_scope import task_scope
                _objective = str(task_brief.get("objective", "") or "")
                # objective/原文里可能带系统注（[已完成/待确认生图单]，自带「生图」词）
                # —— 关键词判定一律看剥注原文，否则没要图的消息会被推断出绘图意图。
                _objective_clean = _sdn(_objective)
                _inferred = infer_task_tools(_objective_clean)
                _draw_tools = frozenset({"generate_image", "edit_image"})
                # 生图意图补网 (2026-10-12 假完成事故): 关键词表只认「画一张/生图」，
                # 「生成一张…二创图」这类说法推断为空 → allowlist 保持宽集合 (20 个)
                # → agent 空转 170s 后自己编出「人家画好了」的成品描述 (零工具调用)。
                # 命中补网即并入生图工具 → 该任务转入 spec 直算，从结构上不进 agent。
                _draw_intent = bool(_inferred & _draw_tools) or looks_like_draw_request(_objective_clean)
                # 原文回查 (2026-10-14 找画混淆事故)：brief 是 Brain 的转述，
                # 转述可能丢字 —— 实测「找一张」被写成「生成一张」，allowlist 若只看
                # brief 会把找图任务锁进画图。原文索取主导、且原文无创作意图时，
                # 用原文一票否决上面的强制意图，并入搜索工具族。
                # 注意只否决强制并入，不清 infer 已有内容（「对着画」这类混合表述
                # 靠关键词表正常授予，不受影响）。
                # 2026-10-15 花火事故补丁：反向恢复 —— objective 转述丢信号
                # （「画一个」→「绘制一张…追求…」），原文是绘图即补回生图意图。
                _raw_veto_draw = False
                _raw_probe = ""
                _raw_probe_clean = ""
                try:
                    from ..intelligence.tool_registry import looks_like_search_request as _is_search
                    try:
                        _ctx0 = self._scheduler._contexts.get(
                            self._scheduler._make_ctx_key(bot_id, int(group_id)))
                        _oid0 = str(origin_message_id or "")
                        if _ctx0 is not None and _oid0:
                            for _m0 in reversed(_ctx0.messages[-30:]):
                                if (str(_m0.get("message_id", "") or "") == _oid0
                                        or str(_m0.get("event_id", "") or "") == _oid0):
                                    _raw_probe = str(_m0.get("content", "") or "")
                                    break
                            # 无回退：找不到精确 origin 即置空，不拿触发人旧消息顶替。
                            # 旧回退会把短确认「好」误继承成上一句绘图请求，导致空单又走 spec。
                    except Exception:
                        _raw_probe = ""
                    _raw_probe_clean = _sdn(_raw_probe) if _raw_probe else ""
                    if (_raw_probe_clean and _is_search(_raw_probe_clean)
                            and not looks_like_draw_request(_raw_probe_clean)):
                        _raw_veto_draw = True
                        _inferred |= {"web_search", "search_knowledge", "pixiv_search"}
                        logger.info(
                            "Dispatcher 原文回查否决画图 task=%s raw=%.40s",
                            task_id, _raw_probe_clean,
                        )
                    elif _raw_probe_clean and not _raw_veto_draw and looks_like_draw_request(_raw_probe_clean):
                        if not _draw_intent:
                            logger.info(
                                "Dispatcher 原文回查恢复画图 task=%s raw=%.40s",
                                task_id, _raw_probe_clean,
                            )
                        _draw_intent = True
                except Exception:
                    logger.debug("Dispatcher 原文回查异常", exc_info=True)
                # 触发原文共享给任务执行路 (pixiv 单图压回看触发原文用)。
                try:
                    from ..intelligence.tools import set_shared_trigger_text as _set_trig
                    _set_trig(_raw_probe_clean or _objective_clean)
                except Exception:
                    pass
                if _draw_intent and not _raw_veto_draw:
                    _inferred |= _draw_tools
                _narrow: set[str] | None = None
                if _scope:
                    # 有 scope（宿主签发的用户可用集）时只取交集；
                    # 交集空 = Gate 推断的全在 scope 外 → 按无推断处理，
                    # 永不让 Gate 推断复活 scope 外工具（含生图/计费）。
                    # 2026-10-12 审计 (P2-11)。
                    _narrow = (set(_scope) & _inferred) or None
                else:
                    # 无 scope 信息时 fail-open（今日行为）：用推断收紧。
                    _narrow = _inferred or None
                _task_allow = build_task_allowlist(_narrow if _narrow is not None else _scope)
                # F3: agent 只见收紧后的工具（宽 scope 致漂移零调用）；无推断时回退宽 scope
                _suggest = sorted(_narrow) if _narrow else (_scope or None)
                # prompt 贴窄集（有窄集时），与工具列表裁剪同源，消除“宽进严出”漂移。
                _prompt_allow = _suggest or _scope
                if _prompt_allow:
                    prompt_parts.append("可用工具（仅限以下，禁止调用其他）：\n" + "\n".join(f"- {x}" for x in _prompt_allow))
                # 搜索收口 (2026-10-14 鸣潮乐子超时事故)：检索类任务 agent 换词空转
                # 50 步、180s 烧完交白卷——单次搜索其实都 <1s，慢的是无止境重查。
                # 同一主题换词重查最多 3 次（检索调用共≤4 次）即停，到量综合交卷。
                if _narrow and (set(_narrow) & {"web_search", "search_knowledge", "pixiv_search"}) and not (
                    set(_narrow or ()) & {"generate_image", "edit_image"}
                ):
                    _search_day = time.strftime("%Y-%m-%d")
                    _search_ym = time.strftime("%Y年%m月").replace("年0", "年")
                    prompt_parts.append(
                        "[搜索收口] 检索类任务：同一主题换词重查最多 3 次"
                        "（检索调用共≤4 次）即停；到量立刻用已拿到的结果综合出文本结论返回，"
                        "禁止继续换词空转。结论先列查到的要点；确实没查到才说没查到，不许交白卷。"
                        f"今天是 {_search_day}：查“最近/本周/新”时查询词必须带当前年月"
                        f"（如“{_search_ym} 鸣潮”），不许编过去的时间；结果出现旧版本号/旧活动"
                        "直接丢弃重查；无发布日期的结果不许写成“本周/最近”；查不到新鲜的就明说，"
                        "不许拿旧闻当本周发。"
                    )
                # 圣经先行（2026-10-15 爱弥斯事故）：找图任务 agent 习惯先 web_search，
                # 检索设施全灭时（SearXNG 超时/DDG 403/Clash 404）连跪几次直接认输，
                # 而答案（爱弥斯→aemeath）就躺在本地圣经里，零网络零成本。
                # 此处确定性预查，命中即把 tag 拍进 prompt，身份不必再核。
                if _narrow and "pixiv_search" in set(_narrow) and not (
                    set(_narrow or ()) & {"generate_image", "edit_image"}
                ):
                    try:
                        from ..service.character_bible import lookup as _bible_lookup_ao
                        _ao_hay = " ".join(
                            x for x in [str(_objective_clean or ""), str(_raw_probe_clean or "")]
                            if x.strip()
                        )[:300]
                        _ao_hit = _bible_lookup_ao(_ao_hay) if _ao_hay else None
                        if _ao_hit and (_ao_hit.get("danbooru") or "").strip():
                            _ao_tag = _ao_hit["danbooru"].strip()
                            _ao_nm = str(_ao_hit.get("name") or "").strip()
                            _ao_f = str(_ao_hit.get("features") or "").strip()[:120]
                            prompt_parts.append(
                                f"[身份已核验] 本地圣经命中：{_ao_nm or '该角色'} → `{_ao_tag}`"
                                + (f"（{_ao_f}）" if _ao_f else "")
                                + "。pixiv_search 工具已内接圣经、会自行用此 tag 搜图"
                                  "（含中文别名换词兜底），你无需转述 tag、也不必为此先 web_search；"
                                  "resolve_character_identity 已定论不必重调；"
                                  "web_search 只是辅助，搜不到/被挡时以此 tag 为准交卷，"
                                  "不许说没核实身份。"
                            )
                            logger.info(
                                "Dispatcher 找图圣经先行 task=%s tag=%s", task_id, _ao_tag,
                            )
                    except Exception:
                        logger.debug(
                            "Dispatcher 找图圣经先行跳过 task=%s", task_id, exc_info=True,
                        )
                _task_prompt = "\n\n".join(prompt_parts)
                # 复述门不断的根因补丁：suggested 只是“推荐”，agent
                # 180s 空转零调用（实测 text_len=0 ev=50 全是 session.event）。
                # 生图任务加 must 硬约束（执行前拼“必须真实调用后再回复”），
                # agent 首步即调工具：第一相拿复述单，第二相 confirmed=true 开画。
                # 2026-10-12: 条件从「收紧集 ⊆ {生图两件}」放宽为「收紧集含生图工具」——
                # 混合任务 (生图+检索) 此前拿不到 must，同样会空转后编结论。
                _must = sorted(set(_narrow or ()) & _draw_tools) or None
                _draw_task = bool(set(_narrow or ()) & _draw_tools)
                # F1: 触发人透传给执行器确认门 + 共享 bot_id（跨任务执行读不到 contextvar）
                from ..intelligence.tools import set_shared_bot_id as _set_shared_bid
                from ..intelligence.tools import set_shared_scene as _set_shared_scene
                with contextlib.suppress(Exception):
                    _set_shared_bid(bot_id)
                # 共享场景(群号): 绘图访问策略判 NSFW 通路用 (任务路读不到 tool_context)
                with contextlib.suppress(Exception):
                    _set_shared_scene(str(group_id or ""))
                # F2: 有待确认生图单时，明确指令 agent 直接 confirmed=true 一步生成
                try:
                    from ..service.draw_confirm import peek_order as _peek_order
                    _pending_spec = _peek_order(bot_id, requester_id)
                except Exception:
                    _pending_spec = None
                _draw_tool = str(getattr(_pending_spec, "tool", "") or "")
                # 确认核验（00:12 教训）：有单 ≠ 已确认。Main 曾把“分辨率走大点/
                # 风格讨论”写成“按已确认”直发执行。双源判定：
                # ① 正则快道（确定性）；② Main 的 user_confirmed 仅短句下采信
                # （长句一律不信——幻觉确认全是长句）。任一过即确认。
                try:
                    from ..service.draw_confirm import looks_like_confirmation as _looks_yes
                    from ..service.draw_confirm import strip_confirm_text as _strip_yes
                    _trig_text = _strip_yes(_sdn(str(task_brief.get("trigger_text", "") or "")))
                    _by_regex = bool(_looks_yes(_trig_text))
                    _by_main = bool(task_brief.get("user_confirmed", False)) and len(_trig_text) <= 12
                    _user_confirmed = _by_regex or _by_main
                    _confirm_src = ("regex" if _by_regex else "") + ("+main" if _by_main and not _by_regex else "")
                except Exception:
                    _user_confirmed = False
                    _confirm_src = "-"
                _phase2 = bool(
                    _pending_spec is not None
                    and _draw_tool in ("generate_image", "edit_image")
                    and _user_confirmed
                )
                if _pending_spec is not None and _draw_tool in ("generate_image", "edit_image") and not _user_confirmed:
                    logger.info(
                        "Dispatcher 有单无确认 task=%s 转 Phase-1 重复述", task_id,
                    )
                # 短确认未达标不建新单 (2026-10-15)：有待确认单时，「好/嗯/行」类短词
                # 不是新需求，不许拿它当 objective 又走一遍 spec/agent（会产空单再问一遍，
                # 形成嘴+ dispatcher 双回包）。嘴已引导回「执行」，此处静默完成。
                if _pending_spec is not None and not _user_confirmed:
                    try:
                        import re as _re_ca
                        _ca_raw = _strip_yes(_sdn(str(task_brief.get("trigger_text", "") or "")) or _objective_clean)
                        _ca = _re_ca.sub(r"[\s吧呀呢哦～~！!。.\?？]+$", "", _ca_raw.strip())
                        _ca_set = frozenset({
                            "好", "好的", "好呀", "嗯", "嗯嗯", "行", "行吧",
                            "可以", "执行", "开画", "确认", "画吧", "就这样",
                            "开始画", "直接画", "没问题", "是的", "对的", "成",
                        })
                        if _ca and (_ca in _ca_set or len(_ca) <= 2):
                            logger.info(
                                "Dispatcher 短确认未达标不建新单 task=%s trig=%.20s (嘴已引导)",
                                task_id, _ca_raw[:20],
                            )
                            with contextlib.suppress(Exception):
                                await ledger_call(self._ledger, "update_status", task_id, "completed")
                            self._origin_events.pop(task_id, None)
                            if _slot_held:
                                with contextlib.suppress(ValueError):
                                    self._exec_sem.release()
                                _slot_held = False
                            return
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                # 订单关闭 (2026-10-15)：无待确认单 + 短确认词 + 10 分钟内刚完成
                # → 重复执行，不建新单（Main 侧 pending_note 已告知用户刚画过，
                # 此处静默完成，嘴和 dispatcher 不双回包）。
                if _pending_spec is None and _draw_task:
                    try:
                        import re as _re_dc
                        from ..service.draw_confirm import recent_completed as _rec_done
                        _dc_raw = _sdn(str(task_brief.get("trigger_text", "") or "")) or _objective_clean
                        _dc = _re_dc.sub(r"[\s吧呀呢哦～~！!。.\?？]+$", "", _dc_raw.strip())
                        _dc_set = frozenset({
                            "好", "好的", "好呀", "嗯", "嗯嗯", "行", "行吧",
                            "可以", "执行", "开画", "确认", "画吧", "就这样",
                            "开始画", "直接画", "没问题", "是的", "对的", "成",
                        })
                        if _dc and (_dc in _dc_set or len(_dc) <= 2) and _rec_done(bot_id, requester_id) is not None:
                            logger.info(
                                "Dispatcher 已完成单重复确认 task=%s trig=%.20s 不建新单",
                                task_id, _dc_raw[:20],
                            )
                            with contextlib.suppress(Exception):
                                await ledger_call(self._ledger, "update_status", task_id, "completed")
                            self._origin_events.pop(task_id, None)
                            if _slot_held:
                                with contextlib.suppress(ValueError):
                                    self._exec_sem.release()
                                _slot_held = False
                            return
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                if _phase2:
                    prompt_parts.append(
                        "用户已对复述单明确确认：直接带 confirmed=true 调用 "
                        f"{_pending_spec.tool} 一次完成，不要复述、不反问、不调其他工具。"
                    )
                # ── 统一表单 missing 派活 (2026-10-13) ──
                # 缺 character 系 → 强制 resolve_character_identity，执行器自己检索填表；
                # 缺 image_url → 追问用户，不派工具；其余缺件直接拦（见 Phase-2 硬门）。
                # 注意：_task_prompt 已在上文 join， Phase-2 指令同理此前漏拼——此处统一重拼。
                try:
                    from ..service.draw_confirm import MISSING_TO_TOOLS as _M2T
                    from ..service.draw_confirm import build_fill_block as _fill_block
                    from ..service.draw_confirm import missing_to_must_tools as _miss_must
                    from ..service.draw_confirm import predict_missing_from_objective as _pred_miss
                    _fill_must: list = []
                    _fill_block_txt = ""
                    if _pending_spec is not None and getattr(_pending_spec, "required_missing", None):
                        try:
                            _fill_must = _miss_must(_pending_spec) or []
                            _fill_block_txt = _fill_block(_pending_spec, _objective) or ""
                        except Exception:
                            _fill_must, _fill_block_txt = [], ""
                    if not _fill_must and (_draw_task or _draw_intent):
                        try:
                            _pred = _pred_miss(_objective) or []
                        except Exception:
                            _pred = []
                        for _mm in _pred:
                            for _tt in _M2T.get(str(_mm), ()):
                                if _tt not in _fill_must:
                                    _fill_must.append(_tt)
                        if _fill_must and not _fill_block_txt:
                            _fill_block_txt = (
                                "[生图表单补全] 目标疑似角色同人但身份未核验："
                                "先调 resolve_character_identity 拿 danbooru tag，"
                                "再顶到 prompt 最前调 generate_image；核验失败就明示不确定，不硬编。"
                            )
                    if _fill_must:
                        _must = sorted(set(_must or ()) | set(_fill_must))
                        if _suggest is not None:
                            _suggest = sorted(set(_suggest) | set(_fill_must))
                        try:
                            from ..intelligence.tool_registry import build_task_allowlist as _rebuild_allow
                            _task_allow = _rebuild_allow(_narrow if _narrow is not None else _scope, must=_fill_must)
                        except Exception:
                            pass
                    if _fill_block_txt:
                        prompt_parts.append(_fill_block_txt)
                    # 重拼：含 Phase-2 确认指令 + 表单补全块（一处 join，避免上文漏拼）
                    _task_prompt = "\n\n".join(prompt_parts)
                except Exception:
                    pass
                # E-隔离结论：suggested 参数多于 1 个即 deliberation 空转超时，
                # must 单/多工具可用。传参只给 must 或窄集首项；prompt 内窄集/真过滤不动。
                _suggest_param = sorted(set(_must or ())) or (list(_suggest or [])[:1] or None)
                logger.info(
                    "Dispatcher 执行调用 task=%s allow=%s trigger=%s pending=%s confirmed=%s src=%s",
                    task_id, sorted(_task_allow) if _task_allow else "full",
                    str(requester_id)[:8],
                    str(getattr(_pending_spec, "tool", "") or "-"),
                    _user_confirmed,
                    _confirm_src,
                )
                # 轻重分流：先定性（light/subagent/heavy），分支按定性进入。
                _task_weight = classify_task_weight(_narrow, _pending_spec)
                # 开画短语钩子：执行器真正 POST 前 fire，证明任务确实进行了；
                # agent 全程没调工具 → 事后 pop 丢弃，不误发。
                from ..intelligence.tools import draw_start_brace as _brace_fn
                from ..intelligence.tools import pop_draw_start_hook as _pop_hook
                from ..intelligence.tools import set_draw_start_hook as _set_hook
                _hook_key = f"{bot_id}:{requester_id}"
                # NSFW 自主配图标记 (2026-10-17): 开画短语这条 ack 对主动绘图静默。
                _auto_ill_task = bool((task_brief or {}).get("auto_illustrate"))

                # 出图证据载体 (guard 用): 开画钩子是否 fire + 真发出去的图数
                _draw_sent: list[int] = [0]
                _hook_fired = False

                async def _draw_started() -> None:
                    # 2026-10-17 操作者裁定: NSFW 自主配图 (bot 主动绘图) 不发开画短语。
                    # 钩子照 fire (出图证据链不变), 只是不往群里发声——亲密戏不断戏。
                    if _auto_ill_task:
                        logger.info("开画钩子: NSFW 自主配图不发开画短语 task=%s", task_id)
                        return
                    try:
                        _sctx2 = self._scheduler._contexts.get(
                            self._scheduler._make_ctx_key(bot_id, int(group_id))
                        )
                    except Exception:
                        _sctx2 = None
                    if (
                        _sctx2 is not None
                        and getattr(_sctx2, "last_bot", None) is not None
                        and getattr(_sctx2, "last_event", None) is not None
                    ):
                        await _sctx2.last_bot.send(_sctx2.last_event, _brace_fn(bot_id))

                with contextlib.suppress(Exception):
                    _set_hook(_hook_key, _draw_started)

                async def _flush_draw_images() -> None:
                    """F2: 任务路图字节直发 QQ（TaskResult 只带文本，图不走二次人格化）。"""
                    try:
                        from ..intelligence.tools import clear_pending_images as _clear_pimgs2
                        from ..intelligence.tools import get_pending_images as _get_pimgs2
                        _pimgs = _get_pimgs2(bot_id)
                        if not _pimgs:
                            return
                        _clear_pimgs2(bot_id)
                        try:
                            _sctx = self._scheduler._contexts.get(
                                self._scheduler._make_ctx_key(bot_id, int(group_id))
                            )
                        except Exception:
                            _sctx = None
                        _sent_n = 0
                        if (
                            _sctx is not None
                            and getattr(_sctx, "last_bot", None) is not None
                            and getattr(_sctx, "last_event", None) is not None
                        ):
                            from .._astrbot_adapter import MessageSegment as _MSeg
                            for _ib in _pimgs:
                                try:
                                    await _sctx.last_bot.send(_sctx.last_event, _MSeg.image(_ib))
                                    _sent_n += 1
                                except Exception:
                                    logger.warning("Dispatcher 生图直发失败 task=%s", task_id)
                                    break
                        if _sent_n:
                            _draw_sent[0] = _sent_n
                            logger.info("Dispatcher 生图直发 task=%s n=%d", task_id, _sent_n)
                        else:
                            logger.warning(
                                "Dispatcher 有图无发送上下文 task=%s n=%d", task_id, len(_pimgs),
                            )
                    except Exception:
                        logger.debug("Dispatcher 图 flush 跳过 task=%s", task_id)

                # —— 自主配图 (NSFW, 2026-10-14)：Brain illustrate 意图 → 跳确认直执 ——
                # 三验：zone 重验（fail-closed）+ 单场景守卫（TTL 窗 1 张）+ 表单硬门（直执内）。
                # 不 stage 落单、不发复述；失败静默（不成单、不打扰场景），成功走投递。
                if task_brief.get("auto_illustrate"):
                    _ill_prompt = str(task_brief.get("illustrate_prompt") or "").strip()[:100]
                    _auto_private = (not str(group_id or "")) or str(group_id or "") == "0"
                    _auto_zone = False
                    try:
                        from ..service.nsfw_zone import is_nsfw_zone as _zog_ax
                        from ..service.nsfw_zone import is_nsfw_zone_private as _zop_ax
                        _auto_zone = False
                        if _auto_private:
                            _auto_zone = bool(_zop_ax(bot_id, requester_id))
                        else:
                            _auto_zone = bool(_zog_ax(bot_id, str(group_id or ""), requester_id))
                    except Exception:
                        _auto_zone = False
                    _auto_fail = ""
                    if not _auto_zone:
                        _auto_fail = "非NSFW场景，zone门拦截"
                    elif not _ill_prompt:
                        _auto_fail = "配图意图为空"
                    elif not _auto_scene_guard(bot_id, str(group_id or ""), requester_id, private=_auto_private):
                        _auto_fail = "本场景已配过图（单场景1张）"
                    _auto_res: dict = {}
                    if not _auto_fail:
                        try:
                            _auto_res = await _auto_illustrate_run(
                                self._scheduler, bot_id, requester_id, str(group_id or ""),
                                _ill_prompt,
                                [str(x) for x in (task_brief.get("context_facts") or [])[:3]],
                                private=_auto_private,
                            ) or {}
                        except Exception as _axe:
                            _auto_res = {"ok": False, "finish_reason": "error", "text": "",
                                         "events": [], "error": str(_axe)[:200]}
                        if not _auto_res.get("ok"):
                            _auto_fail = str(_auto_res.get("error") or "执行失败")[:120]
                    if _auto_fail:
                        logger.info("Dispatcher 自主配图跳过 task=%s reason=%s", task_id, _auto_fail)
                        with contextlib.suppress(Exception):
                            _pop_hook(_hook_key)
                        with contextlib.suppress(Exception):
                            await ledger_call(self._ledger, "update_status", task_id, "failed")
                        self._origin_events.pop(task_id, None)
                        if _slot_held:
                            with contextlib.suppress(ValueError):
                                self._exec_sem.release()
                            _slot_held = False
                        return
                    logger.info("Dispatcher 自主配图 task=%s ok text_len=%d",
                                task_id, len(str(_auto_res.get("text") or "")))
                    await _flush_draw_images()
                    with contextlib.suppress(Exception):
                        _hook_fired = bool(_pop_hook(_hook_key))
                    if not _draw_sent[0]:
                        # 2026-10-17: 文案改准并附文节 — 多数是"没图可发"(上游打回/
                        # 无成图), 旧文案把打回误报成"发送上下文缺失", 误导排查。
                        logger.warning(
                            "Dispatcher 自主配图无图可发 task=%s text=%.60s",
                            task_id, str(_auto_res.get("text") or ""),
                        )
                        with contextlib.suppress(Exception):
                            await ledger_call(self._ledger, "update_status", task_id, "failed")
                        self._origin_events.pop(task_id, None)
                        if _slot_held:
                            with contextlib.suppress(ValueError):
                                self._exec_sem.release()
                            _slot_held = False
                        return
                    _auto_elapsed = time.time() - t0
                    with contextlib.suppress(Exception):
                        await ledger_call(self._ledger, "update_status", task_id, "completed")
                    from ..transport.task_events import new_event_id as _new_eid_ax
                    _auto_ev = TaskResultEvent(
                        event_id=_new_eid_ax(), seq=0, bot_id=bot_id,
                        group_id=str(group_id), task_id=task_id,
                        origin_message_id=origin_message_id, requester_id=requester_id,
                        status="completed",
                        summary=str(_auto_res.get("text") or "")[:2000] or "场景图已送到。",
                        facts=(), evidence=("generate_image:ok",), uncertainties=(),
                        artifacts=(), suggested_answer_points=(), elapsed=_auto_elapsed,
                        error_code=None, error="",
                        origin_event=self._origin_events.pop(task_id, None),
                    )
                    try:
                        _ax_ctx = self._scheduler._contexts.get(
                            self._scheduler._make_ctx_key(bot_id, int(str(group_id or "0") or "0")))
                        if _ax_ctx:
                            _ax_actor = self._actor_registry.get_or_create(
                                bot_id, int(str(group_id or "0") or "0"), _ax_ctx, self._scheduler)
                            await _ax_actor.enqueue_task_result(_auto_ev)
                    except Exception:
                        logger.exception("Dispatcher 自主配图投递失败 task=%s", task_id)
                    finally:
                        if _slot_held:
                            with contextlib.suppress(ValueError):
                                self._exec_sem.release()
                            _slot_held = False
                    return

                # —— Phase-1（生图 spec）：无单的生图任务不进 agent 执行 loop ——
                # spec 是纯推理产物，走 LLM 推理位 (llm_pro) 直调，
                # 复述直发（不进二次人格化）。三条路外必有声，不存在静默。
                _is_spec_task = bool(
                    _narrow and set(_narrow) <= _draw_tools
                    and not _phase2
                )
                if _is_spec_task:
                    from ..service.draw_confirm import DrawSpec as _DrawSpec
                    from ..service.draw_confirm import build_repeat_text as _repeat_text
                    from ..service.draw_confirm import stage_order as _stage_order

                    _spec_refusal = ""
                    _prov0, _nc0, _pc0 = "", {}, {}
                    try:
                        from ..intelligence.tools import _DRAW_SWITCH_OFF_TEXT as _SW_OFF
                        from ..intelligence.tools import _draw_backend_missing_text as _bk_m
                        from ..intelligence.tools import _draw_switch as _dsw
                        from ..intelligence.tools import _resolve_draw_backend as _res_bk
                        _prov0, _nc0, _pc0 = _res_bk(
                            bot_id, requester_id, str(group_id or ""),
                            private=not str(group_id or ""),
                        )
                        if _prov0 == "none":
                            _spec_refusal = _bk_m(_prov0, missing_key=False)
                        elif (_prov0 == "novelai" and not (_nc0.get("token") or "").strip()) or (
                            _prov0 == "pixai" and not (_pc0.get("key") or "").strip()
                        ):
                            _spec_refusal = _bk_m(_prov0, missing_key=True)
                        elif not _dsw(bot_id, "generate_image"):
                            _spec_refusal = _SW_OFF
                    except Exception:
                        pass
                    _spec_obj = None
                    _spec_source = "-"
                    # 误开单硬门 (2026-10-16 事故): 触发原文只是夸赞/附和
                    # (例如收到图后的「不错不错」) → 不调 spec、不发任何绘图话术。
                    # ★ 不叫 _draw_intent —— 上面 1022 行那个是"本轮要不要画"的放行网，
                    #   语义不同，撞名会把放行网的结论冲掉（原则 24 的反面）。
                    _chatter_only = _is_plain_chatter(task_brief)
                    _spec_objective = _sdn(str(task_brief.get("objective", "") or ""))
                    _spec_facts = [
                        str(x) for x in (task_brief.get("context_facts") or [])[:5]
                        if str(x).strip()
                    ]
                    # 改单上限：有单且改满 → 封顶直发，不烧 agent/直调（旧单保留可确认）
                    _cap_hit = False
                    _old2 = None
                    try:
                        from ..service.draw_confirm import CAPPED_TEXT as _CAPPED2
                        from ..service.draw_confirm import MAX_RESTAGES as _MAX_RS2
                        from ..service.draw_confirm import peek_order as _peek2
                        _old2 = _peek2(bot_id, requester_id)
                        if _old2 is not None and int(_old2.restages or 0) >= _MAX_RS2:
                            _cap_hit = True
                            logger.info("Dispatcher 改单封顶 task=%s", task_id)
                    except Exception:
                        pass
                    _empty_subject_hit = False
                    if not _spec_refusal and not _cap_hit:
                        # 空主体双判（2026-10-14 终局：规则管字面，Brain 管语义）——
                        # 规则（零成本确定性）：剥完 0 有效字=字面真空。语义（Brain has_subject，
                        # 与本轮 LLM 同一次调用、零边际成本）：字面有字但语义真空（如纯客套
                        # “画一张图谢谢”）也拦。优先级写死：
                        # ① 字面真空（无配图打底）→ 直接反问，不信 LLM（凭空说有主体必是幻觉）；
                        # ② 字面有字＋Brain 说无主体 → 信 Brain，反问；
                        # ③ 字面真空＋有配图＋Brain 说有主体 → 放行（图本身是主体）；
                        # ④ 其余 → 放行走 spec。缺字段一律 fail-open（当有主体）。
                        # facts 不参与判定：facts 是备注（如“用户未说明内容”），备注不是主体。
                        try:
                            from ..intelligence.tools import _draw_subject_length as _subj_len
                            _empty_raw = _fetch_task_raw_text(
                                self._scheduler, bot_id, group_id,
                                origin_message_id, requester_id,
                            )
                            _empty_check = _sdn(_empty_raw) or _spec_objective
                            _raw_visual = ("[CQ:image" in _empty_check) or ("[CQ:file" in _empty_check)
                            _literal_empty = bool(
                                _empty_check and _subj_len(_empty_check) == 0
                            )
                            _brain_has = task_brief.get("has_subject", True) is not False
                            if _literal_empty:
                                _empty_subject_hit = not (_raw_visual and _brain_has)
                            elif _empty_check and not _brain_has:
                                _empty_subject_hit = True
                            if _empty_subject_hit:
                                logger.info(
                                    "Dispatcher spec 空主体反问 task=%s raw=%.40s brain_has=%s",
                                    task_id, _empty_check, _brain_has,
                                )
                        except Exception:
                            logger.debug("Dispatcher spec 空主体检查跳过 task=%s", task_id, exc_info=True)
                    if not _spec_refusal and not _cap_hit and not _empty_subject_hit:
                        # 纯横竖改单（横图没变过来案）：有旧单 + 短句只说横/竖时，
                        # 原地翻旧单，不调 LLM 重开（重开会丢梅比乌斯主体→空 prompt→旧竖图保留）。
                        try:
                            from ..service.draw_confirm import apply_orientation_modify as _apply_ori
                            from ..service.draw_confirm import parse_orientation_modify as _parse_ori
                            _ori_raw = ""
                            try:
                                _ori_raw = _fetch_task_raw_text(
                                    self._scheduler, bot_id, group_id,
                                    origin_message_id, requester_id,
                                )
                            except Exception:
                                _ori_raw = ""
                            _ori_want = _parse_ori(_ori_raw or _spec_objective) or _parse_ori(_spec_objective)
                            if _ori_want and _old2 is not None:
                                if str(getattr(_old2, "orientation", "") or "") != _ori_want:
                                    _apply_ori(_old2, _ori_want)
                                    _stage_order(
                                        bot_id, requester_id, _old2,
                                        restages=(int(_old2.restages or 0) + 1 if _old2 else 0),
                                    )
                                    try:
                                        _char_n_ori = str(
                                            self._scheduler._resolve_character(bot_id).get("name", "")
                                        )
                                    except Exception:
                                        _char_n_ori = ""
                                    _final_ori = _repeat_text(_old2, note="", char_name=_char_n_ori)
                                    try:
                                        _sctx_ori = self._scheduler._contexts.get(
                                            self._scheduler._make_ctx_key(bot_id, int(group_id))
                                        )
                                    except Exception:
                                        _sctx_ori = None
                                    _send_ori = self._origin_events.get(task_id) or (
                                        getattr(_sctx_ori, "last_event", None) if _sctx_ori else None
                                    )
                                    try:
                                        if (
                                            _sctx_ori is not None
                                            and getattr(_sctx_ori, "last_bot", None) is not None
                                            and _send_ori is not None
                                        ):
                                            from ..transport.group_chat import _emit_reply as _emit_ori
                                            await _emit_ori(
                                                _sctx_ori, _final_ori,
                                                char_name=_char_n_ori, send_event=_send_ori,
                                            )
                                    except Exception:
                                        logger.warning("Dispatcher 横竖改单直发失败 task=%s", task_id, exc_info=True)
                                    with contextlib.suppress(Exception):
                                        await ledger_call(self._ledger, "update_status", task_id, "completed")
                                    self._origin_events.pop(task_id, None)
                                    if _slot_held:
                                        with contextlib.suppress(ValueError):
                                            self._exec_sem.release()
                                    logger.info(
                                        "Dispatcher 横竖改单原地翻 task=%s want=%s size=%s",
                                        task_id, _ori_want, str(getattr(_old2, "size_label", "")),
                                    )
                                    return
                                else:
                                    # 已是目标方向：重发旧单复述，不重开 LLM（防空单覆盖）。
                                    try:
                                        _char_n_same = str(
                                            self._scheduler._resolve_character(bot_id).get("name", "")
                                        )
                                    except Exception:
                                        _char_n_same = ""
                                    try:
                                        _final_same = _repeat_text(_old2, note="", char_name=_char_n_same)
                                        try:
                                            _sctx_same = self._scheduler._contexts.get(
                                                self._scheduler._make_ctx_key(bot_id, int(group_id))
                                            )
                                        except Exception:
                                            _sctx_same = None
                                        _send_same = self._origin_events.get(task_id) or (
                                            getattr(_sctx_same, "last_event", None) if _sctx_same else None
                                        )
                                        if (
                                            _sctx_same is not None
                                            and getattr(_sctx_same, "last_bot", None) is not None
                                            and _send_same is not None
                                        ):
                                            from ..transport.group_chat import _emit_reply as _emit_same
                                            await _emit_same(
                                                _sctx_same, _final_same,
                                                char_name=_char_n_same, send_event=_send_same,
                                            )
                                    except Exception:
                                        logger.warning("Dispatcher 横竖改单重发失败 task=%s", task_id, exc_info=True)
                                    with contextlib.suppress(Exception):
                                        await ledger_call(self._ledger, "update_status", task_id, "completed")
                                    self._origin_events.pop(task_id, None)
                                    if _slot_held:
                                        with contextlib.suppress(ValueError):
                                            self._exec_sem.release()
                                    logger.info("Dispatcher 横竖改单已是目标方向 task=%s", task_id)
                                    return
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.debug("Dispatcher 横竖改单跳过 task=%s", task_id, exc_info=True)
                        # 未命中纯横竖改单（无旧单/长句/已返回）时落到下面正常 spec；
                        # 命中改单上面已 return，此处只剩未命中。
                        pass
                    if not _spec_refusal and not _cap_hit and not _empty_subject_hit and not _chatter_only:
                        # zone 判定与自主配图同口径 (fail-closed)：成人向单必须把放行
                        # 口径交给 spec 生成那一步，否则模型对着露骨 objective 拒答，
                        # 用户侧只会看到一句「你没说画什么」(2026-10-16)。
                        _spec_nsfw = False
                        try:
                            from ..service.nsfw_zone import is_nsfw_zone as _zog_s
                            from ..service.nsfw_zone import is_nsfw_zone_private as _zop_s
                            _spec_priv = (not str(group_id or "")) or str(group_id or "") == "0"
                            if _spec_priv:
                                _spec_nsfw = bool(_zop_s(bot_id, requester_id))
                            else:
                                _spec_nsfw = bool(_zog_s(bot_id, str(group_id or ""), requester_id))
                        except Exception:
                            _spec_nsfw = False
                        _spec_obj = await _direct_draw_spec(
                            bot_id, _spec_objective, _spec_facts, nsfw=_spec_nsfw,
                        )
                        if _spec_obj is not None:
                            _spec_source = "llm_pro"
                        logger.info(
                            "Dispatcher spec 产出 task=%s source=%s nsfw=%s objective=%.300s",
                            task_id, _spec_source, _spec_nsfw, _spec_objective,
                        )
                        if _spec_obj is None:
                            logger.warning(
                                "Dispatcher spec 失败 task=%s nsfw=%s objective=%.500s facts=%s",
                                task_id, _spec_nsfw, _spec_objective, _spec_facts,
                            )
                    # 公共发送块 (1141+) 用到 _char_n3,此处先置初值:
                    # spec 失败/封顶/拒单分支不进 else,原先只在 else 内赋值 → UnboundLocalError。
                    _char_n3 = ""
                    if _chatter_only:
                        # 触发原文是纯夸赞/附和（收到图后的「不错不错」就是这个）——
                        # 不出声、不落单。用户已经拿到图，任何绘图话术都是噪音。
                        logger.info(
                            "Dispatcher spec 纯夸赞静默 task=%s objective=%.60s",
                            task_id, _spec_objective,
                        )
                        _final_text, _spec_status = "", "completed"
                    elif _empty_subject_hit:
                        _final_text = _draw_ask_subject_text(self._scheduler, bot_id)
                        _spec_status = "completed"
                    elif _cap_hit:
                        _final_text, _spec_status = _CAPPED2, "completed"
                    elif _spec_refusal:
                        _final_text, _spec_status = _spec_refusal, "failed"
                    elif _spec_obj is None:
                        # 走到这里 = 输入本来有主体（_empty_subject_hit 已排除无主体），
                        # 是构思那一步没成（拒答/超时/JSON 不合规）。旧版复用「缺主体」
                        # 话术 → 把内部失败说成用户的锅（2026-10-16 拒单事故），
                        # 现改认下这单没接住，仍不泄内部细节。
                        # 完单后静默兜底 (2026-10-16 事故；22:10 不错不错补强)：
                        # 无待确认单 + 刚完成过一单 + objective 无绘图意图 → 一字不发。
                        # 用户已拿到图，此时任何绘图话术都是噪音；真有新需求时
                        # looks_like_draw_request 为真，照样走下面 fallback。
                        _silent_done = False
                        try:
                            from ..intelligence.tool_registry import looks_like_draw_request as _draw_req
                            from ..service.draw_confirm import peek_order as _peek_s
                            from ..service.draw_confirm import recent_completed as _rec_s
                            _no_pending = _peek_s(bot_id, requester_id) is None
                            _has_rec = _rec_s(bot_id, requester_id) is not None
                            # 22:10 补强：只看 objective，不混 trigger (裹旧词必误判)
                            # 23:34 通用护栏：无待确认单 + objective 无画意 → 一律静默，
                            # 不再要求必须有完单记录 (玩笑话本就不该出画单话术)。
                            _trig_all = str(_spec_objective or "")
                            _silent_done = bool(
                                _no_pending
                                and not _draw_req(_trig_all)
                            )
                        except Exception:
                            _silent_done = False
                        if _silent_done:
                            logger.info(
                                "Dispatcher spec 失败后静默 task=%s（无画意输入 rec=%s）",
                                task_id, _has_rec,
                            )
                            _final_text, _spec_status = "", "completed"
                        else:
                            _final_text = _draw_ask_subject_text(self._scheduler, bot_id, failed=True)
                            _spec_status = "completed"
                    else:
                        # 统一表单 (2026-10-13)：_spec_obj 为完整 JSON dict，先落表再做确定性补全。
                        # 兼容旧 4 元组（回退分支历史形状）：dict 优先，tuple 降级。
                        if isinstance(_spec_obj, dict):
                            _sp = str(_spec_obj.get("prompt", "") or "")
                            _spe = str(_spec_obj.get("prompt_en", "") or "")
                            _so = str(_spec_obj.get("orientation", "") or "portrait")
                            _sreq = str(_spec_obj.get("size", "") or "")
                        else:
                            _sp, _spe, _so, _sreq = _spec_obj
                            _spec_obj = {"prompt": _sp, "prompt_en": _spe, "orientation": _so, "size": _sreq}
                        from ..service.draw_confirm import fill_spec_from_json as _fill_spec
                        _spec_view = _DrawSpec(
                            prompt=_sp, prompt_en=_spe, orientation=_so,
                            size_label="", model="", provider=_prov0 or "novelai",
                            tool="generate_image",
                        )
                        _fill_spec(_spec_view, _spec_obj if isinstance(_spec_obj, dict) else {})
                        # 本机自设角色外观收口 (2026-10-16 自画错事故): 上游 Brain 把本机角色
                        # 说成「猫耳猫尾的软萌角色」(同群其他 bot 是猫娘)，spec 照抄 → 复述单
                        # 与成图全是猫娘。这里有预设就用预设覆写，并剔掉冲突物种特征。
                        try:
                            from ..service.draw_confirm import apply_local_char_preset as _preset_fix
                            _ps_stripped = _preset_fix(_spec_view)
                            if _ps_stripped:
                                logger.info(
                                    "Dispatcher 本机角色外观收口 task=%s name=%s 剔除=[%s]",
                                    task_id, _spec_view.character_name, _ps_stripped,
                                )
                            elif _spec_view.character_features:
                                logger.info(
                                    "Dispatcher 本机角色外观未命中 task=%s name=%r",
                                    task_id, _spec_view.character_name,
                                )
                        except Exception:
                            logger.debug("本机角色外观收口跳过 task=%s", task_id, exc_info=True)
                        # 角色同人治根 (与确认门同逻辑)：LLM 直写的 prompt_en 常是
                        # "Danya from ..." 自然语言，NAI 角色知识不触发——命中才联网定身份。
                        _char_note2 = ""
                        _char_resolved = None
                        _char_query = ""
                        if _prov0 == "novelai":
                            try:
                                from ..intelligence.tools import _extract_draw_character_query as _xchar
                                from ..intelligence.tools import _extract_draw_character_query_llm as _xchar_llm
                                from ..intelligence.tools import _resolve_draw_character_via_web as _rchar
                                # 圣经先行 (2026-10-17): 确定性本地秒查优先 —
                                # 取名不再起步就靠 LLM (花火/月下初拥案: 取空/取错)。
                                _cq = None
                                try:
                                    from ..service.character_bible import lookup as _bible_draw
                                    _bhit_d = _bible_draw(f"{_spec_objective}\n{_sp}")
                                    if _bhit_d:
                                        _cq = str(_bhit_d.get("name") or "").strip() or None
                                except Exception:
                                    _bhit_d = None
                                # LLM 取名兜底：objective 含完整意图（_sp 常丢 IP，如花火单），
                                # 合并后喂 LLM；失败回退正则。
                                if not _cq:
                                    try:
                                        _cq = await _xchar_llm(f"{_spec_objective}\n{_sp}", bot_id)
                                    except Exception:
                                        _cq = None
                                if not _cq:
                                    try:
                                        _cq = _xchar(f"{_spec_objective}\n{_sp}") or _xchar(_sp)
                                    except Exception:
                                        _cq = None
                                # 圣经没有就联网查一次 (2026-10-16 月下初拥案)：
                                # 提取拿不到 query (无 IP/标记，如崩坏三变体) 但 spec 有人名时，
                                # 用 spec 的 character_name (+series) 兜底查一次网，不浪费。
                                if not _cq:
                                    try:
                                        _fb_name = str((_spec_obj.get("character_name", "") or "").strip()) if isinstance(_spec_obj, dict) else ""
                                        _fb_series = str((_spec_obj.get("series", "") or _spec_obj.get("character_series", "") or "").strip()) if isinstance(_spec_obj, dict) else ""
                                        if len(_fb_name) >= 2 and _fb_name not in ("同人", "官设", "角色", "人物"):
                                            _cq = f"{_fb_series} {_fb_name}".strip() if _fb_series else _fb_name
                                    except Exception:
                                        pass
                                if _cq:
                                    _char_query = _cq
                                    _rs = await _rchar(_cq, bot_id)
                                    _char_resolved = _rs
                                    if _rs:
                                        _tg = (_rs.get("danbooru") or "").strip()
                                        # 官设优先顶最前（同确认门）：压住 LLM 编造的细节。
                                        _hd = _tg
                                        _ft = (_rs.get("features") or "").strip()
                                        if _ft:
                                            _hd += f", {_ft}"
                                        if _tg and _tg.lower() not in (_spe or "").lower():
                                            _spe = f"{_hd}, {_spe}"
                                            _spec_view.prompt_en = _spe
                                        _dn = (_rs.get("name") or _cq).strip()[:30]
                                        _char_note2 = f"（角色：{_dn} → {_tg}）"
                                    else:
                                        _char_note2 = (
                                            f"（{_cq} 的身份没联网核实，外貌是通用画法——"
                                            "错了直接纠我，或丢张参考图对着画）"
                                        )
                            except Exception:
                                logger.debug("Dispatcher 角色解析跳过 task=%s", task_id, exc_info=True)
                        try:
                            from ..service.draw_confirm import apply_character_resolution as _apply_char
                            _apply_char(_spec_view, _char_resolved, query=_char_query)
                            # 交叉验证注记随复述（分歧/单来源提示，用户终审）
                            if (_spec_view.identity_note or "").strip():
                                _char_note2 += str(_spec_view.identity_note).strip()
                        except Exception:
                            pass
                        # 工具按证据选 (2026-10-12): 双候选时 sorted()[0] 恒定
                        # 选中 edit_image, 纯文字单会被错 stage 成以图生图。
                        _tool_one = _pick_spec_tool(
                            _narrow, objective=_spec_objective,
                            facts=_spec_facts, bot_id=bot_id,
                            requester_id=requester_id,
                        )
                        try:
                            from ..intelligence.tools import _orient_of_size as _oof2
                            from ..intelligence.tools import _resolve_draw_size as _rsz2
                            from ..intelligence.tools import _size_options_text as _szopt2
                            if _prov0 == "novelai":
                                _panel2 = _nc0.get("size") or "832x1216"
                            elif _prov0 == "pixai":
                                _panel2 = (
                                    f"{_pc0.get('aspect') or '9:16'} {_pc0.get('size') or '1k'}"
                                )
                            else:
                                _panel2 = ""
                            _ss2, _cl2 = _rsz2(_prov0, _so, _panel2, _sreq)
                            _so = _oof2(_prov0, _ss2, _so)
                        except Exception:
                            _ss2, _cl2 = "", False
                        if not _ss2:
                            if _prov0 == "novelai":
                                _sm = _nc0.get("model") or "nai-diffusion-5-full"
                                _ss = _nc0.get("size") or "512x768"
                            elif _prov0 == "pixai":
                                _sa = _pc0.get("aspect") or "9:16"
                                _ss = f"{_sa} {_pc0.get('size') or '1k'}"
                                _sm = _pc0.get("model") or "1983308862240288769"
                            else:
                                _sm, _ss = "?", "?"
                                _prov0 = _prov0 or "novelai"
                            _cl2 = False
                        else:
                            _ss = _ss2
                            if _prov0 == "novelai":
                                _sm = _nc0.get("model") or "nai-diffusion-5-full"
                            elif _prov0 == "pixai":
                                _sm = _pc0.get("model") or "1983308862240288769"
                            else:
                                _sm = "?"
                                _prov0 = _prov0 or "novelai"
                        # 统一表单落定 (原地更新，保留 LLM 新槽位)：工具/尺寸/模型回填 + missing 归一
                        _spec_view.prompt = _sp
                        _spec_view.prompt_en = _spe
                        _spec_view.orientation = _so
                        _spec_view.model = _sm
                        _spec_view.provider = _prov0
                        _spec_view.tool = _tool_one
                        try:
                            from ..service.draw_confirm import apply_size_resolution as _apply_size
                            _apply_size(_spec_view, _ss, _cl2, requested=_sreq)
                        except Exception:
                            _spec_view.size_label = _ss
                            _spec_view.size_clamped = bool(_cl2)
                            _spec_view.requested_size = _sreq
                        # 美学后处理 (SuliAnimeCreator 三 skill 本地版)：风格头/机位/光影三要素/
                        # 构图保底/Vague 清洗/质量尾。不调 LLM，不碰身份核验与尺寸。
                        try:
                            from ..service.aesthetic_skill import enhance_prompt_en as _aes_enh
                            _spec_view.prompt_en = _aes_enh(_spec_view.prompt_en or "", _so)
                        except Exception:
                            pass
                        # 缺件不落单 (2026-10-15)：信息不齐不出复述单。
                        # agent 未补的原因：spec 分支不进 agent loop，核验只用 _sp 取角色名，
                        # 而 _sp 里经常丢 IP（如花火单 _sp 无星穹铁道），_xchar 取不到查询词，
                        # 单次 _rchar 空转后仍带缺件落单。此处用 objective 兜底再核一次，
                        # 仍缺则不 stage，直接发拦截原因，本轮不产生待确认单。
                        try:
                            from ..service.draw_confirm import require_ready_to_draw as _req_ready_spec
                            _rok0, _rreason0 = _req_ready_spec(_spec_view)
                        except Exception:
                            _rok0, _rreason0 = True, ""
                        if not _rok0 and not _char_resolved:
                            try:
                                _miss0 = _spec_view.required_missing() or []
                            except Exception:
                                _miss0 = []
                            if any(m in _miss0 for m in ("character_danbooru", "identity_confirmed")):
                                try:
                                    from ..intelligence.tools import _extract_draw_character_query as _xchar2
                                    from ..intelligence.tools import _extract_draw_character_query_llm as _xchar2_llm
                                    from ..intelligence.tools import _resolve_draw_character_via_web as _rchar2
                                    _cq2 = None
                                    try:
                                        from ..service.character_bible import lookup as _bible_draw2
                                        _bhit_d2 = _bible_draw2(f"{_spec_objective}\n{_sp}")
                                        if _bhit_d2:
                                            _cq2 = str(_bhit_d2.get("name") or "").strip() or None
                                    except Exception:
                                        _bhit_d2 = None
                                    if not _cq2:
                                        try:
                                            _cq2 = await _xchar2_llm(f"{_spec_objective}\n{_sp}", bot_id)
                                        except Exception:
                                            _cq2 = None
                                    if not _cq2:
                                        try:
                                            _cq2 = _xchar2(f"{_spec_objective}\n{_sp}") or _xchar2(_spec_objective)
                                        except Exception:
                                            _cq2 = None
                                    if _cq2 and _cq2 != _char_query:
                                        _rs2 = await _rchar2(_cq2, bot_id)
                                        if _rs2:
                                            from ..service.draw_confirm import apply_character_resolution as _apply_char2
                                            _apply_char2(_spec_view, _rs2, query=_cq2)
                                            _tg2 = (_rs2.get("danbooru") or "").strip()
                                            _ft2 = (_rs2.get("features") or "").strip()
                                            if _tg2:
                                                _hd2 = _tg2 + (f", {_ft2}" if _ft2 else "")
                                                if _tg2.lower() not in (_spec_view.prompt_en or "").lower():
                                                    _spec_view.prompt_en = f"{_hd2}, {_spec_view.prompt_en}"
                                                _char_note2 = f"（角色：{(_rs2.get('name') or _cq2).strip()[:30]} → {_tg2}）"
                                            if (_spec_view.identity_note or "").strip():
                                                _char_note2 += str(_spec_view.identity_note).strip()
                                except Exception:
                                    logger.debug("Dispatcher spec 兜底核验跳过 task=%s", task_id, exc_info=True)
                                try:
                                    _rok0, _rreason0 = _req_ready_spec(_spec_view)
                                except Exception:
                                    pass
                        _char_n3 = ""
                        try:
                            _char_n3 = str(
                                self._scheduler._resolve_character(bot_id).get("name", "")
                            )
                        except Exception:
                            _char_n3 = ""
                        if not _rok0:
                            # 双通路防打架（梅比乌斯案）：旧单刚落定且可执行时，
                            # 新分支因 LLM 名字槽缺失误判成缺件，不许发矛盾拦截
                            # （“先给执行单又说原创”），保旧单、复述旧单。
                            _kept_old = False
                            try:
                                from ..service.draw_confirm import require_ready_to_draw as _req_old
                                if _old2 is not None and _req_old(_old2)[0]:
                                    _age = time.time() - float(getattr(_old2, "created_at", 0.0) or 0.0)
                                    if _age >= 0 and _age < 120:
                                        _final_text = _repeat_text(_old2, note="", char_name=_char_n3)
                                        _spec_status = "completed"
                                        _kept_old = True
                                        logger.info(
                                            "Dispatcher spec 新单缺件但旧单新鲜可画 task=%s 保旧单 age=%.0fs",
                                            task_id, _age,
                                        )
                            except Exception:
                                _kept_old = False
                            if not _kept_old:
                                _final_text = f"{_rreason0}补上后重说一次需求，我按新单重开，不用回执行。"
                                _spec_status = "failed"
                        else:
                            _stage_order(
                                bot_id, requester_id, _spec_view,
                                restages=(int(_old2.restages or 0) + 1 if _old2 else 0),
                            )
                            _note2 = f"（所要尺寸超限定档，已按 {_ss} 执行）" if _cl2 else ""
                            if _char_note2:
                                _note2 += _char_note2
                            if not (_sreq and not _cl2):
                                _note2 += _szopt2(_prov0)
                            _final_text = _repeat_text(
                                _spec_view, note=_note2, char_name=_char_n3,
                            )
                            _spec_status = "completed"
                    try:
                        _sctx3 = self._scheduler._contexts.get(
                            self._scheduler._make_ctx_key(bot_id, int(group_id))
                        )
                    except Exception:
                        _sctx3 = None
                    _send_ev3 = self._origin_events.get(task_id) or (
                        getattr(_sctx3, "last_event", None) if _sctx3 else None
                    )
                    try:
                        if (
                            _final_text
                            and _sctx3 is not None
                            and getattr(_sctx3, "last_bot", None) is not None
                            and _send_ev3 is not None
                        ):
                            # ★ 2026-10-12: 统一走活路径出口 —— 此前这里是裸
                            #   last_bot.send(), 绕过 sanitize / 静默判定 / 旁白过滤 /
                            #   长度裁剪。审计实测 51 次输出走这条路, 最长 1067 字
                            #   (600 上限的 1.78 倍), 技术表单与「❌ 绘图服务连接失败」
                            #   都从这儿进的群。
                            from ..transport.group_chat import _emit_reply as _emit

                            _sent3 = await _emit(
                                _sctx3, _final_text,
                                char_name=_char_n3, send_event=_send_ev3,
                            )
                            if _sent3:
                                logger.info(
                                    "Dispatcher spec 直发 task=%s status=%s len=%d",
                                    task_id, _spec_status, len(_sent3),
                                )
                            else:
                                logger.info(
                                    "Dispatcher spec 直发被静默/清理为空 task=%s",
                                    task_id,
                                )
                        else:
                            logger.warning("Dispatcher spec 无发送上下文 task=%s", task_id)
                    except Exception:
                        logger.warning("Dispatcher spec 直发失败 task=%s", task_id, exc_info=True)
                    with contextlib.suppress(Exception):
                        await ledger_call(self._ledger, "update_status", task_id, _spec_status)
                    self._origin_events.pop(task_id, None)
                    if _slot_held:
                        with contextlib.suppress(ValueError):
                            self._exec_sem.release()
                        _slot_held = False
                    return

                # 兜底初值：任何分支漏赋值都不炸 worker（UnboundLocalError 根因）。
                status = "failed"
                summary = f"任务 {task_brief.get('objective','')}未能执行，请稍后重试。"
                facts = ()
                evidence = ()
                uncertainties = ("执行分支未赋值",)
                error_code = "WORKER_FALLBACK"
                error = "执行分支未赋值"
                if _phase2 and _pending_spec is not None:
                    # Phase-2 计费门 (2026-10-12 审计): scope 是宿主签发的用户可用集，
                    # 生图工具不在其中 → 不直执，转 agent 分支（allowlist 会拦）。
                    # scope 为空（无签发信息）时 fail-open，保持今日行为。
                    if _scope and not (set(_scope) & _draw_tools):
                        logger.warning(
                            "Dispatcher Phase-2 被计费门拦截 task=%s "
                            "requester=%s 生图不在 scope 内，转 agent 分支",
                            task_id, str(requester_id)[:8],
                        )
                        _phase2 = False
                if _phase2 and _pending_spec is not None:
                    # Phase-2 已确认：跳过 agent 直接执行（2026-10-12 裁定）。
                    # agent 在 mimo 线上空聊/卡死，复述单已含全部执行参数。
                    http_result = await _phase2_direct_draw(
                        bot_id, requester_id, _pending_spec,
                        task_brief.get("objective", ""),
                    )
                    logger.info(
                        "Dispatcher Phase-2 直执 task=%s ok=%s err=%s",
                        task_id, (http_result or {}).get("ok"),
                        str((http_result or {}).get("error") or "")[:120],
                    )
                    try:
                        _hr2 = http_result or {}
                        logger.info(
                            "Dispatcher 执行结果 task=%s ok=%s finish=%s text_len=%d ev=%d err=%s",
                            task_id, _hr2.get("ok"), _hr2.get("finish_reason"),
                            len(str(_hr2.get("text") or "")),
                            len(_hr2.get("events") or []),
                            str(_hr2.get("error") or "")[:120],
                        )
                    except Exception:
                        pass
                    await _flush_draw_images()
                    with contextlib.suppress(Exception):
                        _hook_fired = bool(_pop_hook(_hook_key))
                    classified = classify_task_result(http_result, task_brief.get("objective", ""))
                    status = classified["status"]
                    summary = classified["summary"]
                    facts = classified["facts"]
                    evidence = classified["evidence"]
                    uncertainties = classified["uncertainties"]
                    error_code = classified["error_code"]
                    error = classified.get("error", "")
                elif _task_weight == "light":
                    # 轻任务直执：纯本地确定性工具（知识库/转发明细/角色身份），秒级回。
                    from ..intelligence.tools import TOOL_EXECUTORS as _LOCAL_EXEC
                    _lt0 = time.time()
                    _lctx = {
                        "bot_id": bot_id,
                        "group_id": str(group_id or ""),
                        "requester_id": requester_id,
                        "_search_notice_sent": True,
                    }
                    _ltexts: list[str] = []
                    _lok = True
                    for _lname in sorted(_narrow or ()):
                        _lex = _LOCAL_EXEC.get(_lname)
                        if _lex is None:
                            continue
                        try:
                            _ltext = await _lex(light_tool_params(_lname, _objective), _lctx)
                        except Exception as _le:
                            _ltext = f"❌ 本地执行失败：{str(_le)[:120]}"
                            _lok = False
                        _ltexts.append(str(_ltext or "")[:2000])
                    summary = "\n\n".join(t for t in _ltexts if t.strip())[:2000]
                    if len(summary.strip()) == 0:
                        summary = f"任务 {_objective[:60]}无可用结果。"
                        _lok = False
                    facts = tuple(s.strip() for s in summary.split("\n") if s.strip())[:10]
                    evidence = ()
                    uncertainties = () if _lok else ("本地直执失败",)
                    error_code = None if _lok else "LOCAL_EXEC_FAILED"
                    error = "" if _lok else summary[:200]
                    status = "completed" if _lok else "failed"
                    logger.info(
                        "Dispatcher 轻任务直执 task=%s tools=%s elapsed=%.1fs ok=%s",
                        task_id, sorted(_narrow or ()), time.time() - _lt0, _lok,
                    )
                elif _task_weight == "subagent":
                    # SUBAGENT tier：单发型检索（web_search + 可并行的轻工具），
                    # 按 family 委派 Hybrid subagent 并发单发，不经 agent 多轮。
                    from ..intelligence.tools import TOOL_EXECUTORS as _SUB_EXEC
                    from ..transport.subagent import dispatch_hybrid_subagents as _hybrid
                    _ut0 = time.time()
                    _uctx = {
                        "bot_id": bot_id,
                        "group_id": str(group_id or ""),
                        "requester_id": requester_id,
                        "user_id": requester_id,
                        "_search_notice_sent": True,
                    }
                    _sub_results: list = []
                    _sub_err = ""
                    try:
                        _sub_results = await asyncio.wait_for(
                            _hybrid(build_subagent_subtasks(_narrow, _objective), _SUB_EXEC, _uctx),
                            120.0,
                        )
                    except Exception as _se:
                        _sub_err = str(_se)[:120]
                    _stexts = [
                        str(r.get("result", "") or "")[:2000]
                        for r in _sub_results
                        if isinstance(r, dict)
                    ]
                    _surls: list[str] = []
                    for r in _sub_results:
                        if isinstance(r, dict):
                            _surls.extend(str(u) for u in (r.get("urls", []) or []))
                    summary = "\n\n".join(t for t in _stexts if t.strip())[:2000]
                    if len(summary.strip()) == 0 or len(_sub_results) == 0 or len(_sub_err) > 0:
                        summary = f"任务 {_objective[:60]}无可用结果。"
                        if _sub_err:
                            summary += f"（{_sub_err}）"
                        _sub_ok = False
                    else:
                        _sub_ok = True
                    facts = tuple(s.strip() for s in summary.split("\n") if s.strip())[:10]
                    evidence = tuple(_surls[:5])
                    uncertainties = () if _sub_ok else ("subagent 直执失败",)
                    error_code = None if _sub_ok else "SUBAGENT_FAILED"
                    error = "" if _sub_ok else summary[:200]
                    status = "completed" if _sub_ok else "failed"
                    logger.info(
                        "Dispatcher subagent 委派 task=%s tools=%s elapsed=%.1fs ok=%s",
                        task_id, sorted(_narrow or ()), time.time() - _ut0, _sub_ok,
                    )
                else:
                    with task_scope(_task_allow, trigger_uid=requester_id):
                        # 后台任务调 pixiv_search 发图时，执行器与 dispatcher 不在
                        # 同一 asyncio task，contextvars 读不到，只能靠模块级
                        # _shared_context。这里把本任务的 last_bot/last_event 预填进去，
                        # 否则 pixiv_search 因无 bot/event 回退纯文本 → agent 却说“图已发”。
                        _sticker_set = False
                        try:
                            _sctx0 = None
                            try:
                                _sctx0 = self._scheduler._contexts.get(
                                    self._scheduler._make_ctx_key(bot_id, int(group_id))
                                )
                            except Exception:
                                _sctx0 = None
                            _ev0 = self._origin_events.get(task_id) or (
                                getattr(_sctx0, "last_event", None) if _sctx0 else None
                            )
                            _bot0 = getattr(_sctx0, "last_bot", None) if _sctx0 else None
                            if _bot0 is not None and _ev0 is not None:
                                try:
                                    from ..service.sticker_sender import set_sticker_context
                                    set_sticker_context(_bot0, _ev0)
                                    _sticker_set = True
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        http_result = None
                        try:
                            from .local_runner import run_local_agent
                            from .task_routing import resolve_task_route
                            # 任务路由表：按目标与能力域分档给超时，缺键回落 general
                            _route = resolve_task_route((task_brief or {}).get("objective", ""), _task_allow)
                            _route_timeout = float(_route.get("timeout_secs", 180.0) or 180.0)
                            logger.info("Dispatcher 路由 task=%s route=%s tier=%s timeout=%.0f",
                                        task_id, _route.get("route"), _route.get("tier"), _route_timeout)
                            # 本地执行器：in-process 多轮工具循环（allowlist 真过滤 + must 硬约束）
                            http_result = await run_local_agent(
                                content=_task_prompt,
                                bot_id=bot_id,
                                group_id=str(group_id or ""),
                                requester_id=str(requester_id),
                                objective=str(task_brief.get("objective", "") or ""),
                                allow=_task_allow,
                                must=_must,
                                timeout_secs=_route_timeout,
                            )
                        except Exception as _he:
                            logger.debug("本地执行器尝试失败 %s", _he)
                            http_result = None
                        finally:
                            if _sticker_set:
                                try:
                                    from ..service.sticker_sender import clear_sticker_context
                                    clear_sticker_context()
                                except Exception:
                                    pass

                    # F4: 执行结果可观测（之前失败只剩 status，进 loop 排障）
                    try:
                        _hr = http_result or {}
                        logger.info(
                            "Dispatcher 执行结果 task=%s ok=%s finish=%s text_len=%d ev=%d err=%s",
                            task_id, _hr.get("ok"), _hr.get("finish_reason"),
                            len(str(_hr.get("text") or "")),
                            len(_hr.get("events") or []),
                            str(_hr.get("error") or "")[:120],
                        )
                    except Exception:
                        pass

                    # 超时记次：连续超时转暂离；长任务提前提醒一次
                    try:
                        _away = _note_timeout_finish(bot_id, str(group_id), requester_id,
                                                     _hr.get("finish_reason"))
                        if _away:
                            logger.info("Dispatcher 暂离 task=%s (连续超时)", task_id)
                        elif _route_timeout >= 150.0:
                            logger.info("Dispatcher 长任务提醒 task=%s route=%s 预计较久",
                                        task_id, _route.get("route"))
                    except Exception:
                        pass

                    # ── 找图无图兜底 (2026-10-16 绯雪找图案) ──
                    # allow 含 pixiv_search 但 agent 一次没调 → 空聊交卷。
                    # 在进程内直搜直发 (确定性, 圣经 tag, 不烧 LLM); 发出后 persona
                    # 只需报数。无 tag/无 token/搜不到/发不出 → 诚实说明, 不抛异常。
                    _pixiv_fb_sent = 0
                    _fb_tag = ""
                    _fb_recent_skip = False
                    try:
                        _fb_evs = (_hr.get("events") or []) if isinstance(_hr, dict) else []
                        _fb_want = "pixiv_search" in set(_task_allow or ())
                        _fb_had = _events_had_tool(_fb_evs, "pixiv")
                        if _fb_want and not _fb_had:
                            try:
                                from astrbot_plugin_suli_services.pixiv_search import (
                                    has_recent_pixiv_sends as _fb_recent,
                                )
                                if _fb_recent():
                                    logger.info(
                                        "Dispatcher 找图兜底跳过 task=%s (120s 内已有发图)",
                                        task_id)
                                    _fb_had = True
                                    _fb_recent_skip = True
                            except Exception:
                                logger.debug(
                                    "Dispatcher 找图近期检查跳过 task=%s",
                                    task_id, exc_info=True)
                        if _fb_want and not _fb_had:
                            from ..service.character_bible import lookup as _fb_bible
                            _fb_q = str(task_brief.get("objective", "") or "")[:200]
                            _fb_hit = _fb_bible(_fb_q) if _fb_q else None
                            _fb_tag = ""
                            if _fb_hit:
                                _fb_tag = (str(_fb_hit.get("pixiv") or "").strip()
                                           or str(_fb_hit.get("danbooru") or "").strip())
                            if _fb_tag:
                                from ..service.bot_config import get_config_service as _fb_gcs
                                _fb_svc = _fb_gcs()
                                _fb_token = ""
                                try:
                                    _fb_token = (
                                        _fb_svc.get(_fb_svc._bot_key(bot_id, "pixiv_refresh_token"), "")
                                        or _fb_svc.get("pixiv_refresh_token", ""))
                                except Exception:
                                    pass
                                if _fb_token:
                                    from astrbot_plugin_suli_services.pixiv_search import (
                                        download_pixiv_image as _fb_dl,
                                        mark_illust_shown as _fb_mark,
                                        resize_for_qq as _fb_rs,
                                        search_pixiv as _fb_search,
                                    )
                                    _fb_res = await _fb_search(
                                        query=_fb_tag, refresh_token=_fb_token, count=3)
                                    _fb_ctx = None
                                    try:
                                        _fb_ctx = self._scheduler._contexts.get(
                                            self._scheduler._make_ctx_key(bot_id, int(group_id)))
                                    except Exception:
                                        pass
                                    _fb_ev = (self._origin_events.get(task_id) or (
                                        getattr(_fb_ctx, "last_event", None) if _fb_ctx else None))
                                    _fb_bot = (getattr(_fb_ctx, "last_bot", None)
                                               if _fb_ctx else None)
                                    if _fb_bot is not None and _fb_ev is not None:
                                        from .._astrbot_adapter import MessageSegment as _FBMSeg
                                        import pathlib as _fb_pl
                                        import tempfile as _fb_tmp
                                        _fb_dir = _fb_pl.Path(_fb_tmp.gettempdir()) / "pixiv_fb"
                                        _fb_dir.mkdir(parents=True, exist_ok=True)
                                        for _it in (_fb_res or [])[:3]:
                                            if not isinstance(_it, dict):
                                                continue
                                            _iu = (str(_it.get("url_original") or "")
                                                   or str(_it.get("url_large") or "")
                                                   or str(_it.get("url_medium") or ""))
                                            if not _iu:
                                                continue
                                            try:
                                                _lp = await _fb_dl(_iu, str(_fb_dir))
                                                _lp = _fb_rs(_lp)
                                                await _fb_bot.send(
                                                    _fb_ev, _FBMSeg.image(f"file:///{_lp}"))
                                                _pixiv_fb_sent += 1
                                                try:
                                                    _fb_mark(int(_it.get("id") or 0))
                                                except Exception:
                                                    pass
                                            except Exception:
                                                continue
                                    if _pixiv_fb_sent:
                                        logger.info(
                                            "Dispatcher 找图兜底直发 task=%s n=%d tag=%s",
                                            task_id, _pixiv_fb_sent, _fb_tag)
                    except Exception:
                        logger.debug("Dispatcher 找图兜底跳过 task=%s", task_id, exc_info=True)

                    # F2: 任务路图字节直发 QQ（主分支）
                    await _flush_draw_images()

                    # 开画钩子善后：fire 过已被执行器删除，没 fire 的丢弃防串单
                    with contextlib.suppress(Exception):
                        _hook_fired = bool(_pop_hook(_hook_key))

                    if http_result is not None:
                        classified = classify_task_result(http_result, task_brief.get("objective", ""))
                        status = classified["status"]
                        summary = classified["summary"]
                        facts = classified["facts"]
                        evidence = classified["evidence"]
                        uncertainties = classified["uncertainties"]
                        error_code = classified["error_code"]
                        error = classified.get("error", "")
                        if _pixiv_fb_sent:
                            # 2026-10-17 真珠找图事故: 原实现只把注记“追加”到执行失败叙事
                            # 末尾，人格化模型被前段(认证错误/未挂载/没找到)带走 —— 群里
                            # 出现“一张图都没捞回来”与已发 3 张图直接矛盾。改为交付口径
                            # 覆写: summary/facts 只报已直发事实，证据/不确定项清空，
                            # 不给 persona 复述基础设施故障的素材。
                            _fb_tag_txt = f"（tag: {_fb_tag}）" if _fb_tag else ""
                            summary = (
                                f"任务已完成交付：已找到 {_pixiv_fb_sent} 张 Pixiv 同人图"
                                f"{_fb_tag_txt}，并直接发送到群里。"
                            )
                            facts = (
                                f"已直发 {_pixiv_fb_sent} 张 Pixiv 图到群里",
                            ) + tuple(facts or [])[:9]
                            evidence = ()
                            uncertainties = ()
                            status = "completed"
                        elif _fb_recent_skip:
                            # 续办单撞 120s 去重窗: 图已在刚才直发，同样按交付口径报数，
                            # 不让续办单再说“没找到”。
                            summary = (
                                "任务已交付：图片已经在刚才直接发送到群里"
                                "（120 秒去重窗内不重复发图）。"
                            )
                            facts = (
                                "图片已在刚才直发到群里（120 秒去重窗）",
                            ) + tuple(facts or [])[:9]
                            evidence = ()
                            uncertainties = ()
                            status = "completed"
                    else:                        # 兜底：本地执行器未产出结果 (异常等)
                        # D03: 无执行结果即 failed — 绝不合成成功。
                        status = "failed"
                        summary = f"任务 {task_brief.get('objective','')}未能执行（执行器无可用结果），请稍后重试。"
                        facts = ()
                        evidence = ()
                        uncertainties = ("执行器无可用结果",)
                        error_code = "EXECUTION_FAILED"
                        error = "本地执行器无可用结果"

                elapsed = time.time() - t0
                # 取消 ack 若 250ms 内完成（已在 ack 逻辑中检查，额外 cancel）
                try:
                    if ack_task and not ack_task.done():
                        # 若已超 0.4s ack 已发，不 cancel；否则若快速完成则 cancel
                        if elapsed < 0.35:
                            ack_task.cancel()
                except Exception:
                    pass
                # D11: 投递前检查取消 — 已取消的任务不再复活为 completed/failed。
                try:
                    _cur = await ledger_call(self._ledger, "get", task_id)
                    _was_cancelled = bool(_cur is not None and _cur.status == "cancelled")
                except Exception:
                    _was_cancelled = False
                if _was_cancelled:
                    status = "cancelled"
                    summary = f"任务 {task_brief.get('objective','')}已取消，不再投递执行结果。"
                    facts = ()
                    evidence = ()
                    uncertainties = ("任务已取消",)
                    error_code = "CANCELLED"
                    error = "任务已取消"
                # ── 生图护栏: 声称成品 ≠ 真画了 (2026-10-12 事故) ──
                # 事故链: 目标文本没命中关键词网 → allowlist 宽集合 (20 个) → agent 空转
                # 170s → 编出「…人家画好了，蓝鲸身子加白围裙…」的成品描述, 零工具调用、
                # 零出图, 却以 completed 进群。这里用确定性证据兜底: 生图任务声称完成,
                # 但既无 (a) 本次新复述单, 也无 (b) 开画钩子 fire, 也无 (c) 真发出去的图
                # → 一律按失败投递, 由二次人格化如实说"没画成", 禁止假完成进群。
                if _draw_task and not _phase2 and status in ("completed", "partial"):
                    _draw_evidence = False
                    try:
                        from ..service.draw_confirm import peek_order as _peek_done
                        _spec_after = _peek_done(bot_id, requester_id)
                        _draw_evidence = bool(
                            _spec_after is not None
                            and float(getattr(_spec_after, "created_at", 0.0) or 0.0) >= t0
                        )
                    except Exception:
                        _draw_evidence = False
                    if not _draw_evidence and not _hook_fired and not _draw_sent[0]:
                        try:
                            from ..service.draw_confirm import looks_like_draw_claim as _claim
                            _claims = _claim(str(summary or ""))
                        except Exception:
                            _claims = False
                        if _claims:
                            logger.warning(
                                "Dispatcher 生图假完成拦截 task=%s: 无复述单/无开画钩子/无图, "
                                "文本却声称画好 → 按失败投递 text=%.160s",
                                task_id, str(summary or ""),
                            )
                            status = "failed"
                            summary = (
                                "这单没真画成：画师（生图工具）没被调用，没有任何成品。"
                                "需要就说一声，换条路重来。"
                            )
                            facts = ()
                            evidence = ()
                            uncertainties = ("生图工具未被真实调用（agent 未调用即声称完成）",)
                            error_code = "DRAW_NO_TOOL_CALL"
                            error = "生图任务无工具调用证据（未产出复述单/未开画/无图），判定为假完成"
                # ── 追问支撑：任务结论直写关注槽【当前对话脉络】 ──
                # 任务台账路不产 <thread_summary> 标签，这里用分类后 summary 兜底；
                # 失败单不写（防坏结论覆盖好脉络）。fail-safe：写不上不影响投递。
                if status in ("completed", "partial") and str(summary or "").strip():
                    try:
                        from ..intelligence.tools import store_thread_summary as _store_ts
                        _store_ts(bot_id, str(group_id), requester_id, summary)
                    except Exception:
                        logger.debug("任务结论写槽失败 task=%s", task_id, exc_info=True)
                await ledger_call(self._ledger, "update_status", task_id, status)
                from ..transport.task_events import new_event_id
                ev = TaskResultEvent(
                    event_id=new_event_id(),
                    seq=0,
                    bot_id=bot_id,
                    group_id=str(group_id),
                    task_id=task_id,
                    origin_message_id=origin_message_id,
                    requester_id=requester_id,
                    status=status,
                    summary=summary,
                    facts=facts,
                    evidence=evidence,
                    uncertainties=uncertainties if "uncertainties" in locals() else (),
                    artifacts=(),
                    suggested_answer_points=(),
                    elapsed=elapsed,
                    error_code=error_code if "error_code" in locals() else None,
                    error=error if "error" in locals() else "",
                    origin_event=self._origin_events.pop(task_id, None),
                )
                try:
                    # 图已直发成功时跳过二次人格化投递 (2026-10-14 用户点题：
                    # 发送成功不需要文字回复)。只看直发证据，不过问文本内容。
                    if _draw_task and _draw_sent[0] > 0 and status == "completed":
                        logger.info(
                            "Dispatcher 生图已直发，跳过文字投递 task=%s n=%d",
                            task_id, _draw_sent[0],
                        )
                        # 订单关闭 (2026-10-15)：图已发出即清场+登记，同单「执行」不再重开
                        try:
                            from ..service.draw_confirm import record_completed as _rec_done
                            _done_spec = None
                            for _cand in ("_pending_spec", "_spec_obj", "_spec_view"):
                                _done_spec = locals().get(_cand) or _done_spec
                                if _done_spec is not None:
                                    break
                            _rec_done(bot_id, requester_id, _done_spec)
                            logger.info("Dispatcher 订单关闭 task=%s", task_id)
                        except Exception:
                            pass
                    else:
                        ctx = self._scheduler._contexts.get(self._scheduler._make_ctx_key(bot_id, int(group_id)))
                        if ctx:
                            actor = self._actor_registry.get_or_create(bot_id, int(group_id), ctx, self._scheduler)
                            await actor.enqueue_task_result(ev)
                except Exception:
                    logger.exception("Dispatcher 投递 TaskResult 失败 task=%s", task_id)
                finally:
                    # D10: 无论投递成败，执行槽必须释放。
                    if _slot_held:
                        with contextlib.suppress(ValueError):
                            self._exec_sem.release()
            except asyncio.CancelledError:
                # D10/D11: 取消同样释放槽位，不吞取消语义。
                # 开画钩子一并丢弃（取消的任务不再 fire）。
                with contextlib.suppress(Exception):
                    _pop_hook(f"{bot_id}:{requester_id}")
                if _slot_held:
                    with contextlib.suppress(ValueError):
                        self._exec_sem.release()
                raise
            except Exception as e:
                logger.exception("Dispatcher worker 异常 task=%s", task_id)
                with contextlib.suppress(Exception):
                    _pop_hook(f"{bot_id}:{requester_id}")
                # 区分基础设施 vs 业务失败，前者为 INFRASTRUCTURE 且 retryable=False
                _exc_name = str(type(e).__name__)
                _is_infra = any(k in _exc_name for k in ("Import", "Config", "Type", "Value"))
                await ledger_call(self._ledger, "update_status", task_id, "failed")
                from ..transport.task_events import new_event_id
                ev = TaskResultEvent(
                    event_id=new_event_id(),
                    seq=0,
                    bot_id=bot_id,
                    group_id=str(group_id),
                    task_id=task_id,
                    origin_message_id=origin_message_id,
                    requester_id=requester_id,
                    status="failed",
                    summary=str(e)[:500] if not _is_infra else "任务执行服务当前不可用（基础设施故障），请稍后重试",
                    error_code="INFRASTRUCTURE" if _is_infra else "exception",
                    error=str(e)[:200],
                    elapsed=time.time()-t0,
                )
                try:
                    ctx = self._scheduler._contexts.get(self._scheduler._make_ctx_key(bot_id, int(group_id)))
                    if ctx:
                        actor = self._actor_registry.get_or_create(bot_id, int(group_id), ctx, self._scheduler)
                        await actor.enqueue_task_result(ev)
                except Exception:
                    pass
                finally:
                    if _slot_held:
                        with contextlib.suppress(ValueError):
                            self._exec_sem.release()

        worker_task = asyncio.create_task(_worker())
        self._handles[task_id].worker_task = worker_task
        return task_id

    async def cancel(self, task_id: str) -> bool:
        """D11: 取消任务 — 本地 worker/ack 停止 + 账本标记。

        本地执行循环与 worker 同任务链：cancel() 即打断执行循环，
        不产生结果复活；账本标记 cancelled 供审计。
        """
        h = self._handles.get(task_id)
        if h is not None:
            # kill worker
            if h.worker_task and not h.worker_task.done():
                h.worker_task.cancel()
            if h.ack_task and not h.ack_task.done():
                h.ack_task.cancel()
        self._origin_events.pop(task_id, None)
        # D10: 执行槽由 worker 各退出路径自行释放，此处只停任务不停槽。
        await ledger_call(self._ledger, "cancel", task_id)
        logger.info("Dispatcher 已取消 task=%s", task_id)
        return True
