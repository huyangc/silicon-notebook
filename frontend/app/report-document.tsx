"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

type ReportHeading = {
  element: HTMLHeadingElement;
  title: string;
  level: number;
};

/** Read the rendered Markdown so fences, inline formatting and duplicate titles
 * follow the very same structure the reader sees, without parsing it twice. */
export function ReportDocument({ markdown, children }: { markdown: string; children: ReactNode }) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const [headings, setHeadings] = useState<ReportHeading[]>([]);
  const [selectedHeading, setSelectedHeading] = useState<HTMLHeadingElement | null>(null);

  useEffect(() => {
    const elements = bodyRef.current?.querySelectorAll<HTMLHeadingElement>(
      ".report-markdown h1, .report-markdown h2, .report-markdown h3, "
      + ".report-markdown h4, .report-markdown h5, .report-markdown h6",
    );
    setHeadings(Array.from(elements ?? []).flatMap((element) => {
      const label = element.cloneNode(true) as HTMLHeadingElement;
      // KaTeX emits both accessible MathML and a visual formula. Count it once.
      label.querySelectorAll(".katex-mathml").forEach((node) => node.remove());
      const title = label.textContent?.trim() ?? "";
      return title ? [{ element, title, level: Number(element.tagName.slice(1)) }] : [];
    }));
    setSelectedHeading(null);
  }, [markdown]);

  const baseLevel = headings.length ? Math.min(...headings.map((heading) => heading.level)) : 1;

  return (
    <div className={`report-document${headings.length ? " has-contents" : ""}`}>
      {headings.length > 0 && (
        <nav className="report-contents" aria-label="报告目录">
          <strong className="report-contents-title">目录</strong>
          <ol>
            {headings.map((heading, index) => (
              <li key={index} style={{ paddingInlineStart: `${(heading.level - baseLevel) * 12}px` }}>
                <button
                  type="button"
                  className="report-contents-link"
                  aria-current={selectedHeading === heading.element ? "location" : undefined}
                  onClick={() => {
                    // 标题住在折叠节（参考文献）的 summary 里时，跳过去顺手展开它：
                    // 从目录点「参考文献」就是要看那份清单。
                    const fold = heading.element.closest("summary")?.parentElement;
                    if (fold instanceof HTMLDetailsElement) fold.open = true;
                    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
                    heading.element.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
                    heading.element.tabIndex = -1;
                    heading.element.focus({ preventScroll: true });
                    setSelectedHeading(heading.element);
                  }}
                >
                  {heading.title}
                </button>
              </li>
            ))}
          </ol>
        </nav>
      )}
      <div className="report-document-body" ref={bodyRef}>{children}</div>
    </div>
  );
}
