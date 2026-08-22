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
  assignmentsIn,
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

test("文件夹遍历带总字节预算，且预算取自 bundleDirTotalBytesLimit 而不是另抄一份数字", () => {
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
    calls.some((call) => call.target === "bundleDirTotalBytesLimit"),
    "预算必须来自 bundleDirTotalBytesLimit（复用 zip 侧同一系数 + 绝对顶，不加容器余量），"
      + "不能在这里写死第二份数字，也不能绕回裸 bundleTotalBytesLimit——顶配部署会放行"
      + "4 GiB，readDirectoryAsBundleFiles 的 Promise.all 会整读进内存耗死标签页"
      + "（codex #518 R6 P2）",
  );
  assert.ok(
    !calls.some((call) => call.target === "bundleTotalBytesLimit"),
    "绕回裸 bundleTotalBytesLimit 会让顶配部署的文件夹预算重新失去绝对顶",
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

test("批量名额闸走 processBundleCandidates，且名额基准是同步 ref 镜像", () => {
  // codex #518 R1 P1：候选默认全选，若在这里直接逐个 processMarkdownCandidate，
  // 单次上传数量上限就只剩 mergeStagedFiles 那道**入列时**的闸——一个合法的两千
  // 条目压缩包会先被全部内联成 base64、再丢掉其中绝大多数。判据因此是「闸有没有
  // 在内联之前」，而不是「有没有算过一个 remaining 变量」：把循环搬回本函数、闸
  // 却留在入列处，是这条最容易发生的回退形态。
  const stage = findFunctionIn(page, "Home", "stageBundleCandidates");
  const calls = callSitesIn(stage);
  const targets = calls.map((call) => call.target);
  assert.ok(
    targets.includes("processBundleCandidates"),
    "必须经带名额预算的 processBundleCandidates 处理候选",
  );
  assert.ok(
    !targets.includes("processMarkdownCandidate"),
    "绕开 processBundleCandidates 直接逐个内联 = 名额闸退回入列时才生效，"
      + "两千条目的合法压缩包会白分配 GB 级 base64",
  );
  const batch = calls.filter((call) => call.target === "processBundleCandidates");
  assert.equal(batch.length, 1, "候选只该被处理一次（攒批单次入列）");
  assert.match(
    batch[0].arguments[3] ?? "",
    /stagedRef\.current/,
    "剩余名额必须从同步 ref 镜像起算；读 render 闭包里的 staged 会在跨 await 的链里"
      + "拿到旧数量，名额算多了闸就形同虚设",
  );
  assert.match(
    batch[0].arguments[3] ?? "",
    /remainingSlots/,
    "预算参数必须真的带上剩余名额",
  );
});

test("图片内联读的是「有效」开关（零值上限等同于关闭），面板提示读同一个判据", () => {
  // codex #518 R1 P2：MINERU_MAX_IMAGE_BYTES=0 / MINERU_MAX_IMAGES_PER_SOURCE=0 是
  // 合法部署值（一张都不存）。直接读 sourceImagesEnabled 会在这类部署上照常内联并
  // 报「N 张已内联」，而服务端把资产全部丢弃。
  const stage = findFunctionIn(page, "Home", "stageBundleCandidates");
  const options = callSitesIn(stage)
    .filter((call) => call.target === "processBundleCandidates")
    .flatMap((call) => call.arguments);
  assert.ok(
    options.some((argument) => argument.includes("sourceImagePairingEnabled")),
    "必须传有效开关 sourceImagePairingEnabled，而不是裸的 sourceImagesEnabled",
  );
  assert.ok(
    !options.some((argument) => /imagesEnabled:\s*sourceImagesEnabled/.test(argument)),
    "裸 sourceImagesEnabled 会漏掉两个零值上限这一半",
  );
  const derived = scopedCalls(page).filter(
    (entry) => entry.target === "bundleImagesEffectivelyEnabled",
  );
  assert.equal(
    derived.length,
    1,
    "有效关闭态只能有一处推导（内联与面板顶部提示读同一个判据），不得各写一份",
  );
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
  assert.ok(targets.includes("rootModals.requestClose"), "closeSourceModal 必须交给唯一 modal close sink");
  const sink = findFunctionIn(page, "Home", "handleRootModalClosed");
  const sinkTargets = callSitesIn(sink).map((call) => call.target);
  assert.ok(sinkTargets.includes("resetStagedIntake"), "source-add close sink 必须清空暂存态");
  const reset = findFunctionIn(page, "Home", "resetStagedIntake");
  assert.ok(
    callSitesIn(reset).some((call) => call.target === "cancelBundleChoice"),
    "统一清空点必须结清挂起的勾选 resolver",
  );
});

// 评审 F2：用户关弹窗（resetStagedIntake）或切库（openNotebook）之后，还在飞的
// zip/文件夹解包链完成时不得把已取消的批次复活进（可能已属于另一个笔记本的）
// 暂存列表。bundleIntakeGenerationRef 是这条契约的世代计数器——两个清理路径各自
// 递增，每条异步链在自己起跑那一刻捕获当前世代，落盘前重新比对。

test("世代计数器由统一 close sink 递增，切库同步撤销 source-add lease", () => {
  const reset = findFunctionIn(page, "Home", "resetStagedIntake");
  assert.ok(
    assignmentsIn(reset).some(
      (a) => a.target === "bundleIntakeGenerationRef.current" && a.operator === "+=",
    ),
    "resetStagedIntake 必须递增 bundleIntakeGenerationRef：它是关弹窗/清空/上传成功/"
      + "新建笔记本共用的统一清空点，不递增就等于让这条契约名存实亡",
  );

  const openTargets = callSitesIn(findFunctionIn(page, "Home", "openNotebook")).map((call) => call.target);
  assert.ok(openTargets.includes("rootModals.beginWorkspaceTransition"));
  const sinkTargets = callSitesIn(findFunctionIn(page, "Home", "handleRootModalClosed"))
    .map((call) => call.target);
  assert.ok(sinkTargets.includes("resetStagedIntake"));
});

test("openNotebook 经 root transition 进入统一 close sink，结清 bundleChoice", () => {
  // codex #518 R3 P2：世代递增只挡了迟到落盘——挂起的 bundleChoice 面板、它的
  // resolver 与忙碌栈帧不会因此自动消失。深链/浏览器导航切库时弹窗未必被
  // closeSourceModal 关过，旧笔记本的勾选面板会悬在新笔记本上，把「添加来源」
  // 入口一直锁死到用户手动确认/取消一个已经不指向当前笔记本的面板。
  const openTargets = callSitesIn(findFunctionIn(page, "Home", "openNotebook")).map((call) => call.target);
  assert.ok(openTargets.includes("rootModals.beginWorkspaceTransition"));
  const sinkTargets = callSitesIn(findFunctionIn(page, "Home", "handleRootModalClosed"))
    .map((call) => call.target);
  assert.ok(sinkTargets.includes("resetStagedIntake"));
  const resetTargets = callSitesIn(findFunctionIn(page, "Home", "resetStagedIntake"))
    .map((call) => call.target);
  assert.ok(resetTargets.includes("cancelBundleChoice"));
});

test("resetStagedIntake 结清挂起的 bundleChoice 勾选（不只是递增世代）", () => {
  // codex #518 R4 P2：resetStagedIntake 是「清空」「上传成功」等入口**直接**调用的
  // 统一清空点（不像 openNotebook 有自己单独的 cancelBundleChoice 调用）。只递增
  // 世代同样挡不住已经挂起的勾选面板——用户点确认时 stageBundleCandidates 会拿它
  // 捕获的旧世代去比对，把这次确认静默丢弃，面板却还悬在弹窗里看不出发生了什么。
  // 结构性不变量：所有 source-add 清理（含切库的 root transition）都收口到本函数，
  // 所以世代递增与 cancelBundleChoice 只能成对发生。
  const reset = findFunctionIn(page, "Home", "resetStagedIntake");
  const targets = callSitesIn(reset).map((call) => call.target);
  assert.ok(
    targets.includes("cancelBundleChoice"),
    "resetStagedIntake 必须调用 cancelBundleChoice，否则「清空」「上传成功」这类"
      + "只调用它、不额外调 cancelBundleChoice 的入口会把用户刚做的勾选静默丢弃",
  );
});

test("每条异步 bundle 链在落盘（入列/写回执/跳过记录）前都比对世代", () => {
  const guarded = [
    "stageIncomingFilesSync",
    "ingestZipFile",
    "ingestDroppedDirectory",
    "handleBundleFiles",
    "stageBundleCandidates",
  ];
  for (const name of guarded) {
    const fn = findFunctionIn(page, "Home", name);
    const conditions = ifConditionsIn(fn);
    assert.ok(
      conditions.some((condition) => condition.includes("bundleIntakeGenerationRef.current")),
      `${name} 必须在写 updateStaged/setBundleReceipts/appendStagedSkipped 之前比对`
        + " bundleIntakeGenerationRef：世代已变说明用户已经取消/切库，整条链的结果"
        + "（含回执）必须静默丢弃，而不是把已取消的文件复活进当前弹窗",
    );
  }
});
