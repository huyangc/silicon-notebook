// 长任务按钮忙碌态守卫 —— 防止「点完还能接着点」的形态回潮。
//
// 背景:用户报的是看板「来源状态」里的「补齐向量」——点完 POST 立刻返回、修复在后台
// 跑,按钮却纹丝不动,于是被反复点,每点一次后端就再排一份同样的活(backfill-vectors /
// reparse 后端都**没有**单飞守卫)。同一类问题在 page.tsx 里散落多处,本守卫把它们钉住。
//
// 判据用 onClick 绑定的**源码文本**作身份,不用行号:按钮在文件里上下挪动不该报红(那不是
// 退化),把 disabled 摘掉才该报红。这也是为什么下面每条都断言「所有 onClick 命中该模式的
// button」而不是「第 N 个 button」——新增一个走同一入口的按钮会自动被纳入,不会漏网。
//
// ⚠ 刻意不覆盖的合法反例(别看到 disabled=null 就来加):
//   · 「索引与构建」正常态那两颗 runScaleIndexOp("rebuild") —— 那一格用的是**另一种**同样
//     有效的形态:忙碌时整排 CTA 换成「取消」,按钮根本不渲染。没有 disabled 是对的。
//     (H8 损坏态那一格常驻显示告警、不换排,所以它必须自带 disabled,下面有断言。)
//   · startKgBuild / startKgRebuild 在看板里同样走「忙碌时整排不渲染」。侧栏与图谱视图里
//     的那几颗才是 disabled 形态,已由既有测试与本文件外的人工评审覆盖。
//
// 覆盖边界(如实说明,不声称全覆盖):本守卫只钉「已知的这几个长任务入口」。**新增**一个
// 长任务按钮却忘了带 disabled,不撞任何一条断言,仍是假阴性——那类兜底是 CLAUDE.md 的
// 工程约束 + 代码评审,不是这份测试。
import test from "node:test";
import assert from "node:assert/strict";

import { jsxElements, parseModule, variableInitializersIn } from "../../test-support/semantic-source.mjs";
import { CHECKUP_FIX, CHECKUP_FIX_BUSY } from "../../app/vocabulary.ts";

// onClick 源码文本里能唯一认出这个入口的片段 → 这个入口是什么、为什么必须禁用。
//
// `requires` 是给**复合** disabled 表达式用的加固:光断言「disabled 非平凡」拦不住
// 「保留了别的条件、只把在飞那一项摘掉」——那种改动 disabled 仍然非平凡,守卫会假绿
// (实测:把 `reviewAllStarting ||` 删掉,只有 requires 这条能抓到)。所以凡是 disabled
// 由多个条件或起来的入口,都必须把那个**在飞标志**的名字钉在这里。
const LONG_TASK_BUTTONS = [
  { match: "runFix(", why: "体检修复 CTA(补齐向量/重新解析/分析新增):后端无单飞,重复点=重复排活" },
  { match: "relinkFromKgView", why: "补上关联:后台任务,忙碌位由 relink/status 轮询解除,期间不能再点", requires: "kgGraph.relinking" },
  { match: 'runScaleIndexOp("rebuild", bumpCheckupRepairPoll)', why: "H8 损坏态重建索引:该格常驻显示,不走「忙碌换取消」" },
  { match: "confirmUpload(", why: "上传:multipart 传大文件期间不能重复提交", requires: "uploadBusy" },
  { match: "reparseSource(", why: "来源重新解析:同步等完,大 PDF 可能数分钟" },
  { match: "decideMerge(", why: "待确认合并落决定:确认分支连带跑全量重建,两颗按钮都需防重复提交", requires: "kgGraph.rebuilding" },
  { match: "runFindDuplicates(", why: "查重:全库归一化比对,大库不是瞬时的" },
  { match: "runMerge(", why: "重复条目合并:连带重拉列表/类型统计并重跑一次查重" },
  { match: "reviewAllMerges", why: "全部自动判重:POST 在飞期间也不能再点(job id 还没回来)", requires: "kgGraph.reviewAllStarting" },
];

// 长任务入口的另一种形态：**file input**。外层 label 无法 :disabled,所以禁用位落在
// input 自己身上,身份用它绑定的 accept 表达式认(与 source-drop-zone-guard 同一身份)。
// 这类入口没有 onClick,上面那张表按 onClick 文本匹配,永远覆盖不到它。
//
// 为什么它算长任务:选中/拖入一个 zip 或文件夹后要跨 await 解包、遍历目录、内联图片,
// 还可能停下来等用户勾选要添加哪几个 markdown。期间不禁用,第二个包的勾选就会覆盖第一个
// 还没确认的选择(resolver 被替换 → 上一条链永久挂起 → 忙碌位再也不释放)。
const LONG_TASK_FILE_INPUTS = [
  {
    accept: "supportedSourceAccept",
    why: "添加来源的文件选择器:zip/文件夹解包跨 await,期间必须禁用整个入口",
    // disabled 绑的是派生常量,所以在它的**初始化表达式**里找在飞标志(解一层引用)。
    requires: "bundleProcessing",
  },
];

// disabled 存在但恒假(`false` / `undefined` / `null`)等于没写——这类「假装修好」也要报红。
const TRIVIALLY_FALSE = new Set(["false", "undefined", "null"]);

function buttonsMatching(elements, match) {
  return elements.filter((element) => {
    const onClick = element.bindings?.onClick ?? element.attributes?.onClick ?? "";
    return onClick.includes(match);
  });
}

test("page.tsx 的长任务按钮都带非平凡的 disabled(点完不能再点)", async () => {
  const page = await parseModule("page.tsx");
  const buttons = jsxElements(page, "button");
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
      } else if (entry.requires && !String(disabled).includes(entry.requires)) {
        offenders.push(`${entry.match}：disabled=${disabled} 里没有在飞标志 ${entry.requires} —— ${entry.why}`);
      }
    }
  }

  assert.deepEqual(offenders, []);
});

test("page.tsx 的长任务 file input 也带非平凡的 disabled，且表达式里含在飞标志", async () => {
  const page = await parseModule("page.tsx");
  const inputs = jsxElements(page, "input");
  const initializers = new Map(
    variableInitializersIn(page).map((item) => [item.name, item.initializer]),
  );
  const offenders = [];

  for (const entry of LONG_TASK_FILE_INPUTS) {
    const matched = inputs.filter(
      (element) => (element.bindings?.accept ?? element.attributes?.accept) === entry.accept,
    );
    if (matched.length === 0) {
      offenders.push(`${entry.accept}：没找到任何 file input（入口被改名或删除？守卫失效）`);
      continue;
    }
    for (const element of matched) {
      const disabled = element.bindings?.disabled ?? element.attributes?.disabled;
      if (disabled === undefined) {
        offenders.push(`${entry.accept}：缺 disabled —— ${entry.why}`);
        continue;
      }
      const expression = String(disabled).trim();
      if (TRIVIALLY_FALSE.has(expression)) {
        offenders.push(`${entry.accept}：disabled=${expression} 恒假，等于没写 —— ${entry.why}`);
        continue;
      }
      // 解一层变量引用：disabled 通常绑的是派生常量而不是内联表达式。
      const resolved = `${expression} ${initializers.get(expression) ?? ""}`;
      if (!resolved.includes(entry.requires)) {
        offenders.push(
          `${entry.accept}：disabled=${expression}（=${initializers.get(expression) ?? "?"}）`
          + ` 里没有在飞标志 ${entry.requires} —— ${entry.why}`,
        );
      }
    }
  }

  // 在飞标志本身必须真的由两个信号组成：解包在飞、以及正等待用户勾选。少了后者，
  // 勾选期间入口会重新可点，第二个包就能覆盖还没确认的第一次选择。
  const processing = initializers.get("bundleProcessing") ?? "";
  if (!processing.includes("bundleBusyLabel")) {
    offenders.push(`bundleProcessing 不再包含解包在飞信号 bundleBusyLabel：${processing}`);
  }
  if (!processing.includes("bundleChoice")) {
    offenders.push(`bundleProcessing 不再包含勾选等待信号 bundleChoice：${processing}`);
  }

  assert.deepEqual(offenders, []);
});

// 站外来源建议的「导入」按钮(ask.gap_consult,X9 PR-A T3)不在 page.tsx 里——它是
// answer-gap-suggestions.tsx 里独立组件的按钮,上面那两条测试按文件名钉死解析
// page.tsx,天生看不到它。同一条理由(后端 POST /sources/url 没有单飞守卫,点完不
// 禁用就是重复排入同一个链接),同一套判据(TRIVIALLY_FALSE),只是换一个要解析的
// 文件。
test("answer-gap-suggestions.tsx 的导入按钮同样带非平凡的 disabled(点完不能再点)", async () => {
  const module = await parseModule("answer-gap-suggestions.tsx");
  const buttons = jsxElements(module, "button");
  const importButtons = buttonsMatching(buttons, "handleImport(");
  // 匹配为 0 说明入口被改名/删了——同上面几条一样,必须响亮失败。
  assert.ok(
    importButtons.length > 0,
    "没找到导入按钮（handleImport(...) 入口被改名或删除？守卫失效）",
  );
  const offenders = [];
  for (const element of importButtons) {
    const disabled = element.bindings?.disabled ?? element.attributes?.disabled;
    if (disabled === undefined) {
      offenders.push("导入按钮缺 disabled —— 后端无单飞守卫，重复点=重复排入同一个链接来源");
    } else if (TRIVIALLY_FALSE.has(String(disabled).trim())) {
      offenders.push(`导入按钮 disabled=${disabled} 恒假，等于没写`);
    }
  }
  assert.deepEqual(offenders, []);
});

test("每个体检修复动作都有配套的进行态文案（否则按钮禁用了却仍显示原文案）", () => {
  // 用户的要求是「变成的内容要按原按钮的功能来」——所以进行态是一张与 CHECKUP_FIX 一一
  // 对应的表,不是一句通用的「处理中」。少一个键,那个动作就会禁用着却还写「补齐向量」。
  const missing = Object.keys(CHECKUP_FIX).filter((fix) => !Object.hasOwn(CHECKUP_FIX_BUSY, fix));
  assert.deepEqual(missing, []);
});

test("进行态文案与静态文案逐条不同，且都是界面词（不出现内部词/英文动作名）", () => {
  for (const [fix, idle] of Object.entries(CHECKUP_FIX)) {
    const busy = CHECKUP_FIX_BUSY[fix];
    assert.notEqual(busy, idle, `${fix} 的进行态与静态文案相同，按钮点完看不出变化`);
    assert.ok(/[一-龥]/.test(busy), `${fix} 的进行态文案不含中文：${busy}`);
    assert.ok(!busy.includes(fix), `${fix} 的进行态文案泄漏了内部枚举名：${busy}`);
  }
});
