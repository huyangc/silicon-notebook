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
  callCapabilityLabel,
  collapseCallRuns,
  groupCallsByAgent,
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

// ————————————————————————————————— 调用记录(kind='call')的纯逻辑

const call = (over = {}) => ({
  id: "c1",
  agent_profile_id: "agent-1",
  agent_name: "巡检助手",
  capability: "knowledge:read",
  created_at: "2026-08-28T00:00:00+00:00",
  ...over,
});

test("能力档译名：认不出的一律回落到中性说法，绝不把协议串上屏", () => {
  assert.equal(callCapabilityLabel("ask:execute"), "提问");
  assert.equal(callCapabilityLabel("sources:write"), "添加资料");
  // 部署侧插件可以带来这份表里没有的能力档——那不是异常,但也绝不能直出。
  assert.equal(callCapabilityLabel("plugin:whatever"), "用了这个库");
  assert.equal(callCapabilityLabel(""), "用了这个库");
});

test("按 Agent 分组：顺序规则与观察记录逐字相同，名字缺失时回落", () => {
  const groups = groupCallsByAgent([
    call({ id: "c1", agent_profile_id: "a" }),
    call({ id: "c2", agent_profile_id: "b", agent_name: "" }),
    call({ id: "c3", agent_profile_id: "a" }),
  ]);
  assert.deepEqual(groups.map((group) => group.agentProfileId), ["a", "b"]);
  assert.deepEqual(groups[0].items.map((item) => item.id), ["c1", "c3"]);
  assert.equal(groups[1].agentName, "该 Agent");
});

test("折叠：只折相邻的同一能力档，计数是真实条数", () => {
  const runs = collapseCallRuns([
    call({ id: "c1", capability: "knowledge:read" }),
    call({ id: "c2", capability: "knowledge:read" }),
    call({ id: "c3", capability: "ask:execute" }),
    call({ id: "c4", capability: "knowledge:read" }),
  ]);
  assert.deepEqual(
    runs.map((run) => [run.capability, run.count]),
    [["knowledge:read", 2], ["ask:execute", 1], ["knowledge:read", 1]],
  );
  // 不相邻的同档**不合并**:合并等于把全天的检索堆成一个数字,时间线就不再是
  // 时间线了。上面第三串就是这条判据的见证。
  assert.equal(runs.length, 3);
});

test("折叠：每一串带的是它最新那一次的时间(列表新到旧,即第一条)", () => {
  const runs = collapseCallRuns([
    call({ id: "c1", created_at: "2026-08-28T10:00:00+00:00" }),
    call({ id: "c2", created_at: "2026-08-28T09:00:00+00:00" }),
  ]);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].created_at, "2026-08-28T10:00:00+00:00");
  assert.equal(runs[0].id, "c1");
});

test("折叠：空列表返回空数组，不抛", () => {
  assert.deepEqual(collapseCallRuns([]), []);
});
