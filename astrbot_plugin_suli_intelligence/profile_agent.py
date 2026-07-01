"""Profile Agent — 异步用户建档 (Layer 2 Task Agent)。

设计:
  - 由 IntentJudge.should_profile 提名触发，完全异步 (fire-and-forget)
  - 独立上下文: 用户最近发言 + 已有档案 + 好感度/情绪
  - 节流层: per-user 冷却 + 串行锁，防止重复建档
  - 输出: 结构化档案更新 → 合并到 daily memory + core 蒸馏标记

用法:
  from .profile_agent import ProfileAgent

  asyncio.create_task(
      ProfileAgent.maybe_build_profile(
          tavern, ctx, user_id, user_name, config, memory_store, tier_manager,
      )
  )
"""

from __future__ import annotations

import asyncio
import logging
import time

# InjectionGuard 采用 lazy import — AstrBot 插件加载时 sys.modules 尚未注册
# 依赖插件，模块级 import 会导致 ModuleNotFoundError。
# 改为在 _do_build_profile / _do_lightweight_profile 函数内部按需导入。

logger = logging.getLogger(__name__)

# ── 节流 ──────────────────────────────────────────────────────

# 模块级: {"{bot_id}:{user_id}": last_run_timestamp} — per-bot 隔离
_profile_cooldowns: dict[str, float] = {}

# 模块级: {"{bot_id}:{user_id}": asyncio.Lock} — 同一 (bot, user) 建档串行化
_profile_locks: dict[str, asyncio.Lock] = {}

# 默认冷却 (秒)
_DEFAULT_COOLDOWN = 1800  # 30 分钟

# ── Profile Agent system prompt ───────────────────────────────

_PROFILE_SYSTEM = """你是群友档案分析器。从用户最近的聊天记录中提取值得长期记住的信息。

[提取类型]
- 设备: 硬件配置、显卡、内存、系统
- 兴趣: 爱好、关注领域、喜欢的作品/角色
- 偏好: 行为偏好、沟通风格
- 经历: 做过的事、遇到的问题、项目经历
- 技能: 技术栈、擅长的领域
- 身份: 职业、角色、自称
- 其他: 任何值得记住的细节

[规则]
- 只提取确实说了的内容，不要脑补
- 一条事实一行，格式: "类别: 内容"
- 没有值得记住的信息就输出 "无"
- 不要重复已有档案中已经记录的内容
- 最多提取 5 条"""

# ── 轻量建档 system prompt (更宽松，追求覆盖面) ─────────────

_LIGHTWEIGHT_PROFILE_SYSTEM = """你是群友快速标注器。给一个刚聊过天的用户贴 1-3 个标签。

[标签类型]
- 兴趣: 常聊的话题、关注的领域 (如 "AI绘画" "ComfyUI" "动漫" "游戏")
- 设备: 透露的硬件/软件信息 (如 "用4060" "跑SDXL" "显存8G")
- 风格: 聊天风格 (如 "话多" "喜欢发图" "技术向" "深夜出没")
- 技能: 提到过会的东西 (如 "会调参数" "写prompt" "懂CUDA")
- 身份: 透露的角色 (如 "学生" "画师" "程序员" "模型训练者")
- 其他: 任何值得记的细节

[规则]
- 有信息就写，不确定也写 (标注"可能")，比完全没有强
- 一条一个标签，格式: "类别: 内容"
- 实在什么信息都没有才输出 "无"
- 最多 3 条"""

# ── 最大输入长度 ──────────────────────────────────────────────

_MAX_USER_MSGS = 20      # 读取用户最近 N 条发言
_MAX_MSG_LEN = 200       # 每条消息截断长度
_MAX_EXISTING_FACTS = 10 # 已有档案最多展示 N 条给 agent


# ═══════════════════════════════════════════════════════════════
# ProfileAgent
# ═══════════════════════════════════════════════════════════════

class ProfileAgent:
    """异步用户建档 Agent — Layer 2 后台任务。

    纯静态方法，模块级节流状态。
    成本: flash 模型 ~400 input + ~100 output tokens。
    """

    @staticmethod
    async def maybe_build_profile(
        tavern,        # duck-typed: .chat(messages, temperature, max_tokens)
        ctx,           # GroupChatContext (duck-typed)
        user_id: str,
        user_name: str,
        config,        # Config
        memory_store,  # UserMemoryStore (duck-typed: .get_hints_for_user, .merge)
        tier_manager=None,  # MemoryTierManager | None (duck-typed: .mark_dirty)
        bot_id: str = "",
    ) -> None:
        """被 IntentJudge 提名后，异步提取用户档案。

        内置节流: 同一 (bot, user) 组合 N 分钟内只建档一次。
        内置串行锁: 同一 (bot, user) 不会并发建档。

        Args:
            tavern: TavernClient
            ctx: GroupChatContext
            user_id: QQ 号
            user_name: 用户名
            config: Config
            memory_store: UserMemoryStore
            tier_manager: MemoryTierManager (可选)
            bot_id: bot QQ 号 (per-bot 隔离)
        """
        if not user_id:
            return

        # ── 配置检查 ──
        enabled = getattr(config, "profile_agent_enabled", True)
        if not enabled:
            return

        cooldown = getattr(
            config, "profile_agent_cooldown_minutes", 30,
        ) * 60

        # ── 节流: per-(bot, user) cooldown ──
        _pkey = f"{bot_id}:{user_id}" if bot_id else user_id
        now = time.time()
        last_run = _profile_cooldowns.get(_pkey, 0)
        if now - last_run < cooldown:
            logger.debug(
                "ProfileAgent: 跳过 bot=%s user=%s (冷却中, %.0fs 前刚建档)",
                bot_id, user_id[:8], now - last_run,
            )
            return

        # ── 串行化: per-(bot, user) lock ──
        lock = _profile_locks.get(_pkey)
        if lock is None:
            lock = asyncio.Lock()
            _profile_locks[_pkey] = lock

        async with lock:
            # 双重检查 (可能在等锁期间另一任务已完成)
            now2 = time.time()
            last_run2 = _profile_cooldowns.get(_pkey, 0)
            if now2 - last_run2 < cooldown:
                return

            try:
                await _do_build_profile(
                    tavern, ctx, user_id, user_name,
                    memory_store, tier_manager,
                    bot_id=bot_id,
                )
            except Exception:
                logger.error(
                    "ProfileAgent: 建档失败 bot=%s user=%s",
                    bot_id, user_id[:8], exc_info=True,
                )
            finally:
                _profile_cooldowns[_pkey] = time.time()

    @staticmethod
    async def maybe_lightweight_profile(
        tavern,        # duck-typed: .chat(messages, temperature, max_tokens)
        ctx,           # GroupChatContext
        user_ids: list[str],
        config,        # Config
        memory_store,  # UserMemoryStore
        admin_qq: int | None = None,
        bot_id: str = "",
    ) -> None:
        """批量轻量建档 — 对最近发言的多个用户快速贴标签。

        比 maybe_build_profile 更宽松:
          - 不需要 IntentJudge 提名
          - prompt 接受低质量标签 ("可能用4060")
          - 单用户最多 3 条，不是 5 条
          - 对已有档案的用户跳过 (避免重复)

        Args:
            tavern: TavernClient
            ctx: GroupChatContext
            user_ids: 候选用户 ID 列表
            config: Config
            memory_store: UserMemoryStore
            admin_qq: 管理员 QQ (不受冷却限制)
            bot_id: bot QQ 号 (per-bot 隔离)
        """
        enabled = getattr(config, "lightweight_profile_enabled", True)
        if not enabled:
            return

        cooldown = getattr(
            config, "lightweight_profile_cooldown_minutes", 30,
        ) * 60
        max_users = getattr(
            config, "lightweight_profile_batch_max_users", 5,
        )

        now = time.time()

        # 过滤: 冷却中 / 无记忆的 bot 自身 / 管理员
        candidates = []
        for uid in user_ids:
            if not uid or uid.startswith("bot_"):
                continue
            # 管理员不受冷却限制但也不做轻量建档 (已有完整档案)
            if admin_qq is not None and uid == str(admin_qq):
                continue
            # 冷却检查 (per-bot 复合键)
            _pkey = f"{bot_id}:{uid}" if bot_id else uid
            last = _profile_cooldowns.get(_pkey, 0)
            if now - last < cooldown:
                continue
            # 已有足够档案的跳过 (>=3条就跳过轻量建档)
            try:
                existing = memory_store.load(uid) if hasattr(memory_store, "load") else None
                if existing and len(existing.get("facts", [])) >= 3:
                    continue
            except Exception:
                pass
            candidates.append(uid)

        if not candidates:
            return

        # 限制单批数量
        candidates = candidates[:max_users]

        logger.info(
            "ProfileAgent: 轻量建档启动 candidates=%d users=%s bot=%s",
            len(candidates), ",".join(u[:8] for u in candidates), bot_id,
        )

        for user_id in candidates:
            # 串行化: per-(bot, user) lock
            _pkey = f"{bot_id}:{user_id}" if bot_id else user_id
            lock = _profile_locks.get(_pkey)
            if lock is None:
                lock = asyncio.Lock()
                _profile_locks[_pkey] = lock

            async with lock:
                now2 = time.time()
                last2 = _profile_cooldowns.get(_pkey, 0)
                if now2 - last2 < cooldown:
                    continue

                try:
                    # 从 ctx.messages 中取该用户最近发言
                    user_msgs_list = [
                        m for m in ctx.messages[-20:]
                        if str(m.get("user_id", "")) == user_id
                    ]
                    if not user_msgs_list:
                        continue

                    user_name = str(user_msgs_list[0].get("user_name", ""))
                    await _do_lightweight_profile(
                        tavern, user_msgs_list, user_id, user_name,
                        memory_store,
                        bot_id=bot_id,
                    )
                except Exception:
                    logger.debug(
                        "ProfileAgent: 轻量建档失败 bot=%s user=%s",
                        bot_id, user_id[:8], exc_info=True,
                    )
                finally:
                    _profile_cooldowns[_pkey] = time.time()


# ═══════════════════════════════════════════════════════════════
# 内部实现
# ═══════════════════════════════════════════════════════════════

async def _do_build_profile(
    tavern, ctx, user_id: str, user_name: str,
    memory_store, tier_manager,
    bot_id: str = "",
) -> None:
    """执行一次用户建档: 收集上下文 → LLM 提取 → 写入档案。"""

    # 1. 收集用户最近发言
    recent = [
        m for m in ctx.messages[-_MAX_USER_MSGS:]
        if str(m.get("user_id", "")) == user_id
    ]
    if not recent:
        return

    user_msgs = []
    for m in recent[-10:]:  # 最近 10 条
        content = str(m.get("content", ""))
        if len(content) > _MAX_MSG_LEN:
            content = content[:_MAX_MSG_LEN - 3] + "..."
        user_msgs.append(content)

    # 2. 获取已有档案 (去重用)
    existing_facts: list[str] = []
    try:
        hints = memory_store.get_hints_for_user(user_id)
        if hints:
            existing_facts = [h.strip() for h in hints.split("\n") if h.strip()]
        existing_facts = existing_facts[:_MAX_EXISTING_FACTS]
    except Exception:
        logger.debug("ProfileAgent: 获取已有档案失败", exc_info=True)

    # 3. 构建 prompt
    context_lines = [f"--- {user_name} 最近的发言 ---"]
    for i, msg in enumerate(user_msgs):
        context_lines.append(f"{i + 1}. {msg}")

    if existing_facts:
        context_lines.append("")
        context_lines.append("--- 已有档案 (不要重复记录) ---")
        for fact in existing_facts:
            context_lines.append(f"- {fact}")

    context_lines.append("")
    context_lines.append("从以上发言中提取值得长期记住的新信息。")
    context_lines.append('格式: 类别: 内容 (一行一条，没有则输出"无")')

    profile_messages = [
        {"role": "system", "content": _PROFILE_SYSTEM},
        {"role": "user", "content": "\n".join(context_lines)},
    ]

    # 4. LLM 提取 (timeout=30s)
    _bg_llm = {}
    try:
        from astrbot_plugin_suli_tavern.service.bot_config import get_config_service
        _bg_llm = get_config_service().resolve_background_llm(bot_id, "profile_build")
    except Exception:
        logger.warning("ProfileAgent: resolve_background_llm 失败", exc_info=True)

    try:
        raw = await asyncio.wait_for(
            tavern.chat(
                profile_messages,
                temperature=0.2,
                max_tokens=150,
                model=_bg_llm.get("model", "deepseek-v4-flash"),
                api_base=_bg_llm.get("api_base", ""),
                api_key=_bg_llm.get("api_key", ""),
                extra_params=_bg_llm.get("extra_params"),
            ),
            timeout=30,
        )
    except TimeoutError:
        logger.warning("ProfileAgent._do_build_profile: LLM 调用超时 (30s)")
        return
    except Exception:
        logger.warning("ProfileAgent: LLM 调用失败", exc_info=True)
        return

    if not raw or raw.strip() == "无":
        return

    # 5. 解析 + 写入
    new_facts = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line or line == "无":
            continue
        # 格式: "类别: 内容"
        if ":" in line or "：" in line:
            new_facts.append(line)

    if not new_facts:
        return

    logger.info(
        "ProfileAgent: user=%s name=%s 提取 %d 条新事实",
        user_id[:8], user_name, len(new_facts),
    )

    # 6. 写入记忆 (通过 UserMemoryStore.remember())
    # ── 安全过滤: 拒绝指令性/注入性陈述写入记忆 (B1 修复) ──
    _rejected_count = 0
    try:
        for fact_text in new_facts:
            # 解析类别
            category = ""
            content = fact_text
            if ":" in fact_text:
                parts = fact_text.split(":", 1)
                category = parts[0].strip()
                content = parts[1].strip() if len(parts) > 1 else fact_text
            elif "：" in fact_text:
                parts = fact_text.split("：", 1)
                category = parts[0].strip()
                content = parts[1].strip() if len(parts) > 1 else fact_text

            # ── 安全过滤: InjectionGuard 扫描提取的事实 ──
            from astrbot_plugin_suli_guards import InjectionGuard  # lazy import (详见文件头注释)

            try:
                _inj_check = InjectionGuard.check(
                    [{"role": "user", "content": content}],
                    user_id=user_id,
                    user_name=user_name,
                    bot_id=bot_id or "",
                )
                if _inj_check.block:
                    _rejected_count += 1
                    logger.info(
                        "ProfileAgent: 指令性陈述已拒绝写入 user=%s "
                        "fact=%s score=%d patterns=%s",
                        user_id[:8], content[:50],
                        _inj_check.score, _inj_check.matched_patterns,
                    )
                    continue
            except Exception:
                # fail-closed: 安全检查异常时拒绝写入 (不能验证安全性 = 不安全)
                _rejected_count += 1
                logger.warning(
                    "ProfileAgent: InjectionGuard 检查异常，已拒绝写入 user=%s fact=%s",
                    user_id[:8], content[:50], exc_info=True,
                )
                continue

            try:
                await memory_store.remember(
                    user_id=user_id,
                    user_name=user_name,
                    fact_value=content,
                    category=category,
                )
            except Exception:
                logger.debug(
                    "ProfileAgent: 写入记忆失败 fact=%s", fact_text[:50],
                    exc_info=True,
                )
    except ImportError:
        # InjectionGuard 不可用时回退到原始逻辑
        for fact_text in new_facts:
            category = ""
            content = fact_text
            if ":" in fact_text:
                parts = fact_text.split(":", 1)
                category = parts[0].strip()
                content = parts[1].strip() if len(parts) > 1 else fact_text
            elif "：" in fact_text:
                parts = fact_text.split("：", 1)
                category = parts[0].strip()
                content = parts[1].strip() if len(parts) > 1 else fact_text
            try:
                await memory_store.remember(
                    user_id=user_id,
                    user_name=user_name,
                    fact_value=content,
                    category=category,
                )
            except Exception:
                logger.debug(
                    "ProfileAgent: 写入记忆失败 fact=%s", fact_text[:50],
                    exc_info=True,
                )

        # 7. 触发 core 蒸馏检查
        if tier_manager is not None:
            try:
                await tier_manager.maybe_distill(user_id)
            except Exception:
                logger.debug("ProfileAgent: maybe_distill 失败", exc_info=True)

    except Exception:
        logger.error(
            "ProfileAgent: 记忆写入失败 user=%s", user_id[:8],
            exc_info=True,
        )


async def _do_lightweight_profile(
    tavern, user_msgs_list: list, user_id: str, user_name: str,
    memory_store,
    bot_id: str = "",
) -> None:
    """执行一次轻量建档: 宽松 prompt → 最多 3 条标签 → 写入记忆。

    与 _do_build_profile 的关键差异:
      - prompt 更宽松 (接受不确定的标签)
      - 上限 3 条 (不是 5 条)
      - 不传已有档案 (避免被空白档案抑制输出)
      - 不触发 core 蒸馏
    """
    # 1. 取最近 5 条发言
    user_msgs = []
    for m in user_msgs_list[-5:]:
        content = str(m.get("content", ""))
        if len(content) > 150:
            content = content[:147] + "..."
        user_msgs.append(content)

    if not user_msgs:
        return

    # 2. 构建 prompt (宽松)
    context_lines = [f"--- {user_name} 最近的发言 (提取标签) ---"]
    for i, msg in enumerate(user_msgs):
        context_lines.append(f"{i + 1}. {msg}")
    context_lines.append("")
    context_lines.append('给这个用户贴 1-3 个标签。格式: 类别: 内容 (没有就写"无")')

    profile_messages = [
        {"role": "system", "content": _LIGHTWEIGHT_PROFILE_SYSTEM},
        {"role": "user", "content": "\n".join(context_lines)},
    ]

    # 3. LLM 提取 (timeout=30s)
    _bg_llm = {}
    try:
        from astrbot_plugin_suli_tavern.service.bot_config import get_config_service
        _bg_llm = get_config_service().resolve_background_llm(bot_id, "profile_lightweight")
    except Exception:
        logger.warning("ProfileAgent: resolve_background_llm 失败", exc_info=True)

    try:
        raw = await asyncio.wait_for(
            tavern.chat(
                profile_messages,
                temperature=0.2,
                max_tokens=100,
                model=_bg_llm.get("model", "deepseek-v4-flash"),
                api_base=_bg_llm.get("api_base", ""),
                api_key=_bg_llm.get("api_key", ""),
                extra_params=_bg_llm.get("extra_params"),
            ),
            timeout=30,
        )
    except TimeoutError:
        logger.warning("ProfileAgent._do_lightweight_profile: LLM 调用超时 (30s)")
        return
    except Exception:
        logger.warning("ProfileAgent: 轻量建档 LLM 调用失败 user=%s", user_id[:8])
        return

    if not raw or raw.strip() == "无":
        return

    # 4. 解析 + 写入 (含 InjectionGuard 过滤, 与 _do_build_profile 一致)
    from astrbot_plugin_suli_guards import InjectionGuard  # lazy import (详见文件头注释)

    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line or line == "无":
            continue

        category = ""
        content = line
        if ":" in line:
            parts = line.split(":", 1)
            category = parts[0].strip()
            content = parts[1].strip() if len(parts) > 1 else line
        elif "：" in line:
            parts = line.split("：", 1)
            category = parts[0].strip()
            content = parts[1].strip() if len(parts) > 1 else line

        if not content:
            continue

        # ── 安全过滤: 拒绝指令性陈述写入记忆 ──
        try:
            _inj_check = InjectionGuard.check(
                [{"role": "user", "content": content}],
                user_id=user_id,
                user_name=user_name,
                bot_id=bot_id or "",
            )
            if _inj_check.block:
                logger.info(
                    "ProfileAgent(轻量): 指令性标签已拒绝写入 user=%s "
                    "fact=%s score=%d",
                    user_id[:8], content[:50], _inj_check.score,
                )
                continue
        except Exception:
            # fail-closed: 安全检查异常时拒绝写入
            logger.warning(
                "ProfileAgent(轻量): InjectionGuard 检查异常，已拒绝写入 user=%s fact=%s",
                user_id[:8], content[:50], exc_info=True,
            )
            continue

        try:
            await memory_store.remember(
                user_id=user_id,
                user_name=user_name,
                fact_value=content,
                category=category,
            )
        except Exception:
            logger.debug(
                "ProfileAgent: 轻量建档写入失败 user=%s fact=%s",
                user_id[:8], content[:40],
                exc_info=True,
            )

    logger.info(
        "ProfileAgent: 轻量建档完成 user=%s name=%s",
        user_id[:8], user_name,
    )
