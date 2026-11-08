"""让 pytest 能 import testlens。

测试直接从仓库根跑（`python -m pytest`），包目录不一定在 sys.path 里；
把发行根（本文件的上两级）插进去后 `from testlens import ...` 才成立。
"""

import sys
from pathlib import Path

# tests/conftest.py -> tests -> 仓库根（包目录 testlens/ 就在这里）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
