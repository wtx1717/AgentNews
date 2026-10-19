import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { buildPageSkeleton, createDocument, el, collectText, tagNames } from "./dom-stub.mjs";

const STATIC_ROOT = fileURLToPath(new URL("../../src/agentnews/static/", import.meta.url));

// ---------------------------------------------------------------------------
// Harness: run the real page scripts against the DOM stub and a fake API.
// ---------------------------------------------------------------------------

function htmlIds(html) {
  const ids = [];
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) ids.push(match[1]);
  return ids;
}

function attributeValue(html, id, attribute) {
  const match = new RegExp(`id="${id}"[^>]*\\b${attribute}="([^"]*)"`).exec(html);
  return match ? match[1] : null;
}

/**
 * The real page ships the forms in its markup; the stub only creates one
 * placeholder element per id. Populate those placeholders in place so the app
 * and the assertions always share the same element objects.
 */
function fixtureElement(document, id, spec) {
  const element = document.querySelectorAll(`#${id}`)[0];
  if (!element) throw new Error(`missing page element #${id}`);
  if (spec.class) element.setAttribute("class", spec.class);
  if (spec.attributes) {
    for (const [name, value] of Object.entries(spec.attributes)) element.setAttribute(name, value);
  }
  for (const [tagName, attributes] of spec.controls || []) {
    const control = el(document, tagName, attributes);
    if (attributes?.id) document.register(control);
    element.appendChild(control);
  }
  return element;
}

function createAppHarness({ html, responses, prompts = [] } = {}) {
  const document = createDocument();
  buildPageSkeleton(document, htmlIds(html));
  // #chat-input is nested inside #chat-form in the real page; the placeholder
  // element would shadow it, so drop the placeholder before wiring the form.
  for (const placeholder of document.querySelectorAll("#chat-input")) placeholder.remove();
  document.querySelector("#session-list").className = "session-list";
  document.querySelector("#report-list").className = "report-list";

  fixtureElement(document, "preferences-form", {
    class: "preferences-form",
    controls: [
      ["input", { name: "display_name", type: "text" }],
      ["input", { name: "email", type: "email" }],
      ["input", { name: "timezone", type: "text", value: "Asia/Shanghai" }],
      ["input", { name: "daily_send_enabled", type: "checkbox" }],
      ["input", { name: "daily_send_time", type: "time", value: "08:00" }],
      ["textarea", { name: "topics" }],
      ["textarea", { name: "keywords" }],
      ["button", { type: "submit", text: "保存偏好" }],
    ],
  });
  fixtureElement(document, "chat-form", {
    class: "chat-form",
    controls: [
      ["textarea", { id: "chat-input", name: "message" }],
      ["button", { type: "submit" }],
    ],
  });
  const calls = [];
  const timers = [];
  const storage = new Map();
  const answers = { prompt: [...prompts], confirm: [...prompts] };
  const callsFor = (path) =>
    calls.filter((call) => (call.search ? `${call.path}${call.search}` : call.path) === path);

  const windowMock = {
    localStorage: {
      getItem: (key) => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
    },
    // Mutable so a test can answer the next confirmation differently.
    prompt: () => (answers.prompt.length ? answers.prompt.shift() : null),
    confirm: () => Boolean(answers.confirm.length ? answers.confirm.shift() : true),
    setInterval: (handler) => {
      timers.push(handler);
      return timers.length;
    },
    clearInterval: () => {},
  };

  const fetchMock = async (path, options = {}) => {
    const method = (options.method || "GET").toUpperCase();
    const url = new URL(String(path), "http://localhost");
    calls.push({ method, path: url.pathname, search: url.search, body: options.body });
    const handler =
      responses[`${method} ${url.pathname}${url.search}`] ?? responses[`${method} ${url.pathname}`];
    if (!handler) throw new Error(`unexpected request: ${method} ${url.pathname}${url.search}`);
    const payload = typeof handler === "function" ? handler({ url, body: options.body }) : handler;
    if (payload instanceof Error) throw payload;
    return {
      ok: payload.status === undefined ? true : payload.status < 400,
      status: payload.status ?? 200,
      async json() {
        if (payload.__raw) throw new Error("bad json");
        return payload.__body === undefined ? payload : payload.__body;
      },    };
  };

  const previousGlobals = new Map();
  for (const [name, value] of Object.entries({
    window: windowMock,
    document,
    fetch: fetchMock,
    localStorage: windowMock.localStorage,
  })) {
    previousGlobals.set(name, globalThis[name]);
    globalThis[name] = value;
  }
  return {
    document,
    calls,
    callsFor,
    timers,
    storage,
    windowMock,
    restore() {
      for (const [name, value] of previousGlobals) {
        if (value === undefined) delete globalThis[name];
        else globalThis[name] = value;
      }
    },
  };
}

async function loadScripts() {
  const markdownSource = await readFile(`${STATIC_ROOT}markdown-render.js`, "utf8");
  const appSource = await readFile(`${STATIC_ROOT}app.js`, "utf8");
  (0, eval)(markdownSource);
  (0, eval)(appSource);
}

async function settle() {
  for (let i = 0; i < 12; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

const SETTINGS = {
  display_name: "测试用户",
  email: "tester@example.com",
  timezone: "Asia/Shanghai",
  topics: ["人工智能"],
  keywords: ["Agent"],
  daily_send_enabled: false,
  daily_send_time: "08:00",
  recent_scheduled_run: {
    status: "succeeded",
    business_date: "2026-10-16",
    attempt_count: 1,
    report_id: 1,
  },
};

const SESSION = {
  id: "s1",
  title: "首条消息标题",
  status: "active",
  updated_at: "2026-10-16T08:00:00+08:00",
  messages: [],
};
const SESSION_ARCHIVED = { ...SESSION, status: "archived" };
const REPORT_LIST = {
  items: [{ id: 1, date: "2026-10-16", title: "日报标题", summary: "摘要", source_count: 1 }],
  limit: 10,
  offset: 0,
};
const REPORT_MARKDOWN = "# 一级标题\n\n- 项目一\n- 项目二\n\n| 列 |\n| --- |\n| 值 |";
const REPORT_DETAIL = {
  id: 1,
  date: "2026-10-16",
  title: "日报标题",
  summary: "摘要",
  markdown: REPORT_MARKDOWN,
  sources: [
    {
      id: 5,
      title: "来源标题",
      source_name: "示例站",
      url: "https://example.com/a",
      archive_path: "x.md",
    },
  ],
  email_deliveries: [],
};
const SOURCE_DETAIL = {
  id: 5,
  title: "来源标题",
  source_name: "示例站",
  url: "https://example.com/a",
  markdown: "## 来源标题\n\n正文段落",
};

function baseResponses(overrides = {}) {
  return {
    "GET /api/settings": { __body: SETTINGS },
    "GET /api/sessions": { __body: { items: [SESSION], limit: 100, offset: 0 } },
    "GET /api/sessions/s1": { __body: SESSION },
    "GET /api/reports": { __body: REPORT_LIST },
    "GET /api/reports/1": { __body: REPORT_DETAIL },
    "GET /api/sources/5": { __body: SOURCE_DETAIL },
    ...overrides,
  };
}

async function startApp(options = {}) {
  const harness = createAppHarness({
    html: options.html ?? html,
    responses: baseResponses(options.responses),
    prompts: options.prompts,
  });
  if (options.storedSession) {
    harness.storage.set("agentnews.currentSessionId", options.storedSession);
  }
  await loadScripts();
  await settle();
  return harness;
}

// ---------------------------------------------------------------------------
// Page markup + wiring
// ---------------------------------------------------------------------------

const html = await readFile(`${STATIC_ROOT}index.html`, "utf8");
const appSource = await readFile(`${STATIC_ROOT}app.js`, "utf8");
const stylesheet = await readFile(`${STATIC_ROOT}app.css`, "utf8");

test("every element id the app looks up exists in the page markup", () => {
  const pageIds = new Set(htmlIds(html));
  // chat-input is looked up by the test fixture; the rest come from the page.
  const fixtureIds = new Set(["tool-call-count", "chat-input"]);
  const missing = [];
  for (const match of appSource.matchAll(/querySelector\("#([\w-]+)"\)/g)) {
    const id = match[1];
    if (!pageIds.has(id) && !fixtureIds.has(id)) missing.push(id);
  }
  assert.deepEqual(missing, []);
  assert.ok(pageIds.has("detail-markdown"));
  assert.ok(pageIds.has("settings-dialog"));
});

test("page labels point at existing controls", () => {
  const pageIds = new Set(htmlIds(html));
  const missing = [];
  for (const match of html.matchAll(/\bfor="([\w-]+)"/g)) {
    if (!pageIds.has(match[1])) missing.push(match[1]);
  }
  assert.deepEqual(missing, []);
});

test("the settings trigger is wired to the dialog it controls", () => {
  assert.equal(attributeValue(html, "open-settings", "aria-controls"), "settings-dialog");
  assert.equal(attributeValue(html, "open-settings", "aria-haspopup"), "dialog");
  assert.ok(html.includes('id="settings-dialog" class="settings-dialog"'));
  assert.ok(html.includes('class="settings-header"'));
});

test("the markdown script loads before the application script", () => {
  const markdownIndex = html.indexOf("markdown-render.js");
  const appIndex = html.indexOf("/static/app.js");
  assert.ok(markdownIndex > -1 && appIndex > -1);
  assert.ok(markdownIndex < appIndex, "the renderer must be registered before app.js runs");
});

test("preferences no longer sit in the conversation grid", () => {
  assert.equal(html.includes('class="preferences section-panel"'), false);
  assert.equal(stylesheet.includes(".primary-grid"), false);
  assert.equal(stylesheet.includes(".chat-workspace"), false);
  assert.ok(stylesheet.includes(".workspace-layout"));
});

test("the session rail and the conversation share one fixed column height", () => {
  // Regression guard: a long session list must scroll inside the rail instead
  // of stretching the page, and a single session must not leave the rail short.
  assert.ok(stylesheet.includes("--column-height:"));
  assert.ok(stylesheet.includes("grid-auto-rows: minmax(var(--column-height-min), var(--column-height))"));
  assert.ok(stylesheet.includes(".session-list { flex: 1 1 auto; min-height: 0;"));
  assert.ok(stylesheet.includes(".chat-messages {"));
  assert.equal(stylesheet.includes("max-height: calc(100vh - 320px)"), false);
  assertsRailScrolls();
});

function assertsRailScrolls() {
  const list = /\.session-list \{([^}]*)\}/.exec(stylesheet);
  assert.ok(list, "expected a .session-list rule");
  assert.ok(list[1].includes("overflow-y: auto"), "the rail list must scroll internally");
  assert.equal(list[1].includes("max-height"), false, "the rail height comes from the shared grid row");
  const messages = /\.chat-messages \{([^}]*)\}/.exec(stylesheet);
  assert.ok(messages, "expected a .chat-messages rule");
  assert.ok(messages[1].includes("flex: 1 1 auto"));
  assert.equal(messages[1].includes("min-height: 300px"), false);
}

// ---------------------------------------------------------------------------
// Startup behaviour
// ---------------------------------------------------------------------------

test("startup loads preferences, sessions and reports into the page", async () => {
  const harness = await startApp({ storedSession: "s1" });
  try {
    const { document } = harness;
    assert.equal(document.querySelector("#preferences-form").elements.display_name.value, "测试用户");
    assert.equal(document.querySelector("#preferences-form").elements.topics.value, "人工智能");
    assert.equal(document.querySelector("#sessions-status").textContent, "1 个会话");
    const sessionItem = document.querySelectorAll(".session-item")[0];
    assert.ok(sessionItem, "expected one rendered session");
    assert.ok(collectText(sessionItem).includes("首条消息标题"));
    assert.equal(document.querySelector("#reports-status").textContent, "1 份日报");
    assert.equal(document.querySelector("#chat-session-label").textContent, "首条消息标题");
    assert.equal(harness.storage.get("agentnews.currentSessionId"), "s1");
    assert.equal(harness.callsFor("/api/sessions?status=active&limit=100&offset=0").length, 1);
  } finally {
    harness.restore();
  }
});

test("a stale stored session id falls back to an empty conversation", async () => {
  const harness = await startApp({
    storedSession: "gone",
    responses: {
      "GET /api/sessions/gone": {
        status: 404,
        __body: { error: { code: "session_not_found", message: "会话不存在" } },
      },
    },
  });
  try {
    assert.equal(harness.document.querySelector("#chat-session-label").textContent, "尚未选择会话");
    assert.equal(harness.storage.get("agentnews.currentSessionId"), undefined);
  } finally {
    harness.restore();
  }
});

// ---------------------------------------------------------------------------
// Report detail: markdown rendering and raw toggle
// ---------------------------------------------------------------------------

test("opening a report renders markdown structure instead of raw text", async () => {
  const harness = await startApp({ storedSession: "s1" });
  try {
    const { document } = harness;
    const reportItem = document.querySelectorAll(".report-item")[0];
    assert.ok(reportItem, "expected one rendered report");
    reportItem.querySelector(".report-select").click();
    await settle();
    const detail = document.querySelector("#detail-markdown");
    const tags = tagNames(detail);
    assert.ok(tags.includes("H1"), `expected a heading, got ${tags.join(",")}`);
    assert.ok(tags.includes("UL"));
    assert.ok(tags.includes("TABLE"));
    assert.equal(detail.firstChild.tagName, "H1");
    assert.equal(document.querySelector("#report-detail").hidden, false);
    assert.equal(document.querySelector("#toggle-detail-markdown").textContent, "查看原文");
  } finally {
    harness.restore();
  }
});

function openFirstReport(document) {
  const item = document.querySelectorAll(".report-item")[0];
  assert.ok(item, "expected a rendered report to open");
  item.querySelector(".report-select").click();
}

test("the raw toggle swaps between rendered markdown and source text", async () => {
  const harness = await startApp({ storedSession: "s1" });
  try {
    const { document } = harness;
    openFirstReport(document);
    await settle();
    const toggle = document.querySelector("#toggle-detail-markdown");
    const detail = document.querySelector("#detail-markdown");

    toggle.click();
    assert.equal(toggle.getAttribute("aria-pressed"), "true");
    assert.equal(toggle.textContent, "查看排版");
    assert.deepEqual(tagNames(detail), ["PRE"]);
    assert.equal(collectText(detail), REPORT_MARKDOWN);

    toggle.click();
    assert.equal(toggle.getAttribute("aria-pressed"), "false");
    assert.ok(tagNames(detail).includes("H1"));
  } finally {
    harness.restore();
  }
});

test("source markdown renders in the viewer and closes again", async () => {
  const harness = await startApp({ storedSession: "s1" });
  try {
    const { document } = harness;
    openFirstReport(document);
    await settle();
    const archiveButton = document.querySelectorAll(".source-actions button")[0];
    assert.ok(archiveButton, "expected an archive button for the source");
    archiveButton.click();
    await settle();
    assert.equal(document.querySelector("#source-viewer").hidden, false);
    const source = document.querySelector("#source-markdown");
    assert.equal(tagNames(source)[0], "H2");
    assert.equal(document.querySelector("#source-original-url").href, "https://example.com/a");

    document.querySelector("#toggle-source-markdown").click();
    assert.deepEqual(tagNames(source), ["PRE"]);
    document.querySelector("#close-source").click();
    assert.equal(document.querySelector("#source-viewer").hidden, true);
  } finally {
    harness.restore();
  }
});

// ---------------------------------------------------------------------------
// Settings drawer
// ---------------------------------------------------------------------------

test("the settings drawer opens from the topbar and closes back to it", async () => {
  const harness = await startApp({ storedSession: "s1" });
  try {
    const { document } = harness;
    const dialog = document.querySelector("#settings-dialog");
    const trigger = document.querySelector("#open-settings");
    assert.equal(dialog.open, false);

    trigger.click();
    assert.equal(dialog.open, true);

    document.querySelector("#close-settings").click();
    assert.equal(dialog.open, false);
    assert.equal(document.activeElement, trigger, "focus should return to the trigger");
  } finally {
    harness.restore();
  }
});

test("saving preferences sends the drawer payload to the API", async () => {
  const harness = await startApp({
    storedSession: "s1",
    responses: { "PUT /api/settings": { __body: { ...SETTINGS, display_name: "新名字" } } },
  });
  try {
    const { document } = harness;
    const form = document.querySelector("#preferences-form");
    form.elements.display_name.value = "新名字";
    form.elements.daily_send_enabled.checked = true;
    form.dispatch("submit");
    await settle();
    assert.equal(document.querySelector("#preferences-status").textContent, "已保存");
    const saved = harness.callsFor("/api/settings").find((call) => call.method === "PUT");
    assert.ok(saved);
    const payload = JSON.parse(saved.body);
    assert.equal(payload.display_name, "新名字");
    assert.equal(payload.daily_send_enabled, true);
    assert.deepEqual(payload.topics, ["人工智能"]);
  } finally {
    harness.restore();
  }
});

test("a failed preference save is reported without leaking internals", async () => {
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      "PUT /api/settings": {
        status: 500,
        __body: { error: { code: "internal_error", message: "服务暂时不可用，请稍后重试。" } },
      },
    },
  });
  try {
    const { document } = harness;
    document.querySelector("#preferences-form").dispatch("submit");
    await settle();
    assert.equal(document.querySelector("#preferences-status").textContent, "失败");
    assert.equal(document.querySelector("#preferences-error").hidden, false);
    assert.equal(
      collectText(document.querySelector("#preferences-error")),
      "服务暂时不可用，请稍后重试。",
    );
  } finally {
    harness.restore();
  }
});

// ---------------------------------------------------------------------------
// Status and error states
// ---------------------------------------------------------------------------

test("report list failures surface a recoverable error", async () => {
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      "GET /api/reports": {
        status: 500,
        __body: { error: { code: "internal_error", message: "服务暂时不可用，请稍后重试。" } },
      },
    },
  });
  try {
    assert.equal(harness.document.querySelector("#reports-status").textContent, "读取失败");
    assert.equal(harness.document.querySelector("#reports-error").hidden, false);
  } finally {
    harness.restore();
  }
});

test("an archived session is read-only, restore works and startup ignores stale active state", async () => {
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      "GET /api/sessions": {
        __body: { items: [], limit: 100, offset: 0 },
      },
      "GET /api/sessions?status=archived&limit=100&offset=0": {
        __body: { items: [SESSION_ARCHIVED], limit: 100, offset: 0 },
      },
      "GET /api/sessions/s1": { __body: SESSION_ARCHIVED },
    },
  });
  try {
    const { document } = harness;
    // The stored id points at an archived session: startup must not resume it.
    assert.equal(document.querySelector("#chat-session-label").textContent, "尚未选择会话");
    assert.equal(document.querySelector("#chat-input").disabled, false);

    document.querySelector("#sessions-archived").click();
    await settle();
    assert.equal(document.querySelector("#sessions-status").textContent, "1 个会话");
    document.querySelectorAll(".session-item")[0].querySelector(".session-select").click();
    await settle();
    assert.equal(document.querySelector("#chat-session-label").textContent, "首条消息标题");
    assert.equal(document.querySelector("#chat-input").disabled, true);
    assert.equal(document.querySelector("#restore-current-session").hidden, false);
    assert.equal(document.querySelector("#chat-status").textContent, "归档会话（只读）");
  } finally {
    harness.restore();
  }
});

test("a failed run shows a safe message and re-enables input", async () => {
  const approval = {
    id: "a1",
    command: "Write-Output 'ok'",
    explanation: "读取一条测试输出",
    risk_summary: "仅执行只读命令",
    working_directory: "D:\\workspace",
    timeout_seconds: 60,
    output_limit_bytes: 1048576,
  };
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      "POST /api/chat": {
        status: 202,
        __body: { run_id: "r1", session_id: "s1", status: "pending_approval", approval },
      },
      "POST /api/chat/runs/r1/approvals/a1": {
        status: 202,
        __body: { run_id: "r1", session_id: "s1", status: "pending_approval", approval },
      },
      // The run finished in the background while the page was polling.
      "GET /api/chat/runs/r1": {
        __body: {
          run_id: "r1",
          session_id: "s1",
          status: "failed",
          error: { code: "agent_error", message: "Agent 暂时无法处理该请求，请稍后重试。" },
        },
      },
    },
  });
  try {
    const { document } = harness;
    document.querySelector("#chat-input").value = "写一份日报";
    document.querySelector("#chat-form").dispatch("submit");
    await settle();
    harness.timers.forEach((timer) => timer());
    await settle();
    assert.equal(document.querySelector("#chat-status").textContent, "失败");
    assert.equal(document.querySelector("#chat-error").hidden, false);
    assert.equal(
      collectText(document.querySelector("#chat-error")),
      "Agent 暂时无法处理该请求，请稍后重试。",
    );
    assert.equal(document.querySelector("#chat-input").disabled, false);
    assert.equal(harness.callsFor("/api/chat/runs/r1").length, 1);
  } finally {
    harness.restore();
  }
});

test("an approval request blocks input, resumes the run and completes", async () => {
  const approval = {
    id: "a1",
    command: "Write-Output 'ok'",
    explanation: "读取一条测试输出",
    risk_summary: "仅执行只读命令",
    working_directory: "D:\\workspace",
    interpreter: "Windows PowerShell 5.1 (C:\\windows\\System32\\WindowsPowerShell\\v1.0\\powershell.EXE)",
    timeout_seconds: 60,
    output_limit_bytes: 1048576,
  };
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      "POST /api/chat": {
        status: 202,
        __body: { run_id: "r1", session_id: "s1", status: "pending_approval", approval },
      },
      "POST /api/chat/runs/r1/approvals/a1": {
        status: 202,
        __body: { run_id: "r1", session_id: "s1", status: "running" },
      },
      "GET /api/chat/runs/r1": {
        __body: {
          run_id: "r1",
          session_id: "s1",
          status: "completed",
          result: { session_id: "s1", answer: "已执行", tool_calls: 2, report_ids: [] },
        },
      },
    },
  });
  try {
    const { document } = harness;
    const dialog = document.querySelector("#bash-approval-dialog");
    document.querySelector("#chat-input").value = "执行测试命令";
    document.querySelector("#chat-form").dispatch("submit");
    await settle();
    assert.equal(dialog.open, true);
    assert.equal(document.querySelector("#bash-approval-command").textContent, "Write-Output 'ok'");
    assert.equal(document.querySelector("#bash-approval-cwd").textContent, "D:\\workspace");
    assert.equal(
      document.querySelector("#bash-interpreter").textContent,
      "Windows PowerShell 5.1 (C:\\windows\\System32\\WindowsPowerShell\\v1.0\\powershell.EXE)",
    );
    // The input stays typable; only sending is blocked for this conversation.
    assert.equal(document.querySelector("#chat-input").disabled, false);
    assert.equal(document.querySelector("#chat-form button[type=submit]").disabled, true);
    assert.equal(document.querySelector("#chat-status").textContent, "等待人工审核");

    document.querySelector("#bash-approve").click();
    await settle();
    harness.timers.forEach((timer) => timer());
    await settle();
    assert.equal(dialog.open, false);
    assert.equal(document.querySelector("#chat-status").textContent, "完成 · 2 次工具调用");
    assert.equal(document.querySelector("#tool-call-count").textContent, "2");
    assert.equal(document.querySelector("#chat-form button[type=submit]").disabled, false);
    assert.equal(harness.callsFor("/api/chat/runs/r1/approvals/a1").length, 1);
    assert.ok(harness.callsFor("/api/chat/runs/r1").length >= 1);
  } finally {
    harness.restore();
  }
});

test("a running conversation shows elapsed time and keeps the final duration", async () => {
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      "POST /api/chat": {
        status: 202,
        __body: {
          run_id: "r1",
          session_id: "s1",
          status: "running",
          elapsed_ms: 3400,
        },
      },
      "GET /api/chat/runs/r1": {
        __body: {
          run_id: "r1",
          session_id: "s1",
          status: "completed",
          elapsed_ms: 5200,
          result: { session_id: "s1", answer: "已执行", tool_calls: 2, report_ids: [] },
        },
      },
    },
  });
  try {
    const { document } = harness;
    document.querySelector("#chat-input").value = "执行一个耗时任务";
    document.querySelector("#chat-form").dispatch("submit");
    await settle();
    const timer = document.querySelector("#chat-run-timer");
    assert.equal(timer.hidden, false);
    assert.equal(timer.getAttribute("data-state"), "running");
    assert.match(document.querySelector("#chat-run-timer-value").textContent, /^已等待 3\.\d 秒$/);

    harness.timers.forEach((handler) => handler());
    await settle();
    assert.equal(timer.hidden, false);
    assert.equal(timer.getAttribute("data-state"), "completed");
    assert.equal(document.querySelector("#chat-run-timer-value").textContent, "本轮用时 5.2 秒");
    assert.equal(document.querySelector("#chat-status").textContent, "完成 · 2 次工具调用");
  } finally {
    harness.restore();
  }
});

test("an agent error reply is surfaced without leaking internals", async () => {
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      "POST /api/chat": {
        status: 502,
        error: { code: "agent_error", message: "Agent 暂时无法处理该请求，请稍后重试。" },
      },
    },
  });
  try {
    const { document } = harness;
    document.querySelector("#chat-input").value = "写一份日报";
    document.querySelector("#chat-form").dispatch("submit");
    await settle();
    assert.equal(document.querySelector("#chat-status").textContent, "失败");
    assert.equal(
      collectText(document.querySelector("#chat-error")),
      "Agent 暂时无法处理该请求，请稍后重试。",
    );
  } finally {
    harness.restore();
  }
});

test("digest generation reports success and reloads the list", async () => {
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      "POST /api/digest/run-once": {
        __body: { status: "completed", report_id: 1, title: "新日报", source_count: 3 },
      },
    },
  });
  try {
    const { document } = harness;
    document.querySelector("#run-digest").click();
    await settle();
    assert.equal(document.querySelector("#digest-status").textContent, "完成");
    assert.equal(
      collectText(document.querySelector("#digest-message")),
      "已生成「新日报」，共 3 个来源。",
    );
    assert.ok(harness.callsFor("/api/reports?limit=10&offset=0").length >= 2, "list reloads");
  } finally {
    harness.restore();
  }
});

test("session actions keep their API contract", async () => {
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      "POST /api/sessions/s1/archive": { __body: { ...SESSION, status: "archived" } },
      "POST /api/sessions": { __body: { id: "s2", title: "未命名会话", status: "active" } },
      "GET /api/sessions/s2": {
        __body: { id: "s2", title: "未命名会话", status: "active", messages: [] },
      },
    },
  });
  try {
    const { document } = harness;
    const item = document.querySelectorAll(".session-item")[0];
    const archiveButton = item.querySelectorAll(".session-action")[1];
    archiveButton.click();
    await settle();
    assert.equal(harness.callsFor("/api/sessions/s1/archive").length, 1);
    assert.equal(document.querySelector("#chat-session-label").textContent, "尚未选择会话");

    document.querySelector("#new-session").click();
    await settle();
    assert.equal(
      harness.callsFor("/api/sessions").some((call) => call.method === "POST"),
      true,
    );
    assert.equal(document.querySelector("#chat-session-label").textContent, "未命名会话");
  } finally {
    harness.restore();
  }
});

test("assistant replies render markdown while user input stays literal", async () => {
  const userText = "看 **这个** 与 <img src=x>";
  const answer = "**重点**：见 [来源](https://example.com/a)\n\n- 一\n- 二\n\n<script>alert(1)</script>";
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      "GET /api/sessions/s1": {
        __body: {
          ...SESSION,
          messages: [
            { role: "user", content: userText },
            { role: "assistant", content: answer },
          ],
        },
      },
    },
  });
  try {
    const { document } = harness;
    const bodies = document.querySelectorAll(".message-body");
    assert.equal(bodies.length, 2);

    // The user's own text is never reinterpreted as Markdown.
    assert.equal(collectText(bodies[0]), userText);
    assert.deepEqual(tagNames(bodies[0]), []);

    // The assistant reply becomes structure inside a compact markdown bubble.
    const tags = tagNames(bodies[1]);
    for (const tag of ["P", "STRONG", "A", "UL", "LI"]) {
      assert.ok(tags.includes(tag), `expected a ${tag} element in the reply`);
    }
    const anchor = bodies[1].querySelector("a");
    assert.ok(anchor);
    assert.equal(anchor.getAttribute("href"), "https://example.com/a");
    assert.equal(anchor.getAttribute("target"), "_blank");
    assert.equal(anchor.getAttribute("rel"), "noopener noreferrer");
    assert.equal(tags.includes("SCRIPT"), false);
    assert.ok(collectText(bodies[1]).includes("<script>alert(1)</script>"));
    assert.ok(stylesheet.includes(".message-body.markdown-body"));
  } finally {
    harness.restore();
  }
});

test("stored content is never injected as markup", async () => {
  const harness = await startApp({ storedSession: "s1" });
  try {
    assert.equal(typeof harness.document.createElement("div").insertAdjacentHTML, "undefined");
    assert.equal(appSource.toLowerCase().includes("innerhtml"), false);
  } finally {
    harness.restore();
  }
});

// ---------------------------------------------------------------------------
// Keyboard: Enter sends, Shift+Enter and IME composition do not
// ---------------------------------------------------------------------------

test("Enter sends the message and Shift+Enter does not", async () => {
  const harness = await startApp({
    storedSession: "s1",
    responses: { "POST /api/chat": { __body: { session_id: "s1", answer: "完成", tool_calls: 0 } } },
  });
  try {
    const { document } = harness;
    const input = document.querySelector("#chat-input");

    input.value = "回车换行的内容";
    input.press("Enter", { shiftKey: true });
    await settle();
    assert.equal(harness.callsFor("/api/chat").length, 0, "Shift+Enter must not send");
    assert.equal(input.value, "回车换行的内容");

    input.press("Enter");
    await settle();
    assert.equal(harness.callsFor("/api/chat").length, 1);
    assert.equal(input.value, "");
  } finally {
    harness.restore();
  }
});

test("an input-method candidate never sends the message", async () => {
  const harness = await startApp({
    storedSession: "s1",
    responses: { "POST /api/chat": { __body: { session_id: "s1", answer: "完成", tool_calls: 0 } } },
  });
  try {
    const { document } = harness;
    const input = document.querySelector("#chat-input");
    input.value = "拼音候选";

    input.press("Enter", { isComposing: true, keyCode: 0 });
    await settle();
    assert.equal(harness.callsFor("/api/chat").length, 0, "isComposing must block Enter");

    input.press("Enter", { keyCode: 229 });
    await settle();
    assert.equal(harness.callsFor("/api/chat").length, 0, "keyCode 229 must block Enter");
  } finally {
    harness.restore();
  }
});

test("blank input is not sent by Enter", async () => {
  const harness = await startApp({ storedSession: "s1" });
  try {
    const input = harness.document.querySelector("#chat-input");
    input.value = "   ";
    input.press("Enter");
    await settle();
    assert.equal(harness.callsFor("/api/chat").length, 0);
  } finally {
    harness.restore();
  }
});

// ---------------------------------------------------------------------------
// Concurrency: one run per session, the rest of the workspace stays usable
// ---------------------------------------------------------------------------

const APPROVAL = {
  id: "a1",
  command: "Write-Output 'ok'",
  explanation: "读取一条测试输出",
  risk_summary: "仅执行只读命令",
  working_directory: "D:\\workspace",
  timeout_seconds: 60,
  output_limit_bytes: 1048576,
};

function secondSessionResponses() {
  return {
    "GET /api/sessions": {
      __body: {
        items: [SESSION, { id: "s2", title: "第二个会话", status: "active", updated_at: SESSION.updated_at }],
        limit: 100,
        offset: 0,
      },
    },
    "GET /api/sessions/s2": {
      __body: { id: "s2", title: "第二个会话", status: "active", messages: [] },
    },
  };
}

test("a running session does not lock the session list", async () => {
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      ...secondSessionResponses(),
      "POST /api/chat": {
        status: 202,
        __body: { run_id: "r1", session_id: "s1", status: "pending_approval", approval: APPROVAL },
      },
      "POST /api/sessions": { __body: { id: "s3", title: "新会话", status: "active" } },
      "GET /api/sessions/s3": { __body: { id: "s3", title: "新会话", status: "active", messages: [] } },
    },
  });
  try {
    const { document } = harness;
    document.querySelector("#chat-input").value = "执行测试命令";
    document.querySelector("#chat-form").dispatch("submit");
    await settle();

    // The run is in flight: the rail is fully usable.
    assert.equal(document.querySelector("#new-session").disabled, false);
    assert.equal(document.querySelector("#sessions-archived").disabled, false);
    assert.equal(document.querySelector("#chat-form button[type=submit]").disabled, true);
    assert.equal(document.querySelector("#chat-input").disabled, false);

    // Switching to another session loads its own conversation.
    document.querySelectorAll(".session-item")[1].querySelector(".session-select").click();
    await settle();
    assert.equal(document.querySelector("#chat-session-label").textContent, "第二个会话");
    assert.equal(document.querySelector("#chat-form button[type=submit]").disabled, false);

    // The other session is not run-locked, so it can send its own message.
    document.querySelector("#chat-input").value = "另一条消息";
    document.querySelector("#chat-form").dispatch("submit");
    await settle();
    assert.equal(harness.callsFor("/api/chat").length, 2, "the second session may run in parallel");
    assert.equal(
      harness.callsFor("/api/chat").some((call) => JSON.parse(call.body).session_id === "s2"),
      true,
    );
  } finally {
    harness.restore();
  }
});

test("a finished background run does not overwrite the visible conversation", async () => {
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      ...secondSessionResponses(),
      "POST /api/chat": {
        status: 202,
        __body: { run_id: "r1", session_id: "s1", status: "pending_approval", approval: APPROVAL },
      },
      "GET /api/chat/runs/r1": {
        __body: {
          run_id: "r1",
          session_id: "s1",
          status: "completed",
          result: { session_id: "s1", answer: "已执行", tool_calls: 4, report_ids: [] },
        },
      },
    },
  });
  try {
    const { document } = harness;
    document.querySelector("#chat-input").value = "执行测试命令";
    document.querySelector("#chat-form").dispatch("submit");
    await settle();

    document.querySelectorAll(".session-item")[1].querySelector(".session-select").click();
    await settle();
    const before = document.querySelector("#chat-session-label").textContent;

    // Let the poller settle session s1's run while s2 is on screen.
    harness.timers.forEach((timer) => timer());
    await settle();
    assert.equal(document.querySelector("#chat-session-label").textContent, before);
    assert.notEqual(document.querySelector("#chat-status").textContent, "完成 · 4 次工具调用");
    assert.equal(
      harness.callsFor("/api/sessions/s1").length,
      1,
      "a background run must not reload the visible session",
    );
  } finally {
    harness.restore();
  }
});

// ---------------------------------------------------------------------------
// Report deletion
// ---------------------------------------------------------------------------

const SECOND_REPORT = {
  id: 2,
  date: "2026-10-17",
  title: "第二份日报",
  summary: "摘要二",
  source_count: 0,
};
const SECOND_REPORT_DETAIL = {
  id: 2,
  date: "2026-10-17",
  title: "第二份日报",
  summary: "摘要二",
  markdown: "# 第二份\n\n正文",
  sources: [],
  email_deliveries: [],
};

function twoReportResponses(overrides = {}) {
  let deleted = false;
  return {
    "GET /api/reports": () => ({
      __body: deleted ? { items: [SECOND_REPORT], limit: 10, offset: 0 } : { ...REPORT_LIST, items: [...REPORT_LIST.items, SECOND_REPORT] },
    }),
    "DELETE /api/reports/1": () => {
      deleted = true;
      return { status: 204 };
    },
    "GET /api/reports/2": { __body: SECOND_REPORT_DETAIL },
    ...overrides,
  };
}

test("deleting a report asks for confirmation and refreshes the list", async () => {
  const harness = await startApp({ storedSession: "s1", responses: twoReportResponses() });
  try {
    const { document } = harness;
    assert.equal(document.querySelectorAll(".report-item").length, 2);

    // Cancel first: no request, nothing removed.
    harness.windowMock.confirm = () => false;
    document.querySelectorAll(".report-delete, .report-action-delete")[0].click();
    await settle();
    assert.equal(harness.callsFor("/api/reports/1").some((call) => call.method === "DELETE"), false);
    assert.equal(document.querySelectorAll(".report-item").length, 2);

    // Confirm: the report is deleted and the list reloads.
    harness.windowMock.confirm = () => true;
    document.querySelectorAll(".report-action-delete")[0].click();
    await settle();
    assert.equal(harness.callsFor("/api/reports/1").some((call) => call.method === "DELETE"), true);
    assert.ok(harness.callsFor("/api/reports?limit=10&offset=0").length >= 2);
    assert.equal(document.querySelectorAll(".report-item").length, 1);
  } finally {
    harness.restore();
  }
});

test("deleting the last report returns to the empty detail state", async () => {
  let deleted = false;
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      "DELETE /api/reports/1": () => {
        deleted = true;
        return { status: 204 };
      },
      "GET /api/reports": () => ({ __body: deleted ? { items: [], limit: 10, offset: 0 } : REPORT_LIST }),
    },
  });
  try {
    const { document } = harness;
    openFirstReport(document);
    await settle();
    assert.equal(document.querySelector("#delete-report").hidden, false);

    document.querySelector("#delete-report").click();
    await settle();
    assert.equal(document.querySelectorAll(".report-item").length, 0);
    assert.equal(document.querySelector("#report-detail").hidden, true);
    assert.equal(document.querySelector("#detail-placeholder").hidden, false);
    assert.equal(document.querySelector("#delete-report").hidden, true);
  } finally {
    harness.restore();
  }
});

test("a failed report deletion is reported and the row stays", async () => {
  const harness = await startApp({
    storedSession: "s1",
    responses: {
      "DELETE /api/reports/1": {
        status: 500,
        __body: { error: { code: "internal_error", message: "服务暂时不可用，请稍后重试。" } },
      },
    },
  });
  try {
    const { document } = harness;
    document.querySelector(".report-action-delete").click();
    await settle();
    assert.equal(document.querySelector("#reports-error").hidden, false);
    assert.equal(collectText(document.querySelector("#reports-error")), "删除日报失败：服务暂时不可用，请稍后重试。");
    assert.equal(document.querySelectorAll(".report-item").length, 1);
  } finally {
    harness.restore();
  }
});
