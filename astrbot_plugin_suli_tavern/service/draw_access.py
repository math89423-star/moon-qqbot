"""绘图访问策略 — 单一真相源 (2026-10-12 用户裁定)。

裁定原文:
  「凡是消耗额度的，包括 pixai 都仅限 Master 可以用，或者是在与 master 互动的
    nsfw 中调用；一般正常群聊用户走 novelai v4.5full，master 默认走 v5 full。」

拆成两条正交轴, 本模块是唯一答案入口 (原则 24: 一个数据一个源):

  1. 后端轴 (花钱): 计费后端 (`METERED_PROVIDERS`, 默认 pixai) 只对主人开放。
     非主人命中计费后端 → **自动降级**到免费后端 (novelai), 不出报错、不打扰用户。
     放行路径两条 (都会记进日志 reason):
       - `master`      : 触发者本人是主人 (owner_qq / super_admin_qq / OWNER_QQ_WHITELIST)
       - `nsfw_master` : 主人自配的 NSFW 通路 (bot 开关 + 群白名单 + 人白名单全过)
  2. 模型轴 (能力): NovelAI 后端按身份分档
       - 主人      → `draw_novelai_model`        (默认 nai-diffusion-5-full，high effort)
       - 普通用户  → `draw_novelai_model_public` (默认 nai-diffusion-5-full，medium effort)

参考图 (Vibe) 的例外 (刻意的, 不是漏判):
  V5 full 不支持任何参考图字段 (实测 500, 见 AGENT.md §9.2) —— 主人档是 V5 时,
  带参考图的这一次会临时改用**另一档里支持 Vibe 的模型** (pick_model(has_refs=True)),
  否则参考图会被静默丢弃。「默认走 V5」保留, 指的是不带参考图的默认路径。

不在这里做的事:
  - 不读 Key / Token 是否配好 (调用方 `_draw_backend_missing_text` 负责);
  - 不管好感等级 / 每日限额 / 冷却 (那是第二层, tools.py 门控 1-3 负责)。
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 主人档 / 公共档的兜底默认 (面板未写时的最后一道; 面板 scope 见 webui/server.py _DRAW_DEFAULTS)
# 2026-10-16: 公共档回落 V4.5 (群友面单写 V5 案) —— V5 走电量真金白银，
# 群友默认跑 V5 等于全员烧主人档；V4.5 支持 Vibe 参考图，兼容性更好。
DEFAULT_MASTER_MODEL = "nai-diffusion-5-full"
DEFAULT_PUBLIC_MODEL = "nai-diffusion-4-5-full"

# 计费后端名单 —— 花真钱/真额度的后端。默认只有 pixai。
# 需要放宽/扩列时用 env 覆盖: MOON_DRAW_METERED_PROVIDERS="pixai,future_backend"
_DEFAULT_METERED = "pixai"


def _metered_providers() -> frozenset[str]:
    raw = (os.getenv("MOON_DRAW_METERED_PROVIDERS", "") or "").strip()
    if not raw:
        return frozenset({_DEFAULT_METERED})
    return frozenset(p.strip() for p in raw.replace(",", " ").split() if p.strip())


METERED_PROVIDERS: frozenset[str] = _metered_providers()


def _truthy(raw: object) -> bool:
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")


def _svc():
    """配置服务入口 (单测可替换)。"""
    from .bot_config import get_config_service

    return get_config_service()


def _cfg_get(svc, key: str, default: str = "") -> str:
    """裸配置读取, 任何异常回 default (策略判断永不因读库失败而炸)。"""
    try:
        v = svc.db.get_config(key, default)
    except Exception:
        return default
    return default if v is None else str(v)


# ── 主人身份 ────────────────────────────────────────────


def get_master_ids(bot_id: str = "") -> set[str]:
    """主人 QQ 集合 (并集, 逐层兜底)。缺省返回空集 = 无人是主人 (fail-closed)。

    收口来源:
      1. DB `bot:{bot_id}:owner_qq`  — 管理面板 per-bot 主人 (真相源)
      2. DB `super_admin_qq`         — 全局超管
      3. env OWNER_QQ_WHITELIST / SUPER_ADMIN_QQ / ADMIN_QQ (docker/.env)
      4. Config().super_admin_qq / OWNER_QQ_WHITELIST
    注意: **不含** `admin_qq_ids` —— 普通管理员不是主人, 计费后端只认主人。
    """
    ids: set[str] = set()
    with contextlib.suppress(Exception):
        ids.add(_cfg_get(_svc(), f"bot:{bot_id}:owner_qq", "").strip())
    with contextlib.suppress(Exception):
        ids.add(_cfg_get(_svc(), "super_admin_qq", "").strip())
    try:
        from .bot_identity import get_admin_qq, get_owner_qq_set

        ids |= get_owner_qq_set()
        _sa = get_admin_qq()
        if _sa:
            ids.add(_sa)
    except Exception:
        logger.debug("主人集合: env 来源读取失败", exc_info=True)
    try:
        from ..config import Config

        _c = Config()
        _sa2 = str(getattr(_c, "super_admin_qq", "") or "").strip()
        if _sa2:
            ids.add(_sa2)
        for _q in getattr(_c, "OWNER_QQ_WHITELIST", None) or []:
            _q = str(_q or "").strip()
            if _q:
                ids.add(_q)
    except Exception:
        logger.debug("主人集合: Config 来源读取失败", exc_info=True)
    return {q for q in ids if q and q.lower() not in ("0", "none")}


def is_master(bot_id: str, user_id: str) -> bool:
    """user_id 是否主人。空 user_id → False (fail-closed)。"""
    uid = str(user_id or "").strip()
    if not uid:
        return False
    try:
        return uid in get_master_ids(bot_id)
    except Exception:
        logger.debug("主人判定异常, fail-closed", exc_info=True)
        return False


# ── 计费后端门 ──────────────────────────────────────────


def is_metered_provider(provider: str) -> bool:
    return str(provider or "").strip().lower() in METERED_PROVIDERS


def metered_master_only() -> bool:
    """计费后端是否仅主人可用 (默认 True)。全局键, 不按 bot 分。"""
    return _truthy(_cfg_get(_svc(), "draw_metered_master_only", "true"))


def metered_nsfw_allow() -> bool:
    """主人 NSFW 通路是否额外放行计费后端 (默认 True)。

    放宽范围 = 主人自己配的 NSFW 白名单 (bot 开关 + 群白名单 + 人白名单);
    名单里只有主人 → 与「仅主人」等价 (当前线上配置即如此)。
    """
    return _truthy(_cfg_get(_svc(), "draw_metered_nsfw_allow", "true"))


def metered_allowed(
    bot_id: str, user_id: str, group_id: str = "", *, private: bool = False,
) -> tuple[bool, str]:
    """计费后端放行判定 → (allowed, reason)。reason 进日志, 便于事后审计。"""
    if is_master(bot_id, user_id):
        return True, "master"
    if not metered_master_only():
        return True, "policy_open"
    if metered_nsfw_allow():
        try:
            from .nsfw_zone import is_nsfw_zone_any

            if is_nsfw_zone_any(bot_id, group_id, str(user_id), private=private):
                return True, "nsfw_master"
        except Exception:
            logger.debug("NSFW 通路判定异常, 按不通路处理", exc_info=True)
    return False, "metered_master_only"


# ── 模型档位 ────────────────────────────────────────────


def supports_vibes(model: str) -> bool:
    """该模型是否支持参考图 (Vibe)。真相源在 novelai_client, 缺失时按命名兜底。"""
    m = str(model or "")
    try:
        from astrbot_plugin_suli_novelai import novelai_client as _nc

        return bool(_nc.supports_vibes(m))
    except Exception:
        return "diffusion-4" in m.lower()


@dataclass(frozen=True)
class DrawRoute:
    """一次生图请求的生效路线 (面板分配 + 访问策略的结果)。"""

    assigned: str = "novelai"      # 面板分配的后端 (改策略前)
    provider: str = "novelai"      # 生效后端 (降级后)
    tier: str = "public"           # master / public
    model: str = ""                # 生效模型 (novelai 档位模型; pixai 为面板模型)
    novelai: dict = field(default_factory=dict)   # novelai 面板快照 (model 已替换为生效档)
    pixai: dict = field(default_factory=dict)     # pixai 面板快照
    downgraded: bool = False       # 是否因额度策略降级
    reason: str = ""               # master / nsfw_master / metered_master_only / ...
    is_metered: bool = False       # 生效后端是否计费 (降级后为 False)

    def pick_model(self, *, has_refs: bool) -> tuple[str, str]:
        """按「本次是否带参考图」定模型 → (model, note)。

        V5 不支持参考图字段 (实测 500)。带参考图 + 当前档模型不支持 Vibe →
        先改用另一档里支持 Vibe 的模型 (note="vibe_carve_out")；
        2026-10-14 起双档统一 V5，两档都不支持时回落硬编码 V4.5
        (note="vibe_fallback_v45")——参考图目前只支持 V4.5，能力兜底不是配置绕开。
        """
        _m = str(self.model or "")
        if not has_refs or self.provider != "novelai" or supports_vibes(_m):
            return _m, ""
        _alt = str(self.novelai.get("model_alt") or "")
        if _alt and _alt != _m and supports_vibes(_alt):
            return _alt, "vibe_carve_out"
        return "nai-diffusion-4-5-full", "vibe_fallback_v45"

    def log_line(self, *, bot_id: str = "", user_id: str = "") -> str:
        """运维可读的一行 (进日志, 不进用户可见文本)。"""
        return (
            f"tier={self.tier} assigned={self.assigned} provider={self.provider} "
            f"model={self.model or '-'} metered={int(self.is_metered)} "
            f"reason={self.reason or '-'}"
            + (f" bot={bot_id}" if bot_id else "")
            + (f" user={str(user_id)[:8]}" if user_id else "")
        )


def resolve_route(
    bot_id: str,
    *,
    user_id: str = "",
    group_id: str = "",
    private: bool = False,
) -> DrawRoute:
    """算出生效路线 (面板分配 → 访问策略 → 分档模型)。

    永不抛: 内部异常一律按「非主人 + 免费后端」降级处理 (钱袋子 fail-closed)。
    """
    try:
        svc = _svc()
        assigned = svc.get_draw_provider(bot_id or "")
        ncfg = dict(svc.get_draw_novelai() or {})
        pcfg = dict(svc.get_draw_pixai() or {})
    except Exception:
        logger.warning("🎨 绘图分配读取异常, fail-closed 返回 none", exc_info=True)
        return DrawRoute(assigned="none", provider="none", tier="public", reason="read_error")

    tier = "master" if is_master(bot_id, user_id) else "public"
    _master_model = str(ncfg.get("model") or DEFAULT_MASTER_MODEL)
    _public_model = str(ncfg.get("model_public") or DEFAULT_PUBLIC_MODEL)
    model = _master_model if tier == "master" else _public_model

    provider, reason, downgraded = assigned, "", False
    is_metered = is_metered_provider(assigned)
    if is_metered:
        allowed, why = metered_allowed(bot_id, user_id, group_id, private=private)
        if allowed:
            reason = why
        else:
            downgraded = True
            provider = "novelai" if (ncfg.get("token") or "").strip() else "none"
            reason = f"{why}→downgrade:{provider}"
            is_metered = False

    ncfg["model"] = model                                  # 生效档位模型
    ncfg["model_master"] = _master_model
    ncfg["model_public"] = _public_model
    ncfg["model_alt"] = _public_model if tier == "master" else _master_model
    ncfg["tier"] = tier

    if provider == "novelai":
        eff_model = model
    elif provider == "pixai":
        eff_model = str(pcfg.get("model") or "")
    else:
        eff_model = ""

    return DrawRoute(
        assigned=assigned, provider=provider, tier=tier, model=eff_model,
        novelai=ncfg, pixai=pcfg, downgraded=downgraded, reason=reason, is_metered=is_metered,
    )
