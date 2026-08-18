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
  decodeMarkdownText,
  inlineMdImages,
  markdownFiles,
  parseZipBundle,
} from "./md-bundle.ts";
import { sourceUploadSizeLabel } from "./source-upload.ts";

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

/** 读取一个 zip `File` 的字节并交给 `md-bundle.ts` 的纯函数解析。 */
export async function unpackZipFile(file: File, caps: BundleCaps): Promise<ZipBundleResult> {
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
      return "压缩包解压后体积过大";
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
};

/** 递归遍历一个被拖入的文件夹，收集其中全部文件的路径 + `File` 引用（跳过
 *  `.DS_Store`）。是否真的读取字节由调用方另行决定（见 `directoryHasMarkdown` /
 *  `readDirectoryAsBundleFiles`），这里只走一遍目录树。
 */
export async function collectDirectoryFiles(
  root: FileSystemDirectoryEntry,
  opts: { maxDepth?: number; maxFiles?: number } = {},
): Promise<CollectedDirectory> {
  const maxDepth = opts.maxDepth ?? BUNDLE_DIR_MAX_DEPTH;
  const maxFiles = opts.maxFiles ?? BUNDLE_DIR_MAX_FILES;
  const entries: CollectedDirectoryEntry[] = [];
  let truncated = false;

  async function walk(entry: FileSystemEntry, path: string, depth: number): Promise<void> {
    if (truncated || entry.name === DS_STORE) return;
    if (entry.isFile) {
      if (entries.length >= maxFiles) { truncated = true; return; }
      const file = await readEntryFile(entry as FileSystemFileEntry);
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
        if (truncated) return;
      }
    }
  }

  const rootReader = root.createReader();
  for (;;) {
    const batch = await readDirEntries(rootReader);
    if (batch.length === 0) break;
    for (const child of batch) {
      await walk(child, child.name, 1);
      if (truncated) break;
    }
    if (truncated) break;
  }
  return { entries, truncated };
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

export type ProcessedMarkdown =
  | { ok: true; fileName: string; rewritten: string; receipt: InlineReceipt }
  | { ok: false; fileName: string; receipt: InlineReceipt; bytes: number; limit: number };

/** 内联一个已选中的 md 候选：解码文本 → 按包内其余文件配对/内联图片 → 算出用作
 *  暂存文件名的 basename。超过单文件上传上限时不返回可入列的正文，只返回体积明细
 *  供调用方拒绝并如实报出（设计文档 §3.1 第 5 条的预检）。
 */
export function processMarkdownCandidate(
  mdFile: BundleFile,
  files: readonly BundleFile[],
  opts: InlineOptions,
): ProcessedMarkdown {
  const text = decodeMarkdownText(mdFile.bytes);
  const fileName = baseNameOf(mdFile.path);
  const result = inlineMdImages(mdFile.path, text, files, opts);
  if (result.ok) {
    return { ok: true, fileName, rewritten: result.rewritten, receipt: result.receipt };
  }
  return {
    ok: false,
    fileName,
    receipt: result.receipt,
    bytes: result.error.bytes,
    limit: result.error.limit,
  };
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

/** 内联后体积超过单文件上传上限时的回执文案。 */
export function inlineTooLargeMessage(bytes: number, limit: number): string {
  return `内联图片后体积${approxByteSizeLabel(bytes)}，超过单文件上限（${sourceUploadSizeLabel(limit)}），请精简图片或拆分文档后重试`;
}

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
