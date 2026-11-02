"use client";

import { type ChangeEvent, type DragEvent as ReactDragEvent, useEffect, useMemo, useRef, useState } from "react";
import { detectSourceTypes, importUrlSources, uploadSources } from "./source-api.ts";
import { parseUrlLines } from "./url-sources.ts";
import { documentUploadBlockReason, type DocumentCapacity } from "./document-limit.ts";
import type { SourceSummary } from "./workspace-model.ts";
import type { SourceLibraryOwner, useSourceLibrary } from "./use-source-library.ts";
import type { RootModalCoordinator } from "./use-root-modal-coordinator.ts";
import { classifyStagedFiles, mergeLiveStagedFileWarnings, scanStandaloneMarkdownImageWarnings, summarizeUpload, uploadDocTypeFields, fillAutoDetectedTypes, markTouched, markAllTouched, sourceUploadSizeLabel, splitFilesByUploadSize, type SkippedStagedFile, type StagedFileWarning } from "./source-upload.ts";
import { emptyStagedList, mergeStagedFiles, stagedFileKey, type StagedList } from "./staged-files.ts";
import {
  bundleCapsFrom, bundleDirTotalBytesLimit, bundleErrorMessage, bundleFileNamesFor,
  bundleImagesEffectivelyEnabled, classifyBundleContents, collectDirectoryFiles, directoryHasMarkdown,
  directoryTooLargeMessage, directoryTruncatedMessage, inlineTooLargeImageLines,
  inlineTooLargeMessage, notStagedNote, processBundleCandidates,
  readDirectoryAsBundleFiles, unpackZipFile,
  ALREADY_STAGED_REASON, BUNDLE_READ_FAILED_REASON, BUNDLE_STAGE_FALLBACK_MAX_FILES_PER_BATCH,
  DIRECTORY_READ_FAILED_REASON, NO_MARKDOWN_IN_BUNDLE_REASON,
} from "./bundle-intake.ts";
import type { BundleReceiptEntry } from "./bundle-upload-panels.tsx";
import type { BundleFile, InlineReceipt } from "./md-bundle.ts";

// .xls is supported by the parser registry; binary .doc/.ppt need an explicit
// save-as-OOXML rejection rather than the generic unsupported-file hint.
const LEGACY_OFFICE_EXTENSIONS = ["doc", "ppt"];

export type SourceIntakeConfiguration = Readonly<{
  sourceUploadMaxBytes: number | null;
  sourceUploadMaxFilesPerBatch: number | null;
  sourceImageMaxBytes: number | null;
  sourceImageMaxPerSource: number | null;
  sourceImagesEnabled: boolean;
  supportedSourceExtensions: readonly string[];
}>;

type SourceIntakeOptions = {
  notebookId: string | null;
  capacity: DocumentCapacity;
  configuration: SourceIntakeConfiguration;
  library: Pick<ReturnType<typeof useSourceLibrary>, "captureOwner" | "commitUploadedSources" | "commitUrlSources" | "sources">;
  modals: Pick<RootModalCoordinator, "open" | "captureWorkspaceOwner" | "requestClose">;
  effects: {
    reportError: (error: unknown) => void;
    notify: (message: string) => void;
    refreshCollection: (guard: () => boolean) => Promise<void>;
    revalidateAskAvailability: () => void;
    reloadCheckup: (notebookId: string) => void;
  };
};

/** Owns one source-intake draft and its asynchronous work. Source-library owns
 * committed sources; the root-modal coordinator owns presentation and invokes
 * dismiss on its single close sink. Neither owner is duplicated here. */
export function useSourceIntake({
  notebookId: currentNotebookId, capacity: docCapacity, configuration, library, modals, effects,
}: SourceIntakeOptions) {
  const {
    sourceUploadMaxBytes, sourceUploadMaxFilesPerBatch, sourceImageMaxBytes,
    sourceImageMaxPerSource, sourceImagesEnabled, supportedSourceExtensions,
  } = configuration;
  const { reportError, notify: setToast } = effects;
  const sources = library.sources;
  const sourceImagePairingEnabled = bundleImagesEffectivelyEnabled({
    imagesEnabled: sourceImagesEnabled,
    imageMaxBytes: sourceImageMaxBytes,
    maxImagesPerSource: sourceImageMaxPerSource,
  });
  const supportedSourceAccept = useMemo(
    () => supportedSourceExtensions.map((ext) => `.${ext}`).join(","),
    [supportedSourceExtensions],
  );
  const supportedSourceUserHint = useMemo(
    () => supportedSourceExtensions.map((ext) => ext.toUpperCase()).join(" / "),
    [supportedSourceExtensions],
  );
  const [linkSectionOpen, setLinkSectionOpen] = useState(false);
  const [urlText, setUrlText] = useState("");
  const [urlBusy, setUrlBusy] = useState(false);
  const urlRequestOwnerRef = useRef<object | null>(null);
  const [urlRejected, setUrlRejected] = useState<Array<{ url: string; reason: string }>>([]);
  // 待上传列表：文件 + 每项文档类型 + 每项是否被用户显式表态，**一个** state 对象。
  // 三条数组必须逐项对齐（uploadDocTypeFields 按下标配对），而入列会被跨 await 的
  // 异步链触发（文件夹遍历或兼容 ZIP intake）——拆成三个 state 就只能各自 setState，等长
  // 不变量没有任何一处能一次性维护。合并语义与等长不变量的真源在 staged-files.ts。
  const [staged, setStaged] = useState<StagedList>(() => emptyStagedList());
  // 同步 ref 镜像：updateStaged 是**唯一**写入口，写 state 的同时先把新值写进 ref，
  // 于是「从最新值起算」不再依赖 React 何时提交。这正是 zip/pdf 同批那个静默丢失的
  // 根因——旧实现从 render 闭包读 stagedFiles 拼好数组再整体写回，zip 解完（跨了
  // await）时读到的是「还没有 pdf」的旧闭包，把 pdf 覆盖没了。沿用本文件既有的
  // applyTouchedUpdate 惯例（同一条理由）。
  const stagedRef = useRef<StagedList>(staged);
  const stagedFiles = staged.files;
  const stagedDocTypes = staged.docTypes;
  const stagedDocTypeTouched = staged.touched;
  // 兜底：万一将来有路径绕开 updateStaged 直接 setStaged，提交后把 ref 拨回一致。
  useEffect(() => {
    stagedRef.current = staged;
  }, [staged]);
  function updateStaged(next: StagedList | ((prev: StagedList) => StagedList)): StagedList {
    const value = typeof next === "function" ? next(stagedRef.current) : next;
    stagedRef.current = value;
    setStaged(value);
    return value;
  }
  // 最近一次「添加文件」里被跳过的文件（类型不支持/超大小/超批量），在弹窗内持久展示。
  // 不能只发 toast：它 2.2 秒即逝，批量选文件时用户根本来不及看清哪些没进列表。
  const [stagedSkipped, setStagedSkipped] = useState<SkippedStagedFile[]>([]);
  const [stagedWarnings, setStagedWarnings] = useState<StagedFileWarning[]>([]);
  // 文件夹兼容 intake（遍历目录、内联图片）或旧 ZIP intake 在飞：非 null 时整个
  // 添加文件入口必须禁用并换成该动作语义的进行态文案（普通 ZIP 上传不经过这里）。
  const [bundleBusyLabel, setBundleBusyLabel] = useState<string | null>(null);
  // 拖入文件夹里有多个 markdown 时，等待用户勾选要添加的文件；旧 ZIP intake 也沿用
  // 此状态。非 null 期间同样视为「忙碌」，避免新的文件夹覆盖当前选择。
  // selected 默认全选（设计文档 §3.1 第 2 条）。
  const [bundleChoice, setBundleChoice] = useState<{
    label: string;
    files: BundleFile[];
    candidates: BundleFile[];
    selected: Set<string>;
    // 这个勾选面板所属那条异步链在起跑时捕获的世代——confirmBundleChoice 落盘
    // 时要拿它去比对 bundleIntakeGenerationRef，而不是比对"确认那一刻"的世代
    // （两者通常相同，但保持「捕获于链起点」的统一语义，见 bundleIntakeGenerationRef）。
    generation: number;
  } | null>(null);
  // 兼容 intake 按到达顺序串行处理；命中「多个 markdown 需勾选」时，本 resolver
  // 让异步链等到用户确认/取消才继续，避免新的文件夹覆盖尚未完成的选择。
  const bundleChoiceResolveRef = useRef<(() => void) | null>(null);
  // 忙碌文案是一个**栈**而不是单值：兼容 intake 可能嵌套，单值形态下内层清零会把
  // 外层进行态一起抹掉，导致入口在链仍运行时提前恢复可用。
  const bundleBusyStackRef = useRef<string[]>([]);
  // 用户主动关闭「添加来源」弹窗后，还在飞的文件夹 intake 不得把弹窗强行弹回来。
  // 重新打开弹窗时清零。
  const sourceModalDismissedRef = useRef(false);
  // 暂存批次的世代计数器：resetStagedIntake()（关弹窗/清空/上传成功/新建笔记本）
  // 与 openNotebook()（切换笔记本）各自 ++。文件夹 intake 这类异步链在自己
  // "起跑"那一刻捕获当前世代；写回暂存列表/回执/跳过记录（落盘）前重新比对，
  // 世代已变说明这批数据在链跑的这段时间里被用户取消或随切库作废，整条链的
  // 结果必须整体静默丢弃（含回执）——不能把已取消的文件复活进当前弹窗。
  // `sourceModalDismissedRef` 只挡弹窗重新弹出这一件事，挡不住数据本身被写回。
  const bundleIntakeGenerationRef = useRef(0);
  // 压缩包/文件夹图片配对回执：每个成功入列的 md 一条，弹窗内持久显示，直至弹窗重置
  // （关闭/清空/上传成功）——对齐「被跳过文件逐条持久列出、不许只发即逝 toast」的
  // 既有红线（stagedSkipped 同一精神，这里是配对细节，独立一块面板，见
  // bundle-upload-panels.tsx）。
  const [bundleReceipts, setBundleReceipts] = useState<BundleReceiptEntry[]>([]);
  // 拖放悬停高亮（仅大拖放区）。
  const [dropZoneDragActive, setDropZoneDragActive] = useState(false);
  // 上传在飞:multipart 传大 PDF 可能几十秒,期间「上传 N 个文件」必须禁用改文案。后端按
  // 内容哈希在同 notebook 内去重,重复提交不会建出重复来源,但会白传一遍并再跑一次解析。
  const [uploadBusy, setUploadBusy] = useState(false);
  const uploadRequestOwnerRef = useRef<object | null>(null);
  const atDocCapacityHint = "已达该笔记本的文档数量上限，无法继续添加文档。";
  const sourceUploadConfigLoading = sourceUploadMaxBytes === null
    || sourceUploadMaxFilesPerBatch === null;
  const sourceBatchAtCapacity = sourceUploadMaxFilesPerBatch !== null
    && stagedFiles.length >= sourceUploadMaxFilesPerBatch;
  // 文件夹/兼容 intake 在飞，或正等待用户在下方勾选要添加的 markdown：两者都必须禁用整个
  // 添加文件入口（红线：长任务按钮的忙碌态）——后者同样要禁，否则再拖入一个 zip 会
  // 覆盖用户还没确认完的第一次选择（bundleChoiceResolveRef 声明处有完整说明）。
  const bundleProcessing = bundleBusyLabel !== null || bundleChoice !== null;
  const sourceFilePickerDisabled = docCapacity.atCapacity
    || sourceUploadConfigLoading
    || sourceBatchAtCapacity
    || bundleProcessing;
  const sourceFilePickerHint = docCapacity.atCapacity
    ? atDocCapacityHint
    : sourceUploadConfigLoading
      ? "正在读取单文件上传上限…"
      : sourceBatchAtCapacity
        ? `单次最多上传 ${sourceUploadMaxFilesPerBatch} 个文件，请先上传当前批次。`
        : bundleBusyLabel
          ? bundleBusyLabel
          : bundleChoice
            ? "请先在下方选择要添加的 Markdown 文件"
            : undefined;
  // 禁用态下拖入文件时写进「已跳过」的原因。刻意与 sourceFilePickerHint 分开：那份
  // 提示在解包途中是一句进行态文案（「解析压缩包…」），把它当跳过原因写进持久列表，
  // 用户读到的是「这个文件被跳过了，原因：解析压缩包…」——答非所问。
  const sourceFilePickerSkipReason = docCapacity.atCapacity
    ? atDocCapacityHint
    : sourceUploadConfigLoading
      ? "正在读取上传限制，请稍后重试"
      : sourceBatchAtCapacity
        ? `单次最多上传 ${sourceUploadMaxFilesPerBatch} 个文件，请先上传当前批次`
        : bundleProcessing
          ? "正在处理已拖入的压缩包或文件夹，请稍后重试"
          : "当前无法添加文件";
  const stagedUploadBlockedReason = documentUploadBlockReason(docCapacity, stagedFiles.length);
  // Stage selected files so the user can pick a document type per file before
  // uploading (auto-detect by default).
  function stageFiles(event: ChangeEvent<HTMLInputElement>) {
    const all = Array.from(event.target.files || []);
    event.target.value = "";
    stageIncomingFiles(all).catch(reportError);
  }

  /** 打开「添加来源」弹窗：清掉「用户已主动关闭」的标记，让后续入列可以正常揭示弹窗。 */
  function openSourceModal() {
    if (!modals.open("source-add", modals.captureWorkspaceOwner())) return;
    sourceModalDismissedRef.current = false;
  }

  /** 弹窗内所有暂存态的统一清空点（新建笔记本 / 关闭弹窗 / 清空 / 上传成功共用）。
   *  世代先 ++ 再清空：还在飞的异步解包链此后落盘时一律比对失败、整条结果丢弃
   *  （见 bundleIntakeGenerationRef）。同时结清挂起的 bundleChoice 勾选面板——
   *  世代递增只挡了迟到落盘，挂起的勾选面板、它的 resolver 与忙碌栈帧不会因此
   *  自动消失；不结清就会让「清空」「上传成功」这类直接调用本函数的入口，让用户
   *  已经点了确认的勾选被 stageBundleCandidates 的世代比对静默丢弃
   *  （codex #518 R4 P2）。切库先由 root-modal transition 同步撤销 source-add
   *  lease，再经同一个 close sink 调本函数；所有清理因此共用这一处世代递增与
   *  bundleChoice 结清，是结构性不变量。 */
  function resetStagedIntake() {
    bundleIntakeGenerationRef.current += 1;
    updateStaged(emptyStagedList());
    setStagedSkipped([]);
    setStagedWarnings([]);
    setBundleReceipts([]);
    cancelBundleChoice();
  }

  /** 关闭「添加来源」弹窗。× 与遮罩都经 coordinator 的同一 close sink，
   *  因而必定结清 bundle choice、暂存批次和迟到解包 generation。 */
  function closeSourceModal(reason: "button" | "backdrop" = "button") {
    modals.requestClose("source-add", reason);
  }

  // 选择器与拖放共用的入列逻辑（**同步**部分）。被跳过的文件（类型不支持/超大小/
  // 超批量）写进 stagedSkipped 在弹窗内持久展示，绝不静默丢弃；注册表准入的 .zip
  // 与 PDF/PPTX 一样作为普通来源原样入列，由后端解包、解析并持久化相对图片。
  //
  // 合并从 stagedRef.current 起算并经 updateStaged 一次性写回：三条并行数组的等长
  // 不变量在 mergeStagedFiles 里维护，跨 await 的异步链不会再读到发起那一刻的旧闭包
  // （旧形态下 pdf + zip 同批时 pdf 会被 zip 的入列静默覆盖掉）。
  //
  // `expectedGeneration`：调用方是异步链（文件夹遍历/压缩包解包）时传入它在自己
  // 起跑那一刻捕获的世代；直接由用户操作触发（文件选择器/拖放普通文件）时省略——
  // 那类调用本身就在当前世代，无需检查。世代已变（用户取消/切库）时整批结果连同
  // 跳过记录一起静默丢弃，不落盘、不弹窗（评审 F2）。
  function stageIncomingFilesSync(
    all: File[], expectedGeneration?: number,
  ): { added: File[]; skipped: SkippedStagedFile[]; duplicates: File[]; bundles: File[] } {
    if (all.length === 0) return { added: [], skipped: [], duplicates: [], bundles: [] };
    if (expectedGeneration !== undefined && expectedGeneration !== bundleIntakeGenerationRef.current) {
      return { added: [], skipped: [], duplicates: [], bundles: [] };
    }
    const { accepted: picked, skipped, bundles } = classifyStagedFiles(all, {
      supportedExtensions: supportedSourceExtensions,
      legacyOfficeExtensions: LEGACY_OFFICE_EXTENSIONS,
      maxBytes: sourceUploadMaxBytes,
      supportedHint: supportedSourceUserHint,
    });
    // 追加而非覆盖（"继续添加文件"语义）；按 name+size 去重，避免重复入列。
    const merge = mergeStagedFiles(stagedRef.current, picked, {
      maxFilesPerBatch: sourceUploadMaxFilesPerBatch,
    });
    const allSkipped = [...skipped, ...merge.skipped];
    appendStagedSkipped(allSkipped);
    if (merge.added.length === 0) {
      return { added: [], skipped: allSkipped, duplicates: merge.duplicates, bundles };
    }
    updateStaged(merge.next);
    // 用户已经主动关掉弹窗时不再弹回来（异步解包链完成得比用户慢是常态）。
    if (!sourceModalDismissedRef.current) {
      modals.open("source-add", modals.captureWorkspaceOwner());
    }
    // 对新增的文本类文件做内容检测，预填类型下拉（异步，不阻塞 UI；用户仍可改）。
    void detectStagedTypes(merge.added);
    return { added: merge.added, skipped: allSkipped, duplicates: merge.duplicates, bundles };
  }

  /** 入列文件。调用方必须 `await` 或 `.catch(reportError)`，因为拖入文件夹的兼容
   *  管线仍会沿用这个异步边界。注册表准入的 ZIP 已进入 `accepted`，不会出现在
   *  `bundles`；保留后半段只为兼容旧分类结果，不能把正常 ZIP 改回浏览器解包。 */
  async function stageIncomingFiles(all: File[], expectedGeneration?: number): Promise<void> {
    const generation = expectedGeneration ?? bundleIntakeGenerationRef.current;
    const { added, bundles } = stageIncomingFilesSync(all, expectedGeneration);
    if (added.length > 0) {
      const warnings = await scanStandaloneMarkdownImageWarnings(added);
      if (generation === bundleIntakeGenerationRef.current && warnings.length > 0) {
        const liveFiles = stagedRef.current.files;
        setStagedWarnings((previous) => mergeLiveStagedFileWarnings(previous, warnings, liveFiles));
      }
    }
    if (bundles.length > 0) await ingestBundleSources(bundles);
  }

  // 拖放必须由我们接管：不 preventDefault 的话文件会落在铺满拖放区的原生
  // <input type=file accept=…> 上，浏览器按 accept **静默**过滤不支持的文件——
  // stageIncomingFiles 根本收不到它们，用户也就得不到任何「已跳过」提示。
  // 累积追加（按 name+reason 去重）：弹窗开着期间的警告不能被下一次「全合法」的
  // 添加静默清掉（codex #485 R1 P2）；只有「知道了」/清空/关闭/上传成功才清。
  function appendStagedSkipped(entries: SkippedStagedFile[]) {
    if (entries.length === 0) return;
    setStagedSkipped((previous) => {
      const combined = [...previous];
      for (const item of entries) {
        if (!combined.some((existing) => existing.name === item.name && existing.reason === item.reason)) {
          combined.push(item);
        }
      }
      return combined;
    });
  }

  // preventDefault 必须**无条件**先做（含禁用态）：不取消默认行为，落在页面上的
  // drop 会让浏览器直接导航打开该文件，整批已暂存文件随页面一起丢掉。是否入列
  // 由禁用判断在取消默认行为**之后**决定（codex #485 R1 P1）。
  function handleStageDragOver(event: ReactDragEvent<HTMLElement>) {
    event.preventDefault();
  }
  function handleStageDrop(event: ReactDragEvent<HTMLElement>) {
    event.preventDefault();
    setDropZoneDragActive(false);
    if (sourceFilePickerDisabled) {
      // 禁用态（容量满/批次满/配置加载中/zip 或文件夹处理中）下拖入的文件不入列，
      // 但必须在「已跳过」列表逐条留痕（codex #485 R2 P2），不能无声消失。文件夹本身
      // 在 dataTransfer.files 里没有条目（浏览器不为目录生成 File），这里只报得出
      // 同批里被拖入的普通文件，与改动前一致。
      const dropped = Array.from(event.dataTransfer?.files ?? []);
      appendStagedSkipped(dropped.map((file) => ({ name: file.name, reason: sourceFilePickerSkipReason })));
      return;
    }
    const entries = extractDroppedEntries(event.dataTransfer?.items ?? null);
    // entries 为空数组说明 webkitGetAsEntry 每一项都返回了 null（个别浏览器/来源在
    // 某些拖放形态下如此），此时 dataTransfer.files 仍可能有内容——不回退就等于把
    // 整批拖入静默吞掉。判据必须是「拿到了至少一个条目」而不是「API 可用」。
    if (entries !== null && entries.length > 0) {
      dispatchDroppedEntries(entries).catch(reportError);
      return;
    }
    // File and Directory Entries API 不可用（极少见的浏览器/环境）：回退到改动前的
    // 扁平文件列表——文件夹在这条路径下本来就拿不到内容，不是新的回归。
    stageIncomingFiles(Array.from(event.dataTransfer?.files ?? [])).catch(reportError);
  }

  // `webkitGetAsEntry` 是 File and Directory Entries API 的 WebKit 前缀方法，主流
  // 浏览器都实现但不是强制标准；任一项拿不到就整体判「不可用」，统一回退扁平文件列表，
  // 不做「部分用 entry、部分用 file」的混合语义。
  function extractDroppedEntries(items: DataTransferItemList | null): FileSystemEntry[] | null {
    if (!items) return null;
    const entries: FileSystemEntry[] = [];
    for (const item of Array.from(items)) {
      if (typeof item.webkitGetAsEntry !== "function") return null;
      const entry = item.webkitGetAsEntry();
      if (entry) entries.push(entry);
    }
    return entries;
  }

  // 拖入的条目按到达顺序串行处理：文件夹递归遍历（见 ingestDroppedDirectory）；普通
  // 文件先攒起来，最后一次性交给 stageIncomingFiles；其中的 .zip 原样上传后台。
  async function dispatchDroppedEntries(entries: FileSystemEntry[]) {
    const plainFiles: File[] = [];
    const unreadable: SkippedStagedFile[] = [];
    for (const entry of entries) {
      if (entry.isDirectory) {
        await ingestDroppedDirectory(entry as FileSystemDirectoryEntry);
        continue;
      }
      if (!entry.isFile) continue;
      try {
        const file = await new Promise<File>((resolve, reject) => {
          (entry as FileSystemFileEntry).file(resolve, reject);
        });
        plainFiles.push(file);
      } catch {
        // 读取失败（极少见，如条目在拖放后被移动/删除）：这一项不阻塞同批其余条目，
        // 但必须逐条留痕——静默吞掉就是「拖了没反应」，用户无从知道少了哪个文件。
        unreadable.push({ name: entry.name, reason: BUNDLE_READ_FAILED_REASON });
      }
    }
    appendStagedSkipped(unreadable);
    if (plainFiles.length > 0) await stageIncomingFiles(plainFiles);
  }

  // 忙碌文案的栈式持有：push/pop 成对，pop 后恢复外层那一帧的文案而不是一律清零
  // （bundleBusyStackRef 声明处有完整说明）。
  function pushBundleBusy(label: string) {
    bundleBusyStackRef.current.push(label);
    setBundleBusyLabel(label);
  }
  function replaceBundleBusy(label: string) {
    const stack = bundleBusyStackRef.current;
    if (stack.length > 0) stack[stack.length - 1] = label; else stack.push(label);
    setBundleBusyLabel(label);
  }
  function popBundleBusy() {
    bundleBusyStackRef.current.pop();
    syncBundleBusyLabel();
  }
  function syncBundleBusyLabel() {
    const stack = bundleBusyStackRef.current;
    setBundleBusyLabel(stack.length > 0 ? stack[stack.length - 1] : null);
  }

  // 一批里可能有多个 zip（多选文件选择器 / 一次拖放多个 zip）：严格串行处理，避免
  // 第二个 zip 的「多个 markdown 需勾选」弹窗覆盖第一个还没确认的选择
  // （bundleChoiceResolveRef 声明处有完整说明）。
  async function ingestBundleSources(files: File[]) {
    for (const file of files) {
      await ingestZipFile(file);
    }
  }

  async function ingestZipFile(file: File) {
    // 这条链自己的世代：起跑那一刻捕获，解压这段真实 I/O 跑完后重新比对
    // bundleIntakeGenerationRef——不等就说明用户在解压期间取消/切库了，整条
    // 结果（含跳过记录）静默丢弃，不落盘（评审 F2）。
    const generation = bundleIntakeGenerationRef.current;
    pushBundleBusy("解析压缩包…");
    try {
      const result = await unpackZipFile(file, bundleCapsFrom(sourceUploadMaxBytes));
      if (generation !== bundleIntakeGenerationRef.current) return;
      if (!result.ok) {
        appendStagedSkipped([{ name: file.name, reason: bundleErrorMessage(result.error) }]);
        return;
      }
      await handleBundleFiles(file.name, result.files, generation);
    } catch {
      // 读字节/解包本身抛异常（条目已被移动、权限变化、内存不足…）：这一个压缩包
      // 当作没进来，逐条留痕而不是让整条链带着一个未处理 rejection 静默死掉。
      if (generation !== bundleIntakeGenerationRef.current) return;
      appendStagedSkipped([{ name: file.name, reason: BUNDLE_READ_FAILED_REASON }]);
    } finally {
      popBundleBusy();
    }
  }

  // 递归遍历一个被拖入的文件夹：含 markdown 时按 zip 同一配对/内联流程处理；不含
  // markdown 时保持既有拖拽行为——文件夹里的文件按普通文件逐个入列，走常规的
  // 类型/大小/批量校验（红线：不含 md 的文件夹不能回归成「拖了没反应」）。
  async function ingestDroppedDirectory(entry: FileSystemDirectoryEntry) {
    // 同上：这条链自己的世代，遍历/读字节这段真实 I/O 之后重新比对再决定是否
    // 落盘（评审 F2）。
    const generation = bundleIntakeGenerationRef.current;
    pushBundleBusy("读取文件夹…");
    try {
      // 总字节预算是**零 I/O** 预检：只累加条目的 File.size 元数据，超限就地止损，
      // 一个字节都不读（zip 那条路由 parseZipBundle 的解压护栏把关，这里是它的对偶）。
      // bundleDirTotalBytesLimit 恒返回有限数（复用 zip 输入侧同一个绝对顶，但不加
      // 容器余量）——顶配部署下裸 bundleTotalBytesLimit 会放行 4 GiB，
      // readDirectoryAsBundleFiles 的 Promise.all 会把它们整读进内存耗死标签页
      // （codex #518 R6 P2）。
      const totalLimit = bundleDirTotalBytesLimit(sourceUploadMaxBytes);
      const { entries, truncated, overBudget } = await collectDirectoryFiles(entry, {
        maxTotalBytes: totalLimit,
      });
      if (generation !== bundleIntakeGenerationRef.current) return;
      if (truncated) {
        appendStagedSkipped([{ name: entry.name, reason: directoryTruncatedMessage() }]);
        return;
      }
      if (overBudget) {
        // bundleDirTotalBytesLimit 恒返回有限数（不像裸 bundleTotalBytesLimit 那样
        // 可能是 null），totalLimit 因此不需要再做 `?? 0` 类型收窄。
        appendStagedSkipped([{ name: entry.name, reason: directoryTooLargeMessage(totalLimit) }]);
        return;
      }
      // 只看路径（零 I/O）判断有没有 markdown：不含 md 的文件夹没必要把里面可能
      // 几十上百个文件的全部字节都读一遍，只为了发现用不上。
      if (!directoryHasMarkdown(entries)) {
        // 必须 await：保持文件夹 intake 的异步生命周期完整；其中的 zip 作为普通来源
        // 原样入列，不再由浏览器解包。
        await stageIncomingFiles(entries.map((item) => item.file), generation);
        return;
      }
      replaceBundleBusy("读取文件夹图片…");
      const bundleFiles = await readDirectoryAsBundleFiles(entries);
      if (generation !== bundleIntakeGenerationRef.current) return;
      await handleBundleFiles(entry.name, bundleFiles, generation);
    } catch {
      // 遍历/读字节失败：同上，整个文件夹当作没进来并留痕。
      if (generation !== bundleIntakeGenerationRef.current) return;
      appendStagedSkipped([{ name: entry.name, reason: DIRECTORY_READ_FAILED_REASON }]);
    } finally {
      popBundleBusy();
    }
  }

  // 虚拟文件集（拖入文件夹遍历产出；旧 ZIP 解包入口仅为兼容保留）→ 按 markdown 数量分派：零个报错、一个
  // 直接处理、多个弹出勾选并挂起，直到用户确认/取消（设计文档 §3.1 第 2 条）。
  // `generation` 是调用方（ingestZipFile/ingestDroppedDirectory）自己那条链起跑
  // 时捕获的世代——这里是同一条链的延续,不重新捕获。世代已变(用户取消/切库)
  // 就整体不落盘,连"多个 markdown 需勾选"的弹窗都不弹出(评审 F2)。
  async function handleBundleFiles(label: string, files: BundleFile[], generation: number) {
    if (generation !== bundleIntakeGenerationRef.current) return;
    const classification = classifyBundleContents(files);
    if (classification.kind === "empty") {
      appendStagedSkipped([{ name: label, reason: NO_MARKDOWN_IN_BUNDLE_REASON }]);
      return;
    }
    if (classification.kind === "single") {
      stageBundleCandidates(label, [classification.file], files, generation);
      return;
    }
    // 同一时刻只允许存在一个挂起的 resolver。走到这里说明上一条链的 resolver 还没
    // 被结清（入口的 bundleProcessing 禁用 + 嵌套链一律 await 之后，理论上到不了）；
    // 真到了也必须先把它 resolve 掉再替换——直接覆盖会让上一条链的 await 永远挂起，
    // 它的忙碌位也就再也不会释放，添加来源入口从此锁死。
    const stale = bundleChoiceResolveRef.current;
    if (stale) {
      bundleChoiceResolveRef.current = null;
      stale();
    }
    await new Promise<void>((resolve) => {
      bundleChoiceResolveRef.current = resolve;
      setBundleChoice({
        label,
        files,
        candidates: classification.candidates,
        selected: new Set(classification.candidates.map((item) => item.path)),
        generation,
      });
      // 勾选等待不是「正在解析」：清掉忙碌文案（栈本身保持不变，链仍持有这一帧），
      // 让 sourceFilePickerHint 的「请先在下方选择要添加的 Markdown 文件」那一支真正
      // 可达。入口在此期间仍被 bundleChoice !== null 禁用，不会因为文案清空而放行。
      setBundleBusyLabel(null);
    });
    // 用户确认/取消后立刻把本帧的进行态文案恢复回来（下面还要接着内联/处理下一个包）。
    syncBundleBusyLabel();
  }

  // 一个虚拟文件集里被选中的那几个 md：逐个内联 → 攒批一次性入列 → 按**实际入列
  // 结果**落持久回执。
  //
  // 攒批而不是逐个入列，是因为入列会写同一份待上传列表；回执在入列**之后**才落，
  // 是因为一条回执若在文件其实没进列表时仍写着「3 张已内联」，就是在伪装成功——
  // 被拒（内联后超限）、被去重、被单次上限挡下的那几份必须带上「未加入待上传列表」
  // 及其原因。
  //
  // `generation`：所属那条异步链起跑时捕获的世代（`confirmBundleChoice` 传的是
  // 挂起面板自己那份，`handleBundleFiles` 单文件分支直接透传）。本函数体全程
  // 同步、无 await，所以在入口比对一次就覆盖了下面全部落盘点（含跳过记录/入列/
  // 回执）——世代已变即整体不落盘（评审 F2）。
  function stageBundleCandidates(
    bundleLabel: string, candidates: BundleFile[], files: BundleFile[], generation: number,
  ) {
    if (generation !== bundleIntakeGenerationRef.current) return;
    // 同批同名 md 的消歧命名（a/README.md 与 b/README.md）：不消歧会被待上传列表的
    // 「同名同大小」去重静默折叠成一份。
    const names = bundleFileNamesFor(candidates.map((item) => item.path));
    const built: File[] = [];
    const pending: Array<
      { fileName: string; receipt: InlineReceipt; notes: string[]; pairingSkipped: boolean }
    > = [];
    const rejected: SkippedStagedFile[] = [];
    // 单次上传数量上限必须在**内联之前**生效（判据与循环都在 bundle-intake 的
    // processBundleCandidates 里，可在纯函数层单测）：候选默认全选，而 mergeStagedFiles
    // 那道权威闸只在入列时才截断，「先全部内联、再截断」在一个合法的两千条目压缩包上
    // 等于白分配 GB 级 base64（codex #518 R1 P1）。
    //
    // 名额从 stagedRef.current（同步镜像，跨 await 安全）起算——与 stageIncomingFilesSync
    // 的合并基准同一份值，不读 render 闭包里的 state。
    const batchCap = sourceUploadMaxFilesPerBatch ?? BUNDLE_STAGE_FALLBACK_MAX_FILES_PER_BATCH;
    const batch = processBundleCandidates(
      candidates,
      files,
      {
        uploadMaxBytes: sourceUploadMaxBytes ?? 0,
        imageMaxBytes: sourceImageMaxBytes,
        maxImagesPerSource: sourceImageMaxPerSource,
        imagesEnabled: sourceImagePairingEnabled,
      },
      {
        names,
        remainingSlots: Math.max(0, batchCap - stagedRef.current.files.length),
        batchCap,
      },
    );
    // 名额耗尽、根本没被处理的那些：只落跳过记录，不造回执（空回执会被渲染成
    // 「未在正文中发现本地图片」——一句没发生过的事实断言）。
    rejected.push(...batch.skipped);
    for (const processed of batch.processed) {
      if (!processed.ok) {
        // 零张实际内联（正文本身已超限，或图片配对整个被跳过/没有本地图片）时，
        // 「请精简图片」是对不上症状的建议——超限的是正文，改说「拆分文档」。
        const reason = inlineTooLargeMessage(
          processed.bytes,
          processed.limit,
          processed.receipt.inlined.length,
        );
        rejected.push({ name: processed.fileName, reason });
        pending.push({
          fileName: processed.fileName,
          receipt: processed.receipt,
          // 只报「总体积超了」，用户唯一能做的就是把整份文档拆开重试；点名最大的
          // 那几张图片才是可操作的那部分信息。
          notes: [notStagedNote(reason), ...inlineTooLargeImageLines(processed.images)],
          pairingSkipped: processed.pairingSkipped,
        });
        continue;
      }
      built.push(new File([processed.rewritten], processed.fileName, { type: "text/markdown" }));
      pending.push({
        fileName: processed.fileName,
        receipt: processed.receipt,
        notes: [],
        pairingSkipped: processed.pairingSkipped,
      });
    }
    appendStagedSkipped(rejected);
    // 内联产物都是 .md，不会再含 bundle，所以走同步入列这一半即可（也因此能拿到
    // 「哪些真进了列表」来决定回执上的标注）。
    const outcome = built.length > 0
      ? stageIncomingFilesSync(built)
      : { added: [] as File[], skipped: [] as SkippedStagedFile[], duplicates: [] as File[] };
    const addedNames = new Set(outcome.added.map((file) => file.name));
    const duplicateNames = new Set(outcome.duplicates.map((file) => file.name));
    const skippedReasons = new Map(outcome.skipped.map((item) => [item.name, item.reason]));
    setBundleReceipts((previous) => [
      ...previous,
      ...pending.map((entry, index) => ({
        key: `${bundleLabel}::${entry.fileName}::${previous.length + index}`,
        bundleLabel,
        fileName: entry.fileName,
        receipt: entry.receipt,
        notes: entry.notes.length > 0
          ? entry.notes
          : stagedOutcomeNotes(entry.fileName, addedNames, duplicateNames, skippedReasons),
        pairingSkipped: entry.pairingSkipped,
      })),
    ]);
  }

  /** 成功内联却没进待上传列表时的回执标注（去重 / 单次上限 / 类型或大小被挡下）。 */
  function stagedOutcomeNotes(
    fileName: string,
    added: ReadonlySet<string>,
    duplicates: ReadonlySet<string>,
    skippedReasons: ReadonlyMap<string, string>,
  ): string[] {
    if (added.has(fileName)) return [];
    if (duplicates.has(fileName)) return [notStagedNote(ALREADY_STAGED_REASON)];
    const reason = skippedReasons.get(fileName);
    return [reason ? notStagedNote(reason) : notStagedNote(ALREADY_STAGED_REASON)];
  }

  function toggleBundleCandidate(path: string) {
    setBundleChoice((previous) => {
      if (!previous) return previous;
      const selected = new Set(previous.selected);
      if (selected.has(path)) selected.delete(path); else selected.add(path);
      return { ...previous, selected };
    });
  }

  function confirmBundleChoice() {
    if (!bundleChoice) return;
    const { label, files, candidates, selected, generation } = bundleChoice;
    setBundleChoice(null);
    // 与 setBundleChoice(null) 同一个事件处理里把进行态文案恢复回来（下面还要内联、
    // 还可能接着处理同批的下一个包）：留给 await 之后再恢复会空出一帧「既没有勾选
    // 面板、也没有进行态」的可点窗口。
    syncBundleBusyLabel();
    // generation 是这个面板所属链起跑时捕获的那份，不是"现在"——传给
    // stageBundleCandidates 让它按同一条链的世代把关（评审 F2）。
    stageBundleCandidates(
      label, candidates.filter((item) => selected.has(item.path)), files, generation,
    );
    bundleChoiceResolveRef.current?.();
    bundleChoiceResolveRef.current = null;
  }

  function cancelBundleChoice() {
    setBundleChoice(null);
    syncBundleBusyLabel();
    bundleChoiceResolveRef.current?.();
    bundleChoiceResolveRef.current = null;
  }

  // 读文本类文件前 8KB → 批量调 /detect-doc-types → 回填仍为空（未手动选）的类型。
  async function detectStagedTypes(added: File[]) {
    const textFiles = added.filter((file) => /\.(md|markdown|csv|txt)$/i.test(file.name));
    if (textFiles.length === 0) return;
    try {
      const items = await Promise.all(
        textFiles.map(async (file) => ({ name: file.name, sample: await file.slice(0, 8000).text() }))
      );
      const results = await detectSourceTypes(items);
      const byName: Record<string, string> = {};
      results.forEach((r) => { if (r.doc_type_id) byName[r.name] = r.doc_type_id; });
      if (Object.keys(byName).length === 0) return;
      // 按**回填这一刻**的列表定位（不是发起时的快照）：检测是异步的，期间用户可能已经
      // 移除了几项或又添了几项。只填**未被用户表态过**的项（值空且未 touched），既不
      // 覆盖已选的具体类型、也不覆盖「检测在飞时用户改回自动检测」这种空但 touched 的
      // 显式选择。**绝不动 touched**——auto-detect 是系统建议、不是用户表态。
      updateStaged((prev) => ({
        ...prev,
        docTypes: fillAutoDetectedTypes(
          prev.docTypes,
          prev.files.map((file) => byName[file.name]),
          prev.touched,
        ),
      }));
    } catch {
      // 检测失败不影响上传：保持"自动检测"，用户可手动选。
    }
  }

  function setStagedDocType(index: number, value: string) {
    // 用户手动改了这一项 → 同时标记为显式设置（哪怕选回「自动检测」也是一次显式表态）。
    // 类型与 touched 在同一次更新里改：拆成两次 setState 会在两者之间留出一个检测可以
    // resolve 进来的间隙，把用户显式选的空「自动检测」当没表态覆盖掉。
    updateStaged((prev) => ({
      ...prev,
      docTypes: prev.docTypes.map((dt, i) => (i === index ? value : dt)),
      touched: markTouched(prev.touched, index),
    }));
  }

  function setAllStagedDocTypes(value: string) {
    updateStaged((prev) => ({
      ...prev,
      docTypes: prev.docTypes.map(() => value),
      touched: markAllTouched(prev.touched),
    }));
  }

  function removeStagedFile(index: number) {
    const removed = stagedRef.current.files[index];
    updateStaged((prev) => ({
      files: prev.files.filter((_, i) => i !== index),
      docTypes: prev.docTypes.filter((_, i) => i !== index),
      touched: prev.touched.filter((_, i) => i !== index),
    }));
    if (removed) {
      const removedKey = stagedFileKey(removed);
      setStagedWarnings((rows) => rows.filter((row) => stagedFileKey(row) !== removedKey));
    }
  }

  async function confirmUpload() {
    if (!currentNotebookId || stagedFiles.length === 0 || uploadBusy) return;
    if (
      sourceUploadMaxFilesPerBatch !== null
      && stagedFiles.length > sourceUploadMaxFilesPerBatch
    ) {
      setToast(`单次最多上传 ${sourceUploadMaxFilesPerBatch} 个来源文件。请先移除多余文件。`);
      return;
    }
    const { rejected: oversized } = splitFilesByUploadSize(stagedFiles, sourceUploadMaxBytes);
    if (oversized.length > 0 && sourceUploadMaxBytes !== null) {
      const limit = sourceUploadSizeLabel(sourceUploadMaxBytes);
      setToast(
        `有 ${oversized.length} 个待上传文件超过单个文件上限（${limit}）。请先移除它们，或联系管理员调整上限。`,
      );
      return;
    }
    const blockedReason = documentUploadBlockReason(docCapacity, stagedFiles.length);
    if (blockedReason) {
      setToast(blockedReason);
      return;
    }
    setUploadBusy(true);
    const requestOwner = {};
    uploadRequestOwnerRef.current = requestOwner;
    try {
      await confirmUploadInner();
    } catch (error) {
      if (uploadRequestOwnerRef.current === requestOwner) reportError(error);
    } finally {
      if (uploadRequestOwnerRef.current === requestOwner) {
        uploadRequestOwnerRef.current = null;
        setUploadBusy(false);
      }
    }
  }

  async function confirmUploadInner() {
    const owner = library.captureOwner();
    if (!owner || stagedFiles.length === 0) return;
    const formData = new FormData();
    stagedFiles.forEach((file) => formData.append("files", file));
    // 每个文件发两个并列字段：doc_types（原值，"" = 自动检测）+ doc_type_explicit
    // （"1"/"0"，用户是否手动动过下拉框）。后端 reuse 路径只在显式时才改/重置既有来源的
    // 类型——auto-detect 自动填的建议值发 "0"，重传不会静默把既有类型抹掉（uploadDocTypeFields）。
    uploadDocTypeFields(stagedDocTypes, stagedDocTypeTouched).forEach(({ docType, explicit }) => {
      formData.append("doc_types", docType);
      formData.append("doc_type_explicit", explicit);
    });
    // 上传前各来源的文档类型：内容判重会沿用既有来源，但用户新选的类型仍会写进
    // 去并触发按新类型重抽——只有对着上传前的值才看得出这次到底改没改。
    const docTypesBefore = new Map(sources.map((source) => [source.id, source.doc_type ?? ""]));
    const uploaded = await uploadSources(owner.notebookId, formData);
    const outcome = summarizeUpload(uploaded, docTypesBefore);
    // 用折叠去重后的 outcome.sources，不是原始 uploaded：一次上传里两个内容相同的
    // 文件会让后端对同一个 id 返回两条（新建 + 命中它的 reused），直接铺进 state 会
    // 渲染出重复卡片，直到重开笔记本才消失。
    if (!library.commitUploadedSources(owner, outcome.sources, outcome.added.length)) return;
    const stillOwned = () => library.captureOwner() === owner;
    await effects.refreshCollection(stillOwned);
    if (!stillOwned()) return;
    // P1(codex PR#334 第9轮):快传小文件可能后台已解析完(返回即 extracted),hasPending 保持
    // false → 处理轮询的 reachedExtracted 分支不触发 → ask_available 陈旧为假、对话框空锁。显式
    // 重拉解禁;仍在解析中的慢路径由处理轮询覆盖。
    effects.revalidateAskAvailability();
    effects.reloadCheckup(owner.notebookId);  // 新源可能立即/后续成 H2–H6:刷新体检铃铛(codex 第5轮 P2)
    modals.requestClose("source-add", "button");
    setToast(outcome.toast);
  }

  // 「新建来源」成功后的收尾半——commitUrlSources + 刷新清单/对话可用性/体检
  // 铃铛。抽成独立函数是给 importGapSuggestion(X9 PR-A T3,站外来源建议的
  // 「导入」按钮)复用同一条链路,行为逐字不变:三处早退(commit 失败 / 刷新期间
  // owner 已变)在这里仍然只是让本函数提前返回 false,调用方(submitUrlSources)
  // 拿到 false 时同样直接 return,与重构前「在 if 块内直接 return」的控制流
  // 完全一致。
  async function applyImportedUrlSources(
    owner: SourceLibraryOwner,
    created: readonly SourceSummary[],
  ): Promise<boolean> {
    if (!library.commitUrlSources(owner, created)) return false;
    // Maintain only the Ask surfaces' count (notebookSourceTotal, unfiltered) optimistically.
    // sourcesTotal (source-list pagination + Source Stack header) is deliberately left to
    // re-sync on the next source-page fetch: it tracks the *applied* source-search filter, and
    // there is no reliable applied-query signal here — sourceQuery is the editable draft (search
    // applies only on Enter), not necessarily what produced the current page — so optimistically
    // bumping it risks either a stale count or a phantom filtered page. This matches the pre-#332
    // baseline (URL import never touched sourcesTotal); the file-upload path's own unconditional
    // bump is pre-existing and out of scope here. 文档上限门控读的 notebookDocumentCount 同步 +N，
    // 故链接导入 +N 后满额判定同步跟进。
    const stillOwned = () => library.captureOwner() === owner;
    await effects.refreshCollection(stillOwned);
    if (!stillOwned()) return false;
    effects.revalidateAskAvailability(); // P1:导入即产出可检索证据时解禁对话框(同 confirmUpload)
    effects.reloadCheckup(owner.notebookId);  // 同理刷新体检铃铛(codex 第5轮 P2)
    return true;
  }

  async function submitUrlSources() {
    const owner = library.captureOwner();
    if (!owner) return;
    const urls = parseUrlLines(urlText);
    if (urls.length === 0) {
      setToast("请粘贴至少一个 http/https 链接");
      return;
    }
    setUrlBusy(true);
    const requestOwner = {};
    urlRequestOwnerRef.current = requestOwner;
    setUrlRejected([]);
    try {
      const result = await importUrlSources(owner.notebookId, urls);
      if (result.created.length > 0) {
        if (!(await applyImportedUrlSources(owner, result.created))) return;
      }
      if (library.captureOwner() !== owner) return;
      setUrlRejected(result.rejected);
      setToast(`已添加 ${result.created.length} 个，被拒 ${result.rejected.length} 个`);
      if (result.rejected.length === 0) {
        setUrlText("");
        setLinkSectionOpen(false);
      }
    } catch (error) {
      if (urlRequestOwnerRef.current === requestOwner) reportError(error);
    } finally {
      if (urlRequestOwnerRef.current === requestOwner) {
        urlRequestOwnerRef.current = null;
        setUrlBusy(false);
      }
    }
  }


  /** Navigation detaches local request feedback; the library still decides
   * whether a completed write belongs to the visible workspace. */
  function detachRequests() {
    uploadRequestOwnerRef.current = null;
    urlRequestOwnerRef.current = null;
    setUploadBusy(false);
    setUrlBusy(false);
  }

  function dismiss() {
    resetStagedIntake();
    setLinkSectionOpen(false);
    sourceModalDismissedRef.current = true;
  }

  function openFiles() {
    setLinkSectionOpen(false);
    openSourceModal();
  }

  return {
    view: {
      stagedFiles, stagedDocTypes, stagedSkipped, stagedWarnings, bundleChoice,
      bundleReceipts, bundleBusyLabel, dropZoneDragActive, uploadBusy,
      linkSectionOpen, urlText, urlBusy, urlRejected, sourceImagePairingEnabled,
      supportedSourceAccept, supportedSourceUserHint, sourceUploadMaxBytes,
      sourceUploadMaxFilesPerBatch, sourceUploadConfigLoading,
      sourceFilePickerDisabled, sourceFilePickerHint, stagedUploadBlockedReason,
      docCapacity, atDocCapacityHint,
    },
    commands: {
      openSourceModal, openFiles, closeSourceModal, resetStagedIntake, dismiss, detachRequests,
      stageFiles, handleStageDragOver, handleStageDrop, setDropZoneDragActive,
      toggleBundleCandidate, confirmBundleChoice, cancelBundleChoice,
      setStagedDocType, setAllStagedDocTypes, removeStagedFile, confirmUpload,
      applyImportedUrlSources, submitUrlSources, setUrlText,
      dismissSkipped: () => setStagedSkipped([]),
      dismissWarnings: () => setStagedWarnings([]),
      dismissReceipts: () => setBundleReceipts([]),
      toggleLinks: () => { setUrlRejected([]); setLinkSectionOpen((open) => !open); },
      cancelLinks: () => { setUrlText(""); setUrlRejected([]); setLinkSectionOpen(false); },
    },
  } as const;
}

export type SourceIntake = ReturnType<typeof useSourceIntake>;

