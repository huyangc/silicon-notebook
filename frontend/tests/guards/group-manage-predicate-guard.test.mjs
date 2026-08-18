// 守卫:「这条授权边给不给管理权」在前端只许有 `confersManage` 一份实现。
//
// 为什么必须是**静态**判据:把 `confersManage(grant)` 换成就地手写的
// `grant.role === "admin"`,行为**逐位相同**——单测与组件测试全绿(已实测)。分叉是在
// **将来**发生的:后端 `_admin_principal_match_expr` 的三条臂哪天变了(比如管理级也认
// 某个新主体、或 role 取值扩了一个),改的人会去改那个共用函数,而那些手抄的副本原地
// 不动,于是同一条边在「标注可管理」与「取消管理权要删哪条」上给出两个答案。
//
// 这条规矩是 codex #519 R7 P2 立的(那轮把 `conferManage` 从局部常量提成模块级共用
// 函数),R9 P2 又多了两个消费者(`manageGrantIds` / `canDropManage`)。判据本身踩过两次
// 坑,都记在 `confersManage` 的注释里:R1 只看主体类型(把权限标小了),R4 补了 role
// 但把类型判定留窄了(又标大了)。
//
// 判据走 `semantic-source` 的 AST 而不是裸文本:`static-source-policy` 明令测试不得直接
// 读生产源码文本(读文本的守卫会在无害重排上误红,也拦不住换个写法的同一件事)。

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  callSitesIn,
  callsIn,
  comparisonsIn,
  findFunction,
  parseModule,
} from "../../test-support/semantic-source.mjs";

/** 管理权判据的唯一定义点,以及必须复用它的那几个消费者。 */
const DEFINITION = "confersManage";
const CONSUMERS = ["revocationOrder", "manageGrantIds", "canDropManage"];

/** 「拿 role 跟 'admin' 比」的语义形状 —— 手抄副本的招牌长相。 */
const isRoleAdminComparison = ({ left, right }) => (
  (left.endsWith(".role") && right === "admin")
  || (left === "admin" && right.endsWith(".role"))
);

test("只有 confersManage 自己拿 role 跟 admin 比", async () => {
  const source = await parseModule("group-api.ts");
  const definition = findFunction(source, DEFINITION);
  // 先证明唯一定义点**确实**是这个形状,否则下面的排除法在它被改写之后会静默放空。
  assert.ok(
    comparisonsIn(definition).some(isRoleAdminComparison),
    "confersManage 不再直接比较 role 与 'admin' —— 判据换了写法就要同步更新本守卫,"
      + "别顺手把守卫删掉(那正是分叉开始的地方)",
  );

  const strays = [];
  for (const consumer of CONSUMERS) {
    for (const comparison of comparisonsIn(findFunction(source, consumer))) {
      if (isRoleAdminComparison(comparison)) {
        strays.push(`${consumer}: ${comparison.left} ${comparison.operator} "admin"`);
      }
    }
  }
  assert.deepEqual(strays, [], (
    `这些消费者手写了管理权判据:${strays.join(" / ")}。`
    + "改用 confersManage(grant) —— 它是后端 _admin_principal_match_expr 三条臂在前端的"
    + "唯一对应物(只看 role,对 group 与 group_admins 一视同仁)。手抄一份不会有任何"
    + "行为测试报红,但后端判据一变,「标注可管理」与「取消管理权删哪条边」就会分叉。"
  ));
});

test("管理权的每个消费者都真的引用了 confersManage", async () => {
  // 空转保护:上面那条只证明「没有手抄的比较」,证不了消费者**用了**共用函数——
  // 把 manageGrantIds 改成 `filter(() => true)` 也能骗过它。
  //
  // 两种合法形态都要认:直接调用 `confersManage(grant)`,以及把它当值传给 filter
  // (`filter(confersManage)`)——后者不是调用点,只出现在实参里。
  const source = await parseModule("group-api.ts");
  const missing = CONSUMERS.filter((consumer) => {
    const declaration = findFunction(source, consumer);
    const called = callsIn(declaration).includes(DEFINITION);
    const passed = callSitesIn(declaration).some((site) =>
      site.arguments.some((argument) => argument.includes(DEFINITION)),
    );
    return !called && !passed;
  });
  assert.deepEqual(missing, [], (
    `这些消费者没有引用 confersManage:${missing.join(" / ")}。`
    + "管理权判据只许有一份实现,消费者要么调用它、要么把它当谓词传进 filter。"
  ));
});
