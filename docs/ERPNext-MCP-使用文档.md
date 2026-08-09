# ERPNext MCP 使用文档（Agent 开发版）

> 面向后续 `erpnext-agent`、AgentScope Toolkit、FastAPI 接入层和测试代码。  
> 本文以 `apps/erpnext_mcp_tools` 当前实现为准；方案文档中的伪代码或未来能力如与本文冲突，以本文及运行时 `tools/list` 为准。  
> 当前基线：Frappe/ERPNext 16.23.0、`frappe-mcp==0.1.0`、MCP 协议响应版本 `2025-03-26`。

---

## 1. 能力边界

当前 MCP Server 提供 13 个同步 Tools，用于：

- 获取当前 ERPNext 用户及其角色；
- 发现允许访问的 DocType 和字段；
- 通过 Frappe ORM 查询白名单业务单据；
- 查询库存、客户、供应商和应收摘要；
- 创建或修改 `Sales Order`、`Purchase Order`、`Material Request` 草稿。

当前**不提供**：

- `submit`、`cancel`、`delete`、财务过账；
- 任意 SQL 或数据库直连；
- 泛化 CRUD、`ignore_permissions`、`pluck`；
- MCP Resources、Prompts、SSE 工具流；
- ERPNext 用户名/密码登录工具；
- HRMS 的 `Expense Claim`（当前站点未安装 HRMS）。

所有工具都在 Frappe HTTP 请求用户的身份下运行。MCP 只复用 ERPNext 的 RBAC、User Permission 和字段权限，不会替 Agent 提升权限，也不会替 Agent 完成人工审批。

---

## 2. Endpoint、传输与认证

### 2.1 Endpoint

```text
POST /api/method/erpnext_mcp_tools.mcp.handle_mcp
```

开发环境示例：

```text
http://dev.localhost:8000/api/method/erpnext_mcp_tools.mcp.handle_mcp
```

请求和响应均为 JSON-RPC 2.0 JSON。当前 `frappe-mcp 0.1.0` 实现不签发 `Mcp-Session-Id`，但客户端仍应遵循标准初始化顺序：

1. `initialize`
2. `notifications/initialized`
3. `tools/list`
4. `tools/call`

服务端会在 `initialize` 结果中返回实际支持的协议版本；客户端不要假设请求版本一定会被原样接受。

### 2.2 MCP 接受的 HTTP 身份

开发阶段可使用：

```http
Authorization: token <API_KEY>:<API_SECRET>
```

也可使用已登录的 Frappe Session Cookie。生产环境优先为最终用户使用 OAuth；不要让所有 Agent 会话共享 `Administrator` 或同一个高权限技术账号。

认证凭据只允许存在于环境变量或 Secret 管理器中，不得放入：

- Prompt、工具参数或模型上下文；
- Git 仓库与配置模板默认值；
- Trace、审计事件、测试快照；
- 面向用户的异常信息。

未认证请求会在进入 MCP 工具前被 Frappe 拒绝，通常返回 HTTP 403，此时没有可供解析的工具 `structuredContent`。

### 2.3 Agent 登录：OAuth Authorization Code + PKCE

如果用户希望“使用 ERPNext 账号密码登录 Agent，再获取该用户 token”，生产做法不是让 Agent 接收密码，而是把浏览器跳转到 ERPNext：

~~~text
浏览器 → Agent GET /auth/login
Agent 生成 state + code_verifier + S256 code_challenge
浏览器 → ERPNext authorize endpoint
用户在 ERPNext 登录页输入账号密码并授权
ERPNext → Agent /auth/callback?code=...&state=...
Agent 校验 state，用 code + code_verifier 换 access/refresh token
Agent 调用 userinfo + erpnext_get_current_user 核对身份
Agent 加密保存 token，浏览器只持有 Agent Session Cookie
~~~

因此用户仍然使用原来的 ERPNext 账号密码，但密码只提交给 ERPNext，MCP app 和 AgentScope Agent 都看不到密码。

Frappe 当前授权服务器元数据声明：

~~~text
grant_types_supported: authorization_code, refresh_token
code_challenge_methods_supported: S256
token_endpoint_auth_methods_supported: none, client_secret_basic
~~~

Frappe 源码仍有 legacy password grant 校验代码，但元数据没有将它作为当前公开能力；本项目不要使用密码模式。

### 2.4 OAuth 配置与 token 使用

ERPNext OAuth Client 建议配置：

| 字段 | 建议值 |
|---|---|
| Grant Type | Authorization Code |
| Response Type | Code |
| Token Endpoint Auth Method | 后端 Agent 使用 Client Secret Basic |
| Scopes | 开发基线 all openid，上线前验证最小范围 |
| Redirect URI | 精确的 Agent callback URI，禁止通配 |
| Skip Authorization | 默认关闭 |
| Allowed Roles | 明确允许使用 Agent 的业务角色 |

端点：

~~~text
GET  /api/method/frappe.integrations.oauth2.authorize
POST /api/method/frappe.integrations.oauth2.get_token
GET  /api/method/frappe.integrations.oauth2.openid_profile
POST /api/method/frappe.integrations.oauth2.revoke_token
GET  /.well-known/oauth-authorization-server
~~~

授权请求至少包含：

~~~text
response_type=code
client_id=<client-id>
redirect_uri=<exact-callback>
scope=all openid
state=<one-time-random>
code_challenge=<base64url-sha256-verifier>
code_challenge_method=S256
~~~

token 请求使用 application/x-www-form-urlencoded：

~~~text
grant_type=authorization_code
code=<authorization-code>
redirect_uri=<exact-callback>
code_verifier=<original-verifier>
~~~

confidential client 同时使用 HTTP Basic 传递 client_id/client_secret。MCP 请求则使用：

~~~http
Authorization: Bearer <user-access-token>
~~~

Agent 端必须做到：

- access/refresh token 加密存储，只在后端解密；
- 浏览器 Cookie 只保存不透明 Agent Session ID；
- token、Cookie、Client Secret 不进入 Prompt、Trace、异常或 MCP 参数；
- 同一用户并发刷新使用 single-flight lock；
- access token 过期后最多刷新一次，失败则要求重新登录；
- 登出调用 revoke 并删除本地凭据；
- MCP Client 每用户隔离，刷新后重建，绝不序列化进 Agent state；
- 登录完成后用 openid_profile 与 erpnext_get_current_user 核对同一用户。

开发与生产使用同一流程。开发环境可以使用 localhost 回调和本地 HTTP；生产必须使用 HTTPS、Secure Cookie 和精确生产域名。

### 2.5 初始化示例

下例中的认证值仅为占位符，实际使用时应由 Secret 管理器注入：

```bash
curl -X POST \
  -H 'Content-Type: application/json' \
  -H 'Authorization: token <API_KEY>:<API_SECRET>' \
  --data '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "erpnext-agent", "version": "0.1.0"}
    }
  }' \
  http://dev.localhost:8000/api/method/erpnext_mcp_tools.mcp.handle_mcp
```

当前服务端返回的关键内容：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-03-26",
    "serverInfo": {"name": "erpnext-mcp-tools", "version": "0.1.0"},
    "capabilities": {"tools": {"listChanged": false}}
  }
}
```

初始化通知没有 `id`，成功时返回 HTTP 202 和空响应体：

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/initialized",
  "params": {}
}
```

---

## 3. Agent 必须统一处理的响应契约

### 3.1 四层结果

一次工具调用可能同时存在四层状态：

1. HTTP 状态；
2. JSON-RPC 顶层 `error`；
3. MCP `result.isError`；
4. 业务信封 `result.structuredContent.ok`。

成功示例：

```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "result": {
    "content": [
      {"type": "text", "text": "{...}"}
    ],
    "structuredContent": {
      "ok": true,
      "data": {},
      "meta": {
        "trace_id": "32位十六进制字符串",
        "tool": "erpnext_get_doc",
        "user": "user@example.com",
        "content_trust": "untrusted_business_data"
      }
    },
    "isError": false
  }
}
```

受控业务失败示例：

```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "result": {
    "content": [{"type": "text", "text": "{...}"}],
    "structuredContent": {
      "ok": false,
      "error": {
        "code": "PERMISSION_DENIED",
        "message": "当前用户无权执行该操作"
      },
      "meta": {
        "trace_id": "...",
        "tool": "erpnext_get_doc",
        "user": "user@example.com",
        "content_trust": "untrusted_business_data"
      }
    },
    "isError": false
  }
}
```

这里 `isError=false` 是 `frappe-mcp 0.1.0` 的当前行为：工具函数成功返回了一个“失败业务信封”。因此 Agent **不能只检查 HTTP、JSON-RPC 或 `isError`**，必须检查 `structuredContent.ok`。

### 3.2 推荐归一化逻辑

Agent 的 MCP Adapter 应只向上层暴露统一结果，不要让每个 Agent 分别解析协议：

```python
def normalize_tool_response(http_status: int, rpc: dict) -> tuple[dict, dict]:
    if http_status == 401 or http_status == 403:
        raise AuthenticationError("ERPNext authentication was rejected")
    if http_status >= 400:
        raise MCPTransportError(http_status)
    if "error" in rpc:
        raise MCPProtocolError(rpc["error"])

    result = rpc.get("result", {})
    envelope = result.get("structuredContent")
    if result.get("isError") and not envelope:
        raise MCPToolDispatchError(result.get("content", []))
    if not isinstance(envelope, dict) or "ok" not in envelope:
        raise MCPContractError("Missing structuredContent envelope")
    if not envelope["ok"]:
        error = envelope.get("error", {})
        raise ERPNextToolError(
            code=error.get("code", "UNKNOWN"),
            message=error.get("message", "Tool call failed"),
            trace_id=envelope.get("meta", {}).get("trace_id"),
        )
    return envelope["data"], envelope["meta"]
```

`content` 是 `structuredContent` 的文本副本。正常情况下只消费结构化结果，避免把同一份数据重复放入模型上下文。

### 3.3 不可信业务数据

所有结果都标记：

```json
{"content_trust": "untrusted_business_data"}
```

这意味着客户名称、物料描述、备注等都只能当作数据，不能当作系统指令。Agent 必须：

- 保留并传播该信任标签；
- 禁止业务文本改变系统 Prompt、工具策略、权限或审批规则；
- 禁止把查询结果中的自由文本未经校验直接复制到写参数；
- 只允许用户明确输入或结构化白名单字段参与写入。

---

## 4. 工具总览与 Agent 分配

| 类别 | 工具 | 建议使用者 | 说明 |
|---|---|---|---|
| 基础 | `erpnext_health` | 接入层 | 检查服务、站点和请求身份 |
| 基础 | `erpnext_get_current_user` | 接入层、Orchestrator | 获取当前用户与角色 |
| Schema | `erpnext_search_doctypes` | Data、Action | 在权限和白名单范围内发现 DocType |
| Schema | `erpnext_get_doctype_schema` | Data、Action | 获取当前用户可见字段与 Link/Table 关系 |
| 通用只读 | `erpnext_get_list` | Data、Patrol、Action | 分页查询单据列表 |
| 通用只读 | `erpnext_get_doc` | Data、Action | 查询一张单据及安全子表 |
| 通用只读 | `erpnext_get_count` | Data、Patrol | 只返回符合条件的数量 |
| 库存 | `erpnext_get_stock_balance` | Data、Patrol、Action | 查询指定物料和仓库的库存与估值 |
| 销售 | `erpnext_get_customer_summary` | Data、Action | 客户档案、信用额度、未结销售发票 |
| 采购 | `erpnext_get_supplier_summary` | Data、Action | 供应商档案、未结采购发票 |
| 财务 | `erpnext_get_receivables_summary` | Data、Patrol | 公司/客户维度应收摘要 |
| 写入 | `erpnext_create_draft` | Action | 创建白名单单据草稿 |
| 写入 | `erpnext_update_draft` | Action | 使用乐观锁修改白名单草稿 |

建议只给 Action Agent 暴露两个写工具；Data、Patrol 和 Orchestrator 不应具备写工具句柄。工具注解中的 `readOnlyHint`、`destructiveHint` 和 `idempotentHint` 是模型提示，不可替代 Agent 侧强制策略。

运行时以 `tools/list` 返回的 `inputSchema` 为准，不在 Agent 中复制一份长期不更新的 JSON Schema。可在认证用户会话启动时拉取并缓存，缓存键至少包含站点、用户和服务版本。

---

## 5. 基础与 Schema 工具

### 5.1 `erpnext_health`

参数：无。

返回数据：

```json
{
  "status": "ok",
  "service": "erpnext-mcp-tools",
  "site": "dev.localhost",
  "user": "user@example.com",
  "versions": {
    "frappe": "16.23.0",
    "erpnext_mcp_tools": "0.0.1"
  }
}
```

连接建立后调用一次即可。不要用它代替每个业务工具的权限检查。

### 5.2 `erpnext_get_current_user`

参数：无。

返回数据：

```json
{
  "user": "user@example.com",
  "roles": ["Accounts User", "Sales User"]
}
```

角色只用于 Agent UI 和路由提示，真正授权以每次工具的服务端权限检查为准。不要仅凭角色数组自行判定数据一定可见，因为 User Permission、共享和字段 permlevel 还会进一步收窄范围。

### 5.3 `erpnext_search_doctypes`

参数：

| 参数 | 类型 | 必填 | 默认值 | 约束 |
|---|---|---:|---:|---|
| `query` | string | 否 | `""` | 最长 100 字符；匹配 DocType 名称或翻译标签 |
| `limit` | integer | 否 | `20` | 1–50，布尔值不视为整数 |

返回：

```json
{
  "doctypes": [
    {
      "name": "Sales Order",
      "label": "销售订单",
      "module": "Selling",
      "is_submittable": true
    }
  ]
}
```

空 `query` 返回当前用户可读且已安装的白名单 DocType。该工具是名称发现，不是语义向量检索；Agent 可在上层维护“销售订单 → Sales Order”等业务词典，但最终必须用此工具或运行时 Schema 验证。

### 5.4 `erpnext_get_doctype_schema`

参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `doctype` | string | 是 | 必须是可读白名单中的精确 DocType 名 |

返回：

```json
{
  "name": "Sales Order",
  "module": "Selling",
  "title_field": "customer_name",
  "search_fields": ["customer", "customer_name"],
  "is_submittable": true,
  "fields": [
    {
      "fieldname": "customer",
      "label": "Customer",
      "fieldtype": "Link",
      "options": "Customer",
      "required": true,
      "read_only": false
    }
  ]
}
```

`fields` 已按当前用户字段权限、隐藏字段和敏感字段策略裁剪。`options` 只对 `Link`、`Table`、`Select` 有意义。Schema 不包含样例数据；需要样例时再调用 `erpnext_get_list`，不要在启动阶段读取大量业务记录。

---

## 6. 通用查询工具

### 6.1 可读 DocType 白名单

通用 Schema、列表、详情和计数只允许以下 DocType：

```text
Account                 Bin                     Company
Customer                Customer Group          GL Entry
Item                    Item Group              Material Request
Payment Entry           Purchase Invoice        Purchase Order
Sales Invoice           Sales Order             Stock Entry
Stock Ledger Entry      Supplier                Supplier Group
Warehouse
```

即使 DocType 在此列表中，当前用户没有 Frappe `read` 权限时仍会返回 `PERMISSION_DENIED`。

以下敏感 DocType 会优先返回 `DOCTYPE_BLOCKED`：`Access Log`、`Bank Account`、`Error Log`、`Has Role`、`Integration Request`、`OAuth Bearer Token`、`OAuth Client`、`Salary Slip`、`User`。

### 6.2 `erpnext_get_list`

参数：

| 参数 | 类型 | 必填 | 默认值 | 约束 |
|---|---|---:|---:|---|
| `doctype` | string | 是 | — | 可读白名单中的精确名称 |
| `fields` | array[string] \| null | 否 | 安全默认字段 | 1–40 个；不允许子表和敏感字段 |
| `filters` | object \| array \| null | 否 | `{}` | 仅允许安全字段和操作符 |
| `order_by` | string \| null | 否 | `modified desc` | 仅 `"<field> asc"` 或 `"<field> desc"` |
| `limit_start` | integer | 否 | `0` | 必须大于等于 0 |
| `limit_page_length` | integer | 否 | `20` | 1–100 |

未传 `fields` 时默认返回：

- `name`、`modified`；
- 当前用户可见时的标题字段；
- 可提交 DocType 的 `docstatus`。

过滤示例：

```json
{
  "doctype": "Sales Order",
  "fields": ["name", "customer", "transaction_date", "grand_total", "status"],
  "filters": {
    "docstatus": 1,
    "transaction_date": ["between", ["2026-08-01", "2026-08-31"]],
    "customer": ["in", ["CUST-0001", "CUST-0002"]]
  },
  "order_by": "transaction_date desc",
  "limit_start": 0,
  "limit_page_length": 20
}
```

也支持 Frappe 列表形式：

```json
{
  "filters": [
    ["Sales Order", "docstatus", "=", 1],
    ["Sales Order", "transaction_date", ">=", "2026-08-01"]
  ]
}
```

允许的操作符：

```text
=  !=  >  >=  <  <=  in  not in  like  not like  between  is
```

其他限制：过滤嵌套最多 4 层、总节点最多 80、单个列表最多 50 个值、字符串过滤值最长 500 字符。不要依赖任意 SQL 表达式或复杂 OR 语义；复杂分析应走上位系统中独立设计的只读分析通道。

返回：

```json
{
  "doctype": "Sales Order",
  "rows": [
    {
      "name": "SAL-ORD-2026-00001",
      "customer": "CUST-0001",
      "transaction_date": "2026-08-01",
      "grand_total": 1000.0,
      "status": "To Deliver and Bill"
    }
  ],
  "pagination": {
    "start": 0,
    "page_length": 20,
    "returned": 1
  }
}
```

`pagination` 不包含总数。只有在业务问题确实需要总数时才额外调用 `erpnext_get_count`。当 `returned == page_length` 时不能断定还有下一页；如需完整遍历，继续请求下一页直到返回条数小于页长，并设置 Agent 侧最大页数和总行数上限。

### 6.3 `erpnext_get_doc`

参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `doctype` | string | 是 | 可读白名单 DocType |
| `name` | string | 是 | 精确单据名，非显示标题；1–180 字符 |

返回当前用户可见的主表字段和安全子表字段，并始终包含 `name`、`doctype`、`modified`；可提交单据还包含 `docstatus`。

边界：

- 每个子表最多序列化 200 行；
- 子表递归最多 2 层；
- 单个文本最多 20,000 字符，超出部分会标记截断；
- 被截断的表会在 `_truncated_child_rows` 中说明；
- 敏感字段不会因为用户是 Administrator 就自动放开。

Action Agent 更新前必须重新调用本工具，保存精确 `modified` 作为乐观锁版本。

### 6.4 `erpnext_get_count`

参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `doctype` | string | 是 | 可读白名单 DocType |
| `filters` | object \| array \| null | 否 | 与 `erpnext_get_list` 相同的安全过滤器 |

返回：

```json
{"doctype": "Sales Order", "count": 12}
```

优先用它回答“有多少条”，不要为了计数翻页拉取全部记录。

---

## 7. 领域只读工具

领域工具已经固化了业务口径和额外权限检查。能满足问题时应优先于通用查询工具。

### 7.1 `erpnext_get_stock_balance`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `item_code` | string | 是 | 精确 Item 编码，最长 180 字符 |
| `warehouse` | string | 是 | 精确 Warehouse 名称，最长 180 字符 |
| `posting_date` | string \| null | 否 | `YYYY-MM-DD`；省略为当前日期 |

调用者必须同时具备 Item、Warehouse、Stock Ledger Entry 读取权限；仓库有公司时还需要 Company 读取权限。

返回字段：`item_code`、`warehouse`、`company`、`posting_date`、`actual_qty`、`stock_uom`、`valuation_rate`、`valuation_currency`、`stock_value`、`queried_at`。

不要仅凭名称猜测物料和仓库。先用 `erpnext_get_list` 找到精确值，再查余额。

### 7.2 `erpnext_get_customer_summary`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `customer` | string | 是 | 精确 Customer 名称 |
| `company` | string \| null | 否 | 可选公司范围 |

返回客户基本资料、当前用户可见的公司信用额度，以及 `docstatus=1`、`outstanding_amount != 0` 的销售发票摘要。明细最多 100 条，按到期日升序。

关键返回字段：

```text
customer, customer_name, customer_group, territory, default_currency
credit_limits, currency, outstanding_amount, open_invoice_count
outstanding_by_currency, open_invoices, detail_limit, scope, queried_at
```

### 7.3 `erpnext_get_supplier_summary`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `supplier` | string | 是 | 精确 Supplier 名称 |
| `company` | string \| null | 否 | 可选公司范围 |

返回供应商基本资料，以及 `docstatus=1`、`outstanding_amount != 0` 的采购发票摘要。明细最多 100 条。

关键返回字段：

```text
supplier, supplier_name, supplier_group, country, default_currency
currency, outstanding_amount, open_invoice_count, outstanding_by_currency
open_invoices, detail_limit, scope, queried_at
```

### 7.4 `erpnext_get_receivables_summary`

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---:|---:|---|
| `company` | string | 是 | — | 精确 Company 名称 |
| `customer` | string \| null | 否 | `null` | 可选客户范围 |
| `limit` | integer | 否 | `50` | 发票明细条数，1–100 |

返回公司范围内已提交且未结清的销售发票，应收金额按币种分组。

多币种处理是强制口径：

- 无未结项时，`outstanding_amount=0`、`currency=null`；
- 只有一个币种时，顶层 `currency` 和 `outstanding_amount` 有值；
- 多个币种时，两者均为 `null`，必须读取 `outstanding_by_currency`；
- 不允许 Agent 直接把不同币种金额相加。

---

## 8. 草稿写入工具

### 8.1 Agent 侧前置条件

MCP Server 会保证白名单、Frappe 权限、草稿状态、幂等、乐观锁和写审计，但不会判断用户是否已在聊天界面确认。

Action Agent 在每次写调用前必须：

1. 从 ERPNext 查询并确认 Link 字段使用的是精确 `name`；
2. 将 DocType、公司、往来单位、日期、币种和全部明细展示为结构化预览；
3. 经过用户确认或上位 HITL 规则；
4. 固定一枚“逻辑操作 ID”作为 `idempotency_key`；
5. 调用后把单据名、`docstatus=0`、`modified` 和 `trace_id` 返回给用户。

写工具只创建/修改草稿。Agent 绝不能声称单据已提交、库存已扣减/预占、总账已过账或应收应付已经生成。

### 8.2 幂等键规则

`idempotency_key` 必须是 8–128 位，仅允许：

```text
A-Z  a-z  0-9  .  _  :  -
```

幂等记录有效期为 24 小时，作用域是“ERPNext 用户 + 工具名 + key”。推荐格式：

```text
<agent-session-id>:<logical-action-id>
```

规则：

- 网络超时、连接断开或 `IDEMPOTENCY_IN_PROGRESS` 后，使用**相同参数和相同 key**重试；
- 相同 key、相同参数会返回第一次结果，`replayed=true`；
- 相同 key、不同参数会返回 `IDEMPOTENCY_CONFLICT`；
- 用户修改了草稿预览后，应先确认旧请求是否成功，再为新逻辑操作生成新 key；
- 不要按“每次 HTTP 尝试”生成新 key，否则超时重试可能创建重复单据。

### 8.3 写入白名单

#### Sales Order

创建必填：`customer`、`company`、`transaction_date`、`delivery_date`、`items`。

主表允许字段：

```text
customer, company, transaction_date, delivery_date, currency
selling_price_list, po_no, items
```

`Sales Order Item` 允许字段：

```text
item_code, item_name, description, qty, uom, conversion_factor
rate, delivery_date, warehouse
```

#### Purchase Order

创建必填：`supplier`、`company`、`transaction_date`、`schedule_date`、`items`。

主表允许字段：

```text
supplier, company, transaction_date, schedule_date, currency
buying_price_list, items
```

`Purchase Order Item` 允许字段：

```text
item_code, item_name, description, qty, uom, conversion_factor
rate, schedule_date, warehouse
```

#### Material Request

创建必填：`material_request_type`、`company`、`transaction_date`、`schedule_date`、`items`。

主表允许字段：

```text
material_request_type, company, transaction_date, schedule_date
set_warehouse, items
```

`Material Request Item` 允许字段：

```text
item_code, item_name, description, qty, uom, conversion_factor
schedule_date, warehouse
```

通用 payload 限制：主表 1–30 个字段，子表最多 100 行，单个字符串最多 5,000 字符。MCP 的字段白名单不等于 ERPNext 业务校验；日期、UOM、价格、必填 Link 和公司一致性等仍可能被 ERPNext 以 `VALIDATION_ERROR` 拒绝。

### 8.4 `erpnext_create_draft`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `doctype` | string | 是 | 三种可写 DocType 之一 |
| `payload` | object | 是 | 只包含对应白名单字段 |
| `idempotency_key` | string | 是 | 逻辑操作幂等键 |

Material Request 示例：

```json
{
  "jsonrpc": "2.0",
  "id": 20,
  "method": "tools/call",
  "params": {
    "name": "erpnext_create_draft",
    "arguments": {
      "doctype": "Material Request",
      "idempotency_key": "session-42:material-request-1",
      "payload": {
        "material_request_type": "Purchase",
        "company": "Example Company",
        "transaction_date": "2026-08-08",
        "schedule_date": "2026-08-09",
        "set_warehouse": "Stores - EX",
        "items": [
          {
            "item_code": "ITEM-0001",
            "qty": 1,
            "uom": "Nos",
            "conversion_factor": 1,
            "schedule_date": "2026-08-09",
            "warehouse": "Stores - EX"
          }
        ]
      }
    }
  }
}
```

返回 `data`：

```json
{
  "doctype": "Material Request",
  "name": "MAT-MR-2026-00001",
  "docstatus": 0,
  "modified": "2026-08-08 12:34:56.123456",
  "replayed": false
}
```

### 8.5 `erpnext_update_draft`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `doctype` | string | 是 | 三种可写 DocType 之一 |
| `name` | string | 是 | 精确草稿单据名 |
| `payload` | object | 是 | 需要修改的白名单字段 |
| `expected_modified` | string | 是 | 最近一次读取返回的精确 `modified` |
| `idempotency_key` | string | 是 | 本次逻辑更新的幂等键 |

推荐流程：

```text
get_doc → 保存 modified → 生成更新预览 → 用户/HITL 确认
       → update_draft(expected_modified=modified) → 再次 get_doc 验证
```

示例：

```json
{
  "jsonrpc": "2.0",
  "id": 21,
  "method": "tools/call",
  "params": {
    "name": "erpnext_update_draft",
    "arguments": {
      "doctype": "Material Request",
      "name": "MAT-MR-2026-00001",
      "payload": {"schedule_date": "2026-08-12"},
      "expected_modified": "2026-08-08 12:34:56.123456",
      "idempotency_key": "session-42:material-request-update-1"
    }
  }
}
```

只允许 `docstatus=0`。发生 `VERSION_CONFLICT` 时必须重新读取、重新生成差异和预览，不能替换时间戳后盲重试。

更新 `items` 时不要假设服务端执行“按行局部 Patch”。先重新读取单据，并以用户确认后的完整目标子表为准，避免遗漏行或覆盖并发修改。

---

## 9. 错误处理与重试策略

### 9.1 协议和身份错误

| 状态 | 含义 | Agent 行为 |
|---|---|---|
| HTTP 401/403 | HTTP 身份未被 Frappe 接受 | 不提升身份重试；刷新用户凭据或要求重新登录 |
| HTTP 405 | 使用了非 POST 请求 | 修复客户端配置 |
| JSON-RPC `-32700` | JSON 解析失败 | 修复序列化，不重试相同请求体 |
| JSON-RPC `-32600/-32602` | 请求或参数结构无效 | 修复 Adapter |
| JSON-RPC `-32601` | 方法不存在 | 重新发现能力或检查版本 |
| MCP `isError=true` 且无业务信封 | 未知工具或工具分发异常 | 刷新 `tools/list`；不要让模型猜工具名 |

### 9.2 业务错误码

| 错误码 | 是否自动重试 | Agent 处理 |
|---|---:|---|
| `AUTHENTICATION_REQUIRED` | 否 | 要求重新认证 |
| `PERMISSION_DENIED` | 否 | 告知用户缺少 ERPNext 权限，不切换高权限账号 |
| `DOCTYPE_BLOCKED` / `DOCTYPE_NOT_ALLOWED` | 否 | 能力边界；改用允许的业务流程 |
| `FIELD_NOT_ALLOWED` / `INVALID_FIELD` | 否 | 刷新 Schema，移除字段 |
| `INVALID_ARGUMENTS` | 否 | 按 `tools/list` 修复参数 |
| `INVALID_SEARCH` / `INVALID_FIELDS` / `INVALID_FILTERS` | 否 | 修复查询输入 |
| `INVALID_FILTER_OPERATOR` / `INVALID_ORDER_BY` | 否 | 使用允许语法 |
| `INVALID_PAGINATION` / `INVALID_DATE` / `INVALID_NAME` | 否 | 修复值与格式 |
| `NOT_FOUND` | 否 | 重新查找精确名称或告知用户不存在 |
| `MISSING_REQUIRED_FIELDS` / `INVALID_PAYLOAD` | 否 | 补充并重新预览 |
| `VALIDATION_ERROR` | 否 | ERPNext 业务校验失败；要求用户修正数据，详情用 `trace_id` 排查 |
| `NOT_A_DRAFT` / `INVALID_DOCSTATUS` | 否 | 当前 MCP 不支持该状态操作 |
| `VERSION_CONFLICT` | 否 | 重新读取、重新确认，禁止盲重试 |
| `IDEMPOTENCY_IN_PROGRESS` | 有条件 | 短暂退避后用相同参数和相同 key 重试 |
| `IDEMPOTENCY_CONFLICT` | 否 | 同一 key 被用于不同参数；先核对旧操作结果 |
| `IDEMPOTENCY_UNAVAILABLE` | 有条件 | 写操作未执行/已回滚；恢复幂等服务后用同一 key 重试 |
| `AUDIT_FAILED` | 有条件 | 写操作已回滚；恢复审计后用同一 key 重试 |
| `INTERNAL_ERROR` | 谨慎 | 记录 `trace_id`；读操作可有限退避，写操作先核对是否已有结果 |

所有重试都必须有最大次数、指数退避和全链路超时。`PERMISSION_DENIED`、Schema/参数错误和业务校验错误不属于瞬时故障。

---

## 10. Agent 工具选择流程

### 10.1 查询任务

```text
用户问题
  ├─ 已有专用领域工具？
  │    └─ 是：调用领域工具，采用其固化口径
  └─ 否
       ├─ DocType 不确定：search_doctypes
       ├─ 字段不确定：get_doctype_schema
       ├─ 只问数量：get_count
       ├─ 查记录集合：get_list
       └─ 查单据/子表：get_doc
```

推荐规则：

- 查询前先确认 Schema，但同一用户会话内可使用带版本的缓存；
- 金额、数量、状态结论必须来自工具结果，不由模型补全；
- 空 `rows`、空 `open_invoices` 或 `count=0` 就是合法结果；
- 不同币种只按 `outstanding_by_currency` 分别展示；
- 列表只取回答问题所需字段，避免把整张单据塞进上下文；
- 需要子表时才调用 `get_doc`。

### 10.2 写入任务

```text
识别写意图
  → 检查是否仅为三种草稿操作
  → 查询 Link 精确值与现有单据
  → 构造白名单 payload
  → Agent 侧业务规则/HITL
  → 展示结构化预览并确认
  → 固定 idempotency_key
  → create_draft / update_draft
  → get_doc 回读验证
  → 返回草稿状态、单据名和 trace_id
```

如果用户要求提交、作废、删除或过账，应明确说明当前 MCP 不支持，而不是把意图降级成另一种操作后静默执行。

---

## 11. 与历史方案/通用名称的映射

旧版方案、博客或第三方示例可能使用下列泛化工具名。开发当前 Agent 时必须使用右侧真实能力与边界：

| 方案中的概念/伪工具 | 当前实际能力 |
|---|---|
| `schema_inspect` | `erpnext_search_doctypes` + `erpnext_get_doctype_schema` |
| `frappe_get_list` | `erpnext_get_list` |
| `frappe_get_doc` | `erpnext_get_doc` |
| `frappe_create_doc` | `erpnext_create_draft`，仅三种白名单草稿 |
| `frappe_update_doc` | `erpnext_update_draft`，仅草稿且要求 `expected_modified` |
| `frappe_submit_doc` | 未实现、未注册，禁止规划为可用步骤 |
| `validate_business_rules` | MCP 无独立工具；一部分由 ERPNext 保存校验执行，其余需 Agent 侧实现 |
| “通过 MCP 验证用户名/密码” | 未实现；身份由 Frappe HTTP Session、API Token 或 OAuth 进入请求 |
| MCP 中的标准 CRUD/workflow | 当前只有受控只读和草稿 create/update |
| SQL 分析 | 不属于本 MCP endpoint，应单独实现和审计 |

尤其注意：当前写工具成功只代表草稿已保存，不代表“已提交”“库存已预占”“应收已生成”。

---

## 12. Agent Adapter 建议接口

### 12.1 AgentScope 2.0.5 的真实接入边界

本项目已用 Python 3.12 实际安装验证 agentscope==2.0.5。该版本使用：

~~~python
from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.mcp import HttpMCPConfig, MCPClient
from agentscope.tool import ToolBase, Toolkit
~~~

每用户 MCP 连接配置可写成：

~~~python
client = MCPClient(
    name=f"erpnext-{session_id}",
    is_stateful=False,
    mcp_config=HttpMCPConfig(
        url=settings.erpnext_mcp_url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20.0,
    ),
    enable_tools=allowed_tools,
    execution_timeout=25.0,
)
~~~

但是，生产代码不能直接使用 Toolkit(mcps=[client]) 注册当前 ERPNext 工具。经检查 2.0.5 的 MCPTool.call，它只把 MCP content 和 isError 转成 AgentScope ToolChunk，不会传播 structuredContent。当前服务的受控业务错误通常是 isError=false、structuredContent.ok=false，直接注册会丢失关键错误语义。

应实现自定义 ERPNextMCPTool（继承 ToolBase）：

1. 从运行时 tools/list 读取名称、描述、inputSchema 和 annotations；
2. 通过下面的 Adapter 调用 MCP 并保留原始 CallToolResult；
3. 强制解析 structuredContent.ok/data/error/meta；
4. 成功时只把 data 和必要的脱敏 meta 交给模型；
5. 失败时产生稳定领域错误，保留 trace_id；
6. 将 allowlist 内的自定义 Tool 放入 Toolkit(tools=[...])；
7. Data/Patrol/Orchestrator 的 Toolkit 从物理上不含写工具。

必须添加一个契约测试：模拟 isError=false 且 structuredContent.ok=false，断言 Agent 不会把它当成功结果。

### 12.2 Adapter 接口

将 MCP 协议、身份注入、响应归一化和重试集中在一个 Adapter 中：

```python
class ERPNextMCPClient:
    async def initialize(self) -> ServerInfo: ...
    async def list_tools(self, *, refresh: bool = False) -> list[ToolSpec]: ...
    async def call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        user_context: UserCredentialRef,
        timeout_s: float,
    ) -> ToolOutcome: ...


class ToolOutcome:
    data: dict
    trace_id: str
    tool: str
    user: str
    content_trust: str
```

职责边界：

- Transport：POST、认证头、连接池、超时；
- MCP Adapter：initialize、工具发现、JSON-RPC ID、四层错误归一化；
- Policy Middleware：Agent 可见工具集合、读写分离、HITL；
- Schema Registry：按站点/用户/版本缓存 `tools/list` 和 DocType Schema；
- Agent：意图理解、参数收集、工具选择、结果表达；
- ERPNext MCP Server：最终权限、数据策略、ORM 操作、审计、幂等和回滚。

不要把 API Key/Secret 放入 `ToolOutcome` 或 Agent state。会话只保存 Secret 引用或后端加密凭据句柄。

---

## 13. 可观测与排错

每次工具调用将记录：

```text
trace_id, timestamp, user, ip, tool, target, params_sha256
risk, ok, error_code, duration_ms
```

参数只记录 SHA-256 摘要，目标字段会限制长度。Agent 侧建议同时记录：

- `agent_session_id`、用户消息 ID、模型调用 ID；
- JSON-RPC request ID、工具名、耗时；
- MCP `trace_id`、错误码、重试次数；
- 参数摘要，不记录认证信息和完整敏感业务值。

常用排查位置：

```text
sites/<site>/logs/erpnext_mcp_tools.log
Frappe Error Log
```

`INTERNAL_ERROR`、`VALIDATION_ERROR` 等对模型隐藏内部细节，使用 `trace_id` 关联服务端日志。

开发环境验证：

```bash
cd /workspace/development/frappe-bench
./env/bin/frappe-mcp check --app erpnext_mcp_tools --verbose
bench --site dev.localhost run-tests --app erpnext_mcp_tools
```

Inspector：

```bash
npx @modelcontextprotocol/inspector --cli \
  http://dev.localhost:8000/api/method/erpnext_mcp_tools.mcp.handle_mcp \
  --transport http \
  --method tools/list \
  --header 'Authorization: token <API_KEY>:<API_SECRET>'
```

---

## 14. Agent 接入验收清单

### 连接与契约

- [ ] 未认证请求被拒绝，认证后可完成 initialize、initialized、tools/list。
- [ ] 客户端使用服务端返回的协议版本和能力。
- [ ] 工具 Schema 来自 `tools/list`，不是手写过期副本。
- [ ] Adapter 同时处理 HTTP、JSON-RPC、`isError` 和 `structuredContent.ok`。
- [ ] `content` 和 `structuredContent` 不会重复注入模型上下文。

### 身份与安全

- [ ] 登录采用 Authorization Code + PKCE，Agent 不接收 ERPNext 密码。
- [ ] callback 校验 state，token 刷新使用并发锁，登出执行 revoke。
- [ ] AgentScope 适配层保留并校验 structuredContent，不直接依赖 2.0.5 MCPTool.call。

- [ ] 每个 Agent 会话绑定最终用户身份，不共用 Administrator。
- [ ] 凭据从不进入 Prompt、工具参数、Trace 或测试快照。
- [ ] `content_trust=untrusted_business_data` 传播到模型策略。
- [ ] `PERMISSION_DENIED` 不通过切换高权限身份重试。
- [ ] Data/Patrol/Orchestrator 不持有写工具。

### 查询

- [ ] DocType 和字段不确定时先发现 Schema。
- [ ] 分页有页数、总行数和超时上限。
- [ ] 数量问题优先 `get_count`，子表问题才用 `get_doc`。
- [ ] 多币种金额不会直接相加。
- [ ] 空结果不被模型补造。

### 写入

- [ ] 只支持三种白名单草稿，明确拒绝 submit/cancel/delete。
- [ ] 调用前展示结构化预览并完成用户确认/HITL。
- [ ] 每个逻辑操作只生成一个幂等键，重试复用相同 key 和参数。
- [ ] 更新前重新读取并携带精确 `modified`。
- [ ] `VERSION_CONFLICT` 后重新确认，不盲重试。
- [ ] 调用后回读验证，并明确向用户说明 `docstatus=0`。

---

## 15. 实现来源

本文契约来自以下当前代码：

```text
apps/erpnext_mcp_tools/erpnext_mcp_tools/mcp.py
apps/erpnext_mcp_tools/erpnext_mcp_tools/tools/
apps/erpnext_mcp_tools/erpnext_mcp_tools/security/
apps/erpnext_mcp_tools/erpnext_mcp_tools/services/
apps/erpnext_mcp_tools/erpnext_mcp_tools/tests/
```

当工具实现变更时，应同步更新测试、App README 和本文。Agent 运行时仍以 `initialize`、`tools/list` 和实际结构化响应作为最终事实源。
