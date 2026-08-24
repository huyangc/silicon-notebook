import type { UploadedSource } from "./workspace-model.ts";
import { findMarkdownImages } from "./md-bundle.ts";

export const STAGED_FILE_NAME_MAX_LENGTH = 48;

type SizedSourceFile = { name: string; size: number };

/** Human-readable form of the exact byte cap sent by the backend. */
export function sourceUploadSizeLabel(maxBytes: number): string {
  const mib = 1024 * 1024;
  const kib = 1024;
  if (maxBytes % mib === 0) return `${maxBytes / mib} MB`;
  if (maxBytes % kib === 0) return `${maxBytes / kib} KB`;
  return `${maxBytes} 字节`;
}

/** Split selected files by the current server-issued per-file byte cap.
 *
 * A missing value means the authenticated configuration has not arrived yet;
 * preserve the selection and let the server's authoritative 413 guard decide
 * rather than guessing a stale client-side limit.
 */
export function splitFilesByUploadSize<T extends SizedSourceFile>(
  files: readonly T[],
  maxBytes: number | null,
): { accepted: T[]; rejected: T[] } {
  if (maxBytes === null || !Number.isSafeInteger(maxBytes) || maxBytes <= 0) {
    return { accepted: [...files], rejected: [] };
  }
  return {
    accepted: files.filter((file) => file.size <= maxBytes),
    rejected: files.filter((file) => file.size > maxBytes),
  };
}

export type SkippedStagedFile = { name: string; reason: string };
export type StagedFileWarning = { name: string; reason: string };

type MarkdownFileLike = {
  name: string;
  text(): Promise<string>;
};

/** Warn when a standalone Markdown file points at image bytes it does not carry.
 *
 * This is advisory, not an admission gate: text-only ingestion remains useful,
 * but the user must not discover only after Ask that every relative image was
 * absent. ZIP/folder intake has its own pairing receipt and is not passed here.
 */
export async function standaloneMarkdownImageWarnings(
  file: MarkdownFileLike,
): Promise<StagedFileWarning[]> {
  if (!/\.(?:md|markdown)$/i.test(file.name)) return [];
  let refs: ReturnType<typeof findMarkdownImages>;
  try {
    refs = findMarkdownImages(await file.text());
  } catch {
    // Advisory inspection must never turn an otherwise valid upload into a
    // staging failure. The authoritative parser still handles the file.
    return [];
  }
  let local = 0;
  let remote = 0;
  for (const ref of refs) {
    const src = ref.src.trim();
    if (!src || /^data:image\//i.test(src)) continue;
    if (/^(?:https?:)?\/\//i.test(src)) remote += 1;
    else local += 1;
  }
  const warnings: StagedFileWarning[] = [];
  if (local > 0) {
    warnings.push({
      name: file.name,
      reason: `检测到 ${local} 个本地图片引用；单个 Markdown 不包含图片文件，问答中将无法展示。请改用 ZIP 或拖入完整文件夹。`,
    });
  }
  if (remote > 0) {
    warnings.push({
      name: file.name,
      reason: `检测到 ${remote} 个远程图片引用；系统不会自动下载远程图片，问答中只保留相关文字。`,
    });
  }
  return warnings;
}

function stagedFileExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** 入列前的逐文件分类：可上传的进 accepted，其余逐条给出**面向用户的跳过原因**。
 *
 *  选择器与拖放两条路径共用。拖放拿到的 DataTransfer 列表不经 accept 过滤，而原生
 *  file input 会按 accept **静默**丢弃不支持的文件——所以拖放必须由我们自己接管并走
 *  这里，跳过的每个文件才有一条用户看得见的原因（否则就是「批量上传时不支持的文档
 *  无声消失」）。maxBytes 语义与 splitFilesByUploadSize 一致：null/非法 = 配置未到，
 *  不做客户端大小预判，交给服务端权威 413。`.zip` 与 PDF/PPTX 等格式一样来自后端
 *  解析注册表，原始字节直接上传；解包、Markdown 路径解析与图片落资产均在后台完成。 */
export function classifyStagedFiles<T extends SizedSourceFile>(
  files: readonly T[],
  opts: {
    supportedExtensions: readonly string[];
    legacyOfficeExtensions: readonly string[];
    maxBytes: number | null;
    supportedHint: string;
  },
): { accepted: T[]; skipped: SkippedStagedFile[]; bundles: T[] } {
  const sizeCap = opts.maxBytes !== null && Number.isSafeInteger(opts.maxBytes) && opts.maxBytes > 0
    ? opts.maxBytes
    : null;
  const accepted: T[] = [];
  const skipped: SkippedStagedFile[] = [];
  const bundles: T[] = [];
  for (const file of files) {
    const ext = stagedFileExtension(file.name);
    if (!opts.supportedExtensions.includes(ext)) {
      skipped.push({
        name: file.name,
        reason: opts.legacyOfficeExtensions.includes(ext)
          ? "旧版 Office 格式不支持，请另存为 .docx / .pptx 后重新上传"
          : `不支持的文件类型（支持：${opts.supportedHint}）`,
      });
      continue;
    }
    if (sizeCap !== null && file.size > sizeCap) {
      skipped.push({
        name: file.name,
        reason: `超过单个文件上限（${sourceUploadSizeLabel(sizeCap)}）`,
      });
      continue;
    }
    accepted.push(file);
  }
  return { accepted, skipped, bundles };
}

/** Compact a long staged filename without hiding its extension or final words.
 *
 * CSS ellipsis still protects very narrow windows, while this deterministic
 * middle compaction keeps an unusually long name from consuming the whole row
 * on wide/resized dialogs. Array.from avoids cutting a surrogate pair in half.
 * The full value remains available in the element's title attribute.
 */
export function compactStagedFileName(
  fileName: string,
  maxLength = STAGED_FILE_NAME_MAX_LENGTH,
): string {
  const characters = Array.from(fileName);
  if (characters.length <= maxLength) return fileName;
  if (maxLength <= 1) return "…";
  const available = maxLength - 1;
  const extensionStart = characters.lastIndexOf(".");
  const extensionLength = extensionStart > 0 && extensionStart < characters.length - 1
    ? characters.length - extensionStart
    : 0;
  const tailLength = Math.min(
    available,
    Math.max(1, Math.floor(available / 3), extensionLength),
  );
  const headLength = available - tailLength;
  return `${characters.slice(0, headLength).join("")}…${characters.slice(-tailLength).join("")}`;
}

/** 上传时每个待上传文件发给后端的两个并列字段：
 *  - ``docType``：界面上的原值（``""`` = 下拉框里的「自动检测」）。
 *  - ``explicit``：用户是否**手动**动过这一项的类型下拉框（``"1"``）还是没动过（``"0"``）。
 *
 *  为什么要这个 explicit 信号：后端在内容判重沿用既有来源时，只有用户**显式**表态才
 *  改/重置那条来源的类型。前端不知道某个文件是不是重传（哈希匹配在后端），单看「值空
 *  不空」区分不了「auto-detect 自动填的建议值」和「用户显式想改」——所以把「用户动没
 *  动下拉框」这件事显式发给后端：
 *  - 显式 + 具体类型 → 改成该类型；显式 + ``""`` → 把已定型来源重置回自动；
 *  - 非显式（重传没动下拉框）→ 保留既有来源的类型，绝不静默重置重抽。 */
export function uploadDocTypeFields(
  types: readonly string[],
  touched: readonly boolean[],
): Array<{ docType: string; explicit: "0" | "1" }> {
  return types.map((dt, i) => ({
    docType: dt ?? "",
    explicit: touched[i] ? "1" : "0",
  }));
}

/** auto-detect 回填：只填**未被用户表态过**的空项，绝不覆盖用户已选的值。
 *
 *  跳过条件是「有值 **或** touched」，不是单看「有值」：检测是异步的，回填落地时用户
 *  可能已经先选了具体类型、又改回「自动检测」——此刻值为空但 ``touched[i]===true``。
 *  只看「值空不空」会把这个显式的「自动检测」当成没表态、填成检测结果，上传就发成
 *  ``explicit=1`` + 检测类型，把复用源改成与用户显式选择相反的类型。所以 touched 的槽
 *  一律跳过（哪怕为空）。
 *
 *  **只产出类型、不碰 touched**：auto-detect 是系统建议、不是用户表态。调用方回填后
 *  touched 数组保持原样，这正是「重传没动下拉框 → 发 explicit=0 → 后端保留既有类型」
 *  这条链路的起点。``detectedByIndex[i]`` 是第 i 个文件的检测结果（``undefined`` =
 *  没检测出/无建议）；``touched`` 与 ``types`` 同序等长（省略/越界项按未表态处理）。 */
export function fillAutoDetectedTypes(
  types: readonly string[],
  detectedByIndex: ReadonlyArray<string | undefined>,
  touched: readonly boolean[] = [],
): string[] {
  return types.map((dt, i) => (dt || touched[i] ? dt : (detectedByIndex[i] ?? dt)));
}

/** 用户手动改了第 ``index`` 项的类型下拉框 → 该项标记为「用户显式设置」。 */
export function markTouched(touched: readonly boolean[], index: number): boolean[] {
  return touched.map((t, i) => (i === index ? true : t));
}

/** 同步更新 touched 的 state **和**它的镜像 ref——一步到位,ref 立刻反映最新值。
 *
 *  为什么不能只靠 useEffect：`detectStagedTypes` 是异步的,它 resolve 时用 ref 的最新
 *  值决定「哪些项还没被用户表态、可以回填」。若 ref 只在 `useEffect(...,[touched])` 里
 *  更新,那么「用户选了自动检测(置 touched)」与「useEffect 把新 touched 写进 ref」之间
 *  有一个 React 提交间隙;检测恰好在这个间隙里 resolve,就会读到**旧** ref(touched=false)
 *  → 把用户显式选的空「自动检测」当没表态、覆盖成检测结果。所以改动 touched 的**每个**
 *  handler 都调本函数:先算出新值,同步写 ref,再推进 state。useEffect 仍在,作兜底
 *  (万一有路径没走本函数,提交后它会把 ref 拨回与 state 一致)。
 *
 *  `next` 可传新数组,或 `(prev)=>next` 的函数式更新(从 `ref.current` 这个最新值算起——
 *  因为所有改动都经本函数同步入 ref,ref 就是当前最新 touched)。返回算出的新值。 */
export function applyTouchedUpdate(
  ref: { current: boolean[] },
  setState: (value: boolean[]) => void,
  next: boolean[] | ((prev: boolean[]) => boolean[]),
): boolean[] {
  const value = typeof next === "function" ? next(ref.current) : next;
  ref.current = value;   // 同步镜像:不等 useEffect,消除检测同 tick resolve 的间隙
  setState(value);
  return value;
}

/** 用户「全部设为…」→ 所有项标记为显式设置。 */
export function markAllTouched(touched: readonly boolean[]): boolean[] {
  return touched.map(() => true);
}

export type UploadOutcome = {
  /** 这次真正新建的来源——只有这些才该计入来源总数（按 id 去重后，批内自我重复
   *  的回声只算一次）。 */
  added: UploadedSource[];
  /** 内容与本笔记本里已有来源完全相同，被沿用的那些（后端没有新建行）。 */
  reused: UploadedSource[];
  /** reused 的子集：这次顺手把文档类型改成了别的那些。后端会把新类型写进原来
   *  那条来源，并按新类型重新抽取知识——所以文案不能只说「沿用，没做别的」。 */
  retyped: UploadedSource[];
  /** 按 source id 折叠去重后、可直接并进 `sources` state 的列表：一个 id 只留一
   *  张卡。后端一次上传可能对同一个 id 返回多条（见下），直接铺进 state 会渲染出
   *  重复卡片（React key 撞车），必须先折叠。 */
  sources: UploadedSource[];
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
 *  不到的（比如那条来源不在当前这一页里）一律当作没改，宁可少说也不谎报。
 *
 *  一次上传可能对**同一个 source id** 返回多条：同一批里两个内容相同的文件，第 2
 *  个会命中第 1 个刚建的那行（同 id、reused=true）。所以先按 id 折叠——否则会渲染
 *  出重复卡片、总数也刷高。折叠取后出现的那条（它带这一行最新的快照），但「新增」
 *  的计数认「该 id 是否被本次真正新建过」（出现过 reused=false），这样批内自我重复
 *  的回声既不重复计数、也不会被当成「沿用了本笔记本已有来源」而误报。 */
export function summarizeUpload(
  uploaded: UploadedSource[],
  previousDocTypes: ReadonlyMap<string, string> = new Map(),
): UploadOutcome {
  const createdIds = new Set<string>();
  const byId = new Map<string, UploadedSource>();
  for (const item of uploaded) {
    if (item.reused !== true) createdIds.add(item.id);
    byId.set(item.id, item); // 后出现的同 id 覆盖：留最新快照
  }
  const folded = [...byId.values()];
  const added = folded.filter((item) => createdIds.has(item.id));
  const reused = folded.filter((item) => !createdIds.has(item.id));
  const retyped = reused.filter((item) => {
    const before = previousDocTypes.get(item.id);
    return before !== undefined && (item.doc_type ?? "") !== before;
  });
  const keptAsIs = reused.filter((item) => !retyped.includes(item));
  return {
    added, reused, retyped, sources: folded,
    toast: uploadToast(added.length, keptAsIs.length, retyped),
  };
}

function uploadToast(added: number, keptAsIs: number, retyped: UploadedSource[]): string {
  const clauses: string[] = [];
  if (keptAsIs > 0) {
    clauses.push(
      `${keptAsIs} 个文件的内容已经在本笔记本里，沿用原有来源（名称保持原样），没有重复添加`,
    );
  }
  if (retyped.length > 0) {
    // 「正在重新分析」只在后端真的把那条来源翻成 extracting 时才说：笔记本还没
    // 建过知识图谱时只记下新类型、不会立刻开抽，谎报会让用户白等。
    const rerunning = retyped.some((item) => item.parse_status === "extracting");
    clauses.push(
      `${retyped.length} 个文件的内容已经在本笔记本里，沿用原有来源并改用了新的文档类型` +
        (rerunning ? "，正在按新类型重新分析" : ""),
    );
  }
  if (clauses.length === 0) return `已上传 ${added} 个来源`;
  const sameContent = clauses.join("；");
  return added === 0 ? sameContent : `已上传 ${added} 个来源；另有 ${sameContent}`;
}
