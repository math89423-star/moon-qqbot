#!/usr/bin/env python3
"""
角色卡 PNG 导出工具 — 将 chara_card_v3 JSON 嵌入 PNG 的 ccv3 + chara 块。

用法:
  # 导出单个角色卡
  python scripts/export_png_card.py characters/moon.json

  # 指定输出路径
  python scripts/export_png_card.py characters/moon.json -o ./exports/moon.png

  # 指定角色头像图
  python scripts/export_png_card.py characters/moon.json --avatar moon_avatar.png -o moon_full.png

  # 批量导出
  python scripts/export_png_card.py --dir astrbot_plugin_suli_tavern/characters/ -o ./exports/

  # 列表模式 (查看 JSON 中有哪些可导出)
  python scripts/export_png_card.py --list characters/moon.json

格式说明:
  - CCv3 块 (关键字: ccv3) — 现代 SillyTavern 版本读取
  - CCv2 块 (关键字: chara) — 旧版兼容
  - 双重块策略确保最大兼容性
  - 输出为 PNG 文件，可直接拖入 SillyTavern/Chub.ai/RisuAI

依赖: pip install Pillow
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import struct
import sys
import zlib
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

# Windows 终端 UTF-8 支持
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# PNG 块关键字
CCV3_KEYWORD = b"ccv3"
CCV2_KEYWORD = b"chara"

# 默认头像 — 1x1 紫色像素 PNG (最小有效 PNG)
DEFAULT_AVATAR_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "nGNgYGD4DwABBAEAcRNFhgAAAABJRU5ErkJggg=="
)


def _make_text_chunk(keyword: bytes, text: bytes) -> bytes:
    """构建 PNG tEXt 块。"""
    # tEXt 块: keyword\0text
    chunk_data = keyword + b"\x00" + text
    crc = zlib.crc32(b"tEXt" + chunk_data) & 0xFFFFFFFF
    length = struct.pack(">I", len(chunk_data))
    return length + b"tEXt" + chunk_data + struct.pack(">I", crc)


def _decode_default_avatar() -> bytes:
    """解码默认 1x1 头像。"""
    return base64.b64decode(DEFAULT_AVATAR_B64)


def _load_or_default_avatar(avatar_path: Optional[str]) -> bytes:
    """加载头像图片或使用默认值。"""
    if avatar_path:
        p = Path(avatar_path)
        if not p.exists():
            logger.warning(f"头像文件不存在: {avatar_path}, 使用默认头像")
        else:
            return p.read_bytes()

    return _decode_default_avatar()


def embed_json_in_png(
    card_json: dict,
    avatar_bytes: Optional[bytes] = None,
    embed_v2: bool = True,
) -> bytes:
    """将角色卡 JSON 嵌入 PNG。

    Args:
        card_json: chara_card_v3 角色卡 dict
        avatar_bytes: 头像 PNG 字节 (可选，默认 1x1 像素)
        embed_v2: 是否同时嵌入 CCv2 chara 块 (向后兼容)

    Returns:
        PNG 文件字节
    """
    if avatar_bytes is None:
        avatar_bytes = _decode_default_avatar()

    # 验证 PNG 签名
    if avatar_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("头像文件不是有效的 PNG")

    json_str = json.dumps(card_json, ensure_ascii=False, separators=(",", ":"))
    json_bytes = json_str.encode("utf-8")

    # 检查大小: SillyTavern 字符卡限制通常 ~20MB PNG——JSON 部分应该没问题
    logger.info(f"JSON 压缩前: {len(json_bytes)} 字节 (~{len(json_str)} 字符)")

    # 读取头像 PNG 结构
    # PNG: signature + chunks... + IEND
    # 我们在 IDAT 之后、IEND 之前插入 tEXt 块
    chunks = []
    pos = 8  # 跳过 PNG signature

    while pos < len(avatar_bytes):
        length = struct.unpack(">I", avatar_bytes[pos : pos + 4])[0]
        chunk_type = avatar_bytes[pos + 4 : pos + 8]
        chunk_data = avatar_bytes[pos + 8 : pos + 8 + length]
        chunk_crc = avatar_bytes[pos + 8 + length : pos + 12 + length]

        chunks.append({
            "type": chunk_type,
            "data": chunk_data,
            "full": avatar_bytes[pos : pos + 12 + length],
        })
        pos += 12 + length

        if chunk_type == b"IEND":
            break

    # 找到 IDAT 结束位置（最后一个 IDAT 块之后）
    last_idat_idx = -1
    for i, chunk in enumerate(chunks):
        if chunk["type"] == b"IDAT":
            last_idat_idx = i

    if last_idat_idx < 0:
        raise ValueError("头像 PNG 中没有 IDAT 块")

    # 构建输出: 在最后一个 IDAT 之后、IEND 之前插入 tEXt 块
    output = bytearray()
    output.extend(avatar_bytes[:8])  # PNG signature

    for i, chunk in enumerate(chunks):
        output.extend(chunk["full"])

        # 在最后一个 IDAT 之后插入元数据块
        if i == last_idat_idx:
            # CCv3 块 (现代)
            ccv3_chunk = _make_text_chunk(CCV3_KEYWORD, json_bytes)
            output.extend(ccv3_chunk)
            logger.info(f"嵌入 CCv3 块: {len(ccv3_chunk)} 字节")

            # CCv2 块 (兼容)
            if embed_v2:
                ccv2_chunk = _make_text_chunk(CCV2_KEYWORD, json_bytes)
                output.extend(ccv2_chunk)
                logger.info(f"嵌入 CCv2 块: {len(ccv2_chunk)} 字节")

    return bytes(output)


def extract_json_from_png(png_path: str) -> Optional[dict]:
    """从 PNG 中提取角色卡 JSON (反向操作)。尝试 ccv3 → chara → 失败。"""
    from struct import unpack

    with open(png_path, "rb") as f:
        data = f.read()

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是有效的 PNG 文件")

    pos = 8
    while pos < len(data):
        length = unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk_data = data[pos + 8 : pos + 8 + length]

        if chunk_type == b"tEXt":
            # 解析 tEXt: keyword\0text
            null_pos = chunk_data.find(b"\x00")
            if null_pos > 0:
                keyword = chunk_data[:null_pos]
                text = chunk_data[null_pos + 1:]
                if keyword == CCV3_KEYWORD or keyword == CCV2_KEYWORD:
                    try:
                        return json.loads(text.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        logger.warning(f"块 {keyword.decode()} 中的 JSON 无效")

        pos += 12 + length
        if chunk_type == b"IEND":
            break

    return None


def list_card_info(card: dict):
    """列出角色卡的可导出信息。"""
    data = card.get("data", {})
    print(f"角色: {data.get('name', 'N/A')}")
    print(f"版本: {data.get('character_version', 'N/A')}")
    print(f"spec: {card.get('spec')} v{card.get('spec_version')}")
    print(f"标签: {', '.join(data.get('tags', []))}")
    print(f"字段:")
    for field in sorted(data.keys()):
        val = data[field]
        if isinstance(val, str):
            print(f"  {field}: {len(val)} 字符")
        elif isinstance(val, list):
            print(f"  {field}: [{len(val)} 项]")
        elif isinstance(val, dict):
            print(f"  {field}: {{...}}")
        else:
            print(f"  {field}: {val}")
    print(f"\n总 JSON 大小: {len(json.dumps(card, ensure_ascii=False))} 字符")


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="角色卡 PNG 导出工具")
    parser.add_argument("path", nargs="?", help="角色卡 JSON 文件路径")
    parser.add_argument("--dir", "-d", help="批量导出目录")
    parser.add_argument("--output", "-o", help="输出 PNG 路径 (或批量输出目录)")
    parser.add_argument("--avatar", "-a", help="头像 PNG 文件路径")
    parser.add_argument("--list", "-l", action="store_true", help="列出角色卡信息 (不导出)")
    parser.add_argument("--extract", "-x", help="从 PNG 中提取 JSON")
    parser.add_argument("--no-v2", action="store_true", help="不嵌入 CCv2 chara 块")
    args = parser.parse_args()

    # ── 提取模式 ──
    if args.extract:
        card = extract_json_from_png(args.extract)
        if card:
            print(json.dumps(card, ensure_ascii=False, indent=2))
        else:
            logger.error(f"未找到嵌入的角色卡: {args.extract}")
            sys.exit(1)
        return

    # ── 列表模式 ──
    if args.list and args.path:
        card = json.loads(Path(args.path).read_text(encoding="utf-8"))
        list_card_info(card)
        return

    # ── 导出模式 ──
    if args.path and not args.dir:
        json_path = Path(args.path)
        if not json_path.exists():
            logger.error(f"文件不存在: {args.path}")
            sys.exit(1)

        card = json.loads(json_path.read_text(encoding="utf-8"))
        name = card.get("data", {}).get("name", json_path.stem)

        # 确定输出路径
        if args.output:
            out_path = Path(args.output)
        else:
            out_path = json_path.with_suffix(".png")

        avatar_bytes = _load_or_default_avatar(args.avatar)
        png_bytes = embed_json_in_png(card, avatar_bytes, embed_v2=not args.no_v2)

        out_path.write_bytes(png_bytes)
        logger.info(f"已导出: {out_path} ({len(png_bytes)} 字节)")
        logger.info(f"可拖入 SillyTavern / Chub.ai / RisuAI 使用")

    elif args.dir:
        dir_path = Path(args.dir)
        if not dir_path.is_dir():
            logger.error(f"目录不存在: {args.dir}")
            sys.exit(1)

        out_dir = Path(args.output) if args.output else dir_path / "png_exports"
        out_dir.mkdir(parents=True, exist_ok=True)

        json_files = sorted(dir_path.glob("*.json"))
        json_files = [f for f in json_files if "_world_book" not in f.name and "TEMPLATE" not in f.name]

        if not json_files:
            logger.error(f"目录中无角色卡: {args.dir}")
            sys.exit(1)

        avatar_bytes = _load_or_default_avatar(args.avatar)
        for f in json_files:
            card = json.loads(f.read_text(encoding="utf-8"))
            name = card.get("data", {}).get("name", f.stem)
            png_bytes = embed_json_in_png(card, avatar_bytes, embed_v2=not args.no_v2)
            out_file = out_dir / f"{name}.png"
            out_file.write_bytes(png_bytes)
            logger.info(f"已导出: {out_file} ({len(png_bytes)} 字节)")

        logger.info(f"\n共导出 {len(json_files)} 个角色卡到 {out_dir}")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
