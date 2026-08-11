# Agent 前端 Markdown 预览渲染优化记录

> 日期：2026-08-11
> 状态：实现完成，已通过 Markdown 解析、DOM 构造、安全、静态资源和容器运行验证

## 一、问题

库存 Agent 回复已经包含 Markdown 粗体、列表和表格，但前端的 `renderMessage()` 统一使用：

```javascript
bubble.textContent = message.content;
```

这会把 `**物料信息**`、`- 列表` 和 `| 表格 |` 完整当作普通字符显示，因此截图中看到的是
Markdown 原文，而不是预览结果。

## 二、实现方案

### 2.1 本地安全 Markdown 渲染器

新增 `src/erpnext_agent/web/markdown.js`，不引入 CDN 或额外 npm 运行时依赖。当前支持：

- 段落与显式换行；
- 一至六级标题；
- 粗体、斜体和删除线；
- 行内代码和 fenced code block；
- 有序/无序列表；
- 引用与分隔线；
- GFM 风格 pipe table 和列对齐；
- HTTP(S)、`mailto:` 和站内相对链接。

为兼容模型常见的 `**物料信息： **` 写法，粗体内容在解析时会容忍关闭标记前的空格。

### 2.2 消息渲染路径

- 助手消息调用 `SafeMarkdown.renderMarkdown()`；
- 用户消息仍使用 `textContent`，不会把用户输入解析为富文本；
- 正在生成的动画也改为 `createElement()`，移除 `bubble.innerHTML`；
- SSE `text_delta` 和历史会话恢复都复用 `renderMessage()`，无需修改已保存的 PostgreSQL 消息。

因此用户刷新页面或重新打开旧会话时，之前保存的 Markdown 源文本也会自动显示为预览格式。

### 2.3 样式

- 标题、段落和列表使用紧凑的对话间距；
- 行内代码使用浅色底，代码块使用深色底和横向滚动；
- 表格包含表头背景、单元格分隔线和对齐规则；
- 表格外层独立横向滚动，在移动端不撑破消息气泡。

## 三、安全边界

本渲染器不支持原始 HTML。模型输出的 `<script>`、`<img onerror>` 等内容只会作为文本节点显示。
所有文本都通过 `document.createTextNode()` 写入，所有标签都由固定映射的
`document.createElement()` 创建，不使用模型输出拼接 HTML。

链接在创建 `<a>` 前检查协议：

- 允许 `http:`、`https:`、`mailto:` 和站内相对路径；
- 拒绝 `javascript:`、`data:`、控制字符和无法解析的目标；
- 外部链接使用 `target=_blank` 和 `rel=noopener noreferrer`。

## 四、验证

### 4.1 截图库存回复样例

Node 测试使用了与截图同类的库存回复，包含容忍空格的粗体、列表、行内代码和四列表格。
解析及 DOM 构造结果确认生成：

```text
STRONG, UL, CODE, TABLE, THEAD, TBODY
```

不再把 `**`、`-` 和 pipe table 源码直接显示给用户。

### 4.2 恶意输入

以下内容已纳入测试：

```text
<img src=x onerror=alert(1)>
[点击](javascript:alert(1))
```

构造后 DOM 不包含 `IMG` 或 `A`，原始 HTML 保留为文本。

### 4.3 自动化与运行检查

```text
JavaScript syntax: passed (markdown.js, app.js)
Markdown parser/DOM/security: passed
Ruff: passed
Mypy strict: Success, 57 source files
Pytest: 41 passed
Docker Compose config: passed
Docker image build: passed
Agent/PostgreSQL/Redis: healthy
/health/ready: redis=ok, database=ok
model_configured: true
agent_chat_runtime: true
```

运行中根页面已确认按以下顺序加载本地资源：

```text
/assets/styles.css
/assets/markdown.js
/assets/app.js
```

## 五、当前边界

- 这是面向 Agent 对话的安全 Markdown 子集，不执行原始 HTML；
- 当前不支持图片 Markdown、脚注、复杂嵌套列表和语法高亮；
- 表格列较多时使用气泡内横向滚动，不强制压缩为不可读宽度。

## 六、主要文件

- `src/erpnext_agent/web/markdown.js`
- `src/erpnext_agent/web/app.js`
- `src/erpnext_agent/web/styles.css`
- `src/erpnext_agent/web/index.html`
- `tests/frontend/test_markdown_renderer.js`
- `tests/unit/test_chat_ui.py`
- `docs/ERPNext-Agent-开发进度.md`
