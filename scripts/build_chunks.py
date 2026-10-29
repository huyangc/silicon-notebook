"""为现有 notebook 回填 chunk + chunk_embedding(不重抽 KG)。
用法: PYTHONPATH=backend python scripts/build_chunks.py <notebook_id>
      [--source <source_id>] [--confirm-service-stopped]

``--source`` 只重建这一个来源(``chunk_and_embed_source`` 本来就是单源粒度),
嵌入成本只按它自己的分块数计;不给则重建该笔记本全部用户来源。来源必须属于
该笔记本且是用户来源(非 Memory / knowhow / 晋升出处),否则退出码 2、什么都不写。"""
import sys
from app.core.config import Settings
from app.services.maintenance_cli import (
    MaintenanceCliError,
    open_maintenance_cli_repository,
)


_PAGE_SIZE = 500
_USAGE = (
    "usage: build_chunks.py <notebook_id> [--source <source_id>] "
    "[--confirm-service-stopped]"
)
#: The same roster ``user_source_ids_page`` walks: user-visible documents only.
_NOT_REBUILT_TYPES = frozenset({"memory", "knowhow", "promotion"})


def _parse(argv: list[str]) -> "tuple[str, str, bool] | None":
    args = [arg for arg in argv if arg != "--confirm-service-stopped"]
    confirmed = len(args) != len(argv)
    source_id = ""
    if "--source" in args:
        at = args.index("--source")
        if at + 1 >= len(args) or args.count("--source") != 1:
            return None
        source_id = args[at + 1]
        del args[at:at + 2]
        if not source_id or source_id.startswith("--"):
            return None
    if len(args) != 1 or args[0].startswith("--"):
        return None
    return args[0], source_id, confirmed


def main() -> int:
    parsed = _parse(sys.argv[1:])
    if parsed is None:
        print(_USAGE)
        return 2
    notebook_id, source_id, confirmed = parsed
    settings = Settings()
    try:
        with open_maintenance_cli_repository(
            settings, confirm_service_stopped=confirmed
        ) as repo:
            mnt = repo.maintenance
            if source_id:
                try:
                    source = repo.get_source(source_id)
                except KeyError:
                    source = None
                if (
                    source is None
                    or source.notebook_id != notebook_id
                    or source.type in _NOT_REBUILT_TYPES
                ):
                    print(
                        f"error: {source_id} is not a user source of {notebook_id}",
                        file=sys.stderr,
                    )
                    return 2
                try:
                    mnt.chunk_and_embed_source(source_id)
                except Exception as exc:
                    print(f"[1] {source_id} FAILED: {exc}", flush=True)
                    return 1
                print(f"[1] {source_id} ok", flush=True)
                print("sources: 1", flush=True)
                return 0
            processed = 0
            after_id = ""
            while sids := mnt.user_source_ids_page(
                notebook_id, after_id=after_id, limit=_PAGE_SIZE
            ):
                for sid in sids:
                    processed += 1
                    try:
                        mnt.chunk_and_embed_source(sid)
                        print(f"[{processed}] {sid} ok", flush=True)
                    except Exception as exc:
                        print(f"[{processed}] {sid} FAILED: {exc}", flush=True)
                after_id = sids[-1]
            print(f"sources: {processed}", flush=True)
            print(f"total chunks: {mnt.count_chunks(notebook_id)}", flush=True)
    except MaintenanceCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
