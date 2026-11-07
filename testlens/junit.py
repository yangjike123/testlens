"""把 pytest 的 junitxml 变成面板能直接展示的运行结果。

为什么要这一步：面板原来只知道「这次运行失败了、用了 3.2 秒」，不知道
**哪个脚本慢、哪条用例挂了**。pytest 原生的 `--junitxml` 已经包含用例级的
耗时和失败信息，用它比再加一个 json-report 插件划算得多。

解析结果只保留面板要展示的字段，并且做两件事防止体积失控：

1. 最慢用例只留前 N 条；
2. 失败信息只留首行并截断（完整 traceback 在运行日志里，不重复存）。

路径归一化：junit 里的 `file` 相对 pytest 的 rootdir（本仓库每个包都有自己的
pyproject.toml，所以是相对包目录）。这里统一拼成仓库相对路径，前端才能和
脚本清单里的 path 对上。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .config import PackageSpec, root_prefix

SLOWEST_LIMIT = 10
MESSAGE_LIMIT = 300


def normalize_file(raw: str | None, package: PackageSpec) -> str:
    """junit 的 file 属性 → 仓库相对路径（正斜杠）。"""
    if not raw:
        return ""
    path = raw.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    prefix = root_prefix(package)
    if prefix and (path.startswith(prefix) or path.rstrip("/") == package.root):
        return path
    return f"{prefix}{path}"


def file_from_classname(classname: str | None, package: PackageSpec, repo_root: Path) -> str:
    """从 classname 反推测试文件路径。

    pytest 的 junitxml 默认只给 `classname="test.unit.test_config"`，没有
    `file` 属性（不同版本行为不一致）。这里把点分路径逐级试成 .py，并用
    「文件是否存在」把类名那一层剥掉（`...test_module.TestClass`）。

    monorepo 里 pytest 从仓库根跑时，classname 会带上包根
    （`services.api.test.test_health`），此时不能再补一遍 `services/api/`，
    否则会拼成 `services/api/services/api/...`，所有文件都归不到包上。
    """
    parts = [part for part in (classname or "").split(".") if part]
    prefix = root_prefix(package)
    while parts:
        dotted = "/".join(parts) + ".py"
        candidate = dotted if prefix and dotted.startswith(prefix) else f"{prefix}{dotted}"
        if (repo_root / candidate).is_file():
            return candidate
        parts.pop()
    return ""


def _case_file(case: ET.Element, package: PackageSpec, repo_root: Path) -> str:
    raw = case.get("file")
    if raw:
        return normalize_file(raw, package)
    return file_from_classname(case.get("classname"), package, repo_root)


def _case_status(case: ET.Element) -> str:
    if case.find("failure") is not None or case.find("error") is not None:
        return "failed"
    if case.find("skipped") is not None:
        return "skipped"
    return "passed"


def _failure_message(case: ET.Element) -> str:
    node = case.find("failure")
    if node is None:
        node = case.find("error")
    if node is None:
        return ""
    text = (node.get("message") or "").strip()
    if not text:
        text = (node.text or "").strip().splitlines()[0] if (node.text or "").strip() else ""
    return text.splitlines()[0][:MESSAGE_LIMIT] if text else ""


def parse_files(
    paths: list[Path], package: PackageSpec, repo_root: Path
) -> dict | None:
    """解析一到多个 junit 文件并合并统计。全部不可用时返回 None。

    多个文件是防御性的：`build_commands` 理论上只会产出一条命令（一次运行只
    属于一个包），但万一按包拆成多批，也不该丢掉其中一批的结果。
    """
    totals = {"tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "time": 0.0}
    files: dict[str, dict] = {}
    slowest: list[dict] = []
    failures: list[dict] = []
    parsed_any = False

    for path in paths:
        if not path.is_file():
            continue
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            # 超时被杀时 XML 一定是半截的：跳过，不影响这次运行的其它结果
            continue
        parsed_any = True

        for case in root.iter("testcase"):
            try:
                cost = float(case.get("time") or 0)
            except ValueError:
                cost = 0.0

            status = _case_status(case)
            file_path = _case_file(case, package, repo_root)
            name = case.get("name") or "（未命名）"

            totals["tests"] += 1
            totals["time"] += cost
            if status == "failed":
                # error 与 failure 在 junit 里是两种节点，分开计数更有信息量
                totals["errors" if case.find("error") is not None else "failed"] += 1
            else:
                totals[status] += 1

            if file_path:
                item = files.setdefault(
                    file_path, {"path": file_path, "tests": 0, "failed": 0, "skipped": 0, "time": 0.0}
                )
                item["tests"] += 1
                item["time"] += cost
                if status == "failed":
                    item["failed"] += 1
                elif status == "skipped":
                    item["skipped"] += 1

            slowest.append(
                {
                    "name": name,
                    "file": file_path,
                    "time": round(cost, 3),
                    "status": status,
                }
            )
            if status == "failed":
                failures.append(
                    {
                        "name": name,
                        "file": file_path,
                        "message": _failure_message(case),
                    }
                )

    if not parsed_any:
        return None

    totals["time"] = round(totals["time"], 2)
    ordered_files = sorted(files.values(), key=lambda item: item["time"], reverse=True)
    for item in ordered_files:
        item["time"] = round(item["time"], 3)

    return {
        "totals": totals,
        "files": ordered_files,
        "slowest": sorted(slowest, key=lambda item: item["time"], reverse=True)[:SLOWEST_LIMIT],
        "failures": failures,
    }
