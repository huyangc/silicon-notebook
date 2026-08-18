// 命令目录长任务按钮的忙碌态守卫 —— 与 long-task-button-guard.test.mjs 同一条红线,
// 只是钉的是本特性自己的模块(那个文件的扫描面写死了 page.tsx)。
//
// 为什么这几颗按钮属于「长任务」:
//   · 「识别命令目录」排的是后台任务,一份手册要跑几十次模型调用、几分钟起步。POST
//     返回 ≠ 做完。后端确实有单飞守卫(重复发起 409),所以重复点不会重复花钱,但
//     按钮纹丝不动会让用户以为没点上、接着点 —— 界面必须当场表态。
//   · 「确认所选 / 确认全部待审阅」写 knowhow 表并触发重投影,后端**没有**单飞守卫,
//     重复提交就是重复写。
//   · 「跳过所选 / 跳过全部待审阅」(R7)改候选的持久状态(state → dismissed),
//     后端同样**没有**单飞守卫,重复提交就是重复写(虽然是幂等的状态转移,但
//     按钮不表态用户仍会反复点、反复发请求)。
//   · 「取消」「加载更多」在飞期间同样不能再点。
//
// 判据用 onClick 绑定的**源码文本**作身份,不用行号:按钮在文件里上下挪动不该报红,
// 把 disabled 摘掉才该报红。`requires` 钉住那个「在飞标志」的名字 —— 光断言 disabled
// 非平凡拦不住「保留了别的条件、只把在飞那一项摘掉」(实测:把 `busy ||` 删掉,disabled
// 仍非平凡,只有 requires 这条抓得到)。
import test from "node:test";
import assert from "node:assert/strict";

import { declarations, importsFrom, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";

const panel = await parseModule("command-catalog-panel.tsx");
const page = await parseModule("page.tsx");
const model = await parseModule("command-catalog-model.ts");

const LONG_TASK_BUTTONS = [
  {
    match: "requestRun(",
    why: "发起识别:后台任务跑数分钟,按钮不表态用户会反复点",
    requires: "busy",
  },
  {
    match: "requestCancel(",
    why: "取消:POST 在飞期间不能再点(worker 要到下一个分片边界才停)",
    requires: "cancelling",
  },
  {
    match: "applySelected",
    why: "确认所选:写 knowhow 表 + 触发重投影,后端无单飞守卫",
    requires: "applying",
  },
  {
    match: "runApply({ all_pending: true })",
    why: "确认全部待审阅:同上,且一次最多一页、要循环点",
    requires: "applying",
  },
  {
    match: "dismissSelected",
    why: "跳过所选(R7):改候选持久状态,后端无单飞守卫",
    requires: "dismissing",
  },
  {
    match: "runDismiss({ all_pending: true })",
    why: "跳过全部待审阅(R7):同上,且一次最多一页、要循环点",
    requires: "dismissing",
  },
  {
    match: "loadPage(tab, cursor, true)",
    why: "加载更多:分页请求在飞期间再点会把同一页 append 两遍",
    requires: "loading",
  },
];

// disabled 存在但恒假(`false` / `undefined` / `null`)等于没写 —— 这类「假装修好」也报红。
const TRIVIALLY_FALSE = new Set(["false", "undefined", "null"]);

function buttonsMatching(elements, match) {
  return elements.filter((element) => {
    const onClick = element.bindings?.onClick ?? element.attributes?.onClick ?? "";
    return onClick.includes(match);
  });
}

test("命令目录的长任务按钮都带非平凡的 disabled，且钉住各自的在飞标志", () => {
  const buttons = jsxElements(panel, "button");
  const offenders = [];

  for (const entry of LONG_TASK_BUTTONS) {
    const matched = buttonsMatching(buttons, entry.match);
    // 匹配为 0 说明入口被改名/删了 —— 也算失败:守卫必须响亮失败,不能静默变成空断言。
    if (matched.length === 0) {
      offenders.push(`${entry.match}：没找到任何按钮（入口被改名或删除？守卫失效）`);
      continue;
    }
    for (const element of matched) {
      const disabled = element.bindings?.disabled ?? element.attributes?.disabled;
      if (disabled === undefined) {
        offenders.push(`${entry.match}：缺 disabled —— ${entry.why}`);
      } else if (TRIVIALLY_FALSE.has(String(disabled).trim())) {
        offenders.push(`${entry.match}：disabled=${disabled} 恒假，等于没写 —— ${entry.why}`);
      } else if (!String(disabled).includes(entry.requires)) {
        offenders.push(`${entry.match}：disabled=${disabled} 里没有在飞标志 ${entry.requires} —— ${entry.why}`);
      }
    }
  }

  assert.deepEqual(offenders, []);
});

test("每个长任务按钮的进行态文案按各自动作写，不是笼统的「处理中」", () => {
  // 用户的要求是「变成的内容要按原按钮的功能来」——所以这里逐条钉,不接受一句通用文案。
  const source = panel.getFullText();
  for (const busy of ["识别中…", "确认中…", "跳过中…", "取消中…", "加载中…"]) {
    assert.ok(source.includes(busy), `缺进行态文案：${busy}`);
  }
  assert.ok(
    !source.includes("处理中…"),
    "出现了笼统的「处理中…」——进行态必须按各自动作的语义写",
  );
});

// —— 移动变异守卫 ——————————————————————————————————————————————————————————
//
// 「删掉 disabled」不是这类回退唯一的形状。另一种同样常见:把忙碌判据从有单测的
// 纯函数里**搬进**组件内联(看着像化简),于是判据还在、单测却再也测不到它,下一次
// 有人把「终态才解除」改成「POST 返回即解除」不会有任何东西报红。所以判据的**唯一
// 声明点**必须钉死在 command-catalog-model.ts。
test("忙碌态判据只许住在有单测的纯函数模块里（搬进组件即报红）", () => {
  const declared = (module, name) => declarations(module).some((finding) => finding.name === name);
  for (const name of ["isCatalogBusy", "isCatalogSettled"]) {
    assert.ok(declared(model, name), `${name} 应声明在 command-catalog-model.ts`);
    assert.ok(!declared(panel, name), `${name} 被搬进了 command-catalog-panel.tsx（判据会脱离单测）`);
    assert.ok(!declared(page, name), `${name} 被搬进了 page.tsx（判据会脱离单测）`);
  }
  const imported = importsFrom(panel, "./command-catalog-model.ts").map((item) => item.imported);
  assert.ok(imported.includes("isCatalogBusy"), "面板没有从模型模块取忙碌判据");
  assert.ok(imported.includes("isCatalogSettled"), "面板没有从模型模块取终态判据");
});

test("入口真的挂在来源详情上（工作区不引用面板就等于这个特性不存在）", () => {
  const imported = importsFrom(page, "./command-catalog-panel").map((item) => item.imported);
  assert.ok(imported.includes("CommandCatalogSection"), "page.tsx 没有引入命令目录入口");
  const mounted = jsxElements(page, "CommandCatalogSection");
  assert.equal(mounted.length, 1, "命令目录入口应在来源详情里挂载恰好一次");
  // 参考库来源是只读的:发起/取消/确认在后端按 `catalog:write` 能力收窄且绑当前
  // notebook。入口渲染条件必须带上这条,否则会对一个只是被挂载进来的库发起识别。
  //
  // ⚠ 判据从 `!isReader` 换成 `!readOnlyWorkspace`(群组知识共享 P2):`catalog:write`
  // 已从 owner-only 翻成「owner ∪ 组管理边」,而 `access` 仍是 "reader"——继续按
  // access 判会把组管理员挡在一个后端明明放行的入口外面。`readOnlyWorkspace` 由
  // `workspaceCapabilities` 派生,是页面里内容管理入口的唯一判据。
  const [element] = mounted;
  assert.equal(
    element.bindings?.canEdit,
    "!readOnlyWorkspace",
    "纯只读成员不该拿到发起/确认能力（判据必须是内容管理权,不是 access）",
  );

  // 授权门断言:命令目录必须绑定**当前活跃笔记本** id,不能落到来源自己的
  // notebook_id(来源可能属于被挂载进来的参考库)。
  assert.equal(
    element.bindings?.notebookId,
    "currentNotebookId",
    "命令目录必须绑定当前笔记本 id，不能落到来源自己的 notebook_id",
  );

  // 授权门断言:挂载条件里必须含 `!sourceDetailBaseId`——这份来源属于当前库自己、
  // 不是被挂载进来的参考库,否则会对一个只是被挂载进来的库发起识别、花那个库的钱、
  // 写那个库的知识。jsxElements 不解析包裹的 JSX 条件表达式,这里按源码文本判定
  // (风格对齐本文件其余用例)。
  const source = page.getFullText();
  const anchor = source.indexOf("<CommandCatalogSection");
  assert.ok(anchor > 0, "找不到命令目录入口的挂载点");
  const before = source.slice(Math.max(0, anchor - 400), anchor);
  assert.ok(
    before.includes("!sourceDetailBaseId"),
    "挂载条件里没有 !sourceDetailBaseId —— 会对挂载库来源发起识别（授权语义错误）",
  );
});

// —— P0:审阅弹窗必须挂在 page 根层,不在来源详情子树内 ————————————————————
//
// 根因:来源详情弹窗(SourceDetailWindow)自己就是一张 FloatingModalCard,桌面态
// 恒给卡片 translate3d(...),卡片因此是它内部一切 position:fixed 后代的包含块。
// 审阅弹窗(CommandCatalogReview)同样是 920px 宽的 FloatingModalCard,塞进 740px
// 的来源详情卡片会被 overflow:hidden 裁掉,确认按钮/底栏整段看不见——两次独立
// 评审都实测到了这个问题。修法是把审阅弹窗的开关状态提到 page.tsx 根层渲染,
// 与成本预告 confirmCommandCatalog/infoModal 同一种「调用方持有开关、根层渲染」
// 形状。这里按源码文本断言两件事:入口渲染只请求打开(不再自己渲染审阅弹窗)、
// 审阅弹窗在 page.tsx 里的挂载点落在 SourceDetailWindow 的开闭标签之外。
test("审阅弹窗提升到 page 根层挂载，不再嵌在来源详情子树里（P0，两次评审独立确认）", () => {
  const source = page.getFullText();
  const openIdx = source.indexOf("<SourceDetailWindow");
  const closeIdx = source.indexOf("</SourceDetailWindow>");
  assert.ok(openIdx >= 0 && closeIdx > openIdx, "找不到来源详情弹窗的边界，守卫失效");

  const reviewImported = importsFrom(page, "./command-catalog-panel")
    .map((item) => item.imported);
  assert.ok(reviewImported.includes("CommandCatalogReview"), "page.tsx 没有引入审阅弹窗组件");

  const reviewMounted = jsxElements(page, "CommandCatalogReview");
  assert.equal(reviewMounted.length, 1, "审阅弹窗应在 page.tsx 里挂载恰好一次");

  const reviewIdx = source.indexOf("<CommandCatalogReview");
  assert.ok(reviewIdx >= 0, "找不到审阅弹窗的挂载点");
  assert.ok(
    reviewIdx < openIdx || reviewIdx > closeIdx,
    "审阅弹窗被挂在来源详情子树内——会被来源详情卡片的 transform 包含块裁掉（P0 回归）",
  );

  // 入口本身不该再自己渲染审阅弹窗——它只应经 onOpenReview 请求打开。
  const sectionMounted = jsxElements(page, "CommandCatalogSection");
  const [sectionElement] = sectionMounted;
  assert.ok(
    sectionElement.bindings?.onOpenReview !== undefined,
    "入口没有把 onOpenReview 接到调用方——回退成自己渲染弹窗就测不出这条",
  );
});

// —— R8:审阅动作必须回流到入口卡片(codex PR #412 评审 P1)————————————————
//
// 根因与上面那条 P0 同源:审阅弹窗提升到 page 根层之后,它与入口卡片之间只剩
// page.tsx 这一条接线。卡片的 job 快照带着「重新识别」的拦截判据
// (`progress.pending_candidates`),审阅者把候选全部确认/跳过完,后端那份 job 早
// 已归零,而卡片手里仍是旧快照——按钮永久禁用,直到用户重开来源详情。
//
// 组件测试里的 harness 复刻了这条接线,所以行为本身有覆盖;它覆盖不到的是
// **page.tsx 有没有真的接**。摘掉任一端,harness 照样绿,生产照样瘫——这正是
// 「加了守卫 ≠ 有效」那条红线要求做移动/删除变异的地方。所以这里按 AST 断言两端
// 都在,且接的是同一个 state。
test("审阅动作回流到入口卡片：page.tsx 两端都接上且共用同一份状态（R8）", () => {
  const [section] = jsxElements(page, "CommandCatalogSection");
  const [review] = jsxElements(page, "CommandCatalogReview");
  assert.ok(section, "找不到命令目录入口的挂载点");
  assert.ok(review, "找不到审阅弹窗的挂载点");

  const seqBinding = section.bindings?.reviewSeq;
  assert.ok(
    seqBinding,
    "入口没有接 reviewSeq——审阅完最后一条候选后「重新识别」会一直禁用",
  );
  const reviewedBinding = review.bindings?.onReviewed;
  assert.ok(
    reviewedBinding,
    "审阅弹窗没有接 onReviewed——确认/跳过之后入口卡片永远不会重读 job",
  );

  // 两端必须共用同一份 state:光各接一个不相干的值就等于没接。名字本身不重要,
  // 「setter 推进的就是入口读的那一个」才重要,所以按 `<name>` / `set<Name>` 这
  // 对约定比对,而不是硬编码某个标识符。
  const stateName = String(seqBinding).trim();
  const setterName = `set${stateName[0].toUpperCase()}${stateName.slice(1)}`;
  assert.ok(
    String(reviewedBinding).includes(setterName),
    `onReviewed 没有推进入口读的那份状态(${stateName});`
      + " 两端各接各的等于这条回流线断了",
  );
  // 该 state 必须真的在 page.tsx 里声明过(否则上面两条都可能是笔误对上了)。
  const source = page.getFullText();
  assert.ok(
    source.includes(`const [${stateName}, ${setterName}] = useState(`),
    `page.tsx 里找不到 ${stateName} 的 useState 声明`,
  );
});
