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

import { findFunction, parseModule } from "./test/semantic-source.mjs";

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
    // 上限从 3000 提到 4500:codex R1 两条 P2(终态先 settle 再刷新 + decideMerge 409
    // 撞槽自动补发)都加在这个 effect 体内,body 因此比之前长——阈值只是防「查找到很远
    // 之后一个不相干的收尾数组」的护栏,不是精确长度断言,跟着真实需要一起调没有问题。
    depsAt > start && depsAt - start < 4500,
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

test("rebuild 轮询终态消费待补发标记、补发一次 rebuildUnifiedKg(成功保留忙碌位续轮询,再 409 才放弃)", async () => {
  // codex R1 P2:decideMerge 撞 409 只保留旧 toast、不做任何补发时,占槽任务(可能在
  // 决定落库**之前**就已经捕获输入版本)跑完发布的是旧聚类——决定被记录了,但图谱可能
  // 永远看不见它,除非用户自己想起来再点一次「重新合并」。这条测试钉住消费端:必须先
  // 判断标记存在、再消费掉标记(否则每次终态都会再补发一次)、再补发请求;补发成功且
  // 仍在同一个库要重启轮询追新任务而不是就此放手;补发再撞 409(或其它失败)必须放弃
  // 重试、落到正常释放,不能无限循环补发。
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

  const consumeAt = retryBody.indexOf(
    "setPendingRebuildNotebookIds((prev) => releaseNotebookClaim(prev, nb))",
  );
  const retryPostAt = retryBody.indexOf("rebuildUnifiedKg(nb)");
  assert.ok(
    consumeAt >= 0,
    "消费标记必须真的清掉它(releaseNotebookClaim),否则每次终态都会再补发一次",
  );
  assert.ok(
    hasMarkerAt < consumeAt && consumeAt < retryPostAt,
    "顺序必须是:先判断标记存在,再消费掉标记,再补发请求",
  );

  const successAt = retryBody.indexOf("startPolling();");
  assert.ok(successAt >= 0, "补发成功必须重启轮询(startPolling)追这次新任务,不能就此撒手不管");
  assert.ok(
    retryPostAt < successAt,
    "必须先 await 补发请求,再重启轮询——不能在请求还没定型时就当作已经成功",
  );

  const catchAt = retryBody.indexOf("catch (err) {");
  assert.ok(catchAt > successAt, "找不到补发失败的 catch 分支");
  const catchBody = retryBody.slice(catchAt);
  assert.ok(
    catchBody.includes("httpErrorStatus(err) !== 409"),
    "补发再撞 409 必须识别出来并放弃重试,不能无限循环补发",
  );
  const finalReleaseAt = retryBody.indexOf(
    "setRebuildingNotebookIds((prev) => releaseNotebookClaim(prev, nb));",
    catchAt,
  );
  assert.ok(finalReleaseAt > catchAt, "放弃补发之后必须落到正常释放,否则忙碌位会永远卡住");
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
