// 参考库检索范围的接线守卫。
//
// 起因是一次真机事故:用户在挂了 84 篇论文参考库的笔记本里勾定单篇文章提问,16 条
// 引用**全部**来自参考库 —— 勾选框当时只约束当前笔记本自己的来源,挂载的参考库无
// 条件全量参与。后端已经能收窄这一维,但这个特性的价值全部押在前端把它接对:少接
// 一个调用点、或「清空」漏掉参考库,就是同一个 bug 换个地方复发,而且**不会报错**,
// 只会安静地多搜一整个库。
//
// page.tsx 是 8000 行的工作区编排组件,没有(也不宜有)组件级测试,所以这里按语义
// 结构钉住那几处容易漏的接线。纯逻辑在 source-scope.test.mjs,回执渲染在
// answer-retrieval-scope.component.test.tsx。
//
// ⚠ 判据一律是**语义身份**(标签 / className / 祖先链 / 绑定源码),不碰源码偏移
// 与行号 —— 那是仓库明令禁止的形态(app/test/static-source-policy.test.mjs 是硬门),
// 而且按位置钉住会让「把控件往下挪三行」这种无害改动报红。元素前后关系用 JSX 的
// **遍历顺序**表达:它是文档顺序,与源码位置无关。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import {
  callSitesIn,
  jsxElements,
  parseModule,
  variableInitializersIn,
} from "../../test-support/semantic-source.mjs";


const page = await parseModule("page.tsx");
const askSession = await parseModule("use-ask-session.ts");
const reportWorkspace = await parseModule("use-report-workspace.ts");


/** 字面量 className;拼接/条件表达式一律返回 null(它们不是稳定身份)。 */
function staticClassName(opening, sourceFile) {
  for (const attribute of opening.attributes.properties) {
    if (!ts.isJsxAttribute(attribute)) continue;
    if (attribute.name.getText(sourceFile) !== "className") continue;
    const initializer = attribute.initializer;
    if (initializer && ts.isStringLiteral(initializer)) return initializer.text;
    return null;
  }
  return null;
}


/** 某个 JSX 开标签上 onClick 绑定的源码;没有绑定则返回 ""。 */
function dynamicOnClick(opening) {
  for (const attribute of opening.attributes.properties) {
    if (!ts.isJsxAttribute(attribute)) continue;
    if (attribute.name.getText(opening.getSourceFile()) !== "onClick") continue;
    const initializer = attribute.initializer;
    if (initializer && ts.isJsxExpression(initializer) && initializer.expression) {
      return initializer.expression.getText(opening.getSourceFile());
    }
  }
  return "";
}


/**
 * 该元素**正文**里的静态文字,用来按人话认出是哪一句提示。
 *
 * 既收裸 JsxText,也收直接表达式子节点里的字符串 / 模板字面量片段 —— 这里的提示语
 * 两种写法都有(`…{`文案${变量}文案`}` 与裸文字),只认一种会把另一种看成空串。
 */
function renderedText(element) {
  const sourceFile = element.getSourceFile();
  const parts = [];
  for (const child of element.children) {
    if (ts.isJsxText(child)) {
      parts.push(child.getText(sourceFile).replace(/\s+/g, " "));
      continue;
    }
    if (!ts.isJsxExpression(child) || !child.expression) continue;
    const visit = (node) => {
      if (
        ts.isStringLiteral(node)
        || ts.isNoSubstitutionTemplateLiteral(node)
        || ts.isTemplateHead(node)
        || ts.isTemplateMiddle(node)
        || ts.isTemplateTail(node)
      ) {
        parts.push(node.text);
      }
      ts.forEachChild(node, visit);
    };
    visit(child.expression);
  }
  return parts.join("").trim();
}


/** 该元素**正文**(直接 JSX 表达式子节点)里引用到的标识符名。 */
function renderedIdentifiers(element) {
  const names = [];
  for (const child of element.children) {
    if (!ts.isJsxExpression(child) || !child.expression) continue;
    const visit = (node) => {
      if (ts.isIdentifier(node)) names.push(node.text);
      ts.forEachChild(node, visit);
    };
    visit(child.expression);
  }
  return names;
}


/**
 * 按文档顺序列出所有 JSX 元素的 { tag, className, ancestors, renders }。
 * ancestors 是从外到内的祖先 className/tag,用来断言「谁装在谁里面」;
 * renders 是正文里引用的标识符,用来断言「这行字是谁算出来的」。
 */
function jsxTree(sourceFile) {
  const stack = [];
  const nodes = [];

  const describe = (opening) => ({
    tag: opening.tagName.getText(sourceFile),
    className: staticClassName(opening, sourceFile),
  });

  function visit(node) {
    if (ts.isJsxElement(node)) {
      const self = describe(node.openingElement);
      nodes.push({
        ...self,
        ancestors: [...stack],
        renders: renderedIdentifiers(node),
        text: renderedText(node),
        node,
      });
      stack.push(self);
      ts.forEachChild(node, visit);
      stack.pop();
      return;
    }
    if (ts.isJsxSelfClosingElement(node)) {
      nodes.push({
        ...describe(node), ancestors: [...stack], renders: [], text: "", node,
      });
    }
    ts.forEachChild(node, visit);
  }

  visit(sourceFile);
  return nodes;
}


const TREE = jsxTree(page);

const byClassName = (className) => TREE.filter((node) => node.className === className);

function only(className) {
  const found = byClassName(className);
  assert.equal(found.length, 1, `期望恰好一个 .${className},实际 ${found.length} 个`);
  return found[0];
}

const ancestorClassNames = (node) => node.ancestors.map((a) => a.className);
const ancestorTags = (node) => node.ancestors.map((a) => a.tag);


// 用户原话:搜索框放在最顶上「给人一个误解是能搜索到参考库中的内容」。它只查当前
// 笔记本,所以必须落在「本库来源」那一组里,排在参考库分组之后。
test("来源搜索框归入「本库来源」分组,排在参考库分组之后", () => {
  const LANDMARKS = [
    "source-scope-toolbar",
    "base-scope-list",
    "source-search",
    "source-list",
  ];
  const order = TREE
    .filter((node) => LANDMARKS.includes(node.className))
    .map((node) => node.className);
  assert.deepEqual(
    order,
    LANDMARKS,
    "顺序必须是 检索范围工具条 → 参考库 → 搜索框 → 本库来源列表;"
      + "搜索框排到参考库之前会让人以为能搜到参考库里的内容",
  );

  const search = only("source-search");
  assert.ok(
    ancestorClassNames(search).includes("scope-group"),
    "搜索框要装在「本库来源」分组里,不能又变成面板上一个无归属的浮块",
  );
});


// .source-list 靠 .sources-body 上的 flex:1 1 auto / min-height:0 / overflow:auto 拿到
// 剩余高度并自己滚动。把它塞进新分组的 wrapper 里就得把这套算术复制一遍,否则列表
// 不再滚动、整个面板被撑长。
test("本库来源列表没有被包进新分组", () => {
  const sourceList = only("source-list");
  const ancestors = ancestorClassNames(sourceList);
  assert.equal(
    ancestors.includes("scope-group"),
    false,
    ".source-list 不能被包进 .scope-group",
  );
  assert.ok(
    ancestors.includes("workspace-panel-body sources-body"),
    ".source-list 必须留在 .sources-body 内",
  );
});


// 只清本库来源 = 用户以为范围空了,参考库还在整份参与检索 = 本次事故的翻版。
//
// ⚠ 判据必须是「两维在**同一颗按钮**里配对」,不能只数「两种参考库动作各出现一次」。
// 后者对**对调**变异全绿:把两颗按钮的参考库调用互换,两种动作仍各出现一次,而
// 「清空」会把参考库恢复成全选 —— 用户点完以为范围空了,`selectedBaseNotebookCount`
// 却大于 0,`sourceScopeBlocked` 放行,84 篇论文的参考库整份参与。那就是这次事故本身。
const CLEARED_BASE = /setBaseScopeSelection\(\{ allSelected: false, ids: new Set\(\) \}\)/;
const CLEARED_SOURCE = /sourceLibrary\.clearSourceSelection\(\)/;

test("「全选」与「清空」两维成对,且成对发生在同一颗按钮里", () => {
  const toolbarButtons = jsxElements(page, "button").filter((element) => (
    String(element.bindings?.onClick ?? "").includes("sourceLibrary.selectAllSources()")
      || String(element.bindings?.onClick ?? "").includes("sourceLibrary.clearSourceSelection()")
  ));
  assert.equal(toolbarButtons.length, 2, "检索范围工具条应有「全选」「清空」两颗按钮");

  const handlers = toolbarButtons.map((element) => String(element.bindings.onClick));
  // 按**本库来源那一维做了什么**认出这两颗按钮 —— 它是按钮语义的锚,
  // 参考库那一维正是这里要验的被测项,不能反过来拿它认按钮。
  const selectAll = handlers.filter(
    (h) => h.includes("sourceLibrary.selectAllSources()"),
  );
  const clear = handlers.filter((h) => CLEARED_SOURCE.test(h));
  assert.equal(selectAll.length, 1, "应恰好一颗按钮把本库来源恢复成全选(「全选」)");
  assert.equal(clear.length, 1, "应恰好一颗按钮把本库来源清空(「清空」)");

  assert.ok(
    selectAll[0].includes("setBaseScopeSelection(defaultBaseScopeSelection())"),
    `「全选」必须在**同一个** handler 里把参考库也恢复成全选:${selectAll[0]}`,
  );
  assert.doesNotMatch(
    selectAll[0],
    CLEARED_BASE,
    `「全选」把参考库清空了 —— 两颗按钮的参考库动作接反:${selectAll[0]}`,
  );

  assert.match(
    clear[0],
    CLEARED_BASE,
    `「清空」必须在**同一个** handler 里把参考库也清空:${clear[0]}`,
  );
  assert.equal(
    clear[0].includes("defaultBaseScopeSelection()"),
    false,
    `「清空」把参考库恢复成全选了 —— 用户以为范围空了,参考库还在整份参与:${clear[0]}`,
  );
});


// 切换/退出笔记本时两维必须一起重置:参考库的选择状态挂在上一个笔记本的挂载集上,
// 留着它会把旧库 id 带进新笔记本的请求,或反过来悄悄沿用旧的排除项。
test("参考库选择与来源选择在同样多的地方被重置", () => {
  const text = page.getText(page);
  assert.match(text, /sourceLibrary\.commitNotebookSnapshot\([\s\S]*setBaseScopeSelection\(defaultBaseScopeSelection\(\)\)/);
  assert.match(text, /function showCollection\(\)[\s\S]*sourceLibrary\.beginTransition\(\)[\s\S]*setBaseScopeSelection\(defaultBaseScopeSelection\(\)\)/);
  const pairedButtons = jsxElements(page, "button").filter((element) => {
    const handler = String(element.bindings?.onClick ?? "");
    return handler.includes("sourceLibrary.selectAllSources()")
      && handler.includes("setBaseScopeSelection(defaultBaseScopeSelection())");
  });
  assert.equal(pairedButtons.length, 1, "全选按钮必须同时重置来源与参考库范围");
});


// 回答进行中还能改范围 = 这轮跑的范围和屏幕上显示的不是同一个。既有的几处禁用是
// 逐个手写的,新控件必须自己补上。
test("参考库勾选框在回答进行中被禁用", () => {
  const boxes = jsxElements(page, "input").filter((element) => (
    String(element.bindings?.["aria-label"] ?? "").includes("检索参考库")
  ));
  assert.equal(boxes.length, 1, "应恰好有一处参考库勾选框");
  assert.equal(boxes[0].bindings.disabled, "askInFlight");
});


// 三个发送点少接一个,那条路径就退回「参考库全量参与」—— 而且静默。
test("base_scope 送达问答预检、问答流式与深度报告创建三处", () => {
  const reportHookCalls = [];
  function findReportHookCalls(node) {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)
      && node.expression.text === "useReportWorkspace") reportHookCalls.push(node);
    ts.forEachChild(node, findReportHookCalls);
  }
  findReportHookCalls(page);
  assert.equal(reportHookCalls.length, 1, "期望恰好一处 useReportWorkspace 调用");
  const reportOptions = reportHookCalls[0].arguments[0];
  assert.ok(reportOptions && ts.isObjectLiteralExpression(reportOptions));
  const reportPolicyProperty = reportOptions.properties.find((property) => (
    ts.isPropertyAssignment(property) && property.name.getText(page) === "policy"
  ));
  assert.ok(reportPolicyProperty && ts.isPropertyAssignment(reportPolicyProperty)
    && ts.isObjectLiteralExpression(reportPolicyProperty.initializer));
  const reportPolicy = new Map(reportPolicyProperty.initializer.properties
    .filter(ts.isPropertyAssignment)
    .map((property) => [property.name.getText(page), property.initializer.getText(page)]));
  assert.equal(reportPolicy.get("sourceScope"), "currentSourceScope");
  assert.equal(reportPolicy.get("baseScope"), "currentBaseScope");
  const reportCalls = callSitesIn(reportWorkspace).filter((call) => call.target === "createReport");
  assert.equal(reportCalls.length, 1, "Report owner 应恰好调用一次 createReport");
  assert.ok(reportCalls[0].arguments.includes("sourceScope"));
  assert.ok(reportCalls[0].arguments.includes("baseScope"));

  // Ask 多了一层 owner，但不能因此把 page→hook 这一跳变成盲区。先从真正的
  // useAskSession 调用点取 policy，逐项确认它直接消费页面算出的两份有效范围。
  const hookCalls = [];
  function findHookCalls(node) {
    if (
      ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === "useAskSession"
    ) hookCalls.push(node);
    ts.forEachChild(node, findHookCalls);
  }
  findHookCalls(page);
  assert.equal(hookCalls.length, 1, `期望恰好一处 useAskSession 调用,实际 ${hookCalls.length}`);
  const options = hookCalls[0].arguments[0];
  assert.ok(options && ts.isObjectLiteralExpression(options), "useAskSession 必须接收具名 options 对象");
  const policyProperty = options.properties.find((property) => (
    ts.isPropertyAssignment(property) && property.name.getText(page) === "policy"
  ));
  assert.ok(
    policyProperty
      && ts.isPropertyAssignment(policyProperty)
      && ts.isObjectLiteralExpression(policyProperty.initializer),
    "useAskSession options 必须包含 policy 对象",
  );
  const policy = new Map(policyProperty.initializer.properties
    .filter(ts.isPropertyAssignment)
    .map((property) => [property.name.getText(page), property.initializer.getText(page)]));
  assert.equal(policy.get("sourceScope"), "currentSourceScope");
  assert.equal(policy.get("baseScope"), "currentBaseScope");

  // 再钉 hook→API：预检读取本次 submit 冻结的同一份 snapshot，而不是重新读 policy。
  const preview = callSitesIn(askSession).filter((call) => call.target === "previewAskIntent");
  assert.equal(preview.length, 1, "Ask owner 应恰好调用一次 previewAskIntent");
  assert.ok(preview[0].arguments.includes("scopeSnapshot.sourceScope"));
  assert.ok(preview[0].arguments.includes("scopeSnapshot.baseScope"));

  // ⚠ 这里必须解析出 runAskStream **自己那个** payload 对象、只看它的**顶层**属性。
  // 全文件数一遍名为 base_scope 的属性对**移动**变异全绿:把它挪进
  // `...(intent ? { intent, base_scope: currentBaseScope } : {})`,属性还在、还是那个
  // 值,但非 reasoning 模式(intent 为 undefined)下整个字段消失,参考库静默恢复全量参与。
  const payload = askStreamPayloadObject();
  const properties = new Map();
  for (const property of payload.properties) {
    if (ts.isPropertyAssignment(property)) {
      properties.set(property.name.getText(askSession), property.initializer.getText(askSession));
    } else if (ts.isShorthandPropertyAssignment(property)) {
      properties.set(property.name.getText(askSession), property.name.getText(askSession));
    }
  }
  assert.equal(
    properties.get("base_scope"),
    "scopeSnapshot.baseScope",
    "/ask/stream 的 payload **顶层**必须带 base_scope: scopeSnapshot.baseScope;"
      + "藏进条件展开等于在非 reasoning 模式下整个丢掉它",
  );
  assert.equal(
    properties.get("source_scope"),
    "scopeSnapshot.sourceScope",
    "/ask/stream 的 payload **顶层**必须带 source_scope: scopeSnapshot.sourceScope",
  );
});


/**
 * `runAskStream` 第二个实参所指向的那个对象字面量。
 *
 * 刻意从**调用点**出发反查声明,而不是按名字在全文件里找一个叫 payload 的变量:
 * page.tsx 有 8000 行,`payload` 是最不具身份的名字之一。
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
  assert.equal(calls.length, 1, `期望恰好一处 runAskStream 调用,实际 ${calls.length}`);

  const argument = calls[0].arguments[1];
  assert.ok(
    argument && ts.isIdentifier(argument),
    "runAskStream 的第二个实参应是一个具名 payload 变量",
  );

  // 从调用点沿祖先链向外找声明它的那个作用域 —— 同名变量在别处的声明够不着。
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


/**
 * `sourceScopeBlocked` 的初始化表达式**节点**(剥掉外层括号)。
 *
 * 走 AST 而不是文本,是因为这道门要钉的是**结构**:顶层的合取、本地那一半由谁回答。
 * 文本匹配对「把某个子表达式提到顶层」这类移动变异是全绿的 —— 字样都还在。
 */
function sourceScopeBlockedInitializer() {
  const found = [];
  function visit(node) {
    if (
      ts.isVariableDeclaration(node)
      && ts.isIdentifier(node.name)
      && node.name.text === "sourceScopeBlocked"
      && node.initializer
    ) {
      found.push(node.initializer);
    }
    ts.forEachChild(node, visit);
  }
  visit(page);
  assert.equal(found.length, 1, "找不到唯一的 sourceScopeBlocked 定义");
  let expression = found[0];
  while (ts.isParenthesizedExpression(expression)) expression = expression.expression;
  return expression;
}


// 「本地为空但挂了参考库」过去被当成恒真的兜底放行。现在两维都能被收窄,把参考库
// 当成永远有货就是放行一次空检索。
//
// 另一半同样致命且方向相反:本地那一维过去按**勾了几个可见来源**判空。只有 Knowhow
// 表(或只有已确认 Memory)的笔记本可见来源恒为 0,于是它的问答输入框和新建报告被这
// 道门整个锁死,而那些格子照常可搜。所以本地那一半必须交给 localScopeIsEmpty ——
// 它同时看后端算好的本地证据信号,是后端 has_local 的镜像。
test("检索范围为空的判据是两维同时为空,本地那维按证据宇宙判", () => {
  const expression = sourceScopeBlockedInitializer();
  assert.ok(
    ts.isBinaryExpression(expression)
      && expression.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken,
    "顶层必须是「两维同时为空」的 && —— 换成 || 就是任一维为空即禁用",
  );

  const base = expression.right.getText(page);
  assert.match(
    base,
    /selectedBaseNotebookCount === 0/,
    `右操作数必须是参考库那一维的空判:${base}`,
  );

  let local = expression.left;
  while (ts.isParenthesizedExpression(local)) local = local.expression;
  assert.ok(
    ts.isCallExpression(local)
      && local.expression.getText(page) === "localScopeIsEmpty",
    "本地那一维必须整个交给共用纯函数 localScopeIsEmpty(后端 has_local 的镜像);"
      + "把它拆开或提到顶层,零可见来源的 Knowhow 库就会被判成空范围",
  );
  assert.deepEqual(
    local.arguments.map((argument) => argument.getText(page)),
    ["selectedLocalSourceCount", "notebookSourceTotal", "hasLocalEvidence(currentNotebook)"],
    "三个实参必须是「勾选数 / 可见来源总数 / 后端本地证据信号」;"
      + "把参考库那一维的计数喂进来就是把两维混成一维",
  );

  const whole = expression.getText(page);
  assert.doesNotMatch(
    whole,
    /hasMountedBase/,
    "不能再拿「挂没挂参考库」当判据 —— 挂着但全被取消勾选时范围同样是空的",
  );
});


// 这句话显示两处(来源工具条 + 问答输入框上方)。过去各写各的字面量,只改一处就会
// 不一致;收敛成一个纯函数后,把任一处改回手写计数都要在这里报红。
test("两处「检索范围」计数共用同一份文案", () => {
  const counters = byClassName("retrieval-scope-count");
  assert.equal(counters.length, 2, "计数应恰好显示两处(来源工具条 + 问答输入框上方)");
  assert.ok(
    ancestorClassNames(counters[0]).includes("source-scope-toolbar"),
    "来源页签的检索范围工具条必须显示计数",
  );
  assert.ok(
    ancestorTags(counters[1]).includes("AskComposer"),
    "问答输入框上方也必须显示计数(过去这里是另一份手写的单段计数)",
  );
  // 刻意断言「正文由谁算出来」,不数 retrievalScopeText 的出现次数:那个计数会被
  // title 之类的额外引用带偏,而这里要钉的是「两处显示的是同一个值」。
  for (const counter of counters) {
    assert.ok(
      counter.renders.includes("retrievalScopeText"),
      `这处计数没有渲染共用文案,而是自己算了一份:${counter.renders.join(", ")}`,
    );
  }

  const summaryCalls = callSitesIn(page)
    .filter((call) => call.target === "retrievalScopeSummary");
  assert.equal(
    summaryCalls.length,
    1,
    "page.tsx 里只应调用一次 retrievalScopeSummary —— 两处显示共用它的结果",
  );
});


test("零挂载库时也提交参考库范围快照，不省略这一维", () => {
  // codex #438 R1:省略这一维等于不冻结它。「创建时零个库」本身就是需要冻结的事实——
  // 报告范围在创建时定格、跨确认与生成复用,中途挂上的库会静默加入一份早已创建的报告;
  // Ask 的 job 也脱离连接后台跑完,提交与实际检索之间同样有窗口。
  //
  // 所以 currentBaseScope 的 initializer 必须是**直接**的 baseScopePayload 调用,
  // 不能是「挂了才发」的条件表达式。
  const entry = variableInitializersIn(page)
    .find((item) => item.name === "currentBaseScope");
  assert.ok(entry, "page.tsx 必须有 currentBaseScope");
  assert.match(
    entry.initializer,
    /^baseScopePayload\(/,
    "currentBaseScope 必须**直接**由 baseScopePayload(...) 算出",
  );
  assert.doesNotMatch(
    entry.initializer,
    /hasMountedBase|undefined/,
    "currentBaseScope 不得写成「挂了才发」的条件表达式:省略这一维等于不冻结它,"
      + "创建之后新挂的库会静默加入一份已冻结的报告/问答",
  );
});


/**
 * 某个变量声明的初始化表达式**节点**(剥掉外层括号),要求全文件恰好一处。
 *
 * 走 AST 而不是文本,是因为要钉的是**结构**:这个值由谁算出来。文本匹配对「把调用挪到
 * 旁边一个新变量、原表达式恢复」这类移动变异是全绿的 —— 标识符都还在文件里。
 */
function initializerOf(name) {
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
  visit(page);
  assert.equal(found.length, 1, `找不到唯一的 ${name} 定义(实际 ${found.length} 处)`);
  let expression = found[0];
  while (ts.isParenthesizedExpression(expression)) expression = expression.expression;
  return expression;
}


// 本笔记本自己没有图谱、用户又取消勾选了**唯一带图**的那个参考库时,聚合的
// `base_kg_available` 仍为真 —— 界面会放行「深入分析 / 知识图谱」两个这轮根本取不到
// 图谱的模式,而后端的可用性闸早已按库维度收窄、会如实答「无图」(codex #438 R2)。
// 判据因此必须落在「勾选集 ∩ 带图库」上,并且只有 kgAvailableForScope 一处实现。
test("严格推理的可用性由共用纯函数按**勾选集**算,不读聚合的 base_kg_available", () => {
  const expression = initializerOf("kgAvailable");
  assert.ok(
    ts.isCallExpression(expression)
      && expression.expression.getText(page) === "kgAvailableForScope",
    "kgAvailable 必须**直接**由共用纯函数 kgAvailableForScope(...) 算出;"
      + `在 page.tsx 里另写一份判据就会与后端的可用性闸分叉:${expression.getText(page)}`,
  );
  assert.deepEqual(
    expression.arguments.map((argument) => argument.getText(page)),
    ["currentNotebook", "selectedBaseNotebookIds"],
    "两个实参必须是「当前笔记本 / 本次勾选的参考库 id」;"
      + "喂 mountedBaseIds(全部挂载库)等于把取消勾选这件事整个抹掉",
  );
});


// 过去这句话 join 的是**全部挂载库名**:它会当着用户的面点名一个这次不参与检索、
// 或者压根没建过图的库。
test("「将借用参考库」只点名本轮参与且带图的那几个", () => {
  const hints = TREE.filter((node) => (
    node.className === "chat-hint" && node.text.includes("将借用参考库")
  ));
  assert.equal(hints.length, 1, `期望恰好一处借用提示,实际 ${hints.length} 处`);

  assert.ok(
    hints[0].renders.includes("borrowedBaseNames"),
    "这句提示必须渲染按勾选集算出的库名,而不是自己再取一遍挂载列表:"
      + hints[0].renders.join(", "),
  );
  assert.equal(
    hints[0].renders.includes("base_notebooks"),
    false,
    "不能再 join 全部挂载库名 —— 取消勾选的库、没建图的库都会被说成「借用」",
  );

  const source = initializerOf("borrowedBaseNames").getText(page);
  assert.match(
    source,
    /borrowedKgBaseNames\(\s*currentNotebook,\s*selectedBaseNotebookIds\s*\)/,
    `borrowedBaseNames 必须由共用纯函数按勾选集算出:${source}`,
  );
});


// 上面那道门开始按勾选集拦人之后,「取不到图谱」有了两种成因,出路差着真金白银:挂了
// 带图的库、只是这次没勾 → 把勾点回来即可;一个带图的库都没挂 → 才真需要为本笔记本
// 跑一次整库的图谱整理。混在一起就会在只需点回一个复选框时劝用户去花那笔钱。
test("「取不到图谱」的两种成因分开提示,只有该建图那一支给整理按钮", () => {
  const source = initializerOf("kgBlockedByScope").getText(page);
  assert.match(
    source,
    /kgBlockedByBaseScope\(\s*currentNotebook,\s*selectedBaseNotebookIds\s*\)/,
    `成因判定必须由共用纯函数按勾选集算出:${source}`,
  );

  const hints = byClassName("mode-hint");
  assert.equal(hints.length, 2, `严格推理提示应有两支,实际 ${hints.length} 支`);

  // 判据是「这支提示里到底有没有那颗按钮」——按后代节点判,不按 renders 数标识符:
  // onClick 挂在按钮的属性上,不是提示正文的直接表达式子节点。
  const startsKgBuild = (hint) => {
    let found = false;
    const visit = (child) => {
      if (
        (ts.isJsxOpeningElement(child) || ts.isJsxSelfClosingElement(child))
        && String(dynamicOnClick(child)).includes("startKgBuild")
      ) found = true;
      ts.forEachChild(child, visit);
    };
    visit(hint.node);
    return found;
  };

  const withBuildButton = hints.filter(startsKgBuild);
  assert.equal(
    withBuildButton.length,
    1,
    "「整理知识图谱」按钮只能出现在**一支**里 —— 另一支的出路是重新勾选参考库,"
      + "给按钮就是劝用户为一个复选框跑一次整库整理",
  );
  assert.match(
    withBuildButton[0].text,
    /该笔记本尚无知识图谱/,
    `带整理按钮的那支必须是「本笔记本没图」那一支:${withBuildButton[0].text}`,
  );

  const scopeHint = hints.find((hint) => !startsKgBuild(hint));
  assert.match(
    scopeHint.text,
    /没勾选/,
    `另一支必须说清成因是「没勾选」:${scopeHint.text}`,
  );
});
