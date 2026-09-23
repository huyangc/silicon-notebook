export function ownsWorkspaceRun(
  expectedRun: number,
  currentRun: number,
  expectedWorkspace: number,
  currentWorkspace: number,
  expectedNotebook: string,
  currentNotebook: string | null,
): boolean {
  return (
    expectedRun === currentRun
    && expectedWorkspace === currentWorkspace
    && expectedNotebook === currentNotebook
  );
}


export function workspaceRequestIsCurrent(
  cancelled: boolean,
  expectedWorkspace: number,
  currentWorkspace: number,
  expectedNotebook: string,
  currentNotebook: string | null,
): boolean {
  return (
    !cancelled
    && expectedWorkspace === currentWorkspace
    && expectedNotebook === currentNotebook
  );
}


export function notebookIsActive(
  expectedNotebook: string,
  currentNotebook: string | null,
): boolean {
  return expectedNotebook === currentNotebook;
}


export function sessionListRequestIsCurrent(
  expectedRequest: number,
  currentRequest: number,
  expectedNotebook: string,
  currentNotebook: string | null,
): boolean {
  return (
    expectedRequest === currentRequest
    && notebookIsActive(expectedNotebook, currentNotebook)
  );
}


export type NotebookRequest<T> = {
  notebookId: string;
  requestId: number;
  promise: Promise<T>;
};


export async function followLatestNotebookRequest<T>(
  initial: NotebookRequest<T>,
  latest: () => NotebookRequest<T> | null,
  isNotebookActive: () => boolean,
): Promise<{ requestId: number; generationId: number; value: T } | null> {
  let current = initial;
  let fallback: { requestId: number; value: T } | null = null;
  while (true) {
    let value: T;
    try {
      value = await current.promise;
    } catch (error) {
      const candidate = latest();
      if (
        candidate
        && candidate.notebookId === current.notebookId
        && candidate.requestId > current.requestId
      ) {
        current = candidate;
        continue;
      }
      if (fallback && isNotebookActive()) {
        return {
          ...fallback,
          // The fallback resolved the generation whose request failed. It is
          // safe to publish only while that generation is still current.
          generationId: current.requestId,
        };
      }
      throw error;
    }
    if (!isNotebookActive()) return null;

    const candidate = latest();
    if (
      candidate
      && candidate.notebookId === current.notebookId
      && candidate.requestId > current.requestId
    ) {
      fallback = { requestId: current.requestId, value };
      current = candidate;
      continue;
    }
    return {
      requestId: current.requestId,
      generationId: current.requestId,
      value,
    };
  }
}


export function historyModeForTransition(
  currentNotebookId: string | null,
  nextNotebookId: string,
): "push" | "replace" {
  return currentNotebookId === nextNotebookId ? "replace" : "push";
}


export async function restoreLatestConversation<T>(
  sessions: readonly { id: string }[],
  apply: (id: string) => Promise<T>,
): Promise<T | null> {
  if (!sessions[0]) return null;
  try {
    return await apply(sessions[0].id);
  } catch {
    return null;
  }
}


export async function openMemoryDeepLink(
  notebookId: string,
  open: (notebookId: string) => Promise<void>,
  fallback: () => void,
): Promise<boolean> {
  try {
    await open(notebookId);
    return true;
  } catch {
    fallback();
    return false;
  }
}


export const NOTEBOOK_PRIVATE_MEMORY_DELETE_WARNING =
  "所有成员各自绑定到此笔记本的私有记忆也会按生命周期一并删除。";


/**
 * 工作区的能力位 —— 「哪些入口画出来」的唯一判据。
 *
 * `canManageContent` 是 `NotebookSummary.can_manage_content`（群组知识共享 P2）:
 * 组管理员打开被共享进本组的库时 `access` 仍是 `"reader"`（权限档没有新增枚举值，
 * 裁决 P2-3），但他确实有内容管理权。所以内容管理那几位不能再只看 `access`。
 *
 * ⚠ 后端才是权威（`require_notebook_capability` / `notebook_capability_allowed`）。
 * 这里画多了按钮只会让用户点进一个必然 404 的动作，所以缺省一律取**收**的那一侧:
 * 参数省略 = false = 逐字复现本参数出现之前的行为（旧后端不发这个字段）。
 *
 * `canConfigureNotebook` 是**第三档、owner-only**（群组知识共享 P2-T2 评审 P0）:
 * 挂载配置（参考库增删）与链接分享是 owner 对本库检索范围与对外处置的配置，后端分别挂
 * `notebook:mount` 与 `notebook:configure`（都恒 owner），**不随内容管理权翻给组管理员**。它 = `!isReader`，
 * 与 `canWriteNotebook`（含组管理员）刻意分开——组管理员能加来源/建图谱/写 knowhow，
 * 但不能改挂载、不能铸/撤对外链接（否则会把库主从未共享的私有库挂进来经代理端点读
 * 全文、或替库主铸链接让组外人整本 copy）。判据只看 `access`，与 `canManageContent`
 * 无关。
 *
 * `syncOrigin` 是 `NotebookSummary.sync_origin`（跨环境增量同步 §5）:非空 = 这本库是
 * 从别的环境同步来的**镜像**。它是一条与「谁有权」**正交**的轴——镜像的 owner 权限一点
 * 没少,只是这本库的**同步层内容**不再允许在目标端被改（改了也留不住,下一次导入原样
 * 覆盖回去,用户得到的是「我明明改过」的静默数据丢失）。所以它不是给 `canWrite` 加一个
 * 与项,而是逐格按后端围栏表 `_CAPABILITY_MIRROR_FENCE` 映射:
 *
 *   挡(镜像上恒 false):`canWriteNotebook`/`canGovernKnowledge`/`canManageNotebookSchemas`
 *     （sources/kg/knowhow/knowledge/catalog:write）、`canManageNotebook`
 *     （notebook:manage = 改名与 tier）、`canMountBases`（notebook:mount）、
 *     `canDeleteNotebook`（notebook:delete）。
 *   放行(镜像上与本地库逐字相同):`canConfigureNotebook`（notebook:configure,链接分享
 *     的 share_token 是目标端自有列）、`canRebuildIndexes`（scale_index:write,检索索引是
 *     目标端自有派生产物,镜像**必须**能重建它）、`canGrantAccess`（notebook:grant,
 *     目标端自己的可见性由目标端管理）、`canManageReports`、理解底座（agent_profile:write）。
 *
 * 缺省 `""` = 本地库 = 逐字复现本参数出现之前的行为（旧后端不发这个字段）。
 */
export function workspaceCapabilities(
  access: string | undefined,
  role: string,
  canManageContent: boolean = false,
  syncOrigin: string = "",
) {
  const canWrite = access !== "reader" || canManageContent;
  const mirrored = Boolean(syncOrigin);
  const isOwner = access !== "reader";
  return {
    /** 这本库是不是同步来的镜像。只给**标注**用（来源面板那句说明），不要拿它在
     *  调用侧重新拼「能不能按」——那些判据已经逐格算在下面各位里。 */
    mirrored,
    /** 镜像的源环境标识;非镜像为空串。同样只给标注用。 */
    mirrorOrigin: syncOrigin,
    /**
     * 镜像**确实从这个人手里收走了**改名/tier/挂载那组控件 —— 即「要不是镜像,他本来
     * 有 `notebook:manage`」。
     *
     * 给的是**就地说明该不该出现**这个问题,不是权限问题:纯只读成员本来就没有那些控件,
     * 对他冒一句「镜像的名称…随源环境同步」是在解释一件他从未见过的事,只会占地方。
     * 判据写成 `mirrored && canWrite`(未加围栏的那个 canWrite)而不是
     * `mirrored && !canManageNotebook`——后者对只读成员恒真,正是要避开的那一格。
     */
    mirrorHidesNotebookManage: mirrored && canWrite,
    canWriteNotebook: canWrite && !mirrored,
    canGovernKnowledge: canWrite && !mirrored,
    /**
     * 检索索引（scale index）的构建/更新/全量重建入口。**= 未加镜像判定的那个
     * `canWrite`**:后端 `scale_index:write` 在围栏表里是 False,因为索引是目标端自有的
     * 派生产物、不在同步闭包里,而重建又恰恰是目标端唯一的修复手段——跟着
     * `canWriteNotebook` 走会让一本镜像库永远停在导入那一刻的索引上且无从修复。
     */
    canRebuildIndexes: canWrite,
    /**
     * 群组授权边的读写（`notebook:grant`,admin 档,组管理员 ✓）。同样**不加镜像与项**:
     * 围栏放行它——设计 §5 明文「目标端自己的可见性由目标端管理;导入只在首次创建笔记本
     * 时写入源端授权,之后不覆盖」。「分享」入口后面同时挂着它与 `canConfigureNotebook`
     * （链接分享,恒 owner,同样放行），所以那颗按钮跟这一位走,**不能**跟
     * `canWriteNotebook` 走——否则镜像的库主连自己这一侧的可见性都管不了。
     */
    canGrantAccess: canWrite,
    // owner 对本库对外处置（链接分享）的配置权，**恒 owner**。组管理员有内容管理权
    // （canWrite）但没有它——见函数头注释与 deps.py 的 notebook:configure。
    // 镜像上照常放行:share_token 是目标端自有列，不随同步走。
    canConfigureNotebook: isOwner,
    /**
     * 挂载参考库（`PUT /notebooks/{id}/bases` 与它专用的 mountable/mounted-by-count
     * 枚举）。级别与 `canConfigureNotebook` 同为恒 owner——P2-T2 评审 P0 的那套论证
     * （mountable 枚举库主全部私有库名、把私有库挂进共享库经代理端点读全文）逐字仍然
     * 成立——但**围栏上两者相反**:挂载写 `notebook_bases`，属同步层，镜像上要挡。
     * 后端已经按这条轴把它从 notebook:configure 拆成 notebook:mount，这里跟着拆。
     */
    canMountBases: isOwner && !mirrored,
    /**
     * 笔记本改名/描述性画像编辑（`PATCH /notebooks/{id}`）与 tier 切换
     * （`POST .../tier`）,即后端的 `notebook:manage`（admin 档,组管理员 ✓）。
     * 镜像上这些字段是**同步来的**，目标端改不留，故收起。
     */
    canManageNotebook: canWrite && !mirrored,
    /**
     * 删除笔记本（`notebook:delete`,恒 owner）。镜像上收起:§5 明文——镜像只能由同步
     * 导入退役，目标端删掉它只会让下一次导入把整本库重新造出来。
     */
    canDeleteNotebook: isOwner && !mirrored,
    // 深度报告对**只读成员也开放**（群组知识共享 P1）。它不是 `canWrite` 的一部分：
    // 报告按创建者行级隔离——成员只看得见、也只改得动**自己建的**那些，owner 也
    // 一样（不引入「owner 看全部」这条新披露）。所以列表里出现的每一份报告都是
    // 当前用户自己的，可操作性恒成立，没有需要按 access 收起来的动作。后端 9 个
    // 写端点各自挂 `require_notebook_read` + 体内 `reports.created_by == 当前用户`，
    // 这里放开的是**入口**，不是判定。
    //
    // 只放开报告面：来源写、图谱构建、知识治理仍跟着 `canWrite` 走。
    // P2 之后 `canWrite` 本身放宽了（组管理员为真），但这一位**恒 true 不变**——
    // 它本来就已经对每一位只读成员开着，没有可放宽的余地。
    canManageReports: true,
    // 图谱类型的有效配置属于当前笔记本：owner 与组管理员可以维护本库的覆盖和自建
    // 类型；纯只读成员不行。全局基线仍只允许系统管理员变更。
    // 图谱类型是 catalog:write，在同步闭包里 → 镜像上收起。
    canManageNotebookSchemas: canWrite && !mirrored,
    canManageGlobalSchemas: role === "admin",
  };
}


/**
 * 这一行笔记本是不是同步来的镜像 —— 只给**标注**用（列表行的「镜像」小标）。
 *
 * 与 `notebookRoleText` 并列:角色列答「我对这本库是什么身份」,这一位答「这本库的内容
 * 从哪来」,两件事不合并进同一个字符串（那一列的既有文案是持久契约,别动）。
 *
 * ⚠ 它**不是**「能不能按」的判据——那些一律走 `workspaceCapabilities` 的逐格位。这里
 * 单独开一个谓词,是为了组件不必自己写 `Boolean(notebook.sync_origin)`,判据仍只有这
 * 一处。
 */
export function notebookIsMirror(notebook: { sync_origin?: string }): boolean {
  return Boolean(notebook.sync_origin);
}


/** 列表行「镜像」小标的文案（角色列旁）。空串 = 本地库,不渲染。 */
export const NOTEBOOK_MIRROR_TAG = "镜像";


/**
 * 镜像上被收起的那两个入口,**就地**换成的说明（AGENTS.md「Interactive feedback」:
 * 结果落在原控件的位置上,不发页面顶部横幅）。文案说的是「为什么这里没有按钮」,
 * 不是「操作失败」——那颗按钮从来就不该出现在镜像上。
 */
// ⚠ 这一句同时顶替**三样**被收走的东西:改名（notebook:manage）、tier 切换
// （同上）、参考库挂载（notebook:mount）。挂载不能单开一句——镜像上整扇「笔记本设置」
// 弹窗都打不开（`openEditor` 的保存会同时写这三样，见 use-notebook-collection.ts），
// 那句话没有任何位置能显示出来。
export const MIRROR_NOTEBOOK_MANAGE_NOTE = "镜像的名称、档位与参考库挂载随源环境同步";
export const MIRROR_NOTEBOOK_DELETE_NOTE = "镜像只能由同步导入退役";


/** 来源面板顶部那一行标注。`origin` 取 `capabilities.mirrorOrigin`。 */
export function mirrorSourcesNotice(origin: string): string {
  return `镜像自 ${origin}，内容只能在源环境修改`;
}


/**
 * 笔记本列表「角色」列的文案。
 *
 * 此前那一列**整列写死 "Owner"**,于是只读共享进来的库也被标成所有者——与挂载选择器
 * 把别人的库标成「我的笔记本」是同一类事实错误的标签,同批修掉。判据按行取;
 * `override` 只给「群组」分区用(那一批的 `access` 同样是 reader,但「群组成员」比
 * 「只读成员」更准)。
 */
export function notebookRoleText(
  notebook: { access?: string },
  override?: string,
): string {
  if (override) return override;
  return (notebook.access ?? "owner") === "reader" ? "只读成员" : "Owner";
}


export function doneItemDestination(kind: string | undefined): "sources" | "kg" {
  return kind === "paper_meta_done" ? "sources" : "kg";
}
