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
import re
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
              sourceless: int = 0, untagged_edges: int = 0,
              notebook_name: str = "测试库") -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE notebooks (id TEXT PRIMARY KEY, name TEXT, tier TEXT);
        CREATE TABLE sources (id TEXT PRIMARY KEY, notebook_id TEXT);
        -- status / review_status 的默认值与真实 schema 一致:产品按它们区分
        -- 「有效 KG」与「留在表里但不进检索的历史行」。
        CREATE TABLE knowledge_objects (
            id TEXT PRIMARY KEY, notebook_id TEXT, object_type TEXT,
            source_id TEXT, payload TEXT, evidence TEXT,
            status TEXT NOT NULL DEFAULT 'approved');
        CREATE TABLE knowledge_relations (
            id TEXT PRIMARY KEY, notebook_id TEXT, source_object_id TEXT,
            target_object_id TEXT, edge_type TEXT, evidence TEXT,
            review_status TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE concept_whitelist (term TEXT PRIMARY KEY);
        """
    )
    conn.execute("INSERT INTO notebooks VALUES ('nb-1',?,'personal')",
                 (notebook_name,))
    for s in range(sources):
        sid = f"src-{s}"
        conn.execute("INSERT INTO sources VALUES (?,'nb-1')", (sid,))
        for i, name in enumerate(concepts_per_source(s)):
            conn.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,source_id,payload,evidence) VALUES (?,'nb-1','concept',?,?,?)",
                (f"o-{s}-{i}", sid, json.dumps({"name": name}),
                 json.dumps([{"element_id": "e1"}])),
            )
        conn.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,source_id,payload,evidence) VALUES (?,'nb-1','claim',?,?,?)",
            (f"c-{s}", sid, json.dumps({"name": claim_name(s)}),
             json.dumps([{"element_id": "e1"}])),
        )
    # 晋升 / Memory→KG 写路径:source_id 是字面量 ''(见 governance_store.py)
    for i in range(sourceless):
        conn.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,source_id,payload,evidence) VALUES (?,'nb-1','concept','',?,?)",
            (f"promoted-{i}", json.dumps({"name": f"promoted concept {i}"}),
             json.dumps([{"element_id": "e1"}])),
        )
    # knowhow 投影写的 about 边:evidence='[]',与 LLM 抽取的边不可区分
    for i in range(untagged_edges):
        conn.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_object_id,target_object_id,edge_type,evidence) VALUES (?,'nb-1',?,?,'about','[]')",
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


def test_promotion_only_library_still_gets_full_concept_analysis(tmp_path, capsys):
    """DF 算不出来不是跳过整节的理由 —— 只有 DF 那一小节依赖 source_id。

    第 2 轮 codex 评审(P2)抓到的回归:上一轮修「不挂来源的对象」时加的早退，会把
    一个全部来自晋升的 base 库的噪声过滤器、probes、词数分布、CJK 诊断全部跳掉，
    而那批对象恰恰是 full 模式声称已纳入的。
    """
    audit = load_audit()
    db = tmp_path / "promo_only.db"
    # 零来源、只有晋升对象:df 必然为空
    _build_db(
        db, sources=0, sourceless=12,
        concepts_per_source=lambda s: [],
        claim_name=lambda s: "",
    )
    assert audit.main(["--db", str(db), "--notebook", "nb-1",
                       "--sources", "0", "--no-samples"]) == 0
    out = capsys.readouterr().out
    assert "DF 无从统计" in out
    assert "以下其余分析照常" in out
    # DF 之后的分析必须仍然跑过
    assert "is_noise_concept 命中" in out, "早退把噪声过滤器整节跳了"
    assert "classify_concept" in out, "早退把质量探针整节跳了"
    assert "按词数" in out, "早退把词数分布跳了"


def test_no_samples_also_redacts_notebook_titles(tmp_path, capsys):
    """笔记本名也是用户内容,且候选表会带出与本次目标无关的其它库。"""
    audit = load_audit()
    db = tmp_path / "titles.db"
    title = "TOPSECRETNOTEBOOKTITLE"
    _build_db(
        db, sources=3, notebook_name=title,
        concepts_per_source=lambda s: [f"concept alpha {s}"],
        claim_name=lambda s: f"Transistor {s} provides gain in saturation region.",
    )
    assert audit.main(["--db", str(db), "--notebook", "nb-1",
                       "--sources", "0", "--no-samples"]) == 0
    quiet = capsys.readouterr().out
    assert title not in quiet, "--no-samples 下仍打印了笔记本名称"
    assert "也不打印笔记本名称" in quiet
    assert "nb-1" in quiet, "选库必需的 id 不能一起隐掉"
    # 反向:默认口径下名称照常出现,免得断言因「压根没渲染」而假绿
    assert audit.main(["--db", str(db), "--notebook", "nb-1", "--sources", "0"]) == 0
    assert title in capsys.readouterr().out


def test_audit_scope_matches_the_product_predicates(tmp_path, capsys):
    """审计口径必须与产品一致,否则回答的是另一个问题。

    第 3 轮 codex 评审(P2)抓到的:合并/弃用的对象仍留在 knowledge_objects 里但不进
    检索,被评审否掉的边也不属于有效图 —— 都算进来会让治理做得越多的库显得越差。
    """
    audit = load_audit()
    db = tmp_path / "scope.db"
    _build_db(
        db, sources=2, untagged_edges=2,
        concepts_per_source=lambda s: [f"concept alpha {s}"],
        claim_name=lambda s: f"Transistor {s} provides gain in saturation region.",
    )
    conn = sqlite3.connect(db)
    # 一个被弃用的对象 + 一条被否掉的边,都必须被排除在指标之外
    conn.execute(
        "INSERT INTO knowledge_objects "
        "(id,notebook_id,object_type,source_id,payload,evidence,status) "
        "VALUES ('dead','nb-1','concept','src-0',?,'[]','deprecated')",
        (json.dumps({"name": "DEPRECATEDCONCEPT"}),),
    )
    conn.execute(
        "INSERT INTO knowledge_relations "
        "(id,notebook_id,source_object_id,target_object_id,edge_type,evidence,"
        "review_status) VALUES ('rej','nb-1','o-0-0','c-1','about',"
        "'[{\"basis\":\"relink:name-match\"}]','rejected')",
    )
    conn.commit()
    conn.close()

    assert audit.main(["--db", str(db), "--notebook", "nb-1", "--sources", "0"]) == 0
    out = capsys.readouterr().out
    assert "DEPRECATEDCONCEPT" not in out, "弃用对象不得进入内容分析"
    assert "1 个对象因状态不在有效集内被排除" in out, "排除量必须显式报出,不能凭空消失"
    assert "relink:name-match" not in out, "被评审否掉的边不得计入连通性统计"


def test_negative_sampling_arguments_fail_before_touching_the_db(tmp_path, capsys):
    """负数不能被当成 0(=全量):那会在千万级库上静默做一次极贵的全扫。"""
    audit = load_audit()
    missing = tmp_path / "never-opened.db"   # 不存在:若真打开了库会是另一种报错
    for flag in ("--sources", "--degree-sample"):
        try:
            audit.main(["--db", str(missing), flag, "-1"])
        except SystemExit as exc:
            assert exc.code == 2, f"{flag} 负数应按参数错误退出(码 2)"
        else:  # pragma: no cover - 回归时才会走到
            raise AssertionError(f"{flag} 接受了负数")
        assert "不能为负数" in capsys.readouterr().err


def test_object_loading_is_bounded_independently_of_source_count(tmp_path, capsys):
    """来源抽样对内存不构成约束 —— 上限必须按对象行数,且截断绝不静默。

    第 4 轮 codex 评审(P1)：来源数少于 --sources 时一个都不会被抽掉，单个来源也
    可能自己就有几百万对象；这个工具的目标恰恰是千万级库。
    """
    audit = load_audit()
    db = tmp_path / "huge.db"
    # 只有 2 个来源（远少于默认抽样值），但每个塞 60 个对象 —— 来源抽样拦不住
    _build_db(
        db, sources=2,
        concepts_per_source=lambda s: [f"concept {s}-{i}" for i in range(60)],
        claim_name=lambda s: f"Transistor {s} provides gain in saturation region.",
    )
    assert audit.main(["--db", str(db), "--notebook", "nb-1", "--sources", "0",
                       "--max-objects", "10", "--no-samples"]) == 0
    out = capsys.readouterr().out
    assert "读到 10 个对象" in out, "对象上限没有生效"
    assert "已达 --max-objects=10 上限" in out, "截断必须显式报出,不能静默"
    assert "前 10 个" in out, "必须说清截断后实际读到了多少"

    # 上限足够大时不应报截断，免得断言因「永远截断」而假绿
    assert audit.main(["--db", str(db), "--notebook", "nb-1", "--sources", "0",
                       "--max-objects", "0", "--no-samples"]) == 0
    uncapped = capsys.readouterr().out
    assert "已达 --max-objects" not in uncapped
    assert "读到 122 个对象" in uncapped

    # 上限恰好等于总行数：什么都没漏，不得报截断（第 5 轮 codex 评审 P2）
    assert audit.main(["--db", str(db), "--notebook", "nb-1", "--sources", "0",
                       "--max-objects", "122", "--no-samples"]) == 0
    exact = capsys.readouterr().out
    assert "已达 --max-objects" not in exact, "恰好读满上限而无剩余时不得报截断"
    assert "读到 122 个对象" in exact


def test_object_cap_also_bounds_sourceless_objects(tmp_path, capsys):
    """晋升 / Memory→KG 那批也得受上限约束,否则上限就是摆设。

    第 5 轮 codex 评审(P1)：--max-objects 只挡住了挂来源的那条路径。
    """
    audit = load_audit()
    db = tmp_path / "promo_cap.db"
    _build_db(
        db, sources=1, sourceless=50,
        concepts_per_source=lambda s: [f"concept alpha {s}"],
        claim_name=lambda s: f"Transistor {s} provides gain in saturation region.",
    )
    # 一个来源贡献 2 个对象 + 50 个不挂来源的。上限 5 是**总**预算：
    # 若不挂来源的那批另拿一份预算，总数会变成 2+5=7 而不是 5。
    assert audit.main(["--db", str(db), "--notebook", "nb-1", "--sources", "0",
                       "--max-objects", "5", "--no-samples"]) == 0
    out = capsys.readouterr().out
    assert "读到 5 个对象" in out, "不挂来源的对象必须共享同一份预算,不能各拿一份"
    assert "已达 --max-objects=5 上限" in out
    assert "不挂来源的对象: 50" in out, "这批的总量仍要报出来"

    # 不封顶时应读满 52，证明上面的 5 是被上限挡下的、不是压根没读到
    assert audit.main(["--db", str(db), "--notebook", "nb-1", "--sources", "0",
                       "--max-objects", "0", "--no-samples"]) == 0
    assert "读到 52 个对象" in capsys.readouterr().out


def test_edges_to_unusable_endpoints_do_not_count_as_connectivity(tmp_path, capsys):
    """对象合并后源对象被标 deprecated,其边仍在表里,但产品图里根本进不去。"""
    audit = load_audit()
    db = tmp_path / "endpoint.db"
    _build_db(
        db, sources=1,
        concepts_per_source=lambda s: [f"concept alpha {s}"],
        claim_name=lambda s: f"Transistor {s} provides gain in saturation region.",
    )
    conn = sqlite3.connect(db)
    # 一个已弃用的对端 + 一条指向它的正常边:该边不属于有效图
    conn.execute(
        "INSERT INTO knowledge_objects "
        "(id,notebook_id,object_type,source_id,payload,evidence,status) "
        "VALUES ('gone','nb-1','concept','src-0',?,'[]','deprecated')",
        (json.dumps({"name": "MERGEDAWAY"}),),
    )
    conn.execute(
        "INSERT INTO knowledge_relations "
        "(id,notebook_id,source_object_id,target_object_id,edge_type,evidence) "
        "VALUES ('e1','nb-1','o-0-0','gone','about',"
        "'[{\"basis\":\"relink:shared-element\"}]')",
    )
    conn.commit()
    conn.close()

    assert audit.main(["--db", str(db), "--notebook", "nb-1", "--sources", "0",
                       "--no-samples"]) == 0
    out = capsys.readouterr().out
    assert "relink:shared-element" not in out, "指向 deprecated 对端的边不得计入连通性"
    # 列宽不参与断言:只要求 concept 那行报出 1/1 全零度
    assert re.search(r"concept\s+1 / 1\b", out), \
        "只连着 deprecated 节点的节点应报为零度"


def test_custom_object_types_are_redacted_in_no_samples_mode(tmp_path, capsys):
    """knowhow 投影拿**用户列名**当 object_type,专有表头不能从「只出数字」的报告漏出。

    第 6 轮 codex 评审(P2)。内置四类是产品固定词汇,不算内容,照常显示。
    """
    audit = load_audit()
    db = tmp_path / "custom.db"
    heading = "客户专有列名"
    _build_db(
        db, sources=1,
        concepts_per_source=lambda s: [f"concept alpha {s}"],
        claim_name=lambda s: f"Transistor {s} provides gain in saturation region.",
    )
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO knowledge_objects "
        "(id,notebook_id,object_type,source_id,payload,evidence) "
        "VALUES ('kh','nb-1',?,'src-0',?,'[]')",
        (heading, json.dumps({"name": "row value"})),
    )
    conn.commit()
    conn.close()

    assert audit.main(["--db", str(db), "--notebook", "nb-1", "--sources", "0",
                       "--no-samples"]) == 0
    quiet = capsys.readouterr().out
    assert heading not in quiet, "--no-samples 下仍打印了自定义类型(用户表头)"
    assert "<自定义类型 #1>" in quiet, "脱敏后仍要能看出有几种自定义类型"
    assert "concept" in quiet, "内置类型不该被一起脱敏"
    # 反向:默认口径下照常显示,免得断言因「压根没渲染」而假绿
    assert audit.main(["--db", str(db), "--notebook", "nb-1", "--sources", "0"]) == 0
    assert heading in capsys.readouterr().out


def test_full_mode_covers_objects_whose_source_row_is_gone(tmp_path, capsys):
    """source_id 无外键约束:指向已删来源的对象不能在「全量」里被静默漏掉。

    第 6 轮 codex 评审(P2)。全量改为按 notebook 直查,不经 sources 表。
    """
    audit = load_audit()
    db = tmp_path / "orphan.db"
    # 3 个来源，这样下面的 --sources 1 才真的是抽样而不是退化成全量
    _build_db(
        db, sources=3,
        concepts_per_source=lambda s: [f"concept alpha {s}"],
        claim_name=lambda s: f"Transistor {s} provides gain in saturation region.",
    )
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO knowledge_objects "
        "(id,notebook_id,object_type,source_id,payload,evidence) "
        "VALUES ('orphan','nb-1','concept','src-deleted',?,'[]')",
        (json.dumps({"name": "ORPHANEDCONCEPT"}),),
    )
    conn.commit()
    conn.close()

    assert audit.main(["--db", str(db), "--notebook", "nb-1", "--sources", "0"]) == 0
    full = capsys.readouterr().out
    assert "孤儿来源引用: 1" in full, "孤儿引用的数量必须报出"
    assert "ORPHANEDCONCEPT" in full, "全量模式必须真的读到它,不能只报数不覆盖"
    assert "读到 7 个对象" in full, "3 来源 × (1 概念 + 1 命题) + 1 个孤儿"

    # 抽样模式抽不到它,必须明说
    assert audit.main(["--db", str(db), "--notebook", "nb-1", "--sources", "1",
                       "--no-samples"]) == 0
    assert "未纳入下面的分析" in capsys.readouterr().out


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
