"use strict";

/**
 * Minimal Markdown renderer for AgentNews report and source content.
 *
 * Design rules (kept deliberately narrow):
 * - Every node is built with document.createElement and textContent; the
 *   module never assigns HTML, so raw HTML in stored Markdown stays text.
 * - Links accept only http/https; anything else degrades to plain text.
 * - Images never load remote resources; they render as a labelled link.
 * - Oversized input falls back to a single preformatted text block.
 */
(function () {
  "use strict";

  var DEFAULT_MAX_BYTES = 200 * 1024;
  var DEFAULT_MAX_NODES = 5000;

  var RE_FENCE = /^ {0,3}(`{3,}|~{3,})\s*([^`~]*)$/;
  var RE_ATX = /^ {0,3}(#{1,6})(\s+.*)?$/;
  var RE_HR = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
  var RE_QUOTE = /^ {0,3}>\s?(.*)$/;
  var RE_BULLET = /^ {0,3}[-*+]\s+(.*)$/;
  var RE_ORDERED = /^ {0,3}(\d{1,9})[.)]\s+(.*)$/;
  var RE_HEADING_UNDERLINE = /^ {0,3}=+\s*$/;
  var RE_TABLE_ROW = /^ {0,3}\|.*\|?\s*$/;
  var RE_TABLE_DIVIDER_CELL = /^:?-+:?$/;
  var RE_TRAILING_HASHES = /\s+#+\s*$/;
  var RE_QUOTE_PREFIX = /^ {0,3}>\s?/;
  var RE_TRAILING_URL_PUNCT = /[.,;:!?\u3001\u3002\uff01\uff0c\uff1a\uff1b\uff1f\u201d\u2019]+$/;
  var RE_TRAILING_BRACKET = /[)\]]$/;
  var RE_URL_BODY = /^https?:\/\/[^\s<>()\[\]"']+$/;

  var INLINE_PATTERNS = [
    { type: "code", re: /`([^`\n]+)`/ },
    { type: "image", re: /!\[([^\]]*)\]\(([^()\s]+)\)/ },
    { type: "link", re: /\[([\p{L}\p{N}][^\]]*)\]\(([^()\s]+)\)/u },
    { type: "strong", re: /\*\*([^*\n]+)\*\*/ },
    { type: "strike", re: /~~([^~\n]+)~~/ },
    { type: "em", re: /\*([^*\n]+)\*/ },
    { type: "autolink", re: /https?:\/\/[^\s<>()\[\]"'*_`]+/ },
  ];

  function isBlank(line) {
    return String(line).trim() === "";
  }

  function trimUrlTail(candidate) {
    var value = candidate;
    var previous = null;
    while (previous !== value) {
      previous = value;
      value = value.replace(RE_TRAILING_URL_PUNCT, "");
      if (value.endsWith(")")) {
        var opens = (value.match(/\(/g) || []).length;
        var closes = (value.match(/\)/g) || []).length;
        if (closes > opens) value = value.slice(0, -1);
      }
      if (value.endsWith("]")) value = value.slice(0, -1);
    }
    return value;
  }

  function asAutolink(candidate) {
    var value = trimUrlTail(candidate);
    if (!RE_URL_BODY.test(value)) return null;
    var href = safeHttpUrl(value);
    return href ? { href: href, value: value } : null;
  }

  function safeHttpUrl(value) {
    var text = String(value || "").trim();
    if (!text) return null;
    try {
      var url = new URL(text);
      return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
    } catch (error) {
      return null;
    }
  }

  function tableCells(line) {
    var value = line.trim();
    if (value.startsWith("|")) value = value.slice(1);
    if (value.endsWith("|")) value = value.slice(0, -1);
    return value.split("|").map(function (cell) {
      return cell.trim();
    });
  }

  function parseTableAlignments(line) {
    return tableCells(line).map(function (cell) {
      var left = cell.startsWith(":");
      var right = cell.endsWith(":");
      if (left && right) return "center";
      if (right) return "right";
      return "left";
    });
  }

  function matchFence(line) {
    var match = RE_FENCE.exec(line);
    if (!match) return null;
    var marker = match[1];
    return {
      marker: marker,
      info: (match[2] || "").trim(),
      char: marker.charAt(0),
      length: marker.length,
    };
  }

  function isTableDivider(line) {
    if (!line.includes("-")) return false;
    var cells = tableCells(line);
    if (!cells.length) return false;
    for (var i = 0; i < cells.length; i += 1) {
      if (!RE_TABLE_DIVIDER_CELL.test(cells[i])) return false;
    }
    return true;
  }

  function detectTable(lines, index) {
    if (index + 1 >= lines.length) return null;
    if (!RE_TABLE_ROW.test(lines[index])) return null;
    if (!isTableDivider(lines[index + 1])) return null;
    var header = tableCells(lines[index]);
    var alignments = parseTableAlignments(lines[index + 1]);
    if (!header.length || header.length !== alignments.length) return null;
    var rows = [];
    var cursor = index + 2;
    while (cursor < lines.length && !isBlank(lines[cursor]) && RE_TABLE_ROW.test(lines[cursor])) {
      rows.push(tableCells(lines[cursor]).slice(0, header.length));
      cursor += 1;
    }
    return { block: { type: "table", header: header, alignments: alignments, rows: rows }, next: cursor };
  }

  function isBlockStart(lines, index) {
    var line = lines[index];
    if (isBlank(line)) return true;
    if (matchFence(line)) return true;
    if (RE_ATX.test(line)) return true;
    if (RE_HR.test(line)) return true;
    if (RE_QUOTE.test(line)) return true;
    if (RE_BULLET.test(line)) return true;
    if (RE_ORDERED.test(line)) return true;
    if (detectTable(lines, index)) return true;
    return false;
  }

  function collectQuote(lines, index) {
    var inner = [];
    var cursor = index;
    while (cursor < lines.length) {
      var match = RE_QUOTE.exec(lines[cursor]);
      if (!match) {
        if (isBlank(lines[cursor]) && cursor + 1 < lines.length && RE_QUOTE.test(lines[cursor + 1])) {
          inner.push("");
          cursor += 1;
          continue;
        }
        break;
      }
      inner.push(match[1]);
      cursor += 1;
    }
    return { block: { type: "quote", blocks: parseMarkdownBlocks(inner.join("\n")) }, next: cursor };
  }

  function collectList(lines, index) {
    var first = RE_ORDERED.exec(lines[index]);
    var ordered = Boolean(first);
    var start = ordered ? Number(first[1]) : 1;
    var items = [];
    var cursor = index;
    while (cursor < lines.length) {
      var orderedMatch = ordered ? RE_ORDERED.exec(lines[cursor]) : null;
      var bulletMatch = ordered ? null : RE_BULLET.exec(lines[cursor]);
      var match = ordered ? orderedMatch : bulletMatch;
      if (!match) break;
      var content = ordered ? match[2] : match[1];
      cursor += 1;
      while (cursor < lines.length && !isBlank(lines[cursor]) && !isBlockStart(lines, cursor)) {
        content += "\n" + lines[cursor].trim();
        cursor += 1;
      }
      items.push(content);
    }
    return { block: { type: "list", ordered: ordered, start: start, items: items }, next: cursor };
  }

  function collectParagraph(lines, index) {
    var collected = "";
    var cursor = index;
    while (cursor < lines.length && !isBlank(lines[cursor])) {
      var line = lines[cursor];
      if (RE_HEADING_UNDERLINE.test(line)) {
        if (collected && collected.indexOf("\n") === -1) {
          return { block: { type: "heading", level: 1, text: collected.trim() }, next: cursor + 1 };
        }
        break;
      }
      if (cursor > index && detectTable(lines, cursor)) break;
      if (isBlockStart(lines, cursor)) break;
      collected = collected ? collected + "\n" + line : line;
      cursor += 1;
    }
    return { block: { type: "paragraph", text: collected }, next: cursor };
  }

  function parseMarkdownBlocks(text) {
    var source = typeof text === "string" ? text : "";
    var lines = source.replace(/\r\n?/g, "\n").split("\n");
    var blocks = [];
    var index = 0;

    while (index < lines.length) {
      var line = lines[index];
      if (isBlank(line)) {
        index += 1;
        continue;
      }

      var fence = matchFence(line);
      if (fence) {
        var code = [];
        index += 1;
        while (index < lines.length) {
          var closing = matchFence(lines[index]);
          var closes =
            closing !== null &&
            closing.char === fence.char &&
            closing.length >= fence.length &&
            closing.info === "";
          if (closes) break;
          code.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        blocks.push({ type: "code", language: fence.info, text: code.join("\n") });
        continue;
      }

      var atx = RE_ATX.exec(line);
      if (atx) {
        blocks.push({
          type: "heading",
          level: atx[1].length,
          text: (atx[2] || "").trim().replace(RE_TRAILING_HASHES, "").trim(),
        });
        index += 1;
        continue;
      }

      if (RE_HR.test(line)) {
        blocks.push({ type: "hr" });
        index += 1;
        continue;
      }

      if (RE_QUOTE.test(line)) {
        var quoted = collectQuote(lines, index);
        blocks.push(quoted.block);
        index = quoted.next;
        continue;
      }

      var table = detectTable(lines, index);
      if (table) {
        blocks.push(table.block);
        index = table.next;
        continue;
      }

      if (RE_BULLET.test(line) || RE_ORDERED.test(line)) {
        var list = collectList(lines, index);
        blocks.push(list.block);
        index = list.next;
        continue;
      }

      var paragraph = collectParagraph(lines, index);
      blocks.push(paragraph.block);
      if (paragraph.next === index) index += 1;
      else index = paragraph.next;
    }

    return blocks;
  }

  function parseInline(text) {
    var source = typeof text === "string" ? text : "";
    var nodes = [];
    var buffer = "";
    var index = 0;

    function flush() {
      if (buffer) {
        nodes.push({ type: "text", value: buffer });
        buffer = "";
      }
    }

    while (index < source.length) {
      var rest = source.slice(index);
      var consumed = 0;

      for (var i = 0; i < INLINE_PATTERNS.length; i += 1) {
        var pattern = INLINE_PATTERNS[i];
        if (!pattern.re.test(rest)) continue;
        var match = pattern.re.exec(rest);
        if (!match || match.index !== 0) continue;

        var node = null;
        if (pattern.type === "code") {
          node = { type: "code", value: match[1] };
        } else if (pattern.type === "strong") {
          node = { type: "strong", children: parseInline(match[1]) };
        } else if (pattern.type === "em") {
          node = { type: "em", children: parseInline(match[1]) };
        } else if (pattern.type === "strike") {
          node = { type: "strike", children: parseInline(match[1]) };
        } else if (pattern.type === "link" || pattern.type === "image") {
          var destination = match[2];
          if (
            destination.length > 1 &&
            destination.charAt(0) === "<" &&
            destination.charAt(destination.length - 1) === ">"
          ) {
            destination = destination.slice(1, -1);
          }
          var href = safeHttpUrl(destination);
          if (href) {
            node =
              pattern.type === "image"
                ? { type: "link", href: href, children: [{ type: "text", value: match[1] || match[2] }] }
                : { type: "link", href: href, children: parseInline(match[1]) };
          }
        } else if (pattern.type === "autolink") {
          var auto = asAutolink(match[0]);
          if (auto) {
            node = { type: "link", href: auto.href, children: [{ type: "text", value: auto.value }] };
            consumed = auto.value.length;
          }
        }

        if (node) {
          flush();
          nodes.push(node);
          consumed = match[0].length;
          break;
        }
      }

      if (consumed) index += consumed;
      else {
        buffer += source.charAt(index);
        index += 1;
      }
    }

    flush();
    return nodes;
  }

  function createElement(doc, tag, className, textContent) {
    var element = doc.createElement(tag);
    if (className) element.setAttribute("class", className);
    if (textContent !== undefined) element.textContent = textContent;
    return element;
  }

  function appendContainer(doc, parent, tagName, children) {
    var element = createElement(doc, tagName);
    appendInline(doc, element, children);
    parent.appendChild(element);
  }

  function appendInline(doc, parent, nodes) {
    for (var i = 0; i < nodes.length; i += 1) {
      var node = nodes[i];
      if (node.type === "text") parent.appendChild(doc.createTextNode(node.value));
      else if (node.type === "code") parent.appendChild(createElement(doc, "code", null, node.value));
      else if (node.type === "strong") appendContainer(doc, parent, "strong", node.children);
      else if (node.type === "em") appendContainer(doc, parent, "em", node.children);
      else if (node.type === "strike") appendContainer(doc, parent, "del", node.children);
      else if (node.type === "link") parent.appendChild(makeLink(doc, node));
    }
  }

  function makeLink(doc, node) {
    var href = safeHttpUrl(node.href);
    if (!href) return doc.createTextNode(plainText(node.children));
    var link = createElement(doc, "a");
    link.href = href;
    link.setAttribute("target", "_blank");
    link.setAttribute("rel", "noopener noreferrer");
    appendInline(doc, link, node.children);
    return link;
  }

  function plainText(nodes) {
    var parts = [];
    for (var i = 0; i < nodes.length; i += 1) {
      var node = nodes[i];
      if (node.type === "text" || node.type === "code") parts.push(node.value);
      else if (node.children) parts.push(plainText(node.children));
    }
    return parts.join("");
  }

  function renderInline(doc, parent, text) {
    appendInline(doc, parent, parseInline(text));
  }

  function renderBlock(doc, parent, block, budget) {
    if (budget.count >= budget.maxNodes) return;

    if (block.type === "heading") {
      var level = block.level >= 1 && block.level <= 6 ? block.level : 6;
      var heading = createElement(doc, "h" + level);
      renderInline(doc, heading, block.text);
      parent.appendChild(heading);
      budget.count += 1;
      return;
    }

    if (block.type === "paragraph") {
      var paragraph = createElement(doc, "p");
      renderInline(doc, paragraph, block.text);
      parent.appendChild(paragraph);
      budget.count += 1;
      return;
    }

    if (block.type === "code") {
      var pre = createElement(doc, "pre", "markdown-code");
      if (block.language) {
        var label = createElement(doc, "span", "markdown-code-language", block.language);
        pre.appendChild(label);
      }
      pre.appendChild(doc.createTextNode(block.text));
      parent.appendChild(pre);
      budget.count += 1;
      return;
    }

    if (block.type === "hr") {
      parent.appendChild(doc.createElement("hr"));
      budget.count += 1;
      return;
    }

    if (block.type === "quote") {
      var quote = createElement(doc, "blockquote");
      renderBlocks(doc, quote, block.blocks, budget);
      parent.appendChild(quote);
      budget.count += 1;
      return;
    }

    if (block.type === "list") {
      var list = doc.createElement(block.ordered ? "ol" : "ul");
      if (block.ordered && Number.isFinite(block.start) && block.start !== 1) {
        list.setAttribute("start", String(block.start));
      }
      for (var i = 0; i < block.items.length; i += 1) {
        var item = doc.createElement("li");
        appendInline(doc, item, parseInline(block.items[i]));
        list.appendChild(item);
      }
      parent.appendChild(list);
      budget.count += 1;
      return;
    }

    if (block.type === "table") {
      var table = createElement(doc, "table", "markdown-table");
      var head = doc.createElement("thead");
      var headRow = doc.createElement("tr");
      for (var h = 0; h < block.header.length; h += 1) {
        var headCell = doc.createElement("th");
        headCell.setAttribute("data-align", block.alignments[h] || "left");
        appendInline(doc, headCell, parseInline(block.header[h]));
        headRow.appendChild(headCell);
      }
      head.appendChild(headRow);
      table.appendChild(head);
      var body = doc.createElement("tbody");
      for (var r = 0; r < block.rows.length; r += 1) {
        var row = doc.createElement("tr");
        for (var c = 0; c < block.header.length; c += 1) {
          var cell = doc.createElement("td");
          cell.setAttribute("data-align", block.alignments[c] || "left");
          appendInline(doc, cell, parseInline(block.rows[r][c] || ""));
          row.appendChild(cell);
        }
        body.appendChild(row);
      }
      table.appendChild(body);
      parent.appendChild(table);
      budget.count += 1;
    }
  }

  function renderBlocks(doc, container, blocks, budget) {
    for (var i = 0; i < blocks.length; i += 1) {
      if (budget.count >= budget.maxNodes) break;
      renderBlock(doc, container, blocks[i], budget);
    }
  }

  function blockPlainText(block) {
    if (block.type === "heading" || block.type === "paragraph") return plainText(parseInline(block.text));
    if (block.type === "code") return block.text;
    if (block.type === "quote") return block.blocks.map(blockPlainText).join("\n");
    if (block.type === "list") {
      return block.items
        .map(function (item, position) {
          return (block.ordered ? String(block.start + position) + ". " : "- ") + plainText(parseInline(item));
        })
        .join("\n");
    }
    if (block.type === "table") {
      var lines = [block.header.map(plainText).join(" | ")];
      for (var i = 0; i < block.rows.length; i += 1) {
        lines.push(block.rows[i].map(plainText).join(" | "));
      }
      return lines.join("\n");
    }
    return "";
  }

  function blocksPlainText(blocks) {
    var parts = [];
    for (var i = 0; i < blocks.length; i += 1) {
      var text = blockPlainText(blocks[i]);
      if (text) parts.push(text);
    }
    return parts.join("\n\n");
  }

  function renderPlainInto(doc, container, text) {
    var pre = createElement(doc, "pre", "markdown-code");
    pre.appendChild(doc.createTextNode(text));
    container.appendChild(pre);
  }

  /**
   * Replace the container children with rendered Markdown.
   * Plain text stays plain: raw HTML in the source is never interpreted.
   */
  function renderMarkdownInto(container, text, options) {
    if (!container) return;
    var settings = options || {};
    var doc = container.ownerDocument || (typeof document !== "undefined" ? document : null);
    if (!doc) return;
    while (container.firstChild) container.removeChild(container.firstChild);

    var source = typeof text === "string" ? text : "";
    if (!source.trim()) {
      container.appendChild(createElement(doc, "p", "markdown-empty", "没有可显示的内容"));
      return;
    }

    var maxBytes = typeof settings.maxBytes === "number" ? settings.maxBytes : DEFAULT_MAX_BYTES;
    if (source.length > maxBytes) {
      renderPlainInto(doc, container, source);
      return;
    }

    var blocks = parseMarkdownBlocks(source);
    var budget = {
      count: 0,
      maxNodes: typeof settings.maxNodes === "number" ? settings.maxNodes : DEFAULT_MAX_NODES,
    };
    renderBlocks(doc, container, blocks, budget);
    if (budget.count >= budget.maxNodes) {
      while (container.firstChild) container.removeChild(container.firstChild);
      renderPlainInto(doc, container, blocksPlainText(blocks));
    }
  }

  var api = {
    parseMarkdownBlocks: parseMarkdownBlocks,
    parseInline: parseInline,
    renderMarkdownInto: renderMarkdownInto,
    safeHttpUrl: safeHttpUrl,
    plainText: plainText,
    DEFAULT_MAX_BYTES: DEFAULT_MAX_BYTES,
    DEFAULT_MAX_NODES: DEFAULT_MAX_NODES,
  };

  if (typeof globalThis !== "undefined") globalThis.AgentNewsMarkdown = api;
  if (typeof window !== "undefined") window.AgentNewsMarkdown = api;
})();
