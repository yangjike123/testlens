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
testlens dev             # 读当前目录的配置起面板（默认不自动打开页面）
testlens dev --browser   # 这次顺便把页面打开
# 或者
testlens serve
```

浏览器打开 `http://127.0.0.1:8787/`：左边是脚本清单，右边是运行面板。
终端会打印「这次读的是哪份配置、几个包、多少个脚本」，包数量对不上时先看这一行。

想直接拿个现成项目试手，仓库里的 `examples/demo` 就是：通过、故意失败、慢用例、跳过、
未覆盖分支都准备好了，还带一份五分钟验收清单。它把本仓库的 testlens 作为 path 依赖装进
自己的 dev 组，**是一个完整的宿主项目示例**：

```bash
cd examples/demo
uv run testlens dev .             # 面板在 http://127.0.0.1:9000/（demo 里 toml / py 配置同值）
```

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

| 字段                 | 推导规则                                                                                                                                                           |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `coverage_config`    | 显式写的（或 `coverage_args` 里 `--cov-config=`）优先；否则取 Makefile 里跑 `--cov=<source_pkg>` 的命令用的 `--cov-config=`；再没有就按包名挑 `.coveragerc.<后缀>` |
| `coverage_data_file` | 该配置的 `[run] data_file`（相对配置文件所在目录）→ `pyproject.toml` 的 `[tool.coverage.run].data_file` → `.coverage.<包目录名>`                                   |
| `coverage_args`      | `--cov-config=<rc>` + `--cov=<source_pkg>` + `--cov-report=`                                                                                                       |
| `coverage_hint`      | Makefile 命中就是 `make <目标>`，否则给一条等价的 pytest 命令                                                                                                      |

四个字段都可以显式覆盖；显式写了就以你写的为准。

覆盖率预热默认开启：启动面板时会先配对——某个包的数据文件不存在就先跑一遍带 `--cov` 的测试
生成它，已存在就跳过。不想预热写 `[coverage] warmup = false`，或加 `--no-warm-coverage`；
`--warm-coverage` 可强制打开。

## 命令行

```bash
testlens list [--package NAME]          # 脚本清单与测试函数数
testlens changed [--package NAME]       # 与缓存基线不一致的脚本
testlens plan [--package NAME] [--run]  # 只运行有更改或新增后的测试脚本文件
testlens page [--output PATH] [--single]  # 生成可双击的静态页
testlens serve [--port 8787] [--browser] [--warm-coverage]
testlens dev [TARGET] [--port 8787] [--browser] [--warm-coverage]
```

- 默认读**当前目录**的 `testlens.toml` 或 `testlens_config.py`（toml 优先）；
  `--config PATH` 可以指向任意一份，写在子命令前后都行。
- 起面板后**不自动打开页面**（[server] `open_browser`，默认 `false`）；想让它顺手打开，
  配置里写 `open_browser = true`，或这次加 `--browser`，反向用 `--no-browser`。
- `dev TARGET`：给目录就先读目录里的配置、没有才按目录推断（一个包、`root="."`、
  测试在 `<TARGET>/test`；目录下只有一个顶层 Python 包时，顺手把它当 `source_pkg`）；
  给 `.py` 时，模块里有 `config` 就用它（`examples/plugin_embed.py` 自带一份，端口 9000），
  没有才按该文件所在目录（向上找最近的配置）起面板。
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

可运行的版本在 `examples/plugin_embed.py`（`uv run python examples/plugin_embed.py`），
它把 `examples/demo` 当宿主项目，可以直接改成你的仓库。

## 配置参考

两种写法表达同一组配置：`testlens.toml`（声明式，推荐）或 `testlens_config.py`
（模块里 `config = build_config(...)`）。同目录都有时 **toml 优先**。
对应关系上只有两个名字不同：TOML 的 `[page] dir` 对应 Python 的 `dist_dir`，
`[page] name` 对应 `static_page_name`，其余同名。

覆盖顺序（后者胜）：

```text
默认值 ＜ 配置文件 ＜ 环境变量 ＜ build_config 显式参数
```

命令行开关（`--port`、`--browser` / `--no-browser`、`--warm-coverage` / `--no-warm-coverage`）
只覆盖这一次运行，不写回配置。

### [server]

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `host` | `"127.0.0.1"` | 只接受回环：`localhost`、`::1`、`127.0.0.0/8`（如 `127.1.1.1`）；`0.0.0.0` 会被拒绝 |
| `port` | `8787` | 1–65535；被占用时启动会提示换端口 |
| `open_browser` | `false` | 启动后自动打开页面；`--browser` / `--no-browser` 可临时覆盖 |
| `run_timeout` | `900` | 单次 pytest 的超时秒数；超时杀进程并记为 failed |
| `max_output_lines` | `20000` | 内存里保留的最大输出行数，超出丢弃最早的行 |
| `tail_lines` | `800` | 页面状态接口返回最后多少行，必须 ≤ `max_output_lines` |

### [page]

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `dir` | `<repo>/testlens.data/page` | 静态页输出目录，相对 `repo_root` |
| `name` | `"testlens.html"` | 文件名，必须以 `.html` 结尾 |
| `single` | `false` | `true` = 合成单个自包含 HTML，否则拆成 html / css / js 三份 |

### [paths]

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `repo_root` | 配置文件所在目录 | 其余相对路径的解析基准；可显式写，相对路径按配置文件目录解析；`dev` 推断时就是目标目录 |
| `data_dir` | `<repo>/testlens.data` | 运行数据目录（脚本缓存、历史、覆盖率缓存、临时目录） |
| `template_path` | 内置 `web/dashboard.html` | 自定义模板；同目录放同名 `.css` / `.js` |

> `[paths]` 里旧的 `dist_dir` / `static_page_name` 仍然认，等价于 `[page] dir` / `[page] name`。

### [history]

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `max_entries` | `50` | 运行历史保留条数：一次运行一条记录 + 一份日志，超出后删最旧的（记录和 `.log` 一起删） |

### [coverage]

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `warmup` | `true` | 启动时补生成缺失的覆盖率数据（数据文件已存在就跳过）；`--no-warm-coverage` 可跳过 |

### [commands]

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `pytest_cmd` | `"uv run pytest"` | 面板里跑测试用的命令前缀；不用 uv 就改成 `python -m pytest` |
| `coverage_json_cmd` | `"uv run coverage json"` | 读取覆盖率数据文件的命令，面板会在后面拼 `--data-file` / `-o` |

### [[package]]

至少要有一个；每个包一套测试。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `name` | 必填 | 唯一 id：URL 段、缓存文件名；只允许小写字母/数字/连字符 |
| `label` | 必填 | 界面 tab 显示名 |
| `root` | 必填 | 包根，相对 `repo_root`；`"."` 表示仓库根（此时只能有一个包） |
| `tests_dir` | `"test"` | 测试目录，相对 `root` |
| `test_glob` | `"test_*.py"` | 测试文件名匹配 |
| `func_prefix` | `"test_"` | 统计测试函数用的前缀 |
| `source_pkg` | 无 | 覆盖率顶层模块；不写 = 该包不统计覆盖率 |
| `coverage_data_file` | 自动推导 | 覆盖率数据文件，如 `.coverage.api` |
| `coverage_config` | 自动推导 | 覆盖率配置，如 `.coveragerc.api` |
| `coverage_args` | 自动推导 | 跑覆盖率时追加给 pytest 的参数 |
| `coverage_hint` | 自动推导 | 没跑过覆盖率时的提示命令（如 `make test-api`） |

覆盖率定位顺序：显式 `coverage_data_file` ＞ 显式 `coverage_config` / args 里
`--cov-config=` 指向的配置 ＞ Makefile 里 `--cov=<pkg>` 那行的 `--cov-config` ＞
按包名挑的 `.coveragerc*` ＞ `.coverage.<包目录名>`。生成数据时统一用 `COVERAGE_FILE`
锁定到最终路径，保证「写哪儿就读哪儿」。

### 环境变量

| 变量 | 覆盖的字段 |
| --- | --- |
| `TESTLENS_HOST` | `[server] host` |
| `TESTLENS_PORT` | `[server] port` |
| `TESTLENS_DATA_DIR` | `[paths] data_dir` |

环境变量优先级低于 `build_config(...)` 的显式参数，高于配置文件。

运行数据默认写在 `<repo>/testlens.data/`（脚本基线、运行历史、覆盖率缓存、临时目录、
静态页），整个目录可随时删掉重建。

运行历史只保留最近 `[history] max_entries` 条（默认 50）：一次运行占一条（`history.json`
里一条记录 + 一份完整输出 `.log`），第 51 次运行时最旧的那条记录和它的日志会被删掉。
它只按**条数**清理，不看文件大小，也不分包——所有包共用这一份历史。

## 开发

```bash
git clone https://github.com/yangjike123/testlens
cd testlens
uv sync                     # 或：python -m pip install -e . pytest pytest-cov
uv run pytest               # 全部测试
uv run pytest --cov=testlens --cov-report=term-missing
```

测试覆盖面板的每条 HTTP 路由（起真实服务发请求）、真实 pytest 子进程（成功 /
失败 / 超时 / 命令不存在）、CLI 的五个子命令与配置解析分支。`pyproject.toml` 里
`[tool.coverage.report] fail_under = 100`：覆盖率掉到 100% 以下会直接失败。

插件自身没有运行时依赖；前端是三个静态文件（`testlens/web/dashboard.{html,css,js}`），
没有构建步骤，改完重启面板即生效。

## License

[MIT](LICENSE)
