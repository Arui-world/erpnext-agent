# ERPNext Agent

这是依据 [`docs/ERPNext-Agent-开发方案.md`](docs/ERPNext-Agent-开发方案.md) 搭建的第一阶段服务骨架。
技术基线固定为 Python 3.12、`agentscope==2.0.5`、FastAPI、PostgreSQL、Redis，以及现有的
ERPNext MCP endpoint。

## 当前已经实现

- FastAPI 应用生命周期、存活/就绪检查和安全响应头；
- OAuth 2.0 Authorization Code + PKCE 登录入口与回调；
- OAuth Profile 与 `erpnext_get_current_user` 的身份一致性校验；
- 加密 OAuth token 持久化，以及 Redis 不透明 Session/一次性 OAuth state；
- MCP HTTP、JSON-RPC、`isError`、`structuredContent.ok` 四层失败归一化；
- Data、Action、Patrol、Orchestrator 的代码级工具白名单；
- AgentScope 2.0.5 Agent/MCP Client 构造边界；
- 从 `.env` 构造 DashScope、OpenAI 或 OpenAI-compatible 模型的 Model Factory；
- 使用环境配置和用户专属 Toolkit 创建四类 Agent 的 `ConfiguredAgentFactory`；
- PostgreSQL HITL Action 状态模型、参数规范化/哈希、本人确认与 CAS 执行占位；
- 环境变量模板、非 root/只读 Agent 镜像、PostgreSQL + Redis Docker Compose；
- PKCE、MCP 响应、工具隔离、意图门控、参数哈希和日志脱敏单元测试。

`POST /api/v1/chat` 目前只执行确定性策略门控，会明确返回
`runtime_not_configured`；模型配置与 Agent 构造已经接通，但保留 `structuredContent` 的
AgentScope `ToolBase` 桥接、用户 Toolkit 组装和流式事件尚未接入 Chat API。骨架不会把这些
能力伪装成已完成。

## 本地配置

要求 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。先生成只用于本机的配置：

```bash
cp .env.example .env
openssl rand -hex 32
openssl rand -base64 32 | tr '+/' '-_'
```

把第一条输出填入 `SESSION_SECRET`，第二条输出填入 `TOKEN_ENCRYPTION_KEY`，并修改数据库、
Redis、OAuth Client 与模型相关占位值。不要提交 `.env`。

ERPNext 中的 OAuth Client 至少应配置：

- Authorization Code、Client Secret Basic、PKCE S256；
- Scope 开发基线为 `all openid`；
- Redirect URI 精确填写 `http://localhost:8001/api/v1/auth/callback`；
- Skip Authorization 关闭，并配置明确的业务角色白名单。

容器内访问宿主机 ERPNext 的默认地址是 `http://host.docker.internal:8000`。如果 ERPNext
也在 Compose 网络内运行，请把 `ERPNEXT_BASE_URL` 和 `ERPNEXT_MCP_URL` 改成对应服务名。

## 启动

```bash
uv sync --frozen
uv run pytest
docker compose --env-file .env config --quiet
docker compose up --build -d
```

验证：

```bash
curl http://localhost:8001/health/live
curl http://localhost:8001/health/ready
```

登录入口是：

```text
GET http://localhost:8001/api/v1/auth/login
```

登录完成后，浏览器只持有 HttpOnly Session Cookie。客户端通过
`GET /api/v1/auth/session` 取得 CSRF token，并在聊天、审批、退出等状态变更请求中发送
`X-CSRF-Token`。

## 核心环境变量

| 分组 | 变量 | 说明 |
|---|---|---|
| 基础设施 | `DATABASE_URL`, `REDIS_URL` | Action/token 与 Session/state 存储 |
| ERPNext | `ERPNEXT_BASE_URL`, `ERPNEXT_MCP_URL`, `ERPNEXT_SITE` | ERP 站点与 MCP endpoint |
| OAuth | `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_REDIRECT_URI` | 每环境独立 OAuth Client |
| 会话 | `SESSION_SECRET`, `SESSION_COOKIE_*`, `SESSION_TTL_SECONDS` | Agent 浏览器会话 |
| 加密 | `TOKEN_ENCRYPTION_KEY`, `TOKEN_ENCRYPTION_KEY_VERSION` | OAuth token 信封加密入口 |
| 模型 | `MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_API_KEY`, `MODEL_BASE_URL` | AgentScope Model Factory |
| 观测 | `OTEL_*` | 下一阶段 OpenTelemetry exporter |

完整默认值与说明见 [`.env.example`](.env.example)。生产环境必须使用 HTTPS、Secure Cookie、
外部 Secret 管理器和正式迁移工具，并设置 `AUTO_CREATE_SCHEMA=false`。

## API 骨架

| 路径 | 状态 |
|---|---|
| `GET /health/live`, `GET /health/ready` | 可用 |
| `GET /api/v1/auth/login`, `GET /api/v1/auth/callback` | 已接线，需真实 ERPNext OAuth 验证 |
| `GET /api/v1/auth/session`, `POST /api/v1/auth/logout` | 可用 |
| `POST /api/v1/chat` | 仅策略门控，模型运行时待接入 |
| `GET /api/v1/approvals/{id}` | 已实现本人可见约束 |
| `POST /api/v1/approvals/{id}/decision` | 已实现本人确认/拒绝与状态锁 |
| Action 写执行 | 未对外开放，待补齐回读验证和故障恢复 |

模型配置示例：

```dotenv
# DashScope
MODEL_PROVIDER=dashscope
MODEL_NAME=qwen-plus
MODEL_API_KEY=replace-with-real-key
MODEL_BASE_URL=

# 或 OpenAI-compatible 服务
MODEL_PROVIDER=openai_compatible
MODEL_NAME=replace-with-model-name
MODEL_API_KEY=replace-with-real-key
MODEL_BASE_URL=https://model-service.example/v1
```

`MODEL_PROVIDER=openai` 使用 AgentScope 官方 `OpenAIChatModel` 默认地址；
`openai_compatible` 必须明确配置 `MODEL_BASE_URL`。服务的 `/health/ready` 会返回不包含密钥的
`model_configured` 状态。模型内部重试关闭，统一由 `MODEL_MAX_RETRIES` 配置 Agent 层重试，
避免两层重试叠加。

## 安全边界

项目不接收 ERPNext 用户密码，不提供共享 Administrator/API Key，不开放 submit、cancel、
delete、过账或任意 SQL。Data/Patrol/Orchestrator 在代码层没有写工具；所有 ERPNext 业务文本
都标记为 `untrusted_business_data`。MCP 权限拒绝不会触发高权限身份或 SQL fallback。
