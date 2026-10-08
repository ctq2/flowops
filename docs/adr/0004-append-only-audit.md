# ADR-0004：审计表只追加，永不改写

- 状态：已接受
- 日期：2026-03-02

## 背景

工单需要历史记录：谁在什么时候做了什么。常见做法有两种：工单表加 `updated_by`/`updated_at`
字段（覆盖式），或单独一张事件表（追加式）。

## 决策

单独的 `audit_log` 表，主键 `(ticket_id, seq)`，**只 INSERT，永不 UPDATE/DELETE**。
每条记录包含动作、操作者、时刻、字段级变更，以及产生该决策的**策略指纹与命中规则**。

## 理由

1. **SLA 争议需要证据。** 客户问"为什么这张单算超时"，答案必须是数据，而不是"当时的代码
   应该是这样算的"。策略指纹把决策钉死在具体版本的规则上。
2. **策略会变，历史不能被追改。** 今天改一条规则，不能让三个月前的定级看起来像是新规则的结果。
   因此 `refresh_sla` 只重算时间戳，绝不重算承诺时限或优先级。
3. **覆盖式字段会丢失信息。** `updated_at` 只能告诉你最后一次变更，无法回答"这张单被谁退回
   过几次"。
4. **追加写在并发下天然安全。** 结合存储层单调的 `version`，写入不产生竞争窗口。

## 后果

**正面**：完整可回溯；可按工单导出归档；审计与主表解耦；变更前/后值可直接在控制台展示。

**负面**：
- 数据量单调增长——需要归档策略（按 `at` 分区或定期导出冷数据），当前实现在
  `audit_trail(limit=200)` 上做了读取上限保护。
- 写入放大：一次建单通常写 2 条（`ticket.created` + `policy.evaluated`），
  一次流转写 1 条并记录 `from`/`to`/备注。
- 快照模式下前端拿不到审计表，界面会明确说明"只读快照不含审计"，而不是伪造时间线。

## 相关实现

- 领域事件来源：`domain/rules/engine.py` 的 `Decision.trace`（规则、动作、变更前、变更后）
- 持久化：`adapters/persistence/sqlite_store.py` 的 `append_audit` / `audit_trail`
- 用例：`application/services/tickets.py` 的 `_audit`
- 测试：`test_audit_sequence_is_monotonic_and_ordered`、`test_audit_trail_records_creation_and_policy_decision`
