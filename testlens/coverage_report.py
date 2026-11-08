"""覆盖率服务（CoverageService）：把 coverage.py 的数据文件读成面板能渲染的结构。

数据来源是每个 PackageSpec 里 `coverage_data_file` 指定的文件（如
`.coverage.web`）。不自己解析 coverage 的私有 SQLite：直接调一次
`coverage json`（官方格式，字段稳定），再把结果精简、缓存。

为什么要缓存：`coverage json` 要起一个 Python 子进程（约 0.5s），而面板
每打开一次覆盖率页签就会请求一次。用数据文件的 mtime 做版本，数据没变
就直接读缓存。缓存位置 `<data_dir>/coverage-cache/`。

包没有配 coverage_data_file → 抛 NoCoverageData（面板显示「未配置覆盖率」，
不隐藏页签）。子进程命令来自 `config.coverage_json_cmd`（默认
`uv run coverage json`，带引号或 Windows 反斜杠路径都能正确拆分），
去 uv 化只需换配置。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .config import Config, PackageSpec, split_command


class NoCoverageData(Exception):
    """该包没有可用的覆盖率数据（未配置 / 没跑过 / 命令失败）。"""


class CoverageService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.cache_dir = config.data_dir / "coverage-cache"

    # -- 对外 ----------------------------------------------------------
    def report(self, package: PackageSpec) -> dict:
        """总览 + 文件清单。数据没变时直接读缓存。"""
        data_file = self._data_file(package)
        if data_file is None or not data_file.is_file():
            raise NoCoverageData(self._missing_message(package))

        stat = data_file.stat()
        cache = self._cache_file(package)
        if cache.is_file():
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached = None
            if isinstance(cached, dict) and cached.get("source_mtime_ns") == stat.st_mtime_ns:
                return cached

        normalized = self._normalize(
            package, self._run_coverage_json(data_file), stat.st_mtime_ns, stat.st_mtime
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temp = cache.with_suffix(".json.tmp")
        temp.write_text(json.dumps(normalized, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, cache)
        return normalized

    def file_detail(self, package: PackageSpec, relative_path: str) -> dict:
        """某文件的未覆盖行 + 每处缺失的源码上下文。"""
        summary = self.report(package)
        entry = next((item for item in summary["files"] if item["path"] == relative_path), None)
        if entry is None:
            raise NoCoverageData(f"覆盖率报告里没有这个文件：{relative_path}")

        source_path = (self.config.repo_root / relative_path).resolve()
        try:
            source_path.relative_to(self.config.repo_root.resolve())
        except ValueError as exc:
            raise NoCoverageData("文件不在仓库内") from exc
        if not source_path.is_file():
            raise NoCoverageData(f"源文件不存在：{relative_path}")

        lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
        total_lines = len(lines)
        # 覆盖率数据可能是上一次跑出来的：源码删过行时 missing 行号会越界，先滤掉
        missing = {number for number in entry["missing_lines"] if 1 <= number <= total_lines}
        hunks = []
        for block in self._blocks(sorted(missing), total_lines):
            hunks.append(
                {
                    "start": block["start"],
                    "end": block["end"],
                    "lines": [
                        {
                            "no": number,
                            "code": lines[number - 1],
                            "missing": number in missing,
                        }
                        for number in range(block["start"], block["end"] + 1)
                    ],
                }
            )

        return {
            "path": entry["path"],
            "percent": entry["percent"],
            "statements": entry["statements"],
            "missing_lines": sorted(missing),
            "hunks": hunks,
        }

    # -- 内部 ----------------------------------------------------------
    def _data_file(self, package: PackageSpec) -> Path | None:
        if not package.coverage_data_file:
            return None
        return self.config.repo_root / package.coverage_data_file

    def _missing_message(self, package: PackageSpec) -> str:
        if not package.coverage_data_file:
            return f"{package.label} 未配置覆盖率（PackageSpec 缺少 coverage_data_file）"
        if package.coverage_config:
            config_file = self.config.repo_root / package.coverage_config
            if not config_file.is_file():
                # 配置写错时，"还没跑过覆盖率"会把人带偏，直接指出文件不存在
                return (
                    f"{package.label} 的覆盖率配置不存在：{package.coverage_config}"
                    f"（{config_file}）"
                )
        hint = package.coverage_hint or "该包的覆盖率测试命令"
        return f"{package.label} 还没有覆盖率数据。先跑 `{hint}`，再回来看这里。"

    def _cache_file(self, package: PackageSpec) -> Path:
        return self.cache_dir / f"{package.name}.json"

    def _run_coverage_json(self, data_file: Path) -> dict:
        """调 coverage json 生成一份原始报告，读完就删临时文件。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "coverage.json"
            command = [
                *split_command(self.config.coverage_json_cmd),
                "--data-file",
                str(data_file),
                "-o",
                str(output),
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=self.config.repo_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as error:
                # 命令写错（比如宿主没装 coverage / 路径不对）也要变成
                # NoCoverageData：server 只兜这个异常，抛别的会让接口断连接。
                raise NoCoverageData(
                    f"读取覆盖率失败：{type(error).__name__}: {error}"
                ) from error
            if not output.is_file():
                message = (completed.stderr or completed.stdout or "").strip()
                raise NoCoverageData(f"读取覆盖率失败：{message[-300:] or 'coverage json 没有输出'}")
            return json.loads(output.read_text(encoding="utf-8"))

    @staticmethod
    def _blocks(missing: list[int], total_lines: int, context: int = 3) -> list[dict]:
        """把缺失行合并成若干「上下文窗口」，相邻/重叠的窗口合并。

        不返回整个文件：一个 800 行的模块只有 2 行没跑到，整篇下发给页面
        既慢又没人看得完。
        """
        blocks: list[dict] = []
        for line_no in sorted(set(missing)):
            start = max(1, line_no - context)
            end = min(total_lines, line_no + context)
            if blocks and start <= blocks[-1]["end"] + 1:
                blocks[-1]["end"] = max(blocks[-1]["end"], end)
            else:
                blocks.append({"start": start, "end": end})
        return blocks

    def _normalize(self, package: PackageSpec, raw: dict, mtime_ns: int, mtime: float) -> dict:
        # 用秒级 mtime 做展示，纳秒级 mtime 做缓存版本判断：
        # JSON 里存 float 会丢精度，第二次读回来就永远对不上。
        totals = raw.get("totals", {})
        files = []
        for raw_path, item in (raw.get("files") or {}).items():
            summary = item.get("summary", {})
            files.append(
                {
                    # coverage 在 Windows 上记录的是反斜杠路径，统一成正斜杠
                    "path": raw_path.replace("\\", "/"),
                    "statements": summary.get("num_statements", 0),
                    "missing": summary.get("missing_lines", 0),
                    "excluded": summary.get("excluded_lines", 0),
                    "branches": summary.get("num_branches", 0),
                    "partial_branches": summary.get("num_partial_branches", 0),
                    "percent": round(summary.get("percent_covered", 0.0), 1),
                    "missing_lines": item.get("missing_lines", []),
                }
            )

        # 覆盖率低的排前面——这才是用户想先看的
        files.sort(key=lambda item: (item["percent"], item["path"]))

        return {
            "package": package.name,
            "label": package.label,
            "generated_at": datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="minutes"),
            "totals": {
                "statements": totals.get("num_statements", 0),
                "covered": totals.get("covered_lines", 0),
                "missing": totals.get("missing_lines", 0),
                "excluded": totals.get("excluded_lines", 0),
                "branches": totals.get("num_branches", 0),
                "partial_branches": totals.get("num_partial_branches", 0),
                "percent": round(totals.get("percent_covered", 0.0), 1),
                "branch_percent": round(totals.get("percent_branches_covered", 0.0), 1),
            },
            "files": files,
            "source_mtime_ns": mtime_ns,
        }
