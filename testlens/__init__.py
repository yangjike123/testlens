"""本地测试工具箱：清单、变更检测、面板、覆盖率。可作为插件嵌入宿主项目。

模块边界：

    config.py           Config / PackageSpec / 校验 / detect_packages / TOML 加载
    scan.py             Scanner：脚本缓存与逐字变更检测（不依赖 git）
    history.py          HistoryStore：每次运行的索引 + 日志落盘
    coverage_report.py  CoverageService：解析 .coverage.* 并给出未覆盖行
    junit.py            junitxml 解析：用例统计 / 每脚本耗时 / 最慢 / 失败
    runner.py           RunManager：pytest 进程生命周期与输出缓冲
    inventory.py        扫描结果 -> 页面数据 / 静态页
    server.py           HTTP 面板（只监听回环地址）
    cli.py              命令行入口（list / changed / plan / page / serve / dev）
    __main__.py         `python -m testlens` 的入口
    toolkit.py          聚合入口：Toolkit(config) 把上面全部串起来

两个入口：

    python -m testlens <command>     CLI（读当前目录的 toml / py 配置）
    from testlens import Toolkit     插件嵌入（宿主显式给 packages 与 repo_root）

详见 README.md 的「嵌入到代码里」与「安装」两节。
"""

from .config import (
    Config,
    ConfigError,
    PackageSpec,
    build_config,
    detect_packages,
    load_config,
    override_config,
    source_label,
)
from .toolkit import Toolkit

__version__ = "0.4.5"

__all__ = [
    "Config",
    "ConfigError",
    "PackageSpec",
    "Toolkit",
    "build_config",
    "detect_packages",
    "load_config",
    "override_config",
    "source_label",
    "__version__",
]
