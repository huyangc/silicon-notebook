// 页面自己发起的整页重载:先记一笔,再重载。
//
// 上传进行中挂着 `beforeunload` 提示(notebook-import-upload.ts),防的是用户手滑关掉或刷新
// 标签页。页面自己因为会话失效(401)或别的标签页换了登录而重载时,上传本来就进行不下去,
// 再弹一句「离开此页？」只会挡住那次重载——所以提示先看这一笔。

let reloading = false;

/** 记下「这是页面自己要重载」,然后重载。 */
export function reloadPage(): void {
  reloading = true;
  window.location.reload();
}

/** 当前这次卸载是不是 `reloadPage` 发起的。 */
export function pageReloading(): boolean {
  return reloading;
}
