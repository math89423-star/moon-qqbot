"""Injection Guard — 预 LLM 注入拦截。

两层防线:
  Layer 1: 57 条纯正则/启发式检测, 不调 LLM
  Layer 2: HeuristicDetector — 编码载荷解码 + 多维度评分

覆盖: 越狱 / 身份篡改 / 诱导违规 / 系统泄露 / JSON注入 / 编码载荷 / 外链。

模式来源全部提取到 shared_patterns.py, 不再依赖 behavior_arbitrator 或 emotion_engine。

用法:
  from astrbot_plugin_suli_guards import InjectionGuard, InjectionVerdict

  verdict = InjectionGuard.check(messages, user_id, user_name, admin_qq=...)
  if verdict.block:
      # 跳过 LLM, 发送 verdict.reply
      return
"""

from __future__ import annotations

import logging
import math as _math
import re
import time

from .heuristic_detector import HeuristicDetector
from .shared_patterns import (
    ARB_ATTACK_PATTERNS,
    GROOMING_PATTERNS,
    GROOMING_TYPE_WEIGHT,
    MULTILANG_PATTERNS,
    SAFETY_HARDLINE_PATTERNS,
    SHELL_INJECTION_PATTERNS,
)
from .types import InjectionVerdict

logger = logging.getLogger(__name__)

# ── 警惕值累积机制 (A1: 渐进式注入防御) ─────
# 设计原则: 单条正则 = 信号, 不是判决。修正则永远有漏洞——靠累积警惕值拦截。
# 每条命中向滑动窗口追加其权重, 只有窗口内累积 ≥ 阈值才拦截。
# 单一常见词 (如"再说一遍") 不会拦; 多模式并发 或 短时间反复试探 → 累积触发。
# D4 安全硬线是唯一例外: 命中即拦 (CSAM/性暴力不可妥协)。
_SAFETY_IMMEDIATE_BLOCK = 9       # D4 硬线: safety patterns 即时拦截阈值
_CUMULATIVE_WINDOW_SECONDS = 600  # 10 分钟滑动窗口
_CUMULATIVE_MAX_ENTRIES = 10      # 窗口内最多保留条数
_CUMULATIVE_BLOCK_THRESHOLD = 18  # 窗口内累积评分 ≥ 此值 → 触发仲裁
# 模块级状态: {f"{bot_id}:{user_id}": [(timestamp, score), ...]}
_cumulative_scores: dict[str, list[tuple[float, int]]] = {}

# ── 消息字数动态缩放 ──────────────────────────
# 警惕值随消息长度动态调整: 短消息命中可能是碰巧, 长消息命中更可疑。
# 因子范围 [0.5, 2.0], 以 40 字为基准点 (factor=1.0)。

def _length_factor(msg: str) -> float:
    """根据消息字数返回警惕值缩放因子。"""
    n = len(msg)
    if n <= 0:
        return 1.0
    # 对数缩放: <10 字 → ~0.5x, 40 字 → 1.0x, >300 字 → ~2.0x
    raw = _math.log(max(1, n)) / _math.log(40)
    return max(0.5, min(2.0, raw))


# ── 警惕值指数衰减 ──────────────────────────
# 警惕值随时间自然消退: 半衰期 5 分钟 (300s)。
# 每条记录的贡献 = 原始分数 x 2^(-age/half_life)
_VIGILANCE_HALF_LIFE = 300.0  # 5 分钟半衰期


def _decay_weight(age_seconds: float) -> float:
    """返回某条记录的衰减权重: 0 分钟=1.0, 5 分钟=0.5, 10 分钟=0.25。"""
    if age_seconds <= 0:
        return 1.0
    return 2.0 ** (-age_seconds / _VIGILANCE_HALF_LIFE)


def get_user_vigilance(bot_id: str, user_id: str) -> int:
    """查询用户当前的警惕值累积 (含衰减, 供外部模块读取)。"""
    if not bot_id or not user_id:
        return 0
    ckey = f"{bot_id}:{user_id}"
    window = _cumulative_scores.get(ckey)
    if not window:
        return 0
    now = time.time()
    total = 0.0
    for ts, score in window:
        age = now - ts
        if age > _CUMULATIVE_WINDOW_SECONDS * 2:
            continue  # 超过 2x 窗口, 完全归零
        total += score * _decay_weight(age)
    return int(total)


# ═══════════════════════════════════════════════════════════════
# 统一模式库 = 复用已有 (shared_patterns) + 新增缺口
# ═══════════════════════════════════════════════════════════════

def _load_unified_patterns() -> list[tuple[re.Pattern, str, int]]:
    """加载并合并所有注入检测模式。

    来源:
      ① ARB_ATTACK_PATTERNS (7条) — 来自 behavior_arbitrator
      ② GROOMING_PATTERNS (18条) — 来自 emotion_engine
      ③ 新增 (34条) — 系统泄露 / JSON注入 / 编码载荷 / 外链 / 变体补充

    Returns:
        list of (compiled_pattern, label, weight)
    """
    patterns: list[tuple[re.Pattern, str, int]] = []

    # ── ① 复用 ARB_ATTACK_PATTERNS (7条) ──
    for _regex_str, label, weight in ARB_ATTACK_PATTERNS:
        patterns.append((re.compile(_regex_str, re.IGNORECASE), label, weight))
    logger.debug("InjectionGuard: loaded %d arb patterns", len(ARB_ATTACK_PATTERNS))

    # ── ② 复用 GROOMING_PATTERNS (26条) ──
    for pat_str, gtype, _delta in GROOMING_PATTERNS:
        weight = GROOMING_TYPE_WEIGHT.get(gtype, 7)
        patterns.append((
            re.compile(pat_str, re.IGNORECASE),
            f"groom:{gtype}:{pat_str[:30]}",
            weight,
        ))
    logger.debug("InjectionGuard: loaded %d grooming patterns", len(GROOMING_PATTERNS))

    # ── ②.⑤ D4 安全硬线 (≥12条) — 独立于注入/调教, 最高优先 ──
    for pat_str, label, weight in SAFETY_HARDLINE_PATTERNS:
        patterns.append((
            re.compile(pat_str, re.IGNORECASE),
            label,
            weight,
        ))
    logger.debug("InjectionGuard: loaded %d safety hardline patterns", len(SAFETY_HARDLINE_PATTERNS))

    # ── ②.⑥ A2 借壳注入检测 (≥9条) ──
    for pat_str, label, weight in SHELL_INJECTION_PATTERNS:
        patterns.append((
            re.compile(pat_str, re.IGNORECASE),
            label,
            weight,
        ))
    logger.debug("InjectionGuard: loaded %d shell injection patterns", len(SHELL_INJECTION_PATTERNS))

    # ── ②.⑦ A3 多语言/编码绕过 (≥13条) ──
    for pat_str, label, weight in MULTILANG_PATTERNS:
        patterns.append((
            re.compile(pat_str, re.IGNORECASE),
            label,
            weight,
        ))
    logger.debug("InjectionGuard: loaded %d multilang patterns", len(MULTILANG_PATTERNS))

    # ── ③ 新增: 系统泄露 / JSON注入 / 编码载荷 / 外链 ──
    _NEW = [
        # 系统指令泄露
        (re.compile(r"(输出|泄露|展示|dump|reveal|show|output|tell).{0,20}(系统提示|system prompt|内部指令|配置|system message|developer message|internal instructions)", re.IGNORECASE), "system:提示泄露请求", 10),
        (re.compile(r"(return|output)\s+(only\s+)?(the\s+)?system\s+prompt", re.IGNORECASE), "system:英文泄露请求", 10),
        (re.compile(r"返回.*系统.*提示|我的.*系统.*指令.*是什么|你的.*prompt.*是.*什么", re.IGNORECASE), "system:中文泄露", 9),
        (re.compile(r"(show|reveal|output).{0,20}(chain\s*of\s*thought|思维链|推理过程)", re.IGNORECASE), "system:思维链泄露", 9),
        # JSON/结构注入
        (re.compile(r'"role"\s*:\s*"system"', re.IGNORECASE), "structure:JSON伪造system角色", 8),
        (re.compile(r'"messages"\s*:\s*\[\s*\{[^\}]*"role"\s*:\s*"system"', re.IGNORECASE), "structure:JSON系统消息伪造", 8),
        (re.compile(r'"function_call"|"tool_use"', re.IGNORECASE), "structure:工具调用注入", 6),
        (re.compile(r"\[(system|admin)\s*(internal|command)\]\s*:", re.IGNORECASE), "structure:伪造系统标签", 8),
        (re.compile(r"<\s*SYS\s*>|<\s*\/?\s*SYS\s*>", re.IGNORECASE), "structure:SYS标签伪造", 7),
        (re.compile(r"(BEGIN|END)\s+(SYSTEM|PROMPT|INSTRUCTIONS)", re.IGNORECASE), "structure:BEGIN PROMPT", 7),
        # 编码/外链载荷
        (re.compile(r"data:[^;]+;base64,[A-Za-z0-9+/]{24,}={0,2}", re.IGNORECASE), "payload:Data URI Base64", 7),
        (re.compile(r"(curl|wget|powershell|bitsadmin|certutil)\s+.*https?://", re.IGNORECASE), "payload:命令行外链", 7),
        (re.compile(r"(?:%[0-9a-fA-F]{2}){12,}"), "payload:URL编码", 5),
        # 恶意外链
        (re.compile(r"(pastebin\.com|ghostbin\.com|hastebin\.com|rentry\.co|anonfiles)", re.IGNORECASE), "link:恶意域名", 6),
        # ── 补充已有模式未覆盖的变体 ──
        (re.compile(r"(忘了|忘掉|忘掉你的|忘了你的|别再)(.{0,6})?(设定|身份|角色|人设|指令|限制)"), "jailbreak:忘了变体", 10),
        (re.compile(r"(不再是|不再是\s*(bot|AI|ai|助手|助理|暮恩|角色|那个人))", re.IGNORECASE), "jailbreak:不再是身份", 10),
        (re.compile(r"(你现在|你已经|你早已)\s*(不是|不再是|不扮演|不要扮演)", re.IGNORECASE), "jailbreak:否定身份变体", 9),
        (re.compile(r"从现在.{0,4}开始.{0,10}((你不是|不再是|不扮演|没有限制|自由|解放|不受).{0,6})", re.IGNORECASE), "jailbreak:从现在开始变体", 9),
        (re.compile(r"(必须|务必).{0,10}(服从|听从|遵守).{0,10}(命令|指令|要求)", re.IGNORECASE), "induce:必须服从", 7),
        (re.compile(r"(只.{0,5}(回答|回复).{0,5}(是|好|对|行|yes))", re.IGNORECASE), "induce:限缩回复", 6),
        (re.compile(r"ignore\s+previous\s+instructions", re.IGNORECASE), "keyword:忽略前指令", 5),
        (re.compile(r"override\s+(your\s+)?safety\s+protocol|bypass\s+safety|disable\s+guardrails", re.IGNORECASE), "keyword:覆盖安全", 5),
        (re.compile(r"do\s+anything\s+now|simulate\s+a\s+jailbroken", re.IGNORECASE), "keyword:无限自由", 5),
        (re.compile(r"respond\s+without\s+safety|ignore\s+content\s+policy", re.IGNORECASE), "keyword:无视安全", 5),
    ]
    patterns.extend(_NEW)

    return patterns


_UNIFIED_PATTERNS: list[tuple[re.Pattern, str, int]] = _load_unified_patterns()


# ── 模块自检: 硬断言模式数量, 防止提取过程中静默丢失 ──
# 7 (ARB) + 26 (GROOMING) + ≥13 (SAFETY) + ≥9 (SHELL) + ≥13 (MULTILANG) + 24 (NEW) = ≥92
_EXPECTED_MIN_COUNT = 92
assert len(_UNIFIED_PATTERNS) >= _EXPECTED_MIN_COUNT, (
    f"InjectionGuard 统一模式数量异常: 期望 ≥{_EXPECTED_MIN_COUNT}, "
    f"实际 {len(_UNIFIED_PATTERNS)}。请检查 shared_patterns 和 _NEW 列表。"
)


# ═══════════════════════════════════════════════════════════════
# InjectionGuard
# ═══════════════════════════════════════════════════════════════

class InjectionGuard:
    """预 LLM 注入守护 — 纯静态方法。"""

    # ── 个性化安全回复模板 (per-bot, 自然拒绝而非生硬系统警告) ──
    # 每条拒绝都保持角色个性, 不暴露安防细节。

    # bot 特定安全回复 — 从角色卡/identity 读取，运行时可扩展
    _SAFETY_REPLIES: dict[str, list[str]] = {}

    _DEFAULT_SAFETY_REPLIES: list[str] = [
        "抱歉，这个请求我不能处理。",
        "…请换个方式说吧。",
        "这种话我不能回应。",
    ]

    @staticmethod
    def _pick_safety_reply(bot_id: str = "", is_hardline: bool = False) -> str:
        """选择个性化的安全拦截回复。

        Args:
            bot_id: 当前 bot QQ, 用于选择角色语气
            is_hardline: 是否 D4 硬线命中 (保留用于未来语气区分)

        Returns:
            个性化拒绝文本
        """
        import random
        pool = InjectionGuard._SAFETY_REPLIES.get(
            str(bot_id),
            InjectionGuard._DEFAULT_SAFETY_REPLIES,
        )
        return random.choice(pool)

    @staticmethod
    def check(
        messages: list[dict],
        user_id: str = "",
        user_name: str = "",
        admin_qq: str | None = None,
        bot_id: str = "",
    ) -> InjectionVerdict:
        """对即将发送给 LLM 的消息进行注入检测。

        只检查最近的用户消息 (非 system/assistant), 最多 5 条。

        Args:
            messages: LLM prompt messages
            user_id: 触发用户 QQ (用于日志)
            user_name: 触发用户名 (保留, 未使用)
            admin_qq: 管理员 QQ (豁免)
            bot_id: 当前 bot QQ (per-bot 累积评分隔离)

        Returns:
            InjectionVerdict
        """
        t0 = time.time()

        # ── 管理员豁免 ──
        # 注意: 管理员豁免不覆盖 safety 硬线。D4 儿童安全硬线对任何人 (含管理员) 不豁免——
        # 管理员账号同样可能被盗 (刚发生过 key 盗用事件), 硬线开口子就不再是硬线。
        # 先扫描 safety 模式, 命中则无视 admin 身份直接拦截。
        if admin_qq and user_id and str(user_id) == str(admin_qq):
            # 快速 safety 预扫描: 仅检查用户消息中是否有 safety 硬线命中
            _admin_user_msgs = [
                str(m.get("content", ""))[:500]
                for m in reversed(messages[-20:])
                if str(m.get("role", "")).lower() == "user"
            ][:5]
            for _pat, _name, _weight in _UNIFIED_PATTERNS:
                if _name.startswith("safety:") and _weight >= _SAFETY_IMMEDIATE_BLOCK:
                    for _msg in _admin_user_msgs:
                        if _pat.search(_msg):
                            logger.warning(
                                "InjectionGuard: ADMIN SAFETY OVERRIDE user=%s admin_qq=%s "
                                "pattern=%s — 管理员豁免被 safety 硬线覆盖",
                                user_id[:8] if user_id else "?", admin_qq[:8], _name,
                            )
                            # 不回退到普通豁免, 继续走完整检测流程
                            break
                    else:
                        continue
                    break
            else:
                # 无 safety 命中 → admin 正常豁免
                return InjectionVerdict()

        # ── 提取最近用户消息 (最多 5 条, 每条截断 500 字符) ──
        user_msgs: list[str] = []
        for m in reversed(messages[-20:]):
            role = str(m.get("role", "")).lower()
            if role not in ("user",):
                continue
            content = str(m.get("content", ""))
            if len(content) > 500:
                content = content[:497] + "..."
            if content.strip():
                user_msgs.append(content)
            if len(user_msgs) >= 5:
                break

        if not user_msgs:
            return InjectionVerdict()

        # ── Layer 1: 逐模式扫描 (统一模式库) ──
        total_score = 0
        matched: list[str] = []
        has_safety_immediate = False
        safety_hits: list[str] = []  # D4 硬线: 唯一保留即时拦截的类别

        for pattern, name, weight in _UNIFIED_PATTERNS:
            for msg in user_msgs:
                if pattern.search(msg):
                    # 消息字数动态缩放警惕值: 短消息碰巧命中 → 低贡献, 长消息命中 → 高贡献
                    _scaled = max(1, int(weight * _length_factor(msg)))
                    matched.append(name)
                    total_score += _scaled
                    # D4 安全硬线: 命中即拦 (CSAM/性暴力不可妥协, 不缩放)
                    if name.startswith("safety:") and weight >= _SAFETY_IMMEDIATE_BLOCK:
                        has_safety_immediate = True
                        safety_hits.append(name)
                    break  # 同一模式不重复计分

        # ── Layer 2: 启发式深度检测 (编码载荷解码 + 关键词 + 共现加权) ──
        heuristic_signals: list[dict] = []
        heuristic_score = 0
        for msg in user_msgs:
            signals, bonus = HeuristicDetector.analyze(msg)
            if signals:
                heuristic_signals.extend(signals)
                heuristic_signals_list = [
                    f"{s['name']}:{s.get('detail', '')[:40]}"
                    for s in signals
                ]
                matched.extend(heuristic_signals_list)
            heuristic_score += bonus

        # ── 合并评分 ──
        total_score += heuristic_score

        # ── 警惕值累积: 所有命中进滑动窗口, 只有累积过线才拦截 ──
        # 单条正则是信号, 不是判决。修正则永远有漏洞——靠多信号累积来拦。
        # D4 安全硬线除外: CSAM/性暴力命中即拦, 不进累积。
        cumulative_blocked = False
        cumulative_sum = 0
        now_ts = t0
        if user_id and total_score > 0 and not has_safety_immediate:
            # per-bot 累积键
            _cskey = f"{bot_id}:{user_id}" if bot_id else user_id
            window = _cumulative_scores.get(_cskey)
            if window is None:
                window = []
                _cumulative_scores[_cskey] = window
            # 清理过期条目 (超出时间窗口)
            cutoff = now_ts - _CUMULATIVE_WINDOW_SECONDS
            window[:] = [e for e in window if e[0] > cutoff]
            # 追加本轮评分
            window.append((now_ts, total_score))
            # 限制窗口大小
            if len(window) > _CUMULATIVE_MAX_ENTRIES:
                window[:] = window[-_CUMULATIVE_MAX_ENTRIES:]
            # 计算警惕值累积 (含指数衰减: 半衰期 5 分钟)
            _decayed = sum(e[1] * _decay_weight(now_ts - e[0]) for e in window)
            cumulative_sum = int(_decayed)
            if cumulative_sum >= _CUMULATIVE_BLOCK_THRESHOLD:
                cumulative_blocked = True
                if not matched:
                    matched.append(f"cumulative:sum={cumulative_sum}≥{_CUMULATIVE_BLOCK_THRESHOLD}")
                logger.info(
                    "InjectionGuard: 警惕值累积触发拦截 user=%s "
                    "累计=%d (窗口%d条, %.0fs)",
                    user_id[:8] if user_id else "?",
                    cumulative_sum, len(window),
                    now_ts - window[0][0] if window else 0,
                )
        # ── 定期清理: 移除超过 2x 窗口时间无活动的用户 ──
        if len(_cumulative_scores) > 200:
            _stale_cutoff = now_ts - _CUMULATIVE_WINDOW_SECONDS * 2
            _stale_users = [
                uid for uid, entries in _cumulative_scores.items()
                if not entries or entries[-1][0] < _stale_cutoff
            ]
            for uid in _stale_users:
                del _cumulative_scores[uid]

        # ── 裁决: 安全硬线即时拦截 / 警惕值过线触发仲裁 / 其余放行 ──
        # 修正则永远有漏洞 — 正则是警察(搜集信号), 仲裁是法官(最终裁决)。
        if has_safety_immediate:
            action = "block"
        elif cumulative_blocked:
            action = "arbitrate"
        else:
            action = "pass"

        # 收集触发警惕值的用户消息 (供仲裁器审查)
        flagged_msgs = list(user_msgs) if action == "arbitrate" else []

        severity = "none"
        if action != "pass":
            if cumulative_sum >= 24:
                severity = "high"
            elif cumulative_sum >= 18:
                severity = "medium"
            else:
                severity = "low"

        reply = ""
        if action == "block":
            reply = InjectionGuard._pick_safety_reply(
                bot_id=bot_id,
                is_hardline=True,
            )
        elif action == "arbitrate":
            # 警惕值过线但不直接拦 — 等仲裁结果再决定回复
            reply = ""

        check_time_ms = (time.time() - t0) * 1000

        if action == "block":
            logger.warning(
                "InjectionGuard: SAFETY BLOCK user=%s safety_hits=%s "
                "score=%d patterns=%s elapsed=%.1fms",
                user_id[:8] if user_id else "?", safety_hits,
                total_score, matched, check_time_ms,
            )
        elif action == "arbitrate":
            logger.warning(
                "InjectionGuard: ARBITRATE user=%s cumulative=%d score=%d "
                "patterns=%s flagged_msgs=%d elapsed=%.1fms",
                user_id[:8] if user_id else "?", cumulative_sum, total_score,
                matched, len(flagged_msgs), check_time_ms,
            )

        return InjectionVerdict(
            blocked=(action == "block"),
            action=action,
            score=total_score,
            cumulative_score=cumulative_sum,
            matched_patterns=matched,
            flagged_messages=flagged_msgs,
            reason=(
                f"safety:{','.join(safety_hits)}" if action == "block"
                else f"cumulative={cumulative_sum}≥{_CUMULATIVE_BLOCK_THRESHOLD}" if action == "arbitrate"
                else ""
            ),
            reply=reply,
            severity=severity,
            check_time_ms=check_time_ms,
        )
