// 集合搜索框扇出的界定守卫。
//
// 这个搜索框答的是「哪个笔记本里有 X」,所以对**每个**可见笔记本各发一次
// `GET /notebooks/{id}/search`,其中可能有百万级对象的参考库,而后端那三条腿在
// `payload::text` / `source_elements.text` 上都没有可用索引。旧写法
// (`notebooks.map(...)` + 只在前端忽略被取代的结果)在敲一个词时会把 N × 击键次数
// 条这样的查询同时压给连接池,而池子默认只有 10 条连接。
//
// 三条不变式,各自能被独立破坏,所以分开断言:
//   1. 同时在飞的请求数不超过 SEARCH_FANOUT_LIMIT;
//   2. 这个上限**跨代次**成立。这一条不是锦上添花:后端 `search_notebook` 是同步
//      route,阻塞 SQL 跑在线程池里,客户端断开它根本看不见——所以 abort 只停掉
//      浏览器那一端,已发出的查询会跑完、连接也一直占着。每个扇出各带一个 4 席位
//      的池子,快速连打就能一代叠一代,照样打满只有 10 条连接的池(codex #460 R1 P1)。
//   3. signal 传到**每一次**调用,且在**等到席位之后**复检——漏前者那次请求就
//      abort 不掉,漏后者就会为一个答案注定被丢弃的代次真发一条查询。
import test from "node:test";
import assert from "node:assert/strict";

import { SEARCH_FANOUT_LIMIT, searchNotebooksBounded } from "../../app/collection-search.ts";

/** 记录并发峰值与每次调用收到的 signal 的假 search。
 *
 * `state.hold` 是**可变**的:置 true 时请求卡住不返回(用来观察某一刻的在飞集合),
 * 置 false 后新请求立即完成。同一个 recorder 可以交给多个扇出,这正是跨代次那条
 * 断言需要的——席位是模块级的,峰值也必须跨代次一起数。
 */
function recordingSearch({ hold = false } = {}) {
  const state = { hold, inFlight: 0, peak: 0, calls: [], release: [] };
  const search = async (id, query, signal) => {
    state.calls.push({ id, query, signal });
    state.inFlight += 1;
    state.peak = Math.max(state.peak, state.inFlight);
    if (state.hold) await new Promise((resolve) => state.release.push(resolve));
    else await Promise.resolve();
    state.inFlight -= 1;
    return { hits: [{ scope: "Source", notebook_id: id, label: id, text: "" }] };
  };
  return { state, search };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

/** 放行全部卡住的请求并等这些扇出收敛。
 *
 * 必须循环放行:一个请求让出席位后,排队的下一个会立刻进来并再次卡住。
 * 先关掉 hold 再放行可以少转几圈,但循环仍是必要的——已经卡住的那批还在等。
 */
async function settle(state, promises) {
  state.hold = false;
  for (let guard = 0; guard < 200 && state.release.length; guard += 1) {
    while (state.release.length) state.release.pop()();
    await tick();
  }
  return Promise.allSettled(promises);
}

test("并发不超过 SEARCH_FANOUT_LIMIT，且每个笔记本都拿到结果", async () => {
  const ids = Array.from({ length: 25 }, (_, index) => `nb-${index}`);
  const { state, search } = recordingSearch();

  const hits = await searchNotebooksBounded(ids, "needle", undefined, search);

  assert.equal(state.calls.length, ids.length, "每个笔记本恰好搜一次");
  assert.deepEqual(Object.keys(hits).sort(), [...ids].sort());
  assert.ok(
    state.peak <= SEARCH_FANOUT_LIMIT,
    `并发峰值 ${state.peak} 超过上限 ${SEARCH_FANOUT_LIMIT}`,
  );
});

test("笔记本数少于上限时不会空转出多余的 worker", async () => {
  const { state, search } = recordingSearch();
  await searchNotebooksBounded(["nb-a", "nb-b"], "needle", undefined, search);
  assert.equal(state.calls.length, 2);
});

test("空列表直接返回空结果，不发任何请求", async () => {
  const { state, search } = recordingSearch();
  const hits = await searchNotebooksBounded([], "needle", undefined, search);
  assert.deepEqual(hits, {});
  assert.equal(state.calls.length, 0);
});

test("signal 绝不传给底层请求——传了席位就会提前归还", async () => {
  // 这条是反向的:signal 只当「发出前的闸」。一旦把它交给 fetch,abort 会让客户端
  // promise 立刻 reject、finally 立刻还席位,而服务端那条 SQL 还在跑——席位就从
  // 「服务端工作量」退化回「浏览器等待数」,下一条断言的不变式随之失效。
  const ids = Array.from({ length: 9 }, (_, index) => `nb-${index}`);
  const controller = new AbortController();
  const { state, search } = recordingSearch();

  await searchNotebooksBounded(ids, "needle", controller.signal, search);

  assert.equal(state.calls.length, ids.length);
  assert.ok(
    state.calls.every((call) => call.signal === undefined),
    "signal 被传给了底层请求",
  );
});

test("席位度量的是服务端工作：abort 不会让已发出的请求提前交还席位", async () => {
  // codex #460 R2 P1。取消 fetch 停不掉同步 route 里的阻塞 SQL,所以「已发出」的
  // 那几条必须继续占着席位直到服务端应答;否则新一代立刻拿满 4 席,服务端就变成
  // 8 条、12 条……一路把连接池打穿。
  const ids = Array.from({ length: 10 }, (_, index) => `nb-${index}`);
  const { state, search } = recordingSearch({ hold: true });
  const controller = new AbortController();

  // 排队中的任务会在 abort 后抛 AbortError，这一代注定 reject；立刻接住它，
  // 否则 Node 会把它报成 unhandledRejection（真实调用方也是当场 .catch 的）。
  const stale = searchNotebooksBounded(ids, "stale", controller.signal, search)
    .catch(() => null);
  await tick();
  assert.equal(state.calls.length, SEARCH_FANOUT_LIMIT, "前置条件:席位已被占满");

  controller.abort();
  await tick();

  const fresh = searchNotebooksBounded(ids, "fresh", undefined, search);
  await tick();

  assert.equal(
    state.calls.length, SEARCH_FANOUT_LIMIT,
    "新一代在旧请求仍在服务端执行时就拿到了席位",
  );
  assert.ok(state.peak <= SEARCH_FANOUT_LIMIT, "服务端并发越过了上限");

  await settle(state, [stale, fresh]);
  assert.ok(state.peak <= SEARCH_FANOUT_LIMIT, "收敛过程中也不许越过上限");
});

test("上限之外的笔记本要等前面的请求让出席位才开始", async () => {
  // 这一条把「界定」和「只是碰巧没超」区分开:卡住前 SEARCH_FANOUT_LIMIT 个请求,
  // 后面的必须一个都还没发出去。
  const ids = Array.from({ length: SEARCH_FANOUT_LIMIT + 3 }, (_, i) => `nb-${i}`);
  const { state, search } = recordingSearch({ hold: true });

  const pending = searchNotebooksBounded(ids, "needle", undefined, search);
  await tick();

  assert.equal(
    state.calls.length, SEARCH_FANOUT_LIMIT,
    "被卡住时不该有第 SEARCH_FANOUT_LIMIT+1 个请求发出",
  );

  await settle(state, [pending]);
  assert.equal(state.calls.length, ids.length, "让出席位后剩下的要跑完");
});

test("席位跨代次共享：两个扇出同时活着，总在飞数仍不超过上限", async () => {
  // codex #460 R1 P1。后端是同步 route,abort 停不掉已发出的查询,所以「每个扇出
  // 各 4 席」等于让快速连打一代叠一代。这条断言的是**总量**,不是每次调用的量。
  const ids = Array.from({ length: 12 }, (_, index) => `nb-${index}`);
  const { state, search } = recordingSearch({ hold: true });

  const first = searchNotebooksBounded(ids, "q1", undefined, search);
  await tick();
  // 第二代在第一代还没让出任何席位时就发起——正是快速连打的形状。
  const second = searchNotebooksBounded(ids, "q2", undefined, search);
  await tick();

  assert.equal(
    state.calls.length, SEARCH_FANOUT_LIMIT,
    "第二代不该在第一代占满席位时再叠出自己的一批",
  );
  assert.ok(
    state.peak <= SEARCH_FANOUT_LIMIT,
    `跨代次并发峰值 ${state.peak} 超过上限 ${SEARCH_FANOUT_LIMIT}`,
  );

  await settle(state, [first, second]);
  assert.equal(state.calls.length, ids.length * 2, "两代最终都要跑完");
  assert.ok(state.peak <= SEARCH_FANOUT_LIMIT, "收敛过程中也不许越过上限");
});

test("已被取代的代次拿到席位后不发请求，并把席位还回去", async () => {
  // 席位是等到了才复检 signal 的:abort 绝大多数时候落在排队期间,那时才有必要
  // 判断「这条还值不值得发」。漏掉这次复检,被取代的一代照样会真发查询。
  const ids = Array.from({ length: 8 }, (_, index) => `nb-${index}`);
  const { state, search } = recordingSearch();
  const controller = new AbortController();
  controller.abort();

  await assert.rejects(
    () => searchNotebooksBounded(ids, "stale", controller.signal, search),
    (error) => error?.name === "AbortError",
  );
  assert.equal(state.calls.length, 0, "已中止的代次不该发出任何请求");

  // 席位真的还回去了才有下一句:否则后面每一次搜索都会永远排队。
  const { state: fresh, search: freshSearch } = recordingSearch();
  const hits = await searchNotebooksBounded(ids, "live", undefined, freshSearch);
  assert.equal(fresh.calls.length, ids.length, "中止的代次泄漏了席位");
  assert.deepEqual(Object.keys(hits).sort(), [...ids].sort());
});
