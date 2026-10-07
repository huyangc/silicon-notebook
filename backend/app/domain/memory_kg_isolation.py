"""M1 写侧:个人记忆派生的知识对象只属于它的主人,不与任何其他对象合并、不经通用路径
进公共库;含个人记忆的笔记本不能发布为公共库。

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
* 两个都是调用者本人的 Memory 派生 → 同样拒绝,`SAME_OWNER_MESSAGE`。理由:合并会把
  一条 Memory 的证据并进另一条 Memory 的对象,而删除/编辑/转移一条 Memory 时按证据
  引用清理对象(`clear_source_graph_state`),于是动其中一条会连带删掉另一条 Memory 的
  对象——正是 N-1 的形状,只是发生在同一个人名下;也破坏「Memory 对象永远是单例、
  删除只删到该 Memory 自己的对象」这条不变量(计划 §2 D4、§3)。

本模块的每个字符串常量都是界面文案(路由原样交给 `user_error`,前端原样上屏),
`tests/test_memory_kg_isolation_vocabulary.py` 逐条跑界面词汇守卫的 `terms_in`:动态
`user_error` 站点不经过静态扫描,这条测试补上那个口子。
"""

from __future__ import annotations

CROSS_CLASS_MESSAGE = "由个人记忆生成的知识对象不能与共享的知识对象合并"
SAME_OWNER_MESSAGE = "由个人记忆生成的知识对象不能相互合并"

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
    """`merge_objects_in_transaction` 拒绝合并调用者本人的 Memory 派生对象。

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


def memory_merge_refusal(source_derived: bool, into_derived: bool) -> str | None:
    """返回应拒绝时的用户文案,可以合并时返回 None。两个后端共用这一份规则。

    调用前提:两行都已通过 `foreign_memory_object_excluded(调用者)`,所以任何一侧
    是 Memory 派生,就一定是调用者本人的。"""
    if not source_derived and not into_derived:
        return None
    if source_derived and into_derived:
        return SAME_OWNER_MESSAGE
    return CROSS_CLASS_MESSAGE
