"""离线建图 CLI:python -m app.scripts.build_kg <notebook_id>。
对 tier='base' 权威库一次性构建尤其适用(不占交互式服务)。"""
import sys
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m app.scripts.build_kg <notebook_id>", file=sys.stderr)
        return 2
    repo = SQLiteRepository(get_settings())
    print(repo.build_notebook_kg(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
