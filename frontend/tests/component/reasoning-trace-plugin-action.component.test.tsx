import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test } from "vitest";

import { ReasoningTracePanel } from "../../app/answer-panel";


afterEach(cleanup);


// 后端单个参数放行到 REFLECT_ACTION_ARGUMENT_MAX_CHARS = 300 字符，而折叠行的参数
// 摘要夹到 120 字符。docs/product-and-api.md「Reflect plugin actions」的 What leaves
// the deployment 段承诺提问的人**始终**看得见替他发出去的原文——那是这个特性代替
// 内容过滤器的全部依仗。所以两态分工必须是「折叠夹、展开全」；若两态共用同一个夹
// 过的格式化器，被夹掉的那一截在整个界面上无处可看，合同只剩纸面。
const LONG_QUERY = `sic mosfet 阈值漂移 ${"字".repeat(300)}`.slice(0, 300);


function pluginActionStep(argumentsMap: Record<string, string>) {
  return {
    step_type: "plugin_action",
    summary: "调用扩展检索 search_ieee，新增 2 条外部材料",
    detail: {
      plugin_id: "acme.ieee",
      action: "search_ieee",
      arguments: argumentsMap,
      found: 2,
    },
    duration_ms: 1200,
  };
}


test("折叠摘要夹断参数，展开后逐项给出完整参数原文", async () => {
  const user = userEvent.setup();
  const { container } = render(
    <ReasoningTracePanel steps={[pluginActionStep({ query: LONG_QUERY, venue: "journal" })]} />,
  );

  // 折叠态：摘要那一行确实被夹断，完整参数进不去。
  const collapsedDetail = container.querySelector(".reasoning-trace-summary small")?.textContent ?? "";
  expect(collapsedDetail).toContain("…");
  expect(collapsedDetail).not.toContain(LONG_QUERY);

  await user.click(screen.getByRole("button", { expanded: false }));

  // 展开态：每个参数一行「参数名: 值」，值一个字不夹。
  const rows = [...container.querySelectorAll(".reasoning-trace-argument")]
    .map((row) => row.textContent ?? "");
  expect(rows).toHaveLength(2);
  expect(rows[0]).toBe(`query:${LONG_QUERY}`);
  expect(rows[1]).toBe("venue:journal");
  const value = container.querySelector(".reasoning-trace-argument-value")?.textContent ?? "";
  expect(value).toHaveLength(300);
  expect(screen.getByText("替你发出的参数")).toBeTruthy();
});


test("空串参数不占一行，全空时整块不渲染（不给一个空框）", async () => {
  const user = userEvent.setup();
  const { container, unmount } = render(
    <ReasoningTracePanel steps={[pluginActionStep({ query: "阈值漂移", venue: "" })]} />,
  );
  await user.click(screen.getByRole("button", { expanded: false }));
  expect([...container.querySelectorAll(".reasoning-trace-argument")]
    .map((row) => row.textContent)).toEqual(["query:阈值漂移"]);
  unmount();

  const empty = render(
    <ReasoningTracePanel steps={[pluginActionStep({ query: "", venue: "" })]} />,
  );
  await user.click(screen.getByRole("button", { expanded: false }));
  expect(empty.container.querySelector(".reasoning-trace-arguments")).toBeNull();
});


// 只有 plugin_action 步走这条披露：别的步即便 detail 里恰好带个 arguments 键，也
// 不该在轨迹里凭空多出一块「替你发出的参数」——那会把没发出去的东西说成发出去了。
test("非 plugin_action 步不渲染参数披露块", async () => {
  const user = userEvent.setup();
  const { container } = render(
    <ReasoningTracePanel
      steps={[
        { step_type: "memory", summary: "找到 2 条相关记忆", detail: { count: 2 } },
        { step_type: "plugin", summary: "扩展步", detail: { arguments: { query: "q" } } },
      ]}
    />,
  );
  await user.click(screen.getByRole("button", { expanded: false }));
  expect(container.querySelector(".reasoning-trace-arguments")).toBeNull();
});
