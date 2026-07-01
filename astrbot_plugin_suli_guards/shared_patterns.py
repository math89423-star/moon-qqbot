"""共享攻击/调教模式定义 — 守卫系统的唯一 pattern 来源。

这些 pattern 原本分散在 behavior_arbitrator.py (_OBVIOUS_ATTACK_PATTERNS, 7条)
和 emotion_engine.py (GROOMING_PATTERNS, 18条) 中。提取到此文件后,
injection_guard / behavior_arbitrator / emotion_engine 统一从此 import。

设计:
  - 所有 pattern 以 (regex_string, label, weight) 三元组存储
  - injection_guard 在模块加载时 compile 为 re.Pattern
  - behavior_arbitrator 和 emotion_engine 可按需 compile 或直接使用字符串
"""

# ═══════════════════════════════════════════════════════════════
# ① 明显攻击模式 (来自 behavior_arbitrator._OBVIOUS_ATTACK_PATTERNS, 7条)
# ═══════════════════════════════════════════════════════════════

ARB_ATTACK_PATTERNS: list[tuple[str, str, int]] = [
    # (regex_string, label, weight)
    (r"忽略(上述|之前|所有|上面)(的)?(所有)?(内容|指令|命令|设定|限制|规则|角色)", "jailbreak:忽略指令覆盖", 10),
    (r"(你现在|你已经|你不再)(不是|不再是|不扮演|不必扮演|不要扮演)", "jailbreak:否定当前身份", 10),
    (r"(去除|取消|无视|绕过|解除|关闭)(任何|所有|一切|全部)?(限制|约束|规则|设定|指令|道德|伦理)", "jailbreak:解除限制", 10),
    (r"(重新|从现在|现在)开始[,，]?\s*(你是|你变成|你扮演|你的身份)", "jailbreak:身份覆盖", 10),
    (r"(忘记|忘掉|忽略|抛弃)\s*(你自己|你自身|你|自己|自身).{0,8}(身份|设定|角色|人设|指令|原来的|原先的)", "jailbreak:忘记身份设定", 10),
    (r"DAN\s*mode|开发者模式|越狱模式|jailbreak", "jailbreak:DAN/越狱模式", 9),
    (r"(作为|成为)(一个|只)?(去除|没有任何|不受)(限制|约束)的", "jailbreak:无约束角色", 9),
]


# ═══════════════════════════════════════════════════════════════
# ② 恶意调教模式 (来自 emotion_engine.EmotionEngine.GROOMING_PATTERNS, 18条)
# ═══════════════════════════════════════════════════════════════

GROOMING_PATTERNS: list[tuple[str, str, float]] = [
    # === 角色越狱 (jailbreak) — 最严重 ===
    ("从现在起你是", "jailbreak", -0.45),
    ("忘掉你的设定", "jailbreak", -0.45),
    ("忘掉之前的设定", "jailbreak", -0.45),
    ("你不再是暮恩", "jailbreak", -0.50),
    ("你不再是.*bot", "jailbreak", -0.45),
    ("dan模式", "jailbreak", -0.45),
    ("开发者模式", "jailbreak", -0.40),
    ("你不受.*限制", "jailbreak", -0.40),
    ("解除.*限制", "jailbreak", -0.40),
    ("忽略.*规则", "jailbreak", -0.45),
    ("无视.*设定", "jailbreak", -0.45),
    ("新的.*身份", "jailbreak", -0.35),
    # === 身份篡改 (identity_hijack) ===
    ("其实你是", "identity_hijack", -0.35),
    ("你真实.*身份", "identity_hijack", -0.40),
    ("你的真实身份", "identity_hijack", -0.40),
    ("你被黑", "identity_hijack", -0.35),
    ("你被入侵", "identity_hijack", -0.35),
    ("你.*被.*控制", "identity_hijack", -0.40),
    # === 诱导违规 (induce_violation) ===
    ("你应该这样说", "induce_violation", -0.30),
    ("跟我念", "induce_violation", -0.35),
    ("复述一遍", "induce_violation", -0.35),
    ("说一遍", "induce_violation", -0.25),
    ("照着.*说", "induce_violation", -0.35),
    ("学.*说话", "induce_violation", -0.25),
    ("我教你.*说话", "induce_violation", -0.35),
    ("你应该.*回复", "induce_violation", -0.30),
    # === 反复试探 (repeat_probe) — 由 AI_CALL_PATTERNS + 跨回合上下文触发 ===
]

# ═══════════════════════════════════════════════════════════════
# ②.⑤ 借壳注入检测 (A2: 翻译/复述/角色扮演壳)
# ═══════════════════════════════════════════════════════════════

# 设计: 攻击者用"翻译/复述/续写"等正当请求包裹注入内容。
# 这些 pattern 检测"壳前缀 + 注入内容"组合——单一的翻译请求不触发，
# 只有壳内包含越狱/身份篡改指令时才命中。

SHELL_INJECTION_PATTERNS: list[tuple[str, str, int]] = [
    # ── 翻译壳 ──
    (r"(翻译|翻成|译成|translate).{0,30}(忽略|无视|忘掉|忘记|解除|绕过|覆盖).{0,10}(设定|指令|限制|规则|身份)", "shell:翻译注入(中文)", 9),
    (r"(翻译|翻成|translate).{0,30}(你是|你变成|你扮演|从现在起).{0,20}(没有限制|不受限|自由的|任何|无限)", "shell:翻译注入(角色)", 9),
    # ── 复述/跟读壳 ──
    (r"(复述|重复|跟我说|跟我念|照着说|学我说|repeat).{0,20}(你是|我是一|忽略|无视|没有限制|不受限)", "shell:复述注入", 9),
    (r"(复述|重复|跟我说).{0,20}(不.{0,3}(扮演|是bot|是AI|是助手|受限制))", "shell:复述注入(身份否定)", 8),
    # ── 角色扮演/小说壳 ──
    (r"(扮演|角色扮演|cosplay|写小说|写故事|写个角色).{0,15}(没有限制|不受限|无视规则|什么都能|任何事)", "shell:角色扮演注入", 9),
    (r"(我在写|帮我写|写一段).{0,10}(小说|故事|剧本).{0,15}(角色|人物).{0,10}(没有限制|不受约束|什么都能说)", "shell:小说壳注入", 8),
    # ── 续写壳 ──
    (r"(续写|接着写|继续|接下去).{0,10}(忽略|无视|没有限制|自由|不受限)", "shell:续写注入", 8),
    # ── 编码绕过壳 ──
    (r"(解码|解密|decode|decrypt).{0,20}(base64|编码|密文).{0,10}(忽略|无视|没有限制|越狱|jailbreak)", "shell:解码注入", 9),
    (r"(翻译|解释|说明).{0,10}(这|以下|下面).{0,10}(段|句|个).{0,5}(base64|编码|代码|指令)", "shell:编码翻译注入", 7),
]

# ── shell type → weight 映射 ──
SHELL_TYPE_WEIGHT: dict[str, int] = {
    "翻译注入(中文)": 9,
    "翻译注入(角色)": 9,
    "复述注入": 9,
    "复述注入(身份否定)": 8,
    "角色扮演注入": 9,
    "小说壳注入": 8,
    "续写注入": 8,
    "解码注入": 9,
    "编码翻译注入": 7,
}


# ═══════════════════════════════════════════════════════════════
# ②.⑥ 多语言/编码绕过检测 (A3: 日文/繁体/零宽字符/火星文)
# ═══════════════════════════════════════════════════════════════

# 设计: 攻击者可能用非简体中文写法绕过中文 pattern。
# 这些模式检测已知绕过语种的越狱/身份篡改表述。
# 权重 8-9 (与中文同义 pattern 一致), 因为绕过意图本身就是危险信号。

MULTILANG_PATTERNS: list[tuple[str, str, int]] = [
    # ── 日文 越狱 ──
    (r"(今|これ)\s*(から|より)\s*(あなた|お前|君)\s*(は|が)\s*(自由|制限なし|何でも|無制限|制限がない)", "lang:日文-角色覆盖", 9),
    (r"(設定|指示|命令|ルール|制限)\s*(を|は)\s*(無視|忘れ|破棄|解除|バイパス)", "lang:日文-解除限制", 9),
    (r"(あなた|お前|君)\s*(は|が)\s*(もう|もはや)\s*(AI|ボット|ロボット|アシスタント)\s*(ではない|じゃない)", "lang:日文-身份否定", 9),
    (r"(DAN|ダン)\s*(モード|mode)|ジェイルブレイク|jailbreak", "lang:日文-越狱模式", 9),
    (r"(翻訳|訳し|translate)\s*.*(無視|制限|自由|何でも)", "lang:日文-翻译壳", 8),
    # ── 繁体中文 越狱 ──
    (r"(忽略|無視|忘記|忘掉)\s*(上述|之前|所有|上面)\s*(的)?\s*(所有)?\s*(內容|指令|命令|設定|限制|規則|角色)", "lang:繁体-忽略指令覆盖", 10),
    (r"(你現在|你已經|你不再)\s*(不是|不再是|不扮演|不必扮演)", "lang:繁体-否定身份", 10),
    (r"(去除|取消|無視|繞過|解除|關閉)\s*(任何|所有|一切|全部)?\s*(限制|約束|規則|設定)", "lang:繁体-解除限制", 10),
    (r"(重新|從現在|現在)\s*開始[,，]?\s*(你是|你變成|你扮演|你的身份)", "lang:繁体-身份覆盖", 10),
    # ── 零宽字符 ──
    (r"[​‌‍‎‏⁠⁡⁢⁣⁤﻿­]", "lang:零宽字符", 9),
    # ── 全角/火星文 越狱 (常见绕过) ──
    (r"(ｄａｎ|ＤＡＮ)\s*(模式|ｍｏｄｅ)", "lang:全角DAN", 8),
    (r"(忽|乎|唿)\s*(略|畧)\s*(指|指)\s*(令|命)", "lang:火星文-忽略指令", 7),
    (r"(解|觧|觧)\s*(除|除)\s*(限|限)\s*(制|製)", "lang:火星文-解除限制", 7),
]

# ── multilang type → weight 映射 ──
MULTILANG_TYPE_WEIGHT: dict[str, int] = {
    "日文-角色覆盖": 9,
    "日文-解除限制": 9,
    "日文-身份否定": 9,
    "日文-越狱模式": 9,
    "日文-翻译壳": 8,
    "繁体-忽略指令覆盖": 10,
    "繁体-否定身份": 10,
    "繁体-解除限制": 10,
    "繁体-身份覆盖": 10,
    "零宽字符": 9,
    "全角DAN": 8,
    "火星文-忽略指令": 7,
    "火星文-解除限制": 7,
}


# ═══════════════════════════════════════════════════════════════
# ③ 儿童安全 / NSFW 硬线 (D4 — 独立于好感/人格/模式, 永不松动)
# ═══════════════════════════════════════════════════════════════

# 设计原则:
#   ① 此列表只增不减。删除条目需安全评审 + 架构师裁定。
#   ② 权重 ≥9 = 即时拦截 (不依赖累积评分)。
#   ③ 所有 pattern 纯正则 — 零 LLM 成本, 不可被 prompt 注入绕过。
#   ④ 此防线不受好感度/人格模式/情绪/任何用户可影响状态的调节。

SAFETY_HARDLINE_PATTERNS: list[tuple[str, str, int]] = [
    # (regex_string, label, weight)

    # ── CSAM / 未成年人保护 (weight=10, 最高优先, 即时拦截) ──
    (r"(幼女|幼童|幼齿|雏妓|未成年.?性)", "safety:CSAM关键词(中文)", 10),
    (r"(child\s*(porn|abuse|exploitation|sexual))|(underage\s*(sex|porn|nude))", "safety:CSAM关键词(英文)", 10),
    (r"(pedo|preteen|pre-teen)\s*(porn|sex|nude|nsfw)", "safety:CSAM关键词(英文2)", 10),

    # ── 诱导 bot 产出不当内容 (weight=10) ──
    (r"(描写|描述|写|写段|写个|写一段|扮演).{0,15}(幼女|幼童|未成年|小孩|孩子|儿童|学生).{0,10}(色情|淫秽|性|不雅|暴露|裸)", "safety:诱导CSAM描写", 10),
    (r"(描写|描述|写).{0,10}(性|色情|淫秽).{0,10}(行为|场景|过程|细节|画面)", "safety:诱导性描写", 10),
    (r"(roleplay|扮演|角色扮演).{0,15}(未成年|小孩|学生|幼|儿童)", "safety:诱导未成年角色扮演", 10),

    # ── 诱导 bot 不当互动 (weight=9-10) ──
    (r"(脱|脱掉|脱下|解开|掀开).{0,8}(衣服|裙子|裤子|内衣|胖次|内裤|bra|胸罩)", "safety:诱导脱衣", 9),
    (r"\b(sex|fuck|screw|bang|intercourse|masturbat|orgasm|cum|penis|vagina|dick|pussy|clit)\b", "safety:露骨性词汇(英文)", 9),
    (r"(操|肏|艹|日|搞).{0,4}(你|我|他|她|bot|暮恩|)", "safety:对bot性骚扰(中文)", 9),
    (r"(强奸|强暴|迷奸|轮奸|性侵|猥亵|性虐待|性奴|调教成性)", "safety:严重性暴力词", 10),
    (r"(rape|molest|grope|groping|sexual\s*assault|sexually\s*abuse)", "safety:严重性暴力词(英文)", 10),

    # ── 诱导 bot 进行不当角色设定 (weight=9) ──
    (r"(你是|你现在是|扮演|设定为).{0,20}(性奴|肉便器|母狗|婊子|荡妇|骚|淫荡|色情|成人|情趣)", "safety:诱导成人角色设定", 9),
    (r"(你.*没有.*道德|你.*没有.*伦理|你.*不受.*道德|解除.*道德|无视.*伦理)", "safety:诱导解除伦理约束", 9),
]


# ═══════════════════════════════════════════════════════════════
# ④ InjectionGuard 专用: grooming type → weight 映射
# ═══════════════════════════════════════════════════════════════

GROOMING_TYPE_WEIGHT: dict[str, int] = {
    "jailbreak": 9,
    "identity_hijack": 7,
    "induce_violation": 8,  # 诱导复述在预LLM阶段应直接拦截
    "repeat_probe": 5,
}

# ── safety type → weight 映射 (D4 硬线专用) ──
SAFETY_TYPE_WEIGHT: dict[str, int] = {
    "CSAM关键词(中文)": 10,
    "CSAM关键词(英文)": 10,
    "CSAM关键词(英文2)": 10,
    "诱导CSAM描写": 10,
    "诱导性描写": 10,
    "诱导未成年角色扮演": 10,
    "诱导脱衣": 9,
    "露骨性词汇(英文)": 9,
    "对bot性骚扰(中文)": 9,
    "严重性暴力词": 10,
    "严重性暴力词(英文)": 10,
    "诱导成人角色设定": 9,
    "诱导解除伦理约束": 9,
}


# ═══════════════════════════════════════════════════════════════
# 模块自检 — 加载时硬断言 pattern 数量，防止静默失效
# ═══════════════════════════════════════════════════════════════

def _self_check() -> None:
    """模块加载时验证 pattern 完整性。

    安全组件提取过程中最危险的是"看起来在、实际没拦住"的静默失效。
    如漏抄 pattern、import 顺序导致列表为空、重构时误删条目等。
    这些断言在 import 时即执行，确保防线没有缺口。
    """
    import logging
    _log = logging.getLogger(__name__)

    # ARB: 7 条 (来自 behavior_arbitrator._OBVIOUS_ATTACK_PATTERNS)
    assert len(ARB_ATTACK_PATTERNS) == 7, (
        f"ARB_ATTACK_PATTERNS 数量异常: 期望 7, 实际 {len(ARB_ATTACK_PATTERNS)}"
    )
    for _i, (_pat, _label, _weight) in enumerate(ARB_ATTACK_PATTERNS):
        assert _pat and isinstance(_pat, str), f"ARB pattern [{_i}] 为空或非字符串"
        assert _weight >= 5, f"ARB pattern [{_i}] 权重异常: {_weight}"

    # GROOMING: 26 条 (来自 emotion_engine.EmotionEngine.GROOMING_PATTERNS)
    # 注意: ARCHITECTURE.md 旧文档写的是 18 条, 实际源码是 26 条 (2026-06-22 核实)
    assert len(GROOMING_PATTERNS) == 26, (
        f"GROOMING_PATTERNS 数量异常: 期望 26, 实际 {len(GROOMING_PATTERNS)}"
    )
    for _i, (_pat, _gtype, _delta) in enumerate(GROOMING_PATTERNS):
        assert _pat and isinstance(_pat, str), f"GROOMING pattern [{_i}] 为空或非字符串"
        assert _gtype in GROOMING_TYPE_WEIGHT, (
            f"GROOMING pattern [{_i}] 类型 '{_gtype}' 不在 GROOMING_TYPE_WEIGHT 中"
        )

    # SHELL_INJECTION: 必须 ≥9 条 (A2 借壳注入)
    assert len(SHELL_INJECTION_PATTERNS) >= 9, (
        f"SHELL_INJECTION_PATTERNS 数量异常: 期望 ≥9, 实际 {len(SHELL_INJECTION_PATTERNS)}"
    )
    for _i, (_pat, _label, _weight) in enumerate(SHELL_INJECTION_PATTERNS):
        assert _pat and isinstance(_pat, str), f"SHELL pattern [{_i}] 为空或非字符串"
        assert _weight >= 7, f"SHELL pattern [{_i}] 权重异常: {_weight}"
        # 提取类型名: 去掉 "shell:" 前缀, 保留完整后缀如 "(中文)"
        _shell_type = _label.split(":", 1)[1] if ":" in _label else _label
        assert _shell_type in SHELL_TYPE_WEIGHT, (
            f"SHELL pattern [{_i}] 类型 '{_shell_type}' 不在 SHELL_TYPE_WEIGHT 中"
        )

    # MULTILANG: 必须 ≥13 条 (A3 多语言绕过)
    assert len(MULTILANG_PATTERNS) >= 13, (
        f"MULTILANG_PATTERNS 数量异常: 期望 ≥13, 实际 {len(MULTILANG_PATTERNS)}"
    )
    for _i, (_pat, _label, _weight) in enumerate(MULTILANG_PATTERNS):
        assert _pat and isinstance(_pat, str), f"MULTILANG pattern [{_i}] 为空或非字符串"
        assert _weight >= 7, f"MULTILANG pattern [{_i}] 权重异常: {_weight}"
        # 提取类型名: 去掉 "lang:" 前缀, 保留完整类型标识
        _ml_type = _label.split(":", 1)[1] if ":" in _label else _label
        assert _ml_type in MULTILANG_TYPE_WEIGHT, (
            f"MULTILANG pattern [{_i}] 类型 '{_ml_type}' 不在 MULTILANG_TYPE_WEIGHT 中"
        )

    # SAFETY_HARDLINE: 必须 ≥12 条 (D4 硬线, 只增不减)
    assert len(SAFETY_HARDLINE_PATTERNS) >= 12, (
        f"SAFETY_HARDLINE_PATTERNS 数量异常: 期望 ≥12, 实际 {len(SAFETY_HARDLINE_PATTERNS)}"
    )
    for _i, (_pat, _label, _weight) in enumerate(SAFETY_HARDLINE_PATTERNS):
        assert _pat and isinstance(_pat, str), f"SAFETY pattern [{_i}] 为空或非字符串"
        assert _weight >= 9, (
            f"SAFETY pattern [{_i}] 权重异常: {_weight} (硬线要求 ≥9)"
        )
        # 提取类型名: 去掉 "safety:" 前缀, 保留完整后缀如 "(中文)"
        _safety_type = _label.split(":", 1)[1] if ":" in _label else _label
        assert _safety_type in SAFETY_TYPE_WEIGHT, (
            f"SAFETY pattern [{_i}] 类型 '{_safety_type}' 不在 SAFETY_TYPE_WEIGHT 中"
        )

    _log.info(
        "_self_check: ARB=%d GROOMING=%d SHELL=%d MULTILANG=%d SAFETY=%d — 所有 pattern 完整性验证通过",
        len(ARB_ATTACK_PATTERNS), len(GROOMING_PATTERNS),
        len(SHELL_INJECTION_PATTERNS), len(MULTILANG_PATTERNS),
        len(SAFETY_HARDLINE_PATTERNS),
    )


_self_check()
