"""demo 的 Python 版配置（与 testlens.toml 二选一，同名目录下 toml 优先）。

`uv run testlens dev .` 只会认 `testlens.toml` / `testlens_config.py` 这两个文件名；
`plugin_embed.py` 不在这份名单里，所以它的 port 只对 `dev plugin_embed.py` 生效。

端口就写在这里：9000。
"""

from __future__ import annotations

from pathlib import Path

from testlens import PackageSpec, build_config

config = build_config(
    packages=(
        PackageSpec(
            name="demo",  # 唯一 id：URL 段、缓存文件名
            label="Demo",  # 界面 tab 显示名
            root=".",  # 包根就是本目录：测试在 ./test
            source_pkg="shop",  # 覆盖率顶层模块；其余参数按 pyproject 推导
        ),
    ),
    repo_root=Path(__file__).parent,
    port=9000,  # 面板端口
    host="127.0.0.1",  # 只允许回环：127.0.0.0/8、::1、localhost
    open_browser=False,  # 想启动自动开页面：改 True 或加 --browser
    coverage_warmup=True,  # 启动先补一遍覆盖率数据（默认就是 True）
)
