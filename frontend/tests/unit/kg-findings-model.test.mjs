// 图谱分析「问题清单」的纯逻辑守卫(2026-10-19 设计 §三/§四/§五,kg-analysis-model.ts)。
//
// 钉五件事:
//   · 「更新分析」按钮的状态表(§五 S4)逐行、按优先级——尤其「合并版本是空串」不得读成
//     「合并规则已更新」,「升级后还没生成」是可点的灰档而不是红色;
//   · 排序与分区(严重度 → 影响面 → 主键;已处理留在待处理;认不出的状态不藏);
//   · 证据格式化只说判据用到的数,缺字段 / 类型不对只少一句话,不上屏代号;
//   · 建议动作的呈现计划:只读无按钮、F2 只删建议删除的那份、收录来源不出重新解析、
//     F4 只认两侧都在组内的那一对、F5 跑过且无新内容不出按钮;
//   · 已通过区三种状态分开(✓ / – 没法检查 / – 部分没判断)。
import test from "node:test";
import assert from "node:assert/strict";

import {
  analysisReadiness,
  analysisUpdateState,
  compareFindings,
  devItemViews,
  findingActionPlan,
  findingActions,
  findingBasedOnChange,
  findingEvidenceView,
  findingImpactText,
  findingRuleLabel,
  findingRuleStatuses,
  findingSeverityTone,
  findingsComparisonView,
  findingsReportLine,
  mergeRulesChanged,
  normalizeFindingRow,
  normalizeFindingRows,
  partitionFindings,
  passedRuleLines,
  pendingMergeForFinding,
  relinkIsSpent,
  relinkResultText,
  sortFindings,
  structureSignal,
  truncatedRuleNotes,
} from "../../app/kg-analysis-model.ts";

const CURRENT = {
  basis: "usable_live", built_at_seq: 128, seq_behind: 0, stale: false, built_at_cluster_seq: 7, cluster_seq_behind: 0,
};
const NEVER = {
  basis: "usable_live", built_at_seq: null, seq_behind: null, stale: null, built_at_cluster_seq: null, cluster_seq_behind: null,
};

function artifact(kind, overrides = {}) {
  return { kind, present: true, optional: false, absence: null, freshness: CURRENT, created_at: "", units: {}, payload: {}, ...overrides };
}

function summary(overrides = {}) {
  return {
    present: true, absence: null, freshness: CURRENT, rules_version: 1, counts: null, rules: [], dev_items: [],
    structure: null, comparison: null, relink: null, units: {}, ...overrides,
  };
}

/** 一份「全部一致」的报告:S4 的最后一档。每条用例只改它要钉的那一处。 */
function report(overrides = {}) {
  return {
    notebook_id: "nb-1",
    generated_at: "",
    level: 0,
    ledger_state: "complete",
    ledger_consistent: true,
    state: { present: true, kg_mutation_seq: 128, community_seq: 128, cluster_mutation_seq: 7, canonical_rel_seq: 128, dirty: false, last_rebuild: {}, units: {} },
    artifacts: ["cluster_size_histogram", "largest_clusters", "relation_provenance", "community_edges", "source_profiles", "findings"]
      .map((kind) => artifact(kind)),
    boards: { freshness: CURRENT, units: {}, payload: {} },
    board_edges: {},
    findings_summary: summary(),
    merge_code: { stored: "v4|1024|1024", current: "v4|1024|1024", matches: true },
    ...overrides,
  };
}

function withArtifact(kind, overrides) {
  const base = report();
  return base.artifacts.map((item) => (item.kind === kind ? artifact(kind, overrides) : item));
}

// ------------------------------------------------------------ S4 状态表

test("S4 第 1 档:合并结果建于旧的合并规则 → 可点并写原因(哪怕其余全部一致)", () => {
  const state = analysisUpdateState(report({ merge_code: { stored: "v3|1024|1024", current: "v4|1024|1024", matches: false } }));
  assert.deepEqual(state, { kind: "rules_updated", enabled: true, label: "更新分析", reason: "合并规则已更新，需要更新一次" });
});

test("S4:合并版本是空串(删除知识图谱 / 管线发布之后)不读成「合并规则已更新」", () => {
  for (const mergeCode of [
    { stored: "", current: "v4|1024|1024", matches: false },
    { stored: "", current: "", matches: false },
    { stored: "", current: "", matches: true },
  ]) {
    assert.equal(mergeRulesChanged(mergeCode), false, JSON.stringify(mergeCode));
    const state = analysisUpdateState(report({ merge_code: mergeCode }));
    assert.notEqual(state.kind, "rules_updated", JSON.stringify(mergeCode));
    assert.ok(!state.reason.includes("合并规则"), state.reason);
  }
  // 缺字段 / 后端还没带这一块:同样不是「规则已更新」。
  assert.equal(mergeRulesChanged(null), false);
  assert.equal(mergeRulesChanged(undefined), false);
  assert.equal(mergeRulesChanged({ stored: "v4", current: "v4", matches: true }), false);
});

test("S4 第 2 档:dirty / 任一在场产物或板块明确落后 / 账本不齐 / 数字对不上 → 可点,不写原因", () => {
  const cases = {
    dirty: report({ state: { ...report().state, dirty: true } }),
    "产物落后": report({ artifacts: withArtifact("largest_clusters", { freshness: { ...CURRENT, seq_behind: 3, stale: true } }) }),
    "板块落后": report({ boards: { freshness: { ...CURRENT, seq_behind: 2, stale: true }, units: {}, payload: {} } }),
    "升级后还没生成": report({
      ledger_state: "partial",
      artifacts: withArtifact("findings", { present: false, absence: "upgrade_pending", freshness: NEVER, payload: null }),
      findings_summary: summary({ present: false, absence: "upgrade_pending" }),
    }),
    "后端没带问题清单摘要": report({ findings_summary: undefined }),
    "本该有却缺失": report({ artifacts: withArtifact("largest_clusters", { present: false, absence: "unexpected", freshness: NEVER }) }),
    "数字口径不一致": report({ ledger_consistent: false }),
    "产物比当前还新": report({ artifacts: withArtifact("largest_clusters", { freshness: { ...CURRENT, seq_behind: -2 } }) }),
  };
  for (const [name, value] of Object.entries(cases)) {
    assert.deepEqual(analysisUpdateState(value), { kind: "needs_update", enabled: true, label: "更新分析", reason: "" }, name);
  }
  // 账本是空的:按钮叫「生成分析」。
  assert.equal(analysisUpdateState(report({ ledger_state: "empty" })).label, "生成分析");
});

test("S4:合法缺席(零板块的库不出依赖板块的产物)不算账本不齐", () => {
  const value = report({
    artifacts: withArtifact("source_profiles", { present: false, absence: "expected", freshness: NEVER, payload: null }),
    boards: { freshness: NEVER, units: {}, payload: {} },
  });
  assert.equal(analysisUpdateState(value).kind, "current");
});

test("S4 第 3 档:只剩合并代次无从判断 → 置灰并写原因;从没建过板块不算这一档", () => {
  const unknownArtifact = report({
    artifacts: withArtifact("community_edges", { freshness: { ...CURRENT, stale: null, built_at_cluster_seq: null, cluster_seq_behind: null } }),
  });
  assert.deepEqual(analysisUpdateState(unknownArtifact), {
    kind: "unverifiable",
    enabled: false,
    label: "更新分析",
    reason: "这一项只有在重新划分主题板块时才能核对，普通更新不会改变它",
  });
  const unknownBoards = report({ boards: { freshness: { ...CURRENT, stale: null }, units: {}, payload: {} } });
  assert.equal(analysisUpdateState(unknownBoards).kind, "unverifiable");
  // 板块从没建过:三个字段全是 null,那是「没有板块」,不是「代次无法验证」。
  assert.equal(analysisUpdateState(report({ boards: { freshness: NEVER, units: {}, payload: {} } })).kind, "current");
  // 「更新」档优先于「无法验证」档。
  const both = report({
    state: { ...report().state, dirty: true },
    artifacts: withArtifact("community_edges", { freshness: { ...CURRENT, stale: null } }),
  });
  assert.equal(analysisUpdateState(both).kind, "needs_update");
});

test("S4 第 4 档:全部一致 → 置灰,写「已与当前图谱一致，无需更新」", () => {
  assert.deepEqual(analysisUpdateState(report()), {
    kind: "current", enabled: false, label: "更新分析", reason: "已与当前图谱一致，无需更新",
  });
});

// ------------------------------------------------------------ 报告可信度

test("可信度:升级后还没生成是灰色,本该有却缺失是红色", () => {
  const upgrade = analysisReadiness(report({
    ledger_state: "partial",
    artifacts: withArtifact("findings", { present: false, absence: "upgrade_pending", freshness: NEVER, payload: null }),
  }));
  assert.equal(upgrade.tone, "neutral");
  assert.equal(upgrade.title, "升级后还没生成问题清单");
  const missing = analysisReadiness(report({
    artifacts: withArtifact("findings", { present: false, absence: "unexpected", freshness: NEVER, payload: null }),
  }));
  assert.equal(missing.tone, "danger");
  assert.equal(analysisReadiness(report()).tone, "ok");
  assert.equal(analysisReadiness(report({ state: { ...report().state, dirty: true } })).tone, "warn");
});

// ------------------------------------------------------------ 头部两行

test("报告新鲜度一行:与当前一致 / 落后;没生成时不编一句「与当前一致」", () => {
  assert.equal(findingsReportLine(summary()), "报告建于变更 #128、合并 #7 · 与当前一致");
  assert.equal(
    findingsReportLine(summary({ freshness: { ...CURRENT, seq_behind: 12, stale: true } })),
    "报告建于变更 #128、合并 #7 · 落后 12 次变更",
  );
  assert.equal(findingsReportLine(summary({ present: false, absence: "upgrade_pending" })), "");
  assert.equal(findingsReportLine(null), "");
});

test("对比行:可比时三个数;不可比时写原因;认不出的原因说中性的话", () => {
  const comparable = findingsComparisonView({
    comparable: true, reason: null, previous_built_at_seq: 120, resolved: 2, persisting: 4, new: 1,
    resolved_samples: [
      { rule_id: "F1", subject_key: "src-z", subject_label: "Titans.pdf" },
      { rule_id: "F1", subject_key: "src-y", subject_label: "Titans.pdf" },
      { rule_id: "F2", subject_key: "x|y", subject_label: "" },
    ],
  });
  assert.deepEqual(comparable, {
    comparable: true,
    text: "上次更新后：2 条已消失 · 4 条仍在 · 1 条新增",
    resolvedNames: ["Titans.pdf"],
  });
  const reasons = {
    rules_version_changed: "规则已更新，本次不与上次比较",
    no_previous: "这是第一份问题清单，还没有可以比较的上一次",
    truncated_or_not_run: "上次或这次有规则没能完整检查，本次不与上次比较",
    something_new: "本次不与上次比较",
  };
  for (const [reason, text] of Object.entries(reasons)) {
    const view = findingsComparisonView({ comparable: false, reason, resolved: null, persisting: null, new: null, resolved_samples: [] });
    assert.deepEqual(view, { comparable: false, text, resolvedNames: [] }, reason);
  }
  assert.equal(findingsComparisonView(null), null);
});

test("报告落后时每条问题标「基于变更 #N」,一致时不标", () => {
  const row = { rule_id: "F1", severity: "high", subject: { kind: "source", key: "a", label: "A" }, status: "open", built_at: { kg_seq: 120, cluster_seq: 6 } };
  assert.equal(findingBasedOnChange(row, summary(), false), "");
  assert.equal(findingBasedOnChange(row, summary(), true), "基于变更 #120");
  assert.equal(findingBasedOnChange({ ...row, built_at: null }, summary({ freshness: { ...CURRENT, stale: true } }), false), "基于变更 #128");
});

// ------------------------------------------------------------ 排序与分区

function row(rule_id, severity, key, impact, status = "open") {
  return { rule_id, severity, subject: { kind: "source", key, label: key }, evidence: {}, impact: { objects: impact }, status };
}

test("排序:严重度 → 影响面降序 → 主键升序;认不出的严重度排最后", () => {
  const rows = [
    row("F4", "low", "z", 15), row("F5", "low", "notebook", 1830), row("F2", "medium", "b|c", 280),
    row("F3", "medium", "d", 200), row("F1", "high", "a", 410), row("F9", "weird", "q", 9999),
    row("F3", "medium", "c", 200),
  ];
  assert.deepEqual(sortFindings(rows).map((item) => `${item.rule_id}:${item.subject.key}`), [
    "F1:a", "F2:b|c", "F3:c", "F3:d", "F5:notebook", "F4:z", "F9:q",
  ]);
  // 稳定:完全相同的键保持原顺序。
  const a = row("F1", "high", "a", 1);
  const b = { ...row("F1", "high", "a", 1), marker: true };
  assert.equal(compareFindings(a, b), 0);
});

test("分区:已处理留在待处理,标为正常 / 已判定不是同一个各进各的区,认不出的状态不藏", () => {
  const partition = partitionFindings([
    row("F1", "high", "a", 1, "open"),
    row("F1", "high", "b", 2, "handled"),
    row("F1", "high", "c", 3, "dismissed"),
    row("F4", "low", "d", 4, "decided_not_same"),
    row("F2", "medium", "e", 5, "mystery"),
  ]);
  assert.deepEqual(partition.pending.map((item) => item.subject.key), ["b", "a", "e"]);
  assert.deepEqual(partition.dismissed.map((item) => item.subject.key), ["c"]);
  assert.deepEqual(partition.decidedApart.map((item) => item.subject.key), ["d"]);
});

test("界面词:规则名与严重度档;认不出的不上屏代号", () => {
  assert.equal(findingRuleLabel("F1"), "疑似内容错配");
  assert.equal(findingRuleLabel("F2"), "疑似重复导入");
  assert.equal(findingRuleLabel("F3"), "疑似解析残缺");
  assert.equal(findingRuleLabel("F4"), "疑似没合并的同名概念");
  assert.equal(findingRuleLabel("F5"), "没建立关联的内容");
  assert.equal(findingRuleLabel("S1"), "其它问题");
  assert.equal(findingRuleLabel("__proto__"), "其它问题");
  assert.equal(findingSeverityTone("high"), "high");
  assert.equal(findingSeverityTone("critical"), "unknown");
});

test("影响面带单位:从这一页的 units 读,没登记时按契约说「知识对象」", () => {
  const item = row("F1", "high", "a", 1234);
  assert.equal(findingImpactText(item, { impact_objects: "objects" }), "影响 1,234 知识对象");
  assert.equal(findingImpactText(item, { impact_objects: "canonical" }), "影响 1,234 合并后的知识对象");
  assert.equal(findingImpactText(item, null), "影响 1,234 知识对象");
  // 规则引擎的扁平拼写一并接受。
  assert.equal(findingImpactText({ ...item, impact: null, impact_objects: 7 }, {}), "影响 7 知识对象");
});

// ------------------------------------------------------------ 证据

test("证据:F1 共享占比与是否在主体内容里;没有关联时不说主体内容", () => {
  const view = findingEvidenceView({
    rule_id: "F1", evidence: { shared_share: 0.045, giant_share: 0.86, concepts: 16, samples: ["a", "b", "", 3, "a"] },
  });
  assert.deepEqual(view.lines, ["16 个概念里，只有 4.5% 也出现在其它来源里", "在主体内容里的部分占 86.0%"]);
  assert.deepEqual(view.samples, ["a", "b"]);
  assert.equal(view.sampleLabel, "代表概念");
  const noGraph = findingEvidenceView({ rule_id: "F1", evidence: { shared_share: 0, giant_share: null, concepts: 30 } });
  assert.deepEqual(noGraph.lines, ["30 个概念里，只有 0% 也出现在其它来源里"]);
});

test("证据:两份重复的错文件合成一条 F1,另一份挂成附注(引擎的 partners 与契约的 attachments 都认)", () => {
  const fromEngine = findingEvidenceView({
    rule_id: "F1", evidence: { shared_share: 0, concepts: 20, partners: [{ source_id: "s2", label: "Hymba_full" }] },
  });
  assert.deepEqual(fromEngine.notes, ["另有一份重复导入：Hymba_full，两份都建议删除"]);
  const fromContract = findingEvidenceView({
    rule_id: "F1", evidence: { shared_share: 0, concepts: 20 },
    attachments: [{ rule_id: "F2", relation: "duplicate_partner", subject: { kind: "source", key: "s2", label: "Hymba_full" } }],
  });
  assert.deepEqual(fromContract.notes, ["另有一份重复导入：Hymba_full，两份都建议删除"]);
});

test("证据:F2 重合度、共有概念与两侧去留;不上屏「Jaccard」", () => {
  const view = findingEvidenceView({
    rule_id: "F2",
    evidence: {
      jaccard: 0.48, shared_concepts: 96,
      sources: [{ source_id: "a", label: "A", objects: 300, keep: true }, { source_id: "b", label: "", objects: 120, keep: false }],
    },
  });
  assert.deepEqual(view.lines, [
    "两份共有 96 个概念，概念重合度 48.0%",
    "A：300 个知识对象（建议保留）",
    "（没有标题）：120 个知识对象（建议删除）",
  ]);
  assert.ok(view.lines.every((line) => !/jaccard/i.test(line)));
});

test("证据:F3 只说命中的子信号;附注「重新解析后若仍与本库无关…」", () => {
  const view = findingEvidenceView({
    rule_id: "F3",
    evidence: {
      signals: ["high_isolated", "brand_new_signal"],
      isolated_objects: 40, isolated_share: 0.25, median_isolated_share: 0.04, objects: 160, also_off_topic: true,
    },
  });
  assert.deepEqual(view.lines, ["40 个知识对象没有任何关联（占 25.0%，本库中位数是 4.0%）"]);
  assert.deepEqual(view.notes, ["重新解析后若仍与本库无关，会转为疑似内容错配"]);
  assert.ok(!view.lines.join("").includes("brand_new_signal"));
});

test("证据:F4 组内名字与各自的知识对象数;契约的名字列表也认", () => {
  const view = findingEvidenceView({
    rule_id: "F4",
    evidence: { spellings: 2, member_sources: 3, members: [{ canonical_id: "K-a", name: "Llama 3.1", objects: 9 }, { canonical_id: "K-b", name: "Llama 31", objects: 2 }] },
  });
  assert.deepEqual(view.lines, ["同一个概念有 2 种写法没有合并，涉及 3 个来源"]);
  assert.deepEqual(view.samples, ["Llama 3.1（9）", "Llama 31（2）"]);
  assert.equal(view.sampleLabel, "组内的名字（知识对象数）");
  const names = findingEvidenceView({ rule_id: "F4", evidence: { names: ["Q-Former", "Qformer"] } });
  assert.deepEqual(names.samples, ["Q-Former", "Qformer"]);
  assert.equal(names.sampleLabel, "组内的名字");
});

test("证据:F5 全库数与最多的来源", () => {
  const view = findingEvidenceView({
    rule_id: "F5",
    evidence: { isolated_objects: 1830, isolated_share: 0.0439, usable_objects: 41713, top_sources: [{ source_id: "s", label: "A.pdf", isolated_objects: 120 }] },
  });
  assert.deepEqual(view.lines, ["1,830 个知识对象没有任何关联，占全部 41,713 个的 4.4%"]);
  assert.deepEqual(view.samples, ["A.pdf（120）"]);
});

test("证据:缺字段、类型不对、认不出的规则都不抛错,只少几句话", () => {
  for (const evidence of [null, undefined, {}, { shared_share: "0.1", concepts: NaN, samples: "x" }, []]) {
    for (const rule of ["F1", "F2", "F3", "F4", "F5", "F9"]) {
      const view = findingEvidenceView({ rule_id: rule, evidence });
      assert.ok(Array.isArray(view.lines) && Array.isArray(view.samples) && Array.isArray(view.notes));
      assert.deepEqual(view.lines, [], `${rule} ${JSON.stringify(evidence)}`);
    }
  }
});

// ------------------------------------------------------------ 建议动作

const CONTEXT = { canWrite: true, pendingMerges: [], relinkMayChange: true };

function live(source_id, title, extra = {}) {
  return { source_id, exists: true, title, source_type: "pdf", processing: false, ...extra };
}

test("动作:只读成员 / 已处理 / 已标为正常一个按钮都没有", () => {
  const f1 = {
    rule_id: "F1", severity: "high", subject: { kind: "source", key: "a", label: "A" }, evidence: {}, status: "open",
    actions: [{ kind: "delete_source", source_id: "a" }], live_sources: [live("a", "A")],
  };
  assert.equal(findingActionPlan(f1, { ...CONTEXT, canWrite: false }).buttons.length, 0);
  assert.equal(findingActionPlan({ ...f1, status: "handled" }, CONTEXT).buttons.length, 0);
  assert.equal(findingActionPlan({ ...f1, status: "dismissed" }, CONTEXT).buttons.length, 0);
  const plan = findingActionPlan(f1, CONTEXT);
  assert.deepEqual(plan.buttons.map((spec) => spec.kind), ["open_source", "delete_source"]);
  assert.deepEqual(plan.notes, ["不可撤销，先打开来源核对"]);
});

test("动作:F2 两份都能打开,删除只给建议删除的那份;契约的单个 action 形状也认", () => {
  const f2 = {
    rule_id: "F2", severity: "medium", subject: { kind: "source_pair", key: "a|b", label: "A ⇄ B" }, status: "open",
    evidence: { sources: [{ source_id: "a", label: "A", objects: 300, keep: true }, { source_id: "b", label: "B", objects: 100, keep: false }] },
    action: { kind: "delete_source", capability: "sources:write", available: true, unavailable_reason: null, target_source_id: "b" },
    live_sources: [live("a", "A"), live("b", "B")],
  };
  const plan = findingActionPlan(f2, CONTEXT);
  assert.deepEqual(plan.buttons.map((spec) => `${spec.kind}:${spec.sourceId ?? ""}`), [
    "open_source:a", "open_source:b", "delete_source:b",
  ]);
  assert.deepEqual(plan.notes, ["不可撤销，先打开来源核对", "两份内容几乎相同，保留对象更多的一份"]);
  assert.deepEqual(findingActions(f2).map((action) => action.kind), ["delete_source"]);
  // 读侧两边都给了删除动作:建议保留的那份旁边照样不出删除(前端自己的防线)。
  const both = findingActionPlan({
    ...f2,
    action: null,
    actions: [{ kind: "delete_source", source_id: "a" }, { kind: "delete_source", source_id: "b" }],
  }, CONTEXT);
  assert.deepEqual(both.buttons.filter((spec) => spec.kind === "delete_source").map((spec) => spec.sourceId), ["b"]);
});

test("动作:收录来源不出重新解析;已不在的来源不出按钮;正在解析的标出来", () => {
  const f3 = {
    rule_id: "F3", severity: "medium", subject: { kind: "source", key: "d", label: "D" }, evidence: {}, status: "open",
    actions: [{ kind: "reparse_source", source_id: "d" }],
  };
  // 收录来源只留「打开来源核对」,不出「重新解析」(后端对它的重新解析回 409)。
  assert.deepEqual(
    findingActionPlan({ ...f3, live_sources: [live("d", "D", { source_type: "promotion" })] }, CONTEXT).buttons
      .map((spec) => spec.kind),
    ["open_source"],
  );
  // 读侧没给这一篇此刻的样子:不知道类型就不出删除 / 重新解析,只留打开。
  assert.deepEqual(
    findingActionPlan({ ...f3, live_sources: [] }, CONTEXT).buttons.map((spec) => spec.kind),
    ["open_source"],
  );
  assert.deepEqual(
    findingActionPlan({ ...f3, live_sources: [live("d", "D")] }, CONTEXT).buttons.map((spec) => spec.kind),
    ["open_source", "reparse_source"],
  );
  assert.equal(findingActionPlan({ ...f3, live_sources: [live("d", "D", { exists: false })] }, CONTEXT).buttons.length, 0);
  const processing = findingActionPlan({ ...f3, live_sources: [live("d", "D", { processing: true })] }, CONTEXT);
  assert.deepEqual(processing.processingSourceIds, ["d"]);
  assert.ok(processing.notes.includes("这篇来源正在解析，完成后再处理"));
});

test("动作:后端说做不了时不出按钮,原因按界面词说;认不出的原因说中性的话", () => {
  const base = {
    rule_id: "F1", severity: "high", subject: { kind: "source", key: "a", label: "A" }, evidence: {}, status: "open",
    live_sources: [live("a", "A")],
  };
  const mirror = findingActionPlan({ ...base, actions: [{ kind: "delete_source", source_id: "a", available: false, unavailable_reason: "mirror" }] }, CONTEXT);
  assert.equal(mirror.buttons.length, 0);
  assert.deepEqual(mirror.blocked, ["这是同步来的镜像库，只能在源环境处理"]);
  const odd = findingActionPlan({ ...base, actions: [{ kind: "delete_source", source_id: "a", available: false, unavailable_reason: "zz_top" }] }, CONTEXT);
  assert.deepEqual(odd.blocked, ["当前无法在这里处理"]);
});

test("动作:F4 只认两侧都在组内的那一对;没有就写「待确认合并里还没有这一组」", () => {
  const f4 = {
    rule_id: "F4", severity: "low", subject: { kind: "concept_group", key: "rmsnorm", label: "RMS Norm" }, status: "open",
    evidence: { members: [{ canonical_id: "K-rms norm", name: "RMS Norm" }, { canonical_id: "K-rmsnorm", name: "RMSNorm" }] },
    actions: [{ kind: "review_merge" }],
  };
  const merges = [
    { id: "one-side", canonical_a: "K-rms norm", canonical_b: "K-layer norm", score: 0.8, status: "pending" },
    { id: "match", canonical_a: "K-rmsnorm", canonical_b: "K-rms norm", score: 0.9, status: "pending" },
  ];
  assert.equal(pendingMergeForFinding(f4, merges)?.id, "match");
  assert.deepEqual(findingActionPlan(f4, { ...CONTEXT, pendingMerges: merges }).buttons, [{ kind: "review_merge", mergeId: "match" }]);
  const none = findingActionPlan(f4, { ...CONTEXT, pendingMerges: [merges[0]] });
  assert.equal(none.buttons.length, 0);
  assert.deepEqual(none.blocked, ["待确认合并里还没有这一组"]);
  // 契约若改给完整的 canonical_ids 列表,同样认。
  assert.equal(pendingMergeForFinding({ ...f4, evidence: { canonical_ids: ["K-rmsnorm", "K-rms norm"] } }, merges)?.id, "match");
  // 组里不止证据列出的那几个(只截了前 5 个):没找到不等于没有——带到那一节,不断言没有。
  const big = { ...f4, evidence: { ...f4.evidence, canonicals: 7 } };
  const hedged = findingActionPlan(big, { ...CONTEXT, pendingMerges: [merges[0]] });
  assert.deepEqual(hedged.buttons, [{ kind: "review_merge", mergeId: "" }]);
  assert.deepEqual(hedged.blocked, []);
  assert.deepEqual(hedged.notes, ["这一组写法较多，没能直接定位到对应的一对，按钮会带你到「待确认合并」那一节查找"]);
  // 完整的 id 表在手、确实没有:如实说。
  const complete = findingActionPlan({ ...f4, evidence: { ...f4.evidence, canonicals: 2 } }, { ...CONTEXT, pendingMerges: [merges[0]] });
  assert.deepEqual(complete.blocked, ["待确认合并里还没有这一组"]);
});

test("动作:F5 跑过且之后没有新内容 → 不出按钮,说明再跑也补不上(只读成员也看得到这句)", () => {
  const f5 = {
    rule_id: "F5", severity: "low", subject: { kind: "notebook", key: "notebook", label: "" }, status: "open",
    evidence: { isolated_objects: 1830 }, actions: [{ kind: "relink" }],
  };
  assert.deepEqual(findingActionPlan(f5, CONTEXT).buttons, [{ kind: "relink" }]);
  assert.deepEqual(findingActionPlan(f5, { ...CONTEXT, relinkMayChange: null }).buttons, [{ kind: "relink" }]);
  for (const canWrite of [true, false]) {
    const plan = findingActionPlan(f5, { ...CONTEXT, canWrite, relinkMayChange: false });
    assert.equal(plan.buttons.length, 0);
    assert.deepEqual(plan.blocked, ["补上关联已经跑过，剩下 1,830 个没有可自动补上的关联；集中在某几个来源的，见疑似解析残缺"]);
  }
  assert.equal(relinkResultText({ edgesAdded: 1200, isolatedAfter: 630 }), "新增 1,200 条关联，还剩 630 个没有关联的知识条目");
});

// ------------------------------------------------------------ 已通过 / 开发 / 结构

test("已通过:✓ 检查过没发现、– 没法检查、– 部分没判断分开;有问题的规则不出 ✓", () => {
  const lines = passedRuleLines(summary({
    rules: [
      { rule_id: "F1", status: "ran", checked: 84, checked_unit: "sources", total: 2, not_judged: 3 },
      { rule_id: "F2", status: "skipped", reason: "fewer_than_two_sources" },
      { rule_id: "F3", status: "ran", checked: 84, checked_unit: "sources", total: 0 },
      { rule_id: "F4", status: "skipped", reason: "a_reason_from_the_future" },
      { rule_id: "F5", status: "ran", total: 0 },
      { rule_id: "S1", status: "ran", total: 0 },
    ],
  }));
  assert.deepEqual(lines, [
    { ruleId: "F1", state: "partial", text: "疑似内容错配：有 3 个来源概念太少，没有判断" },
    { ruleId: "F2", state: "skipped", text: "疑似重复导入：没法检查，能互相比较的来源不足两个" },
    { ruleId: "F3", state: "clean", text: "没发现解析残缺的来源（检查了 84 个来源）" },
    { ruleId: "F4", state: "skipped", text: "疑似没合并的同名概念：没法检查，这一次没有可检查的内容" },
    { ruleId: "F5", state: "clean", text: "没发现没建立关联的内容" },
  ]);
  assert.deepEqual(passedRuleLines(summary({ present: false })), []);
});

test("已通过:F4 用户判定过不是同一个的组数单独一行(重建后那些组不再成行)", () => {
  const lines = passedRuleLines(summary({
    rules: [{ rule_id: "F4", status: "ran", checked: 9400, checked_unit: "canonical", total: 2, decided_not_same: 3 }],
  }));
  assert.deepEqual(lines, [
    { ruleId: "F4", state: "decided", text: "疑似没合并的同名概念：已判定不是同一个的有 3 组，不再列为问题" },
  ]);
});

test("规则状态两种形状都认:按编号为键的表(引擎拼写)与数组,按 F1…F5 排", () => {
  const engine = summary({
    rules: {
      F5: { status: "ran", objects_checked: 41713, total: 0 },
      F1: { status: "ran", sources_checked: 81, sources_too_few_concepts: 3, total: 600, persisted: 500, truncated: true },
      S1: { status: "ran", narrow_groups: 14, narrow_group_objects: 220, examples: [{ names: ["RMS Norm", "RMSNorm"] }] },
      F4: { status: "ran", canonicals_checked: 9400, groups_decided_apart: 2, total: 0 },
      F2: { status: "skipped", reason: "fewer_than_two_sources", total: 0 },
      F3: { status: "ran", sources_checked: 84, total: 0 },
    },
  });
  assert.deepEqual(passedRuleLines(engine).map((line) => line.text), [
    "疑似内容错配：有 3 个来源概念太少，没有判断",
    "疑似重复导入：没法检查，能互相比较的来源不足两个",
    "没发现解析残缺的来源（检查了 84 个来源）",
    "没发现没合并的同名概念（检查了 9,400 个合并后的知识对象）",
    "疑似没合并的同名概念：已判定不是同一个的有 2 组，不再列为问题",
    "没发现没建立关联的内容（检查了 41,713 个知识对象）",
  ]);
  assert.deepEqual(truncatedRuleNotes(engine), ["疑似内容错配共 600 条，只保存了前 500 条"]);
  // 没有 dev_items 时,「需要开发处理」从 S1 的状态里取。
  assert.deepEqual(devItemViews(engine), [{
    key: "S1",
    text: "14 组只差空格/连字符的概念没有合并（例：RMS Norm / RMSNorm），这是合并规则的缺陷，修好后会自动合并，不需要逐条处理。",
  }]);
  assert.deepEqual(findingRuleStatuses(engine).map((rule) => rule.rule_id), ["F1", "F2", "F3", "F4", "F5", "S1"]);
  assert.deepEqual(findingRuleStatuses(summary({ rules: "nonsense" })), []);
});

test("问题行两种形状都认:引擎的扁平列统一成嵌套形状;没有规则编号的行丢掉", () => {
  const flat = normalizeFindingRow({
    rule_id: "F2", severity: "medium", subject_kind: "source_pair", subject_key: "a|b", subject_label: "A ⇄ B",
    impact_objects: 12, evidence: { jaccard: 0.5 },
  });
  assert.deepEqual(flat.subject, { kind: "source_pair", key: "a|b", label: "A ⇄ B" });
  assert.deepEqual(flat.impact, { objects: 12 });
  assert.equal(flat.status, "open");
  const nested = normalizeFindingRow({
    rule_id: "F1", severity: "high", subject: { kind: "source", key: "s", label: "S" }, impact: { objects: 3 }, status: "handled",
  });
  assert.deepEqual(nested.subject, { kind: "source", key: "s", label: "S" });
  assert.equal(nested.status, "handled");
  assert.equal(normalizeFindingRow({ severity: "high" }), null);
  assert.equal(normalizeFindingRow("x"), null);
  assert.deepEqual(normalizeFindingRows(null), []);
  assert.equal(normalizeFindingRows([{ rule_id: "F5" }, null, { rule_id: "" }]).length, 1);
});

test("补上关联跑过且无新内容 = 「已经跑过」;判断不了或还可能改变结果都不算", () => {
  assert.equal(relinkIsSpent(summary({ relink: { attempted: true, at_kg_seq: 1, at: "", may_change: false } })), true);
  assert.equal(relinkIsSpent(summary({ relink: { attempted: true, at_kg_seq: 1, at: "", may_change: true } })), false);
  assert.equal(relinkIsSpent(summary({ relink: { attempted: false, at_kg_seq: null, at: null, may_change: null } })), false);
  assert.equal(relinkIsSpent(summary({ present: false, relink: { attempted: true, at_kg_seq: 1, at: "", may_change: false } })), false);
  assert.equal(relinkIsSpent(null), false);
});

test("截断不静默:只保存了前若干条时写在待处理区", () => {
  assert.deepEqual(truncatedRuleNotes(summary({
    rules: [
      { rule_id: "F1", status: "ran", total: 600, persisted: 500, truncated: true },
      { rule_id: "F2", status: "ran", total: 3, persisted: 3, truncated: true },
      { rule_id: "F3", status: "ran", total: 1, persisted: 1, truncated: false },
    ],
  })), ["疑似内容错配共 600 条，只保存了前 500 条", "疑似重复导入没能检查完整，列出的不是全部"]);
});

test("需要开发处理:只认 S1、只在测得到影响时出现,例子两种形状都认", () => {
  assert.deepEqual(devItemViews(summary({
    dev_items: [
      { item_id: "S1", groups: 14, objects: 220, samples: [{ names: ["RMS Norm", "RMSNorm"] }, "Q-Former / Qformer", { names: [] }, 7] },
      { item_id: "S2", groups: 3, objects: 1, samples: [] },
    ],
  })), [{
    key: "S1",
    text: "14 组只差空格/连字符的概念没有合并（例：RMS Norm / RMSNorm；Q-Former / Qformer），这是合并规则的缺陷，修好后会自动合并，不需要逐条处理。",
  }]);
  assert.deepEqual(devItemViews(summary({ dev_items: [{ item_id: "S1", groups: 0, objects: 0, samples: [] }] })), []);
  assert.deepEqual(devItemViews(summary({ present: false })), []);
});

test("主体内容 / 孤岛(S3):主题数来自依赖板块的产物,缺席时只少那半句", () => {
  const structure = { giant_objects: 30366, objects_total: 41713, giant_object_share: 0.728, island_components: 1137, island_sources: 14 };
  assert.deepEqual(structureSignal(summary({ structure }), { boards_in_giant: 80 }), {
    headline: "主体内容占 72.8%",
    text: "主体内容含 72.8% 的知识对象，内部有 80 个主题；另有 1,137 个孤岛，来自 14 个来源。",
  });
  assert.equal(
    structureSignal(summary({ structure }), null)?.text,
    "主体内容含 72.8% 的知识对象；另有 1,137 个孤岛，来自 14 个来源。",
  );
  assert.equal(
    structureSignal(summary({ structure: { ...structure, island_components: 0 } }), null)?.text,
    "主体内容含 72.8% 的知识对象；没有孤岛。",
  );
  assert.equal(structureSignal(summary({ structure: null }), null), null);
  assert.equal(structureSignal(summary({ present: false, structure }), null), null);
});
