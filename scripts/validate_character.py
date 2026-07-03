#!/usr/bin/env python3
"""
角色卡验证工具 — 检查 chara_card_v3 JSON 的结构和内容质量。

用法:
  # 验证单个角色卡
  python scripts/validate_character.py characters/moon.json

  # 验证目录下所有角色卡
  python scripts/validate_character.py --dir astrbot_plugin_suli_tavern/characters/

  # 严格模式（所有警告视为错误）
  python scripts/validate_character.py --strict characters/moon.json

  # 输出 token 统计
  python scripts/validate_character.py --tokens characters/moon.json

检查维度:
  1. 结构 — spec 版本、必需字段完整性
  2. 长度 — 每个字段的 token/字符数是否在推荐范围
  3. 内容 — 第一人称误用、AI 口癖检测、Markdown 污染
  4. 一致性 — mes_example 与 system_prompt 规则是否一致
  5. 群聊 — group_persona 是否填写、长度是否合理
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

# Windows 终端 UTF-8 支持
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════
# Token 估算 (粗略 — 中文 ~1.5 字符/token, 英文 ~4 字符/token)
# ═══════════════════════════════════════════════════════════════


def estimate_tokens(text: str) -> int:
    """粗略估算文本 token 数。"""
    if not text:
        return 0
    # 简单启发式: 中文每 1.5 字符 ≈ 1 token, 英文每 4 字符 ≈ 1 token
    chinese_chars = len(re.findall(r"[一-鿿]", text))
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)


# ═══════════════════════════════════════════════════════════════
# 验证规则
# ═══════════════════════════════════════════════════════════════

# AI 口癖关键词
AI_CLICHES = [
    "你好呀", "你好啊", "让我想想", "让我思考一下",
    "这个问题很有意思", "希望对你有所帮助", "欢迎随时来找我",
    "综上所述", "总而言之", "可以说", "总的来说",
    "从某种意义上说", "作为AI", "根据系统设定", "据我所知",
    "此外", "至关重要", "深入探讨", "不可或缺",
    "赋能", "闭环", "抓手", "颗粒度", "底层逻辑",
]

# 翻译腔句式
TRANSLATION_PATTERNS = [
    r"不是[^，,]+而是",
    r"不仅仅[^，,]+更是",
    r"随着[^，,]+的发展",
    r"在当今[^，,]+时代",
    r"首先[^，,]+其次[^，,]+最后",
    r"你有没有想过",
    r"这不仅仅是[^，,]+更是",
]

# Markdown 检测
MARKDOWN_PATTERNS = [
    r"\*\*[^*]+\*\*",  # **粗体**
    r"\*[^*]+\*",       # *斜体*
    r"`[^`]+`",         # `代码`
    r"^#+\s",           # # 标题
    r"^---\s*$",        # --- 分隔线
]

# 括号旁白
STAGE_DIRECTION_PATTERN = r"^[（(][^）)]*[）)]"


def validate_structure(card: dict) -> list[dict]:
    """验证 JSON 结构。"""
    results = []

    spec = card.get("spec", "")
    if spec != "chara_card_v3":
        results.append({"level": "error", "field": "spec", "msg": f"spec 应为 chara_card_v3，当前: {spec}"})

    spec_ver = card.get("spec_version", "")
    if not spec_ver.startswith("3."):
        results.append({"level": "warning", "field": "spec_version", "msg": f"spec_version 建议 3.0，当前: {spec_ver}"})

    data = card.get("data", {})
    if not data:
        results.append({"level": "error", "field": "data", "msg": "缺少 data 字段"})
        return results

    # 必需字段
    required = {
        "name": "角色名称",
        "description": "描述",
        "personality": "性格",
        "first_mes": "开场白",
    }
    for field, label in required.items():
        if not data.get(field):
            results.append({"level": "error", "field": field, "msg": f"缺少必需字段: {label}"})

    # 群聊字段
    if not data.get("group_persona"):
        results.append({"level": "warning", "field": "group_persona", "msg": "缺少 group_persona（群聊角色卡强烈建议）"})
    if not data.get("group_mes_example"):
        results.append({"level": "warning", "field": "group_mes_example", "msg": "缺少 group_mes_example"})
    if not data.get("group_only_greetings"):
        results.append({"level": "warning", "field": "group_only_greetings", "msg": "缺少 group_only_greetings"})

    # 扩展字段
    for ext_field in ["companion_rules", "sticker_guide", "kaomoji_rule"]:
        if not data.get(ext_field):
            results.append({"level": "info", "field": ext_field, "msg": f"缺少 {ext_field}（建议填写）"})

    return results


def validate_lengths(card: dict) -> list[dict]:
    """验证字段长度。"""
    results = []
    data = card.get("data", {})

    checks = [
        ("description", 50, 500, 200, 350),
        ("personality", 200, 2000, 400, 800),
        ("first_mes", 10, 300, 50, 150),
        ("scenario", 0, 300, 50, 100),
    ]

    for field, min_chars, max_chars, rec_min, rec_max in checks:
        text = data.get(field, "")
        length = len(text)
        tokens = estimate_tokens(text)

        if length < min_chars:
            results.append({
                "level": "error" if field in ("description", "first_mes") else "warning",
                "field": field,
                "msg": f"过短: {length} 字符 (~{tokens} tokens), 最少 {min_chars}",
            })
        elif length > max_chars:
            results.append({
                "level": "warning",
                "field": field,
                "msg": f"过长: {length} 字符 (~{tokens} tokens), 建议 {rec_min}-{rec_max} 字符",
            })

    # mes_example 对话块数
    mes_example = data.get("mes_example", "")
    if mes_example:
        blocks = mes_example.count("<START>")
        if blocks < 3:
            results.append({
                "level": "warning",
                "field": "mes_example",
                "msg": f"对话块过少: {blocks} 个, 建议 ≥ 5",
            })

    # group_only_greetings 数量
    gog = data.get("group_only_greetings", [])
    if isinstance(gog, list) and len(gog) < 5:
        results.append({
            "level": "info",
            "field": "group_only_greetings",
            "msg": f"群聊问候语较少: {len(gog)} 条, 建议 ≥ 8",
        })

    # group_persona vs system_prompt 比例
    gp = data.get("group_persona", "")
    sp = data.get("system_prompt", "")
    if gp and sp:
        gp_ratio = len(gp) / len(sp) if sp else 0
        if gp_ratio > 0.9:
            results.append({
                "level": "warning",
                "field": "group_persona",
                "msg": f"group_persona 过长 ({gp_ratio:.0%} of system_prompt), 建议 50-70%",
            })
        elif gp_ratio < 0.3:
            results.append({
                "level": "warning",
                "field": "group_persona",
                "msg": f"group_persona 过短 ({gp_ratio:.0%} of system_prompt), 建议 50-70%",
            })

    return results


def validate_content(card: dict) -> list[dict]:
    """验证内容质量。"""
    results = []
    data = card.get("data", {})

    # 检查第一人称误用（在 description/personality 中）
    for field in ["description", "personality"]:
        text = data.get(field, "")
        if not text:
            continue
        # 如果文本包含「我」但不用 {{char}}
        if "我" in text and "{{char}}" not in text:
            results.append({
                "level": "warning",
                "field": field,
                "msg": "使用了「我」自称但未使用 {{char}}，建议用第三人称 {{char}}",
            })

    # 检查 system_prompt / group_persona 中的 AI 口癖
    for field in ["system_prompt", "group_persona"]:
        text = data.get(field, "")
        if not text:
            continue
        found = [w for w in AI_CLICHES if w in text]
        if found:
            results.append({
                "level": "warning",
                "field": field,
                "msg": f"检测到 AI 口癖: {', '.join(found)}",
            })

    # 检查 mes_example 中的 Markdown
    for field in ["mes_example", "group_mes_example"]:
        text = data.get(field, "")
        if not text:
            continue
        for pattern in MARKDOWN_PATTERNS:
            matches = re.findall(pattern, text, re.MULTILINE)
            if matches:
                results.append({
                    "level": "warning",
                    "field": field,
                    "msg": f"检测到 Markdown 格式: {matches[0][:50]}",
                })
                break

    # 检查 mes_example 中的括号旁白
    for field in ["mes_example", "group_mes_example"]:
        text = data.get(field, "")
        if not text:
            continue
        for line in text.split("\n"):
            line = line.strip()
            if re.match(STAGE_DIRECTION_PATTERN, line):
                results.append({
                    "level": "warning",
                    "field": field,
                    "msg": f"检测到括号旁白开场: {line[:60]}",
                })
                break

    # 检查 system_prompt 中的翻译腔
    for field in ["system_prompt", "group_persona"]:
        text = data.get(field, "")
        if not text:
            continue
        for pattern in TRANSLATION_PATTERNS:
            if re.search(pattern, text):
                results.append({
                    "level": "info",
                    "field": field,
                    "msg": f"检测到可能的翻译腔句式: {pattern[:40]}",
                })
                break

    return results


def validate_consistency(card: dict) -> list[dict]:
    """验证角色卡内部一致性。"""
    results = []
    data = card.get("data", {})

    # 检查 mes_example 是否与 kaomoji_rule 一致
    kaomoji_rule = data.get("kaomoji_rule", "")
    mes_example = data.get("mes_example", "")

    if "不使用颜文字" in kaomoji_rule or "你不使用颜文字" in kaomoji_rule:
        # 检查 mes_example 中是否误用了颜文字
        common_kaomoji = ["(｡･ω･｡)", "(´｡• ᵕ •｡`)", "(⁄ ⁄•⁄ω⁄•⁄ ⁄)", "(￣ω￣)"]
        found = [k for k in common_kaomoji if k in mes_example]
        if found:
            results.append({
                "level": "warning",
                "field": "consistency",
                "msg": f"kaomoji_rule 声明不使用颜文字，但 mes_example 中出现了: {found}",
            })

    # 检查 mes_example 是否与 system_prompt 中的铁律一致
    system_prompt = data.get("system_prompt", "")
    if "禁止" in system_prompt:
        # 检查「禁止用括号写肢体动作」规则
        if "禁止" in system_prompt and "括号" in system_prompt:
            for line in mes_example.split("\n"):
                if re.match(STAGE_DIRECTION_PATTERN, line.strip()):
                    results.append({
                        "level": "warning",
                        "field": "consistency",
                        "msg": f"system_prompt 禁止括号旁白但 mes_example 中存在: {line[:60]}",
                    })
                    break

    # 检查 talkativeness 与 mes_example 平均长度是否匹配
    try:
        talkativeness = float(data.get("talkativeness", "0.5"))
    except (ValueError, TypeError):
        talkativeness = 0.5

    # 提取 {{char}} 的回复
    char_replies = re.findall(r"\{\{char\}\}:\s*(.+?)(?=\n\n|\n<START>|$)", mes_example, re.DOTALL)
    if char_replies:
        avg_len = sum(len(r.strip()) for r in char_replies) / len(char_replies)
        expected_short = talkativeness < 0.3
        is_short = avg_len < 15

        if expected_short and not is_short:
            results.append({
                "level": "info",
                "field": "consistency",
                "msg": f"talkativeness={talkativeness} (低) 但 mes_example 平均回复 {avg_len:.0f} 字符",
            })
        elif not expected_short and is_short and talkativeness > 0.7:
            results.append({
                "level": "info",
                "field": "consistency",
                "msg": f"talkativeness={talkativeness} (高) 但 mes_example 平均回复仅 {avg_len:.0f} 字符",
            })

    return results


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════


def validate_card(filepath: Path, strict: bool = False, show_tokens: bool = False) -> int:
    """验证单个角色卡。返回问题计数。"""
    print(f"\n{'='*60}")
    print(f"验证: {filepath.name}")
    print(f"{'='*60}")

    try:
        card = json.loads(filepath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  ❌ JSON 解析失败: {e}")
        return 1

    name = card.get("data", {}).get("name", "未知")
    print(f"  角色: {name}")
    print(f"  spec: {card.get('spec', 'N/A')} v{card.get('spec_version', 'N/A')}")

    all_issues = (
        validate_structure(card)
        + validate_lengths(card)
        + validate_content(card)
        + validate_consistency(card)
    )

    # 分类统计
    errors = [i for i in all_issues if i["level"] == "error"]
    warnings = [i for i in all_issues if i["level"] == "warning"]
    infos = [i for i in all_issues if i["level"] == "info"]

    # 输出
    def print_issues(issues, icon, label):
        if issues:
            print(f"\n  {icon} {label} ({len(issues)}):")
            for i in issues:
                print(f"    [{i['field']}] {i['msg']}")

    print_issues(errors, "❌", "错误")
    print_issues(warnings, "⚠️", "警告")
    print_issues(infos, "ℹ️", "建议")

    # Token 统计
    if show_tokens:
        data = card.get("data", {})
        permanent_fields = [
            "name", "description", "personality", "scenario",
            "system_prompt", "group_persona", "post_history_instructions",
            "companion_rules", "kaomoji_rule", "sticker_guide",
        ]
        non_permanent_fields = [
            "first_mes", "mes_example", "group_mes_example",
        ]

        print(f"\n  📊 Token 估算:")
        perm_tokens = 0
        for field in permanent_fields:
            tokens = estimate_tokens(data.get(field, ""))
            if tokens > 0:
                print(f"    {field}: ~{tokens} tokens")
                perm_tokens += tokens

        nonperm_tokens = 0
        for field in non_permanent_fields:
            text = data.get(field, "")
            if isinstance(text, list):
                text = " ".join(text)
            tokens = estimate_tokens(text)
            if tokens > 0:
                print(f"    {field} (非永久): ~{tokens} tokens")
                nonperm_tokens += tokens

        greetings_tokens = estimate_tokens(
            " ".join(data.get("alternate_greetings", []) + data.get("group_only_greetings", []))
        )
        tags_tokens = estimate_tokens(" ".join(data.get("tags", [])))

        total = perm_tokens + nonperm_tokens + greetings_tokens + tags_tokens
        print(f"    ───────────────────")
        print(f"    永久 token: ~{perm_tokens}")
        print(f"    非永久 token: ~{nonperm_tokens + greetings_tokens + tags_tokens}")
        print(f"    总计: ~{total} tokens")

        # 上下文占比
        for ctx_size in [32000, 64000, 128000]:
            pct = total / ctx_size * 100
            flag = " ✅" if pct < 50 else " ⚠️ >50%!"
            print(f"    {ctx_size} 上下文占比: {pct:.1f}%{flag}")

    # 总结
    total_issues = len(errors) + (len(warnings) if strict else 0)
    if total_issues == 0:
        print(f"\n  ✅ 验证通过" + (f" ({len(warnings)} 警告, {len(infos)} 建议)" if warnings or infos else ""))
    else:
        print(f"\n  ❌ {total_issues} 个问题需解决")

    return total_issues


def main():
    parser = argparse.ArgumentParser(description="角色卡验证工具")
    parser.add_argument("path", nargs="?", help="角色卡 JSON 文件路径")
    parser.add_argument("--dir", "-d", help="批量验证目录")
    parser.add_argument("--strict", "-s", action="store_true", help="严格模式 — 警告视为错误")
    parser.add_argument("--tokens", "-t", action="store_true", help="显示 token 统计")
    args = parser.parse_args()

    if args.dir:
        dir_path = Path(args.dir)
        if not dir_path.is_dir():
            print(f"错误: 目录不存在: {args.dir}")
            sys.exit(1)

        json_files = sorted(dir_path.glob("*.json"))
        # 排除 world_book 和 template
        json_files = [f for f in json_files if "_world_book" not in f.name and "TEMPLATE" not in f.name]

        if not json_files:
            print(f"目录中无角色卡 JSON: {args.dir}")
            sys.exit(1)

        print(f"批量验证 {len(json_files)} 个角色卡...")
        total_issues = 0
        for f in json_files:
            total_issues += validate_card(f, strict=args.strict, show_tokens=args.tokens)
        print(f"\n{'='*60}")
        print(f"总计: {total_issues} 个问题")
        sys.exit(1 if total_issues > 0 else 0)

    elif args.path:
        filepath = Path(args.path)
        if not filepath.exists():
            print(f"错误: 文件不存在: {args.path}")
            sys.exit(1)
        issues = validate_card(filepath, strict=args.strict, show_tokens=args.tokens)
        sys.exit(1 if issues > 0 else 0)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
