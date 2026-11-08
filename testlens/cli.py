#!/usr/bin/env python3
"""命令行入口：`python -m testlens <command>`。

    list     有哪些测试脚本、各有多少用例
    changed  哪些脚本与缓存基线不一致
    plan     该跑哪些（默认；加 --run 直接跑）
    page     生成可直接双击的静态页
    serve    起本地面板（读当前目录的 toml / py 配置）
    dev      按目录起面板：优先读目录里的配置，没有才推断（像 `fastapi dev <app>`）；
             给 .py 时先用模块里的 config，没有就按它所在目录起面板

配置来自宿主项目的 `testlens.toml` 或 `testlens_config.py`（也可以用 --config 指定）。插件**不带任何
包定义**：找不到配置、或配置里没有 `[[package]]`，直接报错退出并给出样例。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import types
from dataclasses import replace
from pathlib import Path

from . import __version__
from .config import (
    DATA_DIR_NAME,
    Config,
    ConfigError,
    PackageSpec,
    build_config,
    count_tests,
    load_config,
    override_config,
    package_of,
    select_packages,
    split_command,
    test_files,
)
from .toolkit import Toolkit


# ----------------------------------------------------------------------
# 配置：从 host 项目的 testlens.toml 读
# ----------------------------------------------------------------------
CONFIG_FILENAME = "testlens.toml"
# 同一目录里也可以只放 Python 配置：覆盖率这类 toml 里不方便表达的字段用它
# （形如 apps/web-service/testlens_config.py）。两者都在时 toml 优先。
CONFIG_MODULE = "testlens_config.py"
CONFIG_CANDIDATES = (CONFIG_FILENAME, CONFIG_MODULE)
CONFIG_ENV = "TESTLENS_CONFIG"

SAMPLE_CONFIG = """# testlens 的宿主配置。插件本身不带任何包定义，这里必须写 [[package]]。

[page]
dir = "reports"            # 静态页目录，相对仓库根；省略就放 testlens.data/page
name = "testlens.html"     # 文件名
single = false             # true = 合成一个自包含的 HTML

[server]
port = 8787
open_browser = false       # 启动后自动打开页面，默认关闭（--browser 可临时打开）

[history]
max_entries = 50           # 运行历史最多留几条：一次运行一条（记录 + 日志），超出删最旧的

[commands]
pytest_cmd = "uv run pytest"
coverage_json_cmd = "uv run coverage json"

[coverage]
warmup = true              # 启动时数据文件不存在，就先跑一遍带 --cov 的测试

[[package]]
name = "web-service"                 # 唯一 id，只能小写字母/数字/连字符
label = "Web Service"                # 界面上的包名
root = "apps/web-service"            # 相对仓库根
source_pkg = "app"                   # 只写这一项：覆盖率四件套自动推导
"""


def find_config(explicit: str | None = None) -> Path:
    """定位配置文件：显式参数 > TESTLENS_CONFIG > 当前目录的 toml / py 配置。

    两个文件名按 toml 优先：声明式配置能表达的东西更全，Python 配置是给
    「要算路径、要写覆盖率参数」的场景兜底的。
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"配置文件不存在：{path}")
        return path
    if os.environ.get(CONFIG_ENV):
        path = Path(os.environ[CONFIG_ENV]).expanduser()
        if not path.is_file():
            raise ConfigError(f"{CONFIG_ENV} 指向的配置文件不存在：{path}")
        return path

    for name in CONFIG_CANDIDATES:
        path = Path.cwd() / name
        if path.is_file():
            return path

    raise ConfigError(
        f"当前目录没有 {CONFIG_FILENAME} 或 {CONFIG_MODULE}，插件不自带包定义。\n"
        f"  指向已有的配置：testlens serve --config path/to/{CONFIG_MODULE}\n"
        f"  或者直接给应用目录：testlens dev apps/web-service\n"
        f"  都没有就建一个：\n\n{SAMPLE_CONFIG}"
    )


def load_cli_config(config_path: str | None = None) -> Config:
    """CLI 的配置入口：`.toml` 走声明式，`.py` 取模块里的 config/toolkit。

    `--config` 两种都收，这样只维护应用自己那份 Python 配置也能跑全部子命令
    （list / plan / serve / page），不必在仓库根再写一份 toml。
    """
    path = find_config(config_path)
    if path.suffix == ".toml":
        return load_config(path)
    if path.suffix == ".py":
        return config_from_module(path)
    raise ConfigError(f"配置文件只支持 .toml 或 .py：{path}")


# 推断包定义时不当成源码包的目录：测试目录本身、虚拟环境、构建产物
_SKIP_PACKAGE_DIRS = {
    "test",
    "tests",
    "build",
    "dist",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
}


def infer_source_pkg(path: Path) -> str | None:
    """目录下**恰好一个**顶层 Python 包（带 __init__.py）时，用它当 source_pkg。

    0 个或多个都不猜：monorepo 里猜错会把别的包算进覆盖率，比不统计更糟。
    """
    try:
        candidates = [
            child.name
            for child in path.iterdir()
            if child.is_dir()
            and child.name not in _SKIP_PACKAGE_DIRS
            and not child.name.startswith((".", "_"))
            and (child / "__init__.py").is_file()
        ]
    except OSError:
        return None
    return candidates[0] if len(candidates) == 1 else None


def infer_spec(path: Path) -> PackageSpec:
    """把一个目录当成一套测试：root='.'、测试在 <dir>/test，名字由目录名推出来。

    覆盖率也顺手推一下：只有一个顶层包时当 source_pkg（数据文件/参数照常按
    pyproject、.coveragerc 推导）；多个包就不猜，覆盖率页签会提示未配置。
    """
    raw = path.name or "app"
    name = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-") or "app"
    return PackageSpec(name=name, label=raw, root=".", source_pkg=infer_source_pkg(path))


def try_config_from_module(path: Path) -> Config | None:
    """把 .py 当配置文件加载；模块里没有 config/toolkit 时返回 None。

    导入失败仍然抛 ConfigError——那是脚本写错了，不能悄悄退回目录推断。
    """
    module_name = f"_testlens_target_{path.stem}"
    # 让模块里的 `import app.xxx` 能找到：把它的目录和当前目录放进 sys.path
    for extra in (path.parent, Path.cwd()):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))

    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    sys.modules[module_name] = module
    try:
        # 直接编译源码、刻意不走 .pyc：配置文件改一行就该立刻生效。
        # 走字节码缓存时，若源码 mtime（秒级）和大小恰好没变，会继续执行旧版本，
        # 表现就是「我明明改了 port，启动还是老端口」。
        source = path.read_text(encoding="utf-8")
        exec(compile(source, str(path), "exec"), module.__dict__)
    except Exception as error:  # noqa: BLE001 - 用户的模块，报清楚原因即可
        sys.modules.pop(module_name, None)
        raise ConfigError(
            f"加载 {path.name} 失败：{type(error).__name__}: {error}"
        ) from error

    for name in ("config", "toolkit"):
        value = getattr(module, name, None)
        if isinstance(value, Config):
            # 记下来源，面板标题与启动横幅都会显示，用来确认读的是哪份配置
            return replace(value, source=str(path.resolve()))
        if isinstance(value, Toolkit):
            return replace(value.config, source=str(path.resolve()))
    return None


def config_from_module(path: Path) -> Config:
    """严格版：模块里必须有 config（或 toolkit），给 `--config` 用。"""
    config = try_config_from_module(path)
    if config is not None:
        return config
    raise ConfigError(
        f"{path.name} 里没有找到 config（或 toolkit），不能当配置文件用。\n"
        "  想把它当配置：在模块里写 config = build_config(...)，别在导入时启动服务\n"
        f"  想运行这个脚本：到 {path.parent} 下执行 python {path.name}\n"
        f"  想给它所在目录起面板：testlens dev \"{path.parent}\""
    )


def config_from_dir(path: Path) -> Config:
    """目录入口：优先读目录里的 testlens.toml / testlens_config.py，没有才推断。"""
    config_file = next(
        (path / name for name in CONFIG_CANDIDATES if (path / name).is_file()), None
    )
    if config_file is not None:
        # 目录自带配置就用它：覆盖率参数只有宿主自己知道
        return load_cli_config(str(config_file))
    # 静态页的默认位置（testlens.data/page）由 build_config 给，这里不用再管
    return build_config(packages=(infer_spec(path),), repo_root=path)


def nearest_config_dir(start: Path, levels: int = 6) -> Path | None:
    """从 start 往上找最近的带配置目录（到 .git 或 6 层为止）。

    给 `dev test/test_cart.py` 这种指到子目录里文件的场景用：配置通常在上层项目根。
    """
    current = Path(start).resolve()
    for _ in range(levels):
        if any((current / name).is_file() for name in CONFIG_CANDIDATES):
            return current
        if (current / ".git").exists() or current.parent == current:
            return None
        current = current.parent
    return None


def config_for_path(
    target: str | None,
    config_path: str | None,
    *,
    port: int | None = None,
    open_browser: bool | None = None,
) -> Config:
    """`dev TARGET` 的配置。TARGET 可以是目录，也可以是 .py。

    - `.py` 里有 `config`（或 `toolkit`）：当成配置文件用它
    - `.py` 里没有：当成「项目入口脚本」，从它所在目录**往上找**最近的配置目录
      （到 .git 或 6 层为止），找不到才按该目录推断。这样
      `testlens dev examples/demo/plugin_embed.py` 与 `testlens dev examples/demo` 等价，
      跟 `fastapi dev app/main.py` 的直觉一致
    - 目录：优先用它自己的 `testlens.toml`，其次 `testlens_config.py`，
      都没有才按目录推断（一个包、root='.'、测试在 test/）
    """
    if config_path:
        base = load_cli_config(config_path)
    else:
        path = Path(target or ".").expanduser().resolve()
        if path.is_file():
            if path.suffix != ".py":
                raise ConfigError(f"只接受目录或 .py 文件：{path}")
            module_config = try_config_from_module(path)
            if module_config is not None:
                base = module_config
            else:
                # 当成「项目入口脚本」：配置通常在它上层，找不到才按该目录推断
                target_dir = nearest_config_dir(path.parent) or path.parent
                print(f"  {path.name} 不是配置文件，按 {target_dir} 起面板")
                base = config_from_dir(target_dir)
        elif path.is_dir():
            base = config_from_dir(path)
        else:
            raise ConfigError(f"路径不存在：{path}")

    overrides = {}
    if port is not None:
        overrides["port"] = port
    if open_browser is not None:
        overrides["open_browser"] = open_browser
    # 走 override_config 而不是 replace：命令行端口也要过 1-65535 的校验
    return override_config(base, **overrides) if overrides else base


SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    DATA_DIR_NAME,
    ".data",  # 旧版目录名，迁移期间也可能存在
    "htmlcov",
    "dist",
}


def discover_configs(root: Path, max_depth: int = 3) -> list[Path]:
    """在目录下找可用的配置文件。只在「找不到配置」的提示里用，不参与主流程。"""
    root = root.resolve()
    found: list[Path] = []
    for current, dirs, files in os.walk(root):
        depth = len(Path(current).relative_to(root).parts)
        if depth >= max_depth:
            dirs[:] = []
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        found.extend(Path(current) / name for name in CONFIG_CANDIDATES if name in files)
    return found


def missing_config_hint(config: Config) -> str:
    """dev 按目录推断出 0 个脚本时的提示：说清这里缺什么、该指向哪。"""
    root = config.repo_root
    first = config.packages[0]
    lines = [
        f"这里没有可用的测试配置：{root}",
        f"  既没有 {CONFIG_FILENAME} / {CONFIG_MODULE}，"
        f"推断出的 {first.tests_dir}/ 下也没有 {first.test_glob}。",
        "  指向已有的配置再跑：",
        "    testlens dev apps/web-service",
        f"    testlens dev apps/web-service/{CONFIG_MODULE}",
    ]
    found = discover_configs(root)
    if found:
        lines.append("  在这个目录下找到这些配置：")
        for path in found:
            try:
                shown = path.relative_to(Path.cwd()).as_posix()
            except ValueError:
                shown = str(path)
            lines.append(f"    {shown}")
    return "\n".join(lines)


def _describe(item: dict) -> str:
    if item["status"] == "new":
        return "新增（缓存里没有）"
    if item["status"] == "missing":
        return "已删除（缓存里还有）"
    diff = item.get("diff")
    if diff:
        return f"内容已修改（+{diff['added']} -{diff['removed']} 行）"
    return "内容已修改"


# ----------------------------------------------------------------------
# list
# ----------------------------------------------------------------------
def cmd_list(toolkit: Toolkit, packages: tuple[PackageSpec, ...]) -> int:
    config = toolkit.config
    total_files = 0
    total_tests = 0

    for pkg in packages:
        files = test_files(pkg, config.repo_root)
        if not files:
            continue
        rows = [(path, count_tests(path, pkg.func_prefix)) for path in files]
        pkg_tests = sum(count for _, count in rows)
        total_files += len(rows)
        total_tests += pkg_tests

        print(f"\n  {pkg.label}  ({pkg.root})")
        print(f"  {len(rows)} 个文件, {pkg_tests} 个测试函数")
        for path, count in rows:
            rel = path.relative_to(config.repo_root / pkg.root).as_posix()
            print(f"    {rel:<48} {count:>3}")

    print(f"\n合计：{len(packages)} 个包，{total_files} 个脚本文件，{total_tests} 个测试函数")
    print("  （参数化的用例会展开成多条，实际执行条数比这个数字多）")
    return 0


# ----------------------------------------------------------------------
# changed
# ----------------------------------------------------------------------
def cmd_changed(toolkit: Toolkit, packages: tuple[PackageSpec, ...]) -> int:
    result = toolkit.scan(packages)
    changed = [item for item in result["scripts"] if item["changed"]]
    if not changed:
        print("\n没有检测到测试脚本变化。")
        print(f"  缓存基线：{result['cached']} 个脚本（{toolkit.scanner.cache_file}）")
        return 0

    print(f"\n相对缓存基线，有 {len(changed)} 个测试脚本变化：\n")
    for pkg in packages:
        items = [item for item in changed if item["package"] == pkg.name]
        if not items:
            continue
        print(f"  {pkg.label}  ({len(items)})")
        for item in items:
            print(f"    {item['path']}")
            print(f"        · {_describe(item)}")
        print()
    return 0


# ----------------------------------------------------------------------
# plan
# ----------------------------------------------------------------------
def cmd_plan(toolkit: Toolkit, packages: tuple[PackageSpec, ...], *, run: bool = False) -> int:
    result = toolkit.scan(packages)
    changed = [item for item in result["scripts"] if item["changed"]]
    if not changed:
        print("\n没有检测到测试脚本变化，不需要重跑。")
        print(f"  缓存基线：{result['cached']} 个脚本")
        print("  这是变更检测的边界：它只对比测试脚本自身。")
        print("  业务源码改过的话它不知道，那种情况在面板里点「全部执行」。")
        return 0

    print(f"\n检测到 {len(changed)} 个测试脚本有变化：\n")
    for item in changed:
        print(f"  {item['path']}")
        print(f"      · {_describe(item)}")

    repo_root = toolkit.config.repo_root
    runnable = [
        repo_root / item["path"] for item in changed if item["status"] != "missing"
    ]
    return _report(toolkit, runnable, run=run, title="建议运行")


def _report(toolkit: Toolkit, files: list[Path], *, run: bool, title: str) -> int:
    config = toolkit.config
    ordered = sorted(set(files))
    total = 0
    for path in ordered:
        rel = path.relative_to(config.repo_root).as_posix()
        pkg = package_of(config.packages, rel)
        if pkg is not None:
            total += count_tests(path, pkg.func_prefix)
    print(f"\n{title}：{len(ordered)} 个脚本 / {total} 个测试函数\n")
    for path in ordered:
        print(f"  {path.relative_to(config.repo_root).as_posix()}")

    if run:
        print("\n开始运行…\n")
        return _run(config, ordered)
    print("\n直接跑：python -m testlens plan --run")
    return 0


def _run(config: Config, files: list[Path]) -> int:
    """按包分组跑。不同包的 pytest 配置（覆盖率、asyncio 模式）不一样，不能混。"""
    grouped: dict[str, list[str]] = {}
    for path in files:
        rel = path.relative_to(config.repo_root).as_posix()
        pkg = package_of(config.packages, rel)
        if pkg is not None:
            grouped.setdefault(pkg.name, []).append(rel)

    order = {pkg.name: index for index, pkg in enumerate(config.packages)}
    failed = False
    sys.stdout.flush()
    for name in sorted(grouped, key=lambda name: order.get(name, len(order))):
        completed = subprocess.run(
            [*split_command(config.pytest_cmd), *grouped[name], "-q"],
            cwd=config.repo_root,
            check=False,
        )
        failed = failed or completed.returncode != 0

    return 1 if failed else 0


# ----------------------------------------------------------------------
# page
# ----------------------------------------------------------------------
def cmd_page(toolkit: Toolkit, output: str | None, single: bool | None = None) -> int:
    target = Path(output) if output else None
    # 只扫一遍：页面内容与下面两行统计共用同一份 payload。
    # （首次打开时 build_payload 会写一次基线，多调一次就是多扫一遍 + 多写一次。）
    payload = toolkit.build_payload()
    files = toolkit.build_static_page(target, single=single, payload=payload)
    for path in files:
        print(f"已生成 {path}  {path.stat().st_size / 1024:.0f} KB")
    print(f"  {payload['total']} 个脚本，{payload['changed']} 个已更新")
    source = toolkit.config.source
    print(f"  想在页面上直接运行：testlens serve --config \"{source}\"" if source else "  想在页面上直接运行：testlens serve")
    return 0


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="testlens",
        description="测试脚本清单、变更检测、面板与覆盖率",
    )
    config_help = (
        f"配置文件路径：.toml 或 .py（默认在当前目录找 {CONFIG_FILENAME} / "
        f"{CONFIG_MODULE}，toml 优先）"
    )
    parser.add_argument("--config", default=None, metavar="PATH", help=config_help)
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    def add_config_arg(target: argparse.ArgumentParser) -> None:
        """子命令里也认 --config（`testlens plan --config x.py`）。

        default=SUPPRESS 是关键：不这么写，子解析器的默认 None 会把顶层
        `testlens --config x.py plan` 已经读到的值覆盖掉。
        """
        target.add_argument(
            "--config", default=argparse.SUPPRESS, metavar="PATH", help=config_help
        )

    def add_warm_coverage_arg(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--warm-coverage",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="启动前先生成缺失的覆盖率数据文件（覆盖配置里的 [coverage] warmup）",
        )

    def add_browser_arg(target: argparse.ArgumentParser) -> None:
        """是否自动打开页面：配置项 [server] open_browser，默认关闭。

        用 BooleanOptionalAction 一次给出 --browser / --no-browser：默认值 None
        表示「听配置的」，两个开关只覆盖这一次运行，不写回配置。
        """
        target.add_argument(
            "--browser",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="启动后自动打开页面（默认关闭；等价于配置 [server] open_browser）",
        )

    def add_package_arg(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--package",
            default="all",
            help="只处理一套：按 name 选（默认 all）",
        )

    p_list = sub.add_parser("list", help="列出全部脚本与各自的测试函数数")
    add_package_arg(p_list)
    add_config_arg(p_list)

    p_changed = sub.add_parser("changed", help="哪些脚本与缓存基线不一致")
    add_package_arg(p_changed)
    add_config_arg(p_changed)

    p_plan = sub.add_parser("plan", help="该跑哪些")
    add_package_arg(p_plan)
    add_config_arg(p_plan)
    p_plan.add_argument("--run", action="store_true", help="列出后直接跑")

    p_page = sub.add_parser("page", help="生成静态页")
    add_config_arg(p_page)
    p_page.add_argument("--output", default=None, help="自定义输出路径")
    p_page.add_argument(
        "--single",
        action="store_true",
        default=None,
        help="合成一个自包含的 HTML（默认拆成 html + css + js）",
    )

    p_serve = sub.add_parser(
        "serve", help="起本地测试面板（读当前目录的 testlens.toml / testlens_config.py）"
    )
    add_config_arg(p_serve)
    p_serve.add_argument("--port", type=int, default=None)
    add_browser_arg(p_serve)
    add_warm_coverage_arg(p_serve)

    p_dev = sub.add_parser(
        "dev",
        help="起面板：给目录先读目录里的配置、没有才推断；给 .py 则读里面的 config，没有就按它所在目录",
    )
    add_config_arg(p_dev)
    p_dev.add_argument(
        "path",
        nargs="?",
        default=".",
        help=(
            "目录（先用目录里的 testlens.toml / testlens_config.py，都没有才推断）"
            "或 .py 文件（模块里有 config 就用它；没有就按它所在目录起面板）"
        ),
    )
    p_dev.add_argument("--port", type=int, default=None)
    add_browser_arg(p_dev)
    add_warm_coverage_arg(p_dev)

    return parser


def config_for_args(args) -> Config:
    """子命令 -> 最终 Config：先定位配置，再套本次命令行开关。

    `--port` / `--browser` / `--warm-coverage` 都只影响这一次运行，但仍然要
    过 `_validate`：`serve --port 0` 曾被 dataclasses.replace 放过，最后打印
    `http://127.0.0.1:0/` 这种打不开的地址。
    """
    if args.command == "dev":
        config = config_for_path(
            args.path,
            args.config,
            port=args.port,
            open_browser=args.browser,
        )
    else:
        config = load_cli_config(args.config)

    if args.command not in ("serve", "dev"):
        return config

    overrides = {}
    if args.command == "serve":
        # dev 的 port / browser 已经由 config_for_path 处理过
        if args.port is not None:
            overrides["port"] = args.port
        # None = 没写这两个开关，沿用配置里的 open_browser（默认 False）
        if args.browser is not None:
            overrides["open_browser"] = args.browser
    if args.warm_coverage is not None:
        overrides["coverage_warmup"] = args.warm_coverage
    return override_config(config, **overrides) if overrides else config


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    try:
        config = config_for_args(args)
    except ConfigError as error:
        print(f"配置有问题：\n{error}", file=sys.stderr)
        return 2

    if args.command in ("serve", "dev"):
        toolkit = Toolkit(config)
        # dev 没找到配置文件、按目录推断又扫不到脚本：直接说清楚，别起一个
        # 「看着在跑、其实一个脚本都没有」的空面板
        if config.source is None and not toolkit.scan()["total"]:
            print(missing_config_hint(config), file=sys.stderr)
            return 2
        # 启动横幅（配置来源 / 仓库 / 包与脚本数）统一由 server.run 打印：
        # serve 与 dev 的区别只在配置怎么来，其余输出没必要各写一份
        return toolkit.serve()

    toolkit = Toolkit(config)

    if args.command == "page":
        return cmd_page(toolkit, args.output, args.single)

    packages = select_packages(config.packages, args.package)
    if args.command == "list":
        return cmd_list(toolkit, packages)
    if args.command == "changed":
        return cmd_changed(toolkit, packages)
    return cmd_plan(toolkit, packages, run=args.run)


if __name__ == "__main__":
    sys.exit(main())
