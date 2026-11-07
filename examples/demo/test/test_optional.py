"""跳过的用例：演示面板里的「跳过」计数与 skipped 状态。"""

from __future__ import annotations

import sys

import pytest


@pytest.mark.skip(reason="演示「跳过」：这条不跑")
def test_planned_feature() -> None:
    assert False


@pytest.mark.skipif(sys.platform != "linux", reason="只在 Linux 上跑（Windows/macOS 会跳过）")
def test_linux_only_path() -> None:
    assert sys.platform == "linux"
