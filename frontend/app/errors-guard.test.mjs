// 错误人话层的防复发守卫。
//
// 「全改人话」很容易变成假绿:errors.ts 的单测全过,但某个独立 API client 根本
// 没接进来,照样把 `403 {"detail":"notebook owner required"}` 直接甩给用户。
// 这里扫全量前端源码,从形态上禁掉两类泄漏,并正面钉住已迁移的调用点:
//
//   ①「裸抛状态码」——`new Error(`${res.status} ...`)`(第一轮评审)
//   ②「直出 err.message」——catch 分支把原始异常文本写进用户可见位置
//      (第二轮评审阻塞 1)。守卫①抓不到这类:`setStatusText(`服务异常:
//      ${err.message}`)` 里没有任何状态码,但 fetch 自身 reject 时用户看到的
//      就是「服务异常:Failed to fetch」——那条路径根本进不了
//      throwHumanizedHttpError。

import test from "node:test";
import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const APP_DIR = fileURLToPath(new URL("./", import.meta.url));

async function sourceFiles() {
  const entries = await readdir(APP_DIR, { recursive: true, withFileTypes: true });
  return entries
    .filter((e) => e.isFile())
    .map((e) => path.relative(APP_DIR, path.join(e.parentPath ?? e.path, e.name)))
    .filter((p) => /\.tsx?$/.test(p) && !p.endsWith(".d.ts"))
    .sort();
}

const FILES = await sourceFiles();

async function read(relPath) {
  return readFile(path.join(APP_DIR, relPath), "utf8");
}

// 已知例外:必须逐行精确登记,新增的裸抛照样会被抓。
// 共同点是「这个 Error 根本走不到用户面前」——被自己的 catch 吞掉用作内部
// 控制流。它们要的是控制流,不是文案。
const ALLOWED_BARE_THROWS = new Map([
  [
    // SSE 重连:Error 被同一个 try 的 catch 吞掉用于退避重连,不进 UI。
    // 也不能改走 throwHumanizedHttpError——那会读掉(消费)流式响应的 body。
    "pending-center.tsx",
    ["if (!resp.ok || !resp.body) throw new Error(`stream ${resp.status}`);"],
  ],
  [
    // 带鉴权的图片加载:catch 只把这张图切成 failed 占位,不展示 message。
    "knowhow-cell-editor.tsx",
    ["if (!res.ok) throw new Error(String(res.status));"],
  ],
  [
    // 同上(source detail 里的 <AuthedImage>):失败只渲染「图片加载失败」占位。
    "page.tsx",
    [".then((r) => (r.ok ? r.blob() : Promise.reject(new Error(String(r.status)))))"],
  ],
]);

test("没有任何地方把 HTTP 状态码裸抛给用户", async () => {
  const offenders = [];
  for (const rel of FILES) {
    const text = await read(rel);
    const allowed = ALLOWED_BARE_THROWS.get(rel) ?? [];
    text.split("\n").forEach((line, i) => {
      const trimmed = line.trim();
      // 形态:`new Error(...)` 的实参里出现 `.status`——模板串
      // (`${res.status} ${await res.text()}`)和 String(res.status) 都算。
      // 只看 `new Error(` 之后的部分,免得把 `if (res.status === 403) throw
      // new Error("forbidden")` 这种「条件里有 status」的哨兵误伤。
      if (trimmed.startsWith("//") || trimmed.startsWith("*")) return; // 注释里可以引用这个形态
      const at = trimmed.indexOf("new Error(");
      if (at < 0) return;
      if (!trimmed.slice(at + "new Error(".length).includes(".status")) return;
      if (allowed.includes(trimmed)) return;
      offenders.push(`${rel}:${i + 1}  ${trimmed}`);
    });
  }
  assert.deepEqual(
    offenders,
    [],
    "这些地方把状态码/后端原文直接抛给了用户,改用 errors.ts 的 " +
      "throwHumanizedHttpError(res, tag):\n" + offenders.join("\n")
  );
});

// 守卫②:`.message` 白名单。
//
// 用「禁形态」写这条守卫是拦不住的——泄漏的写法太多(`err.message`、
// `${e.message}`、`cause instanceof Error ? cause.message : String(cause)`、
// 包一层 helper 再 return……)。反过来做就严密了:**任何** `.message` 读取都
// 必须在这里逐行登记。登记的都是「这不是原始异常文本」的场景。
// errors.ts 自己是人话层的实现,整体豁免。
const ALLOWED_MESSAGE_READS = new Map([
  [
    "answer-panel.tsx",
    [
      // 后端 AskResponse.model_errors[] 的结构化字段(PR#61),不是 JS 异常;
      // 内容是模型服务名 + 原因,刻意展示在 title 里供排查。
      '<div className="answer-model-error" title={answer.model_errors[0]?.message ?? ""}>',
    ],
  ],
  [
    "admin/usage/page.tsx",
    [
      // 哨兵比对(不是展示):403 → 专用无权限视图。
      "if (e instanceof Error && e.message === FORBIDDEN_SENTINEL) {",
      // 展示的是 state.message,而它只由 toUserMessage() 写入(见同文件 catch)。
      'return <main className="usage-page usage-empty">加载失败:{state.message}</main>;',
    ],
  ],
  [
    "knowhow-cell-editor.tsx",
    [
      // 组件自己的状态字段,由 extractErrorMessage()(= toUserMessage 别名)写入。
      '{optimizeState.status === "error" && <p className="kh-inline-error">{optimizeState.message}</p>}',
    ],
  ],
  [
    "page.tsx",
    [
      // 应用自己写的提示弹窗文案,与异常无关。
      "<p>{infoModal.message}</p>",
    ],
  ],
]);

test("没有任何地方把原始异常文本直出给用户", async () => {
  const offenders = [];
  for (const rel of FILES) {
    if (rel === "errors.ts") continue; // 人话层自己的实现
    const text = await read(rel);
    const allowed = ALLOWED_MESSAGE_READS.get(rel) ?? [];
    text.split("\n").forEach((line, i) => {
      const trimmed = line.trim();
      if (trimmed.startsWith("//") || trimmed.startsWith("*")) return; // 注释里可以谈这个形态
      if (!/\.message\b/.test(trimmed)) return; // 注意 `.messages`(复数)不算
      if (allowed.includes(trimmed)) return;
      offenders.push(`${rel}:${i + 1}  ${trimmed}`);
    });
  }
  assert.deepEqual(
    offenders,
    [],
    "这些地方读了原始异常文本。catch 分支一律走 errors.ts 的 toUserMessage(error, 兜底):\n" +
      offenders.join("\n") +
      "\n(确实不是异常文本的,加进 ALLOWED_MESSAGE_READS 并写清楚理由)"
  );
});

test("catch 侧的关键出口确实走了人话层", async () => {
  // 反面守卫抓形态,这里正面钉住几个「一改就全网泄漏」的枢纽。
  const page = await read("page.tsx");
  // 全工作区 90+ 个 .catch(reportError) 都汇到这一个函数。
  assert.match(
    page,
    /function reportError\(error: unknown\) \{\s*\n\s*setStatusText\(toUserMessage\(error, "[^"]+"\)\);/,
    "page.tsx 的 reportError 必须过 toUserMessage"
  );
  // ask 流的 error 事件 / 重连轮询的 job error:后端英文技术串。
  assert.ok(
    page.includes('throw new Error(toUserMessage(new Error(event.error), "回答没能完成，请重试"));'),
    "ask 流的 error 事件必须过 toUserMessage"
  );
  assert.match(page, /toUserMessage\(d\.error \? new Error\(d\.error\) : null,/, "job.error 必须过 toUserMessage");

  // knowhow 面板 ~20 个调用点共用的入口,必须就是 toUserMessage。
  const knowhow = await read("knowhow-import-logic.ts");
  assert.match(
    knowhow,
    /export function extractErrorMessage\([^)]*\): string \{\s*\n\s*return toUserMessage\(err, fallback\);\s*\n\}/,
    "extractErrorMessage 必须只是 toUserMessage 的别名,不能再长出第二套规则"
  );
});

test("每个独立 API client 都接进了错误人话层", async () => {
  // 正面钉住:这些文件各有自己的 fetch 封装,是最容易漏掉的一类。
  const clients = [
    "auth.ts",
    "notebook-share.ts",
    "notebook-tier.ts",
    "promotion-queue.ts",
    "edge-review-queue.ts",
    "knowhow-model.ts",
    "knowhow-panel.tsx",
    "model-settings.ts",
    "memory-panel.tsx",
    "page.tsx",
    "admin/usage/api.ts",
    "admin/usage/notebooks.ts",
    "dev/logs/api.ts",
  ];
  for (const rel of clients) {
    const text = await read(rel);
    assert.ok(
      /from "[./]*errors(\.ts)?"/.test(text),
      `${rel} 有自己的 fetch 封装,必须 import 错误人话层`
    );
    assert.ok(
      text.includes("throwHumanizedHttpError(") || text.includes("readHttpError("),
      `${rel} 应当用 throwHumanizedHttpError()/readHttpError() 处理失败响应`
    );
  }
});

test("model-settings 的诊断带上 detail 和 requestId(不是裸 HTTP 500)", async () => {
  const text = await read("model-settings.ts");
  // 旧写法只有 `console.error(\`[model-settings] HTTP ${res.status}\`)`,
  // 模型服务测试失败时定位不到供应商到底报了什么。
  assert.equal(text.includes("] HTTP ${res.status}"), false);
  assert.equal((text.match(/throwHumanizedHttpError\(res, "model-settings"\)/g) ?? []).length, 3);
});

test("报告面板不把已翻译的错误压平成一句通用文案", async () => {
  const text = await read("report-view.tsx");
  // 无条件 setToast(通用文案) 会把 401/403/404/409 压成同一句,
  // 用户分不清「登录失效 / 没权限 / 已删除 / 冲突」,还会反复重试。
  assert.equal(text.includes('setToast("报告操作没成功，请稍后重试")'), false);
  assert.equal(text.includes('setToast("报告没能生成完，可以重试")'), false);
  assert.ok(text.includes('toUserMessage(error, "报告操作没成功，请稍后重试")'));
  assert.ok(text.includes('toUserMessage(error, "报告没能生成完，可以重试")'));
});
