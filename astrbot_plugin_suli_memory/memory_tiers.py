"""三层记忆管理 — Daily (会话级) + Core (人格级) 蒸馏管道。

CowAgent 三层模型适配版:
  Layer 1 (context): 当前对话窗口 → 由 group_chat.py 上下文管理, 不在此模块
  Layer 2 (daily):   会话级事实 → 复用现有 UserMemoryStore (user_memory.py)
  Layer 3 (core):    长期人格特征 → 本模块 CoreMemoryStore

蒸馏触发: daily 条目 ≥ memory_core_distill_threshold → LLM 提炼 core facts
冷却: 同一用户每天最多蒸馏一次 (memory_core_distill_cooldown)

用法:
  from .memory_tiers import MemoryTierManager  # 用法示例 — 实际导入用 from ..context.memory_tiers import ...

  mgr = MemoryTierManager(config, memory_store, tavern)
  daily = mgr.get_daily_hints(ctx)        # 现有 daily 记忆注入
  core = mgr.get_core_hints(user_id)      # 新 core 特征注入
  await mgr.maybe_distill(user_id)        # 条件蒸馏 (daily → core)
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)

# ── 存储路径 ──────────────────────────────────────────────

def _get_core_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        _base = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_suli_memory"
    except ImportError:
        _base = Path("data/plugin_data/astrbot_plugin_suli_memory")
    _core = _base / "user_core"
    _core.mkdir(parents=True, exist_ok=True)
    return _core


_CORE_DIR = _get_core_dir()


def _core_path(bot_id: str, user_id: str) -> Path:
    """Core 记忆路径 — per-bot 隔离。"""
    return _get_core_dir() / bot_id / f"{user_id}.json"


# ── CoreMemoryStore ────────────────────────────────────────


class CoreMemoryStore:
    """Core 层记忆存储 — 永久人格特征, 无衰减, ≤30条。

    存储: data/user_core/{bot_id}/{user_id}.json
    格式:
      {
        "user_id": "xxx",
        "user_name": "xxx",
        "core_facts": [
          {
            "key": "core_xxx",
            "value": "游戏爱好者，对硬件配置很了解",
            "importance": 0.9,
            "created_at": 1234567890,
            "updated_at": 1234567890
          }
        ],
        "last_distilled_at": 1234567890
      }
    """

    def __init__(self, max_facts: int = 30, bot_id: str = "") -> None:
        self._max_facts = max_facts
        self._bot_id = bot_id

    def _resolve_path(self, user_id: str) -> Path:
        path = _core_path(self._bot_id, user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    # ── 加载 / 保存 ───────────────────────────────────

    def load(self, user_id: str) -> dict | None:
        """加载用户 core 记忆, 不存在则返回 None (含懒迁移)。"""
        try:
            path = self._resolve_path(user_id)
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("core 记忆加载失败: %s", user_id, exc_info=True)
            return None

    def save(self, user_id: str, user_name: str, core_facts: list[dict]) -> None:
        """保存 core 记忆到文件。自动裁剪到 max_facts。"""
        _get_core_dir().mkdir(parents=True, exist_ok=True)
        now = time.time()

        # 裁剪: 保留 importance 最高的
        if len(core_facts) > self._max_facts:
            core_facts = sorted(
                core_facts,
                key=lambda f: f.get("importance", 0.5),
                reverse=True,
            )[:self._max_facts]

        data = {
            "user_id": user_id,
            "user_name": user_name,
            "core_facts": core_facts,
            "last_distilled_at": now,
        }
        path = self._resolve_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── 蒸馏 ──────────────────────────────────────────

    async def distill(
        self,
        user_id: str,
        user_name: str,
        daily_facts: list[dict],
        tavern,  # duck-typed: .chat()
    ) -> list[dict]:
        """从 daily facts 蒸馏出 core 人格特征。

        调用 LLM 将零散事实提炼为持久的人格判断。
        返回合并后的 core_facts 列表 (已有 + 新增)。

        Args:
            user_id: 用户 ID
            user_name: 用户名称
            daily_facts: daily 层事实列表 (来自 UserMemoryStore)
            tavern: TavernClient, 用于 LLM 调用

        Returns:
            合并后的 core_facts 列表
        """
        existing = self.load(user_id)
        existing_facts: list[dict] = (
            existing.get("core_facts", []) if existing else []
        )
        existing_values = {f["value"] for f in existing_facts}

        now = time.time()

        # 构建 daily 事实文本
        daily_lines: list[str] = []
        for f in daily_facts[-20:]:  # 取最近的 20 条
            cat = f.get("category", "")
            val = f.get("value", "")
            if cat:
                daily_lines.append(f"[{cat}] {val}")
            else:
                daily_lines.append(val)

        if not daily_lines:
            logger.debug("无 daily 事实可蒸馏: %s", user_id)
            return existing_facts

        # 已有特征文本
        existing_lines: list[str] = []
        for f in existing_facts:
            existing_lines.append(f"- {f['value']}")

        # LLM 蒸馏
        distill_messages = [
            {
                "role": "system",
                "content": (
                    "你正在了解一个群友。以下是从多段对话中提取的关于TA的事实。\n\n"
                    "请从这些零散事实中提炼出关于这个人的深层特征——"
                    "人格特质、持久偏好、稳定身份、长期兴趣等。\n\n"
                    "规则:\n"
                    "- 每条特征是一个简洁的判断句 (≤20字)\n"
                    "- 只提炼有长期价值的特征，忽略一次性话题\n"
                    "- 不要把具体设备型号当作特征"
                    "（「使用RTX 4060」不是特征，"
                    "「PC游戏玩家，关注硬件」才是）\n"
                    "- 不要重复已有特征\n"
                    "- 没有值得提炼的新特征就回复「无」\n\n"
                    "输出格式: 每行一条特征。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"群友 {user_name} 的近期事实:\n"
                    + "\n".join(f"- {l}" for l in daily_lines)
                    + "\n\n"
                    + ("已有特征:\n" + "\n".join(existing_lines)
                       if existing_lines
                       else "（尚无已知特征）")
                ),
            },
        ]

        _bg_llm = {}
        try:
            from astrbot_plugin_suli_tavern.service.bot_config import get_config_service
            _bg_llm = get_config_service().resolve_background_llm(
                self._bot_id or "", "memory_distill",
            )
        except Exception:
            logger.debug("MemoryTierManager: resolve_background_llm 失败", exc_info=True)

        try:
            result = await asyncio.wait_for(
                tavern.chat(
                    distill_messages,
                    temperature=0.3,
                    max_tokens=256,
                    model=_bg_llm.get("model", "deepseek-v4-flash"),
                    api_base=_bg_llm.get("api_base", ""),
                    api_key=_bg_llm.get("api_key", ""),
                    extra_params=_bg_llm.get("extra_params"),
                ),
                timeout=45,
            )
            result = result.strip()
            if not result or result == "无":
                logger.debug("core 蒸馏无新特征: %s", user_id)
                return existing_facts

            # 解析: 每行一条特征
            new_facts: list[dict] = []
            for line in result.split("\n"):
                line = line.strip().lstrip("- ").lstrip("-")
                if not line or line == "无":
                    continue
                if len(line) < 3:
                    continue
                # 去重 (模糊: 已有值包含或新值包含已有)
                if any(
                    line in ev or ev in line
                    for ev in existing_values
                ):
                    continue
                existing_values.add(line)

                key = f"core_{now:.0f}_{len(new_facts)}"
                new_facts.append({
                    "key": key,
                    "value": line,
                    "importance": 0.8,
                    "created_at": now,
                    "updated_at": now,
                })

            if new_facts:
                logger.info(
                    "core 蒸馏完成: %s +%d 条 → 共 %d 条",
                    user_id,
                    len(new_facts),
                    len(existing_facts) + len(new_facts),
                )
            else:
                logger.debug("core 蒸馏解析后无有效特征: %s", user_id)

            return existing_facts + new_facts

        except TimeoutError:
            logger.warning("CoreMemoryStore.distill: LLM 调用超时 (45s) for user=%s", user_id[:8])
            return existing_facts
        except Exception:
            logger.exception("core 蒸馏失败: %s", user_id)
            return existing_facts

    # ── 注入 ──────────────────────────────────────────

    @staticmethod
    def _fact_context_overlap(fact: str, context: str) -> float:
        """计算单个 fact 与当前对话上下文的关键词重叠度 (0.0~1.0)。

        中文/混合文本使用字符 2-gram Jaccard 相似度。
        """
        if not context or not fact:
            return 0.0
        fact_l = fact.lower()
        ctx_l = context.lower()

        def _bigrams(s: str) -> set[str]:
            return {s[i:i + 2] for i in range(len(s) - 1)}

        f_bg = _bigrams(fact_l)
        c_bg = _bigrams(ctx_l)
        if not f_bg:
            return 0.0
        return len(f_bg & c_bg) / len(f_bg)

    def get_all_for_prompt(self, user_id: str, context: str = "") -> str:
        """获取用户 core 特征, 格式化为 prompt 注入文本。

        Args:
            user_id: 用户 ID
            context: 可选, 当前对话上下文。提供时按关键词重叠过滤 (min_overlap=0.08),
                     只注入与当前话题相关的 core facts。

        Returns:
            格式化的 core 特征, 无数据时返回空字符串
        """
        data = self.load(user_id)
        if not data:
            return ""

        facts: list[dict] = data.get("core_facts", [])
        if not facts:
            return ""

        # ── P2-5: 话题相关性过滤 ──
        if context:
            MIN_OVERLAP = 0.08
            filtered = [
                f for f in facts
                if self._fact_context_overlap(str(f.get("value", "")), context) >= MIN_OVERLAP
            ]
            if not filtered:
                return ""  # 所有 fact 都与当前话题无关, 全跳过
            facts = filtered

        name = data.get("user_name", user_id)
        lines = [f"关于{name}的深层了解:"]
        for f in facts:
            lines.append(f"- {f['value']}")

        return "\n".join(lines)

    def get_facts(self, user_id: str) -> list[dict]:
        """获取用户全部 core facts (原始数据)。"""
        data = self.load(user_id)
        if not data:
            return []
        return data.get("core_facts", [])


# ── MemoryTierManager ──────────────────────────────────────


class MemoryTierManager:
    """三层记忆编排器 — 统一管理 daily + core。

    持有 UserMemoryStore (daily) + CoreMemoryStore, 提供:
      - get_daily_hints(ctx) → str        委托给 UserMemoryStore
      - get_core_hints(user_id) → str     从 CoreMemoryStore
      - get_all_core_hints(ctx) → str     群聊中所有活跃用户 core 特征
      - maybe_distill(user_id) → None     条件蒸馏
    """

    def __init__(self, config: Config, memory_store, tavern, bot_id: str = "") -> None:
        """Args:
            config: Config 实例
            memory_store: UserMemoryStore 实例 (daily 层)
            tavern: TavernClient (duck-typed .chat())
            bot_id: bot 的 QQ 号 (per-bot 隔离)
        """
        self._config = config
        self._daily = memory_store
        self._tavern = tavern
        self._bot_id = bot_id
        self._core = CoreMemoryStore(
            max_facts=config.memory_core_max_facts,
            bot_id=bot_id,
        )
        # 蒸馏冷却: {f"{bot_id}:{user_id}": last_distill_timestamp}
        self._distill_cooldowns: dict[str, float] = {}

    @property
    def daily(self):
        """暴露 daily 层 UserMemoryStore 给外部直接调用。"""
        return self._daily

    @property
    def core(self) -> CoreMemoryStore:
        """暴露 core 层 CoreMemoryStore。"""
        return self._core

    # ── 记忆注入 (prompt builder 调用) ────────────────

    def get_daily_hints(self, ctx) -> str:
        """为群聊上下文中活跃用户注入 daily 记忆。

        委托给 UserMemoryStore.get_hints()。
        """
        if not self._config.user_memory_enabled:
            return ""
        return self._daily.get_hints(ctx)

    def get_daily_hints_for_user(self, user_id: str, query: str = "") -> str:
        """获取单个用户的 daily 记忆。"""
        if not self._config.user_memory_enabled:
            return ""
        return self._daily.get_hints_for_user(user_id, query)

    def get_core_hints(self, user_id: str, context: str = "") -> str:
        """获取单个用户的 core 长期特征, 格式化为 prompt 注入。

        Args:
            user_id: 用户 ID
            context: 可选, 当前对话上下文文本。提供时按话题相关性过滤 core facts,
                     避免注入与当前对话无关的长期特征。

        注入策略: core 条目少 (≤30), 无 context 时全量注入;
                 有 context 时按关键词重叠过滤, 只注入与当前话题相关的特征。
        """
        if not self._config.user_memory_enabled:
            return ""
        return self._core.get_all_for_prompt(user_id, context)

    def get_all_core_hints(self, ctx, context_text: str = "") -> str:
        """为群聊上下文中所有活跃用户注入 core 特征。

        遍历最近消息中的用户, 注入其 core 特征。
        最多返回 5 个用户的 core 特征, 避免 token 膨胀。

        Args:
            ctx: 群聊上下文
            context_text: 可选, 当前对话文本用于话题相关性过滤
        """
        if not self._config.user_memory_enabled:
            return ""

        seen_ids: set[str] = set()
        hints: list[str] = []

        for msg in reversed(ctx.messages[-20:]):
            uid = msg.get("user_id", "")
            if uid in seen_ids or uid.startswith("bot_"):
                continue
            seen_ids.add(uid)

            core_text = self._core.get_all_for_prompt(uid, context_text)
            if core_text:
                hints.append(core_text)

            if len(hints) >= 5:
                break

        return "\n\n".join(hints) if hints else ""

    # ── 蒸馏触发 ──────────────────────────────────────

    async def maybe_distill(self, user_id: str) -> bool:
        """条件蒸馏: daily 条目达标 + 冷却已过 → LLM 提炼 core。

        在 UserMemoryStore.extract_async() 完成后调用。

        Returns:
            True 表示执行了蒸馏, False 表示跳过
        """
        if not self._config.user_memory_enabled:
            return False

        # 冷却检查 (per-bot 键)
        now = time.time()
        _cooldown_key = f"{self._bot_id}:{user_id}" if self._bot_id else user_id
        last = self._distill_cooldowns.get(_cooldown_key, 0)
        cooldown = self._config.memory_core_distill_cooldown
        if now - last < cooldown:
            return False

        # 数量检查
        daily_data = self._daily.load(user_id)
        if not daily_data:
            return False
        daily_facts: list[dict] = daily_data.get("facts", [])
        if len(daily_facts) < self._config.memory_core_distill_threshold:
            return False

        self._distill_cooldowns[_cooldown_key] = now

        user_name = daily_data.get("user_name", user_id)

        # 执行蒸馏
        merged = await self._core.distill(
            user_id, user_name, daily_facts, self._tavern,
        )

        # 保存
        self._core.save(user_id, user_name, merged)

        # Token 用量记录 (蒸馏 LLM 调用)
        try:
            usage = self._tavern.get_last_usage()
            if usage.get("input_tokens") or usage.get("output_tokens"):
                from astrbot_plugin_suli_tavern.service.bot_db import get_bot_db
                get_bot_db().record_token_usage(
                    scenario="core_distill",
                    user_id=user_id,
                    provider="",
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cache_hit_tokens=usage.get("cache_hit_tokens", 0),
                    cache_miss_tokens=usage.get("cache_miss_tokens", 0),
                    latency_ms=usage.get("latency_ms", 0),
                )
        except Exception:
            pass

        return True


# ── 模块级单例 ────────────────────────────────────────────

_global_managers: dict[str, MemoryTierManager] = {}
_DEFAULT_KEY = ""  # 与 user_memory.py 一致的懒迁移键


def get_tier_manager(bot_id: str = "") -> MemoryTierManager | None:
    """获取 per-bot MemoryTierManager (未初始化返回 None)。

    Args:
        bot_id: bot 的 QQ 号。空字符串时返回 None (不再回退到任意 manager)。
    """
    if not bot_id:
        return None
    if bot_id in _global_managers:
        return _global_managers[bot_id]
    # 懒迁移: 默认 manager → bot_id (更新内部 bot_id)
    if _DEFAULT_KEY in _global_managers:
        mgr = _global_managers.pop(_DEFAULT_KEY)
        mgr._bot_id = bot_id
        mgr._core._bot_id = bot_id
        _global_managers[bot_id] = mgr
        logger.debug("MemoryTierManager 懒迁移: '' → %s", bot_id)
        return mgr
    return None


def init_tier_manager(config, memory_store, tavern, bot_id: str = "") -> MemoryTierManager:
    """初始化 per-bot MemoryTierManager。

    在 UserMemoryStore 和 TavernClient 就绪后调用。
    """
    _key = bot_id or _DEFAULT_KEY
    _global_managers[_key] = MemoryTierManager(config, memory_store, tavern, bot_id=bot_id)
    logger.info("MemoryTierManager per-bot 单例已初始化: bot=%s", bot_id or "(default)")
    return _global_managers[_key]
