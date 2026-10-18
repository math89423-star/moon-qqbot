"""[DEPRECATED] Mention Intent Gate — 已合并入 IntentGate Stage 1。

保留为 re-export shim，确保旧 import 路径不中断。
新代码请直接使用:
  from .intent_gate import IntentGate, GateContext, RelevanceResult
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
from astrbot_plugin_suli_gate import GateContext, IntentGate, RelevanceResult

# ── 文本 @直呼快判 + 引用归属 (2026-10-17 操作者裁定) ──────────
# 平台没给 At 标记的"手打 @"也要按直呼算：文本里出现 @本机名/昵称 → 直接放行，
# 不烧意图门。归属由名字定：@谁的名字只对谁生效；
# 只 @ 了对面 bot 的名字 → 本机不触发（仍交门判"不是在叫我"）。
# 全角＠归一；@ 与名字之间允空格（@ 名字）。
#
# ★ 引用特例 (QQ 规则: 带引用会自动 @ 被引用者):
#   - 引用块 [引用消息(发送者: 内容)] 紧邻的 [At:被引用者] 是引用标记, 不算呼叫
#     (既不进 mention 判定, 也不进 @归属, 也不触发手打快判)。
#   - 引用本机消息算"回复"(强信号), 但正文明确叫对面 bot → 本机降级围观。
import re as _re

# 渲染态引用块前缀: [引用消息(发送者: 内容)] [At:被引用者QQ] 正文
_QUOTE_BLOCK_RE = _re.compile(r"^\[引用消息\([^)]*\)\]\s*(?:\[At:\d+\]\s*)?")
# OneBot 原始 CQ 串头部: [CQ:reply,id=x] [CQ:at,qq=被引用者] 正文
_CQ_QUOTE_HEAD_RE = _re.compile(
    r"^\[CQ:reply,[^\]]*\]\s*(?:\[CQ:at,qq=[^\]]+\]\s*)?"
)


def strip_quote_block(text: str) -> str:
    """剥离渲染态引用块前缀 ([引用消息(...)] + 紧邻的 [At:被引用者])。"""
    return _QUOTE_BLOCK_RE.sub("", str(text or "")).strip()


def strip_cq_quote_head(raw: str) -> str:
    """剥离 OneBot 原始 CQ 串头部的引用段 (+ 紧跟的引用自动@)。"""
    return _CQ_QUOTE_HEAD_RE.sub("", str(raw or ""))


def typed_at_directed(content: str, own_names: list[str]) -> bool:
    """文本 @本机名/昵称 → True (直呼)。own_names 传本机名字+昵称。

    2026-10-17 补: 引用块内的文字不参与判定 —— 引用自动@ / 被引用内容里的
    "@名字" 都是引用标记, 只看用户自己打的正文本。
    """
    c = _re.sub(r"@\s+", "@", strip_quote_block(content).replace("＠", "@"))
    return any(f"@{n}" in c for n in own_names if n)


def quote_auto_at_pos(segs: list[tuple[str, str]]) -> set[int]:
    """引用块自动 @ 的段索引集合 (不算呼叫)。

    segs: [(type, qq), ...] 按消息链原序。
    规则: 紧邻 reply 段之后、且 qq == 被引用者 qq 的 at = QQ 引用自动@。
    (点选 @ 会另起 at 段; 手打 @ 不在段里, 由文本判定。)
    """
    skip: set[int] = set()
    for i, (t, qq) in enumerate(segs):
        if t != "at" or i == 0:
            continue
        _pt, _pqq = segs[i - 1]
        if _pt == "reply" and qq and _pqq and qq == _pqq:
            skip.add(i)
    return skip


def body_calls_peer(body: str, *, self_names, peer_names) -> bool:
    """正文(自动剥引用块)是否明确呼叫对面 bot。

    只认两种明确呼叫: "@对面名/昵称"、以对面名/昵称开头;
    正文出现本机名字/昵称 → 一律 False (本机优先, 避免抢单误伤)。
    """
    _b = strip_quote_block(body)
    if not _b:
        return False
    _low = _b.lower()
    for _n in (self_names or []):
        _n = str(_n or "").strip().lower()
        if _n and _n in _low:
            return False
    for _n in (peer_names or []):
        _n = str(_n or "").strip()
        if not _n:
            continue
        if f"@{_n}" in _b or _low.startswith(_n.lower()):
            return True
    return False

# ── Backward-compatible wrapper ────────────────────────────


class MentionIntentGate:
    """[DEPRECATED] 提及意图门控 — 已合并入 IntentGate.evaluate_relevance()。

    保留此类以兼容旧 import 路径。新代码应直接使用 IntentGate。
    """

    @staticmethod
    async def decide(tavern, content: str, sender_name: str,
                     recent_context: list[dict] | None, config,
                     *, api_base: str = "", api_key: str = "",
                     model: str = "", bot_id: str = "",
                     user_id: str = "", bot_name: str = "",
                     bot_nicknames: str = "", peer_bot_qq: str = "",
                     peer_bot_name: str = "") -> str:
        """[DEPRECATED] 使用 IntentGate.evaluate_relevance() 替代 (参数直通)。

        2026-10-17 接回/修宽: 全量透传 — gate 槽位 (api_base/api_key/model/bot_id)、
        最近上下文 recent_context ([{user_id,user_name,content}, ...] — 此前只发
        单条消息, 判「叫 bot vs 讨论 bot」没有上下文) 与触发信息
        (trigger_uid/trigger_content, 供 prompt 高亮触发消息)。
        超时取 config.intent_gate_relevance_timeout (默认 10s, 上限 30)。
        此前缺模型参数 → 每次调用恒异常 fail-open, 门形同虚设。

        2026-10-17 修对面 bot 名: 旧代码 peer_bot_name 读全局 config 兜底了一个固定名字,
        而 tavern Config 根本没有该字段 → 所有 bot 的门都被告知"对面是同一个名字"。
        该名字对应的 bot 于是拿到「同群的那个名字是另一个bot，不是你」→ 判"在叫它=不是我"
        → 其昵称路恒被拦 (2026-10-17 群实例)。
        现按 bot_id 反查对面 bot 名, 调用方可直传 peer_bot_name 覆盖。
        """
        logger.debug("MentionIntentGate.decide() 走 IntentGate.evaluate_relevance (deprecated shim)")
        # ── 对面 bot 名解析 (per-bot) ──
        _peer_resolved = str(peer_bot_name or "").strip()
        if not _peer_resolved:
            try:
                from astrbot_plugin_suli_guards.dual_bot import (
                    get_bot_name as _gbn,
                    get_peer_bot_id as _gpbi,
                )
                _peer_resolved = str(_gbn(_gpbi(bot_id) or "") or "").strip()
            except Exception:
                _peer_resolved = ""
        if not _peer_resolved:
            _peer_resolved = str(getattr(config, "peer_bot_name", "") or "").strip()
        # 单 bot 部署无同群对照 bot: 解析不到就留空, 不编造名字
        _self_name = str(bot_name or "").strip()
        if _self_name and _peer_resolved == _self_name:
            _peer_resolved = ""
        # 防御: 上下文为空时至少带上触发消息本身 —— evaluate_relevance 对空
        # messages 恒回 directed_to_me=False ("无最近消息"), 会把门变成沉默闸。
        _msgs = [m for m in (recent_context or []) if isinstance(m, dict)]
        if not _msgs and content:
            _msgs = [{"user_id": user_id or "", "user_name": sender_name or "?", "content": content}]
        ctx = GateContext(
            messages=_msgs,
            bot_name=bot_name or getattr(config, "bot_name", ""),
            peer_bot_name=_peer_resolved,
            bot_nicknames=bot_nicknames or "",
            peer_bot_qq=peer_bot_qq or "",
            trigger_uid=user_id or "",
            trigger_content=content or "",
            trigger_user_name=sender_name or "",
            is_at_mention=False,
        )
        _timeout = float(getattr(config, "intent_gate_relevance_timeout", 10.0) or 10.0)
        result = await IntentGate.evaluate_relevance(
            tavern, ctx, timeout=_timeout,
            api_base=api_base, api_key=api_key, model=model, bot_id=bot_id,
        )
        return "reply" if result.directed_to_me else "ignore"


__all__ = ["GateContext", "IntentGate", "MentionIntentGate", "RelevanceResult"]
