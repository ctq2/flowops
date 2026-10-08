# 架构说明

## 分层与依赖方向

依赖一律向内：`adapters → application → domain`。`domain` 不 import 任何外层模块（可用
`grep -r "^from \.\.\.\|^from app.adapters" backend/app/domain` 验证为空）。

| 层 | 职责 | 关键约束 |
| --- | --- | --- |
| `domain` | 工单聚合、状态机、风险分档、规则引擎、工作日历 | 纯计算，无 IO、无框架、无全局状态 |
| `application` | 用例编排、端口（Protocol）、审计、幂等、游标分页 | 只依赖端口，不认识 HTTP 与 SQL |
| `adapters` | HTTP 传输、SQLite/内存仓储、CLI | 可替换；两个仓储适配器共用同一套行为测试 |

`bootstrap.py` 是唯一的组合根：只有它知道具体实现如何拼装。测试用内存仓储、CLI 用 SQLite，
都是通过同一个工厂函数，因此测试路径与生产路径的差异被压到最小。

## 一次建单的数据流

```
POST /api/v1/tickets
  → adapters/http/api.py         解析 + Idempotency-Key 校验
  → application TicketService    幂等查表 → 构造聚合 → 引擎求值 → 落库 → 写审计
      → domain Engine.evaluate   规则匹配 → 动作执行（trace）→ SLA 时间戳解析
          → domain BusinessCalendar  工作日/节假日/调休/跨午夜
      → ports TicketStore.save   乐观并发（expected_version）
  → RFC9457 / JSON 响应 + ETag
```

## 为什么这样切

**规则引擎放在 domain，而不是 service。**
策略是业务规则，不是编排逻辑。放在 domain 让它可以脱离数据库与 HTTP 单独测试（现有 60+ 项
引擎测试全部毫秒级完成），也让"求值"保持为纯函数：给定 `(ticket, policy, now)` 必得同一决策。

**`now` 一律显式传入。**
引擎不读系统时钟。这是审计可复现的前提：同一张工单在固定的时间点重放，结果必须逐字节一致
（`test_decision_is_deterministic_for_a_fixed_instant`）。

**SLA 时间戳只重算一次，且不重算时限本身。**
`refresh_sla` 只根据已冻结的 `sla_duration_hours` 重算"何时到期"（例如新增了一段假日），
绝不重新定级或改承诺时限。否则补一次日历数据就会悄悄改写历史，审计失去意义。

**存储层拥有版本单调性。**
`save()` 无论调用方传入什么版本，都只让版本 +1。应用层不需要猜版本号，并发写入无法跳号。

## 扩展点

| 想做什么 | 改哪里 |
| --- | --- |
| 换 Postgres | 实现 `application/ports/store.py` 的 `TicketStore`，替换 `bootstrap.build_store` |
| 换 FastAPI/Starlette | 用 `application/services` 暴露的用例重写 `adapters/http`，领域层不动 |
| 新增规则动作 | 在 `domain/rules/engine.py` 的 `ACTION_CATALOG` 与 `_apply_one` 各加一处，`/meta` 自动暴露 |
| 新增 DSL 函数 | 在 `domain/rules/functions.py` 注册 + 补 `SIGNATURES`，解析器白名单自动生效 |
| 换节假日数据 | 改 `backend/config/calendars/cn-holidays.json`，无需发版 |
| 接消息队列/通知 | 实现 `application/ports/store.py` 的 `EventSink`；引擎已产出结构化 `notifications` |

## 已知取舍

- **列表查询在 Python 侧过滤**（两个适配器共用 `matches()`），换 Postgres 时应下沉到 SQL；
  当前数据量下这是为了保证"两个适配器行为一致"这一更强的不变式。
- **无鉴权**：示例工程范围内刻意省略，`X-Actor` 头承载审计主体。生产接入需在适配层加 OIDC。
- **前端未做虚拟滚动**：默认每页 40 条 + "加载更多"，用于展示游标分页而非取代它。
- **Pyodide 沙盘需要 CDN**：离线时自动降级为只读快照，不会假装求值成功。
