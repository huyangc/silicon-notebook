// 「补上关联」后台化之后的接线守卫。
//
// 这一步的整个风险都在**接线**上,不在纯函数里:端点不再同步返回统计,而 UI 上仍有一句
// 「已补上 N 条关联」。最容易发生、也最难在评审里看出来的退化,是把那句话接回 POST 的
// 返回值——TypeScript 拦不住(那是 `any` 也好、是别的字段也好都能编译),测试里也很容易
// 用 mock 喂出一个「刚好有数字」的回执把它盖过去。所以这里按**源码语义**钉三条:
//
//   ① relinkFromKgView 不得从 relinkKg 的返回值里读统计字段;
//   ② 忙碌位在 await 之前就置上(长任务按钮红线的「立刻不可点」那一半;
//      long-task-button-guard 只钉 disabled 存在,钉不到置位时机);
//   ③ 完成信号真的存在——page.tsx 里有 fetchRelinkStatus + relinkPollOutcome 的消费点,
//      否则忙碌位永远解除不掉,按钮会卡死;
//   ④ 忙碌位与轮询都按**笔记本集合**判(经共享纯函数 relinkBusyFor / claimRelinkSlot /
//      releaseRelinkClaim),并且轮询带尝试上限。裸布尔、乃至只能记一个库的裸字符串,
//      忙碌位在类型上都完全合法、单测也照样绿,只有在「点完 A 切到 B 再点一次」时才
//      现形——后一次认领会覆盖前一次,A 的完成信号从此收不到(codex R1 P2)。
//
// 覆盖边界(如实说明):本守卫认的是源码文本形态,不是运行时行为。轮询的时序、取消与
// 切库竞态由 kg-relink-status.test.mjs 的纯函数用例 + 后端用例覆盖。
import test from "node:test";
import assert from "node:assert/strict";

import { findFunction, jsxElements, parseModule } from "./test/semantic-source.mjs";

const STATS_FIELDS = ["edges_added", "isolated_after", "isolated_before"];

test("relinkFromKgView 不假装从 POST 拿到了统计数字", async () => {
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "relinkFromKgView").getText(page);

  for (const field of STATS_FIELDS) {
    assert.ok(
      !body.includes(field),
      `relinkFromKgView 读了 ${field} —— 后台化之后 POST 只返回 job_id,`
      + "统计必须等 relink/status 的终态",
    );
  }
});

test("relinkFromKgView 在发请求之前就置忙碌位", async () => {
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "relinkFromKgView").getText(page);

  const busyAt = body.indexOf("setRelinkingNotebookIds((prev) => claimRelinkSlot(prev, nb))");
  const postAt = body.indexOf("relinkKg(");
  assert.ok(busyAt >= 0, "relinkFromKgView 必须认领忙碌位(且记下是哪个库,走 claimRelinkSlot)");
  assert.ok(postAt >= 0, "relinkFromKgView 必须发起补上关联请求");
  assert.ok(
    busyAt < postAt,
    "忙碌位必须在 await 之前置上,否则请求在飞的那段窗口按钮还能连点",
  );
});

test("忙碌位是按笔记本作用域的一个集合,不是一个裸布尔或只记一个库的字符串", async () => {
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  // 状态本身存的是 id 集合。`useState(false)` 或 `useState<string | null>(null)` 那种
  // 只能记单个库的形态在这里直接报红——点完 A 切到 B 再点一次会覆盖 A 的认领。
  assert.ok(
    /setRelinkingNotebookIds\s*\]\s*=\s*useState<Set<string>>\(new Set\(\)\)/.test(source),
    "「补上关联」的忙碌位必须是正在补的笔记本 id 集合,单值形态会在"
    + "「点完 A 切到 B 再点」时把 A 的认领覆盖掉",
  );
  // 判据与解除都走共享纯函数(kg-relink-status.test.mjs 单测了它们的每个分支),
  // 手搓 `.has(currentNotebookId)` 也能对,但没有单测钉住那几个边角。
  assert.ok(source.includes("relinkBusyFor("), "按钮/轮询的判据必须走 relinkBusyFor");
  assert.ok(
    source.includes("claimRelinkSlot("),
    "认领忙碌位必须走 claimRelinkSlot —— 手搓 `new Set(prev).add(nb)` 没有单测钉住"
    + "「只加自己那一格」这条边角",
  );
  assert.ok(
    source.includes("releaseRelinkClaim("),
    "解除忙碌位必须走 releaseRelinkClaim —— 直接清空整个集合会把"
    + "用户刚在另一个库点起来的那次一并抹掉",
  );
  assert.ok(
    !/setRelinkingNotebookIds\(new Set\(\)\)/.test(source),
    "无条件清空整个忙碌位集合就是上面那条要防的形态",
  );
});

test("轮询有尝试上限,且不把 kgLimit 塞进依赖里", async () => {
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  // 断言必须落在 effect **体内**:光看整份文件里出现过 RELINK_POLL_MAX_ATTEMPTS 拦不住
  // 「把上限那个分支整块删掉、import 还留着」——实测那样改守卫会假绿。
  const start = source.indexOf(
    "if (!relinkBusyFor(relinkingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到补上关联的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [relinkingNotebookIds, currentNotebookId]);", start);
  assert.ok(
    depsAt > start && depsAt - start < 3000,
    "轮询 effect 的依赖必须恰好是 [relinkingNotebookIds, currentNotebookId]"
    + "(kgLimit 进依赖会在换范围时重启轮询、重置尝试计数)",
  );
  const body = source.slice(start, depsAt);

  assert.ok(
    body.includes("RELINK_POLL_MAX_ATTEMPTS"),
    "轮询必须有尝试上限:后端只在进程内记这件事,任务卡住时 status 会一直如实回报"
    + "running,没有上限按钮就永远解锁不了",
  );
  assert.ok(
    body.includes("RELINK_POLL_TIMED_OUT"),
    "超限也要走一份显式回执,而不是静默把忙碌位一丢",
  );
  // 范围改了不该重启轮询,所以经 ref 读。
  assert.ok(
    body.includes("kgLimitRef.current"),
    "轮询里的图谱重拉必须经 kgLimitRef 读当前范围",
  );
});

test("终态先刷新、刷新完成后才释放忙碌位（不是反过来）", async () => {
  // 双 opus 评审 P1(#478 同型 bug 的第二处):release 若在刷新之前置,会改
  // relinkingNotebookIds → 触发这条 effect 的 cleanup → 把这次刷新自己的 cancelled
  // 闭包置 true → 三个真实 fetch 回来时 setState 被自己的 cleanup 丢弃。jsdom 实测
  // fetch 耗时 ≥5ms 时恒 CANCELLED,生产网络请求远超 5ms,图谱因此永远刷不出来。
  // 断言按**源码顺序**钉:release 调用必须落在 finish 内那个 `void (async () => {...})()`
  // IIFE 的 `await Promise.all([...])` 之后,而不是之前。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!relinkBusyFor(relinkingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到补上关联的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [relinkingNotebookIds, currentNotebookId]);", start);
  assert.ok(depsAt > start, "找不到轮询 effect 的收尾依赖数组");
  const body = source.slice(start, depsAt);

  const iifeAt = body.indexOf("void (async () => {");
  assert.ok(iifeAt >= 0, "finish 的刷新必须包在一个 void (async () => {...})() IIFE 里");
  const asyncBody = body.slice(iifeAt);
  const awaitAt = asyncBody.indexOf("await Promise.all([");
  const releaseAt = asyncBody.indexOf("releaseRelinkClaim(prev, nb)");
  assert.ok(awaitAt >= 0, "刷新必须 await 真实的 fetchUnifiedGraph/fetchUnifiedKgStatus");
  assert.ok(releaseAt >= 0, "刷新完成后必须释放忙碌位(否则按钮永远卡在忙碌态)");
  assert.ok(
    awaitAt < releaseAt,
    "release 必须在 await 刷新之后 —— 提前 release 会让 effect 的 cleanup 把这次刷新"
    + "自己的 setState 当成『已取消』丢掉(#478 同型 bug)",
  );
});

test("终态观测后先停轮询(settled+clearInterval)再收尾,防止刷新耗时跨 tick 时被重复触发", async () => {
  // codex R1 P2:interval 在 finish() 收尾期间仍然继续运行——如果刷新(三个真实 fetch)
  // 耗时超过一个 3s 周期,下一 tick 会再读到同一个终态、再调一次 finish(),让图谱重拉
  // 并发跑两份。修法是终态一旦观测到就立刻(同步)停轮询:settled 标志 + clearInterval
  // 都必须在 finish(...) 调用**之前**完成,且每个 tick 开头都要检查 settled,拦住
  // clearInterval 真正生效前已经排队的迟到 tick。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!relinkBusyFor(relinkingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到补上关联的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [relinkingNotebookIds, currentNotebookId]);", start);
  assert.ok(depsAt > start, "找不到轮询 effect 的收尾依赖数组");
  const body = source.slice(start, depsAt);

  assert.ok(
    body.includes("let settled = false;"),
    "轮询必须有 settled 标志,防止终态之后已经排队的下一 tick 重复触发 finish",
  );

  const pollAt = body.indexOf("window.setInterval(async () => {");
  assert.ok(pollAt >= 0, "找不到轮询的 setInterval 回调");
  const pollBody = body.slice(pollAt);

  assert.ok(
    /async \(\) => \{\s*if \(settled\) return;/.test(pollBody),
    "轮询回调必须在最开头检查 settled,拦住 clearInterval 生效前已经排队的迟到 tick",
  );

  // 两处 finish 调用(超限收工 + 正常终态收工)都必须先 settled = true 再
  // window.clearInterval(poll),再调 finish —— 顺序钉死,防止半吊子修复(只加了
  // settled 变量却没有真正在调用 finish 之前置位/停轮询)。两个分支必须各自**独立**
  // 圈出窗口再局部查找:如果两处都用同一个 pollBody 从头找 lastIndexOf,把 finish(outcome)
  // 挪到 settled=true/clearInterval **之前**(把改对的顺序又改回错的)时,断言会误读到
  // 超限分支里那份无关的 settled=true 而放行——这不是假设,是实测踩过的坑。
  const timeoutBranchAt = pollBody.indexOf("if (attempts > RELINK_POLL_MAX_ATTEMPTS) {");
  const outcomeDeclAt = pollBody.indexOf("let outcome;");
  assert.ok(
    timeoutBranchAt >= 0 && outcomeDeclAt > timeoutBranchAt,
    "找不到超限分支与正常终态分支之间的边界(let outcome;)",
  );
  const branches = [
    ["超限", pollBody.slice(timeoutBranchAt, outcomeDeclAt), "finish(RELINK_POLL_TIMED_OUT)"],
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

test("page.tsx 有补上关联的完成信号(否则忙碌位解除不掉)", async () => {
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  assert.ok(source.includes("fetchRelinkStatus("), "缺少 relink/status 轮询");
  assert.ok(
    source.includes("relinkPollOutcome("),
    "终态判据必须走共享纯函数,不要在组件里手搓一份",
  );
  // 解除忙碌位的地方必须存在,且不在 relinkFromKgView 的 finally 里(那就是同步语义)。
  const body = findFunction(page, "relinkFromKgView").getText(page);
  assert.ok(
    !body.includes("finally"),
    "relinkFromKgView 里的 finally { setRelinkingKg(false) } 是同步语义的残留:"
    + "任务还在后台跑,按钮就已经放开了",
  );
});

test("codex R4 P2(A):打开/切换笔记本时,服务端仍在跑的补上关联任务恢复为本地忙碌位", async () => {
  // 与「重新合并」那半(kg-rebuild-wiring-guard.test.mjs)同一条效果里挂载:忙碌位是
  // 纯前端 state,页面刷新、或另一个会话/标签页发起的补上关联,本地的
  // relinkingNotebookIds 一开始什么都不知道——不认领就不会挂上面那条按笔记本忙碌位
  // 建键的轮询 effect,长任务因此显示空闲、完成不刷新、按钮可点却只会撞服务端 409。
  // 只认领不释放——idle/终态不动本地位。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf("fetchRelinkStatus(nb).catch(() => null)");
  assert.ok(start > 0, "找不到打开笔记本时的补上关联状态恢复(被删除或改名?守卫失效)");
  const depsAt = source.indexOf("}, [currentNotebookId]);", start);
  assert.ok(depsAt > start, "找不到这条恢复 effect 的收尾依赖数组");
  const body = source.slice(start, depsAt);

  assert.ok(
    /relink\s*&&\s*\(relink\.running\s*\|\|\s*relink\.status\s*===\s*"running"\)/.test(body),
    "必须按 running/status===\"running\" 判定服务端的补上关联任务是否仍在跑",
  );
  assert.ok(
    body.includes("setRelinkingNotebookIds((prev) => claimRelinkSlot(prev, nb))"),
    "running 时必须认领忙碌位(claimRelinkSlot),否则下面按忙碌位建键的轮询 effect"
    + "不会挂载",
  );
  assert.ok(
    !body.includes("releaseRelinkClaim"),
    "这条 effect 只认领、不释放 —— idle/终态可能是本地正处在别的中间态,它无权替本地"
    + "状态收尾",
  );
});

test("codex R4 P2(B):知识图谱视图侧栏「补上关联」的早退与 disabled 认『任一忙碌位为真即忙』(含 kgRefreshBusy)", async () => {
  // 「补上关联」与「重新合并」共用服务端同一把按笔记本单飞锁:只看 relinkingKg 时,
  // 「重新合并」在跑期间点「补上关联」仍会发起请求,白撞一次 409。
  const page = await parseModule("page.tsx");

  const body = findFunction(page, "relinkFromKgView").getText(page);
  assert.ok(
    body.includes("kgRefreshBusy"),
    "relinkFromKgView 的早退必须同时认 kgRefreshBusy",
  );

  const buttons = jsxElements(page, "button").filter(
    (el) => (el.bindings?.onClick ?? "").includes("relinkFromKgView"),
  );
  assert.ok(
    buttons.length >= 2,
    "「补上关联」按钮至少有知识图谱视图侧栏 + 看板两处,少了说明入口被删/改名(守卫失效)",
  );
  // 只钉知识图谱视图侧栏那一颗(title 是它独有的静态文案) —— 看板那颗仍是历史形态
  // (disabled={relinkingKg}),不在本轮改动范围内。
  const sidebarButton = buttons.find(
    (el) => el.attributes?.title?.includes("为没建立关联的内容补上关联"),
  );
  assert.ok(sidebarButton, "找不到知识图谱视图侧栏的补上关联按钮(title 被改动?守卫失效)");
  const disabled = sidebarButton.bindings?.disabled ?? "";
  assert.ok(
    disabled.includes("kgRefreshBusy"),
    `补上关联按钮 disabled=${disabled || "(未设置)"} 里没有 kgRefreshBusy —— 「重新`
    + "合并」在跑时这颗按钮仍可点,点了只会撞 409",
  );
});
