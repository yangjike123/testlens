"""测试脚本缓存与变更检测（Scanner 服务）。

**为什么不用 git**：检测"脚本有没有被改过"只需要两样东西——上次留下的内容、
这次读到的内容。git 能回答，但它要求仓库干净、要有提交历史、还要起子进程；
一个纯文件对比更直接，也不受 checkout / rebase 影响。

机制：

1. 每个测试脚本的**完整源码**存在 `<data_dir>/script-cache/cache.json`；
2. 扫描时重新读文件，与缓存里的文本**逐字比较**（不是看 mtime，也不是比 hash）；
3. 不一致 → `changed`；缓存里没有 → `new`；一致 → `unchanged`；
4. 什么时候把新内容写回缓存（"确认基线"）由调用方决定：
   - 面板每次启动一次运行前：`mark_baseline(paths)`，只确认这次要跑的脚本；
   - 用户点「全部标记为已确认」：`mark_baseline(None)`。

读取用 `newline=""`：默认的 universal newlines 会把 CRLF 折成 LF，
那样"逐字比较"就漏掉只在换行符上变化的文件。
"""

from __future__ import annotations

import difflib
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from .config import Config, PackageSpec, count_tests, package_of, root_prefix, test_files


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _diff_stats(old: str, new: str) -> dict:
    """增删行数。只看行级变化，不做字符级 diff。"""
    added = removed = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), n=0, lineterm=""):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return {"added": added, "removed": removed}


class Scanner:
    """脚本扫描与基线维护。一个 Toolkit 一个实例；线程安全。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.cache_dir = config.data_dir / "script-cache"
        self.cache_file = self.cache_dir / "cache.json"
        self._lock = threading.Lock()

    # -- 存储 ----------------------------------------------------------
    def _read_source(self, path: Path) -> str | None:
        try:
            with open(path, "r", encoding="utf-8", newline="") as handle:
                return handle.read()
        except (OSError, UnicodeDecodeError):
            return None

    def _load_raw(self) -> dict:
        if not self.cache_file.is_file():
            return {"version": 1, "scripts": {}}
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "scripts": {}}
        if not isinstance(data, dict) or not isinstance(data.get("scripts"), dict):
            return {"version": 1, "scripts": {}}
        return data

    def _save_raw(self, data: dict) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temp = self.cache_file.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        # 先写临时文件再替换：中途崩了也不会留下半截 JSON
        os.replace(temp, self.cache_file)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.config.repo_root).as_posix()

    def _group_of(self, rel: str, pkg: PackageSpec) -> str:
        """目录分组键：后端算好给前端，前端不再猜 root/tests_dir 前缀。"""
        prefix = root_prefix(pkg) + f"{pkg.tests_dir}/"
        rest = rel[len(prefix):] if rel.startswith(prefix) else Path(rel).name
        parent = Path(rest).parent.as_posix()
        return parent if parent not in ("", ".") else "（根目录）"

    # -- 对外能力 ------------------------------------------------------
    def scan(self, packages: tuple[PackageSpec, ...] | None = None) -> dict:
        """扫描测试脚本，与缓存逐字对比。只读，不改缓存。"""
        packages = packages or self.config.packages
        cached = self._load_raw()["scripts"]
        scripts: list[dict] = []

        for pkg in packages:
            for path in test_files(pkg, self.config.repo_root):
                rel = self._relative(path)
                content = self._read_source(path)
                entry = cached.get(rel)
                tests = count_tests(path, pkg.func_prefix)

                if content is None:
                    status, diff = "unreadable", None
                elif entry is None:
                    status, diff = "new", None
                elif entry.get("content") != content:
                    status, diff = "changed", _diff_stats(str(entry.get("content", "")), content)
                else:
                    status, diff = "unchanged", None

                scripts.append(
                    {
                        "path": rel,
                        "name": path.name,
                        "dir": Path(rel).parent.as_posix(),
                        "group": self._group_of(rel, pkg),
                        "package": pkg.name,
                        "tests": tests,
                        "status": status,
                        "changed": status in ("new", "changed"),
                        "diff": diff,
                    }
                )

        # 缓存里有、磁盘上已经没有的脚本：也算一种"变更"，界面单独提示
        present = {item["path"] for item in scripts}
        for rel, entry in cached.items():
            if rel in present:
                continue
            pkg = package_of(self.config.packages, rel)
            # 只报本次扫描范围内的包：`--package utils` 时不能把别的包当「已删除」
            if pkg is None or pkg not in packages:
                continue
            scripts.append(
                {
                    "path": rel,
                    "name": Path(rel).name,
                    "dir": Path(rel).parent.as_posix(),
                    "group": self._group_of(rel, pkg),
                    "package": pkg.name,
                    "tests": 0,
                    "status": "missing",
                    "changed": True,
                    "diff": None,
                }
            )

        scripts.sort(key=lambda item: (item["package"], item["path"]))
        return {
            "scanned_at": _now(),
            "scripts": scripts,
            "total": sum(1 for item in scripts if item["status"] != "missing"),
            "changed": sum(1 for item in scripts if item["changed"]),
            "new": sum(1 for item in scripts if item["status"] == "new"),
            "cached": sum(1 for rel in cached if package_of(self.config.packages, rel) in packages),
        }

    def mark_baseline(self, paths: list[str] | None = None) -> int:
        """把当前内容写回缓存。paths 为空表示全部脚本。返回写入条数。"""
        wanted = set(paths) if paths else None
        with self._lock:
            data = self._load_raw()
            entries: dict[str, dict] = data["scripts"]
            written = 0

            # 磁盘上现存的全部脚本：既用于「本次要确认哪些」，也用于清掉已删除的
            alive: dict[str, Path] = {
                self._relative(path): path
                for pkg in self.config.packages
                for path in test_files(pkg, self.config.repo_root)
            }

            # 只读本次要确认的文件：跑 3 个脚本时没必要把 29 个都读一遍
            targets = alive if wanted is None else {
                rel: path for rel, path in alive.items() if rel in wanted
            }
            for rel, path in targets.items():
                content = self._read_source(path)
                if content is None:
                    continue
                entries[rel] = {"content": content, "confirmed_at": _now()}
                written += 1

            # 已经不在磁盘上的脚本从缓存里清掉，否则每次扫描都会报"已删除"
            for rel in list(entries):
                if rel not in alive:
                    del entries[rel]

            self._save_raw(data)
            return written
