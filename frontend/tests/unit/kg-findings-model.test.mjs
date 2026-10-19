// 图谱分析「问题清单」的纯逻辑守卫(2026-10-19 设计 §三/§四/§五,kg-analysis-model.ts)。
//
// 夹具一律是后端的规范形状(backend/app/models/kg_analysis.py 的 FindingRow / FindingsSummary /
// MergeCodeState),钉五件事:
//   · 「更新分析」按钮的状态表(§五 S4)逐行、按优先级——尤其 `merge_code.matches` 为 null
//     (还没合并过)不得读成「合并规则已更新」,「升级后还没生成」是可点的灰档而不是红色;
//   · 排序与分区(严重度 → 影响面 → 主键;已处理留在待处理;relink_spent 另放;认不出的状态不藏);
//   · 证据格式化只说判据用到的数,缺字段 / 类型不对只少一句话,不上屏代号;
//   · 建议动作的呈现计划:只有 open 出按钮、只读无按钮、F2 只删建议删除的那份、收录来源不出
//     重新解析、F4 只认两侧都在全组 canonical_ids 里的那一对;
//   · 已通过区三种状态分开(✓ / – 没法检查 / – 部分没判断),规则状态按引擎原字段读。
import test from "node:test";
import assert from "node:assert/strict";

import {
  analysisReadiness,
  analysisUpdateState,
  compareFindings,
  devItemViews,
  findingActionPlan,
  findingBasedOnChange,
  findingEvidenceView,
  findingHandledText,
  findingImpactText,
  findingRuleLabel,
  findingSeverityTone,
  findingsComparisonView,
  findingsReportLine,
  findingSubjectText,
  mergeRulesChanged,
  partitionFindings,
  passedRowText,
  passedRuleLines,
  pendingMergeForFinding,
  relinkResultText,
  relinkSpentText,
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
    present: true, absence: null, freshness: CURRENT, rules_version: 1,
    built_at: { kg_seq: 128, cluster_seq: 7 }, created_at: "2026-10-19T09:00:00Z",
    counts: null, rules: {}, dev_items: [], structure: null, comparison: null, relink: null, units: {}, ...overrides,
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

function live(source_id, title, extra = {}) {
  return { source_id, exists: true, title, source_type: "pdf", processing: false, ...extra };
}

/** 规范形状的一行:没写的字段取「新发现、还没处理」的默认值。 */
function finding(fields) {
  return {
    severity: "low",
    subject: { kind: "source", key: "x", label: "X" },
    subject_refs: [],
    evidence: {},
    impact: { objects: 0 },
    actions: [],
    first_seen: { kg_seq: 128, cluster_seq: 7 },
    status: "open",
    handled_reason: null,
    dismissed_at: null,
    live_sources: [],
    ...fields,
  };
}

// ------------------------------------------------------------ S4 状态表

test("S4 第 1 档:合并结果建于旧的合并规则 → 可点并写原因(哪怕其余全部一致)", () => {
  const state = analysisUpdateState(report({ merge_code: { stored: "v3|1024|1024", current: "v4|1024|1024", matches: false } }));
  assert.deepEqual(state, { kind: "rules_updated", enabled: true, label: "更新分析", reason: "合并规则已更新，需要更新一次" });
});

test("S4:只认 matches === false;null(还没合并过,删除知识图谱 / 管线发布之后)不读成「合并规则已更新」", () => {
  assert.equal(mergeRulesChanged({ stored: "v3", current: "v4", matches: false }), true);
  for (const mergeCode of [
    { stored: "", current: "v4|1024|1024", matches: null },
    { stored: "", current: "", matches: null },
    { stored: "v4", current: "v4", matches: true },
  ]) {
    assert.equal(mergeRulesChanged(mergeCode), false, JSON.stringify(mergeCode));
    const state = analysisUpdateState(report({ merge_code: mergeCode }));
    assert.notEqual(state.kind, "rules_updated", JSON.stringify(mergeCode));
    assert.ok(!state.reason.includes("合并规则"), state.reason);
  }
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
    "问题清单还没生成": report({ findings_summary: summary({ present: false, absence: "never_computed" }) }),
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
    findings_summary: summary({ present: false, absence: "upgrade_pending" }),
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
  assert.equal(findingsReportLine(summary({ present: false, absence: "upgrade_pending", freshness: NEVER })), "");
});

test("对比行:可比时三个数;不可比时写原因;认不出的原因说中性的话", () => {
  const sample = (subject_key, subject_label, rule_id = "F1") => ({
    rule_id, subject_kind: "source", subject_key, subject_label, impact_objects: 10,
  });
  const comparable = findingsComparisonView({
    previous_built_at_seq: 120, previous_built_at_cluster_seq: 6, comparable: true, reason: null,
    resolved: 2, persisting: 4, new: 1,
    resolved_samples: [sample("src-z", "Titans.pdf"), sample("src-y", "Titans.pdf"), sample("x|y", "", "F2")],
    by_rule: {},
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
    const view = findingsComparisonView({
      previous_built_at_seq: null, previous_built_at_cluster_seq: null, comparable: false, reason,
      resolved: null, persisting: null, new: null, resolved_samples: [], by_rule: {},
    });
    assert.deepEqual(view, { comparable: false, text, resolvedNames: [] }, reason);
  }
  assert.equal(findingsComparisonView(null), null);
});

test("报告落后时每条问题标「基于变更 #N」,N 取页级 built_at;一致时不标", () => {
  const builtAt = { kg_seq: 120, cluster_seq: 6 };
  assert.equal(findingBasedOnChange(builtAt, summary(), false), "");
  assert.equal(findingBasedOnChange(builtAt, summary(), true), "基于变更 #120");
  const stale = summary({ freshness: { ...CURRENT, seq_behind: 3, stale: true } });
  assert.equal(findingBasedOnChange(builtAt, stale, false), "基于变更 #120");
  // 页上没有 built_at(清单缺席)时不拿摘要的数冒充,只说是旧的。
  assert.equal(findingBasedOnChange(null, stale, false), "基于旧的报告");
});

// ------------------------------------------------------------ 排序与分区

function row(rule_id, severity, key, impact, status = "open") {
  return finding({ rule_id, severity, subject: { kind: "source", key, label: key }, impact: { objects: impact }, status });
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

test("分区:已处理留在待处理;标为正常 / 已判定不是同一个 / 补上关联已经跑过各进各的;认不出的状态不藏", () => {
  const partition = partitionFindings([
    row("F1", "high", "a", 1, "open"),
    row("F1", "high", "b", 2, "handled"),
    row("F1", "high", "c", 3, "dismissed"),
    row("F4", "low", "d", 4, "decided_not_same"),
    row("F2", "medium", "e", 5, "mystery"),
    row("F5", "low", "notebook", 6, "relink_spent"),
  ]);
  assert.deepEqual(partition.pending.map((item) => item.subject.key), ["b", "a", "e"]);
  assert.deepEqual(partition.dismissed.map((item) => item.subject.key), ["c"]);
  // relink_spent 随 passed 那一页返回,但不在「已通过」列出——另放诊断数据(§4.5)。
  assert.deepEqual(partition.passed.map((item) => item.subject.key), ["d"]);
  assert.deepEqual(partition.spent.map((item) => item.subject.key), ["notebook"]);
});

test("已处理:写「已处理，更新后核验」,带上读侧认出的原因;认不出的原因不说", () => {
  const reasons = {
    source_deleted: "已处理，更新后核验（来源已删除）",
    source_processing: "已处理，更新后核验（来源正在解析）",
    source_reprocessed: "已处理，更新后核验（来源已重新解析）",
    merge_confirmed: "已处理，更新后核验（已确认合并）",
    relinked: "已处理，更新后核验（已补上关联）",
    something_new: "已处理，更新后核验",
  };
  for (const [reason, text] of Object.entries(reasons)) {
    assert.equal(findingHandledText(finding({ rule_id: "F1", status: "handled", handled_reason: reason })), text, reason);
  }
  assert.equal(findingHandledText(finding({ rule_id: "F1", status: "handled", handled_reason: null })), "已处理，更新后核验");
  // 只有 handled 写这一句:relink_spent / decided_not_same / open 都不写。
  for (const status of ["open", "relink_spent", "decided_not_same", "dismissed"]) {
    assert.equal(findingHandledText(finding({ rule_id: "F5", status, handled_reason: "relinked" })), "", status);
  }
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
});

test("定位标签:全库一条说「全库」;没有名字时用来源此刻的标题,再没有就如实说", () => {
  assert.equal(findingSubjectText(finding({ rule_id: "F5", subject: { kind: "notebook", key: "notebook", label: "" } })), "全库");
  assert.equal(findingSubjectText(finding({ rule_id: "F1", subject: { kind: "source", key: "a", label: " A.pdf " } })), "A.pdf");
  assert.equal(findingSubjectText(finding({
    rule_id: "F1", subject: { kind: "source", key: "a", label: "" }, live_sources: [live("a", "Live.pdf")],
  })), "Live.pdf");
  assert.equal(findingSubjectText(finding({ rule_id: "F1", subject: { kind: "source", key: "a", label: "" } })), "（没有标题）");
});

// ------------------------------------------------------------ 证据

test("证据:F1 共享占比与是否在主体内容里;没有关联时(没有 giant_share)不说主体内容", () => {
  const view = findingEvidenceView(finding({
    rule_id: "F1", evidence: { shared_share: 0.045, giant_share: 0.86, concepts: 16, samples: ["a", "b", "", 3, "a"] },
  }));
  assert.deepEqual(view.lines, ["16 个概念里，只有 4.5% 也出现在其它来源里", "在主体内容里的部分占 86.0%"]);
  assert.deepEqual(view.samples, ["a", "b"]);
  assert.equal(view.sampleLabel, "代表概念");
  const noGraph = findingEvidenceView(finding({ rule_id: "F1", evidence: { shared_share: 0, concepts: 30 } }));
  assert.deepEqual(noGraph.lines, ["30 个概念里，只有 0% 也出现在其它来源里"]);
});

test("证据:两份重复的错文件合成一条 F1,另一份(evidence.partners)挂成附注", () => {
  const view = findingEvidenceView(finding({
    rule_id: "F1",
    evidence: {
      shared_share: 0, concepts: 20,
      partners: [{ source_id: "s2", concepts: 19, shared_share: 0.01, label: "Hymba_full" }],
    },
  }));
  assert.deepEqual(view.notes, ["另有一份重复导入：Hymba_full，两份都建议删除"]);
  const unnamed = findingEvidenceView(finding({ rule_id: "F1", evidence: { partners: [{ source_id: "s3", label: "" }] } }));
  assert.deepEqual(unnamed.notes, ["另有一份重复导入，两份都建议删除"]);
});

test("证据:F2 重合度、共有概念与两侧去留;不上屏「Jaccard」", () => {
  const view = findingEvidenceView(finding({
    rule_id: "F2",
    evidence: {
      jaccard: 0.48, shared_concepts: 96,
      sources: [{ source_id: "a", label: "A", objects: 300, keep: true }, { source_id: "b", label: "", objects: 120, keep: false }],
    },
  }));
  assert.deepEqual(view.lines, [
    "两份共有 96 个概念，概念重合度 48.0%",
    "A：300 个知识对象（建议保留）",
    "（没有标题）：120 个知识对象（建议删除）",
  ]);
  assert.ok(view.lines.every((line) => !/jaccard/i.test(line)));
});

test("证据:F3 只说命中的子信号,孤立偏多写出判为偏多的门槛;附注「重新解析后若仍与本库无关…」", () => {
  const view = findingEvidenceView(finding({
    rule_id: "F3",
    evidence: {
      signals: ["high_isolated", "brand_new_signal"],
      isolated_objects: 40, isolated_share: 0.25, median_isolated_share: 0.04, isolated_share_threshold: 0.12,
      also_off_topic: true,
    },
  }));
  assert.deepEqual(view.lines, ["40 个知识对象没有任何关联（占 25.0%，超过本库判为偏多的门槛 12.0%）"]);
  assert.deepEqual(view.notes, ["重新解析后若仍与本库无关，会转为疑似内容错配"]);
  assert.ok(!view.lines.join("").includes("brand_new_signal"));
  // also_off_topic 不是 true 就不写附注。
  assert.deepEqual(findingEvidenceView(finding({ rule_id: "F3", evidence: { also_off_topic: false } })).notes, []);
});

test("证据:F4 组内名字与各自的知识对象数(样例最多 5 个)", () => {
  const view = findingEvidenceView(finding({
    rule_id: "F4",
    evidence: {
      canonicals: 2, spellings: 2, member_sources: 3,
      members: [{ canonical_id: "K-a", name: "Llama 3.1", objects: 9 }, { canonical_id: "K-b", name: "Llama 31", objects: 2 }],
      canonical_ids: ["K-a", "K-b"],
    },
  }));
  assert.deepEqual(view.lines, ["同一个概念有 2 种写法没有合并，涉及 3 个来源"]);
  assert.deepEqual(view.samples, ["Llama 3.1（9）", "Llama 31（2）"]);
  assert.equal(view.sampleLabel, "组内的名字（知识对象数）");
});

test("证据:F5 全库数与最多的来源", () => {
  const view = findingEvidenceView(finding({
    rule_id: "F5",
    evidence: { isolated_objects: 1830, isolated_share: 0.0439, usable_objects: 41713, top_sources: [{ source_id: "s", label: "A.pdf", isolated_objects: 120 }] },
  }));
  assert.deepEqual(view.lines, ["1,830 个知识对象没有任何关联，占全部 41,713 个的 4.4%"]);
  assert.deepEqual(view.samples, ["A.pdf（120）"]);
});

test("证据:缺字段、类型不对、认不出的规则都不抛错,只少几句话", () => {
  for (const evidence of [{}, { shared_share: "0.1", concepts: NaN, samples: "x", partners: "y", members: 3 }]) {
    for (const rule of ["F1", "F2", "F3", "F4", "F5", "F9"]) {
      const view = findingEvidenceView(finding({ rule_id: rule, evidence }));
      assert.ok(Array.isArray(view.lines) && Array.isArray(view.samples) && Array.isArray(view.notes));
      assert.deepEqual(view.lines, [], `${rule} ${JSON.stringify(evidence)}`);
    }
  }
});

// ------------------------------------------------------------ 建议动作

const CONTEXT = { canWrite: true, pendingMerges: [] };

test("动作:只有 open 出按钮;只读成员 / 已处理 / 已标为正常 / 已判定 / 已经跑过都没有", () => {
  const f1 = finding({
    rule_id: "F1", severity: "high", subject: { kind: "source", key: "a", label: "A" }, subject_refs: ["a"],
    actions: [{ kind: "delete_source", capability: "sources:write", source_id: "a" }], live_sources: [live("a", "A")],
  });
  assert.equal(findingActionPlan(f1, { ...CONTEXT, canWrite: false }).buttons.length, 0);
  for (const status of ["handled", "dismissed", "decided_not_same", "relink_spent", "mystery"]) {
    assert.equal(findingActionPlan({ ...f1, status }, CONTEXT).buttons.length, 0, status);
  }
  const plan = findingActionPlan(f1, CONTEXT);
  assert.deepEqual(plan.buttons.map((spec) => spec.kind), ["open_source", "delete_source"]);
  assert.deepEqual(plan.notes, ["不可撤销，先打开来源核对"]);
});

test("动作:F1 两份重复的错文件各一个删除(按 actions[].source_id,标题取 partners)", () => {
  const f1 = finding({
    rule_id: "F1", severity: "high", subject: { kind: "source", key: "a", label: "Hymba_mineru" }, subject_refs: ["a", "b"],
    evidence: { partners: [{ source_id: "b", label: "Hymba_full" }] },
    actions: [
      { kind: "delete_source", capability: "sources:write", source_id: "a" },
      { kind: "delete_source", capability: "sources:write", source_id: "b" },
    ],
    live_sources: [live("a", "Hymba_mineru"), live("b", "")],
  });
  const plan = findingActionPlan(f1, CONTEXT);
  assert.deepEqual(plan.buttons.map((spec) => `${spec.kind}:${spec.sourceId}:${spec.title}`), [
    "open_source:a:Hymba_mineru", "delete_source:a:Hymba_mineru", "open_source:b:Hymba_full", "delete_source:b:Hymba_full",
  ]);
});

test("动作:F2 两份都能打开,删除只给建议删除的那份;两边都给删除时保留的那份旁边照样不出", () => {
  const f2 = finding({
    rule_id: "F2", severity: "medium", subject: { kind: "source_pair", key: "a|b", label: "A ⇄ B" }, subject_refs: ["a", "b"],
    evidence: { sources: [{ source_id: "a", label: "A", objects: 300, keep: true }, { source_id: "b", label: "B", objects: 100, keep: false }] },
    actions: [{ kind: "delete_source", capability: "sources:write", source_id: "b" }],
    live_sources: [live("a", "A"), live("b", "B")],
  });
  const plan = findingActionPlan(f2, CONTEXT);
  assert.deepEqual(plan.buttons.map((spec) => `${spec.kind}:${spec.sourceId ?? ""}`), [
    "open_source:a", "open_source:b", "delete_source:b",
  ]);
  assert.deepEqual(plan.notes, ["不可撤销，先打开来源核对", "两份内容几乎相同，保留对象更多的一份"]);
  const both = findingActionPlan({
    ...f2,
    actions: [
      { kind: "delete_source", capability: "sources:write", source_id: "a" },
      { kind: "delete_source", capability: "sources:write", source_id: "b" },
    ],
  }, CONTEXT);
  assert.deepEqual(both.buttons.filter((spec) => spec.kind === "delete_source").map((spec) => spec.sourceId), ["b"]);
  // 没有 source_id 的删除动作对不准任何一篇:不出。
  const unaimed = findingActionPlan({ ...f2, actions: [{ kind: "delete_source", capability: "sources:write" }] }, CONTEXT);
  assert.deepEqual(unaimed.buttons.map((spec) => spec.kind), ["open_source", "open_source"]);
});

test("动作:收录来源不出重新解析;已不在的来源不出按钮;正在解析的标出来", () => {
  const f3 = finding({
    rule_id: "F3", severity: "medium", subject: { kind: "source", key: "d", label: "D" }, subject_refs: ["d"],
    actions: [{ kind: "reparse_source", capability: "sources:write", source_id: "d" }],
  });
  // 收录来源只留「打开来源核对」,不出「重新解析」(后端对它的重新解析回 409)。
  assert.deepEqual(
    findingActionPlan({ ...f3, live_sources: [live("d", "D", { source_type: "promotion" })] }, CONTEXT).buttons
      .map((spec) => spec.kind),
    ["open_source"],
  );
  // 读侧没给这一篇此刻的样子:不知道类型就不出删除 / 重新解析,只留打开。
  assert.deepEqual(findingActionPlan(f3, CONTEXT).buttons.map((spec) => spec.kind), ["open_source"]);
  assert.deepEqual(
    findingActionPlan({ ...f3, live_sources: [live("d", "D")] }, CONTEXT).buttons.map((spec) => spec.kind),
    ["open_source", "reparse_source"],
  );
  assert.equal(findingActionPlan({ ...f3, live_sources: [live("d", "D", { exists: false })] }, CONTEXT).buttons.length, 0);
  const processing = findingActionPlan({ ...f3, live_sources: [live("d", "D", { processing: true })] }, CONTEXT);
  assert.deepEqual(processing.processingSourceIds, ["d"]);
  assert.ok(processing.notes.includes("这篇来源正在解析，完成后再处理"));
});

test("动作:F4 只认两侧都在全组 canonical_ids 里的那一对;没有就写「待确认合并里还没有这一组」", () => {
  const f4 = finding({
    rule_id: "F4", subject: { kind: "concept_group", key: "rmsnorm", label: "RMS Norm" },
    evidence: {
      members: [{ canonical_id: "K-rms norm", name: "RMS Norm" }],
      // members 只是样例(≤5);组的成员以全组 canonical_ids 为准。
      canonical_ids: ["K-rms norm", "K-rmsnorm", "K-rms-norm"],
    },
    actions: [{ kind: "review_merge", capability: "kg:write" }],
  });
  const merges = [
    { id: "one-side", canonical_a: "K-rms norm", canonical_b: "K-layer norm", score: 0.8, status: "pending" },
    { id: "match", canonical_a: "K-rms-norm", canonical_b: "K-rmsnorm", score: 0.9, status: "pending" },
  ];
  assert.equal(pendingMergeForFinding(f4, merges)?.id, "match");
  assert.deepEqual(findingActionPlan(f4, { ...CONTEXT, pendingMerges: merges }).buttons, [{ kind: "review_merge", mergeId: "match" }]);
  const none = findingActionPlan(f4, { ...CONTEXT, pendingMerges: [merges[0]] });
  assert.equal(none.buttons.length, 0);
  assert.deepEqual(none.blocked, ["待确认合并里还没有这一组"]);
  // 样例里的 canonical_id 不算数:没有 canonical_ids 就认不出任何一对。
  assert.equal(pendingMergeForFinding({ ...f4, evidence: { members: f4.evidence.members } }, merges), null);
});

test("动作:F5 open 出「补上关联」;结果句用它自己返回的计数", () => {
  const f5 = finding({
    rule_id: "F5", subject: { kind: "notebook", key: "notebook", label: "" },
    evidence: { isolated_objects: 1830 }, actions: [{ kind: "relink", capability: "kg:write" }],
  });
  assert.deepEqual(findingActionPlan(f5, CONTEXT).buttons, [{ kind: "relink" }]);
  assert.equal(findingActionPlan(f5, { ...CONTEXT, canWrite: false }).buttons.length, 0);
  assert.equal(relinkResultText({ edgesAdded: 1200, isolatedAfter: 630 }), "新增 1,200 条关联，还剩 630 个没有关联的知识条目");
});

test("补上关联已经跑过(relink_spent):诊断数据里那一句说现状,M 取这条问题的孤立对象数", () => {
  const spent = finding({
    rule_id: "F5", subject: { kind: "notebook", key: "notebook", label: "" }, status: "relink_spent",
    evidence: { isolated_objects: 1830 }, actions: [{ kind: "relink", capability: "kg:write" }],
  });
  assert.equal(relinkSpentText(spent), "补上关联已经跑过，剩下 1,830 个没有可自动补上的关联；集中在某几个来源的，见疑似解析残缺");
  assert.equal(relinkSpentText({ ...spent, evidence: {} }), "补上关联已经跑过，剩下的没有可自动补上的关联；集中在某几个来源的，见疑似解析残缺");
  assert.equal(findingActionPlan(spent, CONTEXT).buttons.length, 0);
});

// ------------------------------------------------------------ 已通过 / 开发 / 结构

test("已通过:✓ 检查过没发现、– 没法检查、– 部分没判断分开;有问题的规则不出 ✓;规则按引擎原字段读", () => {
  const lines = passedRuleLines(summary({
    rules: {
      F5: { status: "ran", objects_checked: 41713, total: 0 },
      F1: { status: "ran", sources_checked: 84, sources_too_few_concepts: 3, total: 2 },
      S1: { status: "ran", narrow_groups: 14, narrow_group_objects: 220 },
      F4: { status: "skipped", reason: "a_reason_from_the_future", total: 0 },
      F2: { status: "skipped", reason: "fewer_than_two_sources", total: 0 },
      F3: { status: "ran", sources_checked: 84, total: 0 },
    },
  }));
  assert.deepEqual(lines, [
    { ruleId: "F1", state: "partial", text: "疑似内容错配：有 3 个来源概念太少，没有判断" },
    { ruleId: "F2", state: "skipped", text: "疑似重复导入：没法检查，能互相比较的来源不足两个" },
    { ruleId: "F3", state: "clean", text: "没发现解析残缺的来源（检查了 84 个来源）" },
    { ruleId: "F4", state: "skipped", text: "疑似没合并的同名概念：没法检查，这一次没有可检查的内容" },
    { ruleId: "F5", state: "clean", text: "没发现没建立关联的内容（检查了 41,713 个知识对象）" },
  ]);
  assert.deepEqual(passedRuleLines(summary({ present: false })), []);
});

test("已通过:F4 用户判定过不是同一个的组数(groups_decided_apart)单独一行", () => {
  const lines = passedRuleLines(summary({
    rules: { F4: { status: "ran", canonicals_checked: 9400, groups_decided_apart: 3, total: 0 } },
  }));
  assert.deepEqual(lines, [
    { ruleId: "F4", state: "clean", text: "没发现没合并的同名概念（检查了 9,400 个合并后的知识对象）" },
    { ruleId: "F4", state: "decided", text: "疑似没合并的同名概念：已判定不是同一个的有 3 组，不再列为问题" },
  ]);
});

test("已通过区一行:已判定不是同一个 / 已标为正常", () => {
  const f4 = finding({ rule_id: "F4", subject: { kind: "concept_group", key: "l", label: "Llama 3.1" }, status: "decided_not_same" });
  assert.equal(passedRowText(f4), "已判定不是同一个：疑似没合并的同名概念 · Llama 3.1");
  assert.equal(passedRowText({ ...f4, status: "dismissed" }), "已标为正常：疑似没合并的同名概念 · Llama 3.1");
});

test("截断不静默:只保存了前若干条时写在待处理区", () => {
  assert.deepEqual(truncatedRuleNotes(summary({
    rules: {
      F1: { status: "ran", total: 600, persisted: 500, truncated: true },
      F2: { status: "ran", total: 3, persisted: 3, truncated: true },
      F3: { status: "ran", total: 1, persisted: 1, truncated: false },
    },
  })), ["疑似内容错配共 600 条，只保存了前 500 条", "疑似重复导入没能检查完整，列出的不是全部"]);
  assert.deepEqual(truncatedRuleNotes(summary({ present: false })), []);
});

test("需要开发处理:只认 S1、只在测得到影响时出现;例子每项是一组写法", () => {
  assert.deepEqual(devItemViews(summary({
    dev_items: [
      { item_id: "S1", groups: 14, objects: 220, samples: [{ names: ["RMS Norm", "RMSNorm"] }, { names: ["Q-Former", "Qformer"] }, { names: [] }] },
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
  const structure = {
    graph_nodes: 37340, components: 1138, giant_nodes: 27000, usable_objects: 41713,
    giant_objects: 30366, giant_object_share: 0.728, island_components: 1137, island_sources: 14,
  };
  assert.deepEqual(structureSignal(summary({ structure }), { boards_in_giant: 80, boards_equal_component: 12 }), {
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
