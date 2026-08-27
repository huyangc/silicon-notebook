// 站外来源建议「导入」（ask.gap_consult，X9 PR-A T3）走的是**核心** URL 来源端点，
// 绝不能悄悄改道插件路由。
//
// 背景：`GapConsultHostPort` 是给插件的建议入口——插件只负责说"这个 URL 值得看"，
// 导入这个动作本身与插件无关，是一次普通的核心「添加来源」写入
// （`POST /notebooks/{id}/sources/url`，同粘贴链接框走的 `importUrlSources`）。
// `frontend/features/extension-sdk` 那份既有守卫（extension-plugin-package-guard /
// extension-ui-boundary）只扫描 `features/ext-*` 与 `features/agent-profile` 这两类
// 插件包，本组件与它的调用点都在 `frontend/app/` 下——不在那两份守卫的扫描面里，
// 一次把导入悄悄换成 `/api/extensions/*` 不会被它们抓到。这是按
// `docs/development.md` 通用变异验证规则在评审中发现并补上的缺口。
//
// 覆盖边界（如实说明，不声称全覆盖）：本守卫按 AST 认**调用名**与**字符串字面量**
// 两种形态——`fetch("/api/extensions/...")`、`someExtensionApi()` 这类都能抓到。
// 抓不到的：① 拼接出来的路径（`"/api/" + "extensions/" + id`，没有一段字面量整段
// 含 `/api/extensions`）；② 经一层间接函数转发（`importGapSuggestion` 调用一个自己
// 起的 helper，helper 内部再打插件路由——`callsIn` 只看直接调用名，不会跟进 helper
// 函数体）；③ 运行时反射/动态 import。这些形态的兜底是代码评审，不是这份测试。
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import {
  callsIn,
  findFunction,
  parseModule,
  stringLiterals,
} from "../../test-support/semantic-source.mjs";

test("page.tsx 的 importGapSuggestion 调用核心 importUrlSources，不触达插件路由", async () => {
  const page = await parseModule("page.tsx");
  const fn = findFunction(page, "importGapSuggestion");
  const calls = callsIn(fn);

  assert.ok(
    calls.includes("importUrlSources"),
    "importGapSuggestion 必须调用核心 source-api.ts::importUrlSources —— "
      + `实际调用集合：${JSON.stringify(calls)}`,
  );
  const extensionRouteCall = calls.find((call) => /extension/i.test(call));
  assert.equal(
    extensionRouteCall,
    undefined,
    `importGapSuggestion 不得调用任何插件路由相关函数，命中了：${extensionRouteCall}`,
  );

  // 调用名扫描只认得出「插件路由长得像一个函数调用」的形态——真打插件路由更常见
  // 的写法是裸 `fetch("/api/extensions/...")`，路径是字符串字面量而不是调用名。
  // 镜像组件半（下面第二条用例）已有的字符串字面量扫描，把它对准这个函数体。
  const literals = stringLiterals(fn);
  // 空转保护：先证明这份扫描面本身不是空的（importUrlSources 调用带了 URL 数组
  // 参数、notebookId 字段等，字面量列表理应非空）——否则下面「没命中」的断言测的
  // 是「什么都没扫到」而不是「扫到了、确认干净」，findFunction 解析失败时会静默
  // 全绿（同 long-task-button-guard 对「入口被改名/删除」的空转保护同一个判据）。
  assert.ok(
    literals.length > 0,
    "importGapSuggestion 函数体内没有扫到任何字符串字面量——函数可能被改名/清空，"
      + "导致下面的路由字符串检查是一次空转",
  );
  const offendingLiteral = literals.find((value) => value.includes("/api/extensions"));
  assert.equal(
    offendingLiteral,
    undefined,
    `importGapSuggestion 不得拼接任何插件路由字符串，命中了字面量：${offendingLiteral}`,
  );
});

test("answer-gap-suggestions.tsx 不含任何插件路由字符串，也不 import 插件端口", async () => {
  const module = await parseModule("answer-gap-suggestions.tsx");

  const literals = stringLiterals(module);
  // 空转保护同上一条：这个文件本身就有不少字符串字面量（className、按钮文案、
  // aria 属性…），先确认扫描面非空，再断言其中没有一条命中插件路由。
  assert.ok(
    literals.length > 0,
    "answer-gap-suggestions.tsx 没有扫到任何字符串字面量——parseModule 可能解析失败，"
      + "导致下面的路由字符串检查是一次空转",
  );
  const offendingLiteral = literals.find((value) => value.includes("/api/extensions"));
  assert.equal(
    offendingLiteral,
    undefined,
    `组件本身不该拼接插件路由，命中了字符串字面量：${offendingLiteral}`,
  );

  const importSpecifiers = [];
  function visit(node) {
    if (
      ts.isImportDeclaration(node)
      && node.moduleSpecifier
      && ts.isStringLiteral(node.moduleSpecifier)
    ) {
      importSpecifiers.push(node.moduleSpecifier.text);
    }
    ts.forEachChild(node, visit);
  }
  visit(module);

  // 空转保护同上：这个文件至少 import 了 react/lucide-react/workspace-model，
  // 先确认扫描面非空，再断言其中没有一条命中 API 客户端/插件端口。
  assert.ok(
    importSpecifiers.length > 0,
    "answer-gap-suggestions.tsx 没有扫到任何 import 声明——parseModule 可能解析失败，"
      + "导致下面的插件端口检查是一次空转",
  );

  // 组件只应该从外部拿到 onImport 这一个回调，绝不该自己 import 任何 API 客户端
  // 或插件端口——真正打网络请求的地方是调用方（page.tsx::importGapSuggestion），
  // 不是这个纯展示组件自己。
  const apiImport = importSpecifiers.find((specifier) => /api-client|extension-sdk\/api/.test(specifier));
  assert.equal(
    apiImport,
    undefined,
    `组件不该自己 import API 客户端/插件端口，命中了：${apiImport}`,
  );
});
