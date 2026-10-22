// 图谱分析「问题清单」的接线守卫(2026-10-19 设计 §四、§十四「默认取舍」)。
//
// 组件测试钉的是面板自己的呈现与回调;这里钉 page.tsx 把这些回调接到了**既有入口**上,
// 而不是在面板旁边另起一套:
//   · 删除走既有 `confirmDeleteSource`(同一份确认文案);
//   · 重新解析先弹说明确认框,确认之后才走体检同款批量重新解析,且「在飞」只在确认之后开始;
//   · 打开来源走 `openSourceDetailById` 且不关面板(根弹窗协调器里那一对允许共存);
//   · 补上关联走知识图谱视图同一个后台任务;「去待确认合并」关面板并请求定位;
//   · 失效令牌含来源内容版本与补上关联的忙碌位;
//   · 只读成员与镜像库:面板的写入口与「生成分析」同一位(`!readOnlyWorkspace`)。
import test from "node:test";
import assert from "node:assert/strict";

import { callSitesIn, findFunctionIn, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const findings = await parseModule("kg-analysis-findings.tsx");

function body(name) {
  return findFunctionIn(page, "Home", name).getText(page);
}

function onlyElement(module, tag) {
  const elements = jsxElements(module, tag);
  assert.equal(elements.length, 1, `expected exactly one <${tag}>`);
  return elements[0];
}

test("面板的处置回调全部接到既有入口", () => {
  const analysis = onlyElement(page, "KgAnalysisView");
  const handlers = analysis.bindings.findingHandlers;
  for (const [key, target] of [
    ["onOpenSource", "openSourceFromAnalysis"],
    ["onDeleteSource", "deleteSourceFromAnalysis"],
    ["onReparseSource", "confirmReparseFromAnalysis"],
    ["onReviewMerge", "reviewMergeFromAnalysis"],
    ["onRelink", "relinkFromKgView"],
  ]) {
    assert.match(handlers, new RegExp(`${key}: ${target}\\b`), `${key} 应接到 ${target}`);
  }
  // 写入口与「生成 / 更新分析」同一位:只读成员与镜像库一个按钮都没有。
  assert.equal(analysis.bindings.canAnalyze, "!readOnlyWorkspace");
  assert.equal(analysis.bindings.deletingSourceIds, "deletingSourceIds");
  assert.equal(analysis.bindings.relinking, "kgGraph.relinking");
  assert.equal(analysis.bindings.relinkResult, "kgGraph.relinkResult");
  assert.equal(analysis.bindings.pendingMerges, "kgGraph.pendingMerges");
  // 失效令牌:来源内容(删除、重新解析状态推进)与补上关联的起止。
  for (const part of ["sourceLibrary.contentRevision", "kgGraph.relinking"]) {
    assert.ok(analysis.bindings.findingsRevision.includes(part), `findingsRevision 缺 ${part}`);
  }
});

test("打开来源核对:走 openSourceDetailById,不关面板", () => {
  const calls = callSitesIn(findFunctionIn(page, "Home", "openSourceFromAnalysis")).map(({ target }) => target);
  assert.ok(calls.includes("openSourceDetailById"));
  assert.equal(calls.includes("closeKgAnalysis"), false);
  assert.equal(calls.some((target) => target.startsWith("rootModals.requestClose")), false);
});

test("删除来源:调起既有删除确认框,结果回报给按钮旁", () => {
  const text = body("deleteSourceFromAnalysis");
  assert.match(text, /if \(!nb \|\| readOnlyWorkspace\) return;/);
  assert.match(text, /confirmDeleteSource\(/);
  assert.match(text, /report\(deleted \? "deleted" : "failed"\)/);
  // 既有确认框的文案不变,只多一个确认之后的回调。
  const confirm = body("confirmDeleteSource");
  assert.match(confirm, /message: sourceDeleteMessage\(source\.type, source\.title\)/);
  assert.match(confirm, /sourceLibrary\.deleteSource\(source\)\s*\.then\(\(deleted\) => onSettled\?\.\(deleted\)\)/);
});

test("重新解析:先弹说明确认框,确认后才进在飞态、才发请求", () => {
  const confirmReparse = findFunctionIn(page, "Home", "confirmReparseFromAnalysis");
  assert.match(confirmReparse.getText(page), /if \(!nb \|\| readOnlyWorkspace\) return;/);
  const calls = callSitesIn(confirmReparse);
  // 整个函数里只有一次请求、一次 running,而且都在确认框的那颗「重新解析」的回调里。
  assert.equal(calls.filter(({ target }) => target === "reparseOutcome").length, 1);
  assert.equal(calls.filter(({ target, arguments: args }) => target === "report" && args[0] === '"running"').length, 1);
  const modal = calls.find(({ target }) => target === "openInfoModal");
  assert.ok(modal, "没有确认框");
  const dialog = modal.arguments[0];
  assert.match(dialog, /report\("running"\);\s*void \(async \(\) => \{/, "确认 → running → 请求");
  // 请求之前先读一次 updated_at 作基准,再发重新解析。
  assert.match(
    dialog,
    /const baseline = await sourceLibrary\.readFollowBaseline\(target\.sourceId\);\s*const outcome = await reparseOutcome\(nb, \[target\.sourceId\]\);/,
  );
  // 确认框说清两件事:会重新调用模型、会替换这篇来源现有的知识条目。
  assert.match(dialog, /会重新调用模型/);
  assert.match(dialog, /替换它现有的知识条目/);
  // 体检同款批量重新解析:runReparse 与面板共用 reparseOutcome。
  const run = page.getFullText();
  assert.match(run, /const runReparse = async \(nb: string, sourceIds: string\[\]\): Promise<boolean> => \(\s*\(await reparseOutcome\(nb, sourceIds\)\) === "started"\s*\);/);
  assert.match(run, /const r = await reparseSources\(nb, sourceIds\);/);
});

test("重新解析排上之后按 id 跟到终态(它多半不在来源库当前页);面板一关就不再跟", () => {
  const dialog = callSitesIn(findFunctionIn(page, "Home", "confirmReparseFromAnalysis"))
    .find(({ target }) => target === "openInfoModal").arguments[0];
  // 只在真的排上了、且仍是发起时那个库时才跟——跟的就是这次请求的那一篇。
  assert.match(
    dialog,
    /if \(outcome === "started" && activeNotebookIdRef\.current === nb\) \{\s*sourceLibrary\.trackSourceProgress\(\[target\.sourceId\], \{ \[target\.sourceId\]: baseline \}\);\s*\}/,
  );
  const text = page.getFullText();
  assert.match(text, /const \{ untrackSources \} = sourceLibrary;\s*useEffect\(\(\) => \{\s*if \(!kgGraph\.analysisOpen\) untrackSources\(\);\s*\}, \[kgGraph\.analysisOpen, untrackSources\]\);/);
});

test("去待确认合并:关掉面板,并请求知识图谱视图定位那一对", () => {
  const calls = callSitesIn(findFunctionIn(page, "Home", "reviewMergeFromAnalysis")).map(({ target }) => target);
  assert.ok(calls.includes("closeKgAnalysis"));
  assert.ok(calls.includes("setKgMergeFocus"));
  assert.equal(onlyElement(page, "KgGraphView").bindings.focusMergeRequest, "kgMergeFocus");
});

test("面板里的写按钮:在飞期间禁用;「标为正常」只给可编辑成员的待处理问题", () => {
  const buttons = jsxElements(findings, "button");
  const byClick = (match) => buttons.filter((element) => (element.bindings?.onClick ?? "").includes(match));
  const [action] = byClick("run(spec, event)");
  assert.ok(action, "处置按钮不见了");
  assert.equal(action.bindings.disabled, "disabledFor(spec)");
  const disabledFor = findFunctionIn(findings, "FindingRowView", "disabledFor").getText(findings);
  for (const flag of ["state.deletingSourceIds.has(spec.sourceId)", "plan.processingSourceIds.includes(spec.sourceId)",
    "state.relinking", "state.maintenanceBusy", "busy === `reparse:${spec.sourceId}`"]) {
    assert.ok(disabledFor.includes(flag), `disabledFor 缺 ${flag}`);
  }
  const text = findings.getFullText();
  assert.match(text, /const canDismiss = state\.canWrite && row\.status === "open";/);
  assert.match(text, /\{canDismiss \? \(\s*<button[\s\S]{0,120}className="sort-button kg-finding-dismiss"/);
});
