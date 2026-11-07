"""让 pytest 能 import testlens。

`tools/` 不是 workspace 成员，也不在仓库根的默认 sys.path 里；
把发行根（本文件的上两级）插进去后 `from testlens import ...` 才成立。
"""

import sys
from pathlib import Path

# packages/testlens/tests/conftest.py -> tests -> testlens（发行根，包在这里）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
