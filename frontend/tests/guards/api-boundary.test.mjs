import test from "node:test";
import assert from "node:assert/strict";

import {
  appSourceModules,
  callsIn,
  findFunction,
  importsIn,
  parseModule,
} from "../../test-support/semantic-source.mjs";

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
  const sourceLibrary = await parseModule("use-source-library.ts");
  const imported = importsIn(sourceLibrary)
    .filter((item) => item.module === "./source-api.ts")
    .map((item) => item.imported);
  assert.ok(imported.includes("getNotebookSource"), "缺 getNotebookSource");
  assert.ok(imported.includes("getNotebookSourceElementsPage"), "缺 getNotebookSourceElementsPage");
});

// 搜索框对**每个**可见笔记本各发一次请求(答的是「哪个库里有 X」),其中可能有
// 百万级对象的参考库。所以工作区必须经 `searchNotebooksBounded` 这个有并发上限的
// 入口消费,而不是自己拿 `searchNotebook` 去 map ——后者正是被修掉的那个形态,
// 它会把 N 条无索引全表查询同时压给只有 10 条连接的池子。
//
// 因此这里比原来多钉一条否定断言:page.tsx 导入了裸 `searchNotebook` 就报红。
// 单看肯定式那半是不够的——两个都导入照样能通过,而扇出早已重新失去界定。
test("notebook search is owned by Ask and fanned out through the bounded entry", async () => {
  const [ask, page] = await Promise.all([
    parseModule("ask-api.ts"),
    parseModule("page.tsx"),
  ]);
  assert.ok(findFunction(ask, "searchNotebook"), "ask-api 仍应拥有单库搜索调用");
  assert.ok(findFunction(ask, "searchNotebooksBounded"), "缺有界扇出入口");
  const fromAsk = importsIn(page)
    .filter((item) => item.module === "./ask-api")
    .map((item) => item.imported);
  assert.ok(fromAsk.includes("searchNotebooksBounded"), "工作区未经有界入口搜索");
  assert.ok(
    !fromAsk.includes("searchNotebook"),
    "工作区直接导入了 searchNotebook —— 扇出会重新失去并发上限",
  );
});
