"""最小接入示例：在宿主项目里构造 testlens，而不是走 CLI。

运行（从仓库根）：

    uv run python examples/plugin_embed.py
    uv run testlens dev examples/plugin_embed.py   # 它自带模块级 config，也能当配置用

示例把 `examples/demo` 当成宿主项目；换成你自己的仓库时，只改 `demo_root` 和包定义即可。
只演示装配与只读扫描，不会起 HTTP 服务——真起面板就是 `toolkit.serve()`。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 没装 testlens（直接跑源码）时，让 `import testlens` 找到仓库根
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from testlens import PackageSpec, Toolkit, build_config  # noqa: E402

# 示例宿主项目：examples/demo。真实接入时换成你自己的仓库根。
demo_root = Path(__file__).resolve().parent / "demo"

# 模块级 config：`dev <文件.py>` 读得到；写在 main() 里就读不到。
config = build_config(
    packages=(
        PackageSpec(
            name="demo",  # 唯一 id：URL 段、缓存文件名
            label="Demo",  # 界面 tab 显示名
            root=".",  # 包根就是 demo 目录：测试在 ./test
            source_pkg="shop",  # 覆盖率顶层模块；其余四项按 pyproject 推导
        ),
    ),
    repo_root=demo_root,
    port=9000,  # 其余参数都有默认值
    open_browser=False,  # 想启动自动开页面：True 或加 --browser
)


def main() -> int:
    toolkit = Toolkit(config)
    result = toolkit.scan()

    print(f"宿主：{config.repo_root}")
    print(f"数据目录：{config.data_dir}")
    print(f"扫描到 {result['total']} 个测试脚本，{result['changed']} 个已更新")
    print("\n起面板只需一行：toolkit.serve()")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
