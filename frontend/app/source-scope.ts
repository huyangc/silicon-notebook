/**
 * 「当前打开的来源属于哪个库」——来源详情弹窗的只读判定单一落点。
 *
 * 挂载参考库不等于获得该库的直接成员权限（红线）。来源详情/元素/图片改走按当前
 * active 笔记本代理读取的端点之后，用户能**看**到参考库来源了，但重新解析与删除
 * 仍然是 owner-only 的写入，挂载方对被挂库既不是 owner 也不是成员——渲染出这两个
 * 按钮只会 404。判定放在这里，弹窗渲染与两个防御性 handler 复用同一份，避免三处
 * 各写一遍 `!==` 而漂移。
 */
export function crossLibrarySourceNotebookId(
  sourceNotebookId: string,
  activeNotebookId: string | null,
): string {
  // activeNotebookId 缺席时不下「跨库」结论：那说明我们根本不知道"当前库"是谁，
  // 猜错的方向应该是保守地当同库（沿用既有行为），而不是凭空把来源标成参考库的。
  if (!activeNotebookId || !sourceNotebookId) return "";
  return sourceNotebookId === activeNotebookId ? "" : sourceNotebookId;
}
