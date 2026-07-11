"""P1: 在现有 notebook 的 concept 上离线试跑 is_noise_concept（无 LLM、无写库）。
用法：
  cd /Users/hzf/workspace/silicon_notebook
  PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python scripts/validate_concept_filter.py
"""
from app.repositories.sqlite.maintenance import ReadOnlySQLiteInspector
from app.services.kg.filters import is_noise_concept

DB = ".local/silicon_notebook.db"
NB = "nb-012fb94249"


def main():
    insp = ReadOnlySQLiteInspector(DB)
    wl = insp.concept_whitelist_terms()
    names = insp.concept_names(NB)
    dropped, kept, reasons = [], [], {}
    for name in names:
        noise, reason = is_noise_concept(name, wl)
        if noise:
            dropped.append((name, reason))
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            kept.append(name)
    print(f"whitelist_terms={len(wl)} concepts={len(names)} dropped={len(dropped)} kept={len(kept)}")
    print("by_reason:", reasons)
    print("\n--- DROPPED 抽样 (前 50) ---")
    for n, why in dropped[:50]:
        print(f"  [{why}] {n}")
    print("\n--- KEPT 抽样 (每 150 个取 1，查误伤) ---")
    for n in kept[::150][:50]:
        print(f"  {n}")


if __name__ == "__main__":
    main()
