import assert from "node:assert/strict";
import test from "node:test";

import {
  FOLLOWUP_SCOPE_FELL_BACK_NOTE,
  followupScopeSelections,
  linkedReportCheckKey,
  linkedReportFromActiveJob,
  linkedReportFromTurn,
  linkedReportLabel,
  linkedReportTagText,
  linkedReportReceiptName,
  linkedReportReceiptText,
  restoredLinkedReport,
} from "../../app/linked-report.ts";
import { baseScopePayload, sourceScopePayload } from "../../app/source-scope.ts";


function turn(receipt) {
  return {
    answer_id: "a",
    question: "q",
    asked_at: "2026-10-21T00:00:00Z",
    created_at: "2026-10-21T00:00:00Z",
    response: receipt ? { linked_report: receipt } : {},
  };
}

function receipt(overrides = {}) {
  return { report_id: "report-1", title: "电池安全综述", status: "read", ...overrides };
}

function activeJob(linkedReport) {
  return {
    job_id: "job-1",
    question: "in flight",
    asked_at: "2026-10-21T00:00:00Z",
    mode: "chunk",
    trace: [],
    ...(linkedReport ? { linked_report: linkedReport } : {}),
  };
}


test("reopening restores the in-flight question's report first, else the last answer's", () => {
  const turns = [turn(receipt({ report_id: "report-old", title: "旧报告" })), turn(receipt())];
  assert.deepEqual(restoredLinkedReport({ turns }), { id: "report-1", title: "电池安全综述" });

  // The question still running was asked with another report: that one wins.
  assert.deepEqual(
    restoredLinkedReport({ turns, active_job: activeJob({ report_id: "report-2", title: "新报告" }) }),
    { id: "report-2", title: "新报告" },
  );
  // It was asked WITHOUT a report: the tag had been removed before asking, and
  // that removal is a fact now — the last answer's report does not come back.
  assert.equal(restoredLinkedReport({ turns, active_job: activeJob(null) }), null);
  // A deleted report still names its id on the job (the tag's check removes it).
  assert.deepEqual(
    restoredLinkedReport({ turns: [], active_job: activeJob({ report_id: "report-3", title: "" }) }),
    { id: "report-3", title: "" },
  );
});


test("only the LAST answer counts: asking without the tag ends its stickiness", () => {
  const turns = [turn(receipt()), turn(null)];
  assert.equal(restoredLinkedReport({ turns }), null);
  assert.equal(restoredLinkedReport({ turns: [] }), null);
  // The summary's snapshot of the last receipt stands in when the turn has none.
  assert.deepEqual(
    restoredLinkedReport({ turns: [], linked_report: { report_id: "report-9", title: "九" } }),
    { id: "report-9", title: "九" },
  );
});


test("a failed read's receipt restores the report with an empty title", () => {
  const failed = receipt({ status: "skipped", title: "", reason_code: "read_failed", reason: "本轮不读取这份报告：读取报告时出错，请重试" });
  assert.deepEqual(linkedReportFromTurn(turn(failed)), { id: "report-1", title: "" });
  assert.equal(linkedReportFromTurn(turn(null)), null);
  assert.equal(linkedReportFromActiveJob(undefined), null);
  assert.equal(linkedReportFromActiveJob(activeJob({ report_id: "" })), null);
});


test("a reading turn's receipt says what it read and what it restored", () => {
  const sections = (flags) => flags.map((read, i) => ({ index: i + 1, title: `节${i + 1}`, read }));
  assert.equal(
    linkedReportReceiptText(receipt({ sections: sections([true, true]) })),
    "已读取报告全文",
  );
  assert.equal(
    linkedReportReceiptText(receipt({ sections: sections([false, true, true, false]) })),
    "已读取执行摘要与第 2、3 节（共 4 节）",
  );
  assert.equal(
    linkedReportReceiptText(receipt({ sections: sections([false, false]) })),
    "已读取执行摘要与各节标题（共 2 节，各节正文未读入）",
  );
  assert.equal(
    linkedReportReceiptText(receipt({ sections: sections([true, true]), summary_truncated: true })),
    "已读取执行摘要（已截断）与全部 2 节",
  );
  assert.equal(linkedReportReceiptText(receipt({ sections: [] })), "已读取报告全文");
  assert.equal(
    linkedReportReceiptText(receipt({ sections: [], summary_truncated: true })),
    "已读取执行摘要（已截断）",
  );
  assert.equal(
    linkedReportReceiptText(receipt({
      sections: sections([true]),
      evidence_restored: 18,
      evidence_gone: 2,
      evidence_over_budget: 3,
    })),
    "已读取报告全文；复原引用证据 18 条，2 条已失效，3 条超出篇幅未读入",
  );
  assert.equal(
    linkedReportReceiptText(receipt({ sections: sections([true]), evidence_restored: 0, evidence_gone: 4 })),
    "已读取报告全文；复原引用证据 0 条，4 条已失效",
  );
});


test("a skipped turn shows the server's reason as is, never a reason code", () => {
  const reason = "本轮不读取这份报告：它引用的 3 份资料不在当前检索范围内";
  assert.equal(
    linkedReportReceiptText(receipt({ status: "skipped", reason_code: "scope_sources", reason })),
    reason,
  );
  const bare = linkedReportReceiptText(receipt({ status: "skipped", reason_code: "scope_sources" }));
  assert.equal(bare, "本轮没有读取这份报告");
  assert.equal(bare.includes("scope_sources"), false);
});


test("the pre-send check identity changes with the report, the engine and either scope", () => {
  const source = { mode: "exclude", source_ids: [] };
  const base = { mode: "exclude", notebook_ids: [] };
  const key = linkedReportCheckKey("report-1", "chunk", source, base);
  assert.equal(linkedReportCheckKey("report-1", "chunk", { ...source }, { ...base }), key);
  for (const other of [
    linkedReportCheckKey("report-2", "chunk", source, base),
    linkedReportCheckKey("report-1", "reasoning", source, base),
    linkedReportCheckKey("report-1", "chunk", { mode: "include", source_ids: [] }, base),
    linkedReportCheckKey("report-1", "chunk", { mode: "exclude", source_ids: ["s1"] }, base),
    linkedReportCheckKey("report-1", "chunk", source, { mode: "include", notebook_ids: ["lib"] }),
  ]) {
    assert.notEqual(other, key);
  }
});


function followup(overrides = {}) {
  return {
    report_id: "report-1",
    title: "电池安全综述",
    source_scope: { mode: "exclude", source_ids: [] },
    base_scope: { mode: "exclude", notebook_ids: [] },
    dropped_sources: 0,
    dropped_libraries: 0,
    fell_back_to_all: false,
    ...overrides,
  };
}


test("a report generated without narrowing restores both dimensions to everything, silently", () => {
  const result = followupScopeSelections(followup(), ["lib-a", "lib-b"]);
  assert.equal(result.source.allSelected, true);
  assert.equal(result.source.ids.size, 0);
  assert.deepEqual(baseScopePayload(result.base, ["lib-a", "lib-b"]), {
    mode: "exclude",
    notebook_ids: [],
  });
  assert.equal(result.note, "");
});


test("a narrowed report restores its selection and counts what can no longer be ticked", () => {
  const result = followupScopeSelections(followup({
    source_scope: { mode: "include", source_ids: ["s1", "s2"] },
    base_scope: { mode: "include", notebook_ids: ["lib-a"] },
    dropped_sources: 2,
    dropped_libraries: 1,
  }), ["lib-a", "lib-b"]);
  assert.deepEqual(sourceScopePayload(result.source, 5), { mode: "include", source_ids: ["s1", "s2"] });
  assert.deepEqual(baseScopePayload(result.base, ["lib-a", "lib-b"]), {
    mode: "include",
    notebook_ids: ["lib-a"],
  });
  assert.equal(result.note, "已按报告生成时的检索范围勾选；其中 2 份资料、1 个参考库现在无法勾选，已略去");

  // The server's "all but a few" form keeps meaning exactly that.
  const complement = followupScopeSelections(followup({
    source_scope: { mode: "exclude", source_ids: ["s9"] },
  }), []);
  assert.equal(complement.source.allSelected, true);
  assert.deepEqual([...complement.source.ids], ["s9"]);
  assert.equal(complement.note, "");
});


test("a library unmounted since the server answered is dropped here and counted too", () => {
  const result = followupScopeSelections(followup({
    source_scope: { mode: "include", source_ids: ["s1"] },
    base_scope: { mode: "include", notebook_ids: ["lib-a", "lib-gone"] },
  }), ["lib-a"]);
  assert.deepEqual(baseScopePayload(result.base, ["lib-a"]), { mode: "include", notebook_ids: ["lib-a"] });
  assert.equal(result.note, "已按报告生成时的检索范围勾选；其中 1 个参考库现在无法勾选，已略去");
});


test("nothing left to tick in either dimension falls back to everything and says so", () => {
  // The server already fell back (whatever the cause): one neutral sentence.
  const server = followupScopeSelections(followup({ fell_back_to_all: true, dropped_sources: 3 }), ["lib-a"]);
  assert.equal(server.source.allSelected, true);
  assert.equal(server.base.allSelected, true);
  assert.equal(server.note, FOLLOWUP_SCOPE_FELL_BACK_NOTE);

  // Emptied only by this client's mount list: same fallback, same sentence —
  // an empty scope would lock the Ask box.
  const client = followupScopeSelections(followup({
    source_scope: { mode: "include", source_ids: [] },
    base_scope: { mode: "include", notebook_ids: ["lib-gone"] },
  }), ["lib-a"]);
  assert.equal(client.source.allSelected, true);
  assert.equal(client.source.ids.size, 0);
  assert.equal(client.base.allSelected, true);
  assert.equal(client.base.ids.size, 0);
  assert.equal(client.note, FOLLOWUP_SCOPE_FELL_BACK_NOTE);

  // One dimension still has something: no fallback.
  const local = followupScopeSelections(followup({
    source_scope: { mode: "include", source_ids: ["s1"] },
    base_scope: { mode: "include", notebook_ids: [] },
  }), ["lib-a"]);
  assert.equal(local.source.allSelected, false);
  assert.equal(local.base.allSelected, false);
  assert.equal(local.note, "");
});


test("the tag and receipt name carry the title, and fall back to no title rather than an invented one", () => {
  assert.equal(linkedReportLabel("电池安全综述"), "关联报告：电池安全综述");
  assert.equal(linkedReportLabel(""), "关联报告");
  // 来源栏分组的标题已是「关联报告」,标签只放题目;没有题目时如实说没有。
  assert.equal(linkedReportTagText("电池安全综述"), "电池安全综述");
  assert.equal(linkedReportTagText(""), "（无题目）");
  assert.equal(linkedReportReceiptName("电池安全综述"), "关联报告《电池安全综述》");
  assert.equal(linkedReportReceiptName(""), "关联报告");
  // 题目整串保留,不截断(界面只做 CSS 单行省略)。
  const long = "长".repeat(4000);
  assert.equal(linkedReportLabel(long), `关联报告：${long}`);
});
