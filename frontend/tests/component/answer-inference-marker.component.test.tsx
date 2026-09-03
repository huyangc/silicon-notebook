// 回归:句首/段首的推断/通识标记(（推断）/(推断)/Likely,/【通识】)要被切成独立
// span,句中出现的普通词「推断」不动。走真组件的真管线(不复刻插件列表),同 answer-markdown-
// tilde.component.test.tsx 的写法。
import { render } from "@testing-library/react";
import { expect, test } from "vitest";

import { AnswerMarkdown } from "../../app/answer-markdown";
import { ReportMarkdown } from "../../app/report-view";

test("全角（推断）在句首被切成 span.answer-inference,整句逐字保留", () => {
  const { container } = render(
    <AnswerMarkdown
      answer="（推断）该电路在高温下增益下降。"
      onReferenceClick={() => undefined}
    />,
  );

  const marker = container.querySelector("span.answer-inference");
  expect(marker).not.toBeNull();
  expect(marker?.textContent).toBe("（推断）");
  expect(container.textContent).toContain("（推断）该电路在高温下增益下降。");
});

test("半角 (推断) 与行首 Likely, 同样命中句首判据", () => {
  const half = render(
    <AnswerMarkdown answer="(推断) 增益会下降。" onReferenceClick={() => undefined} />,
  );
  const halfMarker = half.container.querySelector("span.answer-inference");
  expect(halfMarker).not.toBeNull();
  expect(halfMarker?.textContent).toBe("(推断)");

  const likely = render(
    <AnswerMarkdown answer="Likely, the gain drops at high temperature." onReferenceClick={() => undefined} />,
  );
  const likelyMarker = likely.container.querySelector("span.answer-inference");
  expect(likelyMarker).not.toBeNull();
  expect(likelyMarker?.textContent).toBe("Likely,");
});

test("【通识】命中 span.answer-general-knowledge,与推断标记分色", () => {
  const { container } = render(
    <AnswerMarkdown answer="【通识】半导体的带隙随温度升高而变窄。" onReferenceClick={() => undefined} />,
  );

  const marker = container.querySelector("span.answer-general-knowledge");
  expect(marker).not.toBeNull();
  expect(marker?.textContent).toBe("【通识】");
  expect(container.querySelector("span.answer-inference")).toBeNull();
});

test("句中出现的普通词「推断」不产生任何 span;第二句句首的（推断）仍命中", () => {
  const { container } = render(
    <AnswerMarkdown
      answer="根据推断结果，电路是稳定的。（推断）在极端温度下可能不稳定。"
      onReferenceClick={() => undefined}
    />,
  );

  const markers = container.querySelectorAll("span.answer-inference");
  expect(markers.length).toBe(1);
  expect(markers[0]?.textContent).toBe("（推断）");
  expect(container.textContent).toContain("根据推断结果，电路是稳定的。（推断）在极端温度下可能不稳定。");
});

test("句中的标记字面量不切:全角（推断）夹在句子中间、Likely, 不在句首", () => {
  // 这两条是句首判据的真负例——上一条用例的「根据推断结果」压根不匹配标记字面量,
  // 测不到判据本身;把判据整段删掉,这里必须红。
  const midFullwidth = render(
    <AnswerMarkdown answer="该电路（推断）在高温下增益下降。" onReferenceClick={() => undefined} />,
  );
  expect(midFullwidth.container.querySelector("span.answer-inference")).toBeNull();
  expect(midFullwidth.container.textContent).toContain("该电路（推断）在高温下增益下降。");

  const midLikely = render(
    <AnswerMarkdown answer="This is Likely, wrong." onReferenceClick={() => undefined} />,
  );
  expect(midLikely.container.querySelector("span.answer-inference")).toBeNull();
});

test("英文问号之后的 Likely, 是句首", () => {
  const { container } = render(
    <AnswerMarkdown answer="Is it true? Likely, the gain drops." onReferenceClick={() => undefined} />,
  );
  const marker = container.querySelector("span.answer-inference");
  expect(marker).not.toBeNull();
  expect(marker?.textContent).toBe("Likely,");
});

test("被强调/引用节点切开的句中标记不算段首", () => {
  // `**重点**（推断）`:标记落在 strong 之后新 text 节点的 index 0,只看本节点会误判成段首。
  const afterStrong = render(
    <AnswerMarkdown answer="见 **重点**（推断）说明。" onReferenceClick={() => undefined} />,
  );
  expect(afterStrong.container.querySelector("span.answer-inference")).toBeNull();
  expect(afterStrong.container.textContent).toContain("见 重点（推断）说明。");

  // `[k1]（推断）`:remarkCitations 先跑,在 chip 处切断 text 节点,形态同上。
  const afterCitation = render(
    <AnswerMarkdown
      answer="结论 [k1]（推断）继续说明。"
      anchors={[{
        key: "k1",
        object_id: "el-1",
        object_type: "element",
        label: "增益曲线",
        source_title: "来源论文",
        location_label: "p. 3",
      }]}
      onReferenceClick={() => undefined}
    />,
  );
  expect(afterCitation.container.querySelector("button.cite-chip")).not.toBeNull();
  expect(afterCitation.container.querySelector("span.answer-inference")).toBeNull();

  // 反向:强调之后隔着句号的标记仍是句首。
  const afterStrongSentence = render(
    <AnswerMarkdown answer="见 **重点**。（推断）继续说明。" onReferenceClick={() => undefined} />,
  );
  expect(afterStrongSentence.container.querySelector("span.answer-inference")).not.toBeNull();
});

test("深度报告正文同样识别推断标记", () => {
  const { container } = render(
    <ReportMarkdown markdown="（推断）该模块在高负载下可能过热。" references={[]} />,
  );

  const marker = container.querySelector("span.answer-inference");
  expect(marker).not.toBeNull();
  expect(marker?.textContent).toBe("（推断）");
});

test("带引用标记时,引用 chip 与推断 span 同时存在、互不吞并", () => {
  const { container } = render(
    <AnswerMarkdown
      answer="（推断）该电路在高温下增益下降 [k1]。"
      anchors={[{
        key: "k1",
        object_id: "el-1",
        object_type: "element",
        label: "增益曲线",
        source_title: "来源论文",
        location_label: "p. 3",
      }]}
      onReferenceClick={() => undefined}
    />,
  );

  const inferenceMarker = container.querySelector("span.answer-inference");
  expect(inferenceMarker).not.toBeNull();
  expect(inferenceMarker?.textContent).toBe("（推断）");

  const citeChip = container.querySelector("button.cite-chip");
  expect(citeChip).not.toBeNull();
  expect(citeChip?.textContent).toBe("[1]");

  expect(container.textContent).toContain("（推断）该电路在高温下增益下降 [1]。");
});
