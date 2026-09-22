/**
 * manual-markdown.ts
 *
 * 站内「使用手册」页(`/manual`)渲染 `docs/user-manual_zh.md` 时用到的两个纯函数。
 * 手册的真源是仓库里那份 Markdown,页面在构建期把它整篇读进来;这里只负责让它在
 * 网页里「站得住」:目录锚点能跳、指向仓库其它文档的相对链接不变成死链。
 */

/**
 * 与 GitHub 渲染 Markdown 标题时生成的锚点同一口径:小写、去掉标点(保留字母/数字/
 * 空白/连字符)、空白换成连字符。手册的目录就是按这个口径手写的
 * (「## 8. 沉淀成果:记忆与 Knowhow 表」→ `8-沉淀成果记忆与-knowhow-表`),所以站内
 * 页面必须给标题挂上同样的 id,同一份 Markdown 才能在 GitHub 与站内两边都能跳。
 */
export function headingSlug(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s-]/gu, "")
    .replace(/\s+/g, "-");
}

/**
 * 手册里指向仓库其它 Markdown 文档的相对链接(`../README_zh.md`、
 * `./product-and-api_zh.md`)在站内没有对应页面——那些是开发者文档,不随前端部署。
 * 这类链接按纯文本渲染,而不是留一个 404。站内锚点(`#…`)与 http(s) 链接不受影响。
 */
export function isRepoDocLink(href: string | undefined): boolean {
  if (!href) return false;
  const [pathPart] = href.split("#");
  return /\.md$/i.test(pathPart);
}
