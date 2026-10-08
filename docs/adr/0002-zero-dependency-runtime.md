# ADR-0002：运行时零第三方依赖

- 状态：已接受
- 日期：2026-03-02

## 背景

`pyproject.toml` 里可以写 `dependencies = ["fastapi", "pydantic", "uvicorn", "sqlalchemy"]`，
开发体验更"现代"。

## 决策

运行时依赖保持为空。仅用标准库：`http.server`、`sqlite3`、`json`、`dataclasses`、
`datetime`、`unittest`。开发期可选装 `ruff`/`mypy`/`pytest`（`[project.optional-dependencies]`），
但**所有检查都有不依赖它们的等价命令**。

## 理由

1. **可复现性。** 一个项目的第一次运行体验决定了别人是否继续看下去。零依赖 = 必然跑通。
2. **依赖是一种负债。** 每个依赖都带来升级、CVE、传递依赖与锁定成本。这里的需求
   （JSON over HTTP + SQLite）标准库完全覆盖。
3. **面试/外包场景的现实约束。** 客户环境常有代理、离线仓库、老版本 Python；越少依赖越容易落地。

## 后果

**正面**：`git clone && python -m app.cli serve` 即可；CI 无需 `pip install`（这本身就是一个测试：
若任何模块偷偷 import 了第三方库，CI 会立即失败）；容器镜像仅 ~50MB。

**负面**：
- 需要自己实现游标分页、问题详情、请求日志、DI 拼装（约 600 行，均有测试）。
- 没有 Pydantic 的声明式校验，改用领域构造函数 + `ValidationError`，错误信息更贴近业务。
- `sqlite3` 只适合单机；多实例部署需替换仓储适配器。

## 边界

"零依赖"是**运行时**约束，不是教条。以下情况应立刻引入依赖：需要 Postgres 驱动、
需要正则以外的文本处理、需要真实加密、需要 ASGI 生态。届时新依赖只出现在适配层，
领域层继续保持纯净——这正是分层的意义。
