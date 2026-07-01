"""合并转发消息缓存 — 工具 parse_forwarded_message 读取此缓存。

用户发送合并转发消息时（即使没有 @bot），内容被缓存。
后续用户提及"转发"时，Intent Gate 可建议 parse_forwarded_message 工具，
LLM 调用该工具从缓存中提取转发内容，据此评价/分析。
"""

import time
import logging

logger = logging.getLogger("astrbot_plugin_suli_tavern")

# 结构: {cache_key: [(sender_id, forward_id, text, timestamp), ...]}
#   cache_key = group_id (群聊) 或 "private:user_id" (私聊)
# ★ ADR-001 进程隔离: 双 Bot 各自独立容器，此 dict 天然 per-bot 安全。
#   如需单进程多 bot → cache_key 加 bot_id 前缀。
_FORWARD_CACHE: dict[str, list[tuple[str, str, str, float]]] = {}
_FORWARD_CACHE_MAX = 5       # 每会话最多缓存 5 条
_FORWARD_CACHE_TTL = 600.0   # 缓存有效期 10 分钟


def cache_forward_content(
    cache_key: str, sender_id: str, forward_id: str, text: str,
) -> None:
    """缓存提取的合并转发内容，供后续消息引用。"""
    if not text or not cache_key:
        return
    now = time.time()
    entries = _FORWARD_CACHE.setdefault(cache_key, [])
    # 同 forward_id 去重
    if forward_id:
        entries = [
            (s, fid, t, ts) for s, fid, t, ts in entries if fid != forward_id
        ]
    entries.append((str(sender_id), str(forward_id), text, now))
    _FORWARD_CACHE[cache_key] = entries[-_FORWARD_CACHE_MAX:]
    logger.info(
        "转发缓存已存储: key=%s sender=%s fid=%s len=%d cache_size=%d",
        cache_key, sender_id[:8], str(forward_id)[:16], len(text),
        len(_FORWARD_CACHE.get(cache_key, [])),
    )


def get_cached_forward(cache_key: str, sender_id: str = "") -> str:
    """从缓存中获取最近的合并转发内容（优先同一发送者）。

    Args:
        cache_key: group_id (群聊) 或 "private:user_id" (私聊)
        sender_id: 优先返回此发送者最近的一条转发内容

    Returns:
        转发消息文本，未找到返回空字符串
    """
    now = time.time()
    entries = _FORWARD_CACHE.get(cache_key, [])
    # 优先：同一发送者且在 TTL 内
    for sender, _fid, text, ts in reversed(entries):
        if now - ts > _FORWARD_CACHE_TTL:
            continue
        if sender_id and sender == sender_id:
            return text
    # 回退：任意发送者的最近一条
    for _sender, _fid, text, ts in reversed(entries):
        if now - ts > _FORWARD_CACHE_TTL:
            continue
        return text
    return ""


def get_cached_forward_all(cache_key: str, limit: int = 3) -> list[dict]:
    """获取缓存中最近 N 条转发内容（含发送者和时间）。"""
    now = time.time()
    entries = _FORWARD_CACHE.get(cache_key, [])
    result = []
    for sender_id, _fid, text, ts in reversed(entries):
        if now - ts > _FORWARD_CACHE_TTL:
            continue
        result.append({
            "sender_id": sender_id,
            "text": text,
            "age_seconds": int(now - ts),
        })
        if len(result) >= limit:
            break
    return result
