"""把扫描结果组装成页面数据，并渲染静态页（适配层）。

`Scanner` 给的是扁平的脚本列表，页面需要按包分组、带计数、带模板占位符。
CLI 和服务端都从这里拿数据，保证两边一致。

页面由三个文件组成（模板契约 v2）：

    dashboard.html   结构，含 __STYLE__ / __DATA_SCRIPT__ / __SCRIPT__ 三个占位符
    dashboard.css    样式
    dashboard.js     脚本

服务端始终把三者合成一个响应（少一次请求、也方便宿主换模板）；
静态页默认拆成三个文件输出，`static_page_single=True` 时才合成单文件。
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .scan import Scanner

# 模板里的占位符。用 replace 而不是 str.format：页面里有大量 JS 花括号。
STYLE_PLACEHOLDER = "__STYLE__"
DATA_PLACEHOLDER = "__DATA_SCRIPT__"
SCRIPT_PLACEHOLDER = "__SCRIPT__"
TEMPLATE_CONTRACT = "testlens-template: 2"  # 模板契约版本，升级校验用


def build_payload(scanner: Scanner, packages=None) -> dict:
    """按包分组。每个包都带完整脚本列表（含状态），页面自己切视图。"""
    config = scanner.config
    result = scanner.scan(packages)
    all_packages = tuple(packages) if packages else config.packages

    # 第一次打开（还没有任何缓存）时先把当前内容立为基线，
    # 否则所有脚本会全部显示「新增」，看起来像全被改过。
    if result["cached"] == 0 and result["total"] > 0:
        scanner.mark_baseline(None)
        result = scanner.scan(packages)

    grouped = []
    for pkg in all_packages:
        items = [item for item in result["scripts"] if item["package"] == pkg.name]
        grouped.append(
            {
                "name": pkg.name,
                "label": pkg.label,
                "root": pkg.root,
                "total_scripts": sum(1 for item in items if item["status"] != "missing"),
                "changed_scripts": sum(1 for item in items if item["changed"]),
                "scripts": items,
            }
        )

    return {
        "scanned_at": result["scanned_at"],
        "total": result["total"],
        "changed": result["changed"],
        "packages": grouped,
        # 配置来源：页面标题旁显示。包的数量对不上时，先看这一行是谁。
        "config": {
            "path": config.source or "",
            "repo_root": str(config.repo_root),
            "package_count": len(config.packages),
        },
    }


def _page_assets(template_path: Path) -> tuple[str, str]:
    """读模板同目录的 css / js（同名不同后缀）。"""
    css_path = template_path.with_suffix(".css")
    js_path = template_path.with_suffix(".js")
    missing = [path.name for path in (css_path, js_path) if not path.is_file()]
    if missing:
        raise ValueError(
            f"模板 {template_path.name} 需要同目录的 {' / '.join(missing)}"
            f"（模板契约 {TEMPLATE_CONTRACT}）"
        )
    return (
        css_path.read_text(encoding="utf-8"),
        js_path.read_text(encoding="utf-8"),
    )


def _data_script(payload: dict, run_enabled: bool) -> str:
    """数据与运行开关。放独立 <script>，外链 js 从 window 上读。"""
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        "<script>\n"
        f"window.__TESTLENS_DATA__ = {data};\n"
        f"window.__TESTLENS_RUN__ = {'true' if run_enabled else 'false'};\n"
        "</script>"
    )


def _fill_template(template: str, data_script: str, style_tag: str, script_tag: str) -> str:
    """校验模板契约后填充占位符。

    缺契约标记或占位符直接抛错，避免生成一个静默空白的页面。
    """
    if TEMPLATE_CONTRACT not in template:
        raise ValueError(
            f"模板缺少契约标记 {TEMPLATE_CONTRACT!r}：可能是不兼容的旧模板或别的文件"
        )
    for placeholder in (STYLE_PLACEHOLDER, DATA_PLACEHOLDER, SCRIPT_PLACEHOLDER):
        if placeholder not in template:
            raise ValueError(f"模板缺少占位符 {placeholder}")

    # 先换样式与脚本、最后填数据：payload 里若出现同样的占位符文本不会被误替换
    html = template.replace(STYLE_PLACEHOLDER, style_tag, 1)
    html = html.replace(SCRIPT_PLACEHOLDER, script_tag, 1)
    return html.replace(DATA_PLACEHOLDER, data_script, 1)


def render_html_string(config: Config, payload: dict, *, run_enabled: bool = False) -> str:
    """合成单文件页面（服务端响应走这里）。"""
    template = config.template_path.read_text(encoding="utf-8")
    css, js = _page_assets(config.template_path)
    return _fill_template(
        template,
        _data_script(payload, run_enabled),
        f"<style>\n{css}\n</style>",
        f"<script>\n{js}\n</script>",
    )


def render_html(config: Config, payload: dict, output: Path, *, run_enabled: bool = False) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_html_string(config, payload, run_enabled=run_enabled), encoding="utf-8"
    )


def build_static_page(
    scanner: Scanner, output: Path | None = None, *, single: bool | None = None
) -> list[Path]:
    """生成可直接双击打开的只读页面，返回写出的文件列表。

    默认拆成 `<名字>.html` + `.css` + `.js`（HTML 只有几 KB）；
    `single=True`（或配置 `[page] single = true`）合成一个自包含 HTML。
    """
    config = scanner.config
    payload = build_payload(scanner)
    html_path = Path(output) if output else config.dist_dir / config.static_page_name
    html_path.parent.mkdir(parents=True, exist_ok=True)

    single = config.static_page_single if single is None else single
    if single:
        render_html(config, payload, html_path)
        return [html_path]

    css, js = _page_assets(config.template_path)
    css_path = html_path.with_suffix(".css")
    js_path = html_path.with_suffix(".js")
    template = config.template_path.read_text(encoding="utf-8")
    html = _fill_template(
        template,
        _data_script(payload, False),
        f'<link rel="stylesheet" href="{css_path.name}">',
        f'<script src="{js_path.name}"></script>',
    )
    html_path.write_text(html, encoding="utf-8")
    css_path.write_text(css, encoding="utf-8")
    js_path.write_text(js, encoding="utf-8")
    return [html_path, css_path, js_path]
