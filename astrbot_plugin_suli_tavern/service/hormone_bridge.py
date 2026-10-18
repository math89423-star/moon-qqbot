"""荷尔蒙桥 (Hormone Bridge) — 曲线引擎与业务链路的唯一接缝。

分工:
  astrbot_plugin_suli_emotion/hormone.py  纯函数引擎 (无框架依赖, 可离线仿真)
  service/hormone_bridge.py               本文件 — 门控 / 配置 / 持久化 / 渲染 / 回写

调用方只需要四个动作 (全部 fail-soft, 任何异常都不影响回复):

    from .service.hormone_bridge import observe_user, observe_self, hint, knobs
    observe_user(bot_id, user_id, text, group_id=gid)        # 用户消息进来
    observe_self(bot_id, user_id, reply, group_id=gid)       # 自己的输出 (自反馈)
    dyn.append(hint(bot_id, user_id, group_id=gid))          # 注入 prompt dynamic_parts
    r = knobs(bot_id, user_id, group_id=gid)                 # 发送侧旋钮 (条数/间隔)

门控顺序 (硬约束, 不可调换):

    safety 硬线 → NSFW Zone (bot 开关 + 群/人白名单) → 好感硬门槛 → 荷尔蒙

  荷尔蒙排在最后: 它只能决定「怎么演」, 不能决定「能不能演」。
  前两道任意一道不过 → 本模块所有函数退化为中性值 (neutral), 且不写任何状态。

模式 (per-bot, `bot:{bot_id}:hormone_mode`):
  off    不读不算不注入 (零开销)
  shadow 照算照记日志, 但注入与旋钮全部走中性值 —— 先观测一轮, 确认分区跳变合理
  live   全量生效

Cache 纪律: hint() 的输出**必须**注入 message[1] (dynamic_parts)。它每秒都在变,
进 message[0] 等于每次调用断一次前缀缓存 (原则 29/30/31)。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_LIVE = "live"

# ★ 声线库放在独立目录, 不放 characters/ —— 那里被 _discover_characters()
#   按 *.json 扫描成「QQ → 角色卡」候选, 混进去会在无 BOT_CHAR_* 环境变量时
#   按文件名排序顶掉真正的角色卡 (已实测)。
_VOICE_DIR = Path(__file__).resolve().parents[1] / "hormone_voices"

# 中性旋钮 — 门控不过 / shadow / 异常时的返回值。
# ★ 这些值 = 接线前的既有行为, 逐项对齐旧硬编码, 保证「接上但不开」= 零回归:
#     burst_max=2 / burst_p=0.2 → group_chat.py 旧 `random.random() >= 0.2` + `[:2]`
#     gap_s=1.5                 → 旧 `asyncio.sleep(1.5)`
#     frag_max=3 / split=True   → 旧 `_fragment_reply()` 内部上限 3 条 + 按句切碎
#   live=False 是给「中性值不等于旧行为」的调用点用的:
#     私聊接线前是单条发送, 所以私聊侧必须 live=True 才允许分段 (frag_max>1)。
NEUTRAL: dict[str, Any] = {
    "burst_max": 2,
    "burst_p": 0.2,
    "gap_s": 1.5,
    "split": True,
    "frag_max": 3,
    "onoma": 0,
    "escalate": False,
    "solicit": False,
    "proactive_weight": 1.0,
    "zone": "calm",
    "live": False,
}

_VOICE_CACHE: dict[str, tuple[float, dict]] = {}
# 首次真正进入曲线时的「已接线」日志 — 便于线上确认接线生效 (每个 bot 一次)
_READY_LOGGED: set[str] = set()
_SAVE_AT: dict[tuple[str, str], float] = {}
_LAST_ZONE: dict[tuple[str, str], str] = {}
_SAVE_INTERVAL = 60.0


# ── 配置 ────────────────────────────────────────────────────


def _cfg_get(bot_id: str, key: str, default: str = "") -> str:
    try:
        from .bot_config import get_config_service

        return str(get_config_service().db.get_config(f"bot:{bot_id}:{key}", default))
    except Exception:
        return default


def mode(bot_id: str) -> str:
    """当前模式 (off/shadow/live)。默认 shadow —— 先观测再生效。"""
    m = _cfg_get(bot_id, "hormone_mode", MODE_SHADOW).strip().lower()
    return m if m in (MODE_OFF, MODE_SHADOW, MODE_LIVE) else MODE_SHADOW


def current_bot_id() -> str:
    """本容器的主 bot QQ — 供拿不到 bot_id 的调用方 (如 suli_proactive tick) 使用。

    优先级: BOT_QQ_MAIN/BOT_ID_MAIN 环境变量 (compose 按容器注入, 天然 per-bot 隔离)
            → 已知 bot 列表首个 → 空串 (调用方按门控不过处理)。
    """
    try:
        import os

        bid = (os.getenv("BOT_QQ_MAIN", "") or os.getenv("BOT_ID_MAIN", "") or "").strip()
        if bid:
            return bid
    except Exception:
        logger.debug("读取 BOT_ID_MAIN 失败", exc_info=True)
    try:
        from astrbot_plugin_suli_guards.dual_bot import get_known_bot_ids

        ids = sorted(str(x) for x in (get_known_bot_ids() or []) if str(x).strip())
        return ids[0] if len(ids) == 1 else ""
    except Exception:
        return ""


def _tuning(bot_id: str):
    try:
        from astrbot_plugin_suli_emotion import DEFAULT_TUNING, hormone_tuning_from_mapping

        raw = _cfg_get(bot_id, "hormone_tuning", "")
        if not raw:
            return DEFAULT_TUNING
        return hormone_tuning_from_mapping(json.loads(raw))
    except Exception:
        logger.debug("荷尔蒙参数解析失败, 用默认值 bot=%s", bot_id, exc_info=True)
        from astrbot_plugin_suli_emotion import DEFAULT_TUNING

        return DEFAULT_TUNING


def _admin_qq(bot_id: str) -> int | None:
    """超管 QQ 的 int 视图 (get_user_relation 的 admin_qq 是 int|None)。"""
    raw = _cfg_get(bot_id, "super_admin_qq", "") or ""
    if not raw:
        try:
            from ..config import Config

            raw = str(Config().super_admin_qq or "")
        except Exception:
            raw = ""
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _is_owner(bot_id: str, user_id: str) -> bool:
    """是否 @主人 (超管)。取同一份配置, 只此一处解析。"""
    owner = _admin_qq(bot_id)
    return owner is not None and str(user_id) == str(owner)


def _affinity(bot_id: str, user_id: str) -> float:
    """有效好感等级 (NyatBot 跨群聚合)。取不到 → 0 (fail-closed, 曲线不起)。"""
    try:
        from astrbot_plugin_suli_emotion import get_user_relation

        rel = get_user_relation(
            user_id,
            self_id=bot_id,
            admin_qq=_admin_qq(bot_id),
        )
        return float(rel.get_effective_affinity_level())
    except Exception:
        logger.debug("荷尔蒙取好感失败, 按 0 处理", exc_info=True)
        return 0.0


def _in_zone(bot_id: str, group_id: str, user_id: str, *, private: bool) -> bool:
    try:
        from .nsfw_zone import is_nsfw_zone_any

        return bool(is_nsfw_zone_any(bot_id, group_id, user_id, private=private))
    except Exception:
        logger.debug("荷尔蒙 NSFW 门控异常, fail-closed", exc_info=True)
        return False


def _active(bot_id: str, group_id: str, user_id: str, *, private: bool) -> bool:
    """前置门控: 模式 + NSFW Zone。任一不过 → 不读不写。"""
    if not bot_id or not user_id:
        return False
    if mode(bot_id) == MODE_OFF:
        return False
    return _in_zone(bot_id, group_id, user_id, private=private)


# ── 声线库 (JSON = 单一真相源, mtime 热载) ──────────────────


def _card_name(bot_id: str) -> str:
    """bot → 角色卡名。复用既有唯一入口 (原则 24), 不在这里再写一份映射。"""
    try:
        from .tavern_client import _card_name_for_qq

        return _card_name_for_qq(bot_id) or ""
    except Exception:
        return ""


def voice_for(bot_id: str) -> dict:
    """取该 bot 的声线库: hormone_voices/{card}.json。缺失 → {} (走内置默认)。"""
    card = _card_name(bot_id)
    if not card:
        return {}
    path = _VOICE_DIR / f"{card}.json"
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
        cached = _VOICE_CACHE.get(card)
        if cached and cached[0] == mtime:
            return cached[1]
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("声线库加载失败 card=%s", card, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    _VOICE_CACHE[card] = (mtime, data)
    return data


# ── 核心: 读 / 喂 / 存 ──────────────────────────────────────


def _state(bot_id: str, user_id: str):
    from astrbot_plugin_suli_emotion import load_hormone_state

    return load_hormone_state(bot_id, user_id)


def _read(bot_id: str, user_id: str, *, advance: bool = True):
    from astrbot_plugin_suli_emotion import load_hormone_state

    st = load_hormone_state(bot_id, user_id)
    tun = _tuning(bot_id)
    aff = _affinity(bot_id, user_id)
    return st, tun, aff, st.read(
        time.time(), tun, affinity_level=aff, is_owner=_is_owner(bot_id, user_id),
        advance=advance,
    )


def _persist(bot_id: str, user_id: str, zone: str) -> None:
    """节流落盘: 每 60s 一次, 或分区跳变时立刻落 (跳变点才是有价值的现场)。"""
    key = (str(bot_id), str(user_id))
    now = time.monotonic()
    changed = _LAST_ZONE.get(key) != zone
    if not changed and now - _SAVE_AT.get(key, 0.0) < _SAVE_INTERVAL:
        return
    _SAVE_AT[key] = now
    _LAST_ZONE[key] = zone
    try:
        from astrbot_plugin_suli_emotion import save_hormone_state

        save_hormone_state(_state(bot_id, user_id))
    except Exception:
        logger.debug("荷尔蒙落盘失败 bot=%s", bot_id, exc_info=True)


def _apply(
    bot_id: str,
    user_id: str,
    text: str,
    *,
    self_talk: bool,
    group_id: str = "",
    private: bool = False,
) -> None:
    """识别 + 注入 + 落盘。这是唯一的写入口。"""
    if not _active(bot_id, group_id, user_id, private=private):
        return
    jump = "noop"
    r = None
    try:
        from astrbot_plugin_suli_emotion import classify_text

        voice = voice_for(bot_id)
        sig = classify_text(text, extra_bank=voice.get("extra_keys") or None)
        if sig is None:
            # 没有具体信号也要推进衰减, 否则静默期的憋屈/退火永远不动
            _read(bot_id, user_id)
        else:
            st, tun, aff, _ = _read(bot_id, user_id, advance=False)
            jump = st.apply(sig, time.time(), tun, affinity_level=aff, self_talk=self_talk)
            r = st.read(time.time(), tun, affinity_level=aff, is_owner=_is_owner(bot_id, user_id))
            if jump == "climax":
                _on_climax(bot_id, aff)
    except Exception:
        logger.debug("荷尔蒙喂入失败", exc_info=True)
        return

    if r is not None:
        logger.info(
            "荷尔蒙 bot=%s user=%s kind=%s → %s (h=%.2f p=%.2f s=%.2f f=%.2f)",
            bot_id[:8], str(user_id)[:8], jump, r.zone,
            r.heat, r.pressure, r.satiety, r.frustration,
        )
        _persist(bot_id, user_id, r.zone)


def _on_climax(bot_id: str, affinity_level: float) -> None:
    """高潮 → 顺手推一把全局情绪 (arousal/valence), 让表情层跟上 (情绪回路)。"""
    try:
        from astrbot_plugin_suli_emotion import get_global_mood

        gm = get_global_mood(bot_id)
        gm.apply_event(0.18, 0.35, "hormone:climax", affinity_level=int(affinity_level))
    except Exception:
        logger.debug("荷尔蒙→情绪回写失败", exc_info=True)


def observe_user(
    bot_id: str,
    user_id: str,
    text: str,
    *,
    group_id: str = "",
    private: bool = False,
) -> None:
    """用户消息进来时调用 (热路径, 零 LLM 成本)。"""
    _apply(bot_id, user_id, text, self_talk=False, group_id=group_id, private=private)


def observe_self(
    bot_id: str,
    user_id: str,
    text: str,
    *,
    group_id: str = "",
    private: bool = False,
) -> None:
    """自己刚发出去的内容回灌 — 自反馈 (有上限, 防止自激烧穿)。"""
    _apply(bot_id, user_id, text, self_talk=True, group_id=group_id, private=private)


def sweep(
    bot_id: str,
    user_id: str,
    *,
    silence_s: float,
    group_id: str = "",
    private: bool = False,
) -> str:
    """静默巡检 (由 suli_proactive 的 tick 调用): 戏打到一半人不见了 → 记一次中断。

    这是「她先开口接上上次」的唯一燃料来源 —— 没有它, 戏断了就是断了。

    Returns: "interrupted" | "idle" | "off"
    """
    if not _active(bot_id, group_id, user_id, private=private):
        return "off"
    try:
        from astrbot_plugin_suli_emotion import HormoneSignal

        st, tun, aff, r = _read(bot_id, user_id)
    except Exception:
        logger.debug("荷尔蒙静默巡检读取失败", exc_info=True)
        return "idle"
    try:
        # 曾经的蓄积够高 + 现在沉默够久 + 还没记过中断
        if r.pressure < 0.25 and st.last_intimate_at <= 0:
            return "idle"
        gap = time.time() - max(st.last_intimate_at, st.scene_gap_at)
        if gap < max(600.0, silence_s) or st.scene_gap_at >= st.last_intimate_at:
            return "idle"
        if st.satiety >= tun.afterglow_satiety:
            return "idle"
        st.apply(
            HormoneSignal(kind="interrupt", intensity=0.7, note="silence"),
            time.time(), tun, affinity_level=aff,
        )
        r2 = st.read(time.time(), tun, affinity_level=aff)
    except Exception:
        logger.debug("荷尔蒙静默巡检失败", exc_info=True)
        return "idle"
    logger.info(
        "荷尔蒙 中断记录 bot=%s user=%s gap=%.0fs → 憋屈 %.2f (RESUME=%s)",
        bot_id[:8], str(user_id)[:8], gap, r2.frustration, r2.resume_urge,
    )
    _persist(bot_id, user_id, r2.zone)
    return "interrupted"


# ── 渲染 ────────────────────────────────────────────────────


def hint(
    bot_id: str,
    user_id: str,
    *,
    group_id: str = "",
    private: bool = False,
) -> str:
    """供 prompt_builder 注入 dynamic_parts 的文本。

    live 之外一律返回空串 (shadow 只记日志) —— 保证「开了开关才能看到变化」。
    """
    if not _active(bot_id, group_id, user_id, private=private):
        return ""
    try:
        from astrbot_plugin_suli_emotion import render_hormone_hint

        _, _, _, r = _read(bot_id, user_id)
    except Exception:
        logger.debug("荷尔蒙提示渲染失败", exc_info=True)
        return ""

    if str(bot_id) not in _READY_LOGGED:
        _READY_LOGGED.add(str(bot_id))
        logger.info(
            "荷尔蒙曲线已接线: bot=%s mode=%s zone=%s (首次触发)",
            bot_id, mode(bot_id), r.label,
        )
    if mode(bot_id) != MODE_LIVE:
        if r.active:
            logger.info(
                "[shadow] 荷尔蒙 bot=%s user=%s zone=%s burst=%d/%.2f gap=%.1f "
                "sol=%s esc=%s pw=%.2f sulky=%s resume=%s",
                bot_id[:8], str(user_id)[:8], r.label, r.burst_max, r.burst_p,
                r.gap_s, r.solicit, r.escalate, r.proactive_weight, r.sulky,
                r.resume_urge,
            )
        return ""
    try:
        text = render_hormone_hint(r, voice=voice_for(bot_id))
    except Exception:
        logger.debug("荷尔蒙文本渲染失败", exc_info=True)
        return ""
    if r.solicit_due and r.solicit:
        _note_solicit(bot_id, user_id)
    return text


def _note_solicit(bot_id: str, user_id: str) -> None:
    try:
        st = _state(bot_id, user_id)
        st.solicits_today += 1
        _persist(bot_id, user_id, _LAST_ZONE.get((str(bot_id), str(user_id)), "edge"))
    except Exception:
        logger.debug("荷尔蒙索求计数失败", exc_info=True)


def knobs(
    bot_id: str,
    user_id: str,
    *,
    group_id: str = "",
    private: bool = False,
) -> dict:
    """发送侧旋钮。门控不过 / shadow / 异常 → NEUTRAL (与改动前行为逐字对齐)。"""
    if not _active(bot_id, group_id, user_id, private=private):
        return dict(NEUTRAL)
    try:
        _, _, _, r = _read(bot_id, user_id)
    except Exception:
        logger.debug("荷尔蒙旋钮读取失败", exc_info=True)
        return dict(NEUTRAL)
    if mode(bot_id) != MODE_LIVE:
        # shadow = 影子模式: 照算照记日志, 但对调用方完全隐形
        # (观测通道是 hint() 里的 [shadow] 日志, 不是返回值)
        return dict(NEUTRAL)
    return {
        "live": True,
        "burst_max": r.burst_max,
        "burst_p": r.burst_p,
        "gap_s": r.gap_s,
        "split": r.split,
        # 碎片化上限 = 引擎的 frag_max (打字节奏维度, 与追加条数解耦)
        "frag_max": r.frag_max,
        "onoma": r.onoma,
        "escalate": r.escalate,
        "solicit": r.solicit,
        "proactive_weight": r.proactive_weight,
        "zone": r.zone,
    }


def note_burst(bot_id: str, user_id: str, n: int = 1) -> None:
    """记一次连发消耗 (每日总量兜底, 防刷屏烧 token)。"""
    if n <= 0 or not _active(bot_id, "", user_id, private=True):
        return
    try:
        st = _state(bot_id, user_id)
        st.bursts_today += int(n)
        _persist(bot_id, user_id, _LAST_ZONE.get((str(bot_id), str(user_id)), "edge"))
    except Exception:
        logger.debug("荷尔蒙连发计数失败", exc_info=True)


def proactive_weight(bot_id: str, user_id: str) -> float:
    """主动消息意愿乘数 — 注入 suli_proactive 的决策门控 (1.0 = 中性)。"""
    return float(knobs(bot_id, user_id, private=True).get("proactive_weight", 1.0))


def snapshot(bot_id: str, user_id: str) -> dict:
    """面板 / 调试用快照。"""
    try:
        st, tun, aff, r = _read(bot_id, user_id)
    except Exception:
        logger.debug("荷尔蒙快照失败", exc_info=True)
        return {"error": "unavailable"}
    return {
        "bot_id": str(bot_id),
        "user_id": str(user_id),
        "mode": mode(bot_id),
        "zone": r.zone,
        "label": r.label,
        "heat": r.heat,
        "pressure": r.pressure,
        "satiety": r.satiety,
        "frustration": r.frustration,
        "drive": r.drive,
        "threshold": r.threshold,
        "affinity": round(aff, 2),
        "burst_max": r.burst_max,
        "burst_p": r.burst_p,
        "gap_s": r.gap_s,
        "solicit": r.solicit,
        "sulky": r.sulky,
        "refractory": r.refractory,
        "resume_urge": r.resume_urge,
        "proactive_weight": round(r.proactive_weight, 3),
        "releases_today": st.releases_today,
        "bursts_today": st.bursts_today,
        "updated_at": st.updated_at,
        "history": st.history[-12:],
        "tuning": tun.__dict__,
    }


def reset(bot_id: str, user_id: str) -> None:
    try:
        from astrbot_plugin_suli_emotion import reset_hormone_state

        reset_hormone_state(bot_id, user_id)
        _LAST_ZONE.pop((str(bot_id), str(user_id)), None)
        logger.info("荷尔蒙状态已重置 bot=%s user=%s", str(bot_id)[:8], str(user_id)[:8])
    except Exception:
        logger.debug("荷尔蒙重置失败", exc_info=True)
