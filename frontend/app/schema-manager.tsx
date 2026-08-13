import { useEffect, useState } from "react";

import type { ObjectSchema } from "./workspace-model.ts";

export type SchemaView = "notebook" | "global";

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

const IDENTIFIER = /^[a-z][a-z0-9_]*$/;
const MAX_FIELDS = 64;
const MAX_NAME_CHARS = 80;
const MAX_TEXT_CHARS = 2000;

function splitFields(value: string): string[] {
  return value.split(",").map((field) => field.trim()).filter(Boolean);
}

function validateDefinition(objectType: string, fields: string[], primary: string, listFields: string[]): string {
  if (objectType && !IDENTIFIER.test(objectType)) return "类型标识须以小写字母开头，且只能包含小写字母、数字和下划线。";
  if (objectType.length > MAX_NAME_CHARS) return `类型标识不能超过 ${MAX_NAME_CHARS} 个字符。`;
  if (fields.length === 0) return "请至少填写一个字段。";
  if (fields.length > MAX_FIELDS || listFields.length > MAX_FIELDS) return `字段数量不能超过 ${MAX_FIELDS} 个。`;
  if ([...fields, ...listFields].some((field) => !IDENTIFIER.test(field))) return "字段须使用小写字母、数字和下划线，且以小写字母开头。";
  if ([...fields, ...listFields].some((field) => field.length > MAX_NAME_CHARS)) return `字段名称不能超过 ${MAX_NAME_CHARS} 个字符。`;
  if (new Set(fields).size !== fields.length) return "字段不能重复。";
  if (new Set(listFields).size !== listFields.length) return "列表字段不能重复。";
  if (primary && !fields.includes(primary)) return "主字段必须包含在字段列表中。";
  if (listFields.some((field) => !fields.includes(field))) return "列表字段必须包含在字段列表中。";
  return "";
}

function validateTexts(values: string[]): string {
  return values.some((value) => value.trim().length > MAX_TEXT_CHARS)
    ? `显示名、复数名称和说明均不能超过 ${MAX_TEXT_CHARS} 个字符。`
    : "";
}

function placementLabel(schema: ObjectSchema, view: SchemaView): string {
  if (view === "global") return schema.source === "builtin" ? "内置类型" : "全局基线";
  if (schema.inherited) return "全局继承";
  if (schema.overrides_global) return "当前笔记本覆盖";
  return "当前笔记本自建";
}

function SchemaRow({
  schema,
  busy,
  view,
  canEdit,
  onPatch,
  onDelete,
}: {
  schema: ObjectSchema;
  busy: boolean;
  view: SchemaView;
  canEdit: boolean;
  onPatch: (type: string, patch: SchemaPatch) => void | Promise<void>;
  onDelete: (type: string) => void;
}) {
  const [fieldsText, setFieldsText] = useState(schema.fields.join(", "));
  const [label, setLabel] = useState(schema.label);
  const [description, setDescription] = useState(schema.description);
  const [plural, setPlural] = useState(schema.plural);
  const [primary, setPrimary] = useState(schema.primary);
  const [listFieldsText, setListFieldsText] = useState(schema.list_fields.join(", "));
  const [error, setError] = useState("");
  useEffect(() => {
    setFieldsText(schema.fields.join(", "));
    setLabel(schema.label);
    setDescription(schema.description);
    setPlural(schema.plural);
    setPrimary(schema.primary);
    setListFieldsText(schema.list_fields.join(", "));
    setError("");
  }, [schema.description, schema.fields, schema.label, schema.list_fields, schema.plural, schema.primary]);
  const rowCanEdit = canEdit && schema.can_edit !== false;
  const dirty = fieldsText !== schema.fields.join(", ") || label !== schema.label || description !== schema.description
    || plural !== schema.plural || primary !== schema.primary || listFieldsText !== schema.list_fields.join(", ");
  const removable = view === "notebook" ? !schema.inherited : schema.source !== "builtin";
  const deleteLabel = view === "notebook" && schema.overrides_global ? "恢复全局" : "删除";
  const saveLabel = view === "notebook" && schema.inherited ? "保存并建立覆盖" : "保存";
  const toggleLabel = schema.status === "active" ? "停用" : "启用";
  const scopeToggleLabel = view === "notebook" && schema.inherited
    ? `${toggleLabel}并建立覆盖`
    : toggleLabel;

  return (
    <article className={`item schema-card ${schema.status === "disabled" ? "knowledge-deprecated" : ""}`}>
      <div className="schema-card-head">
        <strong>{schema.object_type}</strong>
        <span className="tag">{placementLabel(schema, view)}</span>
        <span className={`tag ${schema.status === "active" ? "severity-low" : ""}`}>{schema.status === "active" ? "已启用" : "已停用"}</span>
      </div>
      <label className="schema-field">
        <span>显示名</span>
        <input value={label} disabled={busy || !rowCanEdit} onChange={(event) => setLabel(event.target.value)} />
      </label>
      <label className="schema-field">
        <span>复数名称</span>
        <input value={plural} disabled={busy || !rowCanEdit} onChange={(event) => setPlural(event.target.value)} />
      </label>
      <label className="schema-field">
        <span>字段（逗号分隔，按顺序）</span>
        <textarea rows={2} value={fieldsText} disabled={busy || !rowCanEdit} onChange={(event) => setFieldsText(event.target.value)} />
      </label>
      <label className="schema-field">
        <span>主字段</span>
        <input value={primary} disabled={busy || !rowCanEdit} onChange={(event) => setPrimary(event.target.value.trim())} />
      </label>
      <label className="schema-field">
        <span>列表字段（逗号分隔，可留空）</span>
        <input value={listFieldsText} disabled={busy || !rowCanEdit} onChange={(event) => setListFieldsText(event.target.value)} />
      </label>
      <label className="schema-field">
        <span>说明（用于分析提示）</span>
        <input value={description} disabled={busy || !rowCanEdit} onChange={(event) => setDescription(event.target.value)} />
      </label>
      {error && <p className="tool-hint" role="alert">{error}</p>}
      {rowCanEdit && (
        <div className="schema-actions">
          <button className="sort-button" disabled={busy || !dirty} onClick={async () => {
            const fields = splitFields(fieldsText);
            const listFields = splitFields(listFieldsText);
            const message = validateDefinition(schema.object_type, fields, primary, listFields)
              || validateTexts([label, plural, description]);
            if (message) {
              setError(message);
              return;
            }
            setError("");
            try {
              await onPatch(schema.object_type, { fields, label, plural, primary, list_fields: listFields, description });
            } catch {
              setError("保存失败，请检查类型定义后重试；当前输入已保留。");
            }
          }}>{saveLabel}</button>
          <button className="sort-button" disabled={busy} onClick={() => onPatch(schema.object_type, {
            status: schema.status === "active" ? "disabled" : "active",
          })}>{scopeToggleLabel}</button>
          {removable && (
            <button className="sort-button" disabled={busy} onClick={() => onDelete(schema.object_type)}>{deleteLabel}</button>
          )}
        </div>
      )}
    </article>
  );
}

function NewSchemaForm({ busy, canEdit, title, onCreate }: { busy: boolean; canEdit: boolean; title: string; onCreate: (payload: CreatePayload) => void | Promise<void> }) {
  const [objectType, setObjectType] = useState("");
  const [label, setLabel] = useState("");
  const [fieldsText, setFieldsText] = useState("");
  const [description, setDescription] = useState("");
  const [plural, setPlural] = useState("");
  const [primary, setPrimary] = useState("");
  const [listFieldsText, setListFieldsText] = useState("");
  const [error, setError] = useState("");
  const submit = async () => {
    const normalizedType = objectType.trim();
    const fields = splitFields(fieldsText);
    const listFields = splitFields(listFieldsText);
    if (!normalizedType) {
      setError("请填写类型标识。");
      return;
    }
    const resolvedPrimary = primary.trim() || fields[0] || "";
    const message = validateDefinition(normalizedType, fields, resolvedPrimary, listFields)
      || validateTexts([label, plural, description]);
    if (message) {
      setError(message);
      return;
    }
    setError("");
    try {
      await onCreate({
        object_type: normalizedType,
        plural: plural.trim() || `${normalizedType}s`,
        label: label.trim(),
        fields,
        primary: resolvedPrimary,
        list_fields: listFields,
        description: description.trim(),
      });
      setObjectType("");
      setLabel("");
      setFieldsText("");
      setDescription("");
      setPlural("");
      setPrimary("");
      setListFieldsText("");
    } catch {
      setError("新增失败，请检查类型标识是否重复及字段定义是否有效；当前输入已保留。");
    }
  };
  return (
    <article className="item schema-card">
      <p className="section-title">{title}</p>
      <label className="schema-field"><span>类型标识（snake_case）</span><input value={objectType} disabled={busy || !canEdit} placeholder="例如 process_window" onChange={(event) => setObjectType(event.target.value)} /></label>
      <label className="schema-field"><span>显示名</span><input value={label} disabled={busy || !canEdit} onChange={(event) => setLabel(event.target.value)} /></label>
      <label className="schema-field"><span>复数名称（可留空）</span><input value={plural} disabled={busy || !canEdit} onChange={(event) => setPlural(event.target.value)} /></label>
      <label className="schema-field"><span>字段（逗号分隔）</span><textarea rows={2} value={fieldsText} disabled={busy || !canEdit} placeholder="title, condition, limit" onChange={(event) => setFieldsText(event.target.value)} /></label>
      <label className="schema-field"><span>主字段（可留空，默认第一个字段）</span><input value={primary} disabled={busy || !canEdit} onChange={(event) => setPrimary(event.target.value.trim())} /></label>
      <label className="schema-field"><span>列表字段（逗号分隔，可留空）</span><input value={listFieldsText} disabled={busy || !canEdit} onChange={(event) => setListFieldsText(event.target.value)} /></label>
      <label className="schema-field"><span>说明</span><input value={description} disabled={busy || !canEdit} onChange={(event) => setDescription(event.target.value)} /></label>
      {error && <p className="tool-hint" role="alert">{error}</p>}
      <div className="schema-actions"><button className="sort-button" disabled={busy || !canEdit} onClick={submit}>新增类型</button></div>
    </article>
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
  onPatch: (type: string, patch: SchemaPatch) => void | Promise<void>;
  onCreate: (payload: CreatePayload) => void | Promise<void>;
  onDelete: (type: string) => void;
  onInduce: () => void;
}) {
  if (schemas === null) return <p className="tool-hint">加载中…</p>;
  const proposed = schemas.filter((schema) => schema.status === "proposed");
  const managed = schemas.filter((schema) => schema.status !== "proposed");
  const globalView = view === "global";
  return (
    <div className="stack">
      {canManageGlobal && (
        <div className="tag-row" role="group" aria-label="图谱 Schema 视图">
          <button className="sort-button" aria-pressed={!globalView} disabled={busy || !globalView} onClick={() => onView("notebook")}>当前笔记本</button>
          <button className="sort-button" aria-pressed={globalView} disabled={busy || globalView} onClick={() => onView("global")}>全局基线</button>
        </div>
      )}
      <p className="tool-hint">
        {globalView
          ? "全局基线会影响所有尚未建立当前笔记本覆盖的笔记本。"
          : canEdit
            ? "这里显示当前笔记本实际采用的类型；修改全局继承项会只在当前笔记本建立覆盖。"
            : "你拥有只读权限，可以查看当前笔记本实际采用的类型。"}
      </p>
      {!globalView && canEdit && (
        <div className="tag-row">
          <button className="sort-button" disabled={busy || !canEdit} onClick={onInduce}>从当前笔记本归纳候选类型</button>
          {busy && <span className="tag">处理中…</span>}
        </div>
      )}
      {!globalView && proposed.length > 0 && (
        <>
          <p className="section-title">归纳候选（待批准）</p>
          {proposed.map((schema) => (
            <article className="item" key={schema.object_type}>
              <div className="tag-row"><strong>{schema.object_type}</strong><span className="tag">当前笔记本候选</span></div>
              {schema.rationale && <p><strong>理由：</strong>{schema.rationale}</p>}
              <p><strong>字段：</strong>{schema.fields.join(", ")}</p>
              {canEdit && schema.can_edit !== false && (
                <div className="tag-row">
                  <button className="sort-button" disabled={busy} onClick={() => onPatch(schema.object_type, { status: "active" })}>批准并启用</button>
                  <button className="sort-button" disabled={busy} onClick={() => onDelete(schema.object_type)}>拒绝</button>
                </div>
              )}
            </article>
          ))}
        </>
      )}
      <p className="section-title">{globalView ? "全局基线类型" : "当前笔记本有效类型"}（{managed.length}）</p>
      {managed.map((schema) => <SchemaRow key={schema.object_type} schema={schema} busy={busy} view={view} canEdit={canEdit} onPatch={onPatch} onDelete={onDelete} />)}
      {canEdit && (
        <NewSchemaForm
          busy={busy}
          canEdit={canEdit}
          title={globalView ? "新增全局基线类型" : "新增当前笔记本类型"}
          onCreate={onCreate}
        />
      )}
    </div>
  );
}
