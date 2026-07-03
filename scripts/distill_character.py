#!/usr/bin/env python3
"""
角色蒸馏工具 — 从源材料提取角色特质，生成 chara_card_v3 角色卡 JSON。

用法:
  # 从 Fandom/wiki URL 蒸馏
  python scripts/distill_character.py --url "https://xxxx.fandom.com/wiki/CharacterName" --name "角色名"

  # 从文本文件蒸馏
  python scripts/distill_character.py --file "source_material.txt" --name "角色名"

  # 从管道输入蒸馏
  cat source.txt | python scripts/distill_character.py --stdin --name "角色名"

  # 指定输出目录（默认为 astrbot_plugin_suli_tavern/characters/）
  python scripts/distill_character.py --file source.txt --name "角色名" --outDir "./my_characters"

  # 仅生成骨架（不调用 LLM）
  python scripts/distill_character.py --skeleton --name "角色名"

  # 交互模式：手动逐字段输入
  python scripts/distill_character.py --interactive --name "角色名"

依赖:
  pip install openai beautifulsoup4 requests

LLM 配置:
  默认使用环境变量:
    DISTILL_API_KEY  — API 密钥
    DISTILL_API_BASE — API Base URL (默认 https://api.deepseek.com/v1)
    DISTILL_MODEL    — 模型名称 (默认 deepseek-v4-pro)

  也支持 AstrBot 的 bot_db 配置（自动检测 data/bot.db）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
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

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── 角色卡模板路径 ──────────────────────────────
_TEMPLATE_PATH = Path(__file__).resolve().parent / "character_template.json"

# ── 蒸馏 system prompt 路径 ─────────────────────
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "distill_system_prompt.md"

# ── 默认输出目录 ───────────────────────────────
_DEFAULT_OUT_DIR = (
    Path(__file__).resolve().parent.parent / "astrbot_plugin_suli_tavern" / "characters"
)


def load_template() -> dict:
    """加载角色卡模板 JSON。"""
    if _TEMPLATE_PATH.exists():
        with open(_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    # fallback: minimal skeleton
    return {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "",
            "description": "",
            "personality": "",
            "scenario": "",
            "talkativeness": "0.5",
            "first_mes": "",
            "mes_example": "",
            "system_prompt": "",
            "post_history_instructions": "",
            "alternate_greetings": [],
            "group_only_greetings": [],
            "nickname": "",
            "creator_notes": "由 distill_character.py 生成",
            "tags": [],
            "character_version": "1.0.0",
            "group_persona": "",
            "group_mes_example": "",
            "role_description": "",
            "kaomoji_rule": "",
            "companion_rules": "",
            "sticker_guide": "",
        },
    }


def load_prompt_template() -> str:
    """加载蒸馏 system prompt 模板。"""
    if _PROMPT_PATH.exists():
        return _PROMPT_PATH.read_text(encoding="utf-8")
    logger.warning("蒸馏 prompt 模板未找到，使用内置简化版")
    return "你是一位角色设计师。从以下材料提取角色特质，生成 chara_card_v3 JSON。"


# ═══════════════════════════════════════════════════════════════
# 源材料加载
# ═══════════════════════════════════════════════════════════════


def fetch_url(url: str) -> str:
    """从 URL 抓取文本内容（处理 wiki/fandom 页面）。"""
    import requests
    from bs4 import BeautifulSoup

    logger.info(f"抓取 URL: {url}")
    resp = requests.get(url, timeout=30, headers={"User-Agent": "CharacterDistill/1.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # 移除 script/style/nav 等非内容元素
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", ".sidebar", ".navbox"]):
        tag.decompose()

    # 优先提取主内容区 (fandom/wiki 常见选择器)
    content_selectors = [
        ".mw-parser-output",
        "#mw-content-text",
        ".page-content",
        ".article-content",
        "article",
        "main",
        ".main-content",
    ]
    content = None
    for sel in content_selectors:
        content = soup.select_one(sel)
        if content:
            break

    if not content:
        content = soup.body or soup

    text = content.get_text(separator="\n", strip=True)
    # 压缩空白行
    text = re.sub(r"\n{3,}", "\n\n", text)
    logger.info(f"抓取完成: {len(text)} 字符")
    return text


def load_file(path: str) -> str:
    """从文件加载源材料。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    text = p.read_text(encoding="utf-8")
    logger.info(f"加载文件: {path} ({len(text)} 字符)")
    return text


# ═══════════════════════════════════════════════════════════════
# LLM 调用
# ═══════════════════════════════════════════════════════════════


def _get_llm_config() -> dict:
    """获取 LLM 配置 (环境变量 -> AstrBot DB -> 默认值)。"""
    config = {
        "api_key": os.environ.get("DISTILL_API_KEY", ""),
        "api_base": os.environ.get("DISTILL_API_BASE", "https://api.deepseek.com/v1"),
        "model": os.environ.get("DISTILL_MODEL", "deepseek-v4-pro"),
    }

    # 尝试从 AstrBot bot_db 读取
    if not config["api_key"]:
        try:
            _astrbot_db_paths = [
                Path("data/bot.db"),
                Path("../data/bot.db"),
                Path(__file__).resolve().parent.parent / "data" / "bot.db",
            ]
            for db_path in _astrbot_db_paths:
                if db_path.exists():
                    import sqlite3

                    conn = sqlite3.connect(str(db_path))
                    row = conn.execute(
                        "SELECT value FROM config WHERE key = 'llm_api_key'"
                    ).fetchone()
                    if row:
                        config["api_key"] = row[0]
                        logger.info(f"从 {db_path} 读取 API key")
                    conn.close()
                    break
        except Exception:
            pass

    return config


def call_llm(system_prompt: str, user_prompt: str, config: dict) -> str:
    """调用 LLM API 进行角色蒸馏。"""
    from openai import OpenAI

    if not config["api_key"]:
        raise RuntimeError(
            "未配置 API key。请设置环境变量 DISTILL_API_KEY，"
            "或在 AstrBot 的 bot.db 中配置 llm_api_key"
        )

    client = OpenAI(api_key=config["api_key"], base_url=config["api_base"])
    model = config["model"]

    logger.info(f"调用 LLM: {model} @ {config['api_base']}")
    logger.info(f"System prompt: {len(system_prompt)} 字符")
    logger.info(f"User prompt: {len(user_prompt)} 字符")

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
        max_tokens=16384,
    )

    content = response.choices[0].message.content or ""
    logger.info(
        f"LLM 响应: {len(content)} 字符, "
        f"tokens: {response.usage.total_tokens if response.usage else 'N/A'}"
    )
    return content


# ═══════════════════════════════════════════════════════════════
# JSON 提取与验证
# ═══════════════════════════════════════════════════════════════


def extract_json(text: str) -> dict:
    """从 LLM 响应中提取 JSON 对象。处理 markdown 代码块包裹。"""
    # 尝试直接解析
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试提取 ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 尝试提取最外层 { ... }
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"无法从 LLM 响应中提取 JSON。响应开头: {text[:200]}...")


def validate_card(card: dict, strict: bool = False) -> list[str]:
    """验证角色卡结构。返回警告/错误列表。"""
    issues: list[str] = []

    if card.get("spec") not in ("chara_card_v3", "chara_card_v2"):
        issues.append("spec 不是 chara_card_v3")

    data = card.get("data", {})
    if not data:
        issues.append("缺少 data 字段")
        return issues

    # 必需字段
    required = {
        "name": "角色名",
        "description": "描述",
        "personality": "性格",
        "first_mes": "开场白",
    }
    for field, label in required.items():
        if not data.get(field):
            issues.append(f"缺少必需字段: {field} ({label})")

    # 长度警告
    if len(data.get("description", "")) < 50:
        issues.append("description 过短 (< 50 字符)")
    if len(data.get("personality", "")) < 200:
        issues.append("personality 过短 (< 200 字符)")
    if len(data.get("first_mes", "")) < 10:
        issues.append("first_mes 过短 (< 10 字符)")

    mes_example = data.get("mes_example", "")
    if mes_example:
        blocks = mes_example.count("<START>")
        if blocks < 3:
            issues.append(f"mes_example 对话块过少 ({blocks} < 建议 5)")

    # 群聊字段
    if not data.get("group_persona"):
        issues.append("缺少 group_persona（群聊角色卡强烈建议填写）")

    # 第一人称检查
    for field in ["description", "personality"]:
        text = data.get(field, "")
        if re.search(r"(?<!\{\{)我(?!\}\})", text):
            # 粗略检测：如果「我」出现且不在 {{char}} 语境中
            if "我" in text and "{{char}}" not in text:
                issues.append(f"{field} 可能使用了第一人称（建议用 {{{{char}}}}）")

    return issues


# ═══════════════════════════════════════════════════════════════
# 交互模式
# ═══════════════════════════════════════════════════════════════


def interactive_mode(name: str) -> dict:
    """交互式逐字段输入角色卡。"""
    card = load_template()
    card["data"]["name"] = name
    card["data"]["nickname"] = name

    fields = [
        ("description", "外貌+背景描述 (200-350 tokens)"),
        ("personality", "性格分层描述 (400-800 tokens)"),
        ("scenario", "互动场景 (50-100 tokens)"),
        ("talkativeness", "话量 (0.0-1.0)"),
        ("first_mes", "开场白 (2-5 句)"),
        ("role_description", "一句话标签 (5-15 字)"),
    ]

    print(f"\n{'='*60}")
    print(f"交互式角色卡创建: {name}")
    print(f"{'='*60}")
    print("每个字段输入完成后按 Enter。直接按 Enter 跳过。")
    print("输入多行内容时，以单独一行的 'END' 结束。\n")

    for field, desc in fields:
        print(f"\n--- {field} ({desc}) ---")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() == "END":
                break
            lines.append(line)
        if lines:
            card["data"][field] = "\n".join(lines).strip()

    # tags
    tags_input = input("\n--- tags (逗号分隔) ---\n").strip()
    if tags_input:
        card["data"]["tags"] = [t.strip() for t in tags_input.split(",") if t.strip()]

    return card


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="角色蒸馏工具 — 从源材料生成 chara_card_v3 角色卡",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--name", "-n", required=True, help="角色名称")
    parser.add_argument("--url", "-u", help="源材料 URL（Fandom/wiki 等）")
    parser.add_argument("--file", "-f", help="源材料文本文件路径")
    parser.add_argument("--stdin", action="store_true", help="从标准输入读取源材料")
    parser.add_argument(
        "--outDir", "-o", default=str(_DEFAULT_OUT_DIR), help="输出目录"
    )
    parser.add_argument("--skeleton", action="store_true", help="仅生成骨架 JSON（不调用 LLM）")
    parser.add_argument("--interactive", "-i", action="store_true", help="交互模式：手动逐字段输入")
    parser.add_argument(
        "--apiKey", help="LLM API Key（也可用 DISTILL_API_KEY 环境变量）"
    )
    parser.add_argument(
        "--apiBase", help="LLM API Base URL（也可用 DISTILL_API_BASE 环境变量）"
    )
    parser.add_argument(
        "--model", help="LLM 模型名称（也可用 DISTILL_MODEL 环境变量）"
    )
    parser.add_argument("--dryRun", action="store_true", help="仅打印 JSON 到 stdout，不写文件")

    args = parser.parse_args()

    out_dir = Path(args.outDir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 交互模式 ──
    if args.interactive:
        card = interactive_mode(args.name)
        out_path = out_dir / f"{args.name}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(card, f, ensure_ascii=False, indent=2)
        logger.info(f"角色卡已保存: {out_path}")
        return

    # ── 骨架模式 ──
    if args.skeleton:
        card = load_template()
        card["data"]["name"] = args.name
        card["data"]["nickname"] = args.name
        card["data"]["creator_notes"] = "骨架 — 待填充"

        out_path = out_dir / f"{args.name}_skeleton.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(card, f, ensure_ascii=False, indent=2)
        logger.info(f"骨架已保存: {out_path}")
        return

    # ── 加载源材料 ──
    sources = []
    if args.url:
        sources.append(f"[来源 URL: {args.url}]\n\n{fetch_url(args.url)}")
    if args.file:
        sources.append(f"[来源文件: {args.file}]\n\n{load_file(args.file)}")
    if args.stdin:
        stdin_text = sys.stdin.read()
        if stdin_text.strip():
            sources.append(f"[来源: 标准输入]\n\n{stdin_text.strip()}")

    if not sources:
        logger.error("请提供源材料: --url、--file 或 --stdin")
        sys.exit(1)

    source_text = "\n\n---\n\n".join(sources)

    # ── 截断过长的源材料 ──
    max_source_chars = 30000
    if len(source_text) > max_source_chars:
        logger.warning(f"源材料过长 ({len(source_text)} 字符)，截断至 {max_source_chars}")
        source_text = source_text[:max_source_chars] + "\n\n[... 内容已截断 ...]"

    # ── 构建 prompt ──
    system_prompt = load_prompt_template()
    user_prompt = f"请为以下角色生成完整的 chara_card_v3 角色卡：\n\n角色名称：{args.name}\n\n{source_text}"

    # ── LLM 配置 ──
    llm_config = _get_llm_config()
    if args.apiKey:
        llm_config["api_key"] = args.apiKey
    if args.apiBase:
        llm_config["api_base"] = args.apiBase
    if args.model:
        llm_config["model"] = args.model

    # ── 调用 LLM ──
    response = call_llm(system_prompt, user_prompt, llm_config)

    # ── 提取并验证 JSON ──
    card = extract_json(response)

    # 确保名称正确
    if not card.get("data", {}).get("name"):
        card.setdefault("data", {})["name"] = args.name
    card["data"]["character_version"] = "1.0.0"
    if "creator_notes" not in card["data"]:
        card["data"]["creator_notes"] = f"由 distill_character.py 从源材料蒸馏生成"

    issues = validate_card(card)
    if issues:
        logger.warning("验证警告:")
        for issue in issues:
            logger.warning(f"  - {issue}")

    # ── 输出 ──
    if args.dryRun:
        print(json.dumps(card, ensure_ascii=False, indent=2))
    else:
        safe_name = re.sub(r"[^\w\-_.]", "_", args.name)
        out_path = out_dir / f"{safe_name}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(card, f, ensure_ascii=False, indent=2)
        logger.info(f"角色卡已保存: {out_path}")

        # 同时输出 world_book 骨架提示
        wb_path = out_dir / f"{safe_name}_world_book.json"
        if not wb_path.exists():
            skeleton_wb = {
                "_meta": {
                    "name": f"{args.name} 世界书",
                    "version": "1.0.0",
                    "description": f"{args.name} 的世界知识库 — 待填充",
                    "scan_depth": 8,
                    "insert_position": "after_character",
                },
                "entries": [],
                "version": "1.0.0",
            }
            with open(wb_path, "w", encoding="utf-8") as f:
                json.dump(skeleton_wb, f, ensure_ascii=False, indent=2)
            logger.info(f"世界书骨架已保存: {wb_path}")


if __name__ == "__main__":
    main()
