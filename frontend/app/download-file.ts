// Blob -> 临时 URL -> 触发一次下载。
//
// 鉴权下载的既有做法(报告导出、知识表模板):先用带令牌的请求把文件读成 Blob,再用
// `<a download>` 交给浏览器。`blob:` URL 不携带原响应的 Content-Disposition,所以文件名必须
// 由调用方显式给出。报告导出那两个 helper 把文件名写死在函数里(`reports.zip` /
// `report-<id>.md`),这里是同一套做法、文件名可传。
//
// 生产前端跑在 http://<IP>:3000,不是 Secure Context:这里只用 Blob / createObjectURL /
// `<a download>`,不碰 navigator.clipboard、crypto.randomUUID 之类的受限 API。
export function saveBlobAsFile(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}
