# testlens

[![PyPI](https://img.shields.io/pypi/v/testlens.svg)](https://pypi.org/project/testlens/)
[![Python](https://img.shields.io/pypi/pyversions/testlens.svg)](https://pypi.org/project/testlens/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

一个零第三方依赖的本地测试面板：列出测试脚本、检测脚本内容变化、在浏览器里勾选运行
pytest、查看运行历史与代码覆盖率，并标出哪些行没跑到。

它只做工具，不是业务模块：不建表、不写接口、不改业务代码。变更检测基于源码缓存逐字
对比，不依赖 git。可以独立使用，也可以作为插件嵌进任何 Python 项目。

## 特性

- **脚本清单与变更检测**：按包分组列出 `test_*.py`，逐字对比缓存，标出「新增 / 已修改 /
  已删除」，不依赖 git。
- **浏览器里跑 pytest**：勾选脚本直接运行，实时输出；按包隔离并发，同一包内不会有两个
  pytest 抢同一批 fixture。
- **运行历史**：每次运行的输出、耗时、用例统计落盘，重启面板后仍能翻回去看。
- **覆盖率页签**：读 coverage 的数据文件，给出总览与逐文件未覆盖行，支持只跑失败用例。
- **零运行时依赖**：只用标准库；`pytest` / `coverage` 是业务 dev 依赖，按需装。
- **只监听回环地址**：校验 Origin、包归属与脚本路径，命令用列表传参、不经 shell。

## 安装

```bash
# uv（推荐）
uv add --dev testlens

# pip
pip install testlens
```

要求 Python ≥ 3.11。

## 快速开始

在项目根放一份 `testlens.toml`：

```toml
[server]
port = 8787

[[package]]
name = "api"
label = "API"
root = "."            # 包根就是本目录：测试在 ./test
source_pkg = "app"    # 想让覆盖率页签工作就写；不写 = 不统计覆盖率
```

起面板：

```bash
testlens dev          # 读当前目录的配置，自动开浏览器
# 或者
testlens serve
```

浏览器打开 `http://127.0.0.1:8787/`：左边是脚本清单，右边是运行面板。
终端会打印「这次读的是哪份配置、几个包、多少个脚本」，包数量对不上时先看这一行。

monorepo 里一套配置看多个包：

```toml
[[package]]
name = "api"
label = "API"
root = "services/api"
source_pkg = "app"

[[package]]
name = "shared"
label = "Shared"
root = "packages/shared"
source_pkg = "shared"
```

> 多个包的 `root` 都相对同一个 `repo_root`（默认是配置文件所在目录），不能越界；
> 只有一个包时可以用 `root = "."`。

## 覆盖率从哪来

只要写了 `source_pkg`，其余四项会按项目现状推导，不用重复配置：

| 字段 | 推导规则 |
| --- | --- |
| `coverage_config` | 显式写的（或 `coverage_args` 里 `--cov-config=`）优先；否则取 Makefile 里跑 `--cov=<source_pkg>` 的命令用的 `--cov-config=`；再没有就按包名挑 `.coveragerc.<后缀>` |
| `coverage_data_file` | 该配置的 `[run] data_file`（相对配置文件所在目录）→ `pyproject.toml` 的 `[tool.coverage.run].data_file` → `.coverage.<包目录名>` |
| `coverage_args` | `--cov-config=<rc>` + `--cov=<source_pkg>` + `--cov-report=` |
| `coverage_hint` | Makefile 命中就是 `make <目标>`，否则给一条等价的 pytest 命令 |

四个字段都可以显式覆盖；显式写了就以你写的为准。

开启 `[coverage] warmup = true` 后，启动面板时会先配对：某个包的数据文件不存在就先跑
一遍带 `--cov` 的测试生成它，已存在就跳过。`--warm-coverage` / `--no-warm-coverage`
可临时覆盖。

## 命令行

```bash
testlens list [--package NAME]          # 脚本清单与测试函数数
testlens changed [--package NAME]       # 与缓存基线不一致的脚本
testlens plan [--package NAME] [--run]  # 该跑哪些；--run 直接跑
testlens page [--output PATH] [--single]  # 生成可双击的静态页
testlens serve [--port 8787] [--no-browser] [--warm-coverage]
testlens dev [TARGET] [--port 8787] [--no-browser] [--warm-coverage]
```

- 默认读**当前目录**的 `testlens.toml` 或 `testlens_config.py`（toml 优先）；
  `--config PATH` 可以指向任意一份，写在子命令前后都行。
- `dev TARGET`：给目录就先读目录里的配置、没有才按目录推断（一个包、`root="."`、
  测试在 `<TARGET>/test`）；给 `.py` 就直接读模块里的 `config`。
- 没有配置文件、推断出的 `test/` 又是空的时候，`dev` 会直接报错并列出附近找到的配置，
  不会起一个空面板。

## 嵌入到代码里

```python
from pathlib import Path

from testlens import PackageSpec, Toolkit, build_config

config = build_config(
    packages=(
        PackageSpec(
            name="api",
            label="API",
            root="services/api",
            source_pkg="app",      # 覆盖率四项自动推导
        ),
    ),
    repo_root=Path(__file__).parent,
    port=9000,
)

toolkit = Toolkit(config)
toolkit.scan()          # 清单 + 变更检测
toolkit.build_payload() # 页面数据
toolkit.serve()         # 起面板，阻塞到 Ctrl+C
```

`Toolkit` 一个实例持有一份配置与全部服务（扫描、历史、覆盖率、运行器），同一进程可以
建多个实例，互不共享状态。

## 配置参考

`testlens.toml` 支持这些段（全部可选，除 `[[package]]`）：

```toml
[page]
dir = "reports"                    # 静态页目录；省略 = testlens.data/page
name = "testlens.html"
single = false                     # true = 合成单个自包含 HTML

[paths]
data_dir = "testlens.data"         # 运行数据目录
# template_path = "ui/mine.html"   # 换模板（同目录放同名 .css / .js）

[server]
host = "127.0.0.1"                 # 只允许回环
port = 8787
open_browser = true
run_timeout = 900
max_output_lines = 20000
tail_lines = 800

[history]
max_entries = 50

[coverage]
warmup = false                     # 启动时补生成缺失的覆盖率数据

[commands]
pytest_cmd = "uv run pytest"       # 不用 uv 就改成 python -m pytest
coverage_json_cmd = "uv run coverage json"

[[package]]
name = "api"                       # 唯一 id：URL 段、缓存文件名；只允许小写字母/数字/连字符
label = "API"                      # 界面 tab 显示名
root = "services/api"              # 相对 repo_root
tests_dir = "test"                 # 测试目录
test_glob = "test_*.py"            # 测试文件名
func_prefix = "test_"              # 统计测试函数用的前缀
source_pkg = "app"                 # 覆盖率顶层模块；不写 = 不统计覆盖率
coverage_data_file = ".coverage.api"
coverage_config = ".coveragerc.api"
coverage_args = ["--cov-config=.coveragerc.api", "--cov=app", "--cov-report="]
coverage_hint = "make test-api"
```

覆盖率定位顺序：显式 `coverage_data_file` ＞ 显式 `coverage_config` / args 里
`--cov-config=` 指向的配置 ＞ Makefile 里 `--cov=<pkg>` 那行的 `--cov-config` ＞
按包名挑的 `.coveragerc*` ＞ `.coverage.<包目录名>`。生成数据时统一用 `COVERAGE_FILE`
锁定到最终路径，保证「写哪儿就读哪儿」。

运行数据默认写在 `<repo>/testlens.data/`（脚本基线、运行历史、覆盖率缓存、临时目录、
静态页），整个目录可随时删掉重建。

## 开发

```bash
git clone https://github.com/yanjike/testlens
cd testlens
python -m pip install -e . pytest
python -m pytest -q
```

插件自身没有运行时依赖；前端是三个静态文件（`testlens/web/dashboard.{html,css,js}`），
没有构建步骤，改完重启面板即生效。

## License

[MIT](LICENSE)
