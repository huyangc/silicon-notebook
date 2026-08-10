// 拖放接管守卫：来源文件的 <input type=file accept=…> 铺满其父 label，浏览器会把
// 拖进来的文件直接交给原生 input 并按 accept **静默**过滤不支持的类型——用户得不到
// 任何提示（正是「批量上传时不支持的文档无声消失」的根因）。所以每个来源文件 input
// 的**外层 label** 必须显式带 onDragOver + onDrop（preventDefault 后走
// stageIncomingFiles，让被跳过的文件进弹窗内的持久「已跳过」列表）。
//
// 判据是语义的（AST 上「绑定后端解析注册表派生的
// supportedSourceAccept 的 input 的外围 label 元素属性集」），不做文本/位置
// 查询——static-source-policy 禁止测试直接读生产源码。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import { parseModule } from "../../test-support/semantic-source.mjs";

function attributeNames(attributes, sourceFile) {
  const names = [];
  for (const attribute of attributes.properties) {
    if (ts.isJsxAttribute(attribute)) {
      names.push(attribute.name.getText(sourceFile));
    }
  }
  return names;
}

function bindsSourceAccept(node, sourceFile) {
  for (const attribute of node.attributes.properties) {
    if (!ts.isJsxAttribute(attribute)) continue;
    if (attribute.name.getText(sourceFile) !== "accept") continue;
    const initializer = attribute.initializer;
    return Boolean(
      initializer
      && ts.isJsxExpression(initializer)
      && initializer.expression
      && initializer.expression.getText(sourceFile) === "supportedSourceAccept",
    );
  }
  return false;
}

test("来源上传的每个 file input 外层 label 都显式接管拖放", async () => {
  const sourceFile = await parseModule("page.tsx");
  const inputs = [];
  const labelStack = [];

  function visit(node) {
    const isLabel = ts.isJsxElement(node)
      && node.openingElement.tagName.getText(sourceFile) === "label";
    if (isLabel) labelStack.push(node.openingElement);
    if (
      (ts.isJsxSelfClosingElement(node) || ts.isJsxOpeningElement(node))
      && node.tagName.getText(sourceFile) === "input"
      && bindsSourceAccept(node, sourceFile)
    ) {
      inputs.push({
        enclosingLabel: labelStack.length > 0 ? labelStack[labelStack.length - 1] : null,
      });
    }
    ts.forEachChild(node, visit);
    if (isLabel) labelStack.pop();
  }
  visit(sourceFile);

  assert.ok(
    inputs.length >= 2,
    `page.tsx 里应至少有 2 个绑定 supportedSourceAccept 的来源文件 input，实际 ${inputs.length}`,
  );
  for (const { enclosingLabel } of inputs) {
    assert.ok(enclosingLabel, "来源文件 input 应位于 <label> 内");
    const names = attributeNames(enclosingLabel.attributes, sourceFile);
    assert.ok(
      names.includes("onDragOver"),
      `来源文件 input 的外层 label 缺 onDragOver（原生 input 会按 accept 静默吞掉拖放）：${names.join(", ")}`,
    );
    assert.ok(
      names.includes("onDrop"),
      `来源文件 input 的外层 label 缺 onDrop（原生 input 会按 accept 静默吞掉拖放）：${names.join(", ")}`,
    );
  }
});
