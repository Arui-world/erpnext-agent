"use strict";

const API_PREFIX = "/api/v1";
const SESSION_RECHECK_MS = 5_000;
const authChannel =
  typeof BroadcastChannel === "function"
    ? new BroadcastChannel("erpnext-agent-auth")
    : null;

const MODES = {
  model: {
    endpoint: `${API_PREFIX}/chat/model/stream`,
    hint: "仅测试大模型，不调用 ERPNext 工具",
    placeholder: "给模型发送一条消息…",
    welcome:
      "你好，我是模型连接测试助手。这里不会调用 ERPNext 工具，你可以直接测试千问的回复和多轮上下文。",
    suggestions: ["请用一句话介绍你自己", "回复：模型连接正常", "解释什么是 Agent"],
  },
  agent: {
    endpoint: `${API_PREFIX}/chat/stream`,
    hint: "使用当前 ERPNext 用户权限；草稿写入必须预览并批准",
    placeholder: "查询业务数据，或创建/修改 ERPNext 草稿…",
    welcome:
      "已切换到 ERPNext Agent。查询与巡检使用当前登录用户权限；创建或修改草稿会先生成持久化预览，只有你批准后才会执行。",
    suggestions: [
      "查询最近的销售订单",
      "查看当前库存",
      "巡检逾期应收",
      "创建物料需求草稿",
    ],
  },
};

const state = {
  mode: "model",
  csrfToken: null,
  user: null,
  site: null,
  busy: false,
  messages: [],
  conversationIds: { model: null, agent: null },
  conversations: { model: [], agent: [] },
  actionPollTimer: null,
};

const elements = {
  appShell: document.querySelector(".app-shell"),
  conversation: document.querySelector("#conversation"),
  conversationInner: document.querySelector("#conversation-inner"),
  template: document.querySelector("#message-template"),
  composer: document.querySelector("#composer"),
  input: document.querySelector("#message-input"),
  send: document.querySelector("#send-button"),
  suggestions: document.querySelector("#suggestions"),
  modeHint: document.querySelector("#mode-hint"),
  modeButtons: [...document.querySelectorAll(".mode-button")],
  connectionDot: document.querySelector("#connection-dot"),
  connectionLabel: document.querySelector("#connection-label"),
  login: document.querySelector("#login-button"),
  logout: document.querySelector("#logout-button"),
  newConversationButtons: [
    document.querySelector("#new-conversation-button"),
    document.querySelector("#panel-new-conversation-button"),
  ],
  historyToggle: document.querySelector("#history-toggle-button"),
  historyBackdrop: document.querySelector("#history-backdrop"),
  historyStatus: document.querySelector("#history-status"),
  conversationList: document.querySelector("#conversation-list"),
};

function isNarrowScreen() {
  return window.matchMedia("(max-width: 820px)").matches;
}

function setHistoryPanel(open) {
  elements.appShell.classList.toggle("history-collapsed", !open);
  elements.historyToggle.classList.toggle("active", open);
  elements.historyToggle.setAttribute("aria-expanded", String(open));
}

function setConnection(kind, label) {
  elements.connectionDot.className = `connection-dot ${kind}`;
  elements.connectionLabel.textContent = label;
}

function setAuthenticated(session) {
  state.csrfToken = session.csrf_token;
  state.user = session.user;
  state.site = session.site;
  elements.login.classList.add("hidden");
  elements.logout.classList.remove("hidden");
  setConnection("connected", `${session.user} · ${session.site}`);
  syncControls();
}

function setUnauthenticated(label = "需要登录 ERPNext") {
  clearActionPolling();
  state.csrfToken = null;
  state.user = null;
  state.site = null;
  state.conversationIds = { model: null, agent: null };
  state.conversations = { model: [], agent: [] };
  elements.login.classList.remove("hidden");
  elements.logout.classList.add("hidden");
  setConnection("disconnected", label);
  renderConversationList();
  syncControls();
}

function handleSessionEnded(
  message = "ERPNext 已退出或账号发生变化，请重新授权",
  notifyOtherTabs = true,
) {
  state.busy = false;
  setUnauthenticated(message);
  resetConversation();
  if (notifyOtherTabs) {
    authChannel?.postMessage({ type: "erpnext-session-ended", message });
  }
}

function syncControls() {
  const enabled = Boolean(state.csrfToken) && !state.busy;
  elements.input.disabled = !enabled;
  elements.input.placeholder = state.csrfToken
    ? MODES[state.mode].placeholder
    : "登录 ERPNext 后即可开始对话";
  elements.send.disabled = !enabled || !elements.input.value.trim();
  for (const button of elements.suggestions.querySelectorAll("button")) {
    button.disabled = !enabled;
  }
  for (const button of elements.modeButtons) {
    button.disabled = state.busy;
  }
  for (const button of elements.newConversationButtons) {
    button.disabled = !enabled;
  }
  for (const button of elements.conversationList.querySelectorAll("button")) {
    button.disabled = state.busy;
  }
}

function createMessage(role, content, options = {}) {
  const message = {
    id: crypto.randomUUID(),
    role,
    content,
    persisted: options.persisted ?? true,
    pending: options.pending ?? false,
    toolState: "",
    action: options.action ?? null,
    actionBusy: false,
    node: null,
  };
  state.messages.push(message);
  renderMessage(message);
  scrollToBottom();
  return message;
}

function renderMessage(message) {
  if (!message.node) {
    const fragment = elements.template.content.cloneNode(true);
    message.node = fragment.querySelector(".message-row");
    message.node.dataset.messageId = message.id;
    message.node.classList.toggle("user", message.role === "user");
    message.node.querySelector(".message-meta").textContent =
      message.role === "assistant" ? "ERPNext Agent" : state.user || "你";
    elements.conversationInner.appendChild(fragment);
    message.node = elements.conversationInner.querySelector(
      `[data-message-id="${message.id}"]`,
    );
  }

  const bubble = message.node.querySelector(".bubble");
  bubble.classList.toggle("pending", message.pending && Boolean(message.content));
  bubble.classList.toggle("markdown-body", message.role === "assistant");
  if (message.pending && !message.content) {
    const typing = document.createElement("span");
    typing.className = "typing-dots";
    typing.setAttribute("aria-label", "正在生成");
    typing.append(
      document.createElement("span"),
      document.createElement("span"),
      document.createElement("span"),
    );
    bubble.replaceChildren(typing);
  } else if (message.role === "assistant") {
    window.SafeMarkdown.renderMarkdown(bubble, message.content);
  } else {
    bubble.textContent = message.content;
  }

  const toolState = message.node.querySelector(".tool-state");
  toolState.textContent = message.toolState;
  toolState.classList.toggle("hidden", !message.toolState);
  renderActionCard(message);
}

function actionStatusLabel(status) {
  return {
    PENDING: "等待批准",
    APPROVED: "已批准，等待执行",
    EXECUTING: "执行状态待核对",
    SUCCEEDED: "草稿已保存",
    FAILED: "执行失败",
    REJECTED: "已拒绝",
    EXPIRED: "已过期",
  }[status] || status || "未知状态";
}

function actionButton(label, kind, handler, disabled = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `action-button ${kind}`;
  button.textContent = label;
  button.disabled = disabled;
  button.addEventListener("click", handler);
  return button;
}

function renderActionCard(message) {
  const card = message.node.querySelector(".action-card");
  card.replaceChildren();
  card.classList.toggle("hidden", !message.action);
  if (!message.action) {
    return;
  }

  const action = message.action;
  const preview = action.preview || {};
  const header = document.createElement("div");
  header.className = "action-card-header";
  const title = document.createElement("strong");
  title.textContent = preview.title || "ERPNext 草稿审批";
  const status = document.createElement("span");
  status.className = `action-status status-${String(action.status || "").toLowerCase()}`;
  status.textContent = actionStatusLabel(action.status);
  header.append(title, status);

  const identity = document.createElement("code");
  identity.className = "action-id";
  identity.textContent = action.action_id;

  const details = document.createElement("pre");
  details.className = "action-preview";
  details.textContent = JSON.stringify(
    {
      doctype: preview.doctype,
      name: preview.document_name,
      fields: preview.fields || {},
      items: preview.items || [],
    },
    null,
    2,
  );

  card.append(header, identity, details);
  if (action.result_reference) {
    const result = document.createElement("p");
    result.className = "action-result";
    result.textContent = `已保存草稿 ${action.result_reference.doctype} ${action.result_reference.name}（docstatus=0）`;
    card.append(result);
  } else if (action.failure_code) {
    const failure = document.createElement("p");
    failure.className = "action-failure";
    failure.textContent = `${action.failure_code}：${action.failure_message || "操作失败"}`;
    card.append(failure);
  }

  const controls = document.createElement("div");
  controls.className = "action-controls";
  if (action.status === "PENDING") {
    controls.append(
      actionButton(
        "批准并执行",
        "approve",
        () => void approveAndExecuteAction(message),
        message.actionBusy,
      ),
      actionButton(
        "拒绝",
        "reject",
        () => void decideAction(message, "reject"),
        message.actionBusy,
      ),
    );
  } else if (action.status === "APPROVED") {
    controls.append(
      actionButton(
        "继续执行",
        "approve",
        () => void executeAction(message),
        message.actionBusy,
      ),
    );
  } else if (action.status === "EXECUTING") {
    controls.append(
      actionButton(
        "使用原幂等键重新核对",
        "approve",
        () => void executeAction(message),
        message.actionBusy,
      ),
    );
  }
  if (controls.childNodes.length) {
    card.append(controls);
  }
}

async function actionRequest(path, options = {}) {
  const response = await fetch(`${API_PREFIX}${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": state.csrfToken,
    },
    ...options,
  });
  if (response.status === 401 || response.status === 403) {
    handleSessionEnded();
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail = payload?.detail;
    throw new Error(detail?.message || detail || `Action 请求失败（${response.status}）`);
  }
  return response.json();
}

async function decideAction(message, decision) {
  if (!message.action || message.actionBusy) {
    return null;
  }
  message.actionBusy = true;
  renderMessage(message);
  try {
    const action = await actionRequest(
      `/approvals/${encodeURIComponent(message.action.action_id)}/decision`,
      { body: JSON.stringify({ decision }) },
    );
    updateMessage(message, { action, actionBusy: false });
    return action;
  } catch (error) {
    const detail = error instanceof Error ? error.message : "审批失败";
    updateMessage(message, { actionBusy: false, toolState: detail });
    return null;
  }
}

async function executeAction(message) {
  if (!message.action || message.actionBusy) {
    return null;
  }
  message.actionBusy = true;
  updateMessage(message, { toolState: "正在保存 ERPNext 草稿" });
  try {
    const action = await actionRequest(
      `/approvals/${encodeURIComponent(message.action.action_id)}/execute`,
    );
    updateMessage(message, { action, actionBusy: false, toolState: "" });
    return action;
  } catch (error) {
    const detail = error instanceof Error ? error.message : "草稿执行失败";
    updateMessage(message, { actionBusy: false, toolState: detail });
    void refreshConversationActions(state.conversationIds.agent);
    return null;
  }
}

async function approveAndExecuteAction(message) {
  const approved = await decideAction(message, "approve");
  if (approved?.status === "APPROVED") {
    await executeAction(message);
  }
}

function updateMessage(message, patch) {
  Object.assign(message, patch);
  renderMessage(message);
  scrollToBottom();
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    elements.conversation.scrollTop = elements.conversation.scrollHeight;
  });
}

function restoredActionMessage(action) {
  return `已从持久化记录恢复 Action ${action.action_id}，当前状态：${actionStatusLabel(action.status)}。`;
}

function showConversationMessages(messages = [], actions = []) {
  clearActionPolling();
  state.messages = [];
  elements.conversationInner.replaceChildren();
  const restored = window.ActionRestore.attachActionsToMessages(messages, actions);
  if (!messages.length) {
    createMessage("assistant", MODES[state.mode].welcome, { persisted: false });
  } else {
    for (const message of restored.messages) {
      createMessage(message.role, message.content, { action: message.action });
    }
  }
  for (const action of restored.unmatchedActions) {
    createMessage("assistant", restoredActionMessage(action), {
      action,
      persisted: false,
    });
  }
  scheduleActionPolling(state.conversationIds.agent, actions);
}

function resetConversation() {
  clearActionPolling();
  showConversationMessages();
  renderSuggestions();
  elements.modeHint.textContent = MODES[state.mode].hint;
  elements.input.value = "";
  resizeInput();
  syncControls();
}

function renderSuggestions() {
  elements.suggestions.replaceChildren();
  for (const text of MODES[state.mode].suggestions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "suggestion";
    button.textContent = text;
    button.addEventListener("click", () => {
      elements.input.value = text;
      resizeInput();
      syncControls();
      elements.input.focus();
    });
    elements.suggestions.appendChild(button);
  }
  syncControls();
}

function formatConversationTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
  }).format(date);
}

function renderConversationList() {
  elements.conversationList.replaceChildren();
  if (!state.csrfToken) {
    elements.historyStatus.textContent = "登录后显示历史对话";
    syncControls();
    return;
  }

  const conversations = state.conversations[state.mode];
  elements.historyStatus.textContent = conversations.length
    ? `${conversations.length} 个${state.mode === "model" ? "模型" : "Agent"}对话`
    : "还没有历史，点击“新对话”开始";
  const activeId = state.conversationIds[state.mode];
  for (const conversation of conversations) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "conversation-item";
    button.classList.toggle("active", conversation.conversation_id === activeId);
    button.setAttribute(
      "aria-current",
      conversation.conversation_id === activeId ? "page" : "false",
    );

    const title = document.createElement("span");
    title.className = "conversation-item-title";
    title.textContent = conversation.title || "新对话";
    const meta = document.createElement("span");
    meta.className = "conversation-item-meta";
    const count = document.createElement("span");
    count.textContent = `${conversation.message_count} 条消息`;
    const updated = document.createElement("span");
    updated.textContent = formatConversationTime(conversation.updated_at);
    meta.append(count, updated);
    button.append(title, meta);
    button.addEventListener("click", () => {
      if (state.busy) {
        return;
      }
      if (isNarrowScreen()) {
        setHistoryPanel(false);
      }
      void loadWorkspace(state.mode, conversation.conversation_id);
    });
    elements.conversationList.appendChild(button);
  }
  syncControls();
}

function resizeInput() {
  elements.input.style.height = "auto";
  elements.input.style.height = `${Math.min(elements.input.scrollHeight, 150)}px`;
}

function parseEventFrame(frame) {
  let eventName = "message";
  const dataLines = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (!dataLines.length) {
    return null;
  }
  return { event: eventName, data: JSON.parse(dataLines.join("\n")) };
}

async function consumeEventStream(response, onEvent) {
  if (!response.body) {
    throw new Error("浏览器无法读取流式响应");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    buffer = buffer.replaceAll("\r\n", "\n");

    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      if (frame.trim()) {
        const parsed = parseEventFrame(frame);
        if (parsed) {
          onEvent(parsed.event, parsed.data);
        }
      }
      boundary = buffer.indexOf("\n\n");
    }
    if (done) {
      break;
    }
  }
}

function handleStreamEvent(message, event, data, mode) {
  switch (event) {
    case "conversation":
      state.conversationIds[mode] = data.conversation_id || null;
      renderConversationList();
      break;
    case "text_delta":
      updateMessage(message, {
        content: message.content + (data.delta || ""),
        pending: true,
      });
      break;
    case "message":
      updateMessage(message, { content: data.message || "", pending: false });
      break;
    case "tool_call_start":
      updateMessage(message, {
        toolState: `正在调用 ${data.tool_name || "ERPNext 工具"}`,
      });
      break;
    case "tool_result_start":
      updateMessage(message, { toolState: "正在读取工具结果" });
      break;
    case "tool_result_end":
      updateMessage(message, { toolState: "工具调用完成" });
      break;
    case "action_required":
      updateMessage(message, { action: data, toolState: "等待用户批准草稿操作" });
      break;
    case "error":
      throw new Error(data.message || "模型回复失败");
    case "done":
      updateMessage(message, { pending: false });
      break;
    default:
      break;
  }
}

async function fetchConversationList(mode) {
  const response = await fetch(
    `${API_PREFIX}/chat/conversations?${new URLSearchParams({ mode })}`,
    {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    },
  );
  if (response.status === 401 || response.status === 403) {
    handleSessionEnded();
    throw new Error("登录状态已失效，请重新登录 ERPNext");
  }
  if (!response.ok) {
    throw new Error(`无法加载对话列表（${response.status}）`);
  }
  return (await response.json()).conversations;
}

async function fetchConversationActions(conversationId) {
  const query = new URLSearchParams({ conversation_id: conversationId });
  const response = await fetch(`${API_PREFIX}/approvals?${query}`, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  if (response.status === 401 || response.status === 403) {
    handleSessionEnded();
    throw new Error("登录状态已失效，请重新登录 ERPNext");
  }
  if (!response.ok) {
    throw new Error(`无法加载草稿审批状态（${response.status}）`);
  }
  return (await response.json()).actions;
}

function clearActionPolling() {
  if (state.actionPollTimer !== null) {
    window.clearTimeout(state.actionPollTimer);
    state.actionPollTimer = null;
  }
}

function scheduleActionPolling(conversationId, actions = []) {
  clearActionPolling();
  if (
    !conversationId ||
    state.mode !== "agent" ||
    !state.csrfToken ||
    !actions.some((action) => action.status === "EXECUTING")
  ) {
    return;
  }
  state.actionPollTimer = window.setTimeout(() => {
    state.actionPollTimer = null;
    void refreshConversationActions(conversationId);
  }, 5000);
}

function applyConversationActions(conversationId, actions) {
  if (
    state.mode !== "agent" ||
    state.conversationIds.agent !== conversationId
  ) {
    return;
  }
  const remaining = new Map(
    actions.map((action) => [action.action_id, action]),
  );
  for (const message of state.messages) {
    const currentActionId = message.action?.action_id;
    if (currentActionId && remaining.has(currentActionId)) {
      updateMessage(message, { action: remaining.get(currentActionId) });
      remaining.delete(currentActionId);
      continue;
    }
    if (message.role !== "assistant" || message.action) {
      continue;
    }
    for (const [actionId, action] of remaining) {
      if (message.content.includes(actionId)) {
        updateMessage(message, { action });
        remaining.delete(actionId);
        break;
      }
    }
  }
  for (const action of remaining.values()) {
    createMessage("assistant", restoredActionMessage(action), {
      action,
      persisted: false,
    });
  }
  scheduleActionPolling(conversationId, actions);
}

async function refreshConversationActions(conversationId) {
  if (!conversationId || state.mode !== "agent" || !state.csrfToken) {
    return;
  }
  try {
    const actions = await fetchConversationActions(conversationId);
    applyConversationActions(conversationId, actions);
  } catch {
    const currentActions = state.messages
      .map((message) => message.action)
      .filter(Boolean);
    scheduleActionPolling(conversationId, currentActions);
  }
}

async function refreshConversationList(mode) {
  if (!state.csrfToken) {
    return;
  }
  const conversations = await fetchConversationList(mode);
  state.conversations[mode] = conversations;
  if (!state.conversationIds[mode] && conversations.length) {
    state.conversationIds[mode] = conversations[0].conversation_id;
  }
  if (state.mode === mode) {
    renderConversationList();
  }
}

async function loadWorkspace(mode, preferredConversationId = null) {
  if (!state.csrfToken) {
    return;
  }
  state.busy = true;
  elements.historyStatus.textContent = "正在加载对话…";
  resetConversation();
  syncControls();
  try {
    const conversations = await fetchConversationList(mode);
    state.conversations[mode] = conversations;
    if (state.mode !== mode) {
      return;
    }
    const requestedId = preferredConversationId || state.conversationIds[mode];
    const requestedExists = conversations.some(
      (conversation) => conversation.conversation_id === requestedId,
    );
    const activeId = requestedExists
      ? requestedId
      : conversations[0]?.conversation_id || null;
    state.conversationIds[mode] = activeId;
    renderConversationList();
    if (!activeId) {
      showConversationMessages();
      return;
    }

    const query = new URLSearchParams({ mode, conversation_id: activeId });
    const response = await fetch(`${API_PREFIX}/chat/history?${query}`, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (response.status === 401 || response.status === 403) {
      handleSessionEnded();
      return;
    }
    if (!response.ok) {
      throw new Error(`无法加载对话历史（${response.status}）`);
    }
    const history = await response.json();
    let actions = [];
    let actionLoadError = null;
    if (mode === "agent") {
      try {
        actions = await fetchConversationActions(activeId);
      } catch (error) {
        actionLoadError = error;
      }
    }
    if (state.mode === mode && state.conversationIds[mode] === activeId) {
      showConversationMessages(history.messages, actions);
      if (actionLoadError && state.csrfToken) {
        const detail =
          actionLoadError instanceof Error
            ? actionLoadError.message
            : "无法加载草稿审批状态";
        createMessage("assistant", `对话历史已恢复，但${detail}。`, {
          persisted: false,
        });
      }
    }
  } catch (error) {
    if (state.mode === mode && state.csrfToken) {
      const message = error instanceof Error ? error.message : "无法加载对话历史";
      showConversationMessages([
        { role: "assistant", content: `抱歉，${message}` },
      ]);
    }
  } finally {
    state.busy = false;
    syncControls();
  }
}

async function createNewConversation() {
  if (!state.csrfToken || state.busy) {
    return;
  }
  const mode = state.mode;
  state.busy = true;
  syncControls();
  try {
    const response = await fetch(`${API_PREFIX}/chat/conversations`, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.csrfToken,
      },
      body: JSON.stringify({ mode }),
    });
    if (response.status === 401 || response.status === 403) {
      handleSessionEnded();
      throw new Error("登录状态已失效，请重新登录 ERPNext");
    }
    if (!response.ok) {
      throw new Error(`无法创建新对话（${response.status}）`);
    }
    const conversation = await response.json();
    state.conversationIds[mode] = conversation.conversation_id;
    state.conversations[mode] = [
      conversation,
      ...state.conversations[mode].filter(
        (item) => item.conversation_id !== conversation.conversation_id,
      ),
    ];
    resetConversation();
    renderConversationList();
    if (isNarrowScreen()) {
      setHistoryPanel(false);
    }
    elements.input.focus();
  } catch (error) {
    const message = error instanceof Error ? error.message : "无法创建新对话";
    showConversationMessages([
      { role: "assistant", content: `抱歉，${message}` },
    ]);
  } finally {
    state.busy = false;
    syncControls();
  }
}

async function sendMessage(rawMessage) {
  const content = rawMessage.trim();
  if (!content || state.busy || !state.csrfToken) {
    return;
  }

  const mode = state.mode;
  createMessage("user", content);
  const assistant = createMessage("assistant", "", { pending: true });
  state.busy = true;
  elements.input.value = "";
  resizeInput();
  syncControls();

  const body = {
    message: content,
    conversation_id: state.conversationIds[mode],
  };

  try {
    const response = await fetch(MODES[mode].endpoint, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.csrfToken,
      },
      body: JSON.stringify(body),
    });

    if (response.status === 401 || response.status === 403) {
      handleSessionEnded();
      throw new Error("登录状态已失效，请重新登录 ERPNext");
    }
    if (!response.ok) {
      const detail = await response.json().catch(() => null);
      throw new Error(detail?.detail?.message || detail?.detail || `请求失败（${response.status}）`);
    }

    await consumeEventStream(response, (event, data) => {
      handleStreamEvent(assistant, event, data, mode);
    });

    if (!assistant.content) {
      throw new Error("模型没有返回文本内容");
    }
    updateMessage(assistant, { pending: false });
  } catch (error) {
    const message = error instanceof Error ? error.message : "模型回复失败";
    updateMessage(assistant, {
      content: `抱歉，本次对话失败：${message}`,
      pending: false,
      persisted: false,
      toolState: "",
    });
  } finally {
    try {
      await refreshConversationList(mode);
    } catch {
      elements.historyStatus.textContent = "对话已保存，但列表刷新失败";
    }
    state.busy = false;
    syncControls();
    elements.input.focus();
  }
}

async function loadSession({ notifyOtherTabs = false } = {}) {
  try {
    const response = await fetch(`${API_PREFIX}/auth/session`, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (response.status === 401 || response.status === 403) {
      handleSessionEnded(undefined, notifyOtherTabs || Boolean(state.user));
      return;
    }
    if (!response.ok) {
      setConnection("disconnected", "ERPNext 授权状态暂时无法验证");
      syncControls();
      return;
    }
    const session = await response.json();
    const firstLoad = !state.user;
    setAuthenticated(session);
    if (firstLoad) {
      setHistoryPanel(!isNarrowScreen());
      await loadWorkspace(state.mode);
    }
  } catch {
    setConnection("disconnected", "无法连接 Agent 服务");
    syncControls();
  }
}

async function logout() {
  if (!state.csrfToken || state.busy) {
    return;
  }
  try {
    await fetch(`${API_PREFIX}/auth/logout`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": state.csrfToken },
    });
  } finally {
    handleSessionEnded("需要登录 ERPNext");
  }
}

elements.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  void sendMessage(elements.input.value);
});

elements.input.addEventListener("input", () => {
  resizeInput();
  syncControls();
});

elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void sendMessage(elements.input.value);
  }
});

for (const button of elements.modeButtons) {
  button.addEventListener("click", () => {
    if (state.busy || button.dataset.mode === state.mode) {
      return;
    }
    state.mode = button.dataset.mode;
    for (const candidate of elements.modeButtons) {
      candidate.classList.toggle("active", candidate === button);
    }
    renderSuggestions();
    elements.modeHint.textContent = MODES[state.mode].hint;
    void loadWorkspace(state.mode);
  });
}

for (const button of elements.newConversationButtons) {
  button.addEventListener("click", () => void createNewConversation());
}

elements.historyToggle.addEventListener("click", () => {
  setHistoryPanel(elements.appShell.classList.contains("history-collapsed"));
});
elements.historyBackdrop.addEventListener("click", () => setHistoryPanel(false));
elements.logout.addEventListener("click", () => void logout());

authChannel?.addEventListener("message", (event) => {
  if (event.data?.type === "erpnext-session-ended") {
    handleSessionEnded(event.data.message, false);
  }
});

window.addEventListener("focus", () => {
  if (state.user) {
    void loadSession({ notifyOtherTabs: true });
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.user) {
    void loadSession({ notifyOtherTabs: true });
  }
});

window.setInterval(() => {
  if (document.visibilityState === "visible" && state.user) {
    void loadSession({ notifyOtherTabs: true });
  }
}, SESSION_RECHECK_MS);

window.matchMedia("(max-width: 820px)").addEventListener("change", (event) => {
  setHistoryPanel(!event.matches);
});

resetConversation();
renderConversationList();
void loadSession();
