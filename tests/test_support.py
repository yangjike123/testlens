"""各服务模块的边界分支：坏缓存、坏配置、坏 XML、读不到的文件。

这些不是"为了覆盖率硬凑"的路径：磁盘半截 JSON、编码坏掉的脚本、旧报告里的
已删除文件，都是开发机上真实会遇到的情况，面板不能因为它们整页挂掉。
"""

from __future__ import annotations

import builtins
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from testlens import PackageSpec, build_config
from testlens import config as config_module
from testlens.config import ConfigError, count_tests, detect_packages, select_packages
from testlens.coverage_report import CoverageService, NoCoverageData
from testlens.history import HistoryStore
from testlens.inventory import (
    TEMPLATE_CONTRACT,
    _fill_template,
    _page_assets,
    build_payload,
    render_html_string,
)
from testlens.junit import _failure_message, file_from_classname, parse_files
from testlens.scan import Scanner


def write_tests(root: Path, count: int = 1) -> None:
    (root / "test").mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / f"test/test_{index}.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
        )


def make_config(root: Path, specs=None, **kw):
    write_tests(root)
    base = {
        "packages": specs or (PackageSpec(name="demo", label="Demo", root="."),),
        "repo_root": root,
        "data_dir": root / "data",
        "dist_dir": root / "dist",
        "coverage_warmup": False,
    }
    base.update(kw)
    return build_config(**base)


# ----------------------------------------------------------------------
# config：查询辅助 / 推导 / 校验
# ----------------------------------------------------------------------
def test_select_packages_accepts_all_and_rejects_unknown() -> None:
    specs = (
        PackageSpec(name="a", label="A", root="apps/a"),
        PackageSpec(name="b", label="B", root="apps/b"),
    )

    assert select_packages(specs, "all") == specs
    assert select_packages(specs, None) == specs
    assert select_packages(specs, "b") == (specs[1],)

    with pytest.raises(SystemExit, match="未知的包"):
        select_packages(specs, "c")


def test_count_tests_returns_zero_for_unparsable_files(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def test_ok(:\n", encoding="utf-8")
    assert count_tests(broken) == 0

    undecodable = tmp_path / "undecodable.py"
    undecodable.write_bytes(b"\xff\xfe\x00bad")
    assert count_tests(undecodable) == 0

    assert count_tests(tmp_path / "missing.py") == 0


def test_detect_packages_only_takes_real_package_dirs(tmp_path: Path) -> None:
    for name in ("alpha", "beta", ".hidden", "_private"):
        (tmp_path / "apps" / name).mkdir(parents=True)
    (tmp_path / "apps" / "alpha" / "test").mkdir()
    (tmp_path / "apps" / "beta" / "test").mkdir()
    (tmp_path / "apps" / ".hidden" / "test").mkdir()
    (tmp_path / "apps" / "_private" / "test").mkdir()
    (tmp_path / "apps" / "notests").mkdir()

    specs = detect_packages(tmp_path)

    assert [spec.name for spec in specs] == ["alpha", "beta"]
    assert detect_packages(tmp_path, coverage_presets={"alpha": {"label": "Alpha!"}})[0].label == "Alpha!"
    assert detect_packages(tmp_path / "nope") == ()


def test_pyproject_data_file_tolerates_broken_file(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text("this is not toml [[[", encoding="utf-8")

    assert config_module._pyproject_data_file(path) is None


def test_pyproject_data_file_without_tomllib(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text("[tool.coverage.run]\ndata_file = '.coverage.x'\n", encoding="utf-8")
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tomllib":
            raise ModuleNotFoundError("没有 tomllib")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert config_module._pyproject_data_file(path) is None


def test_rc_data_file_tolerates_broken_content(tmp_path: Path) -> None:
    """没有 section 头的 .coveragerc：configparser 抛 Error，按「没配」处理。"""
    broken = tmp_path / ".coveragerc"
    broken.write_text("data_file = .coverage.x\n", encoding="utf-8")

    assert config_module._rc_data_file(broken) is None


def test_makefile_reader_tolerates_unreadable_file(tmp_path: Path, monkeypatch) -> None:
    block = tmp_path / "Makefile"
    block.write_text("test:\n\tpytest --cov=app\n", encoding="utf-8")
    real_read = Path.read_text

    def fake_read(self, *args, **kwargs):
        if self.name == "Makefile":
            raise OSError("文件被锁住了")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read)

    assert config_module._makefile_coverage(block, "app") is None
    assert config_module._makefile_coverage(None, "app") is None


def test_makefile_with_variable_cov_config_falls_back(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "test:\n\tpytest --cov=app --cov-config=$(COVCFG)\n", encoding="utf-8"
    )

    assert config_module._makefile_coverage(makefile, "app") == ("test", None)


def test_package_tokens_without_source_pkg() -> None:
    spec = PackageSpec(name="Web-Service", label="Web", root="apps/web")

    assert config_module._package_tokens(spec) == {"web-service", "web", "service"}


def test_coverage_data_path_with_pyproject_without_data_file(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\n', encoding="utf-8")
    files = config_module._ProjectFiles(coveragercs=(), makefile=None, pyproject=pyproject)
    package = PackageSpec(name="demo", label="Demo", root="apps/demo")

    path = config_module._coverage_data_path(files, None, tmp_path, package)

    assert path == tmp_path / ".coverage.demo"


def test_survey_prefers_pyproject_with_data_file(tmp_path: Path) -> None:
    """向上找配置时，只有写了 data_file 的 pyproject 才算数。"""
    (tmp_path / ".git").mkdir()
    without = tmp_path / "nested" / "pyproject.toml"
    without.parent.mkdir()
    without.write_text('[project]\nname = "no-coverage"\n', encoding="utf-8")
    with_data = tmp_path / "pyproject.toml"
    with_data.write_text(
        "[tool.coverage.run]\ndata_file = '.coverage.custom'\n", encoding="utf-8"
    )

    files = config_module._survey(tmp_path / "nested")

    assert files.pyproject == with_data


def test_display_path_falls_back_to_absolute(tmp_path: Path, monkeypatch) -> None:
    outside = tmp_path.parent / "elsewhere" / "file.txt"
    base = tmp_path / "repo"
    base.mkdir()

    def no_relpath(*args, **kwargs):
        raise ValueError("不同盘符")

    monkeypatch.setattr(config_module.os.path, "relpath", no_relpath)

    assert config_module._display_path(outside, base) == outside.resolve().as_posix()


def test_validate_rejects_empty_and_duplicate_names(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    with pytest.raises(ConfigError, match="packages 不能为空"):
        config_module._validate(replace(config, packages=()))

    with pytest.raises(ConfigError, match="label 不能为空"):
        build_config(
            packages=(PackageSpec(name="demo", label="   ", root="."),),
            repo_root=tmp_path,
        )

    with pytest.raises(ConfigError, match="name 重复"):
        build_config(
            packages=(
                PackageSpec(name="dup", label="A", root="apps/a"),
                PackageSpec(name="dup", label="B", root="apps/b"),
            ),
            repo_root=tmp_path,
        )


def test_validate_rejects_root_resolving_outside_repo(tmp_path: Path, monkeypatch) -> None:
    """root 不含 ".."，但解析后跑到仓库外（符号链接 / 挂载点）也要拒绝。"""
    write_tests(tmp_path)
    outside = tmp_path.parent / "outside-root"
    real_resolve = Path.resolve

    def fake_resolve(self, *args, **kwargs):
        if self.name == "escape":
            return outside
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    with pytest.raises(ConfigError, match="越出 repo_root"):
        build_config(
            packages=(PackageSpec(name="demo", label="Demo", root="escape"),),
            repo_root=tmp_path,
        )


# ----------------------------------------------------------------------
# history：坏索引 / 非法 run_id
# ----------------------------------------------------------------------
def test_history_tolerates_corrupt_index(tmp_path: Path) -> None:
    store = HistoryStore(make_config(tmp_path))
    store.history_dir.mkdir(parents=True)
    store.index_file.write_text("{half", encoding="utf-8")

    assert store.entries() == []


def test_history_rejects_invalid_run_id(tmp_path: Path) -> None:
    store = HistoryStore(make_config(tmp_path))

    with pytest.raises(ValueError, match="非法的 run_id"):
        store.record({"run_id": "../../etc/passwd"}, "x")

    assert store.read_log("../../etc/passwd") is None
    assert store.find("not-a-run-id") is None


def test_history_prunes_entries_with_invalid_run_id(tmp_path: Path) -> None:
    """索引里混进坏 id 时，裁剪只跳过它的日志删除，不能中断整个写入。"""
    store = HistoryStore(make_config(tmp_path, history_max_entries=1))
    store.history_dir.mkdir(parents=True)
    store.index_file.write_text(
        json.dumps(
            [
                {"run_id": "broken-id", "title": "坏记录"},
                {"run_id": "20260101-000000-aaaa", "title": "旧记录"},
            ]
        ),
        encoding="utf-8",
    )
    (store.history_dir / "20260101-000000-aaaa.log").write_text("old", encoding="utf-8")

    store.record({"run_id": "20260101-000000-bbbb", "title": "新记录"}, "new")

    entries = store.entries()
    assert [item["run_id"] for item in entries] == ["20260101-000000-bbbb"]
    assert not (store.history_dir / "20260101-000000-aaaa.log").exists()


# ----------------------------------------------------------------------
# inventory：模板资源缺失 / 占位符缺失
# ----------------------------------------------------------------------
def test_page_assets_reports_missing_files(tmp_path: Path) -> None:
    template = tmp_path / "tpl.html"
    template.write_text(TEMPLATE_CONTRACT, encoding="utf-8")

    with pytest.raises(ValueError, match="需要同目录的 tpl.css / tpl.js"):
        _page_assets(template)


def test_fill_template_requires_contract_and_placeholders() -> None:
    with pytest.raises(ValueError, match="契约标记"):
        _fill_template("<html></html>", "data", "style", "script")

    with pytest.raises(ValueError, match="占位符"):
        _fill_template(TEMPLATE_CONTRACT, "data", "style", "script")


def test_render_html_string_surfaces_missing_template_assets(tmp_path: Path) -> None:
    template = tmp_path / "only.html"
    template.write_text(
        f"<!-- {TEMPLATE_CONTRACT} -->__STYLE__ __DATA_SCRIPT__ __SCRIPT__",
        encoding="utf-8",
    )
    config = make_config(tmp_path, template_path=template)
    scanner = Scanner(config)
    payload = build_payload(scanner)

    with pytest.raises(ValueError, match="需要同目录"):
        render_html_string(config, payload)


# ----------------------------------------------------------------------
# junit：失败信息与坏字段
# ----------------------------------------------------------------------
def write_junit(path: Path, body: str) -> Path:
    path.write_text(f'<?xml version="1.0"?><testsuite>{body}</testsuite>', encoding="utf-8")
    return path


def test_failure_message_variants(tmp_path: Path) -> None:
    import xml.etree.ElementTree as ET

    with_text = ET.fromstring("<testcase><failure>boom\nstack</failure></testcase>")
    assert _failure_message(with_text) == "boom"

    no_node = ET.fromstring("<testcase></testcase>")
    assert _failure_message(no_node) == ""

    empty = ET.fromstring("<testcase><failure message='  '></failure></testcase>")
    assert _failure_message(empty) == ""


def test_file_from_classname_handles_package_prefix(tmp_path: Path) -> None:
    """monorepo 从仓库根跑 pytest 时，classname 会带上包根，不能再补一次前缀。"""
    target = tmp_path / "services/api/test/unit/test_users.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    package = PackageSpec(name="api", label="API", root="services/api")

    assert (
        file_from_classname("services.api.test.unit.test_users", package, tmp_path)
        == "services/api/test/unit/test_users.py"
    )
    # 带测试类名：逐级剥掉类名后仍能落到文件
    assert (
        file_from_classname("services.api.test.unit.test_users.TestUsers", package, tmp_path)
        == "services/api/test/unit/test_users.py"
    )
    # 老行为：classname 相对包根（没有仓库前缀）时补前缀
    assert (
        file_from_classname("test.unit.test_users", package, tmp_path)
        == "services/api/test/unit/test_users.py"
    )
    assert file_from_classname("nope", package, tmp_path) == ""
    assert file_from_classname(None, package, tmp_path) == ""


def test_parse_files_handles_bad_time_and_error_nodes(tmp_path: Path) -> None:
    package = PackageSpec(name="demo", label="Demo", root=".")
    junit = write_junit(
        tmp_path / "junit.xml",
        '<testcase name="a" classname="test.test_x" time="oops">'
        "<error>error text</error></testcase>"
        '<testcase name="b" classname="test.test_x" time="1.5"/>',
    )

    result = parse_files([junit], package, tmp_path)

    assert result is not None
    assert result["totals"]["tests"] == 2
    assert result["totals"]["errors"] == 1
    assert result["totals"]["time"] == 1.5
    assert result["failures"][0]["message"] == "error text"


# ----------------------------------------------------------------------
# scan：坏缓存 / 坏编码 / 已删除脚本
# ----------------------------------------------------------------------
def test_scan_marks_unreadable_and_falls_back_on_bad_cache(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    bad = tmp_path / "test" / "test_bad.py"
    bad.write_bytes(b"\xff\xfe\x00bad")
    scanner = Scanner(config)
    scanner.cache_dir.mkdir(parents=True)
    scanner.cache_file.write_text("{not json", encoding="utf-8")

    result = scanner.scan()

    by_path = {item["path"]: item for item in result["scripts"]}
    assert by_path["test/test_bad.py"]["status"] == "unreadable"

    # 形状不对（不是 dict / 没有 scripts）也按空缓存处理
    scanner.cache_file.write_text(json.dumps(["oops"]), encoding="utf-8")
    assert scanner.scan()["cached"] == 0
    scanner.cache_file.write_text(json.dumps({"version": 1, "scripts": []}), encoding="utf-8")
    assert scanner.scan()["cached"] == 0


def test_mark_baseline_skips_unreadable_and_drops_deleted(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    bad = tmp_path / "test" / "test_bad.py"
    bad.write_bytes(b"\xff\xfe\x00bad")
    scanner = Scanner(config)

    assert scanner.mark_baseline(None) == 1

    (tmp_path / "test" / "test_0.py").unlink()
    assert scanner.mark_baseline(None) == 0
    assert scanner._load_raw()["scripts"] == {}


def test_scan_ignores_cached_entries_of_other_packages(tmp_path: Path) -> None:
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
    scanner = Scanner(config)
    scanner.mark_baseline(None)
    (tmp_path / "apps/beta/test/test_beta.py").unlink()

    scoped = scanner.scan((config.packages[0],))

    assert all(item["package"] == "alpha" for item in scoped["scripts"])
    assert scoped["changed"] == 0


def test_scan_handles_cached_path_outside_tests_dir(tmp_path: Path) -> None:
    """缓存里的是旧布局（不在 test/ 下）：仍按「已删除」提示，分组退回文件名。"""
    config = make_config(tmp_path)
    scanner = Scanner(config)
    scanner.cache_dir.mkdir(parents=True)
    scanner.cache_file.write_text(
        json.dumps(
            {
                "version": 1,
                "scripts": {
                    "legacy/test_old.py": {
                        "content": "def test_old(): pass\n",
                        "tests": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = scanner.scan()

    missing = [item for item in result["scripts"] if item["status"] == "missing"]
    assert missing and missing[0]["path"] == "legacy/test_old.py"
    assert missing[0]["group"] == "（根目录）"


# ----------------------------------------------------------------------
# coverage_report：缓存 / 坏命令 / 文件不在仓库内
# ----------------------------------------------------------------------
def make_coverage_project(tmp_path: Path, *, payload: dict | None = None) -> CoverageService:
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "demo" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    data_file = tmp_path / ".coverage.demo"
    data_file.write_text("fake", encoding="utf-8")
    report = payload if payload is not None else {
        "totals": {
            "num_statements": 2,
            "covered_lines": 1,
            "missing_lines": 1,
            "percent_covered": 50.0,
        },
        "files": {
            "demo/calc.py": {
                "summary": {
                    "num_statements": 2,
                    "missing_lines": 1,
                    "percent_covered": 50.0,
                },
                "missing_lines": [2],
            }
        },
    }
    script = tmp_path / "fake_coverage.py"
    script.write_text(
        "import json, sys\n"
        f"json.dump({report!r}, open(sys.argv[-1], 'w', encoding='utf-8'))\n",
        encoding="utf-8",
    )
    config = build_config(
        packages=(
            PackageSpec(
                name="demo",
                label="Demo",
                root=".",
                coverage_data_file=".coverage.demo",
                source_pkg="demo",
            ),
        ),
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        dist_dir=tmp_path / "dist",
        coverage_json_cmd=f'"{sys.executable}" "{script}"',
    )
    return CoverageService(config)


def test_coverage_report_uses_and_survives_cache(tmp_path: Path) -> None:
    service = make_coverage_project(tmp_path)
    package = service.config.packages[0]

    first = service.report(package)
    second = service.report(package)
    assert first["totals"]["percent"] == 50.0
    assert second["source_mtime_ns"] == first["source_mtime_ns"]

    # 缓存内容坏掉：当作没缓存，重新生成
    cache = service._cache_file(package)
    cache.write_text("{bad", encoding="utf-8")
    assert service.report(package)["totals"]["percent"] == 50.0


def test_coverage_report_reports_command_failure(tmp_path: Path) -> None:
    service = make_coverage_project(tmp_path)

    # 命令不存在：不能让 FileNotFoundError 穿透到 HTTP 层
    service.config = replace(
        service.config, coverage_json_cmd="definitely-not-a-real-command-xyz"
    )
    with pytest.raises(NoCoverageData, match="读取覆盖率失败"):
        service.report(service.config.packages[0])

    # 命令能跑但没产出报告：报清楚而不是返回空
    service.config = replace(
        service.config, coverage_json_cmd=f'"{sys.executable}" -c "pass"'
    )
    with pytest.raises(NoCoverageData, match="没有输出"):
        service.report(service.config.packages[0])


def test_coverage_report_points_at_missing_coveragerc(tmp_path: Path) -> None:
    service = make_coverage_project(tmp_path)
    package = replace(service.config.packages[0], coverage_config="missing.rc")
    service.config = replace(service.config, packages=(package,))
    (tmp_path / ".coverage.demo").unlink()

    with pytest.raises(NoCoverageData, match="覆盖率配置不存在"):
        service.report(package)


def test_coverage_missing_data_uses_hint(tmp_path: Path) -> None:
    service = make_coverage_project(tmp_path)
    (tmp_path / ".coverage.demo").unlink()
    with_hint = replace(service.config.packages[0], coverage_hint="make test-demo")
    service.config = replace(service.config, packages=(with_hint,))

    with pytest.raises(NoCoverageData, match="make test-demo"):
        service.report(with_hint)

    without_hint = replace(with_hint, coverage_hint=None)
    service.config = replace(service.config, packages=(without_hint,))
    with pytest.raises(NoCoverageData, match="该包的覆盖率测试命令"):
        service.report(without_hint)


def test_coverage_missing_data_with_existing_coveragerc(tmp_path: Path) -> None:
    """覆盖率配置存在、只是还没跑过：提示去跑，而不是说配置缺失。"""
    service = make_coverage_project(tmp_path)
    (tmp_path / ".coverage.demo").unlink()
    (tmp_path / ".coveragerc.demo").write_text("[run]\n", encoding="utf-8")
    package = replace(service.config.packages[0], coverage_config=".coveragerc.demo")
    service.config = replace(service.config, packages=(package,))

    with pytest.raises(NoCoverageData, match="还没有覆盖率数据"):
        service.report(package)


def test_coverage_file_detail_checks_repo_and_existence(tmp_path: Path) -> None:
    payload = {
        "totals": {"percent_covered": 100.0},
        "files": {
            "../outside.py": {"summary": {"percent_covered": 0.0}, "missing_lines": [1]},
            "demo/gone.py": {"summary": {"percent_covered": 0.0}, "missing_lines": [1]},
        },
    }
    service = make_coverage_project(tmp_path, payload=payload)
    package = service.config.packages[0]

    with pytest.raises(NoCoverageData, match="文件不在仓库内"):
        service.file_detail(package, "../outside.py")

    with pytest.raises(NoCoverageData, match="源文件不存在"):
        service.file_detail(package, "demo/gone.py")

    with pytest.raises(NoCoverageData, match="没有这个文件"):
        service.file_detail(package, "demo/unknown.py")
