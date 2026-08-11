// 命令目录纯装配逻辑的单测。重点覆盖三类「弄错了用户就会被误导」的判断:
//   ① 忙碌态解除判据(长任务按钮红线);
//   ② 成本预告的约数/采样口径;
//   ③ 确认结果的三件事(新增了几条 / 哪些没写入 / 还剩几条)。
import test from "node:test";
import assert from "node:assert/strict";

import {
  CATALOG_TERMINAL_STATUSES,
  catalogApplyBatch,
  catalogApplyOutcome,
  catalogBlocksRestart,
  catalogDismissOutcome,
  catalogEntryLabel,
  catalogOverflowNote,
  catalogPendingReviewNote,
  catalogPreviewCopy,
  catalogProgressText,
  catalogSectionLabel,
  catalogStatusLine,
  dismissReasonText,
  isCatalogBusy,
  isCatalogSettled,
  rejectionText,
} from "../../app/command-catalog-model.ts";

function job(overrides = {}) {
  const progress = {
    sections_total: 0,
    sections_done: 0,
    entries: 0,
    rejected: 0,
    uncovered: 0,
    ...(overrides.progress ?? {}),
  };
  return {
    id: "job-1",
    notebook_id: "nb-1",
    source_id: "src-1",
    status: "running",
    failure_reason: "",
    created_at: "",
    updated_at: "",
    finished_at: "",
    ...overrides,
    progress,
  };
}

function preview(overrides = {}) {
  return {
    source_id: "src-1",
    source_title: "工具手册",
    estimated_windows: 12,
    estimated_calls: 18,
    windows_in_prefix: 12,
    skipped_windows_in_prefix: 0,
    sampled: false,
    element_limit: 4000,
    ...overrides,
  };
}

// ------------------------------------------------------------ 忙碌态解除判据

test("终态集合与后端 CATALOG_TERMINAL_STATUSES 逐字一致", () => {
  assert.deepEqual([...CATALOG_TERMINAL_STATUSES].sort(), ["cancelled", "failed", "succeeded"]);
});

test("忙碌态只在任务落终态后解除，POST 在飞的那一段也算忙碌", () => {
  // POST 在飞:job 还没回来，按钮必须已经禁用（否则这一段里用户来得及再点一次）。
  assert.equal(isCatalogBusy(true, null), true);
  // 没有任务 + 没在发起 = 可点。
  assert.equal(isCatalogBusy(false, null), false);
  for (const status of ["queued", "running"]) {
    assert.equal(isCatalogBusy(false, job({ status })), true, `${status} 应保持忙碌`);
  }
  for (const status of ["succeeded", "failed", "cancelled"]) {
    assert.equal(isCatalogBusy(false, job({ status })), false, `${status} 应解除忙碌`);
  }
});

test("未知状态保守判成「还没结束」，不把「点完还能接着点」放回来", () => {
  // 方向是刻意的:后端将来新增一个中间状态时，未知值落进「还在跑」才是安全的一边。
  assert.equal(isCatalogSettled("paused"), false);
  assert.equal(isCatalogBusy(false, job({ status: "paused" })), true);
});

// ------------------------------------------------------------------- 进度文案

test("进度文案在总段数未知时说「准备中」，不报 0/0", () => {
  assert.equal(catalogProgressText(job({ progress: { sections_total: 0 } })), "准备中…");
  assert.equal(
    catalogProgressText(job({ progress: { sections_total: 12, sections_done: 3, entries: 5 } })),
    "已处理 3/12 段 · 已识别 5 条",
  );
});

test("失败展示后端文案原文，为空才退到兜底", () => {
  const line = catalogStatusLine(job({ status: "failed", failure_reason: "模型服务未配置。" }));
  assert.deepEqual(line, { text: "模型服务未配置。", tone: "danger" });
  const fallback = catalogStatusLine(job({ status: "failed", failure_reason: "" }));
  assert.equal(fallback.tone, "danger");
  assert.ok(fallback.text.length > 0);
});

test("取消不复述后端那句同义文案，自己写一句更干净的", () => {
  const line = catalogStatusLine(job({ status: "cancelled", failure_reason: "……已取消。" }));
  assert.equal(line.tone, "info");
  assert.ok(!line.text.endsWith("。"), `不该把带句号的整句拼进卡片：${line.text}`);
});

test("零产出必须指向未通过条目——那是用户唯一能自己判断成因的依据", () => {
  const withRejects = catalogStatusLine(
    job({ status: "succeeded", progress: { sections_total: 8, entries: 0, rejected: 7 } }),
  );
  assert.match(withRejects.text, /7 条未通过校验/);
  assert.equal(withRejects.tone, "danger");
  const clean = catalogStatusLine(
    job({ status: "succeeded", progress: { sections_total: 8, entries: 0, rejected: 0 } }),
  );
  assert.match(clean.text, /可能不是命令手册/);
});

test("成功摘要同时报出未通过与未覆盖，零值不占位", () => {
  const full = catalogStatusLine(
    job({ status: "succeeded", progress: { sections_total: 8, entries: 20, rejected: 3, uncovered: 2 } }),
  );
  assert.equal(full.text, "已识别 20 条命令 · 3 条未通过 · 2 段未覆盖");
  const clean = catalogStatusLine(
    job({ status: "succeeded", progress: { sections_total: 8, entries: 20 } }),
  );
  assert.equal(clean.text, "已识别 20 条命令");
});

test("入口按钮只在成功后改成「查看识别结果」", () => {
  assert.equal(catalogEntryLabel(null), "识别命令目录");
  assert.equal(catalogEntryLabel(job({ status: "succeeded" })), "查看识别结果");
  for (const status of ["running", "failed", "cancelled"]) {
    assert.equal(catalogEntryLabel(job({ status })), "识别命令目录");
  }
});

// ------------------------------------------------------------------- 成本预告

test("成本预告写成约数，说明会通读全文并给出预计调用数", () => {
  const copy = catalogPreviewCopy(preview());
  assert.match(copy.body, /将通读全文（约 12 段），预计约 18 次模型调用。/);
  assert.equal(copy.confirmLabel, "开始识别");
  assert.deepEqual(copy.sections, []);
});

test("零成本跳过闸挡下的段数 >0 时附一句纯叙述部分不消耗调用的提示，为 0 时不渲染", () => {
  const withSkips = catalogPreviewCopy(preview({ skipped_windows_in_prefix: 5 }));
  assert.match(withSkips.body, /其中纯叙述部分不消耗调用。/);
  const noSkips = catalogPreviewCopy(preview({ skipped_windows_in_prefix: 0 }));
  assert.ok(!noSkips.body.includes("纯叙述部分"));
});

test("采样到顶时只对已读的那几段报价，不替没读过的段落承诺次数", () => {
  // 旧文案给的是「未见段落按每段 1 次」算出来的总数，再补一句「实际可能更多」
  // ——两句自相矛盾，而且那个总数本身两个方向都错得了：跳过闸让纯叙述的段免费，
  // 参数密集的段一段要好几次。所以现在只报已读那几段，其余给方向。
  const sampled = catalogPreviewCopy(
    preview({ sampled: true, estimated_calls: 5, windows_in_prefix: 3 }),
  );
  assert.match(sampled.body, /按开头 3 段估算需约 5 次模型调用/);
  assert.match(sampled.body, /其余段落视内容而定/);
  assert.ok(!sampled.body.includes("实际可能更多"));
  // 未采样时前缀就是全文，那个次数覆盖每一段，不需要任何方向性说明。
  const exact = catalogPreviewCopy(preview({ sampled: false }));
  assert.ok(!exact.body.includes("其余段落"));
  assert.ok(!exact.body.includes("按开头"));
});

test("采样到顶时段数是下界，写「至少约」；前缀覆盖全文时是精确值，写「约」", () => {
  // 段数在 sampled 侧是下界——元素装不满一段留下的空隙、候选过密时的拆段，都
  // 只会让真实段数更多。写成「约」会把一个只往上跑的下界读成估计值。
  const sampled = catalogPreviewCopy(preview({ sampled: true }));
  assert.match(sampled.body, /全文至少约 12 段/);
  const exact = catalogPreviewCopy(preview({ sampled: false }));
  assert.match(exact.body, /将通读全文（约 12 段）/);
  assert.ok(!exact.body.includes("至少约"));
});

// --------------------------------------------------------------------- 候选出处

test("有继承面包屑的候选出处原样显示", () => {
  assert.equal(catalogSectionLabel("第 3 章 > set_db"), "第 3 章 > set_db");
});

test("后端内部占位标签换成带序号的中文说法，不把裸英文漏给用户", () => {
  assert.equal(catalogSectionLabel("window 3"), "第 3 段");
  assert.equal(catalogSectionLabel("window 12"), "第 12 段");
  assert.ok(!catalogSectionLabel("window 3").includes("window"));
});

test("空串原样传回，由调用方按既有兜底渲染", () => {
  assert.equal(catalogSectionLabel(""), "");
});

// ------------------------------------------------------------------- 未通过原因

test("未通过原因翻成中文，且带上原文里的那个值", () => {
  assert.equal(
    rejectionText("arg", "dash_stripped", "density"),
    "参数「density」：少了前面的短横（原文写作 -xxx）",
  );
  assert.equal(
    rejectionText("command_name", "not_in_candidates", "set_db"),
    "命令名「set_db」：这一段没有记录这条命令",
  );
  assert.equal(rejectionText("slice", "model_response_unusable", ""), "整段：模型返回无法解析");
});

test("分批提取的两条新原因不把用户指回原文去找一个明明存在的参数", () => {
  // 后端 R2 修复:一批参数只判这一批。两条都不是「原文里没有」,所以文案不能
  // 落在原文上——否则用户会去原文里找一个确实写着的参数。
  assert.equal(
    rejectionText("arg", "arg_not_returned", "-pad_right"),
    "参数「-pad_right」：原文里有，但模型没有提取",
  );
  assert.equal(
    rejectionText("arg", "arg_outside_slice", "-flag_25"),
    "参数「-flag_25」：不在这一批要提取的参数里",
  );
});

test("未知字段/原因退到中性词，绝不把内部枚举名泄漏上屏", () => {
  const text = rejectionText("brand_new_field", "brand_new_reason", "x");
  assert.ok(!text.includes("brand_new_field"), text);
  assert.ok(!text.includes("brand_new_reason"), text);
});

// ------------------------------------------------------------------- 已跳过原因

test("已跳过原因翻成中文", () => {
  assert.equal(dismissReasonText("conflict_existing_row"), "已存在同名行");
  // R7:显式跳过（.../dismiss 端点写的原因码）与 apply 冲突自动跳过是两个不同
  // 的写者、两条不同的文案——审阅者不该把「我自己跳过的」读成「已存在同名行」。
  assert.equal(dismissReasonText("user_dismissed"), "人工跳过");
});

test("未知已跳过原因退到中性词，绝不把内部枚举名泄漏上屏", () => {
  const text = dismissReasonText("brand_new_reason");
  assert.ok(!text.includes("brand_new_reason"), text);
});

// ------------------------------------------------------------------- 跳过结果

test("跳过结果归纳：报出跳过了几条、还剩几条待审阅，从不带冲突或去表入口", () => {
  const summary = catalogDismissOutcome({ dismissed: ["c1", "c2"], pending_remaining: 3 });
  assert.equal(summary.headline, "已跳过 2 条候选");
  assert.equal(summary.remainingNote, "还有 3 条待审阅，可以继续确认或跳过。");
  assert.equal(summary.conflictNote, "");
  assert.equal(summary.canOpenTable, false);
});

test("跳过 0 条时报「没有可跳过的候选」，不留下待审阅提示", () => {
  const summary = catalogDismissOutcome({ dismissed: [], pending_remaining: 0 });
  assert.equal(summary.headline, "没有可跳过的候选");
  assert.equal(summary.remainingNote, "");
});

// ------------------------------------------------------------------- 确认结果

test("多选超过单次上限时按列表顺序切前 100 条，剩下的留给下一次", () => {
  const loaded = Array.from({ length: 150 }, (_, i) => ({ id: `c${i}`, position: i }));
  const selected = new Set(loaded.map((item) => item.id));
  const batch = catalogApplyBatch(selected, loaded, 100);
  assert.equal(batch.length, 100);
  assert.equal(batch[0], "c0");
  assert.equal(batch.at(-1), "c99");
});

test("提交顺序按 position，不按勾选顺序（列表看到的就是提交的）", () => {
  const loaded = [
    { id: "b", position: 5 },
    { id: "a", position: 1 },
    { id: "c", position: 9 },
  ];
  assert.deepEqual(catalogApplyBatch(new Set(["c", "a", "b"]), loaded, 10), ["a", "b", "c"]);
  assert.deepEqual(catalogApplyBatch(new Set(["c"]), loaded, 10), ["c"]);
  assert.deepEqual(catalogApplyBatch(new Set(), loaded, 10), []);
});

test("确认结果三件事齐全：新增几条 / 哪些没写入 / 还剩几条", () => {
  const outcome = catalogApplyOutcome({
    table_id: "t-1",
    table_title: "命令目录：工具手册",
    created: true,
    applied: ["c1", "c2"],
    rows_added: 100,
    conflicts: [
      { candidate_id: "c9", command_name: "set_db" },
      { candidate_id: "c8", command_name: "report_timing" },
    ],
    pending_remaining: 200,
  });
  assert.match(outcome.headline, /已新建《命令目录：工具手册》并写入 100 条命令/);
  assert.match(outcome.conflictNote, /2 条已存在同名行，未改动，已跳过，不再重复确认：set_db、report_timing/);
  // 300 条候选点一次「确认全部」拿到 100 会以为做完了 —— 剩余必须显式提示。
  assert.equal(outcome.remainingNote, "还有 200 条待审阅，可以继续确认。");
  assert.equal(outcome.canOpenTable, true);
});

test("表名读后端 table_title——论文来源的上传文件名与实际建表标题可以不同", () => {
  // R15(codex PR #412 评审第 15 轮，P2）：headline 必须原样使用后端返回的
  // table_title，而不是调用方自己从来源标题（论文来源的上传文件名）拼出来的
  // 预测；这里 table_title 刻意是「接地论文标题」，与「上传文件名」不同字面，
  // 证明摘要跟着后端值走。
  const outcome = catalogApplyOutcome({
    table_id: "t-1",
    table_title: "命令目录：A Grounded Paper Title",
    created: true,
    applied: ["c1"],
    rows_added: 1,
    conflicts: [],
    pending_remaining: 0,
  });
  assert.match(outcome.headline, /已新建《命令目录：A Grounded Paper Title》并写入 1 条命令/);
});

test("既有表只说「新增」，不说「新建」", () => {
  const outcome = catalogApplyOutcome({
    table_id: "t-1",
    table_title: "命令目录：工具手册",
    created: false,
    applied: ["c1"],
    rows_added: 1,
    conflicts: [],
    pending_remaining: 0,
  });
  assert.match(outcome.headline, /已向《命令目录：工具手册》新增 1 条命令/);
  assert.equal(outcome.conflictNote, "");
  assert.equal(outcome.remainingNote, "");
});

test("零新增不给「去看这张表」入口——那次确认没有任何东西可看", () => {
  const outcome = catalogApplyOutcome({
    table_id: "",
    table_title: "",
    created: false,
    applied: [],
    rows_added: 0,
    conflicts: [{ candidate_id: "c1", command_name: "set_db" }],
    pending_remaining: 0,
  });
  assert.equal(outcome.headline, "没有新增命令");
  assert.equal(outcome.canOpenTable, false);
  assert.match(outcome.conflictNote, /1 条已存在同名行/);
});

test("冲突很多时只列前几条，不把一屏命令名倒进提示里", () => {
  const conflicts = Array.from({ length: 9 }, (_, i) => ({
    candidate_id: `c${i}`,
    command_name: `cmd_${i}`,
  }));
  const outcome = catalogApplyOutcome({
    table_id: "t-1",
    table_title: "命令目录：手册",
    created: false,
    applied: [],
    rows_added: 0,
    conflicts,
    pending_remaining: 0,
  });
  assert.match(
    outcome.conflictNote,
    /^9 条已存在同名行，未改动，已跳过，不再重复确认：cmd_0、cmd_1、cmd_2、cmd_3、cmd_4 等 9 条$/,
  );
});

// ------------------------------------------------------------- R6 P1:重新发起拦截

test("上一轮成功且还有待审阅候选时拦住「重新识别」", () => {
  assert.equal(
    catalogBlocksRestart(job({ status: "succeeded", progress: { pending_candidates: 3 } })),
    true,
  );
});

test("上一轮成功但候选都已审完（0 条待审）不拦", () => {
  assert.equal(
    catalogBlocksRestart(job({ status: "succeeded", progress: { pending_candidates: 0 } })),
    false,
  );
});

// R6 P1:codex 评审推翻了 R5「失败/取消不拦」的结论——INTERRUPTED_MESSAGE 那句
// 「可重新发起识别」的承诺兑现不了，`.../job` 只认最近一次任务，失败/取消后
// 立刻重新发起同样会把候选永久孤儿化。现在这条判据覆盖任意终态。
test("失败/取消且还有待审阅候选时同样拦住（走的是主按钮「识别命令目录」）", () => {
  for (const status of ["failed", "cancelled"]) {
    assert.equal(
      catalogBlocksRestart(job({ status, progress: { pending_candidates: 5 } })),
      true,
      `${status} 该被这条判据拦住`,
    );
  }
});

test("失败/取消但候选都已审完（0 条待审）不拦", () => {
  for (const status of ["failed", "cancelled"]) {
    assert.equal(
      catalogBlocksRestart(job({ status, progress: { pending_candidates: 0 } })),
      false,
      `${status} 候选清空后不该被拦`,
    );
  }
});

test("排队/运行中不拦——那是单飞守卫的地盘，不归这条判据管", () => {
  assert.equal(catalogBlocksRestart(null), false);
  for (const status of ["running", "queued"]) {
    assert.equal(
      catalogBlocksRestart(job({ status, progress: { pending_candidates: 5 } })),
      false,
      `${status} 不该被这条判据拦住`,
    );
  }
});

test("拦截提示文案带上具体条数", () => {
  assert.equal(
    catalogPendingReviewNote(3),
    "仍有 3 条待审阅候选，请先确认或跳过后再重新识别。",
  );
});

// --------------------------------------------------------- R5 P2:拦截账目溢出小字

test("两个溢出各自独立成句，都为 0 时不渲染（空串）", () => {
  assert.equal(catalogOverflowNote(0, 0), "");
  assert.equal(catalogOverflowNote(2, 0), "另有 2 条拦截记录未显示");
  assert.equal(catalogOverflowNote(0, 5), "有 5 段参数描述因超长被截");
  assert.equal(
    catalogOverflowNote(2, 5),
    "另有 2 条拦截记录未显示；有 5 段参数描述因超长被截",
  );
});
