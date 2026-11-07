"""RunManager 的行为测试：真实起子进程跑 pytest，覆盖成功 / 失败 / 超时 / 出错。

这里刻意不用 mock 顶替 subprocess：面板的核心价值就是"真的把 pytest 跑起来"，
一旦只测内部函数，命令拼错、环境变量漏传、junit 没解析这些问题都测不出来。
子进程用 `sys.executable -m pytest`，不依赖 uv，也不吃网络。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from testlens.config import PackageSpec, build_config
from testlens.runner import BadRequest, Run, RunManager, kill_tree


def pytest_cmd() -> str:
    """当前解释器跑 pytest：不依赖 uv、不依赖 PATH 里有没有 testlens。"""
    return f'"{sys.executable}" -m pytest'


def write_tests(root: Path, *, failing: bool = False, slow: float | None = None) -> None:
    tests = root / "test"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n\n\ndef test_ok_two():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    if failing:
        (tests / "test_bad.py").write_text(
            "def test_bad():\n    assert 1 == 2, '故意失败'\n", encoding="utf-8"
        )
    if slow is not None:
        (tests / "test_slow.py").write_text(
            f"import time\n\n\ndef test_slow():\n    time.sleep({slow})\n", encoding="utf-8"
        )


def make_config(root: Path, **kw):
    (root / "test").mkdir(parents=True, exist_ok=True)
    base = {
        "packages": (PackageSpec(name="demo", label="Demo", root="."),),
        "repo_root": root,
        "data_dir": root / "testlens.data",
        "dist_dir": root / "dist",
        "pytest_cmd": pytest_cmd(),
        "run_timeout": 60.0,
        "coverage_warmup": False,
    }
    base.update(kw)
    return build_config(**base)


def make_manager(config) -> RunManager:
    from testlens.history import HistoryStore
    from testlens.scan import Scanner

    return RunManager(config, Scanner(config), HistoryStore(config))


def wait_done(
    manager: RunManager, run: Run, timeout: float = 60.0, *, expect_history: bool = True
) -> Run:
    """等到运行真正收尾。

    状态是 `finally` 里先落的，结果解析与历史落盘在它之后；只等 status 会拿到
    还没填好的 results（曾把这里跑成偶发失败）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        finished = run.status != "running"
        if finished and (not expect_history or manager.history.find(run.run_id) is not None):
            return run
        time.sleep(0.05)
    raise AssertionError(f"运行没有在 {timeout}s 内结束：{run.status}")


# ----------------------------------------------------------------------
# kill_tree：两条平台分支都要能杀干净
# ----------------------------------------------------------------------
class FakeProcess:
    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.terminated = False
        self.pid = 4321

    def poll(self):
        return None if self.running else 0

    def terminate(self) -> None:
        self.terminated = True


def test_kill_tree_skips_finished_process(monkeypatch) -> None:
    process = FakeProcess(running=False)
    monkeypatch.setattr(
        "testlens.runner.os", SimpleNamespace(name="posix"), raising=False
    )

    kill_tree(process)  # type: ignore[arg-type]

    assert process.terminated is False


def test_kill_tree_terminates_on_posix(monkeypatch) -> None:
    process = FakeProcess()
    monkeypatch.setattr("testlens.runner.os", SimpleNamespace(name="posix"), raising=False)

    kill_tree(process)  # type: ignore[arg-type]

    assert process.terminated is True


def test_kill_tree_uses_taskkill_on_windows(monkeypatch) -> None:
    process = FakeProcess()
    called: list[list[str]] = []
    monkeypatch.setattr("testlens.runner.os", SimpleNamespace(name="nt"), raising=False)
    monkeypatch.setattr(
        "testlens.runner.subprocess.run",
        lambda command, **kw: called.append(command),
    )

    kill_tree(process)  # type: ignore[arg-type]

    assert called and called[0][:3] == ["taskkill", "/PID", "4321"]
    assert "/T" in called[0] and "/F" in called[0]


# ----------------------------------------------------------------------
# Run 数据结构
# ----------------------------------------------------------------------
def test_run_output_overflow_keeps_marker_and_tail() -> None:
    run = Run(run_id="20260101-000000-abcd", package="demo", title="t", paths=[], max_lines=5)

    run.append("\n".join(f"line{i}" for i in range(8)))

    assert run.lines[0].startswith("… 输出超过 5 行")
    assert run.lines[-1] == "line7"
    assert len(run.lines) == 6  # 5 行正文 + 1 行提示
    assert run.snapshot(2).endswith("line7")


def test_run_to_dict_and_history_entry() -> None:
    run = Run(
        run_id="20260101-000000-abcd",
        package="demo",
        title="手动选择",
        paths=["test/test_ok.py"],
        max_lines=100,
        tail_lines=10,
    )
    run.append("hello")
    run.status = "passed"
    run.exit_code = 0

    data = run.to_dict()
    assert data["run_id"] == run.run_id
    assert data["script_total"] == 1
    assert data["log_available"] is True
    assert data["output"] == "hello"

    entry = run.to_history_entry("Demo")
    assert entry["package_label"] == "Demo"
    assert entry["scripts"] == ["test/test_ok.py"]
    assert "output" not in entry

    run.finished = run.started + 1.25
    assert run.duration() == pytest.approx(1.25, abs=0.01)


# ----------------------------------------------------------------------
# 临时目录清理
# ----------------------------------------------------------------------
def test_cleanup_tmp_once_removes_only_old_files(tmp_path: Path) -> None:
    write_tests(tmp_path)
    config = make_config(tmp_path)
    manager = make_manager(config)
    tmp = config.data_dir / "tmp"
    tmp.mkdir(parents=True)
    old = tmp / "old.xml"
    fresh = tmp / "fresh.xml"
    old.write_text("<x/>", encoding="utf-8")
    fresh.write_text("<x/>", encoding="utf-8")
    two_days_ago = time.time() - 2 * 86_400
    os.utime(old, (two_days_ago, two_days_ago))

    manager._cleanup_tmp_once()

    assert not old.exists()
    assert fresh.exists()
    # 第二次调用直接返回（幂等），不再扫目录
    manager._cleanup_tmp_once()


def test_cleanup_tmp_once_tolerates_unremovable_entry(tmp_path: Path) -> None:
    """删不掉的条目（比如同名目录）不能让启动失败。"""
    write_tests(tmp_path)
    config = make_config(tmp_path)
    manager = make_manager(config)
    tmp = config.data_dir / "tmp"
    blocker = tmp / "blocked.xml"
    blocker.mkdir(parents=True)
    two_days_ago = time.time() - 2 * 86_400
    os.utime(blocker, (two_days_ago, two_days_ago))

    manager._cleanup_tmp_once()

    assert blocker.is_dir()


def test_cleanup_tmp_once_without_tmp_dir(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manager = make_manager(config)

    manager._cleanup_tmp_once()  # 目录不存在时静默返回

    assert manager._tmp_cleaned is True


# ----------------------------------------------------------------------
# 请求校验
# ----------------------------------------------------------------------
def test_resolve_package_errors(tmp_path: Path) -> None:
    manager = make_manager(make_config(tmp_path))

    with pytest.raises(BadRequest, match="缺少 package"):
        manager.resolve_package(None)
    with pytest.raises(BadRequest, match="未知的包"):
        manager.resolve_package("nope")
    assert manager.resolve_package("demo").name == "demo"


def test_validate_paths_rejects_bad_shapes(tmp_path: Path) -> None:
    manager = make_manager(make_config(tmp_path))
    package = manager.config.packages[0]

    for raw, message in [
        ("test/test_ok.py", "非空数组"),
        ([], "非空数组"),
        ([f"test/t{i}.py" for i in range(201)], "最多 200"),
        ([123], "必须是字符串"),
    ]:
        with pytest.raises(BadRequest, match=message):
            manager.validate_paths(raw, package)


def test_validate_paths_rejects_outside_and_wrong_kind(tmp_path: Path) -> None:
    manager = make_manager(make_config(tmp_path))
    package = manager.config.packages[0]
    write_tests(tmp_path)
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "test" / "helper.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(BadRequest, match="超出仓库范围"):
        manager.validate_paths(["../outside/test_x.py"], package)
    with pytest.raises(BadRequest, match="只接受 test_"):
        manager.validate_paths(["notes.txt"], package)
    with pytest.raises(BadRequest, match="只接受 test_"):
        manager.validate_paths(["test/helper.py"], package)
    with pytest.raises(BadRequest, match="文件不存在"):
        manager.validate_paths(["test/test_none.py"], package)


def test_validate_paths_enforces_package_ownership(tmp_path: Path) -> None:
    specs = (
        PackageSpec(name="alpha", label="Alpha", root="apps/alpha"),
        PackageSpec(name="beta", label="Beta", root="apps/beta"),
    )
    (tmp_path / "apps/alpha/test").mkdir(parents=True)
    (tmp_path / "apps/beta/test").mkdir(parents=True)
    (tmp_path / "apps/alpha/test/test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (tmp_path / "apps/beta/test/test_b.py").write_text("def test_b(): pass\n", encoding="utf-8")
    (tmp_path / "stray/test").mkdir(parents=True)
    (tmp_path / "stray/test/test_s.py").write_text("def test_s(): pass\n", encoding="utf-8")
    (tmp_path / "apps/alpha/src").mkdir(parents=True)
    (tmp_path / "apps/alpha/src/test_inner.py").write_text("def test_i(): pass\n", encoding="utf-8")
    config = build_config(
        packages=specs,
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        pytest_cmd=pytest_cmd(),
    )
    manager = make_manager(config)

    with pytest.raises(BadRequest, match="不属于 Alpha"):
        manager.validate_paths(["apps/beta/test/test_b.py"], config.packages[0])
    with pytest.raises(BadRequest, match="不在测试目录内"):
        manager.validate_paths(["stray/test/test_s.py"], config.packages[0])
    with pytest.raises(BadRequest, match="不在测试目录内"):
        manager.validate_paths(["apps/alpha/src/test_inner.py"], config.packages[0])

    # 同一个文件重复传只会跑一次
    accepted = manager.validate_paths(
        ["apps/alpha/test/test_a.py", "apps/alpha/test/test_a.py"], config.packages[0]
    )
    assert accepted == ["apps/alpha/test/test_a.py"]


def test_build_commands_groups_by_package_in_defined_order(tmp_path: Path) -> None:
    specs = (
        PackageSpec(name="alpha", label="Alpha", root="apps/alpha"),
        PackageSpec(name="beta", label="Beta", root="apps/beta"),
    )
    for name in ("alpha", "beta"):
        (tmp_path / f"apps/{name}/test").mkdir(parents=True)
    config = build_config(
        packages=specs,
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        pytest_cmd=pytest_cmd(),
    )
    manager = make_manager(config)

    commands = manager.build_commands(
        ["apps/beta/test/test_b.py", "apps/alpha/test/test_a.py", "loose.py"]
    )

    assert [name for name, _ in commands] == ["alpha", "beta"]
    assert "apps/alpha/test/test_a.py" in commands[0][1]
    assert "loose.py" not in " ".join(commands[0][1])


def test_command_for_appends_coverage_args(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        packages=(
            PackageSpec(
                name="demo",
                label="Demo",
                root=".",
                coverage_args=["--cov=demo", "--cov-report="],
            ),
        ),
    )
    manager = make_manager(config)

    plain = manager.command_for(config.packages[0], ["test/test_ok.py"])
    covered = manager.command_for(config.packages[0], ["test/test_ok.py"], coverage=True)

    assert "--cov=demo" not in plain
    assert covered[-2:] == ["--cov=demo", "--cov-report="]
    assert any(arg.startswith("--basetemp=") for arg in covered)


# ----------------------------------------------------------------------
# 运行生命周期
# ----------------------------------------------------------------------
def test_start_rejects_second_run_of_same_package(tmp_path: Path) -> None:
    write_tests(tmp_path, slow=5)
    manager = make_manager(make_config(tmp_path))
    package = manager.config.packages[0]
    run = manager.start(package, ["test/test_slow.py"], "第一次")

    try:
        with pytest.raises(BadRequest, match="已经有一次运行"):
            manager.start(package, ["test/test_ok.py"], "第二次")
    finally:
        manager.stop(run.run_id)
        wait_done(manager, run)


def test_stop_and_get_edge_cases(tmp_path: Path) -> None:
    manager = make_manager(make_config(tmp_path))

    assert manager.get("20260101-000000-abcd") is None
    assert manager.stop("20260101-000000-abcd") is False


def test_run_passes_and_parses_junit(tmp_path: Path) -> None:
    write_tests(tmp_path)
    manager = make_manager(make_config(tmp_path))
    package = manager.config.packages[0]

    run = manager.start(package, ["test/test_ok.py"], "全绿")
    wait_done(manager, run)

    assert run.status == "passed"
    assert run.exit_code == 0
    assert run.results is not None
    assert run.results["totals"]["tests"] == 2
    assert run.results["totals"]["passed"] == 2
    assert run.results["files"][0]["path"] == "test/test_ok.py"
    # 历史要落盘，页面刷新后还能翻到
    assert manager.history.find(run.run_id) is not None
    assert manager.history.read_log(run.run_id)
    assert not list((manager.config.data_dir / "tmp").glob("*.xml"))


def test_run_failure_marks_failed(tmp_path: Path) -> None:
    write_tests(tmp_path, failing=True)
    manager = make_manager(make_config(tmp_path))
    package = manager.config.packages[0]

    run = manager.start(package, ["test/test_bad.py"], "故意失败")
    wait_done(manager, run)

    assert run.status == "failed"
    assert run.exit_code == 1
    assert run.results is not None
    assert run.results["totals"]["failed"] == 1
    assert "故意失败" in run.results["failures"][0]["message"]


def test_run_timeout_kills_process(tmp_path: Path) -> None:
    write_tests(tmp_path, slow=30)
    manager = make_manager(make_config(tmp_path, run_timeout=0.8))
    package = manager.config.packages[0]

    run = manager.start(package, ["test/test_slow.py"], "超时")
    wait_done(manager, run)

    assert run.status == "failed"
    assert "[超时]" in run.snapshot(200)
    assert "超过 0.8 秒" in run.snapshot(200)


def test_run_missing_executable_reports_error(tmp_path: Path) -> None:
    write_tests(tmp_path)
    manager = make_manager(
        make_config(tmp_path, pytest_cmd="definitely-not-a-real-command-xyz")
    )
    package = manager.config.packages[0]

    run = manager.start(package, ["test/test_ok.py"], "命令不存在")
    wait_done(manager, run)

    assert run.status == "error"
    assert "[服务错误]" in run.snapshot(200)


def test_run_stops_before_first_batch(tmp_path: Path) -> None:
    """运行开始前就被停掉：循环第一轮直接 break，不再起进程。"""
    write_tests(tmp_path)
    manager = make_manager(make_config(tmp_path))
    run = Run(
        run_id="20260101-000000-abcd",
        package="demo",
        title="提前停止",
        paths=["test/test_ok.py"],
        batches=1,
    )
    run.status = "stopped"

    manager._execute(run)

    assert run.status == "stopped"
    assert run.exit_code is None


def test_run_with_coverage_writes_data_file(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "demo" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    write_tests(tmp_path)
    config = make_config(
        tmp_path,
        packages=(
            PackageSpec(
                name="demo",
                label="Demo",
                root=".",
                coverage_data_file=".coverage.demo",
                coverage_args=["--cov=demo", "--cov-report="],
            ),
        ),
        coverage_json_cmd=f'"{sys.executable}" -m coverage json',
    )
    manager = make_manager(config)

    run = manager.start(config.packages[0], ["test/test_ok.py"], "带覆盖率", coverage=True)
    wait_done(manager, run)

    assert run.status == "passed"
    assert (tmp_path / ".coverage.demo").is_file()


def test_run_unknown_package_skips_results_and_label(tmp_path: Path) -> None:
    """包被删掉后旧 run 才收尾：结果不解析、历史标签退回包名，不能抛。"""
    write_tests(tmp_path)
    manager = make_manager(make_config(tmp_path))
    run = Run(
        run_id="20260101-000000-abcd",
        package="ghost",
        title="幽灵包",
        paths=["test/test_ok.py"],
        batches=1,
    )

    manager._execute(run)

    assert run.status == "passed"
    assert run.results is None
    entry = manager.history.find(run.run_id)
    assert entry is not None and entry["package_label"] == "ghost"


def test_run_handles_batch_without_package_spec(tmp_path: Path, monkeypatch) -> None:
    """build_commands 产出的包名找不到规格时，仍要能跑完（防御性分支）。"""
    write_tests(tmp_path)
    manager = make_manager(make_config(tmp_path))
    run = Run(
        run_id="20260101-000000-abcd",
        package="demo",
        title="未知批次",
        paths=["test/test_ok.py"],
        batches=1,
        coverage=True,
    )
    monkeypatch.setattr(
        manager,
        "build_commands",
        lambda paths, coverage=False: [
            ("ghost", [sys.executable, "-c", "print('hi')"])
        ],
    )

    manager._execute(run)

    assert run.status == "passed"
    assert "hi" in run.snapshot(20)


def test_run_survives_history_write_failure(tmp_path: Path, monkeypatch) -> None:
    write_tests(tmp_path)
    manager = make_manager(make_config(tmp_path))
    package = manager.config.packages[0]

    def boom(entry, output):
        raise OSError("磁盘满了")

    monkeypatch.setattr(manager.history, "record", boom)
    run = manager.start(package, ["test/test_ok.py"], "历史写失败")
    wait_done(manager, run, expect_history=False)
    deadline = time.time() + 5
    while time.time() < deadline and "[历史记录失败]" not in run.snapshot(200):
        time.sleep(0.05)

    assert run.status == "passed"
    assert "[历史记录失败]" in run.snapshot(200)


def test_run_handles_process_without_stdout(tmp_path: Path, monkeypatch) -> None:
    """stdout 为 None 的极端情况（被替换的 Popen）也要正常收尾。"""

    class SilentProcess:
        def __init__(self, *args, **kwargs) -> None:
            self.stdout = None
            self.returncode = 0
            self.pid = 1234

        def wait(self) -> int:
            return 0

        def poll(self) -> int:
            return 0

    write_tests(tmp_path)
    manager = make_manager(make_config(tmp_path))
    run = Run(
        run_id="20260101-000000-abcd",
        package="demo",
        title="没有输出",
        paths=["test/test_ok.py"],
        batches=1,
    )
    monkeypatch.setattr("testlens.runner.subprocess.Popen", SilentProcess)

    manager._execute(run)

    assert run.status == "passed"
    assert run.exit_code == 0


def test_kill_on_timeout_ignores_finished_process(tmp_path: Path) -> None:
    manager = make_manager(make_config(tmp_path))
    run = Run(run_id="20260101-000000-abcd", package="demo", title="t", paths=[])

    manager._kill_on_timeout(run, FakeProcess(running=False))  # type: ignore[arg-type]

    assert run.status == "running"


def test_shutdown_stops_running_runs_and_kills_processes(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(make_config(tmp_path))
    killed: list[object] = []
    monkeypatch.setattr("testlens.runner.kill_tree", killed.append)

    with_process = Run(
        run_id="20260101-000000-abcd", package="demo", title="运行中", paths=[]
    )
    without_process = Run(
        run_id="20260101-000000-abce", package="demo", title="排队中", paths=[]
    )
    finished = Run(run_id="20260101-000000-abcf", package="demo", title="已结束", paths=[])
    finished.status = "passed"
    manager._runs = {"a": with_process, "b": without_process, "c": finished}
    process = FakeProcess()
    manager._processes = {with_process.run_id: process}  # type: ignore[dict-item]

    manager.shutdown()

    assert with_process.status == "stopped"
    assert without_process.status == "stopped"
    assert finished.status == "passed"  # 已经结束的不再改写状态
    assert "[退出]" in with_process.snapshot(50)
    assert killed == [process]
