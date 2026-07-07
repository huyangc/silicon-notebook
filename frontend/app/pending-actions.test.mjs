import test from "node:test";
import assert from "node:assert/strict";

import { itemSig, doneSig, currentSigs, pruneSigs, pendingView } from "./pending-actions.ts";

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
