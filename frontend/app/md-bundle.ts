/** 「markdown + 图片」压缩包/文件夹 → 自包含 markdown 的纯函数管线。
 *
 *  服务端的 markdown 摄取只认 `data:image/{png,jpeg,gif,webp};base64,...` 形态的
 *  `![alt](src)`，相对路径与 http 链接一概不处理；而且**只有独占一整段的图片**才会
 *  被当成图片元素落资产，列表项/表格单元格里的内嵌图片只留 alt 文本。本模块的职责
 *  就是把「zip 或文件夹」形态的 md+图片改写成上面那个自包含形态，交给现有上传通道，
 *  服务端零改动。
 *
 *  边界（刻意的）：
 *  - 纯函数 + Web 标准 API（TextDecoder / DecompressionStream / btoa），不碰 DOM，
 *    浏览器与 Node 测试环境都能跑。
 *  - 不产出面向用户的中文文案：回执里全是结构化枚举原因，措辞留给 UI 层，避免同一
 *    句话在管线与弹窗里各写一遍。
 *  - 零新依赖：zip 解析手写，deflate 用浏览器原生 `DecompressionStream('deflate-raw')`。
 */

// ---------------------------------------------------------------------------
// 护栏常量
// ---------------------------------------------------------------------------

/** 单个压缩包允许的**保留条目**数上限。
 *
 *  判据刻意是「保留下来的条目」（去掉目录条目、`__MACOSX` 资源叉与同名重复项之后）
 *  而不是 EOCD 声明的条目数：macOS 的「压缩」会给每个文件配一条 `__MACOSX/._x`
 *  伴随条目，按声明数判会让 1001 个真文件的包被当成 2002 条直接拒掉。
 *
 *  精确值登记于 `docs/product-and-api*.md`；这里是唯一实现真源。
 */
export const MD_BUNDLE_MAX_ENTRIES = 2000;

/** EOCD 声明条目数的宽松硬顶系数：`MD_BUNDLE_MAX_ENTRIES × 本系数`。
 *
 *  保留条目数只有扫完 central directory 才知道，所以扫描本身需要一个先验上界，
 *  否则一个声明了 65535 条的畸形包会先让我们白扫一遍。它**不是**产品上限——真实
 *  的闸是 `MD_BUNDLE_MAX_ENTRIES`，这里只要宽到装得下「每个文件配一条 `__MACOSX`
 *  再加上目录条目」的最坏形态即可。
 */
export const MD_BUNDLE_DECLARED_ENTRIES_FACTOR = 4;

/** EOCD 声明条目数的绝对上限（扫描前的先验闸，见上）。 */
export const MD_BUNDLE_MAX_DECLARED_ENTRIES =
  MD_BUNDLE_MAX_ENTRIES * MD_BUNDLE_DECLARED_ENTRIES_FACTOR;

/** 解压后总字节的上限系数：`uploadMaxBytes × 本系数`。
 *
 *  浏览器端 zip bomb 的唯一闸——超限立即停止解压并返回明确错误，绝不静默截断。
 *  精确值登记于 `docs/product-and-api*.md`；这里是唯一实现真源。
 */
export const MD_BUNDLE_TOTAL_BYTES_FACTOR = 4;

/** 配对失败时最多给出几个近似候选路径。
 *
 *  精确值登记于 `docs/product-and-api*.md`；这里是唯一实现真源。
 */
export const MD_BUNDLE_MAX_SUGGESTIONS = 3;

/** 被认作 markdown 的扩展名（小写，不含点）。 */
export const MD_BUNDLE_MARKDOWN_EXTENSIONS: readonly string[] = ["md", "markdown"];

/** base64 分块编码的每块字节数。
 *
 *  **必须是 3 的倍数**：btoa 逐块编码后直接拼接，只有块长为 3 的倍数时每块才不产生
 *  填充符，拼接结果才等于整体编码。同时它也是「禁止一次性把大数组展开进
 *  String.fromCharCode」的落点——按块展开，栈深度与图片大小无关。
 */
const BASE64_CHUNK_BYTES = 24_576;

// zip 结构常量（PKWARE APPNOTE）。
const EOCD_SIGNATURE = 0x06054b50;
const EOCD64_LOCATOR_SIGNATURE = 0x07064b50;
const CENTRAL_HEADER_SIGNATURE = 0x02014b50;
const LOCAL_HEADER_SIGNATURE = 0x04034b50;
const EOCD_FIXED_SIZE = 22;
const EOCD64_LOCATOR_SIZE = 20;
const ZIP_MAX_COMMENT_BYTES = 0xffff;
const CENTRAL_HEADER_FIXED_SIZE = 46;
const LOCAL_HEADER_FIXED_SIZE = 30;
const ZIP64_SENTINEL_32 = 0xffffffff;
const ZIP64_SENTINEL_16 = 0xffff;
const GP_FLAG_ENCRYPTED = 0x0001;
const GP_FLAG_STRONG_ENCRYPTION = 0x0040;
const METHOD_STORE = 0;
const METHOD_DEFLATE = 8;

/** macOS「压缩」生成的 AppleDouble 资源叉目录。整包跳过：它的条目永远不会被 md
 *  引用，却会占满条目数与解压体积预算。 */
const APPLE_DOUBLE_ROOT = "__MACOSX";

/** markdown 缩进代码块的起始缩进（空格数）。到达它的行不是段落，不能算独占段图片。 */
const INDENTED_CODE_SPACES = 4;

/** 一个制表符按几个空格计缩进（CommonMark：制表符扩展到 4 的倍数停止位）。 */
const TAB_WIDTH = 4;

/** 围栏代码块的最小反引号/波浪线个数。 */
const MIN_FENCE_LENGTH = 3;

// ---------------------------------------------------------------------------
// 虚拟文件集
// ---------------------------------------------------------------------------

/** zip 解包与文件夹遍历归一后的「虚拟文件集」条目。
 *
 *  `path` 一律用 `/` 分隔、相对包根、不带前导 `./`。文件夹遍历本身（`webkitGetAsEntry`）
 *  归 UI 接线任务，本模块只消费归一后的 `BundleFile[]`。
 */
export type BundleFile = { path: string; bytes: Uint8Array };

// ---------------------------------------------------------------------------
// zip 解析
// ---------------------------------------------------------------------------

export type BundleErrorCode =
  /** 找不到 EOCD：根本不是 zip，或尾部被截断。 */
  | "not_a_zip"
  /** 结构自相矛盾（签名错位、偏移越界等）。 */
  | "corrupt"
  /** 存在加密条目。 */
  | "encrypted"
  /** 用到了 zip64 扩展（超大包/超多条目）。 */
  | "zip64"
  /** 压缩方法既不是 store 也不是 deflate。 */
  | "unsupported_compression"
  /** 条目路径逃出包根（`../` 越界）。 */
  | "unsafe_entry_path"
  /** 条目数超过 `MD_BUNDLE_MAX_ENTRIES`。 */
  | "too_many_entries"
  /** 解压后总字节超过 `uploadMaxBytes × MD_BUNDLE_TOTAL_BYTES_FACTOR`。 */
  | "too_large";

export type BundleError = {
  code: BundleErrorCode;
  /** 触发拒绝的条目路径；整包级拒绝没有这一项。 */
  path?: string;
  /** 触发拒绝的实际值（条目数 / 字节数）。 */
  actual?: number;
  /** 对应的上限。 */
  limit?: number;
  /** `unsupported_compression` 的 zip 压缩方法号。 */
  method?: number;
};

export type ZipBundleResult =
  | { ok: true; files: BundleFile[]; totalBytes: number }
  | { ok: false; error: BundleError };

/** 解压护栏参数。`uploadMaxBytes` 来自 `/system/config` 的 `source_upload_max_bytes`；
 *  非正数/非整数按「拿不到上限」处理，不做本地预检（见 `resolveLimit`）。 */
export type BundleCaps = { uploadMaxBytes: number };

function fail(error: BundleError): { ok: false; error: BundleError } {
  return { ok: false, error };
}

/** 部署上限归一：缺失 / 非整数 / 非正数一律解成 `null` ＝「不做本地预检」。
 *
 *  `parseZipBundle` 与 `inlineMdImages` **必须**共用这一份，因为它们此前对同一个
 *  `0` 给出了相反的答案：前者算出「预算 0」把任何包都拒掉，后者算成「不检查」全
 *  放行。同一个缺省值一边全拒一边全放不是保守，是矛盾——只要有一处会误拒，用户就
 *  会遇到一个没有任何合法操作能绕开的死路。方向统一取**放行**：本地预检本就只是
 *  提前告知，真正的权威是服务端携带当前上限的流式 413，与既有「省略即关闭预检」
 *  的口径一致。
 */
export function resolveLimit(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : null;
}

/** 解压总量预算；`null` 表示没有可用上限，不做本地预检。 */
function totalBytesLimit(caps: BundleCaps): number | null {
  const base = resolveLimit(caps.uploadMaxBytes);
  return base === null ? null : base * MD_BUNDLE_TOTAL_BYTES_FACTOR;
}

function viewOf(bytes: Uint8Array): DataView {
  return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
}

/** 从尾部回扫定位 EOCD（zip 尾部可以跟一段最长 64KiB 的注释）。 */
function findEocd(bytes: Uint8Array): number {
  if (bytes.byteLength < EOCD_FIXED_SIZE) return -1;
  const view = viewOf(bytes);
  const last = bytes.byteLength - EOCD_FIXED_SIZE;
  const floor = Math.max(0, last - ZIP_MAX_COMMENT_BYTES);
  for (let at = last; at >= floor; at -= 1) {
    if (view.getUint32(at, true) !== EOCD_SIGNATURE) continue;
    const commentLength = view.getUint16(at + 20, true);
    if (at + EOCD_FIXED_SIZE + commentLength === bytes.byteLength) return at;
  }
  return -1;
}

const utf8Decoder = new TextDecoder("utf-8");

/** zip 条目名归一：`\` → `/`，去掉前导 `/` 与 `./`，就地折叠 `.` 与 `..`。
 *
 *  返回 `null` 表示路径逃出了包根——虚拟文件集里没有「包外」这个位置，继续沿用它
 *  会让配对逻辑面对一个没有意义的键，所以按硬失败上报而不是静默丢条目。
 */
function normalizeEntryPath(raw: string): string | null {
  const segments: string[] = [];
  for (const part of raw.replaceAll("\\", "/").split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      if (segments.length === 0) return null;
      segments.pop();
      continue;
    }
    segments.push(part);
  }
  return segments.join("/");
}

/** `inflateRaw` 的「流本身解不开」哨兵，必须与「超预算」的 `null` 可区分。
 *
 *  两者是完全不同的两件事：超预算是**用户能理解并能自己解决**的体积问题（拆包再传），
 *  解不开是**包坏了**。合成同一个返回值就会把一个损坏的 zip 报成「压缩包过大」，
 *  用户照着提示去拆包，怎么拆都还是失败。
 */
const CORRUPT_STREAM = Symbol("md-bundle:corrupt-deflate-stream");

/** `deflate-raw` 解压，边解边计预算。
 *
 *  三种结局：解出的字节 / `null`（超出剩余预算，已取消读取）/ `CORRUPT_STREAM`
 *  （流本身非法）。
 */
async function inflateRaw(
  data: Uint8Array,
  budget: number,
): Promise<Uint8Array | null | typeof CORRUPT_STREAM> {
  const input = new ReadableStream<BufferSource>({
    start(controller) {
      // 裸 `Uint8Array` 参数化成 `ArrayBufferLike`(含 SharedArrayBuffer),而
      // `BufferSource` 只收 ArrayBuffer 支撑的视图。这里的字节一律来自 File /
      // ArrayBuffer 读取,不可能是共享内存,所以就近收窄一次,而不是为了让类型对上
      // 把每个条目再复制一整份。
      controller.enqueue(data as Uint8Array<ArrayBuffer>);
      controller.close();
    },
  });
  const reader = input.pipeThrough(new DecompressionStream("deflate-raw")).getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  let overBudget = false;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      total += value.byteLength;
      if (total > budget) {
        overBudget = true;
        break;
      }
      chunks.push(value);
    }
  } catch {
    // 坏 deflate 流让 `reader.read()` reject。不接住它，这条 rejection 就直接穿过
    // `parseZipBundle` 的 Promise 冒出去：调用方拿不到任何结构化错误码，浏览器里
    // 还会变成一条 unhandled rejection。损坏的包必须走和其它拒绝面同一条回执路径。
    await reader.cancel().catch(() => {});
    return CORRUPT_STREAM;
  }
  if (overBudget) {
    // 取消放在 try 之外：预算耗尽是正常控制流，不能因为 cancel 抖一下就被
    // 上面的 catch 改判成「包坏了」。
    await reader.cancel().catch(() => {});
    return null;
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const chunk of chunks) {
    out.set(chunk, at);
    at += chunk.byteLength;
  }
  return out;
}

/** central directory 扫描阶段留下的条目描述（还没解压）。 */
type CentralEntry = {
  path: string;
  flags: number;
  method: number;
  compressedSize: number;
  uncompressedSize: number;
  localOffset: number;
};

/** 解析 zip 字节为虚拟文件集：EOCD 定位 + central directory 遍历 + 逐条目解压。
 *
 *  刻意分两趟：先扫完整个 central directory（只读头部与文件名，不碰任何压缩数据），
 *  在这一趟里做路径归一、丢弃目录/`__MACOSX`/同名重复项，并对**保留下来**的条目数
 *  执行 `MD_BUNDLE_MAX_ENTRIES`；第二趟才逐条目解压。合成一趟会同时坏掉两件事：
 *  条目闸会退化成「先解压前 N 个再说超限」，而按 EOCD 声明数预拦又会把 macOS 的
 *  资源叉条目算进产品上限。
 *
 *  拒绝面（都是明确错误，不静默丢条目）：加密条目、zip64、非 store/deflate 压缩方法、
 *  逃出包根的路径、条目数超限、解压总量超限、损坏的压缩流。嵌套 zip **不递归**——
 *  `.zip` 条目按普通文件收进文件集（魔数嗅探永远不会把它当图片配上），不再往里解一层。
 */
export async function parseZipBundle(
  bytes: Uint8Array,
  caps: BundleCaps,
): Promise<ZipBundleResult> {
  const eocd = findEocd(bytes);
  if (eocd < 0) return fail({ code: "not_a_zip" });
  const view = viewOf(bytes);

  if (
    eocd >= EOCD64_LOCATOR_SIZE
    && view.getUint32(eocd - EOCD64_LOCATOR_SIZE, true) === EOCD64_LOCATOR_SIGNATURE
  ) {
    return fail({ code: "zip64" });
  }

  const declaredEntries = view.getUint16(eocd + 10, true);
  const centralSize = view.getUint32(eocd + 12, true);
  const centralOffset = view.getUint32(eocd + 16, true);
  if (
    declaredEntries === ZIP64_SENTINEL_16
    || centralSize === ZIP64_SENTINEL_32
    || centralOffset === ZIP64_SENTINEL_32
  ) {
    return fail({ code: "zip64" });
  }
  // 只是给扫描本身封顶，不是产品上限——真正的闸在下面按保留条目数执行。
  if (declaredEntries > MD_BUNDLE_MAX_DECLARED_ENTRIES) {
    return fail({
      code: "too_many_entries",
      actual: declaredEntries,
      limit: MD_BUNDLE_MAX_DECLARED_ENTRIES,
    });
  }
  if (centralOffset + centralSize > bytes.byteLength) return fail({ code: "corrupt" });

  // ---- 第一趟：扫 central directory，只看头部与文件名 ----
  const kept: CentralEntry[] = [];
  const seen = new Set<string>();
  let cursor = centralOffset;

  for (let index = 0; index < declaredEntries; index += 1) {
    if (cursor + CENTRAL_HEADER_FIXED_SIZE > bytes.byteLength) return fail({ code: "corrupt" });
    if (view.getUint32(cursor, true) !== CENTRAL_HEADER_SIGNATURE) return fail({ code: "corrupt" });

    const flags = view.getUint16(cursor + 8, true);
    const method = view.getUint16(cursor + 10, true);
    const compressedSize = view.getUint32(cursor + 20, true);
    const uncompressedSize = view.getUint32(cursor + 24, true);
    const nameLength = view.getUint16(cursor + 28, true);
    const extraLength = view.getUint16(cursor + 30, true);
    const commentLength = view.getUint16(cursor + 32, true);
    const localOffset = view.getUint32(cursor + 42, true);
    const nameStart = cursor + CENTRAL_HEADER_FIXED_SIZE;
    if (nameStart + nameLength > bytes.byteLength) return fail({ code: "corrupt" });
    // 通用位 11 声明「名字是 UTF-8」，未声明的条目官方是 CP437。这里两种都用同一个
    // 非 fatal 的 UTF-8 解码器,所以不需要分支:声明了的逐字正确;没声明的要么本来就是
    // ASCII(CP437 与 UTF-8 在 ASCII 段重合,绝大多数导出包如此),要么坏字节落成 U+FFFD
    // 而配对失败,由「未找到 + 近似候选」如实报出——比拖一张 CP437 码表进来划算。
    const rawName = utf8Decoder.decode(bytes.subarray(nameStart, nameStart + nameLength));
    cursor = nameStart + nameLength + extraLength + commentLength;

    const isDirectory = rawName.endsWith("/") || rawName.endsWith("\\");
    const normalized = normalizeEntryPath(rawName);
    if (normalized === null) return fail({ code: "unsafe_entry_path", path: rawName });
    if (isDirectory || normalized === "") continue;
    if (normalized === APPLE_DOUBLE_ROOT || normalized.startsWith(`${APPLE_DOUBLE_ROOT}/`)) {
      continue;
    }
    // 同名条目保留先出现的那条：文件集、它的索引与 `totalBytes` 必须是同一个口径，
    // 否则「列出来的路径」「配对时取到的字节」「报出去的总量」会来自不同的条目。
    // 丢在这里而不是解压之后，重复条目连解压都不必付。
    if (seen.has(normalized)) continue;
    seen.add(normalized);

    kept.push({ path: normalized, flags, method, compressedSize, uncompressedSize, localOffset });
    if (kept.length > MD_BUNDLE_MAX_ENTRIES) {
      return fail({
        code: "too_many_entries",
        actual: kept.length,
        limit: MD_BUNDLE_MAX_ENTRIES,
      });
    }
  }

  // ---- 第二趟：逐条目解压 ----
  const budgetTotal = totalBytesLimit(caps);
  const files: BundleFile[] = [];
  let consumed = 0;

  for (const entry of kept) {
    const { path, flags, method, compressedSize, uncompressedSize, localOffset } = entry;

    if ((flags & GP_FLAG_ENCRYPTED) !== 0 || (flags & GP_FLAG_STRONG_ENCRYPTION) !== 0) {
      return fail({ code: "encrypted", path });
    }
    if (
      compressedSize === ZIP64_SENTINEL_32
      || uncompressedSize === ZIP64_SENTINEL_32
      || localOffset === ZIP64_SENTINEL_32
    ) {
      return fail({ code: "zip64", path });
    }
    if (method !== METHOD_STORE && method !== METHOD_DEFLATE) {
      return fail({ code: "unsupported_compression", path, method });
    }

    if (localOffset + LOCAL_HEADER_FIXED_SIZE > bytes.byteLength) return fail({ code: "corrupt" });
    if (view.getUint32(localOffset, true) !== LOCAL_HEADER_SIGNATURE) return fail({ code: "corrupt" });
    // 数据起点只能用 local header 自己的名字/扩展长度算：它与 central directory 的
    // 扩展字段长度经常不同。
    const localNameLength = view.getUint16(localOffset + 26, true);
    const localExtraLength = view.getUint16(localOffset + 28, true);
    const dataStart = localOffset + LOCAL_HEADER_FIXED_SIZE + localNameLength + localExtraLength;
    if (dataStart + compressedSize > bytes.byteLength) return fail({ code: "corrupt" });

    const remaining = budgetTotal === null
      ? Number.POSITIVE_INFINITY
      : budgetTotal - consumed;
    let content: Uint8Array;
    if (method === METHOD_STORE) {
      if (compressedSize > remaining) {
        return fail({
          code: "too_large",
          path,
          actual: consumed + compressedSize,
          limit: budgetTotal ?? undefined,
        });
      }
      content = bytes.slice(dataStart, dataStart + compressedSize);
    } else {
      const inflated = await inflateRaw(
        bytes.subarray(dataStart, dataStart + compressedSize),
        remaining,
      );
      if (inflated === CORRUPT_STREAM) return fail({ code: "corrupt", path });
      if (inflated === null) {
        return fail({ code: "too_large", path, limit: budgetTotal ?? undefined });
      }
      content = inflated;
    }
    consumed += content.byteLength;
    files.push({ path, bytes: content });
  }

  return { ok: true, files, totalBytes: consumed };
}

// ---------------------------------------------------------------------------
// md 发现
// ---------------------------------------------------------------------------

function extensionOf(path: string): string {
  const name = path.slice(path.lastIndexOf("/") + 1);
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** 文件集里的全部 markdown 条目，保持原始顺序。
 *
 *  「零个 / 恰好一个 / 多个」三态由调用方处理（零个报错、一个直接用、多个让用户勾选）。
 */
export function markdownFiles(files: readonly BundleFile[]): BundleFile[] {
  return files.filter((file) => MD_BUNDLE_MARKDOWN_EXTENSIONS.includes(extensionOf(file.path)));
}

/** 把 markdown 字节解成文本。
 *
 *  BOM 无需手工剥：`TextDecoder("utf-8")` 默认 `ignoreBOM: false`，已经吞掉首个
 *  U+FEFF。**别改成 `new TextDecoder("utf-8", { ignoreBOM: true })`**——那个选项
 *  的名字读起来像「忽略 BOM」，实际语义是「不把它当 BOM，原样留在文本里」，换过去
 *  会让首个标题带一个不可见字符，而且不会报任何错。
 */
export function decodeMarkdownText(bytes: Uint8Array): string {
  return utf8Decoder.decode(bytes);
}

// ---------------------------------------------------------------------------
// 魔数嗅探
// ---------------------------------------------------------------------------

/** 服务端 `ALLOWED_MIME_EXTENSIONS` 白名单里的四种；SVG 因存储型 XSS 刻意排除。 */
export type ImageMime = "image/png" | "image/jpeg" | "image/gif" | "image/webp";

function startsWith(bytes: Uint8Array, prefix: readonly number[], at = 0): boolean {
  if (bytes.byteLength < at + prefix.length) return false;
  for (let i = 0; i < prefix.length; i += 1) {
    if (bytes[at + i] !== prefix[i]) return false;
  }
  return true;
}

const PNG_MAGIC = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const JPEG_MAGIC = [0xff, 0xd8, 0xff];
const GIF87A_MAGIC = [0x47, 0x49, 0x46, 0x38, 0x37, 0x61];
const GIF89A_MAGIC = [0x47, 0x49, 0x46, 0x38, 0x39, 0x61];
const RIFF_MAGIC = [0x52, 0x49, 0x46, 0x46];
const WEBP_MAGIC = [0x57, 0x45, 0x42, 0x50];
const WEBP_FORM_OFFSET = 8;

/** 按**魔数**判 mime，绝不看后缀——包里的 `logo.png` 完全可能是别的格式，而后缀
 *  判断会把一份服务端注定拒收的字节内联进 md、白白撑爆体积上限。 */
export function sniffImageMime(bytes: Uint8Array): ImageMime | null {
  if (startsWith(bytes, PNG_MAGIC)) return "image/png";
  if (startsWith(bytes, JPEG_MAGIC)) return "image/jpeg";
  if (startsWith(bytes, GIF87A_MAGIC) || startsWith(bytes, GIF89A_MAGIC)) return "image/gif";
  if (startsWith(bytes, RIFF_MAGIC) && startsWith(bytes, WEBP_MAGIC, WEBP_FORM_OFFSET)) {
    return "image/webp";
  }
  return null;
}

// ---------------------------------------------------------------------------
// base64
// ---------------------------------------------------------------------------

/** 分块 base64：每块长度是 3 的倍数，逐块 btoa 后拼接等于整体编码。
 *
 *  刻意不用 `Buffer`（浏览器里没有），也不把整个数组一次性展开进
 *  `String.fromCharCode`（几 MB 的图片会直接爆栈）。
 */
export function bytesToBase64(bytes: Uint8Array): string {
  let out = "";
  for (let at = 0; at < bytes.byteLength; at += BASE64_CHUNK_BYTES) {
    const chunk = bytes.subarray(at, Math.min(at + BASE64_CHUNK_BYTES, bytes.byteLength));
    let latin1 = "";
    for (let i = 0; i < chunk.length; i += 1) latin1 += String.fromCharCode(chunk[i]);
    out += btoa(latin1);
  }
  return out;
}

/** 不分配中间数组地数出一段文本的 UTF-8 字节数。
 *
 *  内联后的 md 可能有几十 MB 且几乎全是 ASCII，`TextEncoder.encode().length` 会为了
 *  一个长度再复制一整份出来。
 */
export function utf8ByteLength(value: string): number {
  let bytes = 0;
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code < 0x80) {
      bytes += 1;
    } else if (code < 0x800) {
      bytes += 2;
    } else if (code >= 0xd800 && code <= 0xdbff && i + 1 < value.length) {
      const next = value.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        bytes += 4;
        i += 1;
      } else {
        bytes += 3;
      }
    } else {
      bytes += 3;
    }
  }
  return bytes;
}

// ---------------------------------------------------------------------------
// markdown 图片语法
// ---------------------------------------------------------------------------

/** 图片的书写形态。只有 `inline` 能被改写；另两种只进回执，正文一个字节都不动。 */
export type MarkdownImageSyntax =
  /** 行内式 `![alt](dest)`——唯一可改写的形态。 */
  | "inline"
  /** 引用式 `![alt][ref]` / `![alt][]`：目标写在文末的链接定义里。 */
  | "reference"
  /** 原生 `<img src=...>` 标签。 */
  | "html";

export type MarkdownImageRef = {
  /** md 里写的原始链接（未做百分号解码）。引用式取方括号里的标签，`<img>` 取 `src`。 */
  src: string;
  alt: string;
  syntax: MarkdownImageSyntax;
  /** 整个 `![...](...)` 在原文里的跨度。 */
  start: number;
  end: number;
  /** 链接目标在原文里的跨度——改写只替换这一段，其余字节逐字保留。
   *  非 `inline` 形态不可改写，这两个值等于 `start`/`end`，不得用于替换。 */
  destStart: number;
  destEnd: number;
  /** 是否独占一整段（服务端唯一会落成图片元素的形态）。 */
  standalone: boolean;
  /** 0 起的行号，供 UI 定位。 */
  line: number;
};

type LineInfo = { start: number; end: number; blank: boolean; fenced: boolean };

function scanLines(md: string): LineInfo[] {
  const lines: LineInfo[] = [];
  let at = 0;
  while (at <= md.length) {
    let end = md.indexOf("\n", at);
    if (end < 0) end = md.length;
    let contentEnd = end;
    if (contentEnd > at && md.charCodeAt(contentEnd - 1) === 0x0d) contentEnd -= 1;
    const raw = md.slice(at, contentEnd);
    lines.push({ start: at, end: contentEnd, blank: raw.trim() === "", fenced: false });
    if (end === md.length) break;
    at = end + 1;
  }
  // 围栏代码块：其中的 `![](...)` 是代码示例的字面文本，改写它会把一大坨 base64
  // 塞进代码块。开栏与闭栏必须同字符、闭栏不短于开栏（CommonMark）。
  let fenceChar = "";
  let fenceLength = 0;
  for (const line of lines) {
    const raw = md.slice(line.start, line.end);
    const trimmed = raw.trimStart();
    const indent = indentWidth(raw);
    const char = trimmed.charAt(0);
    let run = 0;
    if ((char === "`" || char === "~") && indent < INDENTED_CODE_SPACES) {
      while (trimmed.charAt(run) === char) run += 1;
    }
    if (fenceChar === "") {
      if (run >= MIN_FENCE_LENGTH) {
        fenceChar = char;
        fenceLength = run;
        line.fenced = true;
      }
      continue;
    }
    line.fenced = true;
    if (char === fenceChar && run >= fenceLength && trimmed.slice(run).trim() === "") {
      fenceChar = "";
      fenceLength = 0;
    }
  }
  return lines;
}

function indentWidth(raw: string): number {
  let width = 0;
  for (let i = 0; i < raw.length; i += 1) {
    const ch = raw.charAt(i);
    if (ch === " ") width += 1;
    else if (ch === "\t") width += TAB_WIDTH - (width % TAB_WIDTH);
    else break;
    if (width >= INDENTED_CODE_SPACES) return width;
  }
  return width;
}

/** ATX 标题行：`# ` 到 `###### `，最多 3 空格缩进，`#` 串后必须是空白或行尾。 */
const ATX_HEADING_LINE = /^ {0,3}#{1,6}(?:[ \t].*)?$/;

/** 主题分隔线：`***` / `---` / `___`，三个及以上、字符之间可有空白。 */
const THEMATIC_BREAK_LINE = /^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/;

/** setext 标题下划线：纯 `=` 串或纯 `-` 串的一行。 */
const SETEXT_UNDERLINE_LINE = /^ {0,3}(?:=+|-+)[ \t]*$/;

/** 上一行是否结束了上一个块，使得图片行能起一个新段落。
 *
 *  空行以外，ATX 标题与主题分隔线同样是块边界——服务端实测 `# 标题\n![a](x.png)`
 *  产出 `[heading, image]`，图片确实独占一段。此前只认空行，这类紧凑写法（标题下
 *  面直接贴图，导出工具很常见）会被整片判成不可改写。
 */
function closesBlockAbove(raw: string): boolean {
  return ATX_HEADING_LINE.test(raw) || THEMATIC_BREAK_LINE.test(raw);
}

/** 下一行是否开启了新块，使得图片行仍然独占自己那一段。
 *
 *  与上一行**不对称**，这是踩出来的：`---` / `----` / `===` 紧跟在图片行下面时是
 *  **setext 标题下划线**而不是主题分隔线，会把图片行整个吸成一个标题——服务端实测
 *  `![a](x.png)\n---` 产出 `[paragraph, heading]`，一个 image 块都没有。setext 优先
 *  于主题分隔线，所以这里必须显式排除它。带空格的 `- - -` 不是合法的 setext 下划线，
 *  仍按主题分隔线放行（实测产出 image 块）。
 */
function opensBlockBelow(raw: string): boolean {
  if (ATX_HEADING_LINE.test(raw)) return true;
  return THEMATIC_BREAK_LINE.test(raw) && !SETEXT_UNDERLINE_LINE.test(raw);
}

/** 一行上的行内代码跨度（绝对偏移，左闭右开）。
 *
 *  CommonMark 的代码跨度按「等长反引号串配对」：`` `x` ``、``` ``a`b`` ``` 都算。
 *  跨行的代码跨度刻意不处理——按行判已经覆盖了实际写法，而跨行状态机会把整份文档的
 *  反引号耦合起来，一个落单的反引号就能让后面所有图片消失。
 */
function codeSpansOnLine(raw: string, lineStart: number): Array<[number, number]> {
  const spans: Array<[number, number]> = [];
  let i = 0;
  while (i < raw.length) {
    if (raw.charAt(i) !== "`") {
      i += 1;
      continue;
    }
    let run = 0;
    while (raw.charAt(i + run) === "`") run += 1;
    let j = i + run;
    let closeStart = -1;
    while (j < raw.length) {
      if (raw.charAt(j) !== "`") {
        j += 1;
        continue;
      }
      let closeRun = 0;
      while (raw.charAt(j + closeRun) === "`") closeRun += 1;
      if (closeRun === run) {
        closeStart = j;
        break;
      }
      j += closeRun;
    }
    if (closeStart < 0) {
      i += run;
      continue;
    }
    spans.push([lineStart + i, lineStart + closeStart + run]);
    i = closeStart + run;
  }
  return spans;
}

/** 从 `!` 处尝试解析一个完整的 `![alt](dest "title")`。
 *
 *  手写扫描而不是正则：目标里可以有成对括号（`![a](img(1).png)`）、可以用尖括号包裹
 *  （`![a](<my file.png>)`）、后面可以跟标题——正则要么漏掉这些形态，要么在长 base64
 *  上回溯到不可接受。
 */
function parseImageAt(md: string, at: number): MarkdownImageRef | null {
  if (md.charAt(at) !== "!" || md.charAt(at + 1) !== "[") return null;
  let i = at + 2;
  let depth = 1;
  const altStart = i;
  while (i < md.length) {
    const ch = md.charAt(i);
    if (ch === "\\") {
      i += 2;
      continue;
    }
    if (ch === "[") depth += 1;
    else if (ch === "]") {
      depth -= 1;
      if (depth === 0) break;
    }
    i += 1;
  }
  if (depth !== 0) return null;
  const alt = md.slice(altStart, i);
  i += 1;
  if (md.charAt(i) === "[") {
    // 引用式 `![alt][ref]` / `![alt][]`：目标在文末的链接定义里，本模块不解析定义
    // 表，所以只如实登记、不改写。**只认后面紧跟 `[` 的这两种**：省略括号的简写式
    // `![alt]` 与普通的字面文本 `![不是链接]` 在没有定义表时长得一模一样，把它也算
    // 进来会给一堆纯文本编出「未支持的图片」回执。
    let j = i + 1;
    let labelDepth = 1;
    while (j < md.length) {
      const ch = md.charAt(j);
      if (ch === "\\") {
        j += 2;
        continue;
      }
      if (ch === "[") labelDepth += 1;
      else if (ch === "]") {
        labelDepth -= 1;
        if (labelDepth === 0) break;
      }
      j += 1;
    }
    if (labelDepth !== 0) return null;
    return {
      src: md.slice(i + 1, j),
      alt,
      syntax: "reference",
      start: at,
      end: j + 1,
      destStart: at,
      destEnd: j + 1,
      standalone: false,
      line: 0,
    };
  }
  if (md.charAt(i) !== "(") return null;
  i += 1;
  while (i < md.length && /\s/.test(md.charAt(i))) i += 1;

  let destStart = i;
  let destEnd = i;
  if (md.charAt(i) === "<") {
    i += 1;
    destStart = i;
    while (i < md.length && md.charAt(i) !== ">" && md.charAt(i) !== "\n") {
      if (md.charAt(i) === "\\") i += 1;
      i += 1;
    }
    if (md.charAt(i) !== ">") return null;
    destEnd = i;
    i += 1;
  } else {
    let parens = 0;
    while (i < md.length) {
      const ch = md.charAt(i);
      if (/\s/.test(ch)) break;
      if (ch === "\\") {
        i += 2;
        continue;
      }
      if (ch === "(") parens += 1;
      else if (ch === ")") {
        if (parens === 0) break;
        parens -= 1;
      }
      i += 1;
    }
    destEnd = i;
  }

  while (i < md.length && /\s/.test(md.charAt(i))) i += 1;
  const titleChar = md.charAt(i);
  if (titleChar === '"' || titleChar === "'" || titleChar === "(") {
    const closing = titleChar === "(" ? ")" : titleChar;
    i += 1;
    while (i < md.length && md.charAt(i) !== closing) {
      if (md.charAt(i) === "\\") i += 1;
      i += 1;
    }
    if (md.charAt(i) !== closing) return null;
    i += 1;
    while (i < md.length && /\s/.test(md.charAt(i))) i += 1;
  }
  if (md.charAt(i) !== ")") return null;

  return {
    src: md.slice(destStart, destEnd),
    alt,
    syntax: "inline",
    start: at,
    end: i + 1,
    destStart,
    destEnd,
    standalone: false,
    line: 0,
  };
}

/** 原生 `<img ...>` 标签。属性里不可能出现 `>`（否则标签早就闭合了），所以 `[^>]*`
 *  既够用又是线性的，不会在长 base64 上回溯。 */
const HTML_IMG_TAG = /<img\b[^>]*>/gi;

/** 从一个 `<img ...>` 标签里取 `src`（双引号 / 单引号 / 裸值三种写法）。 */
const HTML_IMG_SRC = /\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/i;

/** 扫出全部 `<img>` 标签，按出现位置升序。 */
function findHtmlImgTags(md: string): MarkdownImageRef[] {
  const out: MarkdownImageRef[] = [];
  HTML_IMG_TAG.lastIndex = 0;
  for (;;) {
    const match = HTML_IMG_TAG.exec(md);
    if (match === null) break;
    const src = HTML_IMG_SRC.exec(match[0]);
    out.push({
      src: src === null ? "" : (src[1] ?? src[2] ?? src[3] ?? ""),
      alt: "",
      syntax: "html",
      start: match.index,
      end: match.index + match[0].length,
      destStart: match.index,
      destEnd: match.index + match[0].length,
      standalone: false,
      line: 0,
    });
  }
  return out;
}

/** 列出 md 里的全部图片引用（三种语法形态），按文档顺序，并逐条判定是否「独占一整段」。
 *
 *  独占段的判据镜像服务端 `structural_markdown.parse_blocks`：只有「整段的行内内容
 *  恰好是一个 image、没有任何非空白文字」的段落才会被发成 image 元素。落到行上就是
 *  ——图片跨度正好等于所在行去掉首尾空白后的全部内容、**零缩进**、上一行结束了上一个
 *  块、下一行开启了新块。
 *
 *  两处判据比「上下都是空行、缩进 < 4」更精确，都是对着服务端实测定的：
 *
 *  - **零缩进**（而不是「缩进 < 4」）：松散列表项的续段缩进 1–3 空格（`1. a` 的续段
 *    缩进 3、`- a` 的缩进 2），服务端会把它并进 `list_item` 块、图片只剩 alt 文本。
 *    按「< 4」判就会把它们当成可改写的独占段，把整张图的 base64 内联进一个注定只保留
 *    文字的位置——白白撑爆上传体积上限，而用户看不到任何解释。代价是缩进 1–3 又**不在**
 *    列表里的图片（服务端确实会发成 image 块）被判成不可改写：这是登记接受的假阴，
 *    方向上安全（原样保留链接 + 一条如实回执），且这种写法本身罕见。
 *  - **相邻行**：空行/文件边界之外，ATX 标题与主题分隔线同样是块边界。唯一的坑是
 *    `---`/`===` 跟在图片行**下面**时是 setext 下划线而非分隔线，见 `opensBlockBelow`。
 *
 *  围栏代码块内、以及行内代码跨度内的图片语法整条不列出——那是代码示例，不是引用。
 */
export function findMarkdownImages(md: string): MarkdownImageRef[] {
  const lines = scanLines(md);
  const htmlTags = findHtmlImgTags(md);
  const refs: MarkdownImageRef[] = [];
  let lineIndex = 0;
  let tagIndex = 0;
  let at = 0;
  for (;;) {
    const bang = md.indexOf("![", at);
    while (tagIndex < htmlTags.length && htmlTags[tagIndex].end <= at) tagIndex += 1;
    const tag = tagIndex < htmlTags.length ? htmlTags[tagIndex] : null;
    if (bang < 0 && tag === null) break;

    let parsed: MarkdownImageRef | null;
    if (tag !== null && (bang < 0 || tag.start < bang)) {
      parsed = tag;
      at = tag.end;
      tagIndex += 1;
    } else {
      parsed = parseImageAt(md, bang);
      if (parsed === null) {
        at = bang + 2;
        continue;
      }
      at = parsed.end;
    }

    // 显式收窄成 const，别指望 `let` 的控制流收窄能活到下面那个回调里。
    const ref = parsed;
    while (lineIndex + 1 < lines.length && lines[lineIndex + 1].start <= ref.start) {
      lineIndex += 1;
    }
    const line = lines[lineIndex];
    if (line.fenced) continue;
    const raw = md.slice(line.start, line.end);
    // 行内代码里的图片语法是被展示的字面文本，不是引用——既不改写，也不该在回执里
    // 编出一条「这张图没内联」。判据必须是「整条不列出」而不是 standalone 的一个
    // 条件：后者会照常产出一条 `inline_position` 回执，凭空多出一张“没配上的图”。
    const inCode = codeSpansOnLine(raw, line.start)
      .some(([from, to]) => ref.start >= from && ref.end <= to);
    if (inCode) continue;
    ref.line = lineIndex;
    const leading = raw.length - raw.trimStart().length;
    const trailing = raw.length - raw.trimEnd().length;
    const contentStart = line.start + leading;
    const contentEnd = line.end - trailing;
    const above = lineIndex === 0 ? null : lines[lineIndex - 1];
    const below = lineIndex === lines.length - 1 ? null : lines[lineIndex + 1];
    ref.standalone = ref.syntax === "inline"
      && ref.start === contentStart
      && ref.end === contentEnd
      && indentWidth(raw) === 0
      && (above === null || above.blank || closesBlockAbove(md.slice(above.start, above.end)))
      && (below === null || below.blank || opensBlockBelow(md.slice(below.start, below.end)));
    refs.push(ref);
  }
  return refs;
}

// ---------------------------------------------------------------------------
// 路径配对
// ---------------------------------------------------------------------------

function directoryOf(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut < 0 ? "" : path.slice(0, cut);
}

function baseNameOf(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

/** 剥掉链接尾部的 `?query` / `#fragment`。
 *
 *  `x.png?v=2` 这种缓存戳在导出的 md 里很常见，磁盘上的文件就叫 `x.png`；不剥就永远
 *  配不上，还会给用户一条查无此文件的回执。**必须在百分号解码之前做**：`%23` 正是
 *  「文件名里真的有一个 `#`」的写法，先解码再剥会把它从中间斩断。
 */
function stripLinkSuffix(ref: string): string {
  const hash = ref.indexOf("#");
  const query = ref.indexOf("?");
  const cut = hash < 0 ? query : query < 0 ? hash : Math.min(hash, query);
  return cut < 0 ? ref : ref.slice(0, cut);
}

/** 把已经剥过尾缀的链接解析成包内路径。返回 `null` 表示逃出了包根。 */
function resolveBundlePath(mdPath: string, ref: string): string | null {
  const cleaned = ref.replaceAll("\\", "/");
  const base = cleaned.startsWith("/") ? "" : directoryOf(mdPath);
  return normalizeEntryPath(base === "" ? cleaned : `${base}/${cleaned}`);
}

/** 把 md 里的相对链接解析成包内路径（先剥 `?query`/`#fragment`）。
 *  返回 `null` 表示逃出了包根。 */
export function resolveBundleRef(mdPath: string, ref: string): string | null {
  return resolveBundlePath(mdPath, stripLinkSuffix(ref));
}

function decodePercent(value: string): string | null {
  if (!value.includes("%")) return null;
  try {
    const decoded = decodeURIComponent(value);
    return decoded === value ? null : decoded;
  } catch {
    return null;
  }
}

type BundleIndex = { byPath: Map<string, BundleFile>; lowerPath: Map<string, string[]>; lowerName: Map<string, string[]> };

function buildIndex(files: readonly BundleFile[]): BundleIndex {
  const byPath = new Map<string, BundleFile>();
  const lowerPath = new Map<string, string[]>();
  const lowerName = new Map<string, string[]>();
  for (const file of files) {
    if (byPath.has(file.path)) continue; // 同名先到先得，与 parseZipBundle 同口径
    byPath.set(file.path, file);
    push(lowerPath, file.path.toLowerCase(), file.path);
    push(lowerName, baseNameOf(file.path).toLowerCase(), file.path);
  }
  return { byPath, lowerPath, lowerName };
}

function push(map: Map<string, string[]>, key: string, value: string): void {
  const existing = map.get(key);
  if (existing) existing.push(value);
  else map.set(key, [value]);
}

/** 精确匹配失败时的近似候选：先看大小写不敏感的整路径命中，再看同名文件。
 *
 *  「不静默丢」的落点——找不到就把最像的几个路径摆给用户，让他自己判断是路径写错了
 *  还是图片没打进包。
 */
function suggestFor(index: BundleIndex, resolved: string | null): string[] {
  if (resolved === null) return [];
  const out: string[] = [];
  for (const path of index.lowerPath.get(resolved.toLowerCase()) ?? []) {
    if (!out.includes(path)) out.push(path);
  }
  for (const path of index.lowerName.get(baseNameOf(resolved).toLowerCase()) ?? []) {
    if (!out.includes(path)) out.push(path);
  }
  return out.slice(0, MD_BUNDLE_MAX_SUGGESTIONS);
}

/** 按包内路径找文件：先试百分号解码后的形态，再试原样。
 *
 *  两种都试是因为两种都真实存在——Notion 导出把空格写成 `%20`，而手写的 md 里也可能
 *  真有个名字带 `%20` 的文件。解码后的形态优先：`%20` 在链接里的**规范**含义就是空格，
 *  两个候选同时存在于包里时按规范解读取 `my shot.png`。
 *
 *  尾缀在这里剥一次就够，候选走的是不再剥的 `resolveBundlePath`——否则 `%23` 解码出
 *  的字面 `#` 会在第二轮被当成 fragment 斩掉。
 */
function lookup(
  index: BundleIndex,
  mdPath: string,
  src: string,
): { file: BundleFile; path: string } | { file: null; resolved: string | null } {
  const bare = stripLinkSuffix(src);
  const candidates = [decodePercent(bare), bare].filter((value): value is string => value !== null);
  let firstResolved: string | null = null;
  for (const candidate of candidates) {
    const resolved = resolveBundlePath(mdPath, candidate);
    if (firstResolved === null) firstResolved = resolved;
    if (resolved === null) continue;
    const file = index.byPath.get(resolved);
    if (file) return { file, path: resolved };
  }
  return { file: null, resolved: firstResolved };
}

// ---------------------------------------------------------------------------
// 内联改写
// ---------------------------------------------------------------------------

export type UnsupportedReason =
  /** 图片不在独立段落中（列表项、表格单元格、句子中间、引用块、强调包裹……）：
   *  **本次未内联，保留原始链接**。
   *
   *  措辞刻意只陈述本模块做了什么，不替服务端断言它会怎么处理。判据是保守的——只改写
   *  能确定会落成图片元素的形态，落在这一类里的既有服务端真的只保留文字的（列表项、
   *  表格单元格、句子中间），也有服务端其实会当成图片、只是本模块不敢担保的（引用块
   *  `> ![](x)`、强调包裹 `*![](x)*`、尾随行内代码）。旧文案「服务端只会留下文字」对
   *  后一半是事实错误，会让用户按一个不存在的原因去改 md。 */
  | "inline_position"
  /** 引用式 `![alt][ref]` / `![alt][]`：目标写在文末的链接定义里，本模块不解析定义表。 */
  | "reference_syntax"
  /** 原生 `<img src=...>` 标签，不是 markdown 图片语法。 */
  | "html_syntax"
  /** `![alt]()`：链接是空的。 */
  | "empty_src"
  /** 魔数不是 png/jpeg/gif/webp（含 SVG）。 */
  | "unsupported_image_type"
  /** 超过部署的单图字节上限，服务端会跳过它。 */
  | "image_too_large"
  /** 超过部署的每来源图片张数上限。 */
  | "too_many_images";

export type InlinedImage = {
  src: string;
  /** 包内实际配上的文件路径。 */
  path: string;
  mime: ImageMime;
  /** 图片原始字节数。 */
  bytes: number;
  /** 内联后 data URI 的长度（全 ASCII，等于它的 UTF-8 字节数）。 */
  encodedBytes: number;
  alt: string;
  line: number;
};

export type MissingImage = {
  src: string;
  /** 解析后的包内路径；`null` 表示这个链接逃出了包根。 */
  resolved: string | null;
  suggestions: string[];
  line: number;
};

export type UnsupportedImage = {
  src: string;
  reason: UnsupportedReason;
  path?: string;
  bytes?: number;
  limit?: number;
  line: number;
};

export type RemoteImage = { src: string; line: number };
export type NoAltImage = { src: string; path: string; line: number };

export type InlineReceipt = {
  inlined: InlinedImage[];
  missing: MissingImage[];
  unsupported: UnsupportedImage[];
  remote: RemoteImage[];
  /** `inlined` 的子集：内联成功但没有图注——上传后这张图不可被检索。 */
  noAlt: NoAltImage[];
};

/** 三个上限一律经 `resolveLimit` 归一：省略、`null`、`0`、负数、非整数都表示
 *  「拿不到这个上限」→ 不做本地预检，交给服务端护栏。 */
export type InlineOptions = {
  /** `/system/config` 的 `source_upload_max_bytes`：改写后的 md 必须不超过它。 */
  uploadMaxBytes: number;
  /** 部署的单图字节上限。 */
  imageMaxBytes?: number | null;
  /** 部署的每来源图片张数上限。 */
  maxImagesPerSource?: number | null;
};

export type InlineResult =
  | { ok: true; rewritten: string; bytes: number; receipt: InlineReceipt }
  | {
    ok: false;
    receipt: InlineReceipt;
    error: {
      code: "too_large";
      /** 改写后的总字节数。 */
      bytes: number;
      limit: number;
      /** 原始 md 正文的字节数（不含内联图片）。 */
      textBytes: number;
      /** 按体积降序的逐图明细，供 UI 直接列出「哪几张撑爆了」。 */
      images: Array<{ src: string; path: string; encodedBytes: number }>;
    };
  };

/** 判断链接是不是带 scheme 的绝对 URL（`data:` / `http:` / `https:` / …）。 */
const ABSOLUTE_URL = /^[a-z][a-z0-9+.-]*:/i;

/** 协议相对 URL（`//cdn.example.com/a.png`）：浏览器按当前页协议去网络取，与
 *  `https://` 完全同类。它没有 scheme，所以逃得过 `ABSOLUTE_URL`；不单独拦一下就会
 *  被当成包内相对路径去配对，然后报一条「未找到 cdn.example.com/a.png」——把一个
 *  「不拉取远程图片」的产品决定说成了「你的包里少了个文件」。 */
const PROTOCOL_RELATIVE_URL = /^\/\//;

function emptyReceipt(): InlineReceipt {
  return { inlined: [], missing: [], unsupported: [], remote: [], noAlt: [] };
}

/** 把一份 md 里的本地图片链接改写成自包含的 data URI，并给出逐条配对回执。
 *
 *  分类顺序（每条引用最多产出一条回执，按文档顺序）：
 *  1. 围栏代码块 / 行内代码里的图片语法 —— 根本不列出（是被展示的代码，不是引用）。
 *  2. 引用式 `![a][ref]` 与 `<img>` 标签 —— 记 `reference_syntax` / `html_syntax`。
 *     这两种本模块不改写，但必须如实登记：静默跳过会让用户以为图片配上了。
 *  3. `data:` —— 已经是自包含形态，静默跳过。
 *  4. 其它带 scheme 的链接、以及协议相对的 `//host/x.png` —— 记 `remote`。
 *  5. 非独占段的本地图片 —— 记 `unsupported/inline_position`（语义见该枚举值的注释）。
 *  6. 剩下的独占段本地图片走配对 → 魔数 → 单图上限 → 张数上限 → 内联。
 *
 *  语法形态排在 `data:`/remote 之前：一个 `<img src="https://...">` 报「这种写法不支持」
 *  比报「远程图片不拉取」更可操作——改成 markdown 语法它就能工作，而后者暗示改不了。
 *
 *  `maxImagesPerSource` 只由**真正内联成功**的张数消耗：一张超限的大图不该顺带占掉
 *  一个名额（服务端也是跳过它而不是记账）。
 */
export function inlineMdImages(
  mdPath: string,
  mdText: string,
  files: readonly BundleFile[],
  opts: InlineOptions,
): InlineResult {
  const index = buildIndex(files);
  const receipt = emptyReceipt();
  const imageLimit = resolveLimit(opts.imageMaxBytes);
  const countLimit = resolveLimit(opts.maxImagesPerSource);

  const pieces: string[] = [];
  // 同一张图被引用多次时只编码一次：base64 是纯函数，而一张几 MB 的图编一次就要
  // 遍历全部字节并拼出 4/3 倍长的字符串。按包内路径 memo（不是按 src——`./a.png`
  // 与 `a.png` 是同一个文件）。
  const encodedByPath = new Map<string, string>();
  let cursor = 0;
  let inlinedCount = 0;

  for (const ref of findMarkdownImages(mdText)) {
    if (ref.syntax !== "inline") {
      receipt.unsupported.push({
        src: ref.src,
        reason: ref.syntax === "reference" ? "reference_syntax" : "html_syntax",
        line: ref.line,
      });
      continue;
    }
    if (ABSOLUTE_URL.test(ref.src) || PROTOCOL_RELATIVE_URL.test(ref.src)) {
      if (!/^data:/i.test(ref.src)) receipt.remote.push({ src: ref.src, line: ref.line });
      continue;
    }
    if (!ref.standalone) {
      receipt.unsupported.push({ src: ref.src, reason: "inline_position", line: ref.line });
      continue;
    }
    if (ref.src.trim() === "") {
      receipt.unsupported.push({ src: ref.src, reason: "empty_src", line: ref.line });
      continue;
    }

    const hit = lookup(index, mdPath, ref.src);
    if (hit.file === null) {
      receipt.missing.push({
        src: ref.src,
        resolved: hit.resolved,
        suggestions: suggestFor(index, hit.resolved),
        line: ref.line,
      });
      continue;
    }
    const mime = sniffImageMime(hit.file.bytes);
    if (mime === null) {
      receipt.unsupported.push({
        src: ref.src,
        reason: "unsupported_image_type",
        path: hit.path,
        bytes: hit.file.bytes.byteLength,
        line: ref.line,
      });
      continue;
    }
    if (imageLimit !== null && hit.file.bytes.byteLength > imageLimit) {
      receipt.unsupported.push({
        src: ref.src,
        reason: "image_too_large",
        path: hit.path,
        bytes: hit.file.bytes.byteLength,
        limit: imageLimit,
        line: ref.line,
      });
      continue;
    }
    if (countLimit !== null && inlinedCount >= countLimit) {
      receipt.unsupported.push({
        src: ref.src,
        reason: "too_many_images",
        path: hit.path,
        bytes: hit.file.bytes.byteLength,
        limit: countLimit,
        line: ref.line,
      });
      continue;
    }

    let encoded = encodedByPath.get(hit.path);
    if (encoded === undefined) {
      encoded = bytesToBase64(hit.file.bytes);
      encodedByPath.set(hit.path, encoded);
    }
    const dataUri = `data:${mime};base64,${encoded}`;
    // 只替换链接目标那一段，alt / 标题 / 缩进 / 换行全部逐字保留。
    pieces.push(mdText.slice(cursor, ref.destStart), dataUri);
    cursor = ref.destEnd;
    inlinedCount += 1;
    receipt.inlined.push({
      src: ref.src,
      path: hit.path,
      mime,
      bytes: hit.file.bytes.byteLength,
      encodedBytes: dataUri.length,
      alt: ref.alt,
      line: ref.line,
    });
    if (ref.alt.trim() === "") {
      receipt.noAlt.push({ src: ref.src, path: hit.path, line: ref.line });
    }
  }

  pieces.push(mdText.slice(cursor));
  const rewritten = pieces.join("");
  const bytes = utf8ByteLength(rewritten);
  const limit = resolveLimit(opts.uploadMaxBytes);
  if (limit !== null && bytes > limit) {
    return {
      ok: false,
      receipt,
      error: {
        code: "too_large",
        bytes,
        limit,
        textBytes: utf8ByteLength(mdText),
        images: receipt.inlined
          .map((item) => ({ src: item.src, path: item.path, encodedBytes: item.encodedBytes }))
          .sort((a, b) => b.encodedBytes - a.encodedBytes),
      },
    };
  }
  return { ok: true, rewritten, bytes, receipt };
}
