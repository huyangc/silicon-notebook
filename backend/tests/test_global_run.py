"""D1-2 -- ``global_ask_run`` 的「四件事同时装、同时拆」合同。

一次全局 run 由四件事构成,而它们只在**同时为真**时才是一次正确的 run:参与集
覆盖、逐库来源天花板 + 显式无主体位、detached turn、联邦运行计划。检索层与 ask
层刻意读两个不同的判据(理由见 ``retrieval_participants`` 与 ``source_scope`` 的
docstring),所以「只装一半」不是降级而是**静默答错**:

* 只装覆盖 —— 检索腿进对等模式而 ask 侧仍是单库模式:名义 active 的私有 Memory
  折进跨库答案、唯独它的引用归属被抹空、表格臂保留挂在锚点库下但用户没选的参考库;
* 只装天花板 —— ask 侧进对等模式而检索仍按锚点库的挂载表扇出,搜的根本不是用户
  选的那组库。

天花板必须是参与集上的**全映射**还有第二个理由:
``ask_service._peer_ceiling_participants`` 对「某库无条目」fail-closed(丢库),而
``ActiveSourceScope.allows()`` 对同一事实 fail-open(放行全部来源)。两者只有在
「每个参与库都有条目」时才不分歧,所以可见来源为空的库必须写成 ``frozenset()``
而不是被 ``if sources:`` 跳过。

本文件不测生产接线:今天 ``backend/app`` 下**没有任何** ``global_ask_run`` 的调用
点(D1-4 才接),``test_only_global_ask_installs`` 用 ⊆ 写法把这件事一并钉住。
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

from app.domain.retrieval_control import ParticipantOverrideError
from app.services.ask_service import AskService
from app.services.federated_run import (
    DetachedAskTurn,
    FederatedRunPlan,
    LibraryOutcome,
    current_detached_ask_turn,
    current_federated_run_plan,
)
from app.services.global_run import global_ask_run
from app.services.retrieval_participants import (
    ParticipantOverride,
    current_participant_override,
    federated_ask_active,
)
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import (
    citation_active_id,
    current_source_scope,
    peer_scope_ceiling_active,
    source_scope_context,
    subjectless_run_active,
)
from types import SimpleNamespace


_ACTOR = "user-global-run"
_ROOT = Path(__file__).resolve().parents[2]
_APP = _ROOT / "backend" / "app"
_MANAGER_PATH = "backend/app/services/global_run.py"


def _override(notebook_ids=("nb-a", "nb-b", "nb-c")) -> ParticipantOverride:
    return ParticipantOverride(
        notebook_ids=tuple(notebook_ids), tiers={}, attested_actor_id=_ACTOR,
    )


def _ceilings(notebook_ids=("nb-a", "nb-b", "nb-c")) -> dict:
    # ``nb-c`` 冻结到零个可见来源:全映射的要求就是它必须以 ``frozenset()``
    # 出现,而不是因为集合为假被跳过。
    return {
        notebook_id: ({f"src-{notebook_id}"} if notebook_id != "nb-c" else set())
        for notebook_id in notebook_ids
    }


def _turn() -> DetachedAskTurn:
    return DetachedAskTurn(conversation_id="conv-1", history="h", user_history="u")


def _plan() -> FederatedRunPlan:
    return FederatedRunPlan(
        phase_timeout_seconds=1.0,
        notebook_timeout_seconds=2.0,
        executor=object(),
        window=lambda: 4,
        cancel=SimpleNamespace(is_set=lambda: False),
        on_library=lambda notebook_id, outcome: None,
        on_evidence=lambda fingerprints: None,
    )


def _seats() -> tuple:
    """四个读口的当下取值,顺序固定:覆盖 / scope / turn / plan。"""
    return (
        current_participant_override(),
        current_source_scope(),
        current_detached_ask_turn(),
        current_federated_run_plan(),
    )


def _nothing_installed() -> bool:
    return all(seat is None for seat in _seats())


# ---------------------------------------------------------------------------
# 1. 四件事同时装、同时拆
# ---------------------------------------------------------------------------

def test_one_manager_installs_all_four():
    """块内四者同时非空、块外同时为空,且两侧判据同时为真。"""
    override, ceilings = _override(), _ceilings()

    assert _nothing_installed()
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with global_ask_run(
            override, ceilings, _turn(), _plan(), nominal_active="nb-a",
        ):
            seats = _seats()
            assert all(seat is not None for seat in seats)
            assert seats[0] is override

            scope = seats[1]
            assert scope.notebook_id == "nb-a"
            assert scope.subjectless is True
            # 全映射:含名义 active,含可见来源为空的那个库。
            assert scope.source_ceiling_for("nb-a") == frozenset({"src-nb-a"})
            assert scope.source_ceiling_for("nb-c") == frozenset()
            # 1.5 冻结的五个后果里能在这里直接断言的三个。
            assert scope.source_provided is False and scope.base_provided is False
            assert scope.restricted is False and scope.ceiling_active is False
            assert scope.narrowed is None

            assert seats[2].conversation_id == "conv-1"
            assert seats[3].window() == 4

            # 检索侧与 ask 侧两个判据同时为真——这正是管理器存在的理由。
            assert federated_ask_active() is True
            assert subjectless_run_active() is True
            assert peer_scope_ceiling_active() is True

    assert _nothing_installed()
    assert federated_ask_active() is False
    assert subjectless_run_active() is False


# ---------------------------------------------------------------------------
# 2. 半装必须响亮失败,且不留下半装状态
# ---------------------------------------------------------------------------

def _missing_key() -> tuple:
    ceilings = _ceilings()
    ceilings.pop("nb-b")
    return ceilings, "nb-a"


def _extra_key() -> tuple:
    ceilings = _ceilings()
    ceilings["nb-outsider"] = {"src-outsider"}
    return ceilings, "nb-a"


def _wrong_anchor() -> tuple:
    return _ceilings(), "nb-b"


def _empty_ceilings() -> tuple:
    return {}, "nb-a"


@pytest.mark.parametrize("case", [
    _missing_key, _extra_key, _wrong_anchor, _empty_ceilings,
], ids=["missing-key", "extra-key", "wrong-anchor", "no-ceilings"])
def test_half_install_raises(case):
    """三类半装 + 一类空天花板,四条都抛,且抛出后四个读口全为空。

    「抛出后全为空」不是顺带一句:检查若发生在装了第一件之后,一次失败就会留下
    一个装了覆盖而没装天花板的上下文,而那恰恰是最危险的那一半。
    """
    ceilings, nominal_active = case()

    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with pytest.raises(ParticipantOverrideError):
            with global_ask_run(
                _override(), ceilings, _turn(), _plan(),
                nominal_active=nominal_active,
            ):  # pragma: no cover - 进不到块内才是对的
                raise AssertionError("半装的 run 不该进入块内")
        assert _nothing_installed()

    assert _nothing_installed()


def test_empty_source_list_must_be_present_not_skipped():
    """正对照:可见来源为空的库写成 ``frozenset()`` 就通过,被跳过就报错。

    没有这一条,上面那条参数化只证明了「缺键会抛」,而实现者最容易写出的缺键恰
    恰是 ``{nid: s for nid, s in ... if s}``。
    """
    override = _override()
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with global_ask_run(
            override, _ceilings(), _turn(), _plan(), nominal_active="nb-a",
        ):
            assert current_source_scope().source_ceiling_for("nb-c") == frozenset()

        skipped = {
            notebook_id: sources
            for notebook_id, sources in _ceilings().items() if sources
        }
        with pytest.raises(ParticipantOverrideError):
            with global_ask_run(
                override, skipped, _turn(), _plan(), nominal_active="nb-a",
            ):  # pragma: no cover
                raise AssertionError("跳过空来源库的天花板不该被接受")


# ---------------------------------------------------------------------------
# 3. 只有 global_ask 装,且无主体位只由管理器写
# ---------------------------------------------------------------------------

def _app_sources() -> dict:
    return {
        path.relative_to(_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(_APP.rglob("*.py"))
    }


def _call_sites(sources: dict, function_name: str) -> set:
    """调用 ``function_name`` 的文件集合,别名也算得到。

    记账记的是 import 的**原名**:``from ... import global_ask_run as go`` 之后
    ``go(...)`` 同样是一个安装点,只看字面名的扫描对它是瞎的。
    """
    found: set = set()
    for path, source in sources.items():
        tree = ast.parse(source, filename=path)
        local_names = {function_name}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == function_name:
                        local_names.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else ""
            )
            if name in local_names:
                found.add(path)
    return found


def _subjectless_literal_sites(sources: dict) -> set:
    """把 ``subjectless=True`` 当**字面实参**传出去的文件集合。

    只认 ``Constant(True)``:``source_scope.py`` 内部的 ``subjectless=subjectless``
    是形参转发而不是一次「我宣布这次 run 没有主体」的决定,不该被算进来。
    """
    found: set = set()
    for path, source in sources.items():
        if "subjectless" not in source:
            continue
        for node in ast.walk(ast.parse(source, filename=path)):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "subjectless"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    found.add(path)
    return found


def test_only_global_ask_installs():
    """安装管理器的调用点 ⊆ ``{app/services/global_ask.py}``,无主体位 ⊆ 管理器自己。

    ⊆ 而不是相等:今天两者一个是**空集**(D1-4 才接线,生产上没有任何地方进入
    全局模式),接线之后恰好是那一个文件。⊆ 的判据从今天到接线后都成立,而任何
    第三个文件出现即报红——那才是这条守卫要拦的东西:安装参与集是一次授权动作,
    检索层任何一处自己装一份,就是自己给自己发的授权。

    第二半同理:``subjectless=True`` 是「这次 run 没有当前库」这句话的唯一写入
    口,写在第二个文件里就等于绕过管理器的全映射断言,把 ask 侧单独切进对等模式。
    """
    sources = _app_sources()
    assert sources, "扫空了,守卫会恒真通过"
    assert _MANAGER_PATH in sources, _MANAGER_PATH

    installers = _call_sites(sources, "global_ask_run")
    assert installers <= {"backend/app/services/global_ask.py"}, installers

    declarers = _subjectless_literal_sites(sources)
    assert declarers <= {_MANAGER_PATH}, declarers
    # 正对照:守卫不是恒真的——管理器自己必须被扫到。
    assert declarers == {_MANAGER_PATH}


def test_call_site_guard_sees_through_an_alias():
    """别名分支自己要有用例,否则它是一条永不触发的死守卫。"""
    assert _call_sites({
        "backend/app/x/aliased.py":
            "from app.services.global_run import global_ask_run as go\n"
            "with go(o, c, t, p, nominal_active='nb-a'):\n    pass\n",
        "backend/app/x/qualified.py":
            "import app.services.global_run as m\n"
            "m.global_ask_run(o, c, t, p)\n",
        "backend/app/x/innocent.py": "global_ask_runner()\n",
    }, "global_ask_run") == {
        "backend/app/x/aliased.py", "backend/app/x/qualified.py",
    }

    assert _subjectless_literal_sites({
        "backend/app/x/declares.py": "f(subjectless=True)\n",
        "backend/app/x/forwards.py": "f(subjectless=subjectless)\n",
        "backend/app/x/off.py": "f(subjectless=False)\n",
    }) == {"backend/app/x/declares.py"}


# ---------------------------------------------------------------------------
# 4. 线程本地
# ---------------------------------------------------------------------------

def test_context_local_across_threads():
    """两条线程各装各的,在对方仍在块内时互不串。

    ``Barrier`` 握手而不是 sleep:两条线程必须在**都装好**的那一刻互相观察,
    墙钟等待在慢 runner 上要么变成假绿(一条已经退出)要么变成 flake。
    """
    barrier = threading.Barrier(2)
    seen: dict = {}
    errors: list = []

    def run(name, notebook_ids):
        try:
            override = _override(notebook_ids)
            with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
                with global_ask_run(
                    override, _ceilings(notebook_ids),
                    DetachedAskTurn(conversation_id=f"conv-{name}"), _plan(),
                    nominal_active=notebook_ids[0],
                ):
                    barrier.wait(timeout=10)
                    seen[name] = (
                        current_participant_override().notebook_ids,
                        current_source_scope().notebook_id,
                        current_detached_ask_turn().conversation_id,
                    )
                    barrier.wait(timeout=10)
        except BaseException as exc:  # noqa: BLE001 - 断言失败也要带回主线程
            errors.append(exc)
            barrier.abort()

    threads = [
        threading.Thread(target=run, args=("a", ("nb-a", "nb-b", "nb-c"))),
        threading.Thread(target=run, args=("x", ("nb-x", "nb-y", "nb-c"))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    assert not errors, errors
    assert seen["a"] == (("nb-a", "nb-b", "nb-c"), "nb-a", "conv-a")
    assert seen["x"] == (("nb-x", "nb-y", "nb-c"), "nb-x", "conv-x")
    # 主线程从头到尾什么都没装。
    assert _nothing_installed()


# ---------------------------------------------------------------------------
# 5. 块内异常把四个座位全部复位
# ---------------------------------------------------------------------------

def test_exception_in_body_resets_everything():
    """取消/失败路径尤其要复位:线程要回到池子里,不能带着覆盖回去。"""
    class _Boom(Exception):
        pass

    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with pytest.raises(_Boom):
            with global_ask_run(
                _override(), _ceilings(), _turn(), _plan(),
                nominal_active="nb-a",
            ):
                assert not _nothing_installed()
                raise _Boom()
        assert _nothing_installed()

    assert _nothing_installed()


# ---------------------------------------------------------------------------
# 6. 回归:「有逐库天花板」≠「没有主体库」
# ---------------------------------------------------------------------------

class _ExplodingLaneHost:
    def __getattr__(self, name):  # pragma: no cover - 只在闸误判时触发
        raise AssertionError(f"selected-source-graph lane touched host.{name}")


def test_single_notebook_ceiling_is_not_subjectless():
    """单库 run 冻结自己的来源清单时,ask 侧四条闸全部保持单库行为。

    这个形状今天就构造得出来(``test_knowledge_context_source_ceiling.py`` 里就
    有一份),而 D0 的 ask 侧闸读的是 ``peer_scope_ceiling_active()`` —— 对它同样
    为真。也就是说:任何单库调用方哪天冻结自己的来源列表,就会静默滑进对等模式,
    把自己的私有 Memory 关掉、把 ``index_required`` 永久压成 False、把自己的引用
    归属抹空。显式无主体位就是为了让这条不可能发生。
    """
    memory = SimpleNamespace(
        notebook_memory_hits=lambda user_id, notebook_id, query, limit: ["hit"],
    )
    service = SimpleNamespace(
        memory_retriever=memory,
        scale_index_probe=lambda notebook_id: False,
        scale_profiles=lambda: SimpleNamespace(
            requires_index=lambda notebook_id, has_disk_index: True
        ),
    )
    lane_host = SimpleNamespace(
        selected_source_graph=None,
        retrieval_contributors=_ExplodingLaneHost(),
        retrieval_connection_probe=lambda: True,
    )

    with source_scope_context(
        "nb-a", None, None, notebook_source_ceilings={"nb-a": {"src-1"}},
    ):
        # 过滤口径为真(冻结清单必须绑),模式口径为假(仍然有当前库)。
        assert peer_scope_ceiling_active() is True
        assert subjectless_run_active() is False

        assert AskService._memory_hits(service, "u", "nb-a", "q") == ["hit"]
        assert AskService._needs_index(service, "nb-a") is True
        assert citation_active_id("nb-a") == "nb-a"
        with pytest.raises(AssertionError, match="touched host"):
            AskService._activate_selected_source_graph(
                lane_host, "nb-a", [],
            )


# ---------------------------------------------------------------------------
# 7. 无主体位本身的形状合同
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"notebook_source_ceilings": None},
    {"notebook_source_ceilings": {"nb-b": {"s"}}},
], ids=["no-ceilings", "anchor-not-covered"])
def test_subjectless_requires_a_total_ceiling_over_the_anchor(kwargs):
    """``source_scope_context`` 自己也拒绝不成形的无主体安装。

    第一条尤其重要:天花板缺席时 ``source_scope_context`` 的短路分支会**什么都不
    装**然后静默返回,每条闸都会答「单库」——校验必须发生在那个短路之前。
    """
    with pytest.raises(ValueError):
        with source_scope_context(
            "nb-a", None, None, subjectless=True, **kwargs,
        ):  # pragma: no cover
            raise AssertionError("不成形的无主体 scope 不该装上")
    assert current_source_scope() is None


def test_subjectless_refuses_a_submitted_local_or_library_dimension():
    """提交了本地/库维度 = 这次 run 有一个库的勾选,那它就有主体。"""
    ceilings = {"nb-a": {"s"}}
    with pytest.raises(ValueError):
        with source_scope_context(
            "nb-a", {"mode": "include", "source_ids": ["s"]}, None,
            notebook_source_ceilings=ceilings, subjectless=True,
        ):  # pragma: no cover
            raise AssertionError("提交了本地维度的无主体 scope 不该装上")
    with pytest.raises(ValueError):
        with source_scope_context(
            "nb-a", None, {"mode": "include", "notebook_ids": ["nb-b"]},
            notebook_source_ceilings=ceilings, subjectless=True,
        ):  # pragma: no cover
            raise AssertionError("提交了库维度的无主体 scope 不该装上")
    assert current_source_scope() is None


# ---------------------------------------------------------------------------
# 8. 逐库回执的词汇表
# ---------------------------------------------------------------------------

def test_library_outcome_validates_status_and_reason_together():
    """回执的两半必须自洽:跳过要有原因码,答了的库不许带原因码。

    空原因码的跳过会渲染成一条**空的**中文披露,而带原因码的「已作答」会把一次
    失败码挂到一个正常回答的库上。字段各自合法、组合非法,正是构造期该拦的那类。
    """
    assert LibraryOutcome(status="ok", candidate_count=3).skipped is False
    assert LibraryOutcome(status="skipped", reason="timeout").skipped is True

    for kwargs in (
        {"status": "partial"},
        {"status": "skipped"},
        {"status": "skipped", "reason": "invented"},
        {"status": "ok", "reason": "timeout"},
        {"status": "ok", "candidate_count": -1},
    ):
        with pytest.raises(ValueError):
            LibraryOutcome(**kwargs)
