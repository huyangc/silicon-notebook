// 复制类按钮的**结果**态：结果画在按下的那一颗按钮上，到点自己回到 idle。
//
// 缺陷来源是群组邀请链接那颗「复制」：结果只发页面顶部的横幅，横幅离按钮很远、在长页面
// 上还会滚出视口，于是「点了没反应」。报告工具栏这两颗是同一形态——分享链接的结果只发
// toast，正文复制的失败被 `.catch` 整个吞掉，按钮纹丝不动。
//
// 按下态（globals.css 的 `button:…:active`）由 tests/guards/button-press-feedback-guard
// 钉住——那条没有 AST、jsdom 也不做级联。这里钉它管不到的另一半：结果态的文案、配色类
// 与自动还原，以及「按 key 分格」——同一排里点正文复制不该让分享链接那颗跟着变。
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { ReportsPanel } from "../../app/report-view";
import type { ReportDetailT } from "../../app/report-model";
import { reportWorkspaceFixture } from "./report-workspace-fixture";

const DONE_REPORT: ReportDetailT = {
  id: "rep-copy",
  question: "比较两类封装工艺",
  status: "done",
  progress: "",
  section_count: 1,
  created_at: "2026-08-01T00:00:00Z",
  created_by: "user-1",
  outline: [],
  sections: [],
  section_status: [],
  gaps: [],
  content_md: "# 报告正文",
  references: [],
  error: "",
  understanding: {},
};

/** 已分享的完成态报告：工具栏里同时有「复制」（正文）与「复制链接」（分享链接）。 */
function renderToolbar(copyShareLink: () => Promise<boolean | null>) {
  render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ active: DONE_REPORT, shared: true, copyShareLink })}
      setToast={vi.fn()}
    />,
  );
}

test("分享链接复制成功：按钮自己变成「已复制」并换成成功配色，随后自动还原", async () => {
  const user = userEvent.setup();
  renderToolbar(async () => true);

  await user.click(screen.getByRole("button", { name: "复制链接" }));

  const copied = await screen.findByRole("button", { name: "已复制" });
  expect(copied).toHaveClass("copy-result-copied");

  // 结果态是 JS 状态,不像 :active 那样松手自动还原——忘了摘掉就一直挂着,
  // 下一次点击反而看不出有没有点上。
  const restored = await screen.findByRole("button", { name: "复制链接" }, { timeout: 4000 });
  expect(restored).not.toHaveClass("copy-result-copied");
  expect(restored).not.toHaveClass("copy-result-failed");
});

test("分享链接复制失败：按钮说「复制失败」并换成失败配色", async () => {
  const user = userEvent.setup();
  renderToolbar(async () => false);

  await user.click(screen.getByRole("button", { name: "复制链接" }));

  const failed = await screen.findByRole("button", { name: "复制失败" });
  expect(failed).toHaveClass("copy-result-failed");
});

test("这一次压根没走到复制时按钮不闪结果——错误由 owner 自己报", async () => {
  // owner 返回 null 的场景:前置守卫不通过、切库/换报告让这次请求失效、或取回链接本身
  // 失败。把 null 也当失败会在用户什么都没等到的时候闪一下「复制失败」。
  const user = userEvent.setup();
  renderToolbar(async () => null);

  await user.click(screen.getByRole("button", { name: "复制链接" }));

  await waitFor(() => expect(screen.getByRole("button", { name: "复制链接" })).toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "复制失败" })).toBeNull();
  expect(screen.queryByRole("button", { name: "已复制" })).toBeNull();
});

test("正文复制失败不再被静默吞掉，按钮如实说「复制失败」", async () => {
  // ⚠ 不能靠「jsdom 没有剪贴板」来造这个场景:userEvent.setup() 自己会装一份可用的
  // navigator.clipboard,不打掉它这条用例会反过来测成功路径。execCommand 在 jsdom 里
  // 不存在,所以 DOM 兜底同样不成立——就是那台真的复制不了的浏览器。
  const user = userEvent.setup();
  vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
  renderToolbar(async () => null);

  await user.click(screen.getByRole("button", { name: "复制" }));

  const failed = await screen.findByRole("button", { name: "复制失败" });
  expect(failed).toHaveClass("copy-result-failed");
});

test("结果只落在按下的那一颗上，同排的另一颗不跟着变", async () => {
  const user = userEvent.setup();
  renderToolbar(async () => true);

  await user.click(screen.getByRole("button", { name: "复制" }));

  await screen.findByRole("button", { name: "已复制" });
  const shareButton = screen.getByRole("button", { name: "复制链接" });
  expect(shareButton).not.toHaveClass("copy-result-copied");
  expect(shareButton).not.toHaveClass("copy-result-failed");
});

// —— codex #612 R2 的两条 P2，一条修了一条驳了，两边都要有回归覆盖 ——

test("换到另一份报告时，新报告的按钮不顶着上一份的「已复制」", async () => {
  // 采纳的那条:结果态要挂 1.6s,期间在报告之间切换会让同一颗按钮指向另一份报告。
  // key 里带报告 id,内容一换就自动失配回 idle。
  const user = userEvent.setup();
  const other: ReportDetailT = { ...DONE_REPORT, id: "rep-other", question: "另一份报告" };
  const { rerender } = render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ active: DONE_REPORT, shared: true, copyShareLink: async () => true })}
      setToast={vi.fn()}
    />,
  );

  await user.click(screen.getByRole("button", { name: "复制链接" }));
  expect(await screen.findByRole("button", { name: "已复制" })).toHaveClass("copy-result-copied");

  // 1.6s 还没到就切走：新报告的链接从来没被复制过，不能说「已复制」。
  rerender(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ active: other, shared: true, copyShareLink: async () => true })}
      setToast={vi.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "复制链接" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "已复制" })).toBeNull();
});

test("同一个目标被重复复制：结果不闪回 idle，保持为真", async () => {
  // 驳回的那条(codex #612 R2 P2 第 1 条)建议「每次尝试开始时先清空结果」，好让重复点
  // 有一次可见的状态跳变。这里刻意不那么做，登记理由：
  //  · 「这一次点击有没有被接住」由 `:active` 回答——它每一次 mousedown 都如实位移+变淡
  //    (浏览器实测 translate 0px 1px / scale 0.98 / opacity 0.8)。结果态回答的是另一个
  //    问题:「现在剪贴板里有没有这条链接」，重复点时它照样是真话。
  //  · 想让清空真的落屏，需要一次「空档帧」；React 会把 await 前后的两次 setState 合成
  //    一次渲染，空档多半根本不落屏，要稳定看见就得再引入动画，而这条基线刻意不带动画。
  //
  // 因此这里钉住**刻意的**行为:重复点不把结果打回 idle。停留计时确实会从最后一次点击
  // 重新起算(state 每次都是新对象，effect 依赖它、清掉旧 timer)，但那条不写成断言——
  // 它只能靠墙钟时刻来判定，在满载的 CI 上就是一条 flake。
  const user = userEvent.setup();
  renderToolbar(async () => true);

  await user.click(screen.getByRole("button", { name: "复制链接" }));
  expect(await screen.findByRole("button", { name: "已复制" })).toHaveClass("copy-result-copied");

  await user.click(screen.getByRole("button", { name: "已复制" }));
  const again = await screen.findByRole("button", { name: "已复制" });
  expect(again).toHaveClass("copy-result-copied");
  expect(screen.queryByRole("button", { name: "复制链接" })).toBeNull();
});

test("取消分享再分享后，新链接不顶着旧链接的「已复制」", async () => {
  // codex #612 R5 P2。这颗按钮的 key 只认得报告 id——token 由 use-report-workspace 在点击时
  // 现取，渲染时视图手里没有。于是 1.6s 停留期内「取消分享 → 再分享」发出的**新** token
  // 会顶着旧链接的「已复制」出现，而它根本没被复制过；若 toggleShare 顺带做的那次自动复制
  // 还失败了，按钮就在说反话。`shared` 翻面是「链接身份换了」唯一的信号。
  const user = userEvent.setup();
  const view = (shared: boolean) => (
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ active: DONE_REPORT, shared, copyShareLink: async () => true })}
      setToast={vi.fn()}
    />
  );
  const { rerender } = render(view(true));

  await user.click(screen.getByRole("button", { name: "复制链接" }));
  expect(await screen.findByRole("button", { name: "已复制" })).toHaveClass("copy-result-copied");

  // 取消分享：按钮整颗卸载。
  rerender(view(false));
  expect(screen.queryByRole("button", { name: "已复制" })).toBeNull();

  // 1.6s 还没到就再分享——后端发的是另一条链接，它没被复制过。
  rerender(view(true));
  expect(screen.getByRole("button", { name: "复制链接" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "已复制" })).toBeNull();
});
