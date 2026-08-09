import assert from "node:assert/strict";
import test from "node:test";
import { formatReportCoverage, parseReportSubQueries } from "../../app/report-outline-model.ts";
import { jsxElements, jsxTextValues, parseModule } from "../../test-support/semantic-source.mjs";

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

test("deep report exposes reader-facing credibility disclosure without internal retrieval terms", () => {
  const visibleCopy = jsxTextValues(reportView).join(" ");
  const sourceText = reportView.getFullText();
  assert.match(visibleCopy, /资料基础/);
  assert.match(visibleCopy, /分析框架/);
  assert.match(sourceText, /引证覆盖率/);
  assert.match(sourceText, /本报告按相关性检索生成，未做完整枚举。/);
  assert.doesNotMatch(sourceText, /\bKG\b|\bchunk\b|canonical/i);
});

test("the report detail mounts both credibility disclosures from its active report", () => {
  // Component tests prove the rendered user path.  This AST guard makes a
  // delete/move mutation fail even if a test fixture accidentally renders the
  // standalone component instead of the report-detail composition.
  for (const name of ["ReportCredibilitySummary", "ReportCitationDistribution"]) {
    const mounts = jsxElements(reportView, name).filter((element) => (
      element.scope.endsWith(".ReportsPanel") && element.bindings?.report === "active"
    ));
    assert.equal(
      mounts.length,
      1,
      `${name} must be mounted once by the active report detail`,
    );
  }
});
