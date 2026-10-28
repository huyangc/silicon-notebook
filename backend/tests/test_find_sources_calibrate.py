"""``app.eval.find_sources_calibrate`` (design 2026-10-28 §8, layer 2) with fake
models only: the candidate sets come from the synthetic paper-topics corpus and
the deterministic ``TopicEmbedder``; ``prelabel`` talks to a counting fake that
answers from the fixture labels.  The tool itself is never run against a real
notebook or model here."""
from __future__ import annotations

import ast
import io
import json
import re
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.eval import find_sources_calibrate as calibrate
from tests.paper_topics_testkit import load_labels, load_papers, sqlite_corpus


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    yield from sqlite_corpus(tmp_path, monkeypatch)


class CountingChat:
    """A chat client that labels from the fixture labels by the prompt's title."""

    def __init__(self) -> None:
        self.configured = True
        self.model = "fake-labeller"
        self.calls = 0
        labels = load_labels()
        self._by_topic = {
            spec["query"]: {**{k: "related" for k in spec["related"]},
                            **{k: "strong" for k in spec["strong"]}}
            for spec in labels.values()
        }
        self._key_by_title = {paper["title"]: paper["key"] for paper in load_papers()}

    def chat_json(self, messages, schema_hint, **kwargs):
        assert schema_hint == calibrate.PRELABEL_SCHEMA_HINT
        self.calls += 1
        prompt = messages[-1]["content"]
        topic = re.search(r"^Topic: (.+)$", prompt, re.MULTILINE).group(1)
        title = re.search(r"^Document title: (.+)$", prompt, re.MULTILINE).group(1)
        label = self._by_topic[topic].get(self._key_by_title.get(title, ""), "unrelated")
        return json.dumps({"label": label, "reason": "fixture"})


def _topics_file(tmp_path: Path) -> Path:
    path = tmp_path / "topics.json"
    path.write_text(json.dumps([
        {"id": topic, "query": spec["query"], "terms": spec["terms"]}
        for topic, spec in sorted(load_labels().items())
    ]), encoding="utf-8")
    return path


def _run(argv, repo=None, *, chat=None):
    out = io.StringIO()
    if repo is None:
        repo = SimpleNamespace(_runtime=SimpleNamespace(models=SimpleNamespace(
            chat=lambda workload: chat, embedding=lambda workload: SimpleNamespace(model="emb"),
        )))
    code = calibrate.main(
        [str(part) for part in argv], open_repository=lambda settings: nullcontext(repo),
        settings_factory=lambda: None, out=out,
    )
    return code, out.getvalue()


def _candidates(corpus, tmp_path, per_topic=12):
    instance, nb, _keys = corpus
    labels_path = tmp_path / "labels.json"
    code, output = _run(["candidates", "--notebook", nb, "--topics", _topics_file(tmp_path),
                         "--out", labels_path, "--per-topic", per_topic], instance)
    assert code == 0, output
    return labels_path


def test_candidates_write_the_scored_sets_without_a_chat_model(corpus, tmp_path):
    instance, _nb, keys = corpus
    path = _candidates(corpus, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == calibrate.LABEL_FILE_VERSION
    assert [topic["id"] for topic in data["topics"]] == sorted(load_labels())
    by_source = {source_id: key for key, source_id in keys.items()}
    for topic in data["topics"]:
        assert 0 < len(topic["candidates"]) <= 12
        for candidate in topic["candidates"]:
            assert candidate["source_id"] in by_source
            assert (candidate["label"], candidate["prelabel"], candidate["reviewed"]) == (None, None, False)
            assert set(candidate) >= {
                "best", "semantic_best", "hits", "lane", "matches", "title"}
        # The labelled strong papers of the topic are among its candidates.
        strong = set(load_labels()[topic["id"]]["strong"])
        found = {by_source[c["source_id"]] for c in topic["candidates"]}
        assert strong <= found, (topic["id"], strong - found)


def test_prelabel_without_confirm_sends_nothing(tmp_path):
    path = tmp_path / "labels.json"
    data = {"version": 1, "topics": [{"id": "t", "query": "q", "terms": [], "candidates": [
        {"source_id": "s1", "title": "A", "best": 0.5, "hits": 1, "lane": "baseline",
         "matches": {}, "label": None, "prelabel": None, "reviewed": False},
        {"source_id": "s2", "title": "B", "best": 0.4, "hits": 1, "lane": "baseline",
         "matches": {}, "label": None, "prelabel": "strong", "reviewed": False},
    ]}]}
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    chat = CountingChat()
    code, output = _run(["prelabel", "--labels", path], chat=chat)
    assert code == 0
    assert chat.calls == 0
    assert "1 model requests" in output and "fake-labeller" in output
    assert "--confirm" in output
    assert path.read_bytes() == before


def test_prelabel_with_confirm_labels_and_keeps_human_labels(corpus, tmp_path, monkeypatch):
    instance, _nb, keys = corpus
    path = _candidates(corpus, tmp_path, per_topic=5)
    data = json.loads(path.read_text(encoding="utf-8"))
    kept = data["topics"][0]["candidates"][0]
    kept["label"] = "unrelated"  # a human decision already made
    path.write_text(json.dumps(data), encoding="utf-8")
    chat = CountingChat()
    repo = SimpleNamespace(_runtime=instance._runtime, settings=instance.settings)
    monkeypatch.setattr(repo._runtime.models, "chat", lambda workload: chat)
    code, output = _run(["prelabel", "--labels", path, "--confirm"], repo)
    assert code == 0, output
    labelled = json.loads(path.read_text(encoding="utf-8"))
    assert chat.calls == sum(len(t["candidates"]) for t in labelled["topics"])
    first = labelled["topics"][0]["candidates"][0]
    assert first["label"] == "unrelated" and first["prelabel"] in calibrate.LABELS
    assert all(c["prelabel"] in calibrate.LABELS for t in labelled["topics"] for c in t["candidates"])
    # A second run has nothing left to do.
    code, output = _run(["prelabel", "--labels", path], chat=CountingChat())
    assert "0 model requests" in output


def test_the_whole_flow_finds_lines_meeting_the_targets(corpus, tmp_path, monkeypatch):
    instance, _nb, _keys = corpus
    path = _candidates(corpus, tmp_path)
    chat = CountingChat()
    repo = SimpleNamespace(_runtime=instance._runtime, settings=instance.settings)
    monkeypatch.setattr(repo._runtime.models, "chat", lambda workload: chat)
    assert _run(["prelabel", "--labels", path, "--confirm"], repo)[0] == 0
    code, output = _run(["sweep", "--labels", path])
    assert code == 2 and "not reviewed" in output       # model labels need a human pass
    code, output = _run(["sweep", "--labels", path, "--allow-unreviewed"])
    assert code == 0, output
    assert "warning:" in output and "provisional" in output
    match = re.search(r"chosen: strong=(\S+) related=(\S+) recall=(\S+) strong_precision=(\S+)", output)
    strong, related, recall, precision = (float(v) for v in match.groups())
    assert related <= strong and recall >= 0.9 and precision >= 0.8


def _labels(*candidates) -> dict:
    return {"version": 1, "topics": [{"id": "t", "query": "q", "terms": [], "candidates": [
        {"source_id": f"s{i}", "title": "", "best": best, "hits": 1, "lane": lane,
         "matches": matches, "label": label, "prelabel": label, "reviewed": True}
        for i, (best, label, lane, matches) in enumerate(candidates)
    ]}]}


def test_sweep_picks_the_highest_related_and_lowest_strong_line():
    data = _labels(
        (0.90, "strong", "baseline", {}), (0.80, "strong", "baseline", {}),
        (0.62, "related", "baseline", {}), (0.55, "related", "baseline", {}),
        (0.50, "unrelated", "baseline", {}), (0.20, "unrelated", "baseline", {}),
        (0.10, "strong", "baseline", {"title": True}),  # strong by its title at any line
    )
    result = calibrate.sweep(data, min_recall=0.9, min_strong_precision=0.8)
    chosen = result["chosen"]
    assert (chosen["related"], chosen["strong"]) == (0.55, 0.63)
    assert chosen["recall"] == 1.0 and chosen["strong_precision"] == 1.0
    recalls = [values["recall"] for _line, values in result["related_curve"]]
    assert recalls == sorted(recalls, reverse=True)


def test_the_lines_read_only_semantic_evidence():
    # A keyword-only 0.95 (no vector similarity) is not "strong" at any line;
    # a label file without ``semantic_best`` keeps reading ``best`` per lane.
    data = _labels(
        (0.95, "strong", "baseline:lexical_only", {}),
        (0.95, "strong", "baseline", {}),
        (0.95, "strong", "fts_degraded", {}),
    )
    data["topics"][0]["candidates"][0]["semantic_best"] = None
    result = calibrate.evaluate(data, 0.7, 0.3)
    assert result["recall"] == pytest.approx(1 / 3)


def test_sweep_reports_out_of_reach_targets(tmp_path):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(_labels(
        (0.4, "unrelated", "baseline", {}), (0.4, "strong", "fts_degraded", {}),
    )), encoding="utf-8")
    code, output = _run(["sweep", "--labels", path])
    assert code == 1 and "chosen: none" in output


def test_write_config_rewrites_exactly_the_two_defaults(tmp_path):
    copy = tmp_path / "config.py"
    copy.write_text(calibrate._CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    calibrate.write_config(copy, strong=0.66, related=0.41)
    text = copy.read_text(encoding="utf-8")
    ast.parse(text)
    assert re.search(r"find_sources_strong_relevance: float = Field\(\s*0\.66,", text)
    assert re.search(r"find_sources_related_relevance: float = Field\(\s*0\.41,", text)
    original = calibrate._CONFIG_PATH.read_text(encoding="utf-8")
    assert len(text.splitlines()) == len(original.splitlines())


def test_label_files_inside_the_repository_are_refused(tmp_path):
    inside = calibrate._REPO_ROOT / "backend" / "labels-should-not-be-here.json"
    code, output = _run(["sweep", "--labels", inside])
    assert code == 2 and "inside the repository" in output
    assert not inside.exists()


def _git_main_root() -> Path:
    import subprocess

    common = subprocess.run(
        ["git", "-C", str(calibrate._REPO_ROOT), "rev-parse", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    path = Path(common)
    if not path.is_absolute():
        path = calibrate._REPO_ROOT / path
    return path.resolve().parent


@pytest.mark.process_contract
def test_label_files_inside_the_main_checkout_are_refused():
    inside = _git_main_root() / "labels-should-not-be-here.json"
    code, output = _run(["sweep", "--labels", inside])
    assert code == 2 and "inside the repository" in output
    assert not inside.exists()
    assert _git_main_root() in calibrate.repository_roots()


def test_unreviewed_labels_never_reach_the_config(tmp_path):
    data = _labels((0.9, "strong", "baseline", {}), (0.2, "unrelated", "baseline", {}))
    data["topics"][0]["candidates"][1]["reviewed"] = False
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    config = tmp_path / "config.py"
    config.write_text(calibrate._CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    before = config.read_bytes()
    code, output = _run(["sweep", "--labels", path])
    assert code == 2 and "1 labels are not reviewed" in output
    code, output = _run(["sweep", "--labels", path, "--allow-unreviewed"])
    assert code == 0 and "warning: 1 labels are unreviewed" in output
    code, output = _run(["sweep", "--labels", path, "--allow-unreviewed", "--write-config", config])
    assert code == 2 and "--write-config needs every label reviewed" in output
    assert config.read_bytes() == before


def _pending(n: int) -> dict:
    return {"version": 1, "topics": [{"id": "t", "query": "q", "terms": [], "candidates": [
        {"source_id": f"s{i}", "title": f"T{i}", "best": 0.5, "hits": 1, "lane": "baseline",
         "matches": {}, "label": None, "prelabel": None, "reviewed": False}
        for i in range(n)
    ]}]}


class ScriptedChat:
    def __init__(self, script) -> None:
        self.configured = True
        self.model = "fake"
        self.script = list(script)
        self.calls = 0

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls += 1
        step = self.script.pop(0) if self.script else "strong"
        if isinstance(step, BaseException):
            raise step
        return json.dumps({"label": step, "reason": ""})


def test_prelabel_saves_every_n_labels():
    data = _pending(5)
    saves: list[int] = []
    counts = calibrate.run_prelabel(
        data, ScriptedChat([]), lambda sid: "", save_every=2,
        save=lambda current: saves.append(sum(c["prelabel"] is not None for c in current["topics"][0]["candidates"])),
    )
    assert counts["labelled"] == 5
    assert saves == [2, 4, 5]


def test_prelabel_skips_transient_failures_and_counts_them():
    from app.services.model_work import ModelQueueFull

    data = _pending(3)
    counts = calibrate.run_prelabel(data, ScriptedChat(["strong", ModelQueueFull(), "related"]), lambda sid: "")
    assert counts == {"labelled": 2, "unparsable": 0, "transient": 1}
    assert [c["prelabel"] for c in data["topics"][0]["candidates"]] == ["strong", None, "related"]


def test_prelabel_stops_on_other_failures_after_saving_progress(tmp_path, monkeypatch):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(_pending(4)), encoding="utf-8")
    chat = ScriptedChat(["strong", RuntimeError("auth broken"), "related"])
    repo = SimpleNamespace(_runtime=SimpleNamespace(models=SimpleNamespace(chat=lambda workload: chat)))
    monkeypatch.setattr(calibrate, "source_excerpt", lambda repo, sid: "")
    code, output = _run(["prelabel", "--labels", path, "--confirm"], repo)
    assert code == 2 and "prelabel stopped by RuntimeError after 1 labels" in output
    assert chat.calls == 2
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [c["prelabel"] for c in saved["topics"][0]["candidates"]] == ["strong", None, None, None]


def test_prelabel_without_confirm_sends_nothing_even_with_a_failing_model(tmp_path):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(_pending(3)), encoding="utf-8")
    chat = ScriptedChat([RuntimeError("must not be called")])
    code, output = _run(["prelabel", "--labels", path], chat=chat)
    assert code == 0 and chat.calls == 0 and "3 model requests" in output
