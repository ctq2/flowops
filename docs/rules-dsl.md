# 规则 DSL 规范

策略是 JSON 文档；条件是一门刻意做窄的表达式语言。完整可用动作与函数以运行时导出为准：
`GET /api/v1/meta`，或 `python -m app.cli policy`。

## 文档结构

```json
{
  "name": "flowops-default",
  "version": "3.2.0",
  "strategy": "first_match",
  "calendar": { "tz_offset_minutes": 480, "day_start": "09:00", "day_end": "18:00",
                "working_days": ["mon","tue","wed","thu","fri"],
                "holidays": ["2026-10-01"], "make_up_workdays": ["2026-02-28"] },
  "defaults": { "sla_duration_hours": 24, "sla_calendar": "business" },
  "rules": [ { "name": "...", "priority": 10, "when": "...", "then": ["..."] } ],
  "fallback": [ { "name": "R99-兜底", "when": null, "then": ["assign_team:service-desk"] } ]
}
```

- `strategy`：`first_match`（首个命中即停止，**规则顺序即优先级**）或 `accumulate`（全部命中依次执行）。
- `priority`：决定**执行顺序**（小者先），文件顺序仅用于同优先级时的稳定排序。
- 加载期即校验：未知动作、动作参数个数、非法优先级、条件语法错误、重名规则都会让**整份策略**加载失败
  （而不是某张工单运行时报错）。错误信息带源码定位与 `^` 指示。

## 语法

```
expr        := or_expr
or_expr     := and_expr (("or" | "||") and_expr)*
and_expr    := not_expr (("and" | "&&") not_expr)*
not_expr    := ("not" | "!") not_expr | comparison
comparison  := additive (("==" | "!=" | "<" | "<=" | ">" | ">=" | "in") additive)?
additive    := multiplicative (("+" | "-") multiplicative)*
multiplicative := unary (("*" | "/" | "%") unary)*
unary       := "-" unary | postfix
postfix     := primary ("." IDENT | "(" args ")")*
primary     := NUMBER | STRING | true | false | null | IDENT
             | "[" (expr ("," expr)*)? "]" | "(" expr ")"
```

注释：`#` 或 `--` 至行尾。

## 类型规则（严格，不隐式转换）

| 表达式 | 结果 |
| --- | --- |
| `'high' == 'HIGH'` | `false`（区分大小写） |
| `1 == 1.0` | `true` |
| `true == 1` | `false` |
| `1 < 'a'` | **报错**（跨类型排序） |
| `'a' + 1` | **报错**（字符串与数字不可相加） |
| `ticket.labels.missing` | `null`，且**不等于任何值** |
| `tickt.priority` | **报错**（顶层字段拼写错误） |

求值预算 100000 步，超出抛 `RuleBudgetExceeded`；`matches()` 的正则长度上限 200 字符。

## 作用域

| 字段 | 说明 |
| --- | --- |
| `ticket.*` | 工单当前状态（含规则链上前序动作已产生的修改） |
| `now` | 本次求值的冻结时刻 |
| `age_hours` | 距创建时间的自然小时数 |
| `business_age_hours` | 距创建时间的工作小时数 |
| `is_business_day` / `in_quiet_hours` | 当前时刻是否工作日 / 是否非工作时段 |
| `policy.name` `policy.version` `policy.hash` `policy.calendar.*` | 策略元信息 |
| `operator.*` | 调用方传入的操作者上下文（可选） |

## 函数库（节选）

```
business_hours_between(a, b)   工作小时数（扣除节假日，计入调休）
add_business_hours(ts, h)      从 ts 起推进 h 个工作小时
is_business_day([ts])          节假日 false，调休工作日 true
in_quiet_hours([ts])           非工作时段为 true
hours_since(ts[, ref])         自然小时差
matches(pattern, s)            正则搜索（长度受限）
contains / startswith / endswith / lower / upper
coalesce / len / int / float / str / min / max / abs / round
date_part('hour', ts) / weekday([ts])
```

## 动作

字符串简写取**末位冒号之后**为值（值本身不再切分），因此 `escalate:to:oncall-sre`、
`set_sla:calendar:business` 都无歧义；需要多参数时用对象写法。

```
set_priority:P1                        优先级 P0..P3
assign_team:payments                   路由团队
{"action":"set_sla:duration","value":"4"}      承诺时限（小时；值可为表达式）
{"action":"set_sla:calendar","value":"business"} business | wall
{"action":"set_sla:snooze","value":3}          暂停计时（小时）
{"action":"escalate:after","value":"0.5"}      升级延迟（小时）
escalate:to:oncall-sre                 升级对象
{"action":"escalate:notify","value":["im","#ops"]}  记录通知（不真正外发，由适配层消费）
tag:vip                                打标签
{"action":"label","value":["category","defect"]}    键值标签
require_review:需值班经理确认            要求人工复核
note:未命中任何规则                     写入决策备注
stop                                   停止后续规则
sla_recompute                          按当前草稿重算到期时间
```

## 排查

```bash
python -m app.cli lint --verbose     # 规则可达性 + 按执行顺序列出
python -m app.cli demo               # 每张示例工单的完整 trace
python -m app.cli check              # 编译检查 + 示例预演（CI 用）
```

界面上的「策略沙盘」与工单详情的「策略解释」页都能看到同一份 trace。
