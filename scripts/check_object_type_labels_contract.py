#!/usr/bin/env python3
"""跨栈契约:前端 kg-type-mark.tsx 的 KG_TYPE_LABELS 内置项必须逐字等于后端
OBJECT_TYPE_LABELS。任一侧改了 object_type 的显示名而另一侧没跟,这里失败。
object_type 有前后端两份真源(后端 API 下发 + 前端只有 type 字符串时的小表),
severity 那次的漏网教训就是「没有守卫钉住两份真源」。由 scripts/check.sh 运行。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from app.services.extraction_profiles import OBJECT_TYPE_LABELS  # noqa: E402


def frontend_labels() -> dict[str, str]:
    text = (ROOT / "frontend/app/kg-type-mark.tsx").read_text(encoding="utf-8")
    m = re.search(r"const KG_TYPE_LABELS[^{]*\{(.*?)\};", text, re.S)
    if not m:
        raise SystemExit("kg-type-mark.tsx: KG_TYPE_LABELS 对象字面量未找到")
    return dict(re.findall(r'(\w+):\s*"([^"]+)"', m.group(1)))


def main() -> int:
    backend = dict(OBJECT_TYPE_LABELS)
    frontend = frontend_labels()
    if backend != frontend:
        print("object_type label 跨栈契约 MISMATCH", file=sys.stderr)
        print(f"  backend : {backend}", file=sys.stderr)
        print(f"  frontend: {frontend}", file=sys.stderr)
        diff = {k: (backend.get(k), frontend.get(k))
                for k in set(backend) | set(frontend)
                if backend.get(k) != frontend.get(k)}
        print(f"  差异(backend, frontend): {diff}", file=sys.stderr)
        return 1
    print(f"object_type label 契约 OK: {sorted(backend)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
