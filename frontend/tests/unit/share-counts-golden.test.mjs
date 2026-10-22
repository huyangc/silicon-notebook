// share-counts-golden.test.mjs — 分享弹窗的确数：浏览器就地算（shareCountsFromTurns）与服务端
// 确数端点（backend/app/domain/share_counts.py，经 GlobalAskStore.share_counts）共用同一份
// golden（backend/tests/fixtures/share_counts_golden.json），后端那一侧由
// backend/tests/share_counts_cases.py 在 SQLite 与 PostgreSQL 上逐个场景断言同一组期望值。
//
// 另外两条钉子：
//  · 不超过公开页上限（500 轮）的会话，数字与引入窗口之前的算法**逐项相等**——下面的
//    `legacyCounts` 是那份算法的原样拷贝（不按窗口截），普通长度的会话一个数都不许变；
//  · `countsAfterPublishing`（分享 / 撤销之后弹窗不再问服务端、就地推出的数字）与重新按新
//    水位算出来的一模一样。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  SHARE_SNAPSHOT_MAX_TURNS,
  countsAfterPublishing,
  readServerShareCounts,
  resolveShareBoundary,
  shareCountsFromTurns,
  shareEarlierSentence,
  shareUpdateEarlierSentence,
  withinWatermark,
} from "../../app/conversation-share-disclosure.ts";

const golden = JSON.parse(
  readFileSync(new URL("../../../backend/tests/fixtures/share_counts_golden.json", import.meta.url), "utf8"),
);

const pad = (n) => String(n).padStart(2, "0");
const turnId = (i) => `t${String(i).padStart(4, "0")}`;

function seconds(i, ties) {
  for (const [a, b] of ties || []) if (i > a && i <= b) return a;
  return i;
}

function instant(i, ties) {
  const s = seconds(i, ties);
  return `2026-01-01T${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}+00:00`;
}

/** golden 的展开规则（见 fixture 的 `doc`）。只取浏览器看得到的部分：完成的轮次。 */
function expand(scenario) {
  const turns = [];
  for (let i = 0; i < scenario.turns; i += 1) {
    const images = scenario.images[String(i)] || { anchors: [], citations: [] };
    turns.push({
      answer_id: turnId(i),
      created_at: instant(i, scenario.ties),
      response: {
        anchors: images.anchors.map((asset) => ({ images: [{ asset_id: asset }] })),
        citations: images.citations.map((asset) => ({ images: [{ asset_id: asset }] })),
      },
    });
  }
  const through = scenario.through === null ? "" : scenario.through === "missing" ? "t-missing" : turnId(scenario.through);
  let watermarkId = "";
  let watermarkAt = "";
  if (typeof scenario.watermark === "number") {
    watermarkId = turnId(scenario.watermark);
    watermarkAt = instant(scenario.watermark, scenario.ties);
  } else if (scenario.watermark) {
    watermarkId = "t-gone";
    watermarkAt = instant(scenario.watermark.gone_at, scenario.ties);
  }
  return { turns, through, watermarkId, watermarkAt };
}

for (const scenario of golden.scenarios) {
  test(`share counts golden: ${scenario.name}`, () => {
    const { turns, through, watermarkId, watermarkAt } = expand(scenario);
    const counted = shareCountsFromTurns(turns, through, watermarkId, watermarkAt);
    // 服务端回执（golden 的 `expected` 就是端点的响应体）经同一个读入口，必须与浏览器就地算的一致。
    assert.deepStrictEqual(readServerShareCounts(scenario.expected), counted);
  });
}

// --- 普通长度的会话：与引入窗口之前的算法逐项相等 -------------------------------
//
// 下面两段是窗口之前 summarizeShareDisclosure / summarizeShareUpdate 的原样拷贝（只去掉了
// 个人记忆那一位——它从来不在前端数）。

function legacyClassifier(turns, sharedThroughId, watermark) {
  const id = String(sharedThroughId || "").trim();
  if (!id) return () => true;
  const idx = turns.findIndex((turn) => turn.answer_id === id);
  if (idx >= 0) return (index) => index <= idx;
  return (_index, createdAt) => withinWatermark(createdAt, watermark);
}

function legacySummary(turns, sharedThroughId, watermark) {
  const isShared = legacyClassifier(turns, sharedThroughId, watermark);
  let sharedCount = 0;
  let newCount = 0;
  let imageCount = 0;
  for (let index = 0; index < turns.length; index += 1) {
    const turn = turns[index];
    if (!isShared(index, turn.created_at)) {
      newCount += 1;
      continue;
    }
    sharedCount += 1;
    const response = turn.response || {};
    const assetIds = new Set();
    for (const anchor of response.anchors || []) for (const image of anchor.images || []) if (image.asset_id) assetIds.add(image.asset_id);
    for (const citation of response.citations || []) for (const image of citation.images || []) if (image.asset_id) assetIds.add(image.asset_id);
    imageCount += assetIds.size;
  }
  return { sharedCount, newCount, imageCount };
}

function legacyCounts(turns, through, watermarkId, watermarkAt) {
  const boundary = resolveShareBoundary(turns, through, watermarkId);
  const scoped = boundary.turns;
  const disclosureTurns = (boundary.watermarkAhead || boundary.watermarkUnknown) ? turns : scoped;
  const current = legacySummary(disclosureTurns, watermarkId, watermarkAt);
  const before = legacySummary(scoped, watermarkId, watermarkAt);
  const after = legacySummary(scoped, "", "");
  return {
    totalTurns: turns.length,
    throughId: scoped.length ? scoped[scoped.length - 1].answer_id : "",
    index: boundary.index,
    flags: [boundary.unresolved, boundary.watermarkAhead, boundary.aheadCount, boundary.watermarkBehind, boundary.watermarkUnknown],
    current,
    update: { imageCount: after.imageCount, newImageCount: after.imageCount - before.imageCount },
  };
}

for (const scenario of golden.scenarios.filter((item) => item.turns <= SHARE_SNAPSHOT_MAX_TURNS)) {
  test(`≤${SHARE_SNAPSHOT_MAX_TURNS} 轮的会话与窗口之前的算法逐项相等: ${scenario.name}`, () => {
    const { turns, through, watermarkId, watermarkAt } = expand(scenario);
    const counted = shareCountsFromTurns(turns, through, watermarkId, watermarkAt);
    const legacy = legacyCounts(turns, through, watermarkId, watermarkAt);
    assert.deepStrictEqual({
      totalTurns: counted.totalTurns,
      throughId: counted.throughId,
      index: counted.boundary.index,
      flags: [counted.boundary.unresolved, counted.boundary.watermarkAhead, counted.boundary.aheadCount, counted.boundary.watermarkBehind, counted.boundary.watermarkUnknown],
      current: { sharedCount: counted.current.sharedCount, newCount: counted.current.newCount, imageCount: counted.current.imageCount },
      update: { imageCount: counted.update.imageCount, newImageCount: counted.update.newImageCount },
    }, legacy);
    // 普通长度的会话没有「更早未包含」，也就不可能有「这条回答已滑出公开页」。
    assert.equal(counted.current.earlierCount, 0);
    assert.equal(counted.boundary.answerOmitted, false);
    assert.equal(counted.update.earlierCount, 0);
  });
}

// --- 分享 / 撤销之后就地推出的数字 = 按新水位重算 -------------------------------

for (const scenario of golden.scenarios) {
  test(`countsAfterPublishing 等于按新水位重算: ${scenario.name}`, () => {
    const { turns, through, watermarkId, watermarkAt } = expand(scenario);
    const before = shareCountsFromTurns(turns, through, watermarkId, watermarkAt);
    const after = countsAfterPublishing(before);
    if (!before.boundary.unresolved && before.throughId) {
      // 分享 / 更新：水位钉到边界那一轮。
      const pinned = turns.find((turn) => turn.answer_id === before.throughId);
      assert.deepStrictEqual(after, shareCountsFromTurns(turns, through, pinned.answer_id, pinned.created_at));
    }
    // 撤销：没有水位。
    assert.deepStrictEqual(after, shareCountsFromTurns(turns, through, "", ""));
  });
}

test("readServerShareCounts 拒收形状不对、负数、自相矛盾的回执", () => {
  const good = golden.scenarios.find((item) => item.name === "long_shared_whole_conversation").expected;
  assert.ok(readServerShareCounts(good));
  assert.equal(readServerShareCounts(null), null);
  assert.equal(readServerShareCounts({ ...good, image_count: -1 }), null);
  assert.equal(readServerShareCounts({ ...good, earlier_turns: 1.5 }), null);
  assert.equal(readServerShareCounts({ ...good, boundary_index: -2 }), null);
  assert.equal(readServerShareCounts({ ...good, watermark_ahead: "no" }), null);
  assert.equal(readServerShareCounts({ ...good, through_id: undefined }), null);
  const { total_turns: _dropped, ...missing } = good;
  assert.equal(readServerShareCounts(missing), null);
  assert.equal(readServerShareCounts({ ...good, update_new_image_count: good.update_image_count + 1 }), null);
});

test("更早未包含的两句按现有界面词汇写，并给出确数", () => {
  assert.equal(shareEarlierSentence(120), "公开页只显示最近 500 轮，更早的 120 轮未包含。");
  assert.equal(shareUpdateEarlierSentence(3), "更新后公开页只显示最近 500 轮，更早的 3 轮未包含。");
});

test("滑出公开页只按窗口判：恰好越过窗口下沿的那一条是 omitted，再往后一条仍在页上", () => {
  for (const [name, omitted] of [["one_past_the_page_first_answer_slid_off", true], ["one_past_the_page_second_answer_still_on", false]]) {
    const scenario = golden.scenarios.find((item) => item.name === name);
    const { turns, through, watermarkId, watermarkAt } = expand(scenario);
    const counted = shareCountsFromTurns(turns, through, watermarkId, watermarkAt);
    assert.equal(counted.boundary.watermarkAhead, true, name);
    assert.equal(counted.boundary.answerOmitted, omitted, name);
  }
});
