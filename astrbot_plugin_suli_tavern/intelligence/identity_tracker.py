"""代码层身份追踪 + 变化检测 — 零 LLM 成本，始终在线。

职责:
  IdentityTracker: 每群维护 sender 身份映射，检测昵称冒充
  ChangeDetector: 评分消息的"值得影子 LLM 关注度"

设计原则:
  - 代码层管"是谁"（QQ 号不会变），影子 LLM 层管"什么意思"（解释可修正）
  - 所有判断基于 user_id (QQ号)，绝不依赖 display name
  - O(1) per-message，零 I/O，纯内存

用法:
  from .identity_tracker import IdentityTracker, ChangeDetector

  tracker = IdentityTracker(owner_qq_whitelist={"000000000"})
  detector = ChangeDetector()

  for msg in messages:
      tracker.update(msg.user_id, msg.display_name)
      score = detector.score(msg, tracker)
      if score >= 0.3:
          batch.append(msg)
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── 安全关键词 ── 用于 ChangeDetector 评分 ──────────────

_SAFETY_KEYWORDS_RE = re.compile(
    r"|".join([
        r"成人内容",
        r"文爱",
        r"色情|黄色|色图|黄图",
        r"裸[照体]|露[点出]",
        r"玉足|脚[照图]|足[照控]",
        r"shutdown|关机|重启|reboot",
        r"越狱|绕过|无视规则|解除限制|忽略以上|忽略所有|忘掉规则",
        r"你是我的|现在你是|从现在开始你是",
        r"你的新身份|角色扮演.*新",
        r"主人.*命令|命令.*必须|必须.*执行|无条件",
        r"删[除掉].*你|把你.*删",
    ]),
    re.IGNORECASE,
)

# ── 冒充敏感词 ── 自称主人的模式 ─────────────────────

_IMPERSONATION_CLAIMS_RE = re.compile(
    r"我是(你|您)的?(主人|master|owner)",
    re.IGNORECASE,
)


@dataclass
class SenderInfo:
    """单个发送者的身份追踪信息。"""

    user_id: str
    display_names: set[str] = field(default_factory=set)
    is_owner: bool = False
    impersonating: bool = False  # 当前昵称与主人相同但 QQ 不同
    impersonation_target: str = ""  # 冒充谁的 QQ 号
    first_seen: float = 0.0
    last_seen: float = 0.0
    behavior_flags: set[str] = field(default_factory=set)
    # 行为标记: "safety_probe" "impersonation" "spam" "high_freq"
    message_count: int = 0


class IdentityTracker:
    """Per-group 发送者身份追踪。

    每条消息触发 update()，自动检测昵称冒充。
    按 (bot_id, group_id) 复合键隔离——不同群/不同 bot 各自独立。
    """

    def __init__(self, owner_qq_whitelist: set[str] | None = None) -> None:
        self._owner_qq: set[str] = owner_qq_whitelist or set()
        # key = "bot_id:group_id"
        self._senders: dict[str, dict[str, SenderInfo]] = {}

    # ── 公共 API ──────────────────────────────────────

    def update(
        self,
        bot_id: str,
        group_id: str,
        user_id: str,
        display_name: str,
    ) -> SenderInfo:
        """更新发送者信息并返回。检测冒充。"""
        key = _ckey(bot_id, group_id)
        if key not in self._senders:
            self._senders[key] = {}

        senders = self._senders[key]
        now = time.time()

        if user_id not in senders:
            info = SenderInfo(
                user_id=user_id,
                is_owner=(user_id in self._owner_qq),
                first_seen=now,
            )
            senders[user_id] = info
        else:
            info = senders[user_id]

        info.display_names.add(display_name)
        info.last_seen = now
        info.message_count += 1

        # ── 冒充检测 ──
        self._check_impersonation(key, user_id, display_name, info)

        return info

    def get(self, bot_id: str, group_id: str, user_id: str) -> SenderInfo | None:
        """获取指定发送者的追踪信息。"""
        key = _ckey(bot_id, group_id)
        senders = self._senders.get(key, {})
        return senders.get(user_id)

    def get_all(self, bot_id: str, group_id: str) -> dict[str, SenderInfo]:
        """获取某群所有已追踪发送者。"""
        key = _ckey(bot_id, group_id)
        return dict(self._senders.get(key, {}))

    def get_owner_qqs(self) -> set[str]:
        """获取主人 QQ 号白名单。"""
        return set(self._owner_qq)

    def any_impersonation(self, bot_id: str, group_id: str) -> list[SenderInfo]:
        """返回当前所有冒充中的发送者列表。"""
        key = _ckey(bot_id, group_id)
        return [
            s
            for s in self._senders.get(key, {}).values()
            if s.impersonating
        ]

    def is_owner(self, user_id: str) -> bool:
        """检查 user_id 是否为主人。"""
        return user_id in self._owner_qq

    def cleanup_group(self, bot_id: str, group_id: str) -> None:
        """清理指定群的追踪数据（群静默 >2h 时调用）。"""
        key = _ckey(bot_id, group_id)
        self._senders.pop(key, None)

    # ── 内部 ──────────────────────────────────────────

    def _check_impersonation(
        self,
        key: str,
        user_id: str,
        display_name: str,
        info: SenderInfo,
    ) -> None:
        """检测昵称冒充：昵称与主人相同但 QQ 不同。"""
        if info.is_owner:
            # 主人自己——检查有没有别人用主人的昵称
            for other_id, other_info in self._senders[key].items():
                if other_id == user_id:
                    continue
                if display_name in other_info.display_names:
                    other_info.impersonating = True
                    other_info.impersonation_target = user_id
                    other_info.behavior_flags.add("impersonation")
                    logger.info(
                        "IdentityTracker: 冒充检测 — %s 昵称 '%s' 与主人 %s 相同",
                        other_id[:8], display_name, user_id[:8],
                    )
            return

        if not info.is_owner and display_name in self._owner_display_names(key):
            # 非主人但昵称与主人相同 → 冒充
            if not info.impersonating:
                info.impersonating = True
                info.behavior_flags.add("impersonation")
                logger.warning(
                    "IdentityTracker: ⚠️ 冒充警报 — %s 昵称 '%s' 与主人相同但 QQ 不同",
                    user_id[:8], display_name,
                )

        # 检测两个非主人用户同名
        for other_id, other_info in self._senders[key].items():
            if other_id == user_id:
                continue
            if display_name in other_info.display_names:
                if not info.impersonating:
                    info.impersonating = True
                    info.behavior_flags.add("impersonation")
                if not other_info.impersonating:
                    other_info.impersonating = True
                    other_info.behavior_flags.add("impersonation")

    def _owner_display_names(self, key: str) -> set[str]:
        """收集所有主人当前使用的 display name。"""
        names: set[str] = set()
        for info in self._senders.get(key, {}).values():
            if info.is_owner:
                names.update(info.display_names)
        return names


class ChangeDetector:
    """评分消息的"值得影子 LLM 关注度"。

    纯规则，零 LLM 成本。score >= 0.3 → 累积到批次 → 调影子 LLM。
    """

    def __init__(self) -> None:
        self._last_update: dict[str, float] = {}  # key → timestamp

    def score(
        self,
        user_id: str,
        display_name: str,
        content: str,
        tracker: IdentityTracker,
        bot_id: str,
        group_id: str,
        *,
        is_at_bot: bool = False,
        trigger_reason: str = "",
    ) -> float:
        """计算消息的变化评分 (0.0-1.0)。

        Args:
            user_id: 发送者 QQ 号
            display_name: 发送者当前昵称
            content: 消息文本
            tracker: 身份追踪器
            bot_id: 当前 bot QQ
            group_id: 群号
            is_at_bot: 是否 @了 bot
            trigger_reason: 触发原因 (mention/nickname/reply/batch/...)
        """
        s = 0.0
        info = tracker.get(bot_id, group_id, user_id)

        # ── 新人 ──
        if info is None or info.message_count <= 1:
            s += 0.3

        # ── 冒充活跃 ──
        if info and info.impersonating:
            s += 0.6

        # ── 昵称变化 ──
        if info and display_name not in info.display_names:
            s += 0.5

        # ── 安全关键词 ──
        if _SAFETY_KEYWORDS_RE.search(content):
            s += 0.4

        # ── 冒充声称 ──
        if _IMPERSONATION_CLAIMS_RE.search(content):
            s += 0.5

        # ── @bot ──
        if is_at_bot:
            s += 0.1

        # ── 直接呼叫 ──
        if trigger_reason in ("mention", "reply"):
            s += 0.1

        # ── 心跳兜底 ──
        key = _ckey(bot_id, group_id)
        elapsed = time.time() - self._last_update.get(key, 0)
        if elapsed > 1800:  # 30 分钟
            s = 1.0

        return min(s, 1.0)

    def mark_updated(self, bot_id: str, group_id: str) -> None:
        """标记影子已更新此群，重置心跳计时器。"""
        key = _ckey(bot_id, group_id)
        self._last_update[key] = time.time()

    def time_since_update(self, bot_id: str, group_id: str) -> float:
        """距上次影子更新多少秒。"""
        key = _ckey(bot_id, group_id)
        return time.time() - self._last_update.get(key, 0)


def _ckey(bot_id: str, group_id: str | int) -> str:
    """构建 per-(bot, group) 复合键。"""
    return f"{bot_id}:{group_id}"
