import { Pagination } from "./Pagination";
import { useClientPagination } from "./use-client-pagination.ts";
import type { SharedByMeItem } from "./notebook-share.ts";

/** 成员名单整份返回,界面每页显示的人数。 */
const SHARE_OVERVIEW_MEMBERS_PAGE_SIZE = 20;

/**
 * 「已分享」弹窗里一本笔记本的只读链接成员名单(人数不设上限)。每本笔记本各自一份
 * 页码状态,所以是一个组件而不是 sharedByMeList.map 里的内联片段。
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
