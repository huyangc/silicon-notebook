// 站内「使用手册」路由(`/manual`)。
//
// 手册的真源是仓库里的 `docs/user-manual_zh.md`。这个路由是服务端组件,在 `next build`
// 时从文件系统读一次、预渲染成静态页;离线打包(scripts/pack.sh)只带走 `.next` 产物,
// 不带 `docs/` 目录,所以正文必须在构建期读进来,而不是运行期再去找文件。
// 手册改了要重新构建前端才会生效——与部署方其它前端改动同一口径。
//
// 它和公开分享页一样不经登录门:拿到地址的人直接能看。

import { readFile } from "node:fs/promises";
import path from "node:path";

import { ManualView } from "./manual-view.tsx";

export const metadata = { title: "使用手册" };

const MANUAL_PATH = path.resolve(process.cwd(), "..", "docs", "user-manual_zh.md");

export default async function ManualPage() {
  const markdown = await readFile(MANUAL_PATH, "utf8");
  return <ManualView markdown={markdown} />;
}
