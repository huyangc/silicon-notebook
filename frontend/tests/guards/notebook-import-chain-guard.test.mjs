// 导入上传的「一串」只能在一处结束(换标识)—— notebook-import-upload.ts 的 `retireChain`。
//
// 背景:同一类缺陷前后五轮被评审找到——某条路径(预检被拒、白名单拒绝、作业列表里「没看到」、
// 到了客户端自己定的时限……)把一串结束、换了上传标识,而上一次带这个标识的请求其实还缓存在
// 代理里、或还占着服务端的预留,下一次重传就多出一本。修法是把「结束一串」收成一个只认服务端
// 凭据的函数;这份守卫钉住:删除一串、生成标识、改写标识,都不许出现在别处。
//
// 第二件事(codex 第九轮):浏览器的进度 / `upload.load` 事件证明不了「没被接收」——用户停止时
// 最后的字节可能已经发出,事件还没来得及跑。所以进度回调只许改显示;「已停止上传,没有导入
// 任何内容」(`{ kind: "stopped" }`)只许出自两处:请求还没交出去(`!requestStarted`),或后台
// 对账隔开 `NONE_CONFIRM_MS` 两次读到服务端说 `none`。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import { assignmentsIn, callsIn, findFunction, parseModule, scopedCalls } from "../../test-support/semantic-source.mjs";

const store = await parseModule("notebook-import-upload.ts");
const calls = scopedCalls(store);

function scopesOf(target) {
  return calls.filter((call) => call.target === target).map((call) => call.scope);
}

test("删除一串只在 retireChain 里", () => {
  const scopes = scopesOf("chains.delete");
  assert.ok(scopes.length > 0, "找不到 chains.delete:守卫失效");
  assert.deepEqual([...new Set(scopes)], ["<module>.retireChain"]);
});

test("清空全部只在登出(reset)里", () => {
  assert.deepEqual([...new Set(scopesOf("chains.clear"))], ["<module>.reset"]);
});

test("生成标识、登记一串只在 chainFor 里(只为还没有一串的文件)", () => {
  assert.deepEqual([...new Set(scopesOf("newClientRequestId"))], ["<module>.chainFor"]);
  assert.deepEqual([...new Set(scopesOf("chains.set"))], ["<module>.chainFor"]);
});

test("没有任何地方改写一串的标识", () => {
  const rewrites = assignmentsIn(store).filter((assignment) => /\.uploadId$/.test(assignment.target));
  assert.deepEqual(rewrites, []);
});

test("retireChain 的每个调用都带着服务端凭据(job / server_none)", () => {
  const text = store.getText(store);
  const invocations = text.match(/retireChain\(file, \{ kind: "[a-z_]+"/g) ?? [];
  assert.ok(invocations.length > 0);
  const total = scopesOf("retireChain").length;
  assert.equal(invocations.length, calls.filter((call) => call.target === "retireChain")
    .reduce((sum, call) => sum + call.count, 0));
  assert.ok(total > 0);
  for (const invocation of invocations) {
    assert.match(invocation, /kind: "(job|server_none)"/);
  }
});

function collect(predicate) {
  const found = [];
  const visit = (node) => {
    if (predicate(node)) found.push(node);
    ts.forEachChild(node, visit);
  };
  visit(store);
  return found;
}

function ancestorsOf(node) {
  const list = [];
  for (let parent = node.parent; parent; parent = parent.parent) list.push(parent);
  return list;
}

/** `node` 在 `branch` 这棵子树里(按节点身份,不按源码位置)。 */
const inside = (node, branch) => node === branch || ancestorsOf(node).includes(branch);
const enclosingFunction = (node) => ancestorsOf(node).find(ts.isFunctionLike);
const textOf = (node) => node.getText(store).replace(/\s+/g, " ");
const uploadCalls = collect((node) => ts.isCallExpression(node) && node.expression.getText(store) === "uploadNotebookImport");

test("进度与「请求体已传完」回调只改显示:不记任何状态,喂不进「没有导入」的判定", () => {
  assert.equal(uploadCalls.length, 1, "找不到唯一的 uploadNotebookImport 调用:守卫失效");
  for (const name of ["onProgress", "onBodySent"]) {
    const callbacks = collect((node) => ts.isPropertyAssignment(node)
      && node.name.getText(store) === name
      && inside(node, uploadCalls[0]));
    assert.equal(callbacks.length, 1, `找不到 ${name}:守卫失效`);
    const callback = callbacks[0].initializer;
    assert.deepEqual(assignmentsIn(callback), [], `${name} 里不许记状态`);
    for (const call of callsIn(callback)) {
      assert.ok(["current", "percentOf", "publish"].includes(call), `${name} 里不许调用 ${call}`);
    }
  }
});

test("模块里没有 bodySent 之类的「请求体已传完」状态", () => {
  const names = collect((node) => ts.isIdentifier(node) && /bodySent/i.test(node.text))
    .filter((node) => !(node.text === "onBodySent" && ts.isPropertyAssignment(node.parent) && node.parent.name === node));
  assert.deepEqual(names.map(textOf), []);
});

test("「请求已交出去」只在发请求的那个函数里置为 true:同一个函数、不隔任何等待,别处不改", () => {
  const start = findFunction(store, "start");
  assert.deepEqual(
    assignmentsIn(start).filter((assignment) => assignment.target === "requestStarted"),
    [{ target: "requestStarted", operator: "=", value: "true" }],
  );
  const [assignment] = collect((node) => ts.isBinaryExpression(node) && node.left.getText(store) === "requestStarted");
  const sender = enclosingFunction(assignment);
  assert.ok(sender && ts.isVariableDeclaration(sender.parent) && sender.parent.name.getText(store) === "sendUpload",
    "置位要在 sendUpload 里");
  assert.ok(enclosingFunction(uploadCalls[0]) === sender, "上传请求也要在 sendUpload 里");
  assert.deepEqual(collect((node) => ts.isAwaitExpression(node) && inside(node, sender)).map(textOf), []);
  for (const call of callsIn(sender)) {
    assert.ok(["uploadNotebookImport", "current", "percentOf", "publish"].includes(call), `sendUpload 里不许调用 ${call}`);
  }
});

test("「没有导入任何内容」只出自两处:请求还没交出去,或服务端隔开两次说 none", () => {
  const delivered = collect((node) => ts.isPropertyAssignment(node)
    && node.name.getText(store) === "kind"
    && ts.isStringLiteral(node.initializer)
    && node.initializer.text === "stopped"
    && ts.isObjectLiteralExpression(node.parent)).map((node) => node.parent);
  const sources = delivered.map((node) => {
    const ancestors = ancestorsOf(node);
    if (ancestors.some((ancestor) => ts.isIfStatement(ancestor)
      && textOf(ancestor.expression) === "interrupted && !requestStarted"
      && inside(node, ancestor.thenStatement))) return "before_send";
    const confirmed = ancestors.some((ancestor) => ts.isIfStatement(ancestor)
      && /NONE_CONFIRM_MS/.test(textOf(ancestor.expression))
      && inside(node, ancestor.thenStatement));
    const flagged = ancestors.some((ancestor) => ts.isConditionalExpression(ancestor)
      && textOf(ancestor.condition) === "own.stopped"
      && inside(node, ancestor.whenTrue));
    if (confirmed && flagged) return "server_none_twice";
    return `别处:${textOf(ancestors.find(ts.isStatement) ?? node)}`;
  });
  assert.deepEqual(sources.sort(), ["before_send", "server_none_twice"]);
});
