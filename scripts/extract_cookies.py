#!/usr/bin/env python3
"""从 Chrome cookie SQLite DB 提取指定域名的 cookies → Netscape 格式 (yt-dlp 兼容)."""
import sqlite3
import sys
from pathlib import Path


def extract_cookies(db_path: str, output_path: str, domains: list[str]) -> int:
    """提取 cookies，返回提取的数量。"""
    uri = f"file:{db_path}?mode=ro"

    # Chrome 可能锁着 DB —— 先复制再读
    import shutil
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        shutil.copy2(db_path, tmp.name)
        uri = f"file:{tmp.name}?mode=ro"
    except OSError as e:
        print(f"  ⚠ 无法复制 DB, 尝试直接读取: {e}")

    try:
        conn = sqlite3.connect(uri, uri=True)
        # 构建 LIKE 查询
        where = " OR ".join(["host_key LIKE ?"] * len(domains))
        params = [f"%{d}%" for d in domains]
        rows = conn.execute(
            f"""SELECT host_key, name, value, path, expires_utc, is_secure, is_httponly
                FROM cookies
                WHERE {where}
                ORDER BY host_key, name""",
            params,
        ).fetchall()
        conn.close()
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    if not rows:
        return 0

    # 写 Netscape 格式
    with open(output_path, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write("# Extracted from Chrome\n\n")
        for host, name, val, path, exp, sec, http in rows:
            # Netscape 格式: domain_specified=TRUE 仅当泛域名 (以 . 开头)
            domain = host
            domain_specified = "TRUE" if host.startswith(".") else "FALSE"
            secure = "TRUE" if sec else "FALSE"
            # Chrome 时间戳: microseconds since 1601-01-01 → Unix epoch
            exp_ts = int(exp / 1_000_000) - 11644473600 if exp else 0
            f.write(f"{domain}\t{domain_specified}\t{path}\t{secure}\t{exp_ts}\t{name}\t{val}\n")

    for host, name, _val, _path, _exp, _sec, _http in rows:
        print(f"  {name} ({host})")

    return len(rows)


if __name__ == "__main__":
    db_path = sys.argv[1]
    output_path = sys.argv[2]
    domains = sys.argv[3:] if len(sys.argv) > 3 else ["douyin", "tiktok"]

    if not Path(db_path).exists():
        print(f"✗ 找不到 Chrome cookie 文件: {db_path}")
        sys.exit(1)

    count = extract_cookies(db_path, output_path, domains)

    if count == 0:
        print(f"✗ 没有找到相关 cookies (搜索: {domains})")
        print(f"  请先在 Chrome 中访问 douyin.com")
        sys.exit(1)

    print(f"{count} cookies → {output_path}")
