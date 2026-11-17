"use client";

import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { visit, SKIP } from "unist-util-visit";
import type { Root, PhrasingContent } from "mdast";
import type { PluggableList } from "unified";
import { AnswerMarkdown } from "./answer-markdown";
import { importedShareReferences } from "./imported-share-references";
import { PublicCitationPopover } from "./public-citation-popover";
import type { PublicReportReferenceT } from "./public-report";

type Selection = { reference: PublicReportReferenceT; rect: DOMRect };
const ImportedCitationContext = createContext<{
  byKey: Map<string, PublicReportReferenceT>;
  selected: Selection | null;
  select: (value: Selection) => void;
} | null>(null);

function remarkImportedCitations(byKey: Map<string, PublicReportReferenceT>) {
  return (tree: Root) => {
    visit(tree, "text", (node, index, parent) => {
      if (!parent || index === undefined || parent.type === "link" || parent.type === "linkReference") return;
      const pattern = /\[t\d+-k?\d+\]/g;
      const children: PhrasingContent[] = [];
      let end = 0;
      for (const match of node.value.matchAll(pattern)) {
        const key = match[0].slice(1, -1);
        if (!byKey.has(key)) continue;
        if (match.index > end) children.push({ type: "text", value: node.value.slice(end, match.index) });
        children.push({ type: "link", url: `cite:imported:${key}`, children: [{ type: "text", value: match[0] }] });
        end = match.index + match[0].length;
      }
      if (!children.length) return;
      if (end < node.value.length) children.push({ type: "text", value: node.value.slice(end) });
      parent.children.splice(index, 1, ...children);
      return [SKIP, index + children.length];
    });
  };
}

function ImportedCitationLink({ href, children }: { href?: string; children?: ReactNode }) {
  const context = useContext(ImportedCitationContext);
  const key = href?.startsWith("cite:imported:") ? href.slice("cite:imported:".length) : "";
  const reference = context?.byKey.get(key);
  if (!context || !reference) return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
  const selected = context.selected?.reference.key === key;
  return <button type="button" className={`cite-chip${selected ? " active" : ""}`} aria-expanded={selected}
    aria-label={`查看引用 ${key}`} onClick={(event) => context.select({ reference, rect: event.currentTarget.getBoundingClientRect() })}>{children}</button>;
}
const IMPORTED_COMPONENTS = { a: ImportedCitationLink };

export function ImportedShareText({ text, references }: { text: string; references: unknown }) {
  const byKey = useMemo(() => new Map(importedShareReferences(references).map((ref) => [ref.key, ref])), [references]);
  const plugins = useMemo<PluggableList>(() => [[remarkImportedCitations, byKey]], [byKey]);
  const [selected, setSelected] = useState<Selection | null>(null);
  return <>
    <ImportedCitationContext.Provider value={{ byKey, selected, select: setSelected }}>
      <AnswerMarkdown answer={text} allowImages={false} extraRemarkPlugins={plugins}
        components={IMPORTED_COMPONENTS} onReferenceClick={() => undefined} />
    </ImportedCitationContext.Provider>
    {selected && <PublicCitationPopover key={selected.reference.key} reference={selected.reference}
      displayLabel={`[${selected.reference.key}]`} anchorRect={selected.rect} onClose={() => setSelected(null)} />}
  </>;
}
