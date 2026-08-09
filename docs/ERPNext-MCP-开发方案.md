# ERPNext MCP 工具层开发方案

> 上位方案：[ERPNext-Agent-开发方案.md](./ERPNext-Agent-开发方案.md)  
> 实现载体：`apps/erpnext_mcp_tools` Frappe App  
> MCP 实现：Frappe 官方 `frappe-mcp==0.1.0`  
> 当前环境：Frappe/ERPNext `16.23.0`、Python `3.14`、站点 `dev.localhost`

---

## 一、目标与范围

本阶段先完成 ERPNext 内部的 MCP Server，使后续 AgentScope 服务能够通过标准 MCP Tools 安全地读取 ERPNext 元数据和业务单据，并执行受控的单据操作。

### 1.1 本阶段交付

1. 在现有 `erpnext_mcp_tools` App 中注册一个 MCP endpoint。
2. 实现 Schema、通用只读、销售、采购、库存、财务和受控写操作工具。
3. 所有工具继承当前 Frappe 请求用户身份，并显式执行权限校验。
4. 对写操作增加白名单、状态检查、幂等键、审计记录和危险操作拦截。
5. 完成单元测试、权限负样本、JSON-RPC 集成测试和 MCP Inspector 验收。

### 1.2 不在本阶段实现

- AgentScope Agent、FastAPI、Redis 会话和聊天 UI。
- NL→SQL 生成器和数据库直连只读通道。
- MCP Resources、Prompts、SSE 工具流式输出。
- 由 MCP 工具直接完成高风险自动提交、作废、删除或财务过账。

官方 `frappe-mcp 0.1.0` 当前成熟能力集中在同步 Tools。代码虽然响应部分其他 MCP 方法，但尚无可用的 Resources/Prompts 注册接口，因此一期只承诺 Tools。

---

## 二、当前项目基线

### 2.1 已有资源

- App 骨架已存在：`apps/erpnext_mcp_tools`，无需再次执行 `bench new-app`。
- App 已安装到 `dev.localhost` 且 Phase 0 migrate 已由项目方完成，本轮未重复执行。
- Bench 虚拟环境已安装 `frappe-mcp 0.1.0` 和 `jsonschema 4.26.0`。
- Frappe 与 ERPNext 当前版本均为 `16.23.0`。

### 2.2 依赖兼容性现状

Frappe 16 当前要求：

- Python `>=3.14,<3.15`
- Pydantic `~=2.12.5`
- Werkzeug `==3.1.6`

而 `frappe-mcp 0.1.0` 元数据要求 Pydantic `~=2.11.7`、Werkzeug `==3.1.3`。不能为了满足 MCP 的旧锁定而降级 Frappe 核心依赖。本项目采取以下策略：

1. 保留 Frappe 16 的 Pydantic/Werkzeug 版本。
2. `frappe-mcp` 包本体使用 `--no-deps` 安装，缺失的 `jsonschema` 单独安装。
3. CI 中执行实际导入、Schema 生成、`tools/list` 和 `tools/call` 测试，而不是仅依赖 `pip check`。
4. 在 App `pyproject.toml` 中记录直接依赖和兼容性注释，等待上游发布兼容版本后恢复标准解析。
5. 禁止静默升级 `frappe-mcp`；升级前跑完整协议与权限回归测试。

### 2.3 实际交付状态（2026-08-08）

- Phase 0–4 的代码、测试与文档均已完成。
- endpoint 共注册 13 个同步 Tools；submit、cancel、delete 均未注册。
- 38 项 App 集成测试通过，覆盖协议、真实 RBAC/User Permission、字段 permlevel、查询、领域口径、写入幂等、乐观锁、审计失败和安全负样本。
- 已使用 `sale@sale.com`、`stock@stock.com`、`308642281@qq.com` 三个现存系统用户建立真实 HTTP Session，验证身份不串号、DocType 读写矩阵、敏感字段阻断和字段 permlevel 差异。
- 未认证 HTTP 请求返回 403；认证后的 initialize、tools/list、tools/call 均已通过。
- 官方 `frappe-mcp check --app erpnext_mcp_tools --verbose` 退出成功。
- 官方 MCP Inspector CLI 已通过 Streamable HTTP 发现 13 个工具并成功调用 `erpnext_health`。
- 当前站点没有 HRMS，因此 `Expense Claim` 不进入运行时白名单；这不是缺失实现，而是避免发布必然失败的伪能力。

---

## 三、技术架构

```text
AgentScope / MCP Client
        │ Streamable HTTP + OAuth/Session/API Auth
        ▼
/api/method/erpnext_mcp_tools.mcp.handle_mcp
        │
        ├── frappe-mcp：JSON-RPC、initialize、tools/list、tools/call
        ├── Tool Registry：Schema / Query / Sales / Purchase / Stock / Accounts
        ├── Security：身份、权限、白名单、参数校验、风险拦截
        ├── Audit：调用人、工具、目标、结果、耗时、trace_id
        └── Frappe ORM：get_list / get_doc / insert / save / submit
                              │
                              ▼
                         ERPNext DocType
```

### 3.1 endpoint

```python
# erpnext_mcp_tools/mcp.py
from frappe_mcp import MCP

mcp = MCP("erpnext-mcp-tools")

@mcp.register(allow_guest=False)
def handle_mcp():
    from erpnext_mcp_tools.tools import register_all  # noqa: F401
```

endpoint：

```text
POST /api/method/erpnext_mcp_tools.mcp.handle_mcp
```

`allow_guest` 必须保持 `False`。生产环境使用 Frappe OAuth；开发期可以使用已登录 Session Cookie 或 API Key/Secret。是否有权限取决于进入该 HTTP 请求的 Frappe 用户身份，并非 MCP 客户端天然自动继承权限。

### 3.2 同步执行模型

官方库直接运行同步 Python 函数，工具不得定义成 `async def`，也不能返回 coroutine。耗时操作应提交 Frappe background job，并返回 job id；一期工具调用目标 P95 小于 3 秒。

---

## 四、目录设计

```text
apps/erpnext_mcp_tools/
├── pyproject.toml
├── README.md
└── erpnext_mcp_tools/
    ├── hooks.py
    ├── mcp.py                         # MCP 实例和唯一 endpoint
    ├── tools/
    │   ├── __init__.py                # 幂等注册全部工具
    │   ├── schema.py                  # DocType 元数据
    │   ├── query.py                   # 通用安全只读
    │   ├── sales.py                   # 销售专用工具
    │   ├── purchase.py                # 采购专用工具
    │   ├── stock.py                   # 库存专用工具
    │   ├── accounts.py                # 财务只读工具
    │   └── actions.py                 # 受控创建/更新草稿
    ├── security/
    │   ├── permissions.py             # Frappe 权限与角色检查
    │   ├── policies.py                # DocType/字段/操作白名单
    │   ├── validators.py              # filters/fields/payload 校验
    │   └── masking.py                 # 敏感字段脱敏
    ├── services/
    │   ├── documents.py               # ORM 封装
    │   ├── schema.py                  # Meta 序列化
    │   ├── idempotency.py             # 写操作幂等
    │   └── audit.py                   # 审计记录
    ├── exceptions.py                  # 稳定错误码
    └── tests/
        ├── test_mcp_protocol.py
        ├── test_query_tools.py
        ├── test_domain_tools.py
        ├── test_action_tools.py
        ├── test_permissions.py
        └── test_response_guard.py
```

工具文件只负责 MCP 入参与输出，业务逻辑放在 `services`，安全规则放在 `security`，避免装饰器函数变成无法测试的大函数。

---

## 五、工具契约

### 5.1 通用返回结构

所有工具返回可 JSON 序列化的 `dict`。`frappe-mcp` 会同时生成文本 content 和 structuredContent。

```json
{
  "ok": true,
  "data": {},
  "meta": {
    "trace_id": "...",
    "tool": "erpnext_get_doc",
    "user": "user@example.com"
  }
}
```

失败时不把数据库异常、堆栈或密钥返回给模型：

```json
{
  "ok": false,
  "error": {
    "code": "PERMISSION_DENIED",
    "message": "当前用户无权读取 Sales Invoice"
  },
  "meta": {"trace_id": "..."}
}
```

注意：官方库捕获未处理异常时会把异常字符串写入 MCP content，因此工具边界必须捕获内部异常、服务端记录详情、客户端只返回稳定错误。

### 5.2 第一批必做工具

| 工具 | 风险 | 说明 |
|---|---:|---|
| `erpnext_health` | 只读 | 返回站点、用户、版本和服务状态，不返回敏感配置 |
| `erpnext_get_current_user` | 只读 | 返回当前用户、角色和允许模块 |
| `erpnext_search_doctypes` | 只读 | 在允许范围内搜索 DocType |
| `erpnext_get_doctype_schema` | 只读 | 字段、类型、必填、Link/Table 关系和状态能力 |
| `erpnext_get_list` | 只读 | ORM 列表查询，强制权限、字段白名单、分页上限 |
| `erpnext_get_doc` | 只读 | 获取单据及允许的子表字段 |
| `erpnext_get_count` | 只读 | 受控计数，避免拉全量数据 |
| `erpnext_get_stock_balance` | 只读 | 按 Item/Warehouse 查询库存 |
| `erpnext_get_customer_summary` | 只读 | 客户基本信息、信用和应收摘要 |
| `erpnext_get_supplier_summary` | 只读 | 供应商基本信息与应付摘要 |
| `erpnext_create_draft` | 写入 | 仅白名单 DocType，创建草稿，不自动提交 |
| `erpnext_update_draft` | 写入 | 仅更新草稿和允许字段，支持版本冲突检查 |
| `erpnext_submit_doc` | 高风险 | 当前未注册；未来必须携带审批令牌，不接受模型口头确认 |

### 5.3 通用查询限制

`erpnext_get_list` 输入建议：

```python
def erpnext_get_list(
    doctype: str,
    fields: list[str] | None = None,
    filters: dict | list | None = None,
    order_by: str | None = None,
    limit_start: int = 0,
    limit_page_length: int = 20,
) -> dict:
    """Query permitted ERPNext documents with current-user permissions."""
```

约束：

- `limit_page_length` 默认 20、最大 100。
- 禁止客户端传 `ignore_permissions`、`pluck`、任意 SQL、debug 等参数。
- `fields` 必须属于 Meta 且通过字段策略；默认只返回 `name`、`modified`、`docstatus` 和标题字段。
- `order_by` 只能使用允许字段和 `asc|desc`。
- filters 递归限制层数、数量和操作符，禁止注入 SQL 表达式。
- 不提供任意 `frappe.db.sql` MCP 工具；复杂 SQL 留给上位方案的独立只读通道。

### 5.4 写操作策略

一期只允许创建和修改草稿，建议白名单：

- `Sales Order`
- `Purchase Order`
- `Material Request`
- `Expense Claim`（当前站点未安装 HRMS，暂不启用；安装 HRMS 并补齐权限测试后再加入）

写入流程：

1. 校验用户已登录且不是 Guest。
2. 校验 DocType、动作和字段白名单。
3. `frappe.has_permission(..., ptype="create"|"write")`。
4. 校验 Link 值、必填字段、公司范围和业务规则。
5. 要求 `idempotency_key`，同一用户和工具重复请求返回第一次结果。
6. `doc.insert()` 或 `doc.save()`，不使用 `ignore_permissions=True`。
7. 返回 `name`、`docstatus`、关键字段和审计 trace_id。

严禁提供泛化 delete/cancel 工具。submit 工具只有 Agent 层完成 HITL 后，携带服务端签名、一次性、短 TTL、绑定用户/工具/目标/参数摘要的 approval token 才能执行。

---

## 六、安全与权限设计

### 6.1 四层防线

1. **身份层**：`allow_guest=False`，校验 `frappe.session.user != "Guest"`。
2. **权限层**：使用 Frappe RBAC、User Permission 和字段 permlevel；所有 ORM 调用保持权限检查，Schema/列表/详情按当前用户裁剪，调用底层库存函数前另校验 Stock Ledger Entry 读取权。
3. **策略层**：DocType、字段、动作白名单；敏感字段黑名单优先于角色权限。
4. **业务层**：docstatus、公司、金额、库存、信用、Workflow 状态和幂等校验。

### 6.2 敏感字段

默认拒绝或脱敏：密码、secret、token、银行账号、税号、个人电话/邮箱、薪资和用户认证相关字段。以下 DocType 默认不向通用工具开放：

- `User`、`Has Role`、`OAuth Client`、`OAuth Bearer Token`
- `Integration Request`、`Error Log`、`Access Log`
- `Salary Slip`、`Employee` 的敏感字段
- `Bank Account`、认证和系统配置类 DocType

### 6.3 审计

每次调用至少记录：`trace_id`、时间、用户、IP、工具、目标 DocType/单据、参数摘要、风险等级、执行结果、耗时和错误码。敏感值不落日志。审计失败时写操作 fail closed，读操作可记录告警后继续。

---

## 七、官方库的正确使用方式

### 7.1 Tool 定义

```python
from frappe_mcp import ToolAnnotations
from erpnext_mcp_tools.mcp import mcp

@mcp.tool(
    annotations=ToolAnnotations(
        title="Get ERPNext document",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def erpnext_get_doc(doctype: str, name: str) -> dict:
    """Get one permitted ERPNext document.

    Args:
        doctype: Allowed ERPNext DocType name.
        name: Document name.
    """
    ...
```

使用基础 Python 类型标注和 Google 风格 docstring，使官方库自动生成 inputSchema。复杂嵌套参数若推断不足，显式传 `input_schema`。

### 7.2 避免重复注册

endpoint 每次请求都会执行入口函数中的 import。Python import cache 通常能避免重复运行，但测试热加载可能重复注册。`tools/__init__.py` 应只有稳定导入，不在请求时动态生成工具名；测试需验证连续调用 `tools/list` 不出现重复或顺序漂移。

---

## 八、实施顺序

### Phase 0：环境与基线（已完成）

- 将 App 安装到 `dev.localhost` 并 migrate。
- 在 App 依赖说明中记录 `frappe-mcp` 兼容策略。
- 创建 endpoint，仅注册 `erpnext_health`。
- 用 JSON-RPC 完成 initialize、notifications/initialized、tools/list、tools/call。

### Phase 1：Schema 与只读工具（已完成）

- 完成 current user、DocType 搜索、schema、list、get、count。
- 建立 DocType/字段策略和分页限制。
- 覆盖 Sales User、Accounts User、无权限用户三类测试账号。

### Phase 2：ERPNext 领域查询（已完成）
- 完成客户、供应商、库存摘要工具。
- 返回数据附带口径、单位、币种和查询时间。
- 验证空结果、取消单据、跨公司和无权限场景。

### Phase 3：受控写操作（已完成）

- 完成白名单草稿创建与更新。
- 加入幂等键、版本冲突和审计。
- submit 只完成审批令牌接口设计，默认关闭。
- 当前环境未安装 HRMS，`Expense Claim` 不进入运行时白名单，避免暴露必然失败的伪能力。

### Phase 4：协议、安全与文档（已完成）

- 单元测试、权限负样本、协议集成测试。
- 官方 `frappe-mcp check --app erpnext_mcp_tools --verbose`。
- MCP Inspector 使用 Streamable HTTP + 已认证开发 Session 验收；生产接入使用 Frappe OAuth。
- 更新 App README：认证、工具清单、示例和故障排查。

预计开发时间：4 天。先交付稳定只读与草稿写入，再接 Agent 层。

---

## 九、测试与验收

### 9.1 自动化测试矩阵

| 类型 | 必测内容 |
|---|---|
| Schema | 必填、Optional、list/dict、docstring 描述正确生成 |
| 协议 | initialize、tools/list、tools/call、未知工具、非法参数 |
| 权限 | 有权、无权、Guest、User Permission、跨公司、字段 permlevel、底层领域函数的间接数据权限 |
| 查询 | filters、分页、最大 limit、非法字段、敏感字段、空结果 |
| 写入 | 创建草稿、重复幂等键、非法 DocType、非法字段、无权限、并发修改 |
| 安全 | SQL 注入字符串、越权字段、异常信息泄露、Prompt 注入文本 |
| 兼容 | Frappe 16/Pydantic 2.12/Werkzeug 3.1.6 下完整回归 |

### 9.2 JSON-RPC 冒烟请求

```bash
curl -X POST \\
  -H 'Content-Type: application/json' \\
  -H 'Authorization: token API_KEY:API_SECRET' \\
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke-test","version":"1.0"}}}' \\
  http://dev.localhost:8000/api/method/erpnext_mcp_tools.mcp.handle_mcp
```

密钥只通过环境变量或 Secret 管理器注入，不写入仓库、命令历史和测试快照。

### 9.3 完成定义（Definition of Done）

- App 已安装且 migrate 成功。
- endpoint 未认证访问被拒绝，合法身份可 initialize。
- `tools/list` 返回全部工具且 Schema 可被 MCP 客户端解析。
- 全部只读工具遵循当前用户权限，越权与敏感字段负样本全部通过。
- 草稿写操作具备白名单、幂等、版本校验和审计，无泛化删除能力。
- 关键工具单元测试与 HTTP 集成测试通过。
- `frappe-mcp check` 和 Inspector 验收通过。
- README 与工具契约同步，Agent 开发可直接据此生成客户端适配。

---

## 十、风险与后续演进

| 风险 | 应对 |
|---|---|
| `frappe-mcp` 实验性和 breaking change | 固定版本、封装 endpoint/工具契约、升级跑协议回归 |
| Frappe 16 与 MCP 包元数据冲突 | 保留 Frappe 依赖，以运行测试作兼容基线，跟踪上游版本 |
| 泛化 CRUD 扩大攻击面 | 只读通用、写入白名单、危险动作专用工具 |
| 模型重复调用产生重复单据 | 强制幂等键和结果复用 |
| MCP 异常文本泄露内部信息 | 工具边界转换稳定错误，详情只进服务端日志 |
| 长任务阻塞 WSGI worker | 转 background job，MCP 返回 job id |

后续 Agent 层接入时，优先消费 Schema 与只读工具；写操作必须先经过 Agent HITL 网关，再调用 MCP 草稿工具。NL→SQL 不进入本 MCP endpoint，继续采用隔离的只读账号和独立安全审计。

---

## 十一、开发交付清单

- [x] MCP endpoint 与健康检查工具
- [x] 当前用户、DocType 搜索与 Schema 工具
- [x] 通用列表、详情和计数工具
- [x] 客户、供应商和库存领域工具
- [x] 白名单草稿创建、更新工具
- [x] 权限策略、字段策略、脱敏和输入校验
- [x] 幂等、版本冲突与调用审计
- [x] 协议、权限、查询、写入和安全测试
- [x] CLI、HTTP 与 MCP Inspector 验收
- [x] App README 与 Agent 接入说明
