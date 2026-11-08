"""配置模型与校验：插件接入的唯一定义点。

设计见 README.md 的「嵌入到代码里」。要点：

- **插件本身不带任何包定义**：包列表（PackageSpec）必须由宿主给出，缺了就报错；
- **其余字段都有与项目无关的默认值**：数据与生成物都放 `<repo>/testlens.data/`
  （静态页在 `testlens.data/page/`，文件名 `testlens.html`），端口 8787 …；
- 覆盖优先级：显式参数 > 环境变量（TESTLENS_HOST / TESTLENS_PORT / TESTLENS_DATA_DIR）
  > TOML 文件（经 load_config 读入）> 默认值；
- 校验在 build_config 里一次做完（fail fast），构造出的 Config 不可变。

依赖：本模块只用标准库，TOML 解析用 3.11+ 自带的 tomllib；运行期唯一的必需依赖是
pytest>=8（由 pyproject 声明、随包安装），保证面板一起来就能跑测试。
"""

from __future__ import annotations

import ast
import configparser
import ipaddress
import os
import re
import shlex
from dataclasses import dataclass, replace
from pathlib import Path

PACKAGE_NAME_RE = re.compile(r"^[a-z0-9-]+$")
# host 只允许本机回环：localhost、::1，以及 127.0.0.0/8 里的任意地址（127.1.1.1 也算）。
LOOPBACK_HOSTNAMES = {"localhost"}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
# 启动面板后是否自动打开浏览器/页面。默认关闭：serve/dev 常年在 IDE 终端里反复
# 重启，每次都弹一个标签页纯属打扰；想看页面用 --browser 或 [server] open_browser = true。
DEFAULT_OPEN_BROWSER = False
DEFAULT_RUN_TIMEOUT = 900.0
DEFAULT_MAX_OUTPUT_LINES = 20_000
DEFAULT_TAIL_LINES = 800
# 运行历史（<data_dir>/run-history/）最多保留多少条：每次运行一条记录 + 一份完整日志，
# 超出后从最旧的开始删，对应的 <run_id>.log 一起删。只按条数算，不看文件大小。
DEFAULT_HISTORY_MAX_ENTRIES = 50
DEFAULT_PYTEST_CMD = "uv run pytest"
DEFAULT_COVERAGE_JSON_CMD = "uv run coverage json"

# 运行时数据与生成物放这一个目录：<repo>/testlens.data/
# 带工具名前缀是有意的——`.data` 太通用，容易和别的工具撞名，也不好看懂是谁的。
DATA_DIR_NAME = "testlens.data"

# 包本体位置：内置模板 web/dashboard.* 在这里。除此之外不推导任何仓库路径——
# 宿主仓库在哪由调用方给（CLI 用配置文件所在目录），插件装到 site-packages 也不会指错。
PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class PackageSpec:
    """一套测试的定义。name/label/root 必填，其余字段决定扫描与覆盖率行为。"""

    name: str
    label: str
    root: str
    tests_dir: str = "test"
    test_glob: str = "test_*.py"
    func_prefix: str = "test_"
    # 覆盖率：source_pkg 为 None 表示该包不统计覆盖率；给了就由 build_config
    # 按项目现状推导下面四项（Makefile 的 --cov 命令 + .coveragerc 的 data_file），
    # 显式写了的那项不会被覆盖。
    source_pkg: str | None = None
    coverage_data_file: str | None = None
    coverage_config: str | None = None
    coverage_args: tuple[str, ...] = ()
    coverage_hint: str | None = None


@dataclass(frozen=True)
class Config:
    packages: tuple[PackageSpec, ...]
    repo_root: Path
    data_dir: Path
    dist_dir: Path
    template_path: Path
    static_page_name: str
    host: str
    port: int
    # 启动面板后是否自动打开页面（webbrowser）。默认关闭，想看就 [server] open_browser = true
    # 或命令行 --browser；两者都只是给「要不要弹标签页」这一件事一个开关。
    open_browser: bool
    run_timeout: float
    max_output_lines: int
    tail_lines: int
    # 运行历史保留条数（[history] max_entries）。一次运行 = 一条记录 + 一份完整日志，
    # 新记录写入时按条数截断，最旧的记录和它的 .log 一起删掉。
    history_max_entries: int
    pytest_cmd: str
    coverage_json_cmd: str
    static_page_single: bool
    # 启动面板时，若某个包的覆盖率数据文件不存在，先跑一遍带 --cov 的测试把它生成出来。
    # 默认开启；跑整套测试可能很慢，不想预热就 [coverage] warmup = false 或 --no-warm-coverage。
    coverage_warmup: bool = True
    # 配置来源（配置文件的绝对路径）。None = 没有配置文件：包定义写在代码里，
    # 或者来自 `dev <目录>` 的推断。面板和 CLI 都会显示它——否则「我明明只配了
    # 一个包，怎么出来两个」这种问题只能靠猜是读到了哪份配置。
    source: str | None = None


# ----------------------------------------------------------------------
# 查询辅助（全部显式接收 packages / repo_root，不读模块级状态）
# ----------------------------------------------------------------------
def select_packages(
    packages: tuple[PackageSpec, ...], name: str | None
) -> tuple[PackageSpec, ...]:
    if not name or name == "all":
        return tuple(packages)
    selected = tuple(pkg for pkg in packages if pkg.name == name)
    if not selected:
        options = ", ".join(["all", *(pkg.name for pkg in packages)])
        raise SystemExit(f"未知的包：{name}（可选：{options}）")
    return selected


def is_self_root(root: str) -> bool:
    """root 指向 repo_root 本身。单包项目很常见：测试就在 <repo>/test 下。"""
    return root.strip() in ("", ".", "./")


def is_loopback_host(host: str) -> bool:
    """host 是否本机回环：localhost、::1、以及 127.0.0.0/8 里的任意地址。

    不写死三个字符串：127.1.1.1、127.0.0.2 同属回环段，绑它们不会暴露到局域网。
    """
    if host in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def root_prefix(pkg: PackageSpec) -> str:
    """仓库相对路径的包前缀；包根就是项目根时为空串，拼路径不能带 "./"。"""
    return "" if is_self_root(pkg.root) else pkg.root.rstrip("/") + "/"


def package_of(packages: tuple[PackageSpec, ...], path: str) -> PackageSpec | None:
    for pkg in packages:
        if (
            is_self_root(pkg.root)
            or path == pkg.root
            or path.startswith(pkg.root + "/")
        ):
            return pkg
    return None


def source_label(config: Config, base: Path | str | None = None) -> str:
    """配置来源的展示路径（相对 base，默认当前目录）；没有配置文件时返回空串。

    面板标题旁和 CLI 启动横幅都用它：先确认「读的是哪份配置」，再谈包的数量。
    """
    if not config.source:
        return ""
    path = Path(config.source)
    anchor = Path(base if base is not None else Path.cwd())
    try:
        return path.resolve().relative_to(anchor.resolve()).as_posix() or path.name
    except ValueError:
        # 配置文件在 base 之上（比如在子目录里跑、配置在仓库根），给绝对路径更清楚
        return path.as_posix()


def test_files(pkg: PackageSpec, repo_root: Path) -> list[Path]:
    base = Path(repo_root) / pkg.root / pkg.tests_dir
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob(pkg.test_glob) if p.is_file())


def count_tests(path: Path, func_prefix: str = "test_") -> int:
    """数 test_* 函数（含类方法）。参数化用例只计一条，实际执行条数更多。"""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return 0
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith(func_prefix)
    )


# ----------------------------------------------------------------------
# 自动发现（快速上手用；插件的正式用法是显式传 PackageSpec）
# ----------------------------------------------------------------------
def detect_packages(
    repo_root: Path,
    coverage_presets: dict[str, dict] | None = None,
    parents: tuple[str, ...] = ("apps", "packages"),
) -> tuple[PackageSpec, ...]:
    """扫 <parent>/*/ 里带 test 目录的子目录，按目录名生成 PackageSpec。

    这是给宿主的可选快捷方式（不在 CLI 里自动启用）：自动发现只猜得到目录结构，
    猜不到覆盖率参数，需要的话用 coverage_presets 传一份 `{name: 字段}` 补充。
    找不到任何包返回空元组。
    """
    presets = coverage_presets or {}
    specs: list[PackageSpec] = []
    for parent in parents:
        base = Path(repo_root) / parent
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            if not (child / "test").is_dir():
                continue
            spec = PackageSpec(
                name=child.name, label=child.name, root=f"{parent}/{child.name}"
            )
            preset = presets.get(child.name)
            if preset:
                spec = replace(spec, **preset)
            specs.append(spec)
    return tuple(specs)


# ----------------------------------------------------------------------
# 覆盖率默认值推导：只给 source_pkg，其余四件套从项目现状里找
# ----------------------------------------------------------------------
COVERAGERC_NAME = ".coveragerc"
_MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:(?!=)")
_COV_ARG_RE = re.compile(r"--cov(?:=|\s+)(?P<target>[A-Za-z0-9_./-]+)")
_COV_CONFIG_RE = re.compile(r"--cov-config(?:=|\s+)(?P<path>[^\s\"']+)")


def _pyproject_data_file(path: Path) -> str | None:
    """`pyproject.toml` 里 `[tool.coverage.run].data_file`；没有就返回 None。"""
    try:
        import tomllib  # 3.11+；更老的版本跳过这种配置即可
    except ModuleNotFoundError:
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    run = data.get("tool", {}).get("coverage", {}).get("run", {})
    value = run.get("data_file") if isinstance(run, dict) else None
    return value if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class _ProjectFiles:
    """推导覆盖率时能用上的事实：覆盖率配置、Makefile、写了 data_file 的 pyproject。"""

    coveragercs: tuple[Path, ...]
    makefile: Path | None
    pyproject: Path | None


def _survey(repo_root: Path, levels: int = 6) -> _ProjectFiles:
    """从 repo_root 往上找 `.coveragerc*` / `Makefile` / `pyproject.toml`，到 `.git` 为止。

    应用级配置的 repo_root 往往是 apps/web-service，而覆盖率配置、数据文件、
    测试命令都在仓库根——所以必须往上找。各取最近的一份。
    """
    current = Path(repo_root).resolve()
    coveragercs: tuple[Path, ...] = ()
    makefile: Path | None = None
    pyprojects: list[Path] = []
    for _ in range(levels):
        if not coveragercs:
            coveragercs = tuple(
                sorted(p for p in current.glob(f"{COVERAGERC_NAME}*") if p.is_file())
            )
        if makefile is None:
            candidate = current / "Makefile"
            if candidate.is_file():
                makefile = candidate
        candidate = current / "pyproject.toml"
        if candidate.is_file():
            pyprojects.append(candidate)
        if (current / ".git").exists() or current.parent == current:
            break
        current = current.parent
    return _ProjectFiles(
        coveragercs=coveragercs,
        makefile=makefile,
        pyproject=next(
            (path for path in pyprojects if _pyproject_data_file(path) is not None),
            None,
        ),
    )


def _package_tokens(spec: PackageSpec) -> set[str]:
    """用来把包和 `.coveragerc.<后缀>` 对上：包名按连字符拆开 + 源码包名。"""
    tokens = {spec.name.lower(), *(part.lower() for part in spec.name.split("-"))}
    if spec.source_pkg:
        tokens |= {part.lower() for part in spec.source_pkg.split(".")}
    return {token for token in tokens if token}


def _pick_coveragerc(candidates: tuple[Path, ...], spec: PackageSpec) -> Path | None:
    """多份配置时按包名挑一份；挑不准就返回 None，别去读别的包的数据。

    例：仓库根同时有 `.coveragerc.web` / `.coveragerc.utils`，
    `web-service` 的 token 里有 `web` → 匹配前者。
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    tokens = _package_tokens(spec)
    matched = [
        path
        for path in candidates
        if path.name[len(COVERAGERC_NAME) :].lstrip(".").lower() in tokens
    ]
    return matched[0] if len(matched) == 1 else None


def _rc_data_file(rc: Path) -> str | None:
    """读覆盖率配置里的 `[run] data_file`；没有就返回 None（用 coverage 默认的 .coverage）。"""
    parser = configparser.RawConfigParser()  # Raw：配置文件里的 % 不是插值语法
    try:
        parser.read(rc, encoding="utf-8")
    except (OSError, configparser.Error):
        return None
    if parser.has_option("run", "data_file"):
        value = parser.get("run", "data_file").strip()
        return value or None
    return None


def _makefile_coverage(
    makefile: Path | None, source_pkg: str
) -> tuple[str, str | None] | None:
    """找哪条 make 目标用 `--cov=<source_pkg>` 跑测试。

    返回 (目标名, --cov-config 的值)。有了目标名就能生成 `make test-web` 这种
    提示，也能顺手拿到这个包真正用的覆盖率配置。
    """
    if makefile is None or not makefile.is_file():
        return None
    try:
        text = makefile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    target: str | None = None
    recipe: list[str] = []
    for line in text.splitlines():
        header = _MAKE_TARGET_RE.match(line) if not line[:1].isspace() else None
        if header:
            hit = _match_recipe(target, recipe, source_pkg)
            if hit is not None:
                return hit
            target, recipe = header.group(1), []
            continue
        if target is not None and line.startswith("\t"):
            recipe.append(line)
    # 最后一条目标没有「下一行表头」来触发检查，单独收尾
    return _match_recipe(target, recipe, source_pkg)


def _match_recipe(
    target: str | None, recipe: list[str], source_pkg: str
) -> tuple[str, str | None] | None:
    if target is None or not recipe:
        return None
    body = "\n".join(recipe)
    if not any(
        match.group("target") == source_pkg for match in _COV_ARG_RE.finditer(body)
    ):
        return None
    configured = _COV_CONFIG_RE.search(body)
    path = configured.group("path") if configured else None
    if path and "$(" in path:
        path = None  # Makefile 变量我们解不了，交给后面的按名字匹配
    return target, path


def _display_path(path: Path, base: Path) -> str:
    """尽量给相对路径（配置里是这种形式更好读），跨盘符才退回绝对路径。"""
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        try:
            return Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
        except ValueError:
            return path.resolve().as_posix()


def _coverage_data_path(
    files: _ProjectFiles, rc: Path | None, root: Path, package: PackageSpec
) -> Path:
    """覆盖率数据文件落在哪：rc 的 data_file → pyproject 的 data_file → 包目录名。

    相对路径按「配置文件所在目录」解析——coverage 自己就是按这个基准写的。
    都读不到时用包目录的最后一段做后缀（`packages/utils` → `.coverage.utils`），
    这样每个包都有独立的数据文件，不会互相覆盖。
    """
    if rc is not None:
        name = _rc_data_file(rc)
        if name:
            path = Path(name)
            return path if path.is_absolute() else rc.parent / path
    if files.pyproject is not None:
        name = _pyproject_data_file(files.pyproject)
        if name:
            path = Path(name)
            return path if path.is_absolute() else files.pyproject.parent / path

    base = (
        rc.parent
        if rc is not None
        else (files.makefile.parent if files.makefile else root)
    )
    return base / f".coverage.{_package_suffix(package)}"


def _explicit_coveragerc(spec: PackageSpec, root: Path) -> Path | None:
    """宿主显式给了覆盖率配置时，用它来定位数据文件。

    两种写法都认：`coverage_config="..."`，或 `coverage_args` 里带
    `--cov-config=...`。只有文件真的存在才算数（不存在就退回推导）。
    """
    candidates: list[str] = []
    if spec.coverage_config:
        candidates.append(spec.coverage_config)
    for arg in spec.coverage_args:
        matched = _COV_CONFIG_RE.match(arg)
        if matched:
            candidates.append(matched.group("path"))

    for value in candidates:
        path = Path(value)
        path = path if path.is_absolute() else root / path
        if path.is_file():
            return path.resolve()
    return None


def _package_suffix(spec: PackageSpec) -> str:
    """包目录的最后一段：`packages/utils` → utils；`root="."` 时退回包名。"""
    if is_self_root(spec.root):
        return spec.name
    return Path(spec.root).name or spec.name


def derive_coverage(
    spec: PackageSpec, repo_root: Path, *, pytest_cmd: str = DEFAULT_PYTEST_CMD
) -> PackageSpec:
    """只给 `source_pkg` 时，把覆盖率四件套按项目现状补齐。

    推导顺序（都只补缺的，显式写了就不动）：

    1. `coverage_config`：宿主显式写了（或 args 里带 `--cov-config=`）就用它；
       否则看 Makefile 里跑 `--cov=<source_pkg>` 的那条命令用的 `--cov-config=<路径>`；
       再没有就在 `.coveragerc*` 里按包名后缀挑一份。
       挑不准（多份都不匹配）就当作没有，宁可少统计也不读错包的数据。
    2. `coverage_data_file`：取该配置 `[run] data_file`，相对配置文件所在目录解析
       （coverage 写数据时用的就是那个基准）。没有 `.coveragerc` 就看
       `pyproject.toml` 的 `[tool.coverage.run].data_file`；都没有就用
       `.coverage.<包目录名>`（`packages/utils` → `.coverage.utils`）。
    3. `coverage_args`：`--cov-config=<rc>` + `--cov=<source_pkg>` + `--cov-report=`，
       让面板自己跑的时候也产出同样的数据。
    4. `coverage_hint`：Makefile 里命中就用 `make <目标>`，否则给一条 pytest 命令。

    `source_pkg` 为空表示这个包不统计覆盖率，原样返回。
    """
    if not spec.source_pkg:
        return spec

    root = Path(repo_root)
    files = _survey(root)
    make_hit = _makefile_coverage(files.makefile, spec.source_pkg)

    hinted_config = make_hit[1] if make_hit is not None and make_hit[1] else None
    # 优先级：宿主显式写的配置 > Makefile 指明的那份 > 按包名挑
    rc = _explicit_coveragerc(spec, root)
    if rc is None and hinted_config and files.makefile is not None:
        configured = (files.makefile.parent / hinted_config).resolve()
        if configured.is_file():
            rc = configured
    if rc is None:
        rc = _pick_coveragerc(files.coveragercs, spec)

    hint = spec.coverage_hint
    if hint is None:
        hint = (
            f"make {make_hit[0]}"
            if make_hit is not None
            else f"{pytest_cmd} {_tests_path(spec)} --cov={spec.source_pkg}"
        )

    if spec.coverage_args:
        # 命令是宿主自己写的，数据文件优先按他给的配置/参数推导，推不出来才用默认名
        data_file = spec.coverage_data_file
        if data_file is None:
            data_file = _display_path(_coverage_data_path(files, rc, root, spec), root)
        config_file = spec.coverage_config
        if config_file is None and rc is not None:
            config_file = _display_path(rc, root)
        return replace(
            spec,
            coverage_config=config_file,
            coverage_data_file=data_file,
            coverage_hint=hint,
        )

    config_file = spec.coverage_config
    if config_file is None and rc is not None:
        config_file = _display_path(rc, root)
    elif config_file is None and hinted_config and files.makefile is not None:
        # 配置文件不在了，但 Makefile 还指着它：把这个名字报出去，覆盖率页签的
        # 「配置不存在」才有指向，而不是含糊地说「还没有数据」。
        config_file = _display_path(
            (files.makefile.parent / hinted_config).resolve(), root
        )

    data_file = spec.coverage_data_file
    if data_file is None:
        data_file = _display_path(_coverage_data_path(files, rc, root, spec), root)

    args = [f"--cov={spec.source_pkg}", "--cov-report="]
    # 只有配置真的存在（或宿主自己写了）才传 --cov-config，指向不存在的文件会让
    # coverage 直接报错，连数据都不生成。
    if config_file and (rc is not None or spec.coverage_config):
        args.insert(0, f"--cov-config={config_file}")

    return replace(
        spec,
        coverage_config=config_file,
        coverage_data_file=data_file,
        coverage_args=tuple(args),
        coverage_hint=hint,
    )


def _tests_path(spec: PackageSpec) -> str:
    """测试目录在仓库里的相对路径，给「还没跑过覆盖率」的提示用。"""
    base = Path("") if is_self_root(spec.root) else Path(spec.root)
    return (base / spec.tests_dir).as_posix().rstrip("/") + "/"


# ----------------------------------------------------------------------
# 构造与校验
# ----------------------------------------------------------------------
class ConfigError(ValueError):
    """配置非法。消息面向使用者，直接可读。"""


def _env_values() -> dict:
    values: dict = {}
    if os.environ.get("TESTLENS_HOST"):
        values["host"] = os.environ["TESTLENS_HOST"]
    raw_port = os.environ.get("TESTLENS_PORT")
    if raw_port:
        # 不能直接 int()：ValueError 不是 ConfigError，会绕过 CLI 的
        # 「配置有问题」提示，变成一屏 traceback。
        try:
            values["port"] = int(raw_port)
        except ValueError:
            raise ConfigError(
                f"TESTLENS_PORT 必须是整数：{raw_port!r}"
            ) from None
    if os.environ.get("TESTLENS_DATA_DIR"):
        values["data_dir"] = Path(os.environ["TESTLENS_DATA_DIR"])
    return values


def split_command(text: str, *, windows: bool | None = None) -> list[str]:
    """把配置里的命令串拆成参数列表（`pytest_cmd` / `coverage_json_cmd`）。

    不能直接 `shlex.split`：它默认按 POSIX 规则处理，反斜杠是转义符。Windows 上
    把解释器写成绝对路径（`pytest_cmd = "C:\\venv\\Scripts\\python.exe -m
    pytest"`，最自然也最常见的写法）会被拆成 `C:venvScriptspython.exe`，
    子进程直接 FileNotFoundError。

    `windows` 只给测试用：默认按当前平台判断，显式传值才能在任何平台上把两种
    规则各测一遍（否则 CI 在 Linux 上永远覆盖不到 Windows 分支）。
    """
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return _split_windows_command(text)
    return shlex.split(text)


def _split_windows_command(text: str) -> list[str]:
    """Windows 语义：反斜杠是普通字符，引号只用于分组。

    `shlex.split(posix=False)` 会把反斜杠原样保留，但成对的引号也留着——
    路径带空格时用户会写成 `"C:\\Program Files\\...\\python.exe"`，
    这里再剥一层。
    """
    return [_unquote(token) for token in shlex.split(text, posix=False)]


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def build_config(
    packages: tuple[PackageSpec, ...],
    repo_root: Path | str | None = None,
    *,
    data_dir: Path | str | None = None,
    dist_dir: Path | str | None = None,
    template_path: Path | str | None = None,
    static_page_name: str | None = None,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool | None = None,
    run_timeout: float | None = None,
    max_output_lines: int | None = None,
    tail_lines: int | None = None,
    history_max_entries: int | None = None,
    pytest_cmd: str | None = None,
    coverage_json_cmd: str | None = None,
    static_page_single: bool | None = None,
    coverage_warmup: bool | None = None,
    apply_env: bool = True,
    file_data: dict | None = None,
) -> Config:
    """构造并校验 Config。

    覆盖顺序（后者胜）：默认值 < file_data（TOML 读出的字典）<
    环境变量 < 显式参数。packages 必填（插件不带任何包定义）；repo_root 缺省
    取当前工作目录，相对路径的 data_dir / dist_dir / template_path 按它解析。
    """
    if not packages:
        raise ConfigError(
            "没有配置任何测试包。插件不自带包定义，请在 testlens.toml 里写 [[package]]，"
            "或调用 build_config(packages=(PackageSpec(...),))。"
        )

    file_values = dict(file_data or {})
    # 显式参数按名字列出来：用 locals() 取会在参数改名时静默失效
    explicit = {
        "data_dir": data_dir,
        "dist_dir": dist_dir,
        "template_path": template_path,
        "static_page_name": static_page_name,
        "host": host,
        "port": port,
        "open_browser": open_browser,
        "run_timeout": run_timeout,
        "max_output_lines": max_output_lines,
        "tail_lines": tail_lines,
        "history_max_entries": history_max_entries,
        "pytest_cmd": pytest_cmd,
        "coverage_json_cmd": coverage_json_cmd,
        "static_page_single": static_page_single,
        "coverage_warmup": coverage_warmup,
    }

    def pick(key: str, default):
        # 优先级：显式参数 > 环境变量 > 文件 > 默认值
        if explicit[key] is not None:
            return explicit[key]
        if key in env_values:
            return env_values[key]
        if file_values.get(key) is not None:
            return file_values[key]
        return default

    # repo_root 不进 explicit 字典：它决定其它路径的默认值，必须先算出来
    chosen_root = repo_root if repo_root is not None else file_values.get("repo_root")
    root = (
        Path(chosen_root if chosen_root is not None else Path.cwd())
        .expanduser()
        .resolve()
    )

    def resolve_path(value) -> Path:
        """相对路径按 repo_root 解析：配置里写 "testlens.data" 不跟 shell 的 cwd 跑。"""
        path = Path(value).expanduser()
        return path if path.is_absolute() else (root / path).resolve()

    env_values = _env_values() if apply_env else {}
    merged: dict = {}
    defaults = {
        "data_dir": root / DATA_DIR_NAME,
        # 静态页是生成物（html + css + js 三份），收在数据目录下的 page/：
        # 既不散在宿主项目根，也不去占 reports/ 这种通用目录名。
        # 想放到别处（比如 CI 要归档的目录）就配 [page] dir。
        "dist_dir": root / DATA_DIR_NAME / "page",
        "template_path": PACKAGE_DIR / "web" / "dashboard.html",
        "static_page_name": "testlens.html",
        "host": DEFAULT_HOST,
        "port": DEFAULT_PORT,
        "open_browser": DEFAULT_OPEN_BROWSER,
        "run_timeout": DEFAULT_RUN_TIMEOUT,
        "max_output_lines": DEFAULT_MAX_OUTPUT_LINES,
        "tail_lines": DEFAULT_TAIL_LINES,
        "history_max_entries": DEFAULT_HISTORY_MAX_ENTRIES,
        "pytest_cmd": DEFAULT_PYTEST_CMD,
        "coverage_json_cmd": DEFAULT_COVERAGE_JSON_CMD,
        "static_page_single": False,
        "coverage_warmup": True,
    }
    for key, default in defaults.items():
        merged[key] = pick(key, default)

    # 覆盖率四件套：宿主只写 source_pkg 时，按项目里的 Makefile / .coveragerc 补齐
    packages = tuple(
        derive_coverage(spec, root, pytest_cmd=str(merged["pytest_cmd"]))
        for spec in packages
    )

    config = Config(
        packages=packages,
        repo_root=root,
        data_dir=resolve_path(merged["data_dir"]),
        dist_dir=resolve_path(merged["dist_dir"]),
        template_path=resolve_path(merged["template_path"]),
        static_page_name=str(merged["static_page_name"]),
        host=str(merged["host"]),
        port=int(merged["port"]),
        open_browser=bool(merged["open_browser"]),
        run_timeout=float(merged["run_timeout"]),
        max_output_lines=int(merged["max_output_lines"]),
        tail_lines=int(merged["tail_lines"]),
        history_max_entries=int(merged["history_max_entries"]),
        pytest_cmd=str(merged["pytest_cmd"]),
        coverage_json_cmd=str(merged["coverage_json_cmd"]),
        static_page_single=bool(merged["static_page_single"]),
        coverage_warmup=bool(merged["coverage_warmup"]),
    )
    _validate(config)
    return config


def _validate(config: Config, *, warn: bool = True) -> None:
    problems: list[str] = []

    if not config.packages:
        problems.append("packages 不能为空")
    if not is_loopback_host(config.host):
        problems.append(
            f"host 只允许回环地址（localhost、::1、127.0.0.0/8），收到 {config.host!r}"
        )
    if not 1 <= config.port <= 65535:
        problems.append(f"port 超出范围：{config.port}")
    if config.run_timeout <= 0:
        problems.append(f"run_timeout 必须为正数：{config.run_timeout}")
    if not 1 <= config.max_output_lines <= 500_000:
        problems.append(f"max_output_lines 超出 [1, 500000]：{config.max_output_lines}")
    if not 1 <= config.tail_lines <= config.max_output_lines:
        problems.append(f"tail_lines 超出 [1, max_output_lines]：{config.tail_lines}")
    if config.history_max_entries < 1:
        problems.append(f"history_max_entries 至少为 1：{config.history_max_entries}")
    if not config.static_page_name.endswith(".html"):
        problems.append(f"static_page_name 必须是 .html：{config.static_page_name!r}")

    names: list[str] = []
    roots: list[str] = []
    repo = config.repo_root.resolve()
    for spec in config.packages:
        if not PACKAGE_NAME_RE.match(spec.name):
            problems.append(f"name 只允许小写字母/数字/连字符：{spec.name!r}")
        if not spec.label.strip():
            problems.append(f"label 不能为空：{spec.name!r}")
        if is_self_root(spec.root):
            # 包根就是 repo_root：只能有一个包，否则所有路径都归它，别的包永远匹配不到
            if len(config.packages) > 1:
                problems.append(
                    f"包 {spec.name!r} 的 root 是 '.'（项目根），此时不能再定义别的包"
                )
            roots.append(".")
            names.append(spec.name)
            continue
        if (
            "\\" in spec.root
            or spec.root.startswith("/")
            or ".." in Path(spec.root).parts
        ):
            problems.append(f"root 必须是相对 repo_root 的规范路径：{spec.root!r}")
            continue
        if spec.root in roots:
            problems.append(f"root 重复：{spec.root}")
        for other in roots:
            if spec.root.startswith(other + "/") or other.startswith(spec.root + "/"):
                problems.append(f"root 互相嵌套：{spec.root} 与 {other}")
        roots.append(spec.root)
        names.append(spec.name)

        resolved_root = (repo / spec.root).resolve()
        try:
            resolved_root.relative_to(repo)
        except ValueError:
            problems.append(f"root 越出 repo_root：{spec.root}")
        else:
            if warn and not resolved_root.is_dir():
                # 允许先建配置后建目录，但要点名，免得扫出 0 个脚本还不知道为什么
                print(f"  警告：包 {spec.name} 的目录不存在：{resolved_root}")

    if len(set(names)) != len(names):
        problems.append(f"name 重复：{names}")

    if problems:
        raise ConfigError("配置非法：\n  - " + "\n  - ".join(problems))


def override_config(config: Config, **changes) -> Config:
    """在已有 Config 上改几个字段并**重新校验**。

    `dataclasses.replace` 只换字段、不跑 `_validate`：CLI 的 `--port` 用它改端口
    时，`--port 0` 会绑到系统分配的随机端口，却把 `http://127.0.0.1:0/` 打给
    用户；`--port 70000` 也要等到 bind 失败才报错，还建议 `--port 70001`。
    命令行开关一律走这里，行为与写在配置文件里一致。
    """
    updated = replace(config, **changes)
    # warn=False：目录不存在的告警在 build_config 时已经打过一次，
    # 这里只是改端口/开关，不该再刷一遍。
    _validate(updated, warn=False)
    return updated


# ----------------------------------------------------------------------
# TOML 配置文件（可选）
# ----------------------------------------------------------------------
def load_config(path: Path | str, **overrides) -> Config:
    """从 TOML 读配置，再套用 overrides（显式参数优先级最高）。

    文件结构：

        [paths]    repo_root / data_dir / dist_dir / template_path
        [page]     dir（静态页目录）/ name（文件名）/ single（是否合成单文件）
        [server]   host / port / open_browser（启动后自动开页面，默认 false）
                   / run_timeout / max_output_lines / tail_lines
        [history]  max_entries（运行历史保留条数，默认 50；超出后删最旧的记录和日志）
        [commands] pytest_cmd / coverage_json_cmd
        [coverage] warmup（启动时补生成缺失的覆盖率数据，默认 true）
        [[package]] name / label / root / ... （数组，每项一个包，必填）

    repo_root 优先级：显式参数 > [paths] repo_root > 配置文件所在目录；
    相对路径按配置文件所在目录解析。文件里的 package 列表可以整体被
    overrides["packages"] 取代。
    """
    import tomllib  # 3.11+；只有用文件配置的宿主才需要这个版本

    file_path = Path(path)
    if not file_path.is_file():
        raise ConfigError(f"配置文件不存在：{file_path}")
    data = tomllib.loads(file_path.read_text(encoding="utf-8"))

    paths = data.get("paths", {})
    server_section = data.get("server", {})
    page = data.get("page", {})
    history_section = data.get("history", {})
    commands = data.get("commands", {})
    coverage_section = data.get("coverage", {})

    overrides = dict(overrides)
    packages: tuple[PackageSpec, ...] | None = overrides.pop("packages", None)
    if packages is None and "package" in data:
        specs: list[PackageSpec] = []
        for item in data["package"]:
            item = dict(item)
            args = item.pop("coverage_args", [])
            known = PackageSpec.__dataclass_fields__.keys()
            specs.append(
                PackageSpec(
                    coverage_args=tuple(args),
                    **{k: v for k, v in item.items() if k in known},
                )
            )
        packages = tuple(specs)

    file_data = {
        "repo_root": paths.get("repo_root"),
        "data_dir": paths.get("data_dir"),
        # [page] 是推荐写法；[paths] 里的旧键继续认
        "dist_dir": page.get("dir", paths.get("dist_dir")),
        "template_path": paths.get("template_path"),
        "static_page_name": page.get("name", paths.get("static_page_name")),
        "static_page_single": page.get("single"),
        "host": server_section.get("host"),
        "port": server_section.get("port"),
        "open_browser": server_section.get("open_browser"),
        "run_timeout": server_section.get("run_timeout"),
        "max_output_lines": server_section.get("max_output_lines"),
        "tail_lines": server_section.get("tail_lines"),
        "history_max_entries": history_section.get("max_entries"),
        "pytest_cmd": commands.get("pytest_cmd"),
        "coverage_json_cmd": commands.get("coverage_json_cmd"),
        "coverage_warmup": coverage_section.get("warmup"),
    }
    overrides.setdefault("apply_env", True)
    # repo_root 优先级：调用方显式传的 > [paths] repo_root > 配置文件所在目录。
    # 相对路径按配置文件所在目录解析，和其余路径字段保持一致。
    explicit_root = overrides.pop("repo_root", None)
    configured_root = paths.get("repo_root")
    if explicit_root is not None:
        root = explicit_root
    elif configured_root:
        candidate = Path(configured_root).expanduser()
        root = candidate if candidate.is_absolute() else file_path.resolve().parent / candidate
    else:
        root = file_path.resolve().parent
    config = build_config(
        packages=packages if packages is not None else (),
        repo_root=root,
        file_data=file_data,
        **overrides,
    )
    # 记下来源：面板标题与 CLI 横幅要显示它，用户才能确认读到的是哪份配置
    return replace(config, source=str(file_path.resolve()))
