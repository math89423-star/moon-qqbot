"""SubAgent 调度 — Main ↔ SubAgent 分工 (Hybrid 2026-08-27)

设计（用户裁定）：
  Main（GroupChatScheduler, 深度角色扮演）收到任务 → 按工具族派发 SubAgent → 自己保持群内活跃
  SubAgent 完成后 Main 汇总结果 → 统一角色扮演回复

  Main = tavern_client 直连，TOOLS=[]，完整 persona+worldbook，负责对话与任务分解
  SubAgent = 无 persona 纯执行器，只知道自己是 subagent，为 Main 服务

  Hybrid 分工：
    retrieval/code/pixiv/vision/image_gen → 原生 executors (无 LLM, 零 token, 极速)
    research → 本地无角色 agent (迭代工具循环，检索类工具 + LLM)

类比多代理分工：用户 → Main Agent → SubAgent(检索/代码/搜图/绘图/research) → Main 汇总 → 用户

工具族映射（与 tool_registry.TOOL_FAMILIES 互补，此处为执行侧分工）：
  retrieval: search_knowledge, web_search, parse_forwarded_message
  code:      execute_code
  pixiv:     pixiv_search, pixiv_tag_extract (如有)
  vision:    describe_image
  image_gen: generate_image, edit_image
  video:     fetch_video (如有)
  research:  本地无 persona agent (迭代工具循环)
  sticker:   send_sticker (本地，不走 SubAgent，直接 Main 处理)

每个 SubAgent 为独立 asyncio Task，带独立 tool_context 副本，日志前缀 SubAgent[family]。
Main 通过 asyncio.gather 等待同轮多 SubAgent 完成后，再让 LLM 合成最终回复。
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

# ── Artifact 存储 (P0: 避免 Main 上下文污染) ──
# 插件相对定位 (<repo>/AstrBot/data/...)，temp 目录兜底 (任意部署位置可用)。
try:
    _ARTIFACT_ROOT = Path(__file__).resolve().parents[2] / "AstrBot" / "data" / "subagent-artifacts"
except Exception:
    _ARTIFACT_ROOT = Path(tempfile.gettempdir()) / "subagent-artifacts"
_URL_RE = re.compile(r"https?://[^\s\)\]]+")


def _write_artifact(task_id: str, payload: dict) -> str:
    """落盘完整结果，返回 artifact 路径 (供 Main 按需读取)。"""
    try:
        _ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        _path = _ARTIFACT_ROOT / f"{task_id}.json"
        # 清理旧文件 (保留最近 100 个)
        try:
            _existing = sorted(_ARTIFACT_ROOT.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for _old in _existing[100:]:
                _old.unlink(missing_ok=True)
        except Exception:
            pass
        _path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(_path)
    except Exception as e:
        logger.warning("artifact 落盘失败 %s: %s", task_id, e)
        return ""


def _extract_urls(text: str) -> list[str]:
    return _URL_RE.findall(text)[:8]


def _concise_for_main(full: str, max_chars: int = 900) -> str:
    """压缩给 Main 的片段 — 保留前 max_chars + URLs，避免污染。"""
    if len(full) <= max_chars:
        return full
    return full[:max_chars] + f"...(截断，全文见 artifact，{len(full)}字)"

# ── 族映射 ──
FAMILY_MAP: dict[str, str] = {
    "search_knowledge": "retrieval",
    "web_search": "retrieval",
    "parse_forwarded_message": "retrieval",
    "execute_code": "code",
    "pixiv_search": "pixiv",
    "describe_image": "vision",
    "generate_image": "image_gen",
    "edit_image": "image_gen",
    "fetch_video": "video",
    "research": "research",  # 本地无 persona research worker
    "send_sticker": "sticker",  # 本地，不派发
}

# 反向：family → 工具列表（日志用）
FAMILY_TOOLS: dict[str, list[str]] = {}
for _tool, _fam in FAMILY_MAP.items():
    FAMILY_TOOLS.setdefault(_fam, []).append(_tool)

# 不走 SubAgent 的本地工具（零成本，直接 Main 执行）
LOCAL_FAMILIES = {"sticker"}


def family_of(tool_name: str) -> str:
    return FAMILY_MAP.get(tool_name, "generic")


async def run_as_subagent(
    family: str,
    tool_name: str,
    coro: Any,
    tool_context: dict | None = None,
) -> str:
    """包装单个工具调用为 SubAgent 任务，带日志与超时隔离。

    Args:
        family: 工具族（retrieval/code/...）
        tool_name: 具体工具名
        coro: 实际执行协程 awaitable
        tool_context: 透传上下文（用于日志）

    Returns:
        工具返回字符串
    """
    _start = time.monotonic()
    _cid = (tool_context or {}).get("group_id", "?") if tool_context else "?"
    logger.info("SubAgent[%s] 启动: %s (group=%s)", family, tool_name, _cid)
    try:
        result = await coro
        _elapsed = time.monotonic() - _start
        logger.info(
            "SubAgent[%s] 完成: %s (%.1fs, %d字)",
            family, tool_name, _elapsed, len(result) if isinstance(result, str) else 0,
        )
        return result
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _elapsed = time.monotonic() - _start
        logger.warning(
            "SubAgent[%s] 异常: %s (%.1fs) %s",
            family, tool_name, _elapsed, e, exc_info=True,
        )
        return f"❌ SubAgent[{family}] {tool_name} 执行失败: {e}"


# ── research SubAgent: 本地无 persona ──
async def run_local_research_subagent(
    task_content: str,
    tool_context: dict | None = None,
    must_tools: list[str] | None = None,
    suggested_tools: list[str] | None = None,
    timeout_secs: float = 150.0,
) -> str:
    """Hybrid research worker — 本地无 persona 执行。

    在 AstrBot 进程内跑多轮 function-calling 工具循环 (检索类工具 + LLM)，
    不依赖任何外置 runtime。

    Args:
        task_content: 任务描述 (含必要上下文切片)
        tool_context: 含 group_id/user_id/bot_id
        must_tools/suggested_tools: 约束工具选择 (suggested 即工具 allowlist)
        timeout_secs: 超时 (research 150s，SearXNG 8s×3 + 推理 40s 需放宽)

    Returns:
        结果文本或错误信息
    """
    ctx = tool_context or {}
    _bot_id = str(ctx.get("bot_id", ctx.get("self_id", "")))
    _group_id = str(ctx.get("group_id", "0"))
    _user_id = str(ctx.get("user_id", ctx.get("requester_id", "0")))
    if not _bot_id:
        # 回退：默认 bot (数据库真相源，单测兜底)
        try:
            from ..service.bot_identity import get_default_bot_id
            _bot_id = get_default_bot_id()
        except Exception:
            _bot_id = ""
    # 工具集: suggested 优先, 否则检索族默认
    _allow = [str(t).strip() for t in (suggested_tools or []) if str(t).strip()]
    if not _allow:
        _allow = ["search_knowledge", "web_search", "parse_forwarded_message", "describe_image"]
    try:
        from ..service.local_runner import run_local_agent

        _res = await run_local_agent(
            content=task_content,
            bot_id=_bot_id,
            group_id=_group_id,
            requester_id=_user_id,
            objective=str(task_content or "")[:200],
            allow=_allow,
            must=must_tools,
            tool_context=dict(ctx),
            max_rounds=6,
            timeout_secs=float(timeout_secs),
        )
    except Exception as e:
        logger.warning("research subagent 异常: %s", e, exc_info=True)
        return f"❌ research 子代理异常: {e}"
    if not _res.get("ok"):
        _err = str(_res.get("error") or "").strip() or "无返回"
        return f"❌ research 子代理失败: {_err}"
    _text = str(_res.get("text") or "").strip()
    if not _text:
        return "❌ research 子代理无返回"
    return _text


async def dispatch_tool_subagents(
    tool_calls: list[dict],
    executors: dict[str, Any],
    tool_context: dict | None = None,
) -> list[tuple[dict, str, str, str]]:
    """按族派发同轮多工具到 SubAgent 并并发等待。

    语义：Main 派发 → SubAgents 并发执行 → Main 汇总。

    Args:
        tool_calls: LLM 返回的 tool_calls 列表
        executors: {tool_name: async callable}
        tool_context: 透传给 executor 的上下文

    Returns:
        [(tool_call, func_name, args_str, result_str), ...] 保持原顺序
    """
    if not tool_calls:
        return []

    # 为每个 tool_call 创建 SubAgent 任务
    async def _one(tc: dict):
        _func = tc.get("function", {}).get("name", "")
        _args = tc.get("function", {}).get("arguments", "{}")
        _family = family_of(_func)
        # research 虚拟工具：直接走本地无 persona agent
        if _func == "research":
            import json

            try:
                _params = json.loads(_args) if isinstance(_args, str) else _args
            except Exception:
                _params = {}
            _task = _params.get("task", _params.get("query", _params.get("content", "")))
            if not _task:
                return tc, _func, _args, "❌ research 需要 task/query 参数"
            coro = run_local_research_subagent(
                task_content=_task,
                tool_context=tool_context,
                must_tools=_params.get("must_tools"),
                suggested_tools=_params.get("suggested_tools"),
                timeout_secs=float(_params.get("timeout", 90)),
            )
            _result = await run_as_subagent("research", "research", coro, tool_context)
            return tc, _func, _args, _result

        # 本地族不走 SubAgent，直接执行（避免额外调度）
        if _family in LOCAL_FAMILIES:
            _exec = executors.get(_func)
            if _exec is None:
                return tc, _func, _args, f"❌ 未知工具: {_func}"
            try:
                _res = await _exec({"_raw": _args}, tool_context=tool_context) if _func == "send_sticker" else await _exec(eval(_args) if isinstance(_args, str) else _args, tool_context=tool_context)
                # 上面 eval 仅为兜底，实际走 execute_tool_with_retry 路径
                return tc, _func, _args, _res
            except Exception as e:
                return tc, _func, _args, f"❌ {_func} 失败: {e}"

        # 通用 SubAgent 包装：通过 executors 调度
        _exec = executors.get(_func)
        if _exec is None:
            return tc, _func, _args, f"❌ 未知工具: {_func}"

        # 解析 args 为 dict（与 execute_tool_with_retry 保持一致）
        import json

        try:
            _params = json.loads(_args) if isinstance(_args, str) else _args
        except Exception:
            _params = {}

        # 构造协程
        coro = _exec(_params, tool_context=tool_context)
        _result = await run_as_subagent(_family, _func, coro, tool_context)
        return tc, _func, _args, _result

    # 并发派发，Main 保持活跃，等待 SubAgents 全部完成
    _tasks = [asyncio.create_task(_one(tc)) for tc in tool_calls]
    # 命名便于日志追踪
    for _t, _tc in zip(_tasks, tool_calls):
        _t.set_name(f"SubAgent-{family_of(_tc.get('function', {}).get('name','?'))}")

    results = await asyncio.gather(*_tasks)
    return list(results)


# ── Hybrid 高阶派发：Main 分解的 SubTasks → 并行 SubAgents ──
async def dispatch_hybrid_subagents(
    subtasks: list[dict],
    executors: dict[str, Any],
    tool_context: dict | None = None,
) -> list[dict]:
    """Main 分解后的混合派发 (Hybrid 核心)。

    subtasks 由 Main LLM 产生: [{"family":"retrieval","task":"...","args":{}}]
    本函数按 family 路由到原生 executors 或本地 research agent，并发执行后返回结果列表
    供 Main 合成角色扮演回复。

    Args:
        subtasks: Main 产出的任务列表
        executors: 原生工具 executors
        tool_context: 透传上下文

    Returns:
        [{"id":..., "family":..., "result":...}, ...] 保持原顺序
    """
    if not subtasks:
        return []

    async def _run_one(idx: int, st: dict) -> dict:
        _family = st.get("family", "retrieval")
        _task = st.get("task", st.get("query", st.get("content", "")))
        _args = st.get("args", {})
        _id = st.get("id", f"sub-{idx}")
        _tool = st.get("tool", "")
        _start = time.monotonic()
        # Hybrid 已发即时应答，抑制子代理内 web_search 的二次"帮你搜一下"刷屏
        if tool_context is not None and "_search_notice_sent" not in tool_context:
            tool_context["_search_notice_sent"] = True
        logger.info("SubAgent[%s] Hybrid 启动: %s (id=%s)", _family, str(_task)[:60], _id)
        try:
            if _family == "research":
                _res = await run_local_research_subagent(
                    task_content=_task,
                    tool_context=tool_context,
                    must_tools=_args.get("must_tools"),
                    suggested_tools=_args.get("suggested_tools"),
                    timeout_secs=float(_args.get("timeout", 150)),
                )
                _tool = "research"
            elif _family in ("retrieval", "code", "pixiv", "vision", "image_gen", "video"):
                # 映射到具体工具执行
                if not _tool:
                    # 默认映射
                    _default_tool = {
                        "retrieval": "web_search",
                        "code": "execute_code",
                        "pixiv": "pixiv_search",
                        "vision": "describe_image",
                        "image_gen": "generate_image",
                        "video": "fetch_video",
                    }.get(_family, "web_search")
                    _tool = _default_tool
                _exec = executors.get(_tool)
                if _exec is None:
                    _res = f"❌ 未知工具: {_tool}"
                else:
                    # _args 已是 dict，直接透传
                    _params = _args if isinstance(_args, dict) else {}
                    if _tool in ("web_search", "search_knowledge") and not _params.get("query"):
                        _params["query"] = _task
                    if _tool == "execute_code" and not _params.get("code"):
                        _params["code"] = _task
                    coro = _exec(_params, tool_context=tool_context)
                    _res = await run_as_subagent(_family, _tool, coro, tool_context)
            else:
                _res = f"❌ 未知 family: {_family}"
                _tool = _family
            _elapsed = time.monotonic() - _start
            # ── P0 artifact: 落盘完整结果，返回给 Main 的是压缩版 + metadata ──
            _urls = _extract_urls(_res) if isinstance(_res, str) else []
            _full_len = len(_res) if isinstance(_res, str) else 0
            _concise = _concise_for_main(_res if isinstance(_res, str) else str(_res))
            _artifact_payload = {
                "id": _id,
                "family": _family,
                "tool": _tool,
                "task": _task,
                "args": _args,
                "result": _res,
                "urls": _urls,
                "elapsed": round(_elapsed, 2),
                "timestamp": time.time(),
                "group_id": (tool_context or {}).get("group_id", ""),
            }
            _artifact_path = _write_artifact(f"{_id}-{int(time.time())}", _artifact_payload)
            logger.info("SubAgent[%s] Hybrid 完成: id=%s (%.1fs, %d字, urls=%d, artifact=%s)", _family, _id, _elapsed, _full_len, len(_urls), _artifact_path or "-")
            return {
                "id": _id, "family": _family, "tool": _tool, "task": _task,
                "result": _concise, "full_len": _full_len, "urls": _urls,
                "artifact": _artifact_path, "elapsed": round(_elapsed, 2),
                "_full_result": _res,  # 内部保留，execute_dispatch_subagents 可选使用
            }
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("SubAgent[%s] Hybrid 异常: id=%s %s", _family, _id, e, exc_info=True)
            return {"id": _id, "family": _family, "tool": _tool or _family, "task": _task, "result": f"❌ {e}", "urls": [], "artifact": ""}

    _tasks = [asyncio.create_task(_run_one(i, st)) for i, st in enumerate(subtasks)]
    for _t, _st in zip(_tasks, subtasks):
        _t.set_name(f"Hybrid-{_st.get('family','?')}")
    results = await asyncio.gather(*_tasks)
    return list(results)
