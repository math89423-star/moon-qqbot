"""角色外貌圣经 — 热门二游角色绘制身份本地秒查表.

数据源: knowledge/character_appearance.json (git 跟踪, Danbooru 官设 tag 实测种子)。
零 LLM、零网络、零超时: 命中直接返回 {danbooru, pixiv, name, series, features}。
未命中返回 None, 调用方走原联网解析；联网成功会自动学习追加 (source=web)。

MD 镜像 (供 search_knowledge 召回) 由 render_markdown() 生成：
  python3 -c "import sys; sys.path.insert(0,'plugins');
  from astrbot_plugin_suli_tavern.service.character_bible import render_markdown;
  print(render_markdown())" > plugins/astrbot_plugin_suli_tavern/knowledge/character_appearance.md
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_BIBLE_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "character_appearance.json"
_MAX_ENTRIES = 500

_cache: dict | None = None
_cache_mtime: float = 0.0

# 系列中文/变体 → 标准英文 (与 tools 侧 IP 映射同源, 绘制口径)
_SERIES_CN: tuple[tuple[str, str], ...] = (
    ("明日方舟", "arknights"),
    ("终末地", "arknights endfield"),
    ("原神", "genshin impact"),
    ("崩坏3", "honkai impact"),
    ("星铁", "honkai: star rail"),
    ("崩铁", "honkai: star rail"),
    ("星穹铁道", "honkai: star rail"),
    ("崩坏星穹铁道", "honkai: star rail"),
    ("鸣潮", "wuthering waves"),
    ("碧蓝航线", "azur lane"),
    ("蔚蓝档案", "blue archive"),
    ("蓝档", "blue archive"),
    ("绝区零", "zenless zone zero"),
    ("zzz", "zenless zone zero"),
    ("FGO", "fate grand order"),
)


def _norm(s: str) -> str:
    return "".join(str(s or "").lower().split()).replace(":", "").replace("_", "").replace(".", "")


def _load() -> dict:
    global _cache, _cache_mtime
    try:
        mt = _BIBLE_PATH.stat().st_mtime
    except OSError:
        return {"characters": []}
    if _cache is not None and mt <= _cache_mtime:
        return _cache
    try:
        data = json.loads(_BIBLE_PATH.read_text(encoding="utf-8"))
        assert isinstance(data.get("characters"), list)
    except Exception:
        logger.warning("角色圣经加载失败", exc_info=True)
        return _cache or {"characters": []}
    _cache, _cache_mtime = data, mt
    return data


def _query_series(qn: str) -> str:
    """查询串命中的系列英文 (无返回 \"\")。"""
    for cn, en in _SERIES_CN:
        if _norm(cn) in qn:
            return _norm(en)
    for _, en in _SERIES_CN:
        if _norm(en) in qn:
            return _norm(en)
    # 裸英文系列名 (hsr/zzz 等缩写不管, 宁可漏)
    return ""


def lookup(query: str) -> dict | None:
    """本地秒查。返回 {danbooru, pixiv, name, series, features} 或 None。

    规则: 别名命中 + (查询无系列 / 系列一致)；单字别名必须配系列；
    多命中取最长别名。
    """
    q = str(query or "").strip()
    if not q:
        return None
    qn = _norm(q)
    qs = _query_series(qn)
    # 纯系列查询（去掉所有已知系列名后无残留，如单发「星穹铁道」）：
    # 没有角色名成分，禁止命中（否则首个同系列角色被冒名，星穹铁道→stelle 案）。
    try:
        _rest = qn
        for _cn, _en in _SERIES_CN:
            _rest = _rest.replace(_norm(_cn), "")
            _rest = _rest.replace(_norm(_en), "")
        if not _rest:
            return None
    except Exception:
        pass
    best: dict | None = None
    best_len = 0
    best_series: set[str] = set()
    # 系列名不许当别名参选（圣经里有人把系列写进 aliases，
    # 最长匹配会让系列劫持人名：星穹铁道→firefly 案）。
    _series_like = _series_like_set()
    for c in _load().get("characters", []):
        try:
            cen = _norm(c.get("series_en", ""))
            alts = {_norm(x) for x in (c.get("series_alt", []) or []) if _norm(x)}
        except Exception:
            continue
        if qs and cen and qs != cen and qs not in alts:
            continue
        for a in c.get("aliases", []) or []:
            an = _norm(a)
            if not an or an not in qn:
                continue
            if an in _series_like:
                continue
            if len(an) <= 1:
                # 单字别名只认“去系列后全文恰好等于该字”（原神影→影）。
                # 子串命中会误杀：星铁布洛妮娅含“星”→stelle 案。
                # 裸单字（查询无系列）一律不认，与今天行为一致。
                if not (qs and cen and qs == cen):
                    continue
                try:
                    _r = qn
                    for _cn2, _en2 in _SERIES_CN:
                        _r = _r.replace(_norm(_cn2), "")
                        _r = _r.replace(_norm(_en2), "")
                except Exception:
                    continue
                if _r != an:
                    continue
            if len(an) > best_len:
                best, best_len = c, len(an)
                best_series = {cen}
            elif len(an) == best_len and best is not None:
                best_series.add(cen)
    if best is None:
        return None
    if not qs and len(best_series) > 1:
        # 裸名跨系列撞车（如布洛妮娅崩坏3/星铁都有）：不猜，走联网
        logger.info("[bible] 裸名跨系列歧义，放弃本地命中 query=%r", query[:40])
        return None
    return {
        "danbooru": str(best.get("danbooru", "") or "").strip(),
        "pixiv": str(best.get("pixiv", "") or "").strip(),
        "name": str(best.get("name", "") or "").strip(),
        "series": str(best.get("series_en", "") or "").strip(),
        "features": str(best.get("features", "") or "").strip(),
        # 2026-10-17: 找图路换词兜底用 — pixiv 上部分角色只有中文名 tag 活
        # (英文名空手时按别名顺序换词重搜, 见 tools.execute_pixiv_search)。
        "aliases": [
            str(a).strip() for a in (best.get("aliases") or []) if str(a).strip()
        ],
    }


def learn(entry: dict, *, query_name: str = "") -> bool:
    """联网成功自学习：按 danbooru 去重追加，原子落盘。失败 False。"""
    tag = str((entry or {}).get("danbooru", "") or "").strip()
    if not tag:
        return False
    try:
        data = _load()
        chars = data.get("characters", [])
        for c in chars:
            if str(c.get("danbooru", "")).strip().lower() == tag.lower():
                # 已有：补别名即止（系列名不许进别名：普瑞赛斯案里 learn() 把
                # “明日方舟”写进 shining 别名，污染圣经）
                qn = _norm(query_name)
                if qn and qn not in _series_like_set() and all(
                    _norm(a) != qn for a in c.get("aliases", [])
                ):
                    c.setdefault("aliases", []).append(str(query_name).strip()[:20])
                    _save(data)
                return True
        chars.append({
            "aliases": [str(query_name).strip()[:20]]
            if str(query_name or "").strip()
            and _norm(str(query_name).strip()) not in _series_like_set()
            else [],
            "name": str(entry.get("name", "") or "")[:40],
            "series_cn": str(entry.get("series_cn", "") or "")[:20],
            "series_en": str(entry.get("series", "") or "")[:40],
            "danbooru": tag[:120],
            "pixiv": str(entry.get("pixiv", "") or "")[:60],
            "features": str(entry.get("features", "") or "")[:300],
            "verified": time.strftime("%Y-%m-%d"),
            "source": "web",
        })
        # 上限：超了先砍最老的 web 条目
        while len(chars) > _MAX_ENTRIES:
            for i, c in enumerate(chars):
                if c.get("source") == "web":
                    chars.pop(i)
                    break
            else:
                chars.pop(0)
        _save(data)
        # 自学习不污染正式 MD 镜像？污染才对：知识库就该长大，此处只记日志。
        logger.info("[bible] 自学习: %s (共 %d 条)", tag, len(chars))
        return True
    except Exception:
        logger.warning("[bible] 自学习落盘失败", exc_info=True)
        return False


def _save(data: dict) -> None:
    tmp = _BIBLE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, _BIBLE_PATH)
    global _cache, _cache_mtime
    _cache, _cache_mtime = data, _BIBLE_PATH.stat().st_mtime


def render_markdown() -> str:
    """JSON → 知识库 MD 镜像 (search_knowledge 召回用)。"""
    chars = _load().get("characters", [])
    by_series: dict[str, list] = {}
    for c in chars:
        by_series.setdefault(str(c.get("series_en", "?") or "?"), []).append(c)
    out = [
        "# 二游角色外貌速查（绘制专用）",
        "",
        "> 机读真相源是同目录 character_appearance.json；本文件由其生成，",
        "> 供 search_knowledge/群聊问答召回。画图时以 Danbooru tag + 特征为准。",
        "> 更新：改 JSON 后重跑 render_markdown() 覆写本文件。",
        "",
    ]
    for series in sorted(by_series):
        out.append(f"## {series}")
        out.append("")
        for c in sorted(by_series[series], key=lambda x: str(x.get("name", ""))):
            aliases = " / ".join(c.get("aliases", [])[:6])
            out.append(f"### {c.get('name', '?')}（{aliases}）")
            out.append("")
            out.append(f"- Danbooru：`{c.get('danbooru', '')}`")
            if c.get("pixiv"):
                out.append(f"- Pixiv：`{c.get('pixiv', '')}`")
            out.append(f"- 特征：{c.get('features', '')}")
            out.append("")
    return "\n".join(out)


# ── 已知撞名表（2026-10-15 消除歧义）：裸名被多个系列/形态共享时，
# 圣经只有其中一方的 tag（如布洛妮娅只有崩3），静默命中等于替用户做选择。
# 裸名命中此表 → 调用方走反问（ambiguous），不许直接画。
# 条目结构：label（展示名）/ series_cn（用户重说用）/ danbooru（空=暂无tag，
# 选了也画不了，问完走 unknown，如实说）/ ready（此选项现在能不能画）。
_KNOWN_AMBIGUOUS: dict[str, list[dict]] = {
    "布洛妮娅": [
        {"label": "崩坏3的布洛妮娅（板鸭）", "series_cn": "崩坏3",
         "danbooru": "bronya zaychik", "ready": True},
        {"label": "星铁的布洛妮娅", "series_cn": "星铁",
         "danbooru": "", "ready": False},
    ],
    "bronya": [
        {"label": "崩坏3的布洛妮娅（板鸭）", "series_cn": "崩坏3",
         "danbooru": "bronya zaychik", "ready": True},
        {"label": "星铁的布洛妮娅", "series_cn": "星铁",
         "danbooru": "", "ready": False},
    ],
}


def _series_cn_of(series_en: str) -> str:
    """series_en → 中文简称 (反查 _SERIES_CN, 取首个; 无则原样)。"""
    se = str(series_en or "").strip().lower()
    for _cn, _en in _SERIES_CN:
        if str(_en or "").strip().lower() == se:
            return _cn
    return str(series_en or "")


def _bible_derived_options(query_norm: str) -> list[dict] | None:
    """圣经派生撞名选项: 同一归一 alias 横跨 ≥2 个系列 → 反问选项。

    2026-10-15 希儿/妮可案: 硬编码表只有布洛妮娅, 其他跨 IP 同名裸查
    lookup 因多系列返回 None, 又无反问表, 直接掉进 unknown 三条路
    ("库里没有"——撒谎, 库里明明有俩)。派生表 tag 取圣经自带值
    (入库即核验, 不用再审), 系列中文反查 _SERIES_CN。
    """
    seen_series: dict[str, dict] = {}
    try:
        chars = _load().get("characters", [])
    except Exception:
        return None
    for c in chars:
        try:
            aliases = {_norm(x) for x in (c.get("aliases", []) or []) if _norm(x)}
        except Exception:
            continue
        if query_norm not in aliases:
            continue
        cen = str(c.get("series_en", "") or "").strip()
        if not cen or cen in seen_series:
            continue
        seen_series[cen] = c
    if len(seen_series) < 2:
        return None
    opts: list[dict] = []
    for cen in sorted(seen_series):
        c = seen_series[cen]
        _cn = _series_cn_of(cen)
        _nm = str(c.get("name", "") or "").strip() or query_norm
        _tag = str(c.get("danbooru", "") or "").strip()
        opts.append({
            "label": f"{_cn}的{_nm}" if _cn else _nm,
            "series_cn": _cn,
            "danbooru": _tag,
            "ready": bool(_tag),
        })
    return opts


def check_ambiguous(query: str) -> list[dict] | None:
    """裸名撞名检查。命中返回选项表拷贝（调用方反问用户），否则 None。

    只认裸名：查询带系列限定（如星铁布洛妮娅）时不触发，走正常圣经路径
    （命中/unknown 都比瞎问强）。大小写/空白归一后比对。
    """
    q = str(query or "").strip()
    if not q:
        return None
    qn = _norm(q)
    if _query_series(qn):
        return None
    for _key, _opts in _KNOWN_AMBIGUOUS.items():
        if _norm(_key) and qn == _norm(_key):
            return [dict(o) for o in _opts]
    # 硬编码未收录 → 圣经派生 (希儿/妮可/seele 案)
    return _bible_derived_options(qn)


def is_ambiguous_name(name: str) -> bool:
    """裸名是否在撞名表上（不看系列，调用方自己判断系列限定）。

    圣经优先用：撞名 + 查询自带系列限定 → 跳过单名直命中，
    走系列过滤的联合查询（星铁布洛妮娅案：不许单名命中崩3还改写系列）。
    非撞名（如梅比乌斯）保持单名命中+系列纠偏不动。
    """
    nn = _norm(str(name or "").strip())
    if not nn:
        return False
    return any(_norm(_k) and nn == _norm(_k) for _k in _KNOWN_AMBIGUOUS)


# ── 错字兜底（2026-10-15 爱弥丝案）：精确 lookup 失败后才启用，绝不覆盖精确命中。
# 只认“错一字”：difflib ≥ 0.66（3 字名错 1 字恰好 0.667），
# 同一角色多别名只取最优（银狼lv999/lv999 互抢会把 margin 吃掉），
# 再与次优角色拉开差距，否则拒认（fail-closed）。单字别名不参选；
# 系列过滤与精确路径一致；长度差 >1 不认（防缩写式误杀）。
_FUZZY_MIN_SCORE = 0.66
_FUZZY_MIN_MARGIN = 0.2


def lookup_fuzzy(query: str) -> dict | None:
    """错字容错版 lookup。返回体与 lookup 同形，另带 _matched_alias（命中的别名）。

    调用方注意：命中后若 _matched_alias 本身撞名（如布洛尼娅→布洛妮娅），
    仍要走反问，不能直接 confirmed。
    """
    import difflib as _dl

    q = str(query or "").strip()
    if not q:
        return None
    qn = _norm(q)
    qs = _query_series(qn)
    # 去系列后无残留（纯系列查询）→ 不猜
    _rest = qn
    for _cn, _en in _SERIES_CN:
        _rest = _rest.replace(_norm(_cn), "")
        _rest = _rest.replace(_norm(_en), "")
    _name = _rest.strip()
    if not _name or _name in _series_like_set():
        return None
    # 2 字名不模糊（昔连/湖桃）：1 字相同只有 0.5，胡杨→胡桃类误杀风险太大；
    # 3 字及以上才认“错一字”。短名错字请带系列重说。
    if len(_name) < 3:
        return None
    _ranked: list[tuple[float, dict, str]] = []
    for c in _load().get("characters", []):
        try:
            cen = _norm(c.get("series_en", ""))
            alts = {_norm(x) for x in (c.get("series_alt", []) or []) if _norm(x)}
        except Exception:
            continue
        if qs and cen and qs != cen and qs not in alts:
            continue
        _ce_best = 0.0
        _ce_alias = ""
        for a in c.get("aliases", []) or []:
            an = _norm(a)
            if not an or len(an) < 2 or an in _series_like_set():
                continue
            if abs(len(an) - len(_name)) > 1:
                continue
            try:
                _s = _dl.SequenceMatcher(None, _name, an).ratio()
            except Exception:
                continue
            if _s > _ce_best:
                _ce_best, _ce_alias = _s, a
        if _ce_best > 0.0:
            _ranked.append((_ce_best, c, _ce_alias))
    if not _ranked:
        return None
    _ranked.sort(key=lambda t: t[0], reverse=True)
    (_best_score, _best, _best_alias), _second = _ranked[0], (_ranked[1][0] if len(_ranked) > 1 else 0.0)
    if _best_score < _FUZZY_MIN_SCORE:
        return None
    if _best_score - _second < _FUZZY_MIN_MARGIN:
        return None
    return {
        "danbooru": str(_best.get("danbooru", "") or "").strip(),
        "pixiv": str(_best.get("pixiv", "") or "").strip(),
        "name": str(_best.get("name", "") or "").strip(),
        "series": str(_best.get("series_en", "") or "").strip(),
        "features": str(_best.get("features", "") or "").strip(),
        "_matched_alias": str(_best_alias or "").strip(),
        "_fuzzy_score": round(_best_score, 3),
    }


def _series_like_set() -> set[str]:
    """系列名集合（别名参选黑名单，与 lookup 内联逻辑同源，抽出复用）。"""
    _out: set[str] = set()
    try:
        for _cn, _en in _SERIES_CN:
            if _norm(_cn):
                _out.add(_norm(_cn))
            if _norm(_en):
                _out.add(_norm(_en))
        for _c0 in _load().get("characters", []):
            for _k in ("series_cn", "series_en"):
                _v = _norm(_c0.get(_k, ""))
                if _v:
                    _out.add(_v)
    except Exception:
        pass
    return _out
