import type { Element, Parent, Root } from "hast";

export type CitationImageSlotItem = Readonly<{
  citationKey: string;
  imageId: string;
}>;

export type CitationImageIdsByKey = Readonly<Record<string, readonly string[]>>;

const SLOT_ATTRIBUTE = "data-citation-image-items";

function isElement(node: Parent["children"][number]): node is Element {
  return node.type === "element";
}

function citationKeys(node: Element): string[] {
  const keys: string[] = [];
  const walk = (parent: Parent) => {
    for (const child of parent.children) {
      if (!isElement(child)) continue;
      if (child.tagName === "a") {
        const href = child.properties?.href;
        if (typeof href === "string" && href.startsWith("cite:")) {
          keys.push(href.slice(5));
        }
      }
      walk(child);
    }
  };
  walk(node);
  return keys;
}

function imageSlot(items: CitationImageSlotItem[]): Element {
  return {
    type: "element",
    tagName: "aside",
    properties: {
      [SLOT_ATTRIBUTE]: JSON.stringify(items),
    },
    children: [],
  };
}

function isDirectCitationBlock(node: Element): boolean {
  return node.tagName === "p" || /^h[1-6]$/.test(node.tagName);
}

/**
 * Insert one block-level image slot immediately after the smallest rendered
 * Markdown block containing an image-bearing citation.
 *
 * The citation badge itself remains phrasing content.  Putting the image
 * component inside that badge would create invalid shapes such as
 * `<p><div>...</div></p>` and would break lists/tables.  This HAST pass runs
 * after `remarkCitations`, sees the final `cite:` links, and inserts a sibling
 * `<aside>` instead.
 *
 * Tables are deliberately atomic: a citation in any cell places its image
 * slot after the complete table, never inside a cell.  Repeated assets are
 * emitted only at their first visible citation in document order.
 */
export function rehypeCitationImages(imageIdsByKey: CitationImageIdsByKey) {
  return (tree: Root) => {
    const seenImages = new Set<string>();

    const itemsFor = (node: Element): CitationImageSlotItem[] => {
      const items: CitationImageSlotItem[] = [];
      for (const key of citationKeys(node)) {
        for (const imageId of imageIdsByKey[key] ?? []) {
          if (!imageId || seenImages.has(imageId)) continue;
          seenImages.add(imageId);
          items.push({ citationKey: key, imageId });
        }
      }
      return items;
    };

    const process = (parent: Parent) => {
      const next: Parent["children"] = [];
      for (const child of parent.children) {
        if (!isElement(child)) {
          next.push(child);
          continue;
        }

        if (child.tagName === "table" || isDirectCitationBlock(child)) {
          const items = itemsFor(child);
          next.push(child);
          if (items.length > 0) next.push(imageSlot(items));
          continue;
        }

        process(child);
        // Tight Markdown list items may expose phrasing children directly
        // instead of wrapping them in a paragraph.  Keep their slot inside
        // the list item; nested paragraphs/tables have already claimed their
        // assets through seenImages, so this cannot duplicate them.
        if (child.tagName === "li") {
          const items = itemsFor(child);
          if (items.length > 0) child.children.push(imageSlot(items));
        }
        next.push(child);
      }
      parent.children = next;
    };

    process(tree);
  };
}

export function citationImageSlotItems(value: unknown): CitationImageSlotItem[] {
  if (typeof value !== "string") return [];
  try {
    const rows = JSON.parse(value);
    if (!Array.isArray(rows)) return [];
    return rows.flatMap((row) => {
      if (!row || typeof row !== "object") return [];
      const citationKey = "citationKey" in row ? row.citationKey : "";
      const imageId = "imageId" in row ? row.imageId : "";
      return typeof citationKey === "string" && typeof imageId === "string"
        && citationKey && imageId
        ? [{ citationKey, imageId }]
        : [];
    });
  } catch {
    return [];
  }
}

export { SLOT_ATTRIBUTE as CITATION_IMAGE_SLOT_ATTRIBUTE };
