# ERPNext Agent

这是一个基于 AgentScope、FastAPI、PostgreSQL 和 Redis 的 ERPNext Agent 服务。
技术基线固定为 Python 3.12、`agentscope==2.0.5`，并通过现有 ERPNext MCP endpoint
使用当前登录用户权限访问业务数据。

## 当前已经实现

- FastAPI 应用生命周期、存活/就绪检查和安全响应头；
- OAuth 2.0 Authorization Code + PKCE 登录入口与回调；
- OAuth Profile 与 `erpnext_get_current_user` 的身份一致性校验；
- 加密 OAuth token 持久化，以及 Redis 不透明 Session/一次性 OAuth state；
- OAuth token 到期前自动刷新、Redis single-flight 与 MCP 401/403 后单次重试；
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
- 版本化长对话增量摘要、Redis 摘要锁和失败降级，原始消息保持完整；
- 历史会话侧栏、会话列表、显式新建与模型/Agent 模式独立会话；
- PostgreSQL HITL Action、服务端草稿预览、本人确认/拒绝、CAS 执行、幂等键与回读验证；
- Alembic 前向迁移、现有 `create_all()` 数据库安全接管、ORM drift 检查和应用 revision 门禁；
- 环境变量模板、非 root/只读 Agent 镜像、PostgreSQL + Redis Docker Compose；
- PKCE、MCP 响应、工具隔离、意图门控、参数哈希和日志脱敏单元测试。

`POST /api/v1/chat` 已能对查询和巡检请求执行用户专属的模型/工具循环；
`POST /api/v1/chat/stream` 提供文本、工具状态和待审批 Action SSE。创建或修改草稿时，
Action Agent 只持有只读 MCP 工具和本地预览工具；ERPNext 写工具仅由批准后的独立执行入口
使用固定幂等键调用，并在成功后回读确认 `docstatus=0`。

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
make rebuild
```

首次启动、修改 Python 代码、依赖或 Dockerfile 后使用 `make rebuild`；没有代码变化时使用
`make up`，它只启动/协调现有容器，不再强制执行镜像构建。Compose 每次启动会先运行一次性
`migrate` Job；只有迁移成功退出，Agent 才会启动。常用运维命令：

```bash
make up       # 日常启动，不强制构建
make rebuild  # 构建镜像并启动全部服务
make restart  # 只重启 Agent 容器
make logs     # 持续查看 Agent 日志
make ps       # 查看 Compose 服务状态
make down     # 停止并删除 Compose 容器
```

数据库迁移也可以独立执行和检查：

```bash
docker compose run --rm migrate
docker compose run --rm migrate python -m erpnext_agent.migrate check
docker compose run --rm migrate python -m erpnext_agent.migrate current
```

首次 Alembic revision 支持接管早期由 SQLAlchemy `create_all()` 创建的完整结构：接管前逐表
校验列、主键、关键唯一约束和会话外键。部分或不兼容结构会失败关闭；历史
`oauth_credentials` 表被明确视为非托管表，不会删除或修改。迁移采用 PostgreSQL advisory lock
防止多个部署实例并发升级。应用启动时必须读到预期 revision，否则直接拒绝启动。

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
- `ERPNext Agent`：使用当前登录用户权限执行查询、只读巡检和经人工批准的草稿创建/修改。

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
| 基础设施 | `DATABASE_URL`, `REDIS_URL` | Alembic/Action/token/聊天记录与 Session/state 存储 |
| ERPNext | `ERPNEXT_BASE_URL`, `ERPNEXT_INTERNAL_URL`, `ERPNEXT_MCP_URL`, `ERPNEXT_SITE` | 浏览器地址、容器内地址与 MCP endpoint |
| OAuth | `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_REDIRECT_URI`, `OAUTH_REFRESH_*` | 每环境独立 OAuth Client 与刷新策略 |
| 会话 | `SESSION_SECRET`, `SESSION_COOKIE_*`, `SESSION_TTL_SECONDS` | Agent 浏览器会话 |
| 加密 | `TOKEN_ENCRYPTION_KEY`, `TOKEN_ENCRYPTION_KEY_VERSION` | OAuth token 信封加密入口 |
| Action | `ACTION_TTL_SECONDS`, `ACTION_RECOVERY_*`, `ACTION_EXECUTION_LOCK_TTL_SECONDS`, `ACTION_HISTORY_LIMIT` | 审批期限、后台恢复、执行互斥与会话卡片恢复数量 |
| 模型 | `MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_API_KEY`, `MODEL_BASE_URL` | AgentScope Model Factory |
| 记忆 | `CHAT_HISTORY_*`, `CHAT_SUMMARY_*` | 模型上下文、页面历史和自动摘要策略 |
| 观测 | `OTEL_*` | 下一阶段 OpenTelemetry exporter |

完整默认值与说明见 [`.env.example`](.env.example)。生产环境必须使用 HTTPS、Secure Cookie 和
外部 Secret 管理器。`AUTO_CREATE_SCHEMA` 已移除，所有环境统一通过 Alembic `migrate` Job 管理
结构；迁移文件不保存数据库 URL 或密码。

## API 骨架

| 路径 | 状态 |
|---|---|
| `GET /health/live`, `GET /health/ready` | 可用 |
| `GET /api/v1/auth/login`, `GET /api/v1/auth/callback` | 已接线并通过真实 ERPNext OAuth/MCP 身份验证 |
| `GET /api/v1/auth/session`, `POST /api/v1/auth/logout` | 可用 |
| `POST /api/v1/chat` | Data/Patrol 查询及 Action 草稿预览运行时 |
| `POST /api/v1/chat/stream` | 已接入 AgentScope 流式 SSE |
| `POST /api/v1/chat/model/stream` | 已接入 PostgreSQL 历史驱动的无工具模型多轮 SSE |
| `GET /api/v1/chat/history` | 按当前用户、站点和模式恢复持久化消息 |
| `GET /api/v1/chat/conversations` | 返回当前用户指定模式的历史会话列表 |
| `POST /api/v1/chat/conversations` | 创建独立新会话，要求 Session 与 CSRF |
| `GET /`、`GET /assets/*` | 聊天页面与静态资源 |
| `GET /api/v1/approvals/{id}` | 已实现本人可见约束 |
| `GET /api/v1/approvals?conversation_id={id}` | 恢复当前用户、站点和会话的审批卡片及最新状态 |
| `POST /api/v1/approvals/{id}/decision` | 已实现本人确认/拒绝与状态锁 |
| `POST /api/v1/approvals/{id}/execute` | 已接入本人批准、CAS、分布式执行锁、固定幂等键和草稿回读 |

服务启动后会按 `ACTION_RECOVERY_*` 扫描遗留的 `EXECUTING` Action。恢复任务通过
Action 级 Redis 锁与冷却窗口避免跨实例并发重放，重新加载 Action 所有者的加密 OAuth 凭据、
核验 MCP 当前用户后，才使用原参数和原 `idempotency_key` 核对/重试。身份或凭据无法确认时
Action 保持 `EXECUTING` 并记录诊断信息，等待重新登录或人工重试，不会生成新幂等键。
Agent 对话历史加载时会同时读取该会话的 Action 记录，把审批卡片重新挂载到包含对应 Action ID
的助手消息；若流式连接在助手消息落库前中断，则追加一张本地恢复卡片。页面会短轮询仍处于
`EXECUTING` 的记录，直到恢复 Worker 写入终态。

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
