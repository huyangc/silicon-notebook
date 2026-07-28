import concurrent.futures as cf
import threading
import time
from types import SimpleNamespace

import pytest

from app.models.schemas import SourceElement
from app.services import kg_ingest
from app.services.kg.models import Node, Edge, Evidence, KnowledgeGraph
from app.services.kg.parsing import SourceElementQ
from app.services.kg.run_control import KgBuildAborted, KgBuildFailure


def _el(i, text):
    return SourceElement(id=i, source_id="s1", element_type="paragraph",
                         location_label=f"p{i}", text=text)


def test_build_records_binds_and_drops():
    g = KnowledgeGraph(doc_id="doc.md", doc_type="academic",
        nodes=[
            Node(id="C1", type="Concept", name="Engram",
                 evidence=[Evidence(file="doc.md", char_start=0, char_end=6,
                                    line_start=1, line_end=1, quote="Engram")]),
            Node(id="C2", type="Concept", name="Nowhere",
                 evidence=[Evidence(file="doc.md", char_start=0, char_end=3,
                                    line_start=1, line_end=1, quote="zzz")]),
        ],
        edges=[Edge(id="E1", type="about", source_id="C1", target_id="C2")])
    elements = [_el("e1", "Engram is a memory architecture.")]
    objects, relations = kg_ingest.build_records(
        g, source_id="s1", source_title="Doc", elements=elements)
    assert [o["object_type"] for o in objects] == ["concept"]   # C2 dropped (unbound)
    assert objects[0]["payload"]["name"] == "Engram"
    assert objects[0]["evidence"][0]["element_id"] == "e1"
    assert objects[0]["local_id"] == "C1"                        # carried for edge wiring
    assert relations == []                                       # edge dropped: C2 gone


def test_bind_quote_fuzzy_fallback():
    """Quote that is NOT an exact normalized substring but shares >=60% tokens."""
    # Element text: "Engram is a memory architecture"
    # Quote: "Engram memory architecture model"
    # Exact substring check: "engram memory architecture model" NOT in "engram is a memory architecture" -> fails
    # Token overlap: qt = {engram, memory, architecture, model} (4 tokens)
    #                et = {engram, is, a, memory, architecture}
    #                intersection = {engram, memory, architecture} -> 3/4 = 0.75 >= 0.6 -> binds
    elements = [_el("e1", "Engram is a memory architecture")]
    result = kg_ingest._bind_quote(
        "Engram memory architecture model", elements, "s1", "MyDoc"
    )
    assert result is not None, "fuzzy bind should succeed with 0.75 token overlap"
    assert result["element_id"] == "e1"
    assert result["source_id"] == "s1"
    assert result["source_title"] == "MyDoc"


def test_bind_quote_fuzzy_not_exact():
    """Confirm the fuzzy test quote genuinely fails exact-substring matching."""
    elements = [_el("e1", "Engram is a memory architecture")]
    q = kg_ingest._norm("Engram memory architecture model")
    text_norm = kg_ingest._norm("Engram is a memory architecture")
    assert q not in text_norm, "precondition: quote must NOT be an exact substring"


def test_build_records_binds_relation_quote_to_source_element():
    """Relation evidence keeps raw quote and gains element-level provenance."""
    quote_a = "Premise A is established"
    quote_b = "Conclusion B follows"
    edge_quote = "Premise A therefore leads to Conclusion B"
    elements = [
        _el("e1", quote_a),
        _el("e2", f"{quote_b}. {edge_quote}"),
    ]
    g = KnowledgeGraph(
        doc_id="doc.md", doc_type="academic",
        nodes=[
            Node(id="A", type="Claim", name=quote_a,
                 evidence=[Evidence(file="doc.md", char_start=0, char_end=10,
                                    line_start=1, line_end=1, quote=quote_a)]),
            Node(id="B", type="Claim", name=quote_b,
                 evidence=[Evidence(file="doc.md", char_start=11, char_end=20,
                                    line_start=2, line_end=2, quote=quote_b)]),
        ],
        edges=[Edge(
            id="R", type="derived_from", source_id="A", target_id="B",
            evidence=[Evidence(file="doc.md", char_start=21, char_end=40,
                               line_start=2, line_end=2, quote=edge_quote)],
        )],
    )
    _objects, relations = kg_ingest.build_records(
        g, source_id="s1", source_title="Doc", elements=elements)
    assert relations[0]["evidence"][0]["quote"] == edge_quote
    assert relations[0]["evidence"][0]["element_id"] == "e2"
    assert relations[0]["evidence"][0]["source_title"] == "Doc"


class FakeClient:
    configured = True
    def __init__(self, payload): self._p = payload
    def chat_json(self, messages: list, response_schema_hint: str) -> str: return self._p

ABS = "We propose Engram, a memory architecture. Engram improves perplexity."

def test_extract_graph_grounds_nodes():
    import json
    # ABS is a single prose element (one window) -> ev:0 anchors to it.
    payload = json.dumps({
        "nodes": [
            {"local_id": "a", "type": "Concept", "name": "Engram", "ev": 0},
            {"local_id": "b", "type": "Claim", "name": "Engram improves perplexity",
             "ev": 0},
            {"local_id": "z", "type": "Concept", "name": "Ghost", "ev": 99},
        ],
        "edges": [{"type": "about", "source": "b", "target": "a", "ev": 0}],
    })
    g = kg_ingest.extract_graph(FakeClient(payload), ABS, "doc.md", "academic")
    names = {n.name for n in g.nodes}
    assert "Engram" in names and "Engram improves perplexity" in names
    assert "Ghost" not in names           # bad ev + name not in element -> dropped
    assert len(g.edges) == 1              # edge endpoints survived


def test_extract_graph_drops_and_reports_invalid_endpoint_pair(caplog):
    import json

    payload = json.dumps({
        "nodes": [
            {"local_id": "a", "type": "Formula", "name": "gain equation", "ev": 0},
            {"local_id": "b", "type": "Claim", "name": "gain increases", "ev": 0},
        ],
        # precedes is same-type only; Formula -> Claim is contract-invalid.
        "edges": [{"type": "precedes", "source": "a", "target": "b", "ev": 0}],
    })
    with caplog.at_level("WARNING", logger="app.services.kg.extract"):
        graph = kg_ingest.extract_graph(
            FakeClient(payload), "gain equation; gain increases", "doc.md", "academic"
        )
    assert graph.edges == []
    assert "invalid_endpoint_pair" in caplog.text


class _FlakyClient:
    """Raises APIConnectionError for the first `n_fail` distinct windows it sees
    (keyed by prompt text), returns valid JSON for the rest. The KG node evidence
    is grounded in the shared text so surviving windows contribute a node."""
    configured = True

    def __init__(self, fail_windows: set, payload: str):
        self._fail = set(fail_windows)
        self._payload = payload
        self._seen: list = []

    def chat_json(self, messages: list, response_schema_hint: str) -> str:
        from openai import APIConnectionError
        import httpx
        content = messages[0]["content"]
        # Window index = how many distinct windows already seen
        idx = len(self._seen)
        self._seen.append(content)
        if idx in self._fail:
            raise APIConnectionError(request=httpx.Request("POST", "https://x"))
        return self._payload


def test_extract_graph_counts_failed_windows():
    """Some windows raise a network error (counted as failed), others return valid
    JSON and contribute nodes. failed_windows == # raised, total_windows == #windows,
    surviving windows still produce nodes."""
    import json
    from app.services.kg.windowing import windows_with_elements

    # Build a multi-section markdown so windowing yields >=2 non-empty windows.
    quote = "Engram is a memory architecture"
    section_a = "# Section A\n\n" + quote + "\n\n"
    section_b = "# Section B\n\n" + quote + " indeed.\n\n"
    text = section_a + section_b
    pairs = windows_with_elements(text, "doc.md", None, 40, 5)
    n_windows = len(pairs)
    assert n_windows >= 2, f"need >=2 windows, got {n_windows}"

    # ev:0 anchors to each window's first prose element (contains the quote).
    payload = json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram", "ev": 0}],
        "edges": [],
    })
    # Fail the first window only.
    client = _FlakyClient(fail_windows={0}, payload=payload)
    g = kg_ingest.extract_graph(client, text, "doc.md", "academic", n=40, m=5)
    assert g.total_windows == n_windows
    assert g.failed_windows == 1
    # Surviving windows still grounded at least one Engram node.
    assert any(n.name == "Engram" for n in g.nodes)


def test_extract_graph_counts_programming_errors_as_failed_windows():
    """A client/adapter error is not a legitimate empty extraction result."""
    from app.services.kg.windowing import windows_with_elements

    text = "# Section A\n\nA technical fact.\n\n# Section B\n\nAnother fact.\n"
    n_windows = len(windows_with_elements(text, "doc.md", None, 40, 5))
    assert n_windows >= 1

    class BrokenClient:
        configured = True

        def chat_json(self, messages, response_schema_hint):
            raise TypeError("adapter rejected a supported keyword")

    graph = kg_ingest.extract_graph(
        BrokenClient(), text, "doc.md", "academic", n=40, m=5
    )
    assert graph.failed_windows == n_windows
    assert graph.nodes == []


def test_extract_graph_no_failures_zero_count():
    import json
    payload = json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram", "ev": 0}],
        "edges": [],
    })
    g = kg_ingest.extract_graph(FakeClient(payload), ABS, "doc.md", "academic")
    assert g.failed_windows == 0
    assert g.total_windows >= 1


def test_extract_graph_cancels_and_drains_windows_after_interrupt(monkeypatch):
    """中断(Ctrl-C/SIGTERM)也要逐层排空:兄弟窗口若不取消并等它们停下,来源 future
    会先完成,上层排空的是**来源** future,于是可能在窗口仍在调模型、仍会写图时就落
    终态、放开跨进程单飞守卫,让新构建与它们重叠(评审 P2)。"""
    total = 12
    elements = [
        SourceElementQ(
            id=f"e-{i}", type="paragraph", file="doc.md",
            line_start=i + 1, line_end=i + 1,
            char_start=i * 20, char_end=i * 20 + 12,
            text=f"technical fact {i}",
        )
        for i in range(total)
    ]
    pairs = [
        (SimpleNamespace(section_path=f"section-{i}"), [elements[i]])
        for i in range(total)
    ]
    monkeypatch.setattr(
        kg_ingest, "windows_with_elements", lambda *_args, **_kwargs: pairs
    )
    monkeypatch.setattr(
        kg_ingest, "should_extract_window", lambda *_args, **_kwargs: (True, "")
    )

    executor = cf.ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(
        kg_ingest, "submit_window",
        lambda fn, *args, **kwargs: executor.submit(fn, *args, **kwargs),
    )

    class InterruptThenSlowClient:
        configured = True

        def __init__(self):
            self.lock = threading.Lock()
            self.calls = 0
            self.running = 0

        def chat_json(self, messages, response_schema_hint):
            with self.lock:
                self.calls += 1
                call_number = self.calls
                self.running += 1
            try:
                if call_number == 1:
                    raise KeyboardInterrupt("ctrl-c inside a window")
                time.sleep(0.05)
                return '{"nodes":[],"edges":[]}'
            finally:
                with self.lock:
                    self.running -= 1

    client = InterruptThenSlowClient()
    try:
        with pytest.raises(KeyboardInterrupt):
            kg_ingest.extract_graph(
                client, "ignored", "doc.md", "academic", n=10, m=0
            )
        assert client.running == 0, "兄弟窗口尚未停下就把中断抛了上去"
        assert client.calls < total
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_extract_graph_cancels_and_drains_windows_after_task_abort(monkeypatch):
    total = 12
    elements = [
        SourceElementQ(
            id=f"e-{i}", type="paragraph", file="doc.md",
            line_start=i + 1, line_end=i + 1,
            char_start=i * 20, char_end=i * 20 + 12,
            text=f"technical fact {i}",
        )
        for i in range(total)
    ]
    pairs = [
        (SimpleNamespace(section_path=f"section-{i}"), [elements[i]])
        for i in range(total)
    ]
    monkeypatch.setattr(
        kg_ingest, "windows_with_elements", lambda *_args, **_kwargs: pairs
    )
    monkeypatch.setattr(
        kg_ingest, "should_extract_window", lambda *_args, **_kwargs: (True, "")
    )

    executor = cf.ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(
        kg_ingest, "submit_window",
        lambda fn, *args, **kwargs: executor.submit(fn, *args, **kwargs),
    )

    class AbortThenSlowClient:
        configured = True

        def __init__(self):
            self.lock = threading.Lock()
            self.calls = 0
            self.running = 0

        def chat_json(self, messages, response_schema_hint):
            with self.lock:
                self.calls += 1
                call_number = self.calls
                self.running += 1
            try:
                if call_number == 1:
                    raise KgBuildAborted(
                        KgBuildFailure("model_unavailable", "model unavailable")
                    )
                time.sleep(0.05)
                return '{"nodes":[],"edges":[]}'
            finally:
                with self.lock:
                    self.running -= 1

    client = AbortThenSlowClient()
    try:
        with pytest.raises(KgBuildAborted):
            kg_ingest.extract_graph(
                client, "ignored", "doc.md", "academic", n=10, m=0
            )
        assert client.running == 0
        assert client.calls < total
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_canonicalize_merges_across_windows():
    """Two windows both emit the same Concept 'Engram'; canonicalize must collapse
    to a single node.  The text is long enough (>200 chars in one prose section)
    to produce >=2 windows with n=200, m=20.

    We embed the evidence quote twice in the text so each window can ground its
    Engram node independently; then extract_graph's canonicalize step must merge
    them to exactly one Concept named 'Engram'.
    """
    import json

    # Evidence quote placed near position 0 (window 1: 0-200) and repeated
    # near position ~190 (window 2: 180-380) so both windows contain it.
    # We pad the text between the two occurrences to keep total length > 200.
    quote = "Engram is a memory architecture"
    # 34-char quote at start; padding to push past 200 chars; quote repeated
    padding = " " * (200 - len(quote))   # 166 spaces
    long_text = quote + padding + quote   # total ~234 chars in one paragraph

    payload = json.dumps({
        "nodes": [
            {"local_id": "a", "type": "Concept", "name": "Engram", "ev": 0},
        ],
        "edges": [],
    })
    g = kg_ingest.extract_graph(
        FakeClient(payload), long_text, "doc.md", "academic", n=200, m=20
    )
    engram_nodes = [n for n in g.nodes if n.name == "Engram"]
    # Both windows ground the same Concept; canonicalize must merge to 1.
    assert len(engram_nodes) == 1, (
        f"expected 1 Engram node after canonicalization, got {len(engram_nodes)}"
    )


def test_extract_graph_uses_global_pool():
    import json
    payload = json.dumps({"nodes": [{"local_id": "a", "type": "Concept",
                                      "name": "Engram", "ev": 0}], "edges": []})
    g = kg_ingest.extract_graph(FakeClient(payload), ABS, "doc.md", "academic")
    assert g.total_windows >= 1
    assert any(n.name == "Engram" for n in g.nodes)


def test_drop_noise_concepts_removes_symbols_and_dangling_edges():
    from app.services.kg.models import Node, Edge
    from app.services.kg_ingest import drop_noise_concepts
    nodes = [
        Node(id="n1", type="Concept", name="current mirror"),
        Node(id="n2", type="Concept", name="V_DD"),
        Node(id="n3", type="Claim", name="The current mirror copies the reference current."),
    ]
    edges = [
        Edge(id="e1", type="about", source_id="n3", target_id="n1"),
        Edge(id="e2", type="about", source_id="n3", target_id="n2"),
    ]
    kept_nodes, kept_edges, dropped = drop_noise_concepts(nodes, edges, frozenset())
    assert dropped == 1
    assert {n.id for n in kept_nodes} == {"n1", "n3"}
    assert {e.id for e in kept_edges} == {"e1"}


def test_drop_noise_concepts_keeps_whitelisted():
    from app.services.kg.models import Node
    from app.services.kg_ingest import drop_noise_concepts
    nodes = [Node(id="n1", type="Concept", name="VCO")]
    kept, _e, dropped = drop_noise_concepts(nodes, [], frozenset({"vco"}))
    assert dropped == 0 and len(kept) == 1
