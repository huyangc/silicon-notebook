import { LatexText } from "./answer-panel";
import { Pagination } from "./Pagination";
import { useClientPagination } from "./use-client-pagination.ts";
import { label, KNOWLEDGE_STATUS } from "./vocabulary";
import type { DuplicateGroup } from "./workspace-model";

/** 「其余清单」共用的页大小(20)——接口整份返回、契约上不分页,分页只发生在界面。 */
const DUPLICATE_GROUP_PAGE_SIZE = 20;

/**
 * 知识浏览器里的「查重」结果:一批重复组,每组内可以把非第一条合并进第一条。
 * 单独成组件是分页的硬要求(useClientPagination 不能在 duplicates.map 回调里调),
 * `resetKey` 传当前 kind——切换知识类型 tab 时前一个 tab 翻到的页码不该带过来
 * (虽然实际上 duplicates 本身也会在切 tab 时被父层置空,这里是双重保险)。
 *
 * 单独成文件是 Next.js App Router 的硬约束:`app/page.tsx` 是路由文件,只能有
 * `default` 等少数几个白名单导出,多导出一个具名组件会让 `next build` 的类型检查
 * (`.next/types/app/page.ts`)报错——这个组件需要被组件测试直接渲染,只能挪出来。
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
  resetKey?: unknown;
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
