import test from "node:test";
import assert from "node:assert/strict";

import {
  itemSig,
  doneMessage,
  doneSig,
  currentSigs,
  pruneSigs,
  pendingView,
} from "./pending-actions.ts";

const report = { type: "report_outline", notebook_id: "nb1", notebook_name: "NB1", report_id: "r1", title: "T" };
const gov = { type: "governance", notebook_id: "nb2", notebook_name: "NB2", subtype: "merge", count: 334 };
const idx = { type: "index", notebook_id: "nb3", notebook_name: "NB3", state: "suggested" };

test("itemSig is stable per identity; governance count is part of the signature", () => {
  assert.equal(itemSig(report), "report:r1");
  assert.equal(itemSig(gov), "gov:nb2:merge:334");
  assert.equal(itemSig({ ...gov, count: 335 }), "gov:nb2:merge:335"); // 计数变 → 新签名(重现)
  assert.equal(itemSig(idx), "index:nb3:suggested");
  assert.equal(doneSig("nb9"), "done:nb9");
});

test("pendingView: dismissed items are hidden and uncounted", () => {
  const v = pendingView([report, gov, idx], [], [], [itemSig(gov)]);
  assert.deepEqual(v.visibleItems.map(itemSig), ["report:r1", "index:nb3:suggested"]);
  assert.equal(v.unread, 2);
});

test("pendingView: seen items still show but do not count toward unread", () => {
  const seen = [itemSig(report), itemSig(gov), itemSig(idx)];
  const v = pendingView([report, gov, idx], [], seen, []);
  assert.equal(v.visibleItems.length, 3); // 全部仍展示
  assert.equal(v.unread, 0);              // 已读 → 徽标 0
});

test("pendingView: done items count as unread until seen; passed through", () => {
  const done = [{ notebook_id: "nbD", notebook_name: "D", ts: 1 }];
  const v1 = pendingView([], done, [], []);
  assert.equal(v1.unread, 1);
  assert.equal(v1.visibleDone.length, 1);
  const v2 = pendingView([], done, [doneSig("nbD")], []);
  assert.equal(v2.unread, 0); // 已读 done 不计
});

test("currentSigs + pruneSigs bound the stored set to what is present now", () => {
  const cur = currentSigs([report, gov], [{ notebook_id: "nbD", notebook_name: "D", ts: 1 }]);
  assert.deepEqual(cur, ["report:r1", "gov:nb2:merge:334", "done:nbD"]);
  // 旧签名(gov 334 被计数变化淘汰、以及已消失项)从存储里剪掉
  assert.deepEqual(pruneSigs(["gov:nb2:merge:334", "gov:nb2:merge:300", "stale"], cur), ["gov:nb2:merge:334"]);
});

// --- paper_meta / kind-aware done 覆盖(本 diff 新增分支) --------------------

test("itemSig: paper_meta and index on the same notebook do not collide", () => {
  const pm = { type: "paper_meta", notebook_id: "nbX", notebook_name: "X", state: "building" };
  const ix = { type: "index", notebook_id: "nbX", notebook_name: "X", state: "building" };
  assert.equal(itemSig(pm), "paper_meta:nbX:building");
  assert.notEqual(itemSig(pm), itemSig(ix)); // 同 notebook、不同类型 → 不撞签名
});

test("doneSig: kind distinguishes the two completion events; omitting kind is backward-compatible", () => {
  assert.equal(doneSig("nb"), "done:nb");                                  // 旧签名(kind 省略)
  assert.equal(doneSig("nb", "paper_meta_done"), "done:nb:paper_meta_done");
  assert.notEqual(doneSig("nb", "paper_meta_done"), doneSig("nb", "index_done")); // 两种完成事件不撞
});

test("paper metadata completion copy distinguishes all-non-paper runs", () => {
  assert.match(
    doneMessage("paper_meta_done", {
      notebook_name: "Papers",
      stored: 0,
      not_paper: 3,
    }),
    /3 篇均非论文、无需补全/,
  );
  assert.match(
    doneMessage("paper_meta_done", {
      notebook_name: "Papers",
      stored: 2,
      not_paper: 1,
    }),
    /已补全 2 篇,另有 1 篇非论文/,
  );
  assert.equal(doneMessage("unknown", {}), null);
});

test("mixed-kind done items on one notebook are signed and counted independently", () => {
  const done = [
    { notebook_id: "nbM", notebook_name: "M", ts: 1, kind: "index_done" },
    { notebook_id: "nbM", notebook_name: "M", ts: 2, kind: "paper_meta_done" },
  ];
  // 同 notebook 两种 done 各自独立签名(不被折叠成一个)
  assert.deepEqual(currentSigs([], done), ["done:nbM:index_done", "done:nbM:paper_meta_done"]);
  // 都未读 → unread 记 2(不是 1)
  assert.equal(pendingView([], done, [], []).unread, 2);
  // 只把 index_done 标为已读 → 仅 paper_meta_done 仍未读
  assert.equal(pendingView([], done, [doneSig("nbM", "index_done")], []).unread, 1);
});
