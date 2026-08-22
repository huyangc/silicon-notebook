// 来源解析完成后的问答解禁接线守卫。
//
// 真机踩过:新建笔记本 → 上传一个文档 → 解析完成,输入框仍禁用,必须手动刷新。
// 根因是 page.tsx 来源轮询 effect 里的取消竞态——最后一个 pending 源翻到
// extracted 的那次 tick 中,setSources 让 hasPending 变 false,effect 随即被
// 清理(cancelled=true),而 ask_available 的重拉(loadNotebookCollection +
// getNotebook)是网络请求、此刻才回来;用 `cancelled` 做闸就会恰好在这条路上
// 跳过 setCurrentNotebook,新库的 ask_available 停在 false,输入框永久禁用。
// (老库 ask_available 本来就是 true,漏刷新只是计数陈旧,所以只有新建库能看见。)
//
// ask-availability.ts 的注释明文依赖这条接线:「来源解析出 chunk 后 ask_available
// 翻真,由来源处理轮询重拉 currentNotebook 自动解禁(page.tsx 的 reachedExtracted
// 分支)」。这里按源码语义钉三条:
//
//   ① reachedExtracted 分支存在,且里面真的有 setCurrentNotebook + reloadCheckup
//      (anti-vacuous:分支被整个删掉/改名时守卫要红,不能空转);
//   ② 分支内不出现 `cancelled`——effect 清理与「用户切库」在闭包里不可区分,
//      按它做闸就是本 bug 本身;
//   ③ 切库守卫改为按 live ref 比对 notebook id(currentNotebookIdRef.current),
//      且该 ref 在渲染期真的被赋值——漏了赋值,ref 恒为 null,刷新永远被跳过,
//      bug 以更安静的形态回归。
//
// 覆盖边界(如实说明):守卫认的是源码文本形态,不是运行时时序;它钉「不用错误的闸、
// 用对的闸」,不复现竞态本身。
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { parseModule } from "../../test-support/semantic-source.mjs";

const printer = ts.createPrinter({
  newLine: ts.NewLineKind.LineFeed,
  removeComments: true,
});

/** source-library hook 里条件含 reachedExtracted 的 if 分支。 */
function reachedExtractedBranches(sourceFile) {
  const branches = [];
  const visit = (node) => {
    if (ts.isIfStatement(node) && node.expression.getText(sourceFile).includes("reachedExtracted")) {
      branches.push(
        printer
          .printNode(ts.EmitHint.Unspecified, node.thenStatement, sourceFile)
          .replace(/\s+/g, " "),
      );
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return branches;
}

test("reachedExtracted 分支重拉 currentNotebook,且不被 effect 清理位挡住", async () => {
  const hook = await parseModule("use-source-library.ts");
  const branches = reachedExtractedBranches(hook);

  // ① anti-vacuous:分支必须存在且承担解禁职责。
  assert.equal(branches.length, 1, "source library hook 应恰有一个 reachedExtracted 分支");
  const branch = branches[0];
  assert.ok(branch.includes("refreshNotebook("), "分支内必须重拉并写回 currentNotebook");
  assert.ok(branch.includes("refreshCheckup("), "分支内必须刷新体检");

  // ② 不得用 cancelled 做闸:hasPending 翻 false 会先清理 effect,网络响应后到,
  //    按 cancelled 判就会在「新库首个文档解析完成」时跳过解禁。
  assert.ok(
    !/\bcancelled\b/.test(branch),
    "reachedExtracted 分支不得读 cancelled——effect 清理≠用户切库,按它做闸会跳过 ask_available 刷新",
  );

  // ③ 切库守卫按 hook 的 exact owner 判定。
  assert.ok(
    branch.includes("owns(owner)"),
    "分支必须按 hook owner 判「用户没切库/换用户」",
  );
});

test("hook live refs 在渲染期赋值，轮询不因 callback identity 重启", async () => {
  const hook = await parseModule("use-source-library.ts");
  const text = hook.getText(hook);
  assert.match(text, /sourcesRef\.current = sources/);
  assert.match(text, /effectsRef\.current = effects/);
  assert.match(text, /\[hasPending, ownerSerial\]/);
});
