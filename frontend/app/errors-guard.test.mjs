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
      // 取出来只为进 console(见同文件 logDiagnostic 的 useEffect),不上屏。
      // 横幅本身保留(PR#61 的可观测性),但原文不再进 hover title。
      "const modelErrorDetail = answer.model_errors?.[0]?.message ?? null;",
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

// 守卫③:后端**诊断字段**的白名单(第三轮评审阻塞 2)。
//
// 守卫②只扫 `.message`,所以后端塞在 `.error` / `.error_message` 里的原始
// 异常串整类溜了过去——「6/6 通过」是假绿。这些字段后端统一写成
// `f"{type(exc).__name__}: {exc}"`(grep 到 24 处),和 JS 异常一样不能直出。
//
// `console.error` 不算(那正是原文该去的地方);`.errors`(复数)、
// `.error_count` 之类靠 \b 排除。
const DIAGNOSTIC_FIELD_RE = /\.(error|error_message)\b/;

const ALLOWED_DIAGNOSTIC_READS = new Map([
  [
    "dev/logs/components/LogDetail.tsx",
    [
      // 开发者日志查看器:整个页面的存在意义就是显示原始日志记录,
      // 面向的是 admin/开发者而不是终端用户(路由在 /dev/logs 且 owner 门控)。
      "{record.error ? (",
      '<strong>error{record.attempt != null ? `（attempt ${record.attempt}）` : ""}:</strong> {record.error}',
    ],
  ],
  [
    "dev/logs/components/StatsBar.tsx",
    [
      // 是个计数(number),不是错误文本。
      '{chip("error", stats.by_status.error ?? 0)}',
    ],
  ],
  [
    "page.tsx",
    [
      // 组装快照对象,不是展示;原文在同函数末尾统一进 logDiagnostic。
      "error: body?.error ?? null,",
      "if (snapshot.error) logDiagnostic(\"ready\", snapshot.error);",
      // 模型「测试连接」:200 响应挂不上 X-User-Message 头,出处改由 schema 的
      // code 字段承载(上屏的是 vocabulary.ts 里该 code 的文案);r.error 只进 console。
      'if (!r.ok && r.error) logDiagnostic("model-test", r.error);',
      // ask 流的 error 事件:原文只进受限诊断出口,上抛的是带品牌的场景文案。
      'logDiagnostic("ask-stream", event.error);',
      // 以下三处都已过人话层(裸值包进 Error 交给 toUserMessage)。
      '? `全部预审中止：${toUserMessage(job.error ? new Error(job.error) : null, "出了点问题")}（已处理 ${job.done}）`',
      ': toUserMessage(d.error ? new Error(d.error) : null, "该问答失败，请稍后重试"));',
      ': `失败：${toUserMessage(r.error ? new Error(r.error) : null, "连接未通过")}`,',
      // 条件判断 + 已过人话层的两行(justFailed 那处)。
      "const failureHint = justFailed.error_message",
      '? toUserMessage(new Error(justFailed.error_message), "")',
      // 只用来二选一挑文案,不展示原文。
      "<p>{sourceDetail.error_message",
    ],
  ],
  [
    "report-view.tsx",
    [
      // 条件判断:失败态才渲染那条(文案是写死的中文,不含 active.error)。
      '{active.status === "failed" && active.error && (',
      // 取出来只为进 console(见同文件 logDiagnostic 的 useEffect)。
      'const activeError = active?.status === "failed" ? active.error : null;',
      "if (activeError) logDiagnostic(\"report\", activeError);",
    ],
  ],
]);

test("没有任何地方把后端诊断字段(.error/.error_message)直出给用户", async () => {
  const offenders = [];
  for (const rel of FILES) {
    if (rel === "errors.ts") continue; // 人话层自己的实现
    const text = await read(rel);
    const allowed = ALLOWED_DIAGNOSTIC_READS.get(rel) ?? [];
    text.split("\n").forEach((line, i) => {
      const trimmed = line.trim();
      if (trimmed.startsWith("//") || trimmed.startsWith("*")) return; // 注释里可以谈这个形态
      if (trimmed.startsWith("{/*")) return; // JSX 注释
      // console.error 是诊断通道本身,不是泄漏。
      if (!DIAGNOSTIC_FIELD_RE.test(trimmed.replaceAll("console.error", ""))) return;
      if (allowed.includes(trimmed)) return;
      offenders.push(`${rel}:${i + 1}  ${trimmed}`);
    });
  }
  assert.deepEqual(
    offenders,
    [],
    "这些地方读了后端的原始诊断字段。要么包进 Error 走 toUserMessage(…),要么" +
      "用 logDiagnostic() 只送 console:\n" +
      offenders.join("\n") +
      "\n(确实不是异常文本的,加进 ALLOWED_DIAGNOSTIC_READS 并写清楚理由)"
  );
});

// 守卫④:场景文案必须**带品牌**(第四轮评审阻塞 2)。
//
// 品牌(HUMANIZED)原来只在 humanizeHttpError 一个出口打,于是任何「重新包装」
// 都会掉品牌。page.tsx 的 ask 流就是这么中招的:
//
//   throw new Error(toUserMessage(new Error(event.error), "回答没能完成，请重试"))
//
// 抛出去的是**普通** Error,外层 runAsk → reportError → toUserMessage 认不出
// 它已经安全化,于是再泛化一次:
//   第一跳 → "回答没能完成，请重试"
//   第二跳 → "服务出了点问题，请稍后重试"   ← 场景文案被吃掉
// 而且第一句安全文案还会被当成「未翻译的原始错误」多记一条诊断。
//
// 两条形态一起禁,它们是同一个根因的两种长相:
//   (a) `new Error(toUserMessage(...) / humanize...(...))` —— 把已安全化的文案
//       重新包进裸 Error;
//   (b) `new Error("<含中文>")` —— 写给用户的场景文案,压根没盖过章。
// 两类都改用 errors.ts 的 humanizedError():它产出带品牌的 Error,能穿过外层
// catch 原样抵达用户。
const REWRAP_RE = /new Error\(\s*(toUserMessage|humanize|extractErrorMessage)/;
const CJK_LITERAL_THROW_RE = /new Error\(\s*["'`][^"'`]*[一-鿿]/;

// 目前为空:全仓扫下来没有「确实该裸抛中文」的场景。
// 控制流哨兵(admin 页的 FORBIDDEN_SENTINEL)天然不在射程内——它抛的是常量不是
// 中文字面量。真要加豁免,逐行登记并写清「为什么这句到不了用户面前」。
const ALLOWED_UNBRANDED_COPY = new Map([]);

test("给用户的场景文案必须带品牌(不重新包装、不裸抛中文)", async () => {
  const offenders = [];
  for (const rel of FILES) {
    if (rel === "errors.ts") continue; // 人话层自己的实现:品牌就是在这儿打的
    const text = await read(rel);
    const allowed = ALLOWED_UNBRANDED_COPY.get(rel) ?? [];
    text.split("\n").forEach((line, i) => {
      const trimmed = line.trim();
      if (trimmed.startsWith("//") || trimmed.startsWith("*")) return; // 注释里可以谈这个形态
      if (allowed.includes(trimmed)) return;
      if (REWRAP_RE.test(trimmed)) {
        offenders.push(`${rel}:${i + 1}  [重新包装掉品牌]  ${trimmed}`);
        return;
      }
      // DOMException 不算(page.tsx 的 "已中断回答" 是 AbortError 控制流)。
      if (CJK_LITERAL_THROW_RE.test(trimmed)) {
        offenders.push(`${rel}:${i + 1}  [场景文案没盖章]  ${trimmed}`);
      }
    });
  }
  assert.deepEqual(
    offenders,
    [],
    "这些地方产出的用户文案没带品牌,外层 catch 的 toUserMessage 会把它再泛化一次" +
      "(场景文案被吃成全局兜底)。改用 errors.ts 的 humanizedError(文案):\n" +
      offenders.join("\n")
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
  // ask 流的 error 事件:原文进受限诊断出口,抛出去的是**带品牌**的场景文案。
  // 裸 new Error 会掉品牌,外层 reportError 再泛化一次(第四轮评审阻塞 2)。
  assert.match(
    page,
    /logDiagnostic\("ask-stream", event\.error\);\s*\n\s*throw humanizedError\("回答没能完成，请重试"\);/,
    "ask 流的 error 事件必须 logDiagnostic 原文 + 抛 humanizedError 场景文案"
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
