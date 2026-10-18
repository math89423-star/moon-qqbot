"""代理候选地址 env 解析 — web_search / pixiv_search 共用。

环境变量:
    QQBOT_PROXY_URLS — 逗号分隔的 HTTP 代理候选, 按顺序探测可达性。
        未设置时用默认值 (宿主 clash 优先, 容器内 clash 其次)。

换机器一键部署时只需改 compose 中的 QQBOT_PROXY_URLS, 不用改代码。
"""

from __future__ import annotations

import os

ENV_NAME = "QQBOT_PROXY_URLS"

# 默认顺序即 2026-09-27 实测结论: 宿主 7897 可用,
# infra 内 clash:7890 的 TCP 通但 DNS 全坏 (选它必失败), 只能当备选。
DEFAULT_CANDIDATES: tuple[str, ...] = (
    "http://172.31.160.1:7897",
    "http://clash:7890",
)


def get_proxy_candidates() -> tuple[str, ...]:
    """解析 QQBOT_PROXY_URLS, 返回非空候选元组 (恒不为空)。"""
    raw = os.environ.get(ENV_NAME, "").strip()
    if not raw:
        return DEFAULT_CANDIDATES
    cands = tuple(p.strip() for p in raw.split(",") if p.strip())
    return cands or DEFAULT_CANDIDATES


def first_reachable(timeout: float = 2.0) -> str | None:
    """按顺序做 TCP 可达探测, 返回第一个可达的代理, 都不通则 None (调用方直连)。"""
    import socket
    import urllib.parse as _up

    for cand in get_proxy_candidates():
        try:
            parts = _up.urlparse(cand)
            socket.create_connection(
                (parts.hostname or "", parts.port or 80), timeout=timeout
            ).close()
        except (OSError, TimeoutError, ValueError):
            continue
        else:
            return cand
    return None
