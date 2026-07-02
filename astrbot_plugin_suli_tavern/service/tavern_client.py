"""LLM API 客户端 + 角色卡加载器。

所有 LLM 调用直连 OpenAI 兼容 API。
角色卡从 characters/ 目录加载 JSON 文件 (兼容 SillyTavern 角色卡格式)。
世界书从 characters/ 目录加载 JSON 文件 (兼容 SillyTavern lorebook 格式)。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# 角色卡目录
_CHAR_DIR = Path(__file__).resolve().parent.parent / "characters"


def _load_character_card(name: str = "") -> dict:
    """加载 JSON 角色卡文件。

    system_prompt 支持 txt 文件覆盖:
      如果 characters/{name}_persona_v2.txt 存在，
      system_prompt 从该 txt 文件读取（单一真相源），
      JSON 中的 system_prompt 字段被忽略。
      这样 txt 文件便于 diff / 编辑 / 审查，JSON 保留其余元数据。

    Args:
        name: 角色卡文件名 (不含扩展名)

    Returns:
        角色卡 data dict

    Raises:
        FileNotFoundError: 找不到角色卡 JSON 文件
        ValueError: 角色卡格式无效
    """
    path = _CHAR_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到角色卡文件: {path}")

    with open(path, encoding="utf-8") as f:
        card = json.load(f)

    data = card.get("data")
    if not data:
        raise ValueError(f"角色卡格式无效 (缺少 data 字段): {path}")

    # ── system_prompt txt 覆盖: 单一真相源 ──
    # 优先级: persona_v2.txt > JSON system_prompt
    persona_txt = _CHAR_DIR / f"{name}_persona_v2.txt"
    if persona_txt.exists():
        txt_content = persona_txt.read_text(encoding="utf-8").strip()
        if txt_content:
            data["system_prompt"] = txt_content
            logger.info(
                "角色卡 %s: system_prompt 已从 %s 加载 (%d 字符)",
                data.get("name"), persona_txt.name, len(txt_content),
            )

    logger.info("已加载角色卡: %s (v%s)", data.get("name"), data.get("character_version", "?"))
    return data


# ── 角色卡加载 (双 Bot 支持) ─────────────────────────────
#
# 配置方式 (优先级从高到低):
#   1. 环境变量 BOT_QQ_MAIN + BOT_CHAR_MAIN / BOT_QQ_ALT + BOT_CHAR_ALT (精确指定)
#   2. 自动扫描 characters/ 目录，按文件名排序依次分配给 QQ 号
#   3. 硬编码默认值 (通用回退)
import os as _os


def _builtin_fallback(name: str = "") -> dict:
    """最小后备角色卡 — 确保不会空启动。

    所有角色卡内容应从 JSON 文件加载。此函数仅提供最小骨架，
    当 JSON 文件完全不可用时兜底。
    """
    display_name = name.replace("_", " ").title()
    return {
        "name": display_name,
        "description": f"AI 助手 {display_name}",
        "system_prompt": f"你是{display_name}——一个通过 QQ 提供服务的 AI 助手。",
        "first_mes": "你好！有什么可以帮你的？",
        "personality": "",
        "scenario": "",
        "mes_example": "",
    }


def _discover_characters() -> dict[str, str]:
    """自动发现角色卡映射。

    优先级:
      1. BOT_CHAR_MAIN / BOT_CHAR_ALT 环境变量 (精确指定)
      2. 自动扫描 characters/ 目录，按文件名排序依次分配给 QQ 号
      3. 单角色卡 → 作为默认
    """
    mapping: dict[str, str] = {}
    main_qq = _os.getenv("BOT_QQ_MAIN", "")
    alt_qq = _os.getenv("BOT_QQ_ALT", "")
    main_char = _os.getenv("BOT_CHAR_MAIN", "")
    alt_char = _os.getenv("BOT_CHAR_ALT", "")

    if main_qq and main_char:
        mapping[main_qq] = main_char
    if alt_qq and alt_char:
        mapping[alt_qq] = alt_char

    discovered: list[str] = []
    if _CHAR_DIR.exists():
        for f in sorted(_CHAR_DIR.glob("*.json")):
            if f.stem.endswith("_world_book") or f.stem.startswith("example"):
                continue
            discovered.append(f.stem)

    qq_slots = [q for q in (main_qq, alt_qq) if q and q not in mapping]
    for i, card_name in enumerate(discovered):
        if i < len(qq_slots):
            mapping[qq_slots[i]] = card_name
        elif not mapping:
            mapping["default"] = card_name
            break
    return mapping


# ── 确定 bot QQ → 角色卡映射 ──
# 环境变量优先 → BotIdentityService (DB) → 自动扫描
_main_qq = _os.getenv("BOT_QQ_MAIN", "")
_alt_qq = _os.getenv("BOT_QQ_ALT", "")
_BOT_CARDS = _discover_characters()

_LOPUT_QQ = _main_qq or ""  # 向后兼容 (deprecated)
_LUNA_QQ = _alt_qq or ""     # 向后兼容 (deprecated)

CHARACTERS: dict[str, dict] = {}
if _BOT_CARDS:
    for _qq, _card_name in _BOT_CARDS.items():
        try:
            CHARACTERS[_qq] = _load_character_card(_card_name)
            logger.info("已加载角色卡: %s → QQ %s", _card_name, _qq)
        except (FileNotFoundError, ValueError) as e:
            logger.warning("加载角色卡 %s 失败: %s", _card_name, e)
            CHARACTERS[_qq] = _builtin_fallback(_card_name)
else:
    logger.info("无 BOT_CARDS 映射，将依赖 BotIdentityService 的角色卡加载")

DEFAULT_CHARACTER: dict = (
    CHARACTERS.get(_main_qq) or
    CHARACTERS.get(_alt_qq) or
    next(iter(CHARACTERS.values()), _builtin_fallback("character")) if CHARACTERS
    else _builtin_fallback("character")
)


def get_character_for_self_id(self_id: str) -> dict:
    """根据 bot 的 QQ 号 (self_id) 返回对应的角色卡。

    优先级:
      1. 已缓存的 CHARACTERS dict
      2. BotIdentityService → 角色卡文件名 → 加载
      3. DEFAULT_CHARACTER fallback

    Args:
        self_id: bot 的 QQ 号

    Returns:
        角色卡 data dict
    """
    sid = str(self_id)
    if sid in CHARACTERS:
        return CHARACTERS[sid]
    # 尝试从 BotIdentityService 解析角色卡
    try:
        from .bot_identity import get_bot_identity_service
        svc = get_bot_identity_service()
        card_name = svc.resolve_character_card(sid)
        if card_name:
            try:
                card = _load_character_card(card_name)
                CHARACTERS[sid] = card  # 缓存
                return card
            except Exception:
                pass
    except Exception:
        pass
    return DEFAULT_CHARACTER

# ── 世界书 (World Book / Lorebook) 加载 ───────────────────
# 委托给 context/world_book.py — 支持三模式触发 + 计时效果

from astrbot_plugin_suli_intelligence import (
    DEFAULT_SCAN_DEPTH,
    WorldBookEntry,
    load_world_book,
    scan_world_book_static,
)

_WB_ENTRIES: dict[str, list[WorldBookEntry]] = {}  # {bot_id: [WorldBookEntry]}
_WB_SCAN_DEPTH = DEFAULT_SCAN_DEPTH

# 模块加载时加载世界书 — 跟随角色卡映射
for _wb_qq, _wb_card_name in (_BOT_CARDS.items() if _BOT_CARDS else []):
    _wb_path = _CHAR_DIR / f"{_wb_card_name}_world_book.json"
    if _wb_path.exists():
        _WB_ENTRIES[_wb_qq] = load_world_book(str(_wb_path))
        logger.info("已加载世界书: %s → QQ %s (%d 条目)", _wb_path.name, _wb_qq, len(_WB_ENTRIES[_wb_qq]))


def _scan_world_book(messages: list[dict[str, str]], bot_id: str = "") -> list[str]:
    """扫描消息中的关键词，返回命中的世界书条目内容列表。

    委托给 context/world_book.py 的无状态扫描。
    用于私聊/角色扮演等不需要状态追踪的场景。
    群聊请使用 WorldBookBuffer (有状态: sticky/cooldown/delay)。
    """
    _entries = _WB_ENTRIES.get(str(bot_id), _WB_ENTRIES.get(_LOPUT_QQ, []))
    return scan_world_book_static(
        messages,
        entries=_entries,
        scan_depth=_WB_SCAN_DEPTH,
    )


class TavernClient:
    """LLM API 客户端 — 直连 OpenAI 兼容 API。

    所有 LLM 调用直连 API。角色卡/世界书按 JSON 格式加载。
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        # 最近一次 API 调用的 usage 数据 (调用方在 chat() 后读取)
        self._last_usage: dict[str, int] = {}

    def get_last_usage(self) -> dict[str, int]:
        """获取最近一次 API 调用的 token usage。

        Returns:
            {input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens, latency_ms}
        """
        return dict(self._last_usage)

    def _parse_and_store_usage(self, data: dict, latency_ms: int = 0) -> None:
        """从 API 响应中解析 token usage 并存储。

        兼容 DeepSeek (prompt_cache_hit/miss_tokens) 和
        OpenAI (仅 prompt/completion tokens) 两种格式。
        """
        usage = data.get("usage", {})
        if not usage:
            self._last_usage = {}
            return
        self._last_usage = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_hit_tokens": usage.get("prompt_cache_hit_tokens", 0),
            "cache_miss_tokens": usage.get(
                "prompt_cache_miss_tokens",
                usage.get("prompt_tokens", 0) - usage.get("prompt_cache_hit_tokens", 0),
            ),
            "latency_ms": latency_ms,
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector()
            self._session = aiohttp.ClientSession(
                connector=connector,
                trust_env=True,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── 凭证解析 ─────────────────────────────────────────

    @staticmethod
    def _resolve_credentials(
        api_base: str = "",
        api_key: str = "",
        bot_id: str = "",
    ) -> tuple[str, str]:
        """解析 API 凭证。显式参数优先，否则回退到指定 bot 的 LLM 配置。

        回退链: 显式参数 → bot_id 指定 → 全局默认 → 已知 bot QQ 号列表。
        """
        if api_base and api_key:
            return api_base, api_key
        try:
            from .bot_config import get_config_service
            svc = get_config_service()
            # 1. 按传入 bot_id
            if bot_id:
                active = svc.resolve_active_llm(bot_id)
                if active and active.api_key:
                    return active.normalized_base_url, active.api_key
            # 2. 全局默认
            active = svc.resolve_active_llm("")
            if active and active.api_key:
                return active.normalized_base_url, active.api_key
            # C3: 不再做跨 bot 凭证回退 — 每个 bot 应有独立配置或共享全局默认
            if bot_id:
                logger.warning("凭证解析失败: bot_id=%s 无独立配置且全局默认未设置", bot_id)
        except Exception:
            logger.warning("凭证回退解析失败", exc_info=True)
        return api_base, api_key

    # ── 公开 API ─────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str = "deepseek-v4-pro",
        temperature: float = 0.9,
        max_tokens: int = 512,
        provider: str = "",
        api_base: str = "",
        api_key: str = "",
        bot_id: str = "",
        extra_params: dict | None = None,
    ) -> str:
        """直连 OpenAI 兼容 API 生成回复 (无工具)。

        Args:
            messages: 消息列表
            model: 模型名
            temperature: 温度
            max_tokens: 最大 token 数
            provider: LLM provider (用于缓存策略)
            api_base: API 端点 (空=回退到指定 bot 的 LLM 配置)
            api_key: API 密钥 (空=回退)
            bot_id: bot QQ 号 (空=全局配置)

        Returns:
            回复文本

        Raises:
            RuntimeError: API 不可用或返回错误
        """
        from astrbot_plugin_suli_intelligence import optimize_messages
        optimized = optimize_messages(messages, provider) if provider else messages
        api_base, api_key = self._resolve_credentials(api_base, api_key, bot_id)
        if not api_base or not api_key:
            logger.warning(
                "chat(): 无法解析 API 凭证, 返回空回复 (bot_id=%r)", bot_id,
            )
            return ""

        # 复用 _chat_via_direct_api (不带 tools)
        result = await self._chat_via_direct_api(
            optimized=optimized,
            tools=[],
            tool_choice="none",
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_params=extra_params,
            api_base=api_base,
            api_key=api_key,
        )
        return result.get("content") or "(空回复)"

    async def chat_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        tool_choice: str = "auto",
        model: str = "deepseek-v4-pro",
        temperature: float = 0.8,
        max_tokens: int = 512,
        provider: str = "",
        extra_params: dict | None = None,
        api_base: str = "",
        api_key: str = "",
        bot_id: str = "",
    ) -> dict:
        """直连 OpenAI 兼容 API 生成回复 (支持 function calling)。

        Args:
            messages: 消息列表
            tools: OpenAI 格式的工具定义列表
            tool_choice: "auto" | "none" | "required"
            model: 模型名
            temperature: 温度
            max_tokens: 最大 token 数
            provider: LLM provider (用于缓存策略)
            extra_params: 额外 API 参数 (如 {"reasoning_effort": "xhigh"})
            api_base: API 端点 (空=回退到指定 bot 的 LLM 配置)
            api_key: API 密钥 (空=回退)
            bot_id: bot QQ 号 (空=全局配置)

        Returns:
            {"content": str | None, "tool_calls": list[dict] | None}

        Raises:
            RuntimeError: API 不可用或返回错误
        """
        from astrbot_plugin_suli_intelligence import optimize_messages
        optimized = optimize_messages(messages, provider) if provider else messages
        api_base, api_key = self._resolve_credentials(api_base, api_key, bot_id)
        if not api_base or not api_key:
            logger.warning(
                "chat_with_tools(): 无法解析 API 凭证, 返回空 (bot_id=%r)", bot_id,
            )
            return {"content": None, "tool_calls": None}

        return await self._chat_via_direct_api(
            optimized=optimized,
            tools=tools,
            tool_choice=tool_choice,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_params=extra_params,
            api_base=api_base,
            api_key=api_key,
        )

    async def _chat_via_direct_api(
        self,
        optimized: list[dict[str, str]],
        tools: list[dict],
        tool_choice: str,
        model: str,
        temperature: float,
        max_tokens: int,
        extra_params: dict | None,
        api_base: str,
        api_key: str,
    ) -> dict:
        """绕过酒馆，直接调用 OpenAI 兼容 API (三方代理)。"""
        # ── 动态 max_tokens 双护栏: 地板 + 天花板 ──
        #     地板: 输入长时确保输出预算不枯竭 (TRAPS §八#7 旧修复)
        #     天花板: 输入+输出不超模型上下文窗口 (TRAPS §八#7 延续)
        #     DeepSeek v4: flash/pro 均为 128K 上下文 (之前按 v3 flash 32K 保守估计,
        #     导致 40K chars 输入时天花板压制到 512 → finish=length + 空 content)
        #     估算: 中文 ~1 tok/char, 10% 元数据开销
        _input_chars = sum(
            len(m.get("content") or "") for m in optimized
        )
        # 地板: 取 max_tokens 和 输入*0.15 的较大值, 上限按 reasoning_effort 调整
        #     CoT 思考 token 计入 max_tokens — 需要更大的地板才能容纳思考+输出
        _effort = extra_params.get("reasoning_effort") if extra_params else None
        _floor_cap = 8192 if _effort in ("high", "max", "xhigh") else 4096
        _min_tokens = min(max(256, max_tokens, int(_input_chars * 0.15)), _floor_cap)
        _safe_max_tokens = _min_tokens

        # ── 天花板: 确保 estimated_input + max_tokens ≤ 模型上下文 ──
        _est_input_tokens = int(_input_chars * 1.1)  # 10% 元数据开销
        _ctx_ceiling = 131072  # DeepSeek v4 pro/flash 均为 128K 上下文
        _max_safe_output = max(512, _ctx_ceiling - _est_input_tokens)
        _safe_max_tokens = min(_safe_max_tokens, _max_safe_output)
        if _safe_max_tokens < max_tokens:
            logger.debug(
                "max_tokens 从 %d 天花板压制到 %d (输入估算=%d, 模型=%s, 上下文上限=%d)",
                max_tokens, _safe_max_tokens, _est_input_tokens, model, _ctx_ceiling,
            )

        # ── reasoning_effort 保护: 输出预算不足时自动剥离 ──
        #    推理模型 (v4-pro/flash with reasoning_effort) 的 CoT 思考 token
        #    计入 max_tokens。上下文接近满载时 max_tokens 被天花板压制,
        #    CoT 吃掉全部预算 → finish=length + 空 content (TRAPS §八#7 延续)。
        #    剥离 reasoning_effort — 思考换不来输出就没意义了。
        #    CoT 开销: medium≈1500 tokens, high+≈3000+ tokens
        _reasoning_stripped = False
        _protection_threshold = 2048  # 默认: 至少留 2048 给输出
        if _effort == "medium":
            _protection_threshold = 3072  # CoT 1500 + 输出 1500
        elif _effort in ("high", "max", "xhigh"):
            _protection_threshold = 5120  # CoT 3000 + 输出 2000
        if (
            _safe_max_tokens < _protection_threshold
            and _effort
        ):
            _stripped_effort = extra_params.get("reasoning_effort")
            extra_params = {k: v for k, v in extra_params.items() if k != "reasoning_effort"}
            _reasoning_stripped = True
            logger.warning(
                "reasoning 预算保护: 剥离 reasoning_effort=%s (输出预算=%d < %d, "
                "输入估算=%d, 模型=%s)",
                _stripped_effort, _safe_max_tokens, _protection_threshold, _est_input_tokens, model,
            )

        url = api_base.rstrip("/")
        if not url.endswith("/v1"):
            url += "/v1"
        url += "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        payload: dict = {
            "model": model,
            "messages": optimized,
            "temperature": temperature,
            "max_tokens": _safe_max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if extra_params:
            payload.update(extra_params)

        session = await self._get_session()
        _start = time.time()
        try:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(
                        f"直连 API 返回 {resp.status}: {text[:300]}"
                    )

                data = await resp.json()
                choices = data.get("choices", [])
                if not choices:
                    raise RuntimeError("直连 API 返回空 choices")

                self._parse_and_store_usage(data, int((time.time() - _start) * 1000))

                msg = choices[0].get("message", {})
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls", None)

                if not content and not tool_calls:
                    logger.warning(
                        "直连 API 返回空 content 且无 tool_calls (model=%s, finish=%s)",
                        model, choices[0].get("finish_reason", "?"),
                    )

                logger.warning(
                    "直连 API 成功: model=%s tokens(in=%d/cache=%d out=%d) content_len=%d tool_calls=%d finish=%s",
                    model,
                    self._last_usage.get("input_tokens", 0),
                    self._last_usage.get("cache_hit_tokens", 0),
                    self._last_usage.get("output_tokens", 0),
                    len(content or ""),
                    len(tool_calls) if tool_calls else 0,
                    choices[0].get("finish_reason", "?"),
                )
                return {
                    "content": content.strip() if content else None,
                    "tool_calls": tool_calls or None,
                    "finish_reason": choices[0].get("finish_reason", ""),
                }

        except aiohttp.ClientError as e:
            raise RuntimeError(f"无法连接直连 API ({url}): {e}") from e

    @staticmethod
    def build_messages(
        character: dict | None = None,
        history: list[dict[str, str]] | None = None,
        user_message: str = "",
        user_id: str = "",
        super_admin_qq: int | None = None,
        chat_summary: str = "",
        memory_hints: str = "",
        reasoning_hints: str = "",
        emotion_hints: str = "",
        experience_hints: str = "",
    ) -> list[dict[str, str]]:
        """构建完整的消息列表 (system + history + user)。

        使用角色卡中的 system_prompt 字段作为系统提示。
        如果角色卡有 mes_example，将拼入 system prompt 作为示例。

        {{user}} 占位符会根据 user_id 动态解析:
          - super_admin_qq → 「主人」
          - 其他用户 → 「小可爱」
          - user_id 为空或 super_admin_qq 为 None → 默认「主人」(向后兼容)

        Args:
            character: 角色卡 data dict, 含 name/description/system_prompt/personality/
                      scenario/first_mes/mes_example
            history: 历史消息列表
            user_message: 当前用户消息
            user_id: 当前用户 QQ 号 (用于决定称呼)
            super_admin_qq: 超级管理员 QQ 号
            chat_summary: 早期对话的 LLM 压缩摘要 (注入 system prompt 末尾)

        Returns:
            完整的 messages 列表
        """
        char = character or DEFAULT_CHARACTER
        char_name = char.get("name", "AI")

        # 解析用户称呼: 超级管理员 → 主人, 其他 → 小可爱
        if user_id and super_admin_qq is not None:
            user_title = "主人" if user_id == str(super_admin_qq) else "小可爱"
        else:
            user_title = "主人"  # 向后兼容: 未传参数时保持旧行为

        # 构建系统提示 — 优先使用角色卡的 system_prompt
        system_text = char.get("system_prompt", "")
        if not system_text:
            # 后备: 用旧格式拼接
            system_text = (
                f"[角色: {char_name}]\n"
                f"{char.get('description', '')}\n"
                f"请完全以 {char_name} 的身份回复，不要扮演其他角色。"
                f"不要使用 emoji，保持简洁。"
            )

        # 替换系统提示中的 {{user}} / {{char}} 占位符
        system_text = system_text.replace("{{user}}", user_title)
        system_text = system_text.replace("{{char}}", char_name)

        # ── 角色上下文注入 (对齐 SillyTavern 原生 prompt 构建) ──
        # description + personality + scenario 在 ST 中由 prompt template 注入
        # 我们的 API 模式需要手动拼接。放在 system_prompt 之前作为角色定义。
        context_parts: list[str] = []

        desc = char.get("description", "")
        if desc:
            desc = desc.replace("{{char}}", char_name).replace("{{user}}", user_title)
            context_parts.append(f"[角色外观与背景]\n{desc}")

        pers = char.get("personality", "")
        if pers:
            pers = pers.replace("{{char}}", char_name).replace("{{user}}", user_title)
            context_parts.append(f"[角色性格]\n{pers}")

        scenario = char.get("scenario", "")
        if scenario:
            scenario = scenario.replace("{{char}}", char_name).replace("{{user}}", user_title)
            context_parts.append(f"[当前场景]\n{scenario}")

        if context_parts:
            system_text = "\n\n".join(context_parts) + "\n\n" + system_text

        # 如果角色卡有示例对话，替换占位符后拼入
        mes_example = char.get("mes_example", "")
        if mes_example:
            # 替换 {{char}} → 角色名, {{user}} → 用户称呼
            mes_example = mes_example.replace("{{char}}", char_name)
            mes_example = mes_example.replace("{{user}}", user_title)
            # 清理 <START> 分隔符（在 API 模式下不需要）
            mes_example = mes_example.replace("<START>", "")
            system_text += f"\n\n[对话示例 — 请严格模仿此风格回复]\n{mes_example}"

        # 附加行为准则提示（也替换占位符）
        post_instructions = char.get("post_history_instructions", "")
        if post_instructions:
            post_instructions = post_instructions.replace("{{user}}", user_title)
            system_text += f"\n\n{post_instructions}"

        # 注入对话摘要 (上下文压缩) — 放在 system prompt 末尾 (动态段)
        if chat_summary:
            system_text += f"\n\n[你们之前的对话摘要]\n{chat_summary}"

        # 注入用户记忆 — 放在 system prompt 末尾 (动态段)
        if memory_hints:
            system_text += f"\n\n[你对{user_title}的了解]\n{memory_hints}"

        # 注入 bot 自传体经历记忆 — 主语: bot 自己经历过什么
        if experience_hints:
            system_text += f"\n\n[你的相关经历]\n{experience_hints}"

        # 注入情感状态 — 好感+短期情绪 (动态段)
        if emotion_hints:
            system_text += f"\n\n{emotion_hints}"

        # 注入深度思考指令 — 放在 system prompt 末尾 (动态段)
        if reasoning_hints:
            system_text += f"\n\n{reasoning_hints}"

        # ── 表情系统注入 (共享 AstrBot meme_manager 表情库) ──
        try:
            from ...lport_meme import get_prompt_injection
            meme_prompt = get_prompt_injection()
            if meme_prompt:
                system_text += f"\n\n{meme_prompt.strip()}"
        except Exception:
            pass  # lport_meme 插件未安装，静默跳过

        messages = [{"role": "system", "content": system_text}]
        if history:
            messages.extend(history)
        if user_message:
            messages.append({"role": "user", "content": user_message})

        # ── 世界书注入 ──
        # 扫描已构建的消息列表，命中关键词的条目作为动态 system 消息注入
        # 放在主 system prompt 之后、history 之前 (after_character 位置)
        wb_entries = _scan_world_book(messages)
        if wb_entries:
            wb_text = "[附加背景 — 这些信息在刚才的对话中被触发，你应该自然地融入回复中]\n\n"
            wb_text += "\n\n".join(
                e.replace("{{user}}", user_title).replace("{{char}}", char_name)
                for e in wb_entries
            )
            # 插入到 system (index 0) 之后，history 之前
            messages.insert(1, {"role": "system", "content": wb_text})

        return messages

    @staticmethod
    def get_available_characters() -> list[dict]:
        """扫描角色卡目录，返回可用角色列表。

        Returns:
            [{name, display_name, version, path}, ...]
        """
        if not _CHAR_DIR.exists():
            return []
        chars = []
        for f in sorted(_CHAR_DIR.glob("*.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    card = json.load(fh)
                data = card.get("data", {})
                chars.append({
                    "name": f.stem,
                    "display_name": data.get("name", f.stem),
                    "version": data.get("character_version", "?"),
                    "path": str(f),
                })
            except Exception:
                chars.append({"name": f.stem, "display_name": f.stem, "version": "?", "path": str(f)})
        return chars
