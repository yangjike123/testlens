"""打包契约：pytest 必须是 testlens 的必需依赖。

用户 2026-11-08 明确要求：装 testlens 就要把 pytest 装上，因为运行器、junit 解析、
`--basetemp` / `--cov` 这些能力都建立在 pytest 上；不能再回到“零依赖”让用户自己补。
"""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_pytest_is_a_required_dependency() -> None:
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = data["project"]["dependencies"]

    pytest_deps = [item for item in dependencies if item.startswith("pytest")]
    assert pytest_deps == ["pytest>=8"], (
        "pytest 必须是必需依赖（不能退回 optional/dev），"
        f"当前 dependencies={dependencies!r}"
    )
