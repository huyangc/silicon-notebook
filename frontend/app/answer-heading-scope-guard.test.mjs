// 答案标题字阶的作用域守卫。
//
// 背景(这是评审实测抓到的泄漏,不是假想):按节合成让问答答案第一次带上 `##`/`###`
// 标题,于是 globals.css 里加了一组 `.answer-markdown h2/h3` 字阶。但
// `.answer-markdown` **不是**「问答答案」的身份,而是一条共用的 Markdown 渲染管线类:
//
//   * report-view.tsx  → `<div className="report-markdown answer-markdown">`（同一元素两个类）
//   * memory-panel.tsx / knowhow-cell-editor.tsx → 也各自复用 AnswerMarkdown
//
// 裸 `.answer-markdown h2` 与既有的 `.report-markdown h2` 特异性同为 (0,1,1),按源码
// 顺序后来者赢 —— 深度报告的 18px/带下边框 h2 会被顶成 21px、h3 从 15px 变成 19px 还
// 变灰。所以问答自己的字阶必须挂在 `.chat-answer` 这条容器链上。
//
// 为什么是静态守卫而不是 jsdom 量计算样式:jsdom 的 getComputedStyle 不实现特异性
// 级联(见 effort-picker-style-guard.test.mjs 的同一段说明),据此写断言只会得到稳定
// 的假绿。「narrowed 之后问答答案还吃不吃得到这组字阶」那一半由
// answer-heading-scope.component.test.tsx 用真实 DOM 祖先链证明。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { jsxElements, parseModule } from "./test/semantic-source.mjs";


const APP_DIR = path.dirname(fileURLToPath(import.meta.url));
const CSS = await readFile(path.join(APP_DIR, "globals.css"), "utf8");
const REPORT_VIEW = await parseModule("report-view.tsx");

const SHARED_PIPELINE_CLASS = "answer-markdown";
const ASK_CONTAINER_CLASS = "chat-answer";
const HEADING_TAGS = new Set(["h1", "h2", "h3", "h4", "h5", "h6"]);


/** 粗粒度切出顶层规则块(本文件关心的规则都不在 @media 内)。 */
function cssRules(css) {
  const rules = [];
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = pattern.exec(withoutComments)) !== null) {
    const selectorList = match[1].trim();
    if (selectorList.startsWith("@")) continue;
    rules.push({
      selectors: selectorList.split(",").map((s) => s.trim()).filter(Boolean),
      body: match[2],
    });
  }
  return rules;
}


function compounds(selector) {
  return selector.trim().split(/[\s>+~]+/).filter(Boolean);
}


function declaration(body, property) {
  const match = new RegExp(`(?:^|[;{\\s])${property}\\s*:\\s*([^;]+)`).exec(body);
  return match ? match[1].trim() : "";
}


/** 目标形态:最后一段是标题元素,且链上出现过共用管线类。 */
function headingRulesOnSharedPipeline() {
  const hits = [];
  for (const rule of cssRules(CSS)) {
    for (const selector of rule.selectors) {
      const parts = compounds(selector);
      const last = parts[parts.length - 1] ?? "";
      if (!HEADING_TAGS.has(last.replace(/[.:#].*$/, ""))) continue;
      if (!parts.some((part) => part.includes(`.${SHARED_PIPELINE_CLASS}`))) continue;
      hits.push({ selector, parts, body: rule.body });
    }
  }
  return hits;
}


test("the guard still sees the report heading scale it is defending", () => {
  // 哨兵:对照值若被改名/删掉,下面那条断言会退化成空转真空过。
  const report = cssRules(CSS).filter((rule) =>
    rule.selectors.some((selector) => /^\.report-markdown h[23]$/.test(selector)));
  const sizes = Object.fromEntries(report.map((rule) => [
    rule.selectors.find((selector) => /^\.report-markdown h[23]$/.test(selector)),
    declaration(rule.body, "font-size"),
  ]));
  assert.equal(sizes[".report-markdown h2"], "18px");
  assert.equal(sizes[".report-markdown h3"], "15px");
});


test("the report container really shares the answer-markdown pipeline class", () => {
  // 这条守卫的**前提**:报告容器与问答答案共用同一个类。前提若不再成立(报告换了
  // 自己的类),下面的收窄要求就变成了无谓的约束,应当连同本文件一起重估。
  const containers = jsxElements(REPORT_VIEW, "div")
    .map((element) => element.attributes?.className)
    .filter((name) => typeof name === "string"
      && name.split(/\s+/).includes(SHARED_PIPELINE_CLASS));
  assert.ok(containers.length >= 1, "report-view 应当仍复用 answer-markdown 渲染管线");
  for (const className of containers) {
    const classes = className.split(/\s+/);
    assert.ok(classes.includes("report-markdown"),
      `报告容器 "${className}" 应带自己的 report-markdown 类`);
    assert.ok(!classes.includes(ASK_CONTAINER_CLASS),
      `报告容器 "${className}" 不应带 ${ASK_CONTAINER_CLASS} —— 那会让收窄失效`);
  }
});


test("every heading rule on the shared pipeline is scoped to the Ask answer", () => {
  const hits = headingRulesOnSharedPipeline();
  // 正向下限:按节合成的那组字阶必须真的在(否则本用例会因为一条都没匹配上而空过)。
  assert.ok(hits.length >= 3,
    `expected the sectioned-answer heading rules to exist, saw ${hits.length}`);
  for (const hit of hits) {
    assert.ok(
      hit.parts.some((part) => part.includes(`.${ASK_CONTAINER_CLASS}`)),
      `"${hit.selector}" 必须收窄到 .${ASK_CONTAINER_CLASS} 容器链下`
      + " —— 裸 .answer-markdown h* 与 .report-markdown h* 同特异性，按源码顺序会"
      + "把深度报告的标题字阶顶掉（report h2 18→21px、h3 15→19px 并变灰）",
    );
  }
});
