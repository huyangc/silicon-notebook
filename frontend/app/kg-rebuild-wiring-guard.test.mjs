// 「重新合并」后台化之后的接线守卫。
//
// 与 kg-relink-wiring-guard 同一条理由:整个风险都在**接线**上,不在纯函数里。端点不再
// 同步返回聚类数,而 UI 上仍有一句「现有 N 组概念」;最容易发生、也最难在评审里看出来的
// 退化,是把那句话接回 POST 的返回值——TypeScript 拦不住,mock 也很容易喂出一个「刚好有
// 数字」的回执把它盖过去。所以这里按**源码语义**钉五条:
//
//   ① refreshUnifiedKg 不从 rebuildUnifiedKg 的返回值里读结果字段;
//   ② 忙碌位在 await 之前就置上(长任务按钮红线的「立刻不可点」那一半;
//      long-task-button-guard 只钉 disabled 存在,钉不到置位时机);
//   ③ 完成信号真的存在——page.tsx 里有 fetchUnifiedKgRebuildStatus + rebuildPollOutcome
//      的消费点,且轮询带尝试上限,否则忙碌位永远解除不掉,按钮会卡死;
//   ④ 忙碌位与轮询都按**笔记本集合**判(经共享纯函数 busyForNotebook / claimNotebookSlot /
//      releaseNotebookClaim)。裸布尔在类型上完全合法、单测也照样绿,只有在「点完 A 切到 B
//      再点一次」时才现形;
//   ⑤ decideMerge 也走同一个忙碌位集合。它落完决定要**启动**一次重新合并,后台化之后
//      它不再等那次跑完——不认领忙碌位,轮询 effect 根本不会开,图谱就永远停在决定之前。
//
// 覆盖边界(如实说明):本守卫认的是源码文本形态,不是运行时行为。轮询的时序、取消与切库
// 竞态由 kg-rebuild-status.test.mjs 的纯函数用例 + 后端用例覆盖。
//
// 刻意**不**加进 long-task-button-guard 的 LONG_TASK_BUTTONS:两颗 confirmRefreshUnifiedKg
// 按钮走的是**两种**合法形态——知识图谱视图那颗是 `disabled` + 文案切换,「索引与构建」
// 看板那颗是忙碌时整排 CTA 不渲染。那份守卫断言「所有 onClick 命中该模式的 button 都带
// disabled」,而两颗按钮的 onClick 源码文本逐字相同、分不开,加进去只会把合法的形态②
// 判成违规。忙碌位本身由下面第②④条钉住。
import test from "node:test";
import assert from "node:assert/strict";

import { findFunction, jsxElements, parseModule } from "./test/semantic-source.mjs";

// 后台化之后 POST 只回 {status, notebook_id, job_id};读到这些就是把结果接回了返回值。
const RESULT_FIELDS = ["clusters", "cluster_count"];

test("refreshUnifiedKg 不假装从 POST 拿到了聚类数", async () => {
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "refreshUnifiedKg").getText(page);

  for (const field of RESULT_FIELDS) {
    assert.ok(
      !body.includes(field),
      `refreshUnifiedKg 读了 ${field} —— 后台化之后 POST 只返回 job_id,`
      + "结果必须等 unified-kg/rebuild/status 的终态",
    );
  }
});

test("refreshUnifiedKg 在发请求之前就置忙碌位", async () => {
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "refreshUnifiedKg").getText(page);

  const busyAt = body.indexOf("setRebuildingNotebookIds((prev) => claimNotebookSlot(prev, nb))");
  const postAt = body.indexOf("rebuildUnifiedKg(");
  assert.ok(busyAt >= 0, "refreshUnifiedKg 必须认领忙碌位(且记下是哪个库,走 claimNotebookSlot)");
  assert.ok(postAt >= 0, "refreshUnifiedKg 必须发起重新合并请求");
  assert.ok(
    busyAt < postAt,
    "忙碌位必须在 await 之前置上,否则请求在飞的那段窗口按钮还能连点",
  );
  // codex R9 之前这里直接断言 refreshUnifiedKg 里完全不出现 `finally`——当时唯一可能
  // 出现的写法就是 `finally { setKgRefreshBusy(false) }`,同步语义的残留:任务还在
  // 后台跑,按钮就已经放开了。R9 引入了一个合法的 `finally`(清理提交期标记
  // submittingMaintenanceRef,见下一条测试),所以这里改成更精确的断言:finally 块
  // 可以存在,但绝不能在里面同步释放忙碌位。
  const finallyAt = body.indexOf("finally {");
  if (finallyAt >= 0) {
    const finallyBody = body.slice(finallyAt);
    assert.ok(
      !finallyBody.includes("setRebuildingNotebookIds"),
      "refreshUnifiedKg 的 finally 块不能同步释放忙碌位(setRebuildingNotebookIds):"
      + "那是同步语义的残留,任务还在后台跑,按钮就已经放开了",
    );
  }
});

test("codex R9:refreshUnifiedKg 提交期(POST 还没落地)标记在 submittingMaintenanceRef,finally 里清理", async () => {
  // POST 比首个 3s 轮询 tick 慢时,轮询会先读到服务端**旧的** idle 并当终态收工——随后
  // POST 才成功,没人再轮询,任务完成不会刷新。修法是在 await POST 之前标记「正在提交」,
  // 轮询 tick 撞到 idle 时如果自己的库还在这个标记里就不能当真终态,标记必须在 finally
  // 里清理(不管成功/非 409 失败/409 领养,都不能让标记永久卡住)。
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "refreshUnifiedKg").getText(page);

  const addAt = body.indexOf("submittingMaintenanceRef.current.add(`${nb}:rebuild`);");
  const postAt = body.indexOf("rebuildUnifiedKg(");
  assert.ok(
    addAt >= 0,
    "refreshUnifiedKg 必须在提交前标记 submittingMaintenanceRef(键必须按 kind 分开,"
    + "`${nb}:rebuild`——codex R12:同库的 relink 提交若与这次窗口重叠,不能共用裸 nb 键"
    + "互相冲掉对方的保护)",
  );
  assert.ok(
    addAt < postAt,
    "标记必须在 await rebuildUnifiedKg 之前置上,否则 POST 在飞期间轮询读到的陈旧 idle"
    + "不会被这个标记拦住",
  );

  const finallyAt = body.indexOf("finally {");
  assert.ok(
    finallyAt >= 0,
    "refreshUnifiedKg 必须有 finally 块清理提交期标记,否则某条失败路径会让标记"
    + "永久卡住、轮询永远认为提交还在飞",
  );
  const deleteAt = body.indexOf(
    "submittingMaintenanceRef.current.delete(`${nb}:rebuild`);",
    finallyAt,
  );
  assert.ok(
    deleteAt > finallyAt,
    "finally 块必须清理提交期标记(submittingMaintenanceRef.current.delete(`${nb}:rebuild`))",
  );
});

test("忙碌位是按笔记本作用域的一个集合,不是一个裸布尔", async () => {
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  assert.ok(
    /setRebuildingNotebookIds\s*\]\s*=\s*useState<Set<string>>\(new Set\(\)\)/.test(source),
    "「重新合并」的忙碌位必须是正在重新合并的笔记本 id 集合,单值形态会在"
    + "「点完 A 切到 B 再点」时把 A 的认领覆盖掉",
  );
  assert.ok(
    /const kgRefreshBusy = busyForNotebook\(rebuildingNotebookIds, currentNotebookId\)/.test(source),
    "按钮/轮询的判据必须走 busyForNotebook —— 手搓 `.has(currentNotebookId)` 也能对,"
    + "但没有单测钉住那几个边角",
  );
  assert.ok(
    !/setRebuildingNotebookIds\(new Set\(\)\)/.test(source),
    "无条件清空整个忙碌位集合会把用户刚在另一个库点起来的那次一并抹掉",
  );
});

test("轮询有尝试上限,且不把 kgLimit 塞进依赖里", async () => {
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  // 断言必须落在 effect **体内**:光看整份文件里出现过 REBUILD_POLL_MAX_ATTEMPTS 拦不住
  // 「把上限那个分支整块删掉、import 还留着」。
  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到重新合并的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  assert.ok(
    // 上限从 3000 提到 4500(codex R1 两条 P2:终态先 settle 再刷新 + decideMerge 409
    // 撞槽自动补发),又提到 5300(codex R2 P1:409 补发不再提前消费标记,改成「保留标记 +
    // 保留忙碌位 + 重启轮询,attempts 不因重试复位」,settleOrRetryRebuild 因此又长了一截),
    // 再提到 7200(codex R5 两条 P2:pollTick 加代际捕获/校验 + finish 的刷新 IIFE 里加
    // 选中概念重对账,两处都在这段区间内),再提到 8200(codex R9:settleOrRetryRebuild 的
    // 补发 POST 与 pollTick 都加了 submittingMaintenanceRef 提交期标记的 add/finally-
    // delete,两处都在这段区间内),再提到 9200(codex R11:三处 rebuild POST 成功都要
    // 记下 job_id、settleOrRetryRebuild 的最终释放要清掉 expectation、pollTick 的正常
    // 终态分支加了「提交期不止 idle 不可信」与「job_id 必须配对」两层判据,四处都在这段
    // 区间内),再提到 11000(codex R13:job_id 不匹配不再无限拒收——pollTick 拆出「观测到
    // running 归零 mismatchStreak」的独立分支,job_id 配对判据改成「连续不匹配达阈值才
    // 收工,否则清零计数」,startPolling 每次开新代际也归零 mismatchStreak,三处都在这段
    // 区间内),再提到 11800(codex R15:同代际 in-flight 串行化守卫与注释)——阈值只是防「查找到很远之后一个不相干的收尾数组」的护栏,不是精确长度
    // 断言,跟着真实需要一起调没有问题。
    depsAt > start && depsAt - start < 11800,
    "轮询 effect 的依赖必须恰好是 [rebuildingNotebookIds, currentNotebookId]"
    + "(kgLimit 进依赖会在换范围时重启轮询、重置尝试计数)",
  );
  const body = source.slice(start, depsAt);

  assert.ok(
    body.includes("REBUILD_POLL_MAX_ATTEMPTS"),
    "轮询必须有尝试上限:后端只在进程内记这件事,任务卡住时 status 会一直如实回报"
    + "running,没有上限按钮就永远解锁不了",
  );
  assert.ok(
    body.includes("REBUILD_POLL_TIMED_OUT"),
    "超限也要走一份显式回执,而不是静默把忙碌位一丢",
  );
  assert.ok(
    body.includes("kgLimitRef.current"),
    "轮询里的图谱重拉必须经 kgLimitRef 读当前范围",
  );
  assert.ok(
    body.includes("releaseNotebookClaim(prev, nb)"),
    "终态必须只清自己那一格",
  );
});

test("终态先刷新、刷新完成后才释放忙碌位（不是反过来）", async () => {
  // 双 opus 评审 P1(#478 同型 bug):release 若在刷新之前置,会改
  // rebuildingNotebookIds → 触发这条 effect 的 cleanup → 把这次刷新自己的 cancelled
  // 闭包置 true → 三个真实 fetch(图谱/待确认合并/状态)回来时 setState 被自己的
  // cleanup 丢弃。jsdom 实测 fetch 耗时 ≥5ms 时恒 CANCELLED,生产网络请求远超 5ms,
  // 图谱因此永远刷不出来,「已重新合并」toast 弹了但画布纹丝不动。
  // 断言按**源码顺序**钉:release 调用必须落在 finish 内那个 `void (async () => {...})()`
  // IIFE 的 `await Promise.all([...])` 之后,而不是之前。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到重新合并的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  assert.ok(depsAt > start, "找不到轮询 effect 的收尾依赖数组");
  const body = source.slice(start, depsAt);

  const iifeAt = body.indexOf("void (async () => {");
  assert.ok(iifeAt >= 0, "finish 的刷新必须包在一个 void (async () => {...})() IIFE 里");
  const asyncBody = body.slice(iifeAt);
  const awaitAt = asyncBody.indexOf("await Promise.all([");
  const releaseAt = asyncBody.indexOf("releaseNotebookClaim(prev, nb)");
  assert.ok(
    awaitAt >= 0,
    "刷新必须 await 真实的 fetchUnifiedGraph/fetchPendingMerges/fetchUnifiedKgStatus",
  );
  assert.ok(releaseAt >= 0, "刷新完成后必须释放忙碌位(否则按钮永远卡在忙碌态)");
  assert.ok(
    awaitAt < releaseAt,
    "release 必须在 await 刷新之后 —— 提前 release 会让 effect 的 cleanup 把这次刷新"
    + "自己的 setState 当成『已取消』丢掉(#478 同型 bug)",
  );
});

test("终态观测后先停轮询(settled+clearInterval)再收尾,防止刷新/补发耗时跨 tick 时被重复触发", async () => {
  // codex R1 P2:interval 在 finish() 收尾(刷新图谱,以及下面「补发一次 rebuildUnifiedKg」
  // 那条测试覆盖的 409 补发重试)期间仍然继续运行——如果这段耗时超过一个 3s 周期,下一
  // tick 会再读到同一个终态、再调一次 finish(),让图谱重拉/补发重试并发跑两份。修法是
  // 终态一旦观测到就立刻(同步)停轮询:settled 标志 + clearInterval 都必须在 finish(...)
  // 调用**之前**完成,且每个 tick 开头都要检查 settled,拦住 clearInterval 真正生效前
  // 已经排队的迟到 tick。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到重新合并的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  assert.ok(depsAt > start, "找不到轮询 effect 的收尾依赖数组");
  const body = source.slice(start, depsAt);

  assert.ok(
    body.includes("let settled = false;"),
    "轮询必须有 settled 标志,防止终态之后已经排队的下一 tick 重复触发 finish",
  );

  const pollAt = body.indexOf("function pollTick()");
  assert.ok(pollAt >= 0, "找不到轮询 tick(被改名或改回内联 setInterval 回调?守卫失效)");
  const pollBody = body.slice(pollAt);

  assert.ok(
    /function pollTick\(\) \{\s*void \(async \(\) => \{\s*if \(settled \|\| inFlight\) return;/.test(pollBody),
    "轮询 tick 必须在最开头检查 settled,拦住 clearInterval 生效前已经排队的迟到 tick",
  );

  // 两处 finish 调用(超限收工 + 正常终态收工)都必须先 settled = true 再
  // window.clearInterval(poll),再调 finish —— 顺序钉死,防止半吊子修复(只加了
  // settled 变量却没有真正在调用 finish 之前置位/停轮询)。两个分支必须各自**独立**
  // 圈出窗口再局部查找:如果两处都用同一个 pollBody 从头找 lastIndexOf,把 finish(outcome)
  // 挪到 settled=true/clearInterval **之前**(把改对的顺序又改回错的)时,断言会误读到
  // 超限分支里那份无关的 settled=true 而放行——这不是假设,是实测踩过的坑。
  const timeoutBranchAt = pollBody.indexOf("if (attempts > REBUILD_POLL_MAX_ATTEMPTS) {");
  const outcomeDeclAt = pollBody.indexOf("let outcome;");
  assert.ok(
    timeoutBranchAt >= 0 && outcomeDeclAt > timeoutBranchAt,
    "找不到超限分支与正常终态分支之间的边界(let outcome;)",
  );
  const branches = [
    ["超限", pollBody.slice(timeoutBranchAt, outcomeDeclAt), "finish(REBUILD_POLL_TIMED_OUT)"],
    ["正常终态", pollBody.slice(outcomeDeclAt), "finish(outcome)"],
  ];
  for (const [label, window, finishText] of branches) {
    const finishAt = window.indexOf(finishText);
    assert.ok(finishAt >= 0, `找不到${label}分支的 finish 调用点`);
    const before = window.slice(0, finishAt);
    assert.ok(
      before.includes("settled = true;"),
      `${label}分支调用 finish 之前必须先 settled = true`,
    );
    assert.ok(
      before.includes("window.clearInterval(poll);"),
      `${label}分支调用 finish 之前必须先 clearInterval 停轮询`,
    );
  }
});

test("codex R9:pollTick 撞到 idle 且提交还在飞(submittingMaintenanceRef)时不当真终态", async () => {
  // POST(含 settleOrRetryRebuild 的补发)比首个 tick 慢时,轮询会先读到服务端**旧的**
  // idle——如果这里当真终态收工,随后 POST 才成功,就再也没有人会去轮询这次真正的任务,
  // 完成也不会刷新。修法是:idle 终态额外检查 submittingMaintenanceRef 是否还标记着
  // 这个库,标记着就继续轮询、不 settle。这条断言钉住:①判据必须落在正常终态分支里、
  // settled=true 之前;②判据必须同时检查 status.status === "idle" 与
  // submittingMaintenanceRef.current.has(`${nb}:rebuild`)(不能只检查其中一个)。
  // codex R12:键必须按 kind 分开,不能是裸 nb。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到重新合并的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  assert.ok(depsAt > start, "找不到轮询 effect 的收尾依赖数组");
  const body = source.slice(start, depsAt);

  const pollAt = body.indexOf("function pollTick()");
  assert.ok(pollAt >= 0, "找不到轮询 tick(被改名或改回内联 setInterval 回调?守卫失效)");
  const pollBody = body.slice(pollAt);

  const outcomeDeclAt = pollBody.indexOf("let outcome;");
  assert.ok(outcomeDeclAt >= 0, "找不到正常终态分支的 `let outcome;`");
  const finishAt = pollBody.indexOf("finish(outcome)", outcomeDeclAt);
  assert.ok(finishAt >= 0, "找不到正常终态分支的 finish(outcome) 调用点");
  const guardWindow = pollBody.slice(outcomeDeclAt, finishAt);

  assert.ok(
    /status\.status\s*===\s*"idle"\s*&&\s*submittingMaintenanceRef\.current\.has\(`\$\{nb\}:rebuild`\)/.test(
      guardWindow,
    ),
    "正常终态分支必须检查 `status.status === \"idle\" && submittingMaintenanceRef"
    + ".current.has(`${nb}:rebuild`)`——只查其中一个都不对(只查 idle 会拦住所有真终态,"
    + "只查 submitting 会在 running 时也误判)",
  );
  const idleCheckAt = guardWindow.search(
    /status\.status\s*===\s*"idle"\s*&&\s*submittingMaintenanceRef\.current\.has\(`\$\{nb\}:rebuild`\)/,
  );
  const settledTrueAt = guardWindow.lastIndexOf("settled = true;");
  assert.ok(
    settledTrueAt >= 0 && idleCheckAt >= 0 && idleCheckAt < settledTrueAt,
    "idle+提交中的判据必须在 settled = true 之前生效,否则轮询已经先收工了",
  );
});

test("codex R5 P2(A):pollTick 捕获代际,迟到的上一代际响应必须被丢弃而不是穿过 settled 守卫", async () => {
  // status 请求慢于 3s 轮询间隔时,同一个 interval 会连续派发多个 pollTick——前一个还
  // 没等到响应,后一个已经发出去了。补发重试(settleOrRetryRebuild 成功分支)会复位
  // settled 并调 startPolling() 开一轮新的代际;此时如果一个属于**上一代际**、迟迟才
  // 回来的响应命中终态,它看到的 settled 已经被新代际复位成 false,若没有代际校验就会
  // 照样穿过守卫——提前 clearInterval(此刻 poll 已经指向新代际的 interval id,等于把
  // 新代际也停了)、提前触发刷新、提前释放忙碌位,而真正对应新代际的那次 rebuild 还在
  // 后端跑。这里钉住:①每一代 startPolling 都要自增 generation;②pollTick 必须在
  // await 之前(与 settled 检查同一个同步段内)捕获当时的 generation;③响应回来后的
  // 收工守卫必须比对 generation === myGeneration,不匹配就丢弃,不能提前 clearInterval/
  // settled=true/finish。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到重新合并的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  assert.ok(depsAt > start, "找不到轮询 effect 的收尾依赖数组");
  const body = source.slice(start, depsAt);

  const pollAt = body.indexOf("function pollTick()");
  assert.ok(pollAt >= 0, "找不到轮询 tick(被改名或改回内联 setInterval 回调?守卫失效)");
  // 代际计数必须声明在 pollTick 之前(同一个闭包作用域,且 pollTick/startPolling 都要
  // 读写它)。
  const genDeclAt = body.indexOf("let generation = 0;");
  assert.ok(
    genDeclAt >= 0 && genDeclAt < pollAt,
    "必须在 pollTick 之前声明 `let generation = 0;`(代际计数器)",
  );

  const pollBody = body.slice(pollAt);

  assert.ok(
    /function pollTick\(\) \{\s*void \(async \(\) => \{\s*if \(settled \|\| inFlight\) return;\s*(?:\/\/[^\n]*\n\s*)*const myGeneration = generation;/.test(pollBody),
    "pollTick 必须在 settled 检查之后、await 之前(同一个同步段内)捕获"
    + "`const myGeneration = generation;`——挪到 await 之后捕获就晚了,读到的已经是"
    + "响应回来那一刻的代际,起不到区分『发起时属于哪一代』的作用",
  );

  const outcomeDeclAt = pollBody.indexOf("let outcome;");
  assert.ok(outcomeDeclAt >= 0, "找不到正常终态分支的 `let outcome;`");
  const finishAt = pollBody.indexOf("finish(outcome)", outcomeDeclAt);
  assert.ok(finishAt >= 0, "找不到正常终态分支的 finish(outcome) 调用点");
  const guardWindow = pollBody.slice(outcomeDeclAt, finishAt);
  assert.ok(
    /generation\s*!==\s*myGeneration/.test(guardWindow),
    "正常终态分支调用 finish 之前的收工守卫必须校验 `generation !== myGeneration`——"
    + "不校验,补发重试复位 settled 之后,上一代际迟到的响应会直接把当前代际的轮询"
    + "掐断并触发一次属于上一代际的收尾",
  );
  // 校验必须在 settled=true / clearInterval 之前生效(即在同一个 if 守卫里,而不是
  // 事后才检查)——就近查找 settled = true 出现的位置必须晚于代际校验所在的 if 语句。
  const settledTrueAt = guardWindow.lastIndexOf("settled = true;");
  const genCheckAt = guardWindow.search(/generation\s*!==\s*myGeneration/);
  assert.ok(
    settledTrueAt >= 0 && genCheckAt >= 0 && genCheckAt < settledTrueAt,
    "代际校验必须在收工守卫的 if 条件里,先于 settled = true 生效",
  );

  const startPollAt = body.indexOf("function startPolling() {");
  assert.ok(startPollAt >= 0, "找不到 startPolling(被改名或删除?守卫失效)");
  const setIntervalAt = body.indexOf("poll = window.setInterval(pollTick, 3000);", startPollAt);
  assert.ok(setIntervalAt >= 0, "startPolling 必须真的起一个新 interval");
  const startPollBody = body.slice(startPollAt, setIntervalAt);
  assert.ok(
    startPollBody.includes("generation += 1;"),
    "startPolling 每次被调用(含补发重试成功后的重启)都必须自增 generation——不自增,"
    + "补发前后两轮轮询共享同一个代际号,代际校验形同虚设",
  );
});

test("codex R9/R16:重新合并 POST 撞 409 必须 await 领养、adopted/unknown 不提前 release", async () => {
  // codex R8 只钉住了「409 必须领养」;R9 发现旧写法在领养前先 release 自己的忙碌位
  // (`setRebuildingNotebookIds((prev) => releaseNotebookClaim(prev, nb)); void
  // adoptRunningMaintenance(nb);`)——领养探测本身也可能瞬时失败,一旦失败就返回 false,
  // 而这个提前 release 已经把位清掉了;即便探测成功查到确实还是「重新合并」自己在跑
  // (同种),提前清掉的位也没有任何东西会把它补回来(adoptRunningMaintenance 当时的
  // 实现只会"认领",不会"撤销")。R9 把决定权整个交给 adoptRunningMaintenance(它现在
  // 按服务端真相双向归位),409 分支因此不该再自己动忙碌位,且必须 await 而不是 void
  // fire-and-forget(等它把归位做完,提交期标记才能在 finally 里正确清理)。
  //
  // codex R16 发现"决定权整个交给 adoptRunningMaintenance"这条假设在 idle 这一种结果上
  // 不成立:idle 只证明服务端此刻确认没有任务在跑,不证明用户这次点击已经生效——占槽
  // 任务完全可能恰好在领养的两次探测 await 期间收尾。R16 把 409 分支拆成两半:
  // adopted/unknown 仍然沿用 R9 的旧语义(领养返回后立即 return,不碰忙碌位);idle 则由
  // 调用方接管有界重试(见下一条测试)。这条测试因此收窄断言范围:只钉住"领养返回、判定
  // 不是 idle 之前不能碰忙碌位"这一半,idle 分支自己的重试/release 语义交给下一条测试。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();
  const fnAt = source.indexOf("async function refreshUnifiedKg()");
  assert.ok(fnAt > 0, "找不到 refreshUnifiedKg");
  const four09At = source.indexOf("if (httpErrorStatus(err) === 409) {", fnAt);
  assert.ok(four09At > 0, "找不到 409 分支(被改名或删除?守卫失效)");
  // codex R16:409 分支现在嵌在一层 for 循环里,比 R9 时代多一级缩进(8 空格而非 6)。
  const four09EndAt = source.indexOf("\n        }", four09At);
  assert.ok(four09EndAt > four09At, "找不到 409 分支的收尾");
  const branch = source.slice(four09At, four09EndAt);

  assert.ok(branch.includes("httpErrorStatus(err) === 409"), "409 必须被单独识别");
  const adoptAt = branch.indexOf("const verdict = await adoptRunningMaintenance(nb);");
  assert.ok(
    adoptAt >= 0,
    "409 分支必须 await 领养服务端正在跑的维护任务(另一标签页发起的也要接管轮询)并接住"
    + "返回的 verdict——不能是 void fire-and-forget 或丢弃返回值(codex R16 的 idle 重试"
    + "判断需要它)",
  );

  const notIdleReturnAt = branch.indexOf('if (verdict !== "idle") return;', adoptAt);
  assert.ok(
    notIdleReturnAt > adoptAt,
    "领养返回后必须先判断 verdict !== \"idle\" 就直接 return——adopted(确有任务在跑,"
    + "同种或异种)与 unknown(探测本身失败)都不该继续往下走进重试/release 分支",
  );
  assert.ok(
    !branch.slice(adoptAt, notIdleReturnAt).includes("Claim"),
    "领养返回到判定『不是 idle』之前不能提前动任何忙碌位(releaseNotebookClaim 之类)——"
    + "adopted/unknown 的决定权整个交给 adoptRunningMaintenance,它已经按服务端真相"
    + "双向归位过了,调用方不需要(也无法在不重复探测的前提下)自己再判断一次",
  );
});

test("codex R16:重新合并 409+idle 时有界重试一次,仍是 409+idle 才如实释放并提示用户", async () => {
  // idle=占槽任务在两次领养探测的 await 期间恰好收尾——不是"竞态已消散、什么都没发生",
  // 是"用户这次点击对应的 POST 从没有真正发出去过"。原写法在这里直接 return,UI 也不会
  // 再刷新,等于用户点了一次却什么都没发生。R16 改成有界重试一次:槽刚空出来,立刻重发
  // 这次本该发出的 POST,大概率成功;仍然 409+idle 才是真正反常的双重竞态(两次独立探测
  // 窗口内又冒出第三个任务),这时才兜底提示并如实释放自己的位——不能无界重试,也不能
  // 一遇到 idle 就直接放弃。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();
  const fnAt = source.indexOf("async function refreshUnifiedKg()");
  assert.ok(fnAt > 0, "找不到 refreshUnifiedKg");
  const four09At = source.indexOf("if (httpErrorStatus(err) === 409) {", fnAt);
  assert.ok(four09At > 0, "找不到 409 分支(被改名或删除?守卫失效)");
  const four09EndAt = source.indexOf("\n        }", four09At);
  assert.ok(four09EndAt > four09At, "找不到 409 分支的收尾");
  const branch = source.slice(four09At, four09EndAt);

  const notIdleReturnAt = branch.indexOf('if (verdict !== "idle") return;');
  assert.ok(notIdleReturnAt >= 0, "找不到上一条测试钉住的 verdict !== \"idle\" 判据");

  const continueAt = branch.indexOf("if (attempt === 0) continue;", notIdleReturnAt);
  assert.ok(
    continueAt > notIdleReturnAt,
    "verdict === \"idle\" 时,第一次尝试(attempt === 0)必须 continue 重试,不能直接"
    + "return——变异:把 continue 改成 return(或删掉这个分支)必须让本条断言报红,那正是"
    + "『idle 不重试直接 return』的回归",
  );

  const releaseAt = branch.indexOf("releaseNotebookClaim(prev, nb)", continueAt);
  assert.ok(
    releaseAt > continueAt,
    "只有越过 continue(即第二次尝试仍是 409+idle)才能走到释放忙碌位这一步——不能在"
    + "第一次 idle 就把位释放掉",
  );
  const toastAt = branch.indexOf('setToast("当前有其他整理任务刚结束', continueAt);
  assert.ok(
    toastAt > continueAt && toastAt < releaseAt,
    "第二次仍 409+idle 必须显式 toast 提示用户手动再点一次,不能悄悄把位释放掉就完事——"
    + "用户不会无缘无故知道自己那次点击其实什么都没做",
  );
});

test("codex R16:refreshUnifiedKg 的整段提交逻辑必须包在恰好两次的有界重试循环里", async () => {
  // 单独钉住循环本身的存在与形状——上面两条测试只验证 409 分支内部的分支顺序,不足以
  // 拦住"把整个 for 循环删掉、退回单次尝试"这种变异(那样 409+idle 时 continue 语句会
  // 直接消失,函数退化回 R9 的旧行为)。
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "refreshUnifiedKg").getText(page);

  const loopAt = body.indexOf("for (const attempt of [0, 1]) {");
  assert.ok(
    loopAt >= 0,
    "refreshUnifiedKg 必须用 `for (const attempt of [0, 1])` 包住整段提交逻辑(恰好"
    + "尝试 2 次)——变异:删掉这个循环、退回单次 try/catch,必须让本条断言报红",
  );
  const claimAt = body.indexOf("setRebuildingNotebookIds((prev) => claimNotebookSlot(prev, nb));");
  assert.ok(
    claimAt >= 0 && claimAt < loopAt,
    "认领忙碌位必须在循环之外只做一次——重试的是 POST 提交,不是忙碌位认领",
  );
  const postAt = body.indexOf("rebuildUnifiedKg(", loopAt);
  assert.ok(postAt > loopAt, "POST 必须落在循环体内,不能挪到循环外只执行一次");
  const continueAt = body.indexOf("continue;", loopAt);
  assert.ok(
    continueAt > postAt,
    "循环体内必须存在 continue(重试第二次尝试),否则这个 for 循环只是一个恰好执行一次"
    + "的摆设",
  );
});

test("codex R7:手动重建 POST 成功必须消费残留的待补发标记", async () => {
  // 自动补发放弃后标记刻意保留;但用户手动点「重新合并」且 POST 成功时,这次重建
  // 已覆盖那条决定——不消费标记,终态会跳过刷新并再白发一次可能数小时的重建。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();
  const fnAt = source.indexOf("async function refreshUnifiedKg()");
  assert.ok(fnAt > 0, "找不到 refreshUnifiedKg");
  const tryAt = source.indexOf("await rebuildUnifiedKg(nb);", fnAt);
  const toastAt = source.indexOf("setToast(", tryAt);
  const consumeAt = source.indexOf(
    "setPendingRebuildNotebookIds((prev) => releaseNotebookClaim(prev, nb));", tryAt);
  assert.ok(tryAt > 0 && consumeAt > tryAt && consumeAt < toastAt,
    "POST 成功后、toast 之前必须消费本库的待补发标记");
});

test("codex R5 P2(B):新图替换旧图之后必须重对账当前选中的概念(conceptDetail/nodeCtx)", async () => {
  // 旧同步流程(后台化之前的 refreshUnifiedKg)在 rebuild 成功、图谱重拉之后,会把当前
  // 选中的概念节点在**新图**里重查:还在就用新图给出的 id 重新拉一次详情,不在就把
  // conceptDetail/nodeCtx 一起清空。后台化拆出终态刷新之后这段重对账被漏掉了——
  // conceptDetail 会继续绑着一份不再对应任何可见节点的旧详情(节点可能已被合并进另一
  // 个聚类、也可能整个消失)。这里钉住:重对账必须发生在**这次真正拿到的新图 g**上
  // (不能偷懒用组件里可能还没更新的 uGraphMerged 状态),且在同一段被
  // activeNotebookIdRef 守卫住的代码路径里。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到重新合并的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  assert.ok(depsAt > start, "找不到轮询 effect 的收尾依赖数组");
  const body = source.slice(start, depsAt);

  const iifeAt = body.indexOf("void (async () => {");
  assert.ok(iifeAt >= 0, "找不到 finish 的刷新 IIFE");
  const vizAt = body.indexOf("setVizBuilding(Boolean(g.viz_building));", iifeAt);
  assert.ok(vizAt >= 0, "找不到刷新 IIFE 里的 setVizBuilding 调用");
  const catchAt = body.indexOf("} catch (err) { reportError(err); }", vizAt);
  assert.ok(catchAt > vizAt, "找不到刷新 IIFE 的 catch 分支边界");
  const reconcile = body.slice(vizAt, catchAt);

  assert.ok(
    reconcile.includes(
      "const currentSelection = selectedKgNodeIdRef.current;",
    ),
    "必须按**这次真正拿到的新图** g.nodes 重新定位选中节点——不能用组件状态"
    + "(uGraphMerged 之类)代替,那份状态在这一刻还没被这次刷新更新",
  );
  assert.ok(
    reconcile.includes('const detail = await fetchConceptDetail(nb, selected.id).catch(() => null);'),
    "选中节点若仍是概念,必须用新图给出的 id 重新拉一次概念详情",
  );
  // codex R6:详情取回后必须复验选中未变,变了不发布(新选中的点击处理器自己拉详情)。
  const refetchAt = reconcile.indexOf('const detail = await fetchConceptDetail');
  const publishAt = reconcile.indexOf('setConceptDetail(detail);');
  const recheckAt = reconcile.indexOf('if (selectedKgNodeIdRef.current !== currentSelection) return;');
  assert.ok(refetchAt >= 0 && recheckAt > refetchAt && publishAt > recheckAt,
    "详情发布前必须先复验选中未变(取回→复验→发布的顺序)");
  assert.ok(
    reconcile.includes("setConceptDetail(null);"),
    "选中节点不是概念(或已不存在)时必须清空 conceptDetail,不能留着旧图的详情",
  );
  assert.ok(
    reconcile.includes("if (!selected) setNodeCtx(null);"),
    "选中节点在新图里已经找不到时必须连 nodeCtx 一起清空",
  );
});

test("rebuild 轮询终态尝试补发 rebuildUnifiedKg(标记只在补发真正成功后消费;409/非 409 均保留标记+忙碌位+续轮询,不复位 attempts)", async () => {
  // codex R2 P1:上一版(codex R1 P2)在发补发请求**之前**就无条件消费掉待补发标记——
  // 补发撞 409(占槽的另一个任务,比如「补上关联」,还没跑完;rebuild status 对它恒回
  // idle 终态)时,标记已经被提前吃掉、忙碌位却按原样释放,这条决定就再也没有人会去
  // 补发它,直到用户自己想起来手动点「重新合并」。这条测试钉住修复后的三件事:
  //   ① 标记只在补发 POST **真正成功后**才消费(顺序必须是先判断标记存在、再补发
  //      请求、补发成功后才消费标记——不是"先消费再补发");
  //   ② 补发撞 409 必须保留标记 + 保留忙碌位(不落到正常释放) + 重启轮询,让下一
  //      tick 再试,而不是放弃;
  //   ③ attempts 不能因这类 409 重试复位——只有补发真正成功才复位,否则共享的轮询
  //      尝试上限(约 30 分钟)对这整段等待会失效,占槽任务卡死时会无限重试。
  // codex R14 补充第④件事:非 409 的瞬时失败(网络错误/5xx 等)不能再落到"正常释放"——
  // 那条路径当时是唯一能再触发 settleOrRetryRebuild 的忙碌位,释放掉就等于让已经落库
  // 的合并决定永久搁浅。非 409 分支必须与②同型:保留标记+忙碌位、重启轮询,同样受
  // attempts 上限兜底。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到重新合并的轮询 effect");
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  const body = source.slice(start, depsAt);

  const retryAt = body.indexOf("async function settleOrRetryRebuild()");
  assert.ok(retryAt >= 0, "找不到待补发消费点(settleOrRetryRebuild 被改名或删除?守卫失效)");
  const retryBody = body.slice(retryAt);

  const hasMarkerAt = retryBody.indexOf("pendingRebuildNotebookIdsRef.current.has(nb)");
  assert.ok(hasMarkerAt >= 0, "必须先检查待补发标记是否存在,不能无条件补发");

  assert.ok(
    retryBody.includes("attempts <= REBUILD_POLL_MAX_ATTEMPTS"),
    "补发前必须检查轮询尝试上限尚未耗尽,否则超限之后仍会不停重试",
  );

  const retryPostAt = retryBody.indexOf("rebuildUnifiedKg(nb)");
  assert.ok(retryPostAt > hasMarkerAt, "必须先判断标记存在,再发补发请求");

  const consumeAt = retryBody.indexOf(
    "setPendingRebuildNotebookIds((prev) => releaseNotebookClaim(prev, nb))",
  );
  assert.ok(
    consumeAt >= 0,
    "消费标记必须真的清掉它(releaseNotebookClaim),否则标记会永久卡在待补发状态",
  );
  assert.ok(
    retryPostAt < consumeAt,
    "顺序必须是:先 await 补发请求,补发真正成功后才消费标记——不能像旧代码那样在"
    + "发请求前就无条件消费(那样补发一旦撞 409,标记已经被提前吃掉,占槽任务收工后"
    + "就再没有人会补发这条决定,变异①要拦的正是这种写法)",
  );

  const successAt = retryBody.indexOf("startPolling();", consumeAt);
  assert.ok(successAt >= 0, "补发成功必须重启轮询(startPolling)追这次新任务,不能就此撒手不管");
  assert.ok(
    consumeAt < successAt,
    "必须先消费标记,再重启轮询——顺序颠倒说明重启轮询的不是成功分支",
  );

  const catchAt = retryBody.indexOf("catch (err) {");
  assert.ok(catchAt > successAt, "找不到补发失败的 catch 分支");
  const catchBody = retryBody.slice(catchAt);
  const four09At = catchBody.indexOf("if (httpErrorStatus(err) === 409) {");
  assert.ok(
    four09At >= 0,
    "补发失败必须显式识别 409(占槽任务还没结束),不能只用 !== 409 一刀切放弃重试",
  );
  const reportErrAt = catchBody.indexOf("reportError(err);");
  assert.ok(reportErrAt > four09At, "非 409 的补发失败仍要上报,不能整个 catch 都吞掉");
  const four09Block = catchBody.slice(four09At, reportErrAt);
  assert.ok(
    four09Block.includes("startPolling();"),
    "409(占槽任务还没结束)必须重启轮询、让下一 tick 再试补发——不能就此放弃",
  );
  assert.ok(
    !four09Block.includes("setPendingRebuildNotebookIds"),
    "409 分支不能消费标记——标记必须原样保留到占槽任务收工、补发真正成功的那一刻",
  );
  assert.ok(
    !four09Block.includes("attempts = 0"),
    "409 重试不能复位 attempts(变异②):复位会让共享的轮询尝试上限(约 30 分钟)对"
    + "这整段等待失效,占槽任务本身卡死时会无限重试而永远解锁不了忙碌位",
  );
  assert.strictEqual(
    (retryBody.match(/attempts = 0;/g) || []).length,
    1,
    "attempts 全函数只能有一处复位——补发真正成功那一支,任何其它分支(含 409 重试、"
    + "超限放弃)复位都会让轮询尝试上限失去意义",
  );

  // codex R14:非 409 补发失败(网络错误/5xx 等瞬时故障)不再落到下面的共享释放——那条
  // 释放代码仍然存在(下面这条断言只验证它还在,是"无待补发标记"与"attempts 耗尽"两条
  // 路径共用的收尾),但非 409 catch 分支自己必须在 finally 之前就 return,与 409 分支
  // 同型:标记与忙碌位原样保留、重启轮询等下一 tick 再试,不能穿过去把位释放掉——唯一
  // 还能再调 settleOrRetryRebuild 的只有这条轮询 effect 的终态收尾,忙碌位一旦这里
  // 释放,已经落库的合并决定就再也没有人会去补发它。
  const finalReleaseAt = retryBody.indexOf(
    "setRebuildingNotebookIds((prev) => releaseNotebookClaim(prev, nb));",
    catchAt,
  );
  assert.ok(
    finalReleaseAt > catchAt,
    "共享的正常释放代码必须仍然存在(供无待补发标记 / attempts 耗尽两条路径收尾用)",
  );

  const catchFinallyAt = catchBody.indexOf("finally {");
  assert.ok(catchFinallyAt > reportErrAt, "找不到 catch 块与 finally 块的边界");
  const nonFour09Block = catchBody.slice(reportErrAt, catchFinallyAt);
  assert.ok(
    !nonFour09Block.includes("setRebuildingNotebookIds((prev) => releaseNotebookClaim(prev, nb));"),
    "非 409 补发失败(codex R14)不能在 catch 内直接释放忙碌位——必须像 409 分支一样"
    + "保留标记与忙碌位、重启轮询,而不是搁浅已经落库的合并决定",
  );
  assert.ok(
    /if \(!cancelled && activeNotebookIdRef\.current === nb\) \{\s*settled = false;\s*startPolling\(\);\s*return;\s*\}\s*return;/.test(
      nonFour09Block,
    ),
    "非 409 补发失败(codex R14)必须与 409 分支同型:notebook 仍是当前活动库时"
    + "settled=false 并 startPolling() 续轮询,已切库/effect 已收尾时原样 return"
    + "(标记与忙碌位都留给下次重挂的 effect)",
  );

  assert.ok(
    retryBody.includes("else if (hasPendingRebuild)"),
    "轮询尝试上限耗尽、标记仍未补发成功时,必须走一条独立分支收尾(而不是悄悄和"
    + "无标记的场景共用同一条路径)",
  );
  assert.ok(
    /setToast\("[^"]*手动点击[^"]*"\)/.test(retryBody),
    "尝试上限耗尽必须显式提示用户手动重试「重新合并」,不能悄悄丢弃这条还没兑现的"
    + "合并决定——用户不会无缘无故知道要再点一次",
  );

  // codex R9:补发也是一次真正的 POST 提交,同样会撞「POST 比首个 tick 慢」的陈旧 idle
  // 竞态,必须统一包上 submittingMaintenanceRef(理由与 refreshUnifiedKg 相同)。codex
  // R12:键必须按 kind 分开(`${nb}:rebuild`)。
  const submitAddAt = retryBody.indexOf(
    "submittingMaintenanceRef.current.add(`${nb}:rebuild`);",
  );
  assert.ok(
    submitAddAt >= 0 && submitAddAt < retryPostAt,
    "补发也必须在 await rebuildUnifiedKg 之前标记 submittingMaintenanceRef(键"
    + "`${nb}:rebuild`)",
  );
  const submitFinallyAt = retryBody.indexOf("finally {", submitAddAt);
  const submitDeleteAt = retryBody.indexOf(
    "submittingMaintenanceRef.current.delete(`${nb}:rebuild`);",
    submitFinallyAt,
  );
  assert.ok(
    submitFinallyAt > submitAddAt && submitDeleteAt > submitFinallyAt,
    "补发标记必须在 finally 里清理,不管成功/409 重试/非 409 失败都不能让标记永久卡住",
  );
});

test("page.tsx 有重新合并的完成信号(否则忙碌位解除不掉)", async () => {
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  assert.ok(source.includes("fetchUnifiedKgRebuildStatus("), "缺少 unified-kg/rebuild/status 轮询");
  assert.ok(
    source.includes("rebuildPollOutcome("),
    "终态判据必须走共享纯函数,不要在组件里手搓一份",
  );
});

test("decideMerge 启动的重新合并也挂在同一个忙碌位集合上", async () => {
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "decideMerge").getText(page);

  const claimAt = body.indexOf("setRebuildingNotebookIds((prev) => claimNotebookSlot(prev, nb))");
  const postAt = body.indexOf("rebuildUnifiedKg(");
  assert.ok(postAt >= 0, "decideMerge 落完决定后必须启动一次重新合并");
  assert.ok(
    claimAt >= 0 && claimAt < postAt,
    "decideMerge 必须在发请求之前认领忙碌位 —— 不认领,轮询 effect 根本不会开,"
    + "图谱会永远停在这条决定之前",
  );
  assert.ok(
    body.includes("httpErrorStatus(err) !== 409"),
    "409(已有一次重新合并/补上关联在跑)不是失败:忙碌位要保留,让轮询等那次的终态",
  );
  assert.ok(
    !body.includes("fetchUnifiedGraph("),
    "decideMerge 不该在 POST 之后立刻重拉图谱 —— 后台化之后那时候重建还没跑完,"
    + "拉回来的是决定之前的图;重拉由轮询在终态做",
  );
});

test("decideMerge 的 409 分支不静默：必须显式提示用户", async () => {
  // 双 opus 评审 P2-1:409 = 共槽已经有一次重新合并/补上关联在跑,决定本身已经落库,
  // 但没能立刻触发重新合并去更新图谱——如果占槽的是「补上关联」,rebuild/status 对它
  // 如实回 idle,轮询几乎立刻收工并刷新一次,那次刷新拿到的仍是决定生效前的图。此前这
  // 条分支什么都不说,用户点了「合并」/「分开」却看不到任何反馈,会以为点击没有反应。
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "decideMerge").getText(page);

  const throwAt = body.indexOf("throw err;");
  const pendAt = body.indexOf("fetchPendingMerges(nb)");
  assert.ok(throwAt >= 0 && pendAt > throwAt, "找不到 decideMerge 的 409 分支边界");
  const branch = body.slice(throwAt, pendAt);
  assert.ok(
    branch.includes("setToast("),
    "409(共槽任务在跑)必须显式 toast 告知用户『决定已记录,图还没跟上』,不能悄悄不说话",
  );
});

test("decideMerge 撞 409 时置位待补发标记(供 rebuild 轮询终态自动补发)", async () => {
  // codex R1 P2:只 toast 不补发时,决定已经落库但没有任何机制会让图谱真正跟上它——
  // 用户必须自己记得再点一次「重新合并」。这里在待补发集合(pendingRebuildNotebookIds,
  // 与忙碌位同数据结构风格)里置位这个库,由 rebuild 轮询终态收尾时消费、自动补发一次
  // rebuildUnifiedKg(见「rebuild 轮询终态消费待补发标记」那条测试)。
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "decideMerge").getText(page);

  const throwAt = body.indexOf("throw err;");
  const pendAt = body.indexOf("fetchPendingMerges(nb)");
  assert.ok(throwAt >= 0 && pendAt > throwAt, "找不到 decideMerge 的 409 分支边界");
  const branch = body.slice(throwAt, pendAt);
  assert.ok(
    branch.includes("setPendingRebuildNotebookIds((prev) => claimNotebookSlot(prev, nb))"),
    "409 分支必须置位待补发标记 —— 不置位,占槽任务结束后没有人会去补发这条决定,"
    + "图谱会永久停在这条决定生效之前",
  );
});

test("codex R15:轮询一次只允许一个在飞状态请求(inFlight 串行化)", async () => {
  // 同代际内 setInterval 不等上一次响应——两个都发于补发 POST 完成前的陈旧响应可以
  // 各自 +1 mismatchStreak 把阈值 2 打穿。streak 的论证依赖串行化。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();
  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  const body = source.slice(start, depsAt);
  assert.ok(body.includes("let inFlight = false;"), "rebuild 轮询必须有 inFlight 标志");
  const fetchAt = body.indexOf("status = await fetchUnifiedKgRebuildStatus(nb);");
  const setAt = body.lastIndexOf("inFlight = true;", fetchAt);
  const clearAt = body.indexOf("finally { inFlight = false; }", fetchAt);
  assert.ok(setAt >= 0 && fetchAt > setAt && clearAt > fetchAt,
    "fetch 必须被 inFlight = true / finally 清除包住(请求串行化)");
});

test("codex R10:decideMerge 的 POST 提交期同样标记 submittingMaintenanceRef,finally 里清理"
  + "(三处 POST——refreshUnifiedKg/settleOrRetryRebuild 的补发/decideMerge——全包)", async () => {
  // decideMerge 落决定后启动的这次 rebuildUnifiedKg 同样会撞「POST 比首个 3s 轮询 tick
  // 慢」的陈旧 idle 竞态:轮询先读到服务端旧的 idle 并当终态收工,随后这次 POST 才成功,
  // 没人再轮询,这条决定生效不会被看见。此前只有 refreshUnifiedKg 与
  // settleOrRetryRebuild 的补发包了这层标记,decideMerge 自己发起的那次漏了。
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "decideMerge").getText(page);

  const addAt = body.indexOf("submittingMaintenanceRef.current.add(`${nb}:rebuild`);");
  const postAt = body.indexOf("rebuildUnifiedKg(");
  assert.ok(
    addAt >= 0,
    "decideMerge 必须在提交前标记 submittingMaintenanceRef(键必须按 kind 分开,"
    + "`${nb}:rebuild`——codex R12:同库若有一次 relink POST 还没返回,两次提交的保护"
    + "窗口重叠,不能共用裸 nb 键互相冲掉对方的保护)",
  );
  assert.ok(postAt >= 0, "decideMerge 必须启动一次重新合并");
  assert.ok(
    addAt < postAt,
    "标记必须在 await rebuildUnifiedKg 之前置上,否则 POST 在飞期间轮询读到的陈旧 idle"
    + "不会被这个标记拦住",
  );

  const finallyAt = body.indexOf("finally {", postAt);
  assert.ok(
    finallyAt >= 0,
    "decideMerge 必须有 finally 块清理提交期标记,否则某条失败路径(含 409 领养走的"
    + "常规分支)会让标记永久卡住、轮询永远认为提交还在飞",
  );
  const deleteAt = body.indexOf(
    "submittingMaintenanceRef.current.delete(`${nb}:rebuild`);",
    finallyAt,
  );
  assert.ok(
    deleteAt >= 0 && deleteAt - finallyAt < 60,
    "finally 块必须紧接着清理提交期标记"
    + "(submittingMaintenanceRef.current.delete(`${nb}:rebuild`)),不能是外层"
    + "`finally { setDecidingMerge(null); }` 那个不相干的 finally",
  );
});

test("finish 在刷新 IIFE 之前就检查待补发标记(命中标记的两条路都跳过本轮 Promise.all 刷新)", async () => {
  // codex R3 P2:relink 占槽期间,rebuild status 每个 3s tick 都回终态 idle
  // (refresh:true)。旧顺序是"先 Promise.all 整图刷新、finally 里才试补发"——标记
  // 已经在等的场景下,这份刷新每次都白跑:补发多半立刻撞 409(占槽任务还没收工),
  // 下一 tick 还会再刷新一次,最多陪跑到 REBUILD_POLL_MAX_ATTEMPTS(600)次,慢的
  // 图读取还会推迟真正的补发。修法是把"有没有待补发标记"的判断挪到刷新 IIFE 之前:
  // 命中标记就直接交给 settleOrRetryRebuild 决定(补发成功→续轮询新任务;再撞 409→
  // 续轮询等槽),两条路都不碰 Promise.all;只有真的没有标记的终态才进刷新 IIFE。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到重新合并的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  assert.ok(depsAt > start, "找不到轮询 effect 的收尾依赖数组");
  const body = source.slice(start, depsAt);

  const finishAt = body.indexOf("const finish = (outcome: RebuildPollOutcome) => {");
  assert.ok(finishAt >= 0, "找不到 finish 定义(被改名?守卫失效)");
  const iifeAt = body.indexOf("void (async () => {", finishAt);
  assert.ok(iifeAt > finishAt, "找不到刷新 IIFE(fetchUnifiedGraph 所在的 void async IIFE)");
  // 只在 finish 开头到刷新 IIFE 起点这一段里找——刻意不整段 body 搜索,否则
  // settleOrRetryRebuild 内部自己的标记检查会把断言悄悄喂绿。
  const finishHead = body.slice(finishAt, iifeAt);

  const pendingCheckAt = finishHead.indexOf(
    "pendingRebuildNotebookIdsRef.current.has(nb)",
  );
  assert.ok(
    pendingCheckAt >= 0,
    "finish 必须在刷新 IIFE 之前检查待补发标记 —— 不检查,relink 占槽期间每个 3s tick"
    + "的终态路径都会先跑一次 Promise.all 整图刷新才尝试补发,慢的图读取还会推迟真正的补发",
  );

  const settleAt = finishHead.indexOf("void settleOrRetryRebuild();", pendingCheckAt);
  assert.ok(
    settleAt > pendingCheckAt,
    "命中待补发标记(或 !outcome.refresh)必须直接交给 settleOrRetryRebuild 决定,"
    + "不能在两者之间插入任何刷新逻辑",
  );
  assert.ok(
    finishHead.includes("return;", settleAt),
    "命中待补发标记分支必须 return,不能继续往下跑进刷新 IIFE",
  );
});

test("codex R4 P2(A):打开/切换笔记本时,服务端仍在跑的重新合并任务恢复为本地忙碌位", async () => {
  // 忙碌位是纯前端 state:页面刷新、或另一个会话/标签页发起的重新合并,本地的
  // rebuildingNotebookIds 一开始什么都不知道——不认领就不会挂上面那条按笔记本忙碌位
  // 建键的轮询 effect,长任务因此显示空闲、完成不刷新、按钮可点却只会撞服务端 409。
  // 这条断言钉住「打开/切换笔记本时向服务端各查一次真相,running 就认领忙碌位」这条
  // 接线,且只认领不释放——idle/终态可能是本地正处在 pendingRebuildNotebookIds 之类
  // 的中间态,这条 effect 无权替它收尾。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf("fetchUnifiedKgRebuildStatus(nb).catch(() => null)");
  assert.ok(start > 0, "找不到打开笔记本时的重新合并状态恢复(被删除或改名?守卫失效)");
  const depsAt = source.indexOf("}, [currentNotebookId]);", start);
  assert.ok(depsAt > start, "找不到这条恢复 effect 的收尾依赖数组");
  const body = source.slice(start, depsAt);

  assert.ok(
    /rebuild\s*&&\s*\(rebuild\.running\s*\|\|\s*rebuild\.status\s*===\s*"running"\)/.test(body),
    "必须按 running/status===\"running\" 判定服务端的重新合并任务是否仍在跑",
  );
  assert.ok(
    body.includes("setRebuildingNotebookIds((prev) => claimNotebookSlot(prev, nb))"),
    "running 时必须认领忙碌位(claimNotebookSlot),否则下面按忙碌位建键的轮询 effect"
    + "不会挂载",
  );
  assert.ok(
    !body.includes("releaseNotebookClaim"),
    "这条 effect 只认领、不释放 —— idle/终态可能是本地正处在 pendingRebuildNotebookIds"
    + "之类的中间态,它无权替本地状态收尾",
  );
});

test("codex R17:恢复 effect 不得从 dirty 推断待补发(推翻 R10 的重载恢复)", async () => {
  // relink 每写一条边就推进 kg_mutation_seq(它是 _cluster_input_version 的一部分),
  // 普通补连期间重载 dirty 恒真——按 dirty 认领待补发会在 relink 收工后自动发起一次
  // 用户没请求过的、可能数小时的全量重聚。恢复 effect 只许恢复忙碌位追踪,绝不许
  // 认领 pendingRebuildNotebookIds。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();
  const start = source.indexOf("const relinkRunning = Boolean(relink && ");
  assert.ok(start > 0, "找不到恢复 effect");
  const end = source.indexOf("}, [currentNotebookId]);", start);
  const body = source.slice(start, end);
  assert.ok(!body.includes("setPendingRebuildNotebookIds"),
    "恢复 effect 不得认领待补发标记(R17:dirty 不是决定级信号)");
  assert.ok(body.includes("if (rebuildRunning) {") && body.includes("if (relinkRunning) {"),
    "恢复 effect 仍须恢复两类忙碌位追踪");
});

test("codex R12:submittingMaintenanceRef 的键必须按 kind 分开,不能有裸 nb 键残留", async () => {
  // 同一个库的 relink 与 rebuild 提交窗口可以重叠(relink POST 还没返回时,用户确认
  // 合并触发的是 rebuild POST)。旧代码统一用裸 nb 当键:先落地的那次 POST 在自己的
  // finally 里删掉这唯一的条目,另一次仍在飞的提交就此失去保护——轮询可能把服务端的
  // 陈旧 idle 当真终态提前收工。修法是键按 kind 分开(`${nb}:relink` / `${nb}:rebuild`,
  // 与 expectedMaintenanceJobRef 同风格)。这条断言扫描整份文件,拦住任何一处
  // add/delete/has 退回裸 nb 键的回归——不管发生在四个调用点(relinkFromKgView/
  // refreshUnifiedKg/settleOrRetryRebuild/decideMerge)与两条轮询 tick 里的哪一处。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  assert.ok(
    !/submittingMaintenanceRef\.current\.(?:add|delete|has)\(nb\)/.test(source),
    "submittingMaintenanceRef 不能再有裸 `(nb)` 键的 add/delete/has 调用——必须是"
    + "带 kind 后缀的模板字符串键,如 `(\\`${nb}:relink\\`)` 或 `(\\`${nb}:rebuild\\`)`"
    + "(变异:把任一处改回裸 nb 键,必须让这条断言报红)",
  );
});

test("codex R4 P2(B):「重新合并」的早退与 disabled 认『任一忙碌位为真即忙』(含 relinkingKg)", async () => {
  // 「重新合并」与「补上关联」共用服务端同一把按笔记本单飞锁:只看 kgRefreshBusy 时,
  // 「补上关联」在跑期间点「重新合并」仍会发起请求,白撞一次 409。
  const page = await parseModule("page.tsx");

  const confirmBody = findFunction(page, "confirmRefreshUnifiedKg").getText(page);
  assert.ok(
    /if \(kgRefreshBusy \|\| relinkingKg \|\| buildingKg\) return;/.test(confirmBody),
    "confirmRefreshUnifiedKg 的早退必须同时认 relinkingKg",
  );

  const refreshBody = findFunction(page, "refreshUnifiedKg").getText(page);
  assert.ok(
    refreshBody.includes("relinkingKg"),
    "refreshUnifiedKg 的早退必须同时认 relinkingKg(与 confirmRefreshUnifiedKg 同口径,"
    + "防止绕过确认弹窗直接调用时漏检)",
  );

  const buttons = jsxElements(page, "button").filter(
    (el) => (el.bindings?.onClick ?? "").includes("confirmRefreshUnifiedKg"),
  );
  assert.ok(
    buttons.length >= 2,
    "「重新合并」按钮至少有知识图谱视图 + 看板两处,少了说明入口被删/改名(守卫失效)",
  );
  for (const button of buttons) {
    const disabled = button.bindings?.disabled ?? "";
    assert.ok(
      disabled.includes("relinkingKg"),
      `重新合并按钮 disabled=${disabled || "(未设置)"} 里没有 relinkingKg —— 「补上`
      + "关联」在跑时这颗按钮仍可点,点了只会撞 409",
    );
  }
});

test("codex R9:adoptRunningMaintenance 两个探测任一失败即返回 unknown,不再 per-request 吞成 null", async () => {
  // 与 kg-relink-wiring-guard.test.mjs 里同名测试断言同一个共享函数(它只定义一次,同时
  // 服务于「补上关联」与「重新合并」两条 409 领养路径)——两侧各留一份,防止只改了其中一个
  // 调用点就误以为函数本身也已经修好。旧写法 `fetchXxx(nb).catch(() => null)` 把"这次
  // 探测确实失败"和"探测成功查到没在跑"混成同一个 null——网络抖动导致的假阴性会被当成
  // "两个都不在跑"。改成 Promise.allSettled 后必须显式区分:任一探测 rejected 就整体判
  // "unknown"、不碰任何忙碌位状态。
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "adoptRunningMaintenance").getText(page);

  assert.ok(
    body.includes("Promise.allSettled(["),
    "两个探测必须用 Promise.allSettled,不能各自 `.catch(() => null)` 把失败悄悄吞成 null",
  );
  assert.ok(
    !body.includes(".catch(() => null)"),
    "探测不能再逐个 catch 成 null——那会让『探测失败』和『探测成功查到没在跑』变成"
    + "同一个值,调用方分不清竞态已消散还是网络抖动",
  );
  const rejectedAt = body.indexOf(
    'rebuildResult.status === "rejected" || relinkResult.status === "rejected"',
  );
  assert.ok(rejectedAt >= 0, "必须显式检查两个探测各自的 rejected 状态");
  const unknownAt = body.indexOf('return "unknown";', rejectedAt);
  assert.ok(
    unknownAt > rejectedAt && unknownAt - rejectedAt < 100,
    "任一探测 rejected 必须立刻返回 \"unknown\",且这个 return 要紧跟在判断之后"
    + "(不能先动过忙碌位再判断失败)",
  );
  const firstMutationAt = Math.min(
    ...["setRebuildingNotebookIds", "setRelinkingNotebookIds"]
      .map((needle) => body.indexOf(needle))
      .filter((idx) => idx >= 0),
  );
  assert.ok(
    firstMutationAt > unknownAt,
    "两个忙碌位的 set 调用必须都在 \"unknown\" 提前返回之后——rejected 时不能先动过"
    + "任何一个忙碌位再返回",
  );
  // 两个探测都成功时必须按服务端真相双向归位:确认在跑就认领、确认没在跑就释放——
  // 这是唯一能同时满足「adopted 时若领养的是另一种任务,自己那种没在跑的位要释放」
  // 与「同种在跑,位已经在,原样保留」的写法。只有 claim(旧 R8 形态)会让「adopted 但
  // 是另一种任务」这一支永远无法释放调用方自己的忙碌位。
  assert.ok(
    /rebuildRunning\s*\?\s*claimNotebookSlot\(prev, nb\)\s*:\s*releaseNotebookClaim\(prev, nb\)/.test(body),
    "rebuild 忙碌位必须双向归位(running→claim,否则→release),不能只 claim 不 release",
  );
  assert.ok(
    /relinkRunning\s*\?\s*claimRelinkSlot\(prev, nb\)\s*:\s*releaseRelinkClaim\(prev, nb\)/.test(body),
    "relink 忙碌位必须双向归位(running→claim,否则→release),不能只 claim 不 release",
  );
});

// codex R11:submittingMaintenanceRef 只压制了提交期的陈旧 idle——同库再次点击时,服务端
// 共享的维护槽在这次 POST 落地**前**仍会如实回显**上一个任务**的 succeeded/failed(不止
// idle);POST 落地之后也可能有一次陈旧终态(槽被另一次提交/领养挪走)抢在真正对应这次
// 追踪的终态之前被读到。修法是按 job_id 配对:三处 rebuild POST(refreshUnifiedKg、
// decideMerge、settleOrRetryRebuild 的补发)成功后都要把响应里的 job_id 记进
// expectedMaintenanceJobRef,轮询终态只在 status.job_id 与它一致时才接受。下面五条钉住
// 这套机制的接线,与上面 codex R9 的 idle+submitting 检查互补而非取代。

test("codex R11:refreshUnifiedKg 的 POST 成功后记下 job_id,供轮询终态按 job_id 配对", async () => {
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "refreshUnifiedKg").getText(page);

  const startedAt = body.indexOf("const started = await rebuildUnifiedKg(nb);");
  assert.ok(startedAt >= 0, "refreshUnifiedKg 必须接住 POST 的返回值(拿 job_id)");
  const setAt = body.indexOf(
    "expectedMaintenanceJobRef.current.set(`${nb}:rebuild`, started.job_id);",
    startedAt,
  );
  assert.ok(
    setAt > startedAt,
    "POST 成功后必须把 job_id 记进 expectedMaintenanceJobRef(键 `${nb}:rebuild`),"
    + "否则轮询终态无法按 job_id 配对、陈旧终态照样会被当真(变异:删掉这行判据必须让"
    + "本条断言报红)",
  );
});

test("codex R11:decideMerge 落决定后启动的 rebuildUnifiedKg POST 成功同样记下 job_id", async () => {
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "decideMerge").getText(page);

  const startedAt = body.indexOf("const started = await rebuildUnifiedKg(nb);");
  assert.ok(startedAt >= 0, "decideMerge 必须接住 POST 的返回值(拿 job_id)");
  const setAt = body.indexOf(
    "expectedMaintenanceJobRef.current.set(`${nb}:rebuild`, started.job_id);",
    startedAt,
  );
  assert.ok(
    setAt > startedAt,
    "POST 成功后必须把 job_id 记进 expectedMaintenanceJobRef,否则这条决定生效后的"
    + "终态可能被一次陈旧的 succeeded/failed 抢先吞掉",
  );
});

test("codex R11:settleOrRetryRebuild 的补发 POST 成功后用新 job_id 覆盖 expectation", async () => {
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到重新合并的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  const body = source.slice(start, depsAt);

  const retryAt = body.indexOf("async function settleOrRetryRebuild()");
  assert.ok(retryAt >= 0, "找不到待补发消费点(settleOrRetryRebuild 被改名或删除?守卫失效)");
  const retryBody = body.slice(retryAt);

  const startedAt = retryBody.indexOf("const started = await rebuildUnifiedKg(nb);");
  assert.ok(startedAt >= 0, "补发也必须接住 POST 的返回值(拿新的 job_id)");
  const setAt = retryBody.indexOf(
    "expectedMaintenanceJobRef.current.set(`${nb}:rebuild`, started.job_id);",
    startedAt,
  );
  assert.ok(
    setAt > startedAt,
    "补发成功后必须用**新** job_id 覆盖 expectedMaintenanceJobRef——不覆盖,轮询会继续"
    + "按上一个(已经终结)的 job_id 配对,永远等不到这次补发任务的终态",
  );
  const consumeAt = retryBody.indexOf(
    "setPendingRebuildNotebookIds((prev) => releaseNotebookClaim(prev, nb))",
  );
  assert.ok(
    setAt < consumeAt,
    "job_id 必须先记下、再消费待补发标记——与另两处 POST 记 job_id 的顺序保持一致",
  );
});

test("codex R11:pollTick 正常终态分支必须无条件拒收提交期终态、并按 job_id 配对", async () => {
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到重新合并的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  const body = source.slice(start, depsAt);

  const pollAt = body.indexOf("function pollTick()");
  assert.ok(pollAt >= 0, "找不到轮询 tick(被改名或改回内联 setInterval 回调?守卫失效)");
  const pollBody = body.slice(pollAt);

  const outcomeDeclAt = pollBody.indexOf("let outcome;");
  assert.ok(outcomeDeclAt >= 0, "找不到正常终态分支的 `let outcome;`");
  const finishAt = pollBody.indexOf("finish(outcome)", outcomeDeclAt);
  assert.ok(finishAt >= 0, "找不到正常终态分支的 finish(outcome) 调用点");
  const guardWindow = pollBody.slice(outcomeDeclAt, finishAt);

  // 提交期(POST 还没落地)必须无条件继续轮询,不止 idle 一种终态——同库再次点击时,
  // 服务端在这次 POST 落地前仍可能如实回显上一个任务的 succeeded/failed。codex R12:
  // 键必须按 kind 分开查`${nb}:rebuild`,不能是裸 nb(否则会被同库一次不相干的 relink
  // 提交拖住)。
  const idleSubmittingAt = guardWindow.indexOf(
    'if (status.status === "idle" && submittingMaintenanceRef.current.has(`${nb}:rebuild`)) return;',
  );
  assert.ok(idleSubmittingAt >= 0, "找不到 codex R9 的 idle+提交中判据(被删改?守卫失效)");
  const broadSubmittingAt = guardWindow.indexOf(
    "if (submittingMaintenanceRef.current.has(`${nb}:rebuild`)) return;",
    idleSubmittingAt,
  );
  assert.ok(
    broadSubmittingAt > idleSubmittingAt,
    "必须在 idle+提交中判据之后再加一条无条件的提交期判据(不看 status.status)——"
    + "否则提交期读到上一个任务的 succeeded/failed(不是 idle)时会被误判成这次任务的"
    + "完成",
  );

  const expectedAt = guardWindow.indexOf(
    "const expectedRebuildJobId = expectedMaintenanceJobRef.current.get(`${nb}:rebuild`);",
    broadSubmittingAt,
  );
  assert.ok(
    expectedAt > broadSubmittingAt,
    "必须读出这次追踪期望的 job_id(expectedMaintenanceJobRef),否则无法按 job_id 配对",
  );

  // codex R13:job_id 不匹配不再无限拒收——同一个 tick 只累计一次,连续达到
  // MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK 才真收工,否则继续轮询;匹配的终态照样把
  // 计数清零(见后续两条测试钉住「删掉 streak 收工分支 = 回到无限拒收」「阈值改 1 =
  // R11 竞态回归」「新代际重置 mismatchStreak」三处)。
  const mismatchDeclAt = guardWindow.indexOf(
    "const rebuildJobMismatch = Boolean(",
    expectedAt,
  );
  assert.ok(
    mismatchDeclAt > expectedAt,
    "必须算出这次终态是否与 expectation 不匹配(rebuildJobMismatch),否则无法按"
    + "job_id 配对",
  );
  const incrementAt = guardWindow.indexOf("mismatchStreak += 1;", mismatchDeclAt);
  assert.ok(
    incrementAt > mismatchDeclAt,
    "不匹配时必须累加 mismatchStreak(变异:删掉这行,streak 永远是 0,退化成"
    + "『每次不匹配都当场收工』——单次陈旧响应就会误收工,这正是 R11 要防的竞态)",
  );
  const settleGateAt = guardWindow.indexOf(
    "if (mismatchStreak < MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK) return;",
    incrementAt,
  );
  assert.ok(
    settleGateAt > incrementAt,
    "未达阈值必须继续轮询(return),不能让单次不匹配就直接收工(变异:删掉这行判据"
    + "必须让本条断言报红——那正是把「连续两次才收工」退化回「一次不匹配就收工」的"
    + "R11 竞态回归)",
  );
  const mismatchResetAt = guardWindow.indexOf("mismatchStreak = 0;", settleGateAt);
  assert.ok(
    mismatchResetAt > settleGateAt,
    "匹配的终态(else 分支)必须把连续不匹配计数清零,不能让上一轮的残留计数带进下一次"
    + "追踪",
  );

  const settledTrueAt = guardWindow.lastIndexOf("settled = true;");
  assert.ok(
    settledTrueAt >= 0 && mismatchResetAt < settledTrueAt,
    "job_id 配对判据必须在 settled = true 之前生效,否则轮询已经先收工了",
  );
});

test("codex R13:mismatchStreak 阈值锁定为 2(变异:改成 1 = 单次陈旧响应即收工,R11 竞态回归)", async () => {
  // 论证(与文件顶部 MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK 常量注释同一份):expectation
  // 只在 POST **返回之后**才写进 expectedMaintenanceJobRef,写入那一刻服务端权威状态
  // 必然已经是这次追踪的任务;此后唯一还能读到旧 job_id/旧 idle 的途径只剩"更早发出、
  // 此刻才姗姗来迟的响应"——而轮询串行执行,连续第二次观测必然发生在第一次响应已解析
  // 之后,不可能仍是那个在飞的旧响应。两次即可确证,阈值因此必须是 2,不能退化成 1
  // (退化成 1 就是"单次不匹配即当真收工",与 R11 要修的竞态是同一件事)。这条断言与
  // kg-relink-wiring-guard.test.mjs 的同名断言共同覆盖同一个共享常量、同一个源文件。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  assert.ok(
    source.includes("const MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK = 2;"),
    "阈值常量必须字面等于 2——改成 1(或任何非 2 的值)都必须让本条断言报红",
  );
  const usageCount = (
    source.match(/mismatchStreak < MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK\) return;/g) || []
  ).length;
  assert.strictEqual(
    usageCount,
    2,
    "必须恰好两处轮询(relink + rebuild)都直接用"
    + "`mismatchStreak < MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK` 做收工判据,不能带偏移量",
  );
});

test("codex R13:pollTick 观测到 running 时立即归零 mismatchStreak,不进入 job_id 配对判据", async () => {
  // 设计:running 状态本身不校验 job_id(服务端只要这个库上有任务在跑就回 running),
  // 所以观测到它就该把连续不匹配计数清零重新开始累计——否则一次瞬时的"终态不匹配"和
  // 一次"服务端仍在跑"交替出现时,streak 会被跨越多个 tick 错误地累加到阈值。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到重新合并的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  const body = source.slice(start, depsAt);

  const pollAt = body.indexOf("function pollTick()");
  assert.ok(pollAt >= 0, "找不到轮询 tick(被改名或改回内联 setInterval 回调?守卫失效)");
  const pollBody = body.slice(pollAt);

  const outcomeAssignAt = pollBody.indexOf("outcome = rebuildPollOutcome(status);");
  assert.ok(outcomeAssignAt >= 0, "找不到 outcome 赋值(被改名或删除?守卫失效)");
  const notDoneAt = pollBody.indexOf("if (!outcome.done) {", outcomeAssignAt);
  assert.ok(
    notDoneAt > outcomeAssignAt,
    "outcome.done 的判断必须拆成独立分支(不能再和 cancelled/settled/"
    + "activeNotebookIdRef/generation 揉进同一个组合条件里)——mismatchStreak 的清零"
    + "动作需要一个能挂 running 语义的落脚点",
  );
  const idleSubmittingAt = pollBody.indexOf(
    'if (status.status === "idle" && submittingMaintenanceRef.current.has(`${nb}:rebuild`)) return;',
    notDoneAt,
  );
  assert.ok(idleSubmittingAt > notDoneAt, "找不到紧随其后的 codex R9 判据");
  const notDoneBranch = pollBody.slice(notDoneAt, idleSubmittingAt);

  assert.ok(
    /mismatchStreak\s*=\s*0;/.test(notDoneBranch),
    "running 分支必须把 mismatchStreak 归零(变异:删掉这行会让 running/不匹配交替"
    + "出现时计数被错误地跨 tick 累加)",
  );
  assert.ok(
    /\breturn;/.test(notDoneBranch),
    "running 分支必须 return 继续轮询,不能落到下面的 job_id 配对判据",
  );
});

test("codex R13:startPolling 每次开启新代际都归零 mismatchStreak(与 generation 同步重置)", async () => {
  // 补发重试(settleOrRetryRebuild 的成功分支)会带着一个**新** job_id 复位 settled 并调
  // startPolling() 重启轮询——上一代际遗留的 mismatchStreak 对新代际毫无意义(新
  // expectation 已经指向新 job_id),必须与 generation 一起重置,否则新代际里只需要
  // 再观测到 MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK - 1 次不匹配就会被提前收工。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到重新合并的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  const body = source.slice(start, depsAt);

  const startPollAt = body.indexOf("function startPolling() {");
  assert.ok(startPollAt >= 0, "找不到 startPolling(被改名或删除?守卫失效)");
  const setIntervalAt = body.indexOf("poll = window.setInterval(pollTick, 3000);", startPollAt);
  assert.ok(setIntervalAt >= 0, "startPolling 必须真的起一个新 interval");
  const startPollBody = body.slice(startPollAt, setIntervalAt);

  assert.ok(
    startPollBody.includes("generation += 1;"),
    "找不到 generation 自增(被删改?见 codex R5 的同名断言)",
  );
  assert.ok(
    startPollBody.includes("mismatchStreak = 0;"),
    "startPolling 必须同时归零 mismatchStreak(变异:删掉这行,补发重试成功后新代际会"
    + "带着上一代际的残留计数起步,可能只需一次不匹配就被提前收工)",
  );
});

test("codex R11:settleOrRetryRebuild 最终释放忙碌位时清掉 expectedMaintenanceJobRef 对应键", async () => {
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!busyForNotebook(rebuildingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到重新合并的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [rebuildingNotebookIds, currentNotebookId]);", start);
  const body = source.slice(start, depsAt);

  const retryAt = body.indexOf("async function settleOrRetryRebuild()");
  assert.ok(retryAt >= 0, "找不到待补发消费点(settleOrRetryRebuild 被改名或删除?守卫失效)");
  const retryBody = body.slice(retryAt);

  const releaseAt = retryBody.lastIndexOf(
    "setRebuildingNotebookIds((prev) => releaseNotebookClaim(prev, nb));",
  );
  assert.ok(releaseAt >= 0, "找不到最终释放忙碌位的调用");
  const deleteAt = retryBody.lastIndexOf(
    "expectedMaintenanceJobRef.current.delete(`${nb}:rebuild`);",
  );
  assert.ok(
    deleteAt >= 0 && deleteAt < releaseAt && releaseAt - deleteAt < 100,
    "最终释放忙碌位之前必须清掉这个库在 expectedMaintenanceJobRef 里的 expectation——"
    + "补发成功重启轮询的分支不会走到这里(已经 return),不会误清刚为新任务设下的期望值",
  );
});
