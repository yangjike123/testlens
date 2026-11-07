# Changelog

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
