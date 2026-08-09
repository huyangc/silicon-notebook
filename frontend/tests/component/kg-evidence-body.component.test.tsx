import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { KgEvidenceBody } from "../../app/kg-evidence-body";


describe("knowledge graph evidence body", () => {
  it("renders formula source elements with display KaTeX", () => {
    const { container } = render(
      <KgEvidenceBody
        elementType="formula"
        text={String.raw`C _ {l} = 2 \sigma (\tilde {C} _ {l}).\tag{7}`}
      />,
    );

    expect(container.querySelector(".katex-display")).not.toBeNull();
    expect(container.querySelector(".katex-error")).toBeNull();
    expect(container.textContent).toContain("C");
    expect(container.textContent).toContain("(7)");
  });

  it("keeps malformed formula evidence visible as raw text", () => {
    const raw = String.raw`\frac{missing brace`;
    const { container } = render(<KgEvidenceBody elementType="formula" text={raw} />);

    expect(container.querySelector(".element-formula-raw")).not.toBeNull();
    expect(container.textContent).toBe(raw);
  });

  it("does not reinterpret ordinary evidence as math", () => {
    const text = String.raw`A paragraph mentioning \sigma without formula metadata.`;
    const { container } = render(<KgEvidenceBody elementType="paragraph" text={text} />);

    expect(container.querySelector(".katex")).toBeNull();
    expect(container.textContent).toBe(text);
  });
});
