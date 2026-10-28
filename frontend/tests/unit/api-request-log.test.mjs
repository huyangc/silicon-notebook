// 共享传输的请求日志不带内容(AGENTS.md:日志不带来源内容、凭据)。
//
// `performApiRequest` 每个请求打一行 `[api] METHOD path -> status`。路径里带内容的请求
// (图谱分析问题的主键:F4 是来源派生的概念名、F2 是两篇来源 id;邀请 / 分享口令;自定义
// 对象类型名)经 `logPath` 只写路由形状;查询串只写键不写值(检索词、用户名……)。
// 日志变了,实际请求的 URL 不变,仍按原样编码。
import test from "node:test";
import assert from "node:assert/strict";

import { loggableRequestPath, requestJson } from "../../app/api-client.ts";
import { dismissKgFinding, undismissKgFinding } from "../../app/kg-analysis-api.ts";
import { joinGroupInvite, resolveUser } from "../../app/group-api.ts";
import { fetchPublicReport } from "../../app/public-report.ts";
import { fetchPublicConversation } from "../../app/public-conversation.ts";
import { deleteNotebookObjectSchema, deleteObjectSchema, updateNotebookObjectSchema, updateObjectSchema } from "../../app/knowledge-api.ts";
import { fetchConceptDetail, fetchKgNeighbors, fetchNodeContext } from "../../features/kg-maintenance/kg-api.ts";

const originalFetch = globalThis.fetch;
const originalDebug = console.debug;
const originalWindow = globalThis.window;

globalThis.window = {
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {}, length: 0, key: () => null },
  location: { reload: () => {} },
};

let fetched = [];
let logged = [];

test.beforeEach(() => {
  fetched = [];
  logged = [];
  globalThis.fetch = async (url, init) => {
    fetched.push({ url, init });
    // 读与 POST 回一个 JSON 体;PUT / PATCH / DELETE(requestVoid)回 204。
    const body = ["PUT", "PATCH", "DELETE"].includes(init?.method ?? "GET")
      ? null
      : JSON.stringify({ references: [], turns: [] });
    return new Response(body, { status: body ? 200 : 204, headers: { "Content-Type": "application/json" } });
  };
  console.debug = (...args) => { logged.push(args.join(" ")); };
});

test.after(() => {
  globalThis.fetch = originalFetch;
  console.debug = originalDebug;
  globalThis.window = originalWindow;
});

/** 这一段内容(原样、URL 编码后、编码两次)都不许出现在日志里。 */
function assertAbsent(text, secret) {
  for (const form of [secret, encodeURIComponent(secret), encodeURIComponent(encodeURIComponent(secret))]) {
    assert.equal(text.includes(form), false, `日志里出现了「${form}」:${text}`);
  }
}

test("标为正常 / 撤销:日志只写路由形状,主键(F4 含「/」的概念名、F2 含「|」的来源对)不进日志;实际 URL 仍逐段编码", async () => {
  for (const [ruleId, subjectKey] of [["F4", "tcp/ip 协议栈"], ["F2", "src-b|src-c"]]) {
    for (const call of [dismissKgFinding, undismissKgFinding]) {
      fetched = [];
      logged = [];
      await call("nb-1", ruleId, subjectKey);
      assert.equal(fetched.length, 1);
      assert.equal(
        fetched[0].url,
        `http://127.0.0.1:8000/api/notebooks/nb-1/kg-analysis/findings/${ruleId}/${encodeURIComponent(subjectKey)}/dismissal`,
      );
      // logPath 只给日志用,不会作为 fetch 的参数带出去。
      assert.equal("logPath" in fetched[0].init, false);
      assert.equal(logged.length, 1);
      assert.match(logged[0], new RegExp(`^\\[api\\] (PUT|DELETE) /notebooks/nb-1/kg-analysis/findings/${ruleId}/\\{subject_key\\}/dismissal -> 204 `));
      assertAbsent(logged[0], subjectKey);
      for (const part of subjectKey.split(/[/|]/)) assertAbsent(logged[0], part);
    }
  }
});

test("邀请口令、分享口令、自定义对象类型名都不进日志", async () => {
  await joinGroupInvite("inv-secret-123");
  await fetchPublicReport("rpt-secret-456");
  await fetchPublicConversation("cnv-secret-789");
  await updateObjectSchema("芯片型号", {});
  await deleteObjectSchema("芯片型号");
  await updateNotebookObjectSchema("nb-1", "芯片型号", {});
  await deleteNotebookObjectSchema("nb-1", "芯片型号");
  assert.equal(fetched.length, 7);
  assert.equal(fetched[0].url, "http://127.0.0.1:8000/api/group-invites/inv-secret-123/join");
  assert.equal(fetched[3].url, `http://127.0.0.1:8000/api/object-schemas/${encodeURIComponent("芯片型号")}`);
  const text = logged.join("\n");
  for (const secret of ["inv-secret-123", "rpt-secret-456", "cnv-secret-789", "芯片型号"]) assertAbsent(text, secret);
  assert.match(text, /POST \/group-invites\/\{token\}\/join/);
  assert.match(text, /GET \/public\/reports\/\{token\}/);
  assert.match(text, /GET \/public\/conversations\/\{token\}/);
  assert.match(text, /PATCH \/object-schemas\/\{object_type\}/);
  assert.match(text, /DELETE \/notebooks\/nb-1\/object-schemas\/\{object_type\}/);
});

test("查询串只写键不写值:检索词、用户名不进日志,实际 URL 照常带上", async () => {
  await requestJson(`/notebooks/nb-1/sources?offset=0&limit=50&q=${encodeURIComponent("机密 项目")}`, { tag: "test" });
  await resolveUser("alice.wang");
  assert.equal(fetched[0].url, `http://127.0.0.1:8000/api/notebooks/nb-1/sources?offset=0&limit=50&q=${encodeURIComponent("机密 项目")}`);
  assert.equal(fetched[1].url, "http://127.0.0.1:8000/api/users/resolve?username=alice.wang");
  const text = logged.join("\n");
  assertAbsent(text, "机密 项目");
  assertAbsent(text, "alice.wang");
  assert.match(text, /GET \/notebooks\/nb-1\/sources\?offset=\*&limit=\*&q=\* -> 200/);
  assert.match(text, /GET \/users\/resolve\?username=\* -> 200/);
});

test("loggableRequestPath:没有查询串、没有 logPath 时原样;logPath 自带的查询串同样只留键", () => {
  assert.equal(loggableRequestPath("/notebooks/nb-1/kg-analysis"), "/notebooks/nb-1/kg-analysis");
  assert.equal(loggableRequestPath("/a?x=1&&y=2"), "/a?x=*&y=*");
  assert.equal(loggableRequestPath("/real/secret?q=1", "/real/{id}?q=raw"), "/real/{id}?q=*");
});

test("图谱详情读(概念详情 / 邻居 / 上下文)按合并对象的 canonical id 读时,id 不进日志;对象 id 照常记;实际 URL 仍逐段编码", async () => {
  // canonical id = 类型前缀 + 成员归一后的名字(可能含「/」),是内容;ko- 开头的对象 id 是不透明的。
  const ids = ["K-quiet zebra/xylophone 协议", "KL-the cache is write back", "KF-e=mc^2", "KP-boot sequence"];
  for (const id of ids) {
    fetched = [];
    logged = [];
    await fetchConceptDetail("nb-1", id, "nb-lib", "ko-9");
    await fetchKgNeighbors("nb-1", id, 50, "nb-lib");
    await fetchNodeContext("nb-1", id, "nb-lib");
    assert.equal(fetched.length, 3);
    assert.equal(
      fetched[0].url,
      `http://127.0.0.1:8000/api/notebooks/nb-1/concepts/${encodeURIComponent(id)}/detail?source_notebook_id=nb-lib&after=ko-9`,
    );
    assert.equal(
      fetched[1].url,
      `http://127.0.0.1:8000/api/notebooks/nb-1/objects/${encodeURIComponent(id)}/neighbors?cap=50&source_notebook_id=nb-lib`,
    );
    for (const call of fetched) assert.equal("logPath" in call.init, false);
    const text = logged.join("\n");
    assert.match(text, /GET \/notebooks\/nb-1\/concepts\/\{canonical_id\}\/detail\?source_notebook_id=\*&after=\* -> 200/);
    assert.match(text, /GET \/notebooks\/nb-1\/objects\/\{canonical_id\}\/neighbors\?cap=\*&source_notebook_id=\* -> 200/);
    assert.match(text, /GET \/notebooks\/nb-1\/objects\/\{canonical_id\}\/context\?source_notebook_id=\* -> 200/);
    assertAbsent(text, id);
    for (const part of id.slice(id.indexOf("-") + 1).split(/[ /]/)) assertAbsent(text, part);
  }
  fetched = [];
  logged = [];
  await fetchConceptDetail("nb-1", "ko-1a2b");
  await fetchKgNeighbors("nb-1", "ko-1a2b");
  await fetchNodeContext("nb-1", "ko-1a2b");
  const text = logged.join("\n");
  assert.match(text, /GET \/notebooks\/nb-1\/concepts\/ko-1a2b\/detail -> 200/);
  assert.match(text, /GET \/notebooks\/nb-1\/objects\/ko-1a2b\/neighbors\?cap=\* -> 200/);
  assert.match(text, /GET \/notebooks\/nb-1\/objects\/ko-1a2b\/context -> 200/);
});
