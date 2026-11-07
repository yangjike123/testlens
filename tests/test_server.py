"""面板服务端测试：真的起一个 HTTP 服务，用真实请求打每一条路由。

不用 mock 顶替 handler：路由、状态码、请求体校验、Origin 校验都在这一层，
只有发真请求才能同时验证「前端点一下会发生什么」。端口用 0 让系统分配，
不占 8787，也不会有两个测试抢同一个端口。
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from testlens import PackageSpec, Toolkit, build_config
from testlens import server as server_module
from testlens.server import (
    ToolkitServer,
    display_host,
    port_in_use,
    print_banner,
    refresh_static_page,
    run,
    warm_coverage,
)

PYTEST_CMD = f'"{sys.executable}" -m pytest'
COVERAGE_JSON_CMD = f'"{sys.executable}" -m coverage json'
_MISSING = object()


# ----------------------------------------------------------------------
# 测试用项目与 HTTP 客户端
# ----------------------------------------------------------------------
def add_demo_module(root: Path) -> None:
    (root / "demo").mkdir(parents=True, exist_ok=True)
    (root / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (root / "demo" / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def never_called(value):\n"
        "    if value:\n"
        "        return 1\n"
        "    return 2\n",
        encoding="utf-8",
    )


def project_config(root: Path, *, coverage: bool = False, **kw):
    tests = root / "test"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    package_kw = {}
    if coverage:
        add_demo_module(root)
        package_kw = {
            "coverage_data_file": ".coverage.demo",
            "coverage_args": ["--cov=demo", "--cov-report="],
        }
    package = PackageSpec(name="demo", label="Demo", root=".", **package_kw)
    base = {
        "packages": (package,),
        "repo_root": root,
        "data_dir": root / "data",
        "dist_dir": root / "dist",
        "pytest_cmd": PYTEST_CMD,
        "coverage_json_cmd": COVERAGE_JSON_CMD,
        "coverage_warmup": False,
        "run_timeout": 60.0,
    }
    base.update(kw)
    return build_config(**base)


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


def http(
    port: int,
    path: str,
    *,
    method: str = "GET",
    payload=_MISSING,
    raw: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
):
    data = raw
    if payload is not _MISSING:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers or {}
    )
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read()


def payload_of(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def start_run(port: int, paths: list[str], **extra) -> str:
    status, _, body = http(
        port, "/api/run", method="POST", payload={"package": "demo", "paths": paths, **extra}
    )
    assert status == 200, body
    return payload_of(body)["run_id"]


def wait_run(port: int, run_id: str, timeout: float = 60.0, *, wait_history: bool = True) -> dict:
    """等到运行收尾并落盘。

    状态先变、results 与历史随后写入，所以要在 history 里看到这条才算真正结束；
    返回前再取一次，避免拿到那一刻的旧快照。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _, body = http(port, f"/api/runs/{run_id}")
        assert status == 200, body
        data = payload_of(body)
        if data["status"] != "running":
            if not wait_history:
                return data
            runs = payload_of(http(port, "/api/history")[2])["runs"]
            if any(item["run_id"] == run_id for item in runs):
                return payload_of(http(port, f"/api/runs/{run_id}")[2])
        time.sleep(0.05)
    raise AssertionError("运行没有在限定时间内结束")


# ----------------------------------------------------------------------
# GET 路由
# ----------------------------------------------------------------------
def test_serves_dashboard_scripts_and_history(tmp_path: Path) -> None:
    toolkit = Toolkit(project_config(tmp_path))

    with running_server(toolkit) as port:
        status, content_type, body = http(port, "/")
        assert status == 200
        assert content_type.startswith("text/html")
        assert "window.__TESTLENS_RUN__ = true" in body.decode("utf-8")

        status, _, body = http(port, "/index.html")
        text = body.decode("utf-8")
        assert status == 200
        assert "window.__TESTLENS_DATA__ = {" in text
        assert "__STYLE__" not in text

        status, content_type, body = http(port, "/favicon.ico")
        assert (status, body) == (204, b"")
        assert content_type == "image/x-icon"

        status, _, body = http(port, "/api/scripts")
        assert status == 200
        data = payload_of(body)
        assert data["total"] == 1
        assert data["packages"][0]["scripts"][0]["path"] == "test/test_ok.py"

        status, _, body = http(port, "/api/history")
        assert status == 200 and payload_of(body) == {"runs": []}


def test_dashboard_render_failure_returns_500(tmp_path: Path, monkeypatch) -> None:
    toolkit = Toolkit(project_config(tmp_path))

    def boom(packages=None):
        raise RuntimeError("模板坏了")

    monkeypatch.setattr(toolkit, "build_payload", boom)
    with running_server(toolkit) as port:
        status, _, body = http(port, "/")

    assert status == 500
    assert "模板坏了" in payload_of(body)["detail"]


def test_unknown_paths_return_404(tmp_path: Path) -> None:
    toolkit = Toolkit(project_config(tmp_path))

    with running_server(toolkit) as port:
        assert http(port, "/nope")[0] == 404
        assert http(port, "/api/runs/20260101-000000-abcd")[0] == 404
        assert http(port, "/nope", method="POST")[0] == 404


# ----------------------------------------------------------------------
# 覆盖率路由
# ----------------------------------------------------------------------
def test_coverage_without_data_reports_reason(tmp_path: Path) -> None:
    toolkit = Toolkit(project_config(tmp_path))

    with running_server(toolkit) as port:
        status, _, body = http(port, "/api/coverage?package=demo")
        assert status == 404
        assert "未配置覆盖率" in payload_of(body)["detail"]

        status, _, body = http(port, "/api/coverage")
        assert status == 400
        assert "缺少 package" in payload_of(body)["detail"]

        status, _, body = http(port, "/api/coverage/file?package=demo")
        assert status == 400
        assert "缺少 path" in payload_of(body)["detail"]

        status, _, body = http(port, "/api/coverage/file?package=demo&path=demo/calc.py")
        assert status == 404


def test_coverage_after_covered_run(tmp_path: Path) -> None:
    toolkit = Toolkit(project_config(tmp_path, coverage=True))

    with running_server(toolkit) as port:
        run_id = start_run(port, ["test/test_ok.py"], coverage=True)
        wait_run(port, run_id)

        status, _, body = http(port, "/api/coverage?package=demo")
        assert status == 200, body
        report = payload_of(body)
        assert report["totals"]["statements"] > 0
        assert any(item["path"] == "demo/calc.py" for item in report["files"])

        status, _, body = http(port, "/api/coverage/file?package=demo&path=demo/calc.py")
        assert status == 200, body
        detail = payload_of(body)
        assert detail["path"] == "demo/calc.py"
        assert detail["hunks"]
        assert any(line["missing"] for hunk in detail["hunks"] for line in hunk["lines"])

        # 报告里没有的文件不能假装有
        status, _, body = http(port, "/api/coverage/file?package=demo&path=README.md")
        assert status == 404
        assert "没有这个文件" in payload_of(body)["detail"]


def test_coverage_file_must_belong_to_package(tmp_path: Path) -> None:
    for name in ("alpha", "beta"):
        (tmp_path / f"apps/{name}/test").mkdir(parents=True)
        (tmp_path / f"apps/{name}/test/test_{name}.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
        )
    config = build_config(
        packages=(
            PackageSpec(name="alpha", label="Alpha", root="apps/alpha"),
            PackageSpec(name="beta", label="Beta", root="apps/beta"),
        ),
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        pytest_cmd=PYTEST_CMD,
    )
    toolkit = Toolkit(config)

    with running_server(toolkit) as port:
        status, _, body = http(
            port, "/api/coverage/file?package=alpha&path=apps/beta/test/test_beta.py"
        )

    assert status == 400
    assert "不属于 Alpha" in payload_of(body)["detail"]


# ----------------------------------------------------------------------
# 确认基线
# ----------------------------------------------------------------------
def test_confirm_marks_baseline(tmp_path: Path) -> None:
    toolkit = Toolkit(project_config(tmp_path))

    with running_server(toolkit) as port:
        http(port, "/api/scripts")  # 首次打开会写基线
        (tmp_path / "test" / "test_ok.py").write_text(
            "def test_ok():\n    assert True\n\n\ndef test_more():\n    assert True\n",
            encoding="utf-8",
        )
        assert payload_of(http(port, "/api/scripts")[2])["changed"] == 1

        status, _, body = http(port, "/api/scripts/confirm", method="POST", payload={})
        data = payload_of(body)

    assert status == 200
    assert data["confirmed"] == 1
    assert data["data"]["changed"] == 0


def test_post_rejects_foreign_origin(tmp_path: Path) -> None:
    toolkit = Toolkit(project_config(tmp_path))

    with running_server(toolkit) as port:
        status, _, body = http(
            port,
            "/api/scripts/confirm",
            method="POST",
            payload={},
            headers={"Origin": "http://evil.example"},
        )
        assert status == 403
        assert "只接受本页面" in payload_of(body)["detail"]

        status, _, _ = http(
            port,
            "/api/scripts/confirm",
            method="POST",
            payload={},
            headers={"Origin": f"http://127.0.0.1:{port}"},
        )
        assert status == 200


# ----------------------------------------------------------------------
# 运行路由
# ----------------------------------------------------------------------
def test_run_flow_and_history_routes(tmp_path: Path) -> None:
    toolkit = Toolkit(project_config(tmp_path))

    with running_server(toolkit) as port:
        run_id = start_run(port, ["test/test_ok.py"], title="全绿")
        data = wait_run(port, run_id)
        assert data["status"] == "passed"
        assert data["results"]["totals"]["passed"] == 1
        assert data["log_available"] is True

        status, content_type, body = http(port, f"/api/runs/{run_id}/log")
        assert status == 200
        assert content_type.startswith("text/plain")
        assert "退出码 0" in body.decode("utf-8")

        # 历史：等落盘
        deadline = time.time() + 10
        while time.time() < deadline and not payload_of(http(port, "/api/history")[2])["runs"]:
            time.sleep(0.05)
        runs = payload_of(http(port, "/api/history")[2])["runs"]
        assert runs[0]["run_id"] == run_id

        status, _, body = http(port, f"/api/history/{run_id}")
        assert status == 200
        assert payload_of(body)["output"]

        status, _, body = http(port, f"/api/history/{run_id}/log")
        assert status == 200 and b"" != body

        status, _, body = http(port, "/api/history/20260101-000000-ffff")
        assert status == 404 and "没有这次运行" in payload_of(body)["detail"]

        # 日志被删掉：接口要区分「记录在、日志没了」
        (toolkit.config.data_dir / "run-history" / f"{run_id}.log").unlink()
        status, _, body = http(port, f"/api/history/{run_id}/log")
        assert status == 404 and "没有留下日志" in payload_of(body)["detail"]
        status, _, body = http(port, f"/api/history/{run_id}")
        assert status == 200 and payload_of(body)["output"] == ""

        # 非法 run_id 走历史查询也是 404，不会穿透成路径
        assert http(port, "/api/history/..%2Fetc")[0] == 404


def test_run_rejects_bad_requests(tmp_path: Path) -> None:
    toolkit = Toolkit(project_config(tmp_path))
    (tmp_path / "test" / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(5)\n", encoding="utf-8"
    )

    with running_server(toolkit) as port:
        assert http(port, "/api/run", method="POST", raw=b"")[0] == 400
        # Content-Length 超过 1MB：在读 body 之前就拒绝
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(
                b"POST /api/run HTTP/1.1\r\nHost: x\r\nContent-Length: 2000000\r\n\r\n"
            )
            assert b" 400 " in sock.recv(4096)

        status, _, body = http(port, "/api/run", method="POST", raw=b"{not json")
        assert status == 400 and "不是合法 JSON" in payload_of(body)["detail"]

        status, _, body = http(port, "/api/run", method="POST", payload=[1, 2])
        assert status == 400 and "JSON 对象" in payload_of(body)["detail"]

        status, _, body = http(port, "/api/run", method="POST", payload={"paths": []})
        assert status == 400 and "缺少 package" in payload_of(body)["detail"]

        status, _, body = http(
            port, "/api/run", method="POST", payload={"package": "demo", "paths": []}
        )
        assert status == 400 and "非空数组" in payload_of(body)["detail"]

        # 同包只能跑一个：第二次 409，停掉之后停止接口也要变成 409
        run_id = start_run(port, ["test/test_slow.py"], title="占位")
        try:
            status, _, body = http(
                port,
                "/api/run",
                method="POST",
                payload={"package": "demo", "paths": ["test/test_slow.py"]},
            )
            assert status == 409 and "已经有一次运行" in payload_of(body)["detail"]

            status, _, body = http(port, f"/api/runs/{run_id}/stop", method="POST", payload={})
            assert status == 200 and payload_of(body) == {"stopped": True}

            status, _, body = http(port, f"/api/runs/{run_id}/stop", method="POST", payload={})
            assert status == 409 and "已经结束" in payload_of(body)["detail"]
        finally:
            http(port, f"/api/runs/{run_id}/stop", method="POST", payload={})

        status, _, body = http(
            port, "/api/runs/20260101-000000-ffff/stop", method="POST", payload={}
        )
        assert status == 409


# ----------------------------------------------------------------------
# 启动入口：横幅 / 预热 / 端口 / Ctrl+C
# ----------------------------------------------------------------------
def test_port_in_use_detects_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert port_in_use("127.0.0.1", port) is True

    assert port_in_use("127.0.0.1", port) is False


def test_display_host_brackets_ipv6() -> None:
    assert display_host("127.0.0.1") == "127.0.0.1"
    assert display_host("localhost") == "localhost"
    assert display_host("::1") == "[::1]"


def test_port_in_use_treats_socket_errors_as_free(monkeypatch) -> None:
    """本机没启用 IPv6 时 socket() 会抛 OSError：当成未被占用，让 bind 去报明确错误。"""

    class BoomSocket:
        def __init__(self, *args, **kwargs):
            raise OSError("address family not supported")

    monkeypatch.setattr(server_module.socket, "socket", BoomSocket)

    assert port_in_use("::1", 8787) is False


def test_toolkit_server_selects_ipv6_family(monkeypatch, tmp_path: Path) -> None:
    """host 里带冒号就切 AF_INET6，不依赖本机是否真的启用 IPv6。"""
    recorded: dict = {}

    def fake_init(self, address, handler):
        recorded["family"] = self.address_family

    monkeypatch.setattr(server_module.ThreadingHTTPServer, "__init__", fake_init)
    config = replace(project_config(tmp_path), host="::1")

    ToolkitServer(Toolkit(config))

    assert recorded["family"] == socket.AF_INET6


def test_port_in_use_supports_ipv6() -> None:
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.bind(("::1", 0))
        sock.listen(1)
    except OSError:
        pytest.skip("本机没有启用 IPv6 回环")
    port = sock.getsockname()[1]
    try:
        assert port_in_use("::1", port) is True
    finally:
        sock.close()

    assert port_in_use("::1", port) is False


def test_server_binds_ipv6_and_serves(tmp_path: Path) -> None:
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        probe.bind(("::1", 0))
        probe.close()
    except OSError:
        pytest.skip("本机没有启用 IPv6 回环")

    config = replace(project_config(tmp_path), host="::1", port=0)
    httpd = ToolkitServer(Toolkit(config))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        import http.client

        connection = http.client.HTTPConnection("::1", httpd.server_port, timeout=5)
        connection.request("GET", "/api/scripts")
        response = connection.getresponse()
        body = response.read()
        connection.close()
        assert response.status == 200
        assert payload_of(body)["total"] == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_run_prints_bracketed_ipv6_url(tmp_path: Path, monkeypatch, capsys) -> None:
    config = replace(project_config(tmp_path), host="::1", port=9123)
    toolkit = Toolkit(config)
    monkeypatch.setattr(server_module, "ToolkitServer", DummyServer)
    monkeypatch.setattr(server_module, "port_in_use", lambda host, port: False)
    monkeypatch.setattr(toolkit.runner, "shutdown", lambda: None)

    assert run(toolkit) == 0
    assert "http://[::1]:9123/" in capsys.readouterr().out


def test_refresh_static_page_swallows_errors(tmp_path: Path, monkeypatch, capsys) -> None:
    toolkit = Toolkit(project_config(tmp_path))

    def boom(*args, **kw):
        raise ValueError("磁盘只读")

    monkeypatch.setattr(toolkit, "build_static_page", boom)

    refresh_static_page(toolkit)

    assert "静态页刷新失败" in capsys.readouterr().out


def test_print_banner_mentions_missing_scripts(tmp_path: Path, capsys) -> None:
    (tmp_path / "test").mkdir()
    config = build_config(
        packages=(PackageSpec(name="demo", label="Demo", root="."),),
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        dist_dir=tmp_path / "dist",
    )

    print_banner(Toolkit(config))

    out = capsys.readouterr().out
    assert "一个都没扫到" in out
    assert "test_glob" not in out  # 提示的是真实的 glob，而不是字段名


def test_warm_coverage_skips_without_configuration(tmp_path: Path, capsys) -> None:
    toolkit = Toolkit(project_config(tmp_path))

    assert warm_coverage(toolkit) == []
    assert "没有包配置覆盖率" in capsys.readouterr().out


def test_warm_coverage_skips_existing_data(tmp_path: Path, capsys) -> None:
    toolkit = Toolkit(project_config(tmp_path, coverage=True))
    (tmp_path / ".coverage.demo").write_text("", encoding="utf-8")

    assert warm_coverage(toolkit) == []
    assert "生成覆盖率数据" not in capsys.readouterr().out


def test_warm_coverage_runs_command(tmp_path: Path, capsys) -> None:
    config = project_config(
        tmp_path, coverage=True, pytest_cmd=f'"{sys.executable}" -c "print(1)"'
    )
    toolkit = Toolkit(config)

    assert warm_coverage(toolkit) == ["demo"]
    assert "生成覆盖率数据" in capsys.readouterr().out


def test_warm_coverage_reports_nonzero_exit(tmp_path: Path, capsys) -> None:
    config = project_config(
        tmp_path,
        coverage=True,
        pytest_cmd=f'"{sys.executable}" -c "raise SystemExit(3)"',
    )
    toolkit = Toolkit(config)

    assert warm_coverage(toolkit) == ["demo"]
    assert "退出码 3" in capsys.readouterr().out


def test_warm_coverage_survives_missing_command(tmp_path: Path, capsys) -> None:
    config = project_config(
        tmp_path, coverage=True, pytest_cmd="definitely-not-a-real-command-xyz"
    )
    toolkit = Toolkit(config)

    assert warm_coverage(toolkit) == []
    assert "生成失败" in capsys.readouterr().out


def test_warm_coverage_survives_timeout(tmp_path: Path, capsys) -> None:
    config = project_config(
        tmp_path,
        coverage=True,
        pytest_cmd=f'"{sys.executable}" -c "import time; time.sleep(5)"',
        run_timeout=0.3,
    )
    toolkit = Toolkit(config)

    assert warm_coverage(toolkit) == []
    assert "生成失败" in capsys.readouterr().out


def test_origin_allows_ipv6_bracket_form() -> None:
    """IPv6 字面量在 URL 里必须带方括号：http://[::1]:8787。"""
    from testlens.server import Handler

    handler = SimpleNamespace(
        headers={"Origin": "http://[::1]:8787"},
        config=SimpleNamespace(host="::1"),
        server=SimpleNamespace(server_port=8787),
    )

    assert Handler._origin_allowed(handler) is True

    handler.headers = {"Origin": "http://[::2]:8787"}
    assert Handler._origin_allowed(handler) is False


def test_run_warms_coverage_when_enabled(tmp_path: Path, monkeypatch, capsys) -> None:
    config = replace(project_config(tmp_path), coverage_warmup=True)
    toolkit = Toolkit(config)
    monkeypatch.setattr(server_module, "ToolkitServer", DummyServer)
    monkeypatch.setattr(toolkit.runner, "shutdown", lambda: None)

    assert run(toolkit) == 0

    # 这份配置没有 source_pkg：预热要说明为什么跳过，而不是静默
    assert "没有包配置覆盖率" in capsys.readouterr().out


class DummyServer:
    """替身：serve_forever 立刻按 Ctrl+C 处理，用来测 run() 的收尾。"""

    instances: list["DummyServer"] = []

    def __init__(self, toolkit: Toolkit) -> None:
        self.toolkit = toolkit
        self.closed = False
        DummyServer.instances.append(self)

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


def test_run_stops_cleanly_on_ctrl_c(tmp_path: Path, monkeypatch, capsys) -> None:
    toolkit = Toolkit(project_config(tmp_path))
    shutdown_calls: list[bool] = []
    monkeypatch.setattr(server_module, "ToolkitServer", DummyServer)
    monkeypatch.setattr(toolkit.runner, "shutdown", lambda: shutdown_calls.append(True))

    assert run(toolkit) == 0

    out = capsys.readouterr().out
    assert "测试面板：http://127.0.0.1:" in out
    assert "自动打开页面已关闭" in out
    assert shutdown_calls == [True]
    assert DummyServer.instances[-1].closed is True


def test_run_opens_browser_when_configured(tmp_path: Path, monkeypatch) -> None:
    config = replace(project_config(tmp_path), open_browser=True)
    toolkit = Toolkit(config)
    opened: list[str] = []

    class ImmediateTimer:
        def __init__(self, delay, function, args=()):
            self.daemon = False
            self._function = function

        def start(self) -> None:
            self._function()

    monkeypatch.setattr(server_module, "ToolkitServer", DummyServer)
    monkeypatch.setattr(toolkit.runner, "shutdown", lambda: None)
    monkeypatch.setattr(server_module.webbrowser, "open", opened.append)
    monkeypatch.setattr(server_module.threading, "Timer", ImmediateTimer)

    assert run(toolkit) == 0
    assert opened == [f"http://{config.host}:{config.port}/"]


def test_run_rejects_missing_template(tmp_path: Path, capsys) -> None:
    config = replace(project_config(tmp_path), template_path=tmp_path / "missing.html")

    assert run(Toolkit(config)) == 1
    assert "模板缺失" in capsys.readouterr().out


def test_run_rejects_busy_port(tmp_path: Path, capsys) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        config = replace(project_config(tmp_path), port=port)

        assert run(Toolkit(config)) == 1

    out = capsys.readouterr().out
    assert "已经有服务在跑" in out
    assert f"--port {port + 1}" in out


def test_run_reports_bind_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    toolkit = Toolkit(project_config(tmp_path))

    def boom(toolkit):
        raise OSError("地址被占用")

    monkeypatch.setattr(server_module, "ToolkitServer", boom)

    assert run(toolkit) == 1
    out = capsys.readouterr().out
    assert "起不来" in out and "地址被占用" in out
