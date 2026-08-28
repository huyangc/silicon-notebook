import { useRef, useState } from "react";

import { KgTypeMark } from "./kg-type-mark";
import {
  EMPTY_CREATE_DRAFT,
  type CreateDraft,
  type SchemaDraft,
  type SchemaView,
  deleteActionLabel,
  draftFromSchema,
  draftIsDirty,
  groupSchemas,
  managedRowKey,
  placementBadge,
  placementLabel,
  removable,
  resolveCreatePrimary,
  saveActionLabel,
  schemaRowKey,
  type SchemaWriteOutcome,
  splitFields,
  statusLabel,
  statusTone,
  toggleActionLabel,
  validateCreateDraft,
  validateDraft,
} from "./schema-manager-model.ts";
import type { ObjectSchema } from "./workspace-model.ts";

export type { SchemaView, SchemaWriteOutcome } from "./schema-manager-model.ts";

/**
 * 「图谱 Schema」面板。外层(page.tsx)负责浮动窗与标题栏,这里只管内容——同
 * `AgentProfilePanel` 与 `KgAnalysisView` 的分工。
 *
 * 版式是**三段式**,而不是一条竖列:
 *
 *   ① 作用范围(当前笔记本 / 全局基线)——决定下面看到的是哪一份注册表、以及写下去
 *      会落到哪里。它是别的一切的前提,所以单独占一行、在最上面。
 *   ② 类型清单(左栏)——只读地回答「这个库现在按什么类型分析」。候选 / 生效中 /
 *      已停用分成三组,每行一句话说清它从哪来。
 *   ③ 选中类型的定义(右栏)——默认**只读**,按「编辑」才换成表单。
 *
 * 这次重排要根治的就是「逻辑搅在一起」:原来一条竖列里,归纳候选、生效类型、新增
 * 表单首尾相接,而每一个已有类型都**常驻**摊开成六个输入框——想看一眼「现在有哪些
 * 类型」得先滚过几十个输入框,而想改一个类型又找不到它在哪一段。看与改是两件事,
 * 现在分在两栏里;而「新增」是第三件事,收在清单栏底部的动作区。
 */

type SchemaPatch = Partial<ObjectSchema> & { status?: string };

type CreatePayload = {
  object_type: string;
  plural: string;
  fields: string[];
  primary: string;
  description: string;
  label: string;
  list_fields: string[];
};

/**
 * 写动作的回执(三值,定义见 `schema-manager-model.ts`)。
 *
 * 面板拿它决定两件不能靠猜的事:保存后要不要退回只读态、新增后要不要清空表单。
 * 拿不到回执就只能「反正大概成了」,那正是「新增失败却把输入清光」这类问题的来源。
 * 而 `unconfirmed` 之所以不能并进 `failed`:那一档的写**已经落库**,只是没能确认;
 * 说成失败会引着用户去重试,重试就撞重名。
 */
type MutationOutcome = Promise<SchemaWriteOutcome>;

/** 写已提交但没能确认时的说法。刻意不说「失败」,也刻意劝住重试。 */
const UNCONFIRMED = "改动可能已经生效，但没能读回最新的类型清单。请关掉这个面板重新打开确认，不要直接重试。";

type WorkbenchProps = {
  schemas: ObjectSchema[];
  busy: boolean;
  view: SchemaView;
  canEdit: boolean;
  onPatch: (type: string, patch: SchemaPatch) => MutationOutcome;
  onCreate: (payload: CreatePayload) => MutationOutcome;
  onDelete: (type: string) => MutationOutcome;
  onInduce: () => void;
};

/** 「正在新增」这一格的身份。与行身份同处一个命名空间,故不能与任何 `schemaRowKey` 撞名。 */
const CREATE_PANE = "create";

/**
 * 右栏此刻在显示什么。选中一行和「正在新增」是互斥的两种状态。
 *
 * `key` 是**行的身份**(`schemaRowKey`),不是类型名:同一个类型可以同时有一条继承行和
 * 一条还没批准的同名候选,只按类型名认行会让候选那一行永远选不中。
 */
type Pane =
  | { kind: "empty" }
  | { kind: "detail"; key: string; editing: boolean }
  | { kind: "create" };

function scopeHint(view: SchemaView, canEdit: boolean): string {
  if (view === "global") return "全局基线会影响所有尚未建立当前笔记本覆盖的笔记本。";
  return canEdit
    ? "这里显示当前笔记本实际采用的类型；修改全局继承项会只在当前笔记本建立覆盖。"
    : "你拥有只读权限，可以查看当前笔记本实际采用的类型。";
}

/**
 * 字段的统一呈现:一排 chip,主字段和列表字段直接标在字段自己身上。
 *
 * 刻意不把「字段 / 主字段 / 列表字段」排成三行文本——它们说的是同一组名字的三个
 * 侧面,拆成三行读者得自己对照。编辑表单里仍是三个输入框(那是三个独立的输入),
 * 但**结果**在只读态和输入框下方的预览里都用这一种形状。
 */
function FieldChips({
  fields,
  primary,
  listFields,
}: {
  fields: string[];
  primary: string;
  listFields: string[];
}) {
  if (fields.length === 0) return <span className="schema-def-empty">未填写</span>;
  return (
    <span className="schema-chips">
      {fields.map((field) => (
        <span key={field} className={`schema-chip ${field === primary ? "is-primary" : ""}`}>
          <code>{field}</code>
          {field === primary && <span className="schema-chip-mark" title="主字段">主</span>}
          {listFields.includes(field) && <span className="schema-chip-mark" title="列表字段">列表</span>}
        </span>
      ))}
    </span>
  );
}

function DraftFields({
  draft,
  busy,
  onChange,
}: {
  draft: SchemaDraft;
  busy: boolean;
  onChange: (patch: Partial<SchemaDraft>) => void;
}) {
  return (
    <>
      <label className="schema-field">
        <span>显示名</span>
        <input value={draft.label} disabled={busy} onChange={(event) => onChange({ label: event.target.value })} />
      </label>
      <label className="schema-field">
        <span>复数名称</span>
        <input value={draft.plural} disabled={busy} onChange={(event) => onChange({ plural: event.target.value })} />
      </label>
      <label className="schema-field">
        <span>字段（逗号分隔，按顺序）</span>
        <textarea rows={2} value={draft.fieldsText} disabled={busy} onChange={(event) => onChange({ fieldsText: event.target.value })} />
      </label>
      <label className="schema-field">
        <span>主字段</span>
        <input value={draft.primary} disabled={busy} onChange={(event) => onChange({ primary: event.target.value.trim() })} />
      </label>
      <label className="schema-field">
        <span>列表字段（逗号分隔，可留空）</span>
        <input value={draft.listFieldsText} disabled={busy} onChange={(event) => onChange({ listFieldsText: event.target.value })} />
      </label>
      <label className="schema-field">
        <span>说明（用于分析提示）</span>
        <input value={draft.description} disabled={busy} onChange={(event) => onChange({ description: event.target.value })} />
      </label>
      <div className="schema-field-preview">
        <span className="schema-field-preview-label">保存后会存下这些字段</span>
        <FieldChips
          fields={splitFields(draft.fieldsText)}
          primary={draft.primary}
          listFields={splitFields(draft.listFieldsText)}
        />
      </div>
    </>
  );
}

function SchemaWorkbench({
  schemas,
  busy,
  view,
  canEdit,
  onPatch,
  onCreate,
  onDelete,
  onInduce,
}: WorkbenchProps) {
  const [pane, setPane] = useState<Pane>({ kind: "empty" });
  // 草稿按类型分格保存,而不是随选中项销毁:在编辑一个类型的中途点开另一个类型
  // (对照一眼字段命名,是这个面板最常见的动作)不该把已经打进去的字丢掉。回到原来
  // 那一行时草稿原样还在,清单上那颗「未保存」圆点也一直亮着。
  const [drafts, setDrafts] = useState<Record<string, SchemaDraft>>({});
  const [createDraft, setCreateDraft] = useState<CreateDraft>(EMPTY_CREATE_DRAFT);
  const [error, setError] = useState("");
  // 与 `busy` 分工:`busy` 是整个面板的忙碌位(别人触发的动作也会置起),这一格记的是
  // **哪一行**正由本面板提交(行身份,新增时是 `CREATE_PANE`)。存行身份而不是布尔:
  // 请求在飞期间清单仍然可点,存布尔的话「保存中…」会跟着跑到用户刚点开的另一行上。
  const [submittingKey, setSubmittingKey] = useState("");

  // 提交那一刻用户停在哪一格。写动作完成时要拿它核对「人还在不在原地」——见 settle。
  const paneRef = useRef(pane);
  paneRef.current = pane;

  /**
   * 只有用户仍停在发起这次动作的那一格时,才把结果落到界面上。
   *
   * 请求在飞期间清单行**刻意**仍然可点:只读浏览不该被一次写入冻住。代价是完成回调
   * 会晚于用户的下一次导航到达,不核对就会做两件都错的事——把人从他刚点开的类型拽回
   * 原来那一行,以及把失败提示挂在**另一个**类型旁边。后者正是 `AGENTS.md` 那条
   * 「动作结果落在按钮自身或紧邻处」要消灭的形态:提示还在,只是长在了错的地方,比
   * 不提示更糟(codex #614 R2 P2)。人已经走开时,真失败仍由上层的错误提示兜底。
   *
   * 与 `use-kg-schema.ts` 里 `owns(owner) && ownsOperation(...)` 是同一条纪律,只是
   * 这一层的身份是「面板此刻停在哪一格」。
   */
  const settle = (paneKey: string, apply: () => void) => {
    const current = paneRef.current;
    const currentKey = current.kind === "detail" ? current.key : current.kind === "create" ? CREATE_PANE : "";
    if (currentKey === paneKey) apply();
  };

  const groups = groupSchemas(schemas);
  const selectedKey = pane.kind === "detail" ? pane.key : "";
  const selected = pane.kind === "detail"
    ? schemas.find((schema) => schemaRowKey(schema) === pane.key) ?? null
    : null;

  const draftOf = (schema: ObjectSchema): SchemaDraft => drafts[schemaRowKey(schema)] ?? draftFromSchema(schema);
  const dirtyOf = (schema: ObjectSchema): boolean => {
    const draft = drafts[schemaRowKey(schema)];
    return draft ? draftIsDirty(schema, draft) : false;
  };

  // 点清单永远落在只读态,哪怕这一行还留着脏草稿。点一行是**导航**动作,不是编辑
  // 动作;自动弹回表单会让「我只想看一眼这个类型有哪些字段」变成一屏输入框。草稿
  // 没有因此被藏起来:行上的圆点、只读态那句提醒和「继续编辑」三处都在说它还在。
  const select = (schema: ObjectSchema) => {
    setError("");
    setPane({ kind: "detail", key: schemaRowKey(schema), editing: false });
  };

  const patchDraft = (rowKey: string, base: SchemaDraft, patch: Partial<SchemaDraft>) => {
    setDrafts((previous) => ({ ...previous, [rowKey]: { ...base, ...patch } }));
  };

  const dropDraft = (rowKey: string) => {
    setDrafts((previous) => {
      if (!(rowKey in previous)) return previous;
      const next = { ...previous };
      delete next[rowKey];
      return next;
    });
  };

  const discardDraft = (rowKey: string) => {
    dropDraft(rowKey);
    setError("");
    setPane({ kind: "detail", key: rowKey, editing: false });
  };

  const save = async (schema: ObjectSchema, draft: SchemaDraft) => {
    const message = validateDraft(schema.object_type, draft);
    if (message) {
      setError(message);
      return;
    }
    const rowKey = schemaRowKey(schema);
    const failure = "保存失败，请检查类型定义后重试；当前输入已保留。";
    setError("");
    setSubmittingKey(rowKey);
    try {
      const outcome = await onPatch(schema.object_type, {
        fields: splitFields(draft.fieldsText),
        label: draft.label,
        plural: draft.plural,
        primary: draft.primary,
        list_fields: splitFields(draft.listFieldsText),
        description: draft.description,
      });
      if (outcome !== "confirmed") {
        settle(rowKey, () => setError(outcome === "failed" ? failure : UNCONFIRMED));
        return;
      }
      // 草稿无条件丢掉:它已经落库,不再是「用户还没提交的输入」。退回只读态则要核对
      // 人还在不在原地——只读态显示的就是刚写成功的那一版,那是落在按钮紧邻处的结果
      // 反馈,而人走开之后再把他拽回来就成了打断。
      dropDraft(rowKey);
      settle(rowKey, () => setPane({ kind: "detail", key: rowKey, editing: false }));
    } catch {
      settle(rowKey, () => setError(failure));
    } finally {
      setSubmittingKey("");
    }
  };

  const create = async () => {
    const message = validateCreateDraft(createDraft);
    if (message) {
      setError(message);
      return;
    }
    const failure = "新增失败，请检查类型标识是否重复及字段定义是否有效；当前输入已保留。";
    setError("");
    setSubmittingKey(CREATE_PANE);
    const objectType = createDraft.objectType.trim();
    try {
      const outcome = await onCreate({
        object_type: objectType,
        plural: createDraft.plural.trim() || `${objectType}s`,
        label: createDraft.label.trim(),
        fields: splitFields(createDraft.fieldsText),
        primary: resolveCreatePrimary(createDraft),
        list_fields: splitFields(createDraft.listFieldsText),
        description: createDraft.description.trim(),
      });
      if (outcome !== "confirmed") {
        settle(CREATE_PANE, () => setError(outcome === "failed" ? failure : UNCONFIRMED));
        return;
      }
      setCreateDraft(EMPTY_CREATE_DRAFT);
      // 新增完直接选中它:用户下一步多半就是看看它长什么样,而右栏空着会让人以为没成。
      // 新建出来的是生效类型而不是候选,所以认的是它的 managed 那一行。
      settle(CREATE_PANE, () => setPane({ kind: "detail", key: managedRowKey(objectType), editing: false }));
    } catch {
      settle(CREATE_PANE, () => setError(failure));
    } finally {
      setSubmittingKey("");
    }
  };

  const remove = async (schema: ObjectSchema) => {
    // 「恢复全局」删掉的是本库覆盖,同名的继承版本随即回到清单里;「删除」「拒绝」
    // 则是真的把这一行拿走。判据在发起前就已知(不能事后去翻 schemas——那还是发起
    // 前的那一份),所以直接按动作语义决定选中项去留。
    const restoresInherited = view === "notebook" && Boolean(schema.overrides_global);
    const rowKey = schemaRowKey(schema);
    const failure = "删除失败，请稍后重试；这个类型下若已经有知识对象则不能删除。";
    setError("");
    setSubmittingKey(rowKey);
    try {
      const outcome = await onDelete(schema.object_type);
      if (outcome !== "confirmed") {
        settle(rowKey, () => setError(outcome === "failed" ? failure : UNCONFIRMED));
        return;
      }
      dropDraft(rowKey);
      settle(rowKey, () => setPane(restoresInherited
        ? { kind: "detail", key: managedRowKey(schema.object_type), editing: false }
        : { kind: "empty" }));
    } catch {
      settle(rowKey, () => setError(failure));
    } finally {
      setSubmittingKey("");
    }
  };

  /**
   * 只改状态的那两个动作(停用/启用、批准并启用)。与保存/新增/删除同样认回执:
   * 结果必须落在按钮紧邻处,不能只发页面顶部的横幅。
   *
   * `nextKey` 不是多余的参数:批准会把一行从候选变成生效类型,它的身份(类型 + 是不是
   * 候选)随之改变;沿用旧身份的话,批准成功的那一刻右栏反而会空掉。
   */
  const patchStatus = async (schema: ObjectSchema, status: string, nextKey: string, failure: string) => {
    const rowKey = schemaRowKey(schema);
    setError("");
    setSubmittingKey(rowKey);
    try {
      const outcome = await onPatch(schema.object_type, { status });
      if (outcome !== "confirmed") {
        settle(rowKey, () => setError(outcome === "failed" ? failure : UNCONFIRMED));
        return;
      }
      settle(rowKey, () => setPane({ kind: "detail", key: nextKey, editing: false }));
    } catch {
      settle(rowKey, () => setError(failure));
    } finally {
      setSubmittingKey("");
    }
  };

  const rowWritable = (schema: ObjectSchema) => canEdit && schema.can_edit !== false;

  return (
    <div className="schema-workbench">
      <aside className="schema-list" aria-label="类型清单">
        <div className="schema-list-scroll">
          {schemas.length === 0 && <p className="tool-hint">这个范围里还没有任何类型。</p>}
          {groups.map((group) => (group.rows.length === 0 ? null : (
            <section className="schema-group" key={group.key}>
              <h3 className="schema-group-title">
                {group.title}
                <span className="schema-group-count">{group.rows.length}</span>
              </h3>
              <p className="schema-group-hint">{group.hint}</p>
              <ul className="schema-type-list">
                {group.rows.map((row) => (
                  <li key={schemaRowKey(row)}>
                    <button
                      type="button"
                      className={`schema-type-row${selectedKey === schemaRowKey(row) ? " is-selected" : ""}${statusTone(row.status) === "is-off" ? " is-off" : ""}`}
                      aria-current={selectedKey === schemaRowKey(row) ? "true" : undefined}
                      onClick={() => select(row)}
                    >
                      <KgTypeMark type={row.object_type} />
                      <span className="schema-type-text">
                        <span className="schema-type-name">
                          <code>{row.object_type}</code>
                          {row.label && <span className="schema-type-label">{row.label}</span>}
                        </span>
                        <span className="schema-type-meta">
                          {`${row.fields.length} 个字段`}
                          {row.primary ? ` · 主字段 ${row.primary}` : ""}
                        </span>
                      </span>
                      <span className="schema-type-badges">
                        {dirtyOf(row) && <span className="schema-dirty-dot" title="有还没保存的修改" />}
                        <span className="schema-badge" title={placementLabel(row, view)}>{placementBadge(row, view)}</span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )))}
        </div>
        {canEdit && (
          <div className="schema-list-actions">
            <button
              type="button"
              className="index-cta primary"
              disabled={busy}
              onClick={() => { setError(""); setPane({ kind: "create" }); }}
            >
              新增类型
            </button>
            {view === "notebook" && (
              <button type="button" className="index-cta" disabled={busy} onClick={onInduce}>从当前笔记本归纳候选类型</button>
            )}
            {busy && <span className="tag">处理中…</span>}
          </div>
        )}
      </aside>

      <section className="schema-detail" aria-label="类型定义">
        {pane.kind === "create" ? (
          <>
            <div className="schema-detail-head">
              <div className="schema-detail-title">
                <strong className="schema-detail-name">{view === "global" ? "新增全局基线类型" : "新增当前笔记本类型"}</strong>
              </div>
            </div>
            <div className="schema-detail-body">
              <label className="schema-field">
                <span>类型标识（snake_case）</span>
                <input
                  value={createDraft.objectType}
                  disabled={busy}
                  placeholder="例如 process_window"
                  onChange={(event) => setCreateDraft({ ...createDraft, objectType: event.target.value })}
                />
              </label>
              <DraftFields
                draft={createDraft}
                busy={busy}
                onChange={(patch) => setCreateDraft({ ...createDraft, ...patch })}
              />
              <p className="schema-note">复数名称留空时按类型标识加 s；主字段留空时取第一个字段。</p>
              {error && <p className="tool-hint" role="alert">{error}</p>}
              <div className="schema-actions">
                <button type="button" className="index-cta primary" disabled={busy} onClick={() => { void create(); }}>
                  {submittingKey === CREATE_PANE ? "新增中…" : "创建类型"}
                </button>
                <button
                  type="button"
                  className="index-cta"
                  disabled={busy}
                  onClick={() => { setCreateDraft(EMPTY_CREATE_DRAFT); setError(""); setPane({ kind: "empty" }); }}
                >
                  放弃
                </button>
              </div>
            </div>
          </>
        ) : selected === null ? (
          <div className="schema-detail-empty">
            <p className="tool-hint">从左边选一个类型，这里显示它的完整定义。</p>
          </div>
        ) : (
          <>
            <div className="schema-detail-head">
              <div className="schema-detail-title">
                <KgTypeMark type={selected.object_type} />
                <code className="schema-detail-name">{selected.object_type}</code>
                {selected.label && <span className="schema-detail-label">{selected.label}</span>}
              </div>
              <div className="schema-detail-badges">
                <span className="tag">{placementLabel(selected, view)}</span>
                <span className={`tag schema-status ${statusTone(selected.status)}`}>{statusLabel(selected.status)}</span>
              </div>
            </div>
            <div className="schema-detail-body">
              {selected.status === "proposed" ? (
                <>
                  {selected.rationale && (
                    <p className="schema-note">
                      <strong>归纳理由</strong>
                      {selected.rationale}
                    </p>
                  )}
                  <dl className="schema-def">
                    <div>
                      <dt>字段</dt>
                      <dd><FieldChips fields={selected.fields} primary={selected.primary} listFields={selected.list_fields} /></dd>
                    </div>
                  </dl>
                  {error && <p className="tool-hint" role="alert">{error}</p>}
                  {rowWritable(selected) && (
                    <div className="schema-actions">
                      <button
                        type="button"
                        className="index-cta primary"
                        disabled={busy}
                        onClick={() => {
                          void patchStatus(selected, "active", managedRowKey(selected.object_type), "批准失败，请稍后重试。");
                        }}
                      >
                        批准并启用
                      </button>
                      <button type="button" className="index-cta" disabled={busy} onClick={() => { void remove(selected); }}>拒绝</button>
                    </div>
                  )}
                </>
              ) : pane.kind === "detail" && pane.editing && rowWritable(selected) ? (
                <>
                  <DraftFields
                    draft={draftOf(selected)}
                    busy={busy}
                    onChange={(patch) => patchDraft(schemaRowKey(selected), draftOf(selected), patch)}
                  />
                  {error && <p className="tool-hint" role="alert">{error}</p>}
                  <div className="schema-actions">
                    <button
                      type="button"
                      className="index-cta primary"
                      disabled={busy || !dirtyOf(selected)}
                      onClick={() => { void save(selected, draftOf(selected)); }}
                    >
                      {submittingKey === schemaRowKey(selected) ? "保存中…" : saveActionLabel(selected, view)}
                    </button>
                    <button type="button" className="index-cta" disabled={busy} onClick={() => discardDraft(schemaRowKey(selected))}>
                      放弃修改
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <dl className="schema-def">
                    <div>
                      <dt>显示名</dt>
                      <dd>{selected.label || <span className="schema-def-empty">未填写</span>}</dd>
                    </div>
                    <div>
                      <dt>复数名称</dt>
                      <dd>{selected.plural || <span className="schema-def-empty">未填写</span>}</dd>
                    </div>
                    <div>
                      <dt>字段</dt>
                      <dd><FieldChips fields={selected.fields} primary={selected.primary} listFields={selected.list_fields} /></dd>
                    </div>
                    <div>
                      <dt>说明</dt>
                      <dd>{selected.description || <span className="schema-def-empty">未填写</span>}</dd>
                    </div>
                  </dl>
                  {dirtyOf(selected) && <p className="schema-note is-warning" role="status">这个类型有一份还没保存的修改。</p>}
                  {view === "notebook" && selected.inherited && rowWritable(selected) && (
                    <p className="schema-note">保存或停用都会先在当前笔记本建立一份覆盖，不影响别的笔记本。</p>
                  )}
                  {error && <p className="tool-hint" role="alert">{error}</p>}
                  {rowWritable(selected) && (
                    <div className="schema-actions">
                      <button
                        type="button"
                        className="index-cta primary"
                        disabled={busy}
                        onClick={() => { setError(""); setPane({ kind: "detail", key: schemaRowKey(selected), editing: true }); }}
                      >
                        {dirtyOf(selected) ? "继续编辑" : "编辑"}
                      </button>
                      <button
                        type="button"
                        className="index-cta"
                        disabled={busy}
                        onClick={() => {
                          void patchStatus(
                            selected,
                            selected.status === "active" ? "disabled" : "active",
                            managedRowKey(selected.object_type),
                            "切换启用状态失败，请稍后重试。",
                          );
                        }}
                      >
                        {toggleActionLabel(selected, view)}
                      </button>
                      {removable(selected, view) && (
                        <button type="button" className="index-cta" disabled={busy} onClick={() => { void remove(selected); }}>
                          {deleteActionLabel(selected, view)}
                        </button>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>
          </>
        )}
      </section>
    </div>
  );
}

export function SchemaManager({
  schemas,
  busy,
  view,
  canEdit,
  canManageGlobal,
  onView,
  onPatch,
  onCreate,
  onDelete,
  onInduce,
}: {
  schemas: ObjectSchema[] | null;
  busy: boolean;
  view: SchemaView;
  canEdit: boolean;
  canManageGlobal: boolean;
  onView: (view: SchemaView) => void;
  onPatch: (type: string, patch: SchemaPatch) => MutationOutcome;
  onCreate: (payload: CreatePayload) => MutationOutcome;
  onDelete: (type: string) => MutationOutcome;
  onInduce: () => void;
}) {
  if (schemas === null) return <p className="tool-hint">加载中…</p>;
  const globalView = view === "global";
  return (
    <div className="schema-panel">
      {canManageGlobal && (
        <div className="schema-scope" role="group" aria-label="图谱 Schema 视图">
          <span className="schema-scope-label">作用范围</span>
          <div className="chat-tabs">
            <button
              type="button"
              className={`chat-tab ${globalView ? "" : "active"}`}
              aria-pressed={!globalView}
              disabled={busy}
              onClick={() => { if (globalView) onView("notebook"); }}
            >
              当前笔记本
            </button>
            <button
              type="button"
              className={`chat-tab ${globalView ? "active" : ""}`}
              aria-pressed={globalView}
              disabled={busy}
              onClick={() => { if (!globalView) onView("global"); }}
            >
              全局基线
            </button>
          </div>
        </div>
      )}
      <p className="tool-hint">{scopeHint(view, canEdit)}</p>
      {/* key=作用范围:切范围换的是**另一份**注册表,选中项、草稿、错误提示都不该跟过去。
          交给 key 做结构性保证,而不是再写一条「切换时记得清空」的 effect。 */}
      <SchemaWorkbench
        key={view}
        schemas={schemas}
        busy={busy}
        view={view}
        canEdit={canEdit}
        onPatch={onPatch}
        onCreate={onCreate}
        onDelete={onDelete}
        onInduce={onInduce}
      />
    </div>
  );
}
