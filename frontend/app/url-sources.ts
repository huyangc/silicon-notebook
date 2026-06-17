// 把多行文本解析成去重后的 http/https 列表（前端只做轻校验；
// 是否真的是 PDF 由后端 probe_pdf 判定）。
export function parseUrlLines(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const url = raw.trim();
    if (!url) continue;
    if (!/^https?:\/\//i.test(url)) continue;
    if (seen.has(url)) continue;
    seen.add(url);
    out.push(url);
  }
  return out;
}
