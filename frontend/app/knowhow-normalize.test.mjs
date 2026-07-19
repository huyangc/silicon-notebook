// knowhow-normalize.test.mjs — ruleNormalize（前端 TS 移植）与后端 Python
// rule_normalize（backend/app/services/knowhow/md_normalize.py）的 parity
// 测试。两侧共享同一份 golden fixture（backend/tests/fixtures/
// knowhow_normalize_golden.json）驱动，防止两个语言实现的语义漂移；用例结构
// 镜像后端 backend/tests/test_md_normalize_rule.py（golden 参数化 + 独立断言）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ruleNormalize, isRichMarkdown } from "./knowhow-cell-editor-logic.ts";

const golden = JSON.parse(
  readFileSync(new URL("../../backend/tests/fixtures/knowhow_normalize_golden.json", import.meta.url), "utf8"),
);

for (const c of golden) {
  test(`ruleNormalize golden: ${c.name}`, () => {
    const out = ruleNormalize(c.raw);
    const lines = out.split("\n");
    for (const needle of c.expect_contains) {
      assert.ok(lines.includes(needle), `${c.name}: missing ${JSON.stringify(needle)}\n${out}`);
    }
    for (const absent of c.expect_absent) {
      assert.ok(!out.includes(absent), `${c.name}: ${JSON.stringify(absent)} should be gone\n${out}`);
    }
    // I2: every golden case also pins the FULL exact output string (not just
    // contains/absent) -- the Python side (test_md_normalize_rule.py) asserts
    // the SAME field against the SAME fixture, so any divergence between the
    // two implementations now fails a test in one of them.
    assert.strictEqual(out, c.expect_exact, `${c.name}: exact mismatch`);
  });
}

// --- 独立断言：移植自 backend/tests/test_md_normalize_rule.py 的非 golden 用例 ---
// (test_no_leading_tab_ever / test_bullet_glyph_becomes_dash /
//  test_section_header_alpha_at_col0_is_bolded / test_never_raises_on_garbage /
//  test_empty_stays_empty / test_idempotent)

test("从不残留前导 TAB（no leading tab ever）", () => {
  const out = ruleNormalize("\t• a\n\t\tb. nested");
  assert.ok(!out.split("\n").some((l) => l.startsWith("\t")));
});

test("bullet glyph • 变成 '- '", () => {
  assert.strictEqual(ruleNormalize("• foo").split("\n")[0], "- foo");
});

test("顶格 A. 分节标题被加粗", () => {
  assert.ok(ruleNormalize("A. 考量\n\t• x").split("\n").includes("**A. 考量**"));
});

test("垃圾输入不抛异常", () => {
  assert.strictEqual(typeof ruleNormalize("\t\t\t)(*&^%\n\x00\n•••"), "string");
});

test("空输入 -> 空字符串", () => {
  assert.strictEqual(ruleNormalize(""), "");
  assert.strictEqual(ruleNormalize("   \n  "), "");
});

test("幂等：规整结果再规整一次不变", () => {
  const once = ruleNormalize(golden[0].raw);
  assert.strictEqual(ruleNormalize(once), once);
});

// --- strip 语义对齐 Python（控制符/BOM）---------------------------------------
// Python str.strip() 剥离的空白字符集与 JS .trim() 不同：Python 会剥
// U+001C-U+001F (FS/GS/RS/US) 与 U+0085 (NEL)，但 JS .trim() 不会；反过来
// JS .trim() 会剥 U+FEFF (BOM)，但 Python .strip() 不会。以下用例锁定与真实
// Python rule_normalize 逐字核对过的输出（见 md_normalize.py 的 .strip() 调用
// 站点），防止 TS 端 bare .trim() 站点悄悄漂移回不对齐状态。

test("单个 FS(\\x1c) 整体被当空白剥掉 -> 空串", () => {
  assert.strictEqual(ruleNormalize("\x1c"), "");
});

test("单个 NEL(\\x85) 整体被当空白剥掉 -> 空串", () => {
  assert.strictEqual(ruleNormalize("\x85"), "");
});

test("孤立 BOM(\\ufeff) 不是 Python 空白，保留", () => {
  assert.strictEqual(ruleNormalize("﻿"), "﻿");
});

test("行首 BOM 不被剥掉，随正文一起保留", () => {
  assert.strictEqual(ruleNormalize("﻿hello world"), "﻿hello world");
});

test("单独一行 FS(\\x1c) 被当空行处理，不留下幽灵段落", () => {
  assert.strictEqual(ruleNormalize("line one\n\x1c\nline two"), "line one\n\nline two");
});

test("行首 FS(\\x1c) 不泄漏进 bullet 正文", () => {
  assert.strictEqual(ruleNormalize("\x1c- bullet"), "- bullet");
});

// --- marker body 含 U+2028/U+2029 时的 parity（JS `.` vs Python `.` 语义分歧）---
// JS 正则 `.` 天生排除全部 LineTerminator（\n \r U+2028 U+2029），Python `.`
// （无 DOTALL）只排除 \n。三个 marker 正则的 body 捕获组 (.*)$ 若沿用 JS `.`，
// 当 body 里混入 U+2028/U+2029 时会在 JS 侧整体匹配失败（`.*` 无法跨越该字符
// 触达 $），该行被错误降级为 prose；Python 一侧因为 `.` 能吃掉这两个字符，
// 仍正常识别为列表/标题行（且 U+2028/U+2029 本身落在 Python `.strip()` 的空白
// 集合内，随行尾空白一起被剥掉，不留痕迹）。以下 6 个用例（bullet/ordered/
// alpha × U+2028/U+2029）锁定与真实 Python rule_normalize 逐字核对过的输出。
// 两个分隔符用 String.fromCharCode 显式构造，不把它们直接嵌入源码字符串字面
// 量——嵌入的话这两个不可见字符在 diff/编辑器里会显示成和普通空格无区别的
// 空白，容易被「清理行尾空白」之类的工具悄悄吃掉，反而复现不了本用例要锁的
// 场景。
const LINE_SEPARATOR = String.fromCharCode(0x2028);
const PARAGRAPH_SEPARATOR = String.fromCharCode(0x2029);

test("bullet body 含 U+2028 时仍被识别为列表项（非降级为 prose）", () => {
  assert.strictEqual(ruleNormalize(`-  body ${LINE_SEPARATOR}\n- second`), "- body\n- second");
});

test("bullet body 含 U+2029 时仍被识别为列表项（非降级为 prose）", () => {
  assert.strictEqual(ruleNormalize(`-  body ${PARAGRAPH_SEPARATOR}\n- second`), "- body\n- second");
});

test("ordered body 含 U+2028 时仍被识别为列表项（非降级为 prose）", () => {
  assert.strictEqual(ruleNormalize(`1.  body ${LINE_SEPARATOR}\n2. second`), "1. body\n2. second");
});

test("ordered body 含 U+2029 时仍被识别为列表项（非降级为 prose）", () => {
  assert.strictEqual(ruleNormalize(`1.  body ${PARAGRAPH_SEPARATOR}\n2. second`), "1. body\n2. second");
});

test("alpha 顶格标题 body 含 U+2028 时仍被加粗（非降级为 prose）", () => {
  assert.strictEqual(ruleNormalize(`A.  body ${LINE_SEPARATOR}\n\t- sub`), "**A. body**\n\n- sub");
});

test("alpha 顶格标题 body 含 U+2029 时仍被加粗（非降级为 prose）", () => {
  assert.strictEqual(ruleNormalize(`A.  body ${PARAGRAPH_SEPARATOR}\n\t- sub`), "**A. body**\n\n- sub");
});

// ---------------------------------------------------------------------------
// isRichMarkdown 保守门 parity —— 镜像 backend/tests/test_md_normalize_rule.py
// 的 RICH_MARKDOWN_SIGNALS 参数化用例组（test_is_rich_markdown_detects_each_
// signal / test_rule_normalize_leaves_rich_markdown_byte_identical /
// test_confirmed_repro_* / test_gate_does_not_skip_excel_idiom_golden_cases）。
// 架构性修复：ruleNormalize 从 opt-out 翻转成 opt-in——已有真实 markdown 结构
// 的 cell 一律原样返回（byte-identical），未知结构的安全默认值从"当 prose
// 重排"改成"原样不动"。见 md_normalize.py 的 is_rich_markdown 文档字符串。
// ---------------------------------------------------------------------------

const RICH_MARKDOWN_SIGNALS = [
  ["fence_backtick_closed", "```\ncode\n```"],
  ["fence_tilde_closed", "~~~\ncode\n~~~"],
  ["fence_unclosed", "```\ncode line"],
  ["pipe_table_leading_pipe", "| A | B |\n| - | - |"],
  ["pipe_table_no_leading_pipe", "A | B\n--- | ---\n1 | 2"], // confirmed repro
  ["list_continuation", "- parent\n  continuation text"], // confirmed repro
  ["blockquote", "> 引用文字"],
  ["html_block", "<div>\nhello\n</div>"],
  ["reference_link_definition", "[label]: https://example.com"],
  ["setext_underline", "Title\n---"],
];

for (const [name, raw] of RICH_MARKDOWN_SIGNALS) {
  test(`isRichMarkdown 命中信号: ${name}`, () => {
    assert.strictEqual(isRichMarkdown(raw), true, `signal ${name} should be detected: ${JSON.stringify(raw)}`);
  });

  test(`ruleNormalize 对富 markdown 信号原样返回（byte-identical）: ${name}`, () => {
    assert.strictEqual(ruleNormalize(raw), raw, `signal ${name} should pass through unchanged`);
  });
}

test("确认复现：无前导竖线的表格被保守门拦下", () => {
  const raw = "A | B\n--- | ---\n1 | 2";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("确认复现：列表延续行被保守门拦下", () => {
  const raw = "- parent\n  continuation text";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

// 门不能"过度跳过"：golden fixture 里每一条都是真实 Excel 习惯排版（tab 缩进
// 的 bullet/ordered/alpha、普通散文），不该命中任何一个富 markdown 信号——
// 否则 ruleNormalize 会在格子里恰好出现一个中间点或单个短横线时，静默停止
// 清理真正的 Excel 粘贴脏排版。
for (const c of golden) {
  test(`isRichMarkdown 不误判 golden 用例: ${c.name}`, () => {
    assert.strictEqual(isRichMarkdown(c.raw), false, `${c.name} should NOT trip the rich-markdown gate`);
  });
}

// ---------------------------------------------------------------------------
// 子项缩进 = 祖先 marker 宽度之和 —— 镜像 test_md_normalize_rule.py 的
// test_ordered_parent_child_indented_by_marker_width_not_flat_two_spaces /
// test_double_digit_ordered_parent_indents_child_by_four /
// test_three_level_nesting_sums_ancestor_marker_widths /
// test_bullet_parent_child_still_indented_two_spaces /
// test_idempotent_across_double_digit_ordered_nesting /
// test_idempotent_across_multi_level_all_ordered_nesting。
//
// CommonMark 要求子块缩进到父项 marker 之后内容开始的那一列：父项是
// bullet/alpha（恒渲染成 "- "，宽度 2）时固定 2 格与宽度恰好相等，但父项是
// ordered 时 marker 宽度随数字位数变化（"1. " -> 3，"10. " -> 4），固定 2 格
// 会让 "1. parent\n  - child" 被真实 remark/GFM 解析成两个并列的顶层列表而非
// 嵌套。
// ---------------------------------------------------------------------------

test("有序父项（个位数）+ 字母子项 -> 子项缩进 3 格（父 marker '1. ' 宽度 3，不是固定 2 格）", () => {
  const out = ruleNormalize("1. 父\n\ta. 子");
  assert.deepStrictEqual(out.split("\n"), ["1. 父", "   - 子"]);
});

test("项目符号父项 + 字母子项 -> 子项仍缩进 2 格（回归防护：父 marker '- ' 宽度本就是 2）", () => {
  const out = ruleNormalize("- 父\n\ta. 子");
  assert.deepStrictEqual(out.split("\n"), ["- 父", "  - 子"]);
});

test("有序父项（两位数）+ 字母子项 -> 子项缩进 4 格（父 marker '10. ' 宽度 4）", () => {
  const out = ruleNormalize("10. 父\n\ta. 子");
  assert.deepStrictEqual(out.split("\n"), ["10. 父", "    - 子"]);
});

test("三层嵌套：叶子缩进 = 所有祖先 marker 宽度之和（3 + 2 = 5），不是自身宽度也不是固定每层常数", () => {
  const raw = "1. top\n\ta. mid\n\t\t1. leaf";
  const out = ruleNormalize(raw);
  assert.deepStrictEqual(out.split("\n"), ["1. top", "   - mid", "     1. leaf"]);
});

test("幂等：两位数有序父项嵌套规整两次结果不变", () => {
  const once = ruleNormalize("10. 父\n\ta. 子");
  const twice = ruleNormalize(once);
  assert.strictEqual(twice, once, `not idempotent:\n${JSON.stringify(once)}\n!=\n${JSON.stringify(twice)}`);
});

test("幂等：三层全有序嵌套规整两次结果不变", () => {
  const once = ruleNormalize("1. top\n\t1. mid\n\t\t1. leaf");
  const twice = ruleNormalize(once);
  assert.strictEqual(twice, once, `not idempotent:\n${JSON.stringify(once)}\n!=\n${JSON.stringify(twice)}`);
});

// ---------------------------------------------------------------------------
// deny-list -> allow-list 反转后新关掉的洞 —— 镜像 test_md_normalize_rule.py 的
// test_four_space_indented_code_block_refused / test_tab_indented_non_marker_
// line_refused / test_indented_prose_refused / test_atx_heading_refused /
// test_idempotent_on_real_shape_normalized_cell。allow-list 门拒绝「任何有前导
// 空白、且本身不是 list marker 的行」，一条规则通用地关掉缩进代码块 / 列表延续
// 行 / 缩进 prose 一整类（deny-list 反复漏的正是它）。
// ---------------------------------------------------------------------------

test("4 空格缩进代码块被 allow-list 门拦下、原样返回（deny-list 曾漏的洞）", () => {
  const raw = "    def f():\n        return 1";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw); // byte-identical，代码不被毁
});

test("tab 缩进的非 marker 行被拦下、原样返回", () => {
  const raw = "\tsome indented prose that is not a list item";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("缩进 prose 行被拦下、原样返回", () => {
  const raw = "普通说明\n  这一行有前导空格但不是列表项";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("ATX 标题被拦下、原样返回（已是 markdown，不碰）", () => {
  const raw = "# 标题\n正文";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("已规整的真实形状格子（prose + **A.** 小标题 + - 项 + 1. 列表 + 3 空格嵌套子项）幂等 no-op", () => {
  const s =
    "说明文字\n\n" +
    "**A. 考量**\n\n" +
    "- 一\n- 二\n\n" +
    "**B. 步骤**\n\n" +
    "1. 第一步\n2. 第二步\n   - 子步骤";
  assert.strictEqual(isRichMarkdown(s), false); // 目标形状 cell，门必须放行
  assert.strictEqual(ruleNormalize(s), s); // 已规整 -> 逐字 no-op
});

// ---------------------------------------------------------------------------
// 门禁纳入 CommonMark 4 空格规则 —— 镜像 test_md_normalize_rule.py 的
// test_four_space_indented_marker_line_refused_as_indented_code /
// test_four_space_marker_code_block_two_lines_refused /
// test_tab_indented_marker_line_still_normalizes / test_core_tab_targets_still_normalize /
// test_three_space_emitted_nesting_still_passes_and_round_trips /
// test_marker_indent_boundary_three_allowed_four_refused /
// test_deep_normalized_structure_with_ge4_space_child_refuses_as_no_op /
// test_mixed_tab_and_space_indent_marker_allowed。
//
// 评审发现的对抗性洞：allow-list 门此前接受**任意缩进**的 list-marker 行，于是
// 一个内容行恰好长得像 marker 的 CommonMark 缩进代码块（4 空格缩进的
// "    - literal\n    1. literal"）被当成 marker 行放行，rule_normalize 把真代码
// 改写成顶层列表，而 content_invariant 对文字没变的结构破坏失明、兜不住。
// 修复：对 marker 行的前导空白采用 CommonMark 自己的 4 空格消歧——含 TAB（Excel
// 指纹，目标语料全是 tab 缩进）或纯空格 <=3（CommonMark 里 1-3 空格仍是列表项，
// 3 空格也正是本 emitter 单层嵌套的产物）才放行；纯空格 >=4 判为缩进代码块，
// 意图不可知 -> fail CLOSED 整格拒绝。
// ---------------------------------------------------------------------------

for (const markerLine of ["- literal", "* literal", "+ literal", "• literal", "1. literal", "10. literal", "a. literal", "B. literal"]) {
  test(`4 空格缩进的 marker 行按缩进代码块拒绝（纯空格 >=4）: ${JSON.stringify(markerLine)}`, () => {
    const raw = "    " + markerLine;
    assert.strictEqual(isRichMarkdown(raw), true);
    assert.strictEqual(ruleNormalize(raw), raw); // byte-identical，代码不被毁
  });
}

test("4 空格缩进的双行 marker 代码块被拒绝（评审确认的复现）", () => {
  const raw = "    - literal\n    1. literal";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

for (const markerLine of ["- x", "• x", "1. x", "a. x"]) {
  test(`含 TAB 的缩进 marker 行仍规整（Excel 指纹，不得回归）: ${JSON.stringify(markerLine)}`, () => {
    const raw = "\t" + markerLine;
    assert.strictEqual(isRichMarkdown(raw), false);
    const out = ruleNormalize(raw);
    assert.notStrictEqual(out, raw); // 确实被清理
    assert.ok(!out.includes("\t"));
  });
}

test("任务点名的两个核心 tab 目标不得回归", () => {
  assert.strictEqual(isRichMarkdown("\t• 增大 R"), false);
  assert.strictEqual(ruleNormalize("\t• 增大 R").split("\n")[0], "- 增大 R");
  assert.strictEqual(isRichMarkdown("\ta. 子项"), false);
  assert.strictEqual(ruleNormalize("\ta. 子项"), "**a. 子项**");
});

test("本 emitter 的 3 空格单层嵌套仍过门且逐字 round-trip（幂等）", () => {
  const raw = "1. 父\n   - 子";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), raw);
});

for (const markerLine of ["- 子", "1. 子", "a. 子"]) {
  test(`marker 缩进边界：父行下 3 空格放行 / 4 空格拒绝: ${JSON.stringify(markerLine)}`, () => {
    const allowed = "父\n   " + markerLine;
    const refused = "父\n    " + markerLine;
    assert.strictEqual(isRichMarkdown(allowed), false);
    assert.strictEqual(isRichMarkdown(refused), true);
    assert.strictEqual(ruleNormalize(refused), refused);
  });
}

test("深层已规整结构、子项缩进达 >=4 空格 -> 门现在拒绝、原样返回（refusal-as-no-op 是刻意的安全行为，非 bug）", () => {
  // emitter 确实会产出这种结构（第二层嵌套 = 两个祖先 marker 宽度之和）。新规则
  // 下门拒绝它，返回逐字不变的输入——本身即不动点，幂等仍成立。缩进达 4 空格后
  // 已无法区分深层嵌套列表与缩进代码块，故 fail closed、原样保留已经干净的结构。
  const raw = "1. a\n   1. b\n      - c";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw); // byte-identical no-op
});

test("tab + 空格混合缩进（任一顺序、含 tab）的 marker 行放行", () => {
  assert.strictEqual(isRichMarkdown("\t  - x"), false);
  assert.strictEqual(isRichMarkdown("  \t- x"), false);
  assert.strictEqual(isRichMarkdown("    \t- x"), false);
});

// ---------------------------------------------------------------------------
// Batch F — 门禁两条上下文规则（P2），镜像 backend/tests/test_md_normalize_rule.py
// 的同名用例组。两条都是 GATE 规则：拒绝 = 逐字原样返回，且都不得改变任何当前
// 已通过输入的规整结果（上面的 golden / 已规整形状 cell 保持不动）。
//
// Rule 1（懒延续）：顶格 prose 行紧跟（无空行）list-marker 行时，是该列表项的
// CommonMark 懒延续；逐行文法两行都放行，但 normalizeImpl 会注入空行把延续行拆
// 下来、后端 content_invariant 字符盲兜不住 -> 整格拒绝。marker 与 prose 间有空
// 行则按 CommonMark 断开延续，marker -> 空行 -> prose 仍放行（已规整语料里到处
// 是这个形状，必须保持通过）。
// Rule 2（主题分隔线）：`* * *` 命中 bullet 正则（`*` + 空格 + 正文 `* *`）被
// 「规整」成 `- * *`、毁掉水平分隔线；符号盲不变式放过。同类：`- - -`、`_ _ _`、
// 及顶格无空格的 `***`/`___`。门在 marker 接受**之前**识别 CommonMark 主题分隔
// 线（去掉所有空格/tab 后由 >=3 个同一 `* - _` 字符组成）-> 整格拒绝。既有
// `| : - =` 分隔规则已捕获裸 `---`，本规则推广到空格分隔与 `*`/`_` 形式、不合并。
// ---------------------------------------------------------------------------

test("懒延续：bullet 项后紧跟顶格 prose -> 拒绝、逐字原样返回", () => {
  const raw = "- parent\ncontinuation";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("懒延续：ordered 项后紧跟顶格 prose -> 拒绝", () => {
  const raw = "1. parent\ncontinuation";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("懒延续：缩进 alpha（渲染为嵌套项）后紧跟顶格 prose -> 拒绝（三种 marker 一致）", () => {
  const raw = "1. 父\n\ta. 子\ncontinuation";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("marker 与 prose 间有空行 -> 断开延续、仍规整（幂等 no-op）", () => {
  const raw = "- parent\n\nprose";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), "- parent\n\nprose");
});

test("方向敏感：prose 后跟 marker（intro + 列表）不是延续、仍规整", () => {
  const raw = "实际操作：\n\t1. 遍历各个corner";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), "实际操作：\n\n1. 遍历各个corner");
});

for (const raw of ["* * *", "- - -", "_ _ _", "***", "___", "- - - -", "* * * *"]) {
  test(`主题分隔线 -> 拒绝、逐字原样返回: ${JSON.stringify(raw)}`, () => {
    assert.strictEqual(isRichMarkdown(raw), true);
    assert.strictEqual(ruleNormalize(raw), raw);
  });
}

test("主题分隔线夹在 prose 之间（`* * *` 曾被误判成 bullet -> `- * *`）-> 拒绝", () => {
  const raw = "before\n* * *\nafter";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("无空格主题分隔线夹在 prose 之间 -> 拒绝", () => {
  const raw = "a\n***\nb";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("真正的 bullet `* item`（strip 后非 >=3 同字符）不被误判为主题分隔线，仍规整为 `- item`", () => {
  const raw = "* item";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw).split("\n")[0], "- item");
});

test("边界：`- -`（strip 后 2 字符，低于 >=3 阈值）不是主题分隔线、仍规整", () => {
  const raw = "- -";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), "- -");
});

// ---------------------------------------------------------------------------
// F1 — 门禁认全 CommonMark HTML 块起始符 —— 镜像 test_md_normalize_rule.py 的
// HTML_BLOCK_OPENERS 用例组。此前开口检测只认 `<` + 字母/`/`，漏了 `<!--`
// （注释）、`<?`（处理指令）、`<!` + 字母（声明，如 `<!DOCTYPE html>`）与
// `<![CDATA[`；多行的这类块会被逐行当 prose 重排、注入空行毁掉，字符盲的后端
// content_invariant 兜不住。修复把开口字符类扩到 `<` 后接 字母/`/`/`!`/`?`。
// 行中 `<`（`a < b`）或孤立 `<3`（`<`+数字，非开口）仍必须可规整（不过度拒绝）。
// ---------------------------------------------------------------------------

const HTML_BLOCK_OPENERS = [
  ["comment", "<!-- 注释 -->"],
  ["processing_instruction", '<?xml version="1.0" ?>'],
  ["declaration_doctype", "<!DOCTYPE html>"],
  ["cdata", "<![CDATA[x]]>"],
];

for (const [name, raw] of HTML_BLOCK_OPENERS) {
  test(`HTML 块起始符单独一行被门拦下（byte-identical）: ${name}`, () => {
    assert.strictEqual(isRichMarkdown(raw), true, `opener ${name} should be gated`);
    assert.strictEqual(ruleNormalize(raw), raw);
  });

  test(`HTML 块起始符 + 顶格 prose 行不被拆散（多行 corruption 复现）: ${name}`, () => {
    const cell = raw + "\n内容行";
    assert.strictEqual(isRichMarkdown(cell), true);
    assert.strictEqual(ruleNormalize(cell), cell);
  });
}

test("行中 `<`（`a < b`）不是块起始符，仍可规整", () => {
  assert.strictEqual(isRichMarkdown("a < b"), false);
});

test("孤立 `<3`（`<`+数字，非 HTML 块起始符）不被过度拒绝、逐字 no-op", () => {
  assert.strictEqual(isRichMarkdown("<3"), false);
  assert.strictEqual(ruleNormalize("<3"), "<3");
});

// ---------------------------------------------------------------------------
// F1（本批 review）— 门禁必须拒绝**行中**内联 HTML 标签起头 —— 镜像
// test_md_normalize_rule.py 的 test_midline_inline_html_*。startsBlockConstruct
// 的 HTML_BLOCK_RE 是 `^` 锚定的，只认行首 HTML 块；`前缀 <span>\ncontinued</span>`
// 这类**行中**起头、跨软换行的行内 HTML 漏网：normalizeImpl 在两行 prose 间注入空行、
// 塞进 <span> 内部，符号盲的后端 content_invariant 兜不住。修复：任一行**任意位置**含
// 未转义 `<` 紧跟 ASCII 字母或 `/`（tag 形状）即整格拒绝。fail-closed：`a < b`（空格）、
// `x<0`/`i<3`（数字）仍规整；`x<y`（字母）、`包含 <em>标签` 被拒。
// ---------------------------------------------------------------------------

test("F1：行中 `<span>` 跨软换行 -> 门拒绝、逐字原样返回", () => {
  const cell = "prefix <span>\ncontinued</span>";
  assert.strictEqual(isRichMarkdown(cell), true);
  assert.strictEqual(ruleNormalize(cell), cell);
});

test("F1：行中 `<`+字母（`x<y`，tag 形状）-> 拒绝", () => {
  assert.strictEqual(isRichMarkdown("x<y"), true);
  assert.strictEqual(ruleNormalize("x<y"), "x<y");
});

test("F1：行中 `<em>`（CJK 散文里的 tag）-> 拒绝", () => {
  assert.strictEqual(isRichMarkdown("包含 <em>标签"), true);
});

test("F1：行中 `</span>`（`<`+`/`，闭合标签形状）-> 拒绝", () => {
  assert.strictEqual(isRichMarkdown("尾 </span> 收"), true);
});

for (const raw of ["a < b", "x<0", "i<3"]) {
  test(`F1：行中 < 后接空格/数字（${JSON.stringify(raw)}，非 tag 形状）仍规整、逐字 no-op`, () => {
    assert.strictEqual(isRichMarkdown(raw), false);
    assert.strictEqual(ruleNormalize(raw), raw);
  });
}

// ---------------------------------------------------------------------------
// F3 — marker 宽度必须按 CODE POINT 计，不是 UTF-16 单元 —— 镜像
// test_md_normalize_rule.py 的 test_astral_ordered_marker_child_indented_by_
// codepoint_width。星际有序 marker 如 𝟙.（U+1D7D9，UTF-16 里是代理对，category
// Nd 故两侧都命中 ORDERED_RE）渲染成 "𝟙. "，3 个 code point 宽；子项缩进 = 祖先
// marker 宽度之和，故子项应缩进 3 格。JS `marker.length` 按 UTF-16 单元数会得 4
// （代理对占 2），与 Python len（按 code point）分歧 -> 用 `[...marker].length` 修正。
// ---------------------------------------------------------------------------

const ASTRAL_ORDERED_MARKER = String.fromCodePoint(0x1d7d9); // 𝟙

test("星际有序 marker 子项按 code point 宽度缩进 3 格（非 UTF-16 单元的 4 格）", () => {
  const raw = ASTRAL_ORDERED_MARKER + ". 父\n\ta. 子";
  const out = ruleNormalize(raw);
  assert.deepStrictEqual(out.split("\n"), [ASTRAL_ORDERED_MARKER + ". 父", "   - 子"]);
});

// ---------------------------------------------------------------------------
// F1 — 门禁必须拒绝 CommonMark 硬换行 —— 镜像 test_md_normalize_rule.py 的
// test_two_trailing_spaces_hard_break_refused 等。非空行 RAW 形态以 >=2 个尾随
// 空格、或单个尾随反斜杠结尾 = 段内硬换行（<br>）；normalizeImpl 会 strip 掉尾随
// 标记再用空行分段，把硬换行静默变成段落断裂，符号盲的后端 content_invariant
// 兜不住 -> 门在此 fail CLOSED 整格原样返回。真实 Excel 粘贴的尾随空白最多一个
// 空格（语料唯一形态，非硬换行）仍放行；miss vs 腐蚀的代价不对称要求拒绝。
// ---------------------------------------------------------------------------

test("硬换行：>=2 个尾随空格的非空行 -> 门拒绝、逐字原样返回", () => {
  const raw = "第一行  \n第二行";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("硬换行：单个尾随反斜杠的非空行 -> 门拒绝、逐字原样返回", () => {
  const raw = "第一行\\\n第二行";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("硬换行：恰好一个尾随空格（真实语料形态，非硬换行）仍规整", () => {
  const raw = "4. 计算充足)： \n\ta. x";
  assert.strictEqual(isRichMarkdown(raw), false);
  const out = ruleNormalize(raw);
  assert.notStrictEqual(out, raw);
  assert.deepStrictEqual(out.split("\n"), ["4. 计算充足)：", "   - x"]);
});

test("硬换行：带 >=2 尾随空格的空行不触发拒绝（空行短路在硬换行判定之前）", () => {
  const raw = "第一行\n   \n第二行";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), "第一行\n\n第二行");
});

test("硬换行：>=2 尾随空格落在 marker 行上同样被拒绝", () => {
  const raw = "- 第一项  \n- 第二项";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

// ---------------------------------------------------------------------------
// F2 — 门禁必须拒绝跨软换行的行内构造 —— 镜像 test_md_normalize_rule.py 的
// test_math_formula_spanning_soft_newline_refused 等。渲染器开了 remark-math，
// `$a\n+b$` 是一条软折行的行内公式；旧门把它当两行 prose 放行，normalizeImpl 用
// 空行分段 -> `$a\n\n+b$` 不再解析成公式，符号盲的后端 content_invariant 兜不住。
// 修复（run 配对，fail CLOSED）：逐行追踪未转义 `$` 的 run 配对（`\$` 跳过；长度 N 的 run
// 只由后面同长度 run 闭合）+ 行内代码反引号 run 配对；行尾仍在开着的 run 内（`$` run 或反
// 引号 run）= 构造跨行 -> 整格拒绝。成对 `$...$` / `` `...` `` 仍放行。刻意也拒绝孤立
// `价格 $100`（单个开着的 `$` run）。display 形态 `$$` 由同一套 run 配对识别（见下方 F1 段）。
// ---------------------------------------------------------------------------

test("F2：行内公式软折行（`$a\\n+b$`）-> 门拒绝、逐字原样返回", () => {
  const raw = "$a\n+b$";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F2：行内代码软折行（`` `code\\nspan` ``）-> 门拒绝、逐字原样返回", () => {
  const raw = "`code\nspan`";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F2：单行成对 `$x+y$`（同长度 run 闭合）仍规整", () => {
  const raw = "公式 $x+y$ 正常";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), "公式 $x+y$ 正常");
});

test("F2：转义 `\\$` 不是定界符（0 个未转义 `$`，无 run）仍规整", () => {
  const raw = "价格 \\$100 转义";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), "价格 \\$100 转义");
});

test("F2：单行成对行内代码 `` `ok` `` 仍规整", () => {
  const raw = "行内 `ok` 正常";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), "行内 `ok` 正常");
});

test("F2：孤立单个未转义 `$`（单个开着的 `$` run）-> fail-closed 拒绝、逐字原样返回", () => {
  const raw = "价格 $100 元";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

// ---------------------------------------------------------------------------
// F1（本批）— `$` 按 **run** 追踪，而非逐字符奇偶 —— 镜像 test_md_normalize_rule.py
// 的 test_display_math_spanning_soft_newline_refused 等。remark-math 的 DISPLAY
// 形态 `$$x\n+y$$` 跨软换行；旧的逐字符奇偶把 `$$` 当两个 `$`（偶数＝已闭合）放行，
// 空行分段拆断公式，符号盲的后端 content_invariant 兜不住。修复（镜像反引号）：长度
// N 的 `$`-run 开一个公式 span，只由后面**同长度**的 run 闭合；行尾仍在开着的 `$`-run
// 内 -> 整格拒绝。单 `$` 行为不变（孤立开着的 `$` run 仍拒绝）。
// ---------------------------------------------------------------------------

test("F1：display 公式软折行（`$$x\\n+y$$`）-> 门拒绝、逐字原样返回", () => {
  const raw = "before $$x\n+y$$ after";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：单行成对 `$$x+y$$`（同长度 run 闭合）仍规整", () => {
  const raw = "$$x+y$$ 同行";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：`$$` 独占一行（开着的 display run，下一行才闭合）-> 门拒绝、逐字原样返回", () => {
  const raw = "$$\nx + y\n$$";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：转义 `\\$\\$`（无 `$` run）仍规整", () => {
  const raw = "价格\\$\\$转义";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), raw);
});

// ---------------------------------------------------------------------------
// F1（本批）— 跨行 EMPHASIS 改由**全格配对栈**判定，取代旧的逐行中性规则 ——
// 镜像 backend/tests/test_md_normalize_rule.py 的同名用例组。`*first\nsecond*`
// 仍拒绝（line 1 的 can-open-only 与 line 2 的 can-close-only 成对），真正的洞是
// **词内（两侧词字符）** 形状：`这是*跨行\n强调*内容` 两个 `*` 都是 BOTH run，逐行
// 判中性、放行，但 CommonMark 会把它们**跨软换行**配成一对强调；normalizeImpl 注入
// 空行拆断它，符号盲的后端 content_invariant（`*` 当标点剥掉）兜不住。
//
// 修复：isRichMarkdown 用**一个全格栈**扫 `*`/`_` 定界符 run，每个 entry 记行号。
// can-open-only 压栈；can-close-only 弹栈（成一对）；BOTH run 栈非空则弹栈（成一对）、
// 否则压栈。**任一成对跨两个不同行即拒绝**。未配对的 leftover 自身不拒绝——这保住词内
// 乘法 `R*C`（孤立 BOTH run 压栈、永不成对），哪怕后面某行带一个自成一对的 `**bold**`
// （开 run 压栈、紧接的闭 run 弹掉自己，不动 R*C 的 entry）。丢弃逐行中性的副作用：
// 一个永不成对的**悬空** opener（`first *middle\nmore`、`_emph\ntext`）不再拒绝——它在
// CommonMark 下不构成强调，注入空行拆不断一个不存在的 span（content_invariant 已验证安全）。
// 方括号/`$`/反引号仍逐行留在 endsWithOpenInlineSpan。
// ---------------------------------------------------------------------------

test("F1：行首未闭合强调 `*first`（跨软换行）-> 门拒绝、逐字原样返回", () => {
  const raw = "*first\nsecond*";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：未闭合链接文本 `[label`（跨软换行）-> 门拒绝、逐字原样返回", () => {
  const raw = "[label\ncontinued](url)";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：词内乘法 `R*C`（中性，承重用例）仍规整", () => {
  const raw = "增加RC时间常数，delay正比于R*C";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：tab bullet 里的词内 `R*C`（真实语料形状）仍规整", () => {
  const raw = "\t• 增加RC时间常数，delay正比于R*C";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw).split("\n")[0], "- 增加RC时间常数，delay正比于R*C");
});

test("F1：单行成对强调 `**A. 头**` / `*重点*` 仍放行", () => {
  assert.strictEqual(isRichMarkdown("**A. 头**"), false);
  assert.strictEqual(isRichMarkdown("这是 *重点* 内容"), false);
});

test("F1：emitter 的加粗小标题结构仍过门、逐字 round-trip", () => {
  const raw = "**A. 考量**\n\n- 一\n- 二";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：悬空 opener `first *middle`（永不成对）现在规整（全格栈：leftover 不拒绝）", () => {
  // 词首 `*middle` 是 can-open-only run，全格再无 closer 与之配对 -> 未配对 leftover，
  // CommonMark 下不构成强调，注入空行拆不断不存在的 span（安全）。旧逐行规则过度拒绝。
  const raw = "first *middle\nmore";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), "first *middle\n\nmore");
});

test("F1：悬空 opener `_emph`（永不成对）现在规整", () => {
  const raw = "_emph\ntext";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), "_emph\n\ntext");
});

test("F1：转义方括号 `\\[转义` 不是开着的 span、仍规整", () => {
  const raw = "\\[转义";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), "\\[转义");
});

test("F1：全角【】不是 ASCII []，不受方括号深度规则影响、仍规整", () => {
  const raw = "【注意】这是一段说明";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：单行成对链接文本 `[文档](url)`（深度归零）仍放行", () => {
  assert.strictEqual(isRichMarkdown("见 [文档](url) 说明"), false);
});

// --- F1 全格强调配对（词内洞 + R*C 承重存活），镜像 test_md_normalize_rule.py ---

test("F1：词内强调跨软换行成对（`这是*跨行\\n强调*内容`）-> 拒绝、逐字原样返回", () => {
  const raw = "这是*跨行\n强调*内容";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：下划线词内强调跨软换行成对（`_下划线跨行\\n结束_`）-> 拒绝", () => {
  const raw = "_下划线跨行\n结束_";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：flanking-only 跨行成对（`*开头\\n结尾*`）仍拒绝（回归）", () => {
  const raw = "*开头\n结尾*";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：孤立 `R*C` 后跟自成一对的 `**参考信息**`（不偷悬空 opener）仍规整", () => {
  const raw = "增大RC时间常数，delay正比于R*C\n\n**参考信息**\n正文";
  assert.strictEqual(isRichMarkdown(raw), false);
});

test("F1：转义 `\\*` 在全格扫描中被跳过（无定界符可配对）-> 规整", () => {
  const raw = "开头\\*文字\n第二行\\*更多";
  assert.strictEqual(isRichMarkdown(raw), false);
});

test("F1：多行各自单行成对强调（`*a*` 与 `**b**`）不跨行成对 -> 规整", () => {
  const raw = "前 *a* 中\n后 **b** 尾";
  assert.strictEqual(isRichMarkdown(raw), false);
});

// ---------------------------------------------------------------------------
// F2（本批）— 跨行强调配对栈按**分隔符字符分型** —— 镜像 test_md_normalize_rule.py
// 的 test_star_pair_crosses_line_despite_intervening_underscore_refused 等。旧的
// 单一无类型栈让词内 `_`（BOTH run）弹掉了 `*` opener（CommonMark 绝不把 `*` 与 `_`
// 配对），于是真正的 `*...*` 跨行对没被检出、cell 放行 -> corruption。修复：每个分隔符
// 字符各维护一个独立栈（`*` 与 `_`）；`*`-run 只与 `*` 栈交互、`_`-run 只与 `_` 栈交互。
// 任一栈成对跨两个不同行即拒绝。两个栈彻底规避交错，比单栈严格更精确。
// ---------------------------------------------------------------------------

test("F2：`*` 跨行成对、中间夹词内 `_` 不再干扰（`*open R_C\\nclose*`）-> 拒绝", () => {
  const raw = "*open R_C\nclose*";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F2：孤立词内 `_`（`R_C`，永不成对）仍规整", () => {
  const raw = "R_C\n正常";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), "R_C\n\n正常"); // 两行 prose 空行分段、内容安全
});

test("F2：`_a_` 与 `*b*` 各自单行成对、各走自己的栈 -> 规整", () => {
  const raw = "_a_ 与 *b* 同行";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), raw);
});

// ---------------------------------------------------------------------------
// F1（本批，按分隔符**个数**配对）—— 镜像 test_md_normalize_rule.py 的
// test_double_closer_run_pops_two_units_cross_line_refused 等。长度 N 的 run 携带
// N 个定界符 unit：闭合 run 逐个弹、每个 unit 各成一对。旧的「一整个 run 只弹一次」
// 让 `*outer\n*inner**` 的 `**`（长度 2）只弹掉行 1 的 opener、行 0 悬空、跨行对没
// 登记、cell 放行 -> 注入空行拆断强调，符号盲的 contentInvariant 兜不住。
// ---------------------------------------------------------------------------

test("F1：闭合 run 长度 2 弹两个 unit，第二个跨行（`*outer\\n*inner**`）-> 拒绝", () => {
  const raw = "*outer\n*inner**";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw); // 逐字原样 -- 强调保留
});

test("F1：`**a**\\n*b*` 每行各自成对（含长度 2 的 run）不跨行 -> 规整", () => {
  const raw = "**a**\n*b*";
  assert.strictEqual(isRichMarkdown(raw), false);
});

test("F1：`***bold italic***` 单行三定界符成对 -> 规整", () => {
  const raw = "***bold italic***";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：`***bold\\n收尾***` 三对全部跨行（逐对都查）-> 拒绝", () => {
  const raw = "***bold\n收尾***";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

// --- F1（review）GFM 删除线 `~` 跨软换行 —— 镜像 test_md_normalize_rule.py。
// remark-gfm 4.0.1（前端渲染器，remarkGfm 默认选项）里单个 `~` 就是删除线且会
// 跨软换行成对：node repl 实测（unified().use(remarkParse).use(remarkGfm)，看
// mdast `delete` 节点）"~a\nb~"→delete "a\nb"、"~5ns\n延迟~"→delete、
// "foo~bar\nbaz~qux"→delete "bar\nbaz"（词内 `~` 也活跃，像 `*`）；而孤立
// "约~5ns"/"foo~bar"→无 delete（字面）。故 `~` 加入与 `*`/`_` 同一套全格配对栈
// （自己的 typed 栈、BOTH 语义同 `*`）：跨行成对才拒、孤立 `~` 仍规整。
test("F1：`~~old\\ntext~~` 删除线跨软换行 -> 拒绝、逐字原样返回", () => {
  const raw = "~~old\ntext~~";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：`文字~~删除~~更多` 单行删除线（同行成对）-> 规整", () => {
  const raw = "文字~~删除~~更多";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：转义 `\\~\\~` 不是定界符 -> 规整", () => {
  const raw = "价格\\~\\~转义";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：孤立单 `~`（`延迟约~5ns`，无配对）-> 规整", () => {
  const raw = "延迟约~5ns";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：两个孤立 `~` 跨软换行成对（`~5ns\\n延迟~`）-> 拒绝", () => {
  const raw = "~5ns\n延迟~";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：词内 `~` 跨软换行成对（`foo~bar\\nbaz~qux`）-> 拒绝（像 `*`）", () => {
  const raw = "foo~bar\nbaz~qux";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：孤立词内 `~`（`foo~bar`，无配对）-> 规整（承重 R*C 的删除线孪生）", () => {
  const raw = "foo~bar";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F1：`~` 自己的 typed 栈、不偷 `*`/`_` opener（`*开头~中\\n结尾*`）-> 拒绝", () => {
  const raw = "*开头~中\n结尾*";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

// --- F2（review）CommonMark 词内下划线 `_` 是字面（两侧都是 word char 时既不能
// 开也不能闭，不像 `*`）—— 镜像 test_md_normalize_rule.py。`_outer foo_bar\nclose_`
// 里 `foo_bar` 的 `_` 若被 BOTH 分支当成真 closer 弹掉真正的 opener，跨行 `_..._`
// 对就检不出、cell 放行、注入空行拆断强调。修复：仅 `_` run 两侧都是 word char 时
// 整段跳过（不压不弹）；`*`/`~` 保持 BOTH 语义。node repl 实测："foo_bar"→无 em、
// "_outer foo_bar\nclose_"→em "outer foo_bar\nclose"（跨行）、
// "some_file\nother_file"→无 em。
test("F2：词内 `_` 字面、放行真正的跨行对（`_outer foo_bar\\nclose_`）-> 拒绝", () => {
  const raw = "_outer foo_bar\nclose_";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F2：孤立词内 `_`（`foo_bar`，字面）-> 规整", () => {
  assert.strictEqual(isRichMarkdown("foo_bar"), false);
  assert.strictEqual(ruleNormalize("foo_bar"), "foo_bar");
});

test("F2：两个词内 `_` 跨行不成对（`some_file\\nother_file`，CommonMark 无 em）-> 规整", () => {
  const raw = "some_file\nother_file";
  assert.strictEqual(isRichMarkdown(raw), false);
  assert.strictEqual(ruleNormalize(raw), "some_file\n\nother_file"); // 两行 prose 空行分段
});

test("F2：`*` 不受词内字面规则约束（`*open R_C\\nclose*`）-> 拒绝（回归）", () => {
  const raw = "*open R_C\nclose*";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(ruleNormalize(raw), raw);
});

test("F2：`_同一行_` 单行成对（边界/空格侧、非词内）-> 规整", () => {
  assert.strictEqual(isRichMarkdown("_同一行_ 强调"), false);
});
