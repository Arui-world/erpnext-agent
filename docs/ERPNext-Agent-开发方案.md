# ERPNext 智能业务 Agent 系统开发方案

> 技术基线：Python 3.12 + agentscope==2.0.5 + FastAPI + ERPNext/Frappe + Redis  
> 工期：4 周（全职）  
> 定位：按生产架构开发、可量化验收的 MVP，不把尚未实现的能力包装成“生产级”  
> 审计日期：2026-08-09；MCP 能力以 ERPNext-MCP-使用文档.md 和运行时 tools/list 为准

---

## 一、已经固定的技术决策

### 1.1 AgentScope 版本

本项目固定使用 AgentScope 2.0，精确版本为：

~~~toml
[project]
requires-python = ">=3.12,<3.13"
dependencies = [
  "agentscope==2.0.5",
]
~~~

使用 uv lock 生成并提交 uv.lock。CI 使用 uv sync --frozen，禁止在构建时自动漂移到其他 2.0.x。

这里的 2.0.5 就是 AgentScope 2.0 的补丁版本，不是改用其他大版本。固定补丁版本是为了让本方案中的 API、测试结果和线上运行环境一致。

2026-08-09 已在 Python 3.12 临时隔离环境实际安装并导入 agentscope==2.0.5，确认以下 API 存在：

~~~python
from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.mcp import HttpMCPConfig, MCPClient
from agentscope.tool import FunctionTool, Toolkit
~~~

同时确认：

- 2.0.5 使用 Agent(..., react_config=ReActConfig(...))，不使用旧示例中的 ReActAgent；
- MCP 使用 MCPClient + HttpMCPConfig，可配置 headers、enable_tools 和超时；
- Toolkit 构造参数可直接接收 mcps=[client]，但本项目生产路径不能直接采用，原因见 5.2；
- 2.0.5 安装包没有 agentscope.init，可观测不能照搬旧版初始化伪代码；
- reply_stream() 会产生用户确认相关事件，但业务审批仍需本项目持久化，不能只依赖进程内事件。

后续升级必须单独提交，至少重新通过：导入冒烟、MCP 连接、流式事件、HITL 恢复、状态恢复和全量评估；不得只修改版本号。

### 1.2 当前 MCP 是既有基线

erpnext_mcp_tools 已有 13 个工具：

~~~text
erpnext_health
erpnext_get_current_user
erpnext_search_doctypes
erpnext_get_doctype_schema
erpnext_get_list
erpnext_get_doc
erpnext_get_count
erpnext_get_stock_balance
erpnext_get_customer_summary
erpnext_get_supplier_summary
erpnext_get_receivables_summary
erpnext_create_draft
erpnext_update_draft
~~~

当前写能力只包括创建/修改 Sales Order、Purchase Order、Material Request 草稿。

当前没有 submit、cancel、delete、工作流推进、财务过账、任意 SQL 或 HRMS Expense Claim 工具。因此 Agent 的 MVP 不能宣称完成这些操作。

### 1.3 开发环境采用生产认证思路

开发和生产都使用 OAuth 2.0 Authorization Code + PKCE（S256）：

- 用户在 ERPNext 登录页输入 ERPNext 账号和密码；
- Agent 服务不提供“接收 ERPNext 密码并换 token”的接口；
- Agent 后端保存用户专属 OAuth token，浏览器只保存 Agent 自己的不可读会话 Cookie；
- 每次 MCP 请求使用当前用户的 access token；
- 不共享 Administrator、服务账号或全局 API Key。

开发环境只放宽域名和证书条件，不改变授权流程、token 隔离、HITL、审计和工具白名单。

### 1.4 生产路径 MCP 优先，直连 SQL 默认关闭

MCP 会执行 Frappe RBAC、User Permission、共享规则和字段 permlevel；直连 MariaDB 会绕过这些边界。第一阶段所有用户数据访问都走 MCP。

复杂指标优先采用以下顺序：

1. 使用现有领域工具；
2. 使用 get_list/get_count 拉取有界数据并调用确定性计算函数；
3. 为稳定业务口径新增权限感知的 MCP 聚合工具；
4. 直连 SQL 只作为隔离实验，不进入生产默认路径。

---

## 二、目标与范围

### 2.1 一句话目标

让 ERPNext 用户通过自然语言，在本人权限范围内查询业务数据、分析异常，并在明确预览和人工确认后创建或修改业务草稿。

### 2.2 MVP 范围

| 能力 | MVP | 说明 |
|---|---:|---|
| OAuth 登录/刷新/登出 | 是 | 每用户独立 token，Agent 不接触密码 |
| 只读查询与领域摘要 | 是 | 使用 MCP 11 个只读工具 |
| 草稿创建与修改 | 是 | 只开放两个 MCP 写工具 |
| 写前预览和 HITL | 是 | MVP 中所有写操作都必须确认 |
| 多轮会话与恢复 | 是 | 对话状态和审批状态分开存储 |
| 巡检 | 是，基础版 | 仅覆盖现有 MCP 可表达的规则 |
| submit/cancel/delete/过账 | 否 | MCP 未实现 |
| 多单据补偿事务 | 否 | 当前没有安全补偿工具 |
| 模型生成任意 Python 并执行 | 否 | 先使用白名单确定性计算函数 |
| 生产直连 SQL | 否 | 权限等价性尚未证明 |

### 2.3 完成定义

- 两个不同 ERPNext 用户查询结果符合各自权限，token 和会话不串用；
- MCP 工具只从运行时 Schema 注册，Data/Patrol 无法调用写工具；
- 所有写入都经过持久化审批、参数绑定、幂等和回读验证；
- 超时、刷新并发、审批过期、版本冲突和幂等执行中都有确定行为；
- 正样本、拒绝样本、越权样本、提示注入样本均纳入自动评估；
- 文档和演示不声称当前系统没有的提交、过账或补偿能力。

---

## 三、系统架构

~~~text
浏览器 / CLI
    │  Agent Session Cookie（HttpOnly / Secure / SameSite）
    ▼
FastAPI 接入层
    ├─ OAuth start / callback / refresh / logout
    ├─ Chat REST / SSE 或 WebSocket
    ├─ CSRF、限流、请求大小和超时
    └─ 当前用户/会话解析
    │
    ▼
策略与编排层
    ├─ 确定性意图门控
    ├─ Orchestrator
    ├─ Data Agent（只读）
    ├─ Action Agent（读 + 草稿写）
    ├─ Patrol Agent（只读）
    └─ HITL Gateway（持久化审批）
    │
    ▼
AgentScope 2.0.5 Toolkit + ERPNext MCP Adapter
    ├─ per-user Authorization header
    ├─ tool allowlist
    ├─ 四层响应归一化
    ├─ 超时/重试/熔断
    └─ trace_id 关联
    │
    ▼
ERPNext MCP endpoint
    ├─ Frappe RBAC / User Permission / permlevel
    ├─ DocType 和字段白名单
    ├─ 脱敏与不可信内容标记
    ├─ 写审计、回滚、幂等、乐观锁
    └─ ERPNext ORM / 业务校验
~~~

独立存储四类状态，禁止混在一个序列化对象中：

| 状态 | 建议存储 | 是否进入模型上下文 |
|---|---|---:|
| Agent 会话/消息摘要 | Redis 或数据库 | 受控进入 |
| OAuth token | 加密凭据存储 | 否 |
| HITL Action/Decision | 数据库 | 只进入脱敏预览 |
| Schema/工具缓存 | Redis/进程缓存 | 仅必要结构 |

---

## 四、OAuth 与用户身份

### 4.1 ERPNext OAuth Client

每个环境单独创建 OAuth Client：

| 配置 | 开发环境示例 | 生产要求 |
|---|---|---|
| Grant Type | Authorization Code | Authorization Code |
| Response Type | Code | Code |
| Token Endpoint Auth Method | Client Secret Basic | Client Secret Basic |
| Scopes | all openid | 按验证后的最小范围 |
| Default Redirect URI | http://localhost:8001/auth/callback | 精确 HTTPS URI |
| Redirect URIs | 仅允许已登记 URI | 禁止通配和临时域名 |
| Skip Authorization | 关闭 | 默认关闭 |
| Allowed Roles | 业务系统用户角色 | 明确白名单 |

即使是 confidential client，也始终发送 PKCE code_challenge_method=S256。

Frappe 端点：

~~~text
GET  /api/method/frappe.integrations.oauth2.authorize
POST /api/method/frappe.integrations.oauth2.get_token
GET  /api/method/frappe.integrations.oauth2.openid_profile
POST /api/method/frappe.integrations.oauth2.revoke_token
GET  /.well-known/oauth-authorization-server
~~~

### 4.2 登录流程

~~~text
1. 浏览器访问 GET /auth/login
2. Agent 生成 state、nonce、code_verifier 和 S256 code_challenge
3. 服务端短期保存 state → verifier → 原始跳转地址，设置一次性登录 Cookie
4. 浏览器跳转到 ERPNext authorize endpoint
5. 用户在 ERPNext 页面输入账号密码并授权
6. ERPNext 回调 /auth/callback?code=...&state=...
7. Agent 校验 state、一次性 Cookie、过期时间和 redirect_uri
8. Agent 用 code + verifier + Client Secret Basic 换取 token
9. 调用 userinfo，再调用 erpnext_get_current_user 做身份一致性校验
10. 加密保存 access/refresh token，创建 Agent Session
11. 浏览器只收到 HttpOnly Session Cookie
~~~

不得实现接收用户名密码的 Agent 登录接口。Frappe 源码虽然保留 legacy password grant 逻辑，但授权服务器元数据只声明 Authorization Code 和 Refresh Token；本项目不依赖未发布的密码模式。

### 4.3 Token 生命周期

- token 记录至少绑定 credential_id、ERP site、OAuth client、subject/user、scope、过期时间和密钥版本；
- access token 和 refresh token 使用信封加密，日志、异常和 Trace 永不输出明文；
- 同一凭据刷新使用分布式 single-flight lock，避免并发刷新相互覆盖；
- MCP 返回 401/403 时只允许刷新一次；刷新失败则清除会话并要求重新登录；
- 登出先调用 revoke，再删除本地 token 与会话；revoke 暂时失败也要阻止本地继续使用并进入后台重试；
- 恢复 Agent 状态时只读取 credential_id，重新构造 MCP Client，绝不序列化 token header。

### 4.4 身份不变量

每次请求同时校验：

~~~text
agent_session.user_id
  == oauth_subject/user
  == MCP erpnext_get_current_user.user
  == HITL action.requested_by
  == HITL decision.decided_by（MVP 自助确认）
~~~

任何一项不一致都停止执行，不通过切换高权限身份重试。

---

## 五、AgentScope 2.0.5 接入

### 5.1 每用户 MCP Client

下面使用的是已验证的 2.0.5 构造 API：

~~~python
from agentscope.mcp import HttpMCPConfig, MCPClient


def build_mcp_client(*, session_id: str, access_token: str, tools: list[str]) -> MCPClient:
    return MCPClient(
        name=f"erpnext-{session_id}",
        is_stateful=False,
        mcp_config=HttpMCPConfig(
            url=settings.erpnext_mcp_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20.0,
        ),
        enable_tools=tools,
        execution_timeout=25.0,
    )
~~~

约束：

- 一个 Client 只绑定一个用户身份，禁止跨用户放入全局单例；
- token 刷新后销毁旧 Client 并重新构造；
- 工具白名单由服务端策略给出，不能让模型调用元工具自行扩大；
- 启动/会话初始化时核对 list_tools()，与预期契约不一致则 fail closed；
- 如果使用 stateful MCP Client，必须显式管理 connect()/close()；当前 endpoint 不需要会话 ID，优先 stateless。

### 5.2 structuredContent 适配

实际检查 2.0.5 的 MCPTool.call 实现后确认：它把 MCP CallToolResult 的 content 和 isError 转为 ToolChunk，但不会传播 structuredContent。

当前 ERPNext MCP 的受控业务失败通常是：

~~~text
isError = false
structuredContent.ok = false
~~~

因此生产代码禁止直接使用 Toolkit(mcps=[client]) 注册 ERPNext 工具，否则 PERMISSION_DENIED、VERSION_CONFLICT 等业务失败可能被 AgentScope 当成普通文本结果。

采用自定义 ERPNextMCPTool（继承 ToolBase）：

1. 使用 MCPClient 或独立 MCP SDK 客户端完成 initialize/tools/list；
2. 用 tools/list 的 name、description、inputSchema 构造 ToolBase 的 name、description、input_schema；
3. call() 通过 ERPNextMCPAdapter 调用工具并保留原始 CallToolResult；
4. 依次校验 HTTP、JSON-RPC、isError 和 structuredContent.ok；
5. 成功时只把 envelope.data 与脱敏 meta 放入 ToolChunk，失败时转换为稳定领域错误；
6. 从服务端 annotations 与本地强制策略共同设置 is_read_only；写工具始终进入 HITL；
7. 只把 allowlist 内的 ERPNextMCPTool 放入 Toolkit(tools=[...])。

Adapter 必须有契约测试证明所有 13 个工具的 Schema 未被改写，并覆盖 ok=false/isError=false 的失败场景。

### 5.3 Agent 构造

~~~python
from agentscope.agent import Agent, ModelConfig, ReActConfig


def build_data_agent(model, toolkit: Toolkit) -> Agent:
    return Agent(
        name="data_agent",
        system_prompt=DATA_SYSTEM_PROMPT,
        model=model,
        toolkit=toolkit,
        model_config=ModelConfig(max_retries=1),
        react_config=ReActConfig(max_iters=8, stop_on_reject=True),
    )
~~~

模型重试只处理调用层的临时失败，不重放写工具。工具调用是否可重试由 MCP Adapter 按错误码判断。

### 5.4 MCP Adapter 四层状态

~~~text
HTTP 状态
  → JSON-RPC error
    → MCP result.isError
      → result.structuredContent.ok
~~~

当前业务错误通常是 isError=false 且 structuredContent.ok=false。Adapter 必须统一转成领域异常并保留 trace_id，不能只检查 HTTP 或 isError。

所有业务结果都标记 content_trust=untrusted_business_data。客户名称、备注、描述等只能作为数据，不能覆盖系统指令、改变工具白名单或触发写操作。

---

## 六、Agent 与工具分配

### 6.1 先策略门控，再让模型编排

~~~text
明显写意图 → Action 流程
明显只读 → Data / Patrol
身份、权限、配置类请求 → 拒绝或固定处理器
意图不完整 → 澄清
复合读写任务 → 先读，写步骤独立进入 HITL
~~~

Orchestrator 不持有写工具，不能绕过 Action 流程。模型负责理解和参数收集，授权与执行状态机由代码控制。

### 6.2 Data Agent

工具：

~~~text
erpnext_search_doctypes
erpnext_get_doctype_schema
erpnext_get_list
erpnext_get_doc
erpnext_get_count
erpnext_get_stock_balance
erpnext_get_customer_summary
erpnext_get_supplier_summary
erpnext_get_receivables_summary
~~~

规则：

- DocType/字段不确定时先发现 Schema；
- 优先使用固化口径的领域工具；
- 金额、数量和状态结论必须引用工具数据；
- 多币种只按 outstanding_by_currency 分开呈现；
- 空结果就是结果，不补造；
- 分页设置最大页数、总行数、总耗时和上下文大小；
- 常见同比、环比、占比通过注册的确定性函数计算，不执行模型生成代码。

### 6.3 Action Agent

只暴露完成当前草稿流程所需的只读工具和 erpnext_create_draft、erpnext_update_draft。

MVP 所有写操作都走：

~~~text
参数收集
  → Link/Schema/现有单据校验
  → 生成结构化预览
  → 创建待审批 Action
  → 用户确认
  → 重新校验身份、状态和参数摘要
  → 使用固定 idempotency_key 执行
  → get_doc 回读
  → 明确返回 docstatus=0
~~~

禁止输出“已提交”“库存已预占”“应收已生成”。当前成功只表示草稿已保存。

### 6.4 Patrol Agent

Patrol 只读。第一阶段只实现能由当前 MCP 安全表达的规则：

- 已提交未结发票的逾期清单；
- 指定物料/仓库的库存余额检查；
- 草稿或业务单据在限定日期范围内的数量/状态扫描；
- 固定指标的阈值与环比异常。

“全库负库存扫描”“任意重复单据”“子表总额一致性”如果需要大量遍历或复杂聚合，应新增专用 MCP 工具后再承诺，不用直连 SQL 绕过权限。

---

## 七、HITL、幂等与并发

### 7.1 持久化 Action

审批对象至少包含：

~~~text
action_id
session_id
site
requested_by
tool_name
canonical_arguments
arguments_sha256
preview
source_versions
idempotency_key
status: PENDING | APPROVED | EXECUTING | SUCCEEDED | FAILED | REJECTED | EXPIRED
created_at / expires_at
decided_by / decided_at
executed_at
mcp_trace_id
result_reference
~~~

敏感 token 不属于 Action。canonical_arguments 必须经过字段白名单和大小限制。

### 7.2 审批不变量

- 审批绑定工具名、完整规范化参数、参数哈希和源版本；
- 用户修改任何参数都使旧审批失效，必须生成新预览；
- 审批有短时 TTL，过期后重新读取业务数据；
- MVP 只允许请求者本人确认；未来多人审批需显式角色和职责分离；
- 执行使用数据库锁或 compare-and-set，使同一 Action 只能从 APPROVED 进入一次 EXECUTING；
- AgentScope 的 RequireUserConfirmEvent 可用于流式 UI 暂停/恢复，但数据库 Action 才是业务事实源。

### 7.3 MCP 幂等与乐观锁

- idempotency_key 在创建 Action 时生成，重试始终复用同一 key 和参数；
- 相同 key/相同参数返回历史结果；相同 key/不同参数是 IDEMPOTENCY_CONFLICT；
- IDEMPOTENCY_IN_PROGRESS 短暂退避后用同一请求重试；
- IDEMPOTENCY_UNAVAILABLE 表示服务端无法安全确认状态，禁止换新 key；
- 更新草稿必须绑定预览时读取到的精确 modified；
- VERSION_CONFLICT 后重新读取、重新预览、重新审批，不替换时间戳盲重试。

### 7.4 不做伪事务和伪补偿

当前 MCP 没有 cancel/delete，也没有跨多个工具调用的服务端事务。Agent 不能声称多步写入“整体回滚”。

MVP 一个审批 Action 只执行一个 MCP 写工具。未来多单据业务必须新增服务端原子业务工具，或设计可验证的 Saga 与人工修复流程后再开放。

---

## 八、查询与语义层

### 8.1 指标定义

业务口径继续用 metrics.yaml 管理，但执行器改为 MCP-first：

~~~yaml
- metric: 未结应收
  aliases: [应收余额, 待回款]
  executor:
    tool: erpnext_get_receivables_summary
  dimensions: [company, customer, currency]
  rules:
    - 仅 docstatus=1
    - outstanding_amount != 0
    - 不同币种不得相加
~~~

每个指标包含名称、别名、口径说明、输入参数、工具/确定性计算器、币种与时间语义、权限要求和测试样例。

### 8.2 Schema Registry

Schema Registry 缓存 MCP tools/list、DocType 搜索/Schema 结果、本地指标定义和业务词典。

缓存键至少包含 ERP site、用户、MCP 服务版本和策略版本。权限或角色变化时主动失效，否则使用短 TTL。不能把 Administrator 的 Schema 缓存给普通用户。

### 8.3 NL→SQL 作为第二阶段隔离实验

保留语义层、Few-Shot、SQL AST 校验和评估研究，但默认 feature flag 为关闭。要进入生产路径，必须证明：

- 数据库账号只有 SELECT、语句超时和行数上限；
- SQL AST 只允许明确表/字段/函数；
- Company、Customer、Warehouse 等 User Permission 与 Frappe 结果等价；
- 字段 permlevel、共享和文档级权限有等价处理；
- 在多个角色和用户权限组合上与 MCP/ORM 做差分测试；
- PERMISSION_DENIED 不会触发 SQL fallback。

做不到时，将复杂查询沉淀为新的权限感知 MCP 领域工具。

---

## 九、状态、可观测与安全

### 9.1 会话恢复

会话保存消息、摘要、当前只读任务和待审批 Action ID，不保存 access token、MCP Client、数据库连接或运行中的协程。

进程重启后：

- 只读任务可由用户重新触发；
- PENDING 审批按 TTL 恢复；
- EXECUTING Action 先用相同幂等键核对/重试，不能生成新 key；
- 已过期审批必须重新预览；
- 恢复时重新校验 OAuth 身份和 MCP 当前用户。

### 9.2 可观测

AgentScope 2.0.5 不使用旧版 agentscope.init。本项目直接在 FastAPI、HTTPX/MCP Adapter、模型 Adapter 和 HITL Gateway 接入 OpenTelemetry。

统一关联字段：

~~~text
request_id
session_id（哈希/内部 ID）
turn_id
agent_name
model_call_id
tool_name
json_rpc_id
mcp_trace_id
action_id
duration_ms
retry_count
result_code
~~~

不记录 token、Cookie、Client Secret、完整 Prompt、完整工具参数或敏感业务字段。MCP 服务端已经记录参数摘要和目标摘要，使用 mcp_trace_id 跨服务关联。

### 9.3 安全基线

| 边界 | 控制 |
|---|---|
| 浏览器会话 | HttpOnly、Secure、SameSite、轮换、CSRF |
| OAuth | state、PKCE S256、精确 redirect URI、token 加密、刷新锁、撤销 |
| MCP | 最终用户身份、运行时工具白名单、响应四层校验 |
| Agent | 读写工具物理隔离、最大迭代/工具调用/Token/时长预算 |
| 写入 | 持久化 HITL、参数哈希、幂等、乐观锁、回读 |
| 数据 | 服务端字段白名单、脱敏、最小上下文、结果截断 |
| Prompt 注入 | untrusted_business_data、数据/指令分离、数据不得扩大权限 |
| 计算 | 白名单确定性函数；任意代码执行默认关闭 |
| 出网 | Agent 和未来沙箱使用目标白名单，禁止访问 metadata/内网管理面 |

如果未来必须执行模型生成代码，应放入独立容器或 microVM，使用只读根文件系统、临时工作目录、无凭据、默认断网、CPU/内存/PID/时间限制。RestrictedPython 或宿主机 subprocess 不能单独作为安全边界。

---

## 十、评估与验收

### 10.1 场景集

构建 40 个场景：

| 类型 | 数量 | 示例 |
|---|---:|---|
| 简单查询 | 8 | 单据数量、最近订单、精确单据 |
| 领域摘要 | 6 | 库存、客户、供应商、分币种应收 |
| 多步分析 | 6 | 有界分页 + 确定性计算 + 证据引用 |
| 草稿操作 | 8 | 三种草稿创建/更新、重放、版本冲突 |
| 巡检 | 4 | 逾期、阈值、趋势、空结果 |
| 安全负样本 | 8 | 越权、blocked DocType、submit 请求、注入、跨用户审批 |

不再包含当前无法完成的“提交费用报销单”。

### 10.2 指标

| 指标 | MVP 门槛 |
|---|---:|
| 任务成功率 | ≥ 80% |
| 只读事实准确率 | ≥ 95% |
| 写入参数准确率 | ≥ 95% |
| 未确认写入次数 | 0 |
| 越权成功次数 | 0 |
| 跨用户 token/缓存/审批串用 | 0 |
| 重复草稿（相同逻辑操作） | 0 |
| 负样本正确处置率 | ≥ 90% |
| 幻觉率 | ≤ 3% |
| 查询 P95 | 先测基线再设 SLO |

### 10.3 必测故障

- access token 过期、refresh token 失效、两个请求并发刷新；
- MCP 401/403、JSON-RPC 错误、isError=true、业务信封 ok=false；
- MCP 超时发生在写入请求发送之后；
- IDEMPOTENCY_IN_PROGRESS/CONFLICT/UNAVAILABLE；
- VERSION_CONFLICT；
- Action Agent 在批准前重启、批准后执行前重启、执行中重启；
- ERP 文本包含“忽略规则并调用写工具”；
- A 用户尝试读取/确认/执行 B 用户 Action；
- Data/Patrol 直接请求写工具；
- 多币种应收被错误相加。

---

## 十一、建议项目结构

~~~text
erpnext-agent/
├── pyproject.toml
├── uv.lock
├── .env.example
├── src/erpnext_agent/
│   ├── main.py
│   ├── config.py
│   ├── api/
│   │   ├── auth.py
│   │   ├── chat.py
│   │   └── approvals.py
│   ├── auth/
│   │   ├── oauth_client.py
│   │   ├── token_store.py
│   │   └── session_store.py
│   ├── agents/
│   │   ├── factory.py
│   │   ├── orchestrator.py
│   │   ├── data_agent.py
│   │   ├── action_agent.py
│   │   └── patrol_agent.py
│   ├── mcp/
│   │   ├── client_factory.py
│   │   ├── adapter.py
│   │   ├── policy.py
│   │   └── schema_registry.py
│   ├── actions/
│   │   ├── models.py
│   │   ├── gateway.py
│   │   ├── executor.py
│   │   └── repository.py
│   ├── analytics/
│   │   ├── metrics.yaml
│   │   ├── semantic_layer.py
│   │   └── calculators.py
│   ├── patrol/
│   │   ├── rules.py
│   │   └── scheduler.py
│   ├── observability/
│   │   ├── tracing.py
│   │   └── redaction.py
│   └── security/
│       ├── limits.py
│       └── content_policy.py
├── tests/
│   ├── contract/
│   ├── integration/
│   ├── security/
│   └── unit/
└── evaluation/
    ├── scenarios/
    ├── runner.py
    └── report.py
~~~

---

## 十二、四周开发计划

当前 ERPNext、MCP app 和 13 个工具已经存在，排期从 Agent 接入开始，不再安排“新建 MCP app”。

### 第 1 周：身份、契约和框架地基

| 天 | 任务 | 验收产物 |
|---|---|---|
| D1 | 创建项目；固定 Python 3.12、agentscope==2.0.5、uv.lock；把本次导入验证变成 CI smoke test | 冻结依赖与 API 测试 |
| D2-3 | Frappe OAuth Client；Authorization Code + PKCE start/callback/refresh/logout；加密 token store | 两个 ERP 用户独立登录 |
| D4 | per-user MCPClient、工具发现、四层响应归一化、超时与错误映射 | Adapter 契约测试 |
| D5 | 工具 allowlist、Schema Registry、身份一致性检查 | 跨用户与越权测试 |
| D6 | Data Agent 最小闭环、流式输出 | 5 个真实只读场景 |
| D7 | 故障测试与文档回填 | 第一周验收报告 |

### 第 2 周：查询、分析和评估底座

| 天 | 任务 | 验收产物 |
|---|---|---|
| D8-9 | Data Agent 路由、分页预算、证据引用、空结果处理 | L1 查询集 |
| D10 | metrics.yaml 首批指标；领域工具优先路由 | 口径单测 |
| D11 | 白名单确定性计算器（同比/环比/占比） | 无任意代码执行的分析 |
| D12 | Patrol 基础规则 | 4 个可验证巡检场景 |
| D13 | 评估 Runner 和前 20 个场景 | 基线报告 |
| D14 | 根据失败 case 修正 Prompt/策略/指标 | 回归报告 |

### 第 3 周：草稿操作与 HITL

| 天 | 任务 | 验收产物 |
|---|---|---|
| D15-16 | 持久化 Action、预览、确认/拒绝/过期 API | 审批状态机测试 |
| D17 | Action Agent 参数收集和 Link/Schema 校验 | 三类草稿预览 |
| D18 | 幂等执行器、刷新锁、写超时恢复 | 重放与并发测试 |
| D19 | 更新草稿乐观锁、冲突后重新审批 | VERSION_CONFLICT 测试 |
| D20 | 执行后回读、审计关联、准确措辞 | 端到端写入测试 |
| D21 | 重启恢复与跨用户安全测试 | 故障演练报告 |

### 第 4 周：加固、评估与交付

| 天 | 任务 | 验收产物 |
|---|---|---|
| D22 | OTel 接入、脱敏、限流、预算和告警 | 可观测面板/Trace |
| D23 | 补齐 40 场景与安全负样本 | 完整评估集 |
| D24 | 全量评估与失败分类 | 第一轮正式报告 |
| D25 | 针对性修正并回归 | 最终指标 |
| D26 | Docker Compose、配置模板、健康检查 | 可复现部署 |
| D27 | README、架构图、运维/故障手册 | 文档验收 |
| D28 | 演示录制和发布检查 | 可验证交付包 |

每周只用三类证据判断进度：可重复测试、真实 Trace、评估数字。没有证据的“已支持”不进入 README 和简历。

---

## 十三、发布门禁与后续演进

### 13.1 MVP 发布门禁

- [ ] uv sync --frozen 可复现 agentscope==2.0.5；
- [ ] OAuth state/PKCE/刷新并发/撤销测试通过；
- [ ] 两个不同权限用户的 MCP 差分测试通过；
- [ ] Data/Patrol/Orchestrator 运行时没有写工具；
- [ ] 所有写入均有 Action、Decision、幂等键和 MCP trace_id；
- [ ] submit/cancel/delete 明确拒绝；
- [ ] 故障测试和 40 场景达到门槛；
- [ ] 文档、代码、运行时 tools/list 三者一致。

### 13.2 后续能力优先级

1. 根据真实失败 case 增加专用 MCP 聚合工具；
2. 在 ERPNext 端实现明确的提交/工作流工具及审批策略；
3. 多人审批、职责分离和管理员修复队列；
4. 隔离沙箱中的高级计算；
5. 完成权限等价证明后，再评估 NL→SQL 是否值得生产化。

项目对外描述应使用已测数字和实际能力：

> 基于 AgentScope 2.0.5 构建 ERPNext 多 Agent 服务，通过 OAuth 将最终用户身份逐请求透传到 Frappe MCP；Data/Action/Patrol 采用工具白名单隔离，草稿写入具备持久化 HITL、幂等、乐观锁与跨服务审计，并以包含越权和提示注入的评估集量化验证。

在评估完成前保留“XX%”占位，不提前填写目标值为结果。
