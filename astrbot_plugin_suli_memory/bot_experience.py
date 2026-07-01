"""Bot 自传体经历记忆 — 主语是「bot 自己经历过什么」，per-bot、跨群有效。

与现有三层记忆 (主语: 用户) 正交 —— 这里记录的是 bot 的自传:
  - 近期经历层 (recent): 原始事件颗粒，第一人称简述
  - 核心经历层 (core): 蒸馏后的长期自传片段，≤N 条，几乎不淘汰

设计:
  - 存储: data/bot_experiences/{bot_id}/recent.json + core.json + SQLite 双写
  - 提取: 异步 fire-and-forget，挂载在 context_lifecycle.extract_and_distill() 之后
  - 注入: prompt_builder / tavern_client / request_injection 三处并排挂载
  - 第一阶段: 核心层全量 + 近期层最近几条，不做相关性检索
  - 预留字段: group / valence_at_time / importance — 第一阶段写入但不参与逻辑

用法:
  from astrbot_plugin_suli_memory.bot_experience import (
      BotExperienceStore, init_experience_store, get_experience_store,
  )
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 存储根目录 ──────────────────────────────────────────────

def _get_experience_root() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        _base = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_suli_memory"
    except ImportError:
        _base = Path("data/plugin_data/astrbot_plugin_suli_memory")
    _exp = _base / "bot_experiences"
    _exp.mkdir(parents=True, exist_ok=True)
    return _exp


_EXPERIENCE_ROOT = _get_experience_root()

# ── 默认参数 ─────────────────────────────────────────────────

_DEFAULT_MAX_RECENT = 100       # 近期层最多保留条目
_DEFAULT_MAX_CORE = 20          # 核心层最多保留条目
_DEFAULT_EXTRACT_COOLDOWN = 1800  # 提取冷却 (秒), 30min
_DEFAULT_DISTILL_THRESHOLD = 30   # 近期层 ≥ 此数触发蒸馏
_DEFAULT_DISTILL_COOLDOWN = 86400  # 蒸馏冷却 (秒), 1天

# ── 模块级 per-bot 单例 ─────────────────────────────────────

_global_stores: dict[str, BotExperienceStore] = {}
_DEFAULT_KEY = ""  # 懒迁移键


# ── 提取 System Prompt ──────────────────────────────────────

_EXTRACT_SYSTEM = """你是 {bot_name} 的自我观察员。从最近一段对话中提取「{bot_name} 自己经历或注意到的、值得记住的事」。

[提取视角 — 关键]
- 主语是「我」({bot_name})，不是用户。
- 提取的是「我经历了什么」「我注意到了什么」「我对谁说/做了什么」。
- 不是提取用户的事实——那是用户记忆系统的事。

[应该提取的事]
- 我和某个群友进行了一段有意义的对话 (话题是什么、氛围如何)
- 我帮某人解决了某个问题
- 我被夸奖/被吐槽/被调戏了
- 我注意到群里的某种氛围或变化
- 我说了什么让自己印象深刻的话
- 群友之间的互动让我觉得有趣的 (作为旁观者)
- 我学到了什么新东西

[不应该提取的事]
- 纯粹的水群/刷屏/表情包大战 (无意义)
- 用户说了什么关于自己的事实 (那是用户记忆的事)
- 跟我不相关的群友闲聊
- 重复的日常打招呼

[格式]
每行一条经历，格式: "经历: <第一人称简述>"
例如: "经历: 我帮粟藜解决了 ComfyUI 显存溢出的问题，他挺感激的"
例如: "经历: 群里在讨论新显卡，我注意到大家对价格都很敏感"
没有值得记住的事就输出「无」。
最多提取 3 条。"""


# ── 蒸馏 System Prompt ──────────────────────────────────────

_DISTILL_SYSTEM = """你是 {bot_name} 的自传编辑。从近期的零散经历中提炼出关于「我是个怎样的存在」的长期自传片段。

[提炼原则]
- 从具体事件中抽象出持久的自我认知
- 「我经常和谁聊什么」「我在群里扮演什么角色」「我擅长什么」
- 「大家对我是什么态度」「我和这些群的关系基调是怎样的」
- 不是简单罗列事件——是提炼出「经历过这些之后，我是个怎样的人」

[格式]
每行一条自传片段，≤ 30 字，第一人称。
例如: "我常在群里帮人解决技术问题，大家挺信任我的"
例如: "我和粟藜经常聊 AI 绘画，他对我的建议很重视"
例如: "深夜的群聊氛围更安静，我也更愿意在这种时候说真心话"
没有值得提炼的就输出「无」。
最多提炼 5 条。"""


# ── BotExperienceStore ──────────────────────────────────────


class BotExperienceStore:
    """Bot 自传体经历记忆存储。

    管理近期经历层 (recent) 和核心经历层 (core) 的完整生命周期:
      - 加载/保存 (JSON + SQLite 双写)
      - 提取 (LLM 调用, 第一人称 bot 视角)
      - 蒸馏 (recent → core, 阈值触发)
      - 注入 (格式化为 prompt 文本)
    """

    def __init__(
        self,
        bot_id: str,
        *,
        max_recent: int = _DEFAULT_MAX_RECENT,
        max_core: int = _DEFAULT_MAX_CORE,
        extract_cooldown: int = _DEFAULT_EXTRACT_COOLDOWN,
        distill_threshold: int = _DEFAULT_DISTILL_THRESHOLD,
        distill_cooldown: int = _DEFAULT_DISTILL_COOLDOWN,
    ) -> None:
        self._bot_id = bot_id
        self._max_recent = max_recent
        self._max_core = max_core
        self._extract_cooldown = extract_cooldown
        self._distill_threshold = distill_threshold
        self._distill_cooldown = distill_cooldown

        # 冷却追踪
        self._last_extract_at: dict[str, float] = {}  # {group_id: timestamp}
        self._last_distill_at: float = 0.0

        # 内存缓存 (惰性加载)
        self._recent: list[dict] | None = None
        self._core: list[str] | None = None

        # 确保目录存在
        self._data_dir = _get_experience_root() / bot_id
        self._data_dir.mkdir(parents=True, exist_ok=True)

    # ── 路径 ─────────────────────────────────────────────

    @property
    def _recent_path(self) -> Path:
        return self._data_dir / "recent.json"

    @property
    def _core_path(self) -> Path:
        return self._data_dir / "core.json"

    @property
    def _archive_path(self) -> Path:
        return self._data_dir / "recent_archive.jsonl"

    # ── 加载 / 保存 ─────────────────────────────────────

    def _load_recent(self) -> list[dict]:
        """加载近期经历 (惰性，缓存)。"""
        if self._recent is not None:
            return self._recent
        try:
            if self._recent_path.exists():
                data = json.loads(self._recent_path.read_text(encoding="utf-8"))
                self._recent = data.get("entries", [])
            else:
                self._recent = []
        except Exception:
            logger.warning("bot_experience recent 加载失败: %s", self._bot_id, exc_info=True)
            self._recent = []
        return self._recent  # type: ignore[return-value]

    def _save_recent(self) -> None:
        """保存近期经历到 JSON 文件。

        容量超限时，最旧的条目被归档到 recent_archive.jsonl 而非静默丢弃。
        设计铁律: 蒸馏不可逆要谨慎——原始条目归档保留，蒸馏质量差可回溯。
        """
        if self._recent is None:
            return
        try:
            # 裁剪到 max_recent — 丢弃前先归档
            if len(self._recent) > self._max_recent:
                overflow = len(self._recent) - self._max_recent
                dropped = self._recent[:overflow]
                self._recent = self._recent[-self._max_recent:]
                self._archive_entries(dropped, reason="capacity")

            data = {
                "bot_id": self._bot_id,
                "entries": self._recent,
                "last_updated": time.time(),
            }
            self._recent_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("bot_experience recent 保存失败: %s", self._bot_id, exc_info=True)

    def _archive_entries(self, entries: list[dict], *, reason: str = "capacity") -> None:
        """将条目追加写入归档文件 (JSONL, 每行一条)。

        蒸馏不可逆防护: 原始条目蒸馏后不物理删除, 归档保留可回溯。
        """
        if not entries:
            return
        try:
            archive_stub = {
                "archived_at": time.time(),
                "archive_reason": reason,
                "bot_id": self._bot_id,
            }
            with self._archive_path.open("a", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps({**archive_stub, "entry": entry}, ensure_ascii=False) + "\n")
            logger.debug(
                "BotExperience: bot=%s 归档 %d 条旧近期条目 → %s",
                self._bot_id, len(entries), self._archive_path.name,
            )
        except Exception:
            logger.debug("bot_experience 归档写入失败: %s", self._bot_id, exc_info=True)

    def _load_core(self) -> list[str]:
        """加载核心经历 (惰性，缓存)。"""
        if self._core is not None:
            return self._core
        try:
            if self._core_path.exists():
                data = json.loads(self._core_path.read_text(encoding="utf-8"))
                self._core = data.get("core_fragments", [])
            else:
                self._core = []
        except Exception:
            logger.warning("bot_experience core 加载失败: %s", self._bot_id, exc_info=True)
            self._core = []
        return self._core  # type: ignore[return-value]

    def _save_core(self) -> None:
        """保存核心经历到 JSON 文件。"""
        if self._core is None:
            return
        try:
            if len(self._core) > self._max_core:
                self._core = self._core[-self._max_core:]

            data = {
                "bot_id": self._bot_id,
                "core_fragments": self._core,
                "last_distilled_at": self._last_distill_at,
                "last_updated": time.time(),
            }
            self._core_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("bot_experience core 保存失败: %s", self._bot_id, exc_info=True)

    # ── SQLite 持久化 ───────────────────────────────────

    def _save_to_sqlite(
        self,
        entry: dict,
        msg_start: int = 0,
        msg_end: int = 0,
    ) -> None:
        """双写一条经历到 SQLite。"""
        try:
            from astrbot_plugin_suli_tavern.service.bot_db import get_bot_db  # type: ignore[import-untyped]
            db = get_bot_db()
            db.conn.execute(
                "INSERT INTO bot_experiences "
                "(bot_id, event, ts, source_group, participants, valence_at_time, importance, "
                "message_range_start, message_range_end) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._bot_id,
                    entry.get("event", ""),
                    entry.get("ts", time.time()),
                    entry.get("group", 0),
                    json.dumps(entry.get("participants", [])),
                    entry.get("valence_at_time", 0.0),
                    entry.get("importance", 1.0),
                    msg_start,
                    msg_end,
                ),
            )
            db.conn.commit()
        except Exception:
            logger.debug("bot_experience SQLite 写入失败: %s", self._bot_id, exc_info=True)

    # ── 提取 ────────────────────────────────────────────

    async def extract(  # noqa: PLR0915
        self,
        tavern,  # duck-typed: .chat()
        ctx,      # GroupChatContext (duck-typed)
        *,
        bot_name: str = "",
        valence: float = 0.0,
    ) -> list[dict]:
        """从群聊上下文异步提取 bot 自身经历。

        Args:
            tavern: LLM 客户端 (duck-typed .chat())
            ctx: 群聊上下文
            bot_name: bot 的名字 (用于 prompt)
            valence: 当前全局 mood valence (预留，第二阶段启用)

        Returns:
            新提取的经历条目列表 (已写入 recent 层)
        """
        group_id = getattr(ctx, "group_id", 0)
        if not group_id:
            return []

        # ── 冷却检查 ──
        now = time.time()
        last = self._last_extract_at.get(str(group_id), 0)
        if now - last < self._extract_cooldown:
            logger.debug(
                "BotExperience: bot=%s 群 %s 提取冷却中 (%.0fs)",
                self._bot_id, group_id, now - last,
            )
            return []

        self._last_extract_at[str(group_id)] = now

        # ── 收集消息 ──
        messages = getattr(ctx, "messages", []) or []
        if not messages:
            return []

        recent_msgs = messages[-40:]  # 取最近 40 条
        user_msgs: list[str] = []
        msg_indices: list[int] = []

        for i, m in enumerate(recent_msgs):
            uid = str(m.get("user_id", ""))
            if uid.startswith("bot_"):
                # bot 自身的发言也保留 — 这是经历记忆，需要知道「我说了什么」
                content = str(m.get("content", "")).strip()
                if content:
                    name = str(m.get("user_name", uid))
                    if len(content) > 120:
                        content = content[:117] + "..."
                    user_msgs.append(f"{name} (我): {content}")
                    msg_indices.append(i)
            else:
                content = str(m.get("content", "")).strip()
                if not content:
                    continue
                if len(content) > 120:
                    content = content[:117] + "..."
                name = str(m.get("user_name", uid))
                user_msgs.append(f"{name}: {content}")
                msg_indices.append(i)

        if len(user_msgs) < 3:
            return []

        # ── 构建 prompt ──
        display_name = bot_name or "我"
        system_prompt = _EXTRACT_SYSTEM.format(bot_name=display_name)

        prompt = (
            f"--- 最近 {len(user_msgs)} 条群聊消息 ---\n"
            + "\n".join(user_msgs)
            + f"\n\n请提取 {display_name} 在这段对话中经历或注意到的事。"
        )

        # ── LLM 调用 (timeout=30s) ──
        _bot_id = self._bot_id or ""
        _bg_llm = {}
        try:
            from astrbot_plugin_suli_tavern.service.bot_config import get_config_service
            _bg_llm = get_config_service().resolve_background_llm(_bot_id, "experience_extract")
        except Exception:
            logger.debug("BotExperience: resolve_background_llm 失败", exc_info=True)

        try:
            raw = await asyncio.wait_for(
                tavern.chat(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                    max_tokens=200,
                    model=_bg_llm.get("model", "deepseek-v4-flash"),
                    api_base=_bg_llm.get("api_base", ""),
                    api_key=_bg_llm.get("api_key", ""),
                    extra_params=_bg_llm.get("extra_params"),
                ),
                timeout=30,
            )
        except TimeoutError:
            logger.warning("BotExperience.extract: bot=%s LLM 调用超时 (30s)", self._bot_id)
            return []
        except Exception:
            logger.debug("BotExperience: bot=%s 提取 LLM 调用失败", self._bot_id, exc_info=True)
            return []

        if not raw or raw.strip() == "无":
            return []

        # ── 解析 ──
        new_entries: list[dict] = []
        msg_start = len(messages) - len(recent_msgs)
        msg_end = len(messages) - 1

        for raw_line in raw.strip().split("\n"):
            line = raw_line.strip()
            # 支持 "经历: xxx" 或 "- xxx" 或 纯文本
            for prefix in ("经历:", "经历：", "- ", "-"):
                if line.startswith(prefix):
                    line = line[len(prefix):].strip()
                    break
            if not line or len(line) < 5:
                continue
            if line == "无":
                continue

            # 收集参与者 (从消息中提取 user_id)
            participants: list[str] = []
            for m in recent_msgs:
                uid = str(m.get("user_id", ""))
                if uid and not uid.startswith("bot_") and uid not in participants:
                    participants.append(uid)

            entry = {
                "event": line,
                "ts": now,
                "group": group_id,
                "participants": participants[:10],  # 最多 10 人
                "valence_at_time": valence,
                "importance": 1.0,
            }
            new_entries.append(entry)

        if not new_entries:
            return []

        # ── 写入 recent 层 ──
        recent = self._load_recent()
        recent.extend(new_entries)
        self._save_recent()

        # ── SQLite 双写 ──
        for entry in new_entries:
            self._save_to_sqlite(entry, msg_start, msg_end)

        logger.info(
            "BotExperience: bot=%s 群 %s 提取 %d 条经历",
            self._bot_id, group_id, len(new_entries),
        )

        return new_entries

    # ── 蒸馏 ────────────────────────────────────────────

    async def maybe_distill(self, tavern, *, bot_name: str = "") -> bool:
        """条件蒸馏: recent ≥ 阈值 + 冷却已过 → LLM 提炼 core。

        Returns:
            True 表示执行了蒸馏, False 表示跳过
        """
        now = time.time()

        # 冷却检查
        if now - self._last_distill_at < self._distill_cooldown:
            return False

        # 数量检查
        recent = self._load_recent()
        if len(recent) < self._distill_threshold:
            return False

        self._last_distill_at = now

        # ── 构建 prompt ──
        display_name = bot_name or "我"
        recent_lines = [f"- {e['event']}" for e in recent[-50:]]

        existing_core = self._load_core()
        existing_text = "\n".join(f"- {c}" for c in existing_core) if existing_core else "（尚无核心经历）"

        system_prompt = _DISTILL_SYSTEM.format(bot_name=display_name)

        # ── LLM 调用 (timeout=45s) ──
        _bot_id = self._bot_id or ""
        _bg_llm = {}
        try:
            from astrbot_plugin_suli_tavern.service.bot_config import get_config_service
            _bg_llm = get_config_service().resolve_background_llm(_bot_id, "experience_distill")
        except Exception:
            logger.debug("BotExperience: resolve_background_llm 失败 (distill)", exc_info=True)

        try:
            raw = await asyncio.wait_for(
                tavern.chat(
                    [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": (
                                f"近期经历 ({len(recent_lines)} 条):\n"
                                + "\n".join(recent_lines)
                                + f"\n\n已有自传片段:\n{existing_text}"
                                + "\n\n请提炼新的自传片段。没有值得提炼的就输出「无」。"
                            ),
                        },
                    ],
                    temperature=0.3,
                    max_tokens=256,
                    model=_bg_llm.get("model", "deepseek-v4-flash"),
                    api_base=_bg_llm.get("api_base", ""),
                    api_key=_bg_llm.get("api_key", ""),
                    extra_params=_bg_llm.get("extra_params"),
                ),
                timeout=45,
            )
        except TimeoutError:
            logger.warning("BotExperience.maybe_distill: bot=%s LLM 调用超时 (45s)", self._bot_id)
            return False
        except Exception:
            logger.debug("BotExperience: bot=%s 蒸馏 LLM 调用失败", self._bot_id, exc_info=True)
            return False

        if not raw or raw.strip() == "无":
            logger.debug("BotExperience: bot=%s 蒸馏无新片段", self._bot_id)
            self._save_core()  # 更新时间戳
            return False

        # ── 解析 ──
        new_fragments: list[str] = []
        for raw_line in raw.strip().split("\n"):
            cleaned = raw_line.strip().lstrip("- ").lstrip("-")
            if not cleaned or len(cleaned) < 5:
                continue
            if cleaned == "无":
                continue
            # 去重
            if any(cleaned in existing or existing in cleaned for existing in existing_core):
                continue
            new_fragments.append(cleaned)

        if new_fragments:
            self._core = existing_core + new_fragments
            self._save_core()
            logger.info(
                "BotExperience: bot=%s 蒸馏完成 +%d 条 → 共 %d 条核心经历",
                self._bot_id, len(new_fragments), len(self._core),
            )
        else:
            self._save_core()

        return True

    # ── 注入 ────────────────────────────────────────────

    def get_experience_hints(self, *, max_recent: int = 5, max_tokens: int = 0) -> str:
        """获取格式化的经历记忆提示文本。

        第一阶段策略: 核心层全量 + 近期层最近 N 条。

        Args:
            max_recent: 近期层最多注入条数
            max_tokens: token 预算上限 (0=不限制)。超限时按优先级截断:
                        核心层 (最重要 → 从旧到新丢弃) > 近期层 (从旧到新丢弃)

        Returns:
            格式化的提示文本, 无数据时返回空字符串
        """
        def _est_tokens(text: str) -> int:
            """保守 token 估算: 中英文混合文本, 每字符 ≈ 0.5 token。"""
            return max(1, len(text) // 2)

        parts: list[str] = []
        budget = max_tokens if max_tokens > 0 else float("inf")  # type: ignore[assignment]

        # 核心层 — 全量 (最重要的长期自传, 从旧到新迭代以优先丢弃旧条目)
        core = self._load_core()
        if core:
            header = "[我的经历 — 长期自传片段]"
            acc = header
            kept = 0
            for c in core:
                candidate = f"{acc}\n- {c}"
                if _est_tokens(candidate) > budget:
                    break
                acc = candidate
                kept += 1
            if kept:
                parts.append(acc)
                budget -= _est_tokens(acc)

        # 近期层 — 最近 N 条, 从新到旧 (新的更相关)
        recent = self._load_recent()
        if recent and budget > 0:
            header = "[我最近的经历]"
            acc = header
            kept = 0
            for e in reversed(recent[-max_recent:]):
                candidate = f"{acc}\n- {e['event']}"
                if _est_tokens(candidate) > budget:
                    break
                acc = candidate
                kept += 1
            if kept:
                parts.append(acc)

        return "\n\n".join(parts) if parts else ""

    def get_recent_count(self) -> int:
        """返回近期层条目数 (用于统计)。"""
        return len(self._load_recent())

    def get_core_count(self) -> int:
        """返回核心层条目数 (用于统计)。"""
        return len(self._load_core())


# ── 模块级单例管理 ──────────────────────────────────────────


def get_experience_store(bot_id: str = "") -> BotExperienceStore | None:
    """获取 per-bot BotExperienceStore (未初始化返回 None)。

    fail-closed: bot_id 为空或找不到对应 store → 返回 None。
    不再回退到"任意已初始化的 store"——那是跨 bot 污染的根源。
    """
    if not bot_id:
        return None
    if bot_id in _global_stores:
        return _global_stores[bot_id]
    # 懒迁移: 旧空字符串 store → bot_id (仅首次, 迁移后重建 _data_dir)
    if _DEFAULT_KEY in _global_stores:
        store = _global_stores.pop(_DEFAULT_KEY)
        store._bot_id = bot_id
        store._data_dir = _EXPERIENCE_ROOT / bot_id
        store._data_dir.mkdir(parents=True, exist_ok=True)
        _global_stores[bot_id] = store
        logger.info("BotExperienceStore 懒迁移: '' → %s (已重建路径)", bot_id)
        return store
    return None


def init_experience_store(
    bot_id: str = "",
    *,
    max_recent: int = _DEFAULT_MAX_RECENT,
    max_core: int = _DEFAULT_MAX_CORE,
    extract_cooldown: int = _DEFAULT_EXTRACT_COOLDOWN,
    distill_threshold: int = _DEFAULT_DISTILL_THRESHOLD,
    distill_cooldown: int = _DEFAULT_DISTILL_COOLDOWN,
) -> BotExperienceStore:
    """初始化 per-bot BotExperienceStore。

    在群聊调度器初始化时调用 (每个 bot 调用一次)。
    """
    _key = bot_id or _DEFAULT_KEY
    _global_stores[_key] = BotExperienceStore(
        bot_id=_key,
        max_recent=max_recent,
        max_core=max_core,
        extract_cooldown=extract_cooldown,
        distill_threshold=distill_threshold,
        distill_cooldown=distill_cooldown,
    )
    logger.info("BotExperienceStore per-bot 单例已初始化: bot=%s", bot_id or "(default)")
    return _global_stores[_key]
