/** 「zip / 拖入文件夹」→「配对回执 + 待暂存文件」的浏览器侧编排层。
 *
 *  `md-bundle.ts` 是纯函数管线（zip 解压、路径配对、内联改写），刻意不碰 DOM，浏览器与
 *  Node 测试环境都能跑。本模块是它与浏览器 `File` / `DataTransfer` / File and Directory
 *  Entries API 之间的桥：读 zip 字节、递归遍历拖入的文件夹、把结果组织成好交给
 *  `md-bundle.ts` 的形态，并把回执翻译成面向用户的中文文案。`page.tsx` 只负责 React
 *  state 与事件接线，不重复这里的判断或措辞。
 *
 *  本文件不修改 `md-bundle.ts`——已过双评审的既有管线，只在这里消费它的导出 API。
 */

import {
  type BundleCaps,
  type BundleError,
  type BundleFile,
  type InlineOptions,
  type InlineReceipt,
  type MissingImage,
  type NoAltImage,
  type RemoteImage,
  type UnsupportedImage,
  type ZipBundleResult,
  MD_BUNDLE_MAX_ENTRIES,
  MD_BUNDLE_TOTAL_BYTES_FACTOR,
  decodeMarkdownText,
  inlineMdImages,
  markdownFiles,
  parseZipBundle,
  resolveLimit,
  utf8ByteLength,
} from "./md-bundle.ts";
import { sourceUploadSizeLabel, type SkippedStagedFile } from "./source-upload.ts";

// ---------------------------------------------------------------------------
// 文件夹遍历护栏
// ---------------------------------------------------------------------------

/** 文件夹拖拽递归遍历的最大深度（根的直接子项记 1）。zip 没有这个问题——central
 *  directory 是一次性线性扫描，不递归；文件夹遍历是真实的树递归，需要独立的深度闸
 *  防止病态或成环的目录结构导致无限递归。 */
export const BUNDLE_DIR_MAX_DEPTH = 16;

/** 文件夹拖拽收集的文件数上限。与 zip 的 `MD_BUNDLE_MAX_ENTRIES` 同一护栏思路——都是
 *  「一次前端解包能装下多少个虚拟文件集条目」的同一预算，直接复用同一个数值，不必
 *  凭空造出第二个不相关的上限。 */
export const BUNDLE_DIR_MAX_FILES = MD_BUNDLE_MAX_ENTRIES;

/** zip 里零个 markdown 文件时的回执文案——唯一实现真源，避免 UI 层另写一遍。 */
export const NO_MARKDOWN_IN_BUNDLE_REASON = "压缩包里没有 markdown 文件";

/** 拖入的文件/压缩包在读取字节时失败（条目在拖放后被移动、删除或权限变化）。
 *  必须有一条持久可见的原因：静默吞掉等于「拖了没反应」。 */
export const BUNDLE_READ_FAILED_REASON = "读取失败，文件可能已被移动或删除";

/** 递归遍历拖入的文件夹时失败（同上，只是主体是整个文件夹）。 */
export const DIRECTORY_READ_FAILED_REASON = "读取文件夹失败，其中的文件可能已被移动或删除";

const EMPTY_BYTES = new Uint8Array(0);
const DS_STORE = ".DS_Store";

function baseNameOf(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

// ---------------------------------------------------------------------------
// zip 解包
// ---------------------------------------------------------------------------

/** `sourceUploadMaxBytes` 尚未从 `/system/config` 到达时是 `null`；`BundleCaps`/
 *  `InlineOptions` 要求一个 `number`。两者都在内部经 `resolveLimit` 把「非正数」
 *  归一成「没有可用上限，不做本地预检」，所以 `0` 与「缺失」是同一件事——这里统一
 *  这次转换，不让调用方各写一份 `?? 0`。 */
export function bundleCapsFrom(uploadMaxBytes: number | null): BundleCaps {
  return { uploadMaxBytes: uploadMaxBytes ?? 0 };
}

/** 一个虚拟文件集（zip 解压后 / 拖入文件夹）允许的总字节预算；`null` = 没有可用
 *  上限，不做本地预检（与 `resolveLimit` 的「非正数 = 不预检」同一口径）。
 *
 *  数值不在这里另抄一份：`md-bundle.ts` 的 zip 解压护栏用的就是
 *  `uploadMaxBytes × MD_BUNDLE_TOTAL_BYTES_FACTOR`，两个入口共用同一个系数，
 *  否则「同一份内容打成 zip 拖进来能过、直接拖文件夹却不能」就成了随机行为。 */
export function bundleTotalBytesLimit(uploadMaxBytes: number | null): number | null {
  const base = resolveLimit(uploadMaxBytes);
  return base === null ? null : base * MD_BUNDLE_TOTAL_BYTES_FACTOR;
}

/** 压缩输入上界的具名回退值，**兼**浏览器安全绝对顶（codex #518 R5 P2）。
 *
 *  取值依据：部署变量 `SOURCE_UPLOAD_MAX_MB` 的取值域是 1–1024，协议最大值即 1024 MiB，
 *  所以任何部署的单文件上限都 ≤ 本值——用它当回退不会比真实配置更宽。它刻意比
 *  「已知配置下的 `uploadMaxBytes × MD_BUNDLE_TOTAL_BYTES_FACTOR`」更严：配置到达
 *  之前宁可保守。
 *
 *  双重身份的第二半：已知配置时它是 `min(...)` 的**绝对顶**。顶配部署
 *  （`SOURCE_UPLOAD_MAX_MB=1024`）按系数算出的上界是 4 GiB——那正是这道闸要防的
 *  「整包读进内存耗死标签页」量级，护栏不能被自己的公式放空。代价（已登记）：
 *  压缩态超过 1 GiB 的归档即使解开后逐条合法也会被拒，这类极端包请把 md 拆出来
 *  直接上传。
 *
 *  这里**不能**沿用别处「拿不到上限就不预检」的口径——那正是本条要堵的洞：不预检
 *  等于把几 GB 的压缩包整包读进内存，标签页当场耗死，连一条结构化拒绝都给不出来。 */
export const BUNDLE_ZIP_INPUT_FALLBACK_CAP_BYTES = 1024 * 1024 * 1024;

/** 压缩输入上界里给 zip 容器结构与压缩微膨胀留的固定余量（codex #518 R5 P2）。
 *
 *  `File.size` 不保证 ≤ 解出总量：stored 条目带 local/central 两份头
 *  （`MD_BUNDLE_MAX_ENTRIES`=2000 条 × ~250B ≈ 500 KiB 封顶），deflate 对不可压数据
 *  还会微膨胀（约 5B/16KiB 块 ≈ 0.03%，1 GiB 顶配下 ≈ 320 KiB），另有 EOCD/注释。
 *  没有这份余量，「四个恰好贴着单文件上限的合法 md」打包后会因几百字节的头被误拒，
 *  与 docs 里「不拒掉本来合法的包」的不变量矛盾。4 MiB 把上述各项在绝对顶下全部
 *  盖住且方向保守（多放行的只是头部字节，解压后总量闸原样兜底）。
 *  精确数值登记于 `docs/product-and-api*.md`。 */
export const BUNDLE_ZIP_INPUT_OVERHEAD_SLACK_BYTES = 4 * 1024 * 1024;

/** 压缩输入（**读进内存之前**）的体积上界。
 *
 *  形状：`min(解压后总量线, 绝对顶) + 容器余量`。解压后总量线保证通过它的包大概率
 *  也过得了解压预算；绝对顶保证公式在顶配部署下不会自我放空（4 GiB → 1 GiB）；
 *  容器余量保证贴线合法包不因 zip 头/deflate 微膨胀被误拒。这道闸只把「几 GB 误选」
 *  的拒绝时点从「已经分配完整包」提前到「只看了 `File.size`」，解压后总量仍由
 *  `parseZipBundle` 的流式预算权威兜底。 */
export function bundleZipInputLimit(uploadMaxBytes: number | null): number {
  const configured = bundleTotalBytesLimit(uploadMaxBytes) ?? Infinity;
  return (
    Math.min(configured, BUNDLE_ZIP_INPUT_FALLBACK_CAP_BYTES)
    + BUNDLE_ZIP_INPUT_OVERHEAD_SLACK_BYTES
  );
}

/** 读取一个 zip `File` 的字节并交给 `md-bundle.ts` 的纯函数解析。
 *
 *  体积闸必须跑在 `arrayBuffer()` **之前**：`.zip` 刻意绕开普通上传的单文件大小
 *  校验（它不是上传目标，是前端交换格式），而解压后总量的闸只有在整包字节已经
 *  进了内存之后才够得着——误选一个几 GB 的归档因此能在任何结构化拒绝之前把标签页
 *  耗死（codex #518 R1 P2）。`File.size` 是条目元数据，取它不读任何内容，与文件夹
 *  那条路 `collectDirectoryFiles` 的零 I/O 预检同一口径。 */
export async function unpackZipFile(file: File, caps: BundleCaps): Promise<ZipBundleResult> {
  const inputLimit = bundleZipInputLimit(caps.uploadMaxBytes);
  if (file.size > inputLimit) {
    return { ok: false, error: { code: "too_large", actual: file.size, limit: inputLimit } };
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  return parseZipBundle(bytes, caps);
}

/** zip 解析失败码 → 面向用户的中文文案。 */
export function bundleErrorMessage(error: BundleError): string {
  switch (error.code) {
    case "not_a_zip":
      return "不是有效的压缩包";
    case "corrupt":
      return "压缩包已损坏或不完整";
    case "encrypted":
      return "不支持加密压缩包";
    case "zip64":
      return "压缩包过大（不支持 zip64）";
    case "unsupported_compression":
      return "压缩包使用了不支持的压缩方式";
    case "unsafe_entry_path":
      return "压缩包内存在非法路径条目";
    case "too_many_entries":
      return `压缩包内文件过多（最多 ${error.limit ?? MD_BUNDLE_MAX_ENTRIES} 个）`;
    case "too_large":
      // 两种形态，措辞必须分开：读进内存**之前**按 `File.size` 拦下的压缩输入是
      // 整包级拒绝（没有触发条目 `path`，带实际体积与上限），此时一个字节都没读，
      // 说「解压后」就是在描述一件没发生过的事；`parseZipBundle` 那条则必定带上
      // 触发条目的 `path`。
      return error.path === undefined && error.actual !== undefined && error.limit !== undefined
        ? `压缩包体积${approxByteSizeLabel(error.actual)}，超过上限（${sourceUploadSizeLabel(error.limit)}），未读取内容`
        : "压缩包解压后体积过大";
    default:
      return "无法读取该压缩包";
  }
}

// ---------------------------------------------------------------------------
// 文件夹遍历（File and Directory Entries API）
// ---------------------------------------------------------------------------

function readDirEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

function readEntryFile(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

export type CollectedDirectoryEntry = { path: string; file: File };

export type CollectedDirectory = {
  /** 每个文件条目：相对拖入文件夹根的路径（`/` 分隔，不含文件夹自身名字）+ 原生
   *  `File` 引用。刻意不在遍历阶段读字节——是否需要真的读全部内容（zip/文件夹配对
   *  管线要读，判断「有没有 markdown」或普通文件入列都不需要）由调用方决定。 */
  entries: CollectedDirectoryEntry[];
  /** 命中了深度或文件数护栏，没能遍历完整个文件夹（未完整读取，非法结构风险已止损）。 */
  truncated: boolean;
  /** 命中了总字节预算（`maxTotalBytes`），遍历提前止损。与 `truncated` 分开两个标志：
   *  两者的用户文案与可操作建议完全不同（「文件太多/太深」vs「总体积太大」）。 */
  overBudget: boolean;
  /** 已收集条目的字节总量（止损时是已累计的那部分）。只读 `File.size` 元数据得出，
   *  不读任何字节。 */
  totalBytes: number;
};

/** 递归遍历一个被拖入的文件夹，收集其中全部文件的路径 + `File` 引用（跳过
 *  `.DS_Store`）。是否真的读取字节由调用方另行决定（见 `directoryHasMarkdown` /
 *  `readDirectoryAsBundleFiles`），这里只走一遍目录树。
 *
 *  `maxTotalBytes` 是**零 I/O** 的总量预检：`File.size` 是条目元数据，取它不读内容。
 *  zip 那条路由 `parseZipBundle` 在解压前按 central directory 声明的大小拦下超量包；
 *  文件夹这条路此前完全没有对应护栏——`readDirectoryAsBundleFiles` 会把整个目录树
 *  的字节一次性读进内存，几个 GB 的素材文件夹足以把标签页拖垮。所以预算必须在
 *  **收集阶段**就累计并止损，而不是等到读字节的时候。
 */
export async function collectDirectoryFiles(
  root: FileSystemDirectoryEntry,
  opts: { maxDepth?: number; maxFiles?: number; maxTotalBytes?: number | null } = {},
): Promise<CollectedDirectory> {
  const maxDepth = opts.maxDepth ?? BUNDLE_DIR_MAX_DEPTH;
  const maxFiles = opts.maxFiles ?? BUNDLE_DIR_MAX_FILES;
  const maxTotalBytes = opts.maxTotalBytes ?? null;
  const entries: CollectedDirectoryEntry[] = [];
  let truncated = false;
  let overBudget = false;
  let totalBytes = 0;
  const stopped = () => truncated || overBudget;

  async function walk(entry: FileSystemEntry, path: string, depth: number): Promise<void> {
    if (stopped() || entry.name === DS_STORE) return;
    if (entry.isFile) {
      if (entries.length >= maxFiles) { truncated = true; return; }
      const file = await readEntryFile(entry as FileSystemFileEntry);
      if (maxTotalBytes !== null && totalBytes + file.size > maxTotalBytes) {
        overBudget = true;
        return;
      }
      totalBytes += file.size;
      entries.push({ path, file });
      return;
    }
    if (!entry.isDirectory) return;
    if (depth > maxDepth) { truncated = true; return; }
    const reader = (entry as FileSystemDirectoryEntry).createReader();
    for (;;) {
      const batch = await readDirEntries(reader);
      if (batch.length === 0) break;
      for (const child of batch) {
        await walk(child, path === "" ? child.name : `${path}/${child.name}`, depth + 1);
        if (stopped()) return;
      }
    }
  }

  const rootReader = root.createReader();
  for (;;) {
    const batch = await readDirEntries(rootReader);
    if (batch.length === 0) break;
    for (const child of batch) {
      await walk(child, child.name, 1);
      if (stopped()) break;
    }
    if (stopped()) break;
  }
  return { entries, truncated, overBudget, totalBytes };
}

/** 只看路径（零 I/O）判断这批已收集的文件夹条目里有没有 markdown——用于决定要不要
 *  真的把每个文件读成字节交给配对管线，还是按普通文件逐个入列（红线：不含 md 的
 *  文件夹必须保持既有拖拽行为，不该为了这一个布尔判断去读文件夹里可能几十上百个
 *  文件的全部内容）。
 *
 *  借道 `md-bundle.ts` 的 `markdownFiles`：它只读 `.path`，喂假字节即可。
 */
export function directoryHasMarkdown(entries: readonly CollectedDirectoryEntry[]): boolean {
  const probe: BundleFile[] = entries.map((entry) => ({ path: entry.path, bytes: EMPTY_BYTES }));
  return markdownFiles(probe).length > 0;
}

/** 把已收集的文件夹条目真正读成 `BundleFile[]`（供配对/内联管线使用）。只在确认
 *  文件夹里含 markdown 之后才应调用——这一步会读全部文件的字节。 */
export async function readDirectoryAsBundleFiles(
  entries: readonly CollectedDirectoryEntry[],
): Promise<BundleFile[]> {
  return Promise.all(
    entries.map(async (entry) => ({
      path: entry.path,
      bytes: new Uint8Array(await entry.file.arrayBuffer()),
    })),
  );
}

/** 文件夹遍历命中深度/文件数护栏时的回执文案。 */
export function directoryTruncatedMessage(): string {
  return `文件夹内容过多或层级过深（最多 ${BUNDLE_DIR_MAX_FILES} 个文件、${BUNDLE_DIR_MAX_DEPTH} 层），未能完整读取，请精简后重试`;
}

/** 文件夹遍历命中总字节预算时的回执文案（内容一个字节都没读，只看了体积元数据）。 */
export function directoryTooLargeMessage(limit: number): string {
  return `文件夹内容总体积超过上限（${sourceUploadSizeLabel(limit)}），未读取内容，请精简后重试`;
}

// ---------------------------------------------------------------------------
// md 候选分类 + 内联
// ---------------------------------------------------------------------------

export type BundleClassification =
  | { kind: "empty" }
  | { kind: "single"; file: BundleFile }
  | { kind: "choose"; candidates: BundleFile[] };

/** 虚拟文件集里有几个 markdown：零个／恰好一个／多个，决定调用方是报错、直接处理
 *  还是弹出勾选（设计文档 §3.1 第 2 条）。 */
export function classifyBundleContents(files: readonly BundleFile[]): BundleClassification {
  const candidates = markdownFiles(files);
  if (candidates.length === 0) return { kind: "empty" };
  if (candidates.length === 1) return { kind: "single", file: candidates[0] };
  return { kind: "choose", candidates };
}

/** 同一批被勾选的 md 里 basename 撞车时（`a/README.md` 与 `b/README.md`）的消歧命名。
 *
 *  不消歧的后果不是"名字难看"：待上传列表按「同名同大小」去重，两份同名同大小的
 *  README 会被静默折叠成一份；即使大小不同，列表里也会出现两行看不出区别的
 *  `README.md`，上传后在来源页签同样分不清。
 *
 *  规则：basename 在本批内唯一 → 原样用它；撞车 → 用包内完整路径把分隔符换成 `-`
 *  （`a/README.md` → `a-README.md`，深层目录同理逐段拼接），仍撞车（理论上不会，
 *  路径本身唯一）则在扩展名前追加 `-2`、`-3`。产出永远不含路径分隔符，且保留原
 *  扩展名——后端按扩展名分派解析器。 */
export function bundleFileNamesFor(paths: readonly string[]): Map<string, string> {
  const baseCounts = new Map<string, number>();
  for (const path of paths) {
    const base = baseNameOf(path);
    baseCounts.set(base, (baseCounts.get(base) ?? 0) + 1);
  }
  const used = new Set<string>();
  const names = new Map<string, string>();
  for (const path of paths) {
    const base = baseNameOf(path);
    let name = (baseCounts.get(base) ?? 0) > 1 ? flattenBundlePath(path) : base;
    if (used.has(name)) {
      const dot = name.lastIndexOf(".");
      const stem = dot > 0 ? name.slice(0, dot) : name;
      const ext = dot > 0 ? name.slice(dot) : "";
      let serial = 2;
      while (used.has(`${stem}-${serial}${ext}`)) serial += 1;
      name = `${stem}-${serial}${ext}`;
    }
    used.add(name);
    names.set(path, name);
  }
  return names;
}

function flattenBundlePath(path: string): string {
  return path.replace(/[\\/]+/g, "-").replace(/^-+/, "");
}

export type ProcessedMarkdown =
  | {
    ok: true;
    fileName: string;
    rewritten: string;
    receipt: InlineReceipt;
    /** 本次是否因部署级开关关闭而**整个跳过**图片配对/内联（而不是「配对了但零命中」）。
     *  两者对回执面板必须是不同的措辞：空回执 + 未跳过＝真的没在正文里发现本地图片；
     *  空回执 + 跳过＝根本没去看过，不能说「没发现」。 */
    pairingSkipped: boolean;
  }
  | {
    ok: false;
    fileName: string;
    receipt: InlineReceipt;
    bytes: number;
    limit: number;
    /** 按体积降序的逐图明细，供调用方如实列出「是哪几张撑爆的」。 */
    images: ReadonlyArray<{ src: string; path: string; encodedBytes: number }>;
    /** 同上——`imagesEnabled === false` 短路分支下，超限判定针对的是正文本身而非
     *  内联结果，`images` 恒为空。 */
    pairingSkipped: boolean;
  };

export type MarkdownCandidateOptions = InlineOptions & {
  /** 部署级图片存储总开关（`/system/config` 的 `source_images_enabled`）。省略按
   *  `true` 处理。`false`（或 `imageMaxBytes`/`maxImagesPerSource` 被显式配成 `0`，
   *  见 `bundleImagesEffectivelyEnabled`）时整段图片配对/内联被跳过——服务端不会
   *  持久化任何图片，花时间做 base64 编码没有意义（design doc §3.3：「不白付 base64
   *  体积」），正文原样入列，未内联的相对图片链接由调用方在回执面板顶部统一说明。 */
  imagesEnabled?: boolean;
};

function emptyInlineReceipt(): InlineReceipt {
  return { inlined: [], missing: [], unsupported: [], remote: [], noAlt: [] };
}

/** 图片存储的**有效**开关：部署总开关为 `false`，**或**单图字节上限/每来源张数上限
 *  被显式配成 `0`，都意味着服务端一张图都不会持久化。
 *
 *  `MINERU_MAX_IMAGE_BYTES=0` / `MINERU_MAX_IMAGES_PER_SOURCE=0` 是合法部署值（后端
 *  转发这两个字段时刻意没有正数约束，有后端用例钉住），语义就是「一张都不存」。
 *  浏览器侧若把 `0` 当成 `resolveLimit` 那套「拿不到上限、不做本地预检」，就会照常
 *  base64 内联、在回执里报「N 张已内联」，而上传后这些资产被服务端全部丢弃——既白付
 *  了体积，又对用户撒了谎（codex #518 R1 P2）。
 *
 *  判定放在本模块而不是只写在 `page.tsx` 的调用点，是为了让
 *  `processMarkdownCandidate` 的短路成为**结构性**保证：`0` 因此永远到不了
 *  `inlineMdImages` 的上限参数，也就不会被它内部的 `resolveLimit` 反过来解释成
 *  「无上限」。 */
export function bundleImagesEffectivelyEnabled(opts: {
  imagesEnabled?: boolean;
  imageMaxBytes?: number | null;
  maxImagesPerSource?: number | null;
}): boolean {
  return opts.imagesEnabled !== false
    && opts.imageMaxBytes !== 0
    && opts.maxImagesPerSource !== 0;
}

/** `/system/config` 的 `source_upload_max_files_per_batch` 尚未到达时，
 *  「内联之前先算名额」那道闸的具名回退值。
 *
 *  权威闸 `mergeStagedFiles` 允许 `maxFilesPerBatch === null`＝「配置没到，不预检」，
 *  对它是安全的（只是把截断推迟到服务端）；对本闸不是：没有名额上限就等于退回
 *  「先把两千份候选全部内联成 base64，再丢掉其中一千九百八十份」。
 *
 *  取值 20 = 后端 `SOURCE_UPLOAD_MAX_FILES_PER_BATCH`（`backend/app/core/config.py`），
 *  那是固定的 multipart 资源护栏、不随部署变化；下发值一旦到达一律以它为准。 */
export const BUNDLE_STAGE_FALLBACK_MAX_FILES_PER_BATCH = 20;

/** 候选因单次上传数量上限而**根本没被处理**时的跳过原因。
 *
 *  与 `staged-files.ts` 的 `batchFullReason`（那是「内联完了但装不进列表」）刻意
 *  分成两句：这一句的主体是「连图片配对都没跑」，用户看到的回执里不会有它的任何
 *  配对结果，必须说清是为什么。 */
export function bundleBatchFullReason(maxFilesPerBatch: number): string {
  return `超出单次上传上限（${maxFilesPerBatch} 个），未处理，请先上传当前批次再继续添加`;
}

/** 内联一个已选中的 md 候选：解码文本 → 按包内其余文件配对/内联图片 → 算出用作
 *  暂存文件名的 basename（`fileNameOverride` 用于同批同名消歧，见
 *  `bundleFileNamesFor`）。超过单文件上传上限时不返回可入列的正文，只返回体积明细
 *  供调用方拒绝并如实报出（设计文档 §3.1 第 5 条的预检）。
 *
 *  图片存储**有效关闭**时（总开关 `false`，或两个上限中任一被显式配成 `0`，判据见
 *  `bundleImagesEffectivelyEnabled`）跳过 `inlineMdImages` 整个配对/内联流程，
 *  只做原有的单文件上限校验（正文本身仍受这条护栏约束），md 正文原样返回、回执
 *  全空——`md-bundle.ts` 是已过双评审的纯函数管线，这条部署级开关只在编排层
 *  （本文件）短路，不在那边加分支。
 */
export function processMarkdownCandidate(
  mdFile: BundleFile,
  files: readonly BundleFile[],
  opts: MarkdownCandidateOptions,
  fileNameOverride?: string,
): ProcessedMarkdown {
  const text = decodeMarkdownText(mdFile.bytes);
  const fileName = fileNameOverride ?? baseNameOf(mdFile.path);
  if (!bundleImagesEffectivelyEnabled(opts)) {
    const bytes = utf8ByteLength(text);
    const limit = resolveLimit(opts.uploadMaxBytes);
    if (limit !== null && bytes > limit) {
      return {
        ok: false, fileName, receipt: emptyInlineReceipt(), bytes, limit, images: [],
        pairingSkipped: true,
      };
    }
    return { ok: true, fileName, rewritten: text, receipt: emptyInlineReceipt(), pairingSkipped: true };
  }
  const result = inlineMdImages(mdFile.path, text, files, opts);
  if (result.ok) {
    return {
      ok: true, fileName, rewritten: result.rewritten, receipt: result.receipt, pairingSkipped: false,
    };
  }
  return {
    ok: false,
    fileName,
    receipt: result.receipt,
    bytes: result.error.bytes,
    limit: result.error.limit,
    images: result.error.images,
    pairingSkipped: false,
  };
}

export type BundleCandidateBatch = {
  /** 按候选顺序真正跑过配对/内联的那些（含被单文件上限拒掉的——它们**被处理过**，
   *  有真实回执可展示）。 */
  processed: ProcessedMarkdown[];
  /** 名额耗尽、**根本没被处理**的那些，逐条带用户可读原因。刻意不给它们造回执：
   *  一条空回执会被渲染成「未在正文中发现本地图片」，那是一句没发生过的事实断言。 */
  skipped: SkippedStagedFile[];
};

/** 把一批被勾选的 md 候选按**剩余名额**逐个处理，名额耗尽即**停止调用**
 *  `processMarkdownCandidate`。
 *
 *  为什么闸必须在这里而不是等入列时的 `mergeStagedFiles`：候选默认全选，而内联会把
 *  每张图 base64 展开进正文。一个合法的两千条目压缩包里若每份 md 都引用同一张几 MB
 *  的图，「先全部内联、再由入列闸截断」等于白分配 GB 级 base64——只有前 N 份进得了
 *  列表，其余当场作废（codex #518 R1 P1）。
 *
 *  名额只由**成功产出**的候选消耗：被单文件上限拒掉的那些不入列、也就不占格，与
 *  `mergeStagedFiles` 的口径一致。去重会让这个估算偏保守（同名同大小的重复项在这里
 *  占了格、在入列时其实不占），方向安全：宁可让用户再拖一次，也不能先分配再丢弃。 */
export function processBundleCandidates(
  candidates: readonly BundleFile[],
  files: readonly BundleFile[],
  opts: MarkdownCandidateOptions,
  batch: {
    /** 同批同名消歧后的文件名（`bundleFileNamesFor` 的产出）。 */
    names: ReadonlyMap<string, string>;
    /** 本批还能产出几份（调用方按「上限 − 列表现有数量」算出）。 */
    remainingSlots: number;
    /** 上限本身，只用于跳过原因的措辞。 */
    batchCap: number;
  },
): BundleCandidateBatch {
  const processed: ProcessedMarkdown[] = [];
  const skipped: SkippedStagedFile[] = [];
  let produced = 0;
  for (const candidate of candidates) {
    if (produced >= batch.remainingSlots) {
      skipped.push({
        name: batch.names.get(candidate.path) ?? candidate.path,
        reason: bundleBatchFullReason(batch.batchCap),
      });
      continue;
    }
    const result = processMarkdownCandidate(candidate, files, opts, batch.names.get(candidate.path));
    if (result.ok) produced += 1;
    processed.push(result);
  }
  return { processed, skipped };
}

// ---------------------------------------------------------------------------
// 面向用户的回执文案
// ---------------------------------------------------------------------------

/** 任意字节数的人类可读展示，用于回执里"改写后体积"这类非规整数值。
 *  `sourceUploadSizeLabel`（source-upload.ts）只对整 MB/KB 给出简短形式，其余场景
 *  刻意退化成"N 字节"——那是为部署方给出的整数上限设计的，不适合这里几十 MB 量级、
 *  任意精度的实际图片/正文体积。 */
export function approxByteSizeLabel(bytes: number): string {
  const mib = 1024 * 1024;
  const kib = 1024;
  if (bytes >= mib) return `约 ${(bytes / mib).toFixed(1)} MB`;
  if (bytes >= kib) return `约 ${(bytes / kib).toFixed(1)} KB`;
  return `${bytes} 字节`;
}

/** 超过单文件上传上限时的回执文案。`inlinedCount` 是这次改写实际内联成功的图片张数：
 *  零张时——文本本身已超限、或部署关闭了图片存储、或正文里根本没有本地图片——「请精简
 *  图片」是对不上症状的建议（超限的是正文，不是图片），改说「拆分文档」；有内联时才是
 *  真的「内联图片撑爆了体积」，保留原措辞（含「精简图片」这个可操作项）。 */
export function inlineTooLargeMessage(bytes: number, limit: number, inlinedCount: number): string {
  if (inlinedCount === 0) {
    return `文档体积${approxByteSizeLabel(bytes)}，超过单文件上限（${sourceUploadSizeLabel(limit)}），请拆分文档后重试`;
  }
  return `内联图片后体积${approxByteSizeLabel(bytes)}，超过单文件上限（${sourceUploadSizeLabel(limit)}），请精简图片或拆分文档后重试`;
}

/** 超限回执里逐条列出的最大图片张数。再多就只报剩余计数——回执面板是有界滚动区，
 *  一份文档可能有上百张图，全列出等于把面板变成一份没人看得完的清单。 */
export const INLINE_TOO_LARGE_IMAGE_LINES = 3;

/** 「是哪几张图片撑爆了单文件上限」的明细行（按体积降序）。只报总量而不说是哪几张，
 *  用户唯一能做的就是把整份文档拆开重试——这条明细才是可操作的那部分信息。 */
export function inlineTooLargeImageLines(
  images: ReadonlyArray<{ src: string; path: string; encodedBytes: number }>,
  max: number = INLINE_TOO_LARGE_IMAGE_LINES,
): string[] {
  if (images.length === 0) return [];
  const ordered = [...images].sort((left, right) => right.encodedBytes - left.encodedBytes);
  const shown = ordered.slice(0, Math.max(1, max));
  const detail = shown
    .map((item) => `${item.path || item.src}（${approxByteSizeLabel(item.encodedBytes)}）`)
    .join("、");
  const rest = ordered.length - shown.length;
  return [`体积最大的图片：${detail}${rest > 0 ? `，另有 ${rest} 张` : ""}`];
}

/** 回执行上的「这一份没有进待上传列表」标注前缀。回执面板本身只描述图片配对结果，
 *  不标注就会让一份被拒/被去重的 md 看起来像入列成功了（「3 张已内联」而列表里
 *  根本没有它）。 */
export function notStagedNote(reason: string): string {
  return `未加入待上传列表：${reason}`;
}

/** 被去重掉（同名同大小已在列表里）时的标注原因。 */
export const ALREADY_STAGED_REASON = "同名同大小的文件已在列表中";

/** `source_images_enabled === false` 时，回执面板顶部的一条持久提示（唯一实现
 *  真源，避免 UI 层另写一遍）——此时 `processMarkdownCandidate` 已跳过整个图片
 *  配对/内联，压缩包/文件夹里的图片不会进入待上传正文，这条说明必须贴在结果
 *  概览之前，而不是散落在某一份 md 的逐条明细里（部署级开关，跟哪份 md 无关）。 */
export const BUNDLE_IMAGES_DISABLED_NOTE = "该部署未开启图片存储，压缩包中的图片将不会被保存";

/** `pairingSkipped` 为真的那一行的概览措辞——图片配对根本没有跑过，不能落进
 *  `receiptSummaryLine` 的「未在正文中发现本地图片」兜底（那句话是具体的事实断言：
 *  「看过了、没找到」，而这里是「压根没看」）。唯一实现真源，避免 UI 层另写一遍。 */
export const PAIRING_SKIPPED_SUMMARY = "图片存储已关闭，未做配对";

/** 一份 md 的配对结果概览行——「N 张已内联 / M 张未找到 / K 张不支持 / …」
 *  （设计文档 §3.1 第 7 条的字面措辞）。 */
export function receiptSummaryLine(receipt: InlineReceipt): string {
  const parts: string[] = [];
  if (receipt.inlined.length > 0) parts.push(`${receipt.inlined.length} 张已内联`);
  if (receipt.missing.length > 0) parts.push(`${receipt.missing.length} 张未找到`);
  if (receipt.unsupported.length > 0) parts.push(`${receipt.unsupported.length} 张不支持`);
  if (receipt.remote.length > 0) parts.push(`${receipt.remote.length} 张云端链接未拉取`);
  if (parts.length === 0) return "未在正文中发现本地图片";
  return parts.join(" / ");
}

/** `missing` 一条的展示行：未找到 + 近似候选（如有）。 */
export function missingImageLine(item: MissingImage): string {
  if (item.suggestions.length > 0) {
    return `未找到「${item.src}」，近似候选：${item.suggestions.join("、")}`;
  }
  return `未找到「${item.src}」`;
}

/** `unsupported` 一条的展示行：分类原因（措辞逐字对齐设计文档 §3.1 第 3 条）。 */
export function unsupportedImageLine(item: UnsupportedImage): string {
  const detail = (() => {
    switch (item.reason) {
      case "inline_position":
        return "不在独立段落中，本次未内联、保留原始链接";
      case "reference_syntax":
        return "引用式图片语法暂不支持内联";
      case "html_syntax":
        return "HTML 图片标签暂不支持内联";
      case "empty_src":
        return "图片链接为空";
      case "unsupported_image_type":
        return "不支持的图片格式（仅支持 png/jpeg/gif/webp）";
      case "image_too_large":
        return item.limit !== undefined
          ? `超过单张图片上限（${sourceUploadSizeLabel(item.limit)}）`
          : "超过单张图片上限";
      case "too_many_images":
        return item.limit !== undefined
          ? `超过单来源图片张数上限（${item.limit} 张）`
          : "超过单来源图片张数上限";
      default:
        return "不支持内联";
    }
  })();
  const location = item.path ?? item.src;
  return location ? `${detail}（${location}）` : detail;
}

/** `remote` 一条的展示行：云端链接不拉取（设计文档 §3.1 第 8 条）。 */
export function remoteImageLine(item: RemoteImage): string {
  return `云端图片链接未拉取，保留原文字（${item.src}）`;
}

/** `noAlt` 一条的展示行：无图注提示（设计文档 §3.1 第 7 条）。 */
export function noAltImageLine(item: NoAltImage): string {
  return `「${item.path}」无图注，上传后无法被检索`;
}
