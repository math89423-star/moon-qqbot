"""任务路由表 — 按任务键给模型档与超时，缺键回落默认档。

路由只做分档建议，不替代面板槽位解析；调用方缺键时一律回 general。
"""
from __future__ import annotations

ROUTES: dict[str, dict[str, object]] = {
    "research": {"tier": "pro", "timeout_secs": 180.0, "budget": "high"},
    "daily": {"tier": "lite", "timeout_secs": 60.0, "budget": "low"},
    "vision": {"tier": "lite", "timeout_secs": 90.0, "budget": "mid"},
    "draw": {"tier": "pro", "timeout_secs": 120.0, "budget": "mid"},
    "general": {"tier": "lite", "timeout_secs": 180.0, "budget": "mid"},
}

_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("research", ("搜索", "检索", "查一下", "最新", "research", "search")),
    ("daily", ("日程", "计划", "提醒", "daily", "plan", "schedule")),
    ("vision", ("看图", "识图", "图片", "vision", "image")),
    ("draw", ("画", "生图", "二创", "draw", "image_gen")),
)


def resolve_task_route(objective: object = "", scope: object = None) -> dict[str, object]:
    """按目标文本与能力域推断路由键，缺键回落 general。"""
    text = str(objective or "")
    try:
        items = list(scope) if scope else []
    except Exception:
        items = []
    scope_text = " ".join(str(x) for x in items)
    blob = f"{text}\n{scope_text}"
    for key, words in _KEYWORDS:
        for w in words:
            if w and w in blob:
                route = dict(ROUTES[key])
                route["route"] = key
                return route
    route = dict(ROUTES["general"])
    route["route"] = "general"
    return route
