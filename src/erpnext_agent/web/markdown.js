"use strict";

(function registerSafeMarkdown(globalScope) {
  function textNode(value) {
    return { type: "text", value };
  }

  function appendText(nodes, value) {
    if (!value) {
      return;
    }
    const previous = nodes.at(-1);
    if (previous?.type === "text") {
      previous.value += value;
    } else {
      nodes.push(textNode(value));
    }
  }

  function isWordCharacter(value) {
    return Boolean(value) && /[\p{L}\p{N}]/u.test(value);
  }

  function findEmphasisEnd(source, delimiter, start) {
    let index = source.indexOf(delimiter, start);
    while (index >= 0) {
      const before = source[index - 1];
      const after = source[index + delimiter.length];
      if (before && !/\s/.test(before)) {
        if (delimiter !== "_" || !isWordCharacter(after)) {
          return index;
        }
      }
      index = source.indexOf(delimiter, index + delimiter.length);
    }
    return -1;
  }

  function isSafeLink(value) {
    const target = String(value || "").trim();
    if (!target || /[\u0000-\u001f\u007f]/.test(target)) {
      return false;
    }
    if (target.startsWith("#") || target.startsWith("?") || target.startsWith("./") || target.startsWith("../")) {
      return true;
    }
    try {
      const url = new URL(target, "https://erpnext-agent.invalid/");
      return ["http:", "https:", "mailto:"].includes(url.protocol);
    } catch {
      return false;
    }
  }

  function parseInline(source) {
    const nodes = [];
    let index = 0;
    while (index < source.length) {
      if (source[index] === "\\" && index + 1 < source.length) {
        appendText(nodes, source[index + 1]);
        index += 2;
        continue;
      }

      if (source[index] === "`") {
        const tickCount = source[index + 1] === "`" ? 2 : 1;
        const delimiter = "`".repeat(tickCount);
        const end = source.indexOf(delimiter, index + tickCount);
        if (end >= 0) {
          nodes.push({
            type: "code",
            value: source.slice(index + tickCount, end).trim(),
          });
          index = end + tickCount;
          continue;
        }
      }

      const strongDelimiter = source.startsWith("**", index)
        ? "**"
        : source.startsWith("__", index)
          ? "__"
          : null;
      if (strongDelimiter) {
        const end = source.indexOf(strongDelimiter, index + 2);
        if (end > index + 2) {
          nodes.push({
            type: "strong",
            children: parseInline(source.slice(index + 2, end).trim()),
          });
          index = end + 2;
          continue;
        }
      }

      if (source.startsWith("~~", index)) {
        const end = source.indexOf("~~", index + 2);
        if (end > index + 2) {
          nodes.push({
            type: "delete",
            children: parseInline(source.slice(index + 2, end).trim()),
          });
          index = end + 2;
          continue;
        }
      }

      if (source[index] === "*" || source[index] === "_") {
        const delimiter = source[index];
        const previous = source[index - 1];
        const next = source[index + 1];
        if (!isWordCharacter(previous) && next && !/\s/.test(next)) {
          const end = findEmphasisEnd(source, delimiter, index + 1);
          if (end > index + 1) {
            nodes.push({
              type: "emphasis",
              children: parseInline(source.slice(index + 1, end)),
            });
            index = end + 1;
            continue;
          }
        }
      }

      if (source[index] === "[") {
        const labelEnd = source.indexOf("](", index + 1);
        const targetEnd = labelEnd >= 0 ? source.indexOf(")", labelEnd + 2) : -1;
        if (labelEnd > index + 1 && targetEnd > labelEnd + 2) {
          const label = source.slice(index + 1, labelEnd);
          const target = source.slice(labelEnd + 2, targetEnd).trim();
          if (isSafeLink(target)) {
            nodes.push({ type: "link", target, children: parseInline(label) });
          } else {
            nodes.push(...parseInline(label));
          }
          index = targetEnd + 1;
          continue;
        }
      }

      appendText(nodes, source[index]);
      index += 1;
    }
    return nodes;
  }

  function splitTableRow(line) {
    let value = line.trim();
    if (value.startsWith("|")) {
      value = value.slice(1);
    }
    if (value.endsWith("|")) {
      value = value.slice(0, -1);
    }
    const cells = [];
    let cell = "";
    let codeTicks = 0;
    for (let index = 0; index < value.length; index += 1) {
      const character = value[index];
      if (character === "\\" && value[index + 1] === "|") {
        cell += "|";
        index += 1;
      } else if (character === "`") {
        codeTicks = codeTicks ? 0 : 1;
        cell += character;
      } else if (character === "|" && !codeTicks) {
        cells.push(cell.trim());
        cell = "";
      } else {
        cell += character;
      }
    }
    cells.push(cell.trim());
    return cells;
  }

  function tableAlignment(value) {
    const cell = value.trim();
    if (!/^:?-{3,}:?$/.test(cell)) {
      return null;
    }
    if (cell.startsWith(":") && cell.endsWith(":")) {
      return "center";
    }
    if (cell.endsWith(":")) {
      return "right";
    }
    return "left";
  }

  function tableAt(lines, index) {
    if (index + 1 >= lines.length || !lines[index].includes("|")) {
      return null;
    }
    const headers = splitTableRow(lines[index]);
    const delimiter = splitTableRow(lines[index + 1]);
    if (headers.length < 2 || delimiter.length !== headers.length) {
      return null;
    }
    const alignments = delimiter.map(tableAlignment);
    return alignments.every(Boolean) ? { headers, alignments } : null;
  }

  function isBlockStart(lines, index) {
    const line = lines[index];
    return (
      !line.trim() ||
      /^\s*```/.test(line) ||
      /^\s{0,3}#{1,6}\s+/.test(line) ||
      /^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line) ||
      /^\s*>/.test(line) ||
      /^\s*[-+*]\s+/.test(line) ||
      /^\s*\d+[.)]\s+/.test(line) ||
      Boolean(tableAt(lines, index))
    );
  }

  function parseMarkdown(source) {
    const lines = String(source || "").replaceAll("\r\n", "\n").replaceAll("\r", "\n").split("\n");
    const blocks = [];
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      const fence = line.match(/^\s*```\s*([\w+-]*)\s*$/);
      if (fence) {
        const body = [];
        index += 1;
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
          body.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) {
          index += 1;
        }
        blocks.push({ type: "code", language: fence[1], value: body.join("\n") });
        continue;
      }

      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (heading) {
        blocks.push({
          type: "heading",
          level: heading[1].length,
          children: parseInline(heading[2]),
        });
        index += 1;
        continue;
      }

      if (/^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        blocks.push({ type: "rule" });
        index += 1;
        continue;
      }

      if (/^\s*>/.test(line)) {
        const quoteLines = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) {
          quoteLines.push(lines[index].replace(/^\s*>\s?/, ""));
          index += 1;
        }
        blocks.push({ type: "quote", children: parseMarkdown(quoteLines.join("\n")) });
        continue;
      }

      const table = tableAt(lines, index);
      if (table) {
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
          const row = splitTableRow(lines[index]);
          while (row.length < table.headers.length) {
            row.push("");
          }
          rows.push(row.slice(0, table.headers.length).map(parseInline));
          index += 1;
        }
        blocks.push({
          type: "table",
          headers: table.headers.map(parseInline),
          alignments: table.alignments,
          rows,
        });
        continue;
      }

      const unordered = line.match(/^\s*[-+*]\s+(.+)$/);
      const ordered = line.match(/^\s*(\d+)[.)]\s+(.+)$/);
      if (unordered || ordered) {
        const orderedList = Boolean(ordered);
        const items = [];
        const start = ordered ? Number(ordered[1]) : 1;
        while (index < lines.length) {
          const match = orderedList
            ? lines[index].match(/^\s*(\d+)[.)]\s+(.+)$/)
            : lines[index].match(/^\s*[-+*]\s+(.+)$/);
          if (!match) {
            break;
          }
          items.push(parseInline(match[orderedList ? 2 : 1]));
          index += 1;
        }
        blocks.push({ type: "list", ordered: orderedList, start, items });
        continue;
      }

      const paragraphLines = [];
      while (index < lines.length && (paragraphLines.length === 0 || !isBlockStart(lines, index))) {
        if (!lines[index].trim()) {
          break;
        }
        paragraphLines.push(parseInline(lines[index].trim()));
        index += 1;
      }
      blocks.push({ type: "paragraph", lines: paragraphLines });
    }
    return blocks;
  }

  function appendInline(parent, nodes, documentRef) {
    for (const node of nodes) {
      if (node.type === "text") {
        parent.appendChild(documentRef.createTextNode(node.value));
        continue;
      }
      const tags = {
        code: "code",
        strong: "strong",
        emphasis: "em",
        delete: "del",
        link: "a",
      };
      const element = documentRef.createElement(tags[node.type]);
      if (node.type === "code") {
        element.textContent = node.value;
      } else {
        appendInline(element, node.children, documentRef);
      }
      if (node.type === "link") {
        element.setAttribute("href", node.target);
        element.setAttribute("rel", "noopener noreferrer");
        if (/^(?:https?:)?\/\//i.test(node.target)) {
          element.setAttribute("target", "_blank");
        }
      }
      parent.appendChild(element);
    }
  }

  function appendInlineLines(parent, lines, documentRef) {
    lines.forEach((line, index) => {
      if (index) {
        parent.appendChild(documentRef.createElement("br"));
      }
      appendInline(parent, line, documentRef);
    });
  }

  function renderBlocks(parent, blocks, documentRef) {
    for (const block of blocks) {
      if (block.type === "paragraph") {
        const paragraph = documentRef.createElement("p");
        appendInlineLines(paragraph, block.lines, documentRef);
        parent.appendChild(paragraph);
      } else if (block.type === "heading") {
        const heading = documentRef.createElement(`h${block.level}`);
        appendInline(heading, block.children, documentRef);
        parent.appendChild(heading);
      } else if (block.type === "rule") {
        parent.appendChild(documentRef.createElement("hr"));
      } else if (block.type === "code") {
        const pre = documentRef.createElement("pre");
        const code = documentRef.createElement("code");
        code.textContent = block.value;
        if (block.language) {
          code.dataset.language = block.language;
        }
        pre.appendChild(code);
        parent.appendChild(pre);
      } else if (block.type === "quote") {
        const quote = documentRef.createElement("blockquote");
        renderBlocks(quote, block.children, documentRef);
        parent.appendChild(quote);
      } else if (block.type === "list") {
        const list = documentRef.createElement(block.ordered ? "ol" : "ul");
        if (block.ordered && block.start !== 1) {
          list.start = block.start;
        }
        for (const item of block.items) {
          const listItem = documentRef.createElement("li");
          appendInline(listItem, item, documentRef);
          list.appendChild(listItem);
        }
        parent.appendChild(list);
      } else if (block.type === "table") {
        const wrapper = documentRef.createElement("div");
        wrapper.className = "markdown-table-wrap";
        const table = documentRef.createElement("table");
        const head = documentRef.createElement("thead");
        const headRow = documentRef.createElement("tr");
        block.headers.forEach((header, index) => {
          const cell = documentRef.createElement("th");
          cell.style.textAlign = block.alignments[index];
          appendInline(cell, header, documentRef);
          headRow.appendChild(cell);
        });
        head.appendChild(headRow);
        table.appendChild(head);
        const body = documentRef.createElement("tbody");
        for (const row of block.rows) {
          const tableRow = documentRef.createElement("tr");
          row.forEach((value, index) => {
            const cell = documentRef.createElement("td");
            cell.style.textAlign = block.alignments[index];
            appendInline(cell, value, documentRef);
            tableRow.appendChild(cell);
          });
          body.appendChild(tableRow);
        }
        table.appendChild(body);
        wrapper.appendChild(table);
        parent.appendChild(wrapper);
      }
    }
  }

  function renderMarkdown(container, source) {
    const documentRef = container.ownerDocument;
    const fragment = documentRef.createDocumentFragment();
    renderBlocks(fragment, parseMarkdown(source), documentRef);
    container.replaceChildren(fragment);
  }

  const api = { isSafeLink, parseInline, parseMarkdown, renderMarkdown };
  globalScope.SafeMarkdown = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof window === "undefined" ? globalThis : window);
