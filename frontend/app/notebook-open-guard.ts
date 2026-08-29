// 打开笔记本点击的合并判定 —— 纯逻辑，无 React、无 DOM、无 API。
//
// 大库的 `openNotebook` load 相位（getNotebook + listSources）在后端要跑数秒，期间
// UI 若零反馈，用户会对着同一张卡片连点——每一次点击都会叠出一整套并行请求，把
// 后端打挂。这里维护「当前是否已有一次打开在途」的最小状态机，`page.tsx` 用它在
// 发请求之前直接吞掉同一本笔记本的重复点击。
//
// 语义只有四条，任何一条被打破都会退回连点打挂、顶替失灵，或者吞掉点击：
//  ① 同 id 且**同代**在途 → 合并：调用方应直接放弃这次调用，不做任何事（不发请求、
//     不重置任何既有状态）——已经在飞的那一次自己会收尾。
//  ② 异 id 在途 → 放行：切换到另一本笔记本必须照旧生效，新的一次 open 用新的
//     epoch 顶替旧的，这是既有 `workspaceEpochRef` 顶替语义，本模块不改它，只是
//     借用同一个 epoch 值来给 guard 打标。
//  ③ guard 过期（记的 epoch 已不是当前代）→ 放行：`workspaceEpoch` 在 `openNotebook`
//     之外还会被别的导航自增（回集合页、登出、切换/新建 Ask 会话）。那些导航把在途的
//     那次 open 顶死了（它的 `isCurrent()` 从此恒假，永远不会 apply、也永远不会
//     conclude），可它的 guard 还挂在那里——如果合并只看 id，同一本笔记本之后的每一次
//     点击都会被这个**僵尸 guard** 吞掉，直到那次死跑的请求自己收尾为止；请求挂死
//     （api-client 没有 timeout）时就是**永远打不开**。所以合并必须同时要求 epoch
//     仍是当前代：任何外部自增自动让合并失效。
//  ④ settle 只清自己那一代：一次点击的收尾（成功/失败/被顶替）传回的 epoch 若与
//     guard 当前记的 epoch 不一致，说明 guard 早已经被更晚的一次点击顶替，这次
//     迟到的 settle 必须原样放过，绝不能把新一次点击刚建立的忙碌态抹掉。

/** `null` = 当前没有任何一次打开在途；否则记着在途的是哪本笔记本、哪一代。 */
export type NotebookOpenGuard = { readonly notebookId: string; readonly epoch: number } | null;

/**
 * 是否应当合并（吞掉）这次对 `notebookId` 的打开请求。
 *
 * 只有「同一本笔记本、且 guard 仍属当前代」才合并；guard 为空、指向另一本笔记本、
 * 或者已经被 `openNotebook` 之外的导航把 epoch 顶过去（语义③的僵尸 guard），一律
 * 放行。`currentEpoch` 传的是调用那一刻的 `workspaceEpochRef.current`，也就是本次
 * 调用**自增之前**的值——在途那次的 `beginOpen` 用的就是它，所以正常连点两者相等。
 */
export function shouldCoalesceOpen(
  guard: NotebookOpenGuard,
  notebookId: string,
  currentEpoch: number,
): boolean {
  return guard !== null
    && guard.epoch === currentEpoch
    && guard.notebookId === notebookId;
}

/** 记一次新的打开开始。调用前应已用 `shouldCoalesceOpen` 排除同 id 重复。 */
export function beginOpen(notebookId: string, epoch: number): NotebookOpenGuard {
  return { notebookId, epoch };
}

/**
 * 收尾一次打开（成功、失败、或被顶替都会走到这里）。只有 `epoch` 与 `guard` 当前
 * 记的一致才清空；不一致说明 `guard` 已经被更晚一次点击的 `beginOpen` 顶替，原样
 * 放过，绝不清空——防止迟到的旧 settle 抹掉新一次点击的忙碌态。
 */
export function settleOpen(guard: NotebookOpenGuard, epoch: number): NotebookOpenGuard {
  return guard !== null && guard.epoch === epoch ? null : guard;
}
