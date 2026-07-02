"""事实错误记录数据库 — JSON 持久化存储。

设计:
  - 每次 bot 在技术问题上被指正且确实错误时，记录一条
  - JSON 文件持久化: data/fact_errors.json
  - 线程安全: asyncio.Lock 保护写操作

用法:
  from .fact_errors import get_fact_error_db

  db = get_fact_error_db(bot_id="000000000")
  db.record_error(
      question="LoRA 权重范围是多少",
      bot_answer="LoRA 权重在 0 到 1 之间...",
      user_correction="LoRA 权重可以大于 1，通常 0-2",
      bot_admitted=True,
  )
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 数据目录 (项目根)

def _get_plugin_data_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        _base = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_suli_intelligence"
    except ImportError:
        _base = Path("data/plugin_data/astrbot_plugin_suli_intelligence")
    _base.mkdir(parents=True, exist_ok=True)
    return _base
_DATA_DIR = _get_plugin_data_dir()
_ERRORS_FILE = _DATA_DIR / "fact_errors.json"

# 最多保留的错误记录条数
MAX_ERRORS = 200


@dataclass
class FactErrorEntry:
    """单条错误记录。"""

    timestamp: float           # time.time()
    question: str              # 用户问题 / 话题
    bot_answer: str            # bot 的原回答 (截断到 200 字)
    user_correction: str       # 用户的更正 / 正确信息
    bot_admitted: bool         # bot 是否承认了错误
    resolved: bool = False     # 是否已解决 (当前版本始终 True)
    groups: list[str] = field(default_factory=list)  # 涉及的群号


class FactErrorDB:
    """事实错误持久化数据库。

    遵循项目 JSON 存储模式 (见 group_chat.py 白名单 + 用户记忆)。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _ERRORS_FILE
        self._lock = asyncio.Lock()
        self._errors: list[FactErrorEntry] = []
        self._load()

    # ── 加载 / 保存 ──────────────────────────────────

    def _load(self) -> None:
        """从 JSON 文件加载错误记录。"""
        if not self._path.exists():
            logger.debug("错误记录文件不存在，初始化为空: %s", self._path)
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                logger.warning("错误记录文件格式异常 (非数组)，重置")
                return
            for item in raw[-MAX_ERRORS:]:  # 最多保留最近 N 条
                self._errors.append(FactErrorEntry(
                    timestamp=item.get("timestamp", 0.0),
                    question=item.get("question", ""),
                    bot_answer=item.get("bot_answer", ""),
                    user_correction=item.get("user_correction", ""),
                    bot_admitted=item.get("bot_admitted", False),
                    resolved=item.get("resolved", False),
                    groups=item.get("groups", []),
                ))
            logger.info("错误记录加载: %d 条", len(self._errors))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("错误记录文件读取失败: %s", e)

    async def _save(self) -> None:
        """异步保存到 JSON 文件。"""
        async with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                data = [
                    {
                        "timestamp": e.timestamp,
                        "question": e.question,
                        "bot_answer": e.bot_answer,
                        "user_correction": e.user_correction,
                        "bot_admitted": e.bot_admitted,
                        "resolved": e.resolved,
                        "groups": e.groups,
                    }
                    for e in self._errors[-MAX_ERRORS:]
                ]
                self._path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.debug("错误记录已保存: %d 条", len(data))
            except OSError:
                logger.error("错误记录保存失败", exc_info=True)

    # ── 公开接口 ──────────────────────────────────────

    async def record_error(
        self,
        question: str,
        bot_answer: str,
        user_correction: str,
        bot_admitted: bool = False,
        resolved: bool = True,
        groups: list[str] | None = None,
    ) -> None:
        """记录一条事实错误。

        Args:
            question: 用户问题或话题
            bot_answer: bot 的原回答 (会自动截断到 200 字)
            user_correction: 用户指出的正确信息
            bot_admitted: bot 是否承认了
            resolved: 是否已解决
            groups: 涉及的群号
        """
        entry = FactErrorEntry(
            timestamp=time.time(),
            question=question[:300],
            bot_answer=bot_answer[:200],
            user_correction=user_correction[:300],
            bot_admitted=bot_admitted,
            resolved=resolved,
            groups=groups or [],
        )
        async with self._lock:
            self._errors.append(entry)
            # 超出上限时删旧
            while len(self._errors) > MAX_ERRORS:
                self._errors.pop(0)

        await self._save()
        logger.info(
            "错误记录已写入: question=%r admitted=%s",
            question[:80],
            bot_admitted,
        )

    def get_recent(self, n: int = 5) -> list[FactErrorEntry]:
        """获取最近 N 条错误记录。"""
        return list(reversed(self._errors[-n:]))

    def summary(self) -> str:
        """返回错误记录摘要 (供 LLM 参考)。"""
        if not self._errors:
            return "暂无历史错误记录。"

        recent = self.get_recent(5)
        lines = [f"历史错误记录 (共 {len(self._errors)} 条, 最近 {len(recent)} 条):"]
        for e in recent:
            status = "已认错" if e.bot_admitted else "未认错"
            ts = time.strftime("%m-%d %H:%M", time.localtime(e.timestamp))
            lines.append(
                f"  [{ts}] [{status}] Q: {e.question[:60]}..."
                f" → 正确: {e.user_correction[:60]}..."
            )
        return "\n".join(lines)

    @property
    def total_errors(self) -> int:
        return len(self._errors)

    @property
    def total_admitted(self) -> int:
        return sum(1 for e in self._errors if e.bot_admitted)


# ── per-bot 单例 ──────────────────────────────────────

_fact_dbs: dict[str, FactErrorDB] = {}


def _store_path_for(bot_id: str) -> Path:
    """per-bot 错误记录存储路径。"""
    return _DATA_DIR / f"fact_errors_{bot_id}.json"


def get_fact_error_db(bot_id: str) -> FactErrorDB:
    """获取指定 bot 的错误记录数据库单例。"""
    if bot_id not in _fact_dbs:
        _fact_dbs[bot_id] = FactErrorDB(path=_store_path_for(bot_id))
    return _fact_dbs[bot_id]
