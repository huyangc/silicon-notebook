"""KG 反向出题生成器(铺量 gold)。用法:
PYTHONPATH=backend python -m app.scripts.gen_recall_gold --notebook nb-xxx --n-obj 30 --n-rel 30 --out backend/app/eval/recall_gold.gen.yaml

每个采样的对象/关系让 LLM 写一道自然问题(强制改写、禁逐字引用),gold=源 id;
leakage_ratio > 0.6 的题剔除(泄漏)。生成后人工抽检并入 recall_gold.yaml。"""
import argparse, json, yaml, random
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.retrieval import _payload_text
from app.eval.retrieval_metrics import leakage_ratio

_GEN_SCHEMA = '{"question":""}'
_LEAK_MAX = 0.6


def _gen_question(client, source_text: str) -> str:
    msg = [{"role": "user", "content":
            "根据下面的知识片段,写一道工程师会问的自然问题,其答案需要用到该片段。"
            "要求:改写表述、不要逐字照抄原文、不要直接点名片段里的专有名词原样串联。\n"
            f"知识片段:{source_text}\n只返回 JSON: {{\"question\":\"...\"}}"}]
    try:
        return str(json.loads(client.chat_json(msg, _GEN_SCHEMA)).get("question", "")).strip()
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notebook", required=True)
    ap.add_argument("--n-obj", type=int, default=30)
    ap.add_argument("--n-rel", type=int, default=30)
    ap.add_argument("--out", default="backend/app/eval/recall_gold.gen.yaml")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    repo = SQLiteRepository(get_settings())
    assert repo.llm_client.configured, "LLM 未配置(.env)"
    out, dropped = [], 0
    with repo._connect() as db:
        objs = db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND status IN ('approved','reviewed')",
            (a.notebook,)).fetchall()
        rels = repo._relations_with_names(db, a.notebook)
    for r in rng.sample(objs, min(a.n_obj, len(objs))):
        src = _payload_text(json.loads(r["payload"] or "{}"))
        q = _gen_question(repo.llm_client, src)
        if q and leakage_ratio(q, src) <= _LEAK_MAX:
            out.append({"id": f"r-obj-{r['id'][-6:]}", "track": "reverse", "bucket": "node",
                        "question": q, "gold_object_ids": [r["id"]]})
        else:
            dropped += 1
    for r in rng.sample(rels, min(a.n_rel, len(rels))):
        q = _gen_question(repo.llm_client, r["text"])
        if q and leakage_ratio(q, r["text"]) <= _LEAK_MAX:
            out.append({"id": f"r-rel-{r['id'][-6:]}", "track": "reverse", "bucket": "bridge",
                        "question": q, "gold_relation_ids": [r["id"]],
                        "gold_object_ids": [r["source_object_id"], r["target_object_id"]]})
        else:
            dropped += 1
    yaml.safe_dump(out, open(a.out, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
    print(f"[gen] wrote {len(out)} questions ({dropped} dropped as leakage) -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
