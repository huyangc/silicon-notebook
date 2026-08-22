// 自动/高级界面模式的接线守卫。
//
// 起因：两轮 codex 评审都验证过一个静默回归——把 page.tsx 里 currentSourceScope /
// currentBaseScope 的实参从 effective* 换回原始 sourceScopeSelection /
// baseScopeSelection，全部既有测试仍然绿。原始 state 在自动模式下仍可能残留用户此前
// 在高级模式下收窄过的选择（切回自动模式不清空，见 page.tsx effectiveSourceScopeSelection
// 定义处的注释），所以一旦悄悄改回读原始 state，自动模式下发出的请求会静默沿用一份
// 用户已经看不到、改不了的收窄范围——不报错，只是安静地漏检索。
//
// ⚠ 判据一律是**语义身份**（AST 节点 / 标识符文本），不碰源码偏移与行号——
// app/test/static-source-policy.test.mjs 是硬门。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import { parseModule } from "../../test-support/semantic-source.mjs";


const page = await parseModule("page.tsx");
const askSession = await parseModule("use-ask-session.ts");
const reportWorkspace = await parseModule("use-report-workspace.ts");


/**
 * 某个变量声明的初始化表达式**节点**（剥掉外层括号），要求全文件恰好一处。
 *
 * 走 AST 而不是文本，是因为要钉的是**结构**：这个值由谁算出来、第一个实参是谁。
 * 文本匹配对「把调用挪到旁边一个新变量、原表达式恢复」这类移动变异是全绿的。
 */
function initializerOf(name, module = page) {
  const found = [];
  function visit(node) {
    if (
      ts.isVariableDeclaration(node)
      && ts.isIdentifier(node.name)
      && node.name.text === name
      && node.initializer
    ) {
      found.push(node.initializer);
    }
    ts.forEachChild(node, visit);
  }
  visit(module);
  assert.equal(found.length, 1, `找不到唯一的 ${name} 定义（实际 ${found.length} 处）`);
  let expression = found[0];
  while (ts.isParenthesizedExpression(expression)) expression = expression.expression;
  return expression;
}


function askPolicyProperty(name) {
  const matches = [];
  function visit(node) {
    if (
      ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === "useAskSession"
    ) matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(page);
  assert.equal(matches.length, 1, `期望恰好一处 useAskSession 调用，实际 ${matches.length}`);
  const options = matches[0].arguments[0];
  assert.ok(options && ts.isObjectLiteralExpression(options), "useAskSession 必须接收 options 对象");
  const policy = options.properties.find((property) => (
    ts.isPropertyAssignment(property) && property.name.getText(page) === "policy"
  ));
  assert.ok(
    policy && ts.isPropertyAssignment(policy) && ts.isObjectLiteralExpression(policy.initializer),
    "useAskSession options 必须带 policy 对象",
  );
  const property = policy.initializer.properties.find((item) => (
    ts.isPropertyAssignment(item) && item.name.getText(page) === name
  ));
  assert.ok(property && ts.isPropertyAssignment(property), `Ask policy 缺少 ${name}`);
  return property.initializer.getText(page);
}


/**
 * 来源行 `.source-row` 的 className 模板字面量表达式节点（`\`source-row ...\`` 那个
 * TemplateExpression）。自动模式隐藏勾选框后只剩 2 个 grid item，globals.css 的
 * `.source-row` 是 3-track grid——不叠加 `.source-row--no-select` 修饰类就会让第三个
 * item 落进第一条 max-content track，长标题把列撑宽、把删除/打开按钮顶出可视区
 * （桌面宽度必现，默认模式）。
 */
function sourceRowClassNameTemplate() {
  const templates = [];
  function visit(node) {
    if (
      ts.isTemplateExpression(node)
      && node.head.text.startsWith("source-row compact-source-row")
    ) {
      templates.push(node);
    }
    ts.forEachChild(node, visit);
  }
  visit(page);
  assert.equal(
    templates.length,
    1,
    `期望恰好一处来源行 className 模板，实际 ${templates.length}`,
  );
  return templates[0];
}


test("非 advanced 模式下 source-row 必须叠加 source-row--no-select 修饰类", () => {
  const template = sourceRowClassNameTemplate();
  const spanTexts = template.templateSpans.map((span) => span.expression.getText(page));
  const noSelectSpan = spanTexts.find((text) => text.includes("source-row--no-select"));
  assert.ok(
    noSelectSpan,
    `className 模板里没有任何一段引用 source-row--no-select：${spanTexts.join(" | ")}`,
  );
  assert.match(
    noSelectSpan,
    /isAdvanced\(uiMode\)/,
    `source-row--no-select 的条件必须挂在 isAdvanced(uiMode) 上（advanced 时不加，auto 时才加）：${noSelectSpan}`,
  );
});


test("currentSourceScope 的第一个实参必须是 effectiveSourceScopeSelection 标识符", () => {
  const expression = initializerOf("currentSourceScope");
  assert.ok(
    ts.isCallExpression(expression),
    `currentSourceScope 必须由函数调用算出，实际：${expression.getText(page)}`,
  );
  const first = expression.arguments[0];
  assert.ok(
    first && ts.isIdentifier(first) && first.text === "effectiveSourceScopeSelection",
    "currentSourceScope 的第一个实参必须**直接**是 effectiveSourceScopeSelection 标识符；"
      + "换回 sourceScopeSelection（原始 state）会让自动模式下的请求静默沿用一份用户已"
      + `看不到、改不了的收窄范围。实际：${first ? first.getText(page) : "undefined"}`,
  );
});


test("currentBaseScope 的第一个实参必须是 effectiveBaseScopeSelection 标识符", () => {
  const expression = initializerOf("currentBaseScope");
  assert.ok(
    ts.isCallExpression(expression),
    `currentBaseScope 必须由函数调用算出，实际：${expression.getText(page)}`,
  );
  const first = expression.arguments[0];
  assert.ok(
    first && ts.isIdentifier(first) && first.text === "effectiveBaseScopeSelection",
    "currentBaseScope 的第一个实参必须**直接**是 effectiveBaseScopeSelection 标识符；"
      + `换回 baseScopeSelection（原始 state）同样会静默漏收窄。实际：${first ? first.getText(page) : "undefined"}`,
  );
});


test("selectedBaseNotebookIds 读 effectiveBaseScopeSelection，不读原始 baseScopeSelection", () => {
  const expression = initializerOf("selectedBaseNotebookIds");
  const text = expression.getText(page);
  assert.match(
    text,
    /selectedBaseIds\(\s*effectiveBaseScopeSelection\s*,/,
    `selectedBaseNotebookIds 必须由 selectedBaseIds(effectiveBaseScopeSelection, ...) 算出：${text}`,
  );
});


test("selectedLocalSourceCount（本地计数）读 effectiveSourceScopeSelection，不读原始 sourceScopeSelection", () => {
  const expression = initializerOf("selectedLocalSourceCount");
  const text = expression.getText(page);
  assert.match(
    text,
    /selectedSourceCount\(\s*effectiveSourceScopeSelection\s*,/,
    `selectedLocalSourceCount 必须由 selectedSourceCount(effectiveSourceScopeSelection, ...) 算出：${text}`,
  );
});


/**
 * `runAskStream` 第二个实参所指向的那个对象字面量。同 base-scope-wiring.test.mjs 的
 * 同名做法：从调用点出发反查声明，不按变量名在全文件里找——page.tsx 8000 行，
 * `payload` 不具身份。
 */
function askStreamPayloadObject() {
  const calls = [];
  function findCalls(node) {
    if (
      ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === "runAskStream"
    ) {
      calls.push(node);
    }
    ts.forEachChild(node, findCalls);
  }
  findCalls(askSession);
  assert.equal(calls.length, 1, `期望恰好一处 runAskStream 调用，实际 ${calls.length}`);

  const argument = calls[0].arguments[1];
  assert.ok(
    argument && ts.isIdentifier(argument),
    "runAskStream 的第二个实参应是一个具名 payload 变量",
  );

  for (let node = calls[0].parent; node; node = node.parent) {
    if (!ts.isBlock(node) && !ts.isSourceFile(node)) continue;
    for (const statement of node.statements) {
      if (!ts.isVariableStatement(statement)) continue;
      for (const declaration of statement.declarationList.declarations) {
        if (
          ts.isIdentifier(declaration.name)
          && declaration.name.text === argument.text
          && declaration.initializer
          && ts.isObjectLiteralExpression(declaration.initializer)
        ) {
          return declaration.initializer;
        }
      }
    }
  }
  assert.fail(`找不到 ${argument.text} 的对象字面量声明`);
}


test("问答请求体的 retrieval_effort 必须沿 page→policy→payload 强制自动模式默认档", () => {
  assert.equal(
    askPolicyProperty("advanced"),
    "isAdvanced(uiMode)",
    "页面必须把唯一 UI mode 判据直接交给 Ask owner",
  );
  const payload = askStreamPayloadObject();
  const property = payload.properties.find((prop) => (
    ts.isPropertyAssignment(prop) && prop.name.getText(askSession) === "retrieval_effort"
  ));
  assert.ok(property, "payload 顶层必须有 retrieval_effort 字段");
  const text = property.initializer.getText(askSession);
  assert.match(
    text,
    /currentPolicy\.advanced/,
    "retrieval_effort 必须读取 page 交来的 policy.advanced 分叉——自动模式下控件不渲染，"
      + `state 却可能残留高级模式下选过的档位，发请求必须强制回默认档：${text}`,
  );
  assert.match(text, /DEFAULT_ASK_RETRIEVAL_EFFORT/);
});


test("Report owner 在请求汇聚点按 policy.advanced 固定 auto_generate", () => {
  const createCalls = [];
  function visit(node) {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)
      && node.expression.text === "createReport") createCalls.push(node);
    ts.forEachChild(node, visit);
  }
  visit(reportWorkspace);
  assert.equal(createCalls.length, 1);
  const args = createCalls[0].arguments;
  assert.equal(args[5].getText(reportWorkspace), "!currentPolicy.advanced");
  const depth = initializerOf("depth", reportWorkspace);
  assert.match(depth.getText(reportWorkspace), /currentPolicy\.advanced/);
  assert.match(depth.getText(reportWorkspace), /REPORT_DEFAULT_DEPTH_INDEX/);
});

test("Ask owner 的 submitMode 必须在提交汇聚点按 policy.advanced 收敛 graph", () => {
  // codex R1 P2：把 graph 收回 reasoning 的 useEffect 在 render 之后才跑；用户从
  // 高级模式拨回自动后抢先提交，state 残留的 graph 仍会被发出去。与 scope/档位同一
  // 原则：被隐藏的控件由请求侧强制默认值。钉住 submitMode 初始化式的三个语义要素。
  assert.equal(askPolicyProperty("advanced"), "isAdvanced(uiMode)");
  const initializer = initializerOf("submitMode", askSession);
  const text = initializer.getText(askSession);
  assert.ok(text.includes("currentPolicy.advanced"), `submitMode 初始化式必须按 policy.advanced 分叉：${text}`);
  assert.ok(text.includes('"graph"'), `submitMode 初始化式必须识别 graph 残留：${text}`);
  assert.ok(text.includes('"reasoning"'), `submitMode 初始化式必须收敛到 reasoning：${text}`);

  // 移动变异防线：光有 submitMode 定义不够，executeAsk 的模式实参不许再直接用
  // mode（否则定义成了摆设）。全 hook 扫 executeAsk(...) 调用，第二实参不得是
  // 裸 mode 标识符。
  const offenders = [];
  function visitCalls(node) {
    if (
      ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === "executeAsk"
      && node.arguments.length >= 2
      && ts.isIdentifier(node.arguments[1])
      && node.arguments[1].text === "mode"
    ) {
      offenders.push(node.getText(askSession));
    }
    ts.forEachChild(node, visitCalls);
  }
  visitCalls(askSession);
  assert.deepEqual(offenders, [], "executeAsk 的模式实参不得绕过 submitMode 直接用 mode");
});
