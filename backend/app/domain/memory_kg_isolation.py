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

鉴权分两条路(用户裁决 2026-10-22):有 `knowledge:write` 的调用者照上面的规则合并;
只有读权的成员只能合并「两边都是她本人、证据只出自她本人记忆」的一对——判据就是上面
第三条,由 store 在同一条加锁语句读出的两行上判定(`own_pair_only=True`),不是本人对就
抛 `NeedsKnowledgeWrite`,任何写入之前。路由把只读成员的每一种拒绝都答成读守卫
那句 404,不透露另一侧对象存不存在、是什么。`PATCH` 同样分两条路:只读成员只能把这样一个
本人对象的状态在 approved 与 deprecated 之间改(撤销或重做她的合并,
`update_object_in_transaction(own_status_only=True)`),其余一律同一个 404。镜像库上读权这两条
路都不开(与 knowledge:write 的围栏同口径)。本人的重复组(`own_memory_duplicate_seed_rows`)在 SQL
里逐个对象判同一条(`evidence_only_from_own_memory` 是它的 Python 孪生,差分用例钉住两者一致),
所以列出来的每一对都正是合并会接受的。

放开同主人合并之后,「删除/编辑一条 Memory 只删到该 Memory 自己的对象」由两处维持:
删除/转移/退出走 `remove_memory_sources`,先把该来源的证据从别的来源拥有的对象上
摘掉(`strip_sources_evidence_on`)再拆;编辑后重抽(`ingest_memory_source`)在清旧
产物之前做同一次摘除。被合并的另一条 Memory 的对象因此只失去这条 Memory 贡献的证据,
不会随它一起被删。

反过来,拥有合并结果的那条 Memory 被编辑、转移或删除时,合并结果随它清掉(用户裁决
2026-10-21:被并走那条 Memory 的知识不能因此静默消失)。记录在被并走的那一行自己身上:
本人两条 Memory 对象合并时,被并走对象的 payload 写入 `MERGED_INTO_PAYLOAD_KEY`
(= 目标 id;以 `_` 开头的 payload 键不展示、不进检索文本,不需要迁移)。它再被并进
别处时覆盖为最新目标;手动改它的状态或内容(PATCH)时清掉——主人手动弃用的对象因此
永远不会被自动恢复。共享对象之间的合并不写它(行与证据逐字节不变)。清掉 Memory 来源的
对象之前(同一事务、同一处 `detach_memory_merges_on`),标记指向这些对象的弃用对象按
`plan_merge_follow_actions` 处理:被清掉的目标仍在用 → 恢复为 approved、去掉标记,
重新进入检索;目标本身也已被并走 → 标记改指它的下一层(每清掉一层只恢复一层,a1→a2→a3
清 a2 时 a1 改指 a3,清 a3 时才恢复);目标是手动弃用的 → 只去掉标记。只处理自己的
Memory 仍在的对象。#824 之前的合并没有标记,无法与手动弃用区分,不自动恢复。合并同时在
目标上写 `ABSORBS_PAYLOAD_KEY` 门闸:被清掉的对象里没有带它的,就不读跟随者——没有任何
合并的普通 Memory 删除与编辑只多付前置锁这一条语句。

PostgreSQL 上这一步对被处理对象的 Memory 来源取 `FOR KEY SHARE SKIP LOCKED`,再按 id 序
锁对象行:拿不到锁的(持锁者可能是那条 Memory 的删除或重抽,也可能是关系补全、抽取
发布、元素替换,或一个随后回滚的事务)不在事务里等,提交后由服务层在独立短事务里补跑
(同一锁序,幂等:行已不在、标记已变则跳过)。补跑在删除/编辑的请求线程上,等待有上限
(`SET LOCAL lock_timeout`,几秒);超时就把这本笔记本交给一次后台兜底(等得更久),
再不行留给下次启动的兜底扫描。兜底(`heal_merged_away_candidates`,只读连接,先取 Memory
来源、再按来源取对象,代价随 Memory 量而不随全表)按自愈判据「弃用、标记指向的对象已不在、
自己的 Memory 仍在」挑出候选,并用 `heal_action` 复核:别的来源的对象仍带着它自己的全部
证据条目(链上它本该被改指去的那一层),就改指过去而不是恢复——兜底因此不会把同一份
知识恢复成两份。拆除开始时用一条按 id 排序的语句锁住「本来源自有对象 ∪ 摘证据
候选」(`lock_memory_teardown_objects_on`),再摘证据、再删除,所以两条 Memory 交叉合并后
同时删除不会死锁。PostgreSQL 上合并先按 id 序对源对象的主来源与证据引用的 Memory
来源取 `FOR KEY SHARE`,再锁两个对象——与拆除、重抽「先来源、后派生行」同一锁序,
所以并发的删除/退出/编辑要么排在合并之后(看得到合并写进的证据并摘掉),要么排在
之前(合并读到的是已摘净的行)。

本模块的每个字符串常量都是界面文案(路由原样交给 `user_error`,前端原样上屏),
`tests/test_memory_kg_isolation_vocabulary.py` 逐条跑界面词汇守卫的 `terms_in`:动态
`user_error` 站点不经过静态扫描,这条测试补上那个口子。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AbstractSet, Any, Callable, Iterable, Mapping

from app.domain.promotion_provenance import evidence_source_ids

CROSS_CLASS_MESSAGE = "由个人记忆生成的知识对象不能与共享的知识对象合并"
#: PostgreSQL 上合并读到的源对象证据在加锁前后不一致(另一个合并刚把新的 Memory 证据
#: 并进了它):这次不合并、一行不写,重试即可。
MEMORY_MERGE_RETRY_MESSAGE = "要合并的知识对象刚被修改，请稍后重试"

#: 本人两条 Memory 对象合并时写进**被并走对象** payload 的键,值是合并目标的 id(见模块
#: docstring)。以 `_` 开头:`knowledge_record`、`payload_join`、检索文本与词法索引都跳过
#: 这类键,所以它不展示、不进检索,也不需要新列或迁移。
MERGED_INTO_PAYLOAD_KEY = "_merged_into"

#: 本人两条 Memory 对象合并时写进**目标** payload 的门闸:`true` 表示「可能有对象
#: 并进过它」。清掉一条 Memory 来源时,只有它自己的对象里有带这个键的,才去读跟随者
#: (`merge_followers_on`),所以没有任何合并的普通 Memory 删除与编辑不付这次读。它不会被
#: 合并以外的写入清除(PATCH 保留它):残留为 true 只多一次读,从不会漏处理。
ABSORBS_PAYLOAD_KEY = "_absorbs"

#: `MergeFollowAction.kind` 的三个取值。
FOLLOW_RESTORE = "restore"
FOLLOW_REPOINT = "repoint"
FOLLOW_FORGET = "forget"

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


class NeedsKnowledgeWrite(LookupError):
    """A caller without ``knowledge:write`` asked for a knowledge write the read
    level does not cover (user rulings 2026-10-22): a merge of a pair that is not
    two of her own Memory objects supported by her own Memory alone
    (``merge_objects_in_transaction(own_pair_only=True)``), or a ``PATCH`` that
    is not a status change between approved and deprecated of such an object of
    hers (``update_object_in_transaction(own_status_only=True)``).

    Raised on the rows the locking read returned, before anything is written, so
    the read-level path can never write anything else. The route answers it
    exactly as it answers that caller for every other refusal: the read guard's
    404, the body a missing notebook gets, so nothing tells her whether the
    object exists or what it is."""


#: The two statuses a read-only member may move her own Memory object between
#: (``PATCH …/knowledge/{id}`` on the read path: undoing or redoing a merge).
OWN_STATUS_TOGGLE = frozenset({"approved", "deprecated"})


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


@dataclass(frozen=True)
class MergeFollowAction:
    """一条被并走对象在它的合并目标被清掉时要做的事。

    `expected_target` 是做这件事的前提:行仍是弃用状态、标记仍等于它(补跑与兜底扫描
    据此做成幂等,行已被改过就什么也不做)。`kind`:`FOLLOW_RESTORE` 恢复为 approved
    并去掉标记;`FOLLOW_REPOINT` 把标记改成 `repoint_to`;`FOLLOW_FORGET` 只去掉标记。
    """

    notebook_id: str
    object_id: str
    expected_target: str
    kind: str
    repoint_to: str = ""


@dataclass(frozen=True)
class MemoryMergeDetach:
    """`detach_memory_merges_on` 的结果:事务内已做的,以及因拿不到锁而跳过、要在提交后
    带等待补跑的(只有 PostgreSQL 会有)。"""

    applied: tuple = ()
    skipped: tuple = ()


def plan_merge_follow_actions(
    notebook_id: str,
    followers: Iterable[tuple[str, str]],
    cleared: Mapping[str, tuple[str, str]],
) -> list[MergeFollowAction]:
    """`followers` 是 `(对象 id, 它的标记)`,标记指向 `cleared` 里某个将被清掉的对象;
    `cleared` 是这些对象的 `id → (status, 它自己的标记)`。沿标记往下走过同样要被清掉
    的层:停在一个仍在用的(恢复)、手动弃用的(只去标记)或不在 `cleared` 里的对象
    (改指它)。遇到环一律恢复。两个后端共用这一份规则。"""
    actions: list[MergeFollowAction] = []
    for object_id, marker in followers:
        seen = {object_id}
        current = marker
        kind, repoint_to = FOLLOW_RESTORE, ""
        while current in cleared and current not in seen:
            seen.add(current)
            status, onward = cleared[current]
            if status != "deprecated":
                kind = FOLLOW_RESTORE
                break
            if not onward:
                kind = FOLLOW_FORGET
                break
            if onward in seen:
                kind = FOLLOW_RESTORE
                break
            if onward not in cleared:
                kind, repoint_to = FOLLOW_REPOINT, onward
                break
            current = onward
        actions.append(
            MergeFollowAction(notebook_id, object_id, marker, kind, repoint_to)
        )
    return actions


def followed_payload(payload: Mapping[str, Any], action: MergeFollowAction) -> dict:
    """按 `action` 改写一行的 payload(恢复与去标记删掉键,改指写新目标)。"""
    result = {k: v for k, v in payload.items() if k != MERGED_INTO_PAYLOAD_KEY}
    if action.kind == FOLLOW_REPOINT:
        result[MERGED_INTO_PAYLOAD_KEY] = action.repoint_to
    return result


def without_merge_marker(payload: Mapping[str, Any]) -> dict:
    """去掉合并标记(恢复、去标记时用)。"""
    return {k: v for k, v in payload.items() if k != MERGED_INTO_PAYLOAD_KEY}


def patched_payload(new: Mapping[str, Any], old: Mapping[str, Any]) -> dict:
    """手动修改(PATCH 状态或内容)之后的 payload:两个合并键都只由合并写入——
    调用方传来的一律丢掉,`MERGED_INTO_PAYLOAD_KEY` 因此被清掉(手动弃用的对象不会被
    自动恢复),目标的 `ABSORBS_PAYLOAD_KEY` 从旧行原样带过来(界面编辑内容时提交的
    payload 不含 `_` 开头的键,不保留它会让以后的清除漏掉跟随者)。"""
    result = {
        k: v for k, v in new.items()
        if k not in (MERGED_INTO_PAYLOAD_KEY, ABSORBS_PAYLOAD_KEY)
    }
    if old.get(ABSORBS_PAYLOAD_KEY):
        result[ABSORBS_PAYLOAD_KEY] = True
    return result


def marked_merged_into(payload: Mapping[str, Any], into_id: str) -> dict:
    """本人 Memory 对之间合并时被并走对象的新 payload(覆盖旧标记)。"""
    return {**payload, MERGED_INTO_PAYLOAD_KEY: into_id}


def marked_absorbs(payload: Mapping[str, Any]) -> dict:
    """本人 Memory 对之间合并时目标的新 payload(打开跟随者门闸)。"""
    return {**payload, ABSORBS_PAYLOAD_KEY: True}


def evidence_keys(evidence: Iterable[Any]) -> frozenset:
    """证据条目的去重键 `(element_id, quoted_span)`——合并按它去重,兜底按它判断
    一个被并走对象的条目是否仍在别的对象上。"""
    return frozenset(
        (item.get("element_id"), item.get("quoted_span"))
        for item in evidence if isinstance(item, dict)
    )


def merge_target_eligible(status: str, payload: Any) -> bool:
    """兜底改指的目标是否合格:仍在用且带门闸(本版本起的合并目标),或弃用且自己带
    合并标记(链上一层,它被清掉时会继续沿链处理)。"""
    if not isinstance(payload, Mapping):
        return False
    if status == "deprecated":
        return bool(payload.get(MERGED_INTO_PAYLOAD_KEY))
    return bool(payload.get(ABSORBS_PAYLOAD_KEY))


def heal_action(
    notebook_id: str,
    object_id: str,
    marker: str,
    own_evidence: Iterable[Any],
    holders: Iterable[tuple[str, str, Any, Any]],
) -> MergeFollowAction:
    """启动兜底对一个候选(弃用、标记指向的对象已不在、自己的 Memory 仍在)的复核:
    `holders` 是 `(id, status, evidence, payload)`——别的来源里引用了它来源的对象。
    仍有一个**合格**的对象带着它自己的全部条目(它的知识还在那里),就改指过去(优先
    仍在用的),否则才恢复。合格只有两种(`merge_target_eligible`):仍在用、带
    `ABSORBS_PAYLOAD_KEY` 的合并目标;或弃用、自己带 `MERGED_INTO_PAYLOAD_KEY` 的链上
    一层。主人手动弃用、不带标记的对象不合格——指向它,候选就永远不会再被处理。证据为空时
    无从比对,恢复。这样兜底永远不会把同一份知识恢复成两份。"""
    keys = evidence_keys(own_evidence)
    if keys:
        carrying = sorted(
            (status == "deprecated", holder_id)
            for holder_id, status, evidence, payload in holders
            if holder_id != object_id
            and merge_target_eligible(status, payload)
            and keys <= evidence_keys(evidence)
        )
        if carrying:
            return MergeFollowAction(
                notebook_id, object_id, marker, FOLLOW_REPOINT, carrying[0][1]
            )
    return MergeFollowAction(notebook_id, object_id, marker, FOLLOW_RESTORE)


def merge_evidence_source_ids(*evidence_lists: Iterable[Any]) -> frozenset[str] | None:
    """几份证据引用的来源 id(`promotion_provenance.evidence_source_ids` 的读法);有一条
    不是带非空 `source_id` 的对象时返回 None——这样的条目无从证明出自本人的 Memory,
    `only_own_memory_evidence` 因此判假(那个读法会把它跳过)。"""
    items = [item for evidence in evidence_lists for item in evidence]
    if not all(isinstance(item, dict) and str(item.get("source_id") or "") for item in items):
        return None
    return frozenset(evidence_source_ids(items))


def evidence_only_from_own_memory(
    evidence: Iterable[Any], own_memory_source_ids: AbstractSet[str],
) -> bool:
    """One object's half of the merge's ``only_own_memory_evidence``: every entry
    cites one of ``own_memory_source_ids`` (the caller's own Memory sources of the
    notebook, read with ``own_memory_source``). Read the way the merge reads it
    (``merge_evidence_source_ids``): an entry without a source id fails it, and
    empty evidence passes. Two of the caller's own Memory objects merge exactly
    when both pass (the merge judges the union of both evidence lists, which is
    the same test). The owner's duplicate groups judge it in SQL
    (``_only_own_memory_evidence_sql`` in both governance stores); this is the
    Python twin the differential test (``tests/own_memory_dedup_narrowing_cases``)
    holds them to, so they list only pairs the merge accepts."""
    cited = merge_evidence_source_ids(evidence)
    return cited is not None and cited <= own_memory_source_ids
