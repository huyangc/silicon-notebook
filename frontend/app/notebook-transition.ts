// 「打开笔记本」的单一 transition 编排 —— 纯逻辑，无 React、无 DOM、无 API。
//
// 打开一本笔记本要同时挪动六个 owner hook 的生命周期（root modal 协调器、workspace
// 扩展、来源库、报告、知识图谱、Ask 会话）。它们各自的 begin/finish 曾经手写散落在
// `page.tsx::openNotebook` 的四个位置上——开头一串 begin、中间一段「谁拒绝了就整体
// 放弃」的分支、成功路径上的 commit，以及 `finally` 里带 `opened` 布尔的一串 finish。
// 新增一个 owner 就要在这四处各补一笔，漏掉任意一处的表现都是**静默**的：要么一个
// owner 永远停在 suspended 态，要么一次被顶替的切换把状态提交给了新工作区。
//
// 本模块把这条流程收成一条确定性的相位序列，各 owner 只在**一份 step 列表**里声明
// 自己的 begin / commit / settle：
//
//   begin(按声明序，全部先跑完)
//     └─ 任一步拒绝 → settle(null) 后返回 refused（已 begin 的全部回滚）
//   enter            —— begin 全部成功之后、取数之前的一次性清理
//   load             —— 并行取数（本模块不关心取了什么）
//   isCurrent        —— 取数期间是否被更晚的 transition 顶替
//   apply(loaded)    —— 同步写入共享视图状态，产出 Outcome
//   step.commit      —— 按声明序，仅当返回 Promise 才 await
//   conclude(outcome)—— 最后一次守门 + 副作用（history/滚动）
//   settle(outcome)  —— 逆 begin 序，把 Outcome 交给每个 owner
//
// 三条不变量：
//  ① **settle 恰好一次**，且只覆盖 begin 成功的步骤：拒绝、过期、异常与成功四条路
//    都收敛到同一个 `settle(...)`，`null` 表示回滚。异常路径先 settle(null) 再原样
//    抛出，调用方看到的错误与接入前逐字相同。
//  ② **回滚按 begin 的逆序**：后建立的先撤销，是经典的 unwind 顺序，也让「begin 到
//    一半被拒绝」与「commit 之后被顶替」走同一条撤销路径，不必分别推演。
//  ③ **一个 run 只 settle 自己的 ticket**：`begun` 是本次调用的局部变量，所以被顶替
//    的旧 transition 收尾时绝不会碰到新 transition 刚建立的 owner——这正是「迟到完成
//    不得作废新工作区」那条红线在编排层的落点。
//
// 同步 commit 刻意**不 await**：`await undefined` 也会插一个微任务节拍，而
// `openNotebook` 在 apply 与第二次 epoch 复核之间历史上只有一次真实 await（Ask 会话
// 还原）。多插的节拍会无谓地拉宽被顶替窗口，故这里按鸭子类型判定返回值是否
// thenable（`typeof value?.then === "function"`），而不是 `instanceof Promise`——
// 跨 realm 的 Promise（例如来自 iframe）与自定义 thenable 都不是当前 realm 的
// `Promise` 实例，但 `await` 对它们一样生效，`instanceof Promise` 会把它们误判成
// 「同步返回」而漏掉一次本该有的 await。

/** 步骤 begin 的返回值。必须是对象（非空即代表这一步已建立），`null`/`undefined` = 拒绝。 */
export type TransitionTicket = object;

export type TransitionCommitContext<Loaded, Outcome> = {
  readonly loaded: Loaded;
  readonly outcome: Outcome;
};

/** 编排器看到的步骤形状。ticket 的具体类型由 `transitionStep` 封在闭包里。 */
export type TransitionStep<Loaded, Outcome> = {
  readonly name: string;
  readonly begin: () => TransitionTicket | null;
  readonly commit?: (
    ticket: TransitionTicket,
    context: TransitionCommitContext<Loaded, Outcome>,
  ) => void | Promise<unknown>;
  readonly settle: (ticket: TransitionTicket, outcome: Outcome | null) => void;
};

export type TransitionStepSpec<Ticket extends TransitionTicket, Loaded, Outcome> = {
  /** 只用于诊断（`refusedBy`）与守卫可读性，不参与任何判定。 */
  readonly name: string;
  readonly begin: () => Ticket | null | undefined;
  readonly commit?: (
    ticket: Ticket,
    context: TransitionCommitContext<Loaded, Outcome>,
  ) => void | Promise<unknown>;
  readonly settle: (ticket: Ticket, outcome: Outcome | null) => void;
};

/**
 * 声明一个步骤。唯一存在理由是把每个 owner 各不相同的 ticket 类型封进闭包，让 step
 * 列表可以是同一个数组——两处 `as Ticket` 是本模块**仅有**的类型断言，且都发生在
 * 「这张 ticket 就是本步骤自己 begin 出来的那张」这一构造性事实之上。
 */
export function transitionStep<Ticket extends TransitionTicket, Loaded, Outcome>(
  spec: TransitionStepSpec<Ticket, Loaded, Outcome>,
): TransitionStep<Loaded, Outcome> {
  const commit = spec.commit;
  return {
    name: spec.name,
    begin: () => spec.begin() ?? null,
    commit: commit
      && ((
        ticket: TransitionTicket,
        context: TransitionCommitContext<Loaded, Outcome>,
      ): void | Promise<unknown> => commit(ticket as Ticket, context)),
    settle: (ticket: TransitionTicket, outcome: Outcome | null) => (
      spec.settle(ticket as Ticket, outcome)
    ),
  };
}

export type NotebookTransitionPlan<Loaded, Outcome> = {
  readonly steps: readonly TransitionStep<Loaded, Outcome>[];
  /** begin 全部成功之后、`load()` 之前跑一次。被拒绝时不执行。 */
  readonly enter?: () => void;
  readonly load: () => Promise<Loaded>;
  /** `load()` 之后的守门：false = 本次切换已被更晚的 transition 顶替。 */
  readonly isCurrent: () => boolean;
  readonly apply: (loaded: Loaded) => Outcome;
  /** 所有 step.commit 之后的最后一次守门 + 副作用。false = 已被顶替。 */
  readonly conclude: (outcome: Outcome) => boolean;
};

/**
 * 鸭子类型判定：`value` 是否可被 `await`。不用 `instanceof Promise`——跨 realm 的
 * Promise 与自定义 thenable 都不是当前 realm 的 `Promise` 实例，但 `await` 对它们
 * 一样生效；`isThenable` 是这条判定唯一的收口。
 */
function isThenable(value: unknown): value is PromiseLike<unknown> {
  return typeof (value as { then?: unknown } | null | undefined)?.then === "function";
}

export type NotebookTransitionResult<Outcome> =
  | { readonly status: "committed"; readonly outcome: Outcome }
  | { readonly status: "refused"; readonly refusedBy: readonly string[] }
  | { readonly status: "superseded"; readonly at: "load" | "conclude" };

export async function runNotebookTransition<Loaded, Outcome>(
  plan: NotebookTransitionPlan<Loaded, Outcome>,
): Promise<NotebookTransitionResult<Outcome>> {
  const begun: { step: TransitionStep<Loaded, Outcome>; ticket: TransitionTicket }[] = [];
  const refusedBy: string[] = [];
  // ⚠ 先把**全部** begin 跑完再判拒绝，而不是撞上第一个拒绝就短路：两个会拒绝的
  // owner（Ask 会话与 workspace 扩展）各自按自己的 actor ref 判定，短路会让排在
  // 后面的 owner 连 begin 都没跑过，与接入前的行为不同。
  for (const step of plan.steps) {
    const ticket = step.begin();
    if (ticket === null) {
      refusedBy.push(step.name);
      continue;
    }
    begun.push({ step, ticket });
  }

  const settle = (outcome: Outcome | null) => {
    for (let index = begun.length - 1; index >= 0; index -= 1) {
      begun[index].step.settle(begun[index].ticket, outcome);
    }
  };

  if (refusedBy.length > 0) {
    settle(null);
    return { status: "refused", refusedBy };
  }

  try {
    plan.enter?.();
    const loaded = await plan.load();
    if (!plan.isCurrent()) {
      settle(null);
      return { status: "superseded", at: "load" };
    }
    const outcome = plan.apply(loaded);
    const context: TransitionCommitContext<Loaded, Outcome> = { loaded, outcome };
    for (const entry of begun) {
      const pending: unknown = entry.step.commit?.(entry.ticket, context);
      if (isThenable(pending)) await pending;
    }
    if (!plan.conclude(outcome)) {
      settle(null);
      return { status: "superseded", at: "conclude" };
    }
    settle(outcome);
    return { status: "committed", outcome };
  } catch (error) {
    settle(null);
    throw error;
  }
}
