#!/usr/bin/env python3
"""测试面板本地服务：让页面上的按钮真的能跑 pytest。

为什么需要它：浏览器不允许 `file://` 页面执行本地命令，也不允许它跨源请求
本地端口。所以"能点按钮跑测试"的页面必须由 `http://127.0.0.1` 提供。

用法：

    uv run python -m testlens serve               # 默认 8787；不自动开页面
    uv run python -m testlens serve --browser     # 起服务后直接打开页面
    uv run python -m testlens serve --port 9000
    uv run python -m testlens dev apps/web-service     # 给目录：先读目录里的配置

作为插件嵌入时不用命令行：`toolkit.serve()`。

安全边界（这是本机开发工具，不是对外服务）：

- 只绑回环地址（host 可配，但校验只允许 localhost / ::1 / 127.0.0.0/8）；
- 只接受各包测试目录内 `test_*.py` 的相对路径，跨包/越界一律 400；
- 按包分槽：同一包内只允许一次运行，两个 pytest 会抢同一批 fixture；
- 校验请求来源（Origin），命令用列表传参、不经 shell。

零第三方依赖，只用标准库。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config, root_prefix, source_label
from .coverage_report import NoCoverageData
from .inventory import render_html_string
from .runner import BadRequest
from .toolkit import Toolkit


def refresh_static_page(toolkit: Toolkit) -> None:
    """启动时顺手刷新静态页（config.dist_dir / config.static_page_name）。

    静态页是给「不启动服务、直接双击」的场景用的。和面板用同一份模板，
    只是把"可运行"开关关掉。
    """
    try:
        toolkit.build_static_page()
    except Exception as error:  # noqa: BLE001 - 静态页失败不该拖垮服务
        print(f"  静态页刷新失败（不影响面板）：{type(error).__name__}: {error}", flush=True)


def port_in_use(host: str, port: int) -> bool:
    """端口是否已有人监听。

    不能只依赖 bind 报错：HTTPServer 默认 allow_reuse_address=1，Windows 上
    这会让**两个进程同时绑同一个端口**（实测 8787 出现两条 LISTENING），
    请求随机落到旧进程上，表现是"改了代码页面却没变"。所以先 connect 探一下。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex((host, port)) == 0


class ToolkitServer(ThreadingHTTPServer):
    """把 toolkit 挂在 server 对象上，Handler 通过 self.server.toolkit 访问。"""

    daemon_threads = True

    def __init__(self, toolkit: Toolkit) -> None:
        config = toolkit.config
        super().__init__((config.host, config.port), Handler)
        self.toolkit = toolkit


class Handler(BaseHTTPRequestHandler):
    server_version = "TestLens/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 依赖访问 ----------
    @property
    def toolkit(self) -> Toolkit:
        return self.server.toolkit  # type: ignore[attr-defined]

    @property
    def config(self) -> Config:
        return self.server.toolkit.config  # type: ignore[attr-defined]

    # ---------- 基础输出 ----------
    def _send(
        self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status)

    def _error(self, message: str, status: HTTPStatus) -> None:
        self._json({"detail": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            raise BadRequest("请求体为空或过大")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BadRequest(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise BadRequest("请求体必须是 JSON 对象")
        return payload

    def _origin_allowed(self) -> bool:
        """挡住别的网站借浏览器对本机端口发指令（DNS rebinding / CSRF）。

        通过 http://127.0.0.1:8787 打开的页面，Origin 就是它自己；curl 之类
        不带 Origin，放行。其它来源一律拒绝——这里能执行命令，不能只靠
        "监听回环地址"这一层。
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        host = self.config.host
        port = self.server.server_port  # type: ignore[attr-defined] - HTTPServer 才有
        allowed = {f"http://{host}:{port}", f"http://localhost:{port}"}
        if ":" in host:
            # IPv6 字面量在 URL 里必须带方括号：http://[::1]:8787
            allowed.add(f"http://[{host}]:{port}")
        return origin in allowed

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 覆盖基类签名
        # 运行中的状态轮询每 400ms 一次，打日志只会把终端刷满
        if self.command == "GET" and self.path.startswith("/api/runs/"):
            return
        # flush：输出重定向到管道时是块缓冲，不刷就看不清谁在请求
        print(f"  {self.address_string()} {format % args}", flush=True)

    # ---------- 路由 ----------
    def do_GET(self) -> None:  # noqa: N802 - 基类约定
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            try:
                payload = self.toolkit.build_payload()
                body = render_html_string(self.config, payload, run_enabled=True).encode("utf-8")
            except Exception as error:  # noqa: BLE001
                self._error(
                    f"生成页面失败：{type(error).__name__}: {error}",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self._send(body, content_type="text/html; charset=utf-8")
            return

        if path == "/favicon.ico":
            self._send(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
            return

        if path == "/api/scripts":
            self._json(self.toolkit.build_payload())
            return

        if path == "/api/coverage":
            try:
                package = self.toolkit.resolve_package((query.get("package") or [""])[0])
                self._json(self.toolkit.coverage.report(package))
            except BadRequest as error:
                self._error(str(error), HTTPStatus.BAD_REQUEST)
            except NoCoverageData as error:
                self._error(str(error), HTTPStatus.NOT_FOUND)
            return

        if path == "/api/coverage/file":
            try:
                package = self.toolkit.resolve_package((query.get("package") or [""])[0])
                relative = (query.get("path") or [""])[0]
                if not relative:
                    raise BadRequest("缺少 path 参数")
                owner = self.toolkit.package_of_path(relative)
                if owner != package:
                    raise BadRequest(f"{relative} 不属于 {package.label}")
                self._json(self.toolkit.coverage.file_detail(package, relative))
            except BadRequest as error:
                self._error(str(error), HTTPStatus.BAD_REQUEST)
            except NoCoverageData as error:
                self._error(str(error), HTTPStatus.NOT_FOUND)
            return

        if path == "/api/history":
            self._json({"runs": self.toolkit.history.entries()})
            return

        history_id, history_kind = self._parse_item_path(path, "history", "entry")
        if history_id is not None:
            if history_kind == "log":
                log = self.toolkit.history.read_log(history_id)
                if log is None:
                    self._error("这次运行没有留下日志", HTTPStatus.NOT_FOUND)
                    return
                self._send(log.encode("utf-8"), "text/plain; charset=utf-8")
                return

            entry = self.toolkit.history.find(history_id)
            if entry is None:
                self._error("历史里没有这次运行", HTTPStatus.NOT_FOUND)
                return
            self._json({**entry, "output": self.toolkit.history.read_log(history_id) or ""})
            return

        run_id, kind = self._parse_item_path(path, "runs", "status")
        if run_id is not None:
            run = self.toolkit.runner.get(run_id)
            if run is None:
                self._error("找不到这次运行（服务重启后会清空）", HTTPStatus.NOT_FOUND)
                return
            if kind == "log":
                self._send(
                    run.snapshot(self.config.max_output_lines).encode("utf-8"),
                    content_type="text/plain; charset=utf-8",
                )
            else:
                self._json(run.to_dict())
            return

        self._error("未知路径", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - 基类约定
        path = urlparse(self.path).path

        if not self._origin_allowed():
            self._error("只接受本页面发起的请求", HTTPStatus.FORBIDDEN)
            return

        if path == "/api/scripts/confirm":
            written = self.toolkit.scanner.mark_baseline(None)
            self._json({"confirmed": written, "data": self.toolkit.build_payload()})
            return

        if path == "/api/run":
            try:
                payload = self._body()
                package = self.toolkit.resolve_package(payload.get("package"))
                paths = self.toolkit.runner.validate_paths(payload.get("paths"), package)
            except BadRequest as error:
                self._error(str(error), HTTPStatus.BAD_REQUEST)
                return
            title = str(payload.get("title") or "手动选择")[:120]
            try:
                run = self.toolkit.runner.start(
                    package,
                    paths,
                    title,
                    coverage=bool(payload.get("coverage")),
                )
            except BadRequest as error:
                self._error(str(error), HTTPStatus.CONFLICT)
                return
            self._json({"run_id": run.run_id, "package": run.package})
            return

        run_id, kind = self._parse_item_path(path, "runs", "status")
        if run_id is not None and kind == "stop":
            if self.toolkit.runner.stop(run_id):
                self._json({"stopped": True})
            else:
                self._error("这次运行已经结束", HTTPStatus.CONFLICT)
            return

        self._error("未知路径", HTTPStatus.NOT_FOUND)

    @staticmethod
    def _parse_item_path(path: str, segment: str, default: str) -> tuple[str | None, str]:
        """/api/<segment>/<id>[/<kind>] -> (id, kind)；不匹配时 id 为 None。"""
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == segment:
            return parts[2], parts[3] if len(parts) > 3 else default
        return None, ""


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------

def print_banner(toolkit: Toolkit) -> None:
    """启动横幅：先回答「这份面板读的是哪个配置」，再说包与脚本数。

    反例就是最常见的困惑：在仓库根 `testlens dev` 会读到根的 testlens.toml
    （两个包），而不是某个应用自己的 testlens_config.py。第一行写清配置来源，
    就不用盯着包 tab 猜了。
    """
    config = toolkit.config
    label = source_label(config)
    print()
    print(f"配置：{label or '无配置文件（包定义来自代码或目录推断）'}（{len(config.packages)} 个包）")
    print(f"仓库：{config.repo_root}")
    result = toolkit.scan()
    names = ", ".join(pkg.name for pkg in config.packages)
    print(f"包：{names} · {result['total']} 个测试脚本")
    if not result["total"]:
        first = config.packages[0]
        target_dir = Path(config.repo_root) / first.root / first.tests_dir
        print(f"  一个都没扫到：确认 {target_dir} 下有 {first.test_glob}")


def warm_coverage(toolkit: Toolkit) -> list[str]:
    """把缺失的覆盖率数据文件先跑出来，返回真正跑过的包名。

    只处理「配了 coverage_data_file 但文件不存在」的包；已经有数据就跳过。
    命令和面板里的运行保持一致（同一个 pytest_cmd、同一组 coverage_args、同一 cwd），
    所以生成出来的数据文件正是覆盖率页签要读的那一个。
    """
    config = toolkit.config
    warmed: list[str] = []
    # 没有 source_pkg / coverage_args 的包没法预热：连该统计哪个模块都不知道。
    # 这里要出声，否则 --warm-coverage 看起来像"启动没反应"。
    configured = [pkg for pkg in config.packages if pkg.coverage_data_file and pkg.coverage_args]
    if not configured:
        print(
            "\n  没有包配置覆盖率（[[package]] 缺 source_pkg），跳过预热。"
            "覆盖率需要知道统计哪个模块，光靠目录推断不够。"
        )
        return warmed

    for pkg in configured:
        data_file = (config.repo_root / pkg.coverage_data_file).resolve()
        if data_file.is_file():
            continue

        tests_path = f"{root_prefix(pkg)}{pkg.tests_dir}"
        # 和面板里的运行走同一个构造器：pytest_cmd / -q / basetemp / --cov 参数
        # 一旦两边分叉，预热生成的数据文件就可能不是页签要读的那一个。
        command = toolkit.runner.command_for(pkg, [tests_path], coverage=True)
        # COVERAGE_FILE 高于 .coveragerc 的 data_file：不管配置在不在、cwd 是哪儿，
        # 都写到「这个包该读的那个文件」里，否则生成完页签还是空的。
        env = {**os.environ, "COVERAGE_FILE": str(data_file)}
        print(f"\n生成覆盖率数据：{pkg.label}（{data_file.name} 不存在，先跑一遍 {tests_path}/）")
        print(f"$ {' '.join(command)}", flush=True)
        try:
            completed = subprocess.run(
                command,
                cwd=config.repo_root,
                timeout=config.run_timeout,
                check=False,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as error:
            print(f"  生成失败：{type(error).__name__}: {error}（面板照常启动）", flush=True)
            continue
        if completed.returncode != 0:
            print(
                f"  退出码 {completed.returncode}：测试没过，覆盖率可能不完整（面板照常启动）",
                flush=True,
            )
        warmed.append(pkg.name)
    return warmed


def run(toolkit: Toolkit) -> int:
    """起服务。CLI 和插件宿主都走这里；是否开浏览器看 config.open_browser。"""
    config = toolkit.config

    if not config.template_path.is_file():
        print(f"模板缺失：{config.template_path}")
        return 1

    if port_in_use(config.host, config.port):
        print(f"端口 {config.port} 已经有服务在跑（多半是上一个面板没关）")
        print(f"  打开 http://{config.host}:{config.port}/ 看看是不是它")
        print(f"  关掉它，或者在原命令后面加 --port {config.port + 1} 再跑一次")
        return 1

    print_banner(toolkit)
    if config.coverage_warmup:
        warm_coverage(toolkit)

    try:
        httpd = ToolkitServer(toolkit)
    except OSError as error:
        print(f"端口 {config.port} 起不来：{error}")
        print(f"  换一个：在原命令后面加 --port {config.port + 1}")
        return 1

    refresh_static_page(toolkit)
    url = f"http://{config.host}:{config.port}/"
    print(f"\n测试面板：{url}")
    print("  页面上的按钮直接跑 pytest，Ctrl+C 退出\n", flush=True)

    if config.open_browser:
        opener = threading.Timer(0.4, lambda: webbrowser.open(url))
        opener.daemon = True
        opener.start()
    else:
        # 关掉自动打开后，至少要把「怎么打开」写在 URL 下面，否则第一次用会以为面板没起来
        print(
            "  自动打开页面已关闭：[server] open_browser = true 或加 --browser（只想这次打开）",
            flush=True,
        )

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        toolkit.runner.shutdown()
        httpd.server_close()
    return 0
