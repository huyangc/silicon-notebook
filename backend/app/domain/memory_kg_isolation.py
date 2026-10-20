"""M1 写侧:个人记忆派生的知识对象只属于它的主人,只能与同一主人、同样只由其 Memory
支撑的对象合并,不经通用路径进公共库;含个人记忆的笔记本不能发布为公共库。

「派生自 Memory」的判据只有一处定义(`repositories/*/memory_sql.py` 的
`memory_derived_object`:主 `source_id` 指向 `source_type='memory'` 的来源);「这是
**别人**的 Memory」也只有一处定义(同模块的 `foreign_memory_object_excluded` /
`foreign_memory_relation_excluded`,带调用者 id 一个参数;孤儿 Memory 来源——记忆行已
不存在——对任何人都算别人的)。这里只放写侧拒绝的**结果类型**与它们携带的用户文案,
让 store(在事务内判定)与路由(映射成 409)共用同一个名字,而服务层不必 import 仓储。

**存在性不外泄**(F3):凡是按对象/关系 id 写的路由,调用者不是该 Memory 派生对象的
主人时,store 在加锁读里就把这一行当作不存在(`foreign_memory_*_excluded`),路由回
与「id 不存在」逐字相同的 404。只有主人本人会看到下面带原因的 409。

合并规则(两个后端的 `merge_objects_in_transaction` 在锁住两行的同一条语句里求值;
走到这里时两行都已通过「不是别人的 Memory」过滤):

* 两个对象都不是 Memory 派生 → 照常合并;
* 恰有一个是(调用者本人的)Memory 派生 → `CROSS_CLASS_MESSAGE`;
* 两个都是调用者本人的 Memory 派生,且两份证据的每一条都出自调用者本人在本库的
  Memory 来源(「只由本人的 Memory 支撑」,`only_own_memory_evidence`)→ 照常合并
  (用户裁决 2026-10-21,计划 §13.2 第 9 条)。合并结果的主来源仍是目标对象的 Memory
  来源,所以它照旧只对主人可见;证据只是两份本人 Memory 证据的并集。
* 两个都是 Memory 派生,但任一份证据里有一条不出自调用者本人的 Memory(普通来源、
  别人的 Memory、缺来源 id——隔离上线前手工合并留下的混合证据)→ 同样是
  `CROSS_CLASS_MESSAGE`:那条对象带着共享证据,等同于公共对象。

放开同主人合并之后,「删除/编辑一条 Memory 只删到该 Memory 自己的对象」由两处维持:
删除/转移/退出走 `remove_memory_sources`,先把该来源的证据从别的来源拥有的对象上
摘掉(`strip_sources_evidence_on`)再拆;编辑后重抽(`ingest_memory_source`)在清旧
产物之前做同一次摘除。被合并的另一条 Memory 的对象因此只失去这条 Memory 贡献的证据,
不会随它一起被删。PostgreSQL 上合并先按 id 序对源对象的主来源与证据引用的 Memory
来源取 `FOR KEY SHARE`,再锁两个对象——与拆除、重抽「先来源、后派生行」同一锁序,
所以并发的删除/退出/编辑要么排在合并之后(看得到合并写进的证据并摘掉),要么排在
之前(合并读到的是已摘净的行)。

本模块的每个字符串常量都是界面文案(路由原样交给 `user_error`,前端原样上屏),
`tests/test_memory_kg_isolation_vocabulary.py` 逐条跑界面词汇守卫的 `terms_in`:动态
`user_error` 站点不经过静态扫描,这条测试补上那个口子。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from app.domain.promotion_provenance import evidence_source_ids

CROSS_CLASS_MESSAGE = "由个人记忆生成的知识对象不能与共享的知识对象合并"
#: PostgreSQL 上合并读到的源对象证据在加锁前后不一致(另一个合并刚把新的 Memory 证据
#: 并进了它):这次不合并、一行不写,重试即可。
MEMORY_MERGE_RETRY_MESSAGE = "要合并的知识对象刚被修改，请稍后重试"

# 贡献到公共知识库(通用路径 `propose_promotion` → `approve_promotion_in_transaction`)。
# Memory 进公共库只有一条路:创建者本人的 `propose_memory_promotion`,它只带经过
# `safe_memory_evidence` 裁剪的引用卡。通用路径会把对象 payload 与原始证据(Memory
# 原文摘录)整份拷进公共库,并可能并进某个公共对象的证据,所以对 Memory 派生对象
# 一律拒绝:提交时拒绝(不入队),审批时在锁住候选行之后、任何写入之前复核
# (本修复之前已入队、或与提交竞态的申请)。
PROMOTION_PROPOSE_MESSAGE = (
    "由个人记忆生成的知识对象不能在这里申请贡献到公共知识库，请在你的记忆里提交"
)
PROMOTION_APPROVE_MESSAGE = "这条贡献申请指向由个人记忆生成的知识对象，已自动关闭"
#: 审批时被拒的申请写进 `promotion_candidates.reason` 的机器码(无 UI 渲染 reason)。
MEMORY_PROMOTION_REJECTED_REASON = "memory_derived_object"

# 审批时申请指向的知识对象已不存在(Q5:例如来源重新分析后对象换了新 id)。与
# 「申请本身不存在」(404)分开:申请在同一事务里关闭为已驳回,回 409。
PROMOTION_OBJECT_MISSING_MESSAGE = "这条贡献申请对应的知识对象已不存在，申请已自动关闭"
PROMOTION_OBJECT_MISSING_REASON = "object_missing"

# 发布为公共知识库(`set_notebook_tier` → `mark_notebook_base`):公共库可被任何笔记本
# 挂载,成员个人记忆生成的知识对象不能随之公开。
PUBLISH_HOLDS_MEMORY_MESSAGE = (
    "这本笔记本里还有成员的个人记忆，不能发布为公共知识库；请先让成员转移或删除自己的记忆"
)


class MemoryKnowledgeMergeRefused(ValueError):
    """`merge_objects_in_transaction` 拒绝一次涉及调用者本人 Memory 派生对象的合并
    (跨类,或 PostgreSQL 上读到的源对象证据在加锁前后变了)。

    `user_message` 是完整的中文用户文案,路由原样交给 `user_error(409, ...)`。
    继承 `ValueError`:未专门处理它的调用方仍把它当作无效请求。
    """

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class PromotionRefused(ValueError):
    """通用贡献路径拒绝一条申请,并带上关闭它时写进 `reason` 的机器码。

    `user_message` 是完整的中文用户文案,路由原样交给 `user_error(409, ...)`。
    审批时:store 的 `approve_promotion_in_transaction` 在任何写入之前抛出;服务层在
    **同一个**写事务里把该申请标为 rejected(`reason`)后再抛给路由。
    """

    def __init__(self, user_message: str, reason: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.reason = reason


class MemoryPromotionRefused(PromotionRefused):
    """对象由个人记忆生成(提交时:调用者是主人;审批时:任何申请)。"""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message, MEMORY_PROMOTION_REJECTED_REASON)


class PromotionObjectMissing(PromotionRefused):
    """审批时申请指向的知识对象已不存在(申请本身存在)。"""

    def __init__(self) -> None:
        super().__init__(
            PROMOTION_OBJECT_MISSING_MESSAGE, PROMOTION_OBJECT_MISSING_REASON
        )


class NotebookHoldsMemory(ValueError):
    """发布为公共知识库被拒:这本笔记本里还有 Memory 来源。"""

    def __init__(self) -> None:
        super().__init__(PUBLISH_HOLDS_MEMORY_MESSAGE)
        self.user_message = PUBLISH_HOLDS_MEMORY_MESSAGE


def memory_merge_refusal(
    source_derived: bool,
    into_derived: bool,
    only_own_memory_evidence: Callable[[], bool],
) -> str | None:
    """返回应拒绝时的用户文案,可以合并时返回 None。两个后端共用这一份规则。

    调用前提:两行都已通过 `foreign_memory_object_excluded(调用者)`,所以任何一侧
    是 Memory 派生,就一定是调用者本人的。`only_own_memory_evidence` 只在两侧都是
    Memory 派生时调用(它要多读一次来源表):两份证据的每一条都出自调用者本人在本库的
    Memory 来源时为真。"""
    if not source_derived and not into_derived:
        return None
    if source_derived and into_derived and only_own_memory_evidence():
        return None
    return CROSS_CLASS_MESSAGE


def merge_evidence_source_ids(*evidence_lists: Iterable[Any]) -> frozenset[str] | None:
    """几份证据引用的来源 id(`promotion_provenance.evidence_source_ids` 的读法);有一条
    不是带非空 `source_id` 的对象时返回 None——这样的条目无从证明出自本人的 Memory,
    `only_own_memory_evidence` 因此判假(那个读法会把它跳过)。"""
    items = [item for evidence in evidence_lists for item in evidence]
    if not all(isinstance(item, dict) and str(item.get("source_id") or "") for item in items):
        return None
    return frozenset(evidence_source_ids(items))
