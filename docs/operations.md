# 运维手册

## 部署

```bash
# 方式一：直接跑（推荐，零依赖）
python -m app.cli serve --host 0.0.0.0 --port 8787 --seed

# 方式二：Docker
docker compose up --build -d

# 方式三：交付演示包给客户
python backend/scripts/build_dist.py   # -> dist/flowops-demo.zip（含启动器）
```

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FLOWOPS_DB` | `data/flowops.db` | SQLite 文件路径 |
| `FLOWOPS_POLICY` | `backend/config/policies/default.json` | 策略文件 |
| `FLOWOPS_CALENDAR` | `backend/config/calendars/cn-holidays.json` | 节假日数据 |
| `FLOWOPS_WEB` | `frontend/` | 控制台静态目录 |

任选其一覆盖；也可用命令行 `--store` `--db` `--policy` `--port` `--no-web`。

## 上线前检查

```bash
python -m app.cli lint          # 规则可达性；有 error 时退出码非 0
python -m app.cli check         # 编译 + 示例预演
python backend/scripts/ci_smoke.py   # 真实 HTTP 端到端 59 项
```

CI（`.github/workflows/ci.yml`）在 3 个 Python 版本上跑以上全部，并校验
`frontend/data/` 未过期（改了策略就必须 `make export` 并提交）。

## 备份与恢复

SQLite 使用 WAL。热备份用官方接口，不要直接拷 `.db` 文件：

```bash
sqlite3 data/flowops.db ".backup 'backup-$(date +%F).db'"
```

恢复：停服 → 替换 `data/flowops.db`（并删除同名 `-wal`/`-shm`）→ 启服。
`audit_log` 是纯追加表，可单独导出归档而不影响主表。

## 日常操作

| 场景 | 做法 |
| --- | --- |
| 调整某类工单的时限 | 改策略 JSON 的 `set_sla:duration` → `lint` → 重启；已有工单**不会**被追改 |
| 新增一类节假日 | 改 `cn-holidays.json`，重启后 `POST /api/v1/sla/refresh` 重算到期时间（时限不变） |
| 某策略误判导致大量错派 | 用 `POST /policy/preview` 复现 → 修规则 → `lint` 确认无遮蔽 → 重启 |
| 排查某张工单的判定 | 控制台详情「策略解释」页，或 `GET /tickets/{id}/audit` |
| 观察运行状况 | 日志为逐行 JSON：`{"event":"http_request","request_id":...,"status":...,"duration_ms":...}` |

## 故障排查

| 症状 | 原因与处理 |
| --- | --- |
| 启动报 `policy file not found` | 相对路径按**当前工作目录**解析；用 `--policy` 传绝对路径，或在 `backend/` 下启动 |
| 启动报 `unknown action` | 动作名写错或用了命名空间简写（如 `set_sla:4`）；改用对象写法 `{"action":"set_sla:duration","value":4}` |
| 某条规则永不生效 | `python -m app.cli lint` 会直接指出被哪条无条件规则遮蔽 |
| 工单没有 SLA 时限 | 未命中任何规则且没有 `fallback`；`/sla/snapshot` 将其计为 `untracked` |
| 404 却返回了 HTML | 只可能发生在非 `/api/` 路径（SPA 回退）；API 命名空间永远返回 problem+json |
| PATCH 返回 412 | 版本已变：重新 `GET` 拿新 `ETag` 再写，这是并发保护在正常工作 |
| 沙盘提示引擎不可用 | 静态快照模式且无法访问 Pyodide CDN；启动后端即恢复权威求值 |
| `X-Total-Count` 与列表条数不一致 | 正常：前者是命中总数，后者是当前页条数 |

## 容量参考

演示数据 8 张工单、单机 SQLite：`/tickets` 约 2ms，`/policy/preview` 约 1ms。
列表过滤目前在 Python 侧完成；单租户工单量超过约 5 万时应把过滤下沉到 SQL
（见 [architecture.md](architecture.md) 的"已知取舍"）。
