// arXiv 样板插件（X9 PR-B）检索面板的三条**行为**契约，按源码语义钉。
//
// 为什么不是组件测试。理想形态当然是 render 一次 `ArxivSearchEntry` 再点几下，
// 但那个 `.tsx` 住在 `examples/extensions/arxiv-search/ui/arxiv-search/`，
// **在 `frontend/` 之外**，于是两条路各自堵死，且两条都不是配置疏忽：
//
//   ① 直连源码：vitest 的 root 是 `frontend/`，从 `examples/...` 那个位置向上
//      找 `node_modules` 永远找不到 `frontend/node_modules`——实测报的第一个错
//      就是 `Failed to resolve import "react/jsx-dev-runtime"`，连 React 本身都
//      解析不到；即使解决了它，包内的 `../extension-sdk/contracts.ts` 也只有在
//      **同步进** `frontend/features/ext-arxiv-search/` 之后才成立。要让它可解析
//      就得往基座的 `vitest.config.ts` 里加 alias——那正是本 PR 存在的意义所反对
//      的「为一个插件给公网仓库打补丁」。
//   ② 指向同步后的副本：那份副本只在 G2 的 `scripts/check_sample_plugin.sh`
//      运行期间存在，而那条泳道跑的是 `npm run test:node`（node --test），**不跑
//      vitest**；反过来把 vitest 挂进那条泳道，又会让
//      `extension-ui-host.component.test.tsx`（钉「零插件时合并 registry 长度为 1」）
//      当场变红。
//
// 所以这里走 `conversation-title-limit-guard` 那条既有先例：没有组件接缝时，按
// **源码语义**钉住行为。好处顺带是它落在 G1（`tests/guards/*.test.mjs` 每次 PR
// 都跑）而不是 G2——被钉的这个文件在仓库里恒存在，读它不需要任何同步。
//
// 覆盖边界（如实说明）：钉的是「这几个 setter 在这个 handler 里被调到了/没被调到」
// 与「这个参数是这个标识符」。把重置搬进一个自定义 helper 再调用它
// （`callSitesIn` 只看直接调用形态，不跟进函数体）不是「抓不到」——是会**误报红**：
// 脆而不瞎，一次行为不变的重构会被这份测试拉响警报，但不会放过一个真的漏调。
// 真正的盲区是运行时反射（`obj["set" + "Selected"]()` 这类计算属性调用）：本文件里
// 唯一的负向断言（`!called.includes("setAlreadyImported")`）在这种写法下会静默转绿——
// AST 层面看不出那是一次调用，断言因而「通过」，而实际行为可能恰恰违反了它要钉的那条
// 规则。那种形态的兜底是评审，不是这份测试。
import test from "node:test";
import assert from "node:assert/strict";

import {
  callSitesIn,
  comparisonsIn,
  findFunction,
  findFunctionIn,
  jsxElements,
  jsxTextValues,
  parseRepositoryModule,
} from "../../test-support/semantic-source.mjs";

// Repository-root relative: the sample package sits outside the frontend tree,
// so this goes through `parseRepositoryModule` rather than `parseModule`. The
// read itself stays inside the semantic helper, which is where
// `static-source-policy` sanctions it; this file only ever consumes AST nodes.
const PANEL_PATH =
  "examples/extensions/arxiv-search/ui/arxiv-search/workspace-plugin.tsx";
const COMPONENT = "ArxivSearchEntry";

async function panel() {
  return parseRepositoryModule(PANEL_PATH);
}

function targets(node) {
  return callSitesIn(node).map((call) => call.target);
}

test("新检索清掉上一轮的勾选与结果目录，但保留会话级的已导入记忆", async () => {
  const source = await panel();
  const submit = findFunctionIn(source, COMPONENT, "handleSubmit");
  const called = targets(submit);

  // 勾选与目录必须一起清：只清 `selected` 而留着 `catalog`，用户看不见的旧论文
  // 仍能被 id 解析出 URL；只清 `catalog` 而留着 `selected`，导入按钮上的计数会
  // 指向一批已经解析不出来的 id。
  for (const setter of [
    "setSelected",
    "setCatalog",
    "setReceipt",
    "setImportError",
  ]) {
    assert.ok(
      called.includes(setter),
      `handleSubmit 必须调用 ${setter} —— 实际调用集合：${JSON.stringify(called)}`,
    );
  }

  // 反向：`alreadyImported` 是**跨查询**的会话记忆（「这条链接本次已经发过了」），
  // 一次新检索清掉它，就等于把重复导入的警告关掉。
  assert.ok(
    !called.includes("setAlreadyImported"),
    "handleSubmit 不得重置 alreadyImported：它是会话级的重复导入警告，不是每次查询的状态",
  );
});

test("「加载更多」翻的是已执行的那个查询串，不是输入框里的实时值", async () => {
  const source = await panel();
  const loadMore = findFunctionIn(source, COMPONENT, "handleLoadMore");
  const runSearch = callSitesIn(loadMore).find(
    (call) => call.target === "runSearch",
  );

  assert.ok(runSearch, "handleLoadMore 必须调用 runSearch");
  assert.equal(
    runSearch.arguments[0],
    "executedQuery",
    "「加载更多」的第一个参数必须是冻结的 executedQuery：读输入框的实时值会把"
      + "新关键词的第二页追加在上一个关键词的结果下面 —— 实际收到："
      + `${JSON.stringify(runSearch.arguments)}`,
  );

  // 上面那条只有在 `executedQuery` 真的被写过时才有意义：没人写它，参数就永远是
  // 空串，「冻结」也就成了「从不设置」。
  const search = findFunctionIn(source, COMPONENT, "runSearch");
  const freeze = callSitesIn(search).find(
    (call) => call.target === "setExecutedQuery",
  );
  assert.ok(freeze, "runSearch 必须写下 setExecutedQuery，否则冻结的查询串恒为空");
  assert.equal(
    freeze.arguments[0],
    "term",
    `setExecutedQuery 冻结的必须是本次实际执行的 term —— 实际收到：${JSON.stringify(freeze.arguments)}`,
  );
});

test("一次什么都没记上的导入会出声，不是静默复位", async () => {
  const source = await panel();
  const component = findFunction(source, COMPONENT);
  const empty = comparisonsIn(component).find(
    (comparison) =>
      comparison.left === "receipt.size"
      && comparison.operator === "==="
      && comparison.right === "0",
  );

  assert.ok(
    empty,
    "面板必须显式区分「回执为空」这一支：非空但零条目的回执意味着请求成功却"
      + "什么都没记上，静默复位会被读成「做完了」",
  );

  // 出声要出在无障碍树里看得见的地方，否则「出声」只是一段视觉文本。
  const status = jsxElements(source, "p").find(
    (element) => element.attributes?.role === "status",
  );
  assert.ok(
    status,
    "空回执那一支必须落在一个 role=\"status\" 的元素上",
  );

  // 元素存在不等于出了声：`<p role="status" />` 或 `<p role="status">{""}</p>`
  // 同样能通过上面那条断言，却是一段视觉与无障碍树上都空白的沉默复位。
  assert.ok(
    jsxTextValues(source).some(
      (value) => value.includes("本次导入没有收到任何结果"),
    ),
    "role=\"status\" 元素必须携带非空提示文本，不能是一个空标签",
  );
});

test("检索按钮在超过检索词上限时禁用，并给出可见提示（P2-1）", async () => {
  // 服务端真源是 routes.py::search 现在对超过 MAX_QUERY_TERMS 个词的查询显式
  // 400（不再由 client.py::build_query_url 静默截断到第 8 个词）。面板必须提供
  // 同一道护栏：按钮禁用 + 文案，而不是让用户提交后才在错误横幅里读到限制。
  const source = await panel();
  const submit = jsxElements(source, "button").find(
    (element) => element.attributes?.type === "submit",
  );
  assert.ok(submit, "找不到 type=\"submit\" 的检索按钮");

  const disabled = submit.bindings?.disabled ?? "";
  assert.ok(
    disabled.includes("overQueryTermLimit"),
    `检索按钮的 disabled 表达式必须包含 overQueryTermLimit —— 实际："${disabled}"`,
  );

  assert.ok(
    jsxTextValues(source).some((value) => value.includes("检索词最多")),
    "面板必须显式提示检索词条数上限，不能只让用户从服务端 400 里学到限制",
  );
});

test("runSearch 的防御性重检同样按检索词上限拒绝，而不是只靠按钮禁用态（P2-1）", async () => {
  // 镜像 handleImport 对 MAX_IMPORT_URLS 的既有防御性重检——按钮已经在超限时
  // 禁用，这里是给一次绕过按钮的程序化调用（或未来的第二个调用点）兜底，让它
  // 也不会花一次round trip 去证明服务端会拒绝它。
  const source = await panel();
  const runSearch = findFunctionIn(source, COMPONENT, "runSearch");
  const called = targets(runSearch);
  assert.ok(
    called.includes("countQueryTerms"),
    `runSearch 必须调用 countQueryTerms 做防御性重检 —— 实际调用集合：${JSON.stringify(called)}`,
  );
});
