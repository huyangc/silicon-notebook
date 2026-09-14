"""按节合成的确定性部分(PR-3 O2,设计文档 §3.1)。

这里只有纯逻辑:把 O1 产出的终态大纲切成「每节一份证据」,给每节分配一段互不
相交的 `[k]` 命名空间,再把各节文本拼回一篇答案。模型调用留在
``ask_service._answer_reasoning_sections`` —— 这样切片/偏移/拼接可以脱开
AskService、脱开模型替身单测,而「哪一节失败要整体回退」那条控制流留在它真正
发生的地方。

DualGraph 借鉴的产出侧动机:一次性把全部证据喂给合成模型会 lost-in-the-middle,
按节喂只让模型在写这一节时看见这一节绑上的证据。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence


# 每节的 `[k]` 编号基址步长。合成上下文里既有的分区基址(chunk 0 / KG 1000 /
# chains 2000 / Memory 3000 / element 4000 / 集合清单 5000)全部 < 6000,所以
# 「第 i 节整体加 i × 10000」保证任意两节的 key 号段不相交:合并后的锚点集合里
# 一个 `[k]` 只可能来自一节。
#
# 有界性:大纲封顶 12 节(`_OUTLINE_MAX_SECTIONS`),最大基址 110000,而 key 号
# 只在 prompt 文本与锚点 key 里出现,没有任何上限约束。
#
# 步长若被去掉(或缩到既有基址之内),第 2 节的 chunk 就会和第 1 节的 chunk 抢
# `k1`,合并锚点时后者覆盖前者——答案里两处不同的证据指向同一条来源,而且不会
# 报错,只会静静地把引用指错。回归门见 test_reasoning_outline_synthesis.py。
OUTLINE_SECTION_KEY_STRIDE = 10000


@dataclass
class OutlineSectionSlice:
    """一节及其绑定证据的切片(装配一次合成调用所需的全部输入)。"""

    section: Any                    # reasoning_retrieval.OutlineSection
    index: int                      # 在**实际合成**序列里的 0 基序号(=文档序)
    key_offset: int
    # **解析后**的父节 id:空串 = 顶层节(``##``),非空 = 子节(``###``)。
    # 与 ``section.parent`` 的区别只有一种情况,但那一种必须区分开:父节本身是空节、
    # 在上面被跳过了。那时 ``section.parent`` 仍指着一个不会出现在答案里的 id,照它
    # 渲染就是一个没有父标题的孤儿 ``###``。这里把它晋升为顶层。
    parent_id: str = ""
    hits: list = field(default_factory=list)        # RetrievedKnowledge
    elements: list = field(default_factory=list)    # RetrievedElement
    chunks: list = field(default_factory=list)      # RetrievedChunk
    # 未被任何节绑定、由 ``exact_reserve`` 注入的精确命中副本。**刻意与
    # ``chunks`` 分开**:它们不是本节的证据,只是兜底材料,装配位置也不同
    # (见 ``plan_outline_sections`` docstring 末段)。
    exact_chunks: list = field(default_factory=list)  # RetrievedChunk

    @property
    def evidence_count(self) -> int:
        # 只数绑定证据。注入块不是证据(没有任何节「要」过它们),把它们计进来
        # 会让一节的证据规模凭空虚报 reserve 条。
        return len(self.hits) + len(self.elements) + len(self.chunks)


def _document_order(kept: Sequence[Any]) -> list[tuple[Any, str]]:
    """把幸存的节排成**文档序**,并返回每节解析后的父 id。

    模型提交大纲用的是全量替换语义,顺序完全由它自己决定 ——
    `parse_outline_sections` 明确允许「先写子节再写父节」(它的两层裁剪刻意在收齐
    所有节之后才做,正是为了不把合法的前向引用误判成非法)。所以提交序里
    `### 子节` 完全可能排在它的 `## 父节` 前面,照提交序渲染就会出现一个层级倒挂的
    答案 —— Markdown 里那是个语义错误的文档结构,前端 h2/h3 会照它渲染。

    规则:顶层节按提交序;每个顶层节之后紧跟它的全部子节(子节之间保提交序)。
    父节缺席(空节被跳过)的子节按顶层节处理,原地参与顶层排序。

    只有两层,所以不需要递归 —— `parse_outline_sections` 已经保证父节自己没有父节。
    """
    surviving = {section.id for section in kept}
    children: dict[str, list[Any]] = {}
    roots: list[Any] = []
    for section in kept:
        parent = getattr(section, "parent", "") or ""
        if parent and parent != section.id and parent in surviving:
            children.setdefault(parent, []).append(section)
        else:
            roots.append(section)
    ordered: list[tuple[Any, str]] = []
    for root in roots:
        ordered.append((root, ""))
        for child in children.get(root.id, ()):
            ordered.append((child, root.id))
    return ordered


def plan_outline_sections(
    sections: Sequence[Any],
    *,
    kg_by_id: Mapping[str, Any],
    element_by_id: Mapping[str, Any],
    chunk_by_id: Mapping[str, Any],
    exact_reserve: int = 0,
) -> tuple[list[OutlineSectionSlice], list[str]]:
    """把终态大纲切成 (可合成的切片, 被跳过的节标题)。切片按**文档序**排列。

    绑定键的三个来源(知识对象 id / 元素 id / 原文段 id)是互不相交的代理 id 空间
    (128 位随机 + 各自前缀),所以按 map 依次查找不会误判;查不到的键只可能是本轮
    池子里已经没有的东西,静默略过 —— O1 的服务端校验保证键在**绑定那一刻**合法,
    而三个候选池只增不减,所以正常情况下每个键都查得到。

    **空节保留为跳过项而不是错误**:空节是「问到了但还没找到」的诚实记录(O1 合同),
    它没有可写的证据,硬要合成只会让模型凭常识编一节。被跳过的标题回到 trace,
    用户才看得见「这一节没写」。

    绑定键解析后仍没有任何证据的节同样跳过:那意味着这一节实际上是空的,与
    `evidence_keys` 为空等价。

    **重排在这里做,而不是只在渲染时做**(超出评审要求的那一点,理由是机制性的):
    序号与号段偏移都按这里的顺序分配,于是节级 prompt 里的「(2 of 5)」、进度轨迹步
    的「第 2/共 5 节」和答案里那一节的实际位置说的是同一件事。只在渲染时重排的话,
    这三处会各说各的 —— 模型被告知在写第 2 节,读者却在第 4 个标题下读到它。

    ``exact_reserve``:**未被任何节绑定**的精确命中(用户在问题里逐字点名的那些
    段落,``RetrievedChunk.exact_lookup``)按 ``chunk_by_id`` 迭代序取前 N 条,放进
    **每一节**切片的 ``exact_chunks``。缺省 0 = 不注入,此时 ``exact_chunks`` 为
    空,切片其余内容、顺序、``evidence_count`` 与 ``skipped`` 逐字节等于接这个
    参数之前。

    **为什么镜像外部证据的规则**:``_answer_reasoning_sections`` 已经立了同形的先例
    ——外部材料不由大纲绑定,却是本轮唯一「库里查不到」的东西,于是每一节都装同一
    份。精确命中是这条规则的另一半:它是本轮唯一「用户亲口点名」的东西。模型建大纲
    时按语义把节绑在知识对象与元素上,一段用户指名要看的原文可能一个 ``evidence_key``
    也没拿到;不注入的话,单次合成路径上被前缀席位保住的那段原文,在按节路径上会
    整段消失——同一个问题,只因为大纲够到了两节就读不到自己点名的段落。所以这与
    单次合成的前缀席位(``retrieval.promote_bounded_prefix``)是同一个意图,只是换了
    一条装配路径。

    **只注入未绑定的**:已经绑到某一节的精确块留在那一节,不复制到别处。同一个块在
    两节里会拿到两个不同号段的 ``[k]``,合并锚点后就是两条指向同一段原文的引用——
    读者看见的是「两处不同证据」,而实际只有一处。绑定关系是模型的判断,它比这里的
    兜底更准,不该被兜底覆盖。

    **只注入到幸存节**:空节的判定在注入之前、只按该节自己绑定的证据做。否则「问到
    了但没找到」那条诚实记录会被一段跟它毫无关系的精确块填成一节真节,模型拿着这段
    原文在一个它本来答不了的标题下写作。

    **上限为什么是 reserve**:与单次合成共用 ``REASONING_EXACT_RESERVE`` 一个旋钮,
    量纲相同(合成上下文里的块条数),于是「精确命中至多占多少席」这件事在两条装配
    路径上是同一个数,用户调一次即可。注入只改切片内容,不扩预算:进了切片的块照样
    在 ``_answer_reasoning`` 的 ``chunk_context_chars`` 里跟本节绑定的块竞争。

    **注入的是不带席位标记的副本**(``exact_lookup=False``),不是原对象:
    ``_answer_reasoning`` 会把带标记的块提到该节字符预算的最前(前缀席位),而那把
    席位是为单次合成路径与上百个候选竞争时设计的。在节内,绑定关系是模型的判断;
    带着标记注入等于让一段模型没绑进本节的原文按构造排在模型亲自绑上的块之前——
    预算吃满的节会因此丢掉自己绑定的证据。已绑定到某节的精确块仍是原对象、仍带
    标记,在那一节里照常拿席位。副本按节各建一份,任何节内的就地修改都不会串到别
    的节或 ``stage.chunks``。

    **注入块单独成段**(``exact_chunks``,不混进 ``chunks``):``_answer_reasoning``
    把它们装在本节绑定的原文段与元素**之后**,只用这两段吃完后剩余的预算准入。
    去掉标记只解掉前缀席位那一半;``_answer_reasoning`` 仍按相关度排序,而精确
    命中的相关度常是 1.0、本节绑定块可能更低,于是不带标记的副本照样能排到绑定
    块之前、吃掉整份 chunk 预算。绑到 source element 的节同样中招——chunk 段在
    element 段之前装配。单独成段之后,注入块拿到的永远是剩余预算,绝不挤占模型
    亲自绑定的证据。
    """
    kept: list[Any] = []
    evidence: dict[int, tuple[list, list, list]] = {}
    skipped: list[str] = []
    bound_chunk_keys: set[str] = set()
    for section in sections:
        hits: list = []
        elements: list = []
        chunks: list = []
        for key in getattr(section, "evidence_keys", ()) or ():
            if key in kg_by_id:
                hits.append(kg_by_id[key])
            elif key in element_by_id:
                elements.append(element_by_id[key])
            elif key in chunk_by_id:
                chunks.append(chunk_by_id[key])
                bound_chunk_keys.add(key)
        if not (hits or elements or chunks):
            skipped.append(str(getattr(section, "title", "")))
            continue
        # 按对象身份存证据:节 id 已由 parse 去重,但 id() 不依赖任何这类保证。
        evidence[id(section)] = (hits, elements, chunks)
        kept.append(section)

    # 空节判定已经做完(上面的循环),所以注入不可能让任何一节复活。
    reserved: list = []
    if int(exact_reserve or 0) > 0:
        for key, chunk in chunk_by_id.items():
            if key in bound_chunk_keys:
                continue
            if not getattr(chunk, "exact_lookup", False):
                continue
            reserved.append(chunk)
            if len(reserved) >= int(exact_reserve):
                break

    slices: list[OutlineSectionSlice] = []
    for section, parent_id in _document_order(kept):
        hits, elements, chunks = evidence[id(section)]
        # 注入块进自己的那格,不与本节绑定的 chunks 合流:装配位置与准入预算
        # 都不同,理由见 docstring 末段。副本去掉席位标记,按节各建一份。
        exact_chunks = [
            replace(chunk, exact_lookup=False) for chunk in reserved
        ]
        slices.append(OutlineSectionSlice(
            section=section,
            index=len(slices),
            key_offset=len(slices) * OUTLINE_SECTION_KEY_STRIDE,
            parent_id=parent_id,
            hits=hits, elements=elements, chunks=chunks,
            exact_chunks=exact_chunks,
        ))
    return slices, skipped


def outline_answer_text(rendered: Sequence[tuple[OutlineSectionSlice, str]]) -> str:
    """按文档序拼接各节文本,顶层节 ``## 标题``、子节 ``### 标题``。

    标题由服务端加,不由模型写(每节的 prompt 明确要求「不要重复标题」):模型自己
    写标题时层级、措辞、加不加编号都随机,而这里的层级必须与大纲的 parent 关系
    一致。Markdown 标题由前端 react-markdown 原生渲染成 h2/h3。

    层级读的是 ``parent_id``(解析后的父节)而不是 ``section.parent``(模型提交的
    原值):父节是空节、被跳过时,后者会渲染出一个没有父标题的孤儿 ``###``。
    """
    blocks: list[str] = []
    for item, text in rendered:
        level = "###" if item.parent_id else "##"
        title = str(getattr(item.section, "title", "")).strip()
        body = (text or "").strip()
        blocks.append(f"{level} {title}\n\n{body}".strip())
    return "\n\n".join(blocks)
