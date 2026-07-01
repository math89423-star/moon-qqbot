"""用户记忆存储 — 结构化 JSON 持久化 + 关键词检索 + 衰减。

从 GroupChatScheduler 中提取的独立模块，负责:
  - 记忆的加载/保存/搜索/衰减/提取
  - 类别自动推断
  - 旧格式自动迁移

用法:
  from .user_memory import UserMemoryStore

  store = UserMemoryStore(config, tavern)
  hints = store.get_hints(ctx)          # 获取当前上下文相关记忆
  asyncio.create_task(store.extract_async(ctx, uid))  # 异步提取新记忆
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

import re as _re


def _tokenize(text: str) -> set[str]:
    """中文+英文多粒度分词。优先使用 suli_services，不可用时回退内置实现。"""
    try:
        from astrbot_plugin_suli_services.knowledge_base import tokenize as _ext_tokenize  # type: ignore[assignment]
        return _ext_tokenize(text)
    except ImportError:
        pass
    # 内置回退: 中文 bigram + 英文词
    tokens: set[str] = set()
    text_lower = text.lower()
    for m in _re.finditer(r"[a-z][a-z0-9_]+", text_lower):
        tokens.add(m.group())
    for i in range(len(text) - 1):
        if not (text[i].isspace() or text[i + 1].isspace()):
            tokens.add(text[i:i + 2])
    return tokens

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)

# 类别推断关键词表
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "设备": ["显卡", "gpu", "cpu", "显存", "vram", "内存",
             "4060", "4070", "4080", "4090", "3090", "3080",
             "设备", "电脑", "配置", "硬盘", "ssd", "主板", "电源"],
    "兴趣": ["喜欢", "爱好", "玩", "拍照", "摄影", "画图",
             "游戏", "cos", "看番", "追番", "听歌", "看电影"],
    "偏好": ["最爱", "偏好", "推", "厨", "本命", "老婆", "老公", "推し"],
    "经历": ["做过", "去过", "学过", "工作了", "毕业", "转行", "入职", "辞职"],
    "技能": ["会", "能", "擅长", "精通", "搞", "写代码", "编程", "画", "调参"],
    "身份": ["大学", "研究生", "博士", "专业", "学生", "学校", "学历", "在读"],
}

# 各品类初始重要性 (imp_map)
_INITIAL_IMPORTANCE: dict[str, float] = {
    "设备": 0.9, "身份": 0.8, "技能": 0.7,
    "偏好": 0.6, "兴趣": 0.5, "经历": 0.5,
    "风险": 0.95,  # 恶意调教记录 — 最高重要性，不会被淘汰
    "其他": 0.5,
}

# 有效类别集合
_VALID_CATEGORIES = frozenset(_CATEGORY_KEYWORDS.keys()) | {"其他", "风险"}


def _infer_category(value: str) -> str:
    """根据记忆内容自动推断类别。"""
    v = value.lower()
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in v for kw in keywords):
            return cat
    return "其他"


class UserMemoryStore:
    """用户记忆的完整生命周期管理。

    被 GroupChatScheduler 持有，通过公开方法交互:
      - get_hints(ctx) → str        注入 system prompt
      - extract_async(ctx, uid)    异步提取 + 持久化
    """

    def __init__(self, config: Config, tavern, bot_id: str = "") -> None:
        """tavern 参数 duck-typed: 需要 .chat() 方法用于记忆提取 LLM 调用。"""
        self._config = config
        self._tavern = tavern
        self._bot_id = bot_id
        # 提取冷却: {f"{bot_id}:{user_id}": last_extract_timestamp}
        self._extract_cooldowns: dict[str, float] = {}

    # ── 路径 ──────────────────────────────────────────

    _DATA_DIR: Path | None = None

    @classmethod
    def _data_dir(cls) -> Path:
        if cls._DATA_DIR is not None:
            return cls._DATA_DIR
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
            cls._DATA_DIR = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_suli_memory"
        except ImportError:
            cls._DATA_DIR = Path("data/plugin_data/astrbot_plugin_suli_memory")
        cls._DATA_DIR.mkdir(parents=True, exist_ok=True)
        return cls._DATA_DIR

    @staticmethod
    def _memory_path(bot_id: str, user_id: str) -> Path:
        return UserMemoryStore._data_dir() / "user_memories" / bot_id / f"{user_id}.json"

    def _resolve_path(self, user_id: str) -> Path:
        path = self._memory_path(self._bot_id, user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    # ── 加载 / 衰减 ───────────────────────────────────

    def load(self, user_id: str) -> dict | None:
        """加载用户记忆 (自动迁移旧格式 + 惰性衰减 + 懒迁移旧共享路径)。"""
        if not self._config.user_memory_enabled:
            return None
        try:
            path = self._resolve_path(user_id)
            if not path.exists():
                return None
            mem = json.loads(path.read_text(encoding="utf-8"))
            facts = mem.get("facts", [])

            # 自动迁移: 旧格式 ["string", ...] → 新格式 [{"key": ..., ...}]
            if facts and isinstance(facts[0], str):
                now = time.time()
                mem["facts"] = [
                    {
                        "key": f"fact_{i}",
                        "value": f,
                        "category": "其他",
                        "importance": 0.5,
                        "created_at": now,
                        "last_accessed": now,
                    }
                    for i, f in enumerate(facts)
                ]
                path.write_text(
                    json.dumps(mem, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info("用户记忆已从旧格式迁移: %s", user_id)

            # 惰性衰减
            mem = self._decay_inplace(mem)
            return mem
        except Exception:
            logger.warning("用户记忆加载失败: %s", user_id, exc_info=True)
            return None

    def _decay_inplace(self, mem: dict) -> dict:
        """惰性衰减: 降低旧记忆的 importance，清除过弱条目。

        半衰期公式: importance *= 0.5 ^ (age / half_life)
        """
        facts: list[dict] = mem.get("facts", [])
        if not facts:
            return mem

        now = time.time()
        half_life = self._config.user_memory_decay_half_life
        limit = self._config.user_memory_max_facts
        min_importance = 0.05

        for f in facts:
            age = now - f.get("created_at", now)
            if age > 0:
                f["importance"] = f.get("importance", 0.5) * (
                    0.5 ** (age / half_life)
                )

        # 清除过弱记忆
        mem["facts"] = [
            f for f in facts
            if f.get("importance", 0.5) > min_importance
        ]

        # 容量限制: 保留 importance 最高的
        if len(mem["facts"]) > limit:
            mem["facts"].sort(
                key=lambda f: f.get("importance", 0.5),
                reverse=True,
            )
            mem["facts"] = mem["facts"][:limit]

        return mem

    # ── 检索 ──────────────────────────────────────────

    def search(
        self, user_id: str, query: str, top_n: int = 5,
    ) -> list[dict]:
        """从用户记忆中检索与 query 相关的条目。

        对每条记忆 value 分词，计算与 query tokens 的交集大小，
        乘以 importance 作为最终得分，取 top_n。
        """
        mem = self.load(user_id)
        if not mem:
            return []

        facts: list[dict] = mem.get("facts", [])
        if not facts:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return facts[-top_n:]

        scored: list[tuple[float, dict]] = []
        now = time.time()
        for f in facts:
            value = f.get("value", "")
            value_tokens = _tokenize(value)
            overlap = len(query_tokens & value_tokens)
            if overlap > 0:
                importance = f.get("importance", 0.5)
                score = overlap * importance
                scored.append((score, f))
                f["last_accessed"] = now

        scored.sort(key=lambda x: x[0], reverse=True)
        result = [f for _, f in scored[:top_n]]

        # 无匹配时返回最近访问的记忆
        if not result:
            recent = sorted(
                facts,
                key=lambda f: f.get("last_accessed", f.get("created_at", 0)),
                reverse=True,
            )
            result = recent[:top_n]

        return result

    # ── 注入 prompt ───────────────────────────────────

    def get_hints(self, ctx) -> str:
        """为当前上下文中活跃用户注入相关记忆。

        从最近群聊消息提取关键词 → 逐用户做记忆检索 → 只注入相关的。
        """
        if not self._config.user_memory_enabled:
            return ""

        # 收集最近对话文本作为搜索 query
        recent_texts = []
        for msg in ctx.messages[-12:]:
            content = msg.get("content", "")
            if content:
                recent_texts.append(content[:200])
        search_query = " ".join(recent_texts)
        if not search_query.strip():
            return ""

        seen_ids: set[str] = set()
        hints: list[str] = []
        top_n = self._config.user_memory_search_top_n

        for msg in reversed(ctx.messages):
            uid = msg.get("user_id", "")
            if uid in seen_ids or uid.startswith("bot_"):
                continue
            seen_ids.add(uid)

            related = self.search(uid, search_query, top_n=top_n)
            if related:
                mem = self.load(uid) or {}
                name = mem.get("user_name", uid)
                parts = []
                for f in related:
                    cat = f.get("category", "")
                    val = f.get("value", "")
                    if cat:
                        parts.append(f"[{cat}] {val}")
                    else:
                        parts.append(val)
                hints.append(f"{name}: {' | '.join(parts)}")

            if len(hints) >= 3:
                break

        return "\n".join(hints) if hints else ""

    # ── 异步提取 ──────────────────────────────────────

    async def extract_async(
        self, ctx, trigger_user_id: str,
    ) -> None:
        """从对话中提取值得记住的用户信息 (异步, 不阻塞回复)。

        新版输出结构化条目: {key, value, category, importance, ...}
        """
        if not self._config.user_memory_enabled:
            return

        # 冷却检查 (per-bot 键)
        now = time.time()
        _cooldown_key = f"{self._bot_id}:{trigger_user_id}" if self._bot_id else trigger_user_id
        last = self._extract_cooldowns.get(_cooldown_key, 0)
        if now - last < self._config.user_memory_extract_cooldown:
            return

        self._extract_cooldowns[_cooldown_key] = now

        # 取该用户最近的消息
        user_msgs = [
            m for m in ctx.messages[-20:]
            if m["user_id"] == trigger_user_id
        ]
        if not user_msgs:
            return

        text = "\n".join(
            f"{m['user_name']}: {m['content'][:100]}" for m in user_msgs[-5:]
        )

        # 加载已有记忆用于去重
        existing_mem = self.load(trigger_user_id) or {}
        existing_values = {
            f["value"] for f in existing_mem.get("facts", [])
            if isinstance(f, dict)
        }

        extract_messages = [
            {
                "role": "system",
                "content": (
                    "从聊天消息中提取关于该用户值得记住的信息。\n"
                    "输出格式: 每行一条，格式为「类别:信息内容」"
                    "(如「设备:使用RTX 4060 8G」)。\n"
                    "类别选: 设备/兴趣/偏好/经历/技能/身份/其他。\n"
                    "只记有长期价值的信息(硬件配置/职业/爱好/偏好)，"
                    "不记聊天情绪或一次性话题。没有值得记的就回复「无」。"
                ),
            },
            {
                "role": "user",
                "content": f"用户消息:\n{text}",
            },
        ]

        # 优先从 BotConfigService 读取 (支持 Web 热修改)
        try:
            from astrbot_plugin_suli_tavern.service.bot_config import get_config_service
            mem_temp = get_config_service().get_temperature("memory_extract")
        except Exception:
            mem_temp = 0.2

        try:
            from astrbot_plugin_suli_tavern.service.bot_config import get_config_service
            active_cfg = get_config_service().resolve_active_llm()
            provider = active_cfg.provider if active_cfg else ""
            _api_base = active_cfg.normalized_base_url if active_cfg else ""
            _api_key = active_cfg.api_key if active_cfg else ""
        except Exception:
            logger.warning("extract_async: 无法解析 LLM 凭证, 记忆提取将跳过", exc_info=True)
            provider = ""
            _api_base = ""
            _api_key = ""

        try:
            result = await asyncio.wait_for(
                self._tavern.chat(
                    extract_messages,
                    temperature=mem_temp,
                    max_tokens=128,
                    provider=provider,
                    api_base=_api_base,
                    api_key=_api_key,
                ),
                timeout=30,
            )
            result = result.strip()
            if not result or result == "无":
                return

            # Token 用量记录
            try:
                usage = self._tavern.get_last_usage()
                if usage.get("input_tokens") or usage.get("output_tokens"):
                    from astrbot_plugin_suli_tavern.service.bot_db import get_bot_db
                    get_bot_db().record_token_usage(
                        scenario="memory_extract",
                        user_id=trigger_user_id,
                        provider=provider,
                        input_tokens=usage.get("input_tokens", 0),
                        output_tokens=usage.get("output_tokens", 0),
                        cache_hit_tokens=usage.get("cache_hit_tokens", 0),
                        cache_miss_tokens=usage.get("cache_miss_tokens", 0),
                        latency_ms=usage.get("latency_ms", 0),
                    )
            except Exception:
                pass

            # 解析 "类别:内容" 或 "内容" 格式
            new_facts: list[dict] = []
            for line in result.split("\n"):
                line = line.strip().lstrip("- ").lstrip("-")
                if not line or line == "无":
                    continue

                cat: str = "其他"
                value = line
                if ":" in line and len(line.split(":", 1)) == 2:
                    part_cat, part_val = line.split(":", 1)
                    part_cat = part_cat.strip()
                    part_val = part_val.strip()
                    if part_val and len(part_cat) <= 4:
                        cat = (
                            part_cat if part_cat in _VALID_CATEGORIES
                            else _infer_category(part_val)
                        )
                        value = part_val
                else:
                    cat = _infer_category(value)

                if not value:
                    continue

                # 去重
                if value in existing_values:
                    continue
                existing_values.add(value)

                # 生成 key
                key_base = value[:6].replace(" ", "_")
                key = f"{key_base}_{now:.0f}"

                importance = _INITIAL_IMPORTANCE.get(cat, 0.5)

                new_facts.append({
                    "key": key,
                    "value": value,
                    "category": cat,
                    "importance": importance,
                    "created_at": now,
                    "last_accessed": now,
                })

            if not new_facts:
                return

            path = self._resolve_path(trigger_user_id)
            path.parent.mkdir(parents=True, exist_ok=True)

            existing_mem.setdefault("user_id", trigger_user_id)
            existing_mem.setdefault("user_name", user_msgs[0]["user_name"])
            existing_mem.setdefault("facts", [])
            existing_mem["facts"].extend(new_facts)

            # 限制总条数 (保留 importance 最高的)
            max_facts = self._config.user_memory_max_facts
            if len(existing_mem["facts"]) > max_facts:
                existing_mem["facts"].sort(
                    key=lambda f: (
                        f.get("importance", 0.5)
                        if isinstance(f, dict) else 0.5
                    ),
                    reverse=True,
                )
                existing_mem["facts"] = existing_mem["facts"][:max_facts]

            existing_mem["last_seen"] = now
            path.write_text(
                json.dumps(existing_mem, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # 同步到 SQLite (管理面板查询)
            self._sync_extract_to_db(
                trigger_user_id,
                existing_mem.get("user_name", ""),
                new_facts,
            )
            logger.info(
                "用户记忆更新: %s (+%d 条, 类别: %s)",
                trigger_user_id,
                len(new_facts),
                ", ".join(
                    sorted(
                        {f["category"]
                         for f in new_facts if isinstance(f, dict)},
                    ),
                ),
            )

        except TimeoutError:
            logger.warning("UserMemoryStore.extract_async: LLM 调用超时 (30s)")
            return
        except Exception:
            logger.exception("用户记忆提取失败: %s", trigger_user_id)

    # ── 显式记忆 (工具调用) ───────────────────────────

    async def remember(
        self, user_id: str, user_name: str, fact_value: str,
        category: str = "",
    ) -> bool:
        """显式保存一条用户记忆 — 由 remember_memory 工具调用。

        写入 JSON 文件并同步到 SQLite。
        返回 True 表示新增，False 表示去重跳过。
        """
        if not self._config.user_memory_enabled:
            return False

        if not category or category not in _VALID_CATEGORIES:
            category = _infer_category(fact_value)

        existing_mem = self.load(user_id) or {}
        existing_values = {
            f["value"] for f in existing_mem.get("facts", [])
            if isinstance(f, dict)
        }

        # 去重
        if fact_value in existing_values:
            logger.debug("记忆去重: %s → %s", user_id, fact_value[:40])
            return False

        now = time.time()
        key_base = fact_value[:6].replace(" ", "_")
        key = f"remember_{key_base}_{now:.0f}"

        new_fact = {
            "key": key,
            "value": fact_value,
            "category": category,
            "importance": _INITIAL_IMPORTANCE.get(category, 0.5),
            "created_at": now,
            "last_accessed": now,
        }

        path = self._resolve_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing_mem.setdefault("user_id", user_id)
        existing_mem.setdefault("user_name", user_name)
        existing_mem.setdefault("facts", [])
        existing_mem["facts"].append(new_fact)
        existing_mem["last_seen"] = now

        # 容量限制
        max_facts = self._config.user_memory_max_facts
        if len(existing_mem["facts"]) > max_facts:
            existing_mem["facts"].sort(
                key=lambda f: (
                    f.get("importance", 0.5)
                    if isinstance(f, dict) else 0.5
                ),
                reverse=True,
            )
            existing_mem["facts"] = existing_mem["facts"][:max_facts]

        path.write_text(
            json.dumps(existing_mem, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 同步到 SQLite
        self._sync_fact_to_db(user_id, user_name, new_fact)

        logger.info(
            "显式记忆保存: %s → [%s] %s",
            user_id, category, fact_value[:40],
        )
        return True

    def get_hints_for_user(
        self, user_id: str, query: str = "", top_n: int = 5,
    ) -> str:
        """获取单个用户的相关记忆 (用于私聊注入)。

        Args:
            user_id: 用户 ID
            query: 搜索查询 (空则返回最近记忆)
            top_n: 返回条数

        Returns:
            格式化的记忆提示文本，无记忆时返回空字符串
        """
        if not self._config.user_memory_enabled:
            return ""

        if query.strip():
            related = self.search(user_id, query, top_n=top_n)
        else:
            mem = self.load(user_id)
            if not mem:
                return ""
            facts = mem.get("facts", [])
            if not facts:
                return ""
            recent = sorted(
                facts,
                key=lambda f: f.get("last_accessed", f.get("created_at", 0)),
                reverse=True,
            )
            related = recent[:top_n]

        if not related:
            return ""

        mem = self.load(user_id) or {}
        name = mem.get("user_name", user_id)
        parts = []
        for f in related:
            cat = f.get("category", "")
            val = f.get("value", "")
            if cat:
                parts.append(f"[{cat}] {val}")
            else:
                parts.append(val)
        return f"关于{name}的记忆: {' | '.join(parts)}"

    def get_user_name(self, user_id: str) -> str:
        """获取用户名称。"""
        mem = self.load(user_id)
        if mem:
            return mem.get("user_name", "")
        return ""

    def count_memories(self, user_id: str) -> int:
        """获取用户的记忆总数 (用于情感基线联动)。"""
        mem = self.load(user_id)
        if not mem:
            return 0
        facts = mem.get("facts", [])
        return len(facts)

    def get_memory_impression(self, user_id: str) -> str:
        """根据记忆内容推断用户给bot的印象 ('positive' | 'neutral' | 'negative')。

        简单启发式: 正面类别(偏好/兴趣/技能)多 → positive; 有负面类别 → negative。
        """
        mem = self.load(user_id)
        if not mem:
            return "neutral"
        facts = mem.get("facts", [])
        if not facts:
            return "neutral"
        pos_cats = {"偏好", "兴趣", "技能", "身份"}
        neg_cats = {}
        pos = sum(1 for f in facts if f.get("category", "") in pos_cats)
        neg = sum(1 for f in facts if f.get("category", "") in neg_cats)
        if pos > len(facts) * 0.5:
            return "positive"
        if neg > 0:
            return "negative"
        return "neutral"

    # ── SQLite 同步 ──────────────────────────────────

    def _sync_fact_to_db(
        self, user_id: str, user_name: str, fact: dict,
    ) -> None:
        """将单条记忆同步到 SQLite user_memories 表。"""
        try:
            from astrbot_plugin_suli_tavern.service.bot_db import get_bot_db
            db = get_bot_db()
            db.conn.execute(
                "INSERT OR IGNORE INTO user_memories "
                "(user_id, user_name, fact_key, fact_value, category, "
                " importance, created_at, last_accessed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, user_name,
                    str(fact.get("key", "")),
                    str(fact.get("value", "")),
                    str(fact.get("category", "其他")),
                    float(fact.get("importance", 0.5)),
                    float(fact.get("created_at", time.time())),
                    float(fact.get("last_accessed", time.time())),
                ),
            )
            db.conn.commit()
        except Exception:
            logger.debug("SQLite 记忆同步失败", exc_info=True)

    def _sync_extract_to_db(
        self, user_id: str, user_name: str, new_facts: list[dict],
    ) -> None:
        """批量同步提取的记忆到 SQLite。"""
        try:
            from astrbot_plugin_suli_tavern.service.bot_db import get_bot_db
            db = get_bot_db()
            now = time.time()
            for f in new_facts:
                db.conn.execute(
                    "INSERT OR IGNORE INTO user_memories "
                    "(user_id, user_name, fact_key, fact_value, category, "
                    " importance, created_at, last_accessed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id, user_name,
                        str(f.get("key", "")),
                        str(f.get("value", "")),
                        str(f.get("category", "其他")),
                        float(f.get("importance", 0.5)),
                        float(f.get("created_at", now)),
                        float(f.get("last_accessed", now)),
                    ),
                )
            db.conn.commit()
        except Exception:
            logger.debug("SQLite 批量记忆同步失败", exc_info=True)


# ── per-bot 存储 ─────────────────────────────────────────

_global_stores: dict[str, UserMemoryStore] = {}

# 默认键: init_memory_store 在没有 bot_id 时暂存于此,
# 首次 get_memory_store(bot_id) 调用时自动迁移。
_DEFAULT_KEY = ""


def get_memory_store(bot_id: str = "") -> UserMemoryStore | None:
    """获取 per-bot UserMemoryStore (未初始化返回 None)。

    Args:
        bot_id: bot 的 QQ 号。空字符串时返回 None (不再回退到任意 store)。
    """
    if not bot_id:
        return None
    if bot_id in _global_stores:
        return _global_stores[bot_id]
    # 懒迁移: 默认 store → bot_id (更新内部 bot_id)
    if _DEFAULT_KEY in _global_stores:
        store = _global_stores.pop(_DEFAULT_KEY)
        store._bot_id = bot_id
        _global_stores[bot_id] = store
        logger.debug("UserMemoryStore 懒迁移: '' → %s", bot_id)
        return store
    return None


def init_memory_store(config, tavern, bot_id: str = "") -> UserMemoryStore:
    """初始化 per-bot UserMemoryStore。

    在 tavern 和 config 就绪后调用一次 (每个 bot 各自调用)。
    重复调用会重新初始化 (用于配置热更新)。

    Args:
        bot_id: bot 的 QQ 号。空字符串时存入默认槽，等待首次 get_memory_store 懒迁移。
    """
    _key = bot_id or _DEFAULT_KEY
    _global_stores[_key] = UserMemoryStore(config, tavern, bot_id=bot_id)
    logger.info("UserMemoryStore per-bot 单例已初始化: bot=%s", bot_id or "(default)")
    return _global_stores[_key]
