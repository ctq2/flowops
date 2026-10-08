# FlowOps · SLA 工单运营平台

> 一套**零依赖**可运行的工单运营系统：自研规则表达式引擎（词法/语法/求值器）、
> **中国法定节假日与调休感知**的 SLA 时限计算、完整全栈工程化外壳与 236 项自动化测试。

```bash
# 1) 一秒看效果：终端里跑一遍策略推导（无需安装任何依赖）
python -m app.cli demo

# 2) 启动 API + 运营控制台（自动装载演示数据）
python -m app.cli serve --seed
#    -> http://127.0.0.1:8787
```

> 需要 Python 3.11+。**没有 `pip install`，没有 `npm install`，没有构建步骤**——
> 这是刻意设计，理由见 [ADR-0002](docs/adr/0002-zero-dependency-runtime.md)。

---

## 这是什么

大多数"工单系统"作品是 CRUD 加一个看板。FlowOps 的重心放在真正难、也真正值钱的地方：

| 能力 | 为什么它不平凡 |
| --- | --- |
| **规则表达式引擎** | 自己实现词法分析 → 递归下降 + 优先级爬升解析 → 树遍历求值。策略是**数据**，运营改 JSON 即可上线，不需要发版；作者写的字符串永远是数据，不会变成代码路径。 |
| **决策可解释** | 每次求值产出完整 trace：命中哪条规则、执行哪个动作、字段从什么变成什么。有人问"这张单为什么是 P1 且派给支付组？"——界面里直接给答案。 |
| **节假日/调休感知 SLA** | 4 小时时限在国庆前夜提交会顺延到 10 月 9 日；春节调休的周六**计入** SLA，普通周日**不计入**。这两类日期只按"周末休息"的日历会全部算错。 |
| **工作日历可测** | 跨周末、跨长假、跨零点的夜班班次都有测试钉住，含往返一致性（加 N 小时后再量回来必须等于 N）。 |
| **策略静态检查** | `flowops lint` 能证明某条规则"永远不会执行"（被前面的无条件规则遮蔽），并在 CI 里拦住它。 |
| **审计与并发** | 追加式审计表（永不改写）、乐观并发（ETag/If-Match → 412）、幂等键（重放返回原响应，同键异体返回 409）。 |

---

## 快速开始

### 运行

```bash
cd backend

python -m app.cli demo                  # 策略推导演示：每张示例工单的完整决策链路
python -m app.cli serve --seed          # 启动控制台（SQLite 持久化）
python -m app.cli serve --store memory  # 启动控制台（内存，不落盘）
python -m app.cli lint --verbose        # 策略静态检查 + 按执行顺序列出规则
python -m app.cli calendar --limit 12   # 查看年度假日与调休安排
python -m app.cli export                # 生成前端离线快照 + 浏览器内引擎包
```

Windows / PowerShell 用户可用 `dev.ps1`（等价入口）：

```powershell
.\dev.ps1 demo
.\dev.ps1 serve -Port 9000
.\dev.ps1 verify        # lint + 两套测试
. .\dev.ps1 help
```

### 测试与验证

```bash
cd backend && python -m unittest discover -s tests -t . -v   # 236 项（Python）
cd frontend && node --test tests/lib.test.js                 # 28 项（前端纯函数）

python backend/scripts/ci_smoke.py --port 8799               # 59 项真实 HTTP 端到端
```

### Docker

```bash
docker compose up --build     # -> http://localhost:8787
```

---

## 架构

依赖方向一律向内：`adapters → application → domain`。`domain` 不 import 任何外层代码。

```
                       ┌──────────────────────────────────────────────┐
   HTTP / 控制台  ───▶  │  adapters/http   路由 · RFC9457 · ETag ·     │
                       │                  幂等键 · 结构化访问日志      │
                       ├──────────────────────────────────────────────┤
                       │  application     用例 · 端口(Protocol) ·      │
                       │                  审计 · 游标分页 · 幂等重放    │
                       ├──────────────────────────────────────────────┤
                       │  domain          工单聚合 · 状态机 · 风险分档  │
                       │                  ┌────────────────────────┐  │
                       │                  │ 规则引擎                │  │
                       │                  │ lexer → parser → eval   │  │
                       │                  │ 工作日历（节假日/调休）  │  │
                       │                  └────────────────────────┘  │
                       └──────────────────────────────────────────────┘
                                      ▲                    ▲
                       ┌──────────────┴──────┐  ┌──────────┴───────────┐
                       │ SQLite 适配器        │  │ 内存适配器（测试）    │
                       │ WAL · 乐观并发 · 审计│  │ 同一份契约           │
                       └─────────────────────┘  └──────────────────────┘
```

详情与取舍说明：[docs/architecture.md](docs/architecture.md)

### 目录结构

```
flowops/
├── backend/
│   ├── app/
│   │   ├── domain/             # 纯领域：无 IO、无框架
│   │   │   ├── rules/          #   DSL：lexer / parser / ast / evaluator / functions / calendar / engine
│   │   │   ├── ticket.py       #   工单聚合（不可变 + 版本号）
│   │   │   └── values.py       #   状态机、优先级、风险分档
│   │   ├── application/        # 用例与端口
│   │   │   ├── ports/store.py  #   仓储协议（依赖倒置的边界）
│   │   │   └── services/       #   TicketService：triage / transition / refresh / 幂等
│   │   ├── adapters/
│   │   │   ├── http/           #   零依赖 HTTP：路由、problem+json、静态资源
│   │   │   └── persistence/    #   sqlite_store.py · memory.py（同一契约）
│   │   ├── config/             # 策略 JSON、中国节假日日历 JSON
│   │   ├── policy_lint.py      # 策略静态检查（证明不可达规则）
│   │   └── cli.py              # serve / seed / demo / lint / check / calendar / export
│   ├── tests/                  # 236 项：lexer、parser、calendar、engine、app、sqlite、http、lint
│   └── scripts/                # smoke.py · ci_smoke.py · build_frontend_engine.py · build_dist.py
├── frontend/                   # 零构建 ES Modules 控制台
│   ├── index.html
│   ├── src/lib/                # 纯函数：format · risk · catalog · client · python-bridge
│   ├── src/views/              # dashboard · tickets · detail · sandbox · policy · calendar
│   ├── data/snapshot.json      # 离线数据（由真实引擎导出）
│   ├── data/flowops-engine.zip # 同一份 Python 引擎，供浏览器内 Pyodide 运行
│   └── tests/lib.test.js       # 28 项 Node 原生测试
├── docs/                       # 架构 · DSL 规范 · 运维手册 · ADR
├── Dockerfile · docker-compose.yml · Makefile · dev.ps1 · pyproject.toml
└── .github/workflows/ci.yml    # 3 个 Python 版本 + Docker 构建
```

---

## 规则 DSL

策略是 JSON；条件是一门刻意做窄的表达式语言。

```json
{
  "name": "flowops-default",
  "version": "3.2.0",
  "strategy": "first_match",
  "rules": [
    {
      "name": "R02-支付与资金链路",
      "priority": 30,
      "when": "ticket.channel == 'payments' or matches('(支付|退款|对账)', lower(ticket.title))",
      "then": [
        "set_priority:P1",
        "assign_team:payments",
        { "action": "set_sla:duration", "value": "4" },
        { "action": "set_sla:calendar", "value": "business" },
        { "action": "escalate:after", "value": "3" },
        "escalate:to:payments-lead",
        "stop"
      ]
    }
  ],
  "fallback": [{ "name": "R99-兜底分诊", "when": null, "then": ["assign_team:service-desk"] }]
}
```

**语言特性**

- 布尔/比较/算术、成员判断 `in`、列表字面量、点号取字段、函数调用
- 短路求值；`and`/`or`/`not` 同时支持 `&&`/`||`/`!`
- 30 个内置函数：`business_hours_between`、`add_business_hours`、`is_business_day`、
  `in_quiet_hours`、`matches`、`coalesce`…
- 类型严格：`'high' == 'HIGH'` 为 `false`，跨类型排序直接报错而不是悄悄转换
- 嵌套字段缺失解析为 `null`（永不等于任何值），顶层字段拼写错误立即报错
- 求值预算：单次表达式最多 10 万步，避免策略变成 CPU 炸弹
- 动作名唯一明确：`assign_team:payments`（末位冒号后为值）或
  `{"action": "set_sla:duration", "value": "2 * 3"}`（多参数）

完整规范：[docs/rules-dsl.md](docs/rules-dsl.md)

---

## 工程实践

这些不是装饰，每一条都有对应的测试或命令。

| 关注点 | 做法 | 验证方式 |
| --- | --- | --- |
| **错误契约** | 全部失败都是 RFC 9457 `application/problem+json`，带稳定 `type` URI、`request_id` 与机器可读扩展（如 `allowed` 状态列表） | `tests/test_http_api.py` |
| **幂等** | `Idempotency-Key` 重放原响应；同键异体 → 409；记录跨进程重启仍有效 | `test_idempotent_create_survives_a_restart` |
| **乐观并发** | `ETag`/`If-Match`；版本号由存储层单调递增，丢失更新变成 412 而不是数据损坏 | `test_stale_if_match_is_a_precondition_failure` |
| **审计** | 追加式 `audit_log`，(ticket_id, seq) 主键，永不改写；策略指纹随决策落库 | `test_audit_sequence_is_monotonic_and_ordered` |
| **可观测** | 每请求结构化 JSON 日志，含 `request_id`、耗时、状态码；`X-Request-Id` 回显 | `test_request_id_is_echoed_back` |
| **分页** | 游标分页（不透明 base64），过滤器变化时拒绝旧游标，而非返回错乱结果 | `test_cursor_from_different_filters_is_rejected` |
| **契约一致** | 内存与 SQLite 两个适配器共用同一套行为测试；`matches`/排序/分页语义一致 | `tests/test_sqlite_store.py` |
| **策略门禁** | `flowops lint` 证明规则可达性，CI 里作为失败条件 | `tests/test_policy_lint.py` |
| **真实端到端** | 启动真实进程、真实 socket、真实 HTTP 的 59 项检查（含静态资源 Content-Type） | `backend/scripts/smoke.py` |

### 关于"零依赖"

`pyproject.toml` 里 `dependencies = []`，不是偷懒：

1. 评审者/客户/面试官 `git clone` 后能**立刻**跑起来，没有环境问题；
2. 不依赖框架意味着 HTTP 层、仓储层、DI 全部自己写——这恰好是要展示的能力；
3. 依赖越少，长期可维护性越好（[ADR-0002](docs/adr/0002-zero-dependency-runtime.md)）。

需要接入 FastAPI/SQLAlchemy 时，只需实现 `application/ports/store.py` 的协议，
或把 `adapters/http` 换成 FastAPI 路由——用例与领域层一行都不用改。

### 前端

- **零构建** ES Modules + 原生 CSS 变量主题，无打包器、无框架
- 视图拆分：总览 / 工单队列 / 策略沙盘 / 策略与 DSL / 工作日历
- 工单详情含**策略解释**页：重跑策略并逐条展示 `变更前 → 变更后`
- 快捷键：`/` 搜索、`1`–`5` 切换视图、`Esc` 关闭详情
- **双数据源**：检测到后端则实时读写；否则回退到静态快照（只读），`file://` 直接打开也能看
- **策略沙盘跑的是真引擎**：联网时用 Pyodide 加载 `data/flowops-engine.zip`，
  在浏览器里执行**与后端完全同一份 Python 代码**——绝不重写一遍 DSL

---

## API 一览

Base：`/api/v1`。完整字段见 `GET /api/v1/meta`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 存活 + 策略指纹 + 存储类型 |
| `GET` | `/meta` | 状态枚举、动作目录、函数库、编译后的规则 |
| `GET` | `/tickets` | 列表：`status` `priority` `team` `risk` `q` `sort` `limit` `cursor` |
| `POST` | `/tickets` | 建单（自动分诊）；支持 `Idempotency-Key` |
| `GET` | `/tickets/{id}` | 详情（含派生 SLA 字段）；返回 `ETag` |
| `PATCH` | `/tickets/{id}` | 局部更新；`If-Match` 必填才做并发保护 |
| `POST` | `/tickets/{id}/transition` | 状态流转；非法流转 → 409 并附合法目标 |
| `GET` | `/tickets/{id}/audit` | 审计轨迹（按 seq 升序） |
| `POST` | `/policy/preview` | 策略试算：不落库，返回完整 trace + 作用域 |
| `POST` | `/policy/validate` | 校验候选策略（策略编辑器用） |
| `GET` | `/sla/snapshot` | 运营快照：总量、超时、临期、待升级 |
| `GET` | `/metrics` | 按状态/优先级/团队/风险聚合 + SLA 达成率 |

```bash
curl -s localhost:8787/api/v1/tickets?risk=breached&limit=5 | jq '.page.total'
curl -s -X POST localhost:8787/api/v1/policy/preview \
  -H 'content-type: application/json' \
  -d '{"title":"疑似数据泄露，可越权读取订单"}' | jq '.decision.fired'
```

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [架构说明](docs/architecture.md) | 分层、依赖方向、数据流、扩展点、取舍 |
| [规则 DSL 规范](docs/rules-dsl.md) | 语法、类型规则、函数库、动作目录、错误信息 |
| [运维手册](docs/operations.md) | 部署、配置、备份、调参、故障排查 |
| [ADR-0001](docs/adr/0001-no-framework-http.md) | 为什么 HTTP 层不用框架 |
| [ADR-0002](docs/adr/0002-zero-dependency-runtime.md) | 为什么运行时零依赖 |
| [ADR-0003](docs/adr/0003-business-hours-sla.md) | 为什么 SLA 按工作日历计时 |
| [ADR-0004](docs/adr/0004-append-only-audit.md) | 为什么审计表只追加 |

---

## 许可

MIT
