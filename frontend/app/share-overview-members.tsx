import { Pagination } from "./Pagination";
import { useClientPagination } from "./use-client-pagination.ts";
import type { SharedByMeItem } from "./notebook-share.ts";

/** 「其余清单」共用的页大小(20)——接口整份返回、契约上不分页,分页只发生在界面。 */
const SHARE_OVERVIEW_MEMBERS_PAGE_SIZE = 20;

/**
 * 「已分享」弹窗里一本笔记本的只读成员名单,只读链接分享才会出现,人数不设上限。
 * 单独成一个组件而不是内联在 sharedByMeList.map 里:分页要用 useClientPagination,
 * 这个 hook 不能在 .map 回调里调(每一项的调用次数会随清单长度变化),必须落在一个
 * 真正的组件里,每个笔记本各自一份页码状态。
 *
 * 单独成文件是 Next.js App Router 的硬约束:`app/page.tsx` 是路由文件,只能有
 * `default` 等少数几个白名单导出,多导出一个具名组件会让 `next build` 的类型检查
 * (`.next/types/app/page.ts`)报错——这个组件需要被组件测试直接渲染,只能挪出来。
 */
export function ShareOverviewMembers({
  members,
  notebookName,
}: {
  members: SharedByMeItem["members"];
  notebookName: string;
}) {
  const page = useClientPagination(members, SHARE_OVERVIEW_MEMBERS_PAGE_SIZE);
  return (
    <>
      {page.pageItems.map((member) => (
        <span className="share-member-chip" key={member.username}>{member.username}</span>
      ))}
      <Pagination page={page.page} pageSize={SHARE_OVERVIEW_MEMBERS_PAGE_SIZE} total={page.total} onPage={page.setPage} label={`《${notebookName}》的成员分页`} />
    </>
  );
}
