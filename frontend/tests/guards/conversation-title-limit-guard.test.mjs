// 会话重命名长度护栏的**接线**守卫。
//
// 纯函数那一半（`conversationTitleLimitHint` 的尺子与「不裁剪」）由
// `tests/unit/ask-input-limits.test.mjs` 钉住；这里钉的是它有没有真的接上去——
// 重命名 UI 在 `page.tsx` 里，没有可单独渲染的组件接缝，所以按**源码语义**钉。
//
// 为什么这几条值得单独钉：
//
//   ① 输入框**不得**有 `maxLength`。它是这里最容易「顺手加上」的一行，看起来
//      正是护栏该有的样子，实际上一次犯两条红线：数的是 UTF-16 code unit，与
//      后端 Pydantic 的码点口径对不上（codex #525 R2）；而且它**静默裁剪**用户
//      粘进来的文字（codex #525 R3）。两条都不会报错，只会安静地生效。
//   ② 提交有**两条**路——保存键与 Enter，两者都要读同一个判据。只 gate 按钮，
//      超长标题照样能从键盘存进去（`AskComposer` 的 `submitBlocked` 是同一课）。
//   ③ `commitRenameSession` 里要有防御性复查，且**不得**出现切片——一旦有人在
//      这里裁一刀，上面两个闸看起来还在、红线却已经破了。
//
// 覆盖边界（如实说明）：本守卫认的是源码文本/AST 形态，不是运行时行为。上限取值
// 与两端同值由单测钉，后端 422 与「绝不裁短了存」由
// `backend/tests/test_conversations.py` 与 `test_conversation_public_view.py` 钉。
import test from "node:test";
import assert from "node:assert/strict";

import { findFunction, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";

const GATE = "sessionTitleOverLimit";

async function renameInput() {
  const page = await parseModule("page.tsx");
  const inputs = jsxElements(page, "input").filter(
    (el) => (el.bindings || {}).value === "sessionTitleDraft",
  );
  assert.equal(
    inputs.length, 1,
    "找不到（或找到多个）会话重命名输入框——它的 value 绑定应当是 sessionTitleDraft。"
    + "改了名字就把这个守卫一起改，别让它空转",
  );
  return { page, input: inputs[0] };
}

test("重命名输入框不得用 maxLength 当护栏（尺子不对 + 静默裁剪）", async () => {
  const { input } = await renameInput();
  const keys = [...Object.keys(input.attributes || {}), ...Object.keys(input.bindings || {})];
  assert.ok(
    !keys.some((k) => k.toLowerCase() === "maxlength"),
    "会话重命名输入框挂了 maxLength：它数的是 UTF-16 code unit（与后端 Pydantic 的"
    + "码点口径对不上，codex #525 R2），而且会静默裁掉用户粘进来的文字"
    + "（codex #525 R3）。护栏是拦住保存，不是替用户改标题",
  );
});

test("Enter 与保存键两条提交路都读同一个超限判据", async () => {
  const { page, input } = await renameInput();

  const onKeyDown = (input.bindings || {}).onKeyDown || "";
  assert.ok(
    onKeyDown.includes(GATE),
    `重命名输入框的 onKeyDown 没有读 ${GATE}：只 gate 保存键的话，超长标题照样能`
    + "从键盘按 Enter 存进去",
  );

  const saveButtons = jsxElements(page, "button").filter(
    (el) => (el.attributes || {}).title === "保存"
      && ((el.bindings || {}).onClick || "").includes("commitRenameSession"),
  );
  assert.equal(saveButtons.length, 1, "找不到（或找到多个）会话重命名的保存键");
  assert.ok(
    ((saveButtons[0].bindings || {}).disabled || "").includes(GATE),
    `重命名保存键的 disabled 没有读 ${GATE}：超限时它必须点不动，否则用户只会吃一个 422`,
  );
});

test("commitRenameSession 自己也复查，且绝不裁短", async () => {
  const askSession = await parseModule("use-ask-session.ts");
  const body = findFunction(askSession, "commitRenameSession").getText(askSession);

  assert.ok(
    body.includes("conversationTitleLimitHint"),
    "commitRenameSession 没有防御性复查：绕过按钮/Enter 的调用路径会发出一个必被 422 的 PATCH",
  );
  assert.ok(
    !/\.slice\(|\.substring\(|\[\s*0\s*:/.test(body),
    "commitRenameSession 里出现了截断：护栏是拦住保存，不是替用户裁剪标题"
    + "（用户编辑的数据不得静默截断）",
  );
});
