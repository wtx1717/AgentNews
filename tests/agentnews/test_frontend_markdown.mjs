import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const SOURCE = fileURLToPath(new URL("../../src/agentnews/static/markdown-render.js", import.meta.url));

// Load the browser script with a tiny fake DOM so the module can be exercised
// without a browser. The fake tree keeps the assertions honest: it only records
// what the renderer actually creates.
class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument;
    this.attributes = new Map();
    this.children = [];
    this.textContent = "";
  }

  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
  }

  getAttribute(name) {
    const value = this.attributes.get(String(name));
    return value === undefined ? null : value;
  }

  appendChild(node) {
    this.children.push(node);
    return node;
  }

  removeChild(node) {
    const index = this.children.indexOf(node);
    if (index >= 0) this.children.splice(index, 1);
    return node;
  }

  get firstChild() {
    return this.children.length ? this.children[0] : null;
  }
}

function createDocument() {
  const document = {};
  document.createElement = (tagName) => new FakeElement(tagName, document);
  document.createTextNode = (value) => ({ nodeType: 3, textContent: String(value), children: [] });
  return document;
}

const scriptSource = await readFile(SOURCE, "utf8");
new Function(scriptSource)();
const markdown = globalThis.AgentNewsMarkdown;

function render(text, options) {
  const container = createDocument().createElement("div");
  markdown.renderMarkdownInto(container, text, options);
  return container;
}

function childrenByTag(node, tagName) {
  const wanted = String(tagName).toUpperCase();
  return node.children.filter((child) => child.tagName === wanted);
}

function collectText(node) {
  let output = node.textContent || "";
  for (const child of node.children) output += collectText(child);
  return output;
}

function collectTags(node, output = []) {
  for (const child of node.children) {
    if (!child.tagName) continue;
    output.push(child.tagName);
    collectTags(child, output);
  }
  return output;
}

test("the module exposes its public interface", () => {
  assert.equal(typeof markdown.parseMarkdownBlocks, "function");
  assert.equal(typeof markdown.parseInline, "function");
  assert.equal(typeof markdown.renderMarkdownInto, "function");
  assert.equal(typeof markdown.safeHttpUrl, "function");
  assert.equal(markdown.DEFAULT_MAX_BYTES, 204800);
});

test("ATX and setext headings are parsed with their levels", () => {
  const blocks = markdown.parseMarkdownBlocks("# 一级\n\n### 三级\n\n二级\n===\n\n## 带尾巴 ###");
  assert.deepEqual(
    blocks.map((block) => [block.type, block.level, block.text]),
    [
      ["heading", 1, "一级"],
      ["heading", 3, "三级"],
      ["heading", 1, "二级"],
      ["heading", 2, "带尾巴"],
    ],
  );
});

test("paragraphs join consecutive lines and stop at blank lines", () => {
  const blocks = markdown.parseMarkdownBlocks("第一行\n第二行\n\n第三行");
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].text, "第一行\n第二行");
  assert.equal(blocks[1].text, "第三行");
});

test("unordered and ordered lists keep every item", () => {
  const bullets = markdown.parseMarkdownBlocks("- 甲\n- 乙\n  - 丙");
  assert.equal(bullets.length, 1);
  assert.equal(bullets[0].type, "list");
  assert.equal(bullets[0].ordered, false);
  assert.deepEqual(bullets[0].items, ["甲", "乙", "丙"]);

  const numbers = markdown.parseMarkdownBlocks("3. 三\n4. 四");
  assert.equal(numbers[0].ordered, true);
  assert.equal(numbers[0].start, 3);
  assert.deepEqual(numbers[0].items, ["三", "四"]);
});

test("ordered detection wins over horizontal rules for 1. lines", () => {
  const blocks = markdown.parseMarkdownBlocks("1. 只有一条");
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].type, "list");
  assert.equal(blocks[0].ordered, true);
});

test("horizontal rules and fenced code blocks are recognised", () => {
  const blocks = markdown.parseMarkdownBlocks("段落\n\n***\n\n```python\nprint(1)\n```\n\n~~~\n原始代码\n~~~");
  const types = blocks.map((block) => block.type);
  assert.deepEqual(types, ["paragraph", "hr", "code", "code"]);
  assert.equal(blocks[2].language, "python");
  assert.equal(blocks[2].text, "print(1)");
  assert.equal(blocks[3].text, "原始代码");
});

test("an unclosed code fence swallows only the rest of the document", () => {
  const blocks = markdown.parseMarkdownBlocks("前置\n\n```txt\n未闭合代码\n\n仍在代码内");
  assert.deepEqual(blocks.map((block) => block.type), ["paragraph", "code"]);
  assert.equal(blocks[1].text, "未闭合代码\n\n仍在代码内");
});

test("blockquotes are parsed recursively", () => {
  const blocks = markdown.parseMarkdownBlocks("> 引用一\n> 引用二");
  assert.equal(blocks[0].type, "quote");
  assert.deepEqual(blocks[0].blocks.map((block) => block.text), ["引用一\n引用二"]);

  const nested = markdown.parseMarkdownBlocks("> # 标题");
  assert.equal(nested[0].blocks[0].type, "heading");
});

test("tables keep alignments and every row", () => {
  const blocks = markdown.parseMarkdownBlocks("| 名称 | 数量 |\n| :--- | ---: |\n| 甲 | 1 |\n| 乙 | 2 |");
  assert.equal(blocks.length, 1);
  assert.deepEqual(blocks[0].header, ["名称", "数量"]);
  assert.deepEqual(blocks[0].alignments, ["left", "right"]);
  assert.deepEqual(blocks[0].rows, [["甲", "1"], ["乙", "2"]]);
});

test("emphasis, code spans and links parse into inline nodes", () => {
  const nodes = markdown.parseInline("**粗** *斜* `码` [链接](https://example.com/a) 尾巴");
  const types = nodes.map((node) => node.type);
  assert.deepEqual(types, ["strong", "text", "em", "text", "code", "text", "link", "text"]);
  assert.equal(nodes[0].children[0].value, "粗");
  assert.equal(nodes[4].value, "码");
  assert.equal(nodes[6].href, "https://example.com/a");
  assert.equal(nodes[6].children[0].value, "链接");
});

test("raw HTML in stored markdown stays text", () => {
  const payload = "段落里含 <script>alert(1)</script> 与 <img src=x onerror=alert(2)>";
  const container = render(payload);
  const paragraph = container.children[0];
  assert.equal(paragraph.tagName, "P");
  assert.equal(collectText(container), payload);
  const tags = collectTags(container);
  assert.equal(tags.includes("SCRIPT"), false);
  assert.equal(tags.includes("IMG"), false);
  // A "link" the model never intended is still inert: no handler, no markup.
  for (const anchor of childrenByTag(paragraph, "A")) {
    assert.equal(anchor.getAttribute("rel"), "noopener noreferrer");
    assert.match(anchor.getAttribute("href"), /^https?:/);
  }
});

test("brackets inside a script tag create no executable markup", () => {
  const container = render("<script>alert(1)</script>");
  const tags = collectTags(container);
  assert.deepEqual(tags, ["P"]);
  assert.equal(collectText(container), "<script>alert(1)</script>");
});

test("non-http links degrade to plain text", () => {
  const nodes = markdown.parseInline("[点我](javascript:alert(1))");
  assert.deepEqual(nodes, [{ type: "text", value: "[点我](javascript:alert(1))" }]);
  assert.equal(markdown.safeHttpUrl("javascript:alert(1)"), null);
  assert.equal(markdown.safeHttpUrl("data:text/html;base64,AA"), null);
  assert.equal(markdown.safeHttpUrl("/static/app.js"), null);
  assert.equal(markdown.safeHttpUrl("https://example.com/x"), "https://example.com/x");
});

test("rendered links are hardened and anchors never execute", () => {
  const container = render("见 [文档](https://example.com/doc) 与 https://example.org/plain。");
  const anchors = childrenByTag(container.children[0], "A");
  assert.equal(anchors.length, 2);
  for (const anchor of anchors) {
    assert.equal(anchor.getAttribute("target"), "_blank");
    assert.equal(anchor.getAttribute("rel"), "noopener noreferrer");
  }
  assert.equal(anchors[0].href, "https://example.com/doc");
  assert.equal(anchors[1].href, "https://example.org/plain");
});

test("images become labelled links instead of remote requests", () => {
  const container = render("![封面图](https://example.com/cover.png)");
  const paragraph = container.children[0];
  assert.equal(childrenByTag(paragraph, "IMG").length, 0);
  const link = childrenByTag(paragraph, "A")[0];
  assert.ok(link);
  assert.equal(collectText(link), "封面图");
  const blocked = render("![图](javascript:alert(1))");
  assert.equal(childrenByTag(blocked.children[0], "A").length, 0);
});

test("the renderer builds the expected element structure", () => {
  const container = render("# 标题\n\n正文 **重点**\n\n- 甲\n- 乙\n\n```js\nlet a = 1;\n```\n| 列 |\n| --- |\n| 值 |\n\n> 引用");
  const tags = collectTags(container);
  for (const tag of ["H1", "P", "STRONG", "UL", "LI", "PRE", "TABLE", "THEAD", "TH", "TBODY", "TR", "TD", "BLOCKQUOTE"]) {
    assert.ok(tags.includes(tag), `expected a ${tag} element`);
  }
  assert.equal(collectText(container).includes("let a = 1;"), true);
  const table = childrenByTag(container, "TABLE")[0];
  const head = childrenByTag(table, "THEAD")[0];
  const row = childrenByTag(head, "TR")[0];
  assert.equal(childrenByTag(row, "TH")[0].getAttribute("data-align"), "left");
});

test("empty and whitespace-only input render a placeholder paragraph", () => {
  for (const value of ["", "   \n\n  "]) {
    const container = render(value);
    assert.equal(container.children.length, 1);
    assert.equal(container.children[0].tagName, "P");
    assert.equal(container.children[0].getAttribute("class"), "markdown-empty");
  }
});

test("oversized documents fall back to one preformatted block", () => {
  const container = render("# 标题\n\n正文".repeat(200), { maxBytes: 10 });
  assert.equal(container.children.length, 1);
  assert.equal(container.children[0].tagName, "PRE");
  assert.equal(collectTags(container).includes("H1"), false);
});

test("the node budget degrades to preformatted text instead of flooding the DOM", () => {
  const container = render("# 标题\n\n正文", { maxNodes: 1 });
  assert.equal(container.children.length, 1);
  assert.equal(container.children[0].tagName, "PRE");
});

test("rendering twice does not append duplicated content", () => {
  const container = createDocument().createElement("div");
  markdown.renderMarkdownInto(container, "# 一次");
  markdown.renderMarkdownInto(container, "# 两次");
  assert.equal(container.children.length, 1);
  assert.equal(collectText(container.children[0]), "两次");
});

test("the module source keeps the no-HTML-injection boundary", async () => {
  const source = (await readFile(SOURCE, "utf8")).toLowerCase();
  for (const forbidden of ["innerhtml", "insertadjacenthtml", "outerhtml", "document.write"]) {
    assert.equal(source.includes(forbidden), false, `unexpected ${forbidden} usage`);
  }
});

test("the static file has no unexpected exports for the browser", () => {
  assert.equal(scriptSource.includes("module.exports"), false);
  assert.equal(scriptSource.includes("export default"), false);
});
