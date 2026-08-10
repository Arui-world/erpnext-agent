# ERPNext Agent 开发进度

> 最后更新：2026-08-10
> 当前阶段：真实 OAuth 身份下的物料多仓正库存自动查询完成
> 进度记录原则：每次开发任务完成后更新本文，记录实际完成内容、验证证据、遗留项和下一步。

## 一、当前状态

ERPNext Agent 第一阶段工程骨架和第二阶段最小只读运行时已经完成。项目可以通过 Docker
Compose 启动 FastAPI、PostgreSQL 和 Redis，并具备 OAuth、Session、MCP Adapter、Agent
工具策略和持久化审批等基础代码边界。

模型 Provider 已经可以从环境变量构造。登录用户发起查询或巡检时，服务会读取其加密 OAuth
凭据、核对 MCP 当前用户、发现工具并创建本次请求专属的 Toolkit 与 Agent。普通回复和
`reply_stream()` SSE 均已接入 Chat API；草稿写入仍被持久化审批边界阻断，不会从聊天接口
直接执行。

服务根路径已提供响应式聊天工作台。页面默认进入无工具的“模型对话”模式，也可以切换到
“ERPNext Agent”模式；助手消息位于左侧，用户消息位于右侧，并支持流式文本、工具状态、
多轮模型上下文和 OAuth 登录状态展示。浏览器不再上传历史内容，只携带当前消息与
`conversation_id`；服务端从 PostgreSQL 恢复用户/站点/模式隔离的历史并交给新建 Agent。
左侧历史侧栏会列出持久化会话，支持切换和创建独立新对话，页面刷新时恢复最近会话。

真实 ERPNext OAuth 用户的库存查询已完成单仓和多仓两条端到端链路。指定精确仓库时使用
`erpnext_get_stock_balance`；只提供物料编码或唯一物料名称时，后端会直接以当前 OAuth
身份查询 Item 和 `actual_qty > 0` 的 Bin，返回所有正库存仓库，不再要求用户先指定仓库。

## 二、已经完成

### 2.1 项目与依赖

- 建立 `src/erpnext_agent` Python package 结构；
- 固定 Python 版本为 `>=3.12,<3.13`；
- 固定 `agentscope==2.0.5`；
- 配置 FastAPI、HTTPX、Redis、SQLAlchemy、asyncpg、cryptography 等依赖；
- 生成并提交 `uv.lock`，锁文件包含 AgentScope 2.0.5；
- 配置 Ruff、Mypy 和 Pytest；
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
- Chat 接口已接入确定性意图门控和真实 AgentScope 回复运行时；
- `POST /api/v1/chat/stream` 已把文本增量、工具开始/结束和完成事件转换为 SSE；
- Chat 和 SSE 均使用 OAuth Session 与 CSRF 防护；
- 查询/巡检中的模型或 MCP 失败对客户端返回稳定错误，不泄露凭据和底层连接信息。
- 流式 Agent 在结束事件中没有文本时，会返回并持久化可重试提示，不再向前端透传空回复；
- Chat 和 SSE 对常见“物料的库存”表达接入确定性多仓查询，流中仍返回工具状态和完成事件；
- 新增受 Session/CSRF 保护的 `/api/v1/chat/model/stream`，用于无工具模型多轮对话；
- 新增根路径聊天页面和本地 CSS/JavaScript 静态资源，不依赖外部 CDN；
- 页面支持模型/ERPNext Agent 模式切换、快捷问题、流式文本、工具状态和响应式布局。

### 2.3 OAuth 与 Session

- 实现 OAuth 2.0 Authorization Code + PKCE S256；
- 使用一次性 Redis OAuth state 和登录尝试 Cookie；
- OAuth callback 使用 `frappe.auth.get_logged_user` 的规范 User ID 核对 MCP 当前用户；
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
- 实现自定义 `ERPNextMCPTool(ToolBase)`，原样保留服务器 `inputSchema`；
- ToolBase 只向模型传递结构化 `data`、内容信任标记和白名单元数据；
- MCP 领域错误转换为 AgentScope 错误 ToolChunk，并保留安全的 `trace_id`；
- 同步 MCP 工具执行完成后显式返回 `ToolResultState.SUCCESS`，避免成功结果被误判为
  `RUNNING` 而触发 ReAct 迭代上限；
- Data、Patrol 和 Orchestrator 不持有写工具；
- Action Agent 只允许两个草稿写工具；
- 只读工具权限为 `ALLOW`，写工具强制为不可绕过的 `ASK`；
- submit、cancel、delete 被列为禁止能力。

### 2.5 Agent 与 HITL

- 建立 Orchestrator、Data、Action 和 Patrol Agent 构造边界与系统提示；
- 建立保守的确定性意图门控；
- 当前消息意图不完整时，只继承最近一条明确的用户意图；当前消息的禁止或写操作关键词仍优先；
- 指定仓库时使用单仓库存工具；未指定仓库但物料明确时自动查询正库存 Bin；
- 每次请求创建独立 Agent 与 Toolkit，不共享用户 access token 或会话状态；
- Agent 运行前再次核对 Agent Session 用户与 MCP 当前用户；
- Data/Patrol 已接入模型工具循环，Action 意图在聊天层停在持久化审批入口；
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
- Compose 和镜像健康检查同时验证 API readiness 与根路径聊天页面。
- 优化 Makefile 启动入口：`make up` 不再强制构建，`make rebuild` 专用于代码/依赖变化；
- 新增 `make restart`、`make logs` 和 `make ps` 日常运维命令。

### 2.7 消息持久化与模型记忆

- 新增 `chat_conversations` 与 `chat_messages` PostgreSQL 表；
- 会话按 ERPNext site、登录用户和 `model`/`agent` 模式隔离；
- 每条消息保存 UUID、递增 sequence、角色、正文和 UTC 时间；
- 新请求只接受当前消息和可选 `conversation_id`，不接受客户端提供的历史作为模型上下文；
- 每轮调用前从数据库加载最近消息，受消息数与字符数双重预算限制；
- 用户消息在模型调用前提交，助手消息在流式 `done` 事件前提交；
- `GET /api/v1/chat/history` 支持页面刷新和模式切换后的历史恢复；
- 新增 `GET/POST /api/v1/chat/conversations` 会话列表与创建接口；
- 会话列表标题由首条用户消息安全派生，展示消息数与更新时间；
- 页面分别保留模型模式与 ERPNext Agent 模式的会话 ID；
- 左侧响应式历史栏支持新建、切换和移动端展开/收起；
- 新增 `memory_smoke`，用随机码执行两轮真实模型调用并直接回查 PostgreSQL。

### 2.8 确定性多仓库存

- 支持“`test item1的库存`”、“查询物料 `test item1` 库存”等常见无仓库表达；
- 优先按 Item `name` 精确匹配，未命中时按 `item_name` 匹配；
- 物料唯一时自动分页查询 Bin，强制 `item_code` 匹配且 `actual_qty > 0`；
- 返回每个正库存仓库的实际、预留和预计数量，并计算实际库存合计；
- 空结果明确说明无正库存，多个同名物料则请用户选择精确物料编码；
- 查询前仍核对 Agent Session 与 MCP 当前用户，Item/Bin 列表仍由 Frappe RBAC 和 User Permission 裁剪；
- 每页 100 条、最多 10 页，超过 1000 条时明确标记截断；
- 确定性回复在 SSE `done` 前写入 PostgreSQL，不依赖大模型规划这一固定查询。

## 三、验证证据

| 检查项 | 结果 |
|---|---|
| `uv.lock` 依赖解析 | 通过，锁定 AgentScope 2.0.5 |
| Pytest | 47 项通过，包含物料提取、OAuth 身份、Item/Bin 参数和多仓回复测试 |
| Ruff | 通过，无问题 |
| Mypy strict | 通过，检查 57 个源码文件 |
| Docker Compose config | 通过 |
| Docker 镜像构建 | 通过，生成 `erpnext-agent:local` |
| 真实模型请求 | 通过，容器实际收到回复“千问模型连接成功。” |
| 页面访问 | `/`、CSS、JavaScript 均返回 HTTP 200 和正确 Content-Type |
| 页面真实流式对话 | 通过受保护临时 Session 调用页面 SSE，模型回复“前端对话连接成功。” |
| PostgreSQL 初始化/建表 | 通过，存在 `chat_conversations` 与 `chat_messages` |
| 真实消息落库 | 通过，同一会话直接查询得到 4 条、角色顺序为 user/assistant/user/assistant |
| 真实模型记忆 | 通过，第二次请求未携带历史正文，模型准确返回第一轮运行时随机码 |
| 真实 OAuth 与 MCP 身份 | 通过，Administrator Agent Session 与 MCP 当前用户一致 |
| 多轮库存查询 | 通过，三轮澄清后单次调用 `erpnext_get_stock_balance` |
| 真实库存结果 | `test item1` / `Stores - TQC` 返回 `0.0 Nos`、库存价值 `0.0 INR` |
| Agent 会话落库 | 通过，三轮会话持久化 6 条 user/assistant 消息 |
| 无仓库物料库存请求 | 通过，只输入“`test item1的库存`”，未追问仓库 |
| 真实多仓正库存 | 返回 `仓库 - rw`：实际 `10.0 Nos`、预留 `0.0 Nos`、预计 `10.0 Nos` |
| 多仓会话落库 | 通过，会话 `10279862-b845-451c-b4e6-587a2c590a60` 持久化 2 条消息 |
| 会话列表与新建 | 通过，列表返回 4 条消息计数，新建会话获得独立 UUID |
| 前端静态资源 | Node 语法检查通过，运行中 HTML/CSS/JS 均包含新版多会话组件 |
| Redis 连接 | 通过 |
| Agent 容器启动 | 通过，Agent、PostgreSQL、Redis 均为 healthy |
| `/health/ready` | Redis/Database 为 `ok`，`model_configured` 与 `agent_chat_runtime` 为 `true` |

验证完成后保留最新 Compose 服务运行，便于继续 OAuth 和真实 ERPNext 联调。

## 四、尚未完成

- 尚未实现长会话自动摘要、会话重命名/删除 UI 和正式数据保留策略；
- 尚未实现 access token 自动刷新、分布式 single-flight 和 MCP 401 后单次重试；
- 尚未实现 EXECUTING Action 的重启恢复和后台核对任务；
- 尚未使用两个真实 ERPNext 用户完成权限差分和跨用户隔离测试；
- 尚未建立 Alembic 等正式数据库迁移流程；
- OpenTelemetry 当前只有接入边界，尚未配置正式 exporter 和 Trace；
- 40 个评估场景目前只有目录骨架，尚未实现 Runner 与报告。
- 确定性多仓查询当前为常见无仓库表达提取，更复杂的口语化复合问题仍交给 Data Agent；

## 五、下一阶段建议

1. 使用第二个低权限用户验证权限差分和跨用户隔离；
2. 增加 Token 自动刷新、single-flight 和 MCP 401 后单次重试；
3. 把草稿预览接入持久化 Action 审批与执行入口；
4. 在现有持久化消息基础上实现自动摘要、会话重命名/删除和数据保留策略；
5. 增加正式数据库迁移和 Action 重启恢复流程；
6. 将单仓与多仓真实库存查询纳入可重复的集成测试 Runner，并开始构建安全评估场景。

## 六、进度文档维护约定

后续每次开发任务完成时，至少更新以下内容：

- `最后更新` 和 `当前阶段`；
- 本次新增或修改的能力；
- 实际执行的测试、静态检查和运行验证；
- 仍未完成或无法验证的边界；
- 推荐的下一步开发顺序。

完成代码、测试和进度文档后，还必须创建 Git 提交并推送到当前远程分支；如果远程认证或
网络异常导致推送失败，应保留本地提交并明确报告阻塞原因，不得把“仅本地提交”描述为已经
同步到远程。

未经过测试或真实运行验证的能力，不记录为“已经完成”。
