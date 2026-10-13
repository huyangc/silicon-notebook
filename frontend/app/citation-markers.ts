// Keep evidence-key syntax aligned with backend/services/citation_markers.py.
const keyGroup = String.raw`k\d+(?:\s*[,，、]\s*k\d+)*`;
const displayGroup = String.raw`k?\d+(?:\s*[,，、]\s*k?\d+)*`;

export const ANCHOR_MARKER_GROUP_RE = new RegExp(
  String.raw`(?:\[${keyGroup}\]|【${keyGroup}】|\(${keyGroup}\)|（${keyGroup}）)`, "g",
);

// Numeric parentheses remain ordinary prose, even when a reference has that number.
export const CITATION_MARKER_RE = new RegExp(
  String.raw`(?:\[${displayGroup}\]|【${displayGroup}】|\(${keyGroup}\)|（${keyGroup}）)`, "g",
);

export function markerKeys(marker: string): string[] {
  return marker.slice(1, -1).split(/[,，、]/).map((part) => part.trim()).filter(Boolean);
}
