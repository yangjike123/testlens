"""运行执行器（RunManager）：把页面发起的 pytest 请求变成受控的子进程。

职责边界：**只管"怎么跑"**——路径校验、按包分组、进程生命周期、输出缓冲、
超时与落盘。路由与 HTTP 状态码映射在 server.py。

为什么按包一个槽位：两套测试用不同的资源（web-service 要 PostgreSQL 测试库，
utils 只读 .env.test），没有理由互相排队；同一个包内只允许一次运行，因为
两个 pytest 会抢同一批 fixture 和覆盖率数据文件。
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .config import Config, PackageSpec, package_of, root_prefix
from .history import HistoryStore
from .junit import parse_files
from .scan import Scanner


class BadRequest(Exception):
    """请求不合法。server 层负责把它映射成 400/409。"""


def kill_tree(process: subprocess.Popen) -> None:
    """连子进程一起杀。

    只 terminate uv 不够：真正的 pytest 是它的子进程，在 Windows 上会变成
    孤儿继续占着测试库（表现是下一次运行卡在收集阶段）。
    """
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        process.terminate()


@dataclass
class Run:
    run_id: str
    package: str  # 归属的包 name
    title: str
    paths: list[str]
    coverage: bool = False  # 这次是否带 --cov（跑全量统计覆盖率时才开）
    results: dict | None = None  # junit 解析结果：用例统计 / 每脚本耗时 / 最慢 / 失败
    status: str = "running"
    lines: list[str] = field(default_factory=list)
    exit_code: int | None = None
    started: float = field(default_factory=time.monotonic)
    started_at: str = field(
        default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds")
    )
    finished: float | None = None
    batches: int = 0  # 要跑几个 pytest 进程（正常是 1，按包分组时可能更多）
    # max_lines / tail_lines 由 RunManager 注入（来自 config），Run 本身不持 config
    max_lines: int = 20_000
    tail_lines: int = 800
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def append(self, text: str) -> None:
        chunk = text.splitlines() or [""]
        with self._lock:
            self.lines.extend(chunk)
            if len(self.lines) > self.max_lines:
                overflow = len(self.lines) - self.max_lines
                del self.lines[:overflow]
                self.lines.insert(0, f"… 输出超过 {self.max_lines} 行，已丢弃最早的 {overflow} 行 …")

    def snapshot(self, tail: int) -> str:
        with self._lock:
            return "\n".join(self.lines[-tail:])

    def duration(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "package": self.package,
            "title": self.title,
            "status": self.status,
            "output": self.snapshot(self.tail_lines),
            "exit_code": self.exit_code,
            "duration": round(self.duration(), 1),
            "script_total": len(self.paths),
            "results": self.results,
            # 有 run_id 才有 /api/runs/<id>/log 这个地址，前端据此决定要不要显示「完整日志」
            "log_available": bool(self.run_id),
        }

    def to_history_entry(self, label: str) -> dict:
        """落盘用的精简记录：不含输出（输出单独存 .log），只留能列表展示的字段。"""
        return {
            "run_id": self.run_id,
            "package": self.package,
            "package_label": label,
            "title": self.title,
            "status": self.status,
            "started_at": self.started_at,
            "duration": round(self.duration(), 1),
            "exit_code": self.exit_code,
            "scripts": list(self.paths),
            "script_total": len(self.paths),
            "coverage": self.coverage,
            "results": self.results,
        }


class RunManager:
    """按包隔离的运行管理器。一个 Toolkit 一个实例。"""

    def __init__(self, config: Config, scanner: Scanner, history: HistoryStore) -> None:
        self.config = config
        self.scanner = scanner
        self.history = history
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}
        # 按 run_id 记，不按包名：同包「停止」后立刻再跑时，旧运行的收尾线程
        # 若按包名 pop，会把新运行的进程句柄一起删掉，之后就再也停不掉它。
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._tmp_cleaned = False

    def _cleanup_tmp_once(self) -> None:
        """第一次真正跑测试前清一次上次残留的 junit 文件（只删一天前的）。

        不放在 __init__：`testlens list` / `page` 也会构造 Toolkit，
        为了列个清单就去删文件不合适；真正要跑之前清一次就够。
        """
        if self._tmp_cleaned:
            return
        self._tmp_cleaned = True
        tmp = self.config.data_dir / "tmp"
        if not tmp.is_dir():
            return
        cutoff = time.time() - 86_400
        for path in tmp.glob("*.xml"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    # -- 校验 ----------------------------------------------------------
    def resolve_package(self, name) -> PackageSpec:
        if not name:
            raise BadRequest("缺少 package 参数")
        for pkg in self.config.packages:
            if pkg.name == name:
                return pkg
        known = ", ".join(pkg.name for pkg in self.config.packages)
        raise BadRequest(f"未知的包：{name}（可选：{known}）")

    def validate_paths(self, raw, package: PackageSpec) -> list[str]:
        """把前端传来的路径数组清洗成「本包测试目录内的 test_*.py 相对路径」。

        1. 逐个解析、限制在 repo_root 内——这是防路径穿越的唯一闸门；
        2. 拆分——一次运行只能属于一个包，混进来的另一套脚本会被拒绝。这样
           两个 tab 的运行不会互相串（也就不会出现"点了 utils 却跑了 app"）。
        """
        if not isinstance(raw, list) or not raw:
            raise BadRequest("paths 必须是非空数组")
        if len(raw) > 200:
            raise BadRequest("一次最多 200 个脚本")

        accepted: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise BadRequest("paths 里必须是字符串")
            candidate = (self.config.repo_root / item).resolve()
            try:
                rel = candidate.relative_to(self.config.repo_root).as_posix()
            except ValueError as exc:
                raise BadRequest(f"路径超出仓库范围：{item}") from exc

            owner = package_of(self.config.packages, rel)
            if owner is None:
                raise BadRequest(f"不在测试目录内：{rel}")
            # 用包自己的 test_glob 判断「是不是测试脚本」，和扫描用的是同一个规则；
            # 写死 test_*.py 会让自定义 test_glob 的包能列出来却跑不了。
            if candidate.suffix != ".py" or not candidate.match(owner.test_glob):
                raise BadRequest(f"只接受 {owner.test_glob}：{rel}")
            if not candidate.is_file():
                raise BadRequest(f"文件不存在：{rel}")

            tests_prefix = root_prefix(owner) + f"{owner.tests_dir}/"
            if not rel.startswith(tests_prefix):
                raise BadRequest(f"不在测试目录内：{rel}")
            if owner != package:
                raise BadRequest(f"{rel} 属于 {owner.label}，不属于 {package.label}")
            accepted.append(rel)

        return sorted(set(accepted))

    def build_commands(self, paths: list[str], *, coverage: bool = False) -> list[tuple[str, list[str]]]:
        """按包分组构造 pytest 命令。

        正常情况一个 Run 里的路径全属于同一个包，只产出一条命令；分组是防御性的
        ——两个包的 rootdir 与 pytest 配置不同（覆盖率、asyncio 模式），混进同一个
        进程会让配置全部失效。
        """
        grouped: dict[str, list[str]] = {}
        for path in paths:
            pkg = package_of(self.config.packages, path)
            if pkg is not None:
                grouped.setdefault(pkg.name, []).append(path)

        order = {pkg.name: index for index, pkg in enumerate(self.config.packages)}
        commands: list[tuple[str, list[str]]] = []
        for name in sorted(grouped, key=lambda name: order.get(name, len(order))):
            pkg = next(p for p in self.config.packages if p.name == name)
            commands.append((name, self.command_for(pkg, grouped[name], coverage=coverage)))
        return commands

    def command_for(
        self, package: PackageSpec, paths: list[str], *, coverage: bool = False
    ) -> list[str]:
        """一个包的一批 pytest 命令：pytest_cmd / -q / basetemp / 覆盖率参数只在这里拼。

        面板运行与 `[coverage] warmup` 预热都调它——两边拼法一旦分叉，
        预热生成的数据文件就可能不是页签要读的那一个。
        """
        args = [
            *shlex.split(self.config.pytest_cmd),
            *sorted(paths),
            "-q",
            # pytest 默认在系统临时目录里建会话目录，并会顺手清理旧的 garbage-*；
            # 收进我们自己的 data_dir 更干净，也不会误删用户临时文件。
            f"--basetemp={self._basetemp(package.name)}",
        ]
        if coverage:
            args.extend(package.coverage_args)
        return args

    def _basetemp(self, package_name: str) -> str:
        """每个包一个 pytest 临时目录，避免并行时互删。"""
        base = self.config.data_dir / "tmp" / f"pytest-{package_name}"
        base.parent.mkdir(parents=True, exist_ok=True)
        return str(base)

    # -- 生命周期 ------------------------------------------------------
    def start(
        self,
        package: PackageSpec,
        paths: list[str],
        title: str,
        *,
        coverage: bool = False,
    ) -> Run:
        with self._lock:
            self._cleanup_tmp_once()
            current = self._runs.get(package.name)
            if current is not None and current.status == "running":
                raise BadRequest(f"{package.label} 已经有一次运行在进行，等它结束或先点停止")
            run = Run(
                run_id=f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}",
                package=package.name,
                title=title,
                paths=paths,
                coverage=coverage,
                batches=len(self.build_commands(paths, coverage=coverage)),
                max_lines=self.config.max_output_lines,
                tail_lines=self.config.tail_lines,
            )
            # 运行前把这次要跑的脚本写回缓存：跑完它们就算"已确认"，
            # 页面上的「当前更新」随之清空，下一次改动才会重新出现。
            self.scanner.mark_baseline(paths)
            self._runs[package.name] = run
            threading.Thread(target=self._execute, args=(run,), daemon=True).start()
            return run

    def get(self, run_id: str) -> Run | None:
        # 快照：另一个线程可能正在往 _runs 里加新包的槽位
        for run in list(self._runs.values()):
            if run.run_id == run_id:
                return run
        return None

    def stop(self, run_id: str) -> bool:
        run = self.get(run_id)
        if run is None or run.status != "running":
            return False
        run.append("\n[停止] 收到停止请求，正在结束进程…")
        run.status = "stopped"  # 先落状态，执行线程就不会把它记成 passed/failed
        process = self._processes.get(run.run_id)
        if process is not None:
            kill_tree(process)
        return True

    def shutdown(self) -> None:
        """Ctrl+C 退出时把正在跑的 pytest 一起带走。

        不杀的话，uv 和 pytest 会变成孤儿继续占着测试库，下次启动跑什么都卡。
        """
        for run in list(self._runs.values()):
            if run.status != "running":
                continue
            run.status = "stopped"
            run.append("\n[退出] 面板服务关闭，正在结束进程…")
            process = self._processes.get(run.run_id)
            if process is not None:
                kill_tree(process)

    # -- 执行 ----------------------------------------------------------
    def _kill_on_timeout(self, run: Run, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            run.append(f"\n[超时] 超过 {self.config.run_timeout:g} 秒，已终止")
            run.status = "failed"
            kill_tree(process)

    def _execute(self, run: Run) -> None:
        env = os.environ.copy()
        # pytest 是 Python 进程，这三个变量让它不走块缓冲，页面才能实时看到输出。
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        failed = False
        junit_paths: list[Path] = []
        try:
            # 每批一个 junit 文件：正常只有一批，多批是防御性分组
            tmp_dir = self.config.data_dir / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)

            for index, (package_name, command) in enumerate(
                self.build_commands(run.paths, coverage=run.coverage), start=1
            ):
                if run.status != "running":
                    break
                # 按 .py 参数数，不按 len(command)-4：带 --cov 时会多出参数
                file_count = sum(1 for arg in command if arg.endswith(".py"))
                junit_file = tmp_dir / f"{run.run_id}-{index}.xml"
                junit_paths.append(junit_file)
                command = [*command, f"--junitxml={junit_file}"]
                # 打印命令时藏掉临时路径：用户要看的是"跑了什么"，不是内部实现
                display = [arg for arg in command if not arg.startswith("--junitxml=")]
                run.append(f"\n{'=' * 68}")
                run.append(f"[{index}/{run.batches}] {package_name} · {file_count} 个脚本")
                run.append(f"$ {' '.join(display)}\n")

                # 面板自己跑覆盖率时，强制写到面板读取的同一个文件：COVERAGE_FILE
                # 优先于 .coveragerc 的 data_file，否则 cwd 与配置的基准不一致，
                # 跑完回到覆盖率页签还是「没有数据」。
                batch_pkg = next(
                    (pkg for pkg in self.config.packages if pkg.name == package_name), None
                )
                batch_env = env
                if run.coverage and batch_pkg and batch_pkg.coverage_data_file:
                    data_file = (
                        self.config.repo_root / batch_pkg.coverage_data_file
                    ).resolve()
                    batch_env = {**env, "COVERAGE_FILE": str(data_file)}

                process = subprocess.Popen(
                    command,
                    cwd=self.config.repo_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=batch_env,
                )
                self._processes[run.run_id] = process
                # 用定时器而不是在读取循环里比对时间：进程不输出时循环会一直阻塞，
                # 那种情况下超时检查永远执行不到。
                watchdog = threading.Timer(
                    self.config.run_timeout, self._kill_on_timeout, (run, process)
                )
                watchdog.daemon = True
                watchdog.start()

                try:
                    if process.stdout is not None:
                        for line in process.stdout:
                            run.append(line)
                    code = process.wait()
                finally:
                    watchdog.cancel()

                run.exit_code = code
                if code != 0:
                    failed = True

                if run.status == "running":
                    run.append(f"\n[{package_name}] 退出码 {code}")

        except Exception as error:  # noqa: BLE001 - 服务线程里必须兜住，否则状态永远停在 running
            run.append(f"\n[服务错误] {type(error).__name__}: {error}")
            run.status = "error"
            failed = True
        finally:
            self._processes.pop(run.run_id, None)
            run.finished = time.monotonic()
            if run.status == "running":
                run.status = "failed" if failed else "passed"

            # 结构化结果：解析失败（超时被杀、XML 半截）就当没有，不影响运行的成败判定
            package_spec = next(
                (pkg for pkg in self.config.packages if pkg.name == run.package), None
            )
            if package_spec is not None and junit_paths:
                run.results = parse_files(junit_paths, package_spec, self.config.repo_root)
            for junit_file in junit_paths:
                junit_file.unlink(missing_ok=True)

            summary = {
                "passed": "全部通过",
                "failed": "存在失败",
                "error": "服务出错",
                "stopped": "已停止",
            }.get(run.status, run.status)
            run.append(f"\n— {summary} · 耗时 {run.duration():.1f}s · 退出码 {run.exit_code}")

            # 落盘：面板重启/页面刷新之后，这条运行仍然能在「历史」里翻到。
            # 写盘失败不能影响这次运行的结果，所以单独兜住。
            try:
                label = next(
                    (
                        pkg.label
                        for pkg in self.config.packages
                        if pkg.name == run.package
                    ),
                    run.package,
                )
                self.history.record(
                    run.to_history_entry(label),
                    run.snapshot(self.config.max_output_lines),
                )
            except Exception as error:  # noqa: BLE001
                run.append(f"\n[历史记录失败] {type(error).__name__}: {error}")
