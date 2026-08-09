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
  assert.ok(
    !body.includes("finally"),
    "refreshUnifiedKg 里的 finally { setKgRefreshBusy(false) } 是同步语义的残留:"
    + "任务还在后台跑,按钮就已经放开了",
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
    // 选中概念重对账,两处都在这段区间内)——阈值只是防「查找到很远之后一个不相干的收尾
    // 数组」的护栏,不是精确长度断言,跟着真实需要一起调没有问题。
    depsAt > start && depsAt - start < 7200,
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
    /function pollTick\(\) \{\s*void \(async \(\) => \{\s*if \(settled\) return;/.test(pollBody),
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
    /function pollTick\(\) \{\s*void \(async \(\) => \{\s*if \(settled\) return;\s*(?:\/\/[^\n]*\n\s*)*const myGeneration = generation;/.test(pollBody),
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

test("rebuild 轮询终态尝试补发 rebuildUnifiedKg(标记只在补发真正成功后消费;再 409 保留标记+忙碌位+续轮询,不复位 attempts)", async () => {
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

  const finalReleaseAt = retryBody.indexOf(
    "setRebuildingNotebookIds((prev) => releaseNotebookClaim(prev, nb));",
    catchAt,
  );
  assert.ok(finalReleaseAt > catchAt, "非 409 失败之后必须落到正常释放,否则忙碌位会永远卡住");

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
