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
//      否则忙碌位永远解除不掉,按钮会卡死。
//
// 覆盖边界(如实说明):本守卫认的是源码文本形态,不是运行时行为。轮询的时序、取消与
// 切库竞态由 kg-relink-status.test.mjs 的纯函数用例 + 后端用例覆盖。
import test from "node:test";
import assert from "node:assert/strict";

import { findFunction, parseModule } from "./test/semantic-source.mjs";

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

  const busyAt = body.indexOf("setRelinkingKg(true)");
  const postAt = body.indexOf("relinkKg(");
  assert.ok(busyAt >= 0, "relinkFromKgView 必须置忙碌位");
  assert.ok(postAt >= 0, "relinkFromKgView 必须发起补上关联请求");
  assert.ok(
    busyAt < postAt,
    "忙碌位必须在 await 之前置上,否则请求在飞的那段窗口按钮还能连点",
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
  // 解除忙碌位的地方必须存在,且不在 relinkFromKgView 的 finally 里(那就是同步语义)。
  const body = findFunction(page, "relinkFromKgView").getText(page);
  assert.ok(
    !body.includes("finally"),
    "relinkFromKgView 里的 finally { setRelinkingKg(false) } 是同步语义的残留:"
    + "任务还在后台跑,按钮就已经放开了",
  );
});
