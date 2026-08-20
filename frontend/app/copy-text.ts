/** Copy without ever rejecting. A false result means the UI should offer manual copying. */
export async function copyTextSafely(text: string): Promise<boolean> {
  if (!text) return false;
  if (typeof navigator !== "undefined" && typeof navigator.clipboard?.writeText === "function") {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Clipboard API can exist while being denied by browser permissions or an
      // insecure context. Continue into the DOM fallback instead of making the
      // copy button appear inert.
    }
  }
  if (typeof document === "undefined" || typeof document.execCommand !== "function") return false;
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  try {
    textarea.select();
    return document.execCommand("copy") === true;
  } catch {
    return false;
  } finally {
    textarea.remove();
  }
}
