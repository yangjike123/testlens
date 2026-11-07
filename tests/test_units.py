"""工具箱自身的单元测试。

全部用 tmp_path 构造隔离环境：不碰真实缓存、不连数据库、不跑业务测试，
所以可以单独 `make test-tools` 快速跑一遍。扫描/基线/历史都在假仓库里
（tmp_path 当 repo_root），顺带验证「packages 由调用方给出」的插件化契约。
"""

from __future__ import annotations

from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from testlens import PackageSpec, detect_packages
from testlens.cli import config_for_path
from testlens.config import (
    ConfigError,
    build_config,
    load_config,
    package_of,
    root_prefix,
    source_label,
)
# 别名：名字以 test_ 开头的函数被 import 进测试模块后，pytest 会当成用例收集
from testlens.config import test_files as list_test_files
from testlens.coverage_report import CoverageService, NoCoverageData
from testlens.history import HistoryStore
from testlens.junit import normalize_file, parse_files
from testlens.scan import Scanner, _diff_stats


def make_spec(name: str = "demo", root: str = "apps/demo", **kw) -> PackageSpec:
    return PackageSpec(name=name, label=name.title(), root=root, **kw)


def make_config(tmp_path: Path, specs: tuple[PackageSpec, ...] | list[PackageSpec]):
    for spec in specs:
        (tmp_path / spec.root / "test" / "unit").mkdir(parents=True, exist_ok=True)
    return build_config(
        packages=tuple(specs),
        repo_root=tmp_path,
        data_dir=tmp_path / "testlens.data",
        dist_dir=tmp_path / "dist",
    )


# ----------------------------------------------------------------------
# build_config 校验矩阵
# ----------------------------------------------------------------------
def test_build_config_rejects_bad_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="name"):
        build_config(packages=(make_spec(name="Bad_Name"),), repo_root=tmp_path)


def test_build_config_rejects_duplicate_root(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="重复"):
        build_config(
            packages=(make_spec("a", "apps/x"), make_spec("b", "apps/x")),
            repo_root=tmp_path,
        )


def test_build_config_rejects_nested_root(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="嵌套"):
        build_config(
            packages=(make_spec("a", "apps"), make_spec("b", "apps/x")),
            repo_root=tmp_path,
        )


def test_build_config_rejects_path_outside_repo_root(tmp_path: Path) -> None:
    # ../ 在“规范路径”检查处就被拦下，越界目录同样逃不过（resolver 后比对）
    with pytest.raises(ConfigError, match="规范路径"):
        build_config(packages=(make_spec("a", "../outside"),), repo_root=tmp_path)


def test_build_config_rejects_non_loopback_host(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="host"):
        build_config(
            packages=(make_spec("a", "apps/a"),), repo_root=tmp_path, host="0.0.0.0"
        )


def test_build_config_accepts_any_loopback_address(tmp_path: Path) -> None:
    """127.0.0.0/8 全段都是回环：127.1.1.1 这类地址也该允许，不必写死三个字符串。"""
    config = build_config(
        packages=(make_spec("a", "apps/a"),), repo_root=tmp_path, host="127.1.1.1"
    )

    assert config.host == "127.1.1.1"


def test_is_loopback_host() -> None:
    from testlens.config import is_loopback_host

    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("127.1.1.1")
    assert is_loopback_host("::1")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.1.10")
    assert not is_loopback_host("example.com")


def test_build_config_rejects_bad_port(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="port"):
        build_config(
            packages=(make_spec("a", "apps/a"),), repo_root=tmp_path, port=70000
        )


def test_build_config_rejects_tail_over_max(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="tail_lines"):
        build_config(
            packages=(make_spec("a", "apps/a"),),
            repo_root=tmp_path,
            tail_lines=99_999,
        )


# ----------------------------------------------------------------------
# Scanner
# ----------------------------------------------------------------------
def test_scanner_detects_new_then_changed(tmp_path: Path) -> None:
    config = make_config(tmp_path, (make_spec("demo", "apps/demo"),))
    test_file = tmp_path / "apps/demo/test/unit/test_demo.py"
    test_file.write_text("def test_one():\n    assert True\n", encoding="utf-8")

    scanner = Scanner(config)
    result = scanner.scan()
    assert result["total"] == 1
    assert result["scripts"][0]["status"] == "new"
    assert result["scripts"][0]["group"] == "unit"

    scanner.mark_baseline()
    assert scanner.scan()["changed"] == 0

    with open(test_file, "a", encoding="utf-8", newline="") as handle:
        handle.write("# touched\n")
    result = scanner.scan()
    assert result["changed"] == 1
    assert result["scripts"][0]["diff"] == {"added": 1, "removed": 0}
    assert result["scripts"][0]["group"] == "unit"


def test_scanner_reports_missing_after_delete(tmp_path: Path) -> None:
    config = make_config(tmp_path, (make_spec("demo", "apps/demo"),))
    test_file = tmp_path / "apps/demo/test/unit/test_demo.py"
    test_file.write_text("def test_one(): pass\n", encoding="utf-8")

    scanner = Scanner(config)
    scanner.mark_baseline()
    test_file.unlink()
    result = scanner.scan()
    assert [item["status"] for item in result["scripts"]] == ["missing"]
    assert result["scripts"][0]["changed"] is True


def test_diff_stats_counts_added_and_removed_lines() -> None:
    assert _diff_stats("a\nb\n", "a\nc\nd\n") == {"added": 2, "removed": 1}


def test_diff_stats_ignores_unified_diff_headers() -> None:
    assert _diff_stats("a\n", "a\n") == {"added": 0, "removed": 0}


# ----------------------------------------------------------------------
# HistoryStore
# ----------------------------------------------------------------------
def test_history_store_roundtrip(tmp_path: Path) -> None:
    config = make_config(tmp_path, (make_spec("a", "apps/a"),))
    store = HistoryStore(config)
    store.record(
        {"run_id": "20260101-000000-abcd", "package": "a", "title": "t"},
        "output text",
    )
    entry = store.find("20260101-000000-abcd")
    assert entry is not None and entry["title"] == "t"
    assert store.read_log("20260101-000000-abcd") == "output text"


def test_history_rejects_path_traversal(tmp_path: Path) -> None:
    config = make_config(tmp_path, (make_spec("a", "apps/a"),))
    store = HistoryStore(config)
    assert store.read_log("../../etc/passwd") is None
    assert store.find("../..") is None


# ----------------------------------------------------------------------
# CoverageService
# ----------------------------------------------------------------------
def test_coverage_service_disabled_without_spec(tmp_path: Path) -> None:
    config = make_config(tmp_path, (make_spec("a", "apps/a"),))
    service = CoverageService(config)
    with pytest.raises(NoCoverageData, match="未配置覆盖率"):
        service.report(config.packages[0])


def test_coverage_normalize_sorts_and_converts_windows_paths(tmp_path: Path) -> None:
    config = make_config(tmp_path, (make_spec("demo", "apps/demo"),))
    service = CoverageService(config)
    raw = {
        "totals": {
            "num_statements": 10,
            "covered_lines": 8,
            "missing_lines": 2,
            "num_branches": 0,
            "num_partial_branches": 0,
            "percent_covered": 80.0,
            "percent_branches_covered": 0.0,
        },
        "files": {
            "apps\\demo\\app\\low.py": {
                "summary": {
                    "num_statements": 5,
                    "missing_lines": 2,
                    "percent_covered": 60.0,
                },
                "missing_lines": [3, 4],
            },
            "apps\\demo\\app\\high.py": {
                "summary": {
                    "num_statements": 5,
                    "missing_lines": 0,
                    "percent_covered": 100.0,
                },
                "missing_lines": [],
            },
        },
    }
    result = service._normalize(config.packages[0], raw, 123, time.time())
    assert result["files"][0]["path"] == "apps/demo/app/low.py"
    assert result["files"][0]["percent"] == 60.0
    assert result["totals"]["percent"] == 80.0
    assert result["source_mtime_ns"] == 123


def test_blocks_merge_adjacent_context_windows() -> None:
    blocks = CoverageService._blocks([10, 12, 40], 100, 3)
    assert len(blocks) == 2
    assert (blocks[0]["start"], blocks[0]["end"]) == (7, 15)
    assert (blocks[1]["start"], blocks[1]["end"]) == (37, 43)


def test_blocks_clamp_to_file_bounds() -> None:
    blocks = CoverageService._blocks([1, 99], 100, 5)
    assert blocks[0]["start"] == 1
    assert blocks[-1]["end"] == 100


# ----------------------------------------------------------------------
# detect_packages 与 package_of
# ----------------------------------------------------------------------
def test_detect_packages_merges_given_presets(tmp_path: Path) -> None:
    for root in ("apps/web-service/test", "apps/other/test", "packages/utils/test"):
        (tmp_path / root).mkdir(parents=True, exist_ok=True)

    specs = detect_packages(
        tmp_path, coverage_presets={"web-service": {"source_pkg": "app"}}
    )
    names = [spec.name for spec in specs]
    assert names == ["other", "web-service", "utils"]

    known = next(spec for spec in specs if spec.name == "web-service")
    assert known.source_pkg == "app"  # 预设里合并了覆盖率定义
    unknown = next(spec for spec in specs if spec.name == "other")
    assert unknown.source_pkg is None  # 没给预设的包只拿到目录结构


def test_detect_packages_respects_explicit_presets(tmp_path: Path) -> None:
    (tmp_path / "apps/x/test").mkdir(parents=True)
    specs = detect_packages(tmp_path, coverage_presets={})
    assert specs[0].source_pkg is None


def test_package_of_matches_root_prefix() -> None:
    specs = (make_spec("a", "apps/a"), make_spec("b", "packages/b"))
    first = package_of(specs, "apps/a/test/unit/test_x.py")
    second = package_of(specs, "packages/b")
    assert first is not None and first.name == "a"
    assert second is not None and second.name == "b"
    assert package_of(specs, "apps/other/x.py") is None


# ----------------------------------------------------------------------
# junit 解析：路径归一化 / 统计 / 最慢与失败
# ----------------------------------------------------------------------
JUNIT_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="4" failures="1" errors="1" skipped="1" time="1.5">
    <testcase classname="test.unit.test_a" name="test_ok"
              file="test/unit/test_a.py" time="0.10"/>
    <testcase classname="test.unit.test_a" name="test_bad"
              file="test/unit/test_a.py" time="0.30">
      <failure message="AssertionError: boom">traceback line1
line2</failure>
    </testcase>
    <testcase classname="test.unit.test_b" name="test_skip"
              file="test/unit/test_b.py" time="0.01">
      <skipped message="no db"/>
    </testcase>
    <testcase classname="test.unit.test_b" name="test_err"
              file="test/unit/test_b.py" time="0.20">
      <error message="RuntimeError: conn refused">traceback</error>
    </testcase>
  </testsuite>
</testsuites>
"""


def test_junit_normalizes_relative_and_prefixed_paths() -> None:
    spec = make_spec("demo", "apps/demo")
    assert normalize_file("test/unit/test_a.py", spec) == "apps/demo/test/unit/test_a.py"
    assert normalize_file("./test/unit/test_a.py", spec) == "apps/demo/test/unit/test_a.py"
    assert normalize_file("apps/demo/test/unit/test_a.py", spec) == "apps/demo/test/unit/test_a.py"
    assert normalize_file(r"test\unit\test_a.py", spec) == "apps/demo/test/unit/test_a.py"
    assert normalize_file(None, spec) == ""


def test_junit_parse_totals_files_slowest_failures(tmp_path: Path) -> None:
    spec = make_spec("demo", "apps/demo")
    report = tmp_path / "run.xml"
    report.write_text(JUNIT_SAMPLE, encoding="utf-8")

    data = parse_files([report], spec, tmp_path)

    assert data is not None
    assert data["totals"] == {
        "tests": 4, "passed": 1, "failed": 1, "errors": 1, "skipped": 1, "time": 0.61,
    }

    files = {item["path"]: item for item in data["files"]}
    assert set(files) == {"apps/demo/test/unit/test_a.py", "apps/demo/test/unit/test_b.py"}
    assert files["apps/demo/test/unit/test_a.py"]["failed"] == 1
    assert files["apps/demo/test/unit/test_b.py"]["skipped"] == 1
    # 按耗时降序：test_a 合计 0.40 > test_b 0.21
    assert data["files"][0]["path"].endswith("test_a.py")

    assert data["slowest"][0]["name"] == "test_bad"
    assert data["slowest"][0]["time"] == 0.3
    assert len(data["slowest"]) == 4

    assert [item["name"] for item in data["failures"]] == ["test_bad", "test_err"]
    assert data["failures"][0]["message"] == "AssertionError: boom"


def test_junit_merges_multiple_reports_and_skips_broken(tmp_path: Path) -> None:
    spec = make_spec("demo", "apps/demo")
    first = tmp_path / "a.xml"
    first.write_text(JUNIT_SAMPLE, encoding="utf-8")
    second = tmp_path / "b.xml"
    second.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="test.x" name="test_ok2" file="test/x.py" time="0.05"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    broken = tmp_path / "c.xml"
    broken.write_text("<testsuites><testsuite>", encoding="utf-8")

    data = parse_files([first, second, broken, tmp_path / "missing.xml"], spec, tmp_path)

    assert data is not None
    assert data["totals"]["tests"] == 5
    assert data["totals"]["passed"] == 2


def test_junit_returns_none_when_nothing_parsable(tmp_path: Path) -> None:
    spec = make_spec("demo", "apps/demo")
    broken = tmp_path / "broken.xml"
    broken.write_text("not xml", encoding="utf-8")

    assert parse_files([broken, tmp_path / "missing.xml"], spec, tmp_path) is None


def test_junit_resolves_file_from_classname(tmp_path: Path) -> None:
    """pytest 的 junit 默认没有 file 属性，只能从 classname 反推（含类名那层）。"""
    spec = make_spec("demo", "apps/demo")
    target = tmp_path / "apps/demo/test/unit/test_mod.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")

    report = tmp_path / "r.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="test.unit.test_mod" name="test_a" time="0.1"/>'
        '<testcase classname="test.unit.test_mod.TestKlass" name="test_b" time="0.2"/>'
        '<testcase classname="unknown.module" name="test_c" time="0.05"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    data = parse_files([report], spec, tmp_path)

    assert data is not None
    files = {item["path"]: item for item in data["files"]}
    assert files["apps/demo/test/unit/test_mod.py"]["tests"] == 2


def test_build_config_falls_back_to_cwd(tmp_path: Path, monkeypatch, capsys) -> None:
    """不传 repo_root 时用当前工作目录，而不是插件自己的安装位置。"""
    monkeypatch.chdir(tmp_path)
    config = build_config(packages=(make_spec("demo", "apps/demo"),))

    assert config.repo_root == tmp_path.resolve()
    assert config.data_dir == tmp_path.resolve() / "testlens.data"
    capsys.readouterr()  # 目录不存在时的警告，不影响断言


def test_build_config_requires_packages() -> None:
    """插件不带包定义：一个都没有时直接报错，并在消息里说清怎么配。"""
    with pytest.raises(ConfigError, match="testlens.toml"):
        build_config(packages=())


def test_build_config_defaults_page_under_data(tmp_path: Path) -> None:
    """生成物收在 testlens.data/page：别在宿主项目根散 html/css/js 三个文件。"""
    (tmp_path / "apps/demo").mkdir(parents=True)
    config = build_config(packages=(make_spec("demo", "apps/demo"),), repo_root=tmp_path)

    assert config.dist_dir == tmp_path.resolve() / "testlens.data" / "page"
    assert config.static_page_name == "testlens.html"
    assert config.static_page_single is False


def test_open_browser_defaults_to_off(tmp_path: Path) -> None:
    """默认不自动打开页面：面板常在 IDE 终端里反复重启，每次弹标签页纯属打扰。"""
    (tmp_path / "apps/demo").mkdir(parents=True)
    config = build_config(packages=(make_spec("demo", "apps/demo"),), repo_root=tmp_path)

    assert config.open_browser is False


def test_build_config_resolves_relative_paths_against_repo_root(tmp_path: Path) -> None:
    (tmp_path / "apps/demo").mkdir(parents=True)
    config = build_config(
        packages=(make_spec("demo", "apps/demo"),),
        repo_root=tmp_path,
        data_dir=".cache",
        dist_dir="reports",
    )

    assert config.data_dir == tmp_path.resolve() / ".cache"
    assert config.dist_dir == tmp_path.resolve() / "reports"


def test_build_config_uses_repo_root_from_file_data(tmp_path: Path) -> None:
    """TOML 里给了 repo_root、调用方没显式传时，用文件里的值。"""
    (tmp_path / "apps/demo").mkdir(parents=True)

    config = build_config(
        packages=(make_spec("demo", "apps/demo"),),
        file_data={"repo_root": str(tmp_path)},
    )

    assert config.repo_root == tmp_path.resolve()
    assert config.data_dir == tmp_path.resolve() / "testlens.data"


# ----------------------------------------------------------------------
# TOML 配置与静态页输出
# ----------------------------------------------------------------------
def _write_toml(tmp_path: Path, extra: str = "") -> Path:
    (tmp_path / "apps/demo/test").mkdir(parents=True, exist_ok=True)
    path = tmp_path / "testlens.toml"
    path.write_text(
        '[page]\n'
        'dir = "reports"\n'
        'name = "report.html"\n'
        'single = true\n'
        '\n'
        '[server]\n'
        'port = 9123\n'
        '\n'
        '[[package]]\n'
        'name = "demo"\n'
        'label = "Demo"\n'
        'root = "apps/demo"\n'
        + extra,
        encoding="utf-8",
    )
    return path


def test_load_config_reads_page_section_and_repo_root(tmp_path: Path) -> None:
    config = load_config(_write_toml(tmp_path))

    # repo_root 取配置文件所在目录，所以从任意 cwd 调用都指向同一个项目
    assert config.repo_root == tmp_path.resolve()
    assert config.dist_dir == tmp_path.resolve() / "reports"
    assert config.static_page_name == "report.html"
    assert config.static_page_single is True
    assert config.port == 9123
    assert [pkg.name for pkg in config.packages] == ["demo"]


def test_open_browser_can_be_enabled_from_toml(tmp_path: Path) -> None:
    """想自动打开页面就写 [server] open_browser = true；不写 = 关闭。"""
    (tmp_path / "apps/demo/test").mkdir(parents=True)
    path = tmp_path / "testlens.toml"
    path.write_text(
        '[server]\nopen_browser = true\n\n'
        '[[package]]\nname = "demo"\nlabel = "Demo"\nroot = "apps/demo"\n',
        encoding="utf-8",
    )

    assert load_config(path).open_browser is True


def test_load_config_rejects_file_without_packages(tmp_path: Path) -> None:
    path = tmp_path / "testlens.toml"
    path.write_text('[server]\nport = 9123\n', encoding="utf-8")

    with pytest.raises(ConfigError, match=r"\[\[package\]\]"):
        load_config(path)


def test_load_config_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不存在"):
        load_config(tmp_path / "nope.toml")


def test_static_page_splits_into_three_files(tmp_path: Path) -> None:
    """默认输出：HTML 只留结构，样式与脚本各一个文件。"""
    from testlens.inventory import build_static_page

    config = make_config(tmp_path, [make_spec()])
    out = tmp_path / "testlens.html"

    files = build_static_page(Scanner(config), out)

    assert [path.name for path in files] == ["testlens.html", "testlens.css", "testlens.js"]
    html = out.read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="testlens.css">' in html
    assert '<script src="testlens.js"></script>' in html
    assert "<style>" not in html  # 样式不再内联
    assert len(html) < 12_000  # 结构文件应当很小
    assert 'window.__TESTLENS_DATA__' in html
    assert (tmp_path / "testlens.css").read_text(encoding="utf-8").strip()


def test_static_page_single_file_inlines_everything(tmp_path: Path) -> None:
    from testlens.inventory import build_static_page

    config = make_config(tmp_path, [make_spec()])
    out = tmp_path / "one.html"

    files = build_static_page(Scanner(config), out, single=True)

    assert files == [out]
    html = out.read_text(encoding="utf-8")
    assert "<style>" in html and "<script>" in html
    assert 'src="one.js"' not in html
    assert not (tmp_path / "one.css").exists()


def test_toolkit_exposes_public_entrypoints(tmp_path: Path) -> None:
    """宿主只该碰 build_config / PackageSpec / Toolkit，不用 import server。"""
    from testlens import Toolkit

    config = make_config(tmp_path, [make_spec()])
    toolkit = Toolkit(config)

    assert callable(toolkit.serve)
    result = toolkit.scan()
    assert {"scripts", "total", "changed"} <= set(result)


# ----------------------------------------------------------------------
# root="."：包根就是项目根（`testlens dev <目录>` 推断出来的形态）
# ----------------------------------------------------------------------
def test_self_root_matches_every_path(tmp_path: Path) -> None:
    spec = PackageSpec(name="solo", label="Solo", root=".")
    assert root_prefix(spec) == ""
    assert package_of((spec,), "test/unit/test_x.py") is spec
    assert package_of((spec,), "anything.py") is spec


def test_build_config_accepts_self_root(tmp_path: Path) -> None:
    (tmp_path / "test").mkdir()
    config = build_config(
        packages=(PackageSpec(name="solo", label="Solo", root="."),),
        repo_root=tmp_path,
    )
    assert config.packages[0].root == "."


def test_build_config_rejects_self_root_with_others(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="root 是"):
        build_config(
            packages=(
                PackageSpec(name="solo", label="Solo", root="."),
                make_spec("b", "apps/b"),
            ),
            repo_root=tmp_path,
        )


def test_scan_with_self_root_uses_project_test_dir(tmp_path: Path) -> None:
    target = tmp_path / "test" / "unit" / "test_a.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_one():\n    pass\n", encoding="utf-8")

    config = build_config(
        packages=(PackageSpec(name="solo", label="Solo", root="."),),
        repo_root=tmp_path,
    )
    result = Scanner(config).scan()

    assert result["total"] == 1
    item = result["scripts"][0]
    assert item["path"] == "test/unit/test_a.py"
    assert item["group"] == "unit"  # 分组键不能带 "./"
    assert item["tests"] == 1


def test_junit_normalize_keeps_path_with_self_root() -> None:
    spec = PackageSpec(name="solo", label="Solo", root=".")
    assert normalize_file("test/unit/test_a.py", spec) == "test/unit/test_a.py"
    assert normalize_file("./test/unit/test_a.py", spec) == "test/unit/test_a.py"


def test_config_for_path_infers_single_package(tmp_path: Path) -> None:
    (tmp_path / "test").mkdir()
    config = config_for_path(str(tmp_path), None)

    assert config.repo_root == tmp_path.resolve()
    assert config.packages[0].root == "."
    assert config.packages[0].label == tmp_path.name
    # 静态页收进 testlens.data，不散落在项目根
    assert config.dist_dir == tmp_path.resolve() / "testlens.data" / "page"


def test_config_for_path_prefers_toml_if_present(tmp_path: Path) -> None:
    path = _write_toml(tmp_path)
    config = config_for_path(str(tmp_path), None)

    assert config.repo_root == path.parent.resolve()
    assert [pkg.name for pkg in config.packages] == ["demo"]


# ----------------------------------------------------------------------
# 配置来源：面板标题与启动横幅都要能说清「读的是哪份配置」
# ----------------------------------------------------------------------
def test_load_config_records_source(tmp_path: Path) -> None:
    path = _write_toml(tmp_path)

    assert load_config(path).source == str(path.resolve())


def test_source_label_is_relative_to_base(tmp_path: Path) -> None:
    path = _write_toml(tmp_path)
    config = load_config(path)

    assert source_label(config, tmp_path) == "testlens.toml"
    # base 不是它的上级（比如从子目录跑），给绝对路径，别输出 ../..
    assert source_label(config, tmp_path / "sub") == path.resolve().as_posix()


def test_source_label_is_empty_without_config_file(tmp_path: Path) -> None:
    config = make_config(tmp_path, [make_spec()])

    assert config.source is None
    assert source_label(config) == ""


def test_config_for_path_dir_uses_module_config(tmp_path: Path) -> None:
    """给目录时也认 testlens_config.py：应用自己的端口/覆盖率不该被忽略。"""
    (tmp_path / "test").mkdir()
    module = tmp_path / "testlens_config.py"
    module.write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "config = build_config(\n"
        "    packages=(PackageSpec(name='solo', label='Solo', root='.'),),\n"
        "    repo_root=Path(__file__).parent,\n"
        "    port=9666,\n"
        ")\n",
        encoding="utf-8",
    )

    config = config_for_path(str(tmp_path), None)

    assert config.port == 9666
    assert config.source == str(module.resolve())
    assert [pkg.name for pkg in config.packages] == ["solo"]


def test_config_for_path_prefers_toml_over_module(tmp_path: Path) -> None:
    path = _write_toml(tmp_path)
    (tmp_path / "testlens_config.py").write_text(
        "raise RuntimeError('有 toml 就不该加载我')\n", encoding="utf-8"
    )

    config = config_for_path(str(tmp_path), None)

    assert [pkg.name for pkg in config.packages] == ["demo"]
    assert config.source == str(path.resolve())


def test_config_for_path_inferred_dir_has_no_source(tmp_path: Path) -> None:
    (tmp_path / "test").mkdir()

    assert config_for_path(str(tmp_path), None).source is None


def test_build_payload_reports_config_file(tmp_path: Path) -> None:
    from dataclasses import replace

    from testlens.inventory import build_payload

    source = tmp_path / "testlens.toml"
    config = replace(make_config(tmp_path, [make_spec()]), source=str(source))
    payload = build_payload(Scanner(config))

    assert payload["config"]["path"] == str(source)
    assert payload["config"]["repo_root"] == str(tmp_path.resolve())
    assert payload["config"]["package_count"] == 1


def test_build_payload_without_config_file(tmp_path: Path) -> None:
    from testlens.inventory import build_payload

    payload = build_payload(Scanner(make_config(tmp_path, [make_spec()])))

    assert payload["config"]["path"] == ""  # 页面显示「配置 无文件」


# ----------------------------------------------------------------------
# dev 加载 .py 里的 config
# ----------------------------------------------------------------------
def test_config_from_module_reads_config(tmp_path: Path) -> None:
    (tmp_path / "test").mkdir()
    module = tmp_path / "dev_config.py"
    module.write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "config = build_config(\n"
        "    packages=(PackageSpec(name='solo', label='Solo', root='.'),),\n"
        "    repo_root=Path(__file__).parent,\n"
        "    port=9555,\n"
        ")\n",
        encoding="utf-8",
    )

    config = config_for_path(str(module), None)

    assert config.port == 9555
    assert config.repo_root == tmp_path.resolve()
    assert config.packages[0].root == "."


def test_config_from_module_without_config_raises(tmp_path: Path) -> None:
    """严格版仍要求有 config：--config 传错文件必须报错，而不是猜。"""
    from testlens.cli import config_from_module

    module = tmp_path / "empty.py"
    module.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="没有找到 config"):
        config_from_module(module)


def test_dev_with_entry_script_uses_directory_config(tmp_path: Path) -> None:
    """dev <脚本.py>：脚本不是配置文件时，按它所在目录起面板（像 fastapi dev app/main.py）。"""
    (tmp_path / "test").mkdir()
    (tmp_path / "testlens.toml").write_text(
        '[server]\nport = 9123\n\n[[package]]\nname = "solo"\nlabel = "Solo"\nroot = "."\n',
        encoding="utf-8",
    )
    script = tmp_path / "plugin_embed.py"
    script.write_text("def main() -> int:\n    return 0\n", encoding="utf-8")

    config = config_for_path(str(script), None)

    assert config.port == 9123
    assert config.repo_root == tmp_path.resolve()
    assert config.source == str((tmp_path / "testlens.toml").resolve())


def test_dev_with_entry_script_falls_back_to_inference(tmp_path: Path) -> None:
    """目录里也没配置时继续退回推断：一个包、root='.'，不是直接报错。"""
    (tmp_path / "test").mkdir()
    script = tmp_path / "main.py"
    script.write_text("x = 1\n", encoding="utf-8")

    config = config_for_path(str(script), None)

    assert config.source is None
    assert config.repo_root == tmp_path.resolve()
    assert len(config.packages) == 1
    assert config.packages[0].root == "."


def test_dev_entry_script_searches_up_for_config(tmp_path: Path) -> None:
    """指到子目录里的文件也行：向上找到项目根的配置，而不是拿 test/ 当仓库根。"""
    (tmp_path / "test").mkdir()
    (tmp_path / "testlens.toml").write_text(
        '[server]\nport = 9123\n\n[[package]]\nname = "solo"\nlabel = "Solo"\nroot = "."\n',
        encoding="utf-8",
    )
    script = tmp_path / "test" / "test_cart.py"
    script.write_text("def test_one():\n    pass\n", encoding="utf-8")

    config = config_for_path(str(script), None)

    assert config.repo_root == tmp_path.resolve()
    assert config.port == 9123
    assert config.source == str((tmp_path / "testlens.toml").resolve())


def test_config_module_edits_take_effect_immediately(tmp_path: Path) -> None:
    """配置文件改一行就该生效：不能因为 mtime/大小没变而执行到旧字节码。

    两次写入长度相同、间隔极短，正好是 .pyc 失效判断最容易漏掉的场景。
    """
    from testlens.cli import config_from_module

    (tmp_path / "test").mkdir()
    module = tmp_path / "cfg.py"
    template = (
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "config = build_config(packages=(PackageSpec(name='solo', label='Solo', root='.'),),"
        " repo_root=Path(__file__).parent, port={port})\n"
    )

    module.write_text(template.format(port=9001), encoding="utf-8")
    assert config_from_module(module).port == 9001

    module.write_text(template.format(port=9002), encoding="utf-8")
    assert config_from_module(module).port == 9002


def test_dev_without_config_infers_coverage(tmp_path: Path) -> None:
    """完全没有配置时：只有一个顶层包，就把它的 source_pkg 也推出来，覆盖率可用。"""
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "test").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\ndata_file = ".coverage.demo"\n', encoding="utf-8"
    )
    script = tmp_path / "plugin_embed.py"
    script.write_text("x = 1\n", encoding="utf-8")

    config = config_for_path(str(script), None)

    assert config.source is None
    package = config.packages[0]
    assert package.source_pkg == "shop"
    assert package.coverage_data_file == ".coverage.demo"
    assert "--cov=shop" in package.coverage_args


def test_infer_spec_does_not_guess_multiple_packages(tmp_path: Path) -> None:
    """有多个顶层包就不猜 source_pkg：猜错会把别的包算进覆盖率。"""
    from testlens.cli import infer_spec

    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text("", encoding="utf-8")

    assert infer_spec(tmp_path).source_pkg is None


def test_warm_coverage_explains_when_nothing_configured(tmp_path: Path, capsys) -> None:
    """--warm-coverage 但没有任何包配覆盖率时，要说明原因，别静默什么都不做。"""
    from testlens.server import warm_coverage
    from testlens.toolkit import Toolkit

    (tmp_path / "test").mkdir()
    config = build_config(
        packages=(PackageSpec(name="solo", label="Solo", root="."),),
        repo_root=tmp_path,
        coverage_warmup=True,
    )

    assert warm_coverage(Toolkit(config)) == []
    assert "跳过预热" in capsys.readouterr().out


def test_dev_prefers_module_config_over_directory_toml(tmp_path: Path) -> None:
    """.py 里真有 config 时以它为准，别被同目录的 toml 抢走。"""
    (tmp_path / "test").mkdir()
    (tmp_path / "testlens.toml").write_text(
        '[server]\nport = 9123\n\n[[package]]\nname = "solo"\nlabel = "Solo"\nroot = "."\n',
        encoding="utf-8",
    )
    module = tmp_path / "cfg.py"
    module.write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "config = build_config(packages=(PackageSpec(name='solo', label='Solo', root='.'),),"
        " repo_root=Path(__file__).parent, port=9555)\n",
        encoding="utf-8",
    )

    assert config_for_path(str(module), None).port == 9555


def test_dev_still_raises_when_entry_script_cannot_import(tmp_path: Path) -> None:
    """脚本导入失败是脚本自己的错，不能当成“没有 config”悄悄退回目录推断。"""
    script = tmp_path / "broken.py"
    script.write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="boom"):
        config_for_path(str(script), None)


def test_config_from_module_reports_import_failure(tmp_path: Path) -> None:
    module = tmp_path / "broken.py"
    module.write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="boom"):
        config_for_path(str(module), None)


def test_missing_coverage_config_is_reported(tmp_path: Path) -> None:
    spec = make_spec(
        "demo",
        "apps/demo",
        source_pkg="demo",
        coverage_data_file=".coverage.demo",
        coverage_config=".coveragerc.missing",
    )
    config = make_config(tmp_path, [spec])

    message = CoverageService(config)._missing_message(spec)

    assert "覆盖率配置不存在" in message


def test_config_for_path_uses_data_page_by_default(tmp_path: Path) -> None:
    """dev 与 page 用同一个默认目录：配置里没写 [page] dir 就是 testlens.data/page。"""
    from testlens.cli import config_from_module

    (tmp_path / "test").mkdir()
    module = tmp_path / "cfg.py"
    module.write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "config = build_config(packages=(PackageSpec(name='solo', label='Solo', root='.'),),"
        " repo_root=Path(__file__).parent)\n",
        encoding="utf-8",
    )

    expected = tmp_path.resolve() / "testlens.data" / "page"
    assert config_from_module(module).dist_dir == expected
    assert config_for_path(str(module), None).dist_dir == expected


def test_config_for_path_keeps_explicit_dist_dir(tmp_path: Path) -> None:
    (tmp_path / "test").mkdir()
    module = tmp_path / "cfg.py"
    module.write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "root = Path(__file__).parent\n"
        "config = build_config(packages=(PackageSpec(name='solo', label='Solo', root='.'),),"
        " repo_root=root, dist_dir=root / 'out')\n",
        encoding="utf-8",
    )

    config = config_for_path(str(module), None)
    assert config.dist_dir == tmp_path.resolve() / "out"


def test_config_from_module_accepts_toolkit(tmp_path: Path) -> None:
    """模块里给的是 toolkit 也行：取它的 config。"""
    from testlens.cli import config_from_module

    (tmp_path / "test").mkdir()
    module = tmp_path / "cfg.py"
    module.write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, Toolkit, build_config\n"
        "toolkit = Toolkit(build_config(packages=(PackageSpec(name='solo', label='Solo', root='.'),),"
        " repo_root=Path(__file__).parent))\n",
        encoding="utf-8",
    )

    config = config_from_module(module)
    assert config.packages[0].name == "solo"


# ----------------------------------------------------------------------
# CLI 配置入口：--config 认 .py，dev 推断不到脚本时直接报错
# ----------------------------------------------------------------------
def _write_module_config(directory: Path, *, port: int = 9555) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    module = directory / "testlens_config.py"
    module.write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "config = build_config(\n"
        "    packages=(PackageSpec(name='solo', label='Solo', root='.'),),\n"
        "    repo_root=Path(__file__).parent,\n"
        f"    port={port},\n"
        ")\n",
        encoding="utf-8",
    )
    return module


def test_load_cli_config_accepts_python_module(tmp_path: Path) -> None:
    """--config 收 .py：不用在仓库根再写一份 toml 也能跑 list / plan / serve。"""
    from testlens.cli import load_cli_config

    module = _write_module_config(tmp_path)
    config = load_cli_config(str(module))

    assert config.port == 9555
    assert config.source == str(module.resolve())


def test_load_cli_config_rejects_unknown_suffix(tmp_path: Path) -> None:
    from testlens.cli import load_cli_config

    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ConfigError, match="只支持"):
        load_cli_config(str(path))


def test_browser_flag_parses_both_ways() -> None:
    """--browser / --no-browser 只覆盖这一次；不写 = None，听配置（默认关）。"""
    from testlens.cli import build_parser

    parser = build_parser()

    assert parser.parse_args(["serve"]).browser is None
    assert parser.parse_args(["serve", "--browser"]).browser is True
    assert parser.parse_args(["serve", "--no-browser"]).browser is False
    assert parser.parse_args(["dev", "--browser"]).browser is True


def test_serve_browser_flag_reaches_toolkit(tmp_path: Path, monkeypatch) -> None:
    """serve 的两个开关要真的落到 config.open_browser 上，而不只是被解析出来。"""
    from testlens import server as server_module
    from testlens.cli import main

    _write_module_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    seen: dict[str, bool] = {}

    def fake_run(toolkit) -> int:
        seen["open_browser"] = toolkit.config.open_browser
        return 0

    monkeypatch.setattr(server_module, "run", fake_run)

    assert main(["serve"]) == 0
    assert seen["open_browser"] is False  # 默认关闭
    assert main(["serve", "--browser"]) == 0
    assert seen["open_browser"] is True
    assert main(["serve", "--no-browser"]) == 0
    assert seen["open_browser"] is False

    # dev 走的是 config_for_path 那条路，同一个开关也要落到 config 上
    assert main(["dev", str(tmp_path), "--browser"]) == 0
    assert seen["open_browser"] is True


def test_find_config_prefers_toml_then_module(tmp_path: Path, monkeypatch) -> None:
    from testlens.cli import find_config

    monkeypatch.chdir(tmp_path)
    _write_module_config(tmp_path)
    assert find_config().name == "testlens_config.py"

    (tmp_path / "testlens.toml").write_text("[server]\nport = 9123\n", encoding="utf-8")
    assert find_config().name == "testlens.toml"  # 两个都在时 toml 优先


def test_find_config_error_lists_both_names(tmp_path: Path, monkeypatch) -> None:
    from testlens.cli import find_config

    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="testlens_config.py"):
        find_config()


def test_dev_without_config_fails_with_hint(tmp_path: Path, monkeypatch, capsys) -> None:
    """推断出 0 个脚本时别起空面板：报错并说清该指向哪。"""
    from testlens.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["dev", str(tmp_path), "--no-browser"]) == 2

    err = capsys.readouterr().err
    assert "没有可用的测试配置" in err
    assert "testlens dev apps/web-service" in err


def test_dev_hint_lists_nearby_configs(tmp_path: Path, monkeypatch, capsys) -> None:
    from testlens.cli import main

    _write_module_config(tmp_path / "apps" / "web-service")
    monkeypatch.chdir(tmp_path)

    assert main(["dev", str(tmp_path), "--no-browser"]) == 2

    err = capsys.readouterr().err
    assert "apps/web-service/testlens_config.py" in err


def test_config_flag_works_before_and_after_subcommand() -> None:
    """`--config` 写在子命令前后都得认，且不能被子命令的默认值抹掉。"""
    from testlens.cli import build_parser

    before = build_parser().parse_args(["--config", "x.py", "plan"])
    after = build_parser().parse_args(["plan", "--config", "x.py"])

    assert before.config == "x.py"
    assert after.config == "x.py"
    assert build_parser().parse_args(["plan"]).config is None


# ----------------------------------------------------------------------
# 覆盖率默认值推导：只写 source_pkg，其余四项按项目现状补齐
# ----------------------------------------------------------------------
def _write_coverage_project(tmp_path: Path) -> None:
    """仿本仓库：两个包、两份 rc、一个 Makefile 指明 --cov-config。"""
    (tmp_path / "apps/web-service/test").mkdir(parents=True)
    (tmp_path / "packages/utils/test").mkdir(parents=True)
    (tmp_path / ".coveragerc.web").write_text(
        "[run]\ndata_file = .coverage.web\nbranch = True\n", encoding="utf-8"
    )
    (tmp_path / ".coveragerc.utils").write_text(
        "[run]\ndata_file = .coverage.utils\n", encoding="utf-8"
    )
    (tmp_path / "Makefile").write_text(
        "test-web:\n"
        "\tuv run pytest apps/web-service/test/ -q "
        "--cov-config=.coveragerc.web --cov=app --cov-report=\n"
        "\n"
        "test-utils:\n"
        "\tuv run pytest packages/utils/test/ -q "
        "--cov-config=.coveragerc.utils --cov=upload\n",
        encoding="utf-8",
    )


def test_derives_all_coverage_fields_from_makefile_and_rc(tmp_path: Path) -> None:
    _write_coverage_project(tmp_path)
    config = build_config(
        packages=(
            PackageSpec(
                name="web-service", label="Web", root="apps/web-service", source_pkg="app"
            ),
        ),
        repo_root=tmp_path,
    )

    pkg = config.packages[0]
    assert pkg.coverage_config == ".coveragerc.web"
    assert pkg.coverage_data_file == ".coverage.web"
    assert pkg.coverage_args == (
        "--cov-config=.coveragerc.web",
        "--cov=app",
        "--cov-report=",
    )
    assert pkg.coverage_hint == "make test-web"


def test_derived_paths_are_relative_to_app_repo_root(tmp_path: Path) -> None:
    """应用级配置（repo_root=apps/web-service）要往上找到仓库根的配置与数据文件。"""
    _write_coverage_project(tmp_path)
    app = tmp_path / "apps" / "web-service"

    config = build_config(
        packages=(PackageSpec(name="web-service", label="Web", root=".", source_pkg="app"),),
        repo_root=app,
    )

    pkg = config.packages[0]
    assert pkg.coverage_config == "../../.coveragerc.web"
    assert pkg.coverage_data_file == "../../.coverage.web"
    assert pkg.coverage_args[0] == "--cov-config=../../.coveragerc.web"


def test_derived_coverage_falls_back_to_rc_token_match(tmp_path: Path) -> None:
    """没有 Makefile 时按包名后缀挑 rc：web-service → .coveragerc.web。"""
    (tmp_path / "apps/web-service/test").mkdir(parents=True)
    for name in ("web", "utils"):
        (tmp_path / f".coveragerc.{name}").write_text(
            f"[run]\ndata_file = .coverage.{name}\n", encoding="utf-8"
        )

    config = build_config(
        packages=(
            PackageSpec(name="web-service", label="Web", root="apps/web-service", source_pkg="app"),
        ),
        repo_root=tmp_path,
    )

    pkg = config.packages[0]
    assert pkg.coverage_config == ".coveragerc.web"
    assert pkg.coverage_data_file == ".coverage.web"
    # 没有 Makefile 就给一条等价的 pytest 命令
    assert pkg.coverage_hint == "uv run pytest apps/web-service/test/ --cov=app"


def test_derived_coverage_ignores_ambiguous_rc(tmp_path: Path) -> None:
    """多份 rc 都对不上包名时宁可不用，别去读别的包的数据文件。"""
    (tmp_path / "apps/demo/test").mkdir(parents=True)
    for name in ("web", "utils"):
        (tmp_path / f".coveragerc.{name}").write_text(
            f"[run]\ndata_file = .coverage.{name}\n", encoding="utf-8"
        )

    config = build_config(
        packages=(PackageSpec(name="api", label="API", root="apps/demo", source_pkg="app"),),
        repo_root=tmp_path,
    )

    pkg = config.packages[0]
    assert pkg.coverage_config is None
    # 读不到配置时按包目录名分开：apps/demo → .coverage.demo
    assert pkg.coverage_data_file == ".coverage.demo"
    assert pkg.coverage_args == ("--cov=app", "--cov-report=")


def test_explicit_coverage_fields_are_not_overwritten(tmp_path: Path) -> None:
    _write_coverage_project(tmp_path)
    config = build_config(
        packages=(
            PackageSpec(
                name="web-service",
                label="Web",
                root="apps/web-service",
                source_pkg="app",
                coverage_data_file="custom.data",
                coverage_args=("--cov=app",),
            ),
        ),
        repo_root=tmp_path,
    )

    pkg = config.packages[0]
    assert pkg.coverage_data_file == "custom.data"  # 显式值不被推导覆盖
    assert pkg.coverage_args == ("--cov=app",)  # 不再追加 --cov-report 等
    # 配置没显式给，但 Makefile 里能定位到：填上只为「配置不存在」的报错有指向
    assert pkg.coverage_config == ".coveragerc.web"


def test_without_source_pkg_coverage_stays_disabled(tmp_path: Path) -> None:
    _write_coverage_project(tmp_path)
    config = build_config(
        packages=(PackageSpec(name="web-service", label="Web", root="apps/web-service"),),
        repo_root=tmp_path,
    )

    pkg = config.packages[0]
    assert pkg.coverage_data_file is None
    assert pkg.coverage_args == ()
    assert pkg.coverage_hint is None


def test_rc_without_data_file_uses_coverage_default(tmp_path: Path) -> None:
    (tmp_path / "apps/demo/test").mkdir(parents=True)
    (tmp_path / ".coveragerc").write_text("[run]\nbranch = True\n", encoding="utf-8")

    config = build_config(
        packages=(PackageSpec(name="demo", label="Demo", root="apps/demo", source_pkg="demo"),),
        repo_root=tmp_path,
    )

    pkg = config.packages[0]
    assert pkg.coverage_config == ".coveragerc"
    assert pkg.coverage_data_file == ".coverage.demo"


def test_derived_coverage_reads_pyproject_data_file(tmp_path: Path) -> None:
    """没有 .coveragerc、覆盖率配置写在 pyproject 里时也要能找到数据文件。"""
    (tmp_path / "apps/demo/test").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.coverage.run]\n"
        'data_file = ".coverage.demo"\n'
        "branch = true\n",
        encoding="utf-8",
    )

    config = build_config(
        packages=(PackageSpec(name="demo", label="Demo", root="apps/demo", source_pkg="demo"),),
        repo_root=tmp_path,
    )

    pkg = config.packages[0]
    assert pkg.coverage_data_file == ".coverage.demo"
    assert pkg.coverage_config is None  # pyproject 不需要额外传 --cov-config
    assert pkg.coverage_args == ("--cov=demo", "--cov-report=")


# ----------------------------------------------------------------------
# 启动时补齐覆盖率数据：文件在就跳过，不在才跑一遍
# ----------------------------------------------------------------------
def test_load_config_reads_coverage_warmup(tmp_path: Path) -> None:
    """[coverage] warmup 配什么读什么（不依赖全局默认值是 on 还是 off）。"""
    path = _write_toml(tmp_path, extra="")
    path.write_text(
        path.read_text(encoding="utf-8") + "\n[coverage]\nwarmup = false\n",
        encoding="utf-8",
    )
    assert load_config(path).coverage_warmup is False

    path.write_text(
        path.read_text(encoding="utf-8").replace("warmup = false", "warmup = true"),
        encoding="utf-8",
    )
    assert load_config(path).coverage_warmup is True


def test_warm_coverage_only_runs_for_missing_files(tmp_path: Path, monkeypatch) -> None:
    from testlens.server import warm_coverage
    from testlens.toolkit import Toolkit

    (tmp_path / "apps/demo/test").mkdir(parents=True)
    (tmp_path / "packages/util/test").mkdir(parents=True)
    (tmp_path / ".coverage.present").write_text("", encoding="utf-8")

    config = build_config(
        packages=(
            PackageSpec(
                name="demo",
                label="Demo",
                root="apps/demo",
                source_pkg="demo",
                coverage_data_file=".coverage.present",
                coverage_args=("--cov=demo", "--cov-report="),
            ),
            PackageSpec(
                name="util",
                label="Util",
                root="packages/util",
                source_pkg="util",
                coverage_data_file=".coverage.missing",
                coverage_args=("--cov=util", "--cov-report="),
            ),
        ),
        repo_root=tmp_path,
    )

    calls: list[tuple[list[str], Path]] = []

    def fake_run(
        command: list[str],
        cwd: Path,
        timeout: float | None = None,
        check: bool = False,
        env: dict | None = None,
    ) -> SimpleNamespace:
        # 必须把数据写到「这个包该读的文件」，不是 cwd 下的 .coverage
        assert env is not None and env["COVERAGE_FILE"].endswith(".coverage.missing")
        calls.append((list(command), Path(cwd)))
        (Path(cwd) / ".coverage.missing").write_text("", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("testlens.server.subprocess.run", fake_run)

    warmed = warm_coverage(Toolkit(config))

    # 已经存在数据文件的 demo 不跑，只补 util
    assert warmed == ["util"]
    assert len(calls) == 1
    command, cwd = calls[0]
    assert "packages/util/test" in command
    assert "--cov=util" in command
    assert "--cov=demo" not in command
    assert cwd == tmp_path.resolve()


def test_warm_coverage_is_noop_when_everything_exists(tmp_path: Path, monkeypatch) -> None:
    from testlens.server import warm_coverage
    from testlens.toolkit import Toolkit

    (tmp_path / "apps/demo/test").mkdir(parents=True)
    (tmp_path / ".coverage.demo").write_text("", encoding="utf-8")

    config = build_config(
        packages=(
            PackageSpec(
                name="demo",
                label="Demo",
                root="apps/demo",
                source_pkg="demo",
                coverage_data_file=".coverage.demo",
                coverage_args=("--cov=demo",),
            ),
        ),
        repo_root=tmp_path,
    )

    monkeypatch.setattr(
        "testlens.server.subprocess.run",
        lambda *args, **kwargs: pytest.fail("数据文件已存在，不该再跑测试"),
    )

    assert warm_coverage(Toolkit(config)) == []


def test_warm_coverage_flag_parses() -> None:
    from testlens.cli import build_parser

    assert build_parser().parse_args(["dev"]).warm_coverage is None
    assert build_parser().parse_args(["dev", "--warm-coverage"]).warm_coverage is True
    assert build_parser().parse_args(["dev", "--no-warm-coverage"]).warm_coverage is False
    assert build_parser().parse_args(["serve", "--warm-coverage"]).warm_coverage is True


def test_missing_coveragerc_uses_package_dir_name(tmp_path: Path) -> None:
    """Makefile 指明 --cov-config 但文件被删：数据文件按包目录最后一段命名。"""
    (tmp_path / "apps/web-service/test").mkdir(parents=True)
    (tmp_path / "packages/utils/test").mkdir(parents=True)
    (tmp_path / "Makefile").write_text(
        "test-web:\n"
        "\tuv run pytest apps/web-service/test/ -q "
        "--cov-config=.coveragerc.web --cov=app\n"
        "\n"
        "test-utils:\n"
        "\tuv run pytest packages/utils/test/ -q "
        "--cov-config=.coveragerc.utils --cov=upload\n",
        encoding="utf-8",
    )

    config = build_config(
        packages=(
            PackageSpec(
                name="web-service", label="Web", root="apps/web-service", source_pkg="app"
            ),
            PackageSpec(name="utils", label="Util", root="packages/utils", source_pkg="upload"),
        ),
        repo_root=tmp_path,
    )

    web, util = config.packages
    # 每个包按自己的目录名一份数据文件：apps/web-service → web-service，packages/utils → utils
    assert web.coverage_data_file == ".coverage.web-service"
    assert util.coverage_data_file == ".coverage.utils"
    # 配置名仍然报出去，覆盖率页签能明确说「配置不存在」
    assert web.coverage_config == ".coveragerc.web"
    # 不存在的配置不能传给 coverage，否则连数据都不会生成
    assert "--cov-config=.coveragerc.web" not in web.coverage_args


def test_run_commands_keep_pytest_temp_under_data_dir(tmp_path: Path) -> None:
    """pytest 的会话临时目录收进 testlens.data/tmp，别去动系统临时目录。"""
    from testlens import Toolkit

    config = make_config(tmp_path, [make_spec("demo", "apps/demo")])
    commands = Toolkit(config).runner.build_commands(["apps/demo/test/unit/test_a.py"])

    name, args = commands[0]
    assert name == "demo"
    basetemp = [arg for arg in args if arg.startswith("--basetemp=")]
    assert basetemp
    expected = tmp_path.resolve() / "testlens.data" / "tmp" / "pytest-demo"
    assert basetemp[0].split("=", 1)[1] == str(expected)


def test_stop_finds_process_by_run_id(tmp_path: Path, monkeypatch) -> None:
    """进程句柄按 run_id 记：同包停止后立刻重跑，旧线程收尾不能误删新运行的句柄。"""
    from testlens import Toolkit
    from testlens.runner import Run

    config = make_config(tmp_path, [make_spec("demo", "apps/demo")])
    manager = Toolkit(config).runner
    run = Run(run_id="20260101-000000-abcd", package="demo", title="t", paths=[])
    run.status = "running"
    manager._runs["demo"] = run

    killed: list[object] = []
    monkeypatch.setattr("testlens.runner.kill_tree", lambda process: killed.append(process))
    process = object()
    manager._processes[run.run_id] = process

    assert manager.stop(run.run_id) is True
    assert killed == [process]
    # stop 只负责杀，句柄由执行线程的 finally 自己清；否则新旧运行会互相踩
    assert manager._processes.get(run.run_id) is process


def test_toolkit_construction_does_not_clean_tmp(tmp_path: Path) -> None:
    """list / page 也会构造 Toolkit：只是列个清单不该顺手删文件。"""
    import os

    from testlens import Toolkit

    config = make_config(tmp_path, [make_spec("demo", "apps/demo")])
    tmp = config.data_dir / "tmp"
    tmp.mkdir(parents=True)
    stale = tmp / "pytest-1.xml"
    stale.write_text("<testsuite/>", encoding="utf-8")
    old = time.time() - 2 * 86_400
    os.utime(stale, (old, old))

    toolkit = Toolkit(config)
    assert stale.exists()

    # 真正要跑之前才清：只删一天前的残留
    toolkit.runner._cleanup_tmp_once()
    assert not stale.exists()


def test_scan_reuses_cached_test_count(tmp_path: Path, monkeypatch) -> None:
    """内容没变就不重新 ast.parse：面板每次刷新都要扫全部脚本。"""
    config = make_config(tmp_path, [make_spec("demo", "apps/demo")])
    script = tmp_path / "apps/demo/test/unit/test_a.py"
    script.write_text("def test_one():\n    pass\n\ndef test_two():\n    pass\n", encoding="utf-8")
    scanner = Scanner(config)
    scanner.mark_baseline(None)

    monkeypatch.setattr(
        "testlens.scan.count_tests",
        lambda *args, **kwargs: pytest.fail("内容没变还去解析 AST"),
    )
    result = scanner.scan()
    assert [item["tests"] for item in result["scripts"]] == [2]

    # 内容真的变了就必须重新数，不能吃旧缓存
    monkeypatch.undo()
    script.write_text("def test_only():\n    pass\n", encoding="utf-8")
    changed = [item for item in scanner.scan()["scripts"] if item["path"].endswith("test_a.py")]
    assert changed[0]["status"] == "changed"
    assert changed[0]["tests"] == 1


def test_build_static_page_accepts_precomputed_payload(tmp_path: Path, monkeypatch) -> None:
    """传了 payload 就不再扫仓库：testlens page 靠这个只扫一遍。"""
    from testlens.inventory import build_payload, build_static_page

    config = make_config(tmp_path, [make_spec("demo", "apps/demo")])
    scanner = Scanner(config)
    payload = build_payload(scanner)

    monkeypatch.setattr(
        Scanner, "scan", lambda self, packages=None: pytest.fail("传了 payload 不该再扫")
    )
    files = build_static_page(scanner, tmp_path / "out.html", payload=payload)

    assert files[0].is_file()


def test_demo_project_config_stays_valid() -> None:
    """examples/demo 是给人手动验收的：配置必须跟着 schema 与覆盖率推导走。"""
    demo = Path(__file__).resolve().parents[1] / "examples" / "demo"
    toml = demo / "testlens.toml"
    if not toml.is_file():
        # 本地手动验收时有人会删掉它试「无配置」路径：不替他把文件加回来，也不让测试红
        pytest.skip("examples/demo/testlens.toml 本地不存在，跳过配置守护")
    config = load_config(toml)

    assert [pkg.name for pkg in config.packages] == ["demo"]
    assert config.port == 9000
    assert config.host == "127.0.0.1"
    # source_pkg=shop + pyproject 里的 [tool.coverage.run] data_file ⇒ 自动推导出这两项
    package = config.packages[0]
    assert package.coverage_data_file == ".coverage.demo"
    assert "--cov=shop" in package.coverage_args
    # demo 默认启动就预热覆盖率（演示用；--no-warm-coverage 可跳过）
    assert config.coverage_warmup is True
    # 示例里的测试脚本确实存在，别在重命名后留下空项目
    assert len(list_test_files(package, config.repo_root)) == 5


def test_module_level_config_is_used_by_dev(tmp_path: Path) -> None:
    """模块级 config 才能被 `dev <文件.py>` 当配置读（写进 main() 里读不到）。"""
    (tmp_path / "test").mkdir()
    module = tmp_path / "embed.py"
    module.write_text(
        "from pathlib import Path\n"
        "from testlens import PackageSpec, build_config\n"
        "config = build_config(\n"
        "    packages=(PackageSpec(name='solo', label='Solo', root='.'),),\n"
        "    repo_root=Path(__file__).parent,\n"
        "    port=9000,\n"
        "    open_browser=True,\n"
        "    coverage_warmup=True,\n"
        ")\n",
        encoding="utf-8",
    )

    config = config_for_path(str(module), None)

    assert config.port == 9000
    assert config.open_browser is True
    assert config.coverage_warmup is True
    assert config.source is not None and config.source.endswith("embed.py")


def test_explicit_coverage_config_drives_data_file(tmp_path: Path) -> None:
    """显式给了 coverage_config：数据文件就读它 [run] data_file，不靠猜。"""
    (tmp_path / "apps/demo/test").mkdir(parents=True)
    (tmp_path / ".coveragerc.custom").write_text(
        "[run]\ndata_file = .coverage.custom\n", encoding="utf-8"
    )

    config = build_config(
        packages=(
            PackageSpec(
                name="demo",
                label="Demo",
                root="apps/demo",
                source_pkg="demo",
                coverage_config=".coveragerc.custom",
            ),
        ),
        repo_root=tmp_path,
    )

    pkg = config.packages[0]
    assert pkg.coverage_data_file == ".coverage.custom"
    assert pkg.coverage_config == ".coveragerc.custom"
    assert pkg.coverage_args == (
        "--cov-config=.coveragerc.custom",
        "--cov=demo",
        "--cov-report=",
    )


def test_cov_config_inside_args_drives_data_file(tmp_path: Path) -> None:
    """coverage_args 里带 --cov-config 时，同样按那个配置定位数据文件。"""
    (tmp_path / "apps/demo/test").mkdir(parents=True)
    (tmp_path / ".coveragerc.fromargs").write_text(
        "[run]\ndata_file = .coverage.fromargs\n", encoding="utf-8"
    )

    config = build_config(
        packages=(
            PackageSpec(
                name="demo",
                label="Demo",
                root="apps/demo",
                source_pkg="demo",
                coverage_args=(
                    "--cov-config=.coveragerc.fromargs",
                    "--cov=demo",
                    "--cov-report=",
                ),
            ),
        ),
        repo_root=tmp_path,
    )

    pkg = config.packages[0]
    assert pkg.coverage_data_file == ".coverage.fromargs"
    assert pkg.coverage_config == ".coveragerc.fromargs"
    # 宿主自己写的命令原样保留
    assert pkg.coverage_args == (
        "--cov-config=.coveragerc.fromargs",
        "--cov=demo",
        "--cov-report=",
    )

