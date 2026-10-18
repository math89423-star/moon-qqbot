"""Clash 代理节点动态轮换 — 搜索前切换出口 IP 避免封禁。

通过 Clash RESTful API (clash:9090) 操作 Proxy 组:
  1. 获取当前节点和可用节点列表
  2. 过滤掉不健康的节点 (delay=0 / alive=false)
  3. 切换到最近未使用的节点

用法:
    from .clash_rotate import rotate_proxy
    await rotate_proxy()  # 搜索前调用一次即可
"""

from __future__ import annotations

import logging
import os
import time
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

# Clash RESTful API 地址 (Docker 内部网络; 本地 conda 直跑时 `clash` 域名解析不到，
# 且 infra clash 未发布 9090 到宿主，故本地必然走降级，属预期行为)
CLASH_API = os.environ.get("CLASH_API", "http://clash:9090").rstrip("/")
CLASH_SECRET = os.environ.get("CLASH_SECRET", "")

# Proxy 组名 (config.yaml 中定义)，可用 CLASH_PROXY_GROUP 覆盖
PROXY_GROUP = os.environ.get("CLASH_PROXY_GROUP", "Proxy")


def _rotate_enabled() -> bool:
    return os.environ.get("CLASH_ROTATE_ENABLED", "1").lower() not in ("0", "false", "no", "off")


# 最近使用过的节点缓存 (避免短时间内重复使用同一节点)
_recent_nodes: list[tuple[str, float]] = []
_MAX_RECENT = 5  # 记住最近 5 个
_ROTATION_COOLDOWN = 30  # 同一节点至少间隔 30s 才能再用

# API 失败冷却: 10 分钟内失败过则静默跳过 (search 每次都调 rotate，避免刷屏)
_FAIL_COOLDOWN = 600.0
_last_fail_ts: float = 0.0
_warned_once: bool = False


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if CLASH_SECRET:
        h["Authorization"] = f"Bearer {CLASH_SECRET}"
    return h


async def _get_proxy_group(group: str | None = None) -> dict:
    """获取 Proxy 组当前状态，404 时自动发现可用组名后重试一次。"""
    name = group or PROXY_GROUP
    url = f"{CLASH_API}/proxies/{quote(name, safe='')}"
    async with (
        aiohttp.ClientSession(trust_env=False) as session,
        session.get(
            url,
            headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp,
    ):
        if resp.status == 200:
            return await resp.json()
        if resp.status != 404:
            raise RuntimeError(f"Clash API 返回 {resp.status} (GET {url})")  # noqa: TRY003
    # 404: 组名对不上 (如连到宿主 clash 但组名不同) → 拉 /proxies 自动发现
    found = await _discover_group()
    if found and found != name:
        url2 = f"{CLASH_API}/proxies/{quote(found, safe='')}"
        async with (
            aiohttp.ClientSession(trust_env=False) as session,
            session.get(
                url2,
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp,
        ):
            if resp.status == 200:
                logger.info("Clash 组名自动修正: %s → %s", name, found)
                return await resp.json()
            body = await resp.text()
            raise RuntimeError(  # noqa: TRY003
                f"Clash API 返回 {resp.status} (GET {url} 期望组 {name!r} 不存在，"
                f"已发现组 {found!r} 仍失败: {body[:120]})。"
                "检查 CLASH_PROXY_GROUP 或目标 clash 配置的 proxy-groups 名。"
            )
    raise RuntimeError(  # noqa: TRY003
        f"Clash API 返回 404 (GET {url} 期望组 {name!r} 不存在)。"
        "检查 CLASH_API 是否指到对的 clash (Docker 内用 http://clash:9090)，"
        "或设 CLASH_PROXY_GROUP 覆盖组名 / CLASH_ROTATE_ENABLED=0 关轮换。"
    )


async def _discover_group() -> str | None:
    """拉 /proxies 列表，优先 Proxy → GLOBAL → 首个带 all/now 的组。"""
    url = f"{CLASH_API}/proxies"
    async with (
        aiohttp.ClientSession(trust_env=False) as session,
        session.get(
            url,
            headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp,
    ):
        if resp.status != 200:
            return None
        data = await resp.json()
    proxies = data.get("proxies") or {}
    if PROXY_GROUP in proxies:
        return PROXY_GROUP
    if "GLOBAL" in proxies:
        return "GLOBAL"
    for key, info in proxies.items():
        if isinstance(info, dict) and "all" in info and "now" in info:
            return key
    return None


async def _switch_proxy(name: str, group: str | None = None) -> None:
    """切换 Proxy 组到指定节点。"""
    gname = group or PROXY_GROUP
    async with aiohttp.ClientSession(trust_env=False) as session:
        async with session.put(
            f"{CLASH_API}/proxies/{quote(gname, safe='')}",
            headers=_headers(),
            json={"name": name},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 204:
                body = await resp.text()
                raise RuntimeError(f"Clash 切换失败 ({resp.status}): {body[:200]}")


def _pick_best_node(
    nodes: dict,
    current: str,
) -> str | None:
    """从节点列表中选出最佳轮换目标。

    优先级:
      1. alive=True 且 delay>0 (健康且延迟已知)
      2. alive=True 且 delay=0 (健康但未测速)
      3. 排除最近使用过的节点
    """
    now = time.time()
    candidates: list[tuple[str, int]] = []

    for name, info in nodes.items():
        # 跳过非代理节点
        if info.get("type") not in ("Vless", "Shadowsocks", "Trojan", "VMess", "Hysteria2"):
            continue
        if name == current:
            continue

        alive = info.get("alive", False)
        delay = 0
        # 从 extra 历史中读取最近延迟
        extra = info.get("extra", {})
        for _url, hist in extra.items():
            if hist.get("alive") and hist.get("history"):
                last = hist["history"][-1]
                delay = last.get("delay", 0)
                break

        if not alive:
            continue

        # 排除最近使用过的
        recently_used = any(
            n == name and (now - t) < _ROTATION_COOLDOWN
            for n, t in _recent_nodes
        )
        if recently_used:
            continue

        # 分数: delay>0 优先, delay 越小越好
        score = delay if delay > 0 else 9999
        candidates.append((name, score))

    if not candidates:
        return None

    # 按分数升序 (延迟低优先)
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]


async def rotate_proxy() -> str:
    """轮换 Clash 代理节点。

    Returns:
        切换到的节点名 (如 "jp-01")。
        如果无需切换或切换失败, 返回当前节点名。

    Raises:
        RuntimeError: Clash API 不可达时。
    """
    global _recent_nodes, _last_fail_ts, _warned_once

    if not _rotate_enabled():
        logger.debug("Clash 轮换已禁用 (CLASH_ROTATE_ENABLED=0)，跳过")
        return "unknown"

    # 失败冷却期内静默跳过，避免每次搜索刷一条 WARN
    now = time.time()
    if _last_fail_ts and (now - _last_fail_ts) < _FAIL_COOLDOWN:
        logger.debug("Clash 轮换冷却中，跳过")
        return "unknown"

    try:
        group = await _get_proxy_group()
    except Exception as e:
        _last_fail_ts = time.time()
        if not _warned_once:
            logger.warning("Clash API 不可达, 跳过轮换: %s", e)
            _warned_once = True
        else:
            logger.debug("Clash API 仍不可达 (冷却): %s", e)
        return "unknown"

    current = group.get("now", "")
    all_nodes = group.get("all", [])

    # 获取各节点详细信息 (节点名含 emoji/空格/竖线，必须 quote)
    node_details: dict = {}
    async with aiohttp.ClientSession(trust_env=False) as session:
        for name in all_nodes:
            try:
                async with session.get(
                    f"{CLASH_API}/proxies/{quote(name, safe='')}",
                    headers=_headers(),
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    if resp.status == 200:
                        node_details[name] = await resp.json()
            except Exception:
                pass

    best = _pick_best_node(node_details, current)

    if best is None:
        logger.info("Clash 轮换: 无可用替代节点, 保持当前 %s", current)
        return current

    try:
        effective_group = group.get("name", PROXY_GROUP)
        await _switch_proxy(best, effective_group)
        _recent_nodes.append((best, time.time()))
        if len(_recent_nodes) > _MAX_RECENT:
            _recent_nodes = _recent_nodes[-_MAX_RECENT:]
        _last_fail_ts = 0.0
        _warned_once = False
        logger.info("Clash 轮换: %s → %s", current, best)
        return best
    except Exception as e:
        logger.warning("Clash 切换失败 (%s → %s): %s", current, best, e)
        return current
