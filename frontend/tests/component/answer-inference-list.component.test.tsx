// 回归:T2-d 前端归一——渲染前把「标记在列表序号前」的行改写成「序号在前、标记
// 在后」,让 remark 的列表解析生效,句首那个不带序号的标记仍单独识别。
// 走真组件的真管线(AnswerMarkdown / ReportMarkdown 各自的 useMemo 里都串了
// normalizeInferenceListMarkers(normalizeMathMarkdown(...))),同
// answer-inference-marker.component.test.tsx 的写法。
import { render } from "@testing-library/react";
import { expect, test } from "vitest";

import { AnswerMarkdown } from "../../app/answer-markdown";
import { ReportMarkdown } from "../../app/report-view";

const RAW_ANSWER = "（推断）以下为论文未描述的方向：\n（推断）1. 世界模型闭环。\n（推断）2. 长时程一致。";
const ALREADY_CORRECT_ANSWER = "1. （推断）a\n2. （推断）b";

test("AnswerMarkdown:标记在序号前的行被归一,渲染出真正的有序列表", () => {
  const { container } = render(
    <AnswerMarkdown answer={RAW_ANSWER} onReferenceClick={() => undefined} />,
  );

  const list = container.querySelector("ol");
  expect(list).not.toBeNull();
  const items = list?.querySelectorAll("li") ?? [];
  expect(items.length).toBe(2);

  for (const item of items) {
    expect(item.querySelectorAll("span.answer-inference").length).toBe(1);
  }

  // 段首那行没有列表语法,不进 <ol>,但仍单独识别成一个 span。
  const allMarkers = container.querySelectorAll("span.answer-inference");
  expect(allMarkers.length).toBe(3);

  expect(container.textContent).toContain("以下为论文未描述的方向：");
  expect(container.textContent).toContain("世界模型闭环。");
  expect(container.textContent).toContain("长时程一致。");
});

test("ReportMarkdown:同样的输入得到两个 li 的有序列表", () => {
  const { container } = render(
    <ReportMarkdown markdown={RAW_ANSWER} references={[]} />,
  );

  const list = container.querySelector("ol");
  expect(list).not.toBeNull();
  expect(list?.querySelectorAll("li").length).toBe(2);
});

test("AnswerMarkdown:已经写对的「序号在前、标记在后」结果一致", () => {
  const { container } = render(
    <AnswerMarkdown answer={ALREADY_CORRECT_ANSWER} onReferenceClick={() => undefined} />,
  );

  const list = container.querySelector("ol");
  expect(list).not.toBeNull();
  const items = list?.querySelectorAll("li") ?? [];
  expect(items.length).toBe(2);
  for (const item of items) {
    expect(item.querySelectorAll("span.answer-inference").length).toBe(1);
  }
  expect(container.textContent).toContain("a");
  expect(container.textContent).toContain("b");
});
