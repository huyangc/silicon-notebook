import test from "node:test";
import assert from "node:assert/strict";

import {
  GENERIC_USER_ERROR,
  humanizeHttpError,
  readHttpError,
  throwHumanizedHttpError,
  toUserMessage,
} from "./errors.ts";
import { fetchMe, loginUser, registerUser } from "./auth.ts";
import { testModelService } from "./model-settings.ts";
import { setNotebookTier } from "./notebook-tier.ts";
import { shareNotebook } from "./notebook-share.ts";
import { fetchKnowhowTables } from "./knowhow-model.ts";
import { fetchAdminUsers } from "./admin/usage/api.ts";

// ---------------------------------------------------------------------------
// 纯函数:状态码 → 中文
// ---------------------------------------------------------------------------

test("按状态码映射成中文人话", () => {
  assert.equal(humanizeHttpError(401), "登录状态已失效，请重新登录");
  assert.equal(humanizeHttpError(403), "没有权限进行这个操作");
  assert.equal(humanizeHttpError(404), "没找到，可能已被删除");
  assert.equal(humanizeHttpError(409), "操作有冲突，请刷新后重试");
  assert.equal(humanizeHttpError(413), "文件太大");
  assert.equal(humanizeHttpError(422), "提交的内容有误");
});

test("5xx 一律归「服务暂时不可用」", () => {
  assert.equal(humanizeHttpError(500), "服务暂时不可用，请稍后再试");
  assert.equal(humanizeHttpError(502), "服务暂时不可用，请稍后再试");
  assert.equal(humanizeHttpError(503), "服务暂时不可用，请稍后再试");
});

test("未知状态码退兜底文案", () => {
  assert.equal(humanizeHttpError(400), "操作失败，请重试");
  assert.equal(humanizeHttpError(429), "操作失败，请重试");
  assert.equal(humanizeHttpError(0), "操作失败，请重试");
});

test("英文 detail 不进用户文案(只按状态码泛化)", () => {
  // 后端英文 detail 是给 MCP / 日志看的,泛化掉。
  assert.equal(humanizeHttpError(403, "admin only"), "没有权限进行这个操作");
  assert.equal(humanizeHttpError(403, "notebook owner required"), "没有权限进行这个操作");
  assert.equal(humanizeHttpError(500, "Internal Server Error"), "服务暂时不可用，请稍后再试");
  assert.equal(humanizeHttpError(422, "field required"), "提交的内容有误");
  assert.equal(humanizeHttpError(400, "invalid cell address"), "操作失败，请重试");
});

test("4xx 的中文 detail 是后端写给用户的文案 → 原样透传", () => {
  // 后端刻意写成中文的 detail 比按状态码泛化的话有用得多,不能压平。
  assert.equal(humanizeHttpError(400, "用户名已被占用"), "用户名已被占用");
  assert.equal(humanizeHttpError(400, "格子定位不合法"), "格子定位不合法");
  assert.equal(humanizeHttpError(403, "仅管理员可设置基准库"), "仅管理员可设置基准库");
  assert.equal(humanizeHttpError(403, "仅管理员可管理晋升队列"), "仅管理员可管理晋升队列");
});

test("5xx 的 detail 一律不透传(哪怕是中文,也可能是内部错误)", () => {
  assert.equal(humanizeHttpError(500, "数据库连接失败"), "服务暂时不可用，请稍后再试");
  assert.equal(humanizeHttpError(503, "后台迁移中"), "服务暂时不可用，请稍后再试");
});

test("空白 detail 不影响泛化", () => {
  assert.equal(humanizeHttpError(404, ""), "没找到，可能已被删除");
  assert.equal(humanizeHttpError(404, "   "), "没找到，可能已被删除");
  assert.equal(humanizeHttpError(404, undefined), "没找到，可能已被删除");
});

test("含中文的网关/HTML 正文不算「后端写给用户的文案」,不透传", () => {
  // 评审阻塞 2:此前判据只是「<500 且含一个汉字」,于是 403 的网关正文
  // `<html>访问被拒绝 — nginx request id=req-1</html>` 会整段显示给用户。
  assert.equal(
    humanizeHttpError(403, "<html>访问被拒绝 — nginx request id=req-1</html>"),
    "没有权限进行这个操作"
  );
  assert.equal(humanizeHttpError(404, "<p>页面不存在</p>"), "没找到，可能已被删除");
  // 多行 = 正文/堆栈,不是文案。
  assert.equal(humanizeHttpError(400, "参数不对\n  at handler (app.py:31)"), "操作失败，请重试");
  // JSON 花括号 = 结构化正文,不是文案。
  assert.equal(humanizeHttpError(400, '{"detail":"参数不对","request_id":"req-1"}'), "操作失败，请重试");
  // 超长 = 正文,不是文案(后端为用户写的都是短句)。
  assert.equal(humanizeHttpError(400, `很抱歉${"细节".repeat(200)}`), "操作失败，请重试");
});

// ---------------------------------------------------------------------------
// toUserMessage:catch 到的异常 → 用户文案
// ---------------------------------------------------------------------------

// 换掉 console.error 收集诊断行,顺带保持测试输出干净。
function captureConsole(fn) {
  const logs = [];
  const original = console.error;
  console.error = (...args) => logs.push(args.map(String).join(" "));
  return (async () => {
    try {
      return { value: await fn(), logs };
    } finally {
      console.error = original;
    }
  })();
}

// 同步版:toUserMessage 是纯函数,但兜底时会写 console.error。
function captureSync(fn) {
  const logs = [];
  const original = console.error;
  console.error = (...args) => logs.push(args.map(String).join(" "));
  try {
    return { value: fn(), logs };
  } finally {
    console.error = original;
  }
}

test("toUserMessage 保住 fetch 层已译好的语义,不压平", () => {
  // 401/403/404/409 各不相同——压成同一句用户就分不清该重试还是该换账号。
  const { value, logs } = captureSync(() => [
    toUserMessage(new Error("没有权限进行这个操作"), "兜底"),
    toUserMessage(new Error("没找到，可能已被删除"), "兜底"),
    toUserMessage(new Error("操作有冲突，请刷新后重试"), "兜底"),
  ]);
  assert.deepEqual(value, ["没有权限进行这个操作", "没找到，可能已被删除", "操作有冲突，请刷新后重试"]);
  // 透传路径无损,不该重复刷日志(HTTP 诊断 readHttpError 已经记过)。
  assert.deepEqual(logs, []);
});

test("toUserMessage 不把英文技术异常漏给用户", () => {
  const { value } = captureSync(() => [
    toUserMessage(new TypeError("Failed to fetch"), "兜底"),
    toUserMessage(new Error("Unexpected token < in JSON"), "兜底"),
    toUserMessage(new Error(""), "兜底"),
    toUserMessage("some string", "兜底"),
    toUserMessage(undefined, "兜底"),
  ]);
  assert.deepEqual(value, ["兜底", "兜底", "兜底", "兜底", "兜底"]);
});

test("toUserMessage 兜底时把原始值写进 console(排查不丢)", () => {
  // 阻塞 1 的核心:用户看人话,原文进 console——两边都不能丢。
  const { value, logs } = captureSync(() =>
    toUserMessage(new TypeError("Failed to fetch"), "服务出了点问题，请稍后重试")
  );
  assert.equal(value, "服务出了点问题，请稍后重试");
  assert.equal(logs.length, 1);
  assert.match(logs[0], /Failed to fetch/);
});

test("toUserMessage 不给用户看混着中文的技术正文", () => {
  const { value } = captureSync(() => [
    // 后端 job.error 里塞的堆栈/正文,含中文也不直出。
    toUserMessage(new Error('{"error":"抽取失败","trace":"..."}'), "兜底"),
    toUserMessage(new Error("<html>网关拒绝 nginx/1.25</html>"), "兜底"),
    toUserMessage(new Error("抽取失败\nTraceback (most recent call last):"), "兜底"),
  ]);
  assert.deepEqual(value, ["兜底", "兜底", "兜底"]);
});

test("toUserMessage 有默认兜底文案(调用方可以不传)", () => {
  const { value } = captureSync(() => toUserMessage(new TypeError("Failed to fetch")));
  assert.equal(value, GENERIC_USER_ERROR);
  assert.ok(!/[A-Za-z]/.test(value));
});

// ---------------------------------------------------------------------------
// readHttpError / throwHumanizedHttpError:诊断合同
// ---------------------------------------------------------------------------

const jsonResponse = (status, body, headers = {}) =>
  new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    statusText: status === 500 ? "Internal Server Error" : "",
    headers: { "Content-Type": "application/json", ...headers },
  });

test("readHttpError 抠得出 FastAPI 的几种结构化 detail 形状", async () => {
  const { value: a } = await captureConsole(() =>
    readHttpError(jsonResponse(403, { detail: "notebook owner required" }), "t")
  );
  assert.equal(a.userDetail, "notebook owner required");

  const { value: b } = await captureConsole(() =>
    readHttpError(jsonResponse(422, { detail: [{ msg: "field required" }, { msg: "too long" }] }), "t")
  );
  assert.equal(b.userDetail, "field required；too long");

  const { value: d } = await captureConsole(() =>
    readHttpError(jsonResponse(400, { message: "bad input" }), "t")
  );
  assert.equal(d.userDetail, "bad input");
});

test("非 JSON 原始正文进不了可展示通道(只进诊断)", async () => {
  // 评审阻塞 2:pickDetail 以前对非 JSON 正文 `return text`,原始正文成了
  // detail,再撞上「含汉字就透传」——整段网关 HTML 就上了屏。
  const gateway = "<html>访问被拒绝 — nginx request id=req-1</html>";
  const { value, logs } = await captureConsole(() =>
    readHttpError(jsonResponse(403, gateway), "t")
  );
  assert.equal(value.userDetail, "", "非 JSON 正文不得成为可展示文案");
  assert.match(logs[0], /nginx request id=req-1/, "但必须留在诊断里");

  // JSON 里没有 detail/message,或它不是字符串/字符串数组 → 同样不可展示。
  const { value: v2 } = await captureConsole(() =>
    readHttpError(jsonResponse(400, { error: "参数不对", request_id: "req-2" }), "t")
  );
  assert.equal(v2.userDetail, "");
  const { value: v3 } = await captureConsole(() =>
    readHttpError(jsonResponse(400, { detail: { code: 17, msg: "参数不对" } }), "t")
  );
  assert.equal(v3.userDetail, "");
});

test("诊断值统一截断(非 JSON 大正文也逃不掉)", async () => {
  // 评审阻塞 2:旧写法 `detail || raw.slice(0, 500)`,非 JSON 正文会先被
  // pickDetail 当成 detail 返回,于是走 `detail` 那一侧,截断被绕过。
  const huge = `<html>${"错误详情 ".repeat(2000)}</html>`;
  const { value, logs } = await captureConsole(() => readHttpError(jsonResponse(502, huge), "t"));
  assert.equal(value.userDetail, "");
  assert.ok(logs[0].length < 700, `诊断行应被截断,实际 ${logs[0].length} 字符`);
  assert.match(logs[0], /已截断/);

  // 结构化但超长的 detail 同样截断。
  const { logs: l2 } = await captureConsole(() =>
    readHttpError(jsonResponse(500, { detail: "x".repeat(4000) }), "t")
  );
  assert.ok(l2[0].length < 700, `诊断行应被截断,实际 ${l2[0].length} 字符`);
});

test("含中文的网关正文:用户拿中文兜底,原文只在 console", async () => {
  // 阻塞 2 的端到端断言(自验②)。
  const gateway = "<html>访问被拒绝 — nginx request id=req-1</html>";
  const { logs } = await captureConsole(async () => {
    await assert.rejects(
      throwHumanizedHttpError(jsonResponse(403, gateway, { "X-Request-Id": "req-1" }), "share"),
      (error) => {
        assert.equal(error.message, "没有权限进行这个操作");
        assert.ok(!error.message.includes("<"), "不得出现标签");
        assert.ok(!error.message.includes("nginx"), "不得出现上游名");
        assert.ok(!error.message.includes("req-1"), "不得出现 request id");
        return true;
      }
    );
  });
  assert.match(logs[0], /nginx/);
  assert.match(logs[0], /req-1/);
});

test("readHttpError 把 状态码 + detail + requestId 写进 console", async () => {
  const { value, logs } = await captureConsole(() =>
    readHttpError(
      jsonResponse(500, { detail: "upstream model timeout" }, { "X-Request-Id": "req-abc123" }),
      "model-settings"
    )
  );
  assert.equal(value.requestId, "req-abc123");
  assert.equal(logs.length, 1);
  assert.match(logs[0], /\[model-settings\]/);
  assert.match(logs[0], /500/);
  assert.match(logs[0], /upstream model timeout/);
  assert.match(logs[0], /req-abc123/);
});

test("throwHumanizedHttpError:诊断进 console,用户拿到中文", async () => {
  const { logs } = await captureConsole(async () => {
    await assert.rejects(
      throwHumanizedHttpError(jsonResponse(403, { detail: "notebook owner required" }), "share"),
      (error) => {
        assert.equal(error.message, "没有权限进行这个操作");
        return true;
      }
    );
  });
  assert.match(logs[0], /notebook owner required/);
});

// ---------------------------------------------------------------------------
// 调用端(mock fetch):证明真实回归被锁住
//
// 只测纯函数锁不住回归——「注册 400 中文 detail 被泛化」这种 bug 发生在
// authFetch / apiFetch 里,纯函数测试全绿也照样能再犯一次。
// ---------------------------------------------------------------------------

// 用 mock fetch 跑一个真实 client 调用,返回抛出的错误 + console 诊断行。
async function callFailing(fn, response) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => response;
  try {
    const { value, logs } = await captureConsole(async () => {
      try {
        await fn();
        return null;
      } catch (error) {
        return error;
      }
    });
    assert.ok(value instanceof Error, "调用端应当抛出 Error");
    return { error: value, logs };
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("注册 400 的中文 detail 原样保留(不被泛化成「操作失败」)", async () => {
  const { error } = await callFailing(
    () => registerUser("a00123456", "pw"),
    jsonResponse(400, { detail: "用户名已被占用" })
  );
  assert.equal(error.message, "用户名已被占用");

  const { error: e2 } = await callFailing(
    () => registerUser("bad", "pw"),
    jsonResponse(400, { detail: "用户名须为「单个小写字母+00+六位数字」，如 a00123456" })
  );
  assert.equal(e2.message, "用户名须为「单个小写字母+00+六位数字」，如 a00123456");
});

test("登录 401 → 「用户名或密码不对」", async () => {
  const { error } = await callFailing(
    () => loginUser("a00123456", "wrong"),
    jsonResponse(401, { detail: "用户名或密码错误" })
  );
  assert.equal(error.message, "用户名或密码不对");
});

test("auth 的 5xx / 空 detail 泛化,不漏后端原文", async () => {
  const { error } = await callFailing(
    () => registerUser("a00123456", "pw"),
    jsonResponse(500, { detail: "IntegrityError: UNIQUE constraint failed" })
  );
  assert.equal(error.message, "服务暂时不可用，请稍后再试");
  assert.ok(!error.message.includes("IntegrityError"));

  const { error: e2 } = await callFailing(
    () => registerUser("a00123456", "pw"),
    jsonResponse(400, "")
  );
  assert.equal(e2.message, "操作失败，请重试");
});

test("auth 的英文 detail 不漏给用户", async () => {
  const { error, logs } = await callFailing(
    () => fetchMe(),
    jsonResponse(401, { detail: "Invalid or expired token" })
  );
  assert.equal(error.message, "登录状态已失效，请重新登录");
  assert.ok(!/[A-Za-z]{4,}/.test(error.message), "用户文案里不应出现英文单词");
  // 但原始诊断必须还在 console 里。
  assert.match(logs[0], /Invalid or expired token/);
});

test("分享 client 的 403 英文 detail → 中文,状态码和英文都不进用户文案", async () => {
  // 这是本次评审的核心回归:此前这里裸抛 `403 {"detail":"notebook owner required"}`,
  // 用户看到的是「服务异常：403 {"detail":"notebook owner required"}」。
  const { error, logs } = await callFailing(
    () => shareNotebook("nb-1"),
    jsonResponse(403, { detail: "notebook owner required" })
  );
  assert.equal(error.message, "没有权限进行这个操作");
  assert.ok(!error.message.includes("403"));
  assert.ok(!error.message.includes("notebook owner required"));
  assert.ok(!error.message.includes("{"));
  assert.match(logs[0], /notebook owner required/);
});

test("model-settings 失败:用户拿中文,console 拿到 状态码+detail+requestId", async () => {
  // 阻塞 3:此前只 console.error(`[model-settings] HTTP 500`),排查时看不到
  // 供应商到底报了什么。
  const { error, logs } = await callFailing(
    () => testModelService("llm", "https://u/v1", "m", null),
    jsonResponse(
      500,
      { detail: "upstream 401 from provider: invalid api key" },
      { "X-Request-Id": "req-xyz" }
    )
  );
  assert.equal(error.message, "服务暂时不可用，请稍后再试");
  assert.match(logs[0], /\[model-settings\]/);
  assert.match(logs[0], /500/);
  assert.match(logs[0], /invalid api key/);
  assert.match(logs[0], /req-xyz/);
});

test("治理 client 的中文 detail 透传(比「没有权限」具体)", async () => {
  const { error } = await callFailing(
    () => setNotebookTier("nb-1", "base"),
    jsonResponse(403, { detail: "仅管理员可设置基准库" })
  );
  assert.equal(error.message, "仅管理员可设置基准库");
});

test("knowhow client 的中文 detail 透传(保住可操作信息)", async () => {
  const { error } = await callFailing(
    () => fetchKnowhowTables("nb-1"),
    jsonResponse(400, { detail: "格子定位不合法" })
  );
  assert.equal(error.message, "格子定位不合法");
});

test("admin 总览的 403 仍走 forbidden 哨兵(专用无权限视图)", async () => {
  // admin/usage/page.tsx 按 message === "forbidden" 分支到专门的视图,
  // 这条控制流不能被人话层吃掉。
  const { error } = await callFailing(
    () => fetchAdminUsers(),
    jsonResponse(403, { detail: "仅管理员可查看用户总览" })
  );
  assert.equal(error.message, "forbidden");
});

test("admin 总览的非 403 错误给人话(不是裸状态码)", async () => {
  const { error } = await callFailing(() => fetchAdminUsers(), jsonResponse(500, { detail: "boom" }));
  assert.equal(error.message, "服务暂时不可用，请稍后再试");
});

test("admin 总览的 403 哨兵不吞诊断(状态码 + detail + request id 进 console)", async () => {
  // 顺带收口:哨兵此前在读取诊断前就抛,「明明是管理员却看到无权限」无从查起。
  const { error, logs } = await callFailing(
    () => fetchAdminUsers(),
    jsonResponse(403, { detail: "仅管理员可查看用户总览" }, { "X-Request-Id": "req-adm" })
  );
  assert.equal(error.message, "forbidden");
  assert.equal(logs.length, 1);
  assert.match(logs[0], /\[admin\]/);
  assert.match(logs[0], /403/);
  assert.match(logs[0], /仅管理员可查看用户总览/);
  assert.match(logs[0], /req-adm/);
});

// ---------------------------------------------------------------------------
// 非 HTTP-Response 错误(评审阻塞 1):fetch 自身 reject / 流式 error / job error
//
// 这些路径根本进不了 throwHumanizedHttpError——没有 Response 可读。此前它们
// 一路直出到用户面前(「服务异常：Failed to fetch」)。
// ---------------------------------------------------------------------------

// 让 fetch 自身 reject(断网、DNS 挂、后端没起来),而不是返回一个失败响应。
async function callWithRejectingFetch(fn, cause) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw cause;
  };
  try {
    return await captureConsole(async () => {
      try {
        await fn();
        return null;
      } catch (error) {
        return error;
      }
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("fetch 自身 reject:用户看到中文,「Failed to fetch」只在 console", async () => {
  const { value: caught } = await callWithRejectingFetch(
    () => shareNotebook("nb-1"),
    new TypeError("Failed to fetch")
  );
  assert.ok(caught instanceof TypeError, "client 不吞这个异常,原样往上抛给 catch 侧");

  // 用户看到的是 catch 侧过完人话层的结果——page.tsx 的 reportError 就是这条。
  const { value: shown, logs } = captureSync(() =>
    toUserMessage(caught, "服务出了点问题，请稍后重试")
  );
  assert.equal(shown, "服务出了点问题，请稍后重试");
  assert.ok(!shown.includes("Failed to fetch"));
  assert.ok(!/[A-Za-z]/.test(shown), "用户文案里不该有英文");
  assert.match(logs[0], /Failed to fetch/, "原文必须留在 console");
});

test("流式 error 事件 / 后台 job 的英文 error → 中文", async () => {
  // page.tsx 的 ask 流 `event.error` 与重连轮询的 `job.error` 都是后端英文串。
  const { value, logs } = captureSync(() => [
    toUserMessage(new Error("RuntimeError: llm call failed after 3 retries"), "回答没能完成，请重试"),
    toUserMessage(new Error("TimeoutError"), "该问答失败，请稍后重试"),
  ]);
  assert.deepEqual(value, ["回答没能完成，请重试", "该问答失败，请稍后重试"]);
  assert.match(logs[0], /llm call failed/);
  assert.match(logs[1], /TimeoutError/);
});

test("后端写成中文的 stream/job error 仍然透传(别把可操作信息弄丢)", () => {
  const { value } = captureSync(() =>
    toUserMessage(new Error("知识库正在重建索引，请稍后再问"), "回答没能完成，请重试")
  );
  assert.equal(value, "知识库正在重建索引，请稍后再问");
});
