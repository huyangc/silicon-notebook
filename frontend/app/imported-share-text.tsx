"use client";

import { useState } from "react";
import { importedShareReferences } from "./imported-share-references";
import { PublicCitationPopover } from "./public-citation-popover";
import type { PublicReportReferenceT } from "./public-report";

export function ImportedShareText({ text, references }: { text: string; references: unknown }) {
  const refs = importedShareReferences(references);
  const byKey = new Map(refs.map((ref) => [ref.key, ref]));
  const [selected, setSelected] = useState<{ reference: PublicReportReferenceT; rect: DOMRect } | null>(null);
  return <>
    <p>{text.split(/(\[t\d+-k?\d+\])/g).map((part, index) => {
      const key = part.slice(1, -1);
      const ref = part.startsWith("[") ? byKey.get(key) : undefined;
      return ref ? <button key={index} type="button" className="link-button inline-action"
        aria-label={`查看引用 ${key}`} onClick={(event) => setSelected({ reference: ref, rect: event.currentTarget.getBoundingClientRect() })}>{part}</button> : part;
    })}</p>
    {selected && <PublicCitationPopover key={selected.reference.key} reference={selected.reference}
      displayLabel={`[${selected.reference.key}]`} anchorRect={selected.rect} onClose={() => setSelected(null)} />}
  </>;
}
