// 异常提示分级守卫(anomaly-tiers spec)——防止来源异常提示的手搓形态回潮。
//
// 扫描范围是下面那份 allowlist(page.tsx + 「活动」视图的四个文件),不是单个文件,
// 也不是全仓 —— 逐条理由见 ANOMALY_SOURCE_FILES 上方那段。
//
// 范围收窄(评审 P2-1 定案,别踩这个坑):page.tsx 里 `var(--color-warn, ...)`
// 这个 token 在分析统计弹窗 + 检索索引状态提示两处是**合法**复用的(共 7 处,
// 与本约束无关的历史场景)。朴素全文件 grep `var(--color-warn` 会把这些合法
// 用法一起打成假红,所以本守卫只断言 Task A 删除的、来源异常落点**独有**的
// 遗留形态,不动 `--color-warn`(不带 ing)这个共用 token:
//
//   1. 裸 ⚠/⚠️/⛔ glyph——allowlist 里任何字符串字面量/JSX 文本节点都不该再有。
//      这些文件里现存的 ⚠ 全在注释中;AST 解析天然跳过注释,不会误伤。
//   2. `--color-warning` 系(含 -bg/-border 后缀)——未定义 token 的 fallback
//      引用,在这些文件里从来没有合法用法(区别于上面共用的 --color-warn,
//      故意不带 "ing" 去匹配它)。
//   3. `.extraction_warning` 字段被**渲染点**直接属性访问——唯一合法消费方式是把
//      整个 source/sourceDetail/ActivitySource 对象交给 sourceAnomalies()
//      (anomaly-severity.ts),不该再被内联判断触发裸样式。唯一的例外是
//      source-view.tsx 那个 wire→ActivitySource 的映射点,见下方说明。
//   4. `paper_meta_status === "missing"` / `paper_meta_status === "not_paper"`
//      字面量比较式——同样只能经 sourceAnomalies() 消费。
//
// 单靠上面 4 条否定式断言,仍抓不住"换一个从没出现过的新 token/新写法手搓
// 样式"这类退化(比如内联 `color:"#8a5a00"` 十六进制,不复用任何已知 token 名)。
// 最后两组正向计数断言(page.tsx 一组、活动视图一组)只覆盖其中的**替换**分支:
// 把已有的一处 AnomalyBadge / <SourceAnomalies> 换成等价内联样式,那个文件的渲染
// 次数会掉一档,被计数断言逮到。它抓不住**全新落点**这种子情形——在别处新增一段
// 手搓样式渲染一个新异常,计数不降、也不撞前 4 条黑名单,理论上仍是假阴性。这类
// 新落点的兜底不是这份测试,是 docs/product-and-api*.md 的异常分级约定 + 人工代码
// 评审,如实说明而非声称全覆盖。
import test from "node:test";
import assert from "node:assert/strict";

import {
  callsIn,
  comparisonsIn,
  jsxElements,
  jsxTextValues,
  parseModule,
  propertyAccesses,
  stringLiterals,
} from "../../test-support/semantic-source.mjs";

// 落点不只 page.tsx:「活动」视图(dev/logs 的用户活动流)三栏各有一个来源异常小字的
// 渲染点,共用 source-view.tsx 里的 <SourceAnomalies>。守卫因此按 allowlist 扫,而不是
// 单个文件——否则往活动视图里塞一个裸 ⚠ 或内联颜色样式,整套门禁不会红。
//
// ⚠ 哪几条对新文件生效是逐条判断过的,不是照搬(照搬会假红):
//   · 裸 glyph / --color-warning / paper_meta_status 字面量比较:与文件无关的**形态
//     黑名单**,全表生效。活动侧只搬运 paper_meta_status 字段、不比较它,不会误伤。
//   · .extraction_warning 直接属性访问:**排除 source-view.tsx**。它是「wire 形状 →
//     ActivitySource」的唯一映射点(toActivitySource),那次读取恰恰是把整个对象喂给
//     sourceAnomalies() 的前提,算成违规就是假红。其余渲染点必须整体交出对象。
//   · 计数断言:page.tsx 的 2/2 是它自己的渲染结构,照搬到别的文件没有意义。活动侧
//     另立一组等价计数(见文件末尾那条),判据完全相同——把某一栏的 <SourceAnomalies>
//     换成手搓样式,那个文件的计数就从 1 掉到 0。
const ANOMALY_SOURCE_FILES = [
  "page.tsx",
  "dev/logs/activity/source-view.tsx",
  "dev/logs/activity/ActivityScopePanel.tsx",
  "dev/logs/activity/ActivityStream.tsx",
  "dev/logs/activity/ActivityDetail.tsx",
];

// 三栏各自的渲染点。source-view.tsx 不在其中——它是被渲染的那条**共享路径本身**。
// ⚠ ActivityStream.tsx(中栏活动流)必须在表里:漏掉它,整条中栏就是守卫的盲区,而它
// 恰是来源异常小字最常被看到的地方。
const ACTIVITY_ANOMALY_RENDERERS = [
  "dev/logs/activity/ActivityScopePanel.tsx",
  "dev/logs/activity/ActivityStream.tsx",
  "dev/logs/activity/ActivityDetail.tsx",
];

async function offendersAcross(collect) {
  const offenders = [];
  for (const file of ANOMALY_SOURCE_FILES) {
    const module = await parseModule(file);
    for (const value of collect(module)) {
      offenders.push(`${file}: ${value}`);
    }
  }
  return offenders;
}

test("来源异常落点里不再出现裸 ⚠/⚠️/⛔ glyph(字符串字面量 + JSX 文本节点，注释天然排除)", async () => {
  const offenders = await offendersAcross((module) => (
    [...stringLiterals(module), ...jsxTextValues(module)]
      .filter((value) => /[⚠⛔]/.test(value))
  ));
  assert.deepEqual(offenders, []);
});

test("来源异常落点里不再引用未定义的 --color-warning 系 token(区别于合法共用的 --color-warn)", async () => {
  const offenders = await offendersAcross((module) => (
    stringLiterals(module).filter((value) => value.includes("--color-warning"))
  ));
  assert.deepEqual(offenders, []);
});

test("渲染点不再直接属性访问 .extraction_warning，必须整体交给 sourceAnomalies", async () => {
  // source-view.tsx 排除在外:见文件顶部 allowlist 那段说明——它读这一个字段是为了
  // 把整个对象折成 ActivitySource,而不是为了内联判断触发裸样式。
  const offenders = [];
  for (const file of ANOMALY_SOURCE_FILES) {
    if (file === "dev/logs/activity/source-view.tsx") continue;
    const module = await parseModule(file);
    for (const value of propertyAccesses(module)) {
      if (value.endsWith(".extraction_warning")) offenders.push(`${file}: ${value}`);
    }
  }
  assert.deepEqual(offenders, []);
});

test("来源异常落点里不再对 paper_meta_status 做 missing/not_paper 比较(AST 二元表达式，不被引号/空格绕过)", async () => {
  // 早期版本用 page.getText().includes('paper_meta_status === "missing"') 做
  // 原始子串匹配——单引号 `=== 'missing'` 或去空格 `==="missing"` 语义相同却
  // 能滑过。改成扫二元表达式:操作符限定 ===/==，左侧属性访问以
  // .paper_meta_status 结尾，右侧字符串字面量取 comparisonsIn() 已经解出的
  // 不含引号的 .text，天然对引号风格/空格免疫。has_meta 分支(page.tsx:5029，
  // 来源详情展示已补全的论文元数据，合法保留)右值是 "has_meta"，不在
  // missing/not_paper 名单内，不会被这条断言误伤。
  const offenders = await offendersAcross((module) => (
    comparisonsIn(module)
      .filter((comparison) => (
        (comparison.operator === "===" || comparison.operator === "==")
        && comparison.left.endsWith(".paper_meta_status")
        && (comparison.right === "missing" || comparison.right === "not_paper")
      ))
      .map((comparison) => JSON.stringify(comparison))
  ));
  assert.deepEqual(offenders, []);
});

test("page.tsx:sourceAnomalies 只在来源行+来源详情各调用一次，AnomalyBadge 只渲染这两处", async () => {
  // 计数断言刻意保持严格的 === 2(不放宽成 >= 1):move 变异(把某处 AnomalyBadge
  // 换成等价内联样式)正是靠 2→1 的计数下降被抓到,放宽会打穿这个能力。已知
  // 脆性:若未来把来源行/详情两处的渲染抽成共享 helper 内部复用同一个
  // <AnomalyBadge>,这里的计数需要同步改成 1——那不是本守卫失败,是渲染结构
  // 变了,改测试前先确认改动确实是这种重构而非静默丢弃了一处渲染。
  const page = await parseModule("page.tsx");
  const sourceAnomaliesCalls = callsIn(page).filter((name) => name === "sourceAnomalies");
  assert.equal(sourceAnomaliesCalls.length, 2, "sourceAnomalies 调用次数漂移，检查是否有落点绕开了唯一渲染路径");
  const badgeElements = jsxElements(page, "AnomalyBadge");
  assert.equal(badgeElements.length, 2, "AnomalyBadge 渲染次数漂移，检查是否有徽标被换成了手搓内联样式");
});

test("活动视图三栏都只经共享的 <SourceAnomalies> 渲染异常小字", async () => {
  // page.tsx 那条计数断言的活动视图版本,判据完全相同:把某一栏的 <SourceAnomalies>
  // 换成手搓的内联样式,那个文件的计数就从 1 掉到 0(move 变异)。所以三条断言都是
  // 严格相等,不放宽成 >= 0/>= 1。
  //
  // 共享路径本身只有一处:source-view.tsx::SourceAnomalies 里 sourceAnomalies() 与
  // <AnomalyBadge> 各恰好一次。
  const shared = await parseModule("dev/logs/activity/source-view.tsx");
  assert.equal(
    callsIn(shared).filter((name) => name === "sourceAnomalies").length,
    1,
    "source-view.tsx 里 sourceAnomalies 调用次数漂移",
  );
  assert.equal(
    jsxElements(shared, "AnomalyBadge").length,
    1,
    "source-view.tsx 里 AnomalyBadge 渲染次数漂移",
  );

  for (const file of ACTIVITY_ANOMALY_RENDERERS) {
    const module = await parseModule(file);
    assert.equal(
      jsxElements(module, "SourceAnomalies").length,
      1,
      `${file}: <SourceAnomalies> 渲染次数漂移，检查这一栏的异常小字是否被换成了手搓样式`,
    );
    // 绕开共享件、在这一栏里自己拼一个徽标同样不行(那正是「新落点」这个子情形)。
    assert.equal(
      jsxElements(module, "AnomalyBadge").length,
      0,
      `${file}: 直接渲染了 AnomalyBadge，异常小字必须经 <SourceAnomalies>`,
    );
    assert.equal(
      callsIn(module).filter((name) => name === "sourceAnomalies").length,
      0,
      `${file}: 直接调用了 sourceAnomalies，异常小字必须经 <SourceAnomalies>`,
    );
  }
});
