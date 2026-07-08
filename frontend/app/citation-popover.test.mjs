import test from "node:test";
import assert from "node:assert/strict";

import { placeCitationPopover } from "./citation-popover.ts";

const VP = { width: 1200, height: 800 };

test("opens just below the chip, left-aligned, when there is room", () => {
  const p = placeCitationPopover({ top: 100, bottom: 120, left: 50 }, { width: 360, height: 200 }, VP);
  assert.deepEqual(p, { top: 126, left: 50 }); // bottom(120)+gap(6)
});

test("clamps to the right edge so it never overflows", () => {
  const p = placeCitationPopover({ top: 100, bottom: 120, left: 1000 }, { width: 360, height: 200 }, VP);
  assert.equal(p.left, 1200 - 360 - 8); // 832
});

test("clamps to the left margin when the chip is near x=0", () => {
  const p = placeCitationPopover({ top: 100, bottom: 120, left: 2 }, { width: 360, height: 200 }, VP);
  assert.equal(p.left, 8);
});

test("flips above the chip when there is no room below but room above", () => {
  const p = placeCitationPopover({ top: 700, bottom: 720, left: 50 }, { width: 360, height: 200 }, VP);
  assert.equal(p.top, 700 - 6 - 200); // 494, flipped above
});

test("stays below (pinned within viewport) when neither side fits", () => {
  const p = placeCitationPopover({ top: 10, bottom: 30, left: 50 }, { width: 360, height: 200 }, { width: 1200, height: 150 });
  assert.equal(p.top, 8); // degenerate small viewport → clamped to top margin
});
