// PR-2.5:清单结果卡的第三个 arm —— 来源清单(库里的文档目录)。
//
// answer-panel.tsx 含 JSX,`node --test` 不能直接 import(同
// answer-panel-tier-badge.test.mjs / knowhow-citation.test.mjs 顶部记录的既有
// 限制)。所以这里分两半测:
//   1. 决策逻辑用**同形状表达式**镜像着测,并 import 真实的 label()/TIER,词表
//      漂移时这条测试跟着漂移;
//   2. 结构性的东西(第三个 arm 真的存在、标签表真的在、跨库围栏真的复用)走
//      `test/semantic-source.mjs` 的 AST 助手 —— 前端测试**不得**自己读生产源码
//      文本或做位置/顺序查询(`test/static-source-policy.test.mjs` 是硬门),而
//      镜像表达式证明不了「真实组件里那条分支存在」,只有 AST 能。
import test from "node:test";
import assert from "node:assert/strict";

import { TIER, label } from "./vocabulary.ts";
import {
  callsIn,
  comparisonsIn,
  controlFlowIn,
  declarations,
  findFunction,
  jsxElements,
  parseModule,
  variableInitializersIn,
} from "./test/semantic-source.mjs";

const panel = await parseModule("answer-panel.tsx");
const ROW = "<module>.SourceCollectionItemRow";

// 镜像 answer-panel.tsx 的三张清单标签表选择逻辑(collectionResultTitle)。
// 跨栈 parity 由 scripts/check_enumeration_list_labels_contract.py 硬门保证,
// 这里只钉「分派到哪张表」——它是空 kind 最容易出错的地方。
const ELEMENT_KIND_LIST_LABELS = {
  formula: "公式清单",
  table: "表格清单",
  image: "图片清单",
  code_block: "代码块清单",
};
const KG_OBJECT_LIST_LABELS = {
  concept: "概念知识对象清单",
  claim: "论断知识对象清单",
  formula: "公式知识对象清单",
  procedure: "过程知识对象清单",
};
const SOURCE_LIST_LABELS = { sources: "来源清单" };

function collectionResultTitle(resultSet) {
  if (resultSet.collection === "elements") {
    return label(ELEMENT_KIND_LIST_LABELS, resultSet.element_kind ?? "", "条目清单");
  }
  if (resultSet.collection === "sources") {
    return label(SOURCE_LIST_LABELS, resultSet.collection, "来源清单");
  }
  return label(KG_OBJECT_LIST_LABELS, resultSet.object_type ?? "", "知识对象清单");
}

test("来源清单卡标题走自己那张表,不落进知识对象兜底", () => {
  // 来源清单的 element_kind/object_type 都是空串 —— 这正是「不先判 collection
  // 就会出错」的形状:落进 KG 分支会把一份文档清单叫成「知识对象清单」。
  assert.equal(
    collectionResultTitle({ collection: "sources", element_kind: "", object_type: "" }),
    "来源清单",
  );
  assert.equal(
    collectionResultTitle({ collection: "elements", element_kind: "formula" }),
    "公式清单",
  );
  assert.equal(
    collectionResultTitle({ collection: "kg_objects", object_type: "concept" }),
    "概念知识对象清单",
  );
});

// 一行文档 = 标题(strong)+ 类型小字 + 摘要摘录。三者各有一条缺失时的行为。
function sourceRowText(item) {
  return {
    title: item.source_title || "未命名来源",
    type: item.location_label ?? "",
    summary: item.text ? item.text : "暂无摘要",
  };
}

test("文档行:标题缺失退回中性词,不渲染空 strong,也不吐 source_id", () => {
  const row = sourceRowText({ item_id: "s-1", source_id: "s-1", source_title: "" });
  assert.equal(row.title, "未命名来源");
  assert.ok(!row.title.includes("s-1"), "内部 id 不该上屏");
});

test("文档行:类型为空串时整段不渲染(绝不显示 academic_paper 这类内部 id)", () => {
  assert.equal(sourceRowText({ source_title: "论文一", location_label: "" }).type, "");
  assert.equal(
    sourceRowText({ source_title: "论文一", location_label: "学术论文" }).type,
    "学术论文",
  );
});

test("文档行:无摘要时说「暂无摘要」,而不是留一片空白", () => {
  assert.equal(sourceRowText({ source_title: "论文一", text: "" }).summary, "暂无摘要");
  assert.equal(sourceRowText({ source_title: "论文一", text: "摘要正文" }).summary, "摘要正文");
});

// 跨库围栏(红线):挂载参考库的条目不给「查看来源」跳转,只标注它来自哪个库。
function isCrossLibraryItem(itemNotebookId, activeNotebookId) {
  return Boolean(itemNotebookId) && itemNotebookId !== (activeNotebookId ?? "");
}

test("跨库文档条目:不给跳转,只标注来自哪个参考库", () => {
  assert.equal(isCrossLibraryItem("nb-base", "nb-active"), true);
  assert.equal(isCrossLibraryItem("nb-active", "nb-active"), false);
  // 名字查得到就写库名,查不到退回 tier 泛化文案,绝不吐裸 notebook id。
  const named = ({ "nb-base": "工艺基础" })["nb-base"];
  assert.equal(`来自参考库《${named}》`, "来自参考库《工艺基础》");
  assert.equal(label(TIER, "base", "来自参考库"), "公共知识库");
});

// --- AST 结构断言:第三个 arm 真的接上了 ---------------------------------------

test("结果卡真的渲染文档行组件(否则文档清单会落进知识对象臂)", () => {
  // 三元链的最后一臂是知识对象(它假定条目带 name/section_path),所以缺了这条
  // 显式分支的后果不是「样式不好看」,而是一份文档清单被当成知识对象清单渲染。
  // 按「CollectionResultCard 里真的渲染了 SourceCollectionItemRow」断言,比按
  // 条件表达式的写法断言更贴事实:分支怎么写都行,渲染不到就是没接上。
  assert.deepEqual(
    jsxElements(panel, "SourceCollectionItemRow").map((node) => node.scope),
    ["<module>.CollectionResultCard"],
  );
  // 渲染到了(上面)+ 真的按 collection 分派进去(这里)。只有前者时把条件改成
  // 恒假,组件仍在源码里、清单却永远走不到那一臂;只有后者时条件在、没人渲染。
  assert.ok(
    comparisonsIn(findFunction(panel, "CollectionResultCard")).some(
      (node) => node.left === "resultSet.collection"
        && node.operator === "==="
        && node.right === "sources",
    ),
    "结果卡没有按 collection === sources 分派",
  );
  const tables = declarations(panel).filter(
    (node) => node.scope === "<module>" && node.name === "SOURCE_LIST_LABELS",
  );
  assert.equal(tables.length, 1, "缺少来源清单标签表(跨栈 parity 守卫也依赖它)");
});

test("标题分派:sources 分支必须排在知识对象兜底之前(移动变异守卫)", () => {
  // 上面那条镜像测试证明不了顺序 —— 它测的是本文件里的副本。真实
  // collectionResultTitle 的最后一臂是无条件 `return label(KG_OBJECT_LIST_LABELS…)`,
  // 把 sources 分支挪到它之后就是一段永远到不了的死代码,而一份文档清单会被叫成
  // 「知识对象清单」。`controlFlowIn` 给的是**语句顺序**(AST 语义,不是文本位置),
  // 正好用来钉这件事。
  const flow = controlFlowIn(findFunction(panel, "collectionResultTitle"));
  const sourcesAt = flow.findIndex(
    (step) => step.kind === "if" && step.condition.includes('collection === "sources"'),
  );
  const fallbackAt = flow.findIndex((step) => step.kind === "return");
  assert.ok(sourcesAt >= 0, "collectionResultTitle 缺少 sources 分支");
  assert.ok(fallbackAt >= 0, "collectionResultTitle 缺少无条件兜底 return");
  assert.ok(
    sourcesAt < fallbackAt,
    "sources 分支排在无条件兜底之后 = 永不执行的死代码",
  );
});

test("文档行复用既有的跨库标注与跳转按钮,不另造一套", () => {
  const row = findFunction(panel, "SourceCollectionItemRow");
  assert.ok(callsIn(row).includes("isCrossLibraryItem"), "跨库判定必须复用同一个函数");
  assert.ok(
    jsxElements(panel, "CrossLibraryBadge").some((node) => node.scope === ROW),
    "跨库标注必须复用同一个组件",
  );
  // 「查看来源」跳转:复用既有 class。onOpenSource 出现在调用里 = 它真的被接上,
  // 而不是只声明了一个 prop。
  const jumps = jsxElements(panel, "button").filter(
    (node) => node.scope === ROW
      && node.attributes.className === "answer-collection-open",
  );
  assert.equal(jumps.length, 1, "跳转按钮必须复用既有样式且只有一个");
  assert.ok(callsIn(row).includes("onOpenSource"), "跳转必须真的调用 onOpenSource");
});

test("跨库条目的跳转口径与元素行完全一致(#398 之后不再有 !crossLibrary 围栏)", () => {
  // #398 之前跨库条目刻意不给跳转(挂载 ≠ 直接成员权限);之后参考库来源详情经
  // active notebook 维度的代理端点读取(后端只在有效参与集内解析,不在集内 404),
  // 所以跳转是**被支持的**,再挡就是把平台能力挡掉。这条钉的是「两个行组件同口径」
  // ——一侧改了另一侧没跟,用户会在同一张答案里看到两种可点性。
  for (const scope of [ROW, "<module>.ElementCollectionItemRow"]) {
    const fences = variableInitializersIn(
      findFunction(panel, scope.split(".").pop()),
    ).map((node) => node.initializer);
    assert.ok(
      !fences.some((text) => text.includes("!crossLibrary")),
      `${scope} 又出现了 !crossLibrary 围栏谓词,与另一侧口径不一致`,
    );
  }
  // 库名标注仍必须在(跳转放开 ≠ 不告诉用户这条来自哪个参考库)。
  assert.ok(
    jsxElements(panel, "CrossLibraryBadge").some((node) => node.scope === ROW),
  );
});

test("文档行不得出现第二条跳转路径(裸链接会绕过代理读取语义)", () => {
  // 允许出现的 JSX 按钮就那一个;裸 <a> 会直连另一个库的 URL,绕开「按 active
  // notebook 过权限再在参与集内解析」这条合同。
  assert.equal(
    jsxElements(panel, "button").filter((node) => node.scope === ROW).length,
    1,
  );
  assert.deepEqual(
    jsxElements(panel, "a").filter((node) => node.scope === ROW),
    [],
    "文档行不该出现裸链接(它会绕过 onOpenSource 的代理读取语义)",
  );
});

test("默认折叠结构仍然只有一个内容容器(三个 arm 共享它)", () => {
  // e5df5e6d 把卡片内容包进 `{open && <div className="answer-collection-content">…}`。
  // sources arm 必须落在那个块**内**——落在块外就等于「文档清单永不折叠」,而且
  // 卡片关闭时就已经挂载了全部条目(那正是那次改动要避免的事)。
  //
  // 如实说明边界:本仓库的 AST 助手按**声明**给 scope,不暴露 JSX 祖先链,所以
  // 「这段 JSX 在 open 守卫之下」这一条**机器证不了**。这里能证的是「折叠容器仍然
  // 唯一」——三个 arm 若不在同一个容器里,就必然出现第二个 answer-collection-content
  // 或第二处条目渲染点(下一条计数断言覆盖后者)。嵌套本身靠人工评审。
  const contents = jsxElements(panel, "div").filter(
    (node) => node.scope === "<module>.CollectionResultCard"
      && node.attributes.className === "answer-collection-content",
  );
  assert.equal(contents.length, 1, "折叠容器必须仍然只有一个");
  // 三个 arm 各只有一处渲染点,且都在同一个组件作用域里。
  for (const arm of [
    "SourceCollectionItemRow", "ElementCollectionItemRow", "KgObjectCollectionItemRow",
  ]) {
    assert.deepEqual(
      jsxElements(panel, arm).map((node) => node.scope),
      ["<module>.CollectionResultCard"],
      arm,
    );
  }
});

test("折叠交互:open 决定是否渲染内容,按钮文案与 aria-expanded 跟随", () => {
  // 镜像 e5df5e6d 的折叠逻辑(JSX 不能直接 import),钉住来源清单也吃这套:
  // 关闭时不渲染任何条目,展开后按 collection 分派;按钮文案带清单名。
  function cardBody(collection, open) {
    if (!open) return { items: 0, toggle: `展开${collection}`, ariaExpanded: false };
    return { items: 2, toggle: `收起${collection}`, ariaExpanded: true };
  }
  assert.deepEqual(cardBody("来源清单", false), {
    items: 0, toggle: "展开来源清单", ariaExpanded: false,
  });
  assert.deepEqual(cardBody("来源清单", true), {
    items: 2, toggle: "收起来源清单", ariaExpanded: true,
  });
  // 折叠按钮真的存在且带 aria-expanded(无障碍:纯图标箭头不够)。
  const toggles = jsxElements(panel, "button").filter(
    (node) => node.scope === "<module>.CollectionResultCard"
      && node.attributes.className === "answer-collection-toggle",
  );
  assert.equal(toggles.length, 1);
  assert.equal(toggles[0].bindings?.["aria-expanded"], "open");
});
