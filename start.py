#!/usr/bin/env python3
"""启动入口：python start.py  （端口 8123 起自动找空闲端口）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    main()
