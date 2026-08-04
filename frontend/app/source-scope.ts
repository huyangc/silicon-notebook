export type SourceScopePayload = {
  mode: "include" | "exclude";
  source_ids: string[];
};

export type SourceScopeSelection = {
  /** true: ids are exclusions; false: ids are inclusions. */
  allSelected: boolean;
  ids: Set<string>;
};

export const defaultSourceScopeSelection = (): SourceScopeSelection => ({
  allSelected: true,
  ids: new Set<string>(),
});

export function sourceIsSelected(
  selection: SourceScopeSelection,
  sourceId: string,
): boolean {
  return selection.allSelected
    ? !selection.ids.has(sourceId)
    : selection.ids.has(sourceId);
}

export function toggleSourceSelection(
  selection: SourceScopeSelection,
  sourceId: string,
): SourceScopeSelection {
  const ids = new Set(selection.ids);
  if (ids.has(sourceId)) ids.delete(sourceId);
  else ids.add(sourceId);
  return { ...selection, ids };
}

export function selectedSourceCount(
  selection: SourceScopeSelection,
  total: number,
): number {
  return selection.allSelected
    ? Math.max(0, total - selection.ids.size)
    : Math.min(Math.max(0, total), selection.ids.size);
}

export function sourceScopePayload(
  selection: SourceScopeSelection,
  total: number,
): SourceScopePayload {
  if (selectedSourceCount(selection, total) === 0) {
    return { mode: "include", source_ids: [] };
  }
  return {
    mode: selection.allSelected ? "exclude" : "include",
    source_ids: Array.from(selection.ids),
  };
}

/**
 * 本地那一维的检索范围是否为空 —— 后端 `_require_non_empty_scope` 里 `has_local`
 * 的镜像,逐条对齐,不另立一套判据。
 *
 * 两步:
 *  1. 这一维**被收窄了吗**(selected < total)?收窄了就以选择为准 —— 用户点了
 *     「清空」把 5 篇全取消,那就是真的空,该拦。
 *  2. 没收窄(含 0 选自 0)就按**真实证据宇宙**作答。这里不能只看来源数:Knowhow
 *     表与已确认 Memory 都没有可见来源,一个只有 Knowhow 的库 total 恒为 0,
 *     `sourceScopePayload` 因此冻结出 include:[],按来源数判就会把一个照常可搜的
 *     库判成空范围并禁用问答框(codex #431 R7 P1)。所以再问后端算好的本地证据信号
 *     (`hasLocalEvidence`),与来源数取**或** —— 只增不减:尚在解析(有来源、还没有
 *     chunk)的库仍按来源数放行,字段缺失时逐字回落到旧判据。
 */
export function localScopeIsEmpty(
  selected: number,
  total: number,
  localEvidenceAvailable: boolean,
): boolean {
  if (selected < total) return selected === 0;
  return !localEvidenceAvailable && total <= 0;
}

// ---------------------------------------------------------------------------
// 参考库检索范围
//
// 挂载的参考库过去无条件全量参与检索,勾选框只约束当前笔记本自己的来源 —— 用户
// 在挂了 84 篇论文参考库的笔记本里勾定单篇文章提问,16 条引用全部来自参考库。
//
// ⚠ 刻意与 `SourceScopeSelection` **分成两份**,不把库 id 塞进它的 `ids`:两者
// 粒度不同(整个库 vs 单篇来源)、生命周期也不同(挂载边由设置里的编辑表单管理,
// 来源由来源页签管理),混在一个集合里,任何一侧的「清空」都会连带清掉另一侧,
// 而后端 `_validate_base_scope` / `_validate_source_scope` 也是分开校验的:一个
// 来源 id 出现在 `notebook_ids` 里就是 422。
// ---------------------------------------------------------------------------

export type BaseScopePayload = {
  mode: "include" | "exclude";
  notebook_ids: string[];
};

export type BaseScopeSelection = {
  /** true: ids 是排除项;false: ids 是包含项。 */
  allSelected: boolean;
  ids: Set<string>;
};

export const defaultBaseScopeSelection = (): BaseScopeSelection => ({
  allSelected: true,
  ids: new Set<string>(),
});

export function baseIsSelected(
  selection: BaseScopeSelection,
  baseNotebookId: string,
): boolean {
  return selection.allSelected
    ? !selection.ids.has(baseNotebookId)
    : selection.ids.has(baseNotebookId);
}

export function toggleBaseSelection(
  selection: BaseScopeSelection,
  baseNotebookId: string,
): BaseScopeSelection {
  const ids = new Set(selection.ids);
  if (ids.has(baseNotebookId)) ids.delete(baseNotebookId);
  else ids.add(baseNotebookId);
  return { ...selection, ids };
}

/**
 * 当前真正会参与检索的参考库 id。
 *
 * 判据只能是**当前挂载集**,不能是选择状态自己:选择状态里可能留着一个已经被卸载
 * 的库 id(取消挂载不经过来源页签,不会触发这里的重置),而后端要求提交的每个 id
 * 都在挂载集内。
 */
export function selectedBaseIds(
  selection: BaseScopeSelection,
  mountedIds: readonly string[],
): string[] {
  return mountedIds.filter((id) => baseIsSelected(selection, id));
}

export function selectedBaseCount(
  selection: BaseScopeSelection,
  mountedIds: readonly string[],
): number {
  return selectedBaseIds(selection, mountedIds).length;
}

export function baseScopePayload(
  selection: BaseScopeSelection,
  mountedIds: readonly string[],
): BaseScopePayload {
  const selected = selectedBaseIds(selection, mountedIds);
  // 一个都没选中时归一成显式空 include —— 与 `sourceScopePayload` 同一条规则:
  // 「exclude 掉全部」和「include 空集」在后端是同一个空宇宙,但只有后者能让
  // D12 的 `_require_non_empty_scope` 一眼看出来。
  if (selected.length === 0) return { mode: "include", notebook_ids: [] };
  if (!selection.allSelected) return { mode: "include", notebook_ids: selected };
  const mounted = new Set(mountedIds);
  return {
    mode: "exclude",
    notebook_ids: Array.from(selection.ids).filter((id) => mounted.has(id)),
  };
}

/**
 * 「检索范围」的双段计数文案。
 *
 * 这句话出现在来源页签的工具条和问答输入框上方**两处**,过去各写各的字面量。
 * 收敛成一个纯函数是为了让两处不一致在结构上不可能发生 —— 它们说的是同一件事,
 * 不该有两份拼写。`bases` 为 null 表示当前笔记本没挂参考库,那一段整个不出现。
 */
export function retrievalScopeSummary(
  local: { selected: number; total: number },
  bases: { selected: number; total: number } | null,
): string {
  const localText = `本库 ${local.selected}/${local.total}`;
  if (!bases || bases.total === 0) return localText;
  return `${localText} · 参考库 ${bases.selected}/${bases.total}`;
}

export function removeSourceFromSelection(
  selection: SourceScopeSelection,
  sourceId: string,
): SourceScopeSelection {
  if (!selection.ids.has(sourceId)) return selection;
  const ids = new Set(selection.ids);
  ids.delete(sourceId);
  return { ...selection, ids };
}

/** Return the owning notebook id only when a source comes from a mounted base. */
export function crossLibrarySourceNotebookId(
  sourceNotebookId: string,
  activeNotebookId: string | null,
): string {
  if (!activeNotebookId || !sourceNotebookId) return "";
  return sourceNotebookId === activeNotebookId ? "" : sourceNotebookId;
}
