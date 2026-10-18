"""LLM API 客户端 + 角色卡加载器。

所有 LLM 调用直连 OpenAI 兼容 API。
角色卡从 characters/ 目录加载 JSON 文件 (兼容 SillyTavern 角色卡格式)。
世界书从 characters/ 目录加载 JSON 文件 (兼容 SillyTavern lorebook 格式)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# ── OpenCode Go 网关必需头 (https://opencode.ai/docs/go/#where-can-i-use-it) ──
# Go 是按会话路由/缓存的编码 agent 网关:
#   1. 必须随每请求发送稳定的 x-opencode-session (否则 400 MissingSessionID)
#   2. User-Agent 必须标识自身客户端, 不能用 aiohttp/SDK 默认名
_OPENCODE_SESSION_HEADER = "x-opencode-session"
_QQBOT_USER_AGENT = "suli-qqbot/1.0"


def _build_direct_headers(api_key: str, bot_id: str = "", session_id: str = "") -> dict[str, str]:
    """构建直连 API 请求头 (OpenCode Go 兼容)。

    session 优先级: 显式 session_id > bot_id 派生 > 全局 fallback。
    对非 Go 网关 (DeepSeek/OpenAI 等) 多余的自定义头会被忽略, 故无条件发送。
    """
    sid = (session_id or "").strip()
    if not sid:
        sid = f"suli-qqbot-{bot_id.strip()}" if (bot_id or "").strip() else "suli-qqbot-global"
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": _QQBOT_USER_AGENT,
        _OPENCODE_SESSION_HEADER: sid,
    }


# ── 上游安全审计降级 (2026-10-17 事故) ────────────────────────────────
# 事故: NSFW 人设卡 (system_prompt 15k + group_persona 8k) 在 OpenCode Go 网关
# 上游被判 security_audit_fail 400, 整轮嘴直接失败 (没回复, ok=False)。
# 实测探针 (2026-10-17, 同网关同人设):
#   普通人设+中性用户 → 200 | NSFW人设+中性用户 → 400 | 短system+露骨用户文本 → 200
#   → 触审源是「人设本身」, 不是用户内容; NSFW 人设前后半段都触 (多处犯规)。
# 同网关换模型实测 (嘴形态: 成人人设 + thinking disabled + max_tokens 400):
#   longcat-2.5-preview-free → 400 security_audit_fail (撞审)
#   deepseek-v4.1-flash      → 200, 正文正常输出
#   (glm-5.3 是 thinking-only, 不吃关思考方言; 且贵 → 不作备选)
# 处理: ① 撞审自动换到安全模型重试一次 (通用兜底, 任何 slot 都受益);
#       ② NSFW 区的嘴直接用安全模型, 不白白先撞一次 (nsfw_safe_model)。
_AUDIT_FALLBACK_MODEL: str = "deepseek-v4.1-flash"


def audit_fallback_model(current: str = "") -> str:
    """审计降级模型 (单模型, 非候选列表)。env SULI_AUDIT_FALLBACK_MODEL 可覆盖。"""
    _m = (os.getenv("SULI_AUDIT_FALLBACK_MODEL", "") or "").strip() or _AUDIT_FALLBACK_MODEL
    return "" if _m == current else _m


def is_audit_rejected(text: str) -> bool:
    """上游安全审计拦截判定 (security_audit_fail / 违规信息 / content policy)。"""
    t = str(text or "").lower()
    return ("security_audit_fail" in t or "违规信息" in t
            or "content_policy" in t or "content policy" in t)


def nsfw_safe_model(model: str = "") -> str:
    """NSFW 区用模型: 换实测不触审的模型 (人设含成人层, 审计会整轮打死)。

    env SULI_NSFW_MODEL 覆盖: 留空/缺省 → deepseek-v4.1-flash;
    填 "-" / "none" / "off" → 不换 (完全交回槽位配置)。
    """
    raw = (os.getenv("SULI_NSFW_MODEL", "") or "").strip()
    if raw.lower() in ("-", "none", "off"):
        return model
    if raw:
        return raw
    return audit_fallback_model("") or model


# 认 Anthropic thinking 方言的模型 keyword (2026-10-17 实测):
# longcat-2.5-preview-free —— {"thinking":{"type":"disabled"}} 可真正关思考
# (2 token / 3.6s); 而 reasoning_effort 各档对它只是装饰 (不报错、不调挡)。
# 其余 opencode 模型 (mimo 等) 仍走 reasoning_effort="none" 路线。
_ANTHROPIC_THINKING_MODELS: tuple[str, ...] = ("longcat",)


def _apply_opencode_thinking_dialect(api_base: str, extra_params: dict | None, model: str = "") -> dict | None:
    """OpenCode Go 网关 thinking 方言翻译 (2026-09-08 实测)。

    mimo-v2.5 默认开思考，且忽略 Anthropic 方言 {"thinking": {...}}。
    生产事故: SlimGate out=881/clen=377、MainDecision out=1439/clen=207，差额全是思考链。
    实测唯一有效开关是 OpenAI 方言 reasoning_effort="none" (out→~150, wall→~6s, 回复质量不变)。

    规则: 显式 reasoning_effort / 显式 thinking.enabled (前端思考开关) 一律尊重，
    其余 opencode 流量默认 none。非 opencode 网关 (DeepSeek 等) 保持原样，避免方言误伤。

    Responses 基址 (Muse Spark …/go/v1/responses): 网关拒收顶层 reasoning_effort
    (400 unknown parameter)，改走 Responses 原生 reasoning.effort 结构。
    合法值域以网关为准 [minimal, low, medium, high, xhigh, max] (实测 400 返回)，
    无 "none" 档 —— 关思考只能发 minimal (最小 CoT)。
    背景: SlimGate 发 thinking.disabled + max 1024, 省略 reasoning 后模型按默认
    档思考烧光 1024 → status=incomplete + 零 output_text → Gate 失明 fail-open。
    """
    _resp_efforts = ("minimal", "low", "medium", "high", "xhigh", "max")
    if "/responses" in (api_base or "").rstrip("/"):
        merged = dict(extra_params or {})
        thinking_obj = merged.pop("thinking", None)
        effort = merged.pop("reasoning_effort", None)
        if isinstance(thinking_obj, dict) and thinking_obj.get("type") == "enabled":
            effort = effort or "medium"  # 显式开思考 → 中档
        if effort in _resp_efforts:
            merged["reasoning"] = {"effort": effort}
        else:
            # 关思考/未知档 → minimal (网关无 "none", 省略则按默认档思考烧预算)
            merged["reasoning"] = {"effort": "minimal"}
        return merged
    if "opencode.ai" not in (api_base or ""):
        return extra_params
    # ── 2026-10-17 longcat 方言修正: 关思考 = thinking.disabled (唯一真开关) ──
    if any(k in (model or "").lower() for k in _ANTHROPIC_THINKING_MODELS):
        merged = dict(extra_params or {})
        if merged.get("reasoning_effort"):
            return merged  # 显式档位: 直传 (longcat 只当装饰, 不报错)
        thinking_obj = merged.get("thinking")
        if isinstance(thinking_obj, dict) and thinking_obj.get("type") == "enabled":
            return merged  # 显式开思考: 尊重
        merged.pop("reasoning_effort", None)
        merged["thinking"] = {"type": "disabled"}  # 默认关: longcat 唯一有效的关思考开关
        return merged
    if extra_params and extra_params.get("reasoning_effort"):
        return extra_params
    thinking_obj = (extra_params or {}).get("thinking")
    if isinstance(thinking_obj, dict) and thinking_obj.get("type") == "enabled":
        return extra_params
    merged = dict(extra_params or {})
    merged.pop("thinking", None)
    merged["reasoning_effort"] = "none"
    return merged


def _is_responses_base(api_base: str) -> bool:
    """是否为 Responses API 全路径基址 (Muse Spark: …/go/v1/responses)。"""
    return "/responses" in (api_base or "").rstrip("/")


# Responses 档位阶梯 (网关合法值域，无 none)。
# 空 output 重试时沿阶梯降一档: 实测空 output 只留 reasoning 块，
# 降档 = 少烧思考预算，正文更可能落地。
_EFFORT_LADDER = ("minimal", "low", "medium", "high", "xhigh", "max")


# ── 直连单发超时 + 对冲 (2026-10-15) ──────────────────────────
# 单发 total=60 (connect 12 / 建连 12 / 读空闲 50): 首字节 50s 不来即断。
# 对冲: 首发 _hedge_delay_secs() 秒无应答 → 发完全相同的第二发, 先回者胜,
# 另一发取消。慢网关才双倍 token, 快路径零额外成本。
# 尝试总预算 60s (与此前单发 total 一致 —— 最坏总耗时不增加)。
_ATTEMPT_BUDGET = 60.0
_HEDGE_DELAY_DEFAULT = 15.0


def _direct_req_timeout() -> "aiohttp.ClientTimeout":
    """单发超时对象 (集中一处, 契约测试可断言)。"""
    import aiohttp as _aiohttp

    return _aiohttp.ClientTimeout(total=60, connect=12, sock_connect=12, sock_read=50)


def _hedge_delay_secs() -> float:
    """对冲延迟秒数。env `SULI_HEDGE_DELAY_SECONDS` 覆盖, ≤0 关闭对冲。"""
    raw = (os.getenv("SULI_HEDGE_DELAY_SECONDS", "") or "").strip()
    if not raw:
        return _HEDGE_DELAY_DEFAULT
    try:
        return float(raw)
    except ValueError:
        return _HEDGE_DELAY_DEFAULT


async def _hedged_post(session, url: str, payload: dict, headers: dict,
                       hedge_delay: float, slot_label: str) -> dict:
    """一发 + 到期对冲, 先回者胜。返回解析后的 JSON dict。

    语义 (与原内联块一致):
      - 非 200 → warning + RuntimeError (速败, 不对冲, 直接给重试机)。
      - 响应体非 JSON → RuntimeError (速败)。
      - 传输异常 (断连/超时/取消) → 原样上抛 (重试机按原规则处理)。
      - 双发竞速: 先 settle 者 (结果或异常) 胜出, 另一发取消。
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)

    async def _one_round() -> dict:
        async with session.post(
            url, json=payload, headers=headers, timeout=_direct_req_timeout(),
        ) as resp:
            status = resp.status
            raw = await resp.read()
        if status != 200:
            try:
                text = raw.decode("utf-8", "replace")
            except Exception:
                text = ""
            _log.warning(
                "直连 API 非200: slot=%s status=%s body=%.300s",
                slot_label, status, text,
            )
            raise RuntimeError(f"直连 API 返回 {status}: {text[:300]}")
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise RuntimeError(
                f"直连 API 响应非 JSON (slot={slot_label}): {e}") from e

    first = asyncio.ensure_future(_one_round())
    hedge = None
    try:
        if hedge_delay > 0:
            try:
                return await asyncio.wait_for(asyncio.shield(first), timeout=hedge_delay)
            except asyncio.TimeoutError:
                if first.done():
                    # 首发在窗口内速败 (速断/速超时/400): 不是沉默, 直接抛,
                    # 不对冲 (同样的失败再发一遍毫无意义)。
                    return await first
                pass  # 真沉默 → 对冲
            # 速败 (400/速断) 与外层取消直接上抛, 不对冲
            hedge = asyncio.ensure_future(_one_round())
            _log.warning(
                "直连 API 首发 %.0fs 无应答, 对冲第 2 发 (slot=%s)",
                hedge_delay, slot_label,
            )
            done, _pending = await asyncio.wait(
                {first, hedge}, return_when=asyncio.FIRST_COMPLETED)
            # 双发同时落定 → 优先首发 (先发先出, 语义稳定)
            winner = first if first.done() else next(iter(done))
            try:
                return winner.result()
            finally:
                for t in ({first, hedge} - {winner}):
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await t
        else:
            return await first
    finally:
        for t in [x for x in (first, hedge) if x is not None]:
            if not t.done():
                t.cancel()


def _downgrade_reasoning_effort(payload: dict) -> str:
    """把 payload 里的 reasoning.effort 降一档，返回新档位（无则返回 "-"）。

    只用于「空 output 重试」这一条路径 —— 原档位已经拿不到正文，
    降档不会更差；拿到正文才是目的。
    """
    r = payload.get("reasoning")
    if not isinstance(r, dict):
        return "-"
    cur = str(r.get("effort") or "").strip().lower()
    new = _EFFORT_LADDER[max(0, _EFFORT_LADDER.index(cur) - 1)] if cur in _EFFORT_LADDER else "minimal"
    r["effort"] = new
    return new


_CTX_CEILING = 131072  # 与上游天花板同源（DeepSeek v4 pro/flash 均为 128K 上下文）


def _boost_output_budget(payload: dict) -> int:
    """空 output 重试时把输出预算翻倍，返回新预算（无预算键返回 -1）。

    根因（2026-10-15 空 output 事故）：minimal 档已是地板、降无可降，
    死因是输出预算不够思考+正文分。翻倍直击根因，不靠重采样撞运气。
    上限 16384，且不超上下文天花板（131072 - 输入估算×1.1），不破坏上游约束。
    """
    _key = "max_output_tokens" if "max_output_tokens" in payload else "max_tokens"
    try:
        _cur = int(payload.get(_key) or 0)
    except (TypeError, ValueError):
        return -1
    if _cur <= 0:
        return -1
    _chars = 0
    try:
        _body = payload.get("input", payload.get("messages", []))
        if isinstance(_body, list):
            for _m in _body:
                if isinstance(_m, dict):
                    _chars += len(str(_m.get("content", "") or ""))
                else:
                    _chars += len(str(_m or ""))
    except Exception:
        pass
    _ceiling = max(512, _CTX_CEILING - int(_chars * 1.1))
    _new = min(_cur * 2, 16384, _ceiling)
    if _new < _cur:
        return _cur
    payload[_key] = _new
    return _new


def _chat_messages_to_responses_input(messages: list[dict] | None) -> list[dict]:
    """Chat messages → Responses input (同为 role/content 结构, 非法 role 归 user)。"""
    _valid = ("system", "developer", "user", "assistant")
    return [
        {"role": m.get("role", "user") if m.get("role") in _valid else "user",
         "content": m.get("content") or ""}
        for m in (messages or []) if isinstance(m, dict)
    ]


def _openai_tools_to_responses_tools(tools: list[dict] | None) -> list[dict]:
    """Chat Completions tools → Responses function tools."""
    out: list[dict] = []
    for t in tools or []:
        fn = t.get("function", {}) if isinstance(t, dict) else {}
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append({
            "type": "function",
            "name": fn["name"],
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _parse_responses_output(data: dict) -> tuple[str | None, list[dict] | None, str]:
    """解析 Responses API 响应 → (content, tool_calls, finish_reason)。

    tool_calls 映射回 Chat 格式 {id/type/function{name,arguments}},
    下游 (tools.py 管线) 无需改动。
    """
    texts: list[str] = [
        c["text"]
        for item in data.get("output", []) or []
        if isinstance(item, dict) and item.get("type") == "message"
        for c in item.get("content", []) or []
        if isinstance(c, dict) and c.get("type") == "output_text" and c.get("text")
    ]
    # 拒识块（安全拒绝时网关可能以 refusal 类型返回，不收则拒绝变“空回复”）
    refusals: list[str] = [
        c["refusal"]
        for item in data.get("output", []) or []
        if isinstance(item, dict) and item.get("type") == "message"
        for c in item.get("content", []) or []
        if isinstance(c, dict) and c.get("type") == "refusal" and c.get("refusal")
    ]
    calls: list[dict] = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            calls.append({
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "") or "",
                },
            })
    content = "\n".join(texts) or None
    if content is None and refusals:
        content = "\n".join(refusals)
    status = data.get("status", "")
    finish = "stop" if status == "completed" else (status or "")
    return content, (calls or None), finish


def _dump_llm_exchange(
    tag: str, url: str, request: dict, response: object, note: str = "",
) -> None:
    """把一次 LLM 调用的请求体 + 原始响应落盘（排障用，默认关闭）。

    置 `SULI_LLM_DUMP=/tmp/llmdump` 后每次调用写一对文件。
    用途: 「网关返回空 output」这类事故只有原始报文能定性 —— 日志只能看到
    out_types，看不到 output 数组长什么样、reasoning 块里有什么。
    Authorization 头已掩码，不落 key。
    """
    _dir = (_os.getenv("SULI_LLM_DUMP", "") or "").strip()
    if not _dir:
        # 开关文件: docker exec <容器> touch /tmp/.llm_dump_on 即开，无需改 env 重启
        try:
            from pathlib import Path as _P0

            if not _P0("/tmp/.llm_dump_on").exists():
                return
        except Exception:
            return
        _dir = "/tmp/llm_dump"
    try:
        import json as _json
        import time as _t
        from pathlib import Path as _P

        _p = _P(_dir)
        _p.mkdir(parents=True, exist_ok=True)
        _stamp = f"{_t.strftime('%Y%m%d-%H%M%S')}-{int((_t.time() % 1) * 1000):03d}"
        _req = dict(request)
        _hdrs = dict(_req.get("headers") or {})
        for _k in ("Authorization", "authorization", "api-key"):
            if _hdrs.get(_k):
                _v = str(_hdrs[_k])
                _hdrs[_k] = (_v[:14] + "..." + _v[-4:]) if len(_v) > 20 else "***"
        _req["headers"] = _hdrs
        (_p / f"{_stamp}-{tag}-req.json").write_text(
            _json.dumps({"url": url, "note": note, **_req}, ensure_ascii=False, indent=2)[:8000000],
            encoding="utf-8",
        )
        (_p / f"{_stamp}-{tag}-resp.json").write_text(
            _json.dumps(response, ensure_ascii=False, indent=2)[:8000000],
            encoding="utf-8",
        )
        logger.warning("LLM dump 已落盘 dir=%s tag=%s", _dir, tag)
    except Exception:
        logger.debug("LLM dump 失败", exc_info=True)

# 角色卡目录
_CHAR_DIR = Path(__file__).resolve().parent.parent / "characters"


def _load_character_card(name: str = "") -> dict:
    """加载 JSON 角色卡文件。

    system_prompt 支持 txt 文件覆盖:
      如果 characters/{name}_persona_v2.txt 存在，
      system_prompt 从该 txt 文件读取（单一真相源），
      JSON 中的 system_prompt 字段被忽略。
      这样 txt 文件便于 diff / 编辑 / 审查，JSON 保留其余元数据。

    Args:
        name: 角色卡文件名 (不含扩展名)

    Returns:
        角色卡 data dict

    Raises:
        FileNotFoundError: 找不到角色卡 JSON 文件
        ValueError: 角色卡格式无效
    """
    path = _CHAR_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到角色卡文件: {path}")

    with open(path, encoding="utf-8") as f:
        card = json.load(f)

    data = card.get("data")
    if not data:
        # 容错：扁平格式（字段在顶层，无 data 包裹）也接受——曾出现外部写入
        # 把 V3 包裹抹平的事故。扁平即静默回退到默认人设，在此显式兼容并告警。
        if isinstance(card.get("system_prompt"), str) and card.get("name"):
            logger.warning(
                "角色卡 %s 是扁平格式 (缺少 data 包裹)，已兼容读取——请恢复 V3 包裹",
                path.name,
            )
            data = card
        else:
            raise ValueError(f"角色卡格式无效 (缺少 data 字段): {path}")

    # ── system_prompt txt 覆盖: 单一真相源 ──
    # 优先级: persona_v2.txt > JSON system_prompt
    persona_txt = _CHAR_DIR / f"{name}_persona_v2.txt"
    if persona_txt.exists():
        txt_content = persona_txt.read_text(encoding="utf-8").strip()
        if txt_content:
            data["system_prompt"] = txt_content
            logger.info(
                "角色卡 %s: system_prompt 已从 %s 加载 (%d 字符)",
                data.get("name"), persona_txt.name, len(txt_content),
            )

    logger.info("已加载角色卡: %s (v%s)", data.get("name"), data.get("character_version", "?"))
    return data


# ── 角色卡加载 (双 Bot 支持) ─────────────────────────────
#
# 配置方式 (优先级从高到低):
#   1. 环境变量 BOT_QQ_MAIN + BOT_CHAR_MAIN / BOT_QQ_ALT + BOT_CHAR_ALT (精确指定)
#   2. 自动扫描 characters/ 目录，按文件名排序依次分配给 QQ 号
#   3. 硬编码默认值 (通用回退)
import os as _os


def _builtin_fallback(name: str = "") -> dict:
    """最小后备角色卡 — 确保不会空启动。

    所有角色卡内容应从 JSON 文件加载。此函数仅提供最小骨架，
    当 JSON 文件完全不可用时兜底。
    """
    display_name = name.replace("_", " ").title()
    return {
        "name": display_name,
        "description": f"AI 助手 {display_name}",
        "system_prompt": f"你是{display_name}——一个通过 QQ 提供服务的 AI 助手。",
        "first_mes": "你好！有什么可以帮你的？",
        "personality": "",
        "scenario": "",
        "mes_example": "",
    }


def _discover_characters() -> dict[str, str]:
    """自动发现角色卡映射。

    优先级:
      1. BOT_CHAR_MAIN / BOT_CHAR_ALT 环境变量 (精确指定)
      2. 自动扫描 characters/ 目录，按文件名排序依次分配给 QQ 号
      3. 单角色卡 → 作为默认
    """
    mapping: dict[str, str] = {}
    main_qq = _os.getenv("BOT_QQ_MAIN", "") or _os.getenv("BOT_ID_MAIN", "")
    alt_qq = _os.getenv("BOT_QQ_ALT", "") or _os.getenv("BOT_ID_ALT", "")
    main_char = _os.getenv("BOT_CHAR_MAIN", "")
    alt_char = _os.getenv("BOT_CHAR_ALT", "")

    if main_qq and main_char:
        mapping[main_qq] = main_char
    if alt_qq and alt_char:
        mapping[alt_qq] = alt_char

    discovered: list[str] = []
    if _CHAR_DIR.exists():
        for f in sorted(_CHAR_DIR.glob("*.json")):
            stem = f.stem
            # 跳过: 世界书 / 示例 / 成人变体卡 ({card}_nsfw.json) / 荷尔蒙声线库 /
            # 手工备份 (xxx.json.1791608184.bak 之类)。
            # ★ 这几类文件都住在 characters/ 下, 但都不是「QQ → 基础角色卡」的候选。
            #   一旦混进来, 无环境变量时按文件名排序会顶掉真正的角色卡。
            if (stem.endswith(("_world_book", "_nsfw"))
                    or stem.startswith(("example", "hormone_"))
                    or ".bak" in f.name):
                continue
            discovered.append(stem)

    qq_slots = [q for q in (main_qq, alt_qq) if q and q not in mapping]
    for i, card_name in enumerate(discovered):
        if i < len(qq_slots):
            mapping[qq_slots[i]] = card_name
        elif not mapping:
            mapping["default"] = card_name
            break
    return mapping


# ── 确定 bot QQ → 角色卡映射 ──
# 环境变量优先 → BotIdentityService (DB) → 自动扫描
_main_qq = _os.getenv("BOT_QQ_MAIN", "") or _os.getenv("BOT_ID_MAIN", "")
_alt_qq = _os.getenv("BOT_QQ_ALT", "") or _os.getenv("BOT_ID_ALT", "")
_BOT_CARDS = _discover_characters()

_DEFAULT_MAIN_QQ = _main_qq or ""  # 向后兼容 (deprecated)
_DEFAULT_ALT_QQ = _alt_qq or ""    # 向后兼容 (deprecated)

CHARACTERS: dict[str, dict] = {}
if _BOT_CARDS:
    for _qq, _card_name in _BOT_CARDS.items():
        try:
            CHARACTERS[_qq] = _load_character_card(_card_name)
            logger.info("已加载角色卡: %s → QQ %s", _card_name, _qq)
        except (FileNotFoundError, ValueError) as e:
            logger.warning("加载角色卡 %s 失败: %s", _card_name, e)
            CHARACTERS[_qq] = _builtin_fallback(_card_name)
else:
    logger.info("无 BOT_CARDS 映射，将依赖 BotIdentityService 的角色卡加载")

DEFAULT_CHARACTER: dict = (
    CHARACTERS.get(_main_qq) or
    CHARACTERS.get(_alt_qq) or
    next(iter(CHARACTERS.values()), _builtin_fallback("character")) if CHARACTERS
    else _builtin_fallback("character")
)


def get_character_for_self_id(self_id: str) -> dict:
    """根据 bot 的 QQ 号 (self_id) 返回对应的角色卡。

    优先级:
      1. 已缓存的 CHARACTERS dict
      2. BotIdentityService → 角色卡文件名 → 加载
      3. DEFAULT_CHARACTER fallback

    Args:
        self_id: bot 的 QQ 号

    Returns:
        角色卡 data dict
    """
    sid = str(self_id)
    if sid in CHARACTERS:
        return CHARACTERS[sid]
    # 尝试从 BotIdentityService 解析角色卡
    try:
        from .bot_identity import get_bot_identity_service
        svc = get_bot_identity_service()
        card_name = svc.resolve_character_card(sid)
        if card_name:
            try:
                card = _load_character_card(card_name)
                CHARACTERS[sid] = card  # 缓存
                return card
            except Exception:
                pass
    except Exception:
        pass
    return DEFAULT_CHARACTER

# ── 世界书 (World Book / Lorebook) 加载 ───────────────────
# 委托给 context/world_book.py — 支持三模式触发 + 计时效果

from astrbot_plugin_suli_intelligence import (
    DEFAULT_SCAN_DEPTH,
    WorldBookEntry,
    load_world_book,
    scan_world_book_static,
)

_WB_ENTRIES: dict[str, list[WorldBookEntry]] = {}  # {bot_id: [WorldBookEntry]}  常规
_WB_ENTRIES_NSFW: dict[str, list[WorldBookEntry]] = {}  # {bot_id: [...]}  NSFW Zone 专用
_WB_SCAN_DEPTH = DEFAULT_SCAN_DEPTH


# NSFW 角色卡缓存: {card_name: (mtime, data|None)} — mtime 变化即重载 (原则 25)
_NSFW_CARD_CACHE: dict[str, tuple[float, dict | None]] = {}


def load_nsfw_character_card(card_name: str = "") -> dict | None:
    """加载 {card_name}_nsfw.json 的角色卡 data (带缓存)。缺失/损坏返回 None。

    唯一入口 — prompt_builder 与 main.py 私聊路径共用, 不要再各写一份路径推导。
    缓存以 mtime 为准: 改卡后无需重启容器即可生效 (原则 25: 启动/变更时自同步)。
    调用方在 async 上下文请用 asyncio.to_thread 包裹 (铁律 3: async 禁同步 I/O)。
    """
    if not card_name:
        return None
    path = _CHAR_DIR / f"{card_name}_nsfw.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _NSFW_CARD_CACHE[card_name] = (0.0, None)
        return None
    hit = _NSFW_CARD_CACHE.get(card_name)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    data: dict | None = None
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
        d = card.get("data")
        if not d and isinstance(card.get("system_prompt"), str) and card.get("name"):
            # 容错扁平格式 (同 _load_character_card)
            logger.warning("NSFW 角色卡 %s 是扁平格式，已兼容读取", path.name)
            d = card
        data = d or None
        if data:
            logger.info("NSFW 角色卡已加载: %s (%s)", path.name, data.get("name"))
    except Exception:
        logger.debug("NSFW 角色卡加载失败: %s", path.name, exc_info=True)
        data = None
    _NSFW_CARD_CACHE[card_name] = (mtime, data)
    return data


def _card_name_for_qq(qq: str) -> str:
    """bot QQ → 角色卡文件名 (不含扩展名)。空字符串 = 未知。

    fallback 链: _BOT_CARDS (env/发现) → BotIdentityService (DB)。
    这是「bot → 角色卡」推导的唯一入口——世界书 / NSFW 卡文件名都从它派生，
    不要在别处再写一遍映射 (原则 24)。
    """
    sid = str(qq or "")
    if not sid:
        return ""
    mapped = _BOT_CARDS.get(sid)
    if mapped:
        return str(mapped)
    try:
        from .bot_identity import get_bot_identity_service

        return str(get_bot_identity_service().resolve_character_card(sid) or "")
    except Exception:
        logger.debug("resolve_character_card 失败: bot=%s", sid, exc_info=True)
        return ""


def _load_wb_for(qq: str, card_name: str, *, nsfw: bool) -> None:
    """按卡片名加载世界书到对应字典。缺失文件静默跳过。"""
    if not qq or not card_name:
        return
    suffix = "_nsfw_world_book.json" if nsfw else "_world_book.json"
    path = _CHAR_DIR / f"{card_name}{suffix}"
    if not path.exists():
        if nsfw:
            logger.debug("无 NSFW 世界书 (跳过): %s", path.name)
        return
    entries = load_world_book(str(path))
    table = _WB_ENTRIES_NSFW if nsfw else _WB_ENTRIES
    table[str(qq)] = entries
    logger.info(
        "已加载世界书: %s → QQ %s (%d 条目)%s",
        path.name, qq, len(entries), " [NSFW]" if nsfw else "",
    )


# 模块加载时加载世界书 — 跟随角色卡映射 (常规 + NSFW 双份)
for _wb_qq, _wb_card_name in (_BOT_CARDS.items() if _BOT_CARDS else []):
    _load_wb_for(_wb_qq, _wb_card_name, nsfw=False)
    _load_wb_for(_wb_qq, _wb_card_name, nsfw=True)


def get_world_book_entries(bot_id: str = "", *, nsfw: bool = False) -> list[WorldBookEntry]:
    """取该 bot 的世界书条目。nsfw=True 时取 NSFW Zone 专用书。

    NSFW 书缺失时回退常规书 (fail-safe: 宁可少内容，不给错内容)。
    """
    sid = str(bot_id or "")
    if nsfw:
        entries = _WB_ENTRIES_NSFW.get(sid)
        if entries is not None:
            return entries
    return _WB_ENTRIES.get(sid, [])


def _scan_world_book(
    messages: list[dict[str, str]],
    bot_id: str = "",
    *,
    nsfw: bool = False,
) -> list[str]:
    """扫描消息中的关键词，返回命中的世界书条目内容列表。

    委托给 context/world_book.py 的无状态扫描。
    用于私聊/角色扮演等不需要状态追踪的场景。
    群聊请使用 WorldBookBuffer (有状态: sticky/cooldown/delay)。

    Args:
        bot_id: bot QQ 号 — 决定用哪份世界书。不传会退到默认 bot 的书 (历史行为，已弃用)。
        nsfw:   是否为 NSFW Zone 调用 — 走 NSFW 专用世界书。
    """
    return scan_world_book_static(
        messages,
        entries=get_world_book_entries(bot_id, nsfw=nsfw),
        scan_depth=_WB_SCAN_DEPTH,
    )


class TavernClient:
    """LLM API 客户端 — 直连 OpenAI 兼容 API。

    所有 LLM 调用直连 API。角色卡/世界书按 JSON 格式加载。
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        # 最近一次成功调用时间戳 (通道保活判空闲用, 见 group_chat._keepalive_loop)
        self._last_call_ts: float = 0.0
        # 最近一次 API 调用的 usage 数据 (调用方在 chat() 后读取)
        self._last_usage: dict[str, int] = {}

    def get_last_usage(self) -> dict[str, int]:
        """获取最近一次 API 调用的 token usage。

        Returns:
            {input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens, latency_ms}
        """
        return dict(self._last_usage)

    @property
    def last_call_ts(self) -> float:
        """最近一次成功调用的时间戳 (通道保活判空闲用)。"""
        return self._last_call_ts

    def _parse_and_store_usage(self, data: dict, latency_ms: int = 0) -> None:
        """从 API 响应中解析 token usage 并存储。

        兼容三种格式:
          DeepSeek (prompt_cache_hit/miss_tokens)、
          OpenAI Chat (prompt/completion_tokens)、
          OpenAI Responses (input/output_tokens + input_token_details.cached_tokens)。
        """
        usage = data.get("usage", {})
        if not usage:
            self._last_usage = {}
            return
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
        hit = usage.get("prompt_cache_hit_tokens", 0)
        if not hit:
            # OpenAI Responses 的 wire 字段是复数的 input_tokens_details;
            # 旧拼写 input_token_details 一并兜底 (部分网关两种都见得到)。
            details = (
                usage.get("input_tokens_details")
                or usage.get("input_token_details")
                or {}
            )
            hit = details.get("cached_tokens", 0)
        self._last_usage = {
            "input_tokens": prompt,
            "output_tokens": completion,
            "cache_hit_tokens": hit,
            "cache_miss_tokens": usage.get(
                "prompt_cache_miss_tokens", prompt - hit,
            ),
            "latency_ms": latency_ms,
        }

    @staticmethod
    def _new_session() -> aiohttp.ClientSession:
        """建一条新 session —— 池参数集中在一处，方便调。

        keepalive 15s: 别让空闲连接在池里躺太久 —— 躺着的那条可能已经被
        代理/网关单方面掐了, 复用即挂死 (2026-10-14)。
        """
        connector = aiohttp.TCPConnector(
            keepalive_timeout=15,
            limit=20,
            limit_per_host=10,
            enable_cleanup_closed=True,
        )
        return aiohttp.ClientSession(connector=connector, trust_env=True)

    async def _get_session(self) -> aiohttp.ClientSession:
        cur = self._session
        if cur is not None and not cur.closed:
            return cur
        async with self._session_lock:
            cur = self._session
            if cur is None or cur.closed:
                self._session = self._new_session()
                cur = self._session
        return cur

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── 凭证解析 ─────────────────────────────────────────

    @staticmethod
    def _resolve_credentials(
        api_base: str = "",
        api_key: str = "",
        bot_id: str = "",
    ) -> tuple[str, str]:
        """解析 API 凭证。显式参数优先，否则回退到指定 bot 的 LLM 配置。

        回退链: 显式参数 → bot_id 指定 → 全局默认 → 已知 bot QQ 号列表。
        """
        if api_base and api_key:
            return api_base, api_key
        try:
            from .bot_config import get_config_service
            svc = get_config_service()
            # 1. 按传入 bot_id
            if bot_id:
                active = svc.resolve_active_llm(bot_id)
                if active and active.api_key:
                    return active.normalized_base_url, active.api_key
            # 2. 全局默认
            active = svc.resolve_active_llm("")
            if active and active.api_key:
                return active.normalized_base_url, active.api_key
            # 3. 回退到第一个活跃 bot 的凭证 (bot_id 为空时的最后手段)
            from .bot_identity import get_bot_identity_service
            bots = get_bot_identity_service().list_bots(active_only=True)
            if bots:
                fallback_bot_id = bots[0].bot_id
                active = svc.resolve_active_llm(fallback_bot_id)
                if active and active.api_key:
                    return active.normalized_base_url, active.api_key
            # 4. 最终兜底: 直接取 llm_config 表中任意可用 LLM (忽略 active_llm_id)
            cfg = svc.get_first_available_llm()
            if cfg and cfg.api_key:
                return cfg.normalized_base_url, cfg.api_key
            # C3: 不再做跨 bot 凭证回退 — 每个 bot 应有独立配置或共享全局默认
            if bot_id:
                logger.warning("凭证解析失败: bot_id=%s 无独立配置且全局默认未设置", bot_id)
        except Exception:
            logger.warning("凭证回退解析失败", exc_info=True)
        return api_base, api_key

    # ── 公开 API ─────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str = "",
        temperature: float = 0.9,
        max_tokens: int = 512,
        provider: str = "",
        api_base: str = "",
        api_key: str = "",
        bot_id: str = "",
        extra_params: dict | None = None,
        session_id: str = "",
        quiet: bool = False,
        slot: str = "",
    ) -> str:
        """直连 OpenAI 兼容 API 生成回复 (无工具)。

        前端 6190 唯一真相源: model 必须来自面板 llm_config 槽位。
        空 model 时尝试从 bot_id 的 llm_lite/active 配置回退，前端无配置则直接返回空。
        quiet=True 时成功日志降到 debug（通道预热/保活这类后台 ping 用）。
        slot: 调用方命中的槽位名 (如 llm_primary/shadow_agent)，只进日志不影响路由。
        """
        from astrbot_plugin_suli_intelligence import optimize_messages
        # 模型必须来自前端 — 空时回退前端槽位，仍为空则拒绝调用
        if not model:
            try:
                from .bot_config import get_config_service
                svc = get_config_service()
                cfg = svc.resolve_llm_slot(bot_id, "llm_lite") or svc.resolve_llm_slot(bot_id, "llm_pro") \
                    or svc.resolve_active_llm(bot_id) or svc.resolve_active_llm("")
                if cfg and cfg.model_name:
                    model = cfg.model_name
                    logger.debug("chat(): 空 model 回退前端 llm_lite/active → %s (bot_id=%r)", model, bot_id)
            except Exception:
                pass
        if not model:
            logger.warning("chat(): model 为空且前端无可用槽位，拒绝调用 (bot_id=%r)", bot_id)
            return ""
        optimized = optimize_messages(messages, provider) if provider else messages
        api_base, api_key = self._resolve_credentials(api_base, api_key, bot_id)
        if not api_base or not api_key:
            logger.warning(
                "chat(): 无法解析 API 凭证, 返回空回复 (bot_id=%r)", bot_id,
            )
            return ""

        # 复用 _chat_via_direct_api (不带 tools)
        result = await self._chat_via_direct_api(
            optimized=optimized,
            tools=[],
            tool_choice="none",
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_params=extra_params,
            api_base=api_base,
            api_key=api_key,
            bot_id=bot_id,
            session_id=session_id,
            quiet=quiet,
            slot=slot,
        )
        return result.get("content") or "(空回复)"

    async def chat_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        tool_choice: str = "auto",
        model: str = "",
        temperature: float = 0.8,
        max_tokens: int = 512,
        provider: str = "",
        extra_params: dict | None = None,
        api_base: str = "",
        api_key: str = "",
        bot_id: str = "",
        session_id: str = "",
        slot: str = "",
    ) -> dict:
        """直连 OpenAI 兼容 API 生成回复 (支持 function calling)。

        前端 6190 唯一真相源: model 必须来自面板槽位，空 model 时回退前端并在无配置时拒绝。
        slot: 调用方命中的槽位名，只进日志不影响路由。
        """
        from astrbot_plugin_suli_intelligence import optimize_messages
        if not model:
            try:
                from .bot_config import get_config_service
                svc = get_config_service()
                cfg = svc.resolve_llm_slot(bot_id, "llm_lite") or svc.resolve_llm_slot(bot_id, "llm_pro") \
                    or svc.resolve_active_llm(bot_id) or svc.resolve_active_llm("")
                if cfg and cfg.model_name:
                    model = cfg.model_name
                    logger.debug("chat_with_tools(): 空 model 回退前端 → %s (bot_id=%r)", model, bot_id)
            except Exception:
                pass
        if not model:
            logger.warning("chat_with_tools(): model 为空且前端无可用槽位，拒绝调用 (bot_id=%r)", bot_id)
            return {"content": None, "tool_calls": None}
        optimized = optimize_messages(messages, provider) if provider else messages
        api_base, api_key = self._resolve_credentials(api_base, api_key, bot_id)
        if not api_base or not api_key:
            logger.warning(
                "chat_with_tools(): 无法解析 API 凭证, 返回空 (bot_id=%r)", bot_id,
            )
            return {"content": None, "tool_calls": None}

        return await self._chat_via_direct_api(
            optimized=optimized,
            tools=tools,
            tool_choice=tool_choice,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_params=extra_params,
            api_base=api_base,
            api_key=api_key,
            bot_id=bot_id,
            session_id=session_id,
            slot=slot,
        )

    async def _chat_via_direct_api(
        self,
        optimized: list[dict[str, str]],
        tools: list[dict],
        tool_choice: str,
        model: str,
        temperature: float,
        max_tokens: int,
        extra_params: dict | None,
        api_base: str,
        api_key: str,
        bot_id: str = "",
        session_id: str = "",
        quiet: bool = False,
        slot: str = "",
    ) -> dict:
        """绕过酒馆，直接调用 OpenAI 兼容 API (三方代理)。前端唯一真相源: model 非空校验。

        slot 只进日志 (调用方命中的槽位名)，不影响路由；未知调用方记 "-"。
        """
        if not model:
            raise RuntimeError("前端无可用模型 (model 为空)，拒绝直连 API 调用 — 请在 6190 面板配置 LLM 槽位")
        # ── OpenCode Go 网关 thinking 方言翻译: 无效的 thinking 方言转有效 none ──
        extra_params = _apply_opencode_thinking_dialect(api_base, extra_params, model)
        # ── 动态 max_tokens 双护栏: 地板 + 天花板 ──
        #     地板: 输入长时确保输出预算不枯竭 (TRAPS §八#7 旧修复)
        #     天花板: 输入+输出不超模型上下文窗口 (TRAPS §八#7 延续)
        #     DeepSeek v4: flash/pro 均为 128K 上下文 (之前按 v3 flash 32K 保守估计,
        #     导致 40K chars 输入时天花板压制到 512 → finish=length + 空 content)
        #     估算: 中文 ~1 tok/char, 10% 元数据开销
        _input_chars = sum(
            len(m.get("content") or "") for m in optimized
        )
        # 地板: 取 max_tokens 和 输入*0.15 的较大值, 上限按 reasoning_effort 调整
        #     CoT 思考 token 计入 max_tokens — 需要更大的地板才能容纳思考+输出
        #     xhigh/max 档 CoT 可达数千 token: 地板保底 8192、上限 16384
        #     (spark maxTokens=131072, 16384 远在其内, 网关安全)
        _effort = extra_params.get("reasoning_effort") if extra_params else None
        if not _effort and extra_params and isinstance(extra_params.get("reasoning"), dict):
            _effort = extra_params["reasoning"].get("effort")  # Responses 基址形态
        _slot_label = (slot or "-").strip() or "-"
        if _effort in ("xhigh", "max"):
            _floor_cap = 16384
            _effort_floor = 8192
        elif _effort == "high":
            _floor_cap = 8192
            _effort_floor = 0
        else:
            _floor_cap = 4096
            _effort_floor = 0
        _min_tokens = min(max(256, max_tokens, int(_input_chars * 0.15), _effort_floor), _floor_cap)
        _safe_max_tokens = _min_tokens

        # ── 天花板: 确保 estimated_input + max_tokens ≤ 模型上下文 ──
        _est_input_tokens = int(_input_chars * 1.1)  # 10% 元数据开销
        _ctx_ceiling = 131072  # DeepSeek v4 pro/flash 均为 128K 上下文
        _max_safe_output = max(512, _ctx_ceiling - _est_input_tokens)
        _safe_max_tokens = min(_safe_max_tokens, _max_safe_output)
        if _safe_max_tokens < max_tokens:
            logger.debug(
                "max_tokens 从 %d 天花板压制到 %d (输入估算=%d, slot=%s, 模型=%s, 上下文上限=%d, effort=%s)",
                max_tokens, _safe_max_tokens, _est_input_tokens, _slot_label, model, _ctx_ceiling,
                _effort or "-",
            )

        # ── reasoning_effort 保护: 输出预算不足时自动剥离 ──
        #    推理模型 (v4-pro/flash with reasoning_effort) 的 CoT 思考 token
        #    计入 max_tokens。上下文接近满载时 max_tokens 被天花板压制,
        #    CoT 吃掉全部预算 → finish=length + 空 content (TRAPS §八#7 延续)。
        #    剥离 reasoning_effort — 思考换不来输出就没意义了。
        #    CoT 开销: medium≈1500 tokens, high≈3000+, xhigh/max≈6000+ tokens
        _reasoning_stripped = False
        _protection_threshold = 2048  # 默认: 至少留 2048 给输出
        if _effort == "medium":
            _protection_threshold = 3072  # CoT 1500 + 输出 1500
        elif _effort == "high":
            _protection_threshold = 5120  # CoT 3000 + 输出 2000
        elif _effort in ("max", "xhigh"):
            _protection_threshold = 8192  # CoT 6000 + 输出 2000 (配 16384 地板)
        if (
            _safe_max_tokens < _protection_threshold
            and _effort
            and _effort not in ("low", "none", "minimal")
        ):
            _stripped_effort = extra_params.get("reasoning_effort")
            if not _stripped_effort and isinstance(extra_params.get("reasoning"), dict):
                _stripped_effort = extra_params["reasoning"].get("effort")
            extra_params = {k: v for k, v in extra_params.items() if k not in ("reasoning_effort", "reasoning")}
            _reasoning_stripped = True
            logger.warning(
                "reasoning 预算保护: 剥离 reasoning_effort=%s (slot=%s, 输出预算=%d < %d, "
                "输入估算=%d, 模型=%s)",
                _stripped_effort, _slot_label, _safe_max_tokens, _protection_threshold, _est_input_tokens, model,
            )

        url = api_base.rstrip("/")
        use_responses = _is_responses_base(api_base)
        if use_responses:
            # Muse Spark: base 本身就是完整 /responses endpoint, 不再拼接
            pass
        else:
            if not url.endswith("/v1"):
                url += "/v1"
            url += "/chat/completions"
        # OpenCode Go 网关要求 x-opencode-session + 自有 UA (缺头 → 400 MissingSessionID)
        headers = _build_direct_headers(api_key, bot_id=bot_id, session_id=session_id)
        if use_responses:
            payload = {
                "model": model,
                "input": _chat_messages_to_responses_input(optimized),
                "temperature": temperature,
                "max_output_tokens": _safe_max_tokens,
                "stream": False,
            }
            resp_tools = _openai_tools_to_responses_tools(tools)
            if resp_tools:
                payload["tools"] = resp_tools
                payload["tool_choice"] = tool_choice
        else:
            payload: dict = {
                "model": model,
                "messages": optimized,
                "temperature": temperature,
                "max_tokens": _safe_max_tokens,
                "stream": False,
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = tool_choice
        if extra_params:
            payload.update(extra_params)

        session = await self._get_session()
        _start = time.time()
        _max_retries = 2
        _last_error: Exception | None = None
        _hedge_delay = _hedge_delay_secs()
        for _attempt in range(_max_retries):
            try:
                # 对冲竞速 (尝试总预算内, 见 _hedged_post): 返回已解析 JSON。
                # 预算超时 → TimeoutError → 走下方超时降档分支。
                data = await asyncio.wait_for(
                    _hedged_post(session, url, payload, headers,
                                 _hedge_delay, _slot_label),
                    timeout=_ATTEMPT_BUDGET,
                )
                if use_responses:
                    content, tool_calls, finish_reason = _parse_responses_output(data)
                    if not content and not tool_calls:
                        try:
                            _types = ",".join(sorted({str(i.get("type")) for i in (data.get("output", []) or []) if isinstance(i, dict)})) or "-"
                        except Exception:
                            _types = "?"
                        logger.debug(
                            "直连 API 返回空 output (slot=%s, model=%s, status=%s, effort=%s, out_types=%s)",
                            _slot_label, model, data.get("status", "?"), _effort or "-", _types,
                        )
                        _dump_llm_exchange(
                            "empty", url, {"payload": payload, "headers": headers}, data, _types,
                        )
                        # 空 output 是预算不够死（思考吃掉全部配额，正文没落地），
                        # 只加预算不降档：降档损回答质量，而死因不在档位。
                        if _attempt < _max_retries - 1:
                            import asyncio as _aio

                            _old_budget = payload.get("max_output_tokens", payload.get("max_tokens"))
                            _new_budget = _boost_output_budget(payload)
                            logger.debug(
                                "直连 API 空 output 重试 (attempt %d/%d) slot=%s out_types=%s budget %s→%d",
                                _attempt + 1, _max_retries, _slot_label, _types, _old_budget,
                                _new_budget,
                            )
                            await _aio.sleep(0.6)
                            continue
                        raise RuntimeError(  # noqa: TRY003, TRY301 — 重试耗尽仍空：绝不记成功，调用方按失败处理
                            f"直连 API 返回空 output (重试耗尽 model={model} status={data.get('status', '?')})"
                        )
                else:
                    choices = data.get("choices", [])
                    if not choices:
                        raise RuntimeError("直连 API 返回空 choices")  # noqa: TRY003, TRY301 — 同上，非会话错误直接重抛

                    msg = choices[0].get("message", {})
                    content = msg.get("content", "")
                    tool_calls = msg.get("tool_calls", None)
                    finish_reason = choices[0].get("finish_reason", "")

                    if not content and not tool_calls:
                        logger.debug(
                            "直连 API 返回空 content 且无 tool_calls (slot=%s, model=%s, finish=%s, effort=%s)",
                            _slot_label, model, choices[0].get("finish_reason", "?"), _effort or "-",
                        )
                        _dump_llm_exchange(
                            "empty", url, {"payload": payload, "headers": headers}, data, "chat-empty",
                        )
                        if _attempt < _max_retries - 1:
                            import asyncio as _aio4

                            _old_budget = payload.get("max_output_tokens", payload.get("max_tokens"))
                            _new_budget = _boost_output_budget(payload)
                            logger.debug(
                                "直连 API 空 content 重试 (attempt %d/%d) slot=%s finish=%s budget %s→%d",
                                _attempt + 1, _max_retries, _slot_label,
                                choices[0].get("finish_reason", "?"), _old_budget,
                                _new_budget,
                            )
                            await _aio4.sleep(0.6)
                            continue
                        raise RuntimeError(  # noqa: TRY003, TRY301 — 重试耗尽仍空：绝不记成功
                            f"直连 API 返回空 content 且无 tool_calls (重试耗尽 model={model})"
                        )

                self._parse_and_store_usage(data, int((time.time() - _start) * 1000))
                self._last_call_ts = time.time()

                # 成功日志默认静默（每次 LLM 调用都打 warning 太刷屏，无害信息只进 debug）
                logger.debug(
                    "直连 API 成功: slot=%s model=%s effort=%s "
                    "tokens(in=%d/cache=%d out=%d) content_len=%d tool_calls=%d finish=%s",
                    _slot_label,
                    model,
                    _effort or "-",
                    self._last_usage.get("input_tokens", 0),
                    self._last_usage.get("cache_hit_tokens", 0),
                    self._last_usage.get("output_tokens", 0),
                    len(content or ""),
                    len(tool_calls) if tool_calls else 0,
                    finish_reason,
                )
                # 开关文件开启时，成功调用也留档一对（供与空 output 对比定性）
                if _os.getenv("SULI_LLM_DUMP", "") or Path("/tmp/.llm_dump_on").exists():
                    _dump_llm_exchange(
                        "ok", url, {"payload": payload, "headers": headers}, data,
                    )
                return {
                    "content": content.strip() if content else None,
                    "tool_calls": tool_calls or None,
                    "finish_reason": finish_reason,
                }

            except (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError) as e:
                _last_error = e
                logger.warning(
                    "直连 API 代理断连 (attempt %d/%d): %s — 重试中...",
                    _attempt + 1, _max_retries, e,
                )
                # 不拆共享 session (2026-10-14 修): 单次请求失败只废掉那一条连接,
                # connector 自己会把它踢出池子; 关掉整个 session 会连带掐死并发
                # 协程的在途请求 —— 实测超时与断连同秒成对出现, 互为凶手。
                if _attempt < _max_retries - 1:
                    import asyncio as _aio
                    await _aio.sleep(1.0)
                continue

            except asyncio.CancelledError:
                # 外部截止拆分 (2026-10-17): 调用方 wait_for/取消 → 立即停, 不重试。
                # 旧行为把取消并当"网关超时"重试: 门 10s 截止被拉成 10s+1s+整轮重发
                # (~19s 实测, llm_gate:stage1)。重试只留给下面的真超时。
                raise
            except asyncio.TimeoutError as e:
                # 总超时（网关思考久/代理慢）：降一档 thinking 重试一次，
                # 仍超时则抛可读错误（调用方兜底直接发 summary，不静默）。
                _last_error = e
                try:
                    _new_eff = _downgrade_reasoning_effort(payload)
                except Exception:
                    _new_eff = "-"
                logger.warning(
                    "直连 API 超时 (attempt %d/%d) slot=%s effort→%s — 重试中...",
                    _attempt + 1, _max_retries, _slot_label, _new_eff,
                )
                # 同上: 不拆共享 session, 只丢这一次的失败连接
                if _attempt < _max_retries - 1:
                    import asyncio as _aio2
                    await _aio2.sleep(1.0)
                    continue
                raise RuntimeError(f"直连 API 超时 ({url}，60s)：网关繁忙稍后再试") from e

            except RuntimeError as e:
                # ── 上游安全审计降级 (2026-10-17): 撞审 → 换不触审模型重试一次 ──
                # 只在 opencode 网关内换 (候选模型名是该网关的); 非该网关保持原样上抛。
                _emsg = str(e)
                if (is_audit_rejected(_emsg) and "opencode" in str(url)
                        and _attempt < _max_retries - 1):
                    _fb = audit_fallback_model(model)
                    if _fb:
                        logger.warning(
                            "直连 API 安全审计拦截 → 降级模型重试 (slot=%s %s → %s)",
                            _slot_label, model, _fb,
                        )
                        model = _fb
                        payload["model"] = _fb
                        _last_error = e
                        import asyncio as _aio5
                        await _aio5.sleep(0.3)
                        continue
                # 会话被外部关掉时重建一次; 其余 RuntimeError (非 200/空 choices/
                # 超时终抛) 保持原样上抛，绝不吞业务错误。
                if "Session is closed" not in str(e):
                    raise
                _last_error = e
                logger.warning(
                    "直连 API 会话已关闭 (attempt %d/%d) — 重建重试...",
                    _attempt + 1, _max_retries,
                )
                self._session = None
                session = await self._get_session()
                if _attempt < _max_retries - 1:
                    import asyncio as _aio3
                    await _aio3.sleep(0.5)
                    continue
                raise RuntimeError(f"直连 API 会话重建失败 ({url})") from e  # noqa: TRY003

            except aiohttp.ClientError as e:
                raise RuntimeError(f"无法连接直连 API ({url}): {e}") from e
        else:
            # 所有重试用完
            raise RuntimeError(f"无法连接直连 API ({url}): {_last_error}") from _last_error

    @staticmethod
    def build_messages(
        character: dict | None = None,
        history: list[dict[str, str]] | None = None,
        user_message: str = "",
        user_id: str = "",
        super_admin_qq: int | None = None,
        chat_summary: str = "",
        memory_hints: str = "",
        reasoning_hints: str = "",
        emotion_hints: str = "",
        experience_hints: str = "",
    ) -> list[dict[str, str]]:
        """构建完整的消息列表 (system + history + user)。

        使用角色卡中的 system_prompt 字段作为系统提示。
        如果角色卡有 mes_example，将拼入 system prompt 作为示例。

        {{user}} 占位符会根据 user_id 动态解析:
          - super_admin_qq → 「主人」
          - 其他用户 → 「小可爱」
          - user_id 为空或 super_admin_qq 为 None → 默认「主人」(向后兼容)

        Args:
            character: 角色卡 data dict, 含 name/description/system_prompt/personality/
                      scenario/first_mes/mes_example
            history: 历史消息列表
            user_message: 当前用户消息
            user_id: 当前用户 QQ 号 (用于决定称呼)
            super_admin_qq: 超级管理员 QQ 号
            chat_summary: 早期对话的 LLM 压缩摘要 (注入 system prompt 末尾)

        Returns:
            完整的 messages 列表
        """
        char = character or DEFAULT_CHARACTER
        char_name = char.get("name", "AI")

        # 解析用户称呼: 超级管理员 → 主人, 其他 → 小可爱
        if user_id and super_admin_qq is not None:
            user_title = "主人" if user_id == str(super_admin_qq) else "小可爱"
        else:
            user_title = "主人"  # 向后兼容: 未传参数时保持旧行为

        # 构建系统提示 — 优先使用角色卡的 system_prompt
        system_text = char.get("system_prompt", "")
        if not system_text:
            # 后备: 用旧格式拼接
            system_text = (
                f"[角色: {char_name}]\n"
                f"{char.get('description', '')}\n"
                f"请完全以 {char_name} 的身份回复，不要扮演其他角色。"
                f"不要使用 emoji，保持简洁。"
            )

        # 替换系统提示中的 {{user}} / {{char}} 占位符
        system_text = system_text.replace("{{user}}", user_title)
        system_text = system_text.replace("{{char}}", char_name)

        # ── 角色上下文注入 (对齐 SillyTavern 原生 prompt 构建) ──
        # description + personality + scenario 在 ST 中由 prompt template 注入
        # 我们的 API 模式需要手动拼接。放在 system_prompt 之前作为角色定义。
        context_parts: list[str] = []

        desc = char.get("description", "")
        if desc:
            desc = desc.replace("{{char}}", char_name).replace("{{user}}", user_title)
            context_parts.append(f"[角色外观与背景]\n{desc}")

        pers = char.get("personality", "")
        if pers:
            pers = pers.replace("{{char}}", char_name).replace("{{user}}", user_title)
            context_parts.append(f"[角色性格]\n{pers}")

        scenario = char.get("scenario", "")
        if scenario:
            scenario = scenario.replace("{{char}}", char_name).replace("{{user}}", user_title)
            context_parts.append(f"[当前场景]\n{scenario}")

        if context_parts:
            system_text = "\n\n".join(context_parts) + "\n\n" + system_text

        # 如果角色卡有示例对话，替换占位符后拼入
        mes_example = char.get("mes_example", "")
        if mes_example:
            # 替换 {{char}} → 角色名, {{user}} → 用户称呼
            mes_example = mes_example.replace("{{char}}", char_name)
            mes_example = mes_example.replace("{{user}}", user_title)
            # 清理 <START> 分隔符（在 API 模式下不需要）
            mes_example = mes_example.replace("<START>", "")
            system_text += f"\n\n[对话示例 — 请严格模仿此风格回复]\n{mes_example}"

        # 附加行为准则提示（也替换占位符）
        post_instructions = char.get("post_history_instructions", "")
        if post_instructions:
            post_instructions = post_instructions.replace("{{user}}", user_title)
            system_text += f"\n\n{post_instructions}"

        # 注入对话摘要 (上下文压缩) — 放在 system prompt 末尾 (动态段)
        if chat_summary:
            system_text += f"\n\n[你们之前的对话摘要]\n{chat_summary}"

        # 注入用户记忆 — 放在 system prompt 末尾 (动态段)
        if memory_hints:
            system_text += f"\n\n[你对{user_title}的了解]\n{memory_hints}"

        # 注入 bot 自传体经历记忆 — 主语: bot 自己经历过什么
        if experience_hints:
            system_text += f"\n\n[你的相关经历]\n{experience_hints}"

        # 注入情感状态 — 好感+短期情绪 (动态段)
        if emotion_hints:
            system_text += f"\n\n{emotion_hints}"

        # 注入深度思考指令 — 放在 system prompt 末尾 (动态段)
        if reasoning_hints:
            system_text += f"\n\n{reasoning_hints}"

        # ── 表情系统注入 (共享 AstrBot meme_manager 表情库) ──
        try:
            from ...lport_meme import get_prompt_injection
            meme_prompt = get_prompt_injection()
            if meme_prompt:
                system_text += f"\n\n{meme_prompt.strip()}"
        except Exception:
            pass  # lport_meme 插件未安装，静默跳过

        messages = [{"role": "system", "content": system_text}]
        if history:
            messages.extend(history)
        if user_message:
            messages.append({"role": "user", "content": user_message})

        # ── 世界书注入 ──
        # 扫描已构建的消息列表，命中关键词的条目作为动态 system 消息注入
        # 放在主 system prompt 之后、history 之前 (after_character 位置)
        wb_entries = _scan_world_book(messages)
        if wb_entries:
            wb_text = "[附加背景 — 这些信息在刚才的对话中被触发，你应该自然地融入回复中]\n\n"
            wb_text += "\n\n".join(
                e.replace("{{user}}", user_title).replace("{{char}}", char_name)
                for e in wb_entries
            )
            # 插入到 system (index 0) 之后，history 之前
            messages.insert(1, {"role": "system", "content": wb_text})

        return messages

    @staticmethod
    def get_available_characters() -> list[dict]:
        """扫描角色卡目录，返回可用角色列表。

        Returns:
            [{name, display_name, version, path}, ...]
        """
        if not _CHAR_DIR.exists():
            return []
        chars = []
        for f in sorted(_CHAR_DIR.glob("*.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    card = json.load(fh)
                data = card.get("data", {})
                chars.append({
                    "name": f.stem,
                    "display_name": data.get("name", f.stem),
                    "version": data.get("character_version", "?"),
                    "path": str(f),
                })
            except Exception:
                chars.append({"name": f.stem, "display_name": f.stem, "version": "?", "path": str(f)})
        return chars
