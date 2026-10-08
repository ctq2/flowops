# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] — 2026-03-02

首个可用版本。

### 规则引擎

- 自研规则表达式语言：手写词法分析器、递归下降 + 优先级爬升解析器、树遍历求值器
- 严格类型语义：区分大小写的字符串比较、禁止跨类型排序与字符串/数字相加、
  嵌套字段缺失为 `null` 且永不等于任何值、顶层字段拼写错误立即报错
- 求值预算是硬上限（10 万步），正则长度封顶 200 字符
- 30 个内置函数，含 `business_hours_between`、`add_business_hours`、`is_business_day`、`in_quiet_hours`
- 14 个动作，含分级时限、计时口径切换、暂停计时、升级与通知
- 策略加载期即校验；错误信息带源码定位与 `^` 指示
- `first_match` / `accumulate` 两种匹配策略，规则按 `priority` 排序执行
- 决策全程可解释：命中规则 + 每个动作的「变更前 → 变更后」

### SLA 与工作日历

- 工作日历计时：扣除非工作时段、法定假日，计入调休工作日
- 内置 2025–2026 中国法定节假日与调休安排（纯数据，年度更新无需发版）
- 支持跨周末、跨 8 天长假、跨零点的夜班班次
- 时间戳一律显式传递并统一为 UTC；引擎不读系统时钟，保证决策可复现

### 应用与适配层

- 分层架构：`adapters → application → domain`，依赖倒置由 `Protocol` 端口保证
- 零依赖 HTTP：RFC 9457 问题详情、`ETag`/`If-Match` 乐观并发、`Idempotency-Key` 幂等重放、
  游标分页、结构化访问日志与 `X-Request-Id` 传播
- 双仓储适配器（SQLite / 内存）共用同一套行为测试
- 追加式审计表（永不改写），记录策略指纹与命中规则
- 策略静态检查：证明被前置无条件规则遮蔽的不可达规则

### 运营控制台

- 零构建 ES Modules + 原生 CSS 变量主题，无打包器、无框架
- 五个视图：总览、工单队列、策略沙盘、策略与 DSL、工作日历
- 工单详情含「策略解释」页，逐条展示动作变更
- 双数据源：检测到后端则实时读写，否则回退静态快照（`file://` 亦可打开）
- 策略沙盘通过 Pyodide 在浏览器内运行**同一份** Python 引擎，绝不重写一遍 DSL

### 工程化

- 236 项 Python 测试、28 项前端纯函数测试、59 项真实 HTTP 端到端检查
- 一条命令的验证入口：`make verify` / `.\dev.ps1 verify` / `python backend/scripts/ci_smoke.py`
- Dockerfile、docker-compose、GitHub Actions（3 个 Python 版本 + 镜像构建）
- 可交付演示包：`python backend/scripts/build_dist.py` → `dist/flowops-demo.zip`
- 文档：架构说明、DSL 规范、运维手册、4 篇 ADR

### 已知限制

- 列表过滤在 Python 侧完成，单租户工单量超过约 5 万时应下沉到 SQL
- 无鉴权（示例范围），审计主体由 `X-Actor` 头承载
- 前端未做虚拟滚动，使用「加载更多」展示游标分页
- 浏览器内沙盘需要访问 Pyodide CDN，离线时自动降级为只读快照
