"""Shared CLI table for the schema-guest regression suites (SQLite + PG twin).

Every non-serving entry point that opens the deployment database is listed
here once, with a way to drive it far enough to compose its repository. The
two suites (``test_cli_schema_guest.py`` and
``postgres/test_cli_schema_guest_pg.py``) run the same table against their own
backend, so a CLI added to one cannot be forgotten by the other.

``run_case`` normalizes the three ways these tools end -- a returned exit
status, ``SystemExit``, or an escaping ``MaintenanceCliError`` (the eval
helpers are plain functions) -- into one integer.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterator

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
GUEST_SEAMS = (
    "create_repository",
    "create_schema_guest_repository",
    "create_bare_schema_guest_repository",
)
#: Environment keys a deployment env file must carry for the env-file-only tool.
DEPLOYMENT_ENV_KEYS = (
    "DATABASE_URL",
    "SILICON_NOTEBOOK_STORAGE_DIR",
    "SILICON_NOTEBOOK_ADMIN_PASSWORD",
    "MODEL_SERVICES_CONFIG",
    "EXTENSIONS_CONFIG",
    "EVENT_LOG_DIR",
    "LLM_LOG_PATH",
    "DIAGNOSTICS_DIR",
)


@dataclass(frozen=True)
class CliContext:
    tmp: Path
    #: ``["--confirm-service-stopped"]`` on PostgreSQL, empty on SQLite.
    confirm: tuple[str, ...]


@dataclass(frozen=True)
class CliCase:
    name: str
    invoke: Callable[[CliContext, object], object]
    #: Tools that named ``SQLiteRepository`` directly keep refusing PostgreSQL.
    sqlite_only: bool = False
    #: False for a tool that opens a bare database handle rather than a
    #: repository (it runs the same version gate, but no guest seam).
    composes: bool = True


class Opened(BaseException):
    """Raised by the seam spy right after a real guest composition: proves the
    CLI reached the shared seam, and stops it before it does any work.
    BaseException so no CLI's ``except Exception`` can swallow it."""


def load_script(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(f"schema_guest_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _argv(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", list(argv))


def _fresh_settings_cache() -> None:
    from app.core.config import get_settings

    get_settings.cache_clear()


def _patch_seams(module, monkeypatch, spy) -> None:
    if spy is None:
        return
    for name in GUEST_SEAMS:
        if hasattr(module, name):
            monkeypatch.setattr(module, name, spy[name])


def _batch(args_fn):
    def invoke(ctx, monkeypatch, spy=None):
        from app.services import batch_ingest

        return batch_ingest.main([*args_fn(ctx), *ctx.confirm])

    return invoke


def _root_wrapper(name: str, *args: str):
    def invoke(ctx, monkeypatch, spy=None):
        module = load_script(name)
        _argv(monkeypatch, name, *args, *ctx.confirm)
        return module.main()

    return invoke


def _backend_wrapper(module_name: str):
    def invoke(ctx, monkeypatch, spy=None):
        module = importlib.import_module(module_name)
        _fresh_settings_cache()
        _argv(monkeypatch, module_name, "nb-x", *ctx.confirm)
        return module.main()

    return invoke


def _prepare_selected_source(ctx, monkeypatch, spy=None):
    env_file = ctx.tmp / "deployment.env"
    env_file.write_text(
        "".join(
            f"{key}={os.environ[key]}\n" for key in DEPLOYMENT_ENV_KEYS if key in os.environ
        )
        + "EVENT_LOG_ENABLED=false\nLLM_LOG_ENABLED=false\n",
        encoding="utf-8",
    )
    module = load_script("prepare_selected_source_graph")
    return module.main([
        "--env-file", str(env_file),
        "--receipt", str(ctx.tmp / "selected-source-receipt.json"),
        "--confirm-service-stopped",
    ])


def _global_attribution(ctx, monkeypatch, spy=None):
    from app.services import global_ask_attribution_backfill

    return global_ask_attribution_backfill.main([])


def _navigation_dry_run(ctx, monkeypatch, spy=None):
    from app.services import navigation_backfill_cli

    return navigation_backfill_cli.main(["--all-notebooks", "--dry-run"])


def _navigation_rerun(ctx, monkeypatch, spy=None):
    from app.services import navigation_backfill_cli

    return navigation_backfill_cli.main(["--all-notebooks", *ctx.confirm])


def _knowhow_md(ctx, monkeypatch, spy=None):
    module = load_script("backfill_knowhow_md")
    _patch_seams(module, monkeypatch, spy)
    return module.main(["--notebook", "nb-x", "--use-llm"])


def _replay(ctx, monkeypatch, spy=None):
    module = load_script("replay_retrieval")
    return module.record_run("nb-x", [], False, {}, "admin")


def _gen_recall_gold(ctx, monkeypatch, spy=None):
    from app.scripts import gen_recall_gold

    _patch_seams(gen_recall_gold, monkeypatch, spy)
    _fresh_settings_cache()
    _argv(monkeypatch, "gen_recall_gold", "--notebook", "nb-x", "--out", str(ctx.tmp / "g.yaml"))
    return gen_recall_gold.main()


def _kg_product_smoke(ctx, monkeypatch, spy=None):
    module = load_script("kg_product_smoke")
    _patch_seams(module, monkeypatch, spy)
    return module.main()


def _eval_run_all(ctx, monkeypatch, spy=None):
    from app.eval import run_all

    _argv(monkeypatch, "run_all", "--only", "recall", "--notebook", "nb-x", "--out", str(ctx.tmp / "eval"))
    return run_all.main()


def _eval_inference(ctx, monkeypatch, spy=None):
    from app.eval import inference

    return inference.run_inference("nb-x")


def _eval_speed(ctx, monkeypatch, spy=None):
    from app.eval import speed

    return speed.measure_speed(str(ctx.tmp / "missing.md"))


def _delete_leftovers(ctx, monkeypatch, spy=None):
    from app.migration import legacy_leftover_sweep

    return legacy_leftover_sweep.main([])


def _notebook_export(ctx, monkeypatch, spy=None):
    module = load_script("notebook_archive")
    return module.main([
        "export", "--notebook", "nb-x", "--out", str(ctx.tmp / "nb-x.snbk"),
    ])


def _notebook_import(ctx, monkeypatch, spy=None):
    """The import writes: it opens the maintenance repository (operator lock,
    stopped-service confirmation on PostgreSQL) before it reads the archive."""
    module = load_script("notebook_archive")
    return module.main([
        "import", str(ctx.tmp / "nb-x.snbk"), "--owner", "admin", *ctx.confirm,
    ])


def _reflect_search(ctx, monkeypatch, spy=None):
    from app.core.config import Settings

    rig = load_script("reflect_shadow_rig")
    return rig._search_repository(Settings())


def _reflect_report(ctx, monkeypatch, spy=None):
    rig = load_script("reflect_shadow_rig")
    return rig._run_reports(
        SimpleNamespace(user="admin"), None, [], {}, ctx.tmp / "report-trace.jsonl"
    )


CASES: tuple[CliCase, ...] = (
    CliCase("batch_ingest embed", _batch(lambda ctx: ["embed", "--notebook-id", "nb-x"])),
    CliCase(
        "batch_ingest backfill-images",
        _batch(lambda ctx: [
            "backfill-images", "--notebook-id", "nb-x",
            "--mineru-output", str(ctx.tmp),
        ]),
    ),
    CliCase("build_chunks", _root_wrapper("build_chunks", "nb-x")),
    CliCase("backfill_kg_embeddings", _root_wrapper("backfill_kg_embeddings", "nb-x")),
    CliCase("reextract_notebook", _root_wrapper("reextract_notebook", "nb-x")),
    CliCase(
        "denoise_reextract_nb",
        _root_wrapper("denoise_reextract_nb", "--notebook-id", "nb-x", "--full"),
    ),
    CliCase("prepare_selected_source_graph", _prepare_selected_source),
    CliCase("kg build_kg", _backend_wrapper("app.scripts.build_kg")),
    CliCase("kg recluster_kg", _backend_wrapper("app.scripts.recluster_kg")),
    CliCase("kg reembed_kg", _backend_wrapper("app.scripts.reembed_kg")),
    CliCase(
        "kg backfill_relation_embeddings",
        _backend_wrapper("app.scripts.backfill_relation_embeddings"),
    ),
    CliCase("backfill_global_ask_attribution", _global_attribution),
    CliCase("backfill_navigation_chunks --dry-run", _navigation_dry_run),
    CliCase("backfill_navigation_chunks", _navigation_rerun),
    CliCase("backfill_knowhow_md --use-llm", _knowhow_md, sqlite_only=True),
    CliCase("replay_retrieval", _replay, sqlite_only=True),
    CliCase("gen_recall_gold", _gen_recall_gold, sqlite_only=True),
    CliCase("kg_product_smoke", _kg_product_smoke, sqlite_only=True),
    CliCase("eval run_all recall", _eval_run_all, sqlite_only=True),
    CliCase("eval inference", _eval_inference, sqlite_only=True),
    CliCase("eval speed", _eval_speed, sqlite_only=True),
    CliCase("maintain delete-leftovers", _delete_leftovers, composes=False),
    CliCase("notebook export", _notebook_export),
    CliCase("notebook import", _notebook_import),
    CliCase("reflect_shadow_rig search", _reflect_search),
    CliCase("reflect_shadow_rig report", _reflect_report),
)


def run_case(case: CliCase, ctx: CliContext, monkeypatch, spy=None) -> int:
    """Drive one CLI; return its exit status whichever way it ends."""
    from app.services.maintenance_cli import MaintenanceCliError

    _fresh_settings_cache()
    try:
        result = case.invoke(ctx, monkeypatch, spy)
    except SystemExit as exc:
        if isinstance(exc.code, str):  # ``raise SystemExit("message")``
            print(exc.code, file=sys.stderr)
            return 1
        return int(exc.code or 0)
    except MaintenanceCliError as exc:
        # A plain helper (the eval functions) lets the refusal escape; its
        # uncaught traceback is what the operator would read.
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    finally:
        _fresh_settings_cache()
    return result if isinstance(result, int) else 0


@contextmanager
def guest_seam_spy(monkeypatch) -> Iterator[dict]:
    """Wrap the three shared guest seams: each really composes (so the
    composition's own effect on the database is what the caller measures),
    closes, records the seam name, then raises :class:`Opened`."""
    from app.services import maintenance_cli

    reached: list[str] = []
    real = {name: getattr(maintenance_cli, name) for name in GUEST_SEAMS}

    def wrap(name):
        def spy(settings, *args, **kwargs):
            repository = real[name](settings, *args, **kwargs)
            repository.close()
            reached.append(name)
            raise Opened(name)

        return spy

    spies = {name: wrap(name) for name in GUEST_SEAMS}
    for name, spy in spies.items():
        monkeypatch.setattr(maintenance_cli, name, spy)
    spies["reached"] = reached  # type: ignore[assignment]
    yield spies
