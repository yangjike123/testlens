"""最小接入示例：在宿主项目里构造 testlens，而不是走 CLI。

运行（从仓库根）：

    uv run python packages/testlens/examples/plugin_embed.py

只演示装配，不会起 HTTP 服务——真起面板就是 `toolkit.serve()`。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 从源码目录直接跑示例时，让 `import testlens` 找到同级的包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from testlens import PackageSpec, Toolkit, build_config


def main() -> int:
    # 仅示例：换成你自己的仓库根。真实插件里通常由调用方传入。
    repo_root = Path(__file__).resolve().parents[3]

    config = build_config(
        packages=(
            PackageSpec(
                name="my-api",                  # 唯一 id：URL 段、缓存文件名
                label="My API",                 # 界面 tab 显示名
                root="apps/web-service",        # 换成宿主真实的包目录
                source_pkg="app",               # 不填 = 该包不统计覆盖率
                # 覆盖率其余四项按项目现状推导（Makefile / .coveragerc / pyproject）；
                # 想固定下来再显式写 coverage_data_file / coverage_config / coverage_args。
            ),
        ),
        repo_root=repo_root,
        port=9000,                              # 其余参数都有默认值
        open_browser=False,
    )

    toolkit = Toolkit(config)
    result = toolkit.scan()

    print(f"包数量：{len(config.packages)}")
    print(f"数据目录：{config.data_dir}")
    print(f"扫描到 {result['total']} 个测试脚本，{result['changed']} 个已更新")
    print("\n起面板只需一行：toolkit.serve()")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
