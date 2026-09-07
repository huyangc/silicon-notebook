"""Shape-gate guard: every schema hint must accept an unused enum field.

A hint string containing ``|`` advertises "IF filled, one of these values".
Leaving it empty is how a prompt says "this run does not use the field", and
several prompts explicitly ask for exactly that (the reflect prompt tells the
model to leave ``enumerate.kind`` empty when listing the document roster).  So
the gate is enumerated over every schema-hint constant in ``_SWEPT_MODULES``
(``prompts`` plus the three workloads that keep their hint next to their own
parser) and every ``reflect_schema_hint`` gate combination, rather than over a
hand-copied list: a hint added later to any swept module is covered the day it
is added, and nobody has to remember this rule to keep it true.
"""
from __future__ import annotations

import copy
import importlib
import inspect
import itertools
import json
import re
from typing import Any

import pytest

from app.core.config import Settings
from app.core.model_json import (
    ModelJsonRepairError,
    parse_model_json_object,
    validate_model_json_shape,
)
from app.services import model_provider as provider_mod
from app.services import prompts
from app.services.collection_catalog import (
    ENUMERABLE_ELEMENT_KINDS,
    ENUMERABLE_KG_OBJECT_TYPES,
)
from app.services.model_registry import (
    ModelServiceDefinition,
    SystemModelServiceRegistry,
)


def _blank_enums(
    example: Any, path: tuple = (), paths: list | None = None
) -> tuple[Any, list[tuple]]:
    """Mirror a schema example, emptying every ``a|b`` string it advertises.

    Non-enum shapes (lists, null placeholders, booleans, numbers) are copied
    verbatim so this guard exercises the enum rule and nothing else.
    """
    if paths is None:
        paths = []
    if isinstance(example, str):
        if "|" in example:
            paths.append(path)
            return "", paths
        return example, paths
    if isinstance(example, list):
        return (
            [
                _blank_enums(item, path + (index,), paths)[0]
                for index, item in enumerate(example)
            ],
            paths,
        )
    if isinstance(example, dict):
        return (
            {
                key: _blank_enums(item, path + (key,), paths)[0]
                for key, item in example.items()
            },
            paths,
        )
    return example, paths


def _first_item_only(obj: Any) -> Any:
    """Keep one element per list.

    A few hints spell a list example as a UNION of variants
    (``[{"label":"","value":""},{"label":"","retire":true}]``).  The repair
    branch of the gate checks every element against the FIRST example element,
    so a faithful mirror of the union would trip ``unknown_key`` there for
    reasons that have nothing to do with the enum rule under test.
    """
    if isinstance(obj, list):
        return [_first_item_only(obj[0])] if obj else []
    if isinstance(obj, dict):
        return {key: _first_item_only(item) for key, item in obj.items()}
    return obj


def _with_value(obj: Any, path: tuple, value: Any) -> Any:
    replaced = copy.deepcopy(obj)
    cursor = replaced
    for step in path[:-1]:
        cursor = cursor[step]
    cursor[path[-1]] = value
    return replaced


# 巡检范围:「模块 → 属性名模式」。`prompts` 里的命名约定(``*_SCHEMA_HINT``)只覆
# 盖了一部分工作负载——KG 抽取、概念合并复核、冲突复核各自把提示常量放在自己的
# 模块里、还各用各的名字(``_KG_SCHEMA_HINT`` / ``_SCHEMA``)。只扫 `prompts` 的
# 话,这三份提示的枚举规则从来没被这道守卫看过一眼。清单是显式的(改名会响亮
# 地失败),模块内的收集是动态的(同模块里新增一份提示当天就被覆盖)。
_SWEPT_MODULES: tuple[tuple[str, str], ...] = (
    ("app.services.prompts", r".*_SCHEMA_HINT\Z"),
    ("app.services.kg.extract", r"_KG_SCHEMA_HINT\Z"),
    ("app.services.concept_merge_review", r"_SCHEMA\Z"),
    ("app.services.kg.conflict_review", r"_SCHEMA\Z"),
)

# 已知必须在巡检里的名字。用**子集**断言而不是相等:相等会让「新增一份提示」
# 也变成红,而那恰恰是这道守卫要自动覆盖的事;子集则在改名/删除/模块搬家让
# 巡检悄悄缩水时响亮失败。
_KNOWN_SWEPT_NAMES = frozenset({
    "app.services.prompts.REFLECT_SCHEMA_HINT",
    "app.services.prompts.ANSWER_SCHEMA_HINT",
    "app.services.prompts.PLAN_SCHEMA_HINT",
    "app.services.prompts.REPORT_OUTLINE_SCHEMA_HINT",
    "app.services.prompts.QUERY_INTENT_SCHEMA_HINT",
    "app.services.kg.extract._KG_SCHEMA_HINT",
    "app.services.concept_merge_review._SCHEMA",
    "app.services.kg.conflict_review._SCHEMA",
})


def _schema_hint_constants() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for module_name, pattern in _SWEPT_MODULES:
        module = importlib.import_module(module_name)
        for name, value in inspect.getmembers(module):
            if isinstance(value, str) and re.fullmatch(pattern, name):
                found.append((f"{module_name}.{name}", value))
    return found


def _reflect_hint_cases() -> list[tuple[str, str]]:
    cases = []
    for kinds, types, outline, memory, chunks, kg in itertools.product(
        ((), ENUMERABLE_ELEMENT_KINDS),
        ((), ENUMERABLE_KG_OBJECT_TYPES),
        (False, True),
        (False, True),
        (False, True),
        (False, True),
    ):
        label = (
            f"kinds={bool(kinds)}-types={bool(types)}-outline={outline}"
            f"-memory={memory}-chunks={chunks}-kg={kg}"
        )
        cases.append((
            label,
            prompts.reflect_schema_hint(
                kinds, types, outline, memory, chunks, kg
            ),
        ))
    return cases


def _assert_blank_enums_are_accepted(hint: str) -> tuple[Any, list[tuple]]:
    example = json.loads(hint)
    assert isinstance(example, dict), hint
    obj, enum_paths = _blank_enums(example)
    payload = json.dumps(obj, ensure_ascii=False)

    parsed = parse_model_json_object(payload, hint, allow_repair=True)
    validate_model_json_shape(parsed.content, hint)

    # The same object arriving with a repairable syntax fault takes the other
    # branch of the gate (``_validate_repaired_shape``), which is the branch
    # the production rejection came through.
    repairable = json.dumps(
        _first_item_only(obj), ensure_ascii=False
    )[:-1] + ",}"
    repaired = parse_model_json_object(repairable, hint, allow_repair=True)
    assert repaired.repaired is True
    validate_model_json_shape(repaired.content, hint)
    return obj, enum_paths


def _assert_bogus_enums_are_rejected(
    hint: str, obj: Any, enum_paths: list[tuple]
) -> None:
    for path in enum_paths:
        # A field under list index > 0 is validated against the example's FIRST
        # item, whose value at that position may not be an enum at all.
        if any(isinstance(step, int) and step != 0 for step in path):
            continue
        bogus = json.dumps(
            _with_value(obj, path, "bogus"), ensure_ascii=False
        )
        with pytest.raises(ModelJsonRepairError) as caught:
            validate_model_json_shape(bogus, hint)
        assert caught.value.reason == "invalid_enum", path


def test_every_prompt_schema_hint_constant_is_covered():
    names = {name for name, _ in _schema_hint_constants()}

    # A rename that silences the sweep must fail loudly instead of quietly
    # shrinking the guard to nothing.
    assert _KNOWN_SWEPT_NAMES <= names

    # Every declared module must still contribute something: a module rename
    # or a package move would otherwise leave its pattern matching nothing,
    # and the sweep would go green over a hint it no longer looks at.
    for module_name, _pattern in _SWEPT_MODULES:
        assert any(
            name.startswith(f"{module_name}.") for name in names
        ), module_name


@pytest.mark.parametrize(
    ("name", "hint"),
    _schema_hint_constants(),
    ids=[name for name, _ in _schema_hint_constants()],
)
def test_schema_hint_constants_accept_unused_enum_fields(name, hint):
    obj, enum_paths = _assert_blank_enums_are_accepted(hint)
    _assert_bogus_enums_are_rejected(hint, obj, enum_paths)


@pytest.mark.parametrize(
    ("label", "hint"),
    _reflect_hint_cases(),
    ids=[label for label, _ in _reflect_hint_cases()],
)
def test_reflect_schema_hint_gates_accept_unused_enum_fields(label, hint):
    obj, enum_paths = _assert_blank_enums_are_accepted(hint)
    assert enum_paths, label  # next_action is an enum in every combination
    _assert_bogus_enums_are_rejected(hint, obj, enum_paths)


# The production hint for a run with both enumeration whitelists, chunk search
# on and a knowledge graph in scope -- the shape the reproduced failure used.
_ENUMERATION_HINT = prompts.reflect_schema_hint(
    ENUMERABLE_ELEMENT_KINDS, ENUMERABLE_KG_OBJECT_TYPES,
    False, False, True, True,
)

_CATALOG_DECISION = json.dumps({
    "sufficient": False,
    "next_action": "enumerate_elements",
    "enumerate": {
        "kind": "",
        "collection": "sources",
        "source_id": "",
        "source_title": "",
    },
    "reason": "先列出当前笔记本的文档目录",
}, ensure_ascii=False)

_KG_OBJECT_DECISION = json.dumps({
    "sufficient": False,
    "next_action": "enumerate_kg_objects",
    "enumerate": {
        "kind": "",
        "object_type": "concept",
        "collection": "",
        "source_id": "",
        "source_title": "",
    },
    "reason": "列出概念清单",
}, ensure_ascii=False)


@pytest.mark.parametrize(
    ("decision", "action"),
    [
        (_CATALOG_DECISION, "enumerate_elements"),
        (_KG_OBJECT_DECISION, "enumerate_kg_objects"),
    ],
)
def test_enumeration_decisions_pass_the_real_shape_gate(decision, action):
    parsed = parse_model_json_object(
        decision, _ENUMERATION_HINT, allow_repair=True
    )
    validate_model_json_shape(parsed.content, _ENUMERATION_HINT)

    assert json.loads(parsed.content)["next_action"] == action


def test_enumeration_decision_with_a_bogus_kind_is_still_rejected():
    bogus = json.loads(_CATALOG_DECISION)
    bogus["enumerate"]["kind"] = "bogus"

    with pytest.raises(ModelJsonRepairError) as caught:
        validate_model_json_shape(
            json.dumps(bogus, ensure_ascii=False), _ENUMERATION_HINT
        )

    assert caught.value.reason == "invalid_enum"


class _EventLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


class _Chat:
    configured = True
    model = "raw-private-model"

    def __init__(self, result: str) -> None:
        self.result = result

    def chat_json(self, messages, response_schema_hint, **kwargs):
        return self.result


def _reasoning_provider(content: str, events: _EventLog):
    service = ModelServiceDefinition(
        id="chat",
        display_name="安全服务-chat",
        kind="chat",
        protocol="openai",
        base_url="https://chat.example/v1",
        model="safe-chat",
        api_key_env="CHAT_KEY",
        api_key="sk-private",
        max_concurrency=2,
        fingerprint="fp-chat",
    )
    registry = SystemModelServiceRegistry(
        {"chat": service}, {"reasoning_agent": "chat"}, None
    )
    return provider_mod.RuntimeModelProvider(
        Settings(
            _env_file=None, event_log_enabled=False, llm_log_enabled=False
        ),
        events,
        registry=registry,
        chat_factory=lambda _service: _Chat(content),
    )


@pytest.mark.parametrize(
    ("decision", "action"),
    [
        (_CATALOG_DECISION, "enumerate_elements"),
        (_KG_OBJECT_DECISION, "enumerate_kg_objects"),
    ],
)
def test_reasoning_chat_json_delivers_enumeration_decisions(decision, action):
    events = _EventLog()
    provider = _reasoning_provider(decision, events)
    try:
        raw = provider.chat("reasoning_agent").chat_json(
            [{"role": "user", "content": "当前notebook的文章说明了什么"}],
            _ENUMERATION_HINT,
        )
    finally:
        provider.close()

    assert json.loads(raw)["next_action"] == action
    assert [
        event for event in events.events
        if event.get("kind") == "model_json_repair"
        and event.get("status") == "rejected"
    ] == []


def test_reasoning_chat_json_still_rejects_a_bogus_enum_value():
    bogus = json.loads(_CATALOG_DECISION)
    bogus["enumerate"]["kind"] = "bogus"
    events = _EventLog()
    provider = _reasoning_provider(
        json.dumps(bogus, ensure_ascii=False), events
    )
    try:
        with pytest.raises(provider_mod.ModelInvocationError):
            provider.chat("reasoning_agent").chat_json([], _ENUMERATION_HINT)
    finally:
        provider.close()

    rejected = [
        event for event in events.events
        if event.get("kind") == "model_json_repair"
        and event.get("status") == "rejected"
    ]
    assert [event["reason"] for event in rejected] == ["invalid_enum"]


# --- reflect v2 的提示是**带参数的函数**,模块级常量巡检看不到它 -------------
# 上面的 sweep 只收集模块级 `*_SCHEMA_HINT` 常量和 legacy `reflect_schema_hint`
# 的门组合,所以 T2 新增的 `reflect_v2_schema_hint(capabilities)` 从来没被这道守卫
# 看过一眼——它的两个 P1 缺陷(按配额收窄的 next_action 枚举、被判 unknown_key 的
# 非空 arguments)正是从这条缝里漏出去的。

def _v2_facts(**overrides):
    from app.services.reasoning_actions import ReflectCapabilityFacts

    base = dict(
        kg_in_scope=True, scope_restricted=False, has_candidates=True,
        chunk_search_active=True, exact_lookup_active=True, ppr_active=True,
        community_active=True, enumeration_active=True,
        consult_memory_active=True, outline_active=True,
        element_searches_left=5, chunk_searches_left=3, exact_lookups_left=3,
        ppr_left=3, follow_chain_left=3, consult_left=2,
        outline_updates_left=6, enum_rows_left=200, enum_pages_left=4,
        enum_payload_left=256_000,
        element_kinds=tuple(ENUMERABLE_ELEMENT_KINDS),
        object_types=tuple(ENUMERABLE_KG_OBJECT_TYPES),
    )
    base.update(overrides)
    return ReflectCapabilityFacts(**base)


def _v2_capabilities(**overrides):
    from app.services.reasoning_actions import build_reflect_capabilities

    return build_reflect_capabilities(_v2_facts(**overrides))


def _reflect_v2_hint_cases() -> list[tuple[str, str]]:
    """代表性的能力组合:全开、无图、范围收窄、配额全空、终态纠错轮。"""
    shapes = (
        ("full-house", {}),
        ("graphless", {"kg_in_scope": False}),
        ("scope-restricted", {"scope_restricted": True}),
        ("budgets-spent", {
            "element_searches_left": 0, "chunk_searches_left": 0,
            "exact_lookups_left": 0, "ppr_left": 0, "follow_chain_left": 0,
            "consult_left": 0, "outline_updates_left": 0,
        }),
        ("terminal-repair", {"terminal_overflow_repair": True}),
    )
    return [
        (f"reflect_v2[{label}]",
         prompts.reflect_v2_schema_hint(_v2_capabilities(**overrides)))
        for label, overrides in shapes
    ]


@pytest.mark.parametrize(
    ("label", "hint"),
    _reflect_v2_hint_cases(),
    ids=[label for label, _ in _reflect_v2_hint_cases()],
)
def test_reflect_v2_schema_hint_accepts_unused_enum_fields(label, hint):
    obj, enum_paths = _assert_blank_enums_are_accepted(hint)
    assert enum_paths, label  # next_action is an enum in every shape
    _assert_bogus_enums_are_rejected(hint, obj, enum_paths)


def test_reflect_v2_enum_never_narrows_with_the_turn_s_budget():
    """枚举列全部 13 个可识别动作,与本轮可用性无关。

    收窄的话,一个配额耗尽但可识别的动作会在**传输层**被判 `invalid_enum`:模型
    永远等不到那条"这条路本轮走不通,换别的"的观察,重试耗尽后整份反思退成
    fail-open 的 answer,检索循环就此终止。
    """
    from app.services.reasoning_actions import ACTION_ORDER

    spent = _v2_capabilities(ppr_left=0, exact_lookups_left=0)
    assert "ppr_retrieve" not in spent.actions
    hint = prompts.reflect_v2_schema_hint(spent)
    assert json.loads(hint)["next_action"] == "|".join(ACTION_ORDER)

    for action in ("ppr_retrieve", "exact_lookup"):
        payload = json.dumps(
            {"next_action": action, "sufficient": False,
             "arguments": {"query": "x"}, "reason": "撞一次墙"},
            ensure_ascii=False)
        parsed = parse_model_json_object(payload, hint, allow_repair=True)
        validate_model_json_shape(parsed.content, hint)


_V2_ARGUMENT_SHAPES = (
    ("query", "add_subquery",
     {"query": "set_db 的默认值", "types": ["claim"], "prefer": "keyword"}),
    ("term", "exact_lookup", {"term": "set_db"}),
    ("object_id", "expand_graph",
     {"object_id": "ko-1", "edge_type": "depends_on", "direction": "both"}),
    ("chain", "follow_chain",
     {"start_object_id": "ko-1", "target_object_id": "ko-9",
      "direction": "out"}),
    ("enumerate", "enumerate_elements",
     {"kind": "formula", "source_title": "论文一"}),
    ("sections", "update_outline",
     {"sections": [{"id": "s1", "title": "一节", "evidence": []}]}),
    ("empty", "answer", {}),
)


@pytest.mark.parametrize(
    ("label", "action", "arguments"), _V2_ARGUMENT_SHAPES,
    ids=[shape[0] for shape in _V2_ARGUMENT_SHAPES])
def test_reflect_v2_populated_arguments_pass_both_gate_paths(
    label, action, arguments
):
    """`arguments` 是"开放对象":严格与修复两条路径必须给出同一个答案。

    修复分支的 `issubset` 曾把空 example 读成"一个键都不许有",于是所有带真实参数
    的检索动作在模型打一个尾逗号时就失去修复网。`assessment` 同理:本期不进 schema
    (T4 才消费),但它不能只在严格路径上活着。
    """
    hint = prompts.reflect_v2_schema_hint(_v2_capabilities())
    payload = json.dumps({
        "next_action": action,
        "sufficient": False,
        "arguments": arguments,
        "assessment": {"unresolved": [
            {"aspect_id": "a2", "status": "partial", "gap": "still missing"}]},
        "reason": "next step",
    }, ensure_ascii=False)

    parsed = parse_model_json_object(payload, hint, allow_repair=True)
    validate_model_json_shape(parsed.content, hint)
    assert parsed.repaired is False

    repaired = parse_model_json_object(
        f"{payload[:-1]},}}", hint, allow_repair=True)
    assert repaired.repaired is True
    validate_model_json_shape(repaired.content, hint)
    assert json.loads(repaired.content)["arguments"] == arguments


def test_an_unadvertised_key_that_is_not_tolerated_is_still_rejected():
    """开放对象与具名豁免都是**有界**的:别的未声明根级键照旧 unknown_key。"""
    hint = prompts.reflect_v2_schema_hint(_v2_capabilities())
    payload = json.dumps({
        "next_action": "answer", "sufficient": True, "arguments": {},
        "reason": "done", "smuggled": {"anything": 1},
    }, ensure_ascii=False)

    validate_model_json_shape(payload, hint)      # 严格路径历来容忍额外键
    with pytest.raises(ModelJsonRepairError) as caught:
        parse_model_json_object(f"{payload[:-1]},}}", hint, allow_repair=True)
    assert caught.value.reason == "unknown_key"


def test_v2_malformed_sufficient_and_arguments_are_rejected_before_the_parser():
    """F1(复审):`parse_reflect_v2` 里 `_V2_INVALID_SUFFICIENT`/
    `_V2_INVALID_ARGUMENTS_OBJECT` 两支在生产不可达——真实形状闸先拒。

    schema hint 写的是 `"sufficient":false` / `"arguments":{}`,一个非布尔的
    `sufficient` 或非对象的 `arguments` 在到达 `parse_reflect_v2` 之前就已经被
    这个真实的传输层校验函数以 `invalid_boolean`/`invalid_type` 拒绝,重试耗尽
    后走既有 fail-open 合同。解析层那两支分支只为测试替身与 fail_closed 调用方
    的纵深防御保留,钉住这一点是为了不让文档/代码合同再次分叉。
    """
    hint = prompts.reflect_v2_schema_hint(_v2_capabilities())

    bad_sufficient = json.dumps({
        "next_action": "answer", "sufficient": "false", "arguments": {},
        "reason": "done",
    }, ensure_ascii=False)
    with pytest.raises(ModelJsonRepairError) as caught:
        validate_model_json_shape(bad_sufficient, hint)
    assert caught.value.reason == "invalid_boolean"

    bad_arguments = json.dumps({
        "next_action": "answer", "sufficient": True, "arguments": "x",
        "reason": "done",
    }, ensure_ascii=False)
    with pytest.raises(ModelJsonRepairError) as caught:
        validate_model_json_shape(bad_arguments, hint)
    assert caught.value.reason == "invalid_type"


def test_nested_assessment_key_is_only_tolerated_at_the_root():
    """F4(复审):`assessment` 的具名豁免只在根级生效,嵌套同名键仍是 unknown_key。

    `_TOLERATED_UNADVERTISED_KEYS` 曾经在 `_validate_against_example` 的递归
    dict 分支里生效,于是任意深度的 `assessment` 键都能逃过 `unknown_key`。
    `arguments` 是开放对象、本身不检查未知键,所以借道 legacy hint 的 `expand`
    分支——一个非开放的嵌套 dict——来钉住这一点。
    """
    hint = prompts.reflect_schema_hint(kg_actions=True)
    payload = json.dumps({
        "next_action": "expand_graph",
        "sufficient": False,
        "expand": {"object_id": "ko-1", "edge_type": None, "direction": "out",
                   "assessment": {"x": 1}},
        "reason": "done",
    }, ensure_ascii=False)

    with pytest.raises(ModelJsonRepairError) as caught:
        parse_model_json_object(f"{payload[:-1]},}}", hint, allow_repair=True)
    assert caught.value.reason == "unknown_key"


def test_root_level_assessment_key_survives_repair_on_the_legacy_hint():
    """根级豁免对 legacy hint(顶层同样不是开放对象)一样成立,不止 v2。"""
    hint = prompts.reflect_schema_hint(kg_actions=True)
    payload = json.dumps({
        "next_action": "answer",
        "sufficient": True,
        "assessment": {"x": 1},
        "reason": "done",
    }, ensure_ascii=False)

    parsed = parse_model_json_object(
        f"{payload[:-1]},}}", hint, allow_repair=True)
    assert parsed.repaired is True
    validate_model_json_shape(parsed.content, hint)


# --- T4:`assessment` 进 schema 之后,两条路径必须给出同一个答案 -------------
# T2 的教训是「schema hint 与形状闸语义不符」:hint 上写得通、真闸拒。所以这里的
# 每一条都跑**真实**的 `parse_model_json_object` + `validate_model_json_shape`,
# 而且严格与修复两条路径各跑一遍。
#
# T4-A 复审(P1-1)之后 `assessment` 是**开放对象**:传输层只管"它是对象或
# null",形状与边界的拒绝权全部在 `AspectLedger.apply`(那种拒绝是可存活的零
# I/O 观察,而传输层的拒绝会烧掉重试并把整次 run 推进 fail-open 收尾)。所以
# 下面这张表里多出来的几种形状**必须过闸**,它们该不该被接受由方面账去答。

_V2_ASSESSMENT_SHAPES = (
    ("both-lists", {
        "supported": [{"aspect_id": "a1", "evidence_keys": ["ko-1", "c-2"]}],
        "unresolved": [{"aspect_id": "a2", "status": "partial",
                        "evidence_keys": ["e-3"], "gap": "尚缺适用条件"}]}),
    ("supported-only", {
        "supported": [{"aspect_id": "a1", "evidence_keys": ["ko-1"]}]}),
    ("unresolved-only", {
        "unresolved": [{"aspect_id": "a1", "status": "unknown"}]}),
    ("no-keys", {"supported": [{"aspect_id": "a1"}]}),
    ("empty-lists", {"supported": [], "unresolved": []}),
    # 「这一轮我没什么新判断」:整个对象为空也是合法载荷,不能只在严格路径上活着。
    ("empty-object", {}),
    # JSON 序列化器给"没有内容"的那一格写 null 是常见产物。闭合对象下它在两条
    # 路径上都是 `invalid_type` —— 一次 null 结束整次 run。
    ("null", None),
    # `supported` 项带 `gap`:协议没要求它,但模型顺手写上完全正常。闭合对象下
    # 严格路径放行、修复路径 `unknown_key`,同一份载荷两个答案(T2 事故同形)。
    ("supported-item-with-gap", {
        "supported": [{"aspect_id": "a1", "evidence_keys": ["ko-1"],
                       "gap": "顺手写了一句"}]}),
    ("item-with-unknown-key", {
        "unresolved": [{"aspect_id": "a1", "status": "partial",
                        "confidence": 0.4}]}),
    ("evidence-keys-empty", {
        "supported": [{"aspect_id": "a1", "evidence_keys": []}]}),
)


@pytest.mark.parametrize(
    ("label", "assessment"), _V2_ASSESSMENT_SHAPES,
    ids=[shape[0] for shape in _V2_ASSESSMENT_SHAPES])
def test_reflect_v2_assessment_shapes_pass_both_gate_paths(label, assessment):
    hint = prompts.reflect_v2_schema_hint(_v2_capabilities())
    payload = json.dumps({
        "next_action": "search_chunks", "sufficient": False,
        "arguments": {"query": "set_db 的默认值"},
        "assessment": assessment, "reason": "补一个方面",
    }, ensure_ascii=False)

    parsed = parse_model_json_object(payload, hint, allow_repair=True)
    validate_model_json_shape(parsed.content, hint)
    assert parsed.repaired is False

    repaired = parse_model_json_object(
        f"{payload[:-1]},}}", hint, allow_repair=True)
    assert repaired.repaired is True
    validate_model_json_shape(repaired.content, hint)
    assert json.loads(repaired.content)["assessment"] == assessment


def test_tolerated_assessment_payloads_reach_the_ledger_from_both_paths():
    """四种"曾经会杀死整次 run"的载荷:两条真闸放行 → 解析 → 方面账**接受**。

    这四种在闭合对象下的下场各不相同(null 两条路径都 `invalid_type`;`supported`
    项多一个键在修复路径 `unknown_key`),共同点是它们都不该结束一次 run。
    """
    from app.services.reasoning_actions import build_reflect_capabilities
    from app.services.reasoning_aspects import AspectLedger
    from app.services.reasoning_retrieval import parse_reflect_v2

    hint = prompts.reflect_v2_schema_hint(_v2_capabilities())
    caps = build_reflect_capabilities(
        _v2_facts())  # 解析白名单与 hint 同一个能力投影
    for label, assessment, expected_status in (
        ("null", None, "unknown"),
        ("item-with-unknown-key",
         {"supported": [{"aspect_id": "a1", "evidence_keys": ["ck-1"],
                         "confidence": 0.9}]}, "supported"),
        ("evidence-keys-empty",
         {"supported": [{"aspect_id": "a1", "evidence_keys": []}]}, "unknown"),
        ("empty-object", {}, "unknown"),
    ):
        payload = json.dumps({
            "next_action": "answer", "sufficient": True, "arguments": {},
            "assessment": assessment, "reason": "done",
        }, ensure_ascii=False)
        parsed = parse_model_json_object(payload, hint, allow_repair=True)
        validate_model_json_shape(parsed.content, hint)
        repaired = parse_model_json_object(
            f"{payload[:-1]},}}", hint, allow_repair=True)
        assert repaired.repaired is True, label
        validate_model_json_shape(repaired.content, hint)

        decision = parse_reflect_v2(json.loads(repaired.content), caps)
        assert decision.invalid_reason == "", label
        ledger = AspectLedger(["问题一"], source="intent_topics")
        if decision.assessment is not None:
            assert ledger.apply(
                decision.assessment, allowed_keys={"ck-1"}) == "", label
        assert ledger.snapshot()[0].status == expected_status, label


def test_reflect_v2_assessment_shape_faults_are_the_ledgers_to_reject():
    """形状越界的自评**过传输闸**,拒绝权在方面账(可存活的零 I/O 观察)。

    这几种载荷在闭合对象下会被传输层拒掉(`invalid_type` / `invalid_enum` /
    `unknown_key`)——那种拒绝烧重试、把整次 run 推进 fail-open 收尾。现在它们
    一路走到 `AspectLedger.apply`,由它给出稳定原因码,整轮折成一条 invalid 观察
    而循环继续。

    变异:把 hint 里的 `assessment` 改回闭合对象 ⇒ 这条红。
    """
    from app.services.reasoning_aspects import AspectLedger

    hint = prompts.reflect_v2_schema_hint(_v2_capabilities())
    for assessment, why in (
        ({"supported": ["a1"]}, "item_not_object"),
        ({"supported": [{"aspect_id": "a1", "evidence_keys": "ko-1"}]},
         "evidence_keys_not_list"),
        ({"supported": [{"aspect_id": "a1", "evidence_keys": [{"k": 1}]}]},
         "evidence_key_not_string"),
        ({"supported": "a1"}, "supported_not_list"),
        ({"unresolved": [{"aspect_id": "a1", "status": "definitely-not"}]},
         "invalid_status"),
    ):
        payload = json.dumps({
            "next_action": "answer", "sufficient": False, "arguments": {},
            "assessment": assessment, "reason": "done",
        }, ensure_ascii=False)
        # 两条真闸都放行。
        parsed = parse_model_json_object(payload, hint, allow_repair=True)
        validate_model_json_shape(parsed.content, hint)
        repaired = parse_model_json_object(
            f"{payload[:-1]},}}", hint, allow_repair=True)
        assert repaired.repaired is True
        validate_model_json_shape(repaired.content, hint)
        # 拒绝发生在这里,而且带稳定原因码。
        ledger = AspectLedger(["问题一"], source="intent_topics")
        assert ledger.apply(assessment, allowed_keys=set()) == why, assessment


def test_reflect_v2_assessment_is_an_open_object_and_legacy_is_untouched():
    """v2 广告它、且是**开放对象**;legacy hint 一个字节都没变(关闭态等价)。"""
    v2 = json.loads(prompts.reflect_v2_schema_hint(_v2_capabilities()))
    assert v2["assessment"] == {}
    for kinds, types, outline, memory, chunks, kg in itertools.product(
        ((), ENUMERABLE_ELEMENT_KINDS), ((), ENUMERABLE_KG_OBJECT_TYPES),
        (False, True), (False, True), (False, True), (False, True),
    ):
        legacy = prompts.reflect_schema_hint(
            kinds, types, outline, memory, chunks, kg)
        assert "assessment" not in json.loads(legacy)


def _open_object_paths(example: Any, prefix: tuple = ()) -> list[tuple]:
    """示例里所有**空 dict**(= 开放对象)的路径。"""
    found: list[tuple] = []
    if isinstance(example, dict):
        if not example:
            return [prefix]
        for key, item in example.items():
            found.extend(_open_object_paths(item, prefix + (key,)))
    elif isinstance(example, list):
        for index, item in enumerate(example):
            found.extend(_open_object_paths(item, prefix + (index,)))
    return found


def test_open_objects_exist_only_in_the_v2_hint():
    """开放对象放行 null 这件事,对**其它每一份提示**结构上零影响。

    "开放对象也接受 null" 是 T4-A 复审(P1-1)在 `model_json` 两条路径上加的
    规则。它只可能作用在示例里写着空 dict 的那一格上,而下面这次巡检(既有的
    `_schema_hint_constants` + 全部 `reflect_schema_hint` 组合)证明:全仓库只有
    v2 那一份提示有开放对象,而且只有 `arguments` / `assessment` 两格。所以
    legacy 与其它工作负载的形状闸行为逐字节不变——不是"应该没影响",是没有
    可以受影响的那一格。
    """
    for name, hint in _schema_hint_constants():
        assert _open_object_paths(json.loads(hint)) == [], name
    for label, hint in _reflect_hint_cases():
        assert _open_object_paths(json.loads(hint)) == [], label
    for label, hint in _reflect_v2_hint_cases():
        assert sorted(_open_object_paths(json.loads(hint))) == [
            ("arguments",), ("assessment",)], label


def test_open_objects_accept_json_null_on_both_paths():
    """`arguments` / `assessment` 收到 null:两条路径一致放行(下游归一为缺省)。

    变异:把 `_is_open_object` 的两个调用点改回"必须是 dict" ⇒ 这条红。
    """
    hint = prompts.reflect_v2_schema_hint(_v2_capabilities())
    for field in ("arguments", "assessment"):
        payload = json.dumps({
            "next_action": "answer", "sufficient": True,
            "arguments": {}, "assessment": {}, "reason": "done",
            field: None,
        }, ensure_ascii=False)
        parsed = parse_model_json_object(payload, hint, allow_repair=True)
        validate_model_json_shape(parsed.content, hint)
        repaired = parse_model_json_object(
            f"{payload[:-1]},}}", hint, allow_repair=True)
        assert repaired.repaired is True
        validate_model_json_shape(repaired.content, hint)
    # 非对象非 null 仍然是 `invalid_type`:放行的是 null,不是"什么都行"。
    bogus = json.dumps({
        "next_action": "answer", "sufficient": True, "arguments": 7,
        "reason": "done"}, ensure_ascii=False)
    with pytest.raises(ModelJsonRepairError) as caught:
        validate_model_json_shape(bogus, hint)
    assert caught.value.reason == "invalid_type"
