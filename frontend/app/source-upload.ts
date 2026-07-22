import type { UploadedSource } from "./workspace-model.ts";

export type UploadOutcome = {
  /** 这次真正新建的来源——只有这些才该计入来源总数。 */
  added: UploadedSource[];
  /** 内容与本笔记本里已有来源完全相同，被沿用的那些（后端没有新建行）。 */
  reused: UploadedSource[];
  /** reused 的子集：这次顺手把文档类型改成了别的那些。后端会把新类型写进原来
   *  那条来源，并按新类型重新抽取知识——所以文案不能只说「沿用，没做别的」。 */
  retyped: UploadedSource[];
  /** 上传完成后给用户看的一句话（叫 toast 不叫 message：`.message` 在前端是
   *  「读到了某个原始错误」的守卫信号，这里是我们自己写的界面文案）。 */
  toast: string;
};

/** 把上传返回值拆成「新增的」「沿用既有的」「沿用且改了文档类型的」，并给出如实
 *  的提示文案。
 *
 *  后端在同一个笔记本内按文件内容判重：同一份内容再传一次会把原来那条来源直接
 *  返回，而不是建第二条。所以 `uploaded.length` 不等于「新增了几个」——拿它去加
 *  来源总数，数字会一直偏大到重新打开笔记本为止。
 *
 *  文案要顺带交代两件用户看得见的事：沿用的那条保留原来的名称（同一份内容改个
 *  名字再传，界面上仍是原来的名字）；而如果这次改了文档类型，那条来源会按新类型
 *  重新抽取知识——「类型判错了，我改一下再传一遍」是真的生效了，不是 no-op。
 *
 *  `previousDocTypes` 是上传前界面上已知的「来源 id → 文档类型」。判「改没改」只
 *  能靠它：后端返回的是改完之后的值，单看返回值区分不出「本来就是这个类型」。查
 *  不到的（比如那条来源不在当前这一页里）一律当作没改，宁可少说也不谎报。 */
export function summarizeUpload(
  uploaded: UploadedSource[],
  previousDocTypes: ReadonlyMap<string, string> = new Map(),
): UploadOutcome {
  const added = uploaded.filter((item) => item.reused !== true);
  const reused = uploaded.filter((item) => item.reused === true);
  const retyped = reused.filter((item) => {
    const before = previousDocTypes.get(item.id);
    return before !== undefined && (item.doc_type ?? "") !== before;
  });
  const keptAsIs = reused.filter((item) => !retyped.includes(item));
  return { added, reused, retyped, toast: uploadToast(added.length, keptAsIs.length, retyped) };
}

function uploadToast(added: number, keptAsIs: number, retyped: UploadedSource[]): string {
  const clauses: string[] = [];
  if (keptAsIs > 0) {
    clauses.push(
      `${keptAsIs} 个文件的内容已经在本笔记本里，沿用原有来源（名称保持原样），没有重复添加`,
    );
  }
  if (retyped.length > 0) {
    // 「正在重新抽取」只在后端真的把那条来源翻成 extracting 时才说：笔记本还没
    // 建过知识图谱时只记下新类型、不会立刻开抽，谎报会让用户白等。
    const rerunning = retyped.some((item) => item.parse_status === "extracting");
    clauses.push(
      `${retyped.length} 个文件的内容已经在本笔记本里，沿用原有来源并改用了新的文档类型` +
        (rerunning ? "，正在按新类型重新抽取知识" : ""),
    );
  }
  if (clauses.length === 0) return `已上传 ${added} 个来源`;
  const sameContent = clauses.join("；");
  return added === 0 ? sameContent : `已上传 ${added} 个来源；另有 ${sameContent}`;
}
