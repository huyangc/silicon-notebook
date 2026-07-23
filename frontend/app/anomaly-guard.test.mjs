// 异常提示分级守卫(anomaly-tiers spec)——防止来源异常提示的手搓形态回潮。
//
// 范围收窄(评审 P2-1 定案,别踩这个坑):page.tsx 里 `var(--color-warn, ...)`
// 这个 token 在分析统计弹窗 + 检索索引状态提示两处是**合法**复用的(共 7 处,
// 与本约束无关的历史场景)。朴素全文件 grep `var(--color-warn` 会把这些合法
// 用法一起打成假红,所以本守卫只断言 Task A 删除的、来源异常落点**独有**的
// 遗留形态,不动 `--color-warn`(不带 ing)这个共用 token:
//
//   1. 裸 ⚠/⚠️/⛔ glyph——page.tsx 任何字符串字面量/JSX 文本节点里都不该再有。
//      当前唯一出现处是 439 行一条注释;AST 解析天然跳过注释,不会误伤。
//   2. `--color-warning` 系(含 -bg/-border 后缀)——未定义 token 的 fallback
//      引用,在整个 page.tsx 里从来没有合法用法(区别于上面共用的 --color-warn,
//      故意不带 "ing" 去匹配它)。
//   3. `.extraction_warning` 字段被 page.tsx 直接属性访问——现在唯一合法消费
//      方式是把整个 source/sourceDetail 对象交给 sourceAnomalies()
//      (anomaly-severity.ts),不该再被内联判断触发裸样式。
//   4. `paper_meta_status === "missing"` / `paper_meta_status === "not_paper"`
//      字面量比较式——同样只能经 sourceAnomalies() 消费。
//
// 单靠上面 4 条否定式断言,仍抓不住"换一个从没出现过的新 token/新写法手搓
// 样式"这类退化(比如内联 `color:"#8a5a00"` 十六进制,不复用任何已知 token 名)。
// 最后一组正向计数断言只覆盖其中的**替换**分支:把已有的一处 AnomalyBadge
// 换成等价内联样式,渲染次数会从 2 掉到 1,被这两个计数断言逮到。它抓不住
// **全新落点**这种子情形——在别处新增一段手搓样式渲染一个新异常,计数不降、
// 也不撞前 4 条黑名单,理论上仍是假阴性。这类新落点的兜底不是这份测试,是
// AGENTS.md「Anomaly severity tiers」约定 + 人工代码评审,如实说明而非声称
// 全覆盖。
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
} from "./test/semantic-source.mjs";

test("page.tsx 不再出现裸 ⚠/⚠️/⛔ glyph(字符串字面量 + JSX 文本节点，注释天然排除)", async () => {
  const page = await parseModule("page.tsx");
  const haystacks = [...stringLiterals(page), ...jsxTextValues(page)];
  const offenders = haystacks.filter((value) => /[⚠⛔]/.test(value));
  assert.deepEqual(offenders, []);
});

test("page.tsx 不再引用未定义的 --color-warning 系 token(区别于合法共用的 --color-warn)", async () => {
  const page = await parseModule("page.tsx");
  const offenders = stringLiterals(page).filter((value) => value.includes("--color-warning"));
  assert.deepEqual(offenders, []);
});

test("page.tsx 不再直接属性访问 .extraction_warning，必须整体交给 sourceAnomalies", async () => {
  const page = await parseModule("page.tsx");
  const offenders = propertyAccesses(page).filter((value) => value.endsWith(".extraction_warning"));
  assert.deepEqual(offenders, []);
});

test("page.tsx 不再对 paper_meta_status 做 missing/not_paper 比较(AST 二元表达式，不被引号/空格绕过)", async () => {
  // 早期版本用 page.getText().includes('paper_meta_status === "missing"') 做
  // 原始子串匹配——单引号 `=== 'missing'` 或去空格 `==="missing"` 语义相同却
  // 能滑过。改成扫二元表达式:操作符限定 ===/==，左侧属性访问以
  // .paper_meta_status 结尾，右侧字符串字面量取 comparisonsIn() 已经解出的
  // 不含引号的 .text，天然对引号风格/空格免疫。has_meta 分支(page.tsx:5029，
  // 来源详情展示已补全的论文元数据，合法保留)右值是 "has_meta"，不在
  // missing/not_paper 名单内，不会被这条断言误伤。
  const page = await parseModule("page.tsx");
  const offenders = comparisonsIn(page).filter((comparison) => (
    (comparison.operator === "===" || comparison.operator === "==")
    && comparison.left.endsWith(".paper_meta_status")
    && (comparison.right === "missing" || comparison.right === "not_paper")
  ));
  assert.deepEqual(offenders, []);
});

test("sourceAnomalies 只在来源行+来源详情各调用一次，AnomalyBadge 只渲染这两处", async () => {
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
