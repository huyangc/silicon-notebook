// 「AI 对这个库的理解」纯逻辑单测(P1-T7)。
//
// 钉的是三件不看渲染就能证明、而渲染测试又证明不牢的事:
//   ① 五块的顺序与补空——服务端只回**写过**的行,面板必须恒定显示这一档的每一块,
//      否则「还没整理出来的那块」在界面上直接消失,用户既看不到它存在也写不进去;
//   ② 字符护栏按**码点**数,与后端 422 同口径;
//   ③ 忙碌位按库隔离,且用的就是那份共享实现(不是抄了一份看起来一样的)。
import test from "node:test";
import assert from "node:assert/strict";

import {
  AGENT_PROFILE_VALUE_MAX_CHARS,
  BASE_LABELS,
  OVERLAY_LABELS,
  PROFILE_BLOCK_HINTS,
  PROFILE_BLOCK_TITLES,
  PROFILE_LABEL_ORDER,
  busyForNotebook,
  claimNotebookSlot,
  emptyUnderstandingBlock,
  isUnderstandingChainBusy,
  orderedUnderstandingBlocks,
  releaseNotebookClaim,
  understandingValueLength,
  understandingValueTooLong,
} from "../../features/agent-profile/profile-model.ts";
import {
  busyForNotebook as sharedBusyForNotebook,
  claimNotebookSlot as sharedClaimNotebookSlot,
  releaseNotebookClaim as sharedReleaseNotebookClaim,
} from "../../features/kg-maintenance/notebook-busy-set.ts";

function block(label, value, revision = 3) {
  return { label, value, evidence: [], revision, updated_at: "", updated_origin: "job" };
}

test("五块的 label 与两档的划分同后端一致，且各有中文标题与说明", () => {
  assert.deepEqual(PROFILE_LABEL_ORDER, [...BASE_LABELS, ...OVERLAY_LABELS]);
  assert.deepEqual([...BASE_LABELS], ["corpus_shape", "key_entities", "corpus_gaps"]);
  assert.deepEqual([...OVERLAY_LABELS], ["retrieval_notes", "usage_gaps"]);
  for (const label of PROFILE_LABEL_ORDER) {
    // 查不到标题时组件整块不渲染 —— 少一条这里就少一块界面,必须在这一层报红。
    assert.ok(PROFILE_BLOCK_TITLES[label], `缺中文标题：${label}`);
    assert.ok(PROFILE_BLOCK_HINTS[label], `缺一句说明：${label}`);
    assert.ok(/[一-龥]/.test(PROFILE_BLOCK_TITLES[label]), `标题不是中文：${label}`);
  }
});

test("块按固定顺序排好，服务端没回的那几块补成空块", () => {
  // 服务端只回写过的行,顺序也不保证与界面一致 —— 故意打乱着喂进来。
  const rows = [block("corpus_gaps", "缺少 2024 年之后的资料"), block("corpus_shape", "封装工艺资料")];

  const ordered = orderedUnderstandingBlocks(rows, BASE_LABELS);

  assert.deepEqual(ordered.map((item) => item.label), [...BASE_LABELS]);
  assert.equal(ordered[0].value, "封装工艺资料");
  assert.equal(ordered[2].value, "缺少 2024 年之后的资料");
  // 没写过的那块:空值 + revision 0(与服务端「行还不存在」的创建约定同值)。
  assert.equal(ordered[1].value, "");
  assert.equal(ordered[1].revision, 0);
  assert.deepEqual(ordered[1], emptyUnderstandingBlock("key_entities"));
});

test("一块都没写过时仍然给满一档的全部块", () => {
  assert.deepEqual(
    orderedUnderstandingBlocks([], OVERLAY_LABELS).map((item) => item.label),
    [...OVERLAY_LABELS],
  );
});

test("不属于本档的行不会混进来", () => {
  const ordered = orderedUnderstandingBlocks([block("retrieval_notes", "先查型号")], BASE_LABELS);
  assert.deepEqual(ordered.map((item) => item.value), ["", "", ""]);
});

test("字符数按码点算，与后端 len() 同口径", () => {
  assert.equal(understandingValueLength("封装工艺"), 4);
  // "👍" 在 JS 里 .length 是 2、在 Python 里 len() 是 1。用 .length 会让前端在还
  // 没到上限时就禁用保存,而用户看不出为什么。
  assert.equal(understandingValueLength("👍"), 1);
  assert.equal(understandingValueLength(""), 0);
});

test("超限判据卡在 400 上：正好 400 放行，401 拒绝", () => {
  assert.equal(AGENT_PROFILE_VALUE_MAX_CHARS, 400);
  assert.equal(understandingValueTooLong("字".repeat(400)), false);
  assert.equal(understandingValueTooLong("字".repeat(401)), true);
  assert.equal(understandingValueTooLong("👍".repeat(400)), false);
});

test("在忙的判据只认 running（服务端没有排队态可看）", () => {
  const job = (status) => ({ status, pending: 0, updated_at: "", failure_reason: "" });
  assert.equal(isUnderstandingChainBusy(job("running")), true);
  assert.equal(isUnderstandingChainBusy(job("done")), false);
  assert.equal(isUnderstandingChainBusy(job("failed")), false);
  assert.equal(isUnderstandingChainBusy(job("idle")), false);
  // 从没被认领过的链是 null —— 冷启动,不是错误,更不是「在忙」。
  assert.equal(isUnderstandingChainBusy(null), false);
  assert.equal(isUnderstandingChainBusy(undefined), false);
});

test("忙碌位按库隔离：点完 A 切到 B 再点一次，两次认领互不覆盖", () => {
  let busy = new Set();
  busy = claimNotebookSlot(busy, "nb-a");
  busy = claimNotebookSlot(busy, "nb-b");
  assert.equal(busyForNotebook(busy, "nb-a"), true);
  assert.equal(busyForNotebook(busy, "nb-b"), true);

  // A 那条迟到的终态只清自己那一格,B 刚置上的忙碌位原样保留。
  busy = releaseNotebookClaim(busy, "nb-a");
  assert.equal(busyForNotebook(busy, "nb-a"), false);
  assert.equal(busyForNotebook(busy, "nb-b"), true);
  assert.equal(busyForNotebook(busy, null), false);
});

test("忙碌位用的就是那份共享实现，不是又抄了一遍", () => {
  // 身份比较而不是行为比较:行为一致的第二份实现照样能通过上面那条用例,而它
  // 迟早会与共享那份分叉(那正是 notebook-busy-set.ts 被抽出来的全部理由)。
  assert.equal(busyForNotebook, sharedBusyForNotebook);
  assert.equal(claimNotebookSlot, sharedClaimNotebookSlot);
  assert.equal(releaseNotebookClaim, sharedReleaseNotebookClaim);
});
