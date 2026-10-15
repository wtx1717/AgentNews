"use strict";

const state = { sessionId: null, reportOffset: 0, reportLimit: 10, selectedReportId: null };
const elements = {
  today: document.querySelector("#today-label"),
  chatForm: document.querySelector("#chat-form"),
  chatInput: document.querySelector("#chat-input"),
  chatMessages: document.querySelector("#chat-messages"),
  chatStatus: document.querySelector("#chat-status"),
  chatError: document.querySelector("#chat-error"),
  toolCalls: document.querySelector("#tool-call-count"),
  chatSession: document.querySelector("#chat-session-label"),
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

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error("服务器返回了无法读取的响应");
  }
  if (!response.ok) {
    throw new Error(payload?.error?.message || "请求失败，请稍后重试");
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

function appendMessage(role, text) {
  const empty = elements.chatMessages.querySelector(".chat-empty");
  if (empty) empty.remove();
  const article = document.createElement("article");
  article.className = `message message-${role}`;
  const label = document.createElement("p");
  label.className = "message-role";
  label.textContent = role === "user" ? "你" : "助手";
  const body = document.createElement("p");
  body.className = "message-body";
  body.textContent = text;
  article.append(label, body);
  elements.chatMessages.append(article);
  elements.chatMessages.scrollTop = elements.chatMessages.scrollHeight;
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

async function sendChat(event) {
  event.preventDefault();
  const message = elements.chatInput.value.trim();
  if (!message) return;
  const button = elements.chatForm.querySelector("button");
  button.disabled = true;
  setError(elements.chatError, "");
  setStatus(elements.chatStatus, "running");
  appendMessage("user", message);
  elements.chatInput.value = "";
  try {
    const result = await request("/api/chat", {
      method: "POST",
      body: JSON.stringify({ session_id: state.sessionId, message }),
    });
    state.sessionId = result.session_id;
    elements.chatSession.textContent = "会话已连接";
    elements.toolCalls.textContent = String(result.tool_calls ?? 0);
    appendMessage("assistant", result.answer || "");
    setStatus(elements.chatStatus, "completed");
  } catch (error) {
    setStatus(elements.chatStatus, "failed");
    setError(elements.chatError, error.message);
  } finally {
    button.disabled = false;
    elements.chatInput.focus();
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
elements.preferencesForm.addEventListener("submit", savePreferences);
elements.chatForm.addEventListener("submit", sendChat);
elements.digestButton.addEventListener("click", runDigest);
elements.refreshReports.addEventListener("click", () => loadReports({ reset: true }));
elements.loadMore.addEventListener("click", () => loadReports());
document.querySelector("#close-source").addEventListener("click", () => { elements.sourceViewer.hidden = true; });

loadPreferences();
loadReports({ reset: true });
