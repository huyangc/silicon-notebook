"""Calibrate ``find_sources``' two relevance lines (design 2026-10-28 §8, layer 2).

A local measurement tool, never run in CI.  Three subcommands, in order:

``candidates``
    For a real papers notebook and a topics file (``[{"id", "query",
    "terms"}]``), runs ``SourceFinder`` with the lines opened wide (every
    passage-bearing source is kept) and writes, per topic, the top
    ``--per-topic`` (default 60) candidates with what tiering needs -- the best
    fused relevance, passage count, lane and matched metadata fields -- to a
    label file.  Embedding retrieval only: no chat model is called.

``prelabel``
    Asks a chat model (``--workload``, default ``paper_metadata``) to pre-label
    every unlabelled candidate ``strong`` / ``related`` / ``unrelated``.  This
    spends model quota, so **without ``--confirm`` it only prints how many
    requests it would send and to which model, and sends none**; the operator
    adds ``--confirm`` only after the user has agreed in the conversation.  The
    model's verdict goes to ``prelabel`` and seeds ``label``, which the user
    then reviews and corrects by hand.

``sweep``
    Pure over the reviewed label file (labels without ``"reviewed": true`` are
    refused unless ``--allow-unreviewed``, and never written with
    ``--write-config``): scans the related line for the highest
    value whose topic recall (strong + related, within the candidate sets) is
    at least ``--min-recall`` (0.9), then the strong line for the lowest value
    whose strong-tier precision is at least ``--min-strong-precision`` (0.8).
    Prints both curves and the choice; ``--write-config`` writes the two values
    into ``app/core/config.py``.  Recall is measured within the candidate sets
    only (a paper outside every candidate set is never counted) -- an
    approximation, stated in the PR that carries the values.

The label file holds the user's notebook titles: it must live OUTSIDE the
repository (the local experience directory); a path inside the repository is
refused.  Re-run all three after changing the embedding model.

    cd backend && PYTHONPATH=. python -m app.eval.find_sources_calibrate \\
        candidates --notebook nb-... --topics /abs/topics.json --out /abs/labels.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from app.services.paper_card_service import parse_model_object
from app.services.source_finder import (
    FindSourcesRequest,
    SourceFinder,
    _lexical_only,
    _tier,
)

LABEL_FILE_VERSION = 1
LABELS = ("strong", "related", "unrelated")
DEFAULT_PER_TOPIC = 60
DEFAULT_WORKLOAD = "paper_metadata"
#: ``prelabel`` writes the label file after this many new labels.
PRELABEL_SAVE_EVERY = 10
#: Grid of the sweep (the lines are fused relevances in 0..1).
SWEEP_STEP = 0.01
_EXCERPT_CHARS = 1500

PRELABEL_SCHEMA_HINT = '{"label":"strong|related|unrelated","reason":""}'

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_PATH = _REPO_ROOT / "backend" / "app" / "core" / "config.py"


class CalibrationError(ValueError):
    """Bad input (label file shape, a path inside the repository...)."""


# ------------------------------------------------------------------ files
def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *args], check=True, capture_output=True, text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def repository_roots() -> List[Path]:
    """Every checkout of this repository: the main checkout (the parent of
    ``git rev-parse --git-common-dir``), every linked worktree
    (``git worktree list``), and this file's own checkout.  Without git only
    the last is known."""
    roots = {_REPO_ROOT.resolve()}
    common = _git("rev-parse", "--git-common-dir").strip()
    if common:
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = _REPO_ROOT / common_path
        roots.add(common_path.resolve().parent)
    for line in _git("worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            roots.add(Path(line[len("worktree "):]).resolve())
    return sorted(roots)


def require_outside_repo(path: Path) -> Path:
    """Refuse a label-file path inside the main checkout or any worktree: the
    file holds the user's notebook titles and must never be committed."""
    resolved = Path(path).expanduser().resolve()
    for root in repository_roots():
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        raise CalibrationError(
            f"{resolved} is inside the repository ({root}); keep label files in the "
            "local experience directory, outside every checkout of the repository"
        )
    return resolved


def read_labels(path: Path) -> Dict[str, Any]:
    data = json.loads(require_outside_repo(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != LABEL_FILE_VERSION:
        raise CalibrationError("not a find_sources label file (version mismatch)")
    return data


def write_labels(path: Path, data: Mapping[str, Any]) -> None:
    target = require_outside_repo(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)


def read_topics(path: Path) -> List[Dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    topics = []
    for item in raw if isinstance(raw, list) else []:
        topic_id = str(item.get("id") or "").strip()
        query = str(item.get("query") or "").strip()
        terms = [str(t).strip() for t in item.get("terms") or [] if str(t).strip()]
        if not topic_id or not (query or terms):
            raise CalibrationError(f"topic needs an id and a query or terms: {item!r}")
        topics.append({"id": topic_id, "query": query, "terms": terms})
    if not topics:
        raise CalibrationError("the topics file holds no topic")
    return topics


# ------------------------------------------------------------ candidates
class _RecordingFinder(SourceFinder):
    """``SourceFinder`` that keeps every scored candidate of its last call (the
    public result carries tiers only, the sweep needs the relevance)."""

    recorded: List[Dict[str, Any]]

    def _ranked(self, candidates, request):  # noqa: D401 - same contract
        self.recorded = [
            {
                "source_id": c.row["source_id"],
                "notebook_id": c.row["notebook_id"],
                "best": c.best,
                "semantic_best": c.semantic_best,
                "hits": c.hits,
                "lane": c.lane,
                "matches": dict(c.row["matches"]),
            }
            for c in candidates.values()
        ]
        return super()._ranked(candidates, request)


def wide_open_finder(repo: Any) -> _RecordingFinder:
    """A finder whose lines keep every candidate that has any signal."""
    runtime = repo._runtime
    settings = repo.settings.model_copy(update={
        "find_sources_strong_relevance": 1.0,
        "find_sources_related_relevance": 0.0,
    })
    return _RecordingFinder(
        database=runtime.database,
        sources=runtime.source_store,
        retrieval=repo.retrieval,
        enumeration=runtime.collection_enumeration,
        settings=settings,
        paper_card_version=0,
    )


def _candidate_order(candidate: Mapping[str, Any]) -> tuple:
    return (-(candidate["best"] or 0.0), -len(candidate["matches"]), -candidate["hits"],
            candidate["source_id"])


def topic_candidates(
    finder: _RecordingFinder,
    notebook_id: str,
    topic: Mapping[str, Any],
    allowed_source_ids: Sequence[str],
    *,
    per_topic: int = DEFAULT_PER_TOPIC,
) -> List[Dict[str, Any]]:
    finder.find(notebook_id, FindSourcesRequest(
        query=topic["query"], terms=tuple(topic["terms"]), kind="papers", limit=1,
    ), allowed_source_ids=list(allowed_source_ids))
    ranked = sorted(finder.recorded, key=_candidate_order)[:per_topic]
    return [{**candidate, "label": None, "prelabel": None, "reviewed": False} for candidate in ranked]


def _attach_titles(repo: Any, candidates: List[Dict[str, Any]]) -> None:
    """The paper title (else the source title) of each candidate, for the
    labeller and the human reviewer."""
    runtime = repo._runtime
    with runtime.database.connect() as db:
        rows = runtime.source_store.source_listing_rows(db, [c["source_id"] for c in candidates])
    titles = {row["id"]: str(row["paper_title"] or row["title"] or "") for row in rows}
    for candidate in candidates:
        candidate["title"] = titles.get(candidate["source_id"], "")


def build_candidates(
    repo: Any,
    notebook_id: str,
    topics: Sequence[Mapping[str, Any]],
    *,
    per_topic: int = DEFAULT_PER_TOPIC,
    embedding_model: str = "",
) -> Dict[str, Any]:
    finder = wide_open_finder(repo)
    allowed = repo.all_visible_source_ids(notebook_id)
    entries = []
    for topic in topics:
        candidates = topic_candidates(finder, notebook_id, topic, allowed, per_topic=per_topic)
        _attach_titles(repo, candidates)
        entries.append({**topic, "candidates": candidates})
    return {
        "version": LABEL_FILE_VERSION,
        "notebook_id": notebook_id,
        "embedding_model": embedding_model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "topics": entries,
    }


# -------------------------------------------------------------- prelabel
def pending_prelabels(data: Mapping[str, Any]) -> List[Tuple[int, int]]:
    """``(topic index, candidate index)`` of every candidate without a prelabel."""
    return [
        (ti, ci)
        for ti, topic in enumerate(data["topics"])
        for ci, candidate in enumerate(topic["candidates"])
        if candidate.get("prelabel") is None
    ]


def prelabel_prompt(topic: Mapping[str, Any], title: str, excerpt: str) -> str:
    terms = ", ".join(topic.get("terms") or []) or "(none)"
    return (
        "You judge whether a document belongs to a research topic.\n"
        f"Topic: {topic['query'] or terms}\nLiteral terms: {terms}\n\n"
        "Labels:\n"
        "- strong: the document is mainly about this topic.\n"
        "- related: the topic is a component, baseline or application of the "
        "document, but not its main subject.\n"
        "- unrelated: anything else.\n"
        "Judge from the excerpt only. Return JSON only.\n\n"
        f"Document title: {title}\nExcerpt:\n{excerpt}"
    )


class PrelabelStopped(CalibrationError):
    """A non-transient model failure stopped ``prelabel``; progress was saved."""

    def __init__(self, counts: Mapping[str, int], error: BaseException) -> None:
        super().__init__(
            f"prelabel stopped by {type(error).__name__} after {counts['labelled']} labels "
            "(progress saved; fix the cause and re-run)"
        )
        self.counts = dict(counts)


def run_prelabel(
    data: Dict[str, Any],
    client: Any,
    read_excerpt: Callable[[str], str],
    *,
    save: Callable[[Dict[str, Any]], None] = lambda _data: None,
    save_every: int = PRELABEL_SAVE_EVERY,
) -> Dict[str, int]:
    """Pre-label every pending candidate (one request each).

    ``save`` runs after every ``save_every`` new labels and at the end, so an
    interrupted run keeps its progress.  A transient model failure
    (``is_transient_model_error``: queue full / timeout, rate limit, 5xx …)
    skips that candidate (it stays pending for the next run) and is counted;
    any other failure saves the progress and stops with ``PrelabelStopped``."""
    from app.services.model_work import is_transient_model_error

    counts = {"labelled": 0, "unparsable": 0, "transient": 0}
    unsaved = 0
    for topic in data["topics"]:
        for candidate in topic["candidates"]:
            if candidate.get("prelabel") is not None:
                continue
            try:
                raw = client.chat_json(
                    [{"role": "user", "content": prelabel_prompt(
                        topic, candidate["title"], read_excerpt(candidate["source_id"]))}],
                    PRELABEL_SCHEMA_HINT,
                    temperature=0.0,
                )
            except Exception as exc:  # noqa: BLE001 - classified below
                if is_transient_model_error(exc):
                    counts["transient"] += 1
                    continue
                save(data)
                raise PrelabelStopped(counts, exc) from exc
            parsed = parse_model_object(raw) or {}
            label = str(parsed.get("label") or "").strip().lower()
            if label not in LABELS:
                counts["unparsable"] += 1
                continue
            candidate["prelabel"] = label
            if candidate.get("label") is None:
                candidate["label"] = label
            counts["labelled"] += 1
            unsaved += 1
            if unsaved >= max(1, save_every):
                save(data)
                unsaved = 0
    if unsaved:
        save(data)
    return counts


def source_excerpt(repo: Any, source_id: str) -> str:
    """Title, stored summary and the first two elements, bounded."""
    runtime = repo._runtime
    with runtime.database.connect() as db:
        rows = runtime.source_store.source_listing_rows(db, [source_id])
    parts = []
    if rows:
        parts.append(str(rows[0]["summary"] or ""))
    page = runtime.source_store.source_elements_page(source_id, offset=0, limit=2)
    parts.extend(str(element.text or "") for element in page.items)
    return "\n\n".join(part for part in parts if part.strip())[:_EXCERPT_CHARS]


# ----------------------------------------------------------------- sweep
def _grid(step: float = SWEEP_STEP) -> List[float]:
    return [round(i * step, 4) for i in range(int(round(1 / step)) + 1)]


def _labelled(data: Mapping[str, Any]) -> List[Tuple[str, Mapping[str, Any]]]:
    return [
        (topic["id"], candidate)
        for topic in data["topics"]
        for candidate in topic["candidates"]
        if candidate.get("label") in LABELS
    ]


def _semantic_best(candidate: Mapping[str, Any]) -> Optional[float]:
    """The relevance the lines read: the recorded ``semantic_best`` (best
    among passages with a vector similarity); a label file written before it
    was recorded falls back to ``best`` on a lane that is not lexical-only."""
    if "semantic_best" in candidate:
        value = candidate["semantic_best"]
        return None if value is None else float(value)
    if candidate.get("best") is None or _lexical_only(str(candidate.get("lane") or "")):
        return None
    return float(candidate["best"])


def evaluate(data: Mapping[str, Any], strong_line: float, related_line: float) -> Dict[str, Any]:
    """Topic recall (labelled strong + related placed in any tier) and the
    precision of the strong tier, pooled over topics, plus per-topic recall."""
    positives = found = predicted_strong = strong_hits = 0
    per_topic: Dict[str, List[int]] = {}
    for topic_id, candidate in _labelled(data):
        tier = _tier(
            candidate["matches"], _semantic_best(candidate), int(candidate["hits"]),
            strong_line=strong_line, related_line=related_line,
        )
        positive = candidate["label"] in ("strong", "related")
        bucket = per_topic.setdefault(topic_id, [0, 0])
        if positive:
            positives += 1
            bucket[0] += 1
            if tier:
                found += 1
                bucket[1] += 1
        if tier == "strong":
            predicted_strong += 1
            strong_hits += candidate["label"] == "strong"
    return {
        "recall": found / positives if positives else 0.0,
        "strong_precision": strong_hits / predicted_strong if predicted_strong else 0.0,
        "strong_predicted": predicted_strong,
        "per_topic_recall": {
            topic_id: (hit / total if total else 0.0) for topic_id, (total, hit) in per_topic.items()
        },
    }


def unreviewed_count(data: Mapping[str, Any]) -> int:
    """Labelled candidates the user has not marked ``"reviewed": true`` yet."""
    return sum(1 for _topic, candidate in _labelled(data) if candidate.get("reviewed") is not True)


def sweep(
    data: Mapping[str, Any],
    *,
    min_recall: float = 0.9,
    min_strong_precision: float = 0.8,
    step: float = SWEEP_STEP,
    allow_unreviewed: bool = False,
) -> Dict[str, Any]:
    """Recall depends on the related line only (the strong line just moves
    sources between the two tiers): the highest related line that keeps
    ``min_recall``, then the lowest strong line at or above it whose strong
    tier reaches ``min_strong_precision``.  ``chosen`` is ``None`` when either
    target is out of reach.  Labels nobody reviewed (``reviewed`` is not
    ``true``: model pre-labels) are refused unless ``allow_unreviewed``."""
    if not _labelled(data):
        raise CalibrationError("the label file has no labelled candidate")
    unreviewed = unreviewed_count(data)
    if unreviewed and not allow_unreviewed:
        raise CalibrationError(
            f"{unreviewed} labels are not reviewed (set \"reviewed\": true after checking "
            "them, or pass --allow-unreviewed for a provisional curve)"
        )
    grid = _grid(step)
    related_curve = [(line, evaluate(data, 1.0, line)) for line in grid]
    feasible = [line for line, result in related_curve if result["recall"] >= min_recall]
    related = max(feasible) if feasible else None
    strong_curve: List[Tuple[float, Dict[str, Any]]] = []
    strong = None
    if related is not None:
        strong_curve = [(line, evaluate(data, line, related)) for line in grid if line >= related]
        strong = next((
            line for line, result in strong_curve
            if result["strong_predicted"] and result["strong_precision"] >= min_strong_precision
        ), None)
    chosen = None if related is None or strong is None else {
        "strong": strong, "related": related, **evaluate(data, strong, related),
    }
    return {"chosen": chosen, "related_curve": related_curve, "strong_curve": strong_curve}


_CONFIG_DEFAULT_RE = r"({name}: float = Field\(\s*)([0-9]*\.?[0-9]+)"


def write_config(path: Path, *, strong: float, related: float) -> None:
    """Replace the two ``Field`` defaults in ``app/core/config.py`` (exactly one
    occurrence each, or nothing is written)."""
    text = Path(path).read_text(encoding="utf-8")
    for name, value in (("find_sources_strong_relevance", strong),
                        ("find_sources_related_relevance", related)):
        pattern = re.compile(_CONFIG_DEFAULT_RE.format(name=name))
        if len(pattern.findall(text)) != 1:
            raise CalibrationError(f"cannot find exactly one default of {name} in {path}")
        text = pattern.sub(lambda m, v=value: f"{m.group(1)}{v:g}", text)
    Path(path).write_text(text, encoding="utf-8")


def format_sweep(result: Mapping[str, Any], *, every: float = 0.05) -> str:
    def rows(curve, key):
        for line, values in curve:
            if abs(line / every - round(line / every)) < 1e-9:
                yield f"  {line:4.2f}  recall={values['recall']:.3f}  " \
                      f"strong_precision={values['strong_precision']:.3f}  ({key})"

    lines = ["related line (strong line closed):", *rows(result["related_curve"], "related")]
    if result["strong_curve"]:
        lines += ["strong line (at the chosen related line):", *rows(result["strong_curve"], "strong")]
    chosen = result["chosen"]
    lines.append(
        "chosen: none (targets out of reach)" if chosen is None else
        f"chosen: strong={chosen['strong']:g} related={chosen['related']:g} "
        f"recall={chosen['recall']:.3f} strong_precision={chosen['strong_precision']:.3f}"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------- CLI
def _default_open_repository(settings: Any) -> AbstractContextManager:
    from app.services.maintenance_cli import open_cli_repository

    return open_cli_repository(settings)


def _client_model(client: Any) -> str:
    return str(getattr(client, "model", "") or "")


# The thresholds belong to one embedding configuration: the label file records it.
EMBEDDING_WORKLOAD = "retrieval_query_embedding"


def _embedding_model(models: Any, workload_id: str) -> str:
    """The model id serving ``workload_id``: the client's own ``model`` when it
    has one, else the bound service's -- the scheduled embedder exposes no
    ``model`` (unlike the chat client)."""
    name = _client_model(models.embedding(workload_id))
    if name:
        return name
    registry = getattr(models, "registry", None)
    service = registry.service_for(workload_id) if registry is not None else None
    return str(getattr(service, "model", "") or "")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="find_sources_calibrate", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cand = sub.add_parser("candidates", help="write the candidate sets (embedding retrieval only)")
    cand.add_argument("--notebook", required=True)
    cand.add_argument("--topics", required=True, type=Path)
    cand.add_argument("--out", required=True, type=Path)
    cand.add_argument("--per-topic", type=int, default=DEFAULT_PER_TOPIC)
    pre = sub.add_parser("prelabel", help="pre-label candidates with a chat model (needs --confirm)")
    pre.add_argument("--labels", required=True, type=Path)
    pre.add_argument("--workload", default=DEFAULT_WORKLOAD)
    pre.add_argument("--confirm", action="store_true",
                     help="actually send the requests (only after the user agreed)")
    swp = sub.add_parser("sweep", help="choose the two lines from the reviewed labels")
    swp.add_argument("--labels", required=True, type=Path)
    swp.add_argument("--min-recall", type=float, default=0.9)
    swp.add_argument("--min-strong-precision", type=float, default=0.8)
    swp.add_argument("--allow-unreviewed", action="store_true",
                     help="sweep over unreviewed (model) labels too: a provisional curve, "
                          "never written to the config")
    swp.add_argument("--write-config", type=Path, default=None,
                     help=f"write the chosen lines into this config file (normally {_CONFIG_PATH})")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    open_repository: Callable[[Any], AbstractContextManager] = _default_open_repository,
    settings_factory: Optional[Callable[[], Any]] = None,
    out: Any = None,
) -> int:
    out = out or sys.stdout
    if settings_factory is None:
        from app.core.config import Settings

        settings_factory = Settings
    args = _parser().parse_args(argv)
    try:
        if args.command == "sweep":
            data = read_labels(args.labels)
            unreviewed = unreviewed_count(data)
            if unreviewed and args.write_config is not None:
                raise CalibrationError(
                    f"--write-config needs every label reviewed; {unreviewed} are not"
                )
            result = sweep(data, min_recall=args.min_recall,
                           min_strong_precision=args.min_strong_precision,
                           allow_unreviewed=args.allow_unreviewed)
            if unreviewed:
                print(f"warning: {unreviewed} labels are unreviewed model pre-labels; "
                      "this curve is provisional", file=out)
            print(format_sweep(result), file=out)
            chosen = result["chosen"]
            if chosen is None:
                return 1
            if args.write_config is not None:
                write_config(args.write_config, strong=chosen["strong"], related=chosen["related"])
                print(f"wrote {args.write_config}", file=out)
            return 0
        if args.command == "candidates":
            out_path = require_outside_repo(args.out)
            topics = read_topics(args.topics)
            with open_repository(settings_factory()) as repo:
                data = build_candidates(
                    repo, args.notebook, topics, per_topic=args.per_topic,
                    embedding_model=_embedding_model(repo._runtime.models, EMBEDDING_WORKLOAD),
                )
            write_labels(out_path, data)
            total = sum(len(topic["candidates"]) for topic in data["topics"])
            print(f"wrote {total} candidates for {len(topics)} topics to {out_path}", file=out)
            return 0
        # prelabel
        data = read_labels(args.labels)
        pending = pending_prelabels(data)
        with open_repository(settings_factory()) as repo:
            client = repo._runtime.models.chat(args.workload)
            model = _client_model(client)
            print(
                f"prelabel: {len(pending)} model requests to workload "
                f"{args.workload!r} (model {model or 'unknown'})",
                file=out,
            )
            if not args.confirm:
                print("no request sent; re-run with --confirm once the user has agreed", file=out)
                return 0
            if not pending:
                return 0
            counts = run_prelabel(
                data, client, lambda source_id: source_excerpt(repo, source_id),
                save=lambda current: write_labels(args.labels, current),
            )
        write_labels(args.labels, data)
        print(
            f"labelled {counts['labelled']}, unparsable {counts['unparsable']}, "
            f"skipped on transient model errors {counts['transient']} (still pending)",
            file=out,
        )
        return 0
    except CalibrationError as exc:
        print(f"error: {exc}", file=out)
        return 2


if __name__ == "__main__":  # pragma: no cover - manual tool
    raise SystemExit(main())
