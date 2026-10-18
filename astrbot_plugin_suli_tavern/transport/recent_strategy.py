"""策略多样性跟踪 — RecentStrategy: 防止 bot 连用同一招显假 (对标 HUMA Timeliness)。

设计 (2026-10-02, P1-1 直接生效版):
  - 零 LLM 启发式打标: question / long / recall / quip / chat 五类
  - per-(bot,group) 复合键 (原则16: 状态隔离), 内存 deque, 600s 过期
  - 沉默本身不记录 (Keep Silent 豁免, 对标 HUMA T=1 豁免项)
  - 同标签三连 → 调用方 50% 概率静默 (与情感静默同量级, 非硬悬崖)
  - fail-open: 任何异常 → (False, 0) / 静默跳过, 不挡正常回复
"""

from __future__ import annotations

import time
from collections import deque

# 同标签连击 N 条触发降权
_STREAK_N: int = 3
# 历史窗口: 保留近 N 条 + 600s 过期
_HISTORY_N: int = 10
_WINDOW_S: float = 600.0


# key: f"{bot_id}:{group_id}" → deque[(tag, monotonic)]
_histories: dict[str, deque[tuple[str, float]]] = {}


def _key(bot_id: str, group_id: str) -> str:
    return f"{bot_id}:{group_id}"


def classify_reply(text: str) -> str:
    """零 LLM 启发式打标。纯函数。"""
    t = (text or "").strip()
    if not t:
        return "empty"
    if "?" in t or "？" in t:
        return "question"
    if len(t) > 150:
        return "long"
    if any(k in t for k in ("刚才", "之前", "记得", "上次")):
        return "recall"
    if len(t) < 20:
        return "quip"
    return "chat"


def _prune(key: str) -> deque[tuple[str, float]]:
    """清理过期条目, 返回该 key 的历史队列 (不存在则建空)。"""
    now = time.monotonic()
    dq = _histories.get(key)
    if dq is None:
        dq = deque(maxlen=_HISTORY_N)
        _histories[key] = dq
        return dq
    while dq and (now - dq[0][1]) > _WINDOW_S:
        dq.popleft()
    return dq


def streak_penalty(bot_id: str, group_id: str, tag: str) -> tuple[bool, int]:
    """检查同标签连击。不记录本条 (记录在发送成功后由 record() 做)。

    Returns:
        (是否应降权, 当前同标签尾部连击数)。异常 → (False, 0)。
    """
    try:
        _valid = bool(tag) and tag != "empty"
        _dq = _prune(_key(bot_id, group_id)) if _valid else None
    except Exception:
        return False, 0
    else:
        if not _valid or _dq is None:
            return False, 0
        streak = 0
        for past_tag, _ in reversed(_dq):
            if past_tag == tag:
                streak += 1
            else:
                break
        return streak >= _STREAK_N, streak


def record(bot_id: str, group_id: str, tag: str) -> None:
    """记录一次实际发出的回复。沉默/被拦截的不调此函数。异常静默跳过。"""
    try:
        if not tag or tag == "empty":
            return
        dq = _prune(_key(bot_id, group_id))
        dq.append((tag, time.monotonic()))
    except Exception:
        pass
