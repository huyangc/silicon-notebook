import assert from "node:assert/strict";
import test from "node:test";
import { formatReportCoverage, parseReportSubQueries } from "./report-outline-model.ts";
import { jsxElements, jsxTextValues, parseModule } from "./test/semantic-source.mjs";

const reportView = await parseModule("report-view.tsx");

test("deep report outline exposes frozen intent and objective evidence coverage", () => {
  const visibleCopy = jsxTextValues(reportView).join(" ");
  assert.match(visibleCopy, /这一步只理解你的问题，不读取语料/);
  assert.match(visibleCopy, /确认后的研究问题/);
  assert.match(visibleCopy, /提交补充并开始规划/);
  assert.match(visibleCopy, /本节必须回答/);
  assert.ok(jsxElements(reportView, "aside").some(
    (element) => element.attributes["aria-label"] === "引用原文",
  ));
  assert.equal(formatReportCoverage({
    hits: 5, base_hits: 2, element_hits: 7, source_hits: 3,
  }), "原文 7 · 知识 5 · 公共库 2");
});

test("approved report retrieval directions remain user-editable", () => {
  const visibleCopy = jsxTextValues(reportView).join(" ");
  assert.match(visibleCopy, /检索方向（每行一条）/);
  assert.deepEqual(parseReportSubQueries("query A\nquery B\n"), [
    "query A", "query B", "",
  ]);
});
