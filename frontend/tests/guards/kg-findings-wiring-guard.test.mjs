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
  // 请求交给来源库:它先读一次 updated_at 作基准、再发重新解析,排上就按 id 跟到这一次跑完,从提交起
  // 这篇来源就占着重新解析的占位(读基准在提交之前、占位从提交起,由 use-source-library 的用例钉住)。
  // 来源库把「提交时的 owner 与写权限是否仍在」交给请求:请求回来时工作区已换,就不弹提示、不刷新。
  assert.match(
    dialog,
    /const outcome = await sourceLibrary\.submitFollowedReparse\(\s*target\.sourceId,\s*\(stillOwned\) => reparseOutcome\(nb, \[target\.sourceId\], stillOwned\),?\s*\);/,
  );
  // 被放弃(abandoned)原样交给这一行:只放开在飞态,不写结果。
  assert.match(dialog, /report\(outcome === "none" \? "skipped" : outcome\);/);
  // 确认框说清两件事:会重新调用模型、会替换这篇来源现有的知识条目。
  assert.match(dialog, /会重新调用模型/);
  assert.match(dialog, /替换它现有的知识条目/);
  // 体检同款批量重新解析:runReparse 与面板共用 reparseOutcome,请求、提示与刷新都在
  // source-reparse-request.ts 的 requestReparse 里(归属核对之后才有副作用,那边的用例钉住)。
  const run = page.getFullText();
  assert.match(run, /const runReparse = async \(nb: string, sourceIds: string\[\], stillOwned: \(\) => boolean\): Promise<boolean> => \(\s*\(await reparseOutcome\(nb, sourceIds, stillOwned\)\) === "started"\s*\);/);
  assert.match(run, /requestReparse\(nb, sourceIds, stillOwned, \{\s*reparse: reparseSources,/);
  // 体检的批量修复同样带上发起时的 owner;回来时不再是那本,就不动新库的忙碌位。
  assert.match(run, /const stillOwned = \(\) => owner !== null && sourceLibrary\.captureOwner\(\) === owner;/);
  assert.match(run, /started = await runReparse\(nb, g\.sample, stillOwned\);/);
  assert.match(run, /if \(!started && activeNotebookIdRef\.current === nb\) \{/);
  // 同一个修复按钮里的补齐向量走同一条规则:请求回来先核对归属,再提示、刷新、开轮询窗口。
  assert.match(run, /started = await runBackfillVectors\(nb, stillOwned\);/);
  assert.match(run, /const r = await backfillVectors\(nb\);[\s\S]{0,240}?if \(!stillOwned\(\)\) return false;\s*if \(!r\.accepted\)/);
  assert.match(run, /\} catch \(e\) \{\s*if \(stillOwned\(\)\) reportError\(e\);\s*return false;\s*\}/);
});

test("重新解析排上之后按 id 跟到这一次跑完;面板一关就不再跟;全页的重新解析入口读同一份占位", () => {
  const dialog = callSitesIn(findFunctionIn(page, "Home", "confirmReparseFromAnalysis"))
    .find(({ target }) => target === "openInfoModal").arguments[0];
  // 跟踪、基准、占位都在来源库里,page 不另记一份(不再自己调 trackSourceProgress / readFollowBaseline)。
  assert.equal(/trackSourceProgress|readFollowBaseline/.test(dialog), false);
  const text = page.getFullText();
  assert.match(text, /const \{ untrackSources \} = sourceLibrary;\s*useEffect\(\(\) => \{\s*if \(!kgGraph\.analysisOpen\) untrackSources\(\);\s*\}, \[kgGraph\.analysisOpen, untrackSources\]\);/);
  // 问题清单的占位就是来源库那一份(引用同一篇来源的每一行都按它置灰)。
  assert.equal(onlyElement(page, "KgAnalysisView").bindings.reparsingSourceIds, "sourceLibrary.reparsingSourceIds");
  // 来源详情的三个「重新解析」(标题旁的图标、降级提示里的按钮、解析失败那句话里的行内动作)
  // 用同一份占位置灰并就地写状态;
  // 来源库的 reparseSource 自己也拒收(use-source-library 的用例钉住)。
  assert.match(
    text,
    /const sourceDetailReparseQueued = Boolean\(\s*sourceDetail && !reparsingSource && sourceLibrary\.reparsingSourceIds\.has\(sourceDetail\.id\),\s*\);/,
  );
  const reparseButtons = jsxElements(page, "button")
    .filter((element) => (element.bindings?.onClick ?? "").includes("reparseSource()"));
  assert.equal(reparseButtons.length, 3, "来源详情应有三个重新解析按钮");
  for (const button of reparseButtons) {
    assert.equal(button.bindings.disabled, "reparsingSource || sourceDetailDeleting || sourceDetailReparseQueued");
  }
  assert.match(text, /\{sourceDetailReparseQueued \? \(\s*<p className="tool-hint source-detail-reparse-note" role="status">已提交重新解析，等待完成<\/p>/);
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
    "state.relinking", "state.maintenanceBusy", "busy === `reparse:${spec.sourceId}`",
    "plan.reparsingSourceIds.includes(spec.sourceId)"]) {
    assert.ok(disabledFor.includes(flag), `disabledFor 缺 ${flag}`);
  }
  const text = findings.getFullText();
  assert.match(text, /const canDismiss = state\.canWrite && row\.status === "open";/);
  assert.match(text, /\{canDismiss \? \(\s*<button[\s\S]{0,120}className="sort-button kg-finding-dismiss"/);
});
