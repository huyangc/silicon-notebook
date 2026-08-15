// 回归:模型答案里的单个 `~`(区间 `7~5nm`、约等于 `~3GHz`)不得被渲染成删除线。
//
// remark-gfm 4.0.1 默认 `singleTilde: true`,同段里两个词内 `~` 会配成一对,把中间
// 整段正文塞进 `<del>`——部署环境实测到的「回答里凭空多出一条中划线」就是它。
// 这里走真组件的真管线(不复刻插件列表),`~~x~~` 的 GFM 规范删除线必须原样保留。
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { AnswerMarkdown } from "../../app/answer-markdown";
import { ReportMarkdown } from "../../app/report-view";

const TILDE_ANSWER = "工艺节点为 7~5nm，良率约 80~90%，频率 ~3GHz 时电压 0.8~1.1V。";

test("Ask 回答里的单波浪线区间/约等于不产生删除线", () => {
  const { container } = render(
    <AnswerMarkdown answer={TILDE_ANSWER} onReferenceClick={() => undefined} />,
  );

  expect(container.querySelector("del")).toBeNull();
  // 正文逐字保留——`~` 是字面量,既不吞进标记也不被吃掉。
  expect(container.textContent).toContain("7~5nm");
  expect(container.textContent).toContain("80~90%");
  expect(container.textContent).toContain("0.8~1.1V");
});

test("深度报告正文同样不把单波浪线当删除线", () => {
  const { container } = render(<ReportMarkdown markdown={TILDE_ANSWER} references={[]} />);

  expect(container.querySelector("del")).toBeNull();
  expect(container.textContent).toContain("7~5nm");
});

test("GFM 规范的双波浪线删除线仍然生效", () => {
  const { container } = render(
    <AnswerMarkdown answer="该方案 ~~已废弃~~，改用新流程。" onReferenceClick={() => undefined} />,
  );

  expect(container.querySelector("del")).not.toBeNull();
  expect(screen.getByText("已废弃").tagName).toBe("DEL");
});
