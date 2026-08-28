"use client";

import { useRef, useState } from "react";
import type { KgWorkspaceOwner } from "./kg-workspace-model";
import {
  createNotebookObjectSchema,
  createObjectSchema,
  deleteNotebookObjectSchema,
  deleteObjectSchema,
  listNotebookObjectSchemas,
  listObjectSchemas,
  proposeObjectSchemas,
  updateNotebookObjectSchema,
  updateObjectSchema,
} from "./knowledge-api";
import type { SchemaView, SchemaWriteOutcome } from "./schema-manager";
import type { KgOwnerAuthority } from "./use-kg-owner";
import type { ObjectSchema } from "./workspace-model";

// The graph object-type registry modal: which registry is being viewed
// (notebook overlay vs. admin-wide baseline), its rows, and every mutation
// on them. One of the three KG domain owners; the shared actor + notebook +
// generation gate arrives as `authority` (see `use-kg-owner.ts`).
type KgSchemaPolicy = {
  canManageNotebookSchemas: boolean;
  canManageGlobalSchemas: boolean;
};

type KgSchemaEffects = {
  notify: (message: string) => void;
  reportError: (error: unknown) => void;
};

export type UseKgSchemaOptions = {
  authority: KgOwnerAuthority;
  policy: KgSchemaPolicy;
  effects: KgSchemaEffects;
};

export function useKgSchema({ authority, policy, effects }: UseKgSchemaOptions) {
  const { currentOwner, owns, beginOperation, ownsOperation } = authority;
  const policyRef = useRef(policy);
  policyRef.current = policy;
  const effectsRef = useRef(effects);
  effectsRef.current = effects;

  const schemaRequestRef = useRef(0);

  const [schemaModalOpen, setSchemaModalOpen] = useState(false);
  const [schemas, setSchemas] = useState<ObjectSchema[] | null>(null);
  const [schemaBusy, setSchemaBusy] = useState(false);
  const [schemaView, setSchemaView] = useState<SchemaView>("notebook");
  const schemaViewRef = useRef(schemaView);
  schemaViewRef.current = schemaView;

  const clearVisibleState = () => {
    setSchemaModalOpen(false);
    setSchemas(null);
    setSchemaBusy(false);
    setSchemaView("notebook");
  };

  const invalidate = () => {
    schemaRequestRef.current += 1;
    clearVisibleState();
  };

  const loadSchemasFor = async (owner: KgWorkspaceOwner, view: SchemaView): Promise<boolean> => {
    const requestId = ++schemaRequestRef.current;
    try {
      const rows = view === "global"
        ? await listObjectSchemas()
        : await listNotebookObjectSchemas(owner.notebookId);
      if (owns(owner) && requestId === schemaRequestRef.current && schemaViewRef.current === view) {
        setSchemas(rows);
        return true;
      }
    } catch (error) {
      if (owns(owner) && requestId === schemaRequestRef.current && schemaViewRef.current === view) {
        effectsRef.current.reportError(error);
      }
    }
    return false;
  };

  const openSchemas = () => {
    const owner = currentOwner();
    if (!owner) return;
    setSchemaView("notebook");
    setSchemas(null);
    setSchemaModalOpen(true);
    void loadSchemasFor(owner, "notebook");
  };

  const closeSchemas = () => {
    schemaRequestRef.current += 1;
    setSchemaModalOpen(false);
    setSchemas(null);
    setSchemaBusy(false);
  };

  const selectSchemaView = (view: SchemaView) => {
    const owner = currentOwner();
    if (!owner || (view === "global" && !policyRef.current.canManageGlobalSchemas)) return;
    setSchemaView(view);
    setSchemas(null);
    void loadSchemasFor(owner, view);
  };

  const canWriteSchema = (view: SchemaView): boolean => view === "global"
    ? policyRef.current.canManageGlobalSchemas
    : policyRef.current.canManageNotebookSchemas;

  // 回执语义（三值，见 `SchemaWriteOutcome`）：`confirmed` 只在「写成功 + 重新拉回的
  // 清单已经进 state + 这一格仍归本次操作」三件事同时成立时返回，也就是与那句
  // `notify(...)` 逐字同一个条件。面板拿它决定保存后要不要退回只读态、新增后要不要
  // 清空表单——吞掉结果再让面板去猜，正是「新增撞重名却把输入清光」这类问题的来源。
  //
  // 关键在于中间那一档不能并进 `failed`：请求已经发出去、服务端已经写下了，只是这一格
  // 没能确认（重载失败，或者写成功之后权限/视图换了人）。并进去，界面就会对一次**已经
  // 生效**的写入说「失败，请重试」，而重试会撞重名 409；删除同理，会留下一行删不掉的
  // 陈旧条目（codex #614 R4 P2）。发起前就被挡下的那一支是真的什么都没发生，归 `failed`。
  const mutateSchema = async (
    mutation: (owner: KgWorkspaceOwner, view: SchemaView) => Promise<void>,
    success: (view: SchemaView) => string,
  ): Promise<SchemaWriteOutcome> => {
    const owner = currentOwner();
    const view = schemaViewRef.current;
    if (!owner || !schemaModalOpen || !canWriteSchema(view) || schemaBusy) return "failed";
    const operation = beginOperation("schema");
    setSchemaBusy(true);
    try {
      await mutation(owner, view);
      if (!owns(owner) || !ownsOperation("schema", operation)
        || schemaViewRef.current !== view || !canWriteSchema(view)) return "unconfirmed";
      schemaRequestRef.current += 1;
      const reloaded = await loadSchemasFor(owner, view);
      if (owns(owner) && ownsOperation("schema", operation)
        && reloaded
        && schemaViewRef.current === view && canWriteSchema(view)) {
        effectsRef.current.notify(success(view));
        return "confirmed";
      }
      return "unconfirmed";
    } catch (error) {
      if (owns(owner) && ownsOperation("schema", operation)
        && schemaViewRef.current === view && canWriteSchema(view)) {
        effectsRef.current.reportError(error);
      }
      return "failed";
    } finally {
      if (owns(owner) && ownsOperation("schema", operation)) setSchemaBusy(false);
    }
  };

  const patchSchema = (objectType: string, patch: Partial<ObjectSchema> & { status?: string }) =>
    mutateSchema(async (owner, view) => {
      if (view === "global") await updateObjectSchema(objectType, patch);
      else await updateNotebookObjectSchema(owner.notebookId, objectType, patch);
    }, () => "类型已更新");

  const createSchema = (payload: {
    object_type: string;
    plural: string;
    label: string;
    fields: string[];
    primary: string;
    list_fields: string[];
    description: string;
  }) => mutateSchema(async (owner, view) => {
    if (view === "global") await createObjectSchema(payload);
    else await createNotebookObjectSchema(owner.notebookId, payload);
  }, () => "已新增类型");

  const deleteSchema = (objectType: string) => mutateSchema(async (owner, view) => {
    if (view === "global") await deleteObjectSchema(objectType);
    else await deleteNotebookObjectSchema(owner.notebookId, objectType);
  }, (view) => view === "notebook" ? "类型已更新" : "类型已删除");

  const induceSchemas = async () => {
    const owner = currentOwner();
    if (!owner || !schemaModalOpen || !policyRef.current.canManageNotebookSchemas || schemaBusy) return;
    const operation = beginOperation("schema");
    setSchemaBusy(true);
    try {
      const proposals = await proposeObjectSchemas(owner.notebookId);
      if (!owns(owner) || !ownsOperation("schema", operation)
        || schemaViewRef.current !== "notebook" || !policyRef.current.canManageNotebookSchemas) return;
      schemaRequestRef.current += 1;
      const reloaded = await loadSchemasFor(owner, "notebook");
      if (owns(owner) && ownsOperation("schema", operation)
        && reloaded
        && schemaViewRef.current === "notebook" && policyRef.current.canManageNotebookSchemas) {
        effectsRef.current.notify(
        proposals.length
          ? `归纳出 ${proposals.length} 个候选类型`
          : "未发现可补充的新类型（或模型服务暂不可用）",
        );
      }
    } catch (error) {
      if (owns(owner) && ownsOperation("schema", operation)
        && schemaViewRef.current === "notebook" && policyRef.current.canManageNotebookSchemas) {
        effectsRef.current.reportError(error);
      }
    } finally {
      if (owns(owner) && ownsOperation("schema", operation)) setSchemaBusy(false);
    }
  };

  const visible = Boolean(currentOwner());
  return {
    view: {
      open: visible && schemaModalOpen,
      schemas: visible ? schemas : null,
      busy: visible && schemaBusy,
      view: visible ? schemaView : "notebook" as SchemaView,
    },
    clearVisibleState,
    invalidate,
    openSchemas,
    closeSchemas,
    selectSchemaView,
    patchSchema,
    createSchema,
    deleteSchema,
    induceSchemas,
  };
}
