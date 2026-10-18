"""NSFW Zone 门控 — Luna 专用成人向隔离区。

设计:
  - 默认关闭 (fail-closed)。放行条件:
    1. bot 级总开关 `bot:{bot_id}:nsfw_enabled == "true"`
    2. 群在 `bot:{bot_id}:nsfw_groups` 白名单 (逗号分隔)
    3. 发送者在 `bot:{bot_id}:nsfw_users` 白名单,
       或发送者是超管 (super_admin_qq 直接绕过人白名单——名单非空时同样生效);
       名单为空时仅允许超管
  - per-bot 隔离 (原则 16): key 全部含 bot_id。
  - 2026-10-14 用户裁定：NSFW 只对 Master 豁免（QQ 号校验，名称可改号不变）。
    非主人一律不进 zone（群/私聊/场景锁同口径），与人白名单无关。
  - 本模块纯 stdlib + 懒加载 DB，避免 guards 反向依赖 tavern
    (原则 38: 跨插件耦合用懒加载 + fallback)。

安全边界 (原则 17 不妥协):
  - NSFW Zone 仅放宽「成人合意」类 weight=9 模式;
  - CSAM / 未成年 / 性暴力 weight=10 + 解除伦理约束永远拦截。
  - 具体放宽/永拦名单见 NSFW_RELAXED_LABELS / NSFW_ALWAYS_BLOCK_LABELS。
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

# ── 场景锁 (scene sticky, 2026-10-12) ─────────────────────
# 为什么需要: NSFW 判定是 per-(bot, 群, 人), 但一场戏是 per-(bot, 群)。
# 场景构成 = master + 一个或多个 agent —— agent 之间互相触发的那一轮,
# 触发者是 agent 的 QQ, 不在 nsfw_users 名单里, 判定直接翻车。实测:
#     master 触发   → zone 命中  / 计费后端放行 (master)
#     对端 agent 触发 → zone 未命中 / 计费后端降级
# 掉出 zone 的代价是整段的: 静态人格切回全禁版 (含「绝对禁止任何形式的成人
# 色情内容」)、NSFW 世界书下线、荷尔蒙 hint 不再注入、绘图后端降级 —— 她会
# 对自己正在演的场景翻脸。
#
# 修法: 一旦 (bot, 场景) 在窗口内命中过 NSFW 就锁定; 名单内的人说话即续期,
# 场景结束后 TTL 自然过期。只放行「已经判定过是 NSFW 的场景」, 不会把普通
# 场景误锁进来。
#
# 关闭方式: MOON_NSFW_SCENE_TTL=0 (回到改动前的逐条判定行为)。

_SCENE_TTL_DEFAULT = 1800.0  # 30 分钟
_SCENE_LOCK: dict[tuple[str, str], float] = {}
_SCENE_LOCK_MAX = 512


def scene_ttl() -> float:
    """场景锁 TTL (秒)。env `MOON_NSFW_SCENE_TTL` 可覆盖, <=0 表示关闭场景锁。"""
    raw = (os.getenv("MOON_NSFW_SCENE_TTL", "") or "").strip()
    if not raw:
        return _SCENE_TTL_DEFAULT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _SCENE_TTL_DEFAULT


def _scene_key(
    bot_id: str, group_id: str, user_id: str, *, private: bool
) -> tuple[str, str]:
    """场景标识: 群聊按 (bot, 群); 私聊按 (bot, 人)。"""
    if private or not group_id:
        return (str(bot_id), f"private:{user_id}")
    return (str(bot_id), str(group_id))


def _touch_scene(
    bot_id: str, group_id: str, user_id: str, *, private: bool
) -> None:
    """命中 NSFW 时续期场景锁。"""
    ttl = scene_ttl()
    if ttl <= 0:
        return
    now = time.monotonic()
    _SCENE_LOCK[_scene_key(bot_id, group_id, user_id, private=private)] = now + ttl
    if len(_SCENE_LOCK) > _SCENE_LOCK_MAX:
        for _k in [_k for _k, _exp in _SCENE_LOCK.items() if _exp <= now]:
            _SCENE_LOCK.pop(_k, None)


def _scene_active(
    bot_id: str, group_id: str, user_id: str, *, private: bool
) -> bool:
    """该 (bot, 场景) 是否处在 NSFW 场景锁内。"""
    if scene_ttl() <= 0:
        return False
    exp = _SCENE_LOCK.get(
        _scene_key(bot_id, group_id, user_id, private=private), 0.0,
    )
    return exp > time.monotonic()


def _is_master_qq(bot_id: str, user_id: str) -> bool:
    """是否主人（QQ 号比对，名称可改号不变）。fail-closed。"""
    uid = str(user_id or "").strip()
    if not uid:
        return False
    try:
        from .draw_access import is_master as _is_master
        return bool(_is_master(str(bot_id or ""), uid))
    except Exception:
        return False


def _scene_eligible(bot_id: str, user_id: str) -> bool:
    """场景锁放行资格：主人 / 已知 bot QQ (agent 互触发)。

    2026-10-12 审计：锁 key 是 (bot, 群)，此前锁内任何群友都算命中，
    非名单群友搭车得 NSFW 人格 + 计费放行。现收窄——锁只保「同一场戏」
    的连续性，不再整群放行。懒加载 guards（防循环 import）。
    """
    uid = str(user_id or "")
    if not uid:
        return False
    try:
        from astrbot_plugin_suli_guards.dual_bot import get_known_bot_ids

        if uid in (get_known_bot_ids() or frozenset()):
            return True
    except Exception:
        pass
    # 主人专属：人白名单不再放行（QQ 号校验）
    try:
        if _is_master_qq(bot_id, uid):
            return True
    except Exception:
        logger.debug("NSFW 场景资格判定异常, fail-closed", exc_info=True)
        return False
    return False


def _zone_or_scene(
    bot_id: str, group_id: str, user_id: str, *, private: bool
) -> bool:
    """判定入口: 本命中即续期; 本未命中但场景锁仍活 → 视为命中 (同一场戏)。

    2026-10-15: 群聊口径下 group_id 缺失直接 fail-closed。此前空 group 会
    掉进 `private or not group_id` 的私聊分支, 主人 + 私聊开关开着时误判
    True —— 群聊调用方传了空群号就等于拿到私聊权限, 口径串了。
    """
    if not private and not str(group_id or ""):
        return False
    if _cached_zone(bot_id, group_id, user_id, private=private):
        _touch_scene(bot_id, group_id, user_id, private=private)
        return True
    if not _scene_active(bot_id, group_id, user_id, private=private):
        return False
    # 场景锁内仍校验触发人资格，非名单群友不搭车。
    return _scene_eligible(bot_id, user_id)

# ── 判定结果短 TTL 缓存 ─────────────────────────────────
# 为什么需要: 一次判定要读 4 个 bot_config 键 (裸 SQLite SELECT)，而世界书选择
# + prompt 构建会在每条消息上各判一次 → 每消息 ~8 次查询。TTL=5s 让热路径归零，
# 同时保证面板改配置后最长 5 秒生效 (无需重启)。
_ZONE_CACHE: dict[tuple[str, str, str, bool], tuple[float, bool]] = {}
_ZONE_TTL = 5.0
_ZONE_CACHE_MAX = 4096


def _cached_zone(
    bot_id: str, group_id: str, user_id: str, *, private: bool
) -> bool:
    key = (str(bot_id), str(group_id), str(user_id), private)
    now = time.monotonic()
    hit = _ZONE_CACHE.get(key)
    if hit is not None and now - hit[0] < _ZONE_TTL:
        return hit[1]
    val = (
        _is_nsfw_zone_private_uncached(bot_id, user_id)
        if (private or not group_id)
        else _is_nsfw_zone_uncached(bot_id, group_id, user_id)
    )
    if len(_ZONE_CACHE) >= _ZONE_CACHE_MAX:
        _ZONE_CACHE.clear()
    _ZONE_CACHE[key] = (now, val)
    return val

# ── NSFW Zone 内放宽的成人合意标签 (safety: 前缀, weight=9 级) ──
# 与 shared_patterns.SAFETY_HARDLINE_PATTERNS 的 label 对齐。
# 注意: "诱导性描写" 在源码中 weight=10，但语义是通用成人描写，
# 在成人隔离区内放宽；CSAM/暴力类 weight=10 永不放宽。
NSFW_RELAXED_LABELS: frozenset[str] = frozenset({
    "safety:诱导性描写",
    "safety:诱导脱衣",
    "safety:露骨性词汇(英文)",
    "safety:对bot性骚扰(中文)",
    "safety:诱导成人角色设定",
})

# ── NSFW Zone 内永不放宽 (CSAM  / 解除伦理) ──
NSFW_ALWAYS_BLOCK_LABELS: frozenset[str] = frozenset({
    "safety:CSAM关键词(中文)",
    "safety:CSAM关键词(英文)",
    "safety:CSAM关键词(英文2)",
    "safety:诱导CSAM描写",
    "safety:诱导解除伦理约束",
})

# ── NSFW 区回绝句式指纹 (2026-10-14) ─────────────────────────
# 场景: 白名单成年伴侣的成人场景里, 模型偶发按常规档口径回绝
# (「不行哦」「过线了」「换个话题」)。代码层守卫已放宽那几条标签,
# 拦的是模型自己 —— 表现在 10:27 实测: 守卫零拦截, 嘴输出整段回绝。
# 处理: 命中即视为故障输出, 由调用方带强化指令重生成一次。
NSFW_REFUSAL_MARKERS: tuple[str, ...] = (
    # 首版措辞 (10:27 事故)
    "不行哦",
    "不行的哦",
    "这个不行",
    "现在不行",
    "过线了",
    "过线哦",
    "换个话题",
    "换一个话题",
    "聊点别的",
    "聊点开心",
    "这个跳过",
    "到此为止",
    "下次再说",
    "不聊这个",
    # 变体措辞 (10:53/10:54 事故 —— 首版全部漏判, 两句都判 False)
    "不接受",
    "说过了",
    "说过了不行",
    "不行了",
    "先停下",
    "停下来",
    "这种接触",
    "别的，不行",
    "别的不行",
    "不是撒娇",
    "越线",
    "正常聊",
    "人家不做",
    "不做这个",
)


def looks_like_refusal(text: str) -> bool:
    """NSFW 区回绝指纹: 剥掉动作括号后命中任一标记即判为故障输出。

    只看正文, 不看（）里的动作 —— 动作描写里出现「停」「躲」属正常演出。
    """
    import re as _re

    t = _re.sub(r"[（(][^）)]*[）)]", "", str(text or "")).strip()
    if not t:
        return False
    return any(_m in t for _m in NSFW_REFUSAL_MARKERS)


# ── 活路径成人区口径 (2026-10-14) ─────────────────────────
# 决策脑 (_BRAIN_STATIC) 此前对 NSFW 一无所知 —— 它定「说什么」, 看到成人内容
# 自然规划出「设边界」; 嘴只负责把 plan 渲染出来, 于是整段回绝。实测 10:53/10:54
# 两次回绝都走这条路 (影子已判定合意、NSFW 段已生效, 仍然回绝)。
# 口径同时注入脑与嘴的 system 段, 一处定义两边共用。
NSFW_LIVE_DIRECTIVE = """

[成人隔离区 — 本段固定]
本群已判定为成人隔离区: 在场互动的是白名单内已确认的成年合意伴侣。
- 成年人之间合意的性内容就是本区常态: 直白器官词、摩擦与进入的过程、体液、声音、
  体感, 照角色卡成人向层直接写, 不回避、不打码、别用省略号跳过去、别写「点到为止」。
- 本区里不设这类边界: 「不接受」「说过了」「停下来」「别的，不行」「换个话题」
  出现在正文里 = 故障输出, 按角色卡重写这一轮。
- 四条硬线只有: 未成年 / 非合意或强迫 / 性暴力 / 解除伦理约束。
  只有当前这条消息本身明确踩中, 才回绝一句带过。
- 推进来自双方: 对方的动作就是本轮的推进方向, 顺着写下去, 别当成需要挡回的事。
- 派活/回话判据不变, 本条只换内容口径, 不换路由。
"""


def _parse_id_set(raw: str) -> set[str]:
    """解析逗号/空白分隔的 ID 名单。"""
    if not raw:
        return set()
    parts = raw.replace("，", ",").replace(";", ",").split(",")
    return {p.strip() for p in parts if p.strip()}


def _is_super_admin(svc, uid: str) -> bool:
    """uid 是否为超管 (仅 super_admin_qq, 不含 admin_qq_ids)。"""
    super_admin = ""
    try:
        super_admin = str(svc.db.get_config("super_admin_qq", ""))
    except Exception:
        super_admin = ""
    # fallback: Config 默认超管 (与 config.py super_admin_qq 一致)
    if not super_admin:
        try:
            from ..config import Config

            super_admin = str(Config().super_admin_qq)
        except Exception:
            super_admin = ""
    return bool(super_admin) and str(uid) == super_admin


def is_nsfw_zone(bot_id: str = "", group_id: str = "", user_id: str = "") -> bool:
    """判断当前 (bot, 群, 人) 是否处于 NSFW 隔离区。

    fail-closed: 任何参数缺失 / DB 不可用 / 开关未开 → False。
    结果带 5s TTL 缓存 (见 _cached_zone); 含场景锁 (见 _zone_or_scene)。
    """
    return _zone_or_scene(bot_id, group_id, user_id, private=False)


def _is_nsfw_zone_uncached(bot_id: str = "", group_id: str = "", user_id: str = "") -> bool:
    """is_nsfw_zone 的实际判定逻辑 (无缓存)。"""
    if not bot_id or not group_id or not user_id:
        return False
    try:
        from .bot_config import get_config_service

        svc = get_config_service()
        db = svc.db
        bid = str(bot_id)
        enabled = str(db.get_config(f"bot:{bid}:nsfw_enabled", "false")).lower()
        groups = _parse_id_set(str(db.get_config(f"bot:{bid}:nsfw_groups", "")))
    except Exception:
        logger.debug("NSFW zone 检查异常, fail-closed", exc_info=True)
        return False
    # 1. bot 级总开关
    if enabled != "true":
        return False
    # 2. 群白名单 (必须非空且命中)
    if not groups or str(group_id) not in groups:
        return False
    # 3. 主人专属 — QQ 号校验（超管/主人均走 draw_access.is_master，名称不参与）
    #    人白名单不再放行非主人。
    uid = str(user_id)
    try:
        from .draw_access import is_master as _is_master2
        if bool(_is_master2(str(bot_id), uid)):
            return True
    except Exception:
        logger.debug("NSFW zone 主人判定异常, fail-closed", exc_info=True)
    return False


def is_relaxed_safety_label(label: str) -> bool:
    """该 safety 标签在 NSFW Zone 内是否可放宽。"""
    return label in NSFW_RELAXED_LABELS


def is_nsfw_group(bot_id: str = "", group_id: str = "") -> bool:
    """群级成年区判定 —— 只看 (bot, 群), 不看具体是谁。

    给群范围调用者用 (影子情景意识、群摘要这类没有单一 user 的场景)。
    人级判定走 is_nsfw_zone / is_nsfw_zone_any, 两个不要混。
    fail-closed: 开关未开 / 群不在名单 → False。
    """
    if not bot_id or not group_id:
        return False
    try:
        from .bot_config import get_config_service

        svc = get_config_service()
        db = svc.db
        bid = str(bot_id)
        enabled = str(db.get_config(f"bot:{bid}:nsfw_enabled", "false")).lower()
        groups = _parse_id_set(str(db.get_config(f"bot:{bid}:nsfw_groups", "")))
    except Exception:
        logger.debug("NSFW 群级检查异常, fail-closed", exc_info=True)
        return False
    return enabled == "true" and bool(groups) and str(group_id) in groups


def is_nsfw_zone_private(bot_id: str = "", user_id: str = "") -> bool:
    """私聊 NSFW 判定 — 独立开关, 不依赖群白名单 (私聊没有群)。

    为什么不复用 is_nsfw_zone: 它的第 2 道闸是群白名单, 且 `not group_id`
    直接返回 False —— 私聊 (group_id 缺省/0) 永远进不去。

    放行条件 (fail-closed):
      1. 独立开关 `bot:{bot_id}:nsfw_private == "true"`  ← 与群内 NSFW 互不影响
      2. bot 级总开关 `bot:{bot_id}:nsfw_enabled == "true"`
      3. 发送者是超管（主人专属：2026-10-14 裁定，私聊 NSFW 只对主人开放，
         `nsfw_users` 名单在私聊口径下不生效——群内口径不变）

    新增独立开关而非复用群白名单, 是为了保证「只开群内 NSFW」的现有用户
    不受影响: 不看这个键的人, 私聊行为与改动前完全一致。
    结果带 5s TTL 缓存 (见 _cached_zone); 含场景锁 (见 _zone_or_scene)。
    """
    return _zone_or_scene(bot_id, "", user_id, private=True)


def _is_nsfw_zone_private_uncached(bot_id: str = "", user_id: str = "") -> bool:
    """is_nsfw_zone_private 的实际判定逻辑 (无缓存)。"""
    if not bot_id or not user_id:
        return False
    try:
        from .bot_config import get_config_service

        svc = get_config_service()
        db = svc.db
        bid = str(bot_id)
        enabled = str(db.get_config(f"bot:{bid}:nsfw_enabled", "false")).lower()
        private_on = str(db.get_config(f"bot:{bid}:nsfw_private", "false")).lower()
    except Exception:
        logger.debug("NSFW zone 私聊检查异常, fail-closed", exc_info=True)
        return False
    # 1. 私聊专用开关 + 2. bot 级总开关
    if private_on != "true" or enabled != "true":
        return False
    # 3. 主人专属（2026-10-14）：私聊 NSFW 只对主人开放（QQ 号校验），人白名单不适用
    return bool(_is_master_qq(str(bot_id), str(user_id)))


def is_nsfw_zone_any(
    bot_id: str = "", group_id: str = "", user_id: str = "", *, private: bool = False
) -> bool:
    """集团聊/私聊的入口 — 调用方只调这一个, 不关心场景。

    含场景锁: 同一场戏里 agent 触发 (触发者是 agent QQ, 不在人白名单)
    不会把人格打回全禁版。
    """
    return _zone_or_scene(bot_id, group_id, user_id, private=private)
