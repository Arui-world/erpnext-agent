# ERPNext Agent 开发进度

> 最后更新：2026-08-09  
> 当前阶段：Agent 服务骨架及模型环境配置链路完成  
> 进度记录原则：每次开发任务完成后更新本文，记录实际完成内容、验证证据、遗留项和下一步。

## 一、当前状态

ERPNext Agent 第一阶段工程骨架已经完成。项目目前可以通过 Docker Compose 启动 FastAPI、
PostgreSQL 和 Redis，并具备 OAuth、Session、MCP Adapter、Agent 工具策略和持久化审批等基础
代码边界。

模型 Provider 已经可以从环境变量构造，Agent Factory 也会把模型和用户专属 Toolkit 组装成
四类 Agent。当前聊天接口仍只完成确定性意图门控，尚未接入真实模型调用和完整 AgentScope
ToolBase 运行时。

## 二、已经完成

### 2.1 项目与依赖

- 建立 `src/erpnext_agent` Python package 结构；
- 固定 Python 版本为 `>=3.12,<3.13`；
- 固定 `agentscope==2.0.5`；
- 配置 FastAPI、HTTPX、Redis、SQLAlchemy、asyncpg、cryptography 等依赖；
- 生成并提交 `uv.lock`，锁文件包含 AgentScope 2.0.5；
- 配置 Ruff、Mypy 和 Pytest。
- 实现 `MODEL_PROVIDER`、`MODEL_NAME`、`MODEL_API_KEY`、`MODEL_BASE_URL` 到 AgentScope
  ChatModel 的配置链路；
- 支持 DashScope、OpenAI 和 OpenAI-compatible 三类模型 Provider；
- 模型凭据使用 `SecretStr`，错误信息不输出 API Key；
- Provider 内部重试设为 0，由 `MODEL_MAX_RETRIES` 统一控制 Agent 层重试；
- 每次调用 `ConfiguredAgentFactory.build()` 都创建新的 Agent 会话状态。

### 2.2 FastAPI 接入层

- 完成应用生命周期和依赖初始化；
- 完成 `/health/live` 与 `/health/ready`；
- 完成 Request ID、安全响应头、Trusted Host 和 CORS 基础配置；
- 完成 OAuth、Chat 和 Approval 路由骨架；
- Chat 接口已接入确定性意图门控，并明确标识模型运行时尚未配置。

### 2.3 OAuth 与 Session

- 实现 OAuth 2.0 Authorization Code + PKCE S256；
- 使用一次性 Redis OAuth state 和登录尝试 Cookie；
- OAuth callback 会核对 OpenID Profile 与 MCP 当前用户；
- OAuth access/refresh token 使用 Fernet 加密后存入 PostgreSQL；
- 浏览器只保存不透明 HttpOnly Agent Session Cookie；
- Redis Session key 使用服务端 HMAC，不保存明文 Session ID；
- 状态变更接口使用 CSRF token；
- 实现本地登出、凭据失效和 ERPNext token revoke 调用。

### 2.4 MCP 接入边界

- 实现每用户 AgentScope MCP Client 构造函数；
- 实现 HTTP、JSON-RPC、MCP `isError`、业务信封 `ok` 四层响应检查；
- 正常结果只消费 `structuredContent`，避免重复注入文本副本；
- 业务数据统一标记为 `untrusted_business_data`；
- 运行时发现工具后可核对既定的 13 个 MCP 工具；
- Data、Patrol 和 Orchestrator 不持有写工具；
- Action Agent 只允许两个草稿写工具；
- submit、cancel、delete 被列为禁止能力。

### 2.5 Agent 与 HITL

- 建立 Orchestrator、Data、Action 和 Patrol Agent 构造边界与系统提示；
- 建立保守的确定性意图门控；
- 建立持久化 Action 数据模型和状态枚举；
- 实现规范化参数、SHA-256 参数摘要、审批 TTL 和幂等键；
- 实现请求者本人查看、确认和拒绝审批；
- 实现 APPROVED → EXECUTING 的数据库 CAS；
- 草稿写入成功后通过 `erpnext_get_doc` 回读并验证 `docstatus=0`；
- 对不确定的写入结果保留 EXECUTING 状态，避免更换幂等键重复写入。

### 2.6 配置与容器

- 编写 `.env.example`，覆盖应用、数据库、Redis、ERPNext、OAuth、Session、模型和 OTel 配置；
- 编写 Dockerfile，使用 Python 3.12 和冻结依赖；
- Agent 容器使用 UID 10001、只读文件系统、临时 `/tmp`、移除 Linux capabilities；
- 编写 PostgreSQL、Redis、Agent 三服务 Compose；
- 为数据库、Redis 和 Agent 配置健康检查与持久化数据卷。

## 三、验证证据

| 检查项 | 结果 |
|---|---|
| `uv.lock` 依赖解析 | 通过，锁定 AgentScope 2.0.5 |
| Pytest | 21 项通过，包含环境变量到模型/Agent 的构造测试 |
| Ruff | 通过，无问题 |
| Mypy strict | 通过，检查 46 个源码文件 |
| Docker Compose config | 通过 |
| Docker 镜像构建 | 通过 |
| PostgreSQL 初始化/建表 | 通过 |
| Redis 连接 | 通过 |
| Agent 容器启动 | 通过 |
| `/health/ready` | PostgreSQL、Redis 均返回 `ok` |

验证结束后已删除测试容器、测试网络和测试数据卷，没有遗留后台服务或测试数据。

## 四、尚未完成

- 尚未实现保留 `structuredContent` 的 AgentScope 自定义 `ToolBase`；
- 尚未实现完整的多轮消息存储、摘要和流式 SSE/WebSocket；
- 尚未实现 access token 自动刷新、分布式 single-flight 和 MCP 401 后单次重试；
- 尚未实现 EXECUTING Action 的重启恢复和后台核对任务；
- 尚未使用两个真实 ERPNext 用户完成 OAuth、权限差分和跨用户隔离测试；
- 尚未建立 Alembic 等正式数据库迁移流程；
- OpenTelemetry 当前只有接入边界，尚未配置正式 exporter 和 Trace；
- 40 个评估场景目前只有目录骨架，尚未实现 Runner 与报告。

## 五、下一阶段建议

1. 实现 ERPNext MCP 自定义 ToolBase，保留 structuredContent 和领域错误；
2. 使用真实 OAuth 用户打通 Data Agent 的最小只读闭环；
3. 将 `reply_stream()` 事件接入聊天 SSE API；
4. 增加 Token 自动刷新、single-flight 和身份一致性故障测试；
5. 实现聊天消息持久化与会话恢复；
6. 增加正式数据库迁移和 Action 重启恢复流程；
7. 开始构建第一批真实查询与安全评估场景。

## 六、进度文档维护约定

后续每次开发任务完成时，至少更新以下内容：

- `最后更新` 和 `当前阶段`；
- 本次新增或修改的能力；
- 实际执行的测试、静态检查和运行验证；
- 仍未完成或无法验证的边界；
- 推荐的下一步开发顺序。

未经过测试或真实运行验证的能力，不记录为“已经完成”。
