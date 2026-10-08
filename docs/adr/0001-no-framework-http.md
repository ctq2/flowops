# ADR-0001：HTTP 层不使用 Web 框架

- 状态：已接受
- 日期：2026-03-02

## 背景

需要一个 HTTP API 暴露工单用例。默认选择是 FastAPI（或 Flask），能省掉路由、序列化、
校验、OpenAPI 文档等大量样板代码。

## 决策

用标准库 `http.server.ThreadingHTTPServer` 手写适配层（`adapters/http/`），不引入框架。

## 理由

1. **可移植性优先。** 评审者、客户、面试官 `git clone` 后必须能立刻运行。零依赖意味着
   没有版本冲突、没有 venv、没有"在我机器上能跑"。
2. **适配层本就该薄。** 用例在 `application/services` 里，HTTP 层只做四件事：解析、
   调用用例、映射错误、序列化。手写约 400 行即可覆盖，且每一行的意图都清楚。
3. **错误契约完全可控。** RFC 9457 `application/problem+json` 需要稳定的 `type` URI、
   `request_id` 与 `allowed` 之类扩展字段；手写比在框架里改默认异常处理器更直接。
4. **展示能力。** 依赖倒置、`Protocol` 端口、幂等键、ETag 协商、游标分页这些才是要证明的
   东西；框架会把这些藏在装饰器后面。

## 后果

**正面**：零依赖；响应头与状态码完全可控；启动 <100ms；无版本升级负担。

**负面**：
- 没有自动 OpenAPI；替代方案是 `GET /api/v1/meta` 自描述端点（前端与测试都消费它）。
- 需要自己处理 `Content-Length`、`HEAD`、断连等细节——这些都由集成测试覆盖。
- 生产部署若需要 HTTP/2、TLS 终止、连接池，应在前面放反向代理（nginx/Caddy）。

## 何时推翻

出现下列任一情况就该换成 ASGI 框架：需要 WebSocket、需要服务端流式响应、
需要 OIDC 中间件生态、或路由数量超过约 50 条而样板开始重复。
届时只需重写 `adapters/http/`，`application` 与 `domain` 一行不改。
