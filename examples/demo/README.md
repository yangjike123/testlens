# testlens 手动验收项目

一个能跑的小项目，专门用来手动验收面板：**通过、故意失败、慢用例、跳过、未覆盖分支**都齐了。
代码只有两个模块（`shop/cart.py`、`shop/discount.py`），测试在 `test/` 下，改起来没有心理负担。

它同时是一个**宿主项目示例**：`pyproject.toml` 把本仓库的 `testlens` 以 path 依赖装进了 dev 组
（`[tool.uv.sources] testlens = { path = "../..", editable = true }`），所以面板就是它自己的开发依赖，
不是"拿一个外部命令来扫这个目录"。

## 启动

```bash
cd examples/demo
uv run testlens dev .        # 读 testlens.toml：http://127.0.0.1:9000/
```

配置有两种写法：`testlens.toml`（声明式）和 `testlens_config.py`（Python），字段一一对应。
demo 里**两份都放了示例，值完全一致**（端口 9000）；同目录都有时 **toml 优先**，所以实际读的是 toml。
删掉 toml 就会自动回落到 `testlens_config.py`，行为不变。

「在代码里嵌入」的示例在仓库根：`examples/plugin_embed.py`（它把 demo 当宿主项目），
见下面「在代码里嵌入（不走 CLI）」。

第一次会先 `uv sync`（装 testlens / pytest / pytest-cov）；之后 `uv run` 是秒开的。

覆盖率预热默认开启：**首次启动会先跑一遍带 `--cov` 的测试**（约 3.5 秒，其中包括那条
故意失败的用例，退出码 1 不影响面板启动）再起服务。`.coverage.demo` 已存在时这一步会跳过
——它补的是"缺失的数据"，不是每次重算。想跳过加 `--no-warm-coverage`，想强制刷新就删掉它。
想确认用的是哪份插件：

```bash
uv run testlens --version
uv run python -c "import testlens; print(testlens.__file__)"   # 指向仓库里的 testlens/
```

也可以不装、直接从仓库根用仓库的 Python 跑（等价，适合不想动 demo 的 venv）：

```bash
# Windows
.venv/Scripts/python.exe -m testlens dev examples/demo

# macOS / Linux
.venv/bin/python -m testlens dev examples/demo
```

端口在 `testlens_config.py`（或 `testlens.toml`）里改。

### 关于 `dev <文件.py>`

`dev` 的 `.py` 参数先当**配置文件**读：模块里有 `config`（或 `toolkit`）就用它；没有的话
不报错，而是当成「项目入口」，从文件所在目录**往上找**最近的配置目录再起面板。

```bash
uv run testlens dev .                  # 目录配置（当前是 testlens_config.py）：9000
uv run testlens dev ../plugin_embed.py # 文件里的 config：同样 9000（它把 demo 当宿主）
uv run testlens dev test/test_cart.py  # 自己没 config → 向上找到目录配置：9000
```

`--config xxx.py` 不一样：它明确要求那个文件是配置，模块里没有 `config` 会直接报错。

### 配置优先级与推断

同一目录下：`testlens.toml` ＞ `testlens_config.py` ＞ 目录推断（只有 `dev <目录或脚本>` 会推断）。

- 两个配置文件都删掉后，`dev .` 会推断：一个包、`root="."`、测试在 `./test`、端口 `8787`；
  目录下恰好一个顶层 Python 包（这里是 `shop/`）时，还会把它当 `source_pkg`；
- `warmup` 默认开启，推断模式下也会尝试预热；`--no-warm-coverage` 可跳过；
- 横幅第一行会写「无配置文件（包定义来自代码或目录推断）」；
- `list` / `changed` / `page` / `serve` **不做**推断，没有配置会直接报错并给出样例。

## 在代码里嵌入（不走 CLI）

```bash
cd examples/demo
uv run python ../plugin_embed.py        # 仓库根的示例，把 demo 当宿主
```

它用 `build_config` + `Toolkit` 直接装配：打印宿主目录、数据目录与扫描结果；
要起面板就把最后一行换成 `toolkit.serve()`。它自带模块级 `config`，所以
`uv run testlens dev ../plugin_embed.py` 也能直接起面板（端口 9000）。

## 五分钟验收清单

| # | 操作 | 预期 |
| --- | --- | --- |
| 1 | 勾选 `test_cart.py` → 「运行选中」 | 全绿：6 条用例通过，耗时面板里能看到脚本条 |
| 2 | 勾选 `test_pricing.py` → 「运行选中」 | 1 通过 + 1 失败；摘要条出现「只跑失败 (1)」，点它能只重跑失败所在脚本 |
| 3 | 点开失败用例的「复制」 | 复制出用例名 + 文件 + 断言消息（`故意失败：15% 折扣后应是 170.00`） |
| 4 | 点某个脚本卡片上的「执行」 | 只跑这一个脚本，其余勾选不受影响 |
| 5 | 单独跑 `test_slow.py` | 输出实时滚动，约 3 秒；运行中点「停止」→ 状态变「已停止」 |
| 6 | 顶部「全部执行」 | 两个包以上才并行；这里只有一个包，等同全量；会带上慢用例（约 4 秒） |
| 7 | 改 `test/test_cart.py` 里任意一个断言 → 刷新页面 | 「当前更新」多 1 个，卡片标「已更新 +n -m」；点「标记已确认」后恢复 |
| 8 | 打开「覆盖率」页签（启动时已预热；改了代码就点「跑全量并统计」刷新） | `.coverage.demo` 生效；总覆盖率不是 100%，`shop/cart.py` 的免运费分支、`shop/discount.py` 的 raise 行标红 |
| 9 | 「运行历史」→ 点两条记录对比 | 显示本次脚本清单、与上一次比「新增/移出」、运行概览与完整输出 |
| 10 | `uv run testlens page` | `testlens.data/page/` 下生成可双击的只读页面 |

第 7 步之后想回到干净状态：把改动手动改回去（如果这个目录已经进了版本库，也可以 `git checkout -- test/test_cart.py`）。

## 项目结构

```text
examples/demo/
├── testlens.toml         面板配置（声明式，优先读它；端口 9000）
├── testlens_config.py    同一份配置的 Python 写法（删掉 toml 时生效）
├── pyproject.toml        宿主项目声明：dev 组里是本仓库的 testlens（path 依赖）+ pytest
├── shop/                  被测代码：覆盖率演示留了未走到的分支
│   ├── cart.py
│   └── discount.py
└── test/
    ├── test_cart.py       全通过 + 参数化用例
    ├── test_discount.py   正常路径（raise 故意没测）
    ├── test_pricing.py    一条通过 + 一条**故意失败**
    ├── test_slow.py       3 秒慢用例，用于停止/耗时演示
    └── test_optional.py   跳过用例
```

## 期望结果（全量）

```text
11 passed, 1 failed, 2 skipped
```

失败的那条叫 `test_intentional_failure_for_demo`，是设计好的；其余失败都算验收不通过。

## 清理

```bash
cd examples/demo
rm -rf .venv testlens.data .coverage.demo
```

（`.venv` / `testlens.data` / 覆盖率数据都在本目录的 `.gitignore` 里，不会进版本库。
`.venv` 删掉后下次 `uv run` 会重新装依赖，那时才需要联网。）
