import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LatexText } from "../../app/answer-panel";

describe("direct KaTeX formula rendering", () => {
  it("strips Markdown delimiters from formula knowledge-object headlines", () => {
    const { container } = render(
      <LatexText text={"$$P^{\\mathrm{dyn}} = \\alpha_{\\mathrm{DRAM}}$$"} isFormula />,
    );

    expect(container.querySelector(".katex")).not.toBeNull();
    expect(container.querySelector(".katex-error")).toBeNull();
    expect(container.textContent).toContain("Pdyn=αDRAM");
  });

  it("keeps malformed formulas visibly available as raw text", () => {
    const raw = "$$\\frac{missing brace$$";
    const { container } = render(<LatexText text={raw} isFormula />);

    expect(container.querySelector(".math-render-fallback")).not.toBeNull();
    expect(container.textContent).toBe(raw);
  });
});
