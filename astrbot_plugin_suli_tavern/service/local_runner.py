"""本地任务执行器 — in-process 多轮工具循环 (后台无 persona 任务用)。

替代外置 agent runtime: 在 AstrBot 进程内直接跑 function-calling 循环,
供 dispatcher 重任务分支 / subagent research 复用。

结果契约 (与各执行器统一):
    {"ok": bool, "finish_reason": str, "text": str, "events": [...], "error": str}

- events 为工具调用记录 (classify 分类 / 绘图兜底判定 / 结果打捞用)，
  形如 {"type": "tool_result", "tool": name, "result": ..., "elapsed": ...}。
- 永不抛异常 (CancelledError 除外，向上传递供任务取消)。
"""

from __future__ import annotations

import asyncio
import inspect
import time

from astrbot.api import logger

# 无 persona 执行器 system 提示 (纯执行口径，与人格化回复隔离)
_TASK_SYSTEM_PROMPT = (
    "你是后台任务执行代理，为 bot 完成用户委托的实际操作。\n"
    "规则:\n"
    "1. 能通过工具获取的事实必须真实调用工具，不得凭记忆编造。\n"
    "2. 完成所需操作后，用简洁的中文直接给出结果要点（事实、数字、链接），"
    "不要角色扮演口吻。\n"
    "3. 工具失败或信息不足时如实说明，不得编造替代结论。\n"
    "4. 不要输出思考过程，只给最终结果。"
)


def _record_executor(name: str, fn, events: list[dict]):
    """包装执行器: 记录工具调用事件 (供 classify / 兜底判定使用)。"""
    _takes_ctx = False
    try:
        _takes_ctx = "tool_context" in inspect.signature(fn).parameters
    except (ValueError, TypeError):
        _takes_ctx = False

    async def _wrapped(args, tool_context=None):
        _t0 = time.time()
        try:
            if _takes_ctx:
                _res = await fn(args, tool_context=tool_context)
            else:
                _res = await fn(args)
            _ok = True
        except Exception as e:  # noqa: BLE001
            _res = f"❌ {name} 执行异常: {str(e)[:160]}"
            _ok = False
        events.append({
            "type": "tool_result",
            "tool": name,
            "name": name,
            "args": str(args)[:200],
            "result": str(_res)[:400],
            "ok": _ok,
            "elapsed": round(time.time() - _t0, 2),
        })
        return _res

    return _wrapped


async def run_local_agent(
    *,
    content: str,
    bot_id: str,
    group_id: str = "",
    requester_id: str = "",
    objective: str = "",
    allow: object = None,   # frozenset|set|list|None — None = 全量工具
    must: object = None,    # 必须调用的工具名列表 (硬约束)
    tool_context: dict | None = None,  # 调用方上下文切片 (覆盖默认键)
    max_rounds: int = 6,
    temperature: float = 0.5,
    max_tokens: int = 2048,
    timeout_secs: float = 180.0,
) -> dict:
    """in-process 多轮工具循环 → 执行结果 dict (永不抛异常)。"""
    _content = str(content or "").strip()
    if not _content:
        return {"ok": False, "finish_reason": "error", "text": "",
                "events": [], "error": "空任务"}

    try:
        from ..intelligence.tools import TOOL_EXECUTORS, TOOLS
        from ..intelligence.tools import run_tool_loop
        from .tavern_client import TavernClient
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "finish_reason": "error", "text": "", "events": [],
                "error": f"执行组件不可用: {str(e)[:160]}"}

    _events: list[dict] = []

    # ── 工具选型: allowlist 过滤 (None = 全量), must 恒在 ──
    _allow = {str(t).strip() for t in (allow or []) if str(t).strip()} or None
    _must = [str(t).strip() for t in (must or []) if str(t).strip()]
    if _must:
        _allow = (_allow or set()) | set(_must)
    _tools = [
        t for t in TOOLS
        if _allow is None or t.get("function", {}).get("name") in _allow
    ]
    _execs: dict = {
        _n: _record_executor(_n, _fn, _events)
        for _n, _fn in TOOL_EXECUTORS.items()
        if _allow is None or _n in _allow
    }

    # ── must 硬约束: 注入显式指令 (无执行设施拼装时的本层兜底) ──
    if _must:
        _content += ("\n\n[系统约束] 本次任务必须真实调用以下工具后再作答: "
                     + "、".join(_must))

    _messages = [
        {"role": "system", "content": _TASK_SYSTEM_PROMPT},
        {"role": "user", "content": _content},
    ]
    _tool_context = {
        "bot_id": bot_id,
        "group_id": str(group_id or ""),
        "requester_id": str(requester_id or ""),
        "trigger_user_id": str(requester_id or ""),
        "objective": str(objective or "")[:200],
        "_search_notice_sent": True,
    }
    if tool_context:
        try:
            _tool_context.update(dict(tool_context))
        except Exception:
            pass

    _t0 = time.time()
    try:
        _text = await asyncio.wait_for(
            run_tool_loop(
                TavernClient(), _messages, _tools,
                max_rounds=max_rounds,
                temperature=temperature,
                max_tokens=max_tokens,
                user_id=str(requester_id or ""),
                bot_id=bot_id,
                tool_context=_tool_context,
                executors=_execs,
            ),
            timeout=float(timeout_secs) + 10.0,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "finish_reason": "timeout", "text": "",
                "events": _events, "error": "执行超时"}
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("本地执行器异常 bot=%s: %s", bot_id, e, exc_info=True)
        return {"ok": False, "finish_reason": "error", "text": "",
                "events": _events, "error": str(e)[:200]}

    _text = str(_text or "").strip()
    logger.info(
        "本地执行器完成 bot=%s ok=%s tools=%d ev=%d elapsed=%.1fs text=%d字",
        bot_id, bool(_text), len(_tools), len(_events),
        time.time() - _t0, len(_text),
    )
    if not _text and not _events:
        return {"ok": False, "finish_reason": "error", "text": "",
                "events": [], "error": "空响应"}
    return {"ok": True, "finish_reason": "stop", "text": _text,
            "events": _events, "error": ""}
