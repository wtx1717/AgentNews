"use strict";

const CURRENT_SESSION_KEY = "agentnews.currentSessionId";
const state = {
  sessionId: null,
  sessionStatus: null,
  sessionView: "active",
  sessionRequest: 0,
  sessionBusy: false,
  pendingRunId: null,
  pendingApprovalId: null,
  runPoller: null,
  reportOffset: 0,
  reportLimit: 10,
  selectedReportId: null,
};
const elements = {
  today: document.querySelector("#today-label"),
  newSession: document.querySelector("#new-session"),
  sessionsActive: document.querySelector("#sessions-active"),
  sessionsArchived: document.querySelector("#sessions-archived"),
  sessionsStatus: document.querySelector("#sessions-status"),
  sessionList: document.querySelector("#session-list"),
  sessionsError: document.querySelector("#sessions-error"),
  restoreCurrent: document.querySelector("#restore-current-session"),
  chatForm: document.querySelector("#chat-form"),
  chatInput: document.querySelector("#chat-input"),
  chatMessages: document.querySelector("#chat-messages"),
  chatStatus: document.querySelector("#chat-status"),
  chatError: document.querySelector("#chat-error"),
  toolCalls: document.querySelector("#tool-call-count"),
  chatSession: document.querySelector("#chat-session-label"),
  bashDialog: document.querySelector("#bash-approval-dialog"),
  bashExplanation: document.querySelector("#bash-approval-explanation"),
  bashRisk: document.querySelector("#bash-approval-risk"),
  bashCommand: document.querySelector("#bash-approval-command"),
  bashCwd: document.querySelector("#bash-approval-cwd"),
  bashTimeout: document.querySelector("#bash-approval-timeout"),
  bashOutput: document.querySelector("#bash-approval-output"),
  bashError: document.querySelector("#bash-approval-error"),
  bashApprove: document.querySelector("#bash-approve"),
  bashReject: document.querySelector("#bash-reject"),
  preferencesForm: document.querySelector("#preferences-form"),
  preferencesStatus: document.querySelector("#preferences-status"),
  preferencesError: document.querySelector("#preferences-error"),
  digestButton: document.querySelector("#run-digest"),
  digestStatus: document.querySelector("#digest-status"),
  digestMessage: document.querySelector("#digest-message"),
  reportsStatus: document.querySelector("#reports-status"),
  reportList: document.querySelector("#report-list"),
  loadMore: document.querySelector("#load-more"),
  refreshReports: document.querySelector("#refresh-reports"),
  detailPlaceholder: document.querySelector("#detail-placeholder"),
  reportDetail: document.querySelector("#report-detail"),
  detailDate: document.querySelector("#detail-date"),
  detailSourceCount: document.querySelector("#detail-source-count"),
  detailTitle: document.querySelector("#detail-title"),
  detailSummary: document.querySelector("#detail-summary"),
  emailDeliveryStatus: document.querySelector("#email-delivery-status"),
  sendReportEmail: document.querySelector("#send-report-email"),
  resendReportEmail: document.querySelector("#resend-report-email"),
  detailMarkdown: document.querySelector("#detail-markdown"),
  detailSources: document.querySelector("#detail-sources"),
  sourceViewer: document.querySelector("#source-viewer"),
  sourceUrl: document.querySelector("#source-original-url"),
  sourceSite: document.querySelector("#source-site"),
  sourceError: document.querySelector("#source-error"),
  sourceMarkdown: document.querySelector("#source-markdown"),
};

function setStatus(element, status, message) {
  const labels = {
    idle: "尚未执行",
    running: "执行中",
    pending_approval: "等待人工审核",
    completed: "完成",
    no_results: "无结果",
    failed: "失败",
  };
  element.className = `status status-${status.replace("_", "-")}`;
  element.textContent = message || labels[status] || labels.idle;
}

function setError(element, message) {
  element.textContent = message || "";
  element.hidden = !message;
}

function renderBashApproval(approval) {
  state.pendingApprovalId = approval?.id || null;
  elements.bashExplanation.textContent = approval?.explanation || "模型未提供执行说明。";
  elements.bashRisk.textContent = approval?.risk_summary || "批准后命令将在当前用户权限下执行。";
  elements.bashCommand.textContent = approval?.command || "";
  elements.bashCwd.textContent = approval?.working_directory || "";
  elements.bashTimeout.textContent = `${approval?.timeout_seconds ?? 60} 秒`;
  elements.bashOutput.textContent = `${approval?.output_limit_bytes ?? 1048576} 字节`;
  setError(elements.bashError, "");
  elements.bashApprove.disabled = false;
  elements.bashReject.disabled = false;
  if (!elements.bashDialog.open) elements.bashDialog.showModal();
}

function closeBashApproval() {
  if (elements.bashDialog.open) elements.bashDialog.close();
  state.pendingApprovalId = null;
  setError(elements.bashError, "");
}

function stopRunPolling() {
  if (state.runPoller !== null) {
    window.clearInterval(state.runPoller);
    state.runPoller = null;
  }
}

async function finishChatRun(snapshot) {
  stopRunPolling();
  state.pendingRunId = null;
  closeBashApproval();
  if (snapshot.status === "completed") {
    const result = snapshot.result || {};
    await loadSessionDetail(result.session_id || snapshot.session_id);
    await loadSessions("active");
    elements.toolCalls.textContent = String(result.tool_calls ?? 0);
    setStatus(elements.chatStatus, "completed");
  } else {
    setStatus(elements.chatStatus, "failed");
    setError(elements.chatError, snapshot.error?.message || "Agent 运行失败，请稍后重试。")
  }
  setSessionBusy(false);
  elements.chatInput.focus();
}

async function handleChatRunSnapshot(snapshot) {
  if (snapshot.status === "pending_approval" && snapshot.approval) {
    state.pendingRunId = snapshot.run_id;
    setStatus(elements.chatStatus, "pending_approval");
    renderBashApproval(snapshot.approval);
    return;
  }
  if (snapshot.status === "running") {
    closeBashApproval();
    setStatus(elements.chatStatus, "running");
    return;
  }
  if (["completed", "failed", "expired"].includes(snapshot.status)) {
    await finishChatRun(snapshot);
  }
}

async function pollChatRun() {
  if (!state.pendingRunId) return;
  try {
    const snapshot = await request(`/api/chat/runs/${encodeURIComponent(state.pendingRunId)}`);
    await handleChatRunSnapshot(snapshot);
  } catch (error) {
    stopRunPolling();
    closeBashApproval();
    setStatus(elements.chatStatus, "failed");
    setError(elements.chatError, error.message);
    state.pendingRunId = null;
    setSessionBusy(false);
  }
}

function startRunPolling() {
  stopRunPolling();
  state.runPoller = window.setInterval(pollChatRun, 600);
}

async function submitBashApproval(decision) {
  if (!state.pendingRunId || !state.pendingApprovalId) return;
  elements.bashApprove.disabled = true;
  elements.bashReject.disabled = true;
  setError(elements.bashError, "");
  try {
    const snapshot = await request(
      `/api/chat/runs/${encodeURIComponent(state.pendingRunId)}/approvals/${encodeURIComponent(state.pendingApprovalId)}`,
      { method: "POST", body: JSON.stringify({ decision }) },
    );
    await handleChatRunSnapshot(snapshot);
    if (snapshot.status === "running" || snapshot.status === "pending_approval") startRunPolling();
  } catch (error) {
    setError(elements.bashError, error.message);
    elements.bashApprove.disabled = false;
    elements.bashReject.disabled = false;
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (response.status === 204) return null;
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error("服务器返回了无法读取的响应");
  }
  if (!response.ok) {
    const error = new Error(payload?.error?.message || "请求失败，请稍后重试");
    error.code = payload?.error?.code || "request_failed";
    error.status = response.status;
    throw error;
  }
  return payload;
}

function parseList(value) {
  return value.split(/[\n,，]+/).map((item) => item.trim()).filter(Boolean);
}

function setFormValues(form, preferences) {
  form.elements.display_name.value = preferences.display_name || "";
  form.elements.email.value = preferences.email || "";
  form.elements.timezone.value = preferences.timezone || "Asia/Shanghai";
  form.elements.topics.value = (preferences.topics || []).join("\n");
  form.elements.keywords.value = (preferences.keywords || []).join("\n");
}

function readStoredSession() {
  try {
    return window.localStorage.getItem(CURRENT_SESSION_KEY);
  } catch {
    return null;
  }
}

function storeCurrentSession(sessionId) {
  try {
    if (sessionId) window.localStorage.setItem(CURRENT_SESSION_KEY, sessionId);
    else window.localStorage.removeItem(CURRENT_SESSION_KEY);
  } catch {
    // Private browsing may disable storage; the current page remains usable.
  }
}

function formatSessionTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function appendMessage(role, text) {
  const empty = elements.chatMessages.querySelector(".chat-empty");
  if (empty) empty.remove();
  const article = document.createElement("article");
  article.className = `message message-${role}`;
  const label = document.createElement("p");
  label.className = "message-role";
  label.textContent = { user: "你", assistant: "助手", system: "系统", tool: "工具" }[role] || role;
  const body = document.createElement("p");
  body.className = "message-body";
  body.textContent = text;
  article.append(label, body);
  elements.chatMessages.append(article);
}

function renderMessages(messages) {
  elements.chatMessages.replaceChildren();
  if (!messages?.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state chat-empty";
    empty.textContent = "开始一段新对话";
    elements.chatMessages.append(empty);
    return;
  }
  for (const message of messages) appendMessage(message.role, message.content);
  elements.chatMessages.scrollTop = elements.chatMessages.scrollHeight;
}

function updateChatAvailability() {
  const archived = state.sessionStatus === "archived";
  elements.chatInput.disabled = state.sessionBusy || archived;
  elements.chatForm.querySelector("button[type=submit]").disabled = state.sessionBusy || archived;
  elements.restoreCurrent.hidden = !archived;
}

function setSessionBusy(busy) {
  state.sessionBusy = busy;
  for (const control of document.querySelectorAll(".session-control")) control.disabled = busy;
  updateChatAvailability();
}

function clearCurrentSession() {
  state.sessionId = null;
  state.sessionStatus = null;
  storeCurrentSession(null);
  renderMessages([]);
  elements.chatSession.textContent = "尚未选择会话";
  elements.toolCalls.textContent = "0";
  setStatus(elements.chatStatus, "idle");
  setError(elements.chatError, "");
  updateChatAvailability();
}

function setCurrentSession(session) {
  state.sessionId = session?.id || null;
  state.sessionStatus = session?.status || null;
  if (session?.status === "active") storeCurrentSession(session.id);
  else storeCurrentSession(null);
  elements.chatSession.textContent = session?.title || "未命名会话";
  if (session?.status === "archived") {
    setStatus(elements.chatStatus, "idle", "归档会话（只读）");
  } else {
    setStatus(elements.chatStatus, "idle");
  }
  updateChatAvailability();
  for (const item of elements.sessionList.querySelectorAll(".session-item")) {
    item.setAttribute("aria-current", String(item.dataset.sessionId === state.sessionId));
  }
}

function setSessionView(status) {
  state.sessionView = status;
  elements.sessionsActive.setAttribute("aria-selected", String(status === "active"));
  elements.sessionsArchived.setAttribute("aria-selected", String(status === "archived"));
}

function makeSessionItem(session) {
  const item = document.createElement("article");
  item.className = "session-item";
  item.dataset.sessionId = session.id;
  item.setAttribute("aria-current", String(session.id === state.sessionId));

  const select = document.createElement("button");
  select.type = "button";
  select.className = "session-select session-control";
  select.title = "打开会话";
  const title = document.createElement("span");
  title.className = "session-title";
  title.textContent = session.title || "未命名会话";
  const time = document.createElement("span");
  time.className = "session-time";
  time.textContent = formatSessionTime(session.updated_at);
  select.append(title, time);
  select.addEventListener("click", () => loadSessionDetail(session.id));

  const actions = document.createElement("span");
  actions.className = "session-actions";
  const rename = document.createElement("button");
  rename.type = "button";
  rename.className = "session-action session-control";
  rename.title = "重命名会话";
  rename.setAttribute("aria-label", "重命名会话");
  rename.textContent = "改";
  rename.addEventListener("click", () => renameSession(session));
  const transition = document.createElement("button");
  transition.type = "button";
  transition.className = "session-action session-control";
  transition.title = session.status === "active" ? "归档会话" : "恢复会话";
  transition.setAttribute("aria-label", transition.title);
  transition.textContent = session.status === "active" ? "归" : "恢";
  transition.addEventListener("click", () => session.status === "active"
    ? archiveSession(session)
    : restoreSession(session));
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "session-action session-action-delete session-control";
  remove.title = "永久删除会话";
  remove.setAttribute("aria-label", "永久删除会话");
  remove.textContent = "删";
  remove.addEventListener("click", () => deleteSession(session));
  actions.append(rename, transition, remove);
  item.append(select, actions);
  return item;
}

function renderSessionList(items) {
  elements.sessionList.replaceChildren();
  if (!items?.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = state.sessionView === "active" ? "暂无活动会话" : "暂无归档会话";
    elements.sessionList.append(empty);
    return;
  }
  for (const session of items) elements.sessionList.append(makeSessionItem(session));
}

async function loadSessions(status = state.sessionView) {
  setSessionView(status);
  elements.sessionsStatus.textContent = "读取中…";
  setError(elements.sessionsError, "");
  try {
    const page = await request(`/api/sessions?status=${status}&limit=100&offset=0`);
    renderSessionList(page.items || []);
    elements.sessionsStatus.textContent = page.items?.length ? `${page.items.length} 个会话` : "暂无会话";
  } catch (error) {
    renderSessionList([]);
    elements.sessionsStatus.textContent = "读取失败";
    setError(elements.sessionsError, error.message);
  }
}

async function loadSessionDetail(sessionId, { initial = false } = {}) {
  if (!sessionId) return;
  const requestId = ++state.sessionRequest;
  setSessionBusy(true);
  setError(elements.chatError, "");
  setStatus(elements.chatStatus, "running", "读取会话中…");
  try {
    const session = await request(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (requestId !== state.sessionRequest) return;
    if (initial && session.status !== "active") {
      clearCurrentSession();
      return;
    }
    setCurrentSession(session);
    renderMessages(session.messages || []);
    elements.toolCalls.textContent = "0";
  } catch (error) {
    if (requestId !== state.sessionRequest) return;
    if (error.code === "session_not_found" || (initial && error.status === 404)) {
      clearCurrentSession();
    } else {
      setStatus(elements.chatStatus, "failed");
      setError(elements.chatError, error.message);
    }
  } finally {
    if (requestId === state.sessionRequest) {
      setSessionBusy(false);
      if (state.sessionStatus !== "archived" && state.sessionId) setStatus(elements.chatStatus, "idle");
    }
  }
}

async function initializeSessions() {
  await loadSessions("active");
  const storedId = readStoredSession();
  if (storedId) await loadSessionDetail(storedId, { initial: true });
  else clearCurrentSession();
}

async function createSession() {
  if (state.sessionBusy) return;
  setSessionBusy(true);
  setError(elements.sessionsError, "");
  try {
    const session = await request("/api/sessions", { method: "POST" });
    setCurrentSession(session);
    renderMessages([]);
    elements.toolCalls.textContent = "0";
    await loadSessions("active");
    elements.chatInput.focus();
  } catch (error) {
    setError(elements.sessionsError, error.message);
  } finally {
    setSessionBusy(false);
  }
}

async function renameSession(session) {
  if (state.sessionBusy) return;
  const title = window.prompt("请输入新的会话标题", session.title || "");
  if (title === null || !title.trim()) return;
  setSessionBusy(true);
  try {
    const updated = await request(`/api/sessions/${encodeURIComponent(session.id)}`, {
      method: "PATCH",
      body: JSON.stringify({ title: title.trim() }),
    });
    if (state.sessionId === updated.id) setCurrentSession(updated);
    await loadSessions(state.sessionView);
  } catch (error) {
    setError(elements.sessionsError, error.message);
  } finally {
    setSessionBusy(false);
  }
}

async function archiveSession(session) {
  if (state.sessionBusy) return;
  setSessionBusy(true);
  try {
    await request(`/api/sessions/${encodeURIComponent(session.id)}/archive`, { method: "POST", body: "{}" });
    if (state.sessionId === session.id) clearCurrentSession();
    await loadSessions(state.sessionView);
  } catch (error) {
    setError(elements.sessionsError, error.message);
  } finally {
    setSessionBusy(false);
  }
}

async function restoreSession(session) {
  if (state.sessionBusy) return;
  setSessionBusy(true);
  try {
    const restored = await request(`/api/sessions/${encodeURIComponent(session.id)}/restore`, { method: "POST", body: "{}" });
    setCurrentSession(restored);
    await loadSessions("active");
    await loadSessionDetail(restored.id);
  } catch (error) {
    setError(elements.sessionsError, error.message);
  } finally {
    setSessionBusy(false);
  }
}

async function deleteSession(session) {
  if (state.sessionBusy) return;
  if (!window.confirm(`永久删除“${session.title || "未命名会话"}”？此操作不可恢复。`)) return;
  setSessionBusy(true);
  try {
    await request(`/api/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE" });
    if (state.sessionId === session.id) clearCurrentSession();
    await loadSessions(state.sessionView);
  } catch (error) {
    setError(elements.sessionsError, error.message);
  } finally {
    setSessionBusy(false);
  }
}

async function sendChat(event) {
  event.preventDefault();
  const message = elements.chatInput.value.trim();
  if (!message || state.sessionStatus === "archived" || state.sessionBusy) return;
  const button = elements.chatForm.querySelector("button");
  const requestSessionId = state.sessionId;
  setSessionBusy(true);
  setError(elements.chatError, "");
  setStatus(elements.chatStatus, "running");
  appendMessage("user", message);
  elements.chatInput.value = "";
  let pendingApproval = false;
  try {
    const result = await request("/api/chat", {
      method: "POST",
      body: JSON.stringify({ session_id: requestSessionId, message }),
    });
    if (result.status === "pending_approval") {
      pendingApproval = true;
      state.pendingRunId = result.run_id;
      await handleChatRunSnapshot(result);
      startRunPolling();
    } else {
      await finishChatRun({ status: "completed", result });
    }
  } catch (error) {
    setStatus(elements.chatStatus, "failed");
    setError(elements.chatError, error.message);
  } finally {
    if (!pendingApproval) {
      button.disabled = false;
      setSessionBusy(false);
      elements.chatInput.focus();
    }
  }
}

function safeHttpUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function makeReportItem(report) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "report-item";
  button.dataset.reportId = String(report.id);
  button.setAttribute("aria-current", String(report.id === state.selectedReportId));
  const top = document.createElement("span");
  top.className = "report-item-top";
  const date = document.createElement("time");
  date.textContent = report.date || "";
  const count = document.createElement("span");
  count.textContent = `${report.source_count ?? 0} 个来源`;
  top.append(date, count);
  const title = document.createElement("span");
  title.className = "report-item-title";
  title.textContent = report.title || "未命名日报";
  const summary = document.createElement("span");
  summary.className = "report-item-summary";
  summary.textContent = report.summary || "暂无摘要";
  button.append(top, title, summary);
  button.addEventListener("click", () => loadReport(report.id));
  return button;
}

async function loadReports({ reset = false } = {}) {
  const offset = reset ? 0 : state.reportOffset;
  if (reset) {
    elements.reportList.replaceChildren();
    state.reportOffset = 0;
  }
  elements.reportsStatus.textContent = "读取中…";
  elements.loadMore.disabled = true;
  try {
    const page = await request(`/api/reports?limit=${state.reportLimit}&offset=${offset}`);
    for (const report of page.items || []) elements.reportList.append(makeReportItem(report));
    state.reportOffset = offset + (page.items || []).length;
    elements.loadMore.hidden = (page.items || []).length < state.reportLimit;
    elements.reportsStatus.textContent = state.reportOffset ? `${state.reportOffset} 份日报` : "无历史日报";
  } catch (error) {
    elements.reportsStatus.textContent = `失败：${error.message}`;
  } finally {
    elements.loadMore.disabled = false;
  }
}

function renderSource(source) {
  const item = document.createElement("li");
  item.className = "source-entry";
  const title = document.createElement("span");
  title.className = "source-title";
  title.textContent = source.title || "未命名来源";
  const site = document.createElement("span");
  site.className = "source-site";
  site.textContent = source.source_name || source.url || "";
  const actions = document.createElement("span");
  actions.className = "source-actions";
  if (source.archive_path) {
    const archiveButton = document.createElement("button");
    archiveButton.type = "button";
    archiveButton.textContent = "读取归档";
    archiveButton.addEventListener("click", () => loadSource(source.id));
    actions.append(archiveButton);
  }
  const url = safeHttpUrl(source.url || "");
  if (url) {
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "原始 URL";
    actions.append(link);
  }
  item.append(title, site, actions);
  return item;
}

async function loadReport(reportId) {
  elements.detailPlaceholder.textContent = "读取中…";
  elements.detailPlaceholder.hidden = false;
  elements.reportDetail.hidden = true;
  try {
    const report = await request(`/api/reports/${encodeURIComponent(reportId)}`);
    state.selectedReportId = report.id;
    elements.detailDate.textContent = report.date || "";
    elements.detailSourceCount.textContent = `${(report.sources || []).length} 个来源`;
    elements.detailTitle.textContent = report.title || "未命名日报";
    elements.detailSummary.textContent = report.summary || "";
    renderEmailDeliveryStatus(report.email_deliveries || []);
    elements.detailMarkdown.textContent = report.markdown || "";
    elements.detailSources.replaceChildren(...(report.sources || []).map(renderSource));
    elements.detailPlaceholder.hidden = true;
    elements.reportDetail.hidden = false;
    for (const item of elements.reportList.querySelectorAll(".report-item")) {
      item.setAttribute("aria-current", String(Number(item.dataset.reportId) === report.id));
    }
    elements.sourceViewer.hidden = true;
  } catch (error) {
    elements.detailPlaceholder.textContent = `读取失败：${error.message}`;
  }
}

function renderEmailDeliveryStatus(deliveries) {
  const latest = deliveries[0] || null;
  elements.sendReportEmail.disabled = Boolean(latest);
  elements.sendReportEmail.textContent = latest?.status === "sent" ? "已发送" : "发送日报";
  elements.resendReportEmail.hidden = !latest || latest.status === "pending";
  elements.resendReportEmail.disabled = latest?.status === "pending";
  elements.resendReportEmail.textContent = latest?.status === "failed" ? "重试发送" : "明确重发";
  if (!latest) {
    elements.emailDeliveryStatus.textContent = "尚未发送";
    return;
  }
  const statusText = latest.status === "sent" ? "发送成功" : latest.status === "pending" ? "发送中" : "发送失败";
  const recipient = latest.recipient ? `至 ${latest.recipient}` : "收件邮箱未设置";
  const error = latest.error_message ? `；${latest.error_message}` : "";
  elements.emailDeliveryStatus.textContent = `${statusText}${recipient ? `，${recipient}` : ""}${error}`;
}

async function sendReportEmail(resend) {
  if (!state.selectedReportId) return;
  elements.sendReportEmail.disabled = true;
  elements.resendReportEmail.disabled = true;
  elements.emailDeliveryStatus.textContent = "正在发送日报…";
  try {
    await request(`/api/reports/${encodeURIComponent(state.selectedReportId)}/email`, {
      method: "POST",
      body: JSON.stringify({ resend }),
    });
    await loadReport(state.selectedReportId);
  } catch (error) {
    elements.emailDeliveryStatus.textContent = `发送请求失败：${error.message}`;
    elements.sendReportEmail.disabled = false;
    elements.resendReportEmail.disabled = false;
  }
}

async function loadSource(sourceId) {
  elements.sourceViewer.hidden = false;
  elements.sourceViewer.scrollIntoView({ behavior: "smooth", block: "start" });
  setError(elements.sourceError, "");
  elements.sourceMarkdown.textContent = "读取中…";
  elements.sourceUrl.textContent = "";
  elements.sourceUrl.removeAttribute("href");
  try {
    const source = await request(`/api/sources/${encodeURIComponent(sourceId)}`);
    const url = safeHttpUrl(source.url || "");
    elements.sourceUrl.textContent = url || "原始 URL 不可用";
    if (url) elements.sourceUrl.href = url;
    elements.sourceSite.textContent = source.source_name || "";
    elements.sourceMarkdown.textContent = source.markdown || "该来源没有正文归档。";
  } catch (error) {
    elements.sourceMarkdown.textContent = "";
    setError(elements.sourceError, error.message);
  }
}

async function loadPreferences() {
  elements.preferencesStatus.textContent = "读取中…";
  try {
    setFormValues(elements.preferencesForm, await request("/api/settings"));
    elements.preferencesStatus.textContent = "已载入";
  } catch (error) {
    elements.preferencesStatus.textContent = "读取失败";
    setError(elements.preferencesError, error.message);
  }
}

async function savePreferences(event) {
  event.preventDefault();
  const form = elements.preferencesForm;
  const submit = form.querySelector("[type=submit]");
  submit.disabled = true;
  elements.preferencesStatus.textContent = "保存中…";
  setError(elements.preferencesError, "");
  const payload = {
    display_name: form.elements.display_name.value.trim(),
    email: form.elements.email.value.trim(),
    timezone: form.elements.timezone.value.trim() || "Asia/Shanghai",
    topics: parseList(form.elements.topics.value),
    keywords: parseList(form.elements.keywords.value),
  };
  try {
    const saved = await request("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
    setFormValues(form, saved);
    elements.preferencesStatus.textContent = "已保存";
  } catch (error) {
    elements.preferencesStatus.textContent = "失败";
    setError(elements.preferencesError, error.message);
  } finally {
    submit.disabled = false;
  }
}

async function runDigest() {
  elements.digestButton.disabled = true;
  elements.digestMessage.textContent = "";
  setStatus(elements.digestStatus, "running");
  try {
    const result = await request("/api/digest/run-once", { method: "POST", body: "{}" });
    if (result.status === "no_results") {
      setStatus(elements.digestStatus, "no_results");
      elements.digestMessage.textContent = result.message || "没有可靠来源，未生成日报。";
    } else {
      setStatus(elements.digestStatus, "completed");
      elements.digestMessage.textContent = `已生成「${result.title || "日报"}」，共 ${result.source_count ?? 0} 个来源。`;
    }
    await loadReports({ reset: true });
    if (result.report_id != null) await loadReport(result.report_id);
  } catch (error) {
    setStatus(elements.digestStatus, "failed");
    elements.digestMessage.textContent = error.message;
  } finally {
    elements.digestButton.disabled = false;
  }
}

elements.today.dateTime = new Date().toISOString();
elements.today.textContent = new Intl.DateTimeFormat("zh-CN", { dateStyle: "full" }).format(new Date());
elements.newSession.addEventListener("click", createSession);
elements.sessionsActive.addEventListener("click", () => loadSessions("active"));
elements.sessionsArchived.addEventListener("click", () => loadSessions("archived"));
elements.restoreCurrent.addEventListener("click", async () => {
  if (!state.sessionId) return;
  await restoreSession({ id: state.sessionId, title: elements.chatSession.textContent, status: "archived" });
});
elements.preferencesForm.addEventListener("submit", savePreferences);
elements.chatForm.addEventListener("submit", sendChat);
elements.bashApprove.addEventListener("click", () => submitBashApproval("approve"));
elements.bashReject.addEventListener("click", () => submitBashApproval("reject"));
elements.bashDialog.addEventListener("cancel", (event) => event.preventDefault());
elements.digestButton.addEventListener("click", runDigest);
elements.sendReportEmail.addEventListener("click", () => sendReportEmail(false));
elements.resendReportEmail.addEventListener("click", () => sendReportEmail(true));
elements.refreshReports.addEventListener("click", () => loadReports({ reset: true }));
elements.loadMore.addEventListener("click", () => loadReports());
document.querySelector("#close-source").addEventListener("click", () => { elements.sourceViewer.hidden = true; });

loadPreferences();
initializeSessions();
loadReports({ reset: true });
