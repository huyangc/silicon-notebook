// `app/notebook-open-guard.ts` —— 打开笔记本连点合并的判定（纯逻辑）。
//
// 这里钉的是四条语义,任一条被打破都会退回连点打挂、顶替失灵,或者吞掉点击:
//  ① 同 id 且同代在途 → 合并(shouldCoalesceOpen 为 true);
//  ② 异 id 在途 → 放行(shouldCoalesceOpen 为 false),新一次 open 顶替旧的;
//  ③ guard 过期(记的 epoch 已不是当前代)→ 放行——`workspaceEpoch` 在 openNotebook
//     之外还会被别的导航自增(回集合页/登出/切换或新建 Ask 会话),那些导航把在途那次
//     open 顶死了但 guard 还挂着,只看 id 就会把之后同一本库的每一次点击都吞掉;
//  ④ settle 只清自己那一代——迟到的旧 settle(epoch 不匹配)不许清掉新 guard。
import test from "node:test";
import assert from "node:assert/strict";

import {
  beginOpen,
  settleOpen,
  shouldCoalesceOpen,
} from "../../app/notebook-open-guard.ts";

test("guard 为空:任何 id 都不合并", () => {
  assert.equal(shouldCoalesceOpen(null, "nb-1", 0), false);
});

test("同 id 且同代在途:合并", () => {
  const guard = beginOpen("nb-1", 1);
  assert.equal(shouldCoalesceOpen(guard, "nb-1", 1), true);
});

test("异 id 在途:放行(不合并)", () => {
  const guard = beginOpen("nb-1", 1);
  assert.equal(shouldCoalesceOpen(guard, "nb-2", 1), false);
});

test("beginOpen 记下 notebookId 与 epoch", () => {
  const guard = beginOpen("nb-1", 7);
  assert.deepEqual(guard, { notebookId: "nb-1", epoch: 7 });
});

// —— 语义③:僵尸 guard ————————————————————————————————————————————————
//
// 这一组是 F1 的核心。少了 epoch 判据,下面每一条都会翻成「合并」——也就是把用户的
// 点击吞掉,而那次在途的 open 因为 isCurrent() 恒假,永远不会收尾还原忙碌态。

test("guard 过期(epoch 已被外部导航顶过去):同 id 也不再合并", () => {
  // 点开 nb-1(epoch 1)→ 用户按「返回集合页」/登出/切 Ask 会话,workspaceEpoch 变 2。
  // 那次 open 已经被顶死(isCurrent 恒假),guard 却还指着 nb-1。此时再点 nb-1:
  const zombie = beginOpen("nb-1", 1);
  assert.equal(shouldCoalesceOpen(zombie, "nb-1", 2), false);
});

test("guard 过期:epoch 差多少都不合并(判据是相等,不是「更新的就行」)", () => {
  const zombie = beginOpen("nb-1", 3);
  for (const currentEpoch of [0, 1, 2, 4, 99]) {
    assert.equal(
      shouldCoalesceOpen(zombie, "nb-1", currentEpoch),
      false,
      `epoch=${currentEpoch} 时不该合并`,
    );
  }
  // 佐证:只有恰好同代才合并——否则上面那圈可能是因为函数恒假而通过。
  assert.equal(shouldCoalesceOpen(zombie, "nb-1", 3), true);
});

test("端到端场景:外部导航顶掉在途 open 后,同一本库必须还能重新打开", () => {
  let guard = null;
  let epoch = 0;

  // 点开 nb-1。
  epoch += 1;
  assert.equal(shouldCoalesceOpen(guard, "nb-1", epoch - 1), false);
  guard = beginOpen("nb-1", epoch);

  // 用户等不及,点了「返回集合页」——那条路径自增 epoch(并调 page.tsx 的
  // invalidateNotebookOpenGuard,但这条用例故意**不**清 guard:纯模块这一层必须自己
  // 就挡得住,两层互为兜底)。
  epoch += 1;

  // 再点 nb-1:不能被那个僵尸 guard 吞掉。
  assert.equal(shouldCoalesceOpen(guard, "nb-1", epoch), false);
  epoch += 1;
  guard = beginOpen("nb-1", epoch);
  assert.deepEqual(guard, { notebookId: "nb-1", epoch: 3 });

  // 新的这一代自己仍然合并连点。
  assert.equal(shouldCoalesceOpen(guard, "nb-1", epoch), true);
});

test("settleOpen:epoch 匹配才清空", () => {
  const guard = beginOpen("nb-1", 1);
  assert.equal(settleOpen(guard, 1), null);
});

test("settleOpen:epoch 不匹配(被顶替)原样保留,不清空", () => {
  // 点开 nb-1(epoch 1)期间又点开 nb-2(epoch 2),guard 已经指向 nb-2 了。
  // nb-1 那次迟到的 settle(epoch 1)绝不能把 nb-2 的 guard 抹掉。
  const staleGuard = beginOpen("nb-1", 1);
  const currentGuard = beginOpen("nb-2", 2);
  const settled = settleOpen(currentGuard, 1);
  assert.deepEqual(settled, currentGuard);
  assert.notEqual(settled, null);
  // 佐证:staleGuard 本身与 currentGuard 是两个独立对象,没有被混用。
  assert.notDeepEqual(staleGuard, currentGuard);
});

test("settleOpen:guard 已经是 null 时保持 null", () => {
  assert.equal(settleOpen(null, 1), null);
});

test("settleOpen 在 guard 已被外部作废(置 null)后幂等:仍是 null", () => {
  // page.tsx 的 invalidateNotebookOpenGuard 会把 guard 直接置空。之后那次死跑的 open
  // 收尾时 finally 里照样跑 settleOpen——必须什么都不做,不能把 null 变回别的东西。
  assert.equal(settleOpen(null, 7), null);
});

test("端到端场景:同 id 连点合并,reject 路径的 settle 仍然清空(还原忙碌态)", () => {
  // 模拟 page.tsx 的用法:begin → (可能失败) → finally 里 settle。
  let guard = null;
  const notebookId = "nb-1";
  const epoch = 1;

  // 第一次点击:放行,进入在途。(读的是自增**前**的 epoch,与 page.tsx 一致。)
  assert.equal(shouldCoalesceOpen(guard, notebookId, epoch - 1), false);
  guard = beginOpen(notebookId, epoch);

  // 第二次连点同一本笔记本:必须合并,guard 不变。
  assert.equal(shouldCoalesceOpen(guard, notebookId, epoch), true);
  assert.deepEqual(guard, { notebookId, epoch });

  // 第一次点击的请求 reject 了,finally 里 settle——忙碌态必须还原为 null,
  // 之后同一本笔记本的点击才能重新放行。
  guard = settleOpen(guard, epoch);
  assert.equal(guard, null);
  assert.equal(shouldCoalesceOpen(guard, notebookId, epoch), false);
});

test("端到端场景:异 id 顶替期间,旧 id 的迟到 settle 不清新 guard;新 id 自己的 settle 才清", () => {
  let guard = null;

  // 打开 nb-1。
  guard = beginOpen("nb-1", 1);

  // 打开 nb-2 顶替(异 id 放行,workspaceEpoch 语义:新的一次 open 用新 epoch)。
  assert.equal(shouldCoalesceOpen(guard, "nb-2", 1), false);
  guard = beginOpen("nb-2", 2);

  // nb-1 那次迟到完成,settle(epoch=1)不该清掉 nb-2 的 guard。
  guard = settleOpen(guard, 1);
  assert.deepEqual(guard, { notebookId: "nb-2", epoch: 2 });

  // nb-2 自己的 settle(epoch=2)才真正清空。
  guard = settleOpen(guard, 2);
  assert.equal(guard, null);
});
