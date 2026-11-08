"""CLI 层测试：配置定位、目录推断、六个子命令与 main 的分发。

每条都走 `main(argv)` 这一层（除少数直接测辅助函数的分支），因为用户真正执行的是
命令行；只测 cmd_* 会漏掉参数默认值、--config 的两种位置、退出码这些细节。
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from testlens import PackageSpec, Toolkit, build_config
from testlens import cli
from testlens.cli import (
    CONFIG_MODULE,
    _describe,
    _report,
    _run,
    cmd_changed,
    cmd_list,
    cmd_page,
    cmd_plan,
    config_for_path,
    config_from_dir,
    config_from_module,
    discover_configs,
    find_config,
    infer_source_pkg,
    infer_spec,
    load_cli_config,
    main,
    missing_config_hint,
    nearest_config_dir,
    try_config_from_module,
)
from testlens.config import ConfigError


def write_project(root: Path, *, failing: bool = False) -> Path:
    tests = root / "test"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    if failing:
        (tests / "test_bad.py").write_text(
            "def test_bad():\n    assert False\n", encoding="utf-8"
        )
    return tests


def write_toml(
    root: Path,
    *,
    port: int = 9001,
    open_browser: bool | None = None,
    warmup: bool | None = None,
) -> Path:
    path = root / "testlens.toml"
    lines = ["[server]", f"port = {port}"]
    if open_browser is not None:
        lines.append(f"open_browser = {'true' if open_browser else 'false'}")
    if warmup is not None:
        lines += ["", "[coverage]", f"warmup = {'true' if warmup else 'false'}"]
    lines += ["", "[[package]]", 'name = "demo"', 'label = "Demo"', 'root = "."']
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_toolkit(root: Path, **kw) -> Toolkit:
    write_project(root)
    config = build_config(
        packages=(PackageSpec(name="demo", label="Demo", root="."),),
        repo_root=root,
        data_dir=root / "data",
        dist_dir=root / "dist",
        pytest_cmd=f'"{sys.executable}" -m pytest',
        **kw,
    )
    return Toolkit(config)


# ----------------------------------------------------------------------
# 配置定位
# ----------------------------------------------------------------------
def test_find_config_explicit_and_env(tmp_path: Path, monkeypatch) -> None:
    toml = write_toml(tmp_path)

    assert find_config(str(toml)) == toml
    with pytest.raises(ConfigError, match="配置文件不存在"):
        find_config(str(tmp_path / "missing.toml"))

    monkeypatch.setenv("TESTLENS_CONFIG", str(tmp_path / "gone.toml"))
    with pytest.raises(ConfigError, match="TESTLENS_CONFIG"):
        find_config()

    monkeypatch.setenv("TESTLENS_CONFIG", str(toml))
    assert find_config() == toml


def test_find_config_prefers_toml_in_cwd(tmp_path: Path, monkeypatch) -> None:
    toml = write_toml(tmp_path)
    (tmp_path / CONFIG_MODULE).write_text("config = None\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert find_config() == toml
    toml.unlink()
    assert find_config() == tmp_path / CONFIG_MODULE


def test_find_config_without_any_config_gives_sample(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError) as info:
        find_config()

    message = str(info.value)
    assert "插件不自带包定义" in message
    assert "[[package]]" in message


def test_load_cli_config_supports_toml_py_and_rejects_other(tmp_path: Path) -> None:
    toml = write_toml(tmp_path)
    assert load_cli_config(str(toml)).port == 9001

    module = tmp_path / "mine.py"
    module.write_text(
        "from testlens import PackageSpec, build_config\n"
        "from pathlib import Path\n"
        "config = build_config(packages=(PackageSpec(name='mine', label='Mine', root='.'),),"
        " repo_root=Path(__file__).parent, port=9100)\n",
        encoding="utf-8",
    )
    assert load_cli_config(str(module)).port == 9100

    other = tmp_path / "config.json"
    other.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError, match="只支持"):
        load_cli_config(str(other))


# ----------------------------------------------------------------------
# 目录推断
# ----------------------------------------------------------------------
def test_infer_source_pkg_only_when_exactly_one(tmp_path: Path) -> None:
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "__init__.py").write_text("", encoding="utf-8")
    assert infer_source_pkg(tmp_path) == "shop"

    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "__init__.py").write_text("", encoding="utf-8")
    assert infer_source_pkg(tmp_path) is None

    # 不存在的目录：iterdir 抛 OSError，按「不猜」处理
    assert infer_source_pkg(tmp_path / "nope") is None


def test_infer_source_pkg_skips_special_and_non_package_dirs(tmp_path: Path) -> None:
    for name in ("test", "venv", "_private", ".hidden"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "plain").mkdir()  # 没有 __init__.py 的不算

    assert infer_source_pkg(tmp_path) is None


def test_infer_spec_sanitizes_directory_name(tmp_path: Path) -> None:
    target = tmp_path / "My_App"
    target.mkdir()

    spec = infer_spec(target)

    assert spec.name == "my-app"
    assert spec.label == "My_App"
    assert spec.root == "."

    odd = tmp_path / "___"
    odd.mkdir()
    assert infer_spec(odd).name == "app"


# ----------------------------------------------------------------------
# .py 配置
# ----------------------------------------------------------------------
def test_try_config_from_module_variants(tmp_path: Path) -> None:
    config_file = tmp_path / "with_config.py"
    config_file.write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "config = build_config(packages=(PackageSpec(name='a', label='A', root='.'),),"
        " repo_root=Path(__file__).parent, port=9200)\n",
        encoding="utf-8",
    )
    loaded = try_config_from_module(config_file)
    assert loaded is not None and loaded.port == 9200
    assert loaded.source == str(config_file.resolve())

    toolkit_file = tmp_path / "with_toolkit.py"
    toolkit_file.write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, Toolkit, build_config\n"
        "config = build_config(packages=(PackageSpec(name='b', label='B', root='.'),),"
        " repo_root=Path(__file__).parent, port=9300)\n"
        "toolkit = Toolkit(config)\n",
        encoding="utf-8",
    )
    loaded = try_config_from_module(toolkit_file)
    assert loaded is not None and loaded.port == 9300

    plain = tmp_path / "plain.py"
    plain.write_text("x = 1\n", encoding="utf-8")
    assert try_config_from_module(plain) is None

    broken = tmp_path / "broken.py"
    broken.write_text("raise RuntimeError('导入就炸')\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="导入就炸"):
        try_config_from_module(broken)


def test_config_from_module_requires_config(tmp_path: Path) -> None:
    plain = tmp_path / "plain.py"
    plain.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ConfigError) as info:
        config_from_module(plain)

    message = str(info.value)
    assert "没有找到 config" in message
    assert "python plain.py" in message


def test_config_from_dir_reads_toml_then_module_then_infers(tmp_path: Path) -> None:
    write_project(tmp_path)
    toml = write_toml(tmp_path, port=9400)
    assert config_from_dir(tmp_path).port == 9400

    toml.unlink()
    (tmp_path / CONFIG_MODULE).write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "config = build_config(packages=(PackageSpec(name='py', label='Py', root='.'),),"
        " repo_root=Path(__file__).parent, port=9500)\n",
        encoding="utf-8",
    )
    assert config_from_dir(tmp_path).port == 9500

    (tmp_path / CONFIG_MODULE).unlink()
    inferred = config_from_dir(tmp_path)
    assert inferred.source is None
    # 推断时包名来自目录名（临时目录名会被规范化），root 一定是项目根
    assert inferred.packages[0].name == infer_spec(tmp_path).name
    assert inferred.packages[0].root == "."


def test_nearest_config_dir_walks_up_and_stops_at_boundaries(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested = project / "a" / "b"
    nested.mkdir(parents=True)
    write_toml(project)

    assert nearest_config_dir(nested) == project

    # .git 是边界：再往上不找
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    (other / "deep").mkdir()
    assert nearest_config_dir(other / "deep") is None

    # 层数用完：2 层之内都没有配置
    plain = tmp_path / "plain" / "x"
    plain.mkdir(parents=True)
    assert nearest_config_dir(plain, levels=2) is None


def test_config_for_path_targets_and_overrides(tmp_path: Path) -> None:
    write_project(tmp_path)
    write_toml(tmp_path, port=9600)
    override = tmp_path / "override.py"
    override.write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "config = build_config(packages=(PackageSpec(name='ov', label='Ov', root='.'),),"
        " repo_root=Path(__file__).parent, port=9700)\n",
        encoding="utf-8",
    )

    assert config_for_path(str(tmp_path), None).port == 9600
    assert config_for_path(None, str(override)).port == 9700

    script = tmp_path / "run_me.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    assert config_for_path(str(script), None).port == 9600

    notes = tmp_path / "notes.txt"
    notes.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="只接受目录或 .py"):
        config_for_path(str(notes), None)
    with pytest.raises(ConfigError, match="路径不存在"):
        config_for_path(str(tmp_path / "ghost"), None)

    tuned = config_for_path(str(tmp_path), None, port=9800, open_browser=True)
    assert tuned.port == 9800 and tuned.open_browser is True


def test_config_for_script_without_any_config_falls_back_to_inference(tmp_path: Path) -> None:
    write_project(tmp_path)
    script = tmp_path / "app.py"
    script.write_text("print('entry')\n", encoding="utf-8")

    config = config_for_path(str(script), None)

    assert config.source is None
    assert config.repo_root == tmp_path.resolve()
    assert config.packages[0].root == "."


def test_config_for_dir_alias_keeps_behaviour(tmp_path: Path) -> None:
    write_project(tmp_path)
    write_toml(tmp_path, port=9900)

    assert config_from_dir(tmp_path).port == 9900


# ----------------------------------------------------------------------
# 提示与描述
# ----------------------------------------------------------------------
def test_discover_configs_respects_depth_and_skips(tmp_path: Path) -> None:
    found = tmp_path / "apps" / "web" / "testlens.toml"
    found.parent.mkdir(parents=True)
    found.write_text("", encoding="utf-8")
    deep = tmp_path / "a" / "b" / "c" / "d" / "testlens_config.py"
    deep.parent.mkdir(parents=True)
    deep.write_text("", encoding="utf-8")
    skipped = tmp_path / "node_modules" / "pkg" / "testlens.toml"
    skipped.parent.mkdir(parents=True)
    skipped.write_text("", encoding="utf-8")

    configs = discover_configs(tmp_path)

    assert found in configs
    assert deep not in configs
    assert skipped not in configs


def test_missing_config_hint_lists_found_configs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write_project(tmp_path)
    nested = tmp_path / "apps" / "web"
    nested.mkdir(parents=True)
    write_toml(nested)
    config = build_config(
        packages=(PackageSpec(name="demo", label="Demo", root="."),),
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        dist_dir=tmp_path / "dist",
    )

    hint = missing_config_hint(config)

    assert "这里没有可用的测试配置" in hint
    assert "apps/web/testlens.toml" in hint


def test_missing_config_hint_uses_absolute_path_outside_cwd(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    write_project(root)
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    write_toml(nested)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    config = build_config(
        packages=(PackageSpec(name="demo", label="Demo", root="."),),
        repo_root=root,
        data_dir=root / "data",
        dist_dir=root / "dist",
    )

    hint = missing_config_hint(config)

    assert str(nested / "testlens.toml") in hint


def test_describe_covers_every_status() -> None:
    assert _describe({"status": "new"}) == "新增（缓存里没有）"
    assert _describe({"status": "missing"}) == "已删除（缓存里还有）"
    assert _describe({"status": "changed", "diff": {"added": 2, "removed": 1}}) == (
        "内容已修改（+2 -1 行）"
    )
    assert _describe({"status": "changed"}) == "内容已修改"


# ----------------------------------------------------------------------
# list / changed / plan
# ----------------------------------------------------------------------
def test_cmd_list_prints_projects_and_total(tmp_path: Path, capsys) -> None:
    toolkit = make_toolkit(tmp_path)

    assert cmd_list(toolkit, toolkit.config.packages) == 0

    out = capsys.readouterr().out
    assert "Demo  (.)" in out
    assert "1 个文件, 1 个测试函数" in out
    assert "合计：1 个包" in out


def test_cmd_list_skips_empty_package(tmp_path: Path, capsys) -> None:
    (tmp_path / "apps/empty").mkdir(parents=True)
    (tmp_path / "apps/demo/test").mkdir(parents=True)
    (tmp_path / "apps/demo/test/test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    config = build_config(
        packages=(
            PackageSpec(name="empty", label="Empty", root="apps/empty"),
            PackageSpec(name="demo", label="Demo", root="apps/demo"),
        ),
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
    )
    toolkit = Toolkit(config)

    assert cmd_list(toolkit, config.packages) == 0

    out = capsys.readouterr().out
    assert "Demo  (apps/demo)" in out
    assert "Empty" not in out


def test_cmd_changed_without_changes(tmp_path: Path, capsys) -> None:
    toolkit = make_toolkit(tmp_path)
    toolkit.scan()  # 首次扫描
    toolkit.scanner.mark_baseline(None)

    assert cmd_changed(toolkit, toolkit.config.packages) == 0
    out = capsys.readouterr().out
    assert "没有检测到测试脚本变化" in out
    assert "缓存基线：1 个脚本" in out


def test_cmd_changed_reports_new_modified_and_missing(tmp_path: Path, capsys) -> None:
    toolkit = make_toolkit(tmp_path)
    old = tmp_path / "test" / "test_old.py"
    old.write_text("def test_old():\n    assert True\n", encoding="utf-8")
    toolkit.scanner.mark_baseline(None)

    # 基线建立之后：改一个、新增一个、删掉一个
    (tmp_path / "test" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n\n\ndef test_two():\n    assert True\n",
        encoding="utf-8",
    )
    (tmp_path / "test" / "test_bad.py").write_text(
        "def test_bad():\n    assert False\n", encoding="utf-8"
    )
    old.unlink()

    assert cmd_changed(toolkit, toolkit.config.packages) == 0

    out = capsys.readouterr().out
    assert "有 3 个测试脚本变化" in out
    assert "test/test_ok.py" in out
    assert "内容已修改（+4 -0 行）" in out
    assert "test/test_bad.py" in out
    assert "新增（缓存里没有）" in out
    assert "test/test_old.py" in out
    assert "已删除（缓存里还有）" in out


def test_cmd_changed_skips_package_without_changes(tmp_path: Path, capsys) -> None:
    specs = (
        PackageSpec(name="alpha", label="Alpha", root="apps/alpha"),
        PackageSpec(name="beta", label="Beta", root="apps/beta"),
    )
    for name in ("alpha", "beta"):
        (tmp_path / f"apps/{name}/test").mkdir(parents=True)
        (tmp_path / f"apps/{name}/test/test_{name}.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
        )
    config = build_config(
        packages=specs,
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        dist_dir=tmp_path / "dist",
    )
    toolkit = Toolkit(config)
    toolkit.scanner.mark_baseline(None)
    (tmp_path / "apps/alpha/test/test_alpha.py").write_text(
        "def test_ok():\n    assert True\n\n\ndef test_more():\n    assert True\n",
        encoding="utf-8",
    )

    assert cmd_changed(toolkit, config.packages) == 0

    out = capsys.readouterr().out
    assert "Alpha" in out
    assert "Beta" not in out


def test_run_skips_files_outside_packages(tmp_path: Path) -> None:
    (tmp_path / "apps/demo/test").mkdir(parents=True)
    (tmp_path / "apps/demo/test/test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    config = build_config(
        packages=(PackageSpec(name="demo", label="Demo", root="apps/demo"),),
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        pytest_cmd=f'"{sys.executable}" -m pytest',
    )
    stray = tmp_path / "stray.py"
    stray.write_text("x = 1\n", encoding="utf-8")

    assert _run(config, [stray]) == 0


def test_cmd_plan_without_changes(tmp_path: Path, capsys) -> None:
    toolkit = make_toolkit(tmp_path)
    toolkit.scanner.mark_baseline(None)

    assert cmd_plan(toolkit, toolkit.config.packages) == 0
    out = capsys.readouterr().out
    assert "不需要重跑" in out
    assert "变更检测的边界" in out


def test_cmd_plan_lists_and_runs(tmp_path: Path, monkeypatch, capsys) -> None:
    toolkit = make_toolkit(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kw: calls.append(command) or SimpleNamespace(returncode=0),
    )

    assert cmd_plan(toolkit, toolkit.config.packages, run=True) == 0

    out = capsys.readouterr().out
    assert "建议运行：1 个脚本 / 1 个测试函数" in out
    assert "开始运行…" in out
    assert calls and calls[0][-2:] == ["test/test_ok.py", "-q"]


def test_cmd_plan_returns_failure_when_pytest_fails(tmp_path: Path, monkeypatch) -> None:
    toolkit = make_toolkit(tmp_path)
    monkeypatch.setattr(
        cli.subprocess, "run", lambda command, **kw: SimpleNamespace(returncode=1)
    )

    assert cmd_plan(toolkit, toolkit.config.packages, run=True) == 1


def test_report_handles_file_outside_any_package(tmp_path: Path, capsys) -> None:
    monkeypatch_cwd = tmp_path
    toolkit = make_toolkit(monkeypatch_cwd)
    stray = tmp_path / "stray.py"
    stray.write_text("x = 1\n", encoding="utf-8")
    # 让 package_of 返回 None：包 root 不是 "." 时，仓库根的散文件不属于任何包
    (tmp_path / "apps/demo/test").mkdir(parents=True)
    config = build_config(
        packages=(PackageSpec(name="demo", label="Demo", root="apps/demo"),),
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        dist_dir=tmp_path / "dist",
    )
    toolkit = Toolkit(config)

    assert _report(toolkit, [stray], run=False, title="建议") == 0

    out = capsys.readouterr().out
    assert "建议：1 个脚本 / 0 个测试函数" in out


# ----------------------------------------------------------------------
# page
# ----------------------------------------------------------------------
def test_cmd_page_writes_files_and_prints_stats(tmp_path: Path, capsys) -> None:
    toolkit = make_toolkit(tmp_path)

    assert cmd_page(toolkit, None) == 0

    out = capsys.readouterr().out
    assert "已生成" in out and "1 个脚本" in out
    assert "testlens serve" in out
    assert (toolkit.config.dist_dir / toolkit.config.static_page_name).is_file()


def test_cmd_page_honours_output_single_and_source(tmp_path: Path, capsys) -> None:
    base = build_config(
        packages=(PackageSpec(name="demo", label="Demo", root="."),),
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        dist_dir=tmp_path / "dist",
    )
    write_project(tmp_path)
    toolkit = Toolkit(replace(base, source=str(tmp_path / "testlens.toml")))
    target = tmp_path / "out" / "one.html"

    assert cmd_page(toolkit, str(target), single=True) == 0

    assert target.is_file()
    out = capsys.readouterr().out
    assert 'testlens serve --config' in out
    assert str(tmp_path / "testlens.toml") in out


# ----------------------------------------------------------------------
# main 分发
# ----------------------------------------------------------------------
def test_main_without_command_prints_help(capsys) -> None:
    assert main([]) == 0
    assert "测试脚本清单" in capsys.readouterr().out


def test_main_version_exits(capsys) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert "testlens" in capsys.readouterr().out


def test_main_reports_config_error(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["list"]) == 2
    assert "配置有问题" in capsys.readouterr().err


def test_main_dispatches_list_changed_and_plan(tmp_path: Path, monkeypatch, capsys) -> None:
    write_project(tmp_path)
    write_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))

    assert main(["list"]) == 0
    assert "合计" in capsys.readouterr().out
    assert main(["changed"]) == 0
    changed_out = capsys.readouterr().out
    assert "测试脚本变化" in changed_out or "没有检测到" in changed_out
    assert main(["plan"]) == 0
    assert "建议运行" in capsys.readouterr().out
    assert main(["plan", "--run"]) == 0


def test_main_dispatches_page(tmp_path: Path, monkeypatch, capsys) -> None:
    write_project(tmp_path)
    write_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "page" / "index.html"

    assert main(["page", "--output", str(output), "--single"]) == 0
    assert output.is_file()
    assert "已生成" in capsys.readouterr().out


def test_main_serve_applies_port_and_browser(tmp_path: Path, monkeypatch) -> None:
    write_project(tmp_path)
    write_toml(tmp_path, port=9001)
    monkeypatch.chdir(tmp_path)
    seen: dict = {}

    def fake_serve(self):
        seen["config"] = self.config
        return 0

    monkeypatch.setattr("testlens.toolkit.Toolkit.serve", fake_serve)

    assert main(["serve", "--port", "9123", "--browser"]) == 0
    assert seen["config"].port == 9123
    assert seen["config"].open_browser is True

    assert main(["serve", "--no-browser", "--no-warm-coverage"]) == 0
    assert seen["config"].port == 9001
    assert seen["config"].open_browser is False
    assert seen["config"].coverage_warmup is False


def test_main_serve_keeps_config_when_flags_absent(tmp_path: Path, monkeypatch) -> None:
    write_project(tmp_path)
    write_toml(tmp_path, port=9002, open_browser=True, warmup=True)
    monkeypatch.chdir(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(
        "testlens.toolkit.Toolkit.serve",
        lambda self: seen.setdefault("config", self.config) and 0,
    )

    assert main(["serve"]) == 0
    assert seen["config"].port == 9002
    assert seen["config"].open_browser is True


def test_main_rejects_out_of_range_port_override(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    write_project(tmp_path)
    write_toml(tmp_path, port=9001)
    monkeypatch.chdir(tmp_path)

    # dataclasses.replace 不跑校验时，--port 0 会绑到系统分配的随机端口，
    # 再把 http://127.0.0.1:0/ 这种打不开的地址打给用户
    assert main(["serve", "--port", "0"]) == 2
    assert "port 超出范围" in capsys.readouterr().err

    assert main(["dev", ".", "--port", "70000"]) == 2
    assert "port 超出范围" in capsys.readouterr().err


def test_main_reports_invalid_env_port(tmp_path: Path, monkeypatch, capsys) -> None:
    write_project(tmp_path)
    write_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TESTLENS_PORT", "abc")

    # 退出码 2 + 一句可读的提示，而不是 ValueError 的 traceback
    assert main(["list"]) == 2
    assert "TESTLENS_PORT" in capsys.readouterr().err


def test_main_dev_serves_with_inferred_config(tmp_path: Path, monkeypatch, capsys) -> None:
    write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    seen: dict = {}

    def fake_serve(self):
        seen["config"] = self.config
        return 0

    monkeypatch.setattr("testlens.toolkit.Toolkit.serve", fake_serve)

    assert main(["dev", ".", "--port", "9200", "--browser"]) == 0
    assert seen["config"].port == 9200
    assert seen["config"].open_browser is True
    assert seen["config"].source is None


def test_main_dev_without_scripts_explains_why(tmp_path: Path, monkeypatch, capsys) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)

    assert main(["dev", "."]) == 2

    err = capsys.readouterr().err
    assert "这里没有可用的测试配置" in err


def test_config_from_dir_uses_toml_over_module(tmp_path: Path) -> None:
    write_project(tmp_path)
    write_toml(tmp_path, port=9300)
    (tmp_path / CONFIG_MODULE).write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "config = build_config(packages=(PackageSpec(name='py', label='Py', root='.'),),"
        " repo_root=Path(__file__).parent, port=9400)\n",
        encoding="utf-8",
    )

    assert config_from_dir(tmp_path).port == 9300


def test_config_for_path_uses_module_config_over_directory(tmp_path: Path) -> None:
    write_project(tmp_path)
    write_toml(tmp_path, port=9300)
    module = tmp_path / "mine.py"
    module.write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "config = build_config(packages=(PackageSpec(name='mine', label='Mine', root='.'),),"
        " repo_root=Path(__file__).parent, port=9400)\n",
        encoding="utf-8",
    )

    assert config_for_path(str(module), None).port == 9400


def test_cli_env_config_is_used_by_main(tmp_path: Path, monkeypatch) -> None:
    write_project(tmp_path)
    toml = write_toml(tmp_path, port=9500)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TESTLENS_CONFIG", str(toml))
    seen: dict = {}
    monkeypatch.setattr(
        "testlens.toolkit.Toolkit.serve",
        lambda self: seen.setdefault("config", self.config) and 0,
    )

    assert main(["serve"]) == 0
    assert seen["config"].port == 9500


def test_module_entrypoint_runs(monkeypatch, capsys) -> None:
    """`python -m testlens` 的入口文件也要执行到。"""
    import importlib
    import runpy

    # 作为普通模块被 import 时不该启动（覆盖 if 的 false 分支）
    importlib.import_module("testlens.__main__")

    monkeypatch.setattr(sys, "argv", ["testlens"])
    with pytest.raises(SystemExit) as info:
        runpy.run_module("testlens", run_name="__main__")

    assert info.value.code == 0
    assert "测试脚本清单" in capsys.readouterr().out


def test_cli_dunder_main_guard(monkeypatch, capsys) -> None:
    """cli.py 末尾的 `if __name__ == "__main__"` 也要覆盖到。"""
    import runpy

    monkeypatch.setattr(sys, "argv", ["testlens"])
    with pytest.raises(SystemExit) as info:
        runpy.run_module("testlens.cli", run_name="__main__")

    assert info.value.code == 0
    assert "测试脚本清单" in capsys.readouterr().out
