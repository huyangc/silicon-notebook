import assert from "node:assert/strict";
import test from "node:test";

import ts from "typescript";

import {
  assignmentsIn,
  callsIn,
  comparisonsIn,
  findFunction,
  ifBranchesIn,
  importsFrom,
  jsxElements,
  jsxTextValues,
  parseModule,
  stringLiterals,
  variableInitializersIn,
} from "../../test-support/semantic-source.mjs";


const page = await parseModule("page.tsx");
const askSession = await parseModule("use-ask-session.ts");
const review = await parseModule("ask-intent-review.tsx");


function returnedPropertyInitializer(module, functionName, propertyName) {
  const matches = [];
  const scope = findFunction(module, functionName);
  function visit(node) {
    if (ts.isPropertyAssignment(node) && node.name.getText(module) === propertyName) {
      matches.push(node.initializer.getText(module));
    }
    ts.forEachChild(node, visit);
  }
  visit(scope);
  assert.equal(matches.length, 1, `${functionName} 应恰好返回一处 ${propertyName}`);
  return matches[0];
}


function objectBinding(module, alias) {
  const matches = [];
  function visit(node) {
    if (
      ts.isVariableDeclaration(node)
      && ts.isObjectBindingPattern(node.name)
      && node.initializer
    ) {
      for (const element of node.name.elements) {
        if (element.name.getText(module) !== alias) continue;
        matches.push({
          source: node.initializer.getText(module),
          property: element.propertyName?.getText(module) ?? alias,
        });
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(module);
  assert.equal(matches.length, 1, `应恰好有一处 ${alias} 解构绑定`);
  return matches[0];
}


test("reasoning Ask previews intent before starting its durable stream", () => {
  const previewCalls = callsIn(findFunction(askSession, "submit"));
  const executeCalls = callsIn(findFunction(askSession, "executeAsk"));

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


test("阻断性歧义打开确认卡后不再继续提交 durable ask", () => {
  const branches = ifBranchesIn(findFunction(askSession, "submit"));
  const reviewGate = branches.find(
    (branch) => branch.condition.includes("needs_clarification"),
  );
  assert.ok(reviewGate, "submit 的问题理解确认门不见了");
  assert.ok(reviewGate.thenCalls.includes("setIntentReview"));
  assert.ok(reviewGate.thenReturns, "确认卡打开后仍继续提交了 durable ask");
});


// 问题理解跑在持久 job 之前,后端此时无从产出轨迹。前端合成这几步并把它们拼在
// 后端流下来的步骤之前,用户看到的才是一条从「理解问题」直达「作答」的连续轨迹,
// 而不是先盯一条与轨迹无关的提示、再看轨迹从中途冒出来。
test("问题理解阶段进入同一条轨迹,而不是另起一条提示", () => {
  const runAskCalls = callsIn(findFunction(askSession, "submit"));
  assert.ok(runAskCalls.includes("intentUnderstandingStep"), "理解在途没有落成轨迹的一步");
  assert.ok(runAskCalls.includes("intentUnderstoodStep"));
  assert.ok(runAskCalls.includes("intentClarifyStep"));
  // 在途 turn 从提交那一刻就要出现,否则那几步没有地方渲染。
  assert.ok(runAskCalls.includes("setPendingQuestion"));
  assert.ok(runAskCalls.includes("setPendingTrace"));
  // 用户定稿也记一步。
  assert.ok(callsIn(findFunction(askSession, "confirmIntent")).includes("intentConfirmedStep"));
  // 前缀必须经 handOffIntentTrace 摘掉耗时再交给 executeAsk,否则后端 intent 步
  // 会把同一段理解时间再算一遍(codex 第 2 轮 P2)。
  for (const fn of ["submit", "confirmIntent"]) {
    assert.ok(
      callsIn(findFunction(askSession, fn)).includes("handOffIntentTrace"),
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
  assert.deepEqual(objectBinding(page, "askInFlight"), {
    source: "askSession",
    property: "inFlight",
  });
  assert.deepEqual(objectBinding(page, "askIntentReview"), {
    source: "askSession",
    property: "intentReview",
  });
  const inFlight = returnedPropertyInitializer(askSession, "useAskSession", "inFlight");
  assert.match(inFlight, /asking/);
  assert.match(inFlight, /intentChecking/);
  assert.match(inFlight, /intentReview/);
});


// 理解阶段占用了输入框(草稿清空、问题以在途 turn 先显示)。中止/失败/返回修改
// 三条留在原地的路径必须把草稿还回去,否则用户白打一遍字。
// 逐步推理与深度报告的「确认问题理解」是同一个交互,必须共用同一张卡的外观。
// 此前两边各写一套 report-intent-* 的用法,改一侧就会悄悄长成两个样子 ——
// 与 EffortPicker 那条「同一套档名理应是同一个控件」同理。
test("两个界面共用同一张问题理解确认卡", async () => {
  for (const file of ["ask-intent-review.tsx", "report-view.tsx"]) {
    const module = await parseModule(file);
    const classes = [
      ...jsxElements(module, "section"),
      ...jsxElements(module, "div"),
    ].map((node) => node.attributes?.className ?? "").filter(Boolean);
    assert.ok(
      classes.some((name) => /(^|\s)intent-card(\s|$)/.test(name)),
      `${file} 没有用共用的 .intent-card 外壳`,
    );
    // 卡内结构也共用:三处骨架任缺一处就说明这一侧又自成一套了。
    for (const part of ["intent-card-head", "intent-card-zone", "intent-card-readout"]) {
      assert.ok(
        classes.some((name) => name.includes(part))
        || jsxElements(module, "details").some(
          (node) => (node.attributes?.className ?? "").includes(part),
        ),
        `${file} 缺少 .${part}`,
      );
    }
  }
});


test("理解阶段被打断时草稿退回输入框", () => {
  for (const fn of ["cancelIntent", "abort"]) {
    assert.ok(callsIn(findFunction(askSession, fn)).includes("setQuestion"), `${fn} 未退回草稿`);
  }
  assert.ok(callsIn(findFunction(askSession, "submit")).includes("clearPendingTurn"));
});


// executeAsk 的可用性守卫可能在问题理解跑完之后才拦下来(那段时间里证据/图谱
// 状态会变)。此时输入框已清空、在途 turn 已隐藏,不看返回值就等于把用户打的
// 问题悄悄吞掉(codex 第 5 轮 P2)。
test("提交被可用性守卫拦下时草稿退回,而不是无声丢弃", () => {
  const guarded = variableInitializersIn(askSession)
    .filter((item) => item.name === "started" && item.initializer.includes("executeAsk"));
  assert.equal(guarded.length, 2, "submit / confirmIntent 应各据返回值判断是否退回");
  for (const fn of ["submit", "confirmIntent"]) {
    const body = findFunction(askSession, fn);
    assert.ok(callsIn(body).includes("setQuestion"), `${fn} 未退回草稿`);
    assert.ok(callsIn(body).includes("clearPendingTurn"), `${fn} 未收起在途 turn`);
  }
  // 草稿槽是全局共享的,而清理发生在 `await executeAsk` 之后。期间用户可能已切
  // 会话(那会把 flow 置回 idle、放行新一轮预检),旧 run 返回时若无条件清理,就会
  // 抹掉**新一轮**的草稿,新一轮再取消就无从退回(codex 第 9 轮 P2)。
  // 不变式:这两个函数里**不得**裸清草稿槽,一律经 releaseIntentDraft(令牌) 释放。
  // 只断言「至少有一处认令牌」不够 —— runAsk 有两处清理(await 之后、catch 之中),
  // 删掉其中一处时另一处仍能让守卫变绿(实测打空过)。收敛成一个释放口之后,
  // 「有没有漏认令牌」退化成「有没有裸赋值」,machine-checkable。
  // ⚠ assignmentsIn 保留字面量引号(与 comparisonsIn 的 right 不同,后者给去引号的
  //   .text)——按 `""` 比对会永远匹配不到,守卫就成了摆设。
  for (const fn of ["submit", "confirmIntent"]) {
    const body = findFunction(askSession, fn);
    assert.deepEqual(
      assignmentsIn(body).filter(
        (item) => item.target.startsWith("askIntentDraft") && item.value === '""',
      ),
      [], `${fn} 裸清了草稿槽,必须改走 releaseIntentDraft(draftToken)`,
    );
    assert.ok(
      callsIn(body).includes("releaseIntentDraft"),
      `${fn} 没有经 releaseIntentDraft 释放草稿槽`,
    );
  }
  // 释放口本身必须认令牌,否则上面的收敛就是空的。
  assert.deepEqual(
    comparisonsIn(findFunction(askSession, "releaseIntentDraft"))
      .filter((item) => item.left.includes("askIntentDraftOwnerRef.current"))
      .map((item) => [item.operator, item.right]),
    [["!==", "token"]],
  );

  // 拒收路径必须 return 而不是继续往下跑 —— 否则调用方拿到的返回值再准也没用。
  // (返回的是不是 false 这一层静态守卫钉不住;`Promise<boolean>` 的返回类型让
  //  裸 `return;` 直接过不了 tsc,而「拒收后返回 true」只能靠评审与上面这条
  //  调用方守卫兜住。)
  // 收尾只能按流程状态判断自己该不该收在途 turn。用「草稿 ref 还非空」当阶段
  // 标记会误伤真正在跑的 ask:新会话第一轮的 started 回调 setConversationId 会
  // 触发切库/切会话那个 effect,而草稿要留到 executeAsk 确认接下为止 ——
  // 于是整轮生成期间问题气泡和轨迹一起消失(codex 第 6 轮 P2)。
  const phase = variableInitializersIn(findFunction(askSession, "abortIntentPreview"))
    .find((item) => item.name === "wasIntentPhase");
  assert.ok(phase, "abortIntentPreview 的阶段判据不见了");
  assert.match(phase.initializer, /askIntentFlowRef/);
  assert.doesNotMatch(phase.initializer, /askIntentDraftRef/);
  // 两个理解阶段都要覆盖:preview 是预检在途,review 是等用户补充澄清 ——
  // 漏掉 review,用户在确认卡片上切库/切会话时在途 turn 会残留在新会话里。
  for (const stage of ["preview", "review"]) {
    assert.match(phase.initializer, new RegExp(`"${stage}"`), `阶段判据漏了 ${stage}`);
  }

  const rejections = ifBranchesIn(findFunction(askSession, "executeAsk"))
    .filter((branch) => /askUnavailable|scopeBlocked|requiresKg/.test(branch.condition));
  assert.equal(rejections.length, 2, "executeAsk 的两道可用性守卫不见了");
  for (const branch of rejections) {
    assert.ok(branch.thenReturns, `${branch.condition} 拒收后没有 return`);
    assert.ok(branch.thenCalls.includes("effectsRef.current.notify"), `${branch.condition} 没有告知用户`);
  }
});
