/** 添加来源弹窗里 zip/文件夹上传两块持久面板的纯展示组件——把它们从 `page.tsx` 抽出来，
 *  是为了能独立做 component 测试（`page.tsx` 是不可单独渲染的整页组件，业务编排仍留在
 *  那边，这里只接收 props、渲染，不持有状态）。
 *
 *  两块面板对应设计文档 §3.1：
 *  - `BundleChoicePanel`——zip/文件夹里有多个 markdown 时，让用户勾选要添加的那几个
 *    （第 2 条）。
 *  - `BundleReceiptsPanel`——每个已入列 md 的图片配对回执，弹窗内持久显示，不靠即逝
 *    toast（第 7 条，呼应既有「被跳过文件逐条持久列出」红线）。
 */

import {
  PAIRING_SKIPPED_SUMMARY,
  missingImageLine,
  noAltImageLine,
  receiptSummaryLine,
  remoteImageLine,
  unsupportedImageLine,
} from "./bundle-intake.ts";
import type { BundleFile, InlineReceipt } from "./md-bundle.ts";
import { compactStagedFileName } from "./source-upload.ts";

export type BundleChoiceState = {
  label: string;
  candidates: readonly BundleFile[];
  selected: ReadonlySet<string>;
};

export function BundleChoicePanel({
  choice,
  onToggle,
  onConfirm,
  onCancel,
}: {
  choice: BundleChoiceState;
  onToggle: (path: string) => void;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="source-detail-body bundle-choice">
      <p className="tool-hint" style={{ margin: "0 0 6px" }}>
        「{compactStagedFileName(choice.label)}」里有 {choice.candidates.length} 个 Markdown 文件，选择要添加的：
      </p>
      <div className="stack">
        {choice.candidates.map((candidate) => (
          <label key={candidate.path} className="checklist-row">
            <input
              type="checkbox"
              checked={choice.selected.has(candidate.path)}
              onChange={() => onToggle(candidate.path)}
            />
            <span style={{ flex: 1, wordBreak: "break-all" }}>{candidate.path}</span>
          </label>
        ))}
      </div>
      <div className="tag-row">
        <button
          className="new-pill"
          disabled={choice.selected.size === 0}
          onClick={onConfirm}
        >添加所选（{choice.selected.size}）</button>
        <button className="sort-button" onClick={onCancel}>取消</button>
      </div>
    </div>
  );
}

export type BundleReceiptEntry = {
  key: string;
  bundleLabel: string;
  fileName: string;
  receipt: InlineReceipt;
  /** 配对结果之外、关于这一份 md 本身的补充说明（未加入待上传列表的原因、超限时
   *  最大的几张图片…）。回执面板本来只描述「图片配上了没有」，一份被拒或被去重的
   *  md 若不加标注就会看起来像入列成功了。 */
  notes?: readonly string[];
  /** 本次是否因部署级开关关闭而整个跳过了图片配对/内联（`processMarkdownCandidate`
   *  的同名字段透传）。为真时概览行渲染 `PAIRING_SKIPPED_SUMMARY`，绝不落进
   *  `receiptSummaryLine` 的「未在正文中发现本地图片」——那句话断言「看过了、没找到」，
   *  这里是「压根没看」。 */
  pairingSkipped?: boolean;
};

export function BundleReceiptsPanel({
  receipts,
  onDismiss,
  imagesDisabledNote,
}: {
  receipts: readonly BundleReceiptEntry[];
  onDismiss: () => void;
  /** 部署未开启图片存储时的持久提示（`bundle-intake.ts` 的
   *  `BUNDLE_IMAGES_DISABLED_NOTE`）；省略或空表示该部署支持图片存储，不显示。 */
  imagesDisabledNote?: string | null;
}) {
  if (receipts.length === 0) return null;
  return (
    <div className="staged-skipped bundle-receipts" role="status">
      <div className="staged-skipped-head">
        <span>压缩包/文件夹图片配对结果</span>
        <button type="button" className="sort-button" onClick={onDismiss}>知道了</button>
      </div>
      {imagesDisabledNote && (
        <p className="tool-hint bundle-images-disabled-note">{imagesDisabledNote}</p>
      )}
      <div className="staged-skipped-rows bundle-receipt-rows">
        {receipts.map((entry) => (
          <div className="bundle-receipt-row" key={entry.key}>
            <div className="staged-skipped-row">
              <span className="staged-skipped-name" title={entry.fileName}>{compactStagedFileName(entry.fileName)}</span>
              <small>{entry.pairingSkipped ? PAIRING_SKIPPED_SUMMARY : receiptSummaryLine(entry.receipt)}</small>
            </div>
            {((entry.notes?.length ?? 0) > 0
              || entry.receipt.missing.length > 0
              || entry.receipt.unsupported.length > 0
              || entry.receipt.remote.length > 0
              || entry.receipt.noAlt.length > 0) && (
              <ul className="bundle-receipt-list">
                {(entry.notes ?? []).map((note, index) => (
                  <li key={`note-${index}`} className="bundle-receipt-note">{note}</li>
                ))}
                {entry.receipt.missing.map((item, index) => (
                  <li key={`missing-${index}`}>{missingImageLine(item)}</li>
                ))}
                {entry.receipt.unsupported.map((item, index) => (
                  <li key={`unsupported-${index}`}>{unsupportedImageLine(item)}</li>
                ))}
                {entry.receipt.remote.map((item, index) => (
                  <li key={`remote-${index}`}>{remoteImageLine(item)}</li>
                ))}
                {entry.receipt.noAlt.map((item, index) => (
                  <li key={`noalt-${index}`}>{noAltImageLine(item)}</li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
