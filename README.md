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
- 保留 MCP `structuredContent` 和原始 `inputSchema` 的自定义 AgentScope `ToolBase`；
- 每次请求核对 OAuth 用户并创建物理隔离的 Toolkit/Agent；
- 受 Session/CSRF 保护的真实 Agent 回复与 `reply_stream()` SSE；
- Codex 风格响应式聊天页面，支持模型对话与 ERPNext Agent 两种模式；
- PostgreSQL 会话/消息持久化、按用户隔离的历史恢复和服务端多轮上下文；
- 历史会话侧栏、会话列表、显式新建与模型/Agent 模式独立会话；
- PostgreSQL HITL Action 状态模型、参数规范化/哈希、本人确认与 CAS 执行占位；
- 环境变量模板、非 root/只读 Agent 镜像、PostgreSQL + Redis Docker Compose；
- PKCE、MCP 响应、工具隔离、意图门控、参数哈希和日志脱敏单元测试。

`POST /api/v1/chat` 已能对查询和巡检请求执行用户专属的模型/工具循环；
`POST /api/v1/chat/stream` 提供文本与工具状态 SSE。创建或修改草稿的请求仍只返回
`action_requires_persistent_approval`，聊天接口不会绕过持久化审批直接执行写工具。

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

- Authorization Code、PKCE S256；本地 Frappe 实例当前使用 Client Secret Post；
- Scope 开发基线为 `all openid`；
- Redirect URI 精确填写 `http://localhost:8001/api/v1/auth/callback`；
- Skip Authorization 关闭，并配置明确的业务角色白名单。

浏览器 OAuth 跳转使用公开地址 `ERPNEXT_BASE_URL=http://dev.localhost:8000`；Agent 容器通过
共享 Docker 网络使用 `ERPNEXT_INTERNAL_URL=http://frappe:8000`，MCP 也使用 `frappe:8000`
服务名。内部请求仍携带公开 Host，确保 Frappe 正确路由到 `dev.localhost` 站点。

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

浏览器访问：

```text
http://localhost:8001/
```

页面采用左侧助手气泡、右侧用户气泡，并实时显示文本增量和工具调用状态。首次打开时点击
“登录 ERPNext”完成 OAuth 登录：

- `模型对话`：默认模式，不调用 ERPNext 工具，用于测试模型连接和多轮上下文；
- `ERPNext Agent`：使用当前登录用户权限执行查询与只读巡检。

两种模式分别维护独立 `conversation_id`。浏览器只提交当前消息与会话 ID；历史消息从
PostgreSQL 加载，并在流式回复完成前落库。刷新页面后，页面先读取会话列表，再恢复该模式
最近或已选中的会话。左侧历史栏支持切换已有会话和显式创建空白新会话。

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
| 基础设施 | `DATABASE_URL`, `REDIS_URL` | Action/token/聊天记录与 Session/state 存储 |
| ERPNext | `ERPNEXT_BASE_URL`, `ERPNEXT_INTERNAL_URL`, `ERPNEXT_MCP_URL`, `ERPNEXT_SITE` | 浏览器地址、容器内地址与 MCP endpoint |
| OAuth | `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_REDIRECT_URI` | 每环境独立 OAuth Client |
| 会话 | `SESSION_SECRET`, `SESSION_COOKIE_*`, `SESSION_TTL_SECONDS` | Agent 浏览器会话 |
| 加密 | `TOKEN_ENCRYPTION_KEY`, `TOKEN_ENCRYPTION_KEY_VERSION` | OAuth token 信封加密入口 |
| 模型 | `MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_API_KEY`, `MODEL_BASE_URL` | AgentScope Model Factory |
| 记忆 | `CHAT_HISTORY_MAX_MESSAGES`, `CHAT_HISTORY_MAX_CHARS`, `CHAT_HISTORY_DISPLAY_LIMIT` | 模型上下文与页面历史上限 |
| 观测 | `OTEL_*` | 下一阶段 OpenTelemetry exporter |

完整默认值与说明见 [`.env.example`](.env.example)。生产环境必须使用 HTTPS、Secure Cookie、
外部 Secret 管理器和正式迁移工具，并设置 `AUTO_CREATE_SCHEMA=false`。

## API 骨架

| 路径 | 状态 |
|---|---|
| `GET /health/live`, `GET /health/ready` | 可用 |
| `GET /api/v1/auth/login`, `GET /api/v1/auth/callback` | 已接线，需真实 ERPNext OAuth 验证 |
| `GET /api/v1/auth/session`, `POST /api/v1/auth/logout` | 可用 |
| `POST /api/v1/chat` | 已接入只读 Data/Patrol Agent 运行时 |
| `POST /api/v1/chat/stream` | 已接入 AgentScope 流式 SSE |
| `POST /api/v1/chat/model/stream` | 已接入 PostgreSQL 历史驱动的无工具模型多轮 SSE |
| `GET /api/v1/chat/history` | 按当前用户、站点和模式恢复持久化消息 |
| `GET /api/v1/chat/conversations` | 返回当前用户指定模式的历史会话列表 |
| `POST /api/v1/chat/conversations` | 创建独立新会话，要求 Session 与 CSRF |
| `GET /`、`GET /assets/*` | 聊天页面与静态资源 |
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

仅验证模型连通性（会实际请求模型服务并产生相应 Token 用量）：

```bash
docker compose --env-file .env run --rm --no-deps agent \
  python -m erpnext_agent.model_smoke
```

页面与受保护 SSE 的联合检查（会创建并自动删除临时 Session，也会实际请求模型）：

```bash
docker compose --env-file .env exec -T agent \
  python -m erpnext_agent.ui_smoke
```

数据库持久化与真实模型记忆检查（两次模型请求会产生 Token 用量；测试会保留聊天记录作为
验证证据，但会删除临时 Redis Session）：

```bash
docker compose --env-file .env exec -T agent \
  python -m erpnext_agent.memory_smoke
```

## 安全边界

项目不接收 ERPNext 用户密码，不提供共享 Administrator/API Key，不开放 submit、cancel、
delete、过账或任意 SQL。Data/Patrol/Orchestrator 在代码层没有写工具；所有 ERPNext 业务文本
都标记为 `untrusted_business_data`。MCP 权限拒绝不会触发高权限身份或 SQL fallback。
