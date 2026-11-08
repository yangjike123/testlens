# Changelog

## 0.4.5 — 2026-11-08

- **pytest 改为必需依赖**：testlens 的运行器、junit 结果解析、`-q` / `--basetemp` / `--cov`
  参数都建立在 pytest 上，所以 `pip install testlens` 会一并安装 `pytest>=8`，不再宣称零依赖。
  新项目装完 testlens 就能直接运行测试，不会出现「面板能开、点运行必然失败」。
- 覆盖率页签仍需要 coverage（通常随 `pytest-cov`）；不装只影响覆盖率页签，不影响运行测试。
- 修复 `TESTLENS_PORT` 填非整数时直接抛出一屏 `ValueError` 堆栈：现在转成 `ConfigError`，
  命令行输出「配置有问题：TESTLENS_PORT 必须是整数」并以退出码 2 结束。
- 修复命令行 `--port` 绕过 1–65535 校验：`dataclasses.replace` 不跑 `_validate`，
  `serve --port 0` 会绑到系统分配的随机端口，却把 `http://127.0.0.1:0/` 打给用户。
  新增 `override_config()`（已导出），CLI 的 `--port` / `--browser` / `--warm-coverage`
  统一走它重新校验，行为和写在配置文件里一致。
- 修复 Windows 上不写引号的解释器绝对路径被拆坏：`shlex.split` 按 POSIX 规则把反斜杠
  当转义符，`C:\venv\Scripts\python.exe` 变成 `C:venvScriptspython.exe`，子进程直接
  `FileNotFoundError`。新增 `split_command()`，面板运行 / `plan --run` / `coverage json`
  三处统一使用；POSIX 行为不变。
- 文档：清掉残留的 `packages/testlens/` 旧路径与「五个子命令」，环境变量表补
  `TESTLENS_CONFIG`，`[commands]` 说明命令拆分规则（带空格 / 反斜杠路径怎么写）。
- CI：`ci.yml` 与 `release.yml` 的测试步骤显式加 `--cov-fail-under=100`；发版流程在
  构建后新增「把 wheel 装进干净环境跑 CLI 冒烟」这一步，通过后才发 PyPI 与 GitHub Release。
- 修复 Windows 上 4xx 响应偶发 `ConnectionAbortedError`：面板在 origin 不符、
  `Content-Length` 超限、路径未知时没读完请求体就继续 keep-alive，残留 body 会被
  当成下一个请求解析（响应串台），Windows 上则表现为连接被重置。现在错误响应一律
  带 `Connection: close` 并关闭连接。
- `RunManager.build_commands` 改用字典查包，不再用 `next(p for p in ...)`：生成器表达式
  会多出一条「耗尽」分支，coverage 7.2 会把它算成未覆盖（7.17 不算），同一份代码在
  不同 coverage 版本下会得出 99.96% 与 100% 两种结论。
- 打包：sdist 的 `exclude` 补上 `.venv*` / `build` / `dist` / `*.egg-info` 等；之前
  仓库根放一个 `.venv311` 就会被原样打进源码包。

## 0.4.4 — 2026-11-08

- 仓库完整性：`examples/demo` 纳入版本库（保留 `uv.lock`，继续忽略 `.venv` / 运行数据 /
  覆盖率数据）。干净 clone 后 README 里的 demo 命令可以直接用，相关守护测试不再 skip。
- 发布流程：`pypa/gh-action-pypi-publish` 加 `skip-existing`（tag 重跑不会因同名文件失败），
  GitHub Release 改为「PyPI 发布成功之后」再创建；发布前先跑测试。

## 0.4.3 — 2026-11-08

- 修复 `host = "::1"` 能通过配置校验却起不来：端口探测与 `HTTPServer` 现在按 host
  选择 `AF_INET6`，终端 URL 打印为 `http://[::1]:8787/`（IPv6 字面量需要方括号）。
- 修复 Pyright 报的 `Path / str | None` 类型错误：`warm_coverage` 里用 `cast` 收窄，
  运行期行为不变。
- CI：安装 `pytest-cov` / `ruff`，测试改为带 100% 覆盖率门禁，并跑 `ruff check`；
  发布工作流在构建/上传前先跑测试。

## 0.4.2 — 2026-11-08

- 修复：自定义 `test_glob` 的包能扫到脚本、却在运行前被硬编码的 `test_*.py` 校验拒绝；
  现在扫描与运行统一按包自己的 `test_glob` 判断。
- 文档：新增「怎么判定一个文件是测试脚本」——位置 / 文件名 / 递归规则、正反例表格、
  `func_prefix` 与变更检测的边界、`dev` 推断时的默认值。

## 0.4.1 — 2026-11-08

- 修复多服务（多包）项目里 junit 结果**按文件统计为空**的问题：pytest 9 的 junitxml
  不再输出 `file` 属性，`classname` 会带包根（`services.api.test.test_health`），旧逻辑
  又补了一次 `services/api/`，拼出的路径不存在 → 每脚本耗时与失败定位缺失（总用例数正常）。
- 新增多服务集成测试 `tests/test_multiservice.py`：一个仓库两套服务、每套多个脚本，
  覆盖并发运行（按包分槽）、同包互斥、一次跑多个脚本、覆盖率按包隔离、`--package` 过滤。

## 0.4.0 — 2026-11-08

- 起面板后**默认不再自动打开页面**：是否打开由 `[server] open_browser` 控制（默认 `false`），
  命令行 `--browser` / `--no-browser` 只覆盖这一次运行。关闭时终端会打印怎么打开。
- 修复：同包「停止」后立刻再跑，旧运行的收尾线程会把新运行的进程句柄删掉，
  导致新运行再也停不下来（退出面板也会留下孤儿 pytest）。进程句柄改为按 run_id 记账。
- 测试补齐到 **100% 行 / 分支覆盖**（`uv run pytest --cov=testlens`，`branch = true` 与
  `fail_under = 100` 会卡住回退）：新增面板每条 HTTP 路由的真实请求测试、真实 pytest
  子进程测试（成功 / 失败 / 超时 / 命令不存在）、CLI 五个子命令与配置解析分支，
  以及坏缓存 / 坏编码 / 半截 XML 等边界。
- 修复：`coverage_json_cmd` 指向不存在的命令时抛 `FileNotFoundError`，`/api/coverage`
  会直接断连接；现在统一转成 `NoCoverageData`，页面显示「读取覆盖率失败」。
- 内部整理：pytest 命令统一由 `RunManager.command_for` 拼装（面板运行与覆盖率预热共用）；
  `testlens page` 只扫一遍仓库；内容未变的脚本复用缓存里的函数数，不再重复解析 AST。
- 新增 `examples/demo` 手动验收项目：通过 / 故意失败 / 慢用例 / 跳过 / 覆盖率演示，
  附五分钟验收清单。它以 path 依赖把本仓库的 testlens 装进自己的 dev 组，
  `cd examples/demo && uv run testlens dev .` 即可起面板（端口 9000）。
  两种配置写法都放了示例：`testlens.toml` 与 `testlens_config.py`，值一致、toml 优先。
- `examples/plugin_embed.py` 修正为可用：旧的 `parents[3]` 是按 `packages/testlens/examples/`
  布局写的，在本仓库会指到仓库外；现在把 `examples/demo` 当宿主项目，`config` 放在模块级，
  `testlens dev examples/plugin_embed.py` 会直接读它。
- `dev <文件.py>` 的 `.py` 参数：模块里有 `config`/`toolkit` 就照旧当配置用；**没有时不再直接报错**，
  改为按该文件所在目录（向上找最近的 testlens 配置，到 `.git` 为止）起面板。
  `--config` 仍严格要求该文件是配置文件。
- 修复配置优先级：环境变量曾盖过 `build_config(...)` 的显式参数，和文档写反了；
  现在固定为「默认值 ＜ 配置文件 ＜ 环境变量 ＜ 显式参数」。
- 配置文件改为直接按源码编译执行（不走 `.pyc`），改一行立即生效，不再受字节码缓存影响。
- `host` 允许 `127.0.0.0/8` 里的任意回环地址（如 `127.1.1.1`）；`0.0.0.0`、局域网地址与域名仍拒绝。
- 新增配置项逐项测试 `tests/test_config_params.py`：`[server]` / `[page]` / `[paths]` /
  `[history]` / `[coverage]` / `[commands]` / `[[package]]` 的每个字段、环境变量、
  覆盖优先级和实际行为都有对应用例。
- 纯推断模式（没有任何配置文件）也能有覆盖率：目录下**恰好一个**顶层 Python 包时，自动把它
  当作 `source_pkg`（0 个或多个不猜，避免把别的包算进来）。`--warm-coverage` 在没有可统计的包时
  会明确说明原因，不再静默跳过。
- 覆盖率预热默认改为**开启**：启动时若数据文件缺失会先跑一遍带 `--cov` 的测试。
  `[coverage] warmup = false`、`--no-warm-coverage` 可关闭；`--warm-coverage` 可强制打开。

## 0.3.1 — 2026-11-07

只有元数据修正，功能与 0.3.0 相同。

- 修正 `Project-URL`：`Homepage` / `Repository` / `Issues` / `Changelog` 全部指向
  `github.com/yangjike123/testlens`（0.3.0 的产物是改账号前构建的，链接指向了错误仓库）。
- 建议安装 0.3.1；0.3.0 已 yank。

## 0.3.0 — 2026-11-07

首个公开版本。

- 脚本清单、逐字变更检测（不依赖 git）
- 浏览器里勾选运行 pytest，按包隔离并发，实时输出
- 运行历史与结构化结果（用例统计 / 每脚本耗时 / 最慢用例 / 失败用例）
- 覆盖率页签：数据文件解析、未覆盖行、面板内全量统计
- 覆盖率四项自动推导（Makefile / `.coveragerc` / `pyproject.toml`），支持 `[coverage] warmup`
- 声明式 `testlens.toml` 与代码式 `testlens_config.py` 两种配置，`dev/serve/list/plan/page`
- 静态页导出（三件套或单文件），零第三方运行时依赖
