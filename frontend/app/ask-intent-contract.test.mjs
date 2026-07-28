import assert from "node:assert/strict";
import test from "node:test";

import {
  callsIn,
  comparisonsIn,
  findFunction,
  importsFrom,
  jsxTextValues,
  parseModule,
  stringLiterals,
  variableInitializersIn,
} from "./test/semantic-source.mjs";


const page = await parseModule("page.tsx");
const review = await parseModule("ask-intent-review.tsx");


test("reasoning Ask previews intent before starting its durable stream", () => {
  const previewCalls = callsIn(findFunction(page, "runAsk"));
  const executeCalls = callsIn(findFunction(page, "executeAsk"));

  assert.ok(previewCalls.includes("previewAskIntent"));
  assert.ok(previewCalls.includes("buildAskIntentConfirmation"));
  assert.ok(executeCalls.includes("runAskStream"));
});


test("blocking reasoning ambiguity is visibly confirmed before retrieval", () => {
  const copy = jsxTextValues(review).join(" ");
  assert.match(copy, /只理解你的问题，不读取资料/);
  assert.match(copy, /先补充问题信息/);
  assert.match(copy, /确认后的问题/);
});


// 问题理解跑在持久 job 之前,后端此时无从产出轨迹。前端合成这几步并把它们拼在
// 后端流下来的步骤之前,用户看到的才是一条从「理解问题」直达「作答」的连续轨迹,
// 而不是先盯一条与轨迹无关的提示、再看轨迹从中途冒出来。
test("问题理解阶段进入同一条轨迹,而不是另起一条提示", () => {
  const runAskCalls = callsIn(findFunction(page, "runAsk"));
  assert.ok(runAskCalls.includes("intentUnderstandingStep"), "理解在途没有落成轨迹的一步");
  assert.ok(runAskCalls.includes("intentUnderstoodStep"));
  assert.ok(runAskCalls.includes("intentClarifyStep"));
  // 在途 turn 从提交那一刻就要出现,否则那几步没有地方渲染。
  assert.ok(runAskCalls.includes("setPendingQuestion"));
  assert.ok(runAskCalls.includes("setPendingTrace"));
  // 用户定稿也记一步。
  assert.ok(callsIn(findFunction(page, "confirmAskIntent")).includes("intentConfirmedStep"));
  // 前缀必须经 handOffIntentTrace 摘掉耗时再交给 executeAsk,否则后端 intent 步
  // 会把同一段理解时间再算一遍(codex 第 2 轮 P2)。
  for (const fn of ["runAsk", "confirmAskIntent"]) {
    assert.ok(
      callsIn(findFunction(page, fn)).includes("handOffIntentTrace"),
      `${fn} 直接把带耗时的前缀交给了 executeAsk`,
    );
  }
  // 旧的独立提示条不得复活 —— 两套并存等于又把理解阶段挪出轨迹。
  // 两种取值都要扫:直接写在 JSX 里的文案是 JsxText 而非 StringLiteral,
  // 只扫 stringLiterals 的话把提示条原样贴回去也照样绿(实测过)。
  const pageCopy = [...stringLiterals(page), ...jsxTextValues(page)];
  assert.ok(
    !pageCopy.some((value) => value.includes("尚未读取资料或开始检索")),
    "问题理解的提示条回到了 page.tsx",
  );
});


test("在途占位按引擎是否流轨迹渲染(关联追溯不再空等后端事件)", () => {
  assert.ok(
    importsFrom(page, "./ask-modes").map((item) => item.imported).includes("streamsTrace"),
  );
  // 深入分析组里只有逐步推理是流式的;按 pendingMode 的分组判断必然把关联追溯
  // 也挂上实时轨迹面板,那面板从头到尾只会显示「等待后端事件…」。
  assert.deepEqual(
    comparisonsIn(page).filter((item) => item.left.includes("groupOf(pendingMode)")),
    [],
  );
});


// 预检一返回 intentChecking 就复位,但确认卡片还摆在下面等用户补充。若在途判据
// 不认 askIntentReview,轨迹会恰好在「等待你补充」这一步上消失、空会话还退回欢迎页
// (codex 第 1 轮 P2)。
test("待澄清期间在途轨迹不消失", () => {
  const inFlight = variableInitializersIn(page)
    .find((item) => item.name === "askInFlight");
  assert.ok(inFlight, "askInFlight 没了 —— 在途判据被改写,这条守卫需要重写");
  assert.match(inFlight.initializer, /intentChecking/);
  assert.match(inFlight.initializer, /askIntentReview/);
});


// 理解阶段占用了输入框(草稿清空、问题以在途 turn 先显示)。中止/失败/返回修改
// 三条留在原地的路径必须把草稿还回去,否则用户白打一遍字。
test("理解阶段被打断时草稿退回输入框", () => {
  for (const fn of ["cancelAskIntentReview", "abortAsk"]) {
    assert.ok(callsIn(findFunction(page, fn)).includes("setQuestion"), `${fn} 未退回草稿`);
  }
  assert.ok(callsIn(findFunction(page, "runAsk")).includes("clearPendingTurn"));
});
