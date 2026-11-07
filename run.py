#!/usr/bin/env python3
"""从仓库根运行本插件的兼容入口。

    uv run python packages/testlens/run.py <command>

等价于 `cd packages/testlens && python -m testlens <command>`。保留这一层是因为
Windows 的 make 用 cmd.exe 执行 recipe，`PYTHONPATH=... <cmd>` 那种 POSIX 前置
赋值不生效；由脚本自己把发行根（本文件所在目录）放进 sys.path，跨平台行为一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 本文件在发行根 packages/testlens/，包目录是同一级的 testlens/
sys.path.insert(0, str(Path(__file__).resolve().parent))

from testlens.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
