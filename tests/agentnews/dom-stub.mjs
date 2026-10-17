/**
 * Minimal DOM stub shared by the frontend Node tests.
 *
 * It implements only the APIs the AgentNews static frontend uses, so the real
 * app.js / markdown-render.js files can run in Node without a browser.
 */

export class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.listeners = new Map();
    this.text = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.open = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.dateTime = "";
  }

  set id(value) {
    this.attributes.set("id", String(value));
  }

  get id() {
    return this.attributes.get("id") || "";
  }

  set name(value) {
    this.attributes.set("name", String(value));
  }

  get name() {
    return this.attributes.get("name") || "";
  }

  set className(value) {
    if (value) this.attributes.set("class", String(value));
    else this.attributes.delete("class");
  }

  get className() {
    return this.attributes.get("class") || "";
  }

  get classList() {
    const element = this;
    const read = () => String(element.className).split(/\s+/).filter(Boolean);
    const write = (values) => {
      const unique = [...new Set(values)];
      if (unique.length) element.setAttribute("class", unique.join(" "));
      else element.removeAttribute("class");
    };
    return {
      contains: (name) => read().includes(name),
      add: (name) => write([...read(), name]),
      remove: (name) => write(read().filter((item) => item !== name)),
      toggle: (name, force) => {
        const has = read().includes(name);
        const next = force === undefined ? !has : Boolean(force);
        if (next && !has) write([...read(), name]);
        if (!next && has) write(read().filter((item) => item !== name));
        return next;
      },
    };
  }

  set type(value) {
    this.attributes.set("type", String(value));
  }

  get type() {
    return this.attributes.get("type") || "";
  }

  set href(value) {
    this.attributes.set("href", String(value));
  }

  get href() {
    return this.attributes.get("href") || "";
  }

  /** data-* attributes, mirroring the DOMStringMap the frontend writes to. */  get dataset() {
    const element = this;
    return new Proxy(
      {},
      {
        get(_target, property) {
          return element.getAttribute(`data-${toDataAttribute(property)}`) ?? undefined;
        },
        set(_target, property, value) {
          element.setAttribute(`data-${toDataAttribute(property)}`, String(value));
          return true;
        },
        has(_target, property) {
          return element.hasAttribute(`data-${toDataAttribute(property)}`);
        },
      },
    );
  }

  set textContent(value) {
    this.text = String(value ?? "");
    this.children = [];
  }

  get textContent() {
    let output = this.text;
    for (const child of this.children) output += child.textContent;
    return output;
  }

  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
  }

  getAttribute(name) {
    if (!this.attributes.has(String(name))) return null;
    return this.attributes.get(String(name));
  }

  hasAttribute(name) {
    return this.attributes.has(String(name));
  }

  removeAttribute(name) {
    this.attributes.delete(String(name));
  }

  appendChild(node) {
    if (!node) return node;
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.children.push(node);
    return node;
  }

  append(...nodes) {
    for (const node of nodes) this.appendChild(node);
  }

  removeChild(node) {
    const index = this.children.indexOf(node);
    if (index >= 0) this.children.splice(index, 1);
    node.parentNode = null;
    return node;
  }

  replaceChildren(...nodes) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    for (const node of nodes) this.appendChild(node);
  }

  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  get firstChild() {
    return this.children.length ? this.children[0] : null;
  }

  get elements() {
    const named = {};
    for (const descendant of descendantsOf(this)) {
      const name = descendant.name;
      if (name) named[name] = descendant;
    }
    return named;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  dispatch(type, event = {}) {
    const payload = { type, target: this, preventDefault() {}, stopPropagation() {}, ...event };
    for (const handler of this.listeners.get(type) || []) handler(payload);
    return payload;
  }

  press(key, options = {}) {
    return this.dispatch("keydown", { key, shiftKey: false, isComposing: false, keyCode: 13, ...options });
  }

  requestSubmit() {
    this.dispatch("submit", {});
  }

  showModal() {
    if (this.open) throw new Error("dialog is already open");
    this.open = true;
    this.ownerDocument.activeElement = this;
  }

  close() {
    if (!this.open) return;
    this.open = false;
    this.dispatch("close", {});
  }

  click() {
    this.dispatch("click", {});
  }

  focus() {
    this.ownerDocument.activeElement = this;
  }

  scrollIntoView() {}

  matches(selector) {
    return matchesSelector(this, selector);
  }

  querySelector(selector) {
    return querySelectorAll(this, selector)[0] || null;
  }

  querySelectorAll(selector) {
    return querySelectorAll(this, selector);
  }
}

function toDataAttribute(property) {
  return String(property).replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
}

function descendantsOf(root) {
  const output = [];
  const walk = (node) => {
    for (const child of node.children) {
      if (child.nodeType === 3) continue;
      output.push(child);
      walk(child);
    }
  };
  walk(root);
  return output;
}

export function descendants(root) {
  return descendantsOf(root);
}

function parseSimpleSelector(part) {
  const withId = /^#([\w-]+)$/.exec(part);
  if (withId) return { id: withId[1] };
  const match = /^([a-zA-Z][\w-]*)?(?:\.([\w-]+))?(?:\[([\w:-]+)(?:=["']?([^\]"']*)["']?)?\])?$/.exec(part);
  if (!match) return null;
  return { tag: match[1] || null, className: match[2] || null, attr: match[3] || null, attrValue: match[4] };
}

function matchesSelector(element, selector) {
  const groups = String(selector).split(",").map((group) => group.trim()).filter(Boolean);
  for (const group of groups) {
    const parts = group.split(/\s+/).filter(Boolean);
    const last = parseSimpleSelector(parts[parts.length - 1]);
    if (!last || !matchesSimple(element, last)) continue;
    let ancestor = element.parentNode;
    let index = parts.length - 2;
    while (index >= 0 && ancestor) {
      const want = parseSimpleSelector(parts[index]);
      if (want && matchesSimple(ancestor, want)) index -= 1;
      ancestor = ancestor.parentNode;
    }
    if (index < 0) return true;
  }
  return false;
}

function matchesSimple(element, simple) {
  if (simple.id && element.id !== simple.id) return false;
  if (simple.tag && element.tagName !== simple.tag.toUpperCase()) return false;
  if (simple.className && !String(element.className).split(/\s+/).includes(simple.className)) return false;
  if (simple.attr && element.getAttribute(simple.attr) === null) return false;
  if (simple.attr && simple.attrValue !== undefined && String(element.getAttribute(simple.attr)) !== simple.attrValue) {
    return false;
  }
  return true;
}

function querySelectorAll(root, selector) {
  return descendantsOf(root).filter((element) => matchesSelector(element, selector));
}

export function createDocument() {
  const document = {
    activeElement: null,
    elementsById: new Map(),
    createElement(tagName) {
      return new FakeElement(tagName, document);
    },
    createElementNS(_namespace, tagName) {
      return new FakeElement(tagName, document);
    },
    createTextNode(value) {
      return { nodeType: 3, textContent: String(value ?? ""), children: [] };
    },
    register(element) {
      if (!element.id) return element;
      // One canonical element per id: drop earlier placeholders still in the tree
      // so the lookup and the rendered document never diverge.
      for (const other of document.querySelectorAll(`#${element.id}`)) {
        if (other !== element) other.remove();
      }
      document.elementsById.set(element.id, element);
      return element;
    },
    getElementById(id) {
      return document.elementsById.get(String(id)) || null;
    },
    querySelector(selector) {
      const value = String(selector).trim();
      if (/^#[\w-]+$/.test(value)) return document.getElementById(value.slice(1));
      return document.querySelectorAll(value)[0] || null;
    },
    querySelectorAll(selector) {
      return querySelectorAll(document.root, selector);
    },
  };
  document.root = new FakeElement("BODY", document);
  return document;
}

function build(document, tagName, attrs, children) {
  const element = document.createElement(tagName);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (name === "__document") continue;
    if (name === "id") element.id = value;
    else if (name === "name") element.name = value;
    else if (name === "class") element.className = value;
    else if (name === "text") element.text = String(value);
    else element.setAttribute(name, value);
  }
  for (const child of children) {
    if (typeof child === "string") element.appendChild(document.createTextNode(child));
    else element.appendChild(build(document, child[0], child[1], child.slice(2)));
  }
  return element;
}

/**
 * Build an element tree from a nested spec:
 * ["div", { id: "x", class: "y" }, ["span", { text: "hi" }]]
 */
export function el(document, tagName, attrs = {}, children = []) {
  return build(document, tagName, { ...attrs, __document: document }, children);
}

export function collectText(node) {
  if (!node) return "";
  if (node.nodeType === 3) return node.textContent;
  let output = node.text || "";
  for (const child of node.children) output += collectText(child);
  return output;
}

export function tagNames(node, output = []) {
  for (const child of node.children) {
    if (child.nodeType === 3) continue;
    output.push(child.tagName);
    tagNames(child, output);
  }
  return output;
}

/** Build the element skeleton the AgentNews page provides by id. */
export function buildPageSkeleton(document, ids) {
  for (const id of ids) {
    const element = document.createElement("div");
    element.id = id;
    document.root.appendChild(element);
    document.register(element);
  }
  return document;
}
