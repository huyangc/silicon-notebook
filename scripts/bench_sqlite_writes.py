"""离线 SQLite 写吞吐基准（无 LLM/无嵌入）。
单写者下 N 个并发 writer 各写 RECORDS 条 knowledge_objects, 测吞吐 + 确认无锁。
用法（repo 根）:
  PYTHONPATH=backend python scripts/bench_sqlite_writes.py --workers 1000 --records 100 --mode thread
  PYTHONPATH=backend python scripts/bench_sqlite_writes.py --workers 1000 --records 100 --mode process
"""
import argparse
import os
import tempfile
import time


def _make_repo(db_path):
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ.setdefault("SILICON_NOTEBOOK_STORAGE_DIR", db_path + "_storage")
    os.environ["EVENT_LOG_ENABLED"] = "false"
    os.environ["LLM_LOG_ENABLED"] = "false"
    # 不绑定 embedding workloads → store_kg 不嵌入(纯写基准)
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    return SQLiteRepository(Settings())


def _objs(worker, n):
    return [{"local_id": f"{worker}-{i}", "object_type": "concept",
             "payload": {"name": f"concept {worker}-{i}"}, "evidence": []} for i in range(n)]


def _run_threads(repo, nb_id, workers, records):
    import threading
    errors = []

    def work(w):
        try:
            repo.store_kg(nb_id, None, _objs(w, records), [])
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    ts = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
    t0 = time.perf_counter()
    [t.start() for t in ts]
    [t.join() for t in ts]
    return time.perf_counter() - t0, errors


def _proc_work(args):
    db_path, nb_id, w, records = args
    repo = _make_repo(db_path)
    try:
        repo.store_kg(nb_id, None, _objs(w, records), [])
        return None
    except Exception as exc:  # noqa: BLE001
        return repr(exc)


def _run_processes(db_path, nb_id, workers, records):
    import multiprocessing as mp
    cap = min(workers, (os.cpu_count() or 4) * 8)
    t0 = time.perf_counter()
    with mp.Pool(cap) as pool:
        results = pool.map(_proc_work, [(db_path, nb_id, w, records) for w in range(workers)])
    return time.perf_counter() - t0, [r for r in results if r]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=1000)
    ap.add_argument("--records", type=int, default=100)
    ap.add_argument("--mode", choices=["thread", "process"], default="thread")
    a = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="bench_sqlite_")
    db_path = os.path.join(tmp, "bench.db")
    repo = _make_repo(db_path)
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="bench"))
    total = a.workers * a.records

    if a.mode == "thread":
        elapsed, errors = _run_threads(repo, nb.id, a.workers, a.records)
    else:
        elapsed, errors = _run_processes(db_path, nb.id, a.workers, a.records)

    repo2 = _make_repo(db_path)
    with repo2._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
    locked = len([e for e in errors if "lock" in e.lower()])
    print(f"mode={a.mode} workers={a.workers} records/worker={a.records} total={total}")
    print(f"elapsed={elapsed:.2f}s  throughput={total / elapsed:,.0f} rec/s")
    print(f"locked_errors={locked}  other_errors={len(errors) - locked}")
    print(f"stored={n}/{total}  {'OK' if n == total and not errors else 'MISMATCH/ERRORS'}")
    if errors[:3]:
        print("sample errors:", errors[:3])


if __name__ == "__main__":
    main()
