"""对比检索原语:焦点实体 → 兄弟实体名(横向对比候选)。

两条数据源,统一由 resolve_comparison_peers「共提优先、社区回退」编排:
 · sibling_peers(P2):concept_comentions 跨源共提对,直接查表零 LLM、静默 fail-open;
 · community_peers:base 库 Louvain 社区 + 廉价词法重排,miss 时 emit community_unavailable
   事件(绝不静默零召回)。
两路焦点解析共用同一归一化 + UnifiedKgStore.resolve_focal(Task 13:本模块只保留
编排/重排/事件,持久化读一律走注入的 CommunityQueryService.unified_kg)。

收窄有两道,两道都在「名字被取出来」之前:库维度在 mounted_base_ids,来源维度
(逐库冻结来源天花板)在 _source_ceiling_kwargs —— 后者由两个 peers 函数各自经
_ceiling_kwargs 下推给 store,做成 SQL 谓词;同一个入口顺带发那条 source_index_
fallback 事件(闸在 SQL 里、事件在服务层)。

来源闸只回答「这个兄弟簇留不留」,不回答「它叫什么」:存储的 canonical_name 可能是
天花板外成员(隐藏的 Knowhow 投影 / Memory)的写法,也可能是那个成员已被剪掉后留下
的陈旧名字。所以天花板在绑时,名字再过一道 R3(_ceiling_peer_names):名字只取自
自己的 source_id 在该库天花板内的成员,一个都没有的簇丢弃。共提与社区两路在同一处
出口、按同一规则改名,改名发生在名字被用于任何事情(社区重排、去重、top_k、返回、
事件)之前;无天花板时一条语句都不多发。"""
from __future__ import annotations
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

# PEER mode: the participant-set predicate. This module is reader #5 on the
# participant override's frozen whitelist (see ``mounted_base_ids``).
from app.services.retrieval_participants import federated_ask_active
from app.services.retrieval_run import current_viewer_id

# R3 成员读的首窗口:一次调用里所有兄弟簇各读 member-id 序的前这么多个活成员,
# 一条批量语句;只有整窗都在天花板外的簇才单独往后翻页(窗口翻倍)。与
# ``kg_viewer_scope._FIRST_MEMBER_WINDOW`` 同值同理(那边是「第一个可见成员」)。
# 它不是结果的截断:翻页一直走到找到天花板内成员或读完整簇为止。它只决定「哪些
# 天花板内成员算作已读到」——存储名由已读到的天花板内成员携带时保留存储名,两种
# 答案都只来自天花板内成员。
_PEER_MEMBER_WINDOW = 8


class CommunityQueryService:
    def __init__(self, *, notebooks, unified_kg, event_log,
                 sibling_min_bridge: int = 2) -> None:
        self.notebooks = notebooks
        self.unified_kg = unified_kg
        self.event_log = event_log
        self.sibling_min_bridge = int(sibling_min_bridge)

    def mounted_base_ids(self, active_notebook_id: str) -> List[str]:
        """挂载参考库 id,按本 run 的参考库勾选收窄。

        这是横向对比(共提/社区)唯一的库枚举入口:reasoning 的 ``expand_community``
        与 chunk 模式的对比子查询各自逐库调 ``resolve_comparison_peers``。收窄放在
        这里而不是两个调用点,是因为它们问的是同一个问题——「本轮可以向哪些参考库
        借同类实体」。

        必须收窄:兄弟**实体名**是从那个库的内容里读出来的,它们会原样进可见轨迹、
        进 ``used_queries``,并被回喂进 reflect prompt —— 「取消勾选的库一个字都不
        进 prompt」在这条通道上就不成立了。结果由 ``search`` 过滤并不足以补救:
        泄漏的是**查询词本身**,不是命中。

        判据必须是库维度而不是 ``source_scope_restricted()``:只取消参考库时后者恒为
        False(R1),这条通道会全程敞着。

        参与集覆盖在场时这份库清单来自覆盖集而不是挂载表(本模块是覆盖模块冻结
        白名单上的读者 #5)。顺序固定:覆盖**替换**集合,``scoped_participants``
        再按库勾选**收窄**。座位的形状是「active 在前、其余是参考库」,所以单库
        模式把首项剥掉;fallback 也照这个形状拼出来,免得两条分支对「首项是谁」
        各有一套理解。

        **对等模式(D0-5)不剥首项。** 剥掉首项的理由是「当前库自己不是自己的
        参考库」——那条理由要有一个主体库才成立。对等 run 的首项只是
        ``ParticipantOverride.notebook_ids[0]`` 这个命名锚点,用户逐个选的 8 个
        库里它没有任何特殊地位;继续剥掉等于「唯独这一库不许被问到兄弟实体」,
        是一条反向的特权(名义 active 的内容不参与横向对比)。不剥也不会重复计:
        两个调用点都是**逐库**调 ``resolve_comparison_peers(base_nb, …)``,每一轮
        只查那一个库自己的共提/社区行,拿回来的名字再按名字去重
        (``if pname not in peers`` / ``not in sub_queries``),所以多一个库只是
        多一轮查询,不会让任何一个名字被计两次;它也不会把焦点实体自己当兄弟
        ——那由 ``comention_peers``/``community_member_peers`` 的 ``focal`` 参数
        在 SQL 侧排除,与是哪个库无关。

        这里只答**库维度**。逐库冻结来源天花板在绑时,一本仍在这份清单里的库
        还可能有「只由天花板之外的来源(典型是隐藏 Memory / Knowhow 投影)支撑」
        的实体,或者名字是天花板外成员写法的实体——**来源级闸与 R3 改名都在
        名字出口**,见 ``_source_ceiling_kwargs``、``_ceiling_peer_names`` 与
        ``sibling_peers`` / ``community_peers``;不在这里,也不在两个调用点。
        """
        from app.services.retrieval_participants import (
            federated_ask_active,
            resolve_retrieval_participant_ids,
        )
        from app.services.source_scope import scoped_participants

        participants = resolve_retrieval_participant_ids(
            active_notebook_id,
            lambda: (
                active_notebook_id,
                *self.unified_kg.mounted_base_ids(
                    active_notebook_id, viewer_id=current_viewer_id(),
                ),
            ),
        )
        return list(scoped_participants(
            participants if federated_ask_active() else participants[1:]
        ))

    def community_peers(self, base_notebook_id: str, focal_name: str,
                        question: str, *, top_k: int,
                        candidates: int) -> List[str]:
        from app.services.retrieval import keyword_score
        key = _norm(focal_name)
        if not base_notebook_id or not key:
            return []
        focal = _resolve_focal(
            self.unified_kg, base_notebook_id, focal_name
        )
        if not focal:
            self.event_log.emit({
                "kind": "community_unavailable",
                "notebook_id": base_notebook_id,
                "reason": "focal_unresolved",
                "focal": focal_name,
            })
            return []
        community_id = self.unified_kg.top_community_for(
            base_notebook_id, focal
        )
        if not community_id:
            self.event_log.emit({
                "kind": "community_unavailable",
                "notebook_id": base_notebook_id,
                "reason": "not_built",
                "focal": focal_name,
            })
            return []
        ceiling = self._ceiling_kwargs(base_notebook_id)
        rows = self.unified_kg.community_member_peers(
            base_notebook_id, community_id, focal, candidates,
            **_peer_read_kwargs(ceiling),
        )
        if ceiling:
            # R3:先改名(或丢弃),再重排 / 去重 / 截 top_k —— 排序键
            # ``keyword_score`` 读的也必须是天花板内成员的名字。
            named = self._ceiling_peer_names(base_notebook_id, [
                (str(row["canonical_id"]), row["canonical_name"] or "")
                for row in rows
            ])
            rows = [
                {"canonical_name": named[str(row["canonical_id"])],
                 "centrality": row["centrality"]}
                for row in rows if str(row["canonical_id"]) in named
            ]
        ranked = sorted(
            rows,
            key=lambda row: (
                keyword_score(question, row["canonical_name"] or ""),
                row["centrality"],
            ),
            reverse=True,
        )
        seen, out = set(), []
        for row in ranked:
            name = (row["canonical_name"] or "").strip()
            normalized = _norm(name)
            if name and normalized not in seen:
                seen.add(normalized)
                out.append(name)
            if len(out) >= top_k:
                break
        return out

    def sibling_peers(self, notebook_id: str, focal_name: str, *,
                      top_k: int = 8) -> List[Tuple[str, int]]:
        try:
            focal = _resolve_focal(
                self.unified_kg, notebook_id, focal_name
            )
            if not focal:
                return []
            ceiling = self._ceiling_kwargs(notebook_id)
            peers = self.unified_kg.comention_peers(
                notebook_id, focal, self.sibling_min_bridge, top_k,
                **_peer_read_kwargs(ceiling),
            )
            if not ceiling:
                return peers
            # R3:与社区路同一个出口。成员读失败时落进下面的 except,返回 []
            # (一个名字都不出),由调用方回退社区路。
            named = self._ceiling_peer_names(
                notebook_id, [(cid, name) for cid, name, _claims in peers])
            return [
                (named[cid], claims)
                for cid, _name, claims in peers if named.get(cid)
            ]
        except Exception:
            return []

    def _ceiling_kwargs(self, notebook_id: str) -> dict:
        """``_source_ceiling_kwargs`` 加一条可观测性:闸落在**未回填库**上时发事件。

        为什么要这条信号:闸有两支(见 ``_object_support_exists``)。反向索引
        ``knowledge_object_sources`` 已回填的库走窄索引读;没回填的库退回扫
        ``knowledge_objects.evidence`` JSON 的权威支——语义相同、成本完全不同,
        而且从外面一个字都看不出来。词法臂在同一个判断点上早就发
        ``source_index_fallback``(``retrieval_candidates`` 的
        ``site="kg_source_scoped_fts"``),这里是同一族事件的第二个站点。

        ``store 层不拿 event_log``,所以判断点在 SQL 里、事件在这里,代价是多一次
        短查询——只在天花板真的在绑时发生(今天生产恒不绑,零成本)。

        ``memoized_retrieval_value``:每次 run、每个库至多问一次,也就至多一条
        事件。run 缺席(直调 store 的测试/脚本)时退化为每次调用都问一次,这是
        可观测性的 fail-open 面,不改任何检索结果。
        """
        kwargs = _source_ceiling_kwargs(notebook_id)
        if kwargs:
            self._note_source_index_fallback(notebook_id)
        return kwargs

    def _note_source_index_fallback(self, notebook_id: str) -> None:
        from app.services.retrieval_run import memoized_retrieval_value

        def _probe() -> bool:
            try:
                if self.unified_kg.source_index_backfilled(notebook_id):
                    return False
                self.event_log.emit({
                    "kind": "source_index_fallback",
                    "notebook_id": notebook_id,
                    "site": "comparison_peer_source_ceiling",
                })
                return True
            except Exception:
                # 内容无关的可观测性,fail-open:它绝不能把一次对比检索变成异常
                # (``sibling_peers`` 的 except 会把异常吞成「这库没有兄弟」)。
                return False

        memoized_retrieval_value(
            ("comparison_peer_source_index_fallback", str(notebook_id)), _probe,
        )

    def _ceiling_peer_names(
        self, notebook_id: str, peers: Sequence[Tuple[str, str]],
    ) -> Dict[str, str]:
        """R3:天花板在绑时,经来源闸留下的兄弟簇各自该叫什么。

        ``peers`` 是 ``(canonical_id, 存储的 canonical_name)``;返回
        ``canonical_id -> 名字``,没有天花板内成员的簇**不在**返回里(丢弃)。

        规则:按 member-id 序读该簇的活成员(已发布代、对象未 deprecated),
        「天花板内成员」= 自己的 ``knowledge_objects.source_id`` 在该库冻结
        天花板里的成员(不看 evidence:来源闸已经按 evidence 判过「留不留」,
        名字要的是「这个写法属于谁」)。读到的天花板内成员(读到第一个天花板内
        成员所在的那一页为止)里有人的名字与存储名逐字相同 → 存储名;否则 →
        第一个天花板内成员的名字;一个都没有 → 丢弃。存储名因此只在它确实是
        某个天花板内成员的写法时才出去——既挡住天花板外成员的写法,也挡住
        携带它的成员已被剪掉后留下的陈旧名字。

        成本:**一条**批量语句读本次调用全部兄弟簇的首窗口
        (``_PEER_MEMBER_WINDOW`` 个成员,``comparison_peer_members``),不随兄弟数
        增长;只有首窗口整窗都在天花板外的簇才单独往后翻页
        (``_scan_peer_members``,同 ``KgViewerScope._first_visible_members`` /
        ``_scan_members``)。天花板集合取自本 run 冻结的那一份
        (``library_source_ceiling``,按 run 记忆,不复制、不排序)。无天花板时
        调用方根本不进这里。
        """
        stored: Dict[str, str] = {}
        for canonical_id, name in peers:
            stored.setdefault(str(canonical_id), name or "")
        if not stored:
            return {}
        allowed = _library_ceiling(notebook_id)
        members: Dict[str, list] = {cid: [] for cid in stored}
        for row in self.unified_kg.comparison_peer_members(
            notebook_id, list(stored), limit=_PEER_MEMBER_WINDOW,
        ):
            members.setdefault(str(row["canonical_id"]), []).append(row)
        named: Dict[str, str] = {}
        for canonical_id, stored_name in stored.items():
            page = members.get(canonical_id, [])
            name = _in_ceiling_name(page, stored_name, allowed)
            if name is None and len(page) >= _PEER_MEMBER_WINDOW:
                name = self._scan_peer_members(
                    notebook_id, canonical_id, stored_name, allowed,
                    after=str(page[-1]["member_object_id"]),
                    window=2 * _PEER_MEMBER_WINDOW,
                )
            if name is not None:
                named[canonical_id] = name
        return named

    def _scan_peer_members(
        self, notebook_id: str, canonical_id: str, stored_name: str,
        allowed: FrozenSet[str], *, after: str, window: int,
    ) -> Optional[str]:
        """R3 的单簇翻页:keyset 游标 ``after`` 之后,每页 ``window`` 个成员、
        整页都在天花板外就翻倍再读。终止:每个满页都把游标严格前移,短页即
        读完;读完仍没有天花板内成员 → ``None``(丢弃,fail closed)。"""
        while True:
            rows = self.unified_kg.comparison_peer_members(
                notebook_id, [canonical_id], limit=window, after=after,
            )
            name = _in_ceiling_name(rows, stored_name, allowed)
            if name is not None or len(rows) < window:
                return name
            after = str(rows[-1]["member_object_id"])
            window *= 2

    def comparison_peer_names(self, base_notebook_ids, focal_name: str,
                              question: str, *, top_k: int, candidates: int,
                              cap_factor: int) -> List[str]:
        """每个库的兄弟实体名,去重后交给调用方。

        **对等模式**(一次跨库提问)额外做两件事:按库轮转、截到总量帽以内。
        chunk 模式的对比子查询过去没有任何总量帽,参与集 8 个库 ×
        ``community_peers_topk`` 就是 64 条子查询,每条再按参与库扇出;帽值复用
        reasoning 侧的同一个表达式(``community_peers_topk × cap_factor``),不
        另发明旋钮。截断按轮转而不是 ``peers[:cap]``:逐库拼接后直接切前 N 条,
        会把整个配额发给遍历顺序最靠前的那一两个库,后面的库一个名字都进不了
        子查询——多选一个库反而让它自己的兄弟实体被挤掉。

        **单库模式逐值不变**,而且是刻意的:一个挂了三个参考库的笔记本今天就是
        「逐库顺序追加、无帽」,轮转会重排它的子查询顺序、帽会截掉它本来拿得到
        的名字。那是一次**产品行为改动**,不该搭着一次跨库接线的便车发出去;
        这里要解决的拥挤是参与集带来的,所以闸也只在参与集在场时合上。

        确定性:库序取自调用方给的 ``base_notebook_ids``(``mounted_base_ids``
        的确定性顺序),库内序取自 ``resolve_comparison_peers`` 的名次序,去重按
        首次出现。没有 set/dict 遍历顺序参与。

        取数是**按需**的:对等模式下达到帽即停止解析后面的库——轮转语义保证每个
        库至少被问过第一轮,所以提前停止只会省掉「反正也装不下」的那几次查询,
        不会让某个库整个失声。
        """
        peer_mode = federated_ask_active()
        cap = max(0, int(top_k) * int(cap_factor)) if peer_mode else None
        per_library: List[List[str]] = []
        for base_nb in base_notebook_ids:
            per_library.append(self.resolve_comparison_peers(
                base_nb, focal_name, question,
                top_k=top_k, candidates=candidates,
            )[0])
            if cap is not None and sum(
                min(len(peers), 1) for peers in per_library
            ) >= cap:
                # 每个库已经至少被问过一轮,而第一轮就已经填满帽:后面的库无论
                # 交出什么都装不下,查询它们只是纯成本。
                break
        names: List[str] = []
        if cap is None:
            for peers in per_library:
                for name in peers:
                    if name not in names:
                        names.append(name)
            return names
        for rank in range(max((len(peers) for peers in per_library), default=0)):
            for peers in per_library:
                if len(names) >= cap:
                    return names
                if rank < len(peers) and peers[rank] not in names:
                    names.append(peers[rank])
        return names

    def resolve_comparison_peers(self, base_notebook_id: str, focal_name: str,
                                 question: str, *, top_k: int,
                                 candidates: int) -> Tuple[List[str], str]:
        siblings = self.sibling_peers(
            base_notebook_id, focal_name, top_k=top_k
        )
        if siblings:
            return [name for name, _claims in siblings], "comention"
        return self.community_peers(
            base_notebook_id, focal_name, question,
            top_k=top_k, candidates=candidates,
        ), "community"


def _source_ceiling_kwargs(notebook_id: str) -> dict:
    """兄弟实体名的**来源级闸**:这一库这次 run 的冻结来源天花板,转成 store kwargs。

    为什么闸在这里:``mounted_base_ids`` 只答库维度。一本仍在参与集里的库,
    若某实体只由该库天花板**之外**的来源(典型是隐藏 Memory / Knowhow 投影)
    支撑,它的**名字**照样会经共提/社区成员行出来,进 ``ask_chunk`` 的
    ``sub_queries`` 与 reasoning 的 ``_action_expand_community``,并原样进
    ``used_queries`` 与可见轨迹、被发给 embedding 与模型。``mounted_base_ids``
    的 docstring 已经写明这条通道泄漏的是**查询词本身**——结果侧过滤补救不了,
    所以裁剪必须发生在名字被取出来之前,也就是 SQL 侧。

    为什么闸在两个 peers 函数里、不在两个调用点:两条对比路径(chunk 的子查询
    扩展、reasoning 的 ``expand_community``)问的是同一个问题,闸放在名字的唯一
    出口才自然保证它们拿到同一份裁剪结果。

    这道闸只决定兄弟簇**留不留**;留下的簇**叫什么**由同一出口的 R3 决定
    (``CommunityQueryService._ceiling_peer_names``):kwargs 非空即天花板在绑,
    两个 peers 函数据此多要 canonical id、再按天花板内成员改名。

    ``None`` 与空集是两个不同的答案,必须按 ``is None`` 分(见
    ``ActiveSourceScope.source_ceiling_for``):
      · 没有天花板 → ``{}``,store 一个参数都不多收,SQL 与今天逐字相同、
        不多发任何查询——这是「零行为变化」的落点(R3 也不跑);
      · 有天花板 → ``allowed_source_ids=<清单>``,空集即 deny all(该库一个
        名字都不出),store 侧对空清单直接返回 []。
    """
    from app.services.source_scope import _sorted_library_ceiling, current_source_scope

    scope = current_source_scope()
    ceiling = None if scope is None else scope.source_ceiling_for(notebook_id)
    if ceiling is None:
        return {}
    # The per-scope ordered hand-out (``_library_ceiling_memo``): a default
    # ceiling's mounted library comes back in the order its one read produced,
    # anything else is sorted once per scope -- never ``sorted`` of ~49k ids
    # on every call, which is what this function used to pay per peer query.
    # ``_sorted_library_ceiling`` answers ``frozenset()`` for a library the
    # library dimension excludes; ``mounted_base_ids`` never hands one here.
    return {"allowed_source_ids": list(_sorted_library_ceiling(scope, notebook_id) or ())}


def _peer_read_kwargs(ceiling: dict) -> dict:
    """两个兄弟读的 store kwargs:无天花板 → ``{}``(语句与结果逐字不变);
    有天花板 → 闸清单 + ``with_canonical_id``(R3 按 canonical id 读成员)。"""
    return {**ceiling, "with_canonical_id": True} if ceiling else {}


def _library_ceiling(notebook_id: str) -> FrozenSet[str]:
    """R3 判成员用的天花板集合:与 ``_source_ceiling_kwargs`` 交给 SQL 闸的是
    同一份(``library_source_ceiling``,本 run 内按库记忆,per-library 天花板原样
    返回、不复制)。只在 ``_ceiling_kwargs`` 非空时被调;万一 scope 已不在,按
    空集判(一个名字都不出,fail closed)。"""
    from app.services.source_scope import current_source_scope, library_source_ceiling

    scope = current_source_scope()
    ceiling = None if scope is None else library_source_ceiling(scope, notebook_id)
    return frozenset() if ceiling is None else ceiling


def _member_name(row) -> str:
    value = row["member_name"]
    return "" if value is None else str(value)


def _in_ceiling_name(rows, stored_name: str, allowed: FrozenSet[str]) -> Optional[str]:
    """R3 在一页成员上的判定:这页的天花板内成员(自己的 ``source_id`` 在
    ``allowed`` 里)里有人叫存储名 → 存储名;否则第一个天花板内成员的名字;
    这页一个天花板内成员都没有 → ``None``(由调用方决定翻页还是丢弃)。"""
    inside = [
        _member_name(row) for row in rows
        if row["source_id"] is not None and str(row["source_id"]) in allowed
    ]
    if not inside:
        return None
    return stored_name if stored_name in inside else inside[0]


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _resolve_focal(store, notebook_id: str, focal_name: str) -> Optional[str]:
    """focal 名 → canonical_id(共提/社区两路共用的焦点解析:lower(canonical_name)==_norm(focal),
    多簇取成员最多者)。入参空 / 解析不到 → None。**不 emit 事件**——sibling_peers 走静默、
    community_peers 拿到 None 后自行补 community_unavailable 事件。"""
    key = _norm(focal_name)
    if not notebook_id or not key:
        return None
    return store.resolve_focal(notebook_id, key)


def community_peers(queries, base_nb: str, focal_name: str, query: str, *,
                    top_k: int, candidates: int) -> List[str]:
    return queries.community_peers(
        base_nb, focal_name, query, top_k=top_k, candidates=candidates
    )


def sibling_peers(queries, notebook_id: str, focal_name: str, *,
                  top_k: int = 8) -> List[Tuple[str, int]]:
    """共提兄弟:focal → canonical → concept_comentions 两侧按 bridge_claims 降序取对端名。

    P2 数据源(Task 3 的 concept_comentions,claim 文本确定性抽取的跨源共提对);相比
    Louvain community_peers(实测其把同型号/同族聚在一起,而非「同类可比对象」),共提对更贴
    横向对比语义。**纯查表、零 LLM、版本无关**(直接读表,不依赖任何 rebuild 时点);
    sibling_min_bridge 以下的弱共提对丢弃。

    notebook_id 语义:本原语 notebook 无关——查哪个库的 concept_comentions 由调用方决定。
    resolve_comparison_peers 传的是与 community_peers **同一个 BASE 库 id**(调用方对
    mounted_base_ids 的结果逐个传入),使共提/社区两路口径一致、prefer/fallback 是 like-for-like;
    未来其它调用方可指向活动库自身。返回 [(name, bridge_claims), ...] 降序:无天花板时
    name 是存储的 canonical_name;该库天花板在绑时 name 按 R3 取自天花板内成员、没有
    天花板内成员的对端丢弃(``CommunityQueryService._ceiling_peer_names``,社区路同一规则)。
    任何异常 / 焦点解析不到 / 无共提数据 → [](静默,不 emit;由调用方回退社区路径兜底文案)。"""
    return queries.sibling_peers(
        notebook_id, focal_name, top_k=top_k
    )


def resolve_comparison_peers(queries, base_nb: str, focal_name: str, query: str, *,
                             top_k: int, candidates: int) -> Tuple[List[str], str]:
    """对比兄弟解析(两处对比调用点共享):共提优先、社区回退。

    返回 (names, source),source ∈ {"comention", "community"}:
      · 先 sibling_peers(共提兄弟,零 LLM 直接查表);非空 → 用其名单,source="comention"。
      · 空 → 回退 community_peers(Louvain 社区),source="community",行为与今日逐字一致。
    两路都查同一个 BASE 库 id(调用方对 mounted_base_ids 逐个传入)→ 口径一致 like-for-like。
    两路的名字在各自出口过同一道 R3(该库天花板在绑时只用天花板内成员的名字),所以 chunk
    子查询与 reasoning 轨迹拿到的是同一份名字。
    sibling_peers 内部 fail-open→[] 时自动回退。**不吞 community_peers 异常**——与既有两调用点
    保持一致(expand_community 的 try/except、ask_chunk 的无兜底,都仍在各自调用点)。"""
    siblings = sibling_peers(
        queries, base_nb, focal_name, top_k=top_k
    )
    if siblings:
        return [name for name, _claims in siblings], "comention"
    return community_peers(
        queries, base_nb, focal_name, query,
        top_k=top_k, candidates=candidates,
    ), "community"
