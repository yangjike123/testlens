"""Toolkit：把 config 与各服务对象组装在一起，插件接入的唯一入口。

    from testlens import PackageSpec, Toolkit, build_config

    config = build_config(packages=(PackageSpec(...),), repo_root=Path.cwd())
    toolkit = Toolkit(config)

    toolkit.scan()                     # 清单 + 变更检测（返回扫描结果）
    toolkit.build_payload()            # 页面数据
    toolkit.build_static_page()        # 生成静态页，返回写出的文件
    toolkit.runner.start(...)          # 跑测试
    toolkit.serve()                    # 起面板，阻塞到 Ctrl+C

宿主只需要 `build_config` / `PackageSpec` / `Toolkit` 三样东西；`server`、
`inventory` 这些是实现细节，正常接入不用 import。

一个 Toolkit 一个 config；同一进程可以建多个 Toolkit（比如两套测试各一个），
互不共享状态。
"""

from __future__ import annotations

from pathlib import Path

from .config import Config, PackageSpec, package_of
from .coverage_report import CoverageService
from .history import HistoryStore
from .inventory import build_payload, build_static_page
from .runner import RunManager
from .scan import Scanner


class Toolkit:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.scanner = Scanner(config)
        self.history = HistoryStore(config)
        self.coverage = CoverageService(config)
        self.runner = RunManager(config, self.scanner, self.history)

    # -- 便捷方法 ------------------------------------------------------
    def resolve_package(self, name) -> PackageSpec:
        return self.runner.resolve_package(name)

    def package_of_path(self, path: str) -> PackageSpec | None:
        return package_of(self.config.packages, path)

    def scan(self, packages=None) -> dict:
        """扫一遍脚本，返回 {scripts, total, changed, cached, scanned_at}。"""
        return self.scanner.scan(packages)

    def build_payload(self, packages=None) -> dict:
        return build_payload(self.scanner, packages)

    def build_static_page(
        self, output: Path | None = None, *, single: bool | None = None
    ) -> list[Path]:
        return build_static_page(self.scanner, output, single=single)

    def serve(self) -> int:
        """起面板并阻塞到 Ctrl+C，返回进程退出码。

        延迟 import：只用 scan / page 的宿主不必加载 HTTP 层。
        """
        from . import server

        return server.run(self)
