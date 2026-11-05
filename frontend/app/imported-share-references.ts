import type { PublicReportReferenceT } from "./public-report.ts";

/** Uploaded manifests are untrusted; only presentation fields enter the card. */
export function importedShareReferences(value: unknown): PublicReportReferenceT[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object" || typeof item.key !== "string" || !/^t\d+-k?\d+$/.test(item.key)) return [];
    const text = (key: string) => typeof item[key] === "string" ? item[key] : "";
    const e = item.evidence;
    const evidence = e && typeof e === "object" && ["original", "knowledge", "memory"].includes(e.kind)
      && typeof e.text === "string" ? { kind: e.kind, text: e.text,
        quote: typeof e.quote === "string" ? e.quote : "", location: typeof e.location === "string" ? e.location : "",
        truncated: e.truncated === true } : undefined;
    const verification = item.verification === "changed" || item.verification === "source_gone" || item.verification === "unverifiable"
      ? item.verification : undefined;
    return [{ key: item.key, title: text("title"), file_name: text("file_name"),
      location: text("location"), snippet: text("snippet"), evidence, verification,
      is_memory: item.is_memory === true,
      title_truncated: item.title_truncated === true, file_name_truncated: item.file_name_truncated === true,
      snippet_truncated: item.snippet_truncated === true }];
  });
}
