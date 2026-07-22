import type { UploadedSource } from "./workspace-model.ts";

export type UploadOutcome = {
  /** 这次真正新建的来源——只有这些才该计入来源总数。 */
  added: UploadedSource[];
  /** 内容与本笔记本里已有来源完全相同，被沿用的那些（后端没有新建行）。 */
  reused: UploadedSource[];
  /** 上传完成后给用户看的一句话（叫 toast 不叫 message：`.message` 在前端是
   *  「读到了某个原始错误」的守卫信号，这里是我们自己写的界面文案）。 */
  toast: string;
};

/** 把上传返回值拆成「新增的」与「沿用既有的」，并给出如实的提示文案。
 *
 *  后端在同一个笔记本内按文件内容判重：同一份内容再传一次会把原来那条来源直接
 *  返回，而不是建第二条。所以 `uploaded.length` 不等于「新增了几个」——拿它去加
 *  来源总数，数字会一直偏大到重新打开笔记本为止。
 *
 *  文案要顺带交代一件用户看得见的事：沿用的那条保留原来的名称，所以把同一份内容
 *  改个名字再传一次，界面上仍然显示原来的名字。 */
export function summarizeUpload(uploaded: UploadedSource[]): UploadOutcome {
  const added = uploaded.filter((item) => item.reused !== true);
  const reused = uploaded.filter((item) => item.reused === true);
  return { added, reused, toast: uploadToast(added.length, reused.length) };
}

function uploadToast(added: number, reused: number): string {
  if (reused === 0) return `已上传 ${added} 个来源`;
  const sameContent =
    `${reused} 个文件的内容已经在本笔记本里，沿用原有来源（名称保持原样），没有重复添加`;
  return added === 0 ? sameContent : `已上传 ${added} 个来源；另有 ${sameContent}`;
}
