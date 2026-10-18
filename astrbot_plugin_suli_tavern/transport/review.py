"""Review 旁路 — 发送后记事本：脑标记 + 实发对齐，异步写长期库。

位置：回复发送后 fire-and-forget，不阻塞活路径。
复用现货：AsyncMemoryWriter 排队 + extract_and_distill 日常蒸馏。
外加发送后 QA：@幻觉 QQ 自动撤回纠正（零 LLM，规则判定）。
"""

import asyncio as _aio
import logging as _logging
import re as _re
import time as _time

_logger = _logging.getLogger(__name__)

# ── 撤回线状态（进程内有界） ──
_CQ_AT_RE = _re.compile(r"\[CQ:at,qq=(\d+)\]")
_recalled_mids: set[str] = set()          # 已撤回过的发送 id（防重复撤）
_recall_times: dict[str, list[float]] = {}  # group_id → 近10分钟撤回时间戳（限频）
_RECALL_WINDOW_SEC = 90.0   # 发出后多久内可撤（QQ 上限 2 分钟，留余量）
_RECALL_MAX_PER_10MIN = 3   # 每群每 10 分钟最多自动撤 3 次（防抽风刷屏）


async def maybe_recall_after_send(
    *, bot_wrapper, self_id: str = "", group_id: str = "",
    message_id: str = "", sent_text: str = "", sent_at: float = 0.0,
    trigger_uid: str = "", trigger_reason: str = "",
    peer_qq: str = "", recent_uids: set[str] | None = None,
    char_name: str = "",
) -> bool:
    """发送后 @幻觉检查 → 命中则撤回重发。返回 True=已撤回纠正。

    只动高精度的一类：@ 的 QQ 既不是触发者/自己/对照 bot，也没在最近
    上下文里出现过（LLM 现编的号码）。@错真人（双方都在群里）不断言，
    只记日志，留给影子下一轮纠正——误撤比不撤伤。
    全程 best-effort：拿不到 id、超窗、限频、API 失败一律静默跳过。
    """
    try:
        _gid = str(group_id or "").strip()
        _mid = str(message_id or "").strip()
        if not _gid.isdigit() or not _mid.isdigit() or _mid == "0":
            return False
        if bot_wrapper is None:
            return False
        if _time.time() - float(sent_at or 0.0) > _RECALL_WINDOW_SEC:
            return False
        if _mid in _recalled_mids:
            return False
        _ats = set(_CQ_AT_RE.findall(str(sent_text or "")))
        if not _ats:
            return False
        _self = str(self_id or "").strip()
        _trig = str(trigger_uid or "").strip()
        _peer = str(peer_qq or "").strip()
        _recent = set(recent_uids or set())
        _bad = sorted(
            q for q in _ats
            if q not in (_self, _trig, _peer) and q not in _recent
        )
        if not _bad:
            # @的全是真人（即使可能@错对象）→ 不自动撤，只记一笔
            _susp = sorted(q for q in _ats if q not in (_self, _peer) and q != _trig)
            if _susp:
                _logger.info("Review @存疑不断言 group=%s mid=%s ats=%s", _gid, _mid, _susp)
            return False
        # 限频：每群 10 分钟 3 次
        _now = _time.time()
        _hist = [t for t in _recall_times.get(_gid, []) if _now - t < 600.0]
        if len(_hist) >= _RECALL_MAX_PER_10MIN:
            _logger.warning("Review 撤回限频 group=%s mid=%s", _gid, _mid)
            return False
        # 执行撤回
        try:
            await bot_wrapper.call_action("delete_msg", message_id=int(_mid))
        except Exception:
            _logger.debug("Review delete_msg 失败 group=%s mid=%s", _gid, _mid, exc_info=True)
            return False
        _recalled_mids.add(_mid)
        if len(_recalled_mids) > 500:
            _recalled_mids.pop()
        _hist.append(_now)
        _recall_times[_gid] = _hist[-_RECALL_MAX_PER_10MIN:]
        # 纠正重发：mention/reply 语境大概率是想@触发者，换过去；否则去@+短道歉
        _fixed = str(sent_text or "")
        for _q in _bad:
            _fixed = _fixed.replace(f"[CQ:at,qq={_q}]", "")
        _fixed = _fixed.strip()
        if _trig and str(trigger_reason or "") in ("mention", "reply"):
            _fixed = f"[CQ:at,qq={_trig}] " + _fixed
            _head = ""
        else:
            _head = "（刚@错了，重发一下）"
        try:
            await bot_wrapper.call_action(
                "send_group_msg", group_id=int(_gid),
                message=(_head + _fixed) if _head else _fixed, auto_escape=False,
            )
        except Exception:
            _logger.debug("Review 纠正重发失败 group=%s", _gid, exc_info=True)
        _logger.info("Review 已撤回纠正 group=%s mid=%s bad=%s", _gid, _mid, _bad)
        return True
    except Exception:
        _logger.debug("Review 撤回线异常", exc_info=True)
        return False

_USER_CAP = 3
_USER_LEN = 30
_EPISODE_LEN = 60

_STARTED = False
_HANDLERS_DONE = False
_PENDING: set = set()


def _track(_t) -> None:
    _PENDING.add(_t)
    _t.add_done_callback(_PENDING.discard)


def _clean_str(v: object, cap: int) -> str:
    if isinstance(v, str):
        return v.strip()[:cap]
    return ""


def _clean_list(v: object, cap: int, item_cap: int) -> list[str]:
    if isinstance(v, str):
        _items: list[object] = [v]
    elif isinstance(v, list):
        _items = list(v[:cap])
    else:
        return []
    return [s for s in (_clean_str(x, item_cap) for x in _items) if s]


def _ops_to_dict(ops: object) -> dict:
    if isinstance(ops, dict):
        return dict(ops)
    _out: dict = {}
    try:
        _out = {
            "remember_user": getattr(ops, "remember_user", ""),
            "remember_episode": getattr(ops, "remember_episode", ""),
        }
    except Exception:
        _out = {}
    return _out


def build_review_spec(
    *,
    memory_ops: object = None,
    sent_text: str = "",
    task_objective: str = "",
    dedupe_key: str = "",
) -> dict:
    """纯裁决：拼 ReviewSpec。drop 为 True 表示无事可记，调用方直接跳过。"""
    _ops = _ops_to_dict(memory_ops)
    _facts = _clean_list(_ops.get("remember_user"), _USER_CAP, _USER_LEN)
    _ep = _clean_str(_ops.get("remember_episode"), _EPISODE_LEN)
    _sent = str(sent_text or "").strip()
    _task = str(task_objective or "").strip()
    if _ep == "" and _task != "":
        _ep = _task[:_EPISODE_LEN]
    return {
        "record_user": _facts,
        "record_episode": _ep,
        "drop": (len(_facts) == 0 and _ep == ""),
        "dedupe_key": str(dedupe_key or ""),
        "sent_len": len(_sent),
        "task_len": len(_task),
    }


async def _handle_user_fact(task) -> None:
    try:
        from astrbot_plugin_suli_memory import get_memory_store as _gms

        _store = _gms(str(task.bot_id or ""))
        if _store is None:
            return
        _meta = task.metadata or {}
        await _store.remember(
            str(task.user_id or ""),
            str(_meta.get("user_name", "") or ""),
            str(task.content or ""),
            category=str(_meta.get("category", "") or ""),
        )
    except Exception:
        _logger.debug("review fact 写盘异常", exc_info=True)


async def _llm_episode_keywords(bot_id: str, summary: str, anchor: str) -> list[str]:
    """后台 LLM 抽归档关键词（3-5 个可读词）。失败/超时返回 []，调用方回退规则。

    用 llm_lite flash 档，~150 token，不烧主卡。输出只要词本身，不要解释。
    """
    _summary = str(summary or "").strip()[:120]
    _anchor = str(anchor or "").strip()[:120]
    if not _summary and not _anchor:
        return []
    try:
        from ..service.bot_config import get_config_service as _gcs
        _llm_cfg = _gcs().resolve_background_llm(str(bot_id or ""), purpose="episodic_keywords")
    except Exception:
        return []
    if not _llm_cfg:
        return []
    _msgs = [
        {"role": "system", "content": (
            "你是中文关键词提取器。只输出关键词本身，不要解释、不要序号、不要emoji。\n"
            "规则：3-5个词，每个2-6个汉字或2-20个英文字符；优先人名/作品/地点/动作 "
            "（如角色名/作品名/成人隔离区/上位/主导权）；《》书名拆成短词；不要单字、"
            "不要语气词（爱吧/呢吗啊）、不要表情符号。词之间用逗号分隔，一行输出。"
        )},
        {"role": "user", "content": f"总结：{_summary}\n原话：{_anchor}"},
    ]
    try:
        import contextlib as _cl
        from ..service.tavern_client import TavernClient as _TC
        _tc = _TC()
        try:
            _raw = await _aio.wait_for(_tc.chat(
                messages=_msgs,
                model=_llm_cfg["model"],
                temperature=0.0,
                # 2026-10-17 修宽: 100 预算会被慢模型 CoT 吃光 (起步 60~500 token),
                # 抬到 512 保摘要完整; 超时 15→30s 同因。
                max_tokens=512,
                api_base=_llm_cfg["api_base"],
                api_key=_llm_cfg["api_key"],
                bot_id=str(bot_id or ""),
                extra_params=_llm_cfg.get("extra_params"),
                slot="episodic_keywords",
            ), timeout=30.0)
        finally:
            with _cl.suppress(Exception):
                await _tc.close()
    except Exception:
        _logger.debug("情节关键词 LLM 提取失败，回退规则", exc_info=True)
        return []
    import re as _re2
    _parts = _re2.split(r"[，,、\s|/／]+", str(_raw or ""))
    return [p.strip() for p in _parts if p.strip()][:5]


async def _handle_episode(task) -> None:
    try:
        from astrbot_plugin_suli_memory import get_episodic_store as _ges

        _store = _ges(str(task.bot_id or ""))
        if _store is None:
            return
        _meta = task.metadata or {}
        try:
            _gid = int(str(task.group_id or "0") or "0")
        except Exception:
            _gid = 0
        _content = str(task.content or "")
        _anchor = str(_meta.get("topic_anchor", "") or "")
        _kws = await _llm_episode_keywords(str(task.bot_id or ""), _content, _anchor)
        _store.archive(
            str(task.bot_id or ""),
            _gid,
            _content,
            participants=_meta.get("participants"),
            topic_anchor=_anchor,
            topic_keywords=_kws or None,
        )
    except Exception:
        _logger.debug("review episode 写盘异常", exc_info=True)


async def _ensure_writer() -> None:
    global _STARTED, _HANDLERS_DONE
    from ..context.async_memory_writer import get_memory_writer as _gmw

    _w = _gmw()
    if _HANDLERS_DONE:
        pass
    else:
        _w.register_handler("review_user_fact", _handle_user_fact)
        _w.register_handler("review_episode", _handle_episode)
        _HANDLERS_DONE = True
    if _STARTED:
        return
    await _w.start()
    _STARTED = True


async def schedule_review(scheduler, ctx, batch, shadow, sent_text: str = "", task_objective: str = "", sent_message_id: str = "", sent_at: float = 0.0) -> dict:
    """组装 ReviewSpec，排队写盘 + 触发蒸馏。全程 best-effort，异常只记日志。

    sent_message_id/sent_at：_emit_reply 直发回填的发送回执（旧路径为空则
    撤回线跳过）。有 id 且正文含 @ 时，顺手跑 @幻觉检查，命中自动撤回纠正。
    """
    _shadow: dict = shadow or {}
    _bot = str(getattr(batch, "bot_id", "") or "")
    _gid = str(getattr(batch, "group_id", "") or "")
    _uid = str(getattr(batch, "trigger_user_id", "") or "")
    _uname = str(getattr(batch, "trigger_user_name", "") or "?")
    _merged = str(getattr(batch, "merged_text", "") or "")
    _first = ""
    try:
        for _ev in batch.source_events:
            _mid = getattr(_ev, "message_id", "")
            _eid = getattr(_ev, "event_id", "")
            if _mid in (None, "", 0, "0"):
                if _eid:
                    _first = str(_eid)
                    break
            else:
                _first = str(_mid)
                break
    except Exception:
        _first = ""
    _key = f"{_bot}:{_gid}:{_first}"
    _spec = build_review_spec(
        memory_ops=_shadow.get("memory_ops"),
        sent_text=sent_text,
        task_objective=task_objective,
        dedupe_key=_key,
    )
    if _spec["drop"]:
        _logger.debug("Review 空，直接丢弃 group=%s key=%s", _gid, _key)
        return _spec
    await _ensure_writer()
    from ..context.async_memory_writer import MemoryWriteTask as _MWT  # noqa: N814
    from ..context.async_memory_writer import get_memory_writer as _gmw2

    _w = _gmw2()
    for _fact in _spec["record_user"]:
        _w.schedule_write(
            _MWT(
                task_type="review_user_fact",
                user_id=_uid,
                bot_id=_bot,
                group_id=_gid,
                content=_fact,
                metadata={"user_name": _uname, "dedupe_key": _key},
            )
        )
    if _spec["record_episode"] != "":
        _w.schedule_write(
            _MWT(
                task_type="review_episode",
                user_id=_uid,
                bot_id=_bot,
                group_id=_gid,
                content=str(_spec["record_episode"]),
                metadata={
                    "participants": [_uid],
                    "topic_anchor": _merged[:_EPISODE_LEN],
                    "dedupe_key": _key,
                },
            )
        )
    try:
        from astrbot_plugin_suli_memory import get_memory_store as _gms2

        _ms = _gms2(_bot)
        _tm = getattr(scheduler, "_tier_manager", None)
        if _ms is None:
            pass
        else:
            from .context_lifecycle import extract_and_distill as _ead

            _track(_aio.create_task(_ead(ctx, _uid, memory_store=_ms, tier_manager=_tm)))
    except Exception as _ee:
        _logger.debug("Review 蒸馏排队异常 err=%s", _ee)
    _logger.info(
        "Review 已排队 group=%s key=%s facts=%d episode=%s",
        _gid,
        _key,
        len(_spec["record_user"]),
        _spec["record_episode"] != "",
    )
    # ── 发送后 @幻觉检查（零 LLM，有回执 id 且正文含 @ 才跑） ──
    try:
        if sent_message_id and "@" not in str(sent_text or "") and "[CQ:at" not in str(sent_text or ""):
            pass
        elif sent_message_id and str(sent_text or "").strip():
            _recent: set[str] = set()
            try:
                for _m in list(getattr(ctx, "messages", []) or [])[-80:]:
                    _u = str((_m or {}).get("user_id", "") or "")
                    if _u and not _u.startswith("bot_"):
                        _recent.add(_u)
            except Exception:
                _recent = set()
            _peer = ""
            try:
                _peer = str(getattr(getattr(scheduler, "_config", None), "peer_bot_qq", "") or "")
            except Exception:
                _peer = ""
            _cname = ""
            try:
                _cname = str((getattr(scheduler, "_resolve_character", lambda *a, **k: {})(
                    _bot) or {}).get("name", "") or "")
            except Exception:
                _cname = ""
            _track(_aio.create_task(maybe_recall_after_send(
                bot_wrapper=getattr(ctx, "last_bot", None),
                self_id=_bot, group_id=_gid,
                message_id=str(sent_message_id or ""), sent_text=str(sent_text or ""),
                sent_at=float(sent_at or 0.0),
                trigger_uid=_uid,
                trigger_reason=str(getattr(batch, "trigger_reason", "") or ""),
                peer_qq=_peer, recent_uids=_recent, char_name=_cname,
            )))
    except Exception:
        _logger.debug("Review 撤回检查排队异常", exc_info=True)
    return _spec
