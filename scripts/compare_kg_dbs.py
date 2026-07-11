"""对比 pre-denoise 快照 与 去噪重抽后 的 nb-012 KG，评估去噪/优化成效。
用法: PYTHONPATH=backend python scripts/compare_kg_dbs.py
"""
import re

from app.repositories.sqlite.maintenance import ReadOnlySQLiteInspector
from app.services.kg.filters import is_noise_concept

ROOT = "/Users/hzf/workspace/silicon_notebook/.local"
OLD = f"{ROOT}/backups/snapshot_pre_denoise_20260606_103747.db"
NEW = f"{ROOT}/silicon_notebook.db"
NB = "nb-012fb94249"

# 用新库的白名单对两库统一口径分类(mode=ro,绝不写)
WL = ReadOnlySQLiteInspector(NEW).concept_whitelist_terms()


def concepts(path):
    return ReadOnlySQLiteInspector(path).concept_names(NB)


def analyze(path, label):
    names = concepts(path)
    total = len(names)
    noise = 0
    reasons = {}
    for nm in names:
        is_n, why = is_noise_concept(nm, WL)
        if is_n:
            noise += 1
            reasons[why] = reasons.get(why, 0) + 1
    print(f"[{label}] concepts={total}  noise={noise} ({100*noise/max(1,total):.1f}%)  clean={total-noise}")
    print(f"   by_reason={dict(sorted(reasons.items(), key=lambda x: -x[1]))}")
    return names


print(f"whitelist_terms={len(WL)}\n")
print("=== 概念噪声率 (is_noise_concept 统一口径) ===")
old_names = analyze(OLD, "OLD pre-denoise ")
new_names = analyze(NEW, "NEW denoised    ")

# 具体噪声模式直接计数(不依赖白名单)
PATTERNS = {
    "符号(含_或^)": lambda s: ("_" in s or "^" in s),
    "图表引用Fig/Table": lambda s: bool(re.match(r"^(fig|figure|table|eq|equation)\b", s, re.I)),
    "章节号N.N开头": lambda s: bool(re.match(r"^\d+\.\d", s)),
    "实例标号^X\\d+$": lambda s: bool(re.match(r"^[A-Za-z]\d+$", s)),
    "≤2字符": lambda s: len(s.strip()) <= 2,
}
print("\n=== 具体噪声模式计数 (OLD vs NEW) ===")
for label, fn in PATTERNS.items():
    o = sum(1 for s in old_names if fn(s))
    n = sum(1 for s in new_names if fn(s))
    print(f"  {label:24s} OLD={o:5d}  NEW={n:5d}")
