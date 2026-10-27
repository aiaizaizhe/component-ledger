#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把数据位置写到 config.json（由「更换数据位置.bat」调用）。"""

import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"


def main() -> int:
    if len(sys.argv) < 2:
        print("没有收到路径")
        return 1
    target = sys.argv[1].strip().strip('"').strip()
    if not target:
        return 1

    cfg = {}
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["data_dir"] = target
    cfg.setdefault("port", 8787)
    cfg.setdefault("host", "127.0.0.1")
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    folder = Path(target)
    if not folder.is_absolute():
        folder = (BASE / folder).resolve()
    print(f"数据位置已设为：{folder}")
    print("下次启动会在那里创建 components.db（不存在会自动建文件夹）。")
    print("如果旧位置已经有数据，请先把旧的 components.db 复制过去，再启动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
