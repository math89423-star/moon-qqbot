"""Bot 身份注册中心 — 单一真相源。

替代所有散落在各文件中的硬编码 QQ 号、bot 名称、昵称映射。
所有运行时 bot 身份查询统一走此类，数据持久化在 bot_identity 表中。

用法:
    svc = get_bot_identity_service()
    bot = svc.get_bot("BOT_QQ_MAIN")
    bots = svc.list_bots(active_only=True)
"""

from __future__ import annotations

import json
import logging
import os as _os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class BotIdentity:
    """单个 bot 的完整身份信息。"""
    bot_id: str                      # QQ 号
    name: str                        # 显示名 ("暮恩")
    character_card: str = ""         # 角色卡文件名 ("moon")
    nicknames: list[str] = field(default_factory=list)   # ["小暮", "暮暮"]
    is_active: bool = True
    peer_bot_ids: list[str] = field(default_factory=list)  # ["BOT_QQ_ALT"]
    icon: str = "🤖"
    color: str = "#666666"
    role_description: str = ""       # "蛇娘" / "猫娘"
    rejection_style: dict = field(default_factory=dict)  # {"style_label","pronoun","tone_hint"} 工具拒绝文案风格
    llm_slots: tuple[str, ...] = ()  # per-bot LLM 槽位覆盖，空=默认
    metadata: dict = field(default_factory=dict)  # 原始 JSON blob
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_row(cls, row: dict) -> BotIdentity:
        """从 DB 行 dict 构建。"""
        try:
            nicknames = json.loads(row.get("nicknames", "[]"))
        except (json.JSONDecodeError, TypeError):
            nicknames = []
        try:
            peer_bot_ids = json.loads(row.get("peer_bot_ids", "[]"))
        except (json.JSONDecodeError, TypeError):
            peer_bot_ids = []
        try:
            metadata = json.loads(row.get("metadata", "{}"))
        except (json.JSONDecodeError, TypeError):
            metadata = {}

        return cls(
            bot_id=str(row["bot_id"]),
            name=row.get("name", ""),
            character_card=row.get("character_card", ""),
            nicknames=nicknames,
            is_active=bool(row.get("is_active", 1)),
            peer_bot_ids=peer_bot_ids,
            icon=metadata.get("icon", "🤖"),
            color=metadata.get("color", "#666666"),
            role_description=metadata.get("role_description", ""),
            rejection_style=(
                metadata.get("rejection_style", {})
                if isinstance(metadata.get("rejection_style"), dict)
                else {}
            ),
            llm_slots=tuple(metadata.get("llm_slots", ())),
            metadata=metadata,
            created_at=row.get("created_at", 0),
            updated_at=row.get("updated_at", 0),
        )

    def get_metadata(self, key: str, default=None):
        """从 metadata dict 中取单键值。"""
        return self.metadata.get(key, default)


class BotIdentityService:
    """Bot 身份注册中心。

    内存缓存 + DB version counter 失效策略：
    - 读：先检查缓存版本，一致则命中；不一致则重新加载全部
    - 写：写 DB → bump version → 清缓存
    """

    def __init__(self, db=None):
        """初始化服务。

        Args:
            db: BotDatabase 实例。懒初始化时可传 None，
                后续通过 init_db() 注入。
        """
        self._db = db
        self._cache: dict[str, BotIdentity] = {}
        self._cache_version: int = -1
        self._initialized: bool = False

    # ── 初始化 ──────────────────────────────────────────

    def init_db(self, db) -> None:
        """延迟注入 DB 连接。"""
        self._db = db
        self._ensure_seeded()

    def _ensure_seeded(self) -> None:
        """如果 bot_identity 表为空，自动从环境变量 + 角色卡文件注册。"""
        if self._initialized:
            return
        self._initialized = True

        if not self._db:
            return
        try:
            count = self._db.bot_identity_count()
        except Exception:
            return

        if count == 0:
            self._seed_defaults()

    def _seed_defaults(self) -> None:
        """首次启动：从环境变量 + characters/ 自动发现并注册 bot。

        优先级:
          1. BOT_QQ_MAIN + BOT_CHAR_MAIN / BOT_QQ_ALT + BOT_CHAR_ALT
          2. 扫描 characters/ 目录，按文件名排序依次分配
          3. 无环境变量也无角色卡 → 跳过（bot 列表为空，管理面板手动创建）
        """
        char_dir = Path(__file__).resolve().parent.parent / "characters"
        main_qq = _os.getenv("BOT_QQ_MAIN", "")
        alt_qq = _os.getenv("BOT_QQ_ALT", "")
        main_char = _os.getenv("BOT_CHAR_MAIN", "")
        alt_char = _os.getenv("BOT_CHAR_ALT", "")

        # 构建 QQ → card_name 映射
        mapping: dict[str, str] = {}
        if main_qq and main_char:
            mapping[main_qq] = main_char
        if alt_qq and alt_char:
            mapping[alt_qq] = alt_char

        # 自动扫描角色卡文件
        discovered: list[str] = []
        if char_dir.exists():
            for f in sorted(char_dir.glob("*.json")):
                if f.stem.endswith("_world_book") or f.stem.startswith("example"):
                    continue
                discovered.append(f.stem)

        # 将未分配的 QQ 槽位匹配到未映射的角色卡
        assigned_cards = set(mapping.values())
        qq_slots = [q for q in (main_qq, alt_qq) if q and q not in mapping]
        for card_name in discovered:
            if card_name in assigned_cards:
                continue  # 已通过 env var 映射
            assigned_cards.add(card_name)
            if qq_slots:
                mapping[qq_slots.pop(0)] = card_name
            else:
                mapping[f"auto_{card_name}"] = card_name

        if not mapping:
            logger.info("BotIdentity: 无环境变量且无角色卡，跳过自动注册（需在管理面板手动创建）")
            return

        # 加载角色卡获取元数据
        for qq, card_name in mapping.items():
            try:
                card = self._load_card(card_name)
                data = card.get("data", {}) if card else {}
                name = data.get("name", card_name)
                nicknames_json = json.dumps(
                    data.get("nicknames", [name]) if not isinstance(data.get("nicknames"), str)
                    else data.get("nicknames", [name]),
                    ensure_ascii=False,
                )
                if isinstance(data.get("nicknames"), str):
                    nicknames_json = data["nicknames"]
                elif isinstance(data.get("nicknames"), list):
                    nicknames_json = json.dumps(data["nicknames"], ensure_ascii=False)
                else:
                    nicknames_json = json.dumps([name], ensure_ascii=False)

                role_desc = data.get("role_description", "")
                if not role_desc:
                    # 尝试从 personality 字段提取
                    personality = data.get("personality", "")
                    if personality:
                        role_desc = personality.split("、")[0] if "、" in personality else personality.split(",")[0]

                icon = data.get("icon", "🐍" if "moon" in card_name else "🤖")
                color = data.get("color", "#4ecca3" if "moon" in card_name else "#666666")
                llm_slots = data.get("llm_slots", [])
                # 单 bot 架构 — moon 不需要额外的 llm_slots 覆盖

                metadata = {
                    "icon": icon,
                    "color": color,
                    "role_description": role_desc,
                    "llm_slots": llm_slots,
                    "llm_lite": data.get("llm_lite", ""),
                    "llm_pro": data.get("llm_pro", ""),
                    "llm_gate": data.get("llm_gate", ""),
                    "llm_judge": data.get("llm_judge", ""),
                    "vlm_primary": data.get("vlm_primary", ""),
                }

                peer_ids = [p for p in mapping if p != qq]
                self._db.bot_identity_create(
                    bot_id=qq,
                    name=name,
                    character_card=card_name,
                    nicknames=nicknames_json,
                    peer_bot_ids=json.dumps(peer_ids),
                    metadata=json.dumps(metadata, ensure_ascii=False),
                    is_active=True,
                )
                logger.info("BotIdentity 自动注册: %s → %s (角色卡: %s)", qq, name, card_name)
            except Exception as e:
                logger.warning("BotIdentity 自动注册失败 qq=%s card=%s: %s", qq, card_name, e)

    @staticmethod
    def _load_card(name: str) -> dict | None:
        """加载角色卡 JSON 文件。"""
        try:
            char_dir = Path(__file__).resolve().parent.parent / "characters"
            path = char_dir / f"{name}.json"
            if not path.exists():
                return None
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    # ── 缓存管理 ─────────────────────────────────────────

    def _check_cache(self) -> None:
        """检查缓存版本，失效时重新加载。"""
        if not self._db:
            return
        db_version = self._db.bot_identity_version()
        if db_version != self._cache_version:
            self._reload_cache()

    def _reload_cache(self) -> None:
        """从 DB 全量加载所有 bot 到内存缓存。"""
        if not self._db:
            return
        rows = self._db.bot_identity_list(active_only=False)
        self._cache = {}
        for row in rows:
            identity = BotIdentity.from_row(row)
            self._cache[identity.bot_id] = identity
        self._cache_version = self._db.bot_identity_version()
        logger.debug("BotIdentity 缓存已刷新: %d bots, version=%d",
                      len(self._cache), self._cache_version)

    # ── 查询 API ─────────────────────────────────────────

    def get_bot(self, bot_id: str) -> BotIdentity | None:
        """按 QQ 号获取 bot 身份。"""
        self._check_cache()
        return self._cache.get(str(bot_id))

    def list_bots(self, active_only: bool = True) -> list[BotIdentity]:
        """列出所有 bot。

        Args:
            active_only: True 仅返回活跃 bot，False 返回全部含停用
        """
        self._check_cache()
        bots = list(self._cache.values())
        if active_only:
            bots = [b for b in bots if b.is_active]
        bots.sort(key=lambda b: b.created_at)
        return bots

    def get_peer_bots(self, bot_id: str) -> list[BotIdentity]:
        """返回当前 bot 的所有 peer bot。"""
        bot = self.get_bot(str(bot_id))
        if not bot or not bot.peer_bot_ids:
            return []
        peers = []
        for pid in bot.peer_bot_ids:
            peer = self.get_bot(pid)
            if peer:
                peers.append(peer)
        return peers

    def get_bot_by_name(self, name: str) -> BotIdentity | None:
        """按显示名查找 bot (不区分大小写)。"""
        self._check_cache()
        name_lower = name.lower()
        for bot in self._cache.values():
            if bot.name.lower() == name_lower:
                return bot
        return None

    def resolve_character_card(self, bot_id: str) -> str:
        """获取 bot 对应的角色卡文件名。

        Returns:
            角色卡文件名 (如 "moon")，未知 bot 返回空字符串。
        """
        bot = self.get_bot(str(bot_id))
        return bot.character_card if bot else ""

    def get_nickname_pattern(self, bot_id: str) -> str:
        """生成 bot 昵称 regex alternation (用于消歧/检测)。

        Returns:
            如 "小暮|暮暮|洛宝|暮恩|moon"
        """
        bot = self.get_bot(str(bot_id))
        if not bot:
            return ""
        names = [bot.name] + bot.nicknames
        return "|".join(names)

    def get_llm_slots(self, bot_id: str) -> tuple[str, ...]:
        """获取 bot 的 LLM 槽位列表。

        Returns:
            槽位名 tuple，如 ("llm_lite", "llm_pro", "llm_gate", "llm_judge")
            未配置时返回全部默认槽位。
        """
        bot = self.get_bot(str(bot_id))
        if bot and bot.llm_slots:
            return bot.llm_slots
        # 默认全部 4 个槽位
        return ("llm_lite", "llm_pro", "llm_gate", "llm_judge")

    def get_all_nicknames_alternation(self) -> str:
        """生成所有 bot 的所有昵称的 regex alternation。

        用于 input_classifier 等需要全局匹配的场景。
        Returns:
            如 "小暮|暮恩|moon|||"
        """
        self._check_cache()
        all_names: set[str] = set()
        for bot in self._cache.values():
            all_names.add(bot.name)
            all_names.update(bot.nicknames)
        return "|".join(all_names)

    # ── 写入 API ─────────────────────────────────────────

    def create_bot(self, identity: BotIdentity) -> bool:
        """注册新 bot。"""
        if not self._db:
            return False
        nicknames_json = json.dumps(identity.nicknames, ensure_ascii=False)
        peer_json = json.dumps(identity.peer_bot_ids)
        metadata = dict(identity.metadata)
        metadata["icon"] = identity.icon
        metadata["color"] = identity.color
        metadata["role_description"] = identity.role_description
        metadata["rejection_style"] = identity.rejection_style
        if identity.llm_slots:
            metadata["llm_slots"] = list(identity.llm_slots)
        metadata_json = json.dumps(metadata, ensure_ascii=False)

        ok = self._db.bot_identity_create(
            bot_id=identity.bot_id,
            name=identity.name,
            character_card=identity.character_card,
            nicknames=nicknames_json,
            peer_bot_ids=peer_json,
            metadata=metadata_json,
            is_active=identity.is_active,
        )
        if ok:
            self._cache_version = -1  # 强制下次重载
        return ok

    def update_bot(self, identity: BotIdentity) -> bool:
        """更新已有 bot。"""
        if not self._db:
            return False
        nicknames_json = json.dumps(identity.nicknames, ensure_ascii=False)
        peer_json = json.dumps(identity.peer_bot_ids)
        metadata = dict(identity.metadata)
        metadata["icon"] = identity.icon
        metadata["color"] = identity.color
        metadata["role_description"] = identity.role_description
        metadata["rejection_style"] = identity.rejection_style
        if identity.llm_slots:
            metadata["llm_slots"] = list(identity.llm_slots)
        metadata_json = json.dumps(metadata, ensure_ascii=False)

        ok = self._db.bot_identity_update(
            bot_id=identity.bot_id,
            name=identity.name,
            character_card=identity.character_card,
            nicknames=nicknames_json,
            is_active=identity.is_active,
            peer_bot_ids=peer_json,
            metadata=metadata_json,
        )
        if ok:
            self._cache_version = -1
        return ok

    def delete_bot(self, bot_id: str) -> bool:
        """删除 bot。"""
        if not self._db:
            return False
        ok = self._db.bot_identity_delete(str(bot_id))
        if ok:
            self._cache_version = -1
        return ok


# ── 全局单例 ──────────────────────────────────────────────

_global_identity_service: BotIdentityService | None = None


def get_bot_identity_service() -> BotIdentityService:
    """获取全局 BotIdentityService 单例。

    首次调用时创建实例（但不会自动连接 DB ——
    需要主流程调用 init_db() 注入 BotDatabase）。
    """
    global _global_identity_service
    if _global_identity_service is None:
        _global_identity_service = BotIdentityService()
    return _global_identity_service


def set_bot_identity_service(svc: BotIdentityService) -> None:
    """替换全局 BotIdentityService 实例（主要用于测试）。"""
    global _global_identity_service
    _global_identity_service = svc
