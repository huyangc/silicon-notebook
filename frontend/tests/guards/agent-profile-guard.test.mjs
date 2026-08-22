// 「AI 对这个库的理解」的形态守卫(P1-T7)。
//
// 三条与 long-task-button-guard / command-catalog-button-guard 同源的红线,只是扫描
// 面换成本特性自己的三个模块(那两份的扫描面分别写死在 page.tsx 与命令目录里):
//
//   ① 「重新整理」是后台长任务(一次有界模型调用),点完必须立刻不可点并换进行态
//      文案;忙碌位必须是**按笔记本**的那份共享实现,不是裸布尔;
//   ② 面板上屏的每一句都必须是界面词 —— 内部黑话(巡固/画像/投影/…)一个都不许
//      出现在会渲染的字符串里;
//   ③ 面板只经 features/agent-profile/profile-api.ts 发请求,不自己拼 URL、不直接
//      拿 api-client、更不碰 fetch。
//
// 判据一律用**源码语义**(onClick 绑定文本、导入清单、字符串字面量),不含行号:模块
// 里上下挪动不该报红,把 disabled 摘掉、把黑话写上屏、把请求绕开 api 模块才该报红。
//
// 覆盖边界(如实说明):本守卫只钉这三条形态。运行时行为(轮询解除的时机、409 之后
// 重取)由 agent-profile-panel.component.test.tsx 覆盖,块顺序与字符护栏由
// agent-profile-model.test.mjs 覆盖。
import test from "node:test";
import assert from "node:assert/strict";

import {
  callsIn,
  importsIn,
  jsxElements,
  jsxTextValues,
  parseModule,
  stringLiterals,
} from "../../test-support/semantic-source.mjs";

const panel = await parseModule("agent-profile-panel.tsx");
const api = await parseModule("../features/agent-profile/profile-api.ts");
const model = await parseModule("../features/agent-profile/profile-model.ts");
const page = await parseModule("page.tsx");

const API_MODULE = "../features/agent-profile/profile-api.ts";
const MODEL_MODULE = "../features/agent-profile/profile-model.ts";

// —— ① 长任务按钮 ————————————————————————————————————————————————————————

// disabled 存在但恒假(`false` / `undefined` / `null`)等于没写 —— 这类「假装修好」也报红。
const TRIVIALLY_FALSE = new Set(["false", "undefined", "null"]);

function buttonsMatching(elements, match) {
  return elements.filter((element) => {
    const onClick = element.bindings?.onClick ?? element.attributes?.onClick ?? "";
    return onClick.includes(match);
  });
}

test("「重新整理」带非平凡的 disabled，且钉住那个在飞标志", () => {
  const matched = buttonsMatching(jsxElements(panel, "button"), "onRebuild(scope)");
  // 匹配为 0 说明入口被改名/删了 —— 也算失败:守卫必须响亮失败,不能静默变成空断言。
  assert.ok(matched.length > 0, "没找到「重新整理」按钮（入口被改名或删除？守卫失效）");

  const offenders = [];
  for (const element of matched) {
    const disabled = element.bindings?.disabled ?? element.attributes?.disabled;
    if (disabled === undefined) {
      offenders.push("缺 disabled —— 后台整理在跑时按钮必须不可点");
    } else if (TRIVIALLY_FALSE.has(String(disabled).trim())) {
      offenders.push(`disabled=${disabled} 恒假，等于没写`);
    } else if (!String(disabled).includes("busy")) {
      // 光断言「非平凡」拦不住「保留了别的条件、只把在飞那一项摘掉」。
      offenders.push(`disabled=${disabled} 里没有在飞标志 busy`);
    }
  }
  assert.deepEqual(offenders, []);
});

test("进行态文案按这个动作的语义写，不是笼统的「处理中」", () => {
  const source = panel.getFullText();
  assert.ok(source.includes("整理中…"), "缺进行态文案：整理中…");
  assert.ok(
    !source.includes("处理中…"),
    "出现了笼统的「处理中…」——进行态必须按这个动作自己的语义写",
  );
});

test("忙碌位是按笔记本的那份共享实现，不是裸布尔", () => {
  const source = panel.getFullText();
  // 判据/认领/解除三个都必须经共享纯函数。裸布尔在类型上完全合法、组件测试也照样
  // 绿,只有在「点完 A 切到 B 再点一次」时才现形(A 的追踪被后一次认领冲掉)。
  for (const helper of ["busyForNotebook", "claimNotebookSlot", "releaseNotebookClaim"]) {
    assert.ok(source.includes(`${helper}(`), `面板没有经 ${helper} 管忙碌位`);
  }
  const imported = importsIn(panel)
    .filter((item) => item.module === MODEL_MODULE)
    .map((item) => item.imported);
  for (const helper of ["busyForNotebook", "claimNotebookSlot", "releaseNotebookClaim"]) {
    assert.ok(imported.includes(helper), `${helper} 不是从 profile-model 拿的`);
  }
  // 那三个函数在 profile-model 里是**再导出**共享实现,不是本地又写一份。
  const reexported = importsIn(model)
    .filter((item) => item.module === "../kg-maintenance/notebook-busy-set.ts")
    .map((item) => item.imported);
  assert.deepEqual(
    new Set(reexported),
    new Set(["busyForNotebook", "claimNotebookSlot", "releaseNotebookClaim"]),
  );
});

test("忙碌与超限判据只许住在有单测的纯函数模块里（搬进组件即报红）", () => {
  // 判据一旦内联进组件,判据还在、单测却再也测不到它,下一次有人把「服务端说不跑了
  // 才解除」改成「POST 返回即解除」不会有任何东西报红(命令目录那条守卫的原话)。
  const imported = importsIn(panel)
    .filter((item) => item.module === MODEL_MODULE)
    .map((item) => item.imported);
  for (const helper of ["isUnderstandingChainBusy", "understandingValueTooLong", "orderedUnderstandingBlocks"]) {
    assert.ok(imported.includes(helper), `${helper} 必须从 profile-model 引入，不能在组件里重写`);
  }
  const source = panel.getFullText();
  assert.ok(
    !/status\s*===\s*["']running["']/.test(source),
    "组件里出现了内联的 running 判据 —— 它的唯一声明点是 profile-model",
  );
});

// —— ② 界面词 ————————————————————————————————————————————————————————————

// 面板文案里禁止出现的内部词。ASCII 那几个带词边界断言,免得 `schemas`/`KGraph`
// 这类标识符误伤(与 scripts/check_ui_vocabulary.py 的 ASCII_TERMS 同一形态)。
// 只在**含中文的**单元里匹配,与 check_ui_vocabulary.py 逐条同口径:纯 ASCII 的串是
// 类名/枚举名/URL(`sort-button kg-schema-button`、`"corpus_shape"`),不是上屏文案,
// 拿黑名单去撞它们只会逼人把正常的类名改掉。
const CJK = /[一-鿿]/;

const FORBIDDEN = [
  ["巡固", /巡固/],
  ["画像", /画像/],
  ["投影", /投影/],
  ["抽取", /抽取/],
  ["去重", /去重/],
  ["晋升", /晋升/],
  ["chunk", /(?<![A-Za-z])chunks?(?![A-Za-z])/i],
  ["Memory", /(?<![A-Za-z])memory(?![A-Za-z])/i],
  ["schema", /(?<![A-Za-z])schemas?(?![A-Za-z])/i],
  ["KG", /(?<![A-Za-z])KG(?![A-Za-z])/],
];

test("本特性的生产代码里，会上屏的文本一个内部词都没有", () => {
  const offenders = [];
  for (const [name, module] of [
    ["agent-profile-panel.tsx", panel],
    ["features/agent-profile/profile-api.ts", api],
    ["features/agent-profile/profile-model.ts", model],
  ]) {
    // 扫描面 = 字符串字面量 + JSX 文本,与 check_ui_vocabulary.py 的「渲染文本」口径
    // 一致;注释刻意不扫(内部名留在注释里是仓库惯例)。
    const units = [...stringLiterals(module), ...jsxTextValues(module)].filter(
      (unit) => CJK.test(unit),
    );
    for (const unit of units) {
      for (const [term, pattern] of FORBIDDEN) {
        if (pattern.test(unit)) offenders.push(`${name}：「${unit}」含内部词 ${term}`);
      }
    }
  }
  assert.deepEqual(offenders, []);
});

// —— ③ 请求边界 ————————————————————————————————————————————————————————

test("面板只经 profile-api 发请求：不拼 URL、不碰 api-client、不碰 fetch", () => {
  const fromApi = new Set(
    importsIn(panel).filter((item) => item.module === API_MODULE).map((item) => item.imported),
  );
  assert.deepEqual(
    fromApi,
    new Set([
      "fetchUnderstanding",
      "saveUnderstandingBlock",
      "clearUnderstandingBlock",
      "rebuildUnderstanding",
      "fetchAgentObservations",
      "clearAgentObservations",
    ]),
  );

  const modules = importsIn(panel).map((item) => item.module);
  assert.ok(
    !modules.some((module) => module.includes("api-client")),
    "面板直接引了 api-client —— 请求入口必须收在 profile-api 一处",
  );
  assert.ok(
    !callsIn(panel).some((target) => target === "fetch" || target === "globalThis.fetch"),
    "面板里出现了裸 fetch",
  );
  // URL 拼接同样只许住在 api 模块里:组件里出现端点路径就说明有第二条通往网络的路。
  assert.ok(
    !panel.getFullText().includes("/understanding"),
    "面板里出现了端点路径 —— URL 拼接只属于 profile-api",
  );
  assert.ok(
    !panel.getFullText().includes("/agent-observations"),
    "面板里出现了端点路径 —— URL 拼接只属于 profile-api",
  );
  // 反向:api 模块确实经共用客户端发请求(而不是自己 fetch)。
  assert.ok(
    importsIn(api).some((item) => item.module.includes("api-client")),
    "profile-api 没有经 api-client 发请求",
  );
});

// —— ④ 切库结构性保险 ——————————————————————————————————————————————————
//
// page.tsx 关弹窗的 effect(`useEffect(() => setUnderstandingOpen(false), [currentNotebookId])`)
// 是第二层保险,不是本条守卫的对象——effect 跑在渲染**之后**,`AgentProfilePanel` 在
// 那之前会先用新的 `notebookId` 重渲染一次,草稿/忙碌位/确认态这时还是旧库的。
// `key={currentNotebookId}` 让组件随切库**立即整体重挂**,状态清零与 prop 变化同一次
// 渲染完成,不必等 effect 追上来。判据用 JSX 绑定文本,不含行号——挪动这行不该报红。
test("AgentProfilePanel 随 currentNotebookId 整体重挂（key，不只靠关弹窗 effect）", () => {
  const matched = jsxElements(page, "AgentProfilePanel");
  assert.ok(matched.length > 0, "没找到 AgentProfilePanel 挂载点（改名或删除？守卫失效）");
  const offenders = matched.filter((element) => element.bindings?.key !== "currentNotebookId");
  assert.deepEqual(
    offenders.map((element) => element.scope),
    [],
    "AgentProfilePanel 缺 key={currentNotebookId} —— 切库时状态可能残留到下一个库",
  );
});


test("理解入口只由 workspace 插件提供，旧知识图谱入口已移除", async () => {
  assert.equal(jsxElements(page, "UnderstandingEntryButton").length, 0);
  const plugin = await parseModule("../features/agent-profile/workspace-plugin.ts");
  assert.ok(stringLiterals(plugin).includes("AI 对这个库的理解"));
  assert.ok(callsIn(plugin).includes("actions.openUnderstanding"));
});
