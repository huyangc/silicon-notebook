"""抽取速度:截片段真实抽取计时 + 公式外推。"""
from __future__ import annotations
import json, math, pathlib, shutil, tempfile, time, uuid
from datetime import datetime
from statistics import median
from typing import Dict, List, Optional, Tuple

from app.services.kg_ingest import plan_window_size


def plan_windows(chars: int, workers: int, w_min: int, w_max: int) -> Tuple[int, int]:
    size = plan_window_size(chars, workers, w_min, w_max)
    n = math.ceil(chars / size) if size else 0
    return size, n


def estimate_extract_seconds(n_windows: int, effective_concurrency: int,
                             per_window_p50_s: float, fixed_overhead_s: float) -> float:
    conc = max(1, effective_concurrency)
    batches = math.ceil(n_windows / conc) if n_windows else 0
    return round(batches * per_window_p50_s + fixed_overhead_s, 2)


def parse_llm_log(path: str, since_ts: str) -> Dict[str, float]:
    lats: List[float] = []
    tokens = 0
    retries = 0
    try:
        raw = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return {"calls": 0, "retries": 0, "latency_p50_s": 0.0,
                "latency_p95_s": 0.0, "total_tokens": 0}
    for line in raw:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("kind") != "chat" or rec.get("ts", "") < since_ts:
            continue
        if rec.get("status") == "retry":
            retries += 1
            continue
        if rec.get("status") != "ok":
            continue
        lats.append(rec.get("latency_ms", 0) / 1000.0)
        tokens += (rec.get("usage") or {}).get("total_tokens", 0)
    lats.sort()

    def pct(p):
        if not lats:
            return 0.0
        return round(lats[min(len(lats) - 1, int(p * len(lats)))], 3)
    return {
        "calls": len(lats),
        "retries": retries,
        "latency_p50_s": round(median(lats), 3) if lats else 0.0,
        "latency_p95_s": pct(0.95),
        "total_tokens": tokens,
    }


def _truncate_on_paragraph(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n\n", 0, limit)
    return text[:cut] if cut > 1000 else text[:limit]


def _insert_source(repo, nb_id, name, text, tmpdir):
    from app.services.kg.parsing import parse_elements
    from app.services.sqlite_repository import _now
    f = pathlib.Path(tmpdir) / f"{name}.md"
    f.write_text(text, encoding="utf-8")
    sid = f"src-{uuid.uuid4().hex[:10]}"
    now = _now()
    els = parse_elements(text, source_file=str(f))
    with repo._connect() as db:
        db.execute(
            """INSERT INTO sources
               (id, notebook_id, title, source_type, status, parse_status,
                file_name, file_path, file_size, file_hash, summary, doc_type,
                created_at, updated_at)
               VALUES (?, ?, ?, 'markdown', 'extracted', 'parsed', ?, ?, 0, '', '', ?, ?, ?)""",
            (sid, nb_id, name, f"{name}.md", str(f), "textbook", now, now))
        for el in els:
            db.execute(
                """INSERT INTO source_elements
                   (id, source_id, element_type, location_label, text, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, '{}', ?)""",
                (f"el-{uuid.uuid4().hex[:10]}", sid, el.type,
                 f"L{el.line_start}-{el.line_end}", el.text, now))
    return sid


def _cleanup(repo, nb_id):
    with repo._connect() as db:
        sids = [r[0] for r in db.execute(
            "SELECT id FROM sources WHERE notebook_id=?", (nb_id,)).fetchall()]
        for sid in sids:
            db.execute("DELETE FROM source_elements WHERE source_id=?", (sid,))
        db.execute("DELETE FROM knowledge_relations WHERE notebook_id=?", (nb_id,))
        db.execute("DELETE FROM knowledge_objects WHERE notebook_id=?", (nb_id,))
        db.execute("DELETE FROM knowledge_embeddings WHERE notebook_id=?", (nb_id,))
        db.execute("DELETE FROM sources WHERE notebook_id=?", (nb_id,))
        db.execute("DELETE FROM notebooks WHERE id=?", (nb_id,))


def measure_speed(source_md_path: str, char_steps: Optional[List[int]] = None,
                  llm_log_path: str = ".local/logs/llm.jsonl") -> List[dict]:
    """对一份源 md 按 char_steps 各截一段、真实抽取计时。用临时 notebook,跑完清理。"""
    from app.core.config import Settings
    from app.models.schemas import NotebookCreate
    from app.services.sqlite_repository import SQLiteRepository
    char_steps = char_steps or [5000, 20000, 50000, 100000, 200000]
    settings = Settings()
    repo = SQLiteRepository(settings)
    assert repo.llm_client.configured, "LLM 未配置(.env)"
    full = pathlib.Path(source_md_path).read_text(encoding="utf-8")
    tmpdir = tempfile.mkdtemp()
    results: List[dict] = []
    try:
        for limit in char_steps:
            text = _truncate_on_paragraph(full, limit)
            nb = repo.create_notebook(NotebookCreate(name=f"eval-speed-{limit}-{uuid.uuid4().hex[:6]}"))
            try:
                sid = _insert_source(repo, nb.id, f"seg{limit}", text, tmpdir)
                since = datetime.now().isoformat()
                t0 = time.perf_counter()
                repo._run_extraction(sid)
                wall = time.perf_counter() - t0
                size, n = plan_windows(len(text), settings.kg_extract_workers,
                                       settings.kg_window_min_chars, settings.kg_window_max_chars)
                stats = parse_llm_log(llm_log_path, since)
                eff = max(1, min(n, stats["calls"]) if stats["calls"] else n)
                results.append({
                    "chars": len(text), "n_windows": n, "window_size": size,
                    "wall_s": round(wall, 2),
                    "latency_p50_s": stats["latency_p50_s"], "latency_p95_s": stats["latency_p95_s"],
                    "total_tokens": stats["total_tokens"], "retries": stats["retries"],
                    "effective_concurrency": eff,
                })
                print(f"[speed] {len(text)} chars -> {n} win, wall={wall:.1f}s, "
                      f"p50={stats['latency_p50_s']}s, retries={stats['retries']}", flush=True)
            finally:
                _cleanup(repo, nb.id)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return results


def extrapolate(measured: List[dict], target_chars: List[int],
                workers: int, w_min: int, w_max: int) -> List[dict]:
    p50 = median([m["latency_p50_s"] for m in measured if m["latency_p50_s"]] or [2.0])
    eff = max((m["effective_concurrency"] for m in measured), default=16)
    overhead = min((m["wall_s"] for m in measured), default=3.0)
    out = []
    for c in target_chars:
        size, n = plan_windows(c, workers, w_min, w_max)
        out.append({"chars": c, "n_windows": n,
                    "est_s": estimate_extract_seconds(n, eff, p50, overhead)})
    return out
