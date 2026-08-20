// 「Agent 记录」纯逻辑单测(Agentic Memory P3-T5,T3-T5 修复轮补测)。
//
// 独立成文件而不是并进 `agent-profile-model.test.mjs`——那份文件的开头三件事
// (五块顺序、字符护栏、忙碌位)都是「AI 对这个库的理解」那五块的逻辑,与这里
// 「Agent 记录」只读/清空、按 Agent 分组、算相对时间完全是两套关注点,同后端
// `test_agent_profile_job_observations.py` 独立于 `test_agent_profile_job_overlay.py`
// 的理由一样。
//
// 钉的是两件不看渲染就能证明的事：
//   ① 按 agent_profile_id 分组时，组的先后顺序 = 该组第一条记录在原列表里出现的
//      顺序，交错出现的 Agent 记录必须各自归位、组内顺序不变；`agent_name` 缺失
//      （该 Agent 的资料已被删除）时回落到「该 Agent」而不是空字符串；
//   ② 相对时间的五个刻度分界，以及非法 ISO 输入回落到空字符串而不是抛异常或
//      渲染 "Invalid Date"。
import test from "node:test";
import assert from "node:assert/strict";

import {
  groupObservationsByAgent,
  observationRelativeTime,
} from "../../features/agent-profile/profile-model.ts";

function observation(id, agentProfileId, agentName, text = "text") {
  return { id, agent_profile_id: agentProfileId, agent_name: agentName, text, created_at: "" };
}

test("按 Agent 分组：交错出现的记录各自归位，组顺序=首次出现顺序，组内顺序不变", () => {
  const items = [
    observation("o1", "agent-a", "Agent A"),
    observation("o2", "agent-b", "Agent B"),
    observation("o3", "agent-a", "Agent A"),
    observation("o4", "agent-c", "Agent C"),
    observation("o5", "agent-b", "Agent B"),
  ];

  const groups = groupObservationsByAgent(items);

  // 组的先后顺序：A、B、C——各自第一条记录在原列表里出现的顺序，不是按 id 排序。
  assert.deepEqual(groups.map((g) => g.agentProfileId), ["agent-a", "agent-b", "agent-c"]);
  // 组内顺序不变：A 组是 [o1, o3]，不是按某种规则重排。
  assert.deepEqual(groups[0].items.map((item) => item.id), ["o1", "o3"]);
  assert.deepEqual(groups[1].items.map((item) => item.id), ["o2", "o5"]);
  assert.deepEqual(groups[2].items.map((item) => item.id), ["o4"]);
});

test("按 Agent 分组：agent_name 缺失（该 Agent 的资料已被删除）回落到「该 Agent」", () => {
  const items = [observation("o1", "agent-gone", "")];

  const groups = groupObservationsByAgent(items);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].agentName, "该 Agent");
  // 有名字的正常走查，不被回落逻辑误伤。
  const named = groupObservationsByAgent([observation("o2", "agent-x", "小助手")]);
  assert.equal(named[0].agentName, "小助手");
});

test("按 Agent 分组：空列表返回空数组，不抛", () => {
  assert.deepEqual(groupObservationsByAgent([]), []);
});

test("相对时间：五个刻度分界", () => {
  const now = Date.now();
  const isoAgo = (ms) => new Date(now - ms).toISOString();

  assert.equal(observationRelativeTime(isoAgo(10 * 1000)), "刚刚");
  assert.equal(observationRelativeTime(isoAgo(5 * 60 * 1000)), "5 分钟前");
  assert.equal(observationRelativeTime(isoAgo(3 * 3600 * 1000)), "3 小时前");
  assert.equal(observationRelativeTime(isoAgo(2 * 86400 * 1000)), "2 天前");
  // 超过 30 天回落到本地日期格式——不断言具体文案(依赖运行环境 locale),
  // 只钉住它不再是「N 天前」这种相对措辞，也不是空字符串。
  const farAway = observationRelativeTime(isoAgo(40 * 86400 * 1000));
  assert.notEqual(farAway, "");
  assert.ok(!farAway.includes("前"));
});

test("相对时间：非法 ISO 回落到空字符串，不抛也不渲染 Invalid Date", () => {
  assert.equal(observationRelativeTime("not-a-date"), "");
  assert.equal(observationRelativeTime(""), "");
  assert.equal(observationRelativeTime("2026-13-99T99:99:99Z"), "");
});
