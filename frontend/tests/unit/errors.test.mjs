import test from "node:test";
import assert from "node:assert/strict";

import {
  GENERIC_USER_ERROR,
  humanizedError,
  humanizeHttpError,
  httpErrorStatus,
  logDiagnostic,
  pluginEngineFailureMessage,
  readHttpError,
  throwHumanizedHttpError,
  toUserMessage,
} from "../../app/errors.ts";
import { fetchMe, loginUser, registerUser } from "../../app/auth.ts";
import { testSystemModelService } from "../../app/model-services.ts";
import { setNotebookTier } from "../../app/notebook-tier.ts";
import { shareNotebook } from "../../app/notebook-share.ts";
import { fetchKnowhowTables } from "../../app/knowhow-model.ts";
import { fetchAdminUsers, updateAdminUserRole } from "../../app/admin/usage/api.ts";

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

// 第三轮评审阻塞 1:信任判据从「形态」换成「出处」。没有后端 user_error()
// 盖章(X-User-Message)的 detail,**不管长什么样**都不给用户看。
test("没盖章的 detail 一律不进用户文案——中文也不行", () => {
  // 旧判据「4xx 且含中文就透传」放行的两类真实泄漏:
  assert.equal(
    humanizeHttpError(403, "访问被拒绝 — nginx/1.25 request id=req-1 upstream=10.0.0.7:8000"),
    "没有权限进行这个操作"
  );
  assert.equal(humanizeHttpError(422, "字段不能为空；field required"), "提交的内容有误");
  // 后端 detail=str(exc) 抛出来的中文异常串,同样拦掉。
  assert.equal(humanizeHttpError(400, "解析失败：不支持的文件类型"), "操作失败，请重试");
  // 英文 detail 自然也一样(它们是给 MCP / 日志看的)。
  assert.equal(humanizeHttpError(403, "notebook owner required"), "没有权限进行这个操作");
  assert.equal(humanizeHttpError(500, "Internal Server Error"), "服务暂时不可用，请稍后再试");
  assert.equal(humanizeHttpError(400, "invalid cell address"), "操作失败，请重试");
});

test("盖了章的 4xx detail 原样透传(保住可操作信息)", () => {
  // 后端经 user_error() 明确声明「这是写给用户的」,比状态码泛化有用得多。
  assert.equal(humanizeHttpError(400, "用户名已被占用", true), "用户名已被占用");
  assert.equal(humanizeHttpError(400, "格子定位不合法", true), "格子定位不合法");
  assert.equal(humanizeHttpError(403, "仅管理员可设置基准库", true), "仅管理员可设置基准库");
  assert.equal(humanizeHttpError(403, "仅管理员可管理晋升队列", true), "仅管理员可管理晋升队列");
  // 中文里混标识符 / 数字是正常文案,不该被闸2 误伤。
  assert.equal(
    humanizeHttpError(400, "用户名须为「单个小写字母+八位数字」，如 a12345678", true),
    "用户名须为「单个小写字母+八位数字」，如 a12345678"
  );
});

// 第四轮评审阻塞 1:出处解决了「可不可信」,没解决「该长什么样」。
//
// 这条测试原来断言的是 `humanizeHttpError(400, "Quota exceeded", true) ===
// "Quota exceeded"`,理由写「信任来自出处,不是语言」——那句话对的是**闸1**,
// 却被用来给闸2 的缺席背书,等于把 bug 钉成了契约:AGENTS.md 写着「用户可见
// 错误一律中文」,而这里让英文原样上屏。
//
// 现在两道闸串联:闸1 判可不可信(出处),闸2 判合不合规(中文)。盖了章的英文
// 文案 = 后端写码时的产品缺陷,界面退通用文案,原文喊进 console 让人去改。
test("盖了章但非中文的文案不上屏——退通用文案 + 喊进 console", () => {
  const { value, logs } = captureSync(() => [
    humanizeHttpError(400, "Quota exceeded", true),
    humanizeHttpError(403, "Forbidden: admin only", true),
    humanizeHttpError(404, "not found", true),
  ]);
  assert.deepEqual(value, ["操作失败，请重试", "没有权限进行这个操作", "没找到，可能已被删除"]);
  assert.ok(!value.some((v) => /[A-Za-z]/.test(v)), "退回的通用文案里不该有英文");
  // 三条都得喊出来,否则没人知道后端写了英文用户文案。
  assert.equal(logs.length, 3);
  assert.match(logs[0], /user-copy/);
  assert.match(logs[0], /Quota exceeded/, "原文要留在 console 供定位");
});

// 闸2 只在闸1 放行之后才跑——没盖章的英文不该产生「产品缺陷」告警,它走的是
// 另一条路(不可信 → 泛化),混在一起会把噪声灌满 console。
test("没盖章的非中文走闸1,不触发闸2 的产品缺陷告警", () => {
  const { value, logs } = captureSync(() => humanizeHttpError(403, "notebook owner required"));
  assert.equal(value, "没有权限进行这个操作");
  assert.equal(logs.length, 0, "闸1 就拦住了,不该额外报『非中文用户文案』");
});

test("trusted 默认关(不传 = 不信)", () => {
  // deny by default:漏传参数只会更保守,不会更宽松。
  assert.equal(humanizeHttpError(403, "仅管理员可设置基准库"), "没有权限进行这个操作");
});

test("5xx 的 detail 一律不透传(盖了章也不行)", () => {
  // user_error() 是给「客户端能纠正的 4xx」用的;5xx = 我们坏了,通用文案才对,
  // 也堵住上游/网关伪造标记把内部错误顶上屏。
  assert.equal(humanizeHttpError(500, "数据库连接失败"), "服务暂时不可用，请稍后再试");
  assert.equal(humanizeHttpError(503, "后台迁移中"), "服务暂时不可用，请稍后再试");
  assert.equal(humanizeHttpError(500, "数据库连接失败", true), "服务暂时不可用，请稍后再试");
});

test("空白 detail 不影响泛化", () => {
  assert.equal(humanizeHttpError(404, "", true), "没找到，可能已被删除");
  assert.equal(humanizeHttpError(404, "   ", true), "没找到，可能已被删除");
  assert.equal(humanizeHttpError(404, undefined, true), "没找到，可能已被删除");
});

test("形态闸是第二道:盖了章但不像文案的,照样拦", () => {
  // 出处是第一道闸,形态是第二道。防的是后端把 user_error() 用在了拼了异常
  // 原文 / 正文的串上——一个失误不该直通到屏幕。
  assert.equal(
    humanizeHttpError(403, "<html>访问被拒绝 — nginx request id=req-1</html>", true),
    "没有权限进行这个操作"
  );
  assert.equal(humanizeHttpError(404, "<p>页面不存在</p>", true), "没找到，可能已被删除");
  // 多行 = 正文/堆栈,不是文案。
  assert.equal(humanizeHttpError(400, "参数不对\n  at handler (app.py:31)", true), "操作失败，请重试");
  // JSON 花括号 = 结构化正文,不是文案。
  assert.equal(
    humanizeHttpError(400, '{"detail":"参数不对","request_id":"req-1"}', true),
    "操作失败，请重试"
  );
  // 超长 = 正文,不是文案(后端为用户写的都是短句)。
  assert.equal(humanizeHttpError(400, `很抱歉${"细节".repeat(200)}`, true), "操作失败，请重试");
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
  // 判据是**品牌**(humanizedError 盖的章),不是文本长什么样。
  const { value, logs } = captureSync(() => [
    toUserMessage(humanizedError("没有权限进行这个操作"), "兜底"),
    toUserMessage(humanizedError("没找到，可能已被删除"), "兜底"),
    toUserMessage(humanizedError("操作有冲突，请刷新后重试"), "兜底"),
  ]);
  assert.deepEqual(value, ["没有权限进行这个操作", "没找到，可能已被删除", "操作有冲突，请刷新后重试"]);
  // 透传路径无损,不该重复刷日志(HTTP 诊断 readHttpError 已经记过)。
  assert.deepEqual(logs, []);
});

// 第三轮评审阻塞 2:旧版靠「像不像一句中文」放行,于是后端塞在 job.error /
// error_message / stream event 里的中文技术串一路直出到用户面前。
test("toUserMessage 只认品牌:没盖章的中文技术串一律兜底", () => {
  const { value, logs } = captureSync(() => [
    // 实测复现过的原话:旧版把它整串显示给了用户。
    toUserMessage(new Error("RuntimeError: 模型调用失败 upstream timeout"), "兜底"),
    // 后端 f"{type(exc).__name__}: {exc}" 的典型形状。
    toUserMessage(new Error("ValueError: 解析失败，来源为空"), "兜底"),
    // 纯中文、短、单行、无标签——形态上完全像一句文案,但没盖章。
    toUserMessage(new Error("知识库正在重建索引，请稍后再问"), "兜底"),
  ]);
  assert.deepEqual(value, ["兜底", "兜底", "兜底"]);
  // 原文一条都不能丢,全在 console 里。
  assert.equal(logs.length, 3);
  assert.match(logs[0], /upstream timeout/);
  assert.match(logs[2], /重建索引/);
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

test("品牌伪造不了:后端字符串永远带不上它", () => {
  // 威胁模型是「后端来的字符串被当成可展示文案」。字符串、JSON 反序列化出来的
  // 对象、手工拼的 Error 都盖不上章——JSON 里没有 Symbol。
  const { value } = captureSync(() => [
    toUserMessage(JSON.parse('{"message":"没有权限进行这个操作"}'), "兜底"),
    toUserMessage(Object.assign(new Error("没有权限进行这个操作"), { humanized: true }), "兜底"),
    toUserMessage({ message: "没有权限进行这个操作" }, "兜底"),
  ]);
  assert.deepEqual(value, ["兜底", "兜底", "兜底"]);
});

test("品牌能穿过 catch / rethrow(不是一次性的)", () => {
  // 真实路径:client 抛 → 调用方 catch → 再 throw → page 的 reportError 收。
  let caught;
  try {
    try {
      throw humanizedError("没找到，可能已被删除");
    } catch (error) {
      throw error;
    }
  } catch (error) {
    caught = error;
  }
  const { value } = captureSync(() => toUserMessage(caught, "兜底"));
  assert.equal(value, "没找到，可能已被删除");
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

// 阻塞 3(P2):诊断日志也要有上限——旧版 toUserMessage 把整个 error 对象直接
// 丢进 console,HTTP 路径截断到 500 字符而这条没有,一个超长异常能刷爆控制台。
test("超长的非 HTTP 异常,诊断行同样截断", () => {
  const huge = new Error(`抽取失败 ${"细节 ".repeat(4000)}`);
  const { value, logs } = captureSync(() => toUserMessage(huge, "兜底"));
  assert.equal(value, "兜底");
  assert.equal(logs.length, 1);
  assert.ok(logs[0].length < 700, `诊断行应被截断,实际 ${logs[0].length} 字符`);
  assert.match(logs[0], /已截断/);
  // 截断归截断,开头的实质内容要留住(否则等于没记)。
  assert.match(logs[0], /抽取失败/);
});

test("诊断格式化器认得非 Error 值(不 crash、也截断)", () => {
  const { logs } = captureSync(() => [
    toUserMessage("x".repeat(4000), "兜底"),
    toUserMessage({ nested: { blob: "y".repeat(4000) } }, "兜底"),
    toUserMessage(null, "兜底"),
    toUserMessage(undefined, "兜底"),
  ]);
  assert.equal(logs.length, 4);
  for (const line of logs) assert.ok(line.length < 700, `实际 ${line.length} 字符`);
  assert.match(logs[2], /null/);
  assert.match(logs[3], /undefined/);
});

test("logDiagnostic:渲染点用的显式诊断出口,同一个上限", () => {
  const { logs } = captureSync(() =>
    logDiagnostic("report", new Error(`规划失败 ${"细节 ".repeat(4000)}`))
  );
  assert.equal(logs.length, 1);
  assert.ok(logs[0].length < 700, `实际 ${logs[0].length} 字符`);
  assert.match(logs[0], /\[report\]/);
  assert.match(logs[0], /规划失败/);
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

// 后端经 user_error() 抛出的响应:带出处标记。没有这个头的一律不可展示。
const markedResponse = (status, body, headers = {}) =>
  jsonResponse(status, body, { "X-User-Message": "1", ...headers });

test("readHttpError 读出出处标记(有/没有都要读对)", async () => {
  const { value: marked } = await captureConsole(() =>
    readHttpError(markedResponse(403, { detail: "仅管理员可设置基准库" }), "t")
  );
  assert.equal(marked.trusted, true);
  assert.equal(marked.userDetail, "仅管理员可设置基准库");

  const { value: bare } = await captureConsole(() =>
    readHttpError(jsonResponse(403, { detail: "仅管理员可设置基准库" }), "t")
  );
  assert.equal(bare.trusted, false, "没有 X-User-Message 就不算盖章");
  assert.equal(bare.userDetail, "仅管理员可设置基准库", "detail 照读,只是不可信");

  // 只认精确的 "1"。
  for (const bad of ["0", "true", "yes", ""]) {
    const { value } = await captureConsole(() =>
      readHttpError(jsonResponse(400, { detail: "x" }, { "X-User-Message": bad }), "t")
    );
    assert.equal(value.trusted, false, `X-User-Message: "${bad}" 不该被当成盖章`);
  }
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
      "model-services"
    )
  );
  assert.equal(value.requestId, "req-abc123");
  assert.equal(logs.length, 1);
  assert.match(logs[0], /\[model-services\]/);
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

test("throwHumanizedHttpError: Knowhow 导入校验提示可原样进入向导", async () => {
  const message = "这张表看起来是属性按行排列（第一列是属性名，每一列是一条记录），请先选择“属性按行”，再重新选择这个文件。";
  await captureConsole(async () => {
    await assert.rejects(
      throwHumanizedHttpError(markedResponse(400, { detail: message }), "knowhow-import-preview"),
      (error) => {
        assert.equal(error.message, message);
        assert.equal(toUserMessage(error, "解析文件失败，请重试"), message);
        return true;
      }
    );
  });
});

// httpErrorStatus:并发防护（P1-b）需要在 catch 里区分 409（他人已改 → 跳过该格）
// 与其他失败（真报错）。humanizeHttpError 把 detail 压平成中文文案，丢了状态码；
// 所以 throwHumanizedHttpError 把原始状态码挂在带品牌的 Error 上，httpErrorStatus
// 读回它。非 HTTP 错误（fetch reject、纯 Error）读不到 → undefined。
test("httpErrorStatus: 从带品牌的 HTTP 错误上读回原始状态码（区分 409 与其他）", async () => {
  await captureConsole(async () => {
    await assert.rejects(
      throwHumanizedHttpError(markedResponse(409, { detail: "内容已被其他人修改，请刷新后重试" }), "knowhow"),
      (error) => {
        assert.equal(httpErrorStatus(error), 409);
        return true;
      }
    );
    await assert.rejects(
      throwHumanizedHttpError(jsonResponse(500, { detail: "boom" }), "knowhow"),
      (error) => {
        assert.equal(httpErrorStatus(error), 500);
        return true;
      }
    );
  });
});

test("httpErrorStatus: 非 HTTP 错误（纯 Error / humanizedError 无状态 / 非 Error 值）→ undefined", () => {
  assert.strictEqual(httpErrorStatus(new Error("Failed to fetch")), undefined);
  assert.strictEqual(httpErrorStatus(humanizedError("回答没能完成，请重试")), undefined);
  assert.strictEqual(httpErrorStatus(null), undefined);
  assert.strictEqual(httpErrorStatus("字符串不是错误"), undefined);
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

test("注册 400 的盖章 detail 原样保留(不被泛化成「操作失败」)", async () => {
  // 后端这两处走的是 user_error(),响应带 X-User-Message。
  const { error } = await callFailing(
    () => registerUser("a00123456", "pw"),
    markedResponse(400, { detail: "用户名已被占用" })
  );
  assert.equal(error.message, "用户名已被占用");

  const { error: e2 } = await callFailing(
    () => registerUser("bad", "pw"),
    markedResponse(400, { detail: "用户名须为「单个小写字母+八位数字」，如 a12345678" })
  );
  assert.equal(e2.message, "用户名须为「单个小写字母+八位数字」，如 a12345678");
});

test("同一条注册 400,没盖章就只给通用文案", async () => {
  // 这是本轮的核心回归:形态完全一样(短、中文、单行),差别只在出处。
  const { error, logs } = await callFailing(
    () => registerUser("a00123456", "pw"),
    jsonResponse(400, { detail: "用户名已被占用" })
  );
  assert.equal(error.message, "操作失败，请重试");
  assert.match(logs[0], /用户名已被占用/, "原文仍进 console");
});

test("登录 401 → 「用户名或密码不对」", async () => {
  const { error } = await callFailing(
    () => loginUser("a00123456", "wrong"),
    markedResponse(401, { detail: "用户名或密码错误" })
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

test("model-services 失败:用户拿中文,console 拿到 状态码+detail+requestId", async () => {
  // 共享 transport 保留状态码、请求编号和仅供维护人员查看的诊断。
  // 供应商到底报了什么。
  const { error, logs } = await callFailing(
    () => testSystemModelService("general"),
    jsonResponse(
      500,
      { detail: "upstream 401 from provider: invalid api key" },
      { "X-Request-Id": "req-xyz" }
    )
  );
  assert.equal(error.message, "服务暂时不可用，请稍后再试");
  assert.match(logs[0], /\[model-services\]/);
  assert.match(logs[0], /500/);
  assert.match(logs[0], /invalid api key/);
  assert.match(logs[0], /req-xyz/);
});

test("治理 client 的盖章 detail 透传(比「没有权限」具体)", async () => {
  const { error } = await callFailing(
    () => setNotebookTier("nb-1", "base"),
    markedResponse(403, { detail: "仅管理员可设置基准库" })
  );
  assert.equal(error.message, "仅管理员可设置基准库");
});

test("knowhow client 的盖章 detail 透传(保住可操作信息)", async () => {
  const { error } = await callFailing(
    () => fetchKnowhowTables("nb-1"),
    markedResponse(400, { detail: "格子定位不合法" })
  );
  assert.equal(error.message, "格子定位不合法");
});

test("同一个 knowhow 400,detail=str(exc) 没盖章 → 通用文案", async () => {
  // 后端 knowhow 路由里 `detail=str(exc)` 有十几处,和上面那条形态无法区分,
  // 靠出处才分得开。
  const { error, logs } = await callFailing(
    () => fetchKnowhowTables("nb-1"),
    jsonResponse(400, { detail: "格子定位不合法" })
  );
  assert.equal(error.message, "操作失败，请重试");
  assert.match(logs[0], /格子定位不合法/);
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

test("管理员权限更新保留后端盖章的冲突原因", async () => {
  const { error } = await callFailing(
    () => updateAdminUserRole("user-local", "user"),
    markedResponse(409, { detail: "内置管理员权限不可撤销" })
  );
  assert.equal(error.message, "内置管理员权限不可撤销");
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

// P2 修复:kg_neighbors/search_kg 共享的 KG 预算耗尽码此前不在预算文案清单
// 里,会落进兜底「没能完成回答」——用户读不出「超预算」这个可操作信息。
test("扩展引擎 KG 预算耗尽码归入既有预算文案", () => {
  assert.equal(
    pluginEngineFailureMessage("AskPluginEngineError: plugin_engine_kg_call_limit"),
    "扩展引擎超过了本次调用预算，请重试或切换引擎",
  );
  assert.equal(
    pluginEngineFailureMessage("AskPluginEngineError: plugin_engine_citation_limit"),
    "扩展引擎超过了本次调用预算，请重试或切换引擎",
  );
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

// 这条测试原来断言的是「后端写成中文的 stream/job error 仍然透传」——它把旧的
// 形态信任模型固化成了契约。第三轮评审的结论是那个前提本身就是错的:job.error /
// event.error 由后端写成 f"{type(exc).__name__}: {exc}"(见
// services/ask_execution.py),中文与否纯属偶然,不构成「这句写给用户」的证据。
// 现在它反过来断言:没盖章的一律兜底,可操作信息由后端用 user_error() 显式给,
// 而不是靠前端猜。
test("stream/job 的 error 字段一律不直出——中文也一样", () => {
  const { value, logs } = captureSync(() => [
    toUserMessage(new Error("知识库正在重建索引，请稍后再问"), "回答没能完成，请重试"),
    toUserMessage(new Error("RuntimeError: 模型调用失败 upstream timeout"), "回答没能完成，请重试"),
  ]);
  assert.deepEqual(value, ["回答没能完成，请重试", "回答没能完成，请重试"]);
  assert.match(logs[0], /重建索引/);
  assert.match(logs[1], /upstream timeout/);
});

// ---------------------------------------------------------------------------
// 完整组合链(第四轮评审阻塞 2)
// ---------------------------------------------------------------------------
//
// 上面几条都只测**一跳**。真实路径是三跳:
//
//   ask stream 的 error 事件 → readAskStream 抛 → runAsk 的 catch → reportError
//
// 而 bug 恰恰只在**跨跳**时才显形:第一跳产出的安全文案如果装在裸 Error 里,
// 第二跳的 toUserMessage 认不出它,再泛化一次。单跳测试全绿,用户看到的却是
// 全局兜底。所以这条测试必须把整条链跑完,并数诊断条数。
//
// page.tsx 是 Next client component,node:test 里 import 不了;链路的两端由
// errors-guard.test.mjs 按源码钉死(ask 流那处必须是 logDiagnostic + 抛
// humanizedError,reportError 必须是 setStatusText(toUserMessage(...))),
// 这里跑的是那两处**逐字对应**的组合。
test("组合链:stream error 事件 → catch → reportError,场景文案不被二次泛化", () => {
  // 第一跳:page.tsx 的 consumeLine —— 原文进诊断,抛带品牌的场景文案。
  const streamHop = (rawError) => {
    logDiagnostic("ask-stream", rawError);
    throw humanizedError("回答没能完成，请重试");
  };
  // 第二跳:page.tsx 的 reportError —— 全工作区 90+ 个 .catch 都汇到这里。
  const reportError = (error) => toUserMessage(error, "服务出了点问题，请稍后重试");

  const { value: shown, logs } = captureSync(() => {
    try {
      streamHop("RuntimeError: llm call failed after 3 retries");
      return "(没抛?)";
    } catch (error) {
      return reportError(error);
    }
  });

  // 用户最终看到的是**场景**文案,不是全局兜底。这是回归点:改回裸 new Error
  // 的话这里会变成「服务出了点问题，请稍后重试」。
  assert.equal(shown, "回答没能完成，请重试");
  assert.notEqual(shown, "服务出了点问题，请稍后重试");
  assert.ok(!/[A-Za-z]/.test(shown), "用户文案里不该有英文");

  // 诊断只记一次:原文一条。旧写法会记两条——第二条把已安全化的
  // 「回答没能完成，请重试」当成「未翻译的原始错误」,既是噪声又误导排查。
  assert.equal(logs.length, 1, `诊断应只记一次,实际 ${logs.length} 条:\n${logs.join("\n")}`);
  assert.match(logs[0], /llm call failed after 3 retries/, "记的必须是后端原文");
  assert.ok(
    !logs[0].includes("回答没能完成"),
    "不该把已安全化的用户文案当成原始错误记进诊断"
  );
});

// 第四轮评审阻塞 3:report-view 的两处失败路径原来是「裸 console.error +
// toUserMessage」并排,一次失败打两条日志,其中裸的那条无上限、还是整个对象。
//
// 这条跑的是**调用端的完整组合**(不是只测 helper):report-view 的 surfaceError
// 与 confirmGenerate 的 catch 现在都只剩 `setToast(toUserMessage(error, 兜底))`
// 这一句,逐字对应下面这段。「调用端不许再补裸日志」由 errors-guard.test.mjs
// 的守卫⑤ 钉住——两条合起来才是完整覆盖:守卫管「没有多余的那行」,这条管
// 「剩下的这行行为对」。
test("报告面板的超长异常:一次失败只打一条日志,且被截断", () => {
  // 后端 500 时网关常回整页 HTML;这里模拟一个 5000 字符的异常。
  const huge = new Error("ReportBuildError: " + "报告章节渲染失败 stack frame ".repeat(200));
  assert.ok(huge.message.length > 4000, `构造的异常应足够长,实际 ${huge.message.length}`);

  const setToastCalls = [];
  const setToast = (text) => setToastCalls.push(text);

  // ↓ report-view.tsx surfaceError 的全部内容
  const { logs } = captureSync(() => {
    setToast(toUserMessage(huge, "报告操作没成功，请稍后重试"));
  });

  // 用户看人话,不看堆栈。
  assert.deepEqual(setToastCalls, ["报告操作没成功，请稍后重试"]);
  // 一次失败 = 一条诊断(旧写法是两条:裸 console.error 一条 + logDiagnostic 一条)。
  assert.equal(logs.length, 1, `一次失败应只打一条日志,实际 ${logs.length} 条`);
  // 而且这条受 DIAGNOSTIC_MAX_CHARS 约束——裸 console.error 那条当年是不受的。
  assert.ok(logs[0].length < 700, `诊断行应被截断,实际 ${logs[0].length} 字符`);
  assert.match(logs[0], /已截断/);
  assert.match(logs[0], /ReportBuildError/, "截断归截断,开头的实质内容要留住");
});

test("报告面板的 HTTP 译文路径:同样只有一条诊断(且在 fetch 层记)", () => {
  // 走 throwHumanizedHttpError 的错误已经在 readHttpError 里记过原始正文,
  // toUserMessage 认品牌直接透传、不重复记。所以这条路径同样是「一次失败一条」。
  const setToastCalls = [];
  const { logs } = captureSync(() => {
    setToastCalls.push(toUserMessage(humanizedError("没有权限进行这个操作"), "报告操作没成功，请稍后重试"));
  });
  assert.deepEqual(setToastCalls, ["没有权限进行这个操作"]);
  assert.equal(logs.length, 0, "已在 fetch 层记过,catch 侧不该再记一条");
});

test("组合链:品牌能穿过任意多层 catch(不是只挡住一跳)", () => {
  // 重连轮询、批量预审那几条路径上,错误会经过不止一层 catch。品牌是对象属性,
  // 只要不重新包装就一路带着。
  const { value, logs } = captureSync(() => {
    let error;
    try {
      throw humanizedError("没有权限进行这个操作");
    } catch (e1) {
      try {
        throw e1; // 中间层原样上抛(不重新包装)
      } catch (e2) {
        error = e2;
      }
    }
    return toUserMessage(error, "服务出了点问题，请稍后重试");
  });
  assert.equal(value, "没有权限进行这个操作");
  assert.equal(logs.length, 0, "已安全化的文案穿过 catch 不该产生诊断噪声");
});
