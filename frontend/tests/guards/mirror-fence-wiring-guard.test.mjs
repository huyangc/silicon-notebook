// 镜像笔记本（跨环境增量同步，docs/incremental-sync-design.md §5）在 page.tsx 里的接线。
//
// 后端在镜像上按能力名逐格拒绝会改写同步层内容的写请求
// （`backend/app/api/deps.py::_CAPABILITY_MIRROR_FENCE`，409 + `notebook_mirrored`）。
// 前端这一层要做的是**把那些入口收起来**，判据只有 `workspaceCapabilities` 一处。
//
// 这几条钉的都是「多画了一颗必失败的按钮 / 少画了一颗本该在的按钮」——两类都不会被功能
// 测试自然抓到（jsdom 里没有一个能进到工作区的夹具），所以用 AST 钉住接线本身。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import { jsxElements, parseModule, variableInitializersIn } from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");

/** 深度优先收集满足 predicate 的节点。 */
function collect(root, predicate) {
  const out = [];
  const visit = (node) => {
    if (predicate(node)) out.push(node);
    ts.forEachChild(node, visit);
  };
  visit(root);
  return out;
}

const compact = (value) => value?.replace(/\s+/g, "");

/**
 * 一个 JSX 节点最近的**渲染条件**：向上找到第一个 `cond && <jsx>` 的左操作数，或第一个
 * 三元的条件。找不到返回 null（= 无条件渲染）。
 */
function nearestRenderCondition(node) {
  let current = node.parent;
  while (current && !ts.isSourceFile(current)) {
    if (
      ts.isBinaryExpression(current)
      && current.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
    ) {
      return compact(current.left.getText(page));
    }
    if (ts.isConditionalExpression(current)) {
      return compact(current.condition.getText(page));
    }
    current = current.parent;
  }
  return null;
}

function classNameOf(element) {
  const attribute = element.attributes.properties.find(
    (property) => ts.isJsxAttribute(property) && property.name.getText(page) === "className",
  );
  const initializer = attribute?.initializer;
  if (!initializer) return "";
  return ts.isStringLiteral(initializer) ? initializer.text : initializer.getText(page);
}

const pageInitializers = variableInitializersIn(page);

function initializerOf(name) {
  return pageInitializers.find((row) => row.name === name)?.initializer;
}


test("两道写门各有各的判据,谁都不是谁的别名", () => {
  // `readOnlyWorkspace` 管内容写（镜像上收起）；`readOnlyIndexes` 管检索索引
  // （镜像上**放行**：索引是目标端自有的派生产物，重建是目标端唯一的修复手段）。
  // 把任何一条写成另一条的别名，就把两条正交的轴合并回了一条。
  assert.equal(initializerOf("readOnlyWorkspace"), "!capabilities.canWriteNotebook");
  assert.equal(
    initializerOf("readOnlyIndexes"),
    "!capabilities.canRebuildIndexes",
    "检索索引的写门必须读 canRebuildIndexes——读 canWriteNotebook 会让镜像库的索引"
      + "永远停在导入那一刻且无从修复（scale_index:write 在围栏表里是 False）",
  );
});


// 「索引与构建」面板里的动作区共用 `.index-ctas`：知识图谱构建、概念合并走 kg:write
// （围栏挡，跟 readOnlyWorkspace），检索索引走 scale_index:write（围栏放行，跟
// readOnlyIndexes）。两类同形不同门，所以按区块里调的是哪一族 handler 分派。
const SCALE_INDEX_HANDLERS = /runScaleIndexOp|runScaleIndexIdle|handleCancelScaleIndex/;

test("检索索引的动作区由 readOnlyIndexes 把门,图谱的仍由 readOnlyWorkspace 把门", () => {
  const ctaBlocks = collect(
    page,
    (node) =>
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && classNameOf(node) === "index-ctas",
  );
  assert.ok(
    ctaBlocks.length >= 2,
    "page.tsx 里找不到 `.index-ctas` 动作区——入口被挪走了,同步更新这条守卫,别让它空转",
  );

  const scaleBlocks = ctaBlocks.filter((block) => SCALE_INDEX_HANDLERS.test(block.parent.getText(page)));
  assert.ok(
    scaleBlocks.length >= 2,
    "检索索引的 CTA 区一块都没找到,守卫在空转",
  );
  assert.deepEqual(
    scaleBlocks.map((block) => nearestRenderCondition(block)).filter((c) => c !== "!readOnlyIndexes"),
    [],
    "检索索引的 CTA 区必须写成 `{!readOnlyIndexes && (...)}`：用 readOnlyWorkspace 会把"
      + "镜像库的索引重建一并收走，而后端围栏恰恰放行 scale_index:write",
  );

  const kgBlocks = ctaBlocks.filter((block) => !SCALE_INDEX_HANDLERS.test(block.parent.getText(page)));
  assert.ok(kgBlocks.length >= 1, "图谱那一族的 CTA 区找不到了,守卫的反向一半在空转");
  assert.deepEqual(
    kgBlocks
      .map((block) => nearestRenderCondition(block))
      .filter((condition) => !condition?.includes("!readOnlyWorkspace")),
    [],
    "图谱构建/概念合并写的是同步闭包里的内容（kg:write 在围栏里是 True），"
      + "不能跟着检索索引一起放行到镜像上",
  );
});


test("来源面板顶部有镜像标注,文案与来源都不在 page.tsx 里现编", () => {
  const notices = collect(
    page,
    (node) => ts.isCallExpression(node) && node.expression.getText(page) === "mirrorSourcesNotice",
  );
  assert.equal(notices.length, 1, "镜像标注应当只有一处");
  assert.equal(
    compact(notices[0].arguments[0]?.getText(page)),
    "capabilities.mirrorOrigin",
    "源环境标识取自能力位,不在组件里重读 sync_origin",
  );
  assert.equal(
    nearestRenderCondition(notices[0]),
    "capabilities.mirrored",
    "标注的渲染条件必须是能力位 mirrored,不是就地判 sync_origin",
  );
});


test("顶栏改名、卡片菜单的两颗按钮都按能力位下发", () => {
  // owner 顶栏的标题输入框：镜像上 notebook:manage 被挡，输入框整个换成不可编辑标题
  // + 一句就地说明（不是禁用一颗还在那里的输入框，也不是发页面顶部横幅）。
  const titleInputs = collect(
    page,
    (node) =>
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && classNameOf(node) === "notebook-title-input",
  );
  assert.equal(titleInputs.length, 1, "顶栏标题输入框应当只有一处");
  assert.equal(
    nearestRenderCondition(titleInputs[0]),
    "!capabilities.canManageNotebook",
    "可编辑标题必须由 canManageNotebook 分岔——镜像上名称是同步来的，改了留不住",
  );

  const badges = jsxElements(page, "ReaderNotebookBadge");
  assert.equal(badges.length, 1);
  assert.match(
    compact(badges[0].bindings?.rename ?? ""),
    /^capabilities\.canManageNotebook\?/,
    "徽章里的行内改名同样按 canManageNotebook 下发；镜像上根本不给承接方",
  );
  assert.match(
    compact(badges[0].bindings?.mirrorNote ?? ""),
    /^capabilities\.mirrorHidesNotebookManage\?/,
    "就地说明只在**控件确实被收走**时出现(mirrorHidesNotebookManage)——按裸 `mirrored` 下发"
      + "会对纯只读成员解释一件他从未见过的事",
  );

  const menus = jsxElements(page, "NotebookMenuActions");
  assert.equal(menus.length, 1);
  assert.equal(
    compact(menus[0].bindings?.canManageNotebook ?? ""),
    "menuNotebookCapabilities.canManageNotebook",
  );
  assert.equal(
    compact(menus[0].bindings?.canDeleteNotebook ?? ""),
    "menuNotebookCapabilities.canDeleteNotebook",
    "「删除笔记本」按 canDeleteNotebook 下发：镜像只能由同步导入退役",
  );
  // 说明文案也从这一侧给,且**只在镜像时**给:组件里写死「镜像…」的话,将来某一位因为
  // 别的原因为假时会冒出一句当场说谎的解释。
  for (const [prop, note] of [
    ["manageDisabledNote", "MIRROR_NOTEBOOK_MANAGE_NOTE"],
    ["deleteDisabledNote", "MIRROR_NOTEBOOK_DELETE_NOTE"],
  ]) {
    assert.equal(
      compact(menus[0].bindings?.[prop] ?? ""),
      `menuNotebookCapabilities.mirrored?${note}:""`,
      `${prop} 必须由调用方按 mirrored 下发`,
    );
  }
});


test("每一处 workspaceCapabilities 调用都把 sync_origin 交进去", () => {
  const calls = collect(
    page,
    (node) => ts.isCallExpression(node) && node.expression.getText(page) === "workspaceCapabilities",
  );
  assert.ok(calls.length >= 4, "page.tsx 里的 workspaceCapabilities 调用点少了,守卫可能在空转");
  const offenders = calls
    .map((call) => call.arguments.map((argument) => compact(argument.getText(page))))
    .filter((args) => args.length !== 4 || !args[3].endsWith("sync_origin??\"\""));
  assert.deepEqual(
    offenders,
    [],
    "漏传 sync_origin 的调用点会把镜像库当成本地库，整屏写入口照常画出来（点了必 409）",
  );
});
