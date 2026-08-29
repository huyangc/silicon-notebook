// codex #627 R5 P2 的回归钉:「立即构建」在构建位打满时会被后端停进 slot 等待
// 队列并返回 status:"queued"——前端必须消费这个返回值、如实呈现「已排队」,不能
// 无视响应强报「已开始构建」(AGENTS.md Interactive feedback:动作结果必须如实)。
//
// 判据(语义 AST,经 test-support/semantic-source 消费节点,不做文本位置查询):
//   ① startScaleIndexRebuild 体内把 rebuildScaleIndex(...) 的返回值绑定到变量
//      (不是裸 await 丢弃);
//   ② 存在 `status === "queued"` 的比较(消费该返回值的分支判定);
//   ③ toast 字面量集合里,「已排队」与「已开始」并存——queued 分支的文案含
//      「已排队」且该条不含「已开始」。
// 变异抗性:改回裸 await → ①红;删 queued 分支 → ②红;把排队文案换成
// 「已开始」→ ③红。
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  comparisonsIn,
  findFunction,
  parseModule,
  stringLiterals,
  variableInitializersIn,
} from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const fn = findFunction(page, "startScaleIndexRebuild");

test("rebuildScaleIndex 的返回值必须被接住而不是裸 await 丢弃", () => {
  assert.ok(fn, "page.tsx 里找不到 startScaleIndexRebuild");
  const bindsResult = variableInitializersIn(fn).some(
    (init) => init.initializer.includes("await rebuildScaleIndex("),
  );
  assert.ok(
    bindsResult,
    "startScaleIndexRebuild 必须把 await rebuildScaleIndex(...) 绑定到变量(status 载荷不能丢)",
  );
});

test('必须存在对返回值 status === "queued" 的分支判定', () => {
  // comparisonsIn 的 right 对字符串字面量给的是**裸文本**(无引号),left 是语义文本。
  const hasQueuedComparison = comparisonsIn(fn).some(
    (cmp) =>
      cmp.operator === "===" &&
      cmp.left.endsWith(".status") &&
      cmp.right === "queued",
  );
  assert.ok(
    hasQueuedComparison,
    "缺少 status === \"queued\" 判定——排队受理会被谎报成已开始",
  );
});

test("排队文案说「已排队」且不夹带「已开始」;「已开始构建」文案仍在其自己的分支", () => {
  const toasts = stringLiterals(fn).filter((text) => text.includes("已排队") || text.includes("已开始"));
  const queuedToasts = toasts.filter((text) => text.includes("已排队") && text.includes("构建位"));
  assert.ok(queuedToasts.length >= 1, "找不到 slot 排队专属的「构建位已满，已排队」文案");
  for (const text of queuedToasts) {
    assert.ok(!text.includes("已开始"), `排队文案不得宣称已开始: ${text}`);
  }
  assert.ok(
    toasts.some((text) => text.includes("已开始构建")),
    "既有的「已开始构建」文案锚点消失——文案重构请同步本守卫",
  );
});
