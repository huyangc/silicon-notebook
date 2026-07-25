"""scripts/kg_quality_audit.py 的诚实性守卫。

这个脚本的全部价值在于「数字可信」,所以守的是三件容易悄悄退化的事:
  1. 抽样时必须在报告里明说是抽样(绝不静默降级成看似全量的结论);
  2. 中文库上必须打出 `len(raw) <= 2` 丢弃双字术语的警示,以及
     claim_degraded 只覆盖英文的免责;
  3. 全程只读 —— 跑完产品数据一个字节没变。

关于第 3 条的边界:WAL 模式下只读打开仍可能创建/触碰 `-wal` / `-shm`(SQLite 需要
它们才能读到最新快照),所以这里守的是「**数据**不变 + 主库文件大小不变」,而不是
「一个文件都不碰」—— 后者不是这个脚本能给的保证,写成那样就是假承诺。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def load_audit():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "kg_quality_audit", SCRIPTS / "kg_quality_audit.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _build_db(path: Path, *, sources: int, concepts_per_source, claim_name,
              sourceless: int = 0, untagged_edges: int = 0) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE notebooks (id TEXT PRIMARY KEY, name TEXT, tier TEXT);
        CREATE TABLE sources (id TEXT PRIMARY KEY, notebook_id TEXT);
        CREATE TABLE knowledge_objects (
            id TEXT PRIMARY KEY, notebook_id TEXT, object_type TEXT,
            source_id TEXT, payload TEXT, evidence TEXT);
        CREATE TABLE knowledge_relations (
            id TEXT PRIMARY KEY, notebook_id TEXT, source_object_id TEXT,
            target_object_id TEXT, edge_type TEXT, evidence TEXT);
        CREATE TABLE concept_whitelist (term TEXT PRIMARY KEY);
        """
    )
    conn.execute("INSERT INTO notebooks VALUES ('nb-1','测试库','personal')")
    for s in range(sources):
        sid = f"src-{s}"
        conn.execute("INSERT INTO sources VALUES (?,'nb-1')", (sid,))
        for i, name in enumerate(concepts_per_source(s)):
            conn.execute(
                "INSERT INTO knowledge_objects VALUES (?,'nb-1','concept',?,?,?)",
                (f"o-{s}-{i}", sid, json.dumps({"name": name}),
                 json.dumps([{"element_id": "e1"}])),
            )
        conn.execute(
            "INSERT INTO knowledge_objects VALUES (?,'nb-1','claim',?,?,?)",
            (f"c-{s}", sid, json.dumps({"name": claim_name(s)}),
             json.dumps([{"element_id": "e1"}])),
        )
    # 晋升 / Memory→KG 写路径:source_id 是字面量 ''(见 governance_store.py)
    for i in range(sourceless):
        conn.execute(
            "INSERT INTO knowledge_objects VALUES (?,'nb-1','concept','',?,?)",
            (f"promoted-{i}", json.dumps({"name": f"promoted concept {i}"}),
             json.dumps([{"element_id": "e1"}])),
        )
    # knowhow 投影写的 about 边:evidence='[]',与 LLM 抽取的边不可区分
    for i in range(untagged_edges):
        conn.execute(
            "INSERT INTO knowledge_relations VALUES (?,'nb-1',?,?, 'about','[]')",
            (f"rel-{i}", "o-0-0", f"c-{i % max(1, sources)}"),
        )
    conn.commit()
    conn.close()


def test_sampling_is_declared_and_never_passes_as_full(tmp_path, capsys):
    audit = load_audit()
    db = tmp_path / "s.db"
    _build_db(
        db, sources=40,
        concepts_per_source=lambda s: [f"concept alpha {s}", f"concept beta {s}"],
        claim_name=lambda s: f"Transistor {s} provides gain in saturation region.",
    )
    assert audit.main(["--db", str(db), "--notebook", "nb-1",
                       "--sources", "5", "--no-samples"]) == 0
    out = capsys.readouterr().out
    assert "随机 5/40 个来源" in out, "抽样口径必须写进报告"
    assert "以下所有比例来自抽样,不是全量" in out
    assert "--sources 0" in out, "必须给出取全量的办法"


def test_full_scope_is_labelled_full(tmp_path, capsys):
    audit = load_audit()
    db = tmp_path / "f.db"
    _build_db(
        db, sources=3,
        concepts_per_source=lambda s: [f"concept alpha {s}"],
        claim_name=lambda s: f"Transistor {s} provides gain in saturation region.",
    )
    assert audit.main(["--db", str(db), "--notebook", "nb-1",
                       "--sources", "0", "--no-samples"]) == 0
    out = capsys.readouterr().out
    assert "全部 3 个来源" in out
    assert "以下所有比例来自抽样" not in out


def test_cjk_library_gets_both_english_only_caveats(tmp_path, capsys):
    """中文库上必须同时报出:双字术语被 filter 丢掉、claim_degraded 口径失效。"""
    audit = load_audit()
    db = tmp_path / "cjk.db"
    _build_db(
        db, sources=4,
        # 三字及以上的中文概念存在、两字的一个也没有 —— 正是 `len(raw) <= 2` 生效的痕迹
        concepts_per_source=lambda s: [f"金汇兑本位制{s}", "布雷顿森林体系", "金本位"],
        claim_name=lambda s: f"浮动汇率制度使得货币当局无须持有大量外汇储备{s}。",
    )
    assert audit.main(["--db", str(db), "--notebook", "nb-1",
                       "--sources", "0", "--no-samples"]) == 0
    out = capsys.readouterr().out
    assert "2 字档为 0、3 字档非 0" in out
    assert "len(raw) <= 2" in out
    assert "只覆盖英文" in out, "中文库上必须声明 claim_degraded 数字无效"


def test_run_leaves_product_data_byte_identical(tmp_path, capsys):
    audit = load_audit()
    db = tmp_path / "ro.db"
    _build_db(
        db, sources=3,
        concepts_per_source=lambda s: [f"concept alpha {s}"],
        claim_name=lambda s: f"Transistor {s} provides gain in saturation region.",
    )
    # 走 WAL 模式:生产库就是 WAL,而 WAL 恰恰是最容易被诊断工具误 checkpoint 的地方。
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("INSERT INTO sources VALUES ('src-extra','nb-1')")
    conn.commit()
    conn.close()

    def data_fingerprint():
        probe = sqlite3.connect(db)
        try:
            return {
                table: probe.execute(
                    f"SELECT COUNT(*), COALESCE(GROUP_CONCAT(t.rowid),'') "
                    f"FROM (SELECT rowid FROM {table} ORDER BY rowid) t"
                ).fetchone()
                for table in ("notebooks", "sources", "knowledge_objects",
                              "knowledge_relations", "concept_whitelist")
            }
        finally:
            probe.close()

    before_data = data_fingerprint()
    before_size = db.stat().st_size
    assert audit.main(["--db", str(db), "--notebook", "nb-1",
                       "--sources", "0", "--no-samples"]) == 0
    capsys.readouterr()
    # 数据不变 + 主库文件不增长。-wal/-shm 是 SQLite 读 WAL 快照的必需品,
    # 只读打开可能重建它们(见 open_readonly 的 docstring),不在断言范围内。
    assert data_fingerprint() == before_data, "诊断改动了产品数据"
    assert db.stat().st_size == before_size, "主库文件大小变化 —— 有写入发生"


def test_no_samples_prints_zero_content(tmp_path, capsys):
    """--no-samples 承诺「只出数字」,那就一个概念名/命题/公式原文都不能漏出来。

    第 1 轮 codex 评审(P2)抓到的:probe 的 e.g.、最重复名、meta-claim 例句、公式
    例子都曾无条件打印,与文档承诺不符。
    """
    audit = load_audit()
    db = tmp_path / "quiet.db"
    secret = "SECRETCONCEPTNAME"
    _build_db(
        db, sources=6,
        # 每个来源都放同一个名字 → 必进「最重复」;带下划线 → 必被 probe 判 symbol
        concepts_per_source=lambda s: [secret, f"{secret}_sym", "Q1"],
        claim_name=lambda s: f"This chapter covers {secret} in detail.",
    )
    assert audit.main(["--db", str(db), "--notebook", "nb-1",
                       "--sources", "0", "--no-samples"]) == 0
    out = capsys.readouterr().out
    assert secret not in out, "--no-samples 下仍打印了概念名/命题原文"
    assert "e.g." not in out, "--no-samples 下仍打印了示例"
    assert "不打印任何概念名" in out, "报告头必须声明当前是无内容口径"
    # 反向:默认口径下这些内容是要出现的,否则上面的断言会因为「压根没数据」而假绿
    assert audit.main(["--db", str(db), "--notebook", "nb-1", "--sources", "0"]) == 0
    assert secret in capsys.readouterr().out


def test_sourceless_objects_are_declared_and_included_in_full_mode(tmp_path, capsys):
    """晋升 / Memory→KG 的对象 source_id='',按来源永远抽不到 —— 不能装作不存在。"""
    audit = load_audit()
    db = tmp_path / "promo.db"
    _build_db(
        db, sources=30, sourceless=25,
        concepts_per_source=lambda s: [f"concept alpha {s}"],
        claim_name=lambda s: f"Transistor {s} provides gain in saturation region.",
    )
    # 抽样模式:必须报出数量并说明未纳入
    assert audit.main(["--db", str(db), "--notebook", "nb-1",
                       "--sources", "4", "--no-samples"]) == 0
    sampled = capsys.readouterr().out
    assert "不挂来源的对象: 25" in sampled
    assert "一个都抽不到,未纳入下面的分析" in sampled

    # 全量模式:必须真的读进来
    assert audit.main(["--db", str(db), "--notebook", "nb-1",
                       "--sources", "0", "--no-samples"]) == 0
    full = capsys.readouterr().out
    assert "25 个不挂来源的对象" in full
    assert "已排除 25 个不挂来源的对象" in full, "DF 不能把空 source_id 当成一篇文档"


def test_untagged_edges_are_not_credited_to_the_llm(tmp_path, capsys):
    """knowhow 投影写的 about 边 evidence='[]',与 LLM 抽取的边不可区分。"""
    audit = load_audit()
    db = tmp_path / "edges.db"
    _build_db(
        db, sources=4, untagged_edges=6,
        concepts_per_source=lambda s: [f"concept alpha {s}"],
        claim_name=lambda s: f"Transistor {s} provides gain in saturation region.",
    )
    assert audit.main(["--db", str(db), "--notebook", "nb-1",
                       "--sources", "0", "--no-samples"]) == 0
    out = capsys.readouterr().out
    assert "未标注(LLM 抽取或 knowhow 投影)" in out
    assert "LLM 抽取  " not in out, "无 basis 的边不得被单独标成 LLM 抽取"
    assert "不可区分" in out


def test_cjk_warning_is_phrased_as_risk_not_as_established_loss(tmp_path, capsys):
    """直方图证明不了丢弃(语料可能天然没有双字词,抽样也可能漏)。措辞必须停在风险。"""
    audit = load_audit()
    db = tmp_path / "cjk2.db"
    _build_db(
        db, sources=4,
        concepts_per_source=lambda s: [f"金汇兑本位制{s}", "布雷顿森林体系", "金本位"],
        claim_name=lambda s: f"浮动汇率制度使得货币当局无须持有大量外汇储备{s}。",
    )
    assert audit.main(["--db", str(db), "--notebook", "nb-1",
                       "--sources", "0", "--no-samples"]) == 0
    out = capsys.readouterr().out
    assert "风险提示" in out
    assert "直方图不构成证据" in out
    assert "正在丢弃" not in out, "不得把形状相符说成已确认的丢弃"


def test_missing_tables_fail_loudly_instead_of_skipping(tmp_path):
    audit = load_audit()
    db = tmp_path / "bad.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE notebooks (id TEXT PRIMARY KEY, name TEXT, tier TEXT)")
    conn.commit()
    conn.close()
    try:
        audit.main(["--db", str(db)])
    except SystemExit as exc:
        assert "缺少必需的表" in str(exc)
    else:  # pragma: no cover - 回归时才会走到
        raise AssertionError("缺表必须报错退出,不能静默跳过整节")
