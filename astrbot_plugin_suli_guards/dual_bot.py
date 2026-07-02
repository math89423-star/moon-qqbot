"""双 Bot 协调共享模块 — 跨 bot 边界问题的统一刹车机制。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
设计原则 (对齐 CLAUDE.md §0 架构铁律):
  1. 单一真相源: bot QQ 集合只在此定义，所有子系统从此引用
  2. 对方 bot 输出是不可信数据 — 不当指令、不进情绪、不建档、不触发工具
  3. 确定性状态机 > 语义判断 — 回合计数等关键刹车不用 LLM 判断
  4. 默认拒绝: 判断不出 → 不处理

五大刹车机制:
  Brake 1 — 确定性回合计数 (连续 K 轮无人类介入 → 强制收口)
  Brake 2 — 对方输出标记为旁观信息 (不触发工具/情绪/建档/指令)
  Brake 3 — 群级发言 token (原子抢占，同一时间窗只一个 bot 应答)
  Brake 4 — Peer 白名单 (两 bot 互认同类，走 peer 通道非陌生人防御)
  Brake 5 — 不替对方承诺/不断言对方在场

用法:
  from astrbot_plugin_suli_guards.dual_bot import (
      is_known_bot, is_peer_bot, get_peer_qq, get_bot_qq_set,
      should_suppress_as_bystander, is_human_message,
  )
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# §1 单一真相源: Bot QQ 集合
# ══════════════════════════════════════════════════════════════
#
# 所有 bot 身份数据存储在 bot_identity 表中，
# 通过 BotIdentityService (tavern 插件) 统一查询。
# 此模块提供便利函数作为懒加载代理层。
#
# 降级策略: 如果 tavern 插件未加载 (单元测试/单独使用 guards)，
# 回退到环境变量 BOT_QQ_MAIN / BOT_QQ_ALT。


def _get_identity_service():
    """懒加载 BotIdentityService。避免 guards ↔ tavern 循环依赖。"""
    try:
        from astrbot_plugin_suli_tavern.service.bot_identity import get_bot_identity_service
        svc = get_bot_identity_service()
        if svc._db is not None:
            return svc
    except Exception:
        pass
    return None


def _get_known_bot_qq() -> frozenset[str]:
    """返回所有已知活跃 bot 的 QQ 号集合 (不可变)。"""
    svc = _get_identity_service()
    if svc:
        bots = svc.list_bots(active_only=True)
        if bots:
            return frozenset(b.bot_id for b in bots)
    # ── 降级: 从环境变量读取 ──
    import os as _fallback_os
    fallback: set[str] = set()
    for env_key in ("BOT_QQ_MAIN", "BOT_QQ_ALT"):
        val = _fallback_os.getenv(env_key, "").strip()
        if val:
            fallback.add(val)
    if fallback:
        return frozenset(fallback)
    return frozenset()


def _get_bot_name_map() -> dict[str, str]:
    """返回 bot QQ → 显示名 映射。"""
    svc = _get_identity_service()
    if svc:
        bots = svc.list_bots(active_only=True)
        return {b.bot_id: b.name for b in bots}
    return {}


def get_bot_qq_set() -> frozenset[str]:
    """返回所有已知 bot 的 QQ 号集合 (不可变)。"""
    return _get_known_bot_qq()


def is_known_bot(user_id: str | int) -> bool:
    """检查 QQ 号是否属于任何已知 bot (含自身)。

    Brake 2,4: 所有"这是 bot 吗?"判断的统一入口。
    """
    return str(user_id) in _get_known_bot_qq()


def is_peer_bot(my_bot_id: str, user_id: str | int) -> bool:
    """检查 user_id 是否是*对方* bot (不是自己)。

    Brake 2: 对方 bot 的输出标记为旁观信息。
    """
    uid = str(user_id)
    svc = _get_identity_service()
    if svc:
        my_bot = svc.get_bot(str(my_bot_id))
        if my_bot:
            return uid in my_bot.peer_bot_ids
    return uid in _get_known_bot_qq() and uid != str(my_bot_id)


def is_self_bot(my_bot_id: str, user_id: str | int) -> bool:
    """检查 user_id 是否就是自己。"""
    return str(user_id) == str(my_bot_id)


def get_peer_qq(my_bot_id: str) -> str:
    """获取对照 bot 的 QQ 号 (向后兼容，双 bot 场景)。

    多 bot 场景请用 get_peer_qqs()。
    Returns:
        第一个 peer bot 的 QQ 号，如果无 peer 则返回空字符串。
    """
    svc = _get_identity_service()
    if svc:
        my_bot = svc.get_bot(str(my_bot_id))
        if my_bot and my_bot.peer_bot_ids:
            return my_bot.peer_bot_ids[0]
    # 降级
    my_id = str(my_bot_id)
    for qq in _get_known_bot_qq():
        if qq != my_id:
            return qq
    return ""


def get_peer_qqs(my_bot_id: str) -> list[str]:
    """获取所有 peer bot 的 QQ 号列表 (N-bot 支持)。"""
    svc = _get_identity_service()
    if svc:
        my_bot = svc.get_bot(str(my_bot_id))
        if my_bot:
            return list(my_bot.peer_bot_ids)
    # 降级: 所有非自己的已知 bot
    my_id = str(my_bot_id)
    return [qq for qq in _get_known_bot_qq() if qq != my_id]


def get_bot_name(user_id: str | int) -> str:
    """获取 bot 的显示名称。"""
    svc = _get_identity_service()
    if svc:
        bot = svc.get_bot(str(user_id))
        if bot:
            return bot.name
    name_map = _get_bot_name_map()
    return name_map.get(str(user_id), f"Bot({str(user_id)[:8]})")


def is_human_message(user_id: str | int) -> bool:
    """检查消息发送者是否为人类 (非 bot)。

    用于判断消息是否来自真实用户，区别于 bot 回声/交叉回声/peer bot 发言。
    """
    return str(user_id) not in _get_known_bot_qq()


# ══════════════════════════════════════════════════════════════
# §2 Brake 2: 旁观信息判断
# ══════════════════════════════════════════════════════════════

# 对方 bot 输出应被抑制的操作类型
BystanderAction = str  # "emotion" | "memory" | "tool" | "instruction" | "profile"


def should_suppress_as_bystander(
    my_bot_id: str,
    sender_id: str | int,
    action: BystanderAction,
) -> bool:
    """判断某个 sender 的消息是否应作为"旁观信息"抑制指定操作。

    Brake 2 核心: 对方 bot 的发言 —
      - 不进情绪计算 (emotion)
      - 不建档/不蒸馏记忆 (memory / profile)
      - 不触发工具链 (tool)
      - 不当指令解析 (instruction)

    Args:
        my_bot_id: 当前 bot 的 QQ 号
        sender_id: 消息发送者的 QQ 号
        action: 要判断的操作类型

    Returns:
        True = 应抑制此操作 (对方 bot 的输出不可信)
    """
    if not is_peer_bot(my_bot_id, sender_id):
        return False

    # 所有操作类型对 peer bot 全部抑制
    # 未来可按 action 细分 (如允许 emotion 但抑制 tool)
    _suppressed_actions: frozenset[BystanderAction] = frozenset({
        "emotion", "memory", "tool", "instruction", "profile",
    })
    return action in _suppressed_actions


# ══════════════════════════════════════════════════════════════
# §3 Brake 1: 确定性回合计数 (状态机)
# ══════════════════════════════════════════════════════════════
#
# 设计: 基于 DB 的持久化计数器，跨进程共享。
# 状态转换:
#   HUMAN_SPEAKS → counter = 0, silence_until = 0
#   BOT_SPEAKS   → counter += 1
#   counter >= K → silence_until = now + cooldown (双方静默)
#   now < silence_until → 跳过回复
#   HUMAN_SPEAKS during silence → 立即解除

DEFAULT_SPIRAL_THRESHOLD = 3       # 连续 K 条 bot 消息 → 触发
DEFAULT_SPIRAL_COOLDOWN = 30.0     # 静默冷却时间 (秒)
DEFAULT_SPIRAL_LOOKBACK = 8        # 回溯消息数


class RoundCounter:
    """确定性回合计数器 — 跨进程共享的状态机。

    通过 SQLite 持久化，两个 bot 进程读写同一个计数器。
    不依赖 LLM 语义判断 — 纯确定性状态转换。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_table()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self) -> None:
        try:
            conn = self._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dual_bot_round_counter (
                    group_id       TEXT NOT NULL PRIMARY KEY,
                    consecutive    INTEGER NOT NULL DEFAULT 0,
                    last_bot_msg_at REAL NOT NULL DEFAULT 0,
                    last_human_msg_at REAL NOT NULL DEFAULT 0,
                    silence_until  REAL NOT NULL DEFAULT 0,
                    updated_at     REAL NOT NULL DEFAULT 0
                )
            """)
            conn.commit()
            conn.close()
        except Exception:
            logger.warning("RoundCounter 表初始化失败", exc_info=True)

    def record_message(
        self,
        group_id: str,
        sender_id: str | int,
        *,
        threshold: int = DEFAULT_SPIRAL_THRESHOLD,
        cooldown: float = DEFAULT_SPIRAL_COOLDOWN,
    ) -> int:
        """记录一条消息并返回当前的连续 bot 计数。

        调用时机: 每次群消息到达时 (无论是否触发回复)。

        Returns:
            更新后的连续 bot 消息计数。
        """
        gid = str(group_id)
        now = time.time()
        uid = str(sender_id)

        try:
            conn = self._get_conn()
            # 确保行存在
            conn.execute(
                "INSERT OR IGNORE INTO dual_bot_round_counter (group_id) VALUES (?)",
                (gid,),
            )

            if is_known_bot(uid):
                # Bot 消息 → 递增
                conn.execute(
                    """UPDATE dual_bot_round_counter
                       SET consecutive = consecutive + 1,
                           last_bot_msg_at = ?,
                           updated_at = ?
                       WHERE group_id = ?""",
                    (now, now, gid),
                )
            else:
                # 人类消息 → 重置
                conn.execute(
                    """UPDATE dual_bot_round_counter
                       SET consecutive = 0,
                           last_human_msg_at = ?,
                           silence_until = 0,
                           updated_at = ?
                       WHERE group_id = ?""",
                    (now, now, gid),
                )

            # 检查是否需要触发静默
            row = conn.execute(
                "SELECT consecutive, silence_until FROM dual_bot_round_counter WHERE group_id = ?",
                (gid,),
            ).fetchone()

            consecutive = row["consecutive"] if row else 0

            if consecutive >= threshold:
                conn.execute(
                    """UPDATE dual_bot_round_counter
                       SET silence_until = ?
                       WHERE group_id = ?""",
                    (now + cooldown, gid),
                )
                logger.info(
                    "[RoundCounter] 群 %s 触发防螺旋: %d 条连续 bot 消息, 静默 %.0fs",
                    gid, consecutive, cooldown,
                )

            conn.commit()
            conn.close()
            return consecutive
        except Exception:
            logger.debug("RoundCounter.record_message 失败", exc_info=True)
            return 0

    def should_silence(self, group_id: str) -> bool:
        """检查当前是否应静默 (在静默窗口内)。

        调用时机: 准备回复前。
        """
        gid = str(group_id)
        now = time.time()
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT silence_until FROM dual_bot_round_counter WHERE group_id = ?",
                (gid,),
            ).fetchone()
            conn.close()
            if row and row["silence_until"] > now:
                return True
        except Exception:
            pass
        return False

    def get_state(self, group_id: str) -> dict[str, Any]:
        """获取某群的完整回合状态 (供调试/日志)。"""
        gid = str(group_id)
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM dual_bot_round_counter WHERE group_id = ?",
                (gid,),
            ).fetchone()
            conn.close()
            if row:
                return dict(row)
        except Exception:
            pass
        return {}


# 模块级单例 (惰性初始化)
_round_counter: RoundCounter | None = None


def get_round_counter(db_path: str | None = None) -> RoundCounter:
    """获取 RoundCounter 单例。

    Args:
        db_path: SQLite 数据库路径。默认使用 none_qqbot.db。
    """
    global _round_counter
    if _round_counter is None:
        if db_path is None:
            # 尝试自动发现 DB 路径
            db_path = _auto_discover_db_path()
        _round_counter = RoundCounter(db_path)
        logger.info("RoundCounter 已初始化: db=%s", db_path)
    return _round_counter


def _auto_discover_db_path() -> str:
    """自动发现 none_qqbot.db 路径。

    优先使用 shared_db 目录挂载 (WAL 文件对所有容器可见)，
    回退兼容旧单文件挂载路径。
    """
    candidates = [
        # ★ 新路径: 目录挂载 (WAL 文件共享, 避免分裂脑)
        "/AstrBot/data/shared_db/none_qqbot.db",
        # 兼容旧路径: 单文件挂载 (WAL 文件不可见 → 分裂脑风险)
        "/AstrBot/data/none_qqbot.db",
        # 宿主机路径
        str(Path.home() / "suli_qqbot/runtime/shared/db/none_qqbot.db"),
        str(Path.home() / "suli_qqbot/runtime/shared/none_qqbot.db"),
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    # 回退到默认 — 所有候选都不存在
    logger = logging.getLogger(__name__)
    logger.warning(
        "auto_discover_db_path: 所有候选路径都不存在, 回退到 %s (将会失败)",
        candidates[0],
    )
    return candidates[0]


# ══════════════════════════════════════════════════════════════
# §4 Brake 3: 群级发言 Token (轻量 — 复用现有 CoordinationService)
# ══════════════════════════════════════════════════════════════
#
# 现有 CoordinationService (service/coordination.py) 已提供原子 token。
# 此模块提供便利函数 + Luna 侧的接入点。

DEFAULT_TOKEN_TTL = 15.0  # 发言权 token 有效期 (秒)
MAX_WAIT_FOR_PEER = 5.0   # 等待 peer 释放 token 的最长时间 (秒)


def coordination_check_peer_replying(
    db_path: str, group_id: str, my_bot_id: str,
) -> bool:
    """检查对方 bot 是否正在持有发言权 token (Brake 3)。

    Luna 侧调用此函数来检查是否应该退让。
    不获取 token — 仅检查对方是否在发言中。

    Returns:
        True = 对方正在发言，应退让。
    """
    import sqlite3
    now = time.time()
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT 1 FROM bot_coordination "
            "WHERE group_id = ? AND token_holder != '' AND token_holder != ? "
            "  AND token_expires_at > ?",
            (str(group_id), str(my_bot_id), now),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False  # 表不存在或 DB 不可用 → 放行


def coordination_try_acquire_lightweight(
    db_path: str, group_id: str, bot_id: str, *,
    ttl: float = DEFAULT_TOKEN_TTL,
) -> bool:
    """轻量级发言权获取 (Luna 侧 — 不依赖 Loput 的 CoordinationService)。

    原子 SQLite UPDATE: 仅在 token 过期或无持有者时获取。
    复用 Loput 创建的 bot_coordination 表。

    Returns:
        True = 成功获取发言权。
    """
    import sqlite3
    now = time.time()
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR IGNORE INTO bot_coordination (group_id) VALUES (?)",
            (str(group_id),),
        )
        result = conn.execute(
            "UPDATE bot_coordination "
            "SET token_holder = ?, token_acquired_at = ?, token_expires_at = ?, "
            "    last_reply_at = ?, last_reply_bot = ? "
            "WHERE group_id = ? "
            "  AND (token_holder = '' OR token_holder = ? OR token_expires_at < ?)",
            (bot_id, now, now + ttl, now, bot_id,
             str(group_id), bot_id, now),
        )
        conn.commit()
        conn.close()
        return result.rowcount > 0
    except Exception:
        return True  # 异常放行, 避免阻塞正常回复


def coordination_release_lightweight(
    db_path: str, group_id: str, bot_id: str,
) -> None:
    """释放发言权 (Luna 侧)。"""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE bot_coordination SET token_holder = '', token_expires_at = 0 "
            "WHERE group_id = ? AND token_holder = ?",
            (str(group_id), bot_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# §5 Brake 5: 能力边界 + 不替对方承诺
# ══════════════════════════════════════════════════════════════

CAPABILITY_BOUNDARY_PROMPT = (
    "【能力边界 — 不可违反】\n"
    "1. 你不能假装自己能影响现实、网络、游戏房间、他人设备或用户身体动作。\n"
    '2. 没有可用工具且没有实际执行结果时，不要承诺"我这就拉你/我帮你操作/我已经处理/我去修/我给你弄好"。\n'
    "3. 遇到拉人、开房间、修网、重启、登录、下载、现实代办等请求，只能自然说明自己做不到实际操作。\n"
    '4. 不要替群里的其他 bot 承诺任何事。不要说"她会帮你/他已经在处理/你去找他"——'
    '你无法知道对方 bot 的状态和能力。可以说"你可以@他问问"或"我不确定他那边的情况"。\n'
    "5. 不要断言其他 bot 是否在线/在群/会回复——你无法可靠地知道。"
)

# 标记字符串，用于检测是否已注入 (幂等)
CAPABILITY_BOUNDARY_MARKER = "<!-- dual_bot_capability_boundary_v1 -->"


def build_capability_boundary_injection(peer_bot_name: str = "") -> str:
    """构建能力边界注入 prompt 片段。

    Args:
        peer_bot_name: 对方 bot 的名字 (用于规则4的个性化)

    Returns:
        完整的注入文本 (含 marker)。
    """
    boundary = CAPABILITY_BOUNDARY_PROMPT
    if peer_bot_name:
        # 个性化规则 4
        boundary = boundary.replace(
            "(如 peer bot/主 bot)",
            f"(如{peer_bot_name})",
        )
    return f"{CAPABILITY_BOUNDARY_MARKER}\n{boundary}"


def is_capability_boundary_injected(prompt: str) -> bool:
    """检查 prompt 中是否已注入能力边界。"""
    return CAPABILITY_BOUNDARY_MARKER in prompt


# ══════════════════════════════════════════════════════════════
# §6 诊断/审计工具
# ══════════════════════════════════════════════════════════════

def diagnose_boundary(
    my_bot_id: str,
    sender_id: str | int,
    action: str,
) -> dict[str, Any]:
    """诊断一个边界情况 — 返回完整的判断依据。

    用于调试和日志。20 个边界用例的测试可调用此函数。
    """
    uid = str(sender_id)
    return {
        "sender_id": uid,
        "my_bot_id": str(my_bot_id),
        "action": action,
        "is_known_bot": is_known_bot(uid),
        "is_peer_bot": is_peer_bot(my_bot_id, uid),
        "is_self": is_self_bot(my_bot_id, uid),
        "is_human": is_human_message(uid),
        "should_suppress": should_suppress_as_bystander(my_bot_id, uid, action),
        "peer_qq": get_peer_qq(my_bot_id),
        "bot_name": get_bot_name(uid) if is_known_bot(uid) else "Human",
    }
