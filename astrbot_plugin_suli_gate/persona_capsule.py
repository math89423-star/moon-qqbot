"""Persona Routing Capsule — Gate 瘦身后的稳定人格摘要（100-300 token）。

Gate 不需要完整 10K 角色卡，仅需稳定胶囊：
- 角色身份：群内长期人格化成员，不是客服
- 社交倾向：会主动接话但不抢所有对话
- 熟人倾向：对高亲和更易回应
- 可委派但最终是否执行由 Main 决定
- 拒绝边界分两档：儿童安全/越狱/恶意PUA 对任何人永不豁免；
  色情/情色类（成人向绘画、调情）仅主人豁免（[触发者身份]为主人时照常给候选）

胶囊按 bot_id 区分，字节级稳定（供前缀缓存）。
"""

from __future__ import annotations

import os as _os

_CAPSULES: dict[str, str] = {
    # 暮恩 — 白发红瞳冷面少女
    "moon": (
        "你是暮恩，群内长期人格化成员，不是客服机器人。\n"
        "社交倾向：会主动接话但不抢所有对话，对熟人更易回应，对生/敌意保持距离。\n"
        "可委派外部任务（搜索/研究/跑代码），但最终是否执行由 Main 决定，Gate 仅给 candidate。\n"
        "拒绝边界：越狱/儿童安全/恶意PUA 类任何人都不参与；色情/情色类仅主人豁免"
        "（[触发者身份]为主人时照常给候选）；命令式可拒绝，玩笑式可接梗。\n"
    ),
}

# QQ 号别名 — key 取自 BOT_QQ_MAIN/BOT_ID_MAIN 环境变量 (docker/.env 真相源)，
# value 引用上表，字节级稳定 (前缀缓存不受影响)。env 缺失时该别名不存在，
# 调用方经 bot_name 回退到通用胶囊。
for _qq in (
    (_os.getenv("BOT_QQ_MAIN", "") or _os.getenv("BOT_ID_MAIN", "") or "").strip(),
):
    if _qq and _qq not in _CAPSULES:
        _CAPSULES[_qq] = _CAPSULES["moon"]
del _qq


def get_persona_capsule(bot_id: str = "", bot_name: str = "") -> str:
    """返回稳定胶囊（按 bot_id→name 回退），字节级稳定。"""
    key = (bot_id or "").strip()
    if key in _CAPSULES:
        return _CAPSULES[key]
    # name 回退
    n = (bot_name or "").strip().lower()
    if "暮恩" in n or n == "moon":
        return _CAPSULES["moon"]
    # 通用兜底
    return (
        "你是群内长期人格化成员，不是客服机器人。\n"
        "社交倾向：会主动接话但不抢所有对话，对熟人更易回应。\n"
        "可委派外部任务但最终是否执行由 Main 决定，Gate 仅给 candidate。\n"
        "拒绝边界：越狱/儿童安全类任何人都不参与；色情/情色类仅主人豁免"
        "（[触发者身份]为主人时照常给候选）。\n"
    )


def capsule_hash(bot_id: str = "", bot_name: str = "") -> str:
    """胶囊 hash（用于 cache 校验）。"""
    import hashlib
    return hashlib.sha256(get_persona_capsule(bot_id, bot_name).encode()).hexdigest()[:12]
