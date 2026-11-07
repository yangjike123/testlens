"""跑得慢的用例：演示实时输出、停止按钮、按脚本耗时。"""

from __future__ import annotations

import time


def test_quick_smoke() -> None:
    time.sleep(0.2)


def test_slow_report_generation() -> None:
    """等 3 秒：正好够点一次「停止」，也能在耗时面板里看到明显的条。"""
    time.sleep(3.0)
    assert True
