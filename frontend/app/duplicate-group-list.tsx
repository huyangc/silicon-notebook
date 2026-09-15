import { LatexText } from "./answer-panel";
import { Pagination } from "./Pagination";
import { useClientPagination, type PaginationResetKey } from "./use-client-pagination.ts";
import { label, KNOWLEDGE_STATUS } from "./vocabulary";
import type { DuplicateGroup } from "./workspace-model";

/** 查重结果整份返回,界面每页显示的重复组数。 */
const DUPLICATE_GROUP_PAGE_SIZE = 20;

/**
 * 知识浏览器里的「查重」结果:一批重复组,每组内可以把非第一条合并进第一条。
 * `resetKey` 传当前 kind——切换知识类型 tab 时,前一个 tab 翻到的页码不该带过来。
 */
export function DuplicateGroupList({
  duplicates,
  readOnly,
  mergingId,
  onMerge,
  resetKey,
}: {
  duplicates: DuplicateGroup[];
  readOnly?: boolean;
  mergingId: string | null;
  onMerge: (sourceId: string, intoId: string) => void;
  resetKey?: PaginationResetKey;
}) {
  const page = useClientPagination(duplicates, DUPLICATE_GROUP_PAGE_SIZE, resetKey);
  return (
    <>
      {page.pageItems.map((group, index) => (
        <article className="item" key={`dup-${index}`}>
          <div className="tag-row"><span className="tag">similarity {group.similarity}</span></div>
          {group.members.map((member, memberIndex) => (
            <div className="dup-member" key={member.id}>
              <span><LatexText text={member.headline} isFormula={(member.object_type || group.object_type) === "formula"} /> <span className="tag">{label(KNOWLEDGE_STATUS, member.status, "其他")}</span></span>
              {!readOnly && memberIndex > 0 && (
                <button
                  className="sort-button"
                  disabled={mergingId !== null}
                  onClick={() => onMerge(member.id, group.members[0].id)}
                >
                  {mergingId === member.id ? "合并中…" : "合并到第 1 条"}
                </button>
              )}
            </div>
          ))}
        </article>
      ))}
      <Pagination page={page.page} pageSize={DUPLICATE_GROUP_PAGE_SIZE} total={page.total} onPage={page.setPage} label="重复组分页" />
    </>
  );
}
