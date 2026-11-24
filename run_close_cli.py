#!/usr/bin/env python3
"""顶层运行包装：在任何调用方式（含 ``python -I``）下都能找到本地包。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from inventory_close.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
