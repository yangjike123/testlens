"""配置项逐项测试：每个字段都要真的落到 Config 或具体行为上。

和 `test_units.py` 的分工：那边测流程（扫描 / 运行 / 覆盖率），这里按参数清单过一遍，
防止「文档写了、代码没接」或「接了但方向反了」。
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from testlens.config import (
    ConfigError,
    PackageSpec,
    build_config,
    count_tests,
    load_config,
    override_config,
    split_command,
)
# 别名：名字以 test_ 开头的函数被 import 进测试模块后，pytest 会当成用例收集
from testlens.config import test_files as list_test_files


def spec(**kw) -> PackageSpec:
    base = {"name": "demo", "label": "Demo", "root": "."}
    base.update(kw)
    return PackageSpec(**base)


def make(tmp_path: Path, **kw):
    (tmp_path / "test").mkdir(exist_ok=True)
    return build_config(packages=(spec(),), repo_root=tmp_path, **kw)


# ----------------------------------------------------------------------
# build_config 的每个显式参数
# ----------------------------------------------------------------------
def test_every_explicit_argument_lands_in_config(tmp_path: Path) -> None:
    config = make(
        tmp_path,
        data_dir="data",
        dist_dir="out",
        template_path="ui/mine.html",
        static_page_name="report.html",
        host="127.1.1.1",
        port=9001,
        open_browser=True,
        run_timeout=12.5,
        max_output_lines=500,
        tail_lines=50,
        history_max_entries=3,
        pytest_cmd="python -m pytest -x",
        coverage_json_cmd="coverage json",
        static_page_single=True,
        coverage_warmup=False,
    )

    assert config.data_dir == (tmp_path / "data").resolve()
    assert config.dist_dir == (tmp_path / "out").resolve()
    assert config.template_path == (tmp_path / "ui" / "mine.html").resolve()
    assert config.static_page_name == "report.html"
    assert config.host == "127.1.1.1"
    assert config.port == 9001
    assert config.open_browser is True
    assert config.run_timeout == 12.5
    assert config.max_output_lines == 500
    assert config.tail_lines == 50
    assert config.history_max_entries == 3
    assert config.pytest_cmd == "python -m pytest -x"
    assert config.coverage_json_cmd == "coverage json"
    assert config.static_page_single is True
    assert config.coverage_warmup is False


def test_absolute_paths_are_kept(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere"
    config = make(tmp_path, data_dir=outside, dist_dir=outside / "page")

    assert config.data_dir == outside.resolve()
    assert config.dist_dir == (outside / "page").resolve()


def test_paths_repo_root_is_honored(tmp_path: Path) -> None:
    """`[paths] repo_root` 要生效：相对路径按配置文件所在目录解析。"""
    project = tmp_path / "project"
    (project / "test").mkdir(parents=True)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    path = config_dir / "testlens.toml"
    path.write_text(
        '[paths]\nrepo_root = "../project"\n\n'
        '[[package]]\nname = "solo"\nlabel = "Solo"\nroot = "."\n',
        encoding="utf-8",
    )

    assert load_config(path).repo_root == project.resolve()
    # 调用方显式传的 repo_root 仍然最高
    assert load_config(path, repo_root=tmp_path).repo_root == tmp_path.resolve()


def test_defaults_match_documentation(tmp_path: Path) -> None:
    config = make(tmp_path)

    assert config.host == "127.0.0.1"
    assert config.port == 8787
    assert config.open_browser is False
    assert config.run_timeout == 900.0
    assert config.max_output_lines == 20_000
    assert config.tail_lines == 800
    assert config.history_max_entries == 50
    assert config.pytest_cmd == "uv run pytest"
    assert config.coverage_json_cmd == "uv run coverage json"
    assert config.static_page_name == "testlens.html"
    assert config.static_page_single is False
    assert config.coverage_warmup is True
    assert config.data_dir == tmp_path.resolve() / "testlens.data"
    assert config.dist_dir == tmp_path.resolve() / "testlens.data" / "page"
    assert config.source is None


# ----------------------------------------------------------------------
# TOML 每个键
# ----------------------------------------------------------------------
def test_every_toml_key_lands_in_config(tmp_path: Path) -> None:
    (tmp_path / "apps" / "api" / "cases").mkdir(parents=True)
    path = tmp_path / "testlens.toml"
    path.write_text(
        "[paths]\n"
        'data_dir = "runtime"\n'
        'template_path = "ui/mine.html"\n'
        "\n"
        "[page]\n"
        'dir = "reports"\n'
        'name = "report.html"\n'
        "single = true\n"
        "\n"
        "[server]\n"
        'host = "127.0.0.2"\n'
        "port = 9100\n"
        "open_browser = true\n"
        "run_timeout = 30\n"
        "max_output_lines = 1000\n"
        "tail_lines = 100\n"
        "\n"
        "[history]\n"
        "max_entries = 7\n"
        "\n"
        "[coverage]\n"
        "warmup = false\n"
        "\n"
        "[commands]\n"
        'pytest_cmd = "python -m pytest"\n'
        'coverage_json_cmd = "python -m coverage json"\n'
        "\n"
        "[[package]]\n"
        'name = "api"\n'
        'label = "API"\n'
        'root = "apps/api"\n'
        'tests_dir = "cases"\n'
        'test_glob = "check_*.py"\n'
        'func_prefix = "check_"\n'
        'source_pkg = "app"\n'
        'coverage_data_file = ".coverage.api"\n'
        'coverage_config = ".coveragerc.api"\n'
        'coverage_args = ["--cov=app", "--cov-report="]\n'
        'coverage_hint = "make test-api"\n',
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.data_dir == (tmp_path / "runtime").resolve()
    assert config.template_path == (tmp_path / "ui" / "mine.html").resolve()
    assert config.dist_dir == (tmp_path / "reports").resolve()
    assert config.static_page_name == "report.html"
    assert config.static_page_single is True
    assert config.host == "127.0.0.2"
    assert config.port == 9100
    assert config.open_browser is True
    assert config.run_timeout == 30.0
    assert config.max_output_lines == 1000
    assert config.tail_lines == 100
    assert config.history_max_entries == 7
    assert config.coverage_warmup is False
    assert config.pytest_cmd == "python -m pytest"
    assert config.coverage_json_cmd == "python -m coverage json"

    package = config.packages[0]
    assert package.name == "api"
    assert package.label == "API"
    assert package.root == "apps/api"
    assert package.tests_dir == "cases"
    assert package.test_glob == "check_*.py"
    assert package.func_prefix == "check_"
    assert package.source_pkg == "app"
    assert package.coverage_data_file == ".coverage.api"
    assert package.coverage_config == ".coveragerc.api"
    assert package.coverage_args == ("--cov=app", "--cov-report=")
    assert package.coverage_hint == "make test-api"


# ----------------------------------------------------------------------
# 优先级：默认 < 文件 < 环境变量 < 显式参数
# ----------------------------------------------------------------------
def test_precedence_default_file_env_explicit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESTLENS_HOST", "127.0.0.2")
    monkeypatch.setenv("TESTLENS_PORT", "9200")
    monkeypatch.setenv("TESTLENS_DATA_DIR", "env-data")

    from_file = build_config(
        packages=(spec(),),
        repo_root=tmp_path,
        file_data={"host": "127.0.0.1", "port": 9100},
    )
    assert from_file.host == "127.0.0.2"  # 环境变量盖过文件
    assert from_file.port == 9200
    assert from_file.data_dir == (tmp_path / "env-data").resolve()

    explicit = build_config(
        packages=(spec(),),
        repo_root=tmp_path,
        host="127.0.0.3",
        port=9300,
        file_data={"host": "127.0.0.1", "port": 9100},
    )
    assert explicit.host == "127.0.0.3"  # 显式参数最高
    assert explicit.port == 9300


def test_apply_env_false_ignores_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESTLENS_PORT", "9200")

    config = build_config(packages=(spec(),), repo_root=tmp_path, apply_env=False)

    assert config.port == 8787


def test_invalid_env_port_is_config_error(tmp_path: Path, monkeypatch) -> None:
    # int() 抛的 ValueError 不是 ConfigError：不转换的话 CLI 会打一屏 traceback，
    # 而不是「配置有问题：…」+ 退出码 2。
    monkeypatch.setenv("TESTLENS_PORT", "abc")

    with pytest.raises(ConfigError, match="TESTLENS_PORT 必须是整数"):
        build_config(packages=(spec(),), repo_root=tmp_path)


# ----------------------------------------------------------------------
# PackageSpec 的每个字段都真的影响行为
# ----------------------------------------------------------------------
def test_tests_dir_and_test_glob_drive_scan(tmp_path: Path) -> None:
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "check_one.py").write_text("def check_a():\n    pass\n", encoding="utf-8")
    (tmp_path / "cases" / "test_ignored.py").write_text("def test_a():\n    pass\n", encoding="utf-8")

    package = spec(tests_dir="cases", test_glob="check_*.py")

    assert [item.name for item in list_test_files(package, tmp_path)] == ["check_one.py"]


def test_func_prefix_drives_test_count(tmp_path: Path) -> None:
    script = tmp_path / "test_sample.py"
    script.write_text(
        "def check_one():\n    pass\n\n\ndef test_two():\n    pass\n", encoding="utf-8"
    )

    assert count_tests(script, "check_") == 1
    assert count_tests(script, "test_") == 1


def test_root_dot_matches_nested_files(tmp_path: Path) -> None:
    """root="." 表示包根就是仓库根：test/ 子目录里的脚本也归它。"""
    (tmp_path / "test" / "unit").mkdir(parents=True)
    (tmp_path / "test" / "unit" / "test_a.py").write_text(
        "def test_one():\n    pass\n", encoding="utf-8"
    )

    package = spec(root=".")

    assert [item.name for item in list_test_files(package, tmp_path)] == ["test_a.py"]


# ----------------------------------------------------------------------
# 参数对行为的影响
# ----------------------------------------------------------------------
def test_max_output_lines_and_tail_lines_shape_run_output() -> None:
    from testlens.runner import Run

    run = Run(
        run_id="20260101-000000-abcd",
        package="demo",
        title="t",
        paths=[],
        max_lines=4,
        tail_lines=2,
    )
    for index in range(10):
        run.append(f"line{index}")

    assert len(run.lines) == 5  # 4 行正文 + 1 行“已丢弃”提示
    assert "输出超过 4 行" in run.lines[0]
    assert run.snapshot(2).splitlines() == ["line8", "line9"]


def test_history_max_entries_prunes_records_and_logs(tmp_path: Path) -> None:
    from testlens.history import HistoryStore

    config = make(tmp_path, history_max_entries=2)
    store = HistoryStore(config)
    run_ids = [f"20260101-00000{index}-abcd" for index in range(3)]

    for index, run_id in enumerate(run_ids):
        store.record({"run_id": run_id, "title": f"run {index}"}, f"log {index}")

    assert [item["run_id"] for item in store.entries()] == run_ids[::-1][:2]
    assert not (store.history_dir / f"{run_ids[0]}.log").exists()
    assert (store.history_dir / f"{run_ids[2]}.log").is_file()


def test_pytest_cmd_and_coverage_args_shape_command(tmp_path: Path) -> None:
    from testlens import Toolkit

    config = build_config(
        packages=(spec(source_pkg="shop", coverage_args=("--cov=shop", "--cov-report=")),),
        repo_root=tmp_path,
        pytest_cmd="python -m pytest -x",
    )
    package = config.packages[0]
    args = Toolkit(config).runner.command_for(package, ["test/test_a.py"], coverage=True)

    assert args[:4] == ["python", "-m", "pytest", "-x"]
    assert "test/test_a.py" in args
    assert "--cov=shop" in args
    assert any(arg.startswith("--basetemp=") for arg in args)


def test_pytest_cmd_keeps_absolute_interpreter_path(tmp_path: Path) -> None:
    from testlens import Toolkit

    # 用户最自然的写法：直接贴解释器绝对路径、不加引号。Windows 上按 POSIX
    # 规则拆会把反斜杠当转义符，args[0] 变成 C:...venvScriptspython.exe。
    config = build_config(
        packages=(spec(),), repo_root=tmp_path, pytest_cmd=f"{sys.executable} -m pytest"
    )
    args = Toolkit(config).runner.command_for(config.packages[0], ["test/test_a.py"])

    assert args[0] == sys.executable
    assert args[1:3] == ["-m", "pytest"]


def test_coverage_json_cmd_is_used(tmp_path: Path, monkeypatch) -> None:
    from testlens.coverage_report import CoverageService

    config = build_config(
        packages=(spec(source_pkg="shop"),),
        repo_root=tmp_path,
        coverage_json_cmd="mycov json",
    )
    data_file = tmp_path / ".coverage.demo"
    data_file.write_text("", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        output = Path(command[command.index("-o") + 1])
        output.write_text(json.dumps({"totals": {}}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("testlens.coverage_report.subprocess.run", fake_run)

    assert CoverageService(config)._run_coverage_json(data_file) == {"totals": {}}
    assert calls[0][:2] == ["mycov", "json"]
    assert "--data-file" in calls[0]


def test_template_path_is_used(tmp_path: Path) -> None:
    from testlens.inventory import render_html_string

    template = tmp_path / "mine.html"
    template.write_text(
        "<!-- testlens-template: 2 -->\n__STYLE__\n__DATA_SCRIPT__\n__SCRIPT__\nCUSTOM-MARKER",
        encoding="utf-8",
    )
    template.with_suffix(".css").write_text("/*custom-css*/", encoding="utf-8")
    template.with_suffix(".js").write_text("//custom-js", encoding="utf-8")
    config = build_config(
        packages=(spec(),), repo_root=tmp_path, template_path=template
    )

    html = render_html_string(config, {"packages": []}, run_enabled=False)

    assert "custom-css" in html
    assert "custom-js" in html
    assert "CUSTOM-MARKER" in html


def test_static_page_name_and_dir_from_config(tmp_path: Path) -> None:
    from testlens import Toolkit

    config = make(tmp_path, dist_dir="out", static_page_name="report.html")
    files = Toolkit(config).build_static_page()

    assert files[0] == (tmp_path / "out" / "report.html").resolve()
    assert files[1].name == "report.css"
    assert files[2].name == "report.js"


def test_static_page_single_from_config(tmp_path: Path) -> None:
    from testlens import Toolkit

    config = make(tmp_path, static_page_single=True)
    files = Toolkit(config).build_static_page()

    assert len(files) == 1
    assert "__TESTLENS_DATA__" in files[0].read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# 非法值逐项拒绝
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("port", 0, "port"),
        ("port", 70000, "port"),
        ("run_timeout", 0, "run_timeout"),
        ("max_output_lines", 0, "max_output_lines"),
        ("max_output_lines", 600_000, "max_output_lines"),
        ("tail_lines", 0, "tail_lines"),
        ("history_max_entries", 0, "history_max_entries"),
        ("static_page_name", "report.txt", "static_page_name"),
    ],
)
def test_invalid_values_are_rejected(
    tmp_path: Path, field: str, value, match: str
) -> None:
    with pytest.raises(ConfigError, match=match):
        build_config(packages=(spec(),), repo_root=tmp_path, **{field: value})


# ----------------------------------------------------------------------
# 命令行覆盖：replace 之后必须重新校验
# ----------------------------------------------------------------------
def test_override_config_revalidates_like_build_config(tmp_path: Path) -> None:
    config = build_config(packages=(spec(),), repo_root=tmp_path)

    assert override_config(config, port=9300).port == 9300
    with pytest.raises(ConfigError, match="port 超出范围"):
        override_config(config, port=0)


def test_override_config_does_not_repeat_missing_dir_warning(
    tmp_path: Path, capsys
) -> None:
    config = build_config(packages=(spec(root="missing"),), repo_root=tmp_path)
    assert "目录不存在" in capsys.readouterr().out

    override_config(config, port=9300)

    # 只改端口，不该把 build_config 已经打过的目录告警再刷一遍
    assert capsys.readouterr().out == ""


# ----------------------------------------------------------------------
# 命令拆分：Windows 的反斜杠不能被当成转义符
# ----------------------------------------------------------------------
def test_split_command_keeps_windows_backslashes() -> None:
    # shlex 默认按 POSIX 规则走：C:\venv\python.exe 会被拆成 C:venvpython.exe，
    # 子进程直接 FileNotFoundError
    assert split_command(r"C:\venv\Scripts\python.exe -m pytest", windows=True) == [
        r"C:\venv\Scripts\python.exe",
        "-m",
        "pytest",
    ]


def test_split_command_strips_windows_quotes() -> None:
    argv = split_command(
        r'"C:\Program Files\Python\python.exe" -c "print(1)"', windows=True
    )

    assert argv == [r"C:\Program Files\Python\python.exe", "-c", "print(1)"]


def test_split_command_posix_keeps_shell_rules() -> None:
    argv = split_command("python -m pytest --cov-report=xml:coverage.xml", windows=False)

    assert argv == ["python", "-m", "pytest", "--cov-report=xml:coverage.xml"]
    # POSIX 下反斜杠本来就是转义符，行为保持不变
    assert split_command(r"echo a\ b", windows=False) == ["echo", "a b"]


def test_split_command_defaults_to_current_platform() -> None:
    # 默认分支：两种平台下这个命令的拆分结果都一样
    assert split_command("uv run pytest") == ["uv", "run", "pytest"]


def test_tail_lines_cannot_exceed_max_output_lines(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="tail_lines"):
        build_config(
            packages=(spec(),),
            repo_root=tmp_path,
            max_output_lines=100,
            tail_lines=101,
        )


# ----------------------------------------------------------------------
# 两种配置文件：testlens_config.py / testlens.toml
# ----------------------------------------------------------------------
def _write_python_config(root: Path, port: int) -> None:
    (root / "testlens_config.py").write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "config = build_config(\n"
        "    packages=(PackageSpec(name='demo', label='Demo', root='.', source_pkg='shop'),),\n"
        "    repo_root=Path(__file__).parent,\n"
        f"    port={port},\n"
        ")\n",
        encoding="utf-8",
    )


def _write_toml_config(root: Path, port: int) -> None:
    (root / "testlens.toml").write_text(
        f'[server]\nport = {port}\n\n'
        '[[package]]\nname = "demo"\nlabel = "Demo"\nroot = "."\nsource_pkg = "shop"\n',
        encoding="utf-8",
    )


def test_python_config_only_is_used(tmp_path: Path) -> None:
    from testlens.cli import config_for_path

    _write_python_config(tmp_path, port=9000)

    config = config_for_path(str(tmp_path), None)

    assert config.port == 9000
    assert config.source is not None and config.source.endswith("testlens_config.py")
    assert [pkg.name for pkg in config.packages] == ["demo"]
    assert "--cov=shop" in config.packages[0].coverage_args


def test_toml_only_is_used(tmp_path: Path) -> None:
    from testlens.cli import config_for_path

    _write_toml_config(tmp_path, port=9100)

    config = config_for_path(str(tmp_path), None)

    assert config.port == 9100
    assert config.source is not None and config.source.endswith("testlens.toml")


def test_toml_wins_when_both_exist(tmp_path: Path) -> None:
    from testlens.cli import config_for_path

    _write_python_config(tmp_path, port=9000)
    _write_toml_config(tmp_path, port=9100)

    config = config_for_path(str(tmp_path), None)

    assert config.port == 9100
    assert config.source is not None and config.source.endswith("testlens.toml")


def test_broken_python_config_is_ignored_when_toml_exists(tmp_path: Path) -> None:
    """toml 优先意味着连 import 都不会碰 py 配置：坏了也不影响目录启动。"""
    from testlens.cli import config_for_path

    (tmp_path / "testlens_config.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    _write_toml_config(tmp_path, port=9100)

    assert config_for_path(str(tmp_path), None).port == 9100


def test_dev_py_target_uses_module_config_even_if_toml_exists(tmp_path: Path) -> None:
    """`dev testlens_config.py` 是按文件读：模块里的 port 优先于同目录 toml。"""
    from testlens.cli import config_for_path

    _write_python_config(tmp_path, port=9000)
    _write_toml_config(tmp_path, port=9100)

    config = config_for_path(str(tmp_path / "testlens_config.py"), None)

    assert config.port == 9000
    assert config.source is not None and config.source.endswith("testlens_config.py")


def test_demo_python_config_is_valid() -> None:
    """examples/demo 用的是 testlens_config.py：端口 / 包 / 覆盖率推导都要对得上。"""
    from testlens.cli import config_from_module

    demo = Path(__file__).resolve().parents[1] / "examples" / "demo"
    module = demo / "testlens_config.py"
    if not module.is_file():
        pytest.skip("examples/demo/testlens_config.py 本地不存在，跳过示例配置守护")
    config = config_from_module(module)

    assert config.port == 9000
    assert config.repo_root == demo.resolve()
    assert [pkg.name for pkg in config.packages] == ["demo"]
    package = config.packages[0]
    assert package.source_pkg == "shop"
    assert package.coverage_data_file == ".coverage.demo"
    assert "--cov=shop" in package.coverage_args
    assert len(list_test_files(package, config.repo_root)) == 5


def test_demo_dir_picks_toml_or_python_by_presence() -> None:
    """`dev .` 在 demo 目录：toml 在就用 toml，删掉就用 testlens_config.py，端口都是 9000。"""
    from testlens.cli import config_for_path

    demo = Path(__file__).resolve().parents[1] / "examples" / "demo"
    toml = demo / "testlens.toml"
    module = demo / "testlens_config.py"
    if not toml.is_file() and not module.is_file():
        pytest.skip("examples/demo 没有配置文件，跳过")

    config = config_for_path(str(demo), None)

    expected = "testlens.toml" if toml.is_file() else "testlens_config.py"
    assert config.source is not None and config.source.endswith(expected)
    assert config.port == 9000


def test_demo_toml_and_python_configs_agree() -> None:
    """demo 同时放了两种配置，值不能漂：toml 优先，但两份必须一致。"""
    from testlens.cli import config_from_module

    demo = Path(__file__).resolve().parents[1] / "examples" / "demo"
    toml_path = demo / "testlens.toml"
    module_path = demo / "testlens_config.py"
    if not toml_path.is_file() or not module_path.is_file():
        pytest.skip("demo 的两种配置没有同时存在，跳过一致性检查")

    from_toml = load_config(toml_path)
    from_module = config_from_module(module_path)

    assert from_toml.port == from_module.port
    assert from_toml.host == from_module.host
    assert from_toml.open_browser == from_module.open_browser
    assert from_toml.coverage_warmup == from_module.coverage_warmup
    assert [pkg.name for pkg in from_toml.packages] == [
        pkg.name for pkg in from_module.packages
    ]
    assert from_toml.packages[0].source_pkg == from_module.packages[0].source_pkg
