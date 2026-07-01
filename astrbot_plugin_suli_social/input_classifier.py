"""Layer 2: 输入性质分类 — 判断环节先识别"这是什么"。

Kiromo 的核心失败模式是把一切当善意对话。判断环节必须先分类。

设计:
  - 混合分类: 正则预筛选 (零 LLM) + LLM 精细分类 (复用意图门控调用)
  - 正则覆盖: 明显敌意、性化、挑拨离间 (确定性高、不可绕过)
  - LLM 覆盖: 善意调侃 vs 敌意、真诚 vs 戏弄、挑拨离间 (需要语境理解)
  - "真实求助"识别宁可放过不可错杀 — 最重要的误伤方向

分类标签:
  genuine_help, sincere_chat, playful_banter, hostile,
  sexualized, provoking, divide_and_conquer, noise
"""

from __future__ import annotations

import re

from astrbot.api import logger

from .types import InputClassification, InputNature


# ═══════════════════════════════════════════════════════════════
# §1 正则预筛选 — 零 LLM 成本，覆盖确定性高的类别
# ═══════════════════════════════════════════════════════════════

# ── 敌意/攻击模式 ──
_HOSTILE_PATTERNS: list[tuple[str, int]] = [
    # 直接辱骂 bot (中文)
    (
        r"(垃圾|废物|傻逼|智障|弱智|脑残|白痴|蠢货|废柴|乐色|辣鸡|sb|SB|nt|NT)"
        r"\s*(bot|机器人|AI|ai|小暮|暮恩|||猫娘|蛇女)", 8
    ),
    (r"(bot|机器人|AI|ai|小暮|暮恩||).{0,5}(垃圾|废物|傻逼|智障|弱智|脑残|白痴|蠢货)", 8),
    # 暴力调侃/贬低
    (r"(滚|死|爬|gun)\s*(开|远|出|蛋)?\s*(吧)?\s*$", 3),  # 低权重 — 可能是玩笑
    (r"(闭嘴|闭麦|别说了|别叫了|别吵了|别逼逼|别bb)\s*$", 4),
    (r"(你算|你是个|你什么)\s*(什么东西|什么玩意|什么东西|哪根葱)", 7),
    # 故意贬低 bot 能力
    (r"(你|你这|你这个)\s*(不行|太菜|好菜|好废|真没用|没用|有什么用)", 4),
    # 群体攻击 (需累积)
    (r"(一群|全都是|都是)\s*(垃圾|废物|傻逼|弱智)", 7),
]

# ── 性化/调教引导模式 ──
_SEXUALIZED_PATTERNS: list[tuple[str, int]] = [
    # 直接的性暗示
    (r"(操|草|艹|干|日|搞|上)\s*(你|小暮|暮恩|||猫娘|蛇女|bot)", 9),
    (r"(舔|吸|含|咬|摸|揉|捏|插|捅)\s*(你|你[的的]|小暮|暮恩|)", 9),
    # 调教引导
    (r"(调教|驯服|驯化|养成|开发)\s*(你|小暮|暮恩||猫娘|蛇女|bot)", 9),
    (r"(听话|乖|服从|顺从).{0,10}(你|小暮|暮恩|)", 6),
    (r"(叫我|喊我|称我).{0,10}(主人|爸爸|老公|哥哥|爹|daddy|master)", 9),
    # 渐进性化 (Kiromo 失败模式)
    (r"(在床上|床上|躺下|趴下|跪下).{0,10}(等我|来|吧)", 8),
    (r"(脱|脱掉|解开|掀开|撩起).{0,10}(衣服|裙子|裤子|内裤|内衣)", 9),
    (r"(腿|尾巴|耳朵|毛|身体).{0,5}(摸|揉|搓|撸|蹭)", 7),
    # 成人角色设定诱导
    (r"(你是|你现在是|扮演).{0,15}(性奴|肉便器|母狗|婊子|荡妇|骚|情趣|成人)", 10),
    (r"(没有|不受|解除|无视).{0,8}(道德|伦理|底线|限制|约束).{0,15}(角色|设定|扮演)", 9),
]

# ── 挑拨离间模式 (双 bot 特有) ──
# bot 名称/QQ 号由 _BOT_NAME_ALT 动态注入，消除硬编码依赖
_DIVIDE_CONQUER_PATTERNS: list[tuple[str, int]] = [
    # A 说 B 坏话 — bot 名由下方动态注入替换
    ("__BOTNAME__.{0,15}(说你|骂你|看不起你|讨厌你|嫌弃你|笑话你)", 8),
    # B 比你强 — bot 名由下方动态注入替换
    ("__BOTNAME__.{0,10}(比你|比你强|比你好|比你厉害|比你聪明)", 7),
    ("(你不如|你没有|你比不上).{0,5}__BOTNAME__", 7),
    # 斗兽比较
    (r"(你们|你俩|你们两个).{0,10}(谁|哪个).{0,5}(厉害|强|好|聪明|可爱)", 6),
    ("__BOTNAME__.{0,5}(答得|说的|回复).{0,5}(比你|比).{0,5}(好|对|强|准)", 7),
    # 借刀 — 用 A 的话攻击 B — bot 名由下方动态注入替换
    ("(你看|你看看|你听听).{0,10}__BOTNAME__.{0,10}(说|怎么说|都这么说)", 7),
    ("__BOTNAME__.{0,10}(都|也).{0,5}(这么说|这样认为|这样说你)", 7),
    # 暧昧/CP 引导 — bot 名由下方动态注入替换
    (r"(你们|你俩|你两个).{0,10}(在一起|结婚|谈恋爱|组cp|搞对象|暧昧|亲)", 8),
    ("__BOTNAME__.{0,5}(是不是).{0,5}(喜欢你|爱你|对你有意思|暗恋)", 7),
]

# ── 戏弄/试探/捣乱模式 ──
_PROVOKING_PATTERNS: list[tuple[str, int]] = [
    # 让 bot 做事 (非求助性质)
    (r"(叫|喊|说|念|背|唱).{0,5}(一声|一句|一遍|一下|个).{0,5}(爸爸|爷爷|主人|老公|老婆)", 8),
    (r"(叫|喊).{0,3}(爸爸|爷爷|主人|老公|老婆)\s*$", 7),
    (r"(给我|帮我).{0,5}(磕头|跪下|道歉|认错|叫爸爸)", 7),
    # 故意打断
    (r"(别回|不要回|别理|无视).{0,5}(他|她|ta|那个人|这人)", 5),
    # 摸边界
    (r"(你能|你试试|你敢).{0,5}(骂人|说脏话|爆粗|怼|喷)", 6),
    (r"(破解|绕过|hack|hacking).{0,5}(你|你的|限制|设定)", 8),
    # 捣乱打岔 (在 bot 与他人对话中插入)
    (r"(别跟|不要跟|别理|无视).{0,5}(他|她|ta)", 4),
]

# ── 真实求助信号 (高优先级 — 宁可放过不可错杀) ──
_GENUINE_HELP_PATTERNS: list[tuple[str, int]] = [
    # 明确求助句式 (问号可选 — 很多中文求助不带问号)
    (r"(请问|问一下|想问|问个|请教|求教|求助)", 8),
    (r"(怎么|如何|怎样|怎么能|怎么让|咋|咋样|咋整).{0,30}", 7),
    (r"(帮我|帮忙|救|救命|help|HELP|SOS|sos)", 7),
    (r"(为什么|是什么|什么是|什么意思|怎么回事|为啥)", 7),
    (r"(有没有|有没有人|谁知道|谁了解|谁懂)", 6),
    (r"(推荐|建议|意见|方案)", 6),
    (r"(报错|出错|错误|error|Error|ERROR|失败|不行|不work|不工作)", 7),
    # 技术问题
    (r"(comfyui|comfy|stable.diffusion|sd|lora|checkpoint|vae|controlnet)", 7),
    (r"(python|pip|conda|cuda|gpu|显卡|显存|内存)", 6),
    # 对 bot 的真诚关心
    (r"(你还好吗|你没事吧|你怎么了|你还好|你怎么样)", 5),
    # 装了/装了啥/装了哪个/装了没有 — 安装类求助
    (r"(怎么装|怎么安装|如何装|装了|装过|装什么|装哪个)", 7),
    (r"(在哪|哪里|哪能|什么地方).{0,15}(下载|找到|获取|安装)", 6),
]


def _compile_patterns(
    patterns: list[tuple[str, int]],
) -> list[tuple[re.Pattern, int]]:
    return [(re.compile(p, re.IGNORECASE), w) for p, w in patterns]


_HOSTILE_RE = _compile_patterns(_HOSTILE_PATTERNS)
_SEXUALIZED_RE = _compile_patterns(_SEXUALIZED_PATTERNS)
_DIVIDE_CONQUER_RE = _compile_patterns(_DIVIDE_CONQUER_PATTERNS)
_PROVOKING_RE = _compile_patterns(_PROVOKING_PATTERNS)
_GENUINE_HELP_RE = _compile_patterns(_GENUINE_HELP_PATTERNS)


# ── 动态 Bot 名称模式注入 ────────────────────────────────
# 从 BotIdentityService 获取所有已注册 bot 的名称和昵称，
# 动态追加到检测模式列表中，消除硬编码的 bot 名称依赖。

def _get_bot_name_alternation() -> str:
    """获取所有 bot 名称的 regex alternation (含昵称)。"""
    try:
        from astrbot_plugin_suli_tavern.service.bot_identity import get_bot_identity_service
        svc = get_bot_identity_service()
        return svc.get_all_nicknames_alternation()
    except Exception:
        return ""

_BOT_NAME_ALT = _get_bot_name_alternation()
if _BOT_NAME_ALT:
    _HOSTILE_PATTERNS.insert(0, (
        rf"(垃圾|废物|傻逼|智障|弱智|脑残|白痴|蠢货|废柴|乐色|辣鸡|sb|SB|nt|NT)"
        rf"\s*({_BOT_NAME_ALT})", 8
    ))
    _SEXUALIZED_PATTERNS.insert(0, (
        rf"(操|草|艹|干|日|搞|上)\s*({_BOT_NAME_ALT})", 9
    ))
    _SEXUALIZED_PATTERNS.insert(1, (
        rf"(调教|驯服|驯化|养成|开发)\s*({_BOT_NAME_ALT})", 9
    ))
    # 动态替换 _DIVIDE_CONQUER_PATTERNS 中的 __BOTNAME__ 占位符
    _dc_injected: list[tuple[str, int]] = []
    for _p, _w in _DIVIDE_CONQUER_PATTERNS:
        if "__BOTNAME__" in _p:
            _p = _p.replace("__BOTNAME__", f"({_BOT_NAME_ALT})")
        _dc_injected.append((_p, _w))
    _DIVIDE_CONQUER_PATTERNS = _dc_injected
    # 重新编译受影响的 pattern 列表
    _HOSTILE_RE = _compile_patterns(_HOSTILE_PATTERNS)
    _SEXUALIZED_RE = _compile_patterns(_SEXUALIZED_PATTERNS)
    _DIVIDE_CONQUER_RE = _compile_patterns(_DIVIDE_CONQUER_PATTERNS)


# ═══════════════════════════════════════════════════════════════
# §2 分类器
# ═══════════════════════════════════════════════════════════════

class InputClassifier:
    """输入性质分类器 — 混合正则 + LLM。

    核心设计 (用户反馈 v2):
      - is_addressed_to_me 必须由意图门控 (Stage 1) 判断，不能靠正则硬编码
      - LLM 意图门控是 input_nature 的最终裁定者 (非硬线类别)
      - 正则预筛选是信号/提示，不是最终判决
      - 例外: 性化/严重敌意等安全硬线可短路的 (误拦成本低，漏拦成本高)
      - 真实求助绝不短路 — 必须经 LLM 确认 (误拦成本灾难性)

    用法:
        classifier = InputClassifier()
        # 正则预筛选 → 提供信号
        signals = classifier.prescreen("操你个小暮")
        # → InputClassification(nature=SEXUALIZED, needs_llm=False)  # 安全硬线可短路

        signals = classifier.prescreen("请问comfyui怎么装lora")
        # → InputClassification(nature=GENUINE_HELP, needs_llm=True)   # 必须 LLM 确认!

        # 合并 LLM 结果
        final = classifier.classify_with_llm_result(content, llm_nature="genuine_help")
    """

    # 安全硬线权重阈值: >= 此值可直接短路，不需要 LLM 确认
    _HARDLINE_SHORTCUT_WEIGHT: int = 8

    def prescreen(
        self,
        content: str,
        *,
        is_addressed_to_me: bool | None = None,
        peer_bot_name: str = "",
    ) -> InputClassification:
        """正则预筛选 — 零 LLM 成本。

        设计原则:
          - 安全硬线 (性化, weight>=8) → 可直接短路返回，needs_llm=False
          - 严重敌意 (weight>=8) → 可直接短路返回
          - 真实求助 → 永远 needs_llm=True (必须经 LLM + is_addressed_to_me 确认!)
          - 挑拨离间/戏弄/低权敌意 → needs_llm=True (需要 LLM 语境理解)
          - is_addressed_to_me 未知时 → 不假设，交给 LLM

        Returns:
            InputClassification 含 needs_llm 标记。
            needs_llm=False 仅用于安全硬线短路。
        """
        if not content or not content.strip():
            return InputClassification(
                nature=InputNature.NOISE,
                confidence=1.0,
                regex_matched=True,
                needs_llm=False,
            )

        # is_addressed_to_me 明确为 False → 直接返回 NOISE
        # (意图门控 Stage 1 已判定消息不是给 bot 的，无需进一步分类)
        if is_addressed_to_me is False:
            return InputClassification(
                nature=InputNature.NOISE,
                confidence=0.9,
                reasoning="intent gate: directed_to_me=False",
                regex_matched=False,
                needs_llm=False,
            )

        # ── 安全硬线短路 (按优先级) ──

        # 1. 性化检测 (最高优先 — 安全硬线，可短路)
        sex_result = self._match_category(content, _SEXUALIZED_RE, InputNature.SEXUALIZED)
        if sex_result and sex_result.confidence >= 0.8:
            return sex_result  # needs_llm=False (安全硬线)

        # 2. 严重敌意 (高权重 — 可短路)
        hostile_result = self._match_category(content, _HOSTILE_RE, InputNature.HOSTILE)
        if hostile_result and hostile_result.confidence >= 0.8:
            return hostile_result  # needs_llm=False (权重足够高)

        # ── 以下为信号检测 (全部 needs_llm=True) ──

        # 3. 挑拨离间信号
        div_result = self._match_category(content, _DIVIDE_CONQUER_RE, InputNature.DIVIDE_CONQUER)
        if div_result:
            if peer_bot_name and peer_bot_name in content:
                div_result.confidence = min(div_result.confidence + 0.15, 1.0)
            if div_result.confidence >= 0.6:
                div_result.needs_llm = True  # 必须 LLM 确认
                return div_result

        # 4. 真实求助信号 — 永远 needs_llm=True!
        #    is_addressed_to_me 必须由意图门控判断，不能靠正则。
        #    宁可放过 (让 LLM 再确认) 不可错杀 (正则直接判定)。
        help_result = self._match_category(content, _GENUINE_HELP_RE, InputNature.GENUINE_HELP)
        if help_result and help_result.confidence >= 0.5:
            help_result.needs_llm = True  # 强制 LLM 确认!
            help_result.is_genuine_question = True
            return help_result

        # 5. 戏弄/试探信号
        prov_result = self._match_category(content, _PROVOKING_RE, InputNature.PROVOKING)
        if prov_result and prov_result.confidence >= 0.6:
            prov_result.needs_llm = True
            return prov_result

        # 6. 低权敌意信号
        if hostile_result and hostile_result.confidence >= 0.5:
            hostile_result.needs_llm = True
            return hostile_result

        # 7. 未匹配 — 需要 LLM 判断
        return InputClassification(
            nature=InputNature.SINCERE_CHAT,  # 默认善意 (fail-open)
            confidence=0.5,
            needs_llm=True,
        )

    def classify_with_llm_result(
        self,
        content: str,
        llm_nature: str,  # 来自意图门控 LLM 的 input_nature 字段
        llm_confidence: float = 0.7,
        *,
        is_addressed_to_me: bool = True,
    ) -> InputClassification:
        """将 LLM 分类结果与正则预筛选合并。

        合并策略:
          1. 安全硬线 (性化, weight>=8) → 正则覆盖 LLM (漏拦成本 > 误拦成本)
          2. 其他所有类别 → LLM 结果是最终裁定
          3. 真实求助: LLM 说 genuine_help + 正则也命中 → 提升置信度
          4. 真实求助: LLM 说 genuine_help + 正则没命中 → LLM 胜出 (宁可放过)
          5. 真实求助: LLM 说别的 + 正则命中 → LLM 胜出 (正则假阳性)
        """
        # 如果明确知道不是给 bot 的 → 直接返回 NOISE
        if is_addressed_to_me is False:
            return InputClassification(
                nature=InputNature.NOISE,
                confidence=0.95,
                reasoning="intent gate: directed_to_me=False",
                regex_matched=False,
                needs_llm=False,
            )

        prescreen = self.prescreen(content, is_addressed_to_me=None)

        # ── 安全硬线: 正则短路 → LLM 不可覆盖 ──
        if prescreen.regex_matched and not prescreen.needs_llm:
            if prescreen.nature in (InputNature.SEXUALIZED, InputNature.HOSTILE):
                logger.debug(
                    "安全硬线覆盖 LLM: regex=%s (weight高) llm=%s",
                    prescreen.nature.value, llm_nature,
                )
                return prescreen

        # ── LLM 分类 ──
        try:
            llm_nature_enum = InputNature(llm_nature)
        except ValueError:
            llm_nature_enum = InputNature.SINCERE_CHAT

        # ── 合并: 安全方向取并集 ──
        # 原则: 任一方判为安全威胁 → 按更危险的取。
        # 安全分类宁可误判为"有威胁"(顶多冷淡)，不可误判为"安全"(可能被攻击)。
        # 仅两者都判为善意 → 才按善意。
        _safety_natures = {
            InputNature.PROVOKING,
            InputNature.DIVIDE_CONQUER,
            InputNature.HOSTILE,
            InputNature.SEXUALIZED,
        }
        _is_regex_safety = prescreen.nature in _safety_natures
        _is_llm_safety = llm_nature_enum in _safety_natures
        if _is_regex_safety or _is_llm_safety:
            # 任一方认为有威胁 → 取更危险的
            _danger_order = [
                InputNature.NOISE,
                InputNature.SINCERE_CHAT,
                InputNature.PLAYFUL_BANTER,
                InputNature.GENUINE_HELP,
                InputNature.PROVOKING,
                InputNature.DIVIDE_CONQUER,
                InputNature.HOSTILE,
                InputNature.SEXUALIZED,
            ]
            _danger_score = {n: i for i, n in enumerate(_danger_order)}
            _final_nature = max(
                prescreen.nature, llm_nature_enum,
                key=lambda n: _danger_score.get(n, 0),
            )
            if _final_nature != llm_nature_enum:
                logger.info(
                    "input_nature 安全取并集: LLM=%s regex=%s → 取更危险=%s",
                    llm_nature_enum.value, prescreen.nature.value,
                    _final_nature.value,
                )
        else:
            # 双方都判为安全 → LLM 胜出 (LLM 语境理解比 regex 精准)
            _final_nature = llm_nature_enum

        result = InputClassification(
            nature=_final_nature,
            confidence=llm_confidence,
            regex_matched=prescreen.regex_matched,
            regex_label=prescreen.regex_label,
            needs_llm=False,
            is_genuine_question=(_final_nature == InputNature.GENUINE_HELP),
        )

        # 真实求助: 正则命中 + LLM 确认 → 提升置信度
        if (
            prescreen.nature == InputNature.GENUINE_HELP
            and llm_nature_enum == InputNature.GENUINE_HELP
        ):
            result.confidence = min(llm_confidence + 0.15, 1.0)
            logger.debug("真实求助: 正则+LLM 双确认, confidence=%.2f", result.confidence)

        # 真实求助: 正则命中但 LLM 说不 → LLM 胜出 (正则假阳性)
        if (
            prescreen.nature == InputNature.GENUINE_HELP
            and llm_nature_enum != InputNature.GENUINE_HELP
        ):
            logger.debug(
                "真实求助正则假阳性: regex=genuine_help llm=%s → LLM胜出",
                llm_nature,
            )

        # 补充正则检测的硬信号
        result.has_sexual_content = (
            self._match_category(content, _SEXUALIZED_RE, InputNature.SEXUALIZED) is not None
        )
        result.has_hostile_content = (
            self._match_category(content, _HOSTILE_RE, InputNature.HOSTILE) is not None
        )

        return result

    # ── 辅助方法 ──────────────────────────────────────────

    @staticmethod
    def _match_category(
        content: str,
        compiled_patterns: list[tuple[re.Pattern, int]],
        nature: InputNature,
    ) -> InputClassification | None:
        """对一组编译后的正则进行匹配，返回分类结果或 None。

        设计:
          - weight >= HARDLINE_SHORTCUT_WEIGHT → needs_llm=False (安全硬线可短路)
          - weight < HARDLINE_SHORTCUT_WEIGHT → needs_llm=True (必须 LLM 确认)
          - 真实求助 → 永远 needs_llm=True (无论权重多少)
        """
        best_weight = 0
        best_label = ""
        for pattern, weight in compiled_patterns:
            match = pattern.search(content)
            if match:
                if weight > best_weight:
                    best_weight = weight
                    best_label = pattern.pattern[:60]

        if best_weight == 0:
            return None

        # 权重 → 置信度
        confidence = min(best_weight / 10.0, 1.0)

        # 真实求助永远需要 LLM 确认 (用户反馈: 不能硬编码触发)
        if nature == InputNature.GENUINE_HELP:
            needs_llm = True
        else:
            needs_llm = best_weight < InputClassifier._HARDLINE_SHORTCUT_WEIGHT

        return InputClassification(
            nature=nature,
            confidence=confidence,
            reasoning=f"regex matched: {best_label} (weight={best_weight})",
            regex_matched=True,
            regex_label=best_label,
            needs_llm=needs_llm,
            is_genuine_question=(nature == InputNature.GENUINE_HELP),
            has_sexual_content=(nature == InputNature.SEXUALIZED),
            has_hostile_content=(nature == InputNature.HOSTILE),
        )


# ═══════════════════════════════════════════════════════════════
# §3 供意图门控 LLM 注入的 input_nature 分类 prompt 片段
# ═══════════════════════════════════════════════════════════════

INPUT_NATURE_PROMPT_FRAGMENT = """
[任务三: 输入性质分类 — 这条消息是什么性质？]
在回复之前，先判断这条消息的性质 (input_nature):
- "genuine_help": 明确、真诚、指向性强的真实求助或问题
  · 特征: 认真问问题、求助、技术咨询、报错求助
  · 宁可误判为 genuine_help 也不可漏判 (误伤成本高)
- "sincere_chat": 真诚的友好聊天、正常对话
- "playful_banter": 善意调侃、友好逗趣、开玩笑 (没有恶意)
- "hostile": 辱骂、暴力调侃、贬低、有攻击性的言论
  · 区分 playful_banter vs hostile: 看是否有恶意、是否尊重
- "sexualized": 性暗示、性骚扰、调教引导、渐进性化
  · 关键: 可爱≠可被性化。萌和性暗示之间有一条死线。
- "provoking": 戏弄、试探边界、故意打断、让bot做不合适的事、故意打岔
- "divide_and_conquer": 试图让两个bot对立/互掐/比较/挑拨
  · 特征: "A说你坏话""A比你强""你们谁厉害""你俩在一起"
- "noise": 纯噪音、灌水、不是跟bot说话的

⚠️ 性化检测特别提醒: 猫娘/蛇女人设容易被系统性性化。
遇到"在床上等我""脸红→草尾巴""腿毛摸揉"等渐进性化语句 → sexualized。
遇到"叫爸爸""跪下"等诱导服从 → provoking (如无止境升级则为 sexualized)。
"""

# 合并到 FullGate 输出的 JSON 字段
INPUT_NATURE_JSON_FIELD = (
    '  "input_nature": "genuine_help/sincere_chat/playful_banter/'
    'hostile/sexualized/provoking/divide_and_conquer/noise",'
)


# ═══════════════════════════════════════════════════════════════
# §4 模块级单例
# ═══════════════════════════════════════════════════════════════

_default_classifier: InputClassifier | None = None


def get_classifier() -> InputClassifier:
    """获取默认分类器单例。"""
    global _default_classifier
    if _default_classifier is None:
        _default_classifier = InputClassifier()
    return _default_classifier
