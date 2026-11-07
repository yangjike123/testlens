"""多服务（多包）项目：一个仓库里两套服务端测试，各自有多个脚本。

和 test_server.py 的分工：那边验证单包的路由与状态码；这里验证**多包**的真实行为：

- 清单按包分组，嵌套目录的脚本归到正确的 group；
- 两个包可以同时跑（按包分槽），同包第二次运行被拒；
- 一次运行可以带多个脚本，junit 结果只统计本包的；
- 覆盖率数据按包隔离，A 的报告里不会出现 B 的文件；
- CLI 的 --package 过滤只影响选中的那套。

项目结构（tmp_path 下）：

    services/api/       app/ + test/test_health.py + test/test_slow.py + test/unit/test_users.py
    services/worker/    worker/ + test/test_jobs.py + test/test_retry.py

每个服务的 conftest.py 把服务根放进 sys.path，模拟真实 monorepo 的引用方式。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from testlens import PackageSpec, Toolkit, build_config
from testlens.cli import cmd_changed, cmd_list
from testlens.config import select_packages
from testlens.server import ToolkitServer, print_banner

PYTEST_CMD = f'"{sys.executable}" -m pytest'
COVERAGE_JSON_CMD = f'"{sys.executable}" -m coverage json'


# ----------------------------------------------------------------------
# 两套服务的临时仓库
# ----------------------------------------------------------------------
def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_project(root: Path) -> None:
    write_file(
        root / "services/api/conftest.py",
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent))\n",
    )
    write_file(root / "services/api/app/__init__.py", "")
    write_file(
        root / "services/api/app/health.py",
        "def status():\n"
        '    return "ok"\n'
        "\n"
        "\n"
        "def check(value):\n"
        "    if value:\n"
        "        return True\n"
        "    return False\n",
    )
    write_file(
        root / "services/api/test/test_health.py",
        "from app.health import status\n\n\ndef test_status():\n"
        '    assert status() == "ok"\n',
    )
    write_file(
        root / "services/api/test/test_slow.py",
        "import time\n\n\ndef test_slow():\n    time.sleep(2)\n",
    )
    write_file(
        root / "services/api/test/unit/test_users.py",
        "from app.health import check\n\n\ndef test_check():\n"
        "    assert check(1) is True\n",
    )

    write_file(
        root / "services/worker/conftest.py",
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent))\n",
    )
    write_file(root / "services/worker/worker/__init__.py", "")
    write_file(
        root / "services/worker/worker/job.py",
        "def run():\n    return 1\n",
    )
    write_file(
        root / "services/worker/test/test_jobs.py",
        "from worker.job import run\n\n\ndef test_run():\n    assert run() == 1\n",
    )
    write_file(
        root / "services/worker/test/test_retry.py",
        "def test_retry():\n    assert True\n",
    )


def make_config(root: Path):
    return build_config(
        packages=(
            PackageSpec(
                name="api",
                label="API",
                root="services/api",
                source_pkg="app",
                coverage_data_file=".coverage.api",
                coverage_args=["--cov=services/api/app", "--cov-report="],
            ),
            PackageSpec(
                name="worker",
                label="Worker",
                root="services/worker",
                source_pkg="worker",
                coverage_data_file=".coverage.worker",
                coverage_args=["--cov=services/worker/worker", "--cov-report="],
            ),
        ),
        repo_root=root,
        data_dir=root / "testlens.data",
        dist_dir=root / "dist",
        pytest_cmd=PYTEST_CMD,
        coverage_json_cmd=COVERAGE_JSON_CMD,
        coverage_warmup=False,
        run_timeout=60.0,
    )


# ----------------------------------------------------------------------
# HTTP 客户端（与 test_server.py 同形，保持文件自包含）
# ----------------------------------------------------------------------
@contextmanager
def running_server(toolkit: Toolkit):
    httpd = ToolkitServer(toolkit)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def http(port: int, path: str, *, method: str = "GET", payload=None, timeout: float = 30.0):
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method
    )
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def payload_of(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def start_run(port: int, package: str, paths: list[str], **extra) -> str:
    status, body = http(
        port,
        "/api/run",
        method="POST",
        payload={"package": package, "paths": paths, **extra},
    )
    assert status == 200, body
    return payload_of(body)["run_id"]


def wait_run(port: int, run_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = http(port, f"/api/runs/{run_id}")
        assert status == 200, body
        data = payload_of(body)
        if data["status"] != "running":
            # 历史落盘在结果之后，等它出现再取快照
            runs = payload_of(http(port, "/api/history")[1])["runs"]
            if any(item["run_id"] == run_id for item in runs):
                return payload_of(http(port, f"/api/runs/{run_id}")[1])
        time.sleep(0.05)
    raise AssertionError("运行没有在限定时间内结束")


# ----------------------------------------------------------------------
# 清单：按包分组、嵌套目录
# ----------------------------------------------------------------------
def test_scripts_payload_groups_by_service(tmp_path: Path) -> None:
    build_project(tmp_path)
    toolkit = Toolkit(make_config(tmp_path))

    with running_server(toolkit) as port:
        status, body = http(port, "/api/scripts")

    assert status == 200
    data = payload_of(body)
    assert data["total"] == 5
    assert [pkg["name"] for pkg in data["packages"]] == ["api", "worker"]

    api = next(pkg for pkg in data["packages"] if pkg["name"] == "api")
    worker = next(pkg for pkg in data["packages"] if pkg["name"] == "worker")
    assert api["total_scripts"] == 3
    assert worker["total_scripts"] == 2

    nested = next(item for item in api["scripts"] if item["path"].endswith("unit/test_users.py"))
    assert nested["dir"] == "services/api/test/unit"
    assert nested["group"] == "unit"
    assert nested["package"] == "api"

    flat = next(item for item in api["scripts"] if item["path"].endswith("test_health.py"))
    assert flat["group"] == "（根目录）"
    assert all(item["package"] == "worker" for item in worker["scripts"])


def test_banner_lists_both_services(tmp_path: Path, capsys) -> None:
    build_project(tmp_path)

    print_banner(Toolkit(make_config(tmp_path)))

    out = capsys.readouterr().out
    assert "（2 个包）" in out
    assert "包：api, worker · 5 个测试脚本" in out


# ----------------------------------------------------------------------
# 运行：两包并发、同包互斥、多脚本一次跑
# ----------------------------------------------------------------------
def test_services_run_concurrently_and_same_package_is_exclusive(tmp_path: Path) -> None:
    build_project(tmp_path)
    toolkit = Toolkit(make_config(tmp_path))
    slow = "services/api/test/test_slow.py"

    with running_server(toolkit) as port:
        api_id = start_run(port, "api", [slow], title="API 慢用例")
        worker_id = start_run(port, "worker", ["services/worker/test/test_jobs.py"], title="Worker")

        # 另一个包不受影响：worker 先跑完，api 还在跑
        worker = wait_run(port, worker_id)
        assert worker["status"] == "passed"
        _, body = http(port, f"/api/runs/{api_id}")
        assert payload_of(body)["status"] == "running"

        # 同包第二次运行被拒
        status, body = http(
            port,
            "/api/run",
            method="POST",
            payload={"package": "api", "paths": ["services/api/test/test_health.py"]},
        )
        assert status == 409
        assert "已经有一次运行" in payload_of(body)["detail"]

        api = wait_run(port, api_id)
        assert api["status"] == "passed"

    history = toolkit.history.entries()
    assert {item["package"] for item in history} == {"api", "worker"}


def test_one_run_can_contain_multiple_scripts(tmp_path: Path) -> None:
    build_project(tmp_path)
    toolkit = Toolkit(make_config(tmp_path))

    with running_server(toolkit) as port:
        api_id = start_run(
            port,
            "api",
            ["services/api/test/test_health.py", "services/api/test/unit/test_users.py"],
            title="API 两个脚本",
        )
        api = wait_run(port, api_id)
        assert api["results"]["totals"]["tests"] == 2
        assert {item["path"] for item in api["results"]["files"]} == {
            "services/api/test/test_health.py",
            "services/api/test/unit/test_users.py",
        }

        worker_id = start_run(
            port,
            "worker",
            ["services/worker/test/test_jobs.py", "services/worker/test/test_retry.py"],
            title="Worker 两个脚本",
        )
        worker = wait_run(port, worker_id)
        assert worker["results"]["totals"]["tests"] == 2
        assert {item["path"] for item in worker["results"]["files"]} == {
            "services/worker/test/test_jobs.py",
            "services/worker/test/test_retry.py",
        }

    # 两次运行都进了历史，且脚本归属没有串
    entries = {item["package"]: item for item in toolkit.history.entries()}
    assert entries["api"]["scripts"] == [
        "services/api/test/test_health.py",
        "services/api/test/unit/test_users.py",
    ]
    assert entries["worker"]["scripts"] == [
        "services/worker/test/test_jobs.py",
        "services/worker/test/test_retry.py",
    ]


# ----------------------------------------------------------------------
# 覆盖率：按包隔离
# ----------------------------------------------------------------------
def test_coverage_is_isolated_per_service(tmp_path: Path) -> None:
    build_project(tmp_path)
    toolkit = Toolkit(make_config(tmp_path))

    with running_server(toolkit) as port:
        api_id = start_run(
            port,
            "api",
            ["services/api/test/test_health.py", "services/api/test/unit/test_users.py"],
            coverage=True,
        )
        wait_run(port, api_id)

        status, body = http(port, "/api/coverage?package=api")
        assert status == 200, body
        api_report = payload_of(body)
        api_paths = {item["path"] for item in api_report["files"]}
        assert any(path.endswith("app/health.py") for path in api_paths)
        assert not any(path.startswith("services/worker/") for path in api_paths)

        # worker 还没跑覆盖率：404，而不是拿 api 的数据凑
        status, body = http(port, "/api/coverage?package=worker")
        assert status == 404

        health_path = next(path for path in api_paths if path.endswith("app/health.py"))
        status, body = http(
            port, f"/api/coverage/file?package=api&path={health_path}"
        )
        assert status == 200, body
        assert payload_of(body)["hunks"]

        # 用 api 的身份看 worker 的文件：拒绝
        status, body = http(
            port,
            "/api/coverage/file?package=api&path=services/worker/test/test_jobs.py",
        )
        assert status == 400
        assert "不属于 API" in payload_of(body)["detail"]

        worker_id = start_run(
            port,
            "worker",
            ["services/worker/test/test_jobs.py", "services/worker/test/test_retry.py"],
            coverage=True,
        )
        wait_run(port, worker_id)

        status, body = http(port, "/api/coverage?package=worker")
        assert status == 200, body
        worker_paths = {item["path"] for item in payload_of(body)["files"]}
        assert any(path.endswith("worker/job.py") for path in worker_paths)
        assert not any(path.startswith("services/api/") for path in worker_paths)

        # api 的报告不受影响
        status, body = http(port, "/api/coverage?package=api")
        assert status == 200
        assert {item["path"] for item in payload_of(body)["files"]} == api_paths


# ----------------------------------------------------------------------
# CLI：--package 只作用于选中的那套
# ----------------------------------------------------------------------
def test_cli_package_filter_with_multiple_services(tmp_path: Path, capsys) -> None:
    build_project(tmp_path)
    config = make_config(tmp_path)
    toolkit = Toolkit(config)

    cmd_list(toolkit, select_packages(config.packages, "api"))
    api_out = capsys.readouterr().out
    assert "API  (services/api)" in api_out
    assert "Worker" not in api_out
    assert "合计：1 个包，3 个脚本文件" in api_out

    toolkit.scanner.mark_baseline(None)  # 先把两套都立为基线

    toolkit.scanner.mark_baseline(None)  # 先把两套都立为基线

    # 只改 worker 的脚本：--package api 不应报出变更
    (tmp_path / "services/worker/test/test_retry.py").write_text(
        "def test_retry():\n    assert True\n\n\ndef test_more():\n    assert True\n",
        encoding="utf-8",
    )
    cmd_changed(toolkit, select_packages(config.packages, "api"))
    assert "没有检测到测试脚本变化" in capsys.readouterr().out

    cmd_changed(toolkit, select_packages(config.packages, "worker"))
    worker_out = capsys.readouterr().out
    assert "Worker  (1)" in worker_out
    assert "services/worker/test/test_retry.py" in worker_out


def test_cli_rejects_unknown_package(tmp_path: Path) -> None:
    build_project(tmp_path)
    config = make_config(tmp_path)

    with pytest.raises(SystemExit, match="未知的包"):
        select_packages(config.packages, "gateway")
