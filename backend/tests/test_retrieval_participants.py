"""C1 -- 参与集覆盖座位的行为合同。

结构性隔离(谁能 import、谁能写入、鉴权路径不许改道)在
``test_participant_override_guard.py``;本文件只钉这个模块自己的行为:覆盖
替换 fallback、身份复核 fail-closed、嵌套拒绝、context 本地性、指纹稳定性。
"""
from __future__ import annotations

import threading
from contextvars import copy_context

import pytest

from app.services.retrieval_participants import (
    ParticipantOverride,
    ParticipantOverrideError,
    current_participant_override,
    federated_ask_active,
    override_fingerprint,
    participant_override,
    resolve_retrieval_participants,
)
from app.services.retrieval_run import retrieval_run


_ACTOR = "user-1"
_BARRIER_DEADLOCK_GUARD_SECONDS = 10.0


def _override(notebook_ids, *, tiers=None, actor=_ACTOR) -> ParticipantOverride:
    return ParticipantOverride(
        notebook_ids=tuple(notebook_ids),
        tiers=dict(tiers or {}),
        attested_actor_id=actor,
    )


class _CountingFallback:
    """fallback 必须是「没被调用过」也可断言的,否则"覆盖替换了 fallback"与
    "覆盖恰好返回了和 fallback 一样的东西"两种情况看起来一模一样。"""

    def __init__(self, pairs) -> None:
        self.pairs = list(pairs)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return list(self.pairs)


def test_override_replaces_fallback():
    """覆盖在场 -> 返回覆盖集本身,且 fallback 一次都不调。

    "替换不是收窄":覆盖里的 nb-c 并未挂载在 fallback 的挂载集里,它仍然必须
    出现在结果里——这正是 source_scope 的收窄语义表达不出来的那一半。
    """
    fallback = _CountingFallback([("nb-a", "personal"), ("nb-z", "base")])
    override = _override(
        ["nb-a", "nb-b", "nb-c"], tiers={"nb-a": "personal", "nb-b": "base"},
    )

    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(override):
            assert federated_ask_active() is True
            resolved = resolve_retrieval_participants("nb-a", fallback)

    # 缺 tier 的 nb-c 按 personal 兜底,顺序逐字是覆盖声明的顺序。
    assert resolved == (
        ("nb-a", "personal"), ("nb-b", "base"), ("nb-c", "personal"),
    )
    assert fallback.calls == 0
    assert current_participant_override() is None
    assert federated_ask_active() is False


def test_absent_override_uses_fallback():
    """无覆盖 -> 真实挂载谓词的输出原样(只做 str 归一)。"""
    fallback = _CountingFallback([("nb-a", "personal"), ("nb-b", "base")])

    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        resolved = resolve_retrieval_participants("nb-a", fallback)

    assert resolved == (("nb-a", "personal"), ("nb-b", "base"))
    assert fallback.calls == 1

    # 连 ambient run 都没有时也一样:fail-closed 只针对「覆盖在场」,不能把
    # 今天所有无覆盖的检索路径一起判死。
    assert resolve_retrieval_participants("nb-a", fallback) == (
        ("nb-a", "personal"), ("nb-b", "base"),
    )
    assert fallback.calls == 2


def test_actor_mismatch_raises():
    """run 的 actor 与覆盖的 attested actor 不符 -> raise,不是静默回落。

    静默回落会返回一份"看起来合理"的挂载集,越权上下文泄漏因此永远不会被
    发现;这里要的是第一次使用就炸。
    """
    fallback = _CountingFallback([("nb-a", "personal")])
    override = _override(["nb-a", "nb-b"], actor="user-1")

    with retrieval_run(run_kind="ask_global", actor_id="user-2"):
        with participant_override(override):
            with pytest.raises(ParticipantOverrideError) as excinfo:
                resolve_retrieval_participants("nb-a", fallback)

    assert fallback.calls == 0
    # 消息不含用户数据:两个 actor id 都不许出现在异常文本里。
    message = str(excinfo.value)
    assert "user-1" not in message and "user-2" not in message


def test_no_ambient_run_raises():
    """覆盖在场但没有 ambient retrieval run -> raise(fail-closed)。

    没有 run 就没有可复核的身份。此时信任覆盖等于接受一份无法验证的声明,
    回落到挂载表则把接线 bug 变成一个"只是范围小了点"的静默结果。
    """
    fallback = _CountingFallback([("nb-a", "personal")])
    override = _override(["nb-a"])

    assert current_participant_override() is None
    with participant_override(override):
        with pytest.raises(ParticipantOverrideError):
            resolve_retrieval_participants("nb-a", fallback)

    assert fallback.calls == 0


def test_nominal_active_mismatch_raises():
    """覆盖只对它声明的那个名义 active 生效。

    另一个 active id 来解析参与集(报告腿、插件引擎、任何自带 notebook id 的
    消费者)时必须炸,否则它会悄悄继承本次 run 的覆盖集,检索进别的库。
    """
    fallback = _CountingFallback([("nb-x", "personal")])
    override = _override(["nb-a", "nb-b"])

    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(override):
            # [0] 才是名义 active:覆盖集里的第二个库同样不算。
            for wrong_active in ("nb-x", "nb-b"):
                with pytest.raises(ParticipantOverrideError):
                    resolve_retrieval_participants(wrong_active, fallback)
            # 对照臂:名义 active 本身照常解析。
            assert resolve_retrieval_participants("nb-a", fallback)[0][0] == "nb-a"

    assert fallback.calls == 0


def test_nested_override_is_rejected_and_reset_in_finally():
    """嵌套被拒,且外层不受影响;body 抛异常时 reset 仍然发生。"""
    outer = _override(["nb-a", "nb-b"])
    inner = _override(["nb-c"])

    with participant_override(outer):
        with pytest.raises(ParticipantOverrideError):
            with participant_override(inner):
                pytest.fail("嵌套覆盖必须在进入 body 之前就被拒绝")
        # 被拒的嵌套不许动外层:失败的 __enter__ 不能留下 token,也不能 reset
        # 掉别人的。
        assert current_participant_override() is outer
    assert current_participant_override() is None

    # reset 在 finally:body 抛出(取消路径就是这个形状)时不能把覆盖留在一条
    # 即将回到线程池的线程上。
    with pytest.raises(ZeroDivisionError):
        with participant_override(outer):
            raise ZeroDivisionError("body failed")
    assert current_participant_override() is None


def test_override_is_context_local():
    """两条线程各自 ``copy_context()`` 进不同覆盖,互不串。

    用 ``copy_context()`` 而不是裸线程:裸线程本来就是空 context,那样断言恒
    真、证明不了任何事。检索扇出恰恰是「父 context 的副本进工作线程」,这里
    复现的就是那个形状。``Barrier`` 保证两边都已经 set 之后才各读一次——不用
    墙钟,timeout 只是死锁护栏,不参与任何断言。
    """
    barrier = threading.Barrier(2)
    overrides = {
        "left": _override(["nb-a", "nb-b"]),
        "right": _override(["nb-c"]),
    }
    seen: dict[str, object] = {}
    failures: list[BaseException] = []

    def worker(name: str) -> None:
        try:
            with participant_override(overrides[name]):
                barrier.wait(timeout=_BARRIER_DEADLOCK_GUARD_SECONDS)
                seen[name] = current_participant_override()
        except BaseException as exc:  # noqa: BLE001 - re-raised via `failures`
            failures.append(exc)
            barrier.abort()

    threads = [
        threading.Thread(target=copy_context().run, args=(worker, name))
        for name in ("left", "right")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_BARRIER_DEADLOCK_GUARD_SECONDS)

    assert not failures, failures
    assert seen["left"] is overrides["left"]
    assert seen["right"] is overrides["right"]
    # 父 context 从未被任何一条线程写脏。
    assert current_participant_override() is None


def test_fingerprint_is_order_insensitive_and_stable():
    """同一组库、不同顺序 -> 同一个指纹;不同组 -> 不同指纹。

    缓存键要的是「哪些库」,不是「按什么顺序扇出」。指纹本身必须跨进程稳定
    (所以不是 ``hash()``),这里用一个写死的期望值钉住它,免得有人换算法时
    把已发布的缓存键静默换掉。
    """
    import hashlib

    forward = _override(["nb-a", "nb-b", "nb-c"])
    shuffled = _override(["nb-c", "nb-a", "nb-b"])
    subset = _override(["nb-a", "nb-b"])

    assert override_fingerprint(forward) == override_fingerprint(shuffled)
    assert override_fingerprint(forward) != override_fingerprint(subset)

    expected = hashlib.blake2s(
        "nb-a|nb-b|nb-c".encode("utf-8"), digest_size=8,
    ).hexdigest()
    assert override_fingerprint(forward) == expected
    assert len(expected) == 16

    # tier 与 actor 不进指纹:同一组库换一个 tier 仍然共用缓存条目,因为按它
    # 建的图只依赖成员集合。
    assert override_fingerprint(
        _override(["nb-a", "nb-b", "nb-c"], tiers={"nb-a": "base"}, actor="u9"),
    ) == expected


def test_construction_rejects_empty_duplicate_and_anonymous_overrides():
    """构造期校验:空集、重复 id、空 actor。

    在构造期而不是使用期:一个覆盖建一次、被扇出的每条腿读 N 次,留到使用期
    就变成 N 条来自工作线程深处的困惑报错,而不是建它那一行的一条。
    """
    with pytest.raises(ParticipantOverrideError):
        _override([])
    with pytest.raises(ParticipantOverrideError):
        _override(["nb-a", "nb-b", "nb-a"])
    with pytest.raises(ParticipantOverrideError):
        # 空 actor 会与「从未设置 actor_id 的 run」比对相等,把身份复核变成
        # 恰好在最要紧的时候失效的空转。
        _override(["nb-a"], actor="")


def test_override_is_immutable_after_attestation():
    """构造后不可变:tiers 是我们自己那份拷贝的只读视图。

    冻结 dataclass 若存着调用方的活体 dict,调用方仍可在鉴权通过之后改写
    tier 映射——那是对一个已授权对象的改写。
    """
    tiers = {"nb-a": "personal"}
    override = _override(["nb-a", "nb-b"], tiers=tiers)

    tiers["nb-b"] = "base"
    assert override.pairs() == (("nb-a", "personal"), ("nb-b", "personal"))
    with pytest.raises(TypeError):
        override.tiers["nb-b"] = "base"  # type: ignore[index]
