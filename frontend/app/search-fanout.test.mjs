// 集合搜索框扇出的界定守卫。
//
// 这个搜索框答的是「哪个笔记本里有 X」,所以对**每个**可见笔记本各发一次
// `GET /notebooks/{id}/search`,其中可能有百万级对象的参考库,而后端那三条腿在
// `payload::text` / `source_elements.text` 上都没有可用索引。旧写法
// (`notebooks.map(...)` + 只在前端忽略被取代的结果)在敲一个词时会把 N × 击键次数
// 条这样的查询同时压给连接池,而池子默认只有 10 条连接。
//
// 两条不变式,各自能被独立破坏,所以分开断言:
//   1. 同时在飞的请求数不超过 SEARCH_FANOUT_LIMIT;
//   2. signal 传到**每一次**调用——漏一个,那次请求就 abort 不掉,
//      「取消上一轮」退化成「只是不看它的结果」,服务端照样跑完。
import test from "node:test";
import assert from "node:assert/strict";

import { SEARCH_FANOUT_LIMIT, searchNotebooksBounded } from "./ask-api.ts";

/** 记录并发峰值 + 每次调用收到的 signal 的假 search。`gate` 用来卡住请求。 */
function recordingSearch({ hold = false } = {}) {
  const state = { inFlight: 0, peak: 0, calls: [], release: [] };
  const search = async (id, query, signal) => {
    state.calls.push({ id, query, signal });
    state.inFlight += 1;
    state.peak = Math.max(state.peak, state.inFlight);
    if (hold) {
      await new Promise((resolve) => state.release.push(resolve));
    } else {
      await Promise.resolve();
    }
    state.inFlight -= 1;
    return { hits: [{ scope: "Source", notebook_id: id, label: id, text: "" }] };
  };
  return { state, search };
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

test("signal 传到每一次调用——否则上一轮请求 abort 不掉", async () => {
  const ids = Array.from({ length: 9 }, (_, index) => `nb-${index}`);
  const controller = new AbortController();
  const { state, search } = recordingSearch();

  await searchNotebooksBounded(ids, "needle", controller.signal, search);

  assert.equal(state.calls.length, ids.length);
  assert.ok(
    state.calls.every((call) => call.signal === controller.signal),
    "有调用没收到 signal",
  );
});

test("上限之外的笔记本要等前面的请求让出席位才开始", async () => {
  // 这一条把「界定」和「只是碰巧没超」区分开:卡住前 SEARCH_FANOUT_LIMIT 个请求,
  // 后面的必须一个都还没发出去。
  const ids = Array.from({ length: SEARCH_FANOUT_LIMIT + 3 }, (_, i) => `nb-${i}`);
  const { state, search } = recordingSearch({ hold: true });

  const pending = searchNotebooksBounded(ids, "needle", undefined, search);
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(
    state.calls.length, SEARCH_FANOUT_LIMIT,
    "被卡住时不该有第 SEARCH_FANOUT_LIMIT+1 个请求发出",
  );

  while (state.release.length) state.release.pop()();
  await new Promise((resolve) => setTimeout(resolve, 0));
  while (state.release.length) state.release.pop()();
  await pending;
  assert.equal(state.calls.length, ids.length, "让出席位后剩下的要跑完");
});
