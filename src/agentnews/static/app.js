"use strict";

const CURRENT_SESSION_KEY = "agentnews.currentSessionId";
const state = {
  sessionId: null,
  sessionStatus: null,
  sessionView: "active",
  sessionRequest: 0,
  sessionBusy: false,
  // One active run per session, mirroring the backend's per-session lock.
  activeRuns: new Map(),
  dialogRunId: null,
  pendingApprovalId: null,
  runPoller: null,
  reportOffset: 0,
  reportLimit: 10,
  reportRequest: 0,
  selectedReportId: null,
  selectedReport: null,
  detailRaw: false,
  detailMarkdown: "",
  sourceRequest: 0,
  sourceRaw: false,
  sourceMarkdown: "",
};
const markdown = window.AgentNewsMarkdown;
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
  scheduleTimezone: document.querySelector("#schedule-timezone"),
  recentScheduledRun: document.querySelector("#recent-scheduled-run"),
  digestButton: document.querySelector("#run-digest"),
  digestStatus: document.querySelector("#digest-status"),
  digestMessage: document.querySelector("#digest-message"),
  reportsStatus: document.querySelector("#reports-status"),
  reportsError: document.querySelector("#reports-error"),
  reportList: document.querySelector("#report-list"),
  loadMore: document.querySelector("#load-more"),
  refreshReports: document.querySelector("#refresh-reports"),
  detailPlaceholder: document.querySelector("#detail-placeholder"),
  reportDetail: document.querySelector("#report-detail"),
  deleteReport: document.querySelector("#delete-report"),
  detailDate: document.querySelector("#detail-date"),
  detailSourceCount: document.querySelector("#detail-source-count"),
  detailTitle: document.querySelector("#detail-title"),
  detailSummary: document.querySelector("#detail-summary"),
  emailDeliveryStatus: document.querySelector("#email-delivery-status"),
  sendReportEmail: document.querySelector("#send-report-email"),
  resendReportEmail: document.querySelector("#resend-report-email"),
  detailMarkdown: document.querySelector("#detail-markdown"),
  toggleDetailMarkdown: document.querySelector("#toggle-detail-markdown"),
  detailSources: document.querySelector("#detail-sources"),
  sourceViewer: document.querySelector("#source-viewer"),
  sourceUrl: document.querySelector("#source-original-url"),
  sourceSite: document.querySelector("#source-site"),
  sourceError: document.querySelector("#source-error"),
  sourceMarkdown: document.querySelector("#source-markdown"),
  toggleSourceMarkdown: document.querySelector("#toggle-source-markdown"),
  settingsDialog: document.querySelector("#settings-dialog"),
  openSettings: document.querySelector("#open-settings"),
  closeSettings: document.querySelector("#close-settings"),
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
  state.dialogRunId = null;
  setError(elements.bashError, "");
}

function stopRunPolling() {
  if (state.runPoller !== null) {
    window.clearInterval(state.runPoller);
    state.runPoller = null;
  }
}

function runForSession(sessionId) {
  if (!sessionId) return null;
  return state.activeRuns.get(sessionId) || null;
}

function syncRunPolling() {
  if (state.activeRuns.size === 0) stopRunPolling();
  else startRunPolling();
}

function markSessionRun(sessionId, runId) {
  if (!sessionId) return;
  state.activeRuns.set(sessionId, { runId, status: "running", approval: null });
  updateSessionRunIndicators();
  updateChatAvailability();
}

function clearSessionRun(sessionId) {
  if (!sessionId) return;
  if (state.dialogRunId && state.activeRuns.get(sessionId)?.runId === state.dialogRunId) {
    closeBashApproval();
  }
  state.activeRuns.delete(sessionId);
  updateSessionRunIndicators();
  updateChatAvailability();
  if (state.activeRuns.size === 0) stopRunPolling();
}

function updateSessionRunIndicators() {
  for (const item of elements.sessionList.querySelectorAll(".session-item")) {
    const run = runForSession(item.dataset.sessionId);
    item.classList.toggle("session-item-run", Boolean(run));
    item.setAttribute("aria-busy", String(Boolean(run)));
    const select = item.querySelector(".session-select");
    if (!select) continue;
    let dot = select.querySelector(".session-run-dot");
    if (run && !dot) {
      dot = document.createElement("span");
      dot.className = "session-run-dot";
      dot.setAttribute("aria-hidden", "true");
      select.append(dot);
    } else if (!run && dot) {
      dot.remove();
    }
  }
}

function applySnapshotToSession(sessionId, snapshot, { showDialog = true } = {}) {
  const run = runForSession(sessionId);
  if (run) {
    run.status = snapshot.status;
    run.approval = snapshot.approval || null;
  }
  if (state.sessionId === sessionId) {
    if (snapshot.status === "pending_approval") setStatus(elements.chatStatus, "pending_approval");
    else if (snapshot.status === "running") setStatus(elements.chatStatus, "running");
  }
  if (snapshot.status === "pending_approval" && snapshot.approval && showDialog) {
    // The approval dialog is modal, so surface the session it belongs to.
    if (state.sessionId !== sessionId) loadSessionDetail(sessionId);
    state.dialogRunId = snapshot.run_id;
    state.pendingRunId = snapshot.run_id;
    renderBashApproval(snapshot.approval);
  }
  updateSessionRunIndicators();
}

async function finishChatRun(sessionId, snapshot) {
  const isCurrent = () => state.sessionId === sessionId;
  clearSessionRun(sessionId);
  const result = snapshot.result || {};
  if (snapshot.status === "completed") {
    if (isCurrent()) {
      await loadSessionDetail(sessionId);
      elements.toolCalls.textContent = String(result.tool_calls ?? 0);
      setStatus(elements.chatStatus, "completed", `完成 · ${result.tool_calls ?? 0} 次工具调用`);
    }
  } else if (isCurrent()) {
    setStatus(elements.chatStatus, "failed");
    setError(elements.chatError, snapshot.error?.message || "Agent 运行失败，请稍后重试。");
  }
  await loadSessions(state.sessionView);
  if (isCurrent()) {
    updateChatAvailability();
    elements.chatInput.focus();
  }
}

async function handleChatRunSnapshot(sessionId, snapshot) {
  if (snapshot.status === "running") {
    applySnapshotToSession(sessionId, snapshot);
    return;
  }
  if (snapshot.status === "pending_approval" && snapshot.approval) {
    applySnapshotToSession(sessionId, snapshot);
    return;
  }
  if (["completed", "failed", "expired"].includes(snapshot.status)) {
    await finishChatRun(sessionId, snapshot);
  }
}

async function pollChatRuns() {
  if (state.activeRuns.size === 0) {
    stopRunPolling();
    return;
  }
  for (const [sessionId, run] of [...state.activeRuns]) {
    try {
      const snapshot = await request(`/api/chat/runs/${encodeURIComponent(run.runId)}`);
      await handleChatRunSnapshot(sessionId, snapshot);
    } catch (error) {
      clearSessionRun(sessionId);
      if (state.sessionId === sessionId) {
        setStatus(elements.chatStatus, "failed");
        setError(elements.chatError, error.message);
      }
    }
  }
}

function startRunPolling() {
  if (state.runPoller !== null) return;
  state.runPoller = window.setInterval(pollChatRuns, 600);
}

async function submitBashApproval(decision) {
  if (!state.pendingRunId || !state.pendingApprovalId) return;
  const runId = state.pendingRunId;
  const sessionId = [...state.activeRuns.entries()].find(([, run]) => run.runId === runId)?.[0] || null;
  elements.bashApprove.disabled = true;
  elements.bashReject.disabled = true;
  setError(elements.bashError, "");
  try {
    const snapshot = await request(
      `/api/chat/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(state.pendingApprovalId)}`,
      { method: "POST", body: JSON.stringify({ decision }) },
    );
    const decisionSessionId = snapshot.session_id || sessionId;
    if (decisionSessionId) applySnapshotToSession(decisionSessionId, snapshot, { showDialog: false });
    if (snapshot.status === "completed" || snapshot.status === "failed" || snapshot.status === "expired") {
      await finishChatRun(decisionSessionId, snapshot);
    } else {
      syncRunPolling();
    }
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
  form.elements.daily_send_enabled.checked = Boolean(preferences.daily_send_enabled);
  form.elements.daily_send_time.value = preferences.daily_send_time || "08:00";
  elements.scheduleTimezone.textContent = `发送时间按 ${preferences.timezone || "当前时区"} 计算`;
  renderRecentScheduledRun(preferences.recent_scheduled_run);
}

function renderRecentScheduledRun(run) {
  if (!run) {
    elements.recentScheduledRun.textContent = "最近任务：暂无记录";
    return;
  }
  const labels = { pending: "等待执行", running: "执行中", succeeded: "已成功", failed: "失败", skipped: "已跳过" };
  const details = [
    `最近任务：${labels[run.status] || run.status}`,
    `业务日期 ${run.business_date}`,
    `尝试 ${run.attempt_count}/3`,
  ];
  if (run.report_id != null) details.push(`报告 #${run.report_id}`);
  if (run.email_delivery_id != null) details.push(`邮件记录 #${run.email_delivery_id}`);
  if (run.error_code) details.push(`错误 ${run.error_code}`);
  elements.recentScheduledRun.textContent = details.join(" · ");
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

function iconNode(pathData) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", pathData);
  svg.append(path);
  return svg;
}

function renderMarkdown(container, text) {
  if (!container) return;
  if (markdown) markdown.renderMarkdownInto(container, text);
  else container.textContent = text || "";
}

function renderRawText(container, text) {
  container.replaceChildren();
  const pre = document.createElement("pre");
  pre.className = "markdown-code";
  pre.textContent = text || "";
  container.append(pre);
}

function renderDetailMarkdown() {
  if (state.detailRaw) renderRawText(elements.detailMarkdown, state.detailMarkdown);
  else renderMarkdown(elements.detailMarkdown, state.detailMarkdown);
  elements.toggleDetailMarkdown.setAttribute("aria-pressed", String(state.detailRaw));
  elements.toggleDetailMarkdown.textContent = state.detailRaw ? "查看排版" : "查看原文";
}

function renderSourceMarkdown() {
  if (state.sourceRaw) renderRawText(elements.sourceMarkdown, state.sourceMarkdown);
  else renderMarkdown(elements.sourceMarkdown, state.sourceMarkdown);
  elements.toggleSourceMarkdown.setAttribute("aria-pressed", String(state.sourceRaw));
  elements.toggleSourceMarkdown.textContent = state.sourceRaw ? "查看排版" : "查看原文";
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
  const run = runForSession(state.sessionId);
  const sendButton = elements.chatForm.querySelector("button[type=submit]");
  // Only this conversation is locked while its own run is in flight; the rest
  // of the workspace stays usable.
  elements.chatInput.disabled = archived;
  sendButton.disabled = archived || Boolean(run) || state.sessionBusy;
  sendButton.title = run ? "当前会话正在处理，请等待本轮结束" : "发送消息";
  elements.chatInput.placeholder = run ? "当前会话正在处理，可先输入内容…" : "输入消息…";
  elements.restoreCurrent.hidden = !archived;
  updateSessionControlUsability();
}

/** Re-apply per-session button state after the list is re-rendered. */
function updateSessionControlUsability() {
  for (const item of elements.sessionList.querySelectorAll(".session-item")) {
    const busy = Boolean(runForSession(item.dataset.sessionId));
    const remove = item.querySelector(".session-action-delete");
    if (!remove) continue;
    // Deleting a session whose run is in flight discards that run's result, so
    // say so; the button itself stays available.
    remove.dataset.baseTitle = remove.dataset.baseTitle || remove.title;
    remove.title = busy ? "该会话正在处理中，删除会丢弃本轮结果" : remove.dataset.baseTitle;
  }
}

function setSessionBusy(busy) {
  state.sessionBusy = busy;
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
  rename.append(iconNode("M4 20h4l10.5-10.5a2.1 2.1 0 0 0-3-3L5 17v3Z"));
  rename.addEventListener("click", () => renameSession(session));
  const transition = document.createElement("button");
  transition.type = "button";
  transition.className = "session-action session-control";
  transition.title = session.status === "active" ? "归档会话" : "恢复会话";
  transition.setAttribute("aria-label", transition.title);
  transition.append(iconNode("M4 7h16v13H4zM9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"));
  transition.addEventListener("click", () => session.status === "active"
    ? archiveSession(session)
    : restoreSession(session));
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "session-action session-action-delete session-control";
  remove.title = "永久删除会话";
  remove.setAttribute("aria-label", "永久删除会话");
  remove.append(iconNode("M5 7h14M9 7V5h6v2M7 7l1 13h8l1-13M11 11v6M13 11v6"));
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
    updateSessionRunIndicators();
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
  setError(elements.chatError, "");
  if (!runForSession(sessionId)) setStatus(elements.chatStatus, "running", "读取会话中…");
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
      const run = runForSession(state.sessionId);
      if (run) {
        // A run owns the status label for its own conversation.
        setStatus(elements.chatStatus, run.status === "pending_approval" ? "pending_approval" : "running");
      } else if (state.sessionStatus !== "archived" && state.sessionId) {
        setStatus(elements.chatStatus, "idle");
      }
      updateChatAvailability();
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
  const running = Boolean(runForSession(session.id));
  const warning = running ? "该会话正在处理中，删除后本轮结果将被丢弃。" : "";
  if (!window.confirm(`永久删除“${session.title || "未命名会话"}”？${warning}此操作不可恢复。`)) return;
  setSessionBusy(true);
  try {
    await request(`/api/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE" });
    clearSessionRun(session.id);
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
  const requestSessionId = state.sessionId;
  if (!message || state.sessionStatus === "archived") return;
  if (runForSession(requestSessionId) || state.sessionBusy) {
    setError(elements.chatError, "当前会话正在处理，请等待本轮结束再发送。");
    return;
  }
  setError(elements.chatError, "");
  setStatus(elements.chatStatus, "running");
  appendMessage("user", message);
  elements.chatInput.value = "";
  markSessionRun(requestSessionId, "pending");
  try {
    const result = await request("/api/chat", {
      method: "POST",
      body: JSON.stringify({ session_id: requestSessionId, message }),
    });
    // The reply is either a finished run's result or a run that is still going;
    // the session for a new run comes back in `session_id`, not in the result.
    if (result.status === "pending_approval" || result.status === "running") {
      const runSessionId = result.session_id || requestSessionId;
      markSessionRun(runSessionId, result.run_id);
      applySnapshotToSession(runSessionId, result);
      syncRunPolling();
    } else {
      const finishedSessionId = result.session_id || requestSessionId;
      await finishChatRun(finishedSessionId, { status: "completed", result, session_id: finishedSessionId });
      return;
    }
  } catch (error) {
    clearSessionRun(requestSessionId);
    if (error.code === "run_busy") {
      setError(elements.chatError, error.message);
      if (state.sessionId === requestSessionId) setStatus(elements.chatStatus, "running");
    } else {
      setStatus(elements.chatStatus, "failed");
      setError(elements.chatError, error.message);
    }
  } finally {
    if (state.sessionId === requestSessionId) elements.chatInput.focus();
    updateChatAvailability();
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
  const item = document.createElement("article");
  item.className = "report-item";
  item.dataset.reportId = String(report.id);
  item.setAttribute("aria-current", String(report.id === state.selectedReportId));

  const select = document.createElement("button");
  select.type = "button";
  select.className = "report-select";
  select.title = "打开日报";
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
  select.append(top, title, summary);
  select.addEventListener("click", () => loadReport(report.id));

  const actions = document.createElement("span");
  actions.className = "report-actions";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "report-action report-action-delete";
  remove.title = "删除日报";
  remove.setAttribute("aria-label", `删除日报 ${report.date || ""} ${report.title || ""}`.trim());
  remove.append(iconNode("M5 7h14M9 7V5h6v2M7 7l1 13h8l1-13M11 11v6M13 11v6"));
  remove.addEventListener("click", () => deleteReport(report));
  actions.append(remove);

  item.append(select, actions);
  return item;
}

function reportTitleOf(report) {
  return `「${report.title || "未命名日报"}」${report.date ? `（${report.date}）` : ""}`;
}

async function deleteReport(report) {
  if (!report) return;
  const confirmed = window.confirm(
    `永久删除${reportTitleOf(report)}？日报文件、来源归档与邮件发送记录将一并清理，且不可恢复。`,
  );
  if (!confirmed) return;
  const remaining = (state.selectedReportId === report.id)
    ? null
    : state.selectedReportId;
  const requestId = ++state.reportRequest;
  setError(elements.reportsError, "");
  try {
    await request(`/api/reports/${encodeURIComponent(report.id)}`, { method: "DELETE" });
  } catch (error) {
    if (requestId !== state.reportRequest) return;
    // The row stays visible so the user can retry.
    setError(elements.reportsError, `删除日报失败：${error.message}`);
    return;
  }
  if (requestId !== state.reportRequest) return;
  state.selectedReportId = remaining;
  await loadReports({ reset: true });
  const items = elements.reportList.querySelectorAll(".report-item");
  if (!items.length) {
    elements.detailPlaceholder.textContent = "选择一份日报查看内容";
    elements.detailPlaceholder.hidden = false;
    elements.reportDetail.hidden = true;
    elements.deleteReport.hidden = true;
    return;
  }
  const next = state.selectedReportId != null
    ? [...items].find((item) => Number(item.dataset.reportId) === state.selectedReportId)
    : null;
  await loadReport(Number((next || items[0]).dataset.reportId));
}

async function loadReports({ reset = false } = {}) {
  const offset = reset ? 0 : state.reportOffset;
  if (reset) {
    elements.reportList.replaceChildren();
    state.reportOffset = 0;
  }
  elements.reportsStatus.textContent = "读取中…";
  setError(elements.reportsError, "");
  elements.loadMore.disabled = true;
  try {
    const page = await request(`/api/reports?limit=${state.reportLimit}&offset=${offset}`);
    for (const report of page.items || []) elements.reportList.append(makeReportItem(report));
    state.reportOffset = offset + (page.items || []).length;
    elements.loadMore.hidden = (page.items || []).length < state.reportLimit;
    elements.reportsStatus.textContent = state.reportOffset ? `${state.reportOffset} 份日报` : "无历史日报";
  } catch (error) {
    elements.reportsStatus.textContent = "读取失败";
    setError(elements.reportsError, `日报列表读取失败：${error.message}`);
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
  const requestId = ++state.reportRequest;
  elements.detailPlaceholder.textContent = "读取中…";
  elements.detailPlaceholder.hidden = false;
  elements.reportDetail.hidden = true;
  elements.deleteReport.hidden = true;
  try {
    const report = await request(`/api/reports/${encodeURIComponent(reportId)}`);
    if (requestId !== state.reportRequest) return;
    state.selectedReportId = report.id;
    state.selectedReport = report;
    elements.detailDate.textContent = report.date || "";
    elements.detailSourceCount.textContent = `${(report.sources || []).length} 个来源`;
    elements.detailTitle.textContent = report.title || "未命名日报";
    elements.detailSummary.textContent = report.summary || "";
    renderEmailDeliveryStatus(report.email_deliveries || []);
    state.detailMarkdown = report.markdown || "";
    state.detailRaw = false;
    renderDetailMarkdown();
    elements.detailSources.replaceChildren(...(report.sources || []).map(renderSource));
    elements.detailPlaceholder.hidden = true;
    elements.reportDetail.hidden = false;
    elements.deleteReport.hidden = false;
    for (const item of elements.reportList.querySelectorAll(".report-item")) {
      item.setAttribute("aria-current", String(Number(item.dataset.reportId) === report.id));
    }
    elements.sourceViewer.hidden = true;
  } catch (error) {
    if (requestId !== state.reportRequest) return;
    state.selectedReport = null;
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
  const requestId = ++state.sourceRequest;
  elements.sourceViewer.hidden = false;
  elements.sourceViewer.scrollIntoView({ behavior: "smooth", block: "start" });
  setError(elements.sourceError, "");
  state.sourceRaw = false;
  state.sourceMarkdown = "";
  renderMarkdown(elements.sourceMarkdown, "读取中…");
  elements.sourceUrl.textContent = "";
  elements.sourceUrl.removeAttribute("href");
  try {
    const source = await request(`/api/sources/${encodeURIComponent(sourceId)}`);
    if (requestId !== state.sourceRequest) return;
    const url = safeHttpUrl(source.url || "");
    elements.sourceUrl.textContent = url || "原始 URL 不可用";
    if (url) elements.sourceUrl.href = url;
    elements.sourceSite.textContent = source.source_name || "";
    state.sourceMarkdown = source.markdown || "该来源没有正文归档。";
    renderSourceMarkdown();
  } catch (error) {
    if (requestId !== state.sourceRequest) return;
    state.sourceMarkdown = "";
    renderMarkdown(elements.sourceMarkdown, "");
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
    daily_send_enabled: form.elements.daily_send_enabled.checked,
    daily_send_time: form.elements.daily_send_time.value,
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
elements.deleteReport.addEventListener("click", () => deleteReport(state.selectedReport));
document.querySelector("#close-source").addEventListener("click", () => { elements.sourceViewer.hidden = true; });
elements.toggleDetailMarkdown.addEventListener("click", () => {
  state.detailRaw = !state.detailRaw;
  renderDetailMarkdown();
});
elements.toggleSourceMarkdown.addEventListener("click", () => {
  state.sourceRaw = !state.sourceRaw;
  renderSourceMarkdown();
});
elements.openSettings.addEventListener("click", () => {
  if (!elements.settingsDialog.open) elements.settingsDialog.showModal();
});
elements.closeSettings.addEventListener("click", () => elements.settingsDialog.close());
elements.settingsDialog.addEventListener("close", () => elements.openSettings.focus());

loadPreferences();
initializeSessions();
loadReports({ reset: true });
