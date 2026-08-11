"use strict";

const assert = require("node:assert/strict");
const {
  isSafeLink,
  parseInline,
  parseMarkdown,
  renderMarkdown,
} = require("../../src/erpnext_agent/web/markdown.js");

class FakeNode {
  constructor(tagName, ownerDocument, value = "") {
    this.tagName = tagName;
    this.ownerDocument = ownerDocument;
    this.value = value;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.className = "";
    this.start = 1;
  }

  appendChild(node) {
    this.children.push(node);
    return node;
  }

  replaceChildren(...nodes) {
    this.children = nodes.flatMap((node) =>
      node.tagName === "#fragment" ? node.children : [node],
    );
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
  }

  set textContent(value) {
    this.children = [this.ownerDocument.createTextNode(value)];
  }
}

class FakeDocument {
  createDocumentFragment() {
    return new FakeNode("#fragment", this);
  }

  createElement(tagName) {
    return new FakeNode(tagName.toUpperCase(), this);
  }

  createTextNode(value) {
    return new FakeNode("#text", this, value);
  }
}

function allTags(node) {
  return [node.tagName, ...node.children.flatMap(allTags)];
}

const stockReply = `"test item1" 的库存查询结果如下：

**物料信息： **
- Item Code: \`test item1\`
- 计量单位: Nos

**分仓库明细：**
| 仓库 | 公司 | 实际可用数量 | 预期数量 |
|---|---|---:|---:|
| 仓库 - rw | 锐雯 | 10.0 | 10.0 |`;

const blocks = parseMarkdown(stockReply);
assert.deepEqual(
  blocks.map((block) => block.type),
  ["paragraph", "paragraph", "list", "paragraph", "table"],
);
assert.equal(blocks[1].lines[0][0].type, "strong");
assert.equal(blocks[1].lines[0][0].children[0].value, "物料信息：");
assert.equal(blocks[2].items[0][1].type, "code");
assert.equal(blocks[4].headers.length, 4);
assert.equal(blocks[4].rows[0][0][0].value, "仓库 - rw");
assert.equal(blocks[4].alignments[2], "right");

const hostile = parseMarkdown('<img src=x onerror=alert(1)> [点击](javascript:alert(1))');
assert.equal(hostile[0].lines[0].some((node) => node.type === "link"), false);
assert.match(hostile[0].lines[0][0].value, /<img src=x/);
assert.equal(parseInline("actual_qty")[0].value, "actual_qty");
assert.equal(isSafeLink("javascript:alert(1)"), false);
assert.equal(isSafeLink("data:text/html,boom"), false);
assert.equal(isSafeLink("https://example.com/doc"), true);
assert.equal(isSafeLink("/app/item/ITEM-001"), true);

const documentRef = new FakeDocument();
const container = new FakeNode("DIV", documentRef);
renderMarkdown(container, stockReply);
const renderedTags = allTags(container);
assert.ok(renderedTags.includes("STRONG"));
assert.ok(renderedTags.includes("UL"));
assert.ok(renderedTags.includes("TABLE"));
assert.ok(renderedTags.includes("THEAD"));
assert.ok(renderedTags.includes("TBODY"));

const hostileContainer = new FakeNode("DIV", documentRef);
renderMarkdown(hostileContainer, '<img src=x onerror=alert(1)> [点击](javascript:alert(1))');
const hostileTags = allTags(hostileContainer);
assert.equal(hostileTags.includes("IMG"), false);
assert.equal(hostileTags.includes("A"), false);

console.log("markdown renderer tests passed");
