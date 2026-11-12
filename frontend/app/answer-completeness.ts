/** Keep a server-written completeness notice outside model Markdown renderers. */
export function answerBodyWithoutCompletenessNotice(
  answerText: string,
  notice: string | undefined,
): string {
  if (!notice) return answerText;
  if (answerText === notice) return "";
  for (const suffix of [`\n\n> ${notice}`, `\n\n${notice}`]) {
    if (answerText.endsWith(suffix)) return answerText.slice(0, -suffix.length);
  }
  return answerText;
}

/** Remove only the exact server-written ranking prefix, including its multiline form. */
export function answerBodyWithoutRankingNotice(
  answerText: string,
  notice: string | undefined,
): string {
  if (!notice) return answerText;
  if (answerText === notice) return "";
  const prefix = `> ${notice}\n\n`;
  return answerText.startsWith(prefix) ? answerText.slice(prefix.length) : answerText;
}

export function rankingNoticeLines(notice: string): string[] {
  return notice.split("\n");
}
