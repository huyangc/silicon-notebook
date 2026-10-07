import assert from "node:assert/strict";
import test from "node:test";

import {
  SHARE_DISCLOSURE_COUNTS_ERROR,
  SHARE_UPDATE_BOUNDED_COUNTS_ERROR,
  SHARE_UPDATE_COUNTS_ERROR,
  readServerMemoryDisclosure,
  resolveShareBoundary,
  shareMemorySentence,
  shareScopeState,
  shareUpdateMemorySentence,
  summarizeShareDisclosure,
  summarizeShareUpdate,
  withinWatermark,
} from "../../app/conversation-share-disclosure.ts";

// Minimal turn shaped like ConversationDetail["turns"][number]: only the fields
// summarizeShareDisclosure reads (answer_id + created_at + response.anchors/
// citations). Classification is by answer_id position now, NOT timestamp.
function turnId(answerId, createdAt, { anchors = [], citations = [] } = {}) {
  return {
    answer_id: answerId,
    question: "q",
    asked_at: createdAt,
    created_at: createdAt,
    response: { anchors, citations },
  };
}

function turn(createdAt, opts = {}) {
  return turnId(`ans-${createdAt}`, createdAt, opts);
}

function citation(overrides = {}) {
  return { images: [], ...overrides };
}

// --- Memory disclosure red line (设计 §五 consent; M4) ------------------------
//
// M4：个人记忆条数只有服务端一处定义（披露端点），前端不再自己数——Memory 投影命中的
// 引用没有 memory_id，前端数不到。所以这些用例把「服务端的数字」当输入，钉两件事：
//  1) 服务端的数字原样呈现（哪怕前端能看到的 memory_id 与它对不上）；
//  2) 前端看得到的 memory_id 一概不参与计数（变异：把前端计数加回来，下面几条必红）。

test("the memory count is the server number, even when the turns' own memory_ids disagree", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", {
      citations: [citation({ memory_id: "mem-a" }), citation({ memory_id: "mem-a" })],
    }),
    turn("2026-01-01T00:01:00Z", {
      citations: [citation({ memory_id: "mem-b" }), citation({ memory_id: "" })],
    }),
  ];
  // Two distinct memory_ids are visible in the turns, but the server (which also
  // sees Memory-projection hits that carry no memory_id) says 5: 5 is what shows.
  assert.equal(summarizeShareDisclosure(turns, "", "", 5).memoryCount, 5);
});

test("the frontend never counts memory_id itself: server says 0 -> memoryCount 0", () => {
  const turns = [turn("2026-01-01T00:00:00Z", { citations: [citation({ memory_id: "mem-x" })] })];
  assert.equal(summarizeShareDisclosure(turns, "", "", 0).memoryCount, 0);
  // ...and a turn without any memory_id still discloses what the server counted.
  const plain = [turn("2026-01-01T00:00:00Z", { citations: [citation()] })];
  assert.equal(summarizeShareDisclosure(plain, "", "", 2).memoryCount, 2);
});

test("readServerMemoryDisclosure accepts the endpoint shape and rejects anything malformed", () => {
  assert.deepEqual(readServerMemoryDisclosure({ memory_count: 3, new_memory_count: 1 }), {
    memoryCount: 3,
    newMemoryCount: 1,
  });
  // new_memory_count is optional (report endpoint omits it): defaults to 0.
  assert.deepEqual(readServerMemoryDisclosure({ memory_count: 0 }), { memoryCount: 0, newMemoryCount: 0 });
  // Anything else is a failed fetch (null) -> the fallback copy, never a guessed number.
  for (const bad of [null, undefined, "3", [], {}, { memory_count: -1 }, { memory_count: 1.5 },
    { memory_count: "2" }, { memory_count: 2, new_memory_count: -1 }, { memory_count: 2, new_memory_count: "1" },
    // New is a subset of the total: "1 total, 3 new" contradicts itself -> failed fetch, no guessing.
    { memory_count: 1, new_memory_count: 3 }]) {
    assert.equal(readServerMemoryDisclosure(bad), null, JSON.stringify(bad));
  }
});

test("the two disclosure sentences are the product-decided wording", () => {
  assert.equal(shareMemorySentence(3), "公开页会包含 3 条你引用到的个人记忆摘录。");
  assert.equal(shareUpdateMemorySentence(4, 0), "更新后公开页共 4 条你引用到的个人记忆摘录。");
  assert.equal(shareUpdateMemorySentence(4, 2), "更新后公开页共 4 条你引用到的个人记忆摘录（新增 2 条）。");
});

// --- Watermark filter excludes turns AFTER the watermark id (freeze) ----------

test("turns after the watermark id are counted as new, not included in shared counts", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ images: [{ asset_id: "in" }] })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ images: [{ asset_id: "late" }] })] }),
  ];
  // Watermark id at the first turn: the second (later) turn is "new", its images
  // must NOT enter the disclosure — you only publish up to the watermark.
  const d = summarizeShareDisclosure(turns, turns[0].answer_id, turns[0].created_at, 1);
  assert.equal(d.sharedCount, 1);
  assert.equal(d.newCount, 1);
  assert.equal(d.imageCount, 1); // only "in", not "late"
  assert.equal(d.memoryCount, 1); // passed through from the server number
});

test("empty id (unshared preview / afterUpdate) counts every turn", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ images: [{ asset_id: "a" }] })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ images: [{ asset_id: "b" }] })] }),
  ];
  const d = summarizeShareDisclosure(turns, "", "", 2);
  assert.equal(d.sharedCount, 2);
  assert.equal(d.newCount, 0);
  assert.equal(d.imageCount, 2);
  assert.equal(d.memoryCount, 2);
});

// --- Same-instant tie-break: classify by ORDER, not timestamp (codex #522 R3) -
// Two answers at the SAME created_at. The `turns` array order IS the backend
// keyset order (rowid/ordinal tie-break). The watermark points to the EARLIER
// one; the later one must be classified NEW even though its timestamp is equal —
// the exact bug where a pure-timestamp classifier hides it from "更新到最新".

test("a same-instant answer AFTER the watermark id is 'new', not 'shared'", () => {
  const at = "2026-01-01T00:00:00Z";
  const turns = [
    turnId("ans-a", at, { citations: [citation({ images: [{ asset_id: "a" }] })] }),
    turnId("ans-b", at, { citations: [citation({ images: [{ asset_id: "b" }] })] }),
  ];
  // Watermark pinned to ans-a (the earlier tie member); ans-b sorts AFTER it.
  const d = summarizeShareDisclosure(turns, "ans-a", at, 1);
  assert.equal(d.sharedCount, 1);
  assert.equal(d.newCount, 1); // ans-b is NEW despite the equal timestamp
  assert.equal(d.imageCount, 1); // only ans-a's image — ans-b is not yet published
  // Forward-looking update must flag the newly-exposed memory BEFORE the click.
  const preview = summarizeShareUpdate(turns, "ans-a", at, { memoryCount: 2, newMemoryCount: 1 });
  assert.equal(preview.afterUpdate.memoryCount, 2);
  assert.equal(preview.newMemoryCount, 1);
});

// --- Deleted/drifted watermark answer -> timestamp fallback (never crash) ------

test("a shared_through_id absent from turns falls back to the timestamp interval", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ images: [{ asset_id: "in" }] })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ images: [{ asset_id: "late" }] })] }),
  ];
  // Watermark answer was deleted: its id is not in `turns`. Fall back to the
  // created_at interval (mirrors the backend's deleted-watermark fallback).
  const d = summarizeShareDisclosure(turns, "ans-deleted", "2026-01-01T00:00:00Z", 0);
  assert.equal(d.sharedCount, 1);
  assert.equal(d.newCount, 1);
  assert.equal(d.imageCount, 1); // only "in" (created_at <= watermark)
});

// --- Forward-looking "update to latest" disclosure (consent; codex #522 R1) ---
// "更新到最新" pushes the watermark to ALL turns, so its consent judgement is
// "what will this button publish". The server number for the post-watermark range
// (memoryCount) and its increment (newMemoryCount) must be shown BEFORE the click.

test("summarizeShareUpdate takes both memory numbers from the server", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ memory_id: "mem-in" })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ memory_id: "mem-late" })] }),
  ];
  const id = turns[0].answer_id;
  const preview = summarizeShareUpdate(turns, id, turns[0].created_at, { memoryCount: 7, newMemoryCount: 3 });
  assert.equal(preview.afterUpdate.memoryCount, 7); // the server total, not 2 (frontend visible ids)
  assert.equal(preview.newMemoryCount, 3);          // the server increment
});

test("summarizeShareUpdate reports no new memory when the server says 0 new", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ memory_id: "mem-a" })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ memory_id: "mem-b" })] }),
  ];
  const preview = summarizeShareUpdate(turns, turns[0].answer_id, turns[0].created_at, {
    memoryCount: 1,
    newMemoryCount: 0,
  });
  assert.equal(preview.afterUpdate.memoryCount, 1);
  assert.equal(preview.newMemoryCount, 0); // not 1: the frontend's two visible ids don't count
});

test("summarizeShareUpdate flags newly exposed images from post-watermark turns", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ images: [{ asset_id: "a1" }] })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ images: [{ asset_id: "a2" }] })] }),
  ];
  const preview = summarizeShareUpdate(turns, turns[0].answer_id, turns[0].created_at, {
    memoryCount: 0,
    newMemoryCount: 0,
  });
  assert.equal(preview.afterUpdate.imageCount, 2);
  assert.equal(preview.newImageCount, 1);
});

// --- Images: dedup by asset_id per turn, summed; anchors ∪ citations ----------

test("M dedups images by asset_id within a turn and spans anchors and citations", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", {
      anchors: [{ images: [{ asset_id: "a1" }, { asset_id: "a1" }] }], // dup a1
      citations: [citation({ images: [{ asset_id: "a2" }] })],
    }),
    turn("2026-01-01T00:01:00Z", {
      citations: [citation({ images: [{ asset_id: "a3" }] })],
    }),
  ];
  // turn 1: {a1, a2} = 2; turn 2: {a3} = 1 -> 3 summed.
  assert.equal(summarizeShareDisclosure(turns, "", "").imageCount, 3);
});

// --- countsError fallback discloses BOTH images and memory (codex #522 R2 P2) -
// When the conversation detail fails to load the modal can't compute exact
// counts, but the consent surface has TWO halves — attached images AND private
// memory — and dropping either from the fallback is a silent omission. Both
// fallback strings must name both. Mutation guard: deleting 附图 from either
// constant reds this.

test("countsError fallbacks name BOTH images and memory, never just one", () => {
  for (const message of [SHARE_DISCLOSURE_COUNTS_ERROR, SHARE_UPDATE_COUNTS_ERROR]) {
    assert.ok(message.includes("附图"), `fallback must mention images: ${message}`);
    assert.ok(message.includes("个人记忆"), `fallback must mention memory: ${message}`);
  }
});

// --- withinWatermark degrades safe (unparseable -> include, "宁可多披露") ------

test("withinWatermark includes a turn when either timestamp is unparseable", () => {
  assert.equal(withinWatermark("not-a-date", "2026-01-01T00:00:00Z"), true);
  assert.equal(withinWatermark("2026-01-01T00:00:00Z", "garbage"), true);
  assert.equal(withinWatermark("anything", ""), true); // empty watermark = include all
});

// --- 分享边界（每条回答下的分享按钮，T6）------------------------------------
//
// 承重的那一条是 watermarkAhead：后端水位 advance-only（`share_conversation`，
// codex #522 R3），边界排在已发布水位之前一律 409。界面据此决定给不给发布按钮，
// 所以这个分类错了，用户要么点了拿回一句误导的「会话已有变化」，要么本来能推进却
// 被拦住。

test("空边界逐字返回原样 turns —— 会话列表那个按钮的既有语义不变", () => {
  const turns = [turn("2026-01-01T00:00:00Z"), turn("2026-01-01T00:01:00Z")];
  const boundary = resolveShareBoundary(turns, "", "");
  assert.equal(boundary.index, -1);
  assert.equal(boundary.turns, turns); // 同一引用：没有复制、没有裁剪
  assert.equal(boundary.unresolved, false);
  assert.equal(boundary.watermarkAhead, false);
  assert.equal(boundary.aheadCount, 0);
});

test("命中的边界把 turns 截到那条答案（含）为止", () => {
  const turns = [
    turnId("a1", "2026-01-01T00:00:00Z"),
    turnId("a2", "2026-01-01T00:01:00Z"),
    turnId("a3", "2026-01-01T00:02:00Z"),
  ];
  const boundary = resolveShareBoundary(turns, "a2", "");
  assert.equal(boundary.index, 1);
  assert.deepEqual(boundary.turns.map((t) => t.answer_id), ["a1", "a2"]);
  assert.equal(boundary.unresolved, false);
});

test("边界解析不出 → unresolved + 空批次（披露退化成不带数字的兜底，不是算错的数字）", () => {
  const turns = [turnId("a1", "2026-01-01T00:00:00Z")];
  const boundary = resolveShareBoundary(turns, "gone", "");
  assert.equal(boundary.unresolved, true);
  assert.equal(boundary.index, -1);
  assert.deepEqual(boundary.turns, []);
  // 仍不判 ahead：调用方据此保留发布动作（expected 是那条 id 本人，不依赖 turns）。
  assert.equal(boundary.watermarkAhead, false);
});

test("水位越过边界 → watermarkAhead + 多包含的轮数（后端不允许收回，界面必须不给按钮）", () => {
  const turns = [
    turnId("a1", "2026-01-01T00:00:00Z"),
    turnId("a2", "2026-01-01T00:01:00Z"),
    turnId("a3", "2026-01-01T00:02:00Z"),
    turnId("a4", "2026-01-01T00:03:00Z"),
  ];
  const boundary = resolveShareBoundary(turns, "a2", "a4");
  assert.equal(boundary.watermarkAhead, true);
  assert.equal(boundary.aheadCount, 2);
});

test("水位正好在边界上、或还在边界之前 → 不判 ahead（推进是合法的）", () => {
  const turns = [
    turnId("a1", "2026-01-01T00:00:00Z"),
    turnId("a2", "2026-01-01T00:01:00Z"),
    turnId("a3", "2026-01-01T00:02:00Z"),
  ];
  assert.equal(resolveShareBoundary(turns, "a2", "a2").watermarkAhead, false);
  assert.equal(resolveShareBoundary(turns, "a3", "a1").watermarkAhead, false);
});

test("水位答案已被删（id 解析不到）→ 不判 ahead —— 后端那条回归检查同样跳过并推进", () => {
  const turns = [turnId("a1", "2026-01-01T00:00:00Z"), turnId("a2", "2026-01-01T00:01:00Z")];
  const boundary = resolveShareBoundary(turns, "a1", "deleted-answer");
  assert.equal(boundary.watermarkAhead, false);
  assert.equal(boundary.aheadCount, 0);
  assert.deepEqual(boundary.turns.map((t) => t.answer_id), ["a1"]);
});

test("兜底文案两条各自独立：边界模式那句写的是「更新到这一条」而不是「更新到最新」", () => {
  // 用户据以决定要不要按下去的那句话，必须与按钮上写的公开范围一致。
  assert.ok(SHARE_UPDATE_COUNTS_ERROR.includes("更新到最新"));
  assert.ok(SHARE_UPDATE_BOUNDED_COUNTS_ERROR.includes("更新到这一条"));
  assert.ok(!SHARE_UPDATE_BOUNDED_COUNTS_ERROR.includes("更新到最新"));
  // 两句都必须同时提到附图与个人记忆（consent 红线：绝不静默省略其中一面）。
  for (const line of [SHARE_UPDATE_COUNTS_ERROR, SHARE_UPDATE_BOUNDED_COUNTS_ERROR]) {
    assert.ok(line.includes("附图"));
    assert.ok(line.includes("个人记忆"));
  }
});


// --- codex #530 R2：水位相对边界是**五值**不是二值 --------------------------
//
// 「不是 ahead」曾被当成「链接就到这条为止」，于是两种情形被写成了假话：水位停在更早
// 一轮（这条回答还没进链接），以及水位指向本地 turns 里没有的答案（另一个标签页在我们
// 读完详情之后推进了分享）。两种情形下链接与复制按钮都当场可用。

test("水位停在边界之前 → watermarkBehind（这条回答还没进链接）", () => {
  const turns = [
    turnId("a1", "2026-01-01T00:00:00Z"),
    turnId("a2", "2026-01-01T00:01:00Z"),
    turnId("a3", "2026-01-01T00:02:00Z"),
  ];
  const boundary = resolveShareBoundary(turns, "a3", "a1");
  assert.equal(boundary.watermarkBehind, true);
  assert.equal(boundary.watermarkAhead, false);
  assert.equal(boundary.watermarkUnknown, false);
  assert.equal(shareScopeState(boundary, true), "behind");
});

test("水位指向 turns 里没有的答案 → watermarkUnknown，绝不当成「没越界」", () => {
  // 成因之一：另一个标签页在我们读完会话详情之后把分享推进到了新的一轮。
  const turns = [turnId("a1", "2026-01-01T00:00:00Z"), turnId("a2", "2026-01-01T00:01:00Z")];
  const boundary = resolveShareBoundary(turns, "a1", "a3-not-loaded-here");
  assert.equal(boundary.watermarkUnknown, true);
  assert.equal(boundary.watermarkAhead, false);
  assert.equal(boundary.watermarkBehind, false);
  assert.equal(shareScopeState(boundary, true), "unknown");
  // 发布仍不受影响：expected 是用户点的那条 id，服务端要么钉住、要么 409。
  assert.deepEqual(boundary.turns.map((t) => t.answer_id), ["a1"]);
});

test("shareScopeState 五值齐全且互斥", () => {
  const turns = [
    turnId("a1", "2026-01-01T00:00:00Z"),
    turnId("a2", "2026-01-01T00:01:00Z"),
    turnId("a3", "2026-01-01T00:02:00Z"),
  ];
  // 未分享时不看水位。
  assert.equal(shareScopeState(resolveShareBoundary(turns, "a2", ""), false), "unshared");
  assert.equal(shareScopeState(resolveShareBoundary(turns, "a2", "a2"), true), "at");
  assert.equal(shareScopeState(resolveShareBoundary(turns, "a2", "a1"), true), "behind");
  assert.equal(shareScopeState(resolveShareBoundary(turns, "a2", "a3"), true), "ahead");
  assert.equal(shareScopeState(resolveShareBoundary(turns, "a2", "zzz"), true), "unknown");
  // 边界本身解析不出时，「这条回答在不在链接里」同样答不了。
  assert.equal(shareScopeState(resolveShareBoundary(turns, "zzz", "a1"), true), "unknown");
});
