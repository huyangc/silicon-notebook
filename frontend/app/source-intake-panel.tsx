"use client";

import { ExternalLink, Upload } from "lucide-react";
import { FloatingModalCard } from "./floating-modal-card";
import { BundleChoicePanel, BundleReceiptsPanel } from "./bundle-upload-panels";
import { BUNDLE_IMAGES_DISABLED_NOTE } from "./bundle-intake.ts";
import { compactStagedFileName, sourceUploadSizeLabel } from "./source-upload.ts";
import type { RootModalView } from "./use-root-modal-coordinator.ts";
import type { SourceIntake } from "./use-source-intake.ts";

type SourceIntakePanelProps = {
  intake: SourceIntake;
  modal: RootModalView;
  docTypeOptions: readonly { id: string; label: string }[];
  onManageSources: () => void;
};

/** Presentation of the intake draft; modal leases stay with the shell's
 * coordinator and all staging/upload decisions stay with the intake owner. */
export function SourceIntakePanel({ intake, modal, docTypeOptions, onManageSources }: SourceIntakePanelProps) {
  const { view, commands } = intake;
  const { stagedFiles, stagedDocTypes, stagedSkipped, stagedWarnings, bundleChoice, bundleReceipts, bundleBusyLabel, dropZoneDragActive, uploadBusy, linkSectionOpen, urlText, urlBusy, urlRejected, sourceImagePairingEnabled, supportedSourceAccept, supportedSourceUserHint, sourceUploadMaxBytes, sourceUploadMaxFilesPerBatch, sourceUploadConfigLoading, sourceFilePickerDisabled, sourceFilePickerHint, stagedUploadBlockedReason, docCapacity, atDocCapacityHint } = view;
  const { closeSourceModal, stageFiles, handleStageDragOver, handleStageDrop, setDropZoneDragActive, toggleBundleCandidate, confirmBundleChoice, cancelBundleChoice, setStagedDocType, setAllStagedDocTypes, removeStagedFile, confirmUpload, submitUrlSources, setUrlText, resetStagedIntake } = commands;
  return (
        <section className="source-modal" role="dialog" aria-modal={modal.topmost} aria-hidden={!modal.topmost} inert={modal.topmost ? undefined : true} style={{ zIndex: modal.zIndex }} onClick={(event) => { if (event.currentTarget === event.target) closeSourceModal("backdrop"); }}>
          <FloatingModalCard storageKey="source.add.window" className="source-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>添加来源</h2>
                <p>上传文件或添加链接；文件可为每个指定文档类型（默认自动检测），类型决定要分析出哪些字段。</p>
              </div>
              <button className="icon-button" onClick={() => closeSourceModal("button")} title="关闭">×</button>
            </div>
            {docCapacity.show && (
              <div className={`source-doc-capacity${docCapacity.atCapacity ? " is-full" : ""}`}>
                <span className="source-doc-capacity-count">文档 {docCapacity.count} / {docCapacity.limit}</span>
                {docCapacity.atCapacity && (
                  <span className="source-doc-capacity-hint">
                    已达该笔记本的文档数量上限，如需继续添加，请先删除部分文档（
                    <button type="button" className="link-button inline-action" onClick={onManageSources}>
                      关闭并管理来源
                    </button>
                    ），或联系管理员调整上限。
                  </span>
                )}
              </div>
            )}
            <label
              className={`drop-zone${sourceFilePickerDisabled ? " is-disabled" : ""}${dropZoneDragActive ? " is-dragover" : ""}`}
              title={sourceFilePickerHint}
              onDragOver={handleStageDragOver}
              onDragEnter={() => { if (!sourceFilePickerDisabled) setDropZoneDragActive(true); }}
              onDragLeave={() => setDropZoneDragActive(false)}
              onDrop={handleStageDrop}
            >
              <input type="file" multiple accept={supportedSourceAccept} onChange={stageFiles} disabled={sourceFilePickerDisabled} />
              <span className="drop-plus">＋</span>
              <strong>{stagedFiles.length > 0 ? "继续添加文件" : "或拖放文件"}</strong>
              <small>
                {bundleBusyLabel
                  ? bundleBusyLabel
                  : sourceUploadConfigLoading
                    ? "正在读取上传限制…"
                    : `支持 ${supportedSourceUserHint}。单个文件最大 ${sourceUploadMaxBytes === null ? "" : sourceUploadSizeLabel(sourceUploadMaxBytes)}，单次最多 ${sourceUploadMaxFilesPerBatch} 个。Markdown 图片压缩包会上传到后台解析并保存图片。`}
              </small>
            </label>
            {stagedSkipped.length > 0 && (
              <div className="staged-skipped" role="alert">
                <div className="staged-skipped-head">
                  <span>已跳过 {stagedSkipped.length} 个文件（不会上传）</span>
                  <button type="button" className="sort-button" onClick={commands.dismissSkipped}>知道了</button>
                </div>
                <div className="staged-skipped-rows">
                  {stagedSkipped.map((item, index) => (
                    <div className="staged-skipped-row" key={`${item.name}-${index}`}>
                      <span className="staged-skipped-name" title={item.name}>{compactStagedFileName(item.name)}</span>
                      <small className="staged-skipped-reason">{item.reason}</small>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {stagedWarnings.length > 0 && (
              <div className="staged-skipped staged-warnings" role="status">
                <div className="staged-skipped-head">
                  <span>图片提示 {stagedWarnings.length} 条（文件仍可上传）</span>
                  <button type="button" className="sort-button" onClick={commands.dismissWarnings}>知道了</button>
                </div>
                <div className="staged-skipped-rows">
                  {stagedWarnings.map((item, index) => (
                    <div className="staged-skipped-row" key={`${item.name}-${index}`}>
                      <span className="staged-skipped-name" title={item.name}>{compactStagedFileName(item.name)}</span>
                      <small className="staged-skipped-reason">{item.reason}</small>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {bundleChoice && (
              <BundleChoicePanel
                choice={bundleChoice}
                onToggle={toggleBundleCandidate}
                onConfirm={confirmBundleChoice}
                onCancel={cancelBundleChoice}
              />
            )}
            <BundleReceiptsPanel
              receipts={bundleReceipts}
              onDismiss={commands.dismissReceipts}
              imagesDisabledNote={sourceImagePairingEnabled ? null : BUNDLE_IMAGES_DISABLED_NOTE}
            />
            <div className="source-action-row">
              <label
                className={`source-action-button${sourceFilePickerDisabled ? " is-disabled" : ""}`}
                title={sourceFilePickerHint}
                onDragOver={handleStageDragOver}
                onDrop={handleStageDrop}
              >
                <Upload size={18} strokeWidth={2.5} /> 上传文件
                <input type="file" multiple accept={supportedSourceAccept} onChange={stageFiles} disabled={sourceFilePickerDisabled} />
              </label>
              <button
                type="button"
                className={`source-action-button${linkSectionOpen ? " is-active" : ""}`}
                disabled={docCapacity.atCapacity}
                title={docCapacity.atCapacity ? atDocCapacityHint : undefined}
                onClick={commands.toggleLinks}
              >
                <ExternalLink size={18} strokeWidth={2.5} /> 添加链接
              </button>
            </div>
            {linkSectionOpen && (
              <div className="source-detail-body">
                <p className="tool-hint" style={{ margin: "0 0 6px" }}>每行一个可直链的 PDF；非 PDF 会被直接拒绝。</p>
                <textarea
                  rows={5}
                  value={urlText}
                  placeholder={"https://arxiv.org/pdf/2401.00001\nhttps://example.com/paper.pdf"}
                  onChange={(event) => setUrlText(event.target.value)}
                />
                {urlRejected.length > 0 && (
                  <div className="stack" style={{ marginTop: 8 }}>
                    <span className="section-title">被拒链接</span>
                    {urlRejected.map((item, index) => (
                      <div className="checklist-row" key={`${item.url}-${index}`}>
                        <span style={{ flex: 1, wordBreak: "break-all" }}>{item.url}</span>
                        <small style={{ color: "var(--danger, #c0392b)" }}>{item.reason}</small>
                      </div>
                    ))}
                  </div>
                )}
                <div className="tag-row">
                  <button className="new-pill" disabled={urlBusy || docCapacity.atCapacity} title={docCapacity.atCapacity ? atDocCapacityHint : undefined} onClick={() => submitUrlSources()}>
                    {urlBusy ? "添加中…" : "添加并解析"}
                  </button>
                  <button className="sort-button" onClick={commands.cancelLinks}>取消</button>
                </div>
              </div>
            )}
            {stagedFiles.length > 0 && (
              <div className="source-detail-body">
                <div className="staged-head">
                  <span className="section-title">{stagedFiles.length} 个待上传文件</span>
                  <label className="staged-bulk">全部设为
                    <select value="__none__" onChange={(event) => { if (event.target.value !== "__none__") setAllStagedDocTypes(event.target.value); }}>
                      <option value="__none__">— 批量设置 —</option>
                      {docTypeOptions.map((opt) => <option key={opt.id || "auto"} value={opt.id}>{opt.label}</option>)}
                    </select>
                  </label>
                </div>
                <div className="stack">
                  {stagedFiles.map((file, index) => (
                    <div className="staged-file-row" key={`${file.name}-${index}`}>
                      <span className="staged-file-name" title={file.name}>{compactStagedFileName(file.name)}</span>
                      <select
                        className="staged-file-type"
                        value={stagedDocTypes[index] ?? ""}
                        onChange={(event) => setStagedDocType(index, event.target.value)}
                      >
                        {docTypeOptions.length === 0 && <option value="">自动检测</option>}
                        {docTypeOptions.map((opt) => <option key={opt.id || "auto"} value={opt.id}>{opt.label}</option>)}
                      </select>
                      <button
                        type="button"
                        className="staged-file-remove"
                        title="移除此文件"
                        aria-label={`移除 ${file.name}`}
                        onClick={() => removeStagedFile(index)}
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>
                {stagedUploadBlockedReason && (
                  <p id="staged-upload-blocked-reason" className="source-upload-blocked-hint" role="alert">
                    {stagedUploadBlockedReason}
                  </p>
                )}
                <div className="tag-row">
                  <button
                    className="new-pill"
                    disabled={uploadBusy || Boolean(stagedUploadBlockedReason)}
                    title={stagedUploadBlockedReason ?? undefined}
                    aria-describedby={stagedUploadBlockedReason ? "staged-upload-blocked-reason" : undefined}
                    onClick={() => confirmUpload()}
                  >{uploadBusy ? "上传中…" : `上传 ${stagedFiles.length} 个文件`}</button>
                  <button className="sort-button" disabled={uploadBusy} onClick={resetStagedIntake}>清空</button>
                </div>
              </div>
            )}
            </>)}
          </FloatingModalCard>
        </section>
  );
}

