import type { ObjectSchema } from "./workspace-model.ts";

// 「图谱 Schema」面板的纯逻辑:校验、草稿、分组与各种标签。
//
// 单独成文件的理由与 `knowhow-*-logic.ts` / `effort-picker-logic.ts` 同一条:面板本身
// 被重排成「清单 + 详情」两栏之后,判据(哪些类型能删、保存按钮该叫什么、草稿脏没脏)
// 会被清单列、详情只读态、编辑表单三处同时读到。留在组件里就会各写一份、慢慢分叉。

export type SchemaView = "notebook" | "global";

/** 一份类型定义的可编辑投影。字段用逗号分隔的文本编辑，提交前才切成数组。 */
export type SchemaDraft = {
  label: string;
  plural: string;
  fieldsText: string;
  primary: string;
  listFieldsText: string;
  description: string;
};

/** 新建时多一个类型标识；已有类型的标识不可改，所以不在 `SchemaDraft` 里。 */
export type CreateDraft = SchemaDraft & { objectType: string };

export const EMPTY_CREATE_DRAFT: CreateDraft = {
  objectType: "",
  label: "",
  plural: "",
  fieldsText: "",
  primary: "",
  listFieldsText: "",
  description: "",
};

const IDENTIFIER = /^[a-z][a-z0-9_]*$/;
const MAX_FIELDS = 64;
const MAX_NAME_CHARS = 80;
const MAX_TEXT_CHARS = 2000;

export function splitFields(value: string): string[] {
  return value.split(",").map((field) => field.trim()).filter(Boolean);
}

export function draftFromSchema(schema: ObjectSchema): SchemaDraft {
  return {
    label: schema.label,
    plural: schema.plural,
    fieldsText: schema.fields.join(", "),
    primary: schema.primary,
    listFieldsText: schema.list_fields.join(", "),
    description: schema.description,
  };
}

/**
 * 草稿与服务端定义是否已经不一致。
 *
 * 这条判据同时承担两件事,所以它必须逐字对比而不是「有没有草稿」:①编辑态的保存按钮
 * 是否可按;②清单里那颗「未保存」圆点是否要亮。保存成功后服务端定义会变得与刚提交的
 * 草稿逐字相同,于是同一条判据自动把圆点熄掉——不需要再写一条「保存后清草稿」的路径,
 * 也就不存在「清早了把用户续打的字一起吞掉」的窗口。
 */
export function draftIsDirty(schema: ObjectSchema, draft: SchemaDraft): boolean {
  const base = draftFromSchema(schema);
  return base.label !== draft.label
    || base.plural !== draft.plural
    || base.fieldsText !== draft.fieldsText
    || base.primary !== draft.primary
    || base.listFieldsText !== draft.listFieldsText
    || base.description !== draft.description;
}

export function validateDefinition(
  objectType: string,
  fields: string[],
  primary: string,
  listFields: string[],
): string {
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

export function validateTexts(values: string[]): string {
  return values.some((value) => value.trim().length > MAX_TEXT_CHARS)
    ? `显示名、复数名称和说明均不能超过 ${MAX_TEXT_CHARS} 个字符。`
    : "";
}

/** 编辑既有类型:标识不可改，主字段照原样送出（留空由后端沿用既有值）。 */
export function validateDraft(objectType: string, draft: SchemaDraft): string {
  return validateDefinition(objectType, splitFields(draft.fieldsText), draft.primary, splitFields(draft.listFieldsText))
    || validateTexts([draft.label, draft.plural, draft.description]);
}

/** 新建时主字段可留空，默认取第一个字段——这条默认必须与提交的载荷一致。 */
export function resolveCreatePrimary(draft: CreateDraft): string {
  return draft.primary.trim() || splitFields(draft.fieldsText)[0] || "";
}

export function validateCreateDraft(draft: CreateDraft): string {
  const objectType = draft.objectType.trim();
  if (!objectType) return "请填写类型标识。";
  return validateDefinition(
    objectType,
    splitFields(draft.fieldsText),
    resolveCreatePrimary(draft),
    splitFields(draft.listFieldsText),
  ) || validateTexts([draft.label, draft.plural, draft.description]);
}

/**
 * 这份定义从哪来、写下去会落到哪——详情面板里说全的那一版。
 *
 * 候选先判:一条还没批准的候选在数据上确实既不继承也不覆盖,照常规判据算下来会被
 * 标成「当前笔记本自建」——而它根本还没被采纳,那句话是假的。
 */
export function placementLabel(schema: ObjectSchema, view: SchemaView): string {
  if (schema.status === "proposed") return "归纳候选";
  if (view === "global") return schema.source === "builtin" ? "内置类型" : "全局基线";
  if (schema.inherited) return "全局继承";
  if (schema.overrides_global) return "当前笔记本覆盖";
  return "当前笔记本自建";
}

/**
 * 清单列里的短徽章。与 `placementLabel` 同一判据、同一顺序,只是短到能和类型名同排。
 * 长版本在详情面板恒可见,行上还挂 title,所以缩写不会变成只有作者读得懂的暗号。
 */
export function placementBadge(schema: ObjectSchema, view: SchemaView): string {
  if (schema.status === "proposed") return "候选";
  if (view === "global") return schema.source === "builtin" ? "内置" : "自定义";
  if (schema.inherited) return "继承";
  if (schema.overrides_global) return "覆盖";
  return "自建";
}

/**
 * 清单行的身份。**不是** `object_type`——同一个类型在当前笔记本视图下最多有两行。
 *
 * 后端刻意如此:一条还没批准的候选在批准前**不遮蔽**继承来的同名类型(见
 * `docs/product-and-api_zh.md` 里 `POST /schema-proposals` 那段),于是
 * `list_notebook_object_schemas` 对这种情况同时返回继承行(active)与候选行(proposed),
 * 并把 active 排在前面。只按类型名认行,`find` 必然命中那条继承行:两行同时显示为
 * 选中,而候选的归纳理由与批准/拒绝按钮永远够不着——审批那条路直接断掉。
 *
 * 两行的区别只有一个:是不是候选。所以身份就是「类型 + 是不是候选」。
 */
export function schemaRowKey(schema: ObjectSchema): string {
  return `${schema.status === "proposed" ? "proposed" : "managed"}:${schema.object_type}`;
}

/** 某个类型**非候选**那一行的身份。批准会把一行从候选变成生效类型，身份随之改变。 */
export function managedRowKey(objectType: string): string {
  return `managed:${objectType}`;
}

export function statusLabel(status: string): string {
  if (status === "active") return "已启用";
  if (status === "proposed") return "待批准";
  return "已停用";
}

/**
 * 状态徽章的配色分档。与 `statusLabel` 同一组取值、同一顺序,拆成两函数是因为一个
 * 出文案、一个出类名——旧版这里挂的是 `severity-low`,而 `globals.css` 里根本没有
 * 这条规则(只有 severity-high / severity-medium),于是「已启用」徽章多年来一直以
 * 默认 `.tag` 样式裸奔,没有任何门禁会红。
 */
export function statusTone(status: string): string {
  if (status === "active") return "is-on";
  if (status === "proposed") return "is-pending";
  return "is-off";
}

/** 全局视图不能删内置类型；当前笔记本视图不能删纯继承来的（它本来就不在本库）。 */
export function removable(schema: ObjectSchema, view: SchemaView): boolean {
  return view === "notebook" ? !schema.inherited : schema.source !== "builtin";
}

/** 删掉一条本库覆盖等于恢复继承，按钮就该这么说，而不是统一叫「删除」。 */
export function deleteActionLabel(schema: ObjectSchema, view: SchemaView): string {
  return view === "notebook" && schema.overrides_global ? "恢复全局" : "删除";
}

/** 改继承类型是 copy-on-write，按钮要提前说清这一点，别让用户以为改的是全局。 */
export function saveActionLabel(schema: ObjectSchema, view: SchemaView): string {
  return view === "notebook" && schema.inherited ? "保存并建立覆盖" : "保存";
}

export function toggleActionLabel(schema: ObjectSchema, view: SchemaView): string {
  const base = schema.status === "active" ? "停用" : "启用";
  return view === "notebook" && schema.inherited ? `${base}并建立覆盖` : base;
}

export type SchemaGroupKey = "proposed" | "active" | "disabled";

export type SchemaGroup = {
  key: SchemaGroupKey;
  title: string;
  hint: string;
  rows: ObjectSchema[];
};

/**
 * 清单分三组:待批准的候选 / 生效中 / 已停用。
 *
 * 分组本身就是这次重排要解决的问题——原来候选、生效、停用三种语义完全不同的行按
 * 服务端顺序混在一条竖列里,每行还各自摊开成六个输入框,唯一的区别只是一枚小徽章。
 * 组内保持服务端顺序(内置在前),不另外排序:同一个库两次打开面板顺序应当一致。
 */
export function groupSchemas(schemas: readonly ObjectSchema[]): SchemaGroup[] {
  const pick = (predicate: (schema: ObjectSchema) => boolean) => schemas.filter(predicate);
  return [
    {
      key: "proposed",
      title: "待批准的候选",
      hint: "批准后才参与分析",
      rows: pick((schema) => schema.status === "proposed"),
    },
    {
      key: "active",
      title: "生效中",
      hint: "分析时实际会用到",
      rows: pick((schema) => schema.status === "active"),
    },
    {
      key: "disabled",
      title: "已停用",
      hint: "保留定义，不再分析",
      rows: pick((schema) => schema.status !== "active" && schema.status !== "proposed"),
    },
  ];
}
