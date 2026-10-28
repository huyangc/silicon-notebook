"use client";

// 网页导出 / 导入单本笔记本的界面(计划 docs/superpowers/plans/2026-10-22-notebook-export-import.md
// §7、§9)。状态机在 use-notebook-transfer.ts,网络与文案在 notebook-transfer-api.ts。
//
// 三个入口:
//   * `NotebookExportSection` —— 笔记本设置弹窗里的「导出」一节;镜像库打不开设置弹窗,
//     卡片菜单的「导出笔记本」打开 `NotebookExportModal`,里面是同一节。
//   * `NotebookImportModal` —— 「＋ 新建」旁「导入」打开的根弹窗(只呈现 page 持有的
//     `useNotebookImport`)。
//
// 每颗会持续的按钮在途时禁用并换成进行中的字,结果写在它旁边(AGENTS.md「Interactive
// feedback」);关闭入口任何时候都不禁用(无退路弹窗契约:作业在服务端接着跑)。

import { FloatingModalCard } from "./floating-modal-card.tsx";
import {
  REEMBED_NOTICE,
  exportReportNotes,
  formatTransferBytes,
  formatTransferDeadline,
  importReportNotes,
  importedNotebookReady,
  transferActive,
  transferFailureText,
  transferLimitLabel,
  transferPhaseText,
  transferProgressDetail,
  transferProgressText,
  type NotebookTransferJob,
} from "./notebook-transfer-api.ts";
import { useImportUpload, useImportUploadBusy, type UploadSnapshot } from "./notebook-import-upload.ts";
import {
  useNotebookExport,
  type NotebookImportView,
} from "./use-notebook-transfer.ts";

// ======================================================================== 导出

export const EXPORT_INTRO =
  "把这本笔记本（原文件、知识图谱、经验表与检索数据）导出成一个 .snbk 文件，"
  + "可以在本部署或其他部署里导入为一本新的笔记本。";
export const MEMORY_EXPORT_LABEL = "导出我的记忆（Markdown）";

function limitSentence(limitBytes: number | null, verb: "导出" | "导入"): string {
  if (limitBytes === null) return "";
  return `网页${verb}上限 ${transferLimitLabel(limitBytes)}；更大的笔记本请联系管理员用命令行${verb}。`;
}

/** 「开始导入」那颗按钮在上传各阶段的字:核对名额 → 百分比 → 传完、等服务器检查文件。 */
function uploadLabel(upload: UploadSnapshot): string {
  if (upload === null) return "开始导入";
  if (upload.stage === "precheck") return "正在准备…";
  if (upload.stage === "checking") return "正在检查文件…";
  return `上传中 ${upload.percent}%`;
}

/** 笔记本设置里的「导出」一节(卡片菜单的导出弹窗里也是它)。 */
export function NotebookExportSection({ notebookId }: { notebookId: string }) {
  const exporter = useNotebookExport(notebookId);
  const { view } = exporter;
  const job = view.job;
  const active = Boolean(job && transferActive(job));
  const ready = Boolean(job && job.state === "succeeded" && job.downloadable);
  const failed = Boolean(job && (job.state === "failed" || job.state === "cancelled"));
  const deadline = formatTransferDeadline(job?.expires_at ?? null);
  const startLabel = view.starting ? "正在提交…" : ready || failed ? "重新导出" : "导出笔记本";
  return (
    <section className="settings-section notebook-transfer-section" aria-label="导出">
      <div className="settings-section-head">
        <h3>导出</h3>
        <p>{EXPORT_INTRO}{limitSentence(view.limitBytes, "导出")}</p>
      </div>
      <p className="transfer-note">
        导出文件不包含记忆。需要的话可以另外
        <button
          type="button"
          className="link-button inline-action"
          disabled={view.memoryExporting}
          onClick={() => { void exporter.exportMemories(); }}
        >
          {view.memoryExporting ? "正在导出记忆…" : MEMORY_EXPORT_LABEL}
        </button>
        。
        {view.memoryResult === "exported" && <span className="transfer-ok" role="status"> 已开始下载记忆。</span>}
        {view.memoryResult === "failed" && <span className="transfer-error" role="alert"> {view.memoryText}</span>}
      </p>

      {view.loading ? (
        <p className="transfer-hint" role="status">正在读取导出状态…</p>
      ) : active && job ? (
        <div className="transfer-status" data-testid="export-status">
          {/* 只有阶段在播报区里(阶段变了才读一次);一秒一变的字节数在播报区外,
              不会每 1.5 秒被读屏软件重读一遍。 */}
          <p className="transfer-progress">
            <span role="status" aria-live="polite">{`正在导出：${transferPhaseText(job)}`}</span>
            {transferProgressDetail(job) && <span>{transferProgressDetail(job)}</span>}
          </p>
          <p className="transfer-hint">关掉这个窗口不影响导出；完成后也可以在右上角的通知里下载。</p>
          <div className="transfer-actions">
            <button
              type="button"
              className="sort-button"
              disabled={view.cancelling}
              onClick={() => { void exporter.cancel(); }}
            >
              {view.cancelling ? "正在取消…" : "取消导出"}
            </button>
            {view.cancelError && <span className="transfer-error" role="alert">{view.cancelError}</span>}
          </div>
        </div>
      ) : (
        <div className="transfer-status" data-testid="export-status">
          {ready && job && (
            <>
              <p className="transfer-progress">
                {`导出完成：${formatTransferBytes(job.archive_bytes)}`}
                {deadline ? `，${deadline} 前可以下载` : ""}
                {job.downloaded ? "（已下载过，期限内可以再次下载）" : ""}。
              </p>
              {exportReportNotes(job).map((note) => <p className="transfer-hint" key={note}>{note}</p>)}
              <div className="transfer-actions">
                <button
                  type="button"
                  className={view.downloadResult === "started" ? "new-pill transfer-button-done" : "new-pill"}
                  disabled={view.downloading}
                  onClick={() => { void exporter.download(); }}
                >
                  {view.downloading ? "准备下载…" : view.downloadResult === "started" ? "已开始下载" : "下载"}
                </button>
                {view.downloadResult === "failed" && <span className="transfer-error" role="alert">{view.downloadError}</span>}
              </div>
            </>
          )}
          {failed && job && <p className="transfer-error" role="alert">{transferFailureText(job)}</p>}
          <div className="transfer-actions">
            <button
              type="button"
              className={ready ? "sort-button" : "new-pill"}
              disabled={view.starting}
              onClick={() => { void exporter.start(); }}
            >
              {startLabel}
            </button>
            {view.startError && <span className="transfer-error" role="alert">{view.startError}</span>}
            {!view.startError && view.notice && <span className="transfer-ok" role="status">{view.notice}</span>}
          </div>
        </div>
      )}
    </section>
  );
}

type ModalShellProps = {
  onClose: () => void;
  interactive?: boolean;
  zIndex?: number;
};

/** 卡片菜单「导出笔记本」打开的弹窗(镜像库没有设置弹窗,导出只能从这里进)。 */
export function NotebookExportModal({
  notebookId,
  notebookName,
  onClose,
  interactive = true,
  zIndex,
}: ModalShellProps & { notebookId: string; notebookName: string }) {
  return (
    <section className="utility-modal" role="dialog" aria-modal={interactive} aria-hidden={!interactive} inert={interactive ? undefined : true} style={{ zIndex }}>
      <FloatingModalCard storageKey="notebook.export.window" className="utility-modal-card notebook-edit-card notebook-transfer-card">
        {(floating) => (<>
          <div className="source-modal-header" {...floating.dragHandleProps}>
            <div>
              <h2>导出「{notebookName}」</h2>
              <p>导出在后台进行，关掉这个窗口不影响它。</p>
            </div>
            {/* 关闭入口任何时候都不禁用:导出在服务端接着跑,铃铛里看得见。 */}
            <button type="button" className="icon-button" onClick={onClose} title="关闭">×</button>
          </div>
          <div className="edit-form notebook-transfer-body">
            <NotebookExportSection notebookId={notebookId} />
          </div>
        </>)}
      </FloatingModalCard>
    </section>
  );
}

// ======================================================================== 导入

/**
 * 集合页工具栏上的「导入」。上传与它的失败都要落在发起它的这颗按钮上:弹窗关着时,
 * 上传进行中说「导入中…」,失败了说「导入失败」(原因在弹窗里「开始导入」旁边),点一下
 * 都是打开导入弹窗。它自己订阅「有没有上传在进行」,进度变化不重渲它,更不重渲 Home。
 */
export function ImportToolbarButton({ failed, onOpen }: { failed: boolean; onOpen: () => void }) {
  const uploadInFlight = useImportUploadBusy();
  return (
    <button
      type="button"
      className={!uploadInFlight && failed ? "sort-button transfer-toolbar-failed" : "sort-button"}
      title={uploadInFlight ? "正在上传导入文件，点开查看进度" : failed ? "上一次导入没有开始，点开查看原因" : "把导出的笔记本文件导入为一本新的笔记本"}
      onClick={onOpen}
    >
      {uploadInFlight ? "导入中…" : failed ? "导入失败" : "导入"}
    </button>
  );
}

export const IMPORT_INTRO =
  "把导出的笔记本文件（.snbk）导入为一本新的笔记本：它归你所有，与原笔记本此后互不影响；同一个文件可以导入多次。";

function importStateLabel(job: NotebookTransferJob): string {
  if (job.state === "succeeded") return "导入完成";
  if (job.state === "failed") return "导入失败";
  if (job.state === "cancelled") return "已取消";
  if (importedNotebookReady(job)) return "已导入";
  return job.state === "queued" ? "排队中" : "导入中";
}

type ImportModalProps = ModalShellProps & {
  view: NotebookImportView;
  onChooseFile: (file: File | null) => void;
  onNameChange: (name: string) => void;
  onStart: () => void;
  onStopUpload: () => void;
  onCancelJob: (job: NotebookTransferJob) => void;
  onRemoveJob: (job: NotebookTransferJob) => void;
  onOpenNotebook: (notebookId: string) => void;
};

/** 「＋ 新建」旁「导入」打开的根弹窗。只呈现 page 持有的导入状态机。 */
export function NotebookImportModal({
  view,
  onChooseFile,
  onNameChange,
  onStart,
  onStopUpload,
  onCancelJob,
  onRemoveJob,
  onOpenNotebook,
  onClose,
  interactive = true,
  zIndex,
}: ImportModalProps) {
  // 上传进度只有这个弹窗订阅(Home 不因它重渲)。
  const upload = useImportUpload();
  const uploading = upload !== null;
  const blocked = !view.file || Boolean(view.fileError);
  return (
    <section className="utility-modal" role="dialog" aria-modal={interactive} aria-hidden={!interactive} inert={interactive ? undefined : true} style={{ zIndex }}>
      <FloatingModalCard storageKey="notebook.import.window" className="utility-modal-card notebook-edit-card notebook-transfer-card">
        {(floating) => (<>
          <div className="source-modal-header" {...floating.dragHandleProps}>
            <div>
              <h2>导入笔记本</h2>
              <p>{IMPORT_INTRO}</p>
            </div>
            {/* 关闭入口任何时候都不禁用:上传与导入都不因关掉窗口而停止,重新打开还看得见。 */}
            <button type="button" className="icon-button" onClick={onClose} title="关闭">×</button>
          </div>
          <div className="edit-form notebook-transfer-body">
            <label>
              导出文件
              <input
                key={view.fileInputKey}
                type="file"
                accept=".snbk"
                disabled={uploading}
                onChange={(event) => onChooseFile(event.target.files?.[0] ?? null)}
              />
            </label>
            <p className="transfer-hint">
              {view.file ? `${view.file.name} · ${formatTransferBytes(view.file.size)}。` : ""}
              {limitSentence(view.limitBytes, "导入")}
            </p>
            <label>
              新笔记本名称
              <input
                value={view.name}
                maxLength={80}
                placeholder="留空则沿用导出时的名称"
                disabled={uploading}
                onChange={(event) => onNameChange(event.target.value)}
              />
            </label>
            <div className="transfer-actions">
              <button
                type="button"
                className="new-pill"
                disabled={uploading || blocked}
                onClick={onStart}
              >
                {uploadLabel(upload)}
              </button>
              {/* 请求体传完之后(checking)停不下来了:服务器在检查文件、会建出作业。那时不给
                  「停止上传」,只说文件已经传上去了。 */}
              {uploading && upload.stage !== "checking" && (
                <button type="button" className="sort-button" onClick={onStopUpload}>停止上传</button>
              )}
              {upload?.stage === "checking" && (
                <span className="transfer-hint" role="status">文件已上传，正在检查…</span>
              )}
              {upload && upload.stage !== "precheck" && (
                <span
                  className="transfer-upload-bar"
                  role="progressbar"
                  aria-label="上传进度"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={upload.percent}
                  aria-valuetext={upload.stage === "checking" ? "已上传，正在检查文件" : `${upload.percent}%`}
                >
                  <span style={{ width: `${upload.percent}%` }} />
                </span>
              )}
              {view.fileError && <span className="transfer-error" role="alert">{view.fileError}</span>}
              {!view.fileError && view.uploadError && <span className="transfer-error" role="alert">{view.uploadError}</span>}
              {!view.fileError && !view.uploadError && view.uploadNotice && (
                <span className="transfer-ok" role="status">{view.uploadNotice}</span>
              )}
            </div>
            <p className="transfer-note">记忆、问答记录与深度报告不在导出文件里，不会被导入。</p>

            <div className="transfer-jobs">
              <h3>导入记录</h3>
              {view.loadingJobs && view.jobs.length === 0 && <p className="transfer-hint" role="status">正在读取…</p>}
              {!view.loadingJobs && view.jobs.length === 0 && <p className="transfer-hint">还没有导入记录。</p>}
              {view.jobs.map((job) => {
                const note = view.notes[job.id];
                if (note) {
                  return (
                    <div className="transfer-job transfer-job-settled" key={job.id} data-testid="import-job-row">
                      <p className="transfer-ok" role="status">
                        {note.text}
                        {note.notebookId && (
                          <>
                            {" "}
                            <button type="button" className="link-button inline-action" onClick={() => onOpenNotebook(note.notebookId ?? "")}>打开笔记本</button>
                          </>
                        )}
                      </p>
                    </div>
                  );
                }
                return (
                  <ImportJobRow
                    key={job.id}
                    job={job}
                    fileName={view.fileNames[job.id] ?? ""}
                    row={view.rows[job.id] ?? {}}
                    onCancel={() => onCancelJob(job)}
                    onRemove={() => onRemoveJob(job)}
                    onOpenNotebook={onOpenNotebook}
                  />
                );
              })}
            </div>
          </div>
        </>)}
      </FloatingModalCard>
    </section>
  );
}

function ImportJobRow({
  job,
  fileName,
  row,
  onCancel,
  onRemove,
  onOpenNotebook,
}: {
  job: NotebookTransferJob;
  fileName: string;
  row: NotebookImportView["rows"][string];
  onCancel: () => void;
  onRemove: () => void;
  onOpenNotebook: (notebookId: string) => void;
}) {
  const active = transferActive(job);
  const ready = importedNotebookReady(job);
  const title = job.notebook_name || fileName || "导入的笔记本";
  const busy = Boolean(row.busy) || Boolean(row.stopping);
  let detail: string;
  if (row.stopping) detail = "正在停止，结果确定后这里会更新。";
  else if (active && ready) detail = REEMBED_NOTICE;
  else if (active) detail = transferProgressText(job);
  else if (job.state === "failed") detail = transferFailureText(job);
  else if (job.state === "cancelled") detail = "这次导入已取消，没有留下任何内容。";
  else detail = "导入完成，新笔记本已在你的笔记本列表里。";
  const notes = job.state === "succeeded" ? importReportNotes(job) : [];
  return (
    <div className={`transfer-job transfer-job--${job.state}`} data-testid="import-job-row">
      <div className="transfer-job-head">
        <strong title={title}>{title}</strong>
        <span className="transfer-job-state">{importStateLabel(job)}</span>
      </div>
      <p className={job.state === "failed" ? "transfer-error" : "transfer-job-text"}>{detail}</p>
      {notes.map((note) => <p className="transfer-hint" key={note}>{note}</p>)}
      <div className="transfer-actions">
        {ready && (
          <button type="button" className="new-pill" onClick={() => onOpenNotebook(job.notebook_id)}>打开笔记本</button>
        )}
        {active && !ready && (
          <button type="button" className="sort-button" disabled={busy} onClick={onCancel}>
            {row.busy === "cancel" ? "正在取消…" : row.stopping ? "正在停止…" : "取消导入"}
          </button>
        )}
        {!active && (
          <button type="button" className="sort-button" disabled={busy} onClick={onRemove}>
            {row.busy === "remove" ? "正在移除…" : "移除记录"}
          </button>
        )}
        {row.failure && <span className="transfer-error" role="alert">{row.failure}</span>}
      </div>
    </div>
  );
}
