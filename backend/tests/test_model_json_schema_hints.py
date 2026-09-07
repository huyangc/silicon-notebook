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
