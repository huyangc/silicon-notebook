// 「添加来源」入列链的接线守卫 —— 钉住三个**只在跨 await 时才现形**、且失败时完全
// 无声的形态：
//
//  1. 入列合并必须从同步 ref 镜像（最新值）起算，不能从 render 闭包里的 state 起算。
//     旧形态下一次选中 pdf + zip，zip 解完（跨了 await）再入列时读到的是「还没有
//     pdf」的旧闭包，非函数式写回就把 pdf 覆盖没了——没有 toast、没有跳过记录。
//  2. 每条异步链都必须有错误出口（被 await，或 .catch(reportError)）。`void f()` 把
//     整条链的失败变成一个未处理 rejection：用户看到的是「拖了没反应」。
//  3. 文件夹遍历必须带总字节预算（零 I/O 预检），否则 readDirectoryAsBundleFiles 会
//     把整棵目录树读进内存。
//
// 判据全部是语义的（AST 上的调用点/实参/父节点形态），不含行号、不读裸源码文本。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import {
  callSitesIn,
  findFunctionIn,
  ifConditionsIn,
  jsxElements,
  parseModule,
  scopedCalls,
} from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");

/** 一个调用表达式是否有错误出口：被 await，或链上接了 .catch / .then。 */
function hasErrorOutlet(call, parents) {
  const parent = parents.get(call);
  if (!parent) return false;
  if (ts.isAwaitExpression(parent)) return true;
  if (ts.isReturnStatement(parent) || ts.isArrowFunction(parent)) return true;
  if (
    ts.isPropertyAccessExpression(parent)
    && (parent.name.text === "catch" || parent.name.text === "then")
  ) {
    return true;
  }
  return false;
}

function collectCalls(sourceFile, names) {
  const parents = new Map();
  const found = [];
  function visit(node, parent) {
    if (parent) parents.set(node, parent);
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && names.has(node.expression.text)) {
      found.push(node);
    }
    ts.forEachChild(node, (child) => visit(child, node));
  }
  visit(sourceFile, undefined);
  return { found, parents };
}

test("入列合并从同步 ref 镜像起算，而不是 render 闭包里的 state", () => {
  const sync = findFunctionIn(page, "Home", "stageIncomingFilesSync");
  const merges = callSitesIn(sync).filter((call) => call.target === "mergeStagedFiles");
  assert.equal(merges.length, 1, "stageIncomingFilesSync 应恰好合并一次（攒批单次入列）");
  assert.equal(
    merges[0].arguments[0],
    "stagedRef.current",
    "合并基准必须是同步 ref 镜像；读 staged/stagedFiles 会在跨 await 的链里拿到旧闭包，"
      + "把同批其它文件静默覆盖掉",
  );
});

test("待上传列表只有 updateStaged 一个写入口（ref 镜像才可能恒等于最新值）", () => {
  const writers = scopedCalls(page)
    .filter((entry) => entry.target === "setStaged")
    .map((entry) => entry.scope);
  assert.deepEqual(
    writers,
    ["<module>.Home.updateStaged"],
    "绕开 updateStaged 直接 setStaged 会让 stagedRef 落后于 state，B1 那类覆盖立刻复发",
  );
});

test("入列/解包的每条异步链都有错误出口（不是被 await 就是接了 .catch）", () => {
  const asyncChains = new Set([
    "stageIncomingFiles",
    "dispatchDroppedEntries",
    "ingestBundleSources",
    "ingestZipFile",
    "ingestDroppedDirectory",
    "handleBundleFiles",
  ]);
  const { found, parents } = collectCalls(page, asyncChains);
  assert.ok(found.length >= 6, `应至少有 6 处调用，实际 ${found.length}（入口被改名？）`);
  const dangling = found
    .filter((call) => !hasErrorOutlet(call, parents))
    .map((call) => call.expression.getText(page));
  assert.deepEqual(dangling, [], "这些调用丢掉了 promise：失败会变成未处理 rejection，用户只看到「拖了没反应」");
});

test("文件夹遍历带总字节预算，且预算取自 bundleTotalBytesLimit 而不是另抄一份数字", () => {
  const ingest = findFunctionIn(page, "Home", "ingestDroppedDirectory");
  const calls = callSitesIn(ingest);
  const collect = calls.filter((call) => call.target === "collectDirectoryFiles");
  assert.equal(collect.length, 1);
  assert.match(
    collect[0].arguments[1] ?? "",
    /maxTotalBytes/,
    "collectDirectoryFiles 必须收到总字节预算，否则整棵目录树会被无界读进内存",
  );
  assert.ok(
    calls.some((call) => call.target === "bundleTotalBytesLimit"),
    "预算必须来自 bundleTotalBytesLimit（复用 zip 侧同一系数），不能在这里写死第二份数字",
  );
});

test("拿不到任何拖放条目时回退扁平文件列表（不是「API 可用就走 entry 分支」）", () => {
  const drop = findFunctionIn(page, "Home", "handleStageDrop");
  const guarded = ifConditionsIn(drop).filter(
    (condition) => condition.includes("entries !== null") && condition.includes("entries.length"),
  );
  assert.equal(
    guarded.length,
    1,
    "webkitGetAsEntry 逐项返回 null 时 entries 是空数组，而 dataTransfer.files 仍可能有内容；"
      + "只判 `entries !== null` 会把整批拖入静默吞掉",
  );
});

test("内联超限的回执带上体积明细（只报总量对用户不可操作）", () => {
  const stage = findFunctionIn(page, "Home", "stageBundleCandidates");
  const targets = callSitesIn(stage).map((call) => call.target);
  assert.ok(
    targets.includes("inlineTooLargeImageLines"),
    "超限分支必须点名最大的几张图片，否则用户唯一能做的就是把整份文档拆开重试",
  );
  assert.ok(targets.includes("bundleFileNamesFor"), "同批同名 md 必须先消歧再入列（否则会被去重折叠）");
});

test("解包的忙碌位成对 push/pop，嵌套帧不被内层的清零抹掉", () => {
  for (const name of ["ingestZipFile", "ingestDroppedDirectory"]) {
    const targets = callSitesIn(findFunctionIn(page, "Home", name)).map((call) => call.target);
    assert.ok(targets.includes("pushBundleBusy"), `${name} 必须取得忙碌位`);
    assert.ok(targets.includes("popBundleBusy"), `${name} 必须在 finally 里释放忙碌位`);
    assert.ok(
      !targets.includes("setBundleBusyLabel"),
      `${name} 不得直接清零忙碌文案：文件夹里含 zip 时内外两帧会嵌套，一律清零会让外层`
        + "的进行态被内层抹掉、入口提前恢复可点",
    );
  }
});

test("等待用户勾选期间清掉进行态文案（否则「请先选择」那支提示永远到不了用户面前）", () => {
  const handle = findFunctionIn(page, "Home", "handleBundleFiles");
  const cleared = callSitesIn(handle).filter(
    (call) => call.target === "setBundleBusyLabel" && call.arguments[0] === "null",
  );
  assert.equal(
    cleared.length,
    1,
    "勾选等待不是「正在解析」：不清掉忙碌文案，sourceFilePickerHint 会一直显示"
      + "「解析压缩包…」，而它其实什么都没在解析",
  );
});

test("弹窗的两个关闭入口（× 与点遮罩）走同一个 handler，且该 handler 结清挂起的勾选", () => {
  const overlay = jsxElements(page, "section")
    .filter((element) => element.attributes?.className === "source-modal");
  assert.equal(overlay.length, 1, "未找到添加来源弹窗的遮罩层（类名被改？守卫失效）");
  assert.match(
    overlay[0].bindings?.onClick ?? "",
    /closeSourceModal/,
    "点遮罩必须走与 × 相同的关闭 handler：只 setSourceModalOpen(false) 会把等待勾选的那条链"
      + "永久挂起，它持有的忙碌位再也不释放",
  );
  const close = findFunctionIn(page, "Home", "closeSourceModal");
  const targets = callSitesIn(close).map((call) => call.target);
  assert.ok(targets.includes("cancelBundleChoice"), "closeSourceModal 必须结清挂起的勾选 resolver");
  assert.ok(targets.includes("resetStagedIntake"), "closeSourceModal 必须清空暂存态");
});
