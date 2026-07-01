"""群聊回复管线 — 将 _evaluate_and_reply 拆解为独立的 PipelineStep。

每个 Step 对应回复管线中的一个阶段，可独立启用/禁用/测试。
新增能力只需注册 Step，不修改核心调度逻辑。

用法:
  from .reply_pipeline import build_reply_pipeline

  pipeline = build_reply_pipeline(scheduler)
  result = await pipeline.run(ctx)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from astrbot_plugin_suli_pipeline import PIPELINE_SILENCE, Pipeline, PipelineContext, PipelineStep

logger = logging.getLogger(__name__)


# ── Step 1: 交叉验证 ─────────────────────────────────────

class CrossValidationStep(PipelineStep):
    """检测用户质疑信号并执行交叉验证。"""

    name = "cross_validation"
    required = False  # 失败不影响回复

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        scheduler = ctx.get("scheduler")
        gctx = ctx.get("ctx")
        cfg = ctx.get("config")

        if not cfg.cross_validation_enabled or not gctx.messages:
            ctx.set("challenge_info", None)
            return ctx

        try:
            from astrbot_plugin_suli_validation import CrossValidator, detect_challenge
        except ImportError:
            from .cross_validation import CrossValidator, detect_challenge

        challenge_info: dict | None = None
        for msg in reversed(gctx.messages):
            uid = msg.get("user_id", "")
            if isinstance(uid, str) and not uid.startswith("bot_"):
                user_content = msg.get("content", "")
                if detect_challenge(user_content):
                    logger.info(
                        "群 %d: 检测到质疑信号，启动交叉验证",
                        gctx.group_id,
                    )
                    validator = CrossValidator(tavern=scheduler._tavern)
                    try:
                        _bot_id = getattr(scheduler, "_current_bot_id", "") or ""
                        challenge_info = await validator.validate(
                            ctx_messages=gctx.messages,
                            user_message=user_content,
                            group_id=str(gctx.group_id),
                            config=cfg,
                            bot_id=_bot_id,
                        )
                        challenge_info["user_id"] = uid
                    except Exception:
                        logger.error(
                            "群 %d: 交叉验证异常",
                            gctx.group_id,
                            exc_info=True,
                        )
                break

        ctx.set("challenge_info", challenge_info)
        return ctx


# ── Step 2: Pre-flight 分析 ──────────────────────────────

class PreFlightStep(PipelineStep):
    """上下文复杂度评分 + 工具推荐。"""

    name = "preflight"
    required = False

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        cfg = ctx.get("config")
        if not getattr(cfg, "preflight_enabled", True):
            ctx.set("preflight", None)
            ctx.set("collected_context", {})
            return ctx

        try:
            from astrbot_plugin_suli_context import ContextGatherer
        except ImportError:
            from .context_gatherer import ContextGatherer

        gctx = ctx.get("ctx")
        scheduler = ctx.get("scheduler")
        trigger_reason = ctx.get("trigger_reason", "")
        trigger_uid = ctx.get("trigger_user_id", "")

        try:
            preflight = ContextGatherer.analyze(
                ctx=gctx,
                trigger_reason=trigger_reason,
                trigger_user_id=trigger_uid,
                config=cfg,
            )
            ctx.set("preflight", preflight)

            # Phase 1b: 上下文收集 (低复杂度跳过)
            collected: dict[str, str] = {}
            if preflight.should_collect:
                collected = await ContextGatherer.collect(
                    preflight,
                    tavern=scheduler._tavern,
                    provider=scheduler._resolve_provider(),
                    config=cfg,
                    bot_id=getattr(scheduler, "_current_bot_id", "") or "",
                )
            ctx.set("collected_context", collected)
        except Exception:
            logger.debug("Pre-flight 分析异常", exc_info=True)
            ctx.set("preflight", None)
            ctx.set("collected_context", {})

        return ctx


# ── Step 3: 模型路由 ─────────────────────────────────────

class ModelRoutingStep(PipelineStep):
    """自适应模型路由: FLASH / PRO / OPUS。"""

    name = "model_routing"
    required = True

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        cfg = ctx.get("config")
        scheduler = ctx.get("scheduler")
        trigger_uid = ctx.get("trigger_user_id", "")
        preflight = ctx.get("preflight")
        challenge_info = ctx.get("challenge_info")
        gctx = ctx.get("ctx")

        try:
            from astrbot_plugin_suli_routing import ModelRouter
        except ImportError:
            from .model_router import ModelRouter

        # 触发用户消息
        _trigger_msg = ""
        if trigger_uid:
            for m in reversed(gctx.messages[-5:]):
                if str(m.get("user_id", "")) == trigger_uid:
                    _trigger_msg = str(m.get("content", ""))
                    break

        # 工具可用性
        _tools_avail = cfg.tool_calling_enabled
        if _tools_avail and trigger_uid and cfg.emotion_enabled:
            try:
                from astrbot_plugin_suli_emotion import can_use_tools
            except ImportError:
                from .emotion import can_use_tools
            if not can_use_tools(
                trigger_uid,
                admin_qq=cfg.super_admin_qq,
                self_id=getattr(scheduler, "_current_bot_id", "") or "",
            ):
                _tools_avail = False

        try:
            _tier = ModelRouter.decide_tier(
                trigger_reason=ctx.get("trigger_reason", ""),
                active_domains=gctx.active_domains or None,
                user_message=_trigger_msg,
                user_id=trigger_uid,
                admin_qq=cfg.super_admin_qq,
                challenge_verdict=(
                    challenge_info.get("verdict")
                    if challenge_info else None
                ),
                tools_enabled=_tools_avail,
                context_complexity=(
                    preflight.complexity_score if preflight else 0.0
                ),
                tool_chain_depth=(
                    preflight.tool_chain_depth if preflight else 0
                ),
                has_unresolved_images=(
                    preflight.has_unresolved_images if preflight else False
                ),
                user_affinity_level=(
                    preflight.trigger_user_affinity_level if preflight else 0
                ),
                active_domain_count=(
                    preflight.active_domain_count if preflight else 0
                ),
            )
            _route = ModelRouter.resolve(
                _tier,
                default_provider=scheduler._resolve_provider(),
            )
            ctx.set("model_route", _route)
            logger.info(
                "群 %d: 模型路由 %s → %s%s",
                gctx.group_id, _tier.name, _route.model,
                " (直连)" if _route.api_base else "",
            )
        except Exception:
            logger.debug("模型路由异常", exc_info=True)
            ctx.set("model_route", None)

        return ctx


# ── Step 4: 提示词构建 ───────────────────────────────────

class PromptBuildStep(PipelineStep):
    """构建 LLM messages (system + user)。"""

    name = "prompt_build"
    required = True

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        scheduler = ctx.get("scheduler")
        gctx = ctx.get("ctx")
        preflight = ctx.get("preflight")
        collected = ctx.get("collected_context", {})
        challenge_info = ctx.get("challenge_info")

        messages = scheduler._prompt_builder.build(
            ctx=gctx,
            challenge_info=challenge_info,
            trigger_reason=ctx.get("trigger_reason", ""),
            trigger_user_id=ctx.get("trigger_user_id", ""),
            preflight=preflight,
            collected_context=collected,
            judge_decision=ctx.get("judge_decision"),
        )

        ctx.set("messages", messages)
        return ctx


# ── Step 5: LLM 调用 ─────────────────────────────────────

class LLMCallStep(PipelineStep):
    """调用 LLM (含 function calling 工具循环)。"""

    name = "llm_call"
    required = True

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        scheduler = ctx.get("scheduler")
        messages = ctx.get("messages", [])
        if not messages:
            ctx.set("reply", PIPELINE_SILENCE)
            return ctx

        route = ctx.get("model_route")
        trigger_uid = ctx.get("trigger_user_id", "")

        _routing_model = route.model if route else ""
        _routing_extra = route.extra_params or None if route else None
        _routing_api_base = route.api_base if route else ""
        _routing_api_key = route.api_key if route else ""

        # 设置贴图上下文
        gctx = ctx.get("ctx")
        try:
            from ..sticker_sender import clear_sticker_context, set_sticker_context
        except ImportError:
            from sticker_sender import clear_sticker_context, set_sticker_context
        if gctx.last_bot and gctx.last_event:
            set_sticker_context(gctx.last_bot, gctx.last_event)

        # 设置记忆上下文
        try:
            from ..group_chat import _setup_memory_ctx
        except ImportError:
            from group_chat import _setup_memory_ctx
        _setup_memory_ctx(gctx, trigger_uid, bot_id=getattr(scheduler, "_current_bot_id", "") or "")

        try:
            reply = await scheduler._call_llm_with_tools(
                messages, user_id=trigger_uid,
                model=_routing_model,
                extra_params=_routing_extra,
                api_base=_routing_api_base,
                api_key=_routing_api_key,
            )
        finally:
            clear_sticker_context()
            try:
                from ..group_chat import clear_memory_context
            except ImportError:
                from group_chat import clear_memory_context
            clear_memory_context(getattr(scheduler, "_current_bot_id", "") or "")

        ctx.set("raw_reply", reply)
        ctx.set("reply", reply)
        return ctx


# ── Step 6: 后处理 ───────────────────────────────────────

class PostProcessStep(PipelineStep):
    """回复后处理: Markdown 清理 → 反臃肿 → 重复检测 → 静默 → 发送。"""

    name = "post_process"
    required = True

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        reply = ctx.get("reply", "")
        if not reply or reply == PIPELINE_SILENCE:
            return ctx

        gctx = ctx.get("ctx")
        cfg = ctx.get("config")
        trigger_uid = ctx.get("trigger_user_id", "")
        trigger_reason = ctx.get("trigger_reason", "")
        char_name = ctx.get("char_name", "暮恩")

        try:
            from ..group_chat import (
                get_recent_bot_replies,
                filter_narration,
                is_duplicate,
                sanitize_qq_reply,
            )
        except ImportError:
            from group_chat import (
                get_recent_bot_replies,
                filter_narration,
                is_duplicate,
                sanitize_qq_reply,
            )

        # 1. Markdown 清理
        reply = sanitize_qq_reply(reply)
        reply = (
            reply.strip()
            .strip('"').strip("'")
            .strip("「」").strip("【】")
            .strip()
        )

        # 2. 反臃肿过滤
        reply, nar_changes = filter_narration(reply)
        if nar_changes:
            logger.info("群 %d: 反臃肿过滤 %d 处修改", gctx.group_id, nar_changes)

        # 3. 重复检测
        recent_bot_replies = get_recent_bot_replies(
            gctx.messages, char_name, count=5,
        )
        if is_duplicate(reply, recent_bot_replies):
            logger.info(
                "群 %d: 回复被重复检测拦截 (%d 字)",
                gctx.group_id, len(reply),
            )
            ctx.set("reply", PIPELINE_SILENCE)
            return ctx

        # 4. 静默检查
        SILENCE_MARKER = "[静默]"
        if not reply or reply == SILENCE_MARKER:
            logger.info("群 %d: LLM 选择静默", gctx.group_id)
            ctx.set("reply", PIPELINE_SILENCE)
            return ctx

        # 5. 情感调制静默
        if cfg.emotion_enabled and trigger_uid:
            try:
                from astrbot_plugin_suli_emotion import get_user_relation
            except ImportError:
                from emotion import get_user_relation
            try:
                _sched = ctx.get("scheduler")
                _bot_id = getattr(_sched, "_current_bot_id", "") or "BOT_QQ_MAIN"
                rel = get_user_relation(trigger_uid, self_id=_bot_id, peer_bot_qq=cfg.peer_bot_qq)
                from astrbot_plugin_suli_emotion import get_global_mood
                global_mood = get_global_mood(_bot_id)
                silence_prob = 0.0
                if rel.affinity.level <= -1 and trigger_reason not in ("mention", "reply", "nickname"):
                    silence_prob += 0.50
                if global_mood.valence < -0.5:
                    silence_prob += 0.50
                elif global_mood.valence < -0.3:
                    silence_prob += 0.30
                if silence_prob > 0 and random.random() < min(silence_prob, 0.8):
                    logger.debug(
                        "群 %d: 情感调制静默 (affinity=Lv.%+d)",
                        gctx.group_id, rel.affinity.level,
                    )
                    ctx.set("reply", PIPELINE_SILENCE)
                    return ctx
            except Exception:
                pass

        ctx.set("reply", reply)
        return ctx


# ── Step 7: 发送回复 ────────────────────────────────────

class SendReplyStep(PipelineStep):
    """发送最终回复到群聊。"""

    name = "send_reply"
    required = False  # 发送失败不中断管线

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        reply = ctx.get("reply", "")
        if not reply or reply == PIPELINE_SILENCE:
            return ctx

        gctx = ctx.get("ctx")
        cfg = ctx.get("config")
        trigger_uid = ctx.get("trigger_user_id", "")
        trigger_reason = ctx.get("trigger_reason", "")
        trigger_user_name = ctx.get("trigger_user_name", "")
        char_name = ctx.get("char_name", "暮恩")
        scheduler = ctx.get("scheduler")

        # ── ★ 触发事件: 优先从 pipeline context 读取快照, fallback 到 gctx.last_event ──
        #     管线入口处应快照 trigger_event 存入 ctx("trigger_event"),
        #     防止 gctx.last_event 在管线执行期间被并发 on_message() 覆写
        #     (竞态窗口: 管线执行 3-10s, on_message 随时可能写入新事件)。
        if not gctx.last_bot:
            return ctx
        trigger_event = ctx.get("trigger_event") or gctx.last_event
        if not trigger_event:
            return ctx

        # 打字延迟
        try:
            delay = random.uniform(
                cfg.heat_reply_delay_min, cfg.heat_reply_delay_max,
            )
            if cfg.emotion_enabled and trigger_uid:
                try:
                    from astrbot_plugin_suli_emotion import get_global_mood
                    _sched = ctx.get("scheduler")
                    _bot_id = getattr(_sched, "_current_bot_id", "") or "BOT_QQ_MAIN"
                    global_mood = get_global_mood(_bot_id)
                    if global_mood.arousal > 0.5:
                        delay *= 0.5
                    elif global_mood.arousal < -0.4:
                        delay *= 1.5
                except Exception:
                    pass
            await asyncio.sleep(delay)
        except Exception:
            pass

        # 反推缓存 prepend
        try:
            from ..main import get_reverse_prompt_cache
        except ImportError:
            get_reverse_prompt_cache = None

        if get_reverse_prompt_cache:
            try:
                _sched = ctx.get("scheduler")
                _bid = getattr(_sched, "_current_bot_id", "") or "BOT_QQ_MAIN"
                _vlm_cached = get_reverse_prompt_cache(f"{_bid}:g{gctx.group_id}")
                if _vlm_cached:
                    reply = _vlm_cached + "\n\n---\n\n" + reply
            except Exception:
                pass

        # ── @提及转换: LLM 输出的 @名字 纯文本 → QQ [CQ:at,qq=...] ──
        try:
            from ..transport.reply_postprocessor import resolve_at_mentions
            _peer_qq = ""
            _peer_name = ""
            try:
                _sched = ctx.get("scheduler")
                _peer_qq = str(getattr(_sched, "_chat_param", lambda *a: "")(  # noqa
                    "peer_bot_qq", "peer_bot_qq",
                ) or "")
            except Exception:
                pass
            reply = resolve_at_mentions(
                reply,
                gctx.messages,
                bot_name=char_name,
                peer_bot_qq=_peer_qq,
                trigger_uid=trigger_uid,
                trigger_name=trigger_user_name,
            )
        except Exception:
            pass

        # ── 自动 @触发者: 直接触发时确保触发者收到 QQ 通知 ──
        if (
            trigger_uid
            and trigger_reason in ("mention", "nickname", "reply", "thread_continuation")
            and f"[CQ:at,qq={trigger_uid}]" not in reply
        ):
            _bot_qqs = set()
            try:
                from astrbot_plugin_suli_guards.dual_bot import get_bot_qq_set
                _bot_qqs = set(get_bot_qq_set())
            except Exception:
                pass
            _peer_qq2 = ""
            try:
                _sched = ctx.get("scheduler")
                _peer_qq2 = str(getattr(_sched, "_chat_param", lambda *a: "")(  # noqa
                    "peer_bot_qq", "peer_bot_qq",
                ) or "")
            except Exception:
                pass
            if trigger_uid not in _bot_qqs and trigger_uid != _peer_qq2:
                reply = f"[CQ:at,qq={trigger_uid}] {reply}"

        # ── QQ 引用: 直接触发时 reply_message=True 锚定原消息 ──
        _reply_message = trigger_reason in ("mention", "nickname", "reply",
                                            "thread_continuation")

        # 发送 (长回复分段)
        max_len = 800
        if len(reply) <= max_len:
            await gctx.last_bot.send(trigger_event, reply, reply_message=_reply_message)
        else:
            paras = reply.split("\n\n")
            buf = ""
            first = True
            for para in paras:
                if len(buf) + len(para) + 2 <= max_len:
                    buf = (buf + "\n\n" + para).strip()
                else:
                    if buf:
                        await gctx.last_bot.send(trigger_event, buf,
                                                  reply_message=(_reply_message and first))
                        first = False
                    buf = para
            if buf:
                await gctx.last_bot.send(trigger_event, buf,
                                          reply_message=(_reply_message and first))

        # 更新上下文
        gctx.add_message(f"bot_{char_name}", char_name, reply)
        gctx.last_reply_time = time.time()
        logger.info("群 %d: bot 发言 (%d 字)", gctx.group_id, len(reply))

        # Token 记录
        scheduler._record_usage(
            scenario="group_chat",
            group_id=str(gctx.group_id),
        )

        return ctx


# ── 管线工厂 ──────────────────────────────────────────────

def build_reply_pipeline(scheduler) -> Pipeline:
    """构建标准群聊回复管线。

    Args:
        scheduler: GroupChatScheduler 实例

    Returns:
        配置好的 Pipeline
    """
    return Pipeline("reply_pipeline", steps=[
        CrossValidationStep(),
        PreFlightStep(),
        ModelRoutingStep(),
        PromptBuildStep(),
        LLMCallStep(),
        PostProcessStep(),
        SendReplyStep(),
    ])
