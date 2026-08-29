"""R2-5(热路径修复批 2 / 审计 P1-15):stale manifest 身份比对的轻量化。

现场:``ScaleArtifactCatalog._stale_manifest_admissible`` 每次都用整份
``read_manifest`` 做身份比对,而生产 manifest 的 ``watermark_sources`` 有 48k
个元素(≈2MB JSON);冷路径一次 ``load(allow_stale=True)`` 调它两次,一次提问
又要 5–10 次 ``_scale_index(allow_stale=True)`` —— 每次提问 10–20MB 的 JSON
解析,只为读出 ``version`` 与 ``pipeline_identity`` 两个字段。

改法与取舍(为什么不是「轻读头部字段」)写在 ``_manifest_identity`` 的
docstring 里:JSON 没有部分解析,``read_manifest_version`` 省的也只是返回值;
真正的轻读要把 watermark 拆成独立文件,那是工件格式改动,本批不做。这里按磁盘
stat 签名 memo 解析结果。

本文件是那次改动的等价 oracle:把改造**前**的正文原样抄下来,对同一份磁盘状态
逐条比对身份判定(含损坏 manifest 的兜底分支),再钉住「同一份工件只解析一次」
与「工件一换就重新解析」。
"""
import json
import os

import pytest

from app.core.config import Settings
from app.domain.indexing_pipeline import BUILTIN_INDEXING_PIPELINE_VERSION
from app.repositories.filesystem.scale_artifact_store import ScaleArtifactStore
from app.services.scale_artifact_catalog import ScaleArtifactCatalog


def _legacy_stale_manifest_admissible(catalog, notebook_id):
    """改造前的 ``_stale_manifest_admissible`` 正文,原样抄成 oracle。"""
    try:
        manifest = catalog.artifacts.read_manifest(
            catalog.artifacts.scale_dir(notebook_id))
    except (OSError, ValueError):
        return None
    if manifest is None or manifest.get("version") is None:
        return None
    if catalog.pipeline_identity is not None:
        artifact_identity = list(
            manifest.get("pipeline_identity")
            or ["", BUILTIN_INDEXING_PIPELINE_VERSION]
        )
        if artifact_identity != list(catalog.pipeline_identity(notebook_id)):
            return None
    return manifest


def _catalog(tmp_path, *, pipeline_identity=None):
    settings = Settings(storage_dir=str(tmp_path))
    store = ScaleArtifactStore(settings)
    return ScaleArtifactCatalog(
        artifacts=store,
        settings=settings,
        version=lambda _notebook_id: ["v"],
        scale_cache=lambda: {},
        load_lock=lambda: None,
        load_locks=lambda: {},
        note_model_error=lambda *a, **k: None,
        pipeline_identity=pipeline_identity,
    )


def _write_manifest(catalog, notebook_id, payload, *, raw=None):
    directory = catalog.artifacts.scale_dir(notebook_id)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(str(directory), "manifest.json")
    with open(path, "w") as handle:
        handle.write(raw if raw is not None else json.dumps(payload))
    return path


def _big_manifest(version="ver-1", pipeline_identity=None):
    """带一份「大水位」的 manifest —— 正是这次改动要避免反复解析的那部分。"""
    manifest = {
        "version": version,
        "dim": 64,
        # 生产上是 48k 个元素;这里几千个就够体现「解析不是免费的」。
        "watermark_sources": [f"src-{i}" for i in range(4000)],
    }
    if pipeline_identity is not None:
        manifest["pipeline_identity"] = pipeline_identity
    return manifest


def _assert_same_verdict(catalog, notebook_id):
    """新旧两径对同一磁盘状态给出同一个身份判定。

    新实现返回的是身份**投影**({version, pipeline_identity}),旧实现返回整份
    manifest —— 所以「逐字一致」比的是判定本身(admit / 不 admit)与调用方真正
    消费的字段(``load()`` 只读 ``.get("version")``)。
    """
    expected = _legacy_stale_manifest_admissible(catalog, notebook_id)
    actual = catalog._stale_manifest_admissible(notebook_id)
    assert (actual is None) == (expected is None), (actual, expected)
    if expected is not None:
        assert actual["version"] == expected.get("version")
        assert actual["pipeline_identity"] == expected.get("pipeline_identity")
    return actual


@pytest.mark.parametrize(
    "case",
    ["missing", "valid", "legacy_without_pipeline_identity", "no_version",
     "corrupt_json", "non_object_json"],
)
def test_identity_verdict_matches_the_full_read_oracle(tmp_path, case):
    catalog = _catalog(tmp_path)
    notebook_id = "nb-oracle"
    if case == "valid":
        _write_manifest(catalog, notebook_id, _big_manifest())
    elif case == "legacy_without_pipeline_identity":
        _write_manifest(catalog, notebook_id, _big_manifest())
    elif case == "no_version":
        _write_manifest(catalog, notebook_id, {"dim": 64})
    elif case == "corrupt_json":
        _write_manifest(catalog, notebook_id, None, raw="{ truncated")
    elif case == "non_object_json":
        _write_manifest(catalog, notebook_id, None, raw="[]")
    # "missing":什么都不写

    verdict = _assert_same_verdict(catalog, notebook_id)
    if case in ("valid", "legacy_without_pipeline_identity"):
        assert verdict is not None and verdict["version"] == "ver-1"
    else:
        assert verdict is None


@pytest.mark.parametrize(
    "artifact_identity,current_identity,admitted",
    [
        (["plug", "2.0"], ("plug", "2.0"), True),
        (["plug", "2.0"], ("plug", "3.0"), False),
        (["plug", "2.0"], ("", BUILTIN_INDEXING_PIPELINE_VERSION), False),
        # legacy 工件缺 pipeline_identity → 按内建身份放行 / 与插件身份不符则拦。
        (None, ("", BUILTIN_INDEXING_PIPELINE_VERSION), True),
        (None, ("plug", "2.0"), False),
    ],
)
def test_pipeline_identity_gate_matches_the_oracle(
    tmp_path, artifact_identity, current_identity, admitted,
):
    """管线身份闸的每条分支都与旧实现同判(codex #602 R8 P1 的语义不得被
    R2-5 的投影/memo 改动碰到)。数据库那一侧仍然每次现读。"""
    catalog = _catalog(tmp_path, pipeline_identity=lambda _nb: current_identity)
    _write_manifest(catalog, "nb-gate",
                    _big_manifest(pipeline_identity=artifact_identity))

    verdict = _assert_same_verdict(catalog, "nb-gate")
    assert (verdict is not None) is admitted


def test_repeated_probes_parse_the_manifest_once(tmp_path, monkeypatch):
    """R2-5 的方向钉:同一份磁盘工件重复探测只解析一次。

    **变异锚点**:把 ``_manifest_identity`` 改回每次 ``read_manifest``(或让
    memo 永不命中)→ 解析计数变成 5,这条报红。等价 oracle 对那条变异天然是
    绿的(它就是 oracle 自己),所以「少解析」这一半只能由本条钉住。
    """
    catalog = _catalog(tmp_path)
    _write_manifest(catalog, "nb-memo", _big_manifest())

    parses = {"n": 0}
    real_read = catalog.artifacts.read_manifest
    monkeypatch.setattr(
        catalog.artifacts, "read_manifest",
        lambda directory: (parses.__setitem__("n", parses["n"] + 1),
                           real_read(directory))[1],
    )

    verdicts = [catalog._stale_manifest_admissible("nb-memo") for _ in range(5)]

    assert all(v is not None and v["version"] == "ver-1" for v in verdicts)
    assert parses["n"] == 1, (
        f"one disk artifact must be parsed once, parsed {parses['n']} times")


def test_a_republished_artifact_is_reparsed(tmp_path, monkeypatch):
    """工件换了(rebuild/fold 的 ``.tmp`` + 原子 rename)必须重新解析——否则
    memo 会把已经过时的 version 一直发给检索侧。

    这里用 rename 复现生产的原子换目录:新文件是**另一个 inode**,所以即使
    mtime/size 恰好撞上,签名也必然不同。
    """
    catalog = _catalog(tmp_path)
    _write_manifest(catalog, "nb-swap", _big_manifest(version="ver-1"))
    assert catalog._stale_manifest_admissible("nb-swap")["version"] == "ver-1"

    directory = str(catalog.artifacts.scale_dir("nb-swap"))
    staged = os.path.join(directory, "manifest.json.tmp")
    with open(staged, "w") as handle:
        json.dump(_big_manifest(version="ver-2"), handle)
    os.replace(staged, os.path.join(directory, "manifest.json"))

    assert catalog._stale_manifest_admissible("nb-swap")["version"] == "ver-2"


def test_a_corrupt_manifest_is_never_memoized(tmp_path):
    """损坏结论不进 memo(与 ``scale_manifest_identity`` 的既有约定一致):
    用户把工件修好之后,下一次探测就该看见,不必等任何东西过期。"""
    catalog = _catalog(tmp_path)
    _write_manifest(catalog, "nb-fix", None, raw="{ truncated")
    assert catalog._stale_manifest_admissible("nb-fix") is None

    # 原地写回一个健康 manifest —— 不换 inode,只有内容/大小/mtime 变。
    path = os.path.join(str(catalog.artifacts.scale_dir("nb-fix")), "manifest.json")
    with open(path, "w") as handle:
        json.dump(_big_manifest(version="ver-fixed"), handle)

    verdict = catalog._stale_manifest_admissible("nb-fix")
    assert verdict is not None and verdict["version"] == "ver-fixed"
