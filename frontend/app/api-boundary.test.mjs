import test from "node:test";
import assert from "node:assert/strict";

import {
  appSourceModules,
  callsIn,
  findFunction,
  importsIn,
  parseModule,
} from "./test/semantic-source.mjs";

test("production HTTP calls are owned by api-client", async () => {
  const offenders = [];
  for (const { path, module } of await appSourceModules()) {
    if (path === "api-client.ts") continue;
    const direct = callsIn(module).filter((target) => target === "fetch" || target === "globalThis.fetch");
    if (direct.length > 0) offenders.push({ path, direct });
  }
  assert.deepEqual(offenders, []);
});

// 参与集内的代理读取:来源详情/元素必须经 active 笔记本维度的端点取,浏览器绝不
// 直连另一个库(挂载参考库 ≠ 该库的直接成员权限,红线)。把「工作区导入了这两个
// 代理读取函数」钉成语义断言——改回只导入裸 getSource/getSourceElements 就报红。
test("source detail is opened through the active-notebook proxy readers", async () => {
  const page = await parseModule("page.tsx");
  const imported = importsIn(page)
    .filter((item) => item.module === "./source-api")
    .map((item) => item.imported);
  assert.ok(imported.includes("getNotebookSource"), "缺 getNotebookSource");
  assert.ok(imported.includes("getNotebookSourceElements"), "缺 getNotebookSourceElements");
});

test("notebook search is owned by Ask and imported by the workspace", async () => {
  const [ask, page] = await Promise.all([
    parseModule("ask-api.ts"),
    parseModule("page.tsx"),
  ]);
  assert.ok(findFunction(ask, "searchNotebook"));
  assert.ok(importsIn(page).some((item) => (
    item.module === "./ask-api" && item.imported === "searchNotebook"
  )));
});
