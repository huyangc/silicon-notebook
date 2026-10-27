// 「取消链接分享」两个入口的接线守卫(kick-all 评审 P2-1)。
//
// 取消链接分享不可撤销:链接失效、已加入的成员被移除,他们在这本笔记本里的检索心得与
// Agent 记录随之清空。分享弹窗与「已分享」总览两个入口都必须先过同一个确认弹窗
// (unshare-link-confirm.ts,组件用例测它本身),点一下就执行的接线只存在于 page.tsx,
// 组件用例测不到,所以在这里钉。判据用语义结构,不用行号。
import test from "node:test";
import assert from "node:assert/strict";

import {
  callsIn,
  findFunction,
  importsFrom,
  jsxElements,
  parseModule,
} from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const EXECUTORS = ["handleUnshare", "handleUnshareFromOverview"];

test("没有任何按钮点一下就直接取消链接分享", () => {
  for (const button of jsxElements(page, "button")) {
    const onClick = button.bindings?.onClick ?? "";
    for (const executor of EXECUTORS) {
      assert.ok(
        !new RegExp(`\\b${executor}\\b`).test(onClick),
        `按钮直接调用 ${executor},跳过了确认:${onClick}`,
      );
    }
  }
});

test("两个入口都经同一份确认弹窗,确认之后才执行", () => {
  assert.deepEqual(
    importsFrom(page, "./unshare-link-confirm").map((item) => item.imported).sort(),
    ["UNSHARE_LINK_LABEL", "unshareLinkConfirmation"],
  );
  const pairs = [
    ["confirmUnshareFromShareModal", "handleUnshare"],
    ["confirmUnshareFromOverview", "handleUnshareFromOverview"],
  ];
  for (const [confirm, executor] of pairs) {
    const fn = findFunction(page, confirm);
    assert.ok(fn, `${confirm} 找不到了(改名或删除?)`);
    const calls = callsIn(fn);
    assert.ok(calls.includes("unshareLinkConfirmation"), `${confirm} 没走共用的确认弹窗`);
    assert.ok(calls.includes("openInfoModal"), `${confirm} 没打开确认弹窗`);
    assert.ok(calls.includes(executor), `${confirm} 的确认动作不再执行 ${executor}`);
  }
  const clicks = jsxElements(page, "button").map((button) => button.bindings?.onClick ?? "");
  assert.ok(clicks.includes("confirmUnshareFromShareModal"), "分享弹窗的按钮没接确认");
  assert.ok(
    clicks.some((onClick) => /confirmUnshareFromOverview\(item\)/.test(onClick)),
    "「已分享」总览的按钮没接确认",
  );
});
