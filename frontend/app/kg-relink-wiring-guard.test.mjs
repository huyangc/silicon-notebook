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
    // 上限从 3000 提到 4000(codex R11:finish 的两处释放都要清掉
    // expectedMaintenanceJobRef 的 expectation,pollTick 的正常终态分支加了「提交期
    // 不止 idle 不可信」与「job_id 必须配对」两层判据,三处都在这段区间内)——阈值只是
    // 防「查找到很远之后一个不相干的收尾数组」的护栏,不是精确长度断言,跟着真实需要
    // 一起调没有问题(同一条取舍见 kg-rebuild-wiring-guard.test.mjs 的同名断言)。
    depsAt > start && depsAt - start < 4000,
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

test("codex R9:轮询 tick 撞到 idle 且提交还在飞(submittingMaintenanceRef)时不当真终态", async () => {
  // POST 比首个 tick 慢时,轮询会先读到服务端**旧的** idle——如果这里当真终态收工,
  // 随后 POST 才成功,就再也没有人会去轮询这次真正的任务,完成也不会刷新。修法是:
  // idle 终态额外检查 submittingMaintenanceRef 是否还标记着这个键,标记着就继续轮询、
  // 不 settle。这条断言钉住:①判据必须落在正常终态分支里、settled=true 之前;②判据
  // 必须同时检查 status.status === "idle" 与
  // submittingMaintenanceRef.current.has(`${nb}:relink`)(不能只检查其中一个——只查
  // idle 会拦住所有真终态,只查 submitting 会在 running 时也误判);codex R12:键必须
  // 按 kind 分开,不能是裸 nb。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!relinkBusyFor(relinkingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到补上关联的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [relinkingNotebookIds, currentNotebookId]);", start);
  assert.ok(depsAt > start, "找不到轮询 effect 的收尾依赖数组");
  const body = source.slice(start, depsAt);

  const pollAt = body.indexOf("window.setInterval(async () => {");
  assert.ok(pollAt >= 0, "找不到轮询的 setInterval 回调");
  const pollBody = body.slice(pollAt);

  const outcomeDeclAt = pollBody.indexOf("let outcome;");
  assert.ok(outcomeDeclAt >= 0, "找不到正常终态分支的 `let outcome;`");
  const finishAt = pollBody.indexOf("finish(outcome)", outcomeDeclAt);
  assert.ok(finishAt >= 0, "找不到正常终态分支的 finish(outcome) 调用点");
  const guardWindow = pollBody.slice(outcomeDeclAt, finishAt);

  assert.ok(
    /status\.status\s*===\s*"idle"\s*&&\s*submittingMaintenanceRef\.current\.has\(`\$\{nb\}:relink`\)/.test(
      guardWindow,
    ),
    "正常终态分支必须检查 `status.status === \"idle\" && submittingMaintenanceRef"
    + ".current.has(`${nb}:relink`)`——只查其中一个都不对(只查 idle 会拦住所有真终态,"
    + "只查 submitting 会在 running 时也误判);codex R12:键必须按 kind 分开,不能是"
    + "裸 nb(否则会被同库一次不相干的 rebuild 提交拖住)",
  );
  const idleCheckAt = guardWindow.search(
    /status\.status\s*===\s*"idle"\s*&&\s*submittingMaintenanceRef\.current\.has\(`\$\{nb\}:relink`\)/,
  );
  const settledTrueAt = guardWindow.lastIndexOf("settled = true;");
  assert.ok(
    settledTrueAt >= 0 && idleCheckAt >= 0 && idleCheckAt < settledTrueAt,
    "idle+提交中的判据必须在 settled = true 之前生效,否则轮询已经先收工了",
  );
});

test("page.tsx 有补上关联的完成信号(否则忙碌位解除不掉)", async () => {
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  assert.ok(source.includes("fetchRelinkStatus("), "缺少 relink/status 轮询");
  assert.ok(
    source.includes("relinkPollOutcome("),
    "终态判据必须走共享纯函数,不要在组件里手搓一份",
  );
  // 解除忙碌位的地方必须存在,且不在 relinkFromKgView 的 finally 里同步做(那就是同步
  // 语义)。codex R9 之前这里直接断言 relinkFromKgView 里完全不出现 `finally`——当时
  // 唯一可能出现的写法就是 `finally { setRelinkingKg(false) }`。R9 引入了一个合法的
  // `finally`(清理提交期标记 submittingMaintenanceRef,见下一条测试),所以这里改成
  // 更精确的断言:finally 块可以存在,但绝不能在里面同步释放忙碌位。
  const body = findFunction(page, "relinkFromKgView").getText(page);
  const finallyAt = body.indexOf("finally {");
  if (finallyAt >= 0) {
    const finallyBody = body.slice(finallyAt);
    assert.ok(
      !finallyBody.includes("setRelinkingNotebookIds"),
      "relinkFromKgView 的 finally 块不能同步释放忙碌位(setRelinkingNotebookIds):"
      + "那是同步语义的残留,任务还在后台跑,按钮就已经放开了",
    );
  }
});

test("codex R9:relinkFromKgView 提交期(POST 还没落地)标记在 submittingMaintenanceRef,finally 里清理", async () => {
  // POST 比首个 3s 轮询 tick 慢时,轮询会先读到服务端**旧的** idle 并当终态收工——随后
  // POST 才成功,没人再轮询,任务完成不会刷新。修法是在 await POST 之前标记「正在提交」,
  // 两条轮询 tick 撞到 idle 时如果自己的库还在这个标记里就不能当真终态(见下一条测试),
  // 标记必须在 finally 里清理(不管成功/非 409 失败/409 领养,都不能让标记永久卡住)。
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "relinkFromKgView").getText(page);

  const addAt = body.indexOf("submittingMaintenanceRef.current.add(`${nb}:relink`);");
  const postAt = body.indexOf("relinkKg(");
  assert.ok(
    addAt >= 0,
    "relinkFromKgView 必须在提交前标记 submittingMaintenanceRef(键必须按 kind 分开,"
    + "`${nb}:relink`——codex R12:同库的 rebuild 提交若与这次窗口重叠,不能共用裸 nb"
    + "键互相冲掉对方的保护)",
  );
  assert.ok(
    addAt < postAt,
    "标记必须在 await relinkKg 之前置上,否则 POST 在飞期间轮询读到的陈旧 idle"
    + "不会被这个标记拦住",
  );

  const finallyAt = body.indexOf("finally {");
  assert.ok(
    finallyAt >= 0,
    "relinkFromKgView 必须有 finally 块清理提交期标记,否则某条失败路径会让标记"
    + "永久卡住、轮询永远认为提交还在飞",
  );
  const deleteAt = body.indexOf(
    "submittingMaintenanceRef.current.delete(`${nb}:relink`);",
    finallyAt,
  );
  assert.ok(
    deleteAt > finallyAt,
    "finally 块必须清理提交期标记(submittingMaintenanceRef.current.delete(`${nb}:relink`))",
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

test("codex R9:补上关联 POST 撞 409 不再提前 release,决定权整个交给 adoptRunningMaintenance", async () => {
  // codex R8 只钉住了「409 必须领养」;R9 发现旧写法在领养前先 release 自己的忙碌位
  // (`setRelinkingNotebookIds((prev) => releaseRelinkClaim(prev, nb)); void
  // adoptRunningMaintenance(nb);`)——领养探测本身也可能瞬时失败,一旦失败就返回 false,
  // 而这个提前 release 已经把位清掉了;即便探测成功查到确实还是「补上关联」自己在跑
  // (同种),提前清掉的位也没有任何东西会把它补回来(adoptRunningMaintenance 当时的
  // 实现只会"认领",不会"撤销")。R9 把决定权整个交给 adoptRunningMaintenance(它现在
  // 按服务端真相双向归位),409 分支因此不该再自己动忙碌位,且必须 await 而不是 void
  // fire-and-forget(等它把归位做完,提交期标记才能在 finally 里正确清理)。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();
  const fnAt = source.indexOf("async function relinkFromKgView()");
  assert.ok(fnAt > 0, "找不到 relinkFromKgView");
  const four09At = source.indexOf("if (httpErrorStatus(err) === 409) {", fnAt);
  assert.ok(four09At > 0, "找不到 409 分支(被改名或删除?守卫失效)");
  const four09EndAt = source.indexOf("\n      }", four09At);
  assert.ok(four09EndAt > four09At, "找不到 409 分支的收尾");
  const branch = source.slice(four09At, four09EndAt);

  assert.ok(branch.includes("httpErrorStatus(err) === 409"), "409 必须被单独识别");
  assert.ok(
    branch.includes("await adoptRunningMaintenance(nb);"),
    "409 分支必须 await 领养服务端正在跑的维护任务(另一标签页发起的也要接管轮询)——"
    + "不能是 void fire-and-forget,否则提交期标记会在归位完成前就被 finally 清理",
  );
  assert.ok(
    !branch.includes("releaseRelinkClaim"),
    "409 分支不能提前 release 自己的忙碌位:决定权整个交给 adoptRunningMaintenance,"
    + "它按服务端真相双向归位(同种保留、异种释放、探测失败原样不动),调用方不需要"
    + "(也无法在不重复探测的前提下)自己再判断一次",
  );
});

test("codex R9:adoptRunningMaintenance 两个探测任一失败即返回 unknown,不再 per-request 吞成 null", async () => {
  // 旧写法 `fetchXxx(nb).catch(() => null)` 把"这次探测确实失败"和"探测成功查到没在跑"
  // 混成同一个 null——网络抖动导致的假阴性会被当成"两个都不在跑"。改成 Promise.allSettled
  // 后必须显式区分:任一探测 rejected 就整体判 "unknown"、不碰任何忙碌位状态。
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
// 追踪的终态之前被读到。修法是按 job_id 配对:relinkFromKgView 的 POST 成功后把响应里的
// job_id 记进 expectedMaintenanceJobRef,轮询终态只在 status.job_id 与它一致时才接受。
// 下面三条钉住这套机制的接线(与 kg-rebuild-wiring-guard.test.mjs 的同名分组对称),与
// 上面 codex R9 的 idle+submitting 检查互补而非取代。

test("codex R11:relinkFromKgView 的 POST 成功后记下 job_id,供轮询终态按 job_id 配对", async () => {
  const page = await parseModule("page.tsx");
  const body = findFunction(page, "relinkFromKgView").getText(page);

  const startedAt = body.indexOf("const started = await relinkKg(nb);");
  assert.ok(startedAt >= 0, "relinkFromKgView 必须接住 POST 的返回值(拿 job_id)");
  const setAt = body.indexOf(
    "expectedMaintenanceJobRef.current.set(`${nb}:relink`, started.job_id);",
    startedAt,
  );
  assert.ok(
    setAt > startedAt,
    "POST 成功后必须把 job_id 记进 expectedMaintenanceJobRef(键 `${nb}:relink`),"
    + "否则轮询终态无法按 job_id 配对、陈旧终态照样会被当真(变异:删掉这行判据必须让"
    + "本条断言报红)",
  );
});

test("codex R11:轮询正常终态分支必须无条件拒收提交期终态、并按 job_id 配对", async () => {
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!relinkBusyFor(relinkingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到补上关联的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [relinkingNotebookIds, currentNotebookId]);", start);
  const body = source.slice(start, depsAt);

  const pollAt = body.indexOf("window.setInterval(async () => {");
  assert.ok(pollAt >= 0, "找不到轮询的 setInterval 回调");
  const pollBody = body.slice(pollAt);

  const outcomeDeclAt = pollBody.indexOf("let outcome;");
  assert.ok(outcomeDeclAt >= 0, "找不到正常终态分支的 `let outcome;`");
  const finishAt = pollBody.indexOf("finish(outcome)", outcomeDeclAt);
  assert.ok(finishAt >= 0, "找不到正常终态分支的 finish(outcome) 调用点");
  const guardWindow = pollBody.slice(outcomeDeclAt, finishAt);

  // 提交期(POST 还没落地)必须无条件继续轮询,不止 idle 一种终态——同库再次点击时,
  // 服务端在这次 POST 落地前仍可能如实回显上一个任务的 succeeded/failed。codex R12:
  // 键必须按 kind 分开查`${nb}:relink`,不能是裸 nb(否则会被同库一次不相干的 rebuild
  // 提交拖住)。
  const idleSubmittingAt = guardWindow.indexOf(
    'if (status.status === "idle" && submittingMaintenanceRef.current.has(`${nb}:relink`)) return;',
  );
  assert.ok(idleSubmittingAt >= 0, "找不到 codex R9 的 idle+提交中判据(被删改?守卫失效)");
  const broadSubmittingAt = guardWindow.indexOf(
    "if (submittingMaintenanceRef.current.has(`${nb}:relink`)) return;",
    idleSubmittingAt,
  );
  assert.ok(
    broadSubmittingAt > idleSubmittingAt,
    "必须在 idle+提交中判据之后再加一条无条件的提交期判据(不看 status.status)——"
    + "否则提交期读到上一个任务的 succeeded/failed(不是 idle)时会被误判成这次任务的"
    + "完成",
  );

  const expectedAt = guardWindow.indexOf(
    "const expectedRelinkJobId = expectedMaintenanceJobRef.current.get(`${nb}:relink`);",
    broadSubmittingAt,
  );
  assert.ok(
    expectedAt > broadSubmittingAt,
    "必须读出这次追踪期望的 job_id(expectedMaintenanceJobRef),否则无法按 job_id 配对",
  );
  const mismatchAt = guardWindow.indexOf(
    "if (expectedRelinkJobId && status.job_id !== expectedRelinkJobId) return;",
    expectedAt,
  );
  assert.ok(
    mismatchAt > expectedAt,
    "存在 expectation 时,status.job_id 必须与它一致才能继续接受终态——不一致必须"
    + "return 继续轮询(变异:删掉这行判据必须让本条断言报红)",
  );

  const settledTrueAt = guardWindow.lastIndexOf("settled = true;");
  assert.ok(
    settledTrueAt >= 0 && mismatchAt < settledTrueAt,
    "job_id 配对判据必须在 settled = true 之前生效,否则轮询已经先收工了",
  );
});

test("codex R11:finish 的两条释放路径都清掉 expectedMaintenanceJobRef 对应键", async () => {
  const page = await parseModule("page.tsx");
  const source = page.getFullText();

  const start = source.indexOf(
    "if (!relinkBusyFor(relinkingNotebookIds, currentNotebookId)) return;",
  );
  assert.ok(start > 0, "找不到补上关联的轮询 effect(被改名或删除?守卫失效)");
  const depsAt = source.indexOf("}, [relinkingNotebookIds, currentNotebookId]);", start);
  const body = source.slice(start, depsAt);

  const finishAt = body.indexOf("const finish = (outcome: RelinkPollOutcome) => {");
  assert.ok(finishAt >= 0, "找不到 finish 定义(被改名?守卫失效)");
  const finishBody = body.slice(finishAt);

  // 分支一:!outcome.refresh,直接释放(不刷新)。
  const noRefreshReleaseAt = finishBody.indexOf(
    "setRelinkingNotebookIds((prev) => releaseRelinkClaim(prev, nb));",
  );
  assert.ok(noRefreshReleaseAt >= 0, "找不到『不刷新』分支的释放调用");
  const noRefreshDeleteAt = finishBody.lastIndexOf(
    "expectedMaintenanceJobRef.current.delete(`${nb}:relink`);",
    noRefreshReleaseAt,
  );
  assert.ok(
    noRefreshDeleteAt >= 0 && noRefreshReleaseAt - noRefreshDeleteAt < 100,
    "『不刷新』分支释放忙碌位之前必须先清掉 expectedMaintenanceJobRef 对应键",
  );

  // 分支二:刷新 IIFE 的 finally,释放在 await Promise.all 之后。
  const iifeAt = finishBody.indexOf("void (async () => {");
  assert.ok(iifeAt >= 0, "找不到刷新 IIFE");
  const iifeBody = finishBody.slice(iifeAt);
  const finallyReleaseAt = iifeBody.lastIndexOf(
    "setRelinkingNotebookIds((prev) => releaseRelinkClaim(prev, nb));",
  );
  assert.ok(finallyReleaseAt >= 0, "找不到刷新 IIFE finally 里的释放调用");
  const finallyDeleteAt = iifeBody.lastIndexOf(
    "expectedMaintenanceJobRef.current.delete(`${nb}:relink`);",
    finallyReleaseAt,
  );
  assert.ok(
    finallyDeleteAt >= 0 && finallyReleaseAt - finallyDeleteAt < 100,
    "刷新 IIFE 的 finally 释放忙碌位之前必须先清掉 expectedMaintenanceJobRef 对应键——"
    + "两条释放路径(不刷新 / 刷新后)都必须清,漏一条就会让这张表随笔记本无限攒旧键",
  );
});
