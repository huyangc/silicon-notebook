"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

type ReportHeading = {
  element: HTMLHeadingElement;
  title: string;
  level: number;
  parent: number | null;
  children: number[];
};

function readingPath(headings: ReportHeading[], index: number): Set<number> {
  const path = new Set<number>();
  for (let cursor: number | null = index; cursor !== null; cursor = headings[cursor].parent) {
    path.add(cursor);
  }
  return path;
}

function scrollingAncestors(element: HTMLElement): HTMLElement[] {
  const ancestors: HTMLElement[] = [];
  for (let parent = element.parentElement; parent; parent = parent.parentElement) {
    if (/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) ancestors.push(parent);
  }
  return ancestors;
}

function readingBounds(containers: HTMLElement[]) {
  let top = 0;
  let bottom = window.innerHeight;
  for (const container of containers) {
    const rect = container.getBoundingClientRect();
    top = Math.max(top, rect.top);
    bottom = Math.min(bottom, rect.bottom);
  }
  return { top, bottom };
}

function keepCurrentVisible(nav: HTMLElement) {
  const active = nav.querySelector<HTMLElement>('[aria-current="location"]');
  if (!active) return;
  const viewport = readingBounds(scrollingAncestors(nav));
  const bounds = nav.getBoundingClientRect();
  const top = Math.max(viewport.top, bounds.top);
  const bottom = Math.min(viewport.bottom, bounds.bottom);
  if (bottom <= top) return;
  const item = active.getBoundingClientRect();
  // Scroll only the contents, never the report or its surrounding panels.
  if (item.top < top) nav.scrollTop += item.top - top;
  else if (item.bottom > bottom) nav.scrollTop += item.bottom - bottom;
}

/** Read the rendered Markdown so fences, inline formatting and duplicate titles
 * follow the very same structure the reader sees, without parsing it twice. */
export function ReportDocument({ markdown, children }: { markdown: string; children: ReactNode }) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const contentsRef = useRef<HTMLElement>(null);
  const [headings, setHeadings] = useState<ReportHeading[]>([]);
  const [selectedHeading, setSelectedHeading] = useState<HTMLHeadingElement | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  useEffect(() => {
    const elements = bodyRef.current?.querySelectorAll<HTMLHeadingElement>(
      ".report-markdown h1, .report-markdown h2, .report-markdown h3, "
      + ".report-markdown h4, .report-markdown h5, .report-markdown h6",
    );
    const next: ReportHeading[] = Array.from(elements ?? []).flatMap((element) => {
      const label = element.cloneNode(true) as HTMLHeadingElement;
      // KaTeX emits both accessible MathML and a visual formula. Count it once.
      label.querySelectorAll(".katex-mathml").forEach((node) => node.remove());
      const title = label.textContent?.trim() ?? "";
      return title ? [{ element, title, level: Number(element.tagName.slice(1)), parent: null, children: [] }] : [];
    });
    const stack: number[] = [];
    // A single leading h1 is the report title, not a fold around every chapter.
    const hasReportTitle = next[0]?.level === 1 && next.filter((heading) => heading.level === 1).length === 1;
    next.forEach((heading, index) => {
      if (hasReportTitle && index === 0) return;
      while (stack.length && next[stack[stack.length - 1]].level >= heading.level) stack.pop();
      const parent = stack[stack.length - 1] ?? null;
      heading.parent = parent;
      if (parent !== null) next[parent].children.push(index);
      stack.push(index);
    });
    setHeadings(next);
    setSelectedHeading(next[0]?.element ?? null);
    setExpanded(new Set());
  }, [markdown]);

  useEffect(() => {
    const body = bodyRef.current;
    if (!body || !headings.length) return;
    let frame: number | null = null;
    let current: HTMLHeadingElement | null = null;
    const scrollContainers = scrollingAncestors(body);
    const update = () => {
      frame = null;
      const { top, bottom } = readingBounds(scrollContainers);
      const nav = contentsRef.current;
      if (nav && bottom > top) {
        const bounds = nav.getBoundingClientRect();
        if (bounds.top < bottom && bounds.bottom > top) {
          const height = `${bottom - Math.max(top, bounds.top)}px`;
          if (nav.style.getPropertyValue("--report-contents-available-height") !== height) {
            nav.style.setProperty("--report-contents-available-height", height);
            keepCurrentVisible(nav);
          }
        }
      }
      const rect = body.getBoundingClientRect();
      if (bottom <= top || rect.bottom <= top || rect.top >= bottom) return;
      const center = (top + bottom) / 2;
      let active = 0;
      headings.forEach((heading, index) => {
        // A closed references fold can contain headings that have no position.
        if (!heading.element.getClientRects().length) return;
        if (heading.element.getBoundingClientRect().top <= center) active = index;
      });
      const element = headings[active].element;
      if (element === current) return;
      current = element;
      setSelectedHeading(element);
      setExpanded(readingPath(headings, active));
    };
    const schedule = () => {
      if (frame === null) frame = window.requestAnimationFrame(update);
    };
    update();
    // Capture also sees scrolling inside the notebook workspace's own panels.
    window.addEventListener("scroll", schedule, true);
    window.addEventListener("resize", schedule);
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(schedule) : null;
    observer?.observe(body);
    scrollContainers.forEach((container) => observer?.observe(container));
    return () => {
      window.removeEventListener("scroll", schedule, true);
      window.removeEventListener("resize", schedule);
      observer?.disconnect();
      if (frame !== null) window.cancelAnimationFrame(frame);
    };
  }, [headings]);

  useEffect(() => {
    const nav = contentsRef.current;
    if (nav) keepCurrentVisible(nav);
  }, [selectedHeading]);

  let visibleCurrent = headings.findIndex((heading) => heading.element === selectedHeading);
  if (visibleCurrent >= 0) {
    for (let parent = headings[visibleCurrent].parent; parent !== null; parent = headings[parent].parent) {
      if (!expanded.has(parent)) visibleCurrent = parent;
    }
  }

  function renderEntries(indices: number[]) {
    return <ol>{indices.map((index) => {
      const heading = headings[index];
      const hasChildren = heading.children.length > 0;
      const open = expanded.has(index);
      return (
        <li key={index}>
          <div className="report-contents-row">
            {hasChildren ? (
              <button
                type="button"
                className="report-contents-toggle"
                aria-label={`${open ? "收起" : "展开"}「${heading.title}」子目录`}
                aria-expanded={open}
                onClick={() => setExpanded((previous) => {
                  const next = new Set(previous);
                  if (next.has(index)) next.delete(index);
                  else next.add(index);
                  return next;
                })}
              >{open ? "▾" : "▸"}</button>
            ) : <span className="report-contents-toggle-spacer" />}
            <button
              type="button"
              className="report-contents-link"
              aria-current={visibleCurrent === index ? "location" : undefined}
              onClick={() => {
                const fold = heading.element.closest("summary")?.parentElement;
                if (fold instanceof HTMLDetailsElement) fold.open = true;
                const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
                heading.element.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "center" });
                heading.element.tabIndex = -1;
                heading.element.focus({ preventScroll: true });
                setSelectedHeading(heading.element);
                setExpanded(readingPath(headings, index));
              }}
            >{heading.title}</button>
          </div>
          {hasChildren && open && renderEntries(heading.children)}
        </li>
      );
    })}</ol>;
  }

  return (
    <div className={`report-document${headings.length ? " has-contents" : ""}`}>
      {headings.length > 0 && (
        <nav className="report-contents" aria-label="报告目录" ref={contentsRef}>
          <strong className="report-contents-title">目录</strong>
          <div className="report-contents-actions">
            <button type="button" className="link-button" onClick={() => setExpanded(new Set(headings.map((_, index) => index)))}>全部展开</button>
            <button type="button" className="link-button" onClick={() => setExpanded(new Set())}>全部收起</button>
          </div>
          {selectedHeading && <p className="report-contents-current">正在阅读：{headings.find((heading) => heading.element === selectedHeading)?.title}</p>}
          {renderEntries(headings.flatMap((heading, index) => heading.parent === null ? [index] : []))}
        </nav>
      )}
      <div className="report-document-body" ref={bodyRef}>{children}</div>
    </div>
  );
}
