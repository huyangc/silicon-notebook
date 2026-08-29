// 「打开笔记本连点合并」的接线守卫。
//
// `app/notebook-open-guard.ts` 是纯逻辑,单测已经把它的四条语义钉死了。可这套东西真正
// 会坏的地方不在纯模块里,而在 `page.tsx` 里**三条谁都编译得过、全套现有门也照样全绿**
// 的接线:
//
//  ① 合并早退挪到 `++workspaceEpochRef.current` 之后。此后每一次调用都先把 epoch 顶掉,
//     于是双击的第二下:epoch 已经不是 guard 记的那一代 → 不合并 → 继续跑,但它自己
//     刚顶掉的世代让第一次的 `isCurrent()` 恒假……两次互相顶死,那本库**再也打不开**。
//  ② `settleOpen` 从 `finally` 挪进 `try` 的成功路径。一次 reject(网络抖动/后端 500)
//     就再也不收尾,卡片永久挂着「打开中…」且按钮永久 disabled。
//  ③ `openNotebook` 之外的某处 `workspaceEpochRef.current` 自增忘了作废 guard。那次导航
//     把在途的 open 顶死(isCurrent 恒假 → 永远不 apply/conclude),guard 却还挂着 →
//     之后同一本库的每一次点击都被这个僵尸 guard 吞掉,直到那次死跑的请求自己收尾;
//     api-client 没有 timeout,请求挂死就是**永远打不开**,期间还一直显示假的「打开中…」。
//
// 判据全部走 `controlFlowIn` 给出的**语句序列结构**(if / try{}catch{}finally{} 都是各自
// 独立的子数组),不做任何跨块的文本匹配。⚠ 特别地:不用 `[\s\S]*?` 这类懒惰正则去找
// 「try 之后的 settleOpen」——那种写法会越过 try 的收尾大括号,把「挪进 try 成功路径」
// 误判成合格。块归属在这里是 AST 事实,不是文本距离。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import {
  callSitesIn,
  controlFlowIn,
  findFunctionIn,
  parseModule,
} from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");

/** guard 作废 helper 的名字。改名要连带改这里,否则守卫会响亮失败(而不是静默放行)。 */
const INVALIDATE = "invalidateNotebookOpenGuard";
/** 忙碌位所在的 ref。自增站点就是按它认的。 */
const EPOCH_REF = "workspaceEpochRef.current";
/** 自己带 settle 收尾、因此不需要 helper 的那一个作用域。 */
const SELF_SETTLING_SCOPE = "openNotebook";

/** flow 条目里直接发出的调用名(不下钻子块——下钻由 allCallTargets 负责)。 */
function directCallTargets(entry) {
  return (entry.calls ?? []).map((call) => call.target);
}

/** 递归收集一段 flow(含 if/try/循环/回调子块)里出现的全部调用名。 */
function allCallTargets(entries) {
  const targets = [];
  for (const entry of entries) {
    targets.push(...directCallTargets(entry));
    for (const key of ["then", "else", "try", "catch", "finally", "body", "flow"]) {
      const nested = entry[key];
      if (Array.isArray(nested)) targets.push(...allCallTargets(nested));
    }
  }
  return targets;
}

function countOf(values, wanted) {
  return values.filter((value) => value === wanted).length;
}

// —— ① 合并早退的位置 ————————————————————————————————————————————————

test("shouldCoalesceOpen 早退是 openNotebook 的第一条语句", () => {
  const flow = controlFlowIn(findFunctionIn(page, "Home", "openNotebook"));
  const first = flow[0];
  assert.equal(
    first.kind,
    "if",
    `openNotebook 的第一条语句不再是条件早退（实测 ${first.kind}）`,
  );
  assert.match(
    first.condition,
    /\bshouldCoalesceOpen\(/,
    `第一条语句的条件里没有 shouldCoalesceOpen：${first.condition}`,
  );
  assert.deepEqual(
    first.then,
    [{ kind: "return" }],
    "命中合并时必须**只**返回、什么都不做（不得顺手重置任何状态或发请求）",
  );
});

test("早退读的是自增**前**的 epoch，且排在 epoch 自增与 historyMode 计算之前", () => {
  const flow = controlFlowIn(findFunctionIn(page, "Home", "openNotebook"));

  // 早退的条件必须把当前代传给 shouldCoalesceOpen —— 少了这个实参，僵尸 guard 会
  // 重新开始吞点击（纯模块那侧的单测钉的是同一件事的另一半）。
  assert.match(
    flow[0].condition,
    new RegExp(`shouldCoalesceOpen\\([^)]*${EPOCH_REF.replace(".", "\\.")}`),
    `早退没有把当前 epoch 传给 shouldCoalesceOpen：${flow[0].condition}`,
  );

  const bumpIndex = flow.findIndex(
    (entry) => entry.kind === "variables" && entry.declarations.some(
      (item) => (item.initializer ?? "").includes(`++${EPOCH_REF}`),
    ),
  );
  const historyIndex = flow.findIndex(
    (entry) => directCallTargets(entry).includes("historyModeForTransition"),
  );
  // 空转保护：两个参照点都必须真的存在，否则下面两条比较会因为 -1 而恒真。
  assert.notEqual(bumpIndex, -1, "openNotebook 里找不到 workspaceEpoch 自增（守卫失效）");
  assert.notEqual(historyIndex, -1, "openNotebook 里找不到 historyModeForTransition（守卫失效）");
  assert.ok(bumpIndex > 0, `合并早退必须在 epoch 自增之前（自增在第 ${bumpIndex} 条）`);
  assert.ok(historyIndex > 0, `合并早退必须在 historyMode 计算之前（它在第 ${historyIndex} 条）`);
});

// —— ② settleOpen 的块归属 ————————————————————————————————————————————

test("settleOpen 只出现在 finally 块里（不在 try 的成功路径上）", () => {
  const flow = controlFlowIn(findFunctionIn(page, "Home", "openNotebook"));
  const transitions = flow.filter((entry) => entry.kind === "try");
  assert.equal(
    transitions.length,
    1,
    `openNotebook 应当恰好有一个 try/finally（实测 ${transitions.length}）`,
  );
  const transition = transitions[0];

  const everywhere = countOf(allCallTargets(flow), "settleOpen");
  const inFinally = countOf(allCallTargets(transition.finally), "settleOpen");
  const inTry = countOf(allCallTargets(transition.try), "settleOpen");

  assert.equal(inFinally, 1, `finally 块里应当恰好 settleOpen 一次（实测 ${inFinally}）`);
  assert.equal(inTry, 0, "settleOpen 不得出现在 try 里——一次 reject 就再也不收尾");
  // `everywhere` 把 finally 也数在内，所以两者相等 = settleOpen 只在 finally。
  assert.equal(
    everywhere,
    inFinally,
    "settleOpen 出现在了 finally 之外（挪出 try/finally 同样会让失败路径不收尾）",
  );

  // 收尾还要把忙碌位一起还原，否则按钮永久 disabled、卡片永久「打开中…」。
  assert.ok(
    allCallTargets(transition.finally).includes("setOpeningNotebookId"),
    "finally 里没有还原 openingNotebookId",
  );
});

// —— ②b 「换目的地」的调用点必须显式退出合并（或改用 intent） ————————————————
//
// 合并早退与「被顶替」共用 `return false` 这一个返回值,所以凡是**目的地和在途那次
// 不一样**的调用,都必须让合并判据失效——要么传 `{ coalesce: false }` 无条件顶替,
// 要么传一个跟默认 "open" 不同的 `intent`,否则它的意图会被静默丢弃:
//   · popstate:用户在 `#memory=<id>` 按返回 → 这次 openNotebook 若被合并早退,它既不
//     做事也不自增 epoch,在飞的 openNotebookMemory 随后 committed,把 URL 用
//     replaceState 改回 `#memory`——返回键失效,破坏「hash 是唯一真相源」。它要顶替
//     的目的地不固定(可能是 open 也可能是 memory),没有一个 intent 能覆盖,所以继续
//     用 coalesce:false。
//   · openPendingItem / openDoneItem:目的地是报告/治理/索引/来源面板里的具体位置,
//     同库同类型的不同条目之间 intent 也无法安全区分,继续用 coalesce:false。
// 反过来,挂载还原与 onNotebookCreated **刻意**保持默认(合并):那里的重复调用是
// StrictMode 双执行,去重正是想要的。
const EXPLICIT_NO_COALESCE = {
  openPendingItem: "目的地是待办项指向的报告/治理/索引位置",
  openDoneItem: "目的地是来源面板或知识图谱",
  onPopState: "hash 是唯一真相源,返回键必须顶掉在飞的那次 open",
};

test("换目的地的三个调用点都显式传了 { coalesce: false }", () => {
  const offenders = [];
  for (const [scope, why] of Object.entries(EXPLICIT_NO_COALESCE)) {
    const calls = callSitesIn(findFunctionIn(page, "Home", scope))
      .filter((call) => call.target === "openNotebook");
    // 空转保护:一个都没有说明入口被改名/挪走了,守卫必须响亮失败而不是空断言。
    if (calls.length === 0) {
      offenders.push(`${scope}：找不到任何 openNotebook 调用（守卫失效）`);
      continue;
    }
    for (const call of calls) {
      const opts = call.arguments[3];
      if (!opts || !/coalesce:\s*false/.test(opts)) {
        offenders.push(
          `${scope}：openNotebook(${call.arguments.join(", ")}) 没有 { coalesce: false } —— ${why}`,
        );
      }
    }
  }
  assert.deepEqual(offenders, []);
});

test("openNotebookMemory 靠 intent 换目的地,不再传 { coalesce: false }", () => {
  // C1:合并判据升级为「同 id + 同 intent」之后,openNotebookMemory 不必再用
  // coalesce:false 无条件顶替——它只需要一个跟默认 "open" 不同的 intent,让「普通
  // 打开在途时点记忆」放行顶替,同时让「连点同一个记忆链接」照样合并(不再像
  // coalesce:false 那样连点也逐次发出一整套请求,见 C1 的 PR 描述)。
  const calls = callSitesIn(findFunctionIn(page, "Home", "openNotebookMemory"))
    .filter((call) => call.target === "openNotebook");
  assert.notEqual(calls.length, 0, "openNotebookMemory：找不到任何 openNotebook 调用（守卫失效）");
  for (const call of calls) {
    const opts = call.arguments[3];
    assert.ok(
      opts && /intent:\s*["']memory["']/.test(opts),
      `openNotebookMemory：openNotebook(${call.arguments.join(", ")}) 没有传 intent: "memory"`,
    );
    assert.ok(
      !opts || !/coalesce:\s*false/.test(opts),
      `openNotebookMemory：openNotebook(${call.arguments.join(", ")}) 不该再传 coalesce: false`,
    );
  }
});

// —— ③ openNotebook 之外的每个 epoch 自增都要作废 guard ——————————————————
//
// 判据边界（如实写清）：
//   · 发现面是**整份 page.tsx** 的 AST：`++x` / `x++` / `x += …` 等任何对
//     `workspaceEpochRef.current` 的写入都会被认出来，并按最近的具名函数作用域归类。
//     所以「在一个新函数里新增一处自增」一定会被发现，不是靠一张手写清单。
//   · 顺序面用 `controlFlowIn` 的**顶层语句序列**：自增语句的**紧接下一条**必须是
//     `invalidateNotebookOpenGuard()`。这是最严的一档，理由是现在四处写法完全一致，
//     没有必要给「隔几条再清」留口子。
//   · 已知盲区：自增若被塞进 if / 循环 / 回调的子块里，顶层序列扫不到它。那种情况不会
//     静默放行——下面「发现数 == 顶层命中数」那条交叉校验会报红，提示把判据补到子块。
function epochBumpScopes() {
  const scopes = [];
  const stack = [];

  function declaredName(node) {
    if (
      ts.isFunctionDeclaration(node)
      || ts.isMethodDeclaration(node)
      || ts.isClassDeclaration(node)
    ) {
      return node.name?.getText(page);
    }
    if (
      ts.isVariableDeclaration(node)
      && node.initializer
      && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))
    ) {
      return node.name.getText(page);
    }
    return undefined;
  }

  function mutatesEpoch(node) {
    if (
      (ts.isPrefixUnaryExpression(node) || ts.isPostfixUnaryExpression(node))
      && [ts.SyntaxKind.PlusPlusToken, ts.SyntaxKind.MinusMinusToken].includes(node.operator)
    ) {
      return node.operand.getText(page) === EPOCH_REF;
    }
    return Boolean(
      ts.isBinaryExpression(node)
      && node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
      && node.operatorToken.kind <= ts.SyntaxKind.LastAssignment
      && node.left.getText(page) === EPOCH_REF,
    );
  }

  function visit(node) {
    const name = declaredName(node);
    if (name) stack.push(name);
    if (mutatesEpoch(node)) scopes.push(stack.at(-1) ?? "<module>");
    ts.forEachChild(node, visit);
    if (name) stack.pop();
  }

  visit(page);
  return scopes;
}

/** flow 的顶层条目里，哪些是对 epoch 的自增。 */
function bumpsEpoch(entry) {
  if (entry.kind === "assignment") return entry.target === EPOCH_REF;
  if (entry.kind === "variables") {
    return entry.declarations.some(
      (item) => (item.initializer ?? "").includes(EPOCH_REF)
        && /(?:\+\+|--)/.test(item.initializer ?? ""),
    );
  }
  return false;
}

test("空转保护：epoch 自增站点确实被发现，且覆盖已知的那几个作用域", () => {
  const scopes = epochBumpScopes();
  assert.ok(
    scopes.length >= 5,
    `发现的 workspaceEpoch 自增站点只有 ${scopes.length} 个，判据大概率失效了`,
  );
  assert.ok(
    scopes.includes(SELF_SETTLING_SCOPE),
    "openNotebook 自己那处自增没有被发现（判据失效）",
  );
  // helper 本身必须存在，否则下面那条会因为「一处自增都没有要求」而空转。
  findFunctionIn(page, "Home", INVALIDATE);
});

test(`openNotebook 之外的每处 epoch 自增，都紧跟着 ${INVALIDATE}()`, () => {
  const offenders = [];
  const bumpsByScope = new Map();
  for (const scope of epochBumpScopes()) {
    bumpsByScope.set(scope, (bumpsByScope.get(scope) ?? 0) + 1);
  }

  for (const [scope, discovered] of bumpsByScope) {
    const flow = controlFlowIn(findFunctionIn(page, "Home", scope));
    const indexes = flow.flatMap((entry, index) => (bumpsEpoch(entry) ? [index] : []));
    // 交叉校验：AST 发现了 n 处，顶层语句序列也必须命中 n 处。对不上说明自增被挪进了
    // 子块（if / 循环 / 回调），顶层判据看不见它——报红而不是静默放行。
    if (indexes.length !== discovered) {
      offenders.push(
        `${scope}：AST 发现 ${discovered} 处 epoch 自增，但顶层语句序列只命中 ${indexes.length} 处`
        + "（自增被挪进了子块？判据需要跟着补到那一层）",
      );
      continue;
    }
    if (scope === SELF_SETTLING_SCOPE) continue;
    for (const index of indexes) {
      const next = flow[index + 1];
      if (!next || !directCallTargets(next).includes(INVALIDATE)) {
        offenders.push(
          `${scope}：第 ${index} 条的 epoch 自增后面没有紧跟 ${INVALIDATE}() —— `
          + "这次导航会把在途的 open 顶死，guard 却还挂着，之后同一本库的点击会被"
          + "僵尸 guard 吞掉（请求挂死时就是永远打不开）",
        );
      }
    }
  }

  assert.deepEqual(offenders, []);
});
