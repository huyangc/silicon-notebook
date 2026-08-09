import assert from "node:assert/strict";
import test from "node:test";

import { clampPopoverLeft } from "../../app/effort-picker-logic.ts";


// 复现口径:实测量到的那组数——360px 面板里 420px 复合宽下,档位 chip 折行落到行首,
// 232px popover 右对齐后左边溢出面板 90px。下面用同一形状的数字表达各分支。
const POPOVER = 232;
const MARGIN = 8;

const clamp = (over) => clampPopoverLeft({
  popoverWidth: POPOVER, margin: MARGIN, ...over,
});


test("宽松时保持右对齐控件,与报告那侧观感一致", () => {
  // 控件在容器中部:首选位(anchorRight - 宽)完全放得下,不该被挪动。
  const offset = clamp({
    anchorLeft: 500, anchorRight: 580, containerLeft: 100, containerRight: 900,
  });
  assert.equal(offset, 580 - POPOVER - 500);
  // 换算回视口坐标即 anchorRight - popoverWidth。
  assert.equal(500 + offset + POPOVER, 580);
});


test("控件贴容器左缘时夹回左内缘,不再向左溢出", () => {
  // chip 落在行首:右对齐会让 popover 左边跑到 containerLeft 之外。
  const containerLeft = 100;
  const offset = clamp({
    anchorLeft: 120, anchorRight: 200, containerLeft, containerRight: 900,
  });
  const left = 120 + offset;
  assert.equal(left, containerLeft + MARGIN);
  assert.ok(left >= containerLeft, "夹取后不得越过容器左缘");
});


test("控件贴容器右缘时夹回右内缘,不向右溢出", () => {
  // 恰好不折行的那段宽度:chip 右缘贴近容器右缘。
  const containerRight = 400;
  const offset = clamp({
    anchorLeft: 300, anchorRight: 396, containerLeft: 100, containerRight,
  });
  const left = 300 + offset;
  assert.equal(left + POPOVER, containerRight - MARGIN);
  assert.ok(left + POPOVER <= containerRight, "夹取后不得越过容器右缘");
});


test("容器窄到放不下 popover 时贴左内缘(必然溢出,至少保住开头可读)", () => {
  const containerLeft = 100;
  const offset = clamp({
    anchorLeft: 110, anchorRight: 190, containerLeft, containerRight: 260,
  });
  assert.equal(110 + offset, containerLeft + MARGIN);
});
