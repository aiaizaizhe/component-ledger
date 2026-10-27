#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""备份元件库数据库（SQLite 在线备份，服务开着也能安全备份）。"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent


def resolve_data_dir() -> Path:
    chosen = "data"
    config_path = BASE / "config.json"
    if config_path.exists():
        try:
            chosen = (json.loads(config_path.read_text(encoding="utf-8")) or {}).get("data_dir") or "data"
        except Exception:
            chosen = "data"
    path = Path(chosen)
    return path if path.is_absolute() else (BASE / path).resolve()


def main() -> int:
    data_dir = resolve_data_dir()
    src = data_dir / "components.db"
    if not src.exists():
        print(f"还没有数据文件：{src}")
        print("先启动一次服务再说。")
        return 1

    out_dir = BASE / "backup"
    out_dir.mkdir(exist_ok=True)
    dst = out_dir / f"components_{datetime.now():%Y%m%d_%H%M%S}.db"

    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(dst)
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()

    size = dst.stat().st_size / 1024
    print(f"已备份到：{dst}")
    print(f"文件大小：{size:.1f} KB")
    print(f"备份目录：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
