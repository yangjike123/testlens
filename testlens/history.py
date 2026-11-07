"""运行历史（HistoryStore 服务）：把每次 pytest 运行的结果落盘。

为什么必须落盘：面板的抽屉是进程内状态，服务一重启、页面一刷新就全没了。
用户要的是「每次运行都留一条，能翻回去看，并且看得出这次和上次跑了哪些
不一样的脚本」——那只能写文件。

存储位置 `<data_dir>/run-history/`：

    history.json        运行记录索引，最新在前；条数上限来自 config.history_max_entries，
                        超出后删掉最旧的记录和它的 <run_id>.log
    <run_id>.log        该次运行的完整输出

为什么不塞进数据库：这是开发工具，不是业务。一个 JSON + 几个文本文件，
用编辑器都能直接看，不值得为它起表、跑迁移。
"""

from __future__ import annotations

import json
import os
import re
import threading

from .config import Config

RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")


class HistoryStore:
    """运行历史读写。run_id 由调用方生成，这里严格校验防止路径穿越。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.history_dir = config.data_dir / "run-history"
        self.index_file = self.history_dir / "history.json"
        self.max_entries = config.history_max_entries
        self._lock = threading.Lock()

    def _ensure_dir(self) -> None:
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def _write_index(self, entries: list[dict]) -> None:
        """先写临时文件再替换：半截 JSON 会让整个历史读不出来。"""
        self._ensure_dir()
        temp = self.index_file.with_suffix(".json.tmp")
        temp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.index_file)

    def entries(self) -> list[dict]:
        """读取索引。文件不存在 / 损坏都返回空列表，不让面板因为历史挂掉。"""
        if not self.index_file.is_file():
            return []
        try:
            data = json.loads(self.index_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def record(self, entry: dict, output: str) -> dict:
        """追加一次运行。同一个 run_id 重复写入时覆盖，不会出现两条。"""
        run_id = str(entry.get("run_id", ""))
        if not RUN_ID_RE.match(run_id):
            raise ValueError(f"非法的 run_id：{run_id!r}")

        with self._lock:
            self._ensure_dir()
            (self.history_dir / f"{run_id}.log").write_text(output, encoding="utf-8")

            current = self.entries()
            current = [item for item in current if item.get("run_id") != run_id]
            current.insert(0, entry)

            removed = current[self.max_entries:]
            current = current[: self.max_entries]
            self._write_index(current)

            for old in removed:
                old_id = str(old.get("run_id", ""))
                if RUN_ID_RE.match(old_id):
                    (self.history_dir / f"{old_id}.log").unlink(missing_ok=True)

        return entry

    def read_log(self, run_id: str) -> str | None:
        """读某次运行的完整输出。run_id 非法或文件不存在返回 None。"""
        if not RUN_ID_RE.match(run_id):
            return None
        path = self.history_dir / f"{run_id}.log"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def find(self, run_id: str) -> dict | None:
        if not RUN_ID_RE.match(run_id):
            return None
        return next((item for item in self.entries() if item.get("run_id") == run_id), None)
