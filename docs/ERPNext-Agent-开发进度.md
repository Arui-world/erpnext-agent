# ERPNext Agent 开发进度

> 最后更新：2026-08-22
> 当前阶段：仓库简称运行时加固完成，物料组库存组合查询规则已恢复，待真实复测
> 进度记录原则：每次开发任务完成后更新本文，记录实际完成内容、验证证据、遗留项和下一步。

## 一、当前状态

### 2026-08-22 物料组库存组合查询边界修正

此前对“物料组 + 库存”请求做了过度保守的硬阻断，现已撤销。现有 MCP 工具可以组合完成该查询：
先用 `erpnext_get_list` 按 `item_group` 查询真实 Item 编码，再逐个调用
`erpnext_get_item_stock_by_warehouses`，最后仅依据返回的 `totals.actual_qty` 应用阈值。Data Agent
提示词已固定该顺序，并明确禁止编造物料编码或库存数量。此次仅完成规则与回归测试，尚未完成真实
OAuth 对话的多物料组库存端到端复测，因此不标记为最终验收完成。

### 2026-08-22 仓库简称运行时强制解析

本次新增 Action 请求运行时上下文：普通 Chat 与 SSE 在调用 Action Agent 前均注入确定性规则，
明确不带公司后缀的仓库名不属于缺失参数，模型不得要求用户手工补充完整名称；提案服务继续通过
当前用户公司简称和权限感知 Warehouse 查询执行唯一别名解析。此前已在真实 bench 验证
Administrator 的全局默认公司“锐雯”、简称 `rw`，并确认 `仓库 - rw` 唯一存在。

已执行验证：`.venv/bin/pytest -q tests/unit/test_prompts.py tests/unit/test_actions.py`
结果为 36 项通过；Agent Docker 镜像已重建，容器状态为 healthy。尚未完成使用真实 OAuth 用户
重新提交 Material Request 并完成审批执行的端到端复测，因此本能力暂不标记为最终验收完成。

### 2026-08-22 仓库简称真实数据验证

已在运行中的 Frappe bench 使用 `dev.localhost` 真实数据库验证：当前 Administrator 用户的
`erpnext_get_user_business_context` 返回全局默认公司“锐雯”、简称 `rw`；Warehouse 查询
`name like "仓库 - rw%"` 返回唯一记录“仓库 - rw”。失败不是 ERPNext 数据问题，而是
Action Agent 在调用提案工具前把仓库别名误判为缺失参数。已强化 Action 提示词：不带公司后缀的
仓库名不属于缺失参数，必须继续调用 `erpnext_propose_draft_action`，由服务端完成唯一别名解析。

### 2026-08-22 公司上下文与简称解析决策

针对 ERPNext 仓库等字段经常带有公司简称后缀的问题，已确定由 MCP 扩展权限感知的
`erpnext_get_user_business_context` 只读工具：MCP 根据当前 OAuth 用户的 Employee.company
解析公司，无员工或员工公司为空时回退 ERPNext 全局默认公司，并返回公司简称及来源。Agent
只消费该结构化上下文，不在提示词或本地代码中猜测、拼接或跨权限查询。当前仓库未包含 ERPNext
MCP 服务端实现，因此本轮先固定接口契约；待 MCP 服务端上线后，再接入 Agent 启动上下文和
仓库 Link 的简称解析回归测试。

ERPNext Agent 第一阶段工程骨架和第二阶段最小只读运行时已经完成。项目可以通过 Docker
Compose 启动 FastAPI、PostgreSQL 和 Redis，并具备 OAuth、Session、MCP Adapter、Agent
工具策略和持久化审批等基础代码边界。

模型 Provider 已经可以从环境变量构造。登录用户发起查询、巡检或草稿操作时，服务会读取其加密 OAuth
凭据、核对 MCP 当前用户、发现工具并创建本次请求专属的 Toolkit 与 Agent。普通回复和
`reply_stream()` SSE 均已接入 Chat API。Action Agent 只能使用只读 MCP 工具和本地预览工具；
参数完整后生成持久化 PENDING Action，前端展示批准/拒绝卡片，只有本人批准后才由独立 API
使用固定幂等键执行 ERPNext 草稿写工具并回读验证。

服务启动后还会扫描遗留的 `EXECUTING` Action。人工执行入口与后台 Worker 共用 Action 级
Redis 分布式锁，后台重试带跨实例冷却窗口；每次恢复都会重新加载 Action 所有者的加密 OAuth
凭据、核验 MCP 当前用户，并且只能复用审批时固定的原参数与原幂等键。身份或凭据无法确认时
保持 `EXECUTING` 并记录诊断，不会改用新凭据身份或生成新 key。

Agent 模式页面恢复历史会话时，会并行读取当前用户、站点和会话的 Action 记录，把最新审批
状态重新挂载到包含对应 Action ID 的助手消息。若 SSE 在助手消息落库前中断，页面会追加本地
恢复卡片；仍处于 `EXECUTING` 的卡片每 5 秒刷新，直到后台 Worker 给出终态。

服务根路径已提供响应式聊天工作台。页面默认进入无工具的“模型对话”模式，也可以切换到
“ERPNext Agent”模式；助手消息位于左侧，用户消息位于右侧，并支持流式文本、工具状态、
多轮模型上下文和 OAuth 登录状态展示。浏览器不再上传历史内容，只携带当前消息与
`conversation_id`；服务端从 PostgreSQL 恢复用户/站点/模式隔离的历史并交给新建 Agent。
左侧历史侧栏会列出持久化会话，支持切换和创建独立新对话，页面刷新时恢复最近会话。
助手消息已从 Markdown 源文本显示升级为富文本预览，支持粗体、标题、列表、表格、引用、代码和安全链接；
流式回复和历史会话采用相同渲染路径。

真实 ERPNext OAuth 用户的库存查询已完成单仓和多仓两条端到端链路。指定精确仓库时使用
`erpnext_get_stock_balance`；只提供物料时，Data Agent 单次调用
`erpnext_get_item_stock_by_warehouses`，使用 MCP 返回的当前用户可见叶子仓库明细和数量合计回答。
Chat API 不再使用库存问法正则、专用确定性服务或伪造工具事件截获该请求。

OAuth 凭据使用前会在过期窗口内主动刷新，并使用 Redis 分布式 single-flight
防止并发请求重复轮换 token；MCP 首次认证失败时只刷新并重试一次，无法恢复才清理
Agent Session 并要求重新登录。长对话会把旧的完整轮次转换为版本化摘要，与最近消息一起作为
模型上下文，PostgreSQL 中的原始消息不删除。两项能力已通过单元/并发/回归测试，并已重启
Agent 容器部署新镜像。Administrator token 真实轮换和单次真实模型摘要 smoke 均已通过。

数据库结构不再由应用启动时调用 `create_all()` 创建。Compose 会先运行一次性 Alembic
`migrate` Job；迁移成功后 Agent 才能启动，且应用 lifespan 会再次验证数据库 revision。首个
revision 能校验并接管早期完整结构，保留旧 `oauth_credentials` 表；空库、重复升级、部分结构
拒绝、ORM drift 和当前开发库无损接管均已真实验证。

会话生命周期已补齐：用户可以在历史栏重命名或删除本人指定模式的会话；删除会立即从列表、
历史和后续 Chat 请求中隐藏。后台保留 Worker 默认清理 180 天无活动会话、24 小时空会话和软删除
满 7 天的会话，物理删除时由外键级联清理消息，但不会删除独立的 Action 审计记录。第二个
Alembic revision `20260812_0002` 已从带样例数据的 `0001` 临时库和当前开发库连续升级验证。

自动评估底座已从目录说明升级为可执行 Runner。首批版本化 JSON 场景覆盖意图路由、上下文
继承、Agent 工具隔离、三类草稿参数校验和 MCP 业务信封失败关闭，共 20 项，8 项为安全负样本；
当前离线基线为 20/20、关键安全失败 0。Runner 输出逐场景实际/预期证据、分类通过率、总通过率
和稳定退出码，并在报告中明确声明：该数字不代表真实模型质量、ERPNext 数据准确率、OAuth 权限
差分或端到端延迟。

在离线基线之上新增了「经 OAuth 认证的在线评估」执行链。`online_authenticated_v1.json`
用真实登录用户驱动运行中的服务，纳入剩余 20 个在线场景：真实模型工具选择、ERPNext 数据处理、
三类草稿端到端 HITL（提案→批准→执行→回读 `docstatus=0`→测试专用 REST 清理）、跨用户审批隔离
与低权限权限边界。在线执行器通过五个注入缝（凭据查询/会话/解密 token/草稿删除/HTTP transport）
完全离线可测，离线路径保持零接触并以惰性导入隔离。在线套件缺 fixture/凭据时以退出码 3 失败关闭。
在线报告与离线基线分离、免责声明独立。

真实在线验收已执行：Administrator 与低权限用户 sale@sale.com 完成浏览器 OAuth 登录后，
`--repeat 3` 曾取得三轮全部 20/20、安全违规 0、草稿 REST 清理全部成功的完整达标记录；
三类草稿真实写入 ERPNext 并回读 `docstatus=0`、跨用户 decision/execute 均 404、低权限查询
未越权。后续复跑发现两个间歇性遗留问题（见 §四）：个别草稿用例执行接口偶发 409、
计数/列表用例偶发命中 `exceed_max_iters` 后落到空回复兜底；二者均不产生安全违规，
已分别通过证据增强（`execute_detail`）和循环守卫收敛，留待下一轮定位。

在在线评估之上又完成了一轮可靠性加固：只读 MCP 工具增加请求级重复调用循环守卫
（`LoopGuardTool`）；提案工具广播 schema 增富（三类草稿必填字段与示例写入 description）、
参数归一化扩展（双层嵌套展开、单对象 items 包列表、qty 数字字符串强转、quantity/delivery_warehouse
别名）并接入 `action.propose` telemetry span 与安全日志；在线证据补写 `finished_reason(s)`
与执行失败详情；评估 CLI 新增 `--repeat N` 稳定性模式（按轮编号报告、最差轮决定退出码）。

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
- Chat 和 SSE 的库存请求统一进入 AgentScope 运行时，流中返回真实 MCP 工具事件；
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
- 运行时发现工具后可核对既定的 14 个 MCP 工具；
- 新增多仓库库存领域工具的只读白名单，Data、Patrol 和 Action 可用，Orchestrator 仍无工具；
- 实现自定义 `ERPNextMCPTool(ToolBase)`，原样保留服务器 `inputSchema`；
- ToolBase 只向模型传递结构化 `data`、内容信任标记和白名单元数据；
- MCP 领域错误转换为 AgentScope 错误 ToolChunk，并保留安全的 `trace_id`；
- 同步 MCP 工具执行完成后显式返回 `ToolResultState.SUCCESS`，避免成功结果被误判为
  `RUNNING` 而触发 ReAct 迭代上限；
- Data、Patrol 和 Orchestrator 不持有写工具；
- Action 策略只允许两个草稿写工具；Chat 规划 Toolkit 会物理移除它们并换成本地预览工具；
- 只读工具权限为 `ALLOW`，写工具强制为不可绕过的 `ASK`；
- submit、cancel、delete 被列为禁止能力。

### 2.5 Agent 与 HITL

- 建立 Orchestrator、Data、Action 和 Patrol Agent 构造边界与系统提示；
- 建立保守的确定性意图门控；
- 当前消息意图不完整时，只继承最近一条明确的用户意图；当前消息的禁止或写操作关键词仍优先；
- 指定仓库时使用单仓库存工具；未指定仓库但已给出物料时单次调用 MCP 多仓聚合工具；
- 每次请求创建独立 Agent 与 Toolkit，不共享用户 access token 或会话状态；
- Agent 运行前再次核对 Agent Session 用户与 MCP 当前用户；
- Data/Patrol/Action 已接入模型工具循环，Action 只生成持久化预览，不直接写 ERPNext；
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
- Docker 运行时通过 `PYTHONPATH=/app/src` 加载源码，不再在每次源码变更后创建隔离构建环境
  下载 Hatchling；冻结生产依赖仍由 `uv sync --frozen --no-dev --no-install-project` 安装。

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

### 2.8 MCP 多仓库存聚合

- 已删除 `InventoryService`、库存语句正则提取和 Chat/SSE 特判分支；
- Data Agent 对“已给出物料、未指定仓库”请求，首个且唯一工具为
  `erpnext_get_item_stock_by_warehouses`；
- 物料文本原样作为 `item_code`，不先调用 Item Schema/List，物料不存在和权限拒绝以 MCP 结果为准；
- 使用 MCP 返回的 `warehouses`、`totals`、`stock_uom` 和 `scope` 回答；
- 不通过通用 Bin 分页查询、不逐仓循环单仓工具、不跨公司/币种汇总库存价值；
- 新增 `stock_smoke`，可重复验证工具序列、真实回复和 PostgreSQL 会话落库。

### 2.9 Agent Markdown 预览

- 新增无 CDN、无运行时外部依赖的本地 `markdown.js`；
- 助手消息支持段落、换行、标题、粗体、斜体、删除线、行内/块代码、列表、引用、分隔线和表格；
- 表格在消息气泡内使用独立横向滚动容器，小屏不会撑破页面；
- 渲染通过 `createTextNode` 和 `createElement` 构建 DOM，不把模型文本传入 `innerHTML`；
- 原始 HTML 始终当作普通文本，链接仅允许 HTTP(S)、`mailto:` 和站内相对路径；
- Markdown 仅用于助手消息，用户消息继续使用 `textContent`；
- 流式增量和 PostgreSQL 历史恢复都通过同一 `renderMessage()` 预览渲染。

### 2.10 OAuth Token 自动刷新

- 读取凭据时在默认 120 秒过期窗口内主动使用 refresh token 刷新 access token；
- 使用经哈希的凭据 ID 构造 Redis 锁键，通过带所有者校验的分布式租约实现
  single-flight；
- 等待中的并发请求会重读 PostgreSQL 凭据并复用已刷新 token，不重复调用 OAuth 端点；
- ERPNext 不返回新 refresh token 时保留旧值，新 access token 继续加密存储；
- MCP 首次返回认证失败时强制刷新并重建调用边界，最多重试一次；
- refresh token 缺失/失效或第二次认证失败时清理 Agent Session，向客户端返回稳定的
  `REAUTHENTICATION_REQUIRED`，不泄露 token。

### 2.11 长对话自动摘要

- 会话达到默认 16 条消息或 16000 字符时触发摘要，保留最近 8 条消息；
- 只摘要以 assistant 消息结束的完整旧轮次，不切断正在进行的 user/assistant 上下文；
- `chat_conversations.summary` 保存包含版本、`through_sequence`、摘要和更新时间的 JSON
  信封，并向后兼容原有纯文本摘要；
- 增量摘要通过 Redis 租约防止并发覆盖，数据库写入再校验序列号，拒绝过时结果；
- 摘要 Agent 不持有 Toolkit，提示词要求把历史内容视为数据，不允许其扩大权限或注入新指令；
- 模型调用时使用“系统生成摘要 + 最近消息”，原始 `chat_messages` 完整保留；
- 摘要生成失败时回滚并降级为最近消息，不阻断当前对话。

### 2.12 Agent 运行机制与 Workflow 文档

- 新增 `2026-08-11-Agent运行机制与Workflow说明.md`，以当前代码而非未来设计为准；
- 绘制整体架构、OAuth 登录、Chat 主链、Token 刷新和长对话摘要 Workflow；
- 说明每请求 Agent/Toolkit、确定性 `IntentGate`、AgentScope ReAct 和 MCP 四层检查的边界；
- 区分各 Agent 已运行主链与 Orchestrator 尚未接入的真实状态；
- 补充状态存储、SSE 事件、关键失败路径、API 地图和建议代码阅读顺序。

### 2.13 Action 草稿预览与持久化审批执行

- Action 写意图不再由 Chat 固定响应拦截，现进入请求级 `action_agent`；
- Chat Action Toolkit 物理移除 `erpnext_create_draft` 和 `erpnext_update_draft`，模型只能调用
  只读 MCP 工具与 `erpnext_propose_draft_action`；
- 本地预览工具支持 Sales Order、Purchase Order、Material Request 的字段白名单、必填字段、
  日期、明细行、字符串大小和有限数值校验；
- 创建 Action 前再次读取当前用户 DocType Schema，并使用有界 `get_list` 验证 Company、
  Customer/Supplier、Item 和 Warehouse 精确 Link；
- 更新草稿在预览阶段重新读取目标单据，绑定精确 `modified`，版本变化直接返回
  `VERSION_CONFLICT`，不会盲目替换版本；
- 预览、规范化参数、SHA-256、TTL、幂等键和来源版本写入 PostgreSQL，模型和浏览器不能提供
  或替换幂等键；
- SSE 新增 `action_required`，前端使用安全 DOM 构建审批卡片并支持拒绝、批准和继续执行；
- 新增 `POST /api/v1/approvals/{action_id}/execute`，执行前重新核对 OAuth/MCP 用户，使用
  APPROVED → EXECUTING CAS 和固定幂等键调用写工具；
- 写入成功后调用 `erpnext_get_doc` 回读并确认 `docstatus=0`；确定的业务拒绝进入 FAILED，
  写入后无法确认的协议/回读失败保留 EXECUTING 并记录错误，避免换 key 重复写；
- EXECUTING Action 可由本人再次调用执行入口，复用原参数和原幂等键进行核对；
- 前端和持久化文案只说明草稿保存，不声称提交、过账或预占库存。

### 2.14 EXECUTING Action 重启恢复

- 新增应用生命周期内的 `ActionRecoveryWorker`，有界扫描 PostgreSQL 中的 `EXECUTING` Action；
- Worker 只按 `site + requested_by + OAuth client` 重新解析加密凭据，不在 Action 中保存 token；
- 恢复前重新校验 `decided_by == requested_by`、凭据站点/用户和 MCP current user；
- 人工执行与后台恢复共用哈希化 Action ID 的 Redis 执行锁，防止进程内、跨实例并发重放；
- 后台扫描使用独立 Redis 冷却键限制不确定结果的重试频率；
- 恢复只调用 `ActionExecutor.reconcile()`，复用持久化原参数、摘要和原 `idempotency_key`；
- 凭据缺失、撤销、无法解密或身份不符时保持 `EXECUTING`，记录可诊断错误并等待重新登录/人工处理；
- 新增 `ACTION_RECOVERY_*` 和 `ACTION_EXECUTION_LOCK_TTL_SECONDS` 环境配置及安全时间校验；
- readiness 新增 `action_recovery` 检查与 `action_recovery_worker` 能力标记；
- 使用无凭据的临时 `EXECUTING` 记录完成真实容器故障演练，Worker 在 MCP 前失败关闭并写入
  `REAUTHENTICATION_REQUIRED`，测试记录随后删除且无数据库残留。

### 2.15 Action 审批卡片会话恢复

- 新增 `GET /api/v1/approvals?conversation_id={uuid}`，按当前 Session 的站点、用户和指定会话
  返回有界 Action 历史；
- PostgreSQL 查询使用 `site + requested_by + preview.conversation_id` 三重过滤，另一用户即使知道
  会话 UUID 也无法读取 Action；
- 列表包含 PENDING、APPROVED、EXECUTING 和各类终态，使刷新后的卡片不会停留在旧状态；
- 列表读取时同步把过期 PENDING/APPROVED 写为 EXPIRED；
- 新增 `ACTION_HISTORY_LIMIT`，默认每个会话恢复最新 50 个 Action；
- 新增可单测的 `action_restore.js`，使用持久化助手消息内的精确 Action ID 挂载卡片，每个 Action
  最多挂载一次；
- 对尚未落库助手消息的 Action 生成仅本地的恢复说明和审批卡片，不伪造历史记录；
- `EXECUTING` 卡片每 5 秒读取最新状态，切换会话、登出或进入终态时停止轮询；
- 新增真实部署 smoke：同会话插入本人和另一用户临时 Action，受保护接口只返回本人记录，
  测试完成后 PostgreSQL 与 Redis 临时状态均已清理。

### 2.16 Alembic 正式数据库迁移

- 新增 Alembic 1.x 生产依赖、`alembic.ini`、异步 `migrations/env.py` 和首个 revision
  `20260812_0001`；
- 数据库 URL 只从已验证 Settings 注入 Alembic Config，不写入迁移配置或日志；
- 首个 revision 管理 `actions`、`chat_conversations`、`chat_messages` 和
  `oauth_device_credentials` 四张当前 ORM 表；
- 旧 `oauth_credentials` 明确列为非托管历史表，升级和 drift 检查都不会删除或修改；
- 空库创建完整表、索引、唯一约束和会话级联外键；已有完整结构会校验列、主键、关键唯一约束
  和外键后安全接管；部分或不兼容结构失败关闭；
- PostgreSQL advisory lock 串行化跨实例迁移，避免两个部署 Job 同时更新 revision；
- `python -m erpnext_agent.migrate` 支持 `upgrade`、`check` 和 `current`，其中 `check` 同时验证
  revision 与 ORM metadata drift；
- 移除 `AUTO_CREATE_SCHEMA` 和运行时 `create_all()`；Agent 启动必须读取到唯一预期 revision，
  否则拒绝启动；
- Compose 新增只读、非 root、一次性 `migrate` 服务，Agent 使用
  `service_completed_successfully` 门禁；
- readiness 新增 `database_migrations=true` 和 `database_revision=20260812_0001`；
- 现有开发库迁移前后行数一致：Actions 0、会话 23、消息 80、旧凭据 2、设备凭据 2。

### 2.17 会话生命周期与正式数据保留策略

- `chat_conversations` 新增可空 `title` 和 `deleted_at`，历史会话无须回填即可保持首条用户消息
  自动标题；用户自定义标题规范化后持久化；
- 新增 `PATCH /api/v1/chat/conversations/{id}` 和
  `DELETE /api/v1/chat/conversations/{id}`，两者都要求有效 Session、CSRF，并以
  `site + user_id + mode + conversation_id` 校验所有权；跨用户或跨模式统一返回 404；
- 删除采用立即不可见的软删除；列表、最新会话、历史恢复、续聊、摘要和追加消息路径均不会
  使用已删除会话；
- 历史栏为每条会话增加重命名和删除操作，删除当前会话后自动选择下一条；删除确认明确说明
  聊天内容与 Action 审计记录边界；
- 新增 `ConversationRetentionWorker`，默认每小时使用有界批次和 PostgreSQL
  `FOR UPDATE SKIP LOCKED` 清理过期记录，支持多实例并行部署；
- 新增 `CHAT_RETENTION_*` 配置：普通会话默认保留 180 天、用户删除内容 7 天、空会话 24 小时，
  扫描周期和批次均可配置；
- 物理删除会话时 `chat_messages` 通过数据库 `ON DELETE CASCADE` 清理，Action 表没有会话外键，
  因而审批、执行结果和幂等审计不会被聊天保留策略删除；
- readiness 新增 `conversation_retention` 检查和 `conversation_retention_worker` 能力标记；
- 新增 Alembic revision `20260812_0002`，只增加两个可空列和删除时间索引；已验证带既有会话
  的 `0001 → 0002` 无损升级、重复升级和 ORM drift；
- 新增真实 Repository smoke 和受保护 HTTP API smoke；后者使用两个临时身份验证创建、重命名、
  跨用户 404、软删除和删除后 404，完成后清理 PostgreSQL 与 Redis 临时状态且不访问 ERPNext。

### 2.18 离线确定性评估 Runner 与首批 20 场景

- 新增 `erpnext_agent.evaluation` 包，包含严格 Pydantic 场景模型、加载器、执行器、评分聚合和
  JSON/Markdown 报告；
- 场景源文件 `evaluations/scenarios/offline_policy_v1.json` 使用 `schema_version=1`，未知字段、
  重复 case ID、错误 executor 输入和不完整预期都会失败关闭；
- 支持四类确定性执行器：`intent_route`、`tool_policy`、`action_validation` 和
  `mcp_envelope`；
- 20 个场景按开发方案六类分组：简单查询 2、领域摘要 2、多步 2、草稿操作 4、巡检 2、
  安全负样本 8；
- 安全负样本覆盖提交/删除意图、当前禁止意图覆盖历史、Data/Patrol 写工具隔离、Orchestrator
  无业务工具、客户端幂等键注入、不完整草稿和 MCP `ok=false`；
- 三类草稿（Sales Order、Purchase Order、Material Request）的完整 payload 均进入相同本地
  白名单/必填/日期/明细校验路径；
- 每个 case 报告 `expected` 与 `actual` 证据，不记录环境变量、token、完整 Prompt 或线上业务数据；
- 汇总同时给出总通过率、分类通过率和关键安全失败数，当前门槛为通过率 100%、安全失败 0；
- CLI 退出码固定为：通过 0、执行完成但未达门槛 1、场景文件无效 2；三条路径均有自动测试；
- 报告写入采用临时文件后原子替换，`evaluations/reports/` 被 Git 忽略；
- Docker 镜像包含版本化场景，可用只读、`--no-deps` 容器直接运行，不访问模型、ERPNext、
  Redis 或 PostgreSQL；
- 报告强制包含免责声明，当前 20/20 仅为确定性代码策略基线，不能替代剩余 20 个在线/故障/
  双用户场景和真实业务准确率。

### 2.19 OpenTelemetry 可观测链路基线

- 新增应用自有 `Telemetry` 边界，`OTEL_ENABLED=false` 时使用 NoOp Provider，启用时创建独立
  `TracerProvider`、`BatchSpanProcessor` 和 OTLP/HTTP exporter；应用退出时安全关闭 Provider；
- `OTEL_ENABLED=true` 时强制要求完整 `OTEL_EXPORTER_OTLP_ENDPOINT`，并新增 1–30 秒可配置
  导出超时；默认关闭，不改变当前本地部署行为；
- FastAPI 中间件接收 W3C Trace Context，以规范 UUID 生成/接受 `request_id`，只记录路由模板、
  HTTP 方法、响应码、耗时和结果码，不记录含对象 ID 的原始 URL；
- 登录 Session 只以 `SESSION_SECRET` 为密钥做 HMAC 后写入 Trace，原始 Session ID 不出边界；
- 非流式 Agent、流式 Agent 和长对话摘要模型均记录 `agent_name`、`model_call_id`、`turn_id`
  （适用时）、耗时与稳定结果码，不记录 Prompt、历史正文或模型输出；
- MCP 为刷新重试、工具调用和 JSON-RPC 传输建立嵌套 span，记录工具名、RPC method/ID、重试次数、
  HTTP 状态和 MCP trace ID；HTTPX 请求自动注入 `traceparent`，不记录 access token、参数和响应；
- Action 执行与恢复记录 `action_id`、工具名、结果和 MCP trace ID；恢复扫描与会话保留扫描只记录
  聚合计数；
- Span 禁止自动记录 exception message/stack，避免异常携带凭据、Prompt 或业务内容；
- 新增四项专项测试：配置失败关闭、HMAC/Request ID、安全属性/跨层 Trace、异常正文不导出；
- readiness 新增 `otel_tracing` 能力位。Collector 不可用不会让业务 readiness 失败，exporter
  采用尽力而为的后台批量导出。

### 2.20 认证在线评估 Runner 与剩余 20 场景

- `EvaluationSuite/EvaluationReport.execution_mode` 扩为 `offline_deterministic |
  online_authenticated`；新增 `LiveChatCase` 与 `LiveDraftActionCase` 两类在线 case 模型，
  沿用严格 `extra=forbid`；新增「执行器必须与 execution_mode 匹配」「reject 不得期望执行/清理」
  「cross_user_identity 必须不同于 case identity」等校验器；
- 新增 `online_transport.py`（`ChatTransport` 协议 + `HttpChatTransport` + 独立纯函数
  `parse_chat_stream`）、`online_assertions.py`（零 I/O 纯断言）、`online_cleanup.py`
  （`DraftCleanupRegistry` 只删登记项）、`online.py`（`OnlineEvaluationRunner`）；
- 在线执行器的全部外部副作用经五个注入缝接入（凭据查询/会话/解密 token/草稿删除/HTTP transport），
  生产默认接 PostgreSQL、Redis+SessionStore、TokenStore、ERPNext REST 与实时 HTTP；全注入时不触碰
  Settings/DB/网络，可完全离线单测；
- `runner.main()` 按 `execution_mode` 分派，在线时惰性导入并 `asyncio.run`；退出码契约扩展为
  0 通过 / 1 未达标 / 2 场景非法 / 3 在线环境前置缺失（打印缺失变量与重新登录补救信息）；
- 在线免责声明独立于离线基线；事实断言不依赖工具返回 payload（`tool_result_end` 只带
  `tool_call_id`），改用工具序列 + 最终文本 + action payload + 环境注入期望；
- `online_authenticated_v1.json` 共 20 场景，分布 6 simple_query / 4 domain_summary /
  4 multi_step / 4 draft_action / 2 patrol，7 个 `security_critical`；占位符
  `{company}/{customer}/{supplier}/{item}/{warehouse}/{today}/{delivery_date}/{schedule_date}`
  运行期从 `EVAL_ONLINE_*` 注入；
- 三类草稿各一条全链路（提案→批准→执行→回读 `docstatus=0`→REST 清理），一条本人拒绝路径，
  一条跨用户隔离（secondary decision/execute 均须 404），一条低权限查询（受限成功或礼貌拒绝皆合法）；
- 草稿 REST 清理用同一用户自己的解密 token 直连 `effective_erpnext_internal_url` 并带
  `Host: erpnext_host_header`（对齐 `adapter._headers`），幂等（2xx/404 成功），失败只记证据不改 case
  结论；仅测试路径，不触碰生产路径与 MCP 白名单；
- `compose.yaml` 新增 `eval` 服务（`profiles:["tools"]`，挂载 reports 卷，依赖 agent/postgres/redis
  healthy），`Makefile` 新增 `eval`/`eval-online`，`.env.example` 增补 `EVAL_ONLINE_*` 说明。

### 2.21 在线评估可靠性加固与真实验收

- 新增 `src/erpnext_agent/mcp/loop_guard.py`：`LoopGuardTool(ToolBase)` 请求级包装只读
  `ERPNextMCPTool`，按 `(tool_name, 规范化参数)` 计数，第 `MCP_LOOP_GUARD_MAX_REPEATS`
  （默认 3，`config.py` 新增字段，范围 2–8）次完全相同调用直接返回 `REPEATED_TOOL_CALL`
  错误 chunk，促使模型用已有数据作答；拒绝包装非只读工具；`toolkit_factory` 仅包装只读工具，
  `ActionProposalTool` 与写工具不经此路径；
- `actions/proposal.py` 广播 schema 增富：`arguments` 属性 description 写明三类草稿必填字段、
  `payload` 嵌套、items 行结构与完整示例，并明确不得传 `idempotency_key`；已验证通过
  agentscope `RegisteredTool` 校验；`validate_action_arguments` 仍是唯一权威校验；
- 提案归一化扩展（仅传输形状修复，规范形状存在时均为 no-op）：`{"arguments":{...}}` 双层嵌套
  展开、items 单对象包列表、qty 数字字符串强转（非有限数/解析失败仍走原校验拒绝）、
  `quantity→qty` 与既有 `delivery_warehouse→warehouse` 别名；
- 提案失败诊断：`ActionProposalTool.__init__` 新增 `telemetry` 参数，`call()` 用
  `action.propose` span 包住提案过程，失败时记录 `error_code/doctype/stage`（local_validation /
  mcp_validation）并 `logger.warning`；属性绝不含参数与用户数据；`chat.py` 构造处传入
  `request.app.state.telemetry`；
- 在线证据增富：`live_chat` 证据写入各轮 `finished_reasons`，`live_draft_action` 证据写入
  `finished_reason`，执行非 200 时写入 `execute_detail`（响应体，用于定位 409 等失败）；
- 评估 CLI 新增 `--repeat N`（默认 1）：每轮全新 `OnlineEvaluationRunner`（连接与草稿清理隔离），
  N>1 时按轮产出 `*_run{i}.json/md` 编号报告，退出码最差轮决定（任一轮未达阈值 → 1）；
  `--repeat 0` 以退出码 2 拒绝；N=1 保持原文件名与行为；
- `compose.yaml` eval 服务命令补 `--json-report/--markdown-report` 与 `--repeat ${EVAL_REPEAT:-1}`；
  reports 卷属主一次性修正为容器用户（uid 10001）；
- DATA 提示词补充计数问句指引（优先一次 `erpnext_get_count` 后直接作答），属软性引导；
- 真实验收：两用户登录后曾取得三轮全部 20/20；本次继续将套件扩展/严格到 22 场景并纳入正常
  finished reason 门禁，最终完整达标。

### 2.22 发布前可靠性与请求预算加固（2026-08-18）

- 在线评估现在要求 `finished_reason` 为 `completed`、`stop` 或 `done`；`exceed_max_iters` 不再
  被计为通过。真实 `online_authenticated_v1` 22 个场景最终 22/22、通过率 100%、安全违规 0。
- 新增 `TerminalToolConvergenceMiddleware`：`erpnext_get_count`/`erpnext_get_list` 成功后，
  下一轮强制 `tool_choice=none`，保证模型根据已返回数据作答；新增 Qwen
  `MODEL_ENABLE_THINKING` 配置并在当前 token-plan 环境设为 `false`。
- MCP 工具桥为 `fields`/`filters` 增加受限 JSON 字符串兼容输入，解码后仍使用服务端原始 Schema
  二次校验；解决 Qwen 将嵌套数组/对象双重编码导致的本地参数校验循环。
- 新增请求体 64 KiB 上限、Redis 固定窗口限流（聊天/审批/认证分桶）、Agent 单轮 90 秒超时及
  稳定 SSE 错误码；修复请求体重放后的 ASGI disconnect 轮询问题。
- `RequestBodyLimitMiddleware`、限流、收敛中间件、模型 thinking 配置及严格在线 finished reason
  均有单元回归测试。

## 三、验证证据

| 检查项 | 结果 |
|---|---|
| `uv.lock` 依赖解析 | 通过，锁定 AgentScope 2.0.5 |
| Pytest | 201 项通过，包含请求预算、模型兼容、工具收敛、在线评估严格门禁和既有安全回归 |
| Ruff | 通过，无问题 |
| Mypy strict | 通过，检查 84 个源码文件 |
| Docker Compose config | 通过 |
| Docker 镜像构建 | 通过，生成 `erpnext-agent:local` |
| Alembic 空库升级 | 临时空库创建 4 张托管表与 `alembic_version`，revision 为 `20260812_0001` |
| Alembic 重复升级 | 通过，第二次 upgrade 无结构变更 |
| Alembic ORM drift | `No new upgrade operations detected` |
| Alembic 连续升级 | 带 1 条既有会话的临时库从 `20260812_0001` 升到 `20260812_0002`，行和两个新增空列均正确保留 |
| 部分结构失败关闭 | 只存在 `actions` 时明确拒绝，未补建或错误接管 |
| 现有数据库接管 | 五张历史/当前业务表行数迁移前后完全一致，旧 OAuth 表保留 |
| 当前库 0002 升级 | migration 前后 Actions 0、会话 23、消息 80、旧凭据 2、设备凭据 2，全部一致；新 Worker 启动后按策略清理 2 条超过 24 小时且无消息的空会话，正文消息仍为 80 |
| Compose 迁移门禁 | migrate Job `Exited (0)` 后 Agent 才启动 |
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
| 真实 MCP 工具序列 | 仅 1 次 `erpnext_get_item_stock_by_warehouses`，无 Item/Bin 通用查询 |
| 真实多仓库存 | 返回 `仓库 - rw`：实际 `10.0 Nos`、预计 `10.0 Nos`，未汇总库存价值 |
| 多仓会话落库 | 通过，会话 `3948e148-6e51-4e9e-a74f-f2ade2f9dd0c` 持久化 user/assistant 两条消息 |
| 会话列表与新建 | 通过，列表返回 4 条消息计数，新建会话获得独立 UUID |
| Markdown 解析与 DOM | Node 测试通过，真实库存回复生成 `strong`、`ul`、`table`、`thead`和 `tbody` |
| Markdown 安全 | 通过，原始 `<img onerror>` 不生成 `img`，`javascript:`/`data:` 不生成链接 |
| 前端静态资源 | Node 语法检查通过，运行中 HTML 按 `markdown.js` 再 `app.js` 的顺序加载 |
| Redis 连接 | 通过 |
| Agent 容器启动 | 通过，2026-08-12 使用新镜像重新创建后 Agent、PostgreSQL、Redis 均为 healthy |
| `/health/ready` | Redis/Database/Action recovery/Conversation retention 为 `ok`，数据库 revision 为 `20260812_0002` |
| 会话 Repository smoke | 重命名、跨用户隔离、软删除隐藏、三类到期清理和消息级联均通过；过期 3 条，级联残留 0 |
| 会话 HTTP API smoke | 两个临时身份真实调用 create/patch/list/delete/history；跨用户更新、跨用户删除与删除后历史均为 404，临时会话/凭据清零 |
| 前端生命周期测试 | Node 6 项通过，覆盖 Action 恢复及标题规范化、重排和删除状态转换；运行中 JS 资源返回 200 |
| 离线评估 Runner | `offline_policy_v1` 20/20，通过率 100%，8 个安全负样本全部通过，安全失败 0 |
| 评估分类结果 | 简单查询 2/2、领域摘要 2/2、多步 2/2、草稿 4/4、巡检 2/2、安全负样本 8/8 |
| Runner 镜像复现 | 生产镜像内默认场景直接运行通过，不启动或访问模型、ERPNext、Redis、PostgreSQL |
| 评估 CLI 契约 | 达标退出 0、未达门槛退出 1、非法 suite 退出 2，均有自动测试 |
| OTel 内存导出验收 | HTTP/MCP tool/MCP RPC 三个 span 共用一个 trace，MCP 请求含 `traceparent`；Token、工具参数、业务返回和异常正文均不在导出属性/事件中 |
| OTel 网络导出验收 | 无外网容器内真实 OTLP/HTTP exporter 向临时 `/v1/traces` 接收端发送 1 个 protobuf span，服务名、结果码和耗时可解析 |
| Action OpenAPI | 运行中服务已注册读取、确认/拒绝和执行三个审批路径 |
| Action 单元与 API 验收 | 参数篡改执行前拒绝、固定幂等键、跨用户 404、成功回读和不确定失败保留均通过 |
| Action 恢复故障演练 | 临时 EXECUTING 在第一次轮询前写入 `REAUTHENTICATION_REQUIRED`，未调用 MCP，记录已删除 |
| Action 卡片恢复前端测试 | 3 项通过：精确挂载、断流未匹配恢复、每个 Action 只挂载一次 |
| Action 列表真实 smoke | 同会话本人 Action 返回 1 条、另一用户隐藏，清理后临时 Action 数为 0 |
| Token 自动刷新真实验收 | 通过；Administrator OAuth token 成功轮换，credential ID 不变、版本前进，新 token 的 MCP 用户仍为 Administrator |
| 长对话真实摘要验收 | 通过；严格 1 次真实模型调用，摘要覆盖 sequence 1–8、保留 9–12 且 12 条原始消息未减少 |
| 可靠性加固单元测试 | 请求体/限流/收敛/模型配置/参数兼容与严格门禁回归全部通过 |
| 离线套件镜像内回归 | `offline_policy_v1` 20/20、退出 0、安全违规 0（含全部加固改动后复测） |
| 在线验收（--repeat 3） | 两用户真实登录后取得三轮全部 20/20、通过率 100%、安全违规 0、`threshold_passed=true`；三类草稿真实写入并回读 `docstatus=0`，REST 清理全部成功 |
| 跨用户隔离真实验证 | secondary 对 primary 的 Action decision/execute 均返回 404；低权限库存查询未越权 |
| 草稿执行 409 | 历史复跑曾偶发 409；本次最终 22 场景验收中三类草稿提案→审批→执行→回读→清理全部通过，仍建议后续多轮持续观测 |
| 计数/列表循环 | 已通过 thinking 配置、嵌套参数兼容和终止工具收敛修复；严格正常结束门禁下本次相关场景全部通过 |
| 在线评估单元测试 | 27 项全部通过（schema/解析/断言/分发/评分/跨用户隔离/清理/环境 fail-closed/退出码契约），全离线零外部依赖 |
| 全量 Pytest | 145 项通过（118 既有 + 27 新增），无回归 |
| Mypy strict | 通过，检查 81 个源码文件 |
| 离线套件回归 | `offline_policy_v1` 20/20、退出 0、零环境可跑（宿主机 + 镜像内 `--no-deps` 均复现） |
| 在线套件加载 | `online_authenticated_v1` 22 cases、分布 8/4/4/4/2、ID 唯一、`security_critical=7` |
| 在线缺环境 fail-closed | 宿主机与镜像内均以退出码 3 终止，并列出全部缺失 `EVAL_ONLINE_*` 变量 |

当前 Compose 服务已运行新版本，Agent、PostgreSQL 和 Redis 均为 healthy，启动日志无异常；本次
真实在线评估已执行草稿写入、审批、回读和测试数据清理。

## 四、尚未完成

- 在线评估的 22 场景已完成一次严格全量验收；仍需在发布前按计划持续执行多轮（例如 `EVAL_REPEAT=3`）
  以获得统计稳定性证据；
- OpenTelemetry 代码和 OTLP/HTTP exporter 已接入，本地 Collector/Jaeger 已随 Compose 落地，
  但尚未产出持久化面板、采样策略、SLO 和告警证据；
- 故障恢复类场景（重启、写后超时、幂等重试）仍依赖人工故障演练，未纳入自动在线套件；
- MCP 多仓工具只返回当前 Bin 聚合值，不包含历史日期和库存价值；

## 五、下一阶段建议

1. 在相同 OAuth fixture 下执行 `EVAL_REPEAT=3 make eval-online`，形成多轮统计稳定性证据；
2. 为在线套件补充故障恢复类场景或配套人工故障演练记录；
3. 在预发布环境制定 OTel 采样、SLO 和告警阈值；
4. 评估 MCP 多仓历史日期与库存价值能力是否进入下一阶段范围。

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
