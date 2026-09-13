"""插件 reflect 动作投影进 prompt / schema / 白名单 / 解析(T2)。

设计文档 `docs/superpowers/specs/2026-09-13-reflect-plugin-action-design_zh.md`
§3.2–§3.4 与 §九 不变量 6/7。三组:

1. **关闭态逐字节等价**——`plugin_actions=()` 时两个渲染函数的输出与不传这个
   参数时逐字节相同(不变量 6)。这是「没有插件 / 插件不可用 / 档位或预算把它
   关掉」这三件事在核心侧的同一种表达。
2. **渲染样例**——带 text+enum 参数的动作与零参数动作的精确串,外加不变量 7
   (模型看到的文本里没有 `plugin_id`/`contribution_id`)。
3. **真实形状闸下的解析**——参数落地、枚举、必填、夹取、未提供时的拒收。凡是
   模型响应形状相关的用例都过 `_ValidatingReflectLLM`(生产的两道校验),理由
   同 `test_reasoning_enumeration_tools._ValidatingLLM`:参数的形状合同有两个
   执行者,只测解析器会给出「模型这样填是可以的」这个在生产上不成立的结论。
"""
from __future__ import annotations

import itertools
import json

import pytest

from app.core.config import Settings
from app.domain.reflect_action import (
    REFLECT_ACTION_ARGUMENT_MAX_CHARS,
    ReflectActionDescriptor,
    ReflectActionParameter,
    ReflectActionSpec,
)
from app.services.embedding import FakeEmbedder
from app.services.prompts import (
    SCOPE_DEIXIS_GROUNDING,
    project_reflect_actions,
    reflect_prompt,
    reflect_schema_hint,
)
from app.services.reasoning_retrieval import ReasoningRetriever
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client
# 关闭态的对照物复用既有的冻结字面量,而不是在这里抄第二份 —— 见
# ``test_the_default_gate_schema_still_equals_the_frozen_pre_tools_literal``。
from tests.test_reasoning_enumeration_tools import FROZEN_REFLECT_SCHEMA_HINT


# --- fixtures ---------------------------------------------------------------

SEARCH_IEEE = ReflectActionSpec(
    contribution_id="ieee-search-contribution",
    plugin_id="acme-literature-plugin",
    descriptor=ReflectActionDescriptor(
        name="search_ieee",
        description="Search IEEE Xplore for peer-reviewed papers.",
        source_label="IEEE Xplore",
        parameters=(
            ReflectActionParameter(
                name="query",
                description="the search string, in English",
                kind="text",
                required=True,
            ),
            ReflectActionParameter(
                name="venue",
                description="narrow to one venue kind",
                kind="enum",
                values=("journal", "conference"),
            ),
        ),
    ),
)
ASK_WEB = ReflectActionSpec(
    contribution_id="web-contribution",
    plugin_id="acme-web-plugin",
    descriptor=ReflectActionDescriptor(
        name="ask_web",
        # 全部参数可选:模型完全可能一个都不填,这正是 §3.3 与校验层
        # `missing_expected_key` 打架的那种形状。
        description="Search the public web.",
        source_label="Web",
        parameters=(
            ReflectActionParameter(
                name="query", description="the search string", kind="text"),
            ReflectActionParameter(
                name="depth", description="how far to read", kind="enum",
                values=("headline", "full")),
        ),
    ),
)
ASK_DESK = ReflectActionSpec(
    contribution_id="desk-contribution",
    plugin_id="acme-desk-plugin",
    descriptor=ReflectActionDescriptor(
        name="ask_desk",
        description="Ask the reference desk for a pointer.",
        source_label="Reference desk",
    ),
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    # 本机 .env 里的真实推理端点会让这些用例打真实网络(同 test_reasoning_*)。
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    return instance


class _ReplayLLM:
    """回放一份 reflect 响应,并记下模型真正看到的 prompt 与 schema。

    **不过**生产校验:用它的用例测的是解析层自己的纵深防御(校验层放行、被
    修复路径改写、或换一个不带形状闸的 provider 时,解析器还得站得住)。
    """

    configured = True

    def __init__(self, payload):
        self.payload = payload
        self.prompts: list[str] = []
        self.schema_hints: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        self.prompts.append(messages[-1]["content"])
        self.schema_hints.append(schema_hint)
        return json.dumps(self.payload)


class _ValidatingReflectLLM(_ReplayLLM):
    """把回放的 JSON 真的送过**生产的那两道校验**再交给 `reflect()`。

    镜像 `model_provider.ScheduledJsonChatClient` 的拒收合同,与
    `test_reasoning_enumeration_tools._ValidatingLLM` 同一份说明:参数的形状
    合同有两个执行者,只测后者的用例在生产上早就被前者打成兜底了。
    """

    def chat_json(self, messages, schema_hint, **kwargs):
        from app.core.model_json import (
            ModelJsonRepairError, parse_model_json_object,
            validate_model_json_shape,
        )
        from app.services.model_work import MalformedModelResponse

        raw = super().chat_json(messages, schema_hint, **kwargs)
        try:
            parsed = parse_model_json_object(
                raw, schema_hint, allow_repair=False)
            validate_model_json_shape(parsed.content, schema_hint)
        except ModelJsonRepairError as exc:
            raise MalformedModelResponse() from exc
        return parsed.content


def _retriever(repo, llm, *, fail_closed=False):
    bind_chat_client(repo, "reasoning_agent", llm)
    return ReasoningRetriever.from_repository(
        repo, repo.settings, fail_closed=fail_closed
    )


def _decide(repo, payload, *, plugin_actions=(), fail_closed=False,
            validating=True):
    llm = (_ValidatingReflectLLM if validating else _ReplayLLM)(payload)
    retriever = _retriever(repo, llm, fail_closed=fail_closed)
    decision = retriever.reflect(
        "版图设计的最新进展是什么", "- [chunk] 论文一 · §1: 片段",
        plugin_actions=plugin_actions,
    )
    return decision, llm


# --- 1. 关闭态逐字节等价(不变量 6) -----------------------------------------
#
# 「`plugin_actions=()` 与不传相等」这种写法**测不出任何东西**:两边是同一次
# 调用,任何实现都绿。关闭态要能红,断言的另一边必须是一个**独立于本次实现**的
# 对照物——一份冻结字面量、一条相邻关系、或者一个不含插件的词表结构。

_GATE_COMBINATIONS = list(itertools.product(
    [(), ("formula", "table")],   # element_kinds
    [(), ("concept",)],           # object_types
    [False, True],                # outline
    [False, True],                # consult_memory
    [False, True],                # search_chunks
    [False, True],                # kg_actions
))


def test_the_default_gate_schema_still_equals_the_frozen_pre_tools_literal():
    """(a) 默认闸下的 schema 逐字节等于接入枚举工具之前那份冻结串。

    对照物直接复用 `test_reasoning_enumeration_tools` 里已有的冻结字面量,不再
    抄第二份:抄一份就等于给同一条基线开了两个可以各自漂移的写入点。插件投影
    在关闭态多吐一个字节(哪怕只是一个 `|` 或一个换行),这里就红。
    """
    assert reflect_schema_hint() == FROZEN_REFLECT_SCHEMA_HINT


@pytest.mark.parametrize("consult_memory", [False, True])
def test_the_closed_state_leaves_no_character_before_the_scope_paragraph(
    consult_memory,
):
    """(b) 关闭态下动作列表尾与范围段头之间**零字符**。

    插件行就插在这两者中间,所以「关闭态什么都不插」在 prompt 里的可观测形态
    就是这条相邻关系。`f"\\n{plugin_action_lines}"` 这类写法在这里必红——它在
    关闭态也会多一个换行。
    """
    prompt = reflect_prompt("q", "c", consult_memory=consult_memory)
    before_scope = prompt[:prompt.index(SCOPE_DEIXIS_GROUNDING)]

    assert before_scope.endswith(
        "when genuinely unsure what to try next.\n" if consult_memory
        # consult_memory 关着时,动作列表的最后一行是 exact_lookup。
        else "paraphrase returns nothing.\n"
    )


def _expected_last_action_word(gates) -> str:
    """关闭态枚举串的尾词,按闸组合独立推出来(不读实现的拼接顺序)。"""
    element_kinds, object_types, outline, consult_memory, chunks, kg = gates
    enumeration = bool(element_kinds or object_types)
    if outline:
        return "update_outline"
    if consult_memory:
        return "consult_memory"
    if enumeration and kg:
        return "enumerate_kg_objects"
    if enumeration:
        return "enumerate_elements"
    if chunks:
        return "search_chunks"
    return "exact_lookup"


@pytest.mark.parametrize("gates", _GATE_COMBINATIONS)
def test_the_closed_state_enum_has_no_empty_word_and_ends_on_a_core_word(gates):
    """(c) 关闭态词表:没有空词,且尾词是本闸组合下的那个**核心**动作。

    插件词是追加在最后的,所以「关闭态尾词仍是核心词」正是它一个字都没加的
    可观测判据;空词检查则拦住多一个分隔符那一类(`+ "|"`)的写法。
    """
    words = json.loads(reflect_schema_hint(*gates))["next_action"].split("|")

    assert "" not in words
    assert len(set(words)) == len(words)
    assert words[-1] == _expected_last_action_word(gates)


@pytest.mark.parametrize("gates", _GATE_COMBINATIONS)
def test_an_offered_action_adds_exactly_its_own_two_pieces_to_the_schema(gates):
    """开启态减去插件的两块,必须逐字节回到关闭态(减法,不重写拼接顺序)。"""
    opened = reflect_schema_hint(*gates, plugin_actions=(SEARCH_IEEE,))
    stripped = opened.replace(
        '"search_ieee":{"query":"","venue":"journal|conference"},', "", 1,
    ).replace("|search_ieee", "", 1)

    assert stripped == reflect_schema_hint(*gates)


@pytest.mark.parametrize("consult_memory", [False, True])
def test_an_offered_action_only_splices_its_lines_before_the_scope_paragraph(
    consult_memory,
):
    """开启态 = 关闭态在范围段之前原位插入动作行,别处一个字节都不动。"""
    closed = reflect_prompt("q", "c", consult_memory=consult_memory)
    head, separator, tail = closed.partition(SCOPE_DEIXIS_GROUNDING)

    assert reflect_prompt(
        "q", "c", consult_memory=consult_memory,
        plugin_actions=(SEARCH_IEEE,),
    ) == head + project_reflect_actions(
        (SEARCH_IEEE,)).prompt_lines + separator + tail


def test_an_empty_projection_is_empty_in_all_four_faces():
    projection = project_reflect_actions(())

    assert projection.prompt_lines == ""
    assert projection.schema_branches == ""
    assert projection.next_action_words == ""
    assert projection.action_names == ()


# --- 2. 渲染样例 ------------------------------------------------------------

_FIXED_SENTENCES = (
    "Returns EXTERNAL material from outside the library, labelled [external] "
    "in the candidates; it is citable with [k] but must never be presented as "
    "library content. Use it only for an aspect that library actions have "
    "already come back empty on; derive the arguments from the question and "
    "the missing aspect, never copy candidate text into them.\n"
)


def test_a_parameterised_action_renders_its_exact_prompt_line():
    """精确串,不是子串断言:核心那四句是插件改不了的模板文本,措辞漂了就该红。"""
    projection = project_reflect_actions((SEARCH_IEEE,))

    assert projection.prompt_lines == (
        "- search_ieee: Search IEEE Xplore for peer-reviewed papers. "
        "Set search_ieee.query (the search string, in English); "
        "search_ieee.venue is one of journal|conference "
        "(narrow to one venue kind, optional). "
        "Always write every key of the search_ieee object; leave an unused "
        "parameter as an empty string. "
        + _FIXED_SENTENCES
    )


def test_a_parameterless_action_renders_without_an_argument_clause():
    projection = project_reflect_actions((ASK_DESK,))

    assert projection.prompt_lines == (
        "- ask_desk: Ask the reference desk for a pointer. "
        "Takes no parameters. "
        + _FIXED_SENTENCES
    )


def test_a_parameterised_action_renders_its_exact_schema_branch():
    projection = project_reflect_actions((SEARCH_IEEE,))

    assert projection.schema_branches == (
        '"search_ieee":{"query":"","venue":"journal|conference"},'
    )
    assert projection.next_action_words == "|search_ieee"
    assert projection.action_names == ("search_ieee",)


def test_a_parameterless_action_adds_an_enum_word_but_no_branch():
    """零参数动作只加枚举词(同 consult_memory):没有分支就没有可填的槽位。"""
    projection = project_reflect_actions((ASK_DESK,))

    assert projection.schema_branches == ""
    assert projection.next_action_words == "|ask_desk"


def test_actions_keep_the_caller_s_order_in_every_face():
    projection = project_reflect_actions((SEARCH_IEEE, ASK_DESK))

    assert projection.next_action_words == "|search_ieee|ask_desk"
    assert projection.action_names == ("search_ieee", "ask_desk")
    assert projection.prompt_lines.index("- search_ieee:") < (
        projection.prompt_lines.index("- ask_desk:"))


def test_the_schema_appends_plugin_words_after_every_core_word():
    schema = reflect_schema_hint(
        ("formula",), ("concept",), True, True, True, True,
        plugin_actions=(SEARCH_IEEE, ASK_DESK),
    )
    parsed = json.loads(schema)
    words = parsed["next_action"].split("|")

    assert words[-2:] == ["search_ieee", "ask_desk"]
    assert parsed["search_ieee"] == {"query": "", "venue": "journal|conference"}
    assert "ask_desk" not in parsed


def test_the_prompt_places_plugin_lines_after_consult_memory_and_before_scope():
    """位置合同(§3.2):库内动作全部读完,才轮到唯一一条离开库的通道。"""
    prompt = reflect_prompt(
        "q", "c", consult_memory=True, plugin_actions=(SEARCH_IEEE,))

    assert prompt.index("- consult_memory:") < prompt.index("- search_ieee:")
    assert prompt.index("- search_ieee:") < prompt.index(SCOPE_DEIXIS_GROUNDING)


def test_the_model_never_sees_a_plugin_id(repo):
    """§九 不变量 7:模型看到的只有动作名、描述、参数名/描述与枚举值。

    断言打在**模型真正收到的那两个串**上(retriever 递给 client 的 prompt 与
    schema),不是渲染函数的返回值——投影正确而 `reflect()` 另拼一份带 id 的
    文本,是这条不变量唯一值得担心的失手形状。
    """
    _, llm = _decide(
        repo,
        {"next_action": "answer", "sufficient": True, "reason": "够了"},
        plugin_actions=(SEARCH_IEEE, ASK_DESK),
    )
    seen = "".join(llm.prompts + llm.schema_hints)

    assert "search_ieee" in seen and "ask_desk" in seen
    for spec in (SEARCH_IEEE, ASK_DESK):
        assert spec.plugin_id not in seen
        assert spec.contribution_id not in seen


# --- 3. 白名单与解析(真实形状闸) ------------------------------------------

def test_a_legal_plugin_call_lands_its_arguments(repo):
    decision, _ = _decide(
        repo,
        {"next_action": "search_ieee", "reason": "库内空手,转外部",
         "search_ieee": {"query": "layout DRC", "venue": "journal"}},
        plugin_actions=(SEARCH_IEEE,),
    )

    assert decision.fallback is False
    assert decision.next_action == "search_ieee"
    assert decision.plugin_action_arguments == {
        "query": "layout DRC", "venue": "journal"}
    assert decision.plugin_rejected_arguments == {}


def test_an_omitted_optional_parameter_arrives_as_an_empty_string(repo):
    """键集恒等于描述符的参数集:下游不用为每个参数写一次 `.get` 兜底。"""
    decision, _ = _decide(
        repo,
        {"next_action": "search_ieee", "reason": "转外部",
         "search_ieee": {"query": "layout DRC"}},
        plugin_actions=(SEARCH_IEEE,),
    )

    assert decision.plugin_action_arguments == {"query": "layout DRC",
                                                "venue": ""}


def test_an_empty_enum_string_is_accepted_by_the_validation_layer(repo):
    """模型把枚举参数留空是**合法**的,整轮不能被打成兜底。

    F1 立下的规则:枚举示例串对空串永远宽容(「本轮不用这个字段」)。这条在
    插件参数上必须同样成立,否则一个照着提示词「optional」留空的模型反而比
    乱填的模型更容易失败。
    """
    decision, _ = _decide(
        repo,
        {"next_action": "search_ieee", "reason": "转外部",
         "search_ieee": {"query": "layout DRC", "venue": ""}},
        plugin_actions=(SEARCH_IEEE,),
    )

    assert decision.fallback is False
    assert decision.plugin_action_arguments == {"query": "layout DRC",
                                                "venue": ""}


def test_a_non_empty_illegal_enum_value_is_refused_by_the_validation_layer(repo):
    """如实记录**校验层**的行为:非空非法枚举值在到达解析器之前就被拒。

    与 `enumerate.scope` 的同名用例是同一条规则(F1:空串永远合法、非空非法
    仍拒),所以生产上这一轮走兜底,`plugin_rejected_arguments` 这条路根本走
    不到——它是下面那条纵深防御用例的事。
    """
    decision, _ = _decide(
        repo,
        {"next_action": "search_ieee", "reason": "转外部",
         "search_ieee": {"query": "layout DRC", "venue": "workshop"}},
        plugin_actions=(SEARCH_IEEE,),
    )

    assert decision.fallback is True
    assert decision.fallback_reason == "invalid_enum"


def test_an_illegal_enum_value_reaching_the_parser_is_cleared_and_recorded(repo):
    """纵深防御:形状闸之外(修复路径、别的 provider)进来的非法值。

    动作**仍然成立**——非法的是一个可选参数,不是动作本身;参数清成空串,原值
    留在 `plugin_rejected_arguments` 供 T3 写教学式 skip 文案。与
    `enumerate_collection_rejected` 同形。
    """
    decision, _ = _decide(
        repo,
        {"next_action": "search_ieee", "reason": "转外部",
         "search_ieee": {"query": "layout DRC", "venue": "workshop"}},
        plugin_actions=(SEARCH_IEEE,),
        validating=False,
    )

    assert decision.next_action == "search_ieee"
    assert decision.fallback is False
    assert decision.plugin_action_arguments == {"query": "layout DRC",
                                                "venue": ""}
    assert decision.plugin_rejected_arguments == {"venue": "workshop"}


def test_a_non_object_argument_payload_is_read_as_no_arguments(repo):
    """模型把参数写成一个串 ⇒ 动作照旧成立,参数全空(fail-open)。"""
    decision, _ = _decide(
        repo,
        {"next_action": "search_ieee", "reason": "转外部",
         "search_ieee": "layout DRC"},
        plugin_actions=(SEARCH_IEEE,),
        validating=False,
    )

    assert decision.next_action == "search_ieee"
    assert decision.plugin_action_arguments == {"query": "", "venue": ""}


@pytest.mark.parametrize("given", [None, True, 7, ["a"], {"b": 1}])
def test_a_non_string_argument_value_is_read_as_not_given(repo, given):
    """非字符串一律清空,绝不 `str(...)` 强转。

    强转的产物(`"None"` / `"True"` / `"['a']"`)会被原样发给库外的插件,并逐字
    进轨迹——那是一次没有任何人写过的外部检索请求。描述符只有 text 与 enum 两
    种参数,所以「不是字符串」永远等价于「模型没按合同填」。
    """
    decision, _ = _decide(
        repo,
        {"next_action": "search_ieee", "reason": "转外部",
         "search_ieee": {"query": given, "venue": given}},
        plugin_actions=(SEARCH_IEEE,),
        validating=False,
    )

    assert decision.plugin_action_arguments == {"query": "", "venue": ""}
    assert decision.plugin_rejected_arguments == {}


def test_an_over_long_text_argument_is_clamped_not_refused(repo):
    """运行期夹取合同:参数是在途文本,超长夹掉,不像描述符那样响亮失败。"""
    decision, _ = _decide(
        repo,
        {"next_action": "search_ieee", "reason": "转外部",
         "search_ieee": {"query": "x" * (REFLECT_ACTION_ARGUMENT_MAX_CHARS + 50)}},
        plugin_actions=(SEARCH_IEEE,),
    )

    assert decision.fallback is False
    assert decision.plugin_action_arguments["query"] == (
        "x" * REFLECT_ACTION_ARGUMENT_MAX_CHARS)


# --- 全可选参数动作:为什么模板里那句「每个键都要写」是必需的 ---------------
#
# `model_json._validate_known_shape` 要求一个**被描述过的**嵌套对象至少与示例
# 共享一个键,否则 `missing_expected_key`。于是一个所有参数都标了「optional」的
# 动作,模型照字面什么都不填吐 `{}`,整轮反思就被打成兜底——插件被通告了却调
# 不动。核心不去改 `model_json`(那是全仓共用的形状闸),改的是提示词:告诉模型
# 键要写全、不用的留空串(空串这一层永远接受)。
# 下面三条用例如实钉住这一层的真实行为,以及那句模板文本确实在。

def test_the_prompt_tells_the_model_to_write_every_key(repo):
    _, llm = _decide(
        repo,
        {"next_action": "answer", "sufficient": True, "reason": "够了"},
        plugin_actions=(ASK_WEB,),
    )

    assert (
        "Always write every key of the ask_web object; leave an unused "
        "parameter as an empty string."
    ) in llm.prompts[0]


@pytest.mark.parametrize("arguments", [{}, {"unknown": "x"}])
def test_an_argument_object_sharing_no_key_is_refused_by_the_validation_layer(
    repo, arguments,
):
    """空对象与「只有未知键」都拿不到兜底之外的结果——这就是那句模板的理由。"""
    decision, _ = _decide(
        repo,
        {"next_action": "ask_web", "reason": "转外部", "ask_web": arguments},
        plugin_actions=(ASK_WEB,),
    )

    assert decision.fallback is True
    assert decision.fallback_reason == "missing_expected_key"


def test_a_known_key_carries_the_call_and_an_unknown_key_is_dropped(repo):
    """已知键 + 未知键 ⇒ 校验层放行(它只拒未知**类型**,不拒多余键),解析层
    按描述符取键,所以未知键根本进不了 `plugin_action_arguments`。"""
    decision, _ = _decide(
        repo,
        {"next_action": "ask_web", "reason": "转外部",
         "ask_web": {"query": "layout DRC", "nonsense": "x"}},
        plugin_actions=(ASK_WEB,),
    )

    assert decision.fallback is False
    assert decision.next_action == "ask_web"
    assert decision.plugin_action_arguments == {"query": "layout DRC",
                                                "depth": ""}


def test_writing_every_key_empty_is_accepted_for_an_all_optional_action(repo):
    """模板那句话教给模型的那种填法,必须真的能过闸。"""
    decision, _ = _decide(
        repo,
        {"next_action": "ask_web", "reason": "转外部",
         "ask_web": {"query": "", "depth": ""}},
        plugin_actions=(ASK_WEB,),
    )

    assert decision.fallback is False
    assert decision.plugin_action_arguments == {"query": "", "depth": ""}


def test_a_parameterless_action_lands_with_no_arguments(repo):
    decision, _ = _decide(
        repo,
        {"next_action": "ask_desk", "reason": "问问台"},
        plugin_actions=(ASK_DESK,),
    )

    assert decision.next_action == "ask_desk"
    assert decision.plugin_action_arguments == {}


def test_fail_closed_refuses_a_missing_required_argument(repo):
    with pytest.raises(ValueError,
                       match="reasoning search_ieee action is missing query"):
        _decide(
            repo,
            {"next_action": "search_ieee", "reason": "转外部",
             "search_ieee": {"venue": "journal"}},
            plugin_actions=(SEARCH_IEEE,),
            fail_closed=True,
        )


def test_fail_open_keeps_a_missing_required_argument_for_the_executor(repo):
    """非 fail_closed 不在解析层拦:交给 T3 的执行分支记 skip,模型下轮还能补。"""
    decision, _ = _decide(
        repo,
        {"next_action": "search_ieee", "reason": "转外部",
         "search_ieee": {"venue": "journal"}},
        plugin_actions=(SEARCH_IEEE,),
    )

    assert decision.fallback is False
    assert decision.next_action == "search_ieee"
    assert decision.plugin_action_arguments == {"query": "", "venue": "journal"}


def test_an_unoffered_plugin_action_is_rejected_by_the_validation_layer(repo):
    """未传 `plugin_actions` ⇒ 那个词根本不在 schema 的枚举串里。"""
    decision, llm = _decide(
        repo,
        {"next_action": "search_ieee", "reason": "转外部"},
    )

    assert "search_ieee" not in llm.schema_hints[0]
    assert "search_ieee" not in llm.prompts[0]
    assert decision.fallback is True
    assert decision.fallback_reason == "invalid_enum"


def test_an_unoffered_plugin_action_reaching_the_parser_is_an_invalid_action(repo):
    """纵深防御:白名单与 prompt/schema 同源,所以关闭态它也不在白名单里。"""
    decision, _ = _decide(
        repo,
        {"next_action": "search_ieee", "reason": "转外部"},
        validating=False,
    )

    assert decision.fallback is True
    assert decision.fallback_reason == "invalid_action:search_ieee"


def test_fail_closed_refuses_an_unoffered_plugin_action(repo):
    with pytest.raises(ValueError,
                       match="reasoning model returned an invalid action"):
        _decide(
            repo,
            {"next_action": "search_ieee", "reason": "转外部"},
            validating=False,
            fail_closed=True,
        )


def test_a_core_action_ignores_a_stray_plugin_argument_object(repo):
    """只有模型选中的动作命中插件集合时才读那一格。"""
    decision, _ = _decide(
        repo,
        {"next_action": "answer", "sufficient": True, "reason": "够了",
         "search_ieee": {"query": "layout DRC"}},
        plugin_actions=(SEARCH_IEEE,),
    )

    assert decision.next_action == "answer"
    assert decision.plugin_action_arguments == {}
    assert decision.plugin_rejected_arguments == {}
