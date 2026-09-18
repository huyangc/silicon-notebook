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
