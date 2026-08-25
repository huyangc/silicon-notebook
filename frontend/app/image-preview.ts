/**
 * image-preview.ts
 *
 * 图片放大预览的纯数据面：一次预览打开时**冻结**的图片快照（items）加当前位置
 * （index）。冻结而不是每次渲染重算，是为了让「3 / 5」这个计数在预览期间稳定：
 * 生成中的回答随时可能再插进来一张图，重算会让计数在用户眼皮底下变。
 */

export type AnswerImagePreviewItem = Readonly<{
  assetId: string;
  alt: string;
  referenceLabel: string;
}>;

export type AnswerImagePreviewRequest = Readonly<{
  /** 本次预览可以左右切换的全部图片，按它们在回答正文里的显示顺序。 */
  items: readonly AnswerImagePreviewItem[];
  /** 当前显示的是 `items` 里的第几张。 */
  index: number;
}>;

type GalleryReference = Readonly<{
  displayLabel: string;
  images: readonly Readonly<{ asset_id: string; caption?: string | null }>[];
}>;

type GallerySlot = Readonly<{ citationKey: string; imageId: string }>;

/**
 * 一条回答里可切换的附图清单。
 *
 * 顺序**不在这里推导**：`slots` 就是 `rehypeCitationImages` 本次渲染真正插进正文的
 * 那些条目，按正文顺序、且已按资产去重。早先版本按引用数组顺序自己排一遍——anchor
 * 路径上碰巧一致，但回退到 `citations` 列表时（正文先写 `[2]` 后写 `[1]`，而列表按
 * 检索序给出 `[1]`、`[2]`）左右切换的顺序就与眼睛看到的相反，还会把从未在正文出现过
 * 的引用的图片算进画册（codex #599 R1 P2）。这里只负责把每条落位记录翻成预览要用的
 * 标签与 alt，用的是与 `InlineCitationImages` 渲染时**同一条**取值规则。
 */
export function buildImageGallery(
  slots: readonly GallerySlot[],
  resolveReference: (citationKey: string) => GalleryReference | undefined,
): AnswerImagePreviewItem[] {
  const items: AnswerImagePreviewItem[] = [];
  for (const { citationKey, imageId } of slots) {
    const reference = resolveReference(citationKey);
    if (!reference) continue;
    const image = reference.images.find((candidate) => candidate.asset_id === imageId);
    if (!image) continue;
    items.push({
      assetId: image.asset_id,
      alt: image.caption || `${reference.displayLabel} 的附图`,
      referenceLabel: reference.displayLabel,
    });
  }
  return items;
}

/**
 * 点开 `current` 这张图：在清单里定位它，定位不到就退化成只有它自己的一张快照
 * （左右按键与切换按钮随之消失）。绝不因为定位不到就打开清单里的**另一张**图。
 */
export function imagePreviewRequest(
  gallery: readonly AnswerImagePreviewItem[],
  current: AnswerImagePreviewItem,
): AnswerImagePreviewRequest {
  const index = gallery.findIndex((item) => item.assetId === current.assetId);
  return index >= 0 ? { items: gallery, index } : { items: [current], index: 0 };
}

/** 当前这一张；index 越界（不应发生）时返回 null，由调用方决定不渲染。 */
export function currentPreviewImage(
  request: AnswerImagePreviewRequest | null,
): AnswerImagePreviewItem | null {
  return request?.items[request.index] ?? null;
}
