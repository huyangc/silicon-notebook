"""Backend-neutral knowhow asset-reference helpers.

Two distinct predicates live here, deliberately with DIFFERENT strictness —
they answer different questions and the asymmetry is the safety argument:

* ``rendered_asset_ids`` — "does this write NEWLY INTRODUCE a live image
  reference?"  Strict: only the rendered image form ``![alt](asset://<id>)``
  counts, because prose or a code sample merely MENTIONING ``asset://x``
  renders as literal text and must not be rejected as a dangling reference.
* ``asset_ref_tokens`` / ``keepers_among`` — "may the sweeper reclaim this
  asset?"  Loose: any ``asset://<id>`` substring keeps it, matching the
  per-asset ``LIKE '%asset://<id>%'`` the orphan sweep has always used.
  Over-matching here only ever RETAINS an asset, never wrongly deletes one.

``content_strings_in_payload`` (the history-payload field extractor the sweep
searches) lives here rather than in either adapter because BOTH the SQLite and
the PostgreSQL maintenance adapters scan ``knowhow_changes``; an adapter
importing its sibling for it would cross the backend boundary.
"""
from __future__ import annotations

import bisect
import json
import re
from collections.abc import Iterable, Sequence


_ASSET_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(asset://([A-Za-z0-9_-]+)\)")

# Loose sweeper-side scan: every ``asset://`` occurrence and the id-shaped run
# that follows it. ``[A-Za-z0-9_-]`` is the character class store-generated ids
# live in (``_new_id`` = ``asset-<32 hex>``), so the maximal run after
# ``asset://`` always CONTAINS a referenced id as its prefix — see
# ``keepers_among`` for why prefix, not equality, is the safe comparison.
#
# ⚠ Scanned as "find every ``asset://`` marker, then read the run that starts at
# its end", NOT as one combined pattern. A combined ``asset://([...]+)`` would
# make the two markers in ``asset://Xasset://Y`` non-overlapping matches of a
# single regex: the first match's run swallows the literal ``asset`` of the
# second, the scan resumes past it, and ``Y`` is never seen — an UNDER-match,
# the one direction this predicate may never take. ``finditer`` over the bare
# marker finds both.
_ASSET_MARKER_RE = re.compile(r"asset://")
_ASSET_ID_SHAPE_RE = re.compile(r"[A-Za-z0-9_-]+")


def rendered_asset_ids(content_md: str) -> frozenset[str]:
    """Return only live rendered-image refs, not prose/code literals."""
    return frozenset(_ASSET_IMAGE_REF_RE.findall(str(content_md or "")))


def required_asset_ids(
    explicit: Sequence[str], changes: Iterable[tuple[str, str]]
) -> tuple[str, ...]:
    """Canonical refs that a write newly introduces.

    ``changes`` contains ``(old_content, new_content)`` pairs for every actual
    target. A legacy dead ref retained by an unrelated edit is not revalidated;
    adding that same ref to another target still is. Caller-supplied ids remain
    authoritative and are unioned with the computed deltas.
    """
    required = {str(asset_id) for asset_id in explicit if str(asset_id)}
    for old_content, new_content in changes:
        required.update(
            rendered_asset_ids(new_content) - rendered_asset_ids(old_content)
        )
    return tuple(sorted(required))


# ---------------------------------------------------------------------------
# Orphan-sweep side: ONE pass over the notebook's text, not one LIKE per asset.
# ---------------------------------------------------------------------------


def asset_ref_tokens(text: object) -> list[str]:
    """Every id-shaped run following an ``asset://`` in ``text``.

    The sweep accumulates these across a single streamed pass over the
    notebook's live cell content and history payloads; membership then answers
    "is this asset still referenced?" for EVERY asset at once, replacing the
    old ``O(assets × cells)`` per-asset leading-wildcard ``LIKE`` scans.

    A marker with nothing id-shaped after it (``asset://`` at end of text, or
    followed by ``)``) yields no token — nothing could have matched it either.
    """
    haystack = str(text or "")
    out: list[str] = []
    for marker in _ASSET_MARKER_RE.finditer(haystack):
        run = _ASSET_ID_SHAPE_RE.match(haystack, marker.end())
        if run is not None:
            out.append(run.group(0))
    return out


def collect_change_payload_tokens(kind: str, payload_json: object, into: set) -> None:
    """Add the ``asset://`` tokens carried by ONE ``knowhow_changes`` row.

    Only the ``content_md``-shaped fields are read (see
    ``content_strings_in_payload``); a code attachment's ``code_text`` is never
    a keeper reference, whichever ``kind`` embeds it.
    """
    payload = json.loads(payload_json)
    for text in content_strings_in_payload(str(kind), payload):
        into.update(asset_ref_tokens(text))


def keepers_among(asset_ids: Iterable[str], tokens: Iterable[str]) -> set:
    """The subset of ``asset_ids`` that ``tokens`` keeps alive.

    Restates the retired per-asset ``LIKE '%asset://<id>%'``: that predicate was
    true iff, at some ``asset://`` occurrence, the characters that follow BEGIN
    with the id. An id drawn from ``[A-Za-z0-9_-]`` is therefore entirely
    consumed into the maximal run ``asset_ref_tokens`` captures — hence
    ``token.startswith(asset_id)``, not ``token == asset_id``. Equality alone
    would be strictly LESS permissive (an id that happens to be a prefix of a
    longer referenced id would become collectable), and the sweep's whole
    asymmetry is that over-matching may only ever retain.

    **Scope of that equivalence** (deliberately narrower than "provably
    identical for all inputs"): it holds for ids this repository actually mints
    (``_new_id`` → ``asset-<32 hex>``, entirely inside the token charset) across
    every reference form, adjacent markers included. Two registered, accepted
    departures:

    * **Case.** SQLite's ``LIKE`` is ASCII-case-insensitive by default, so the
      retired SQLite predicate also matched ``ASSET://<ID>``; PostgreSQL's was
      case-sensitive. This implementation is case-sensitive, converging the two
      backends on PostgreSQL's口径. Minted ids are lowercase hex and the marker
      is machine-written, so no real reference changes classification — but on
      SQLite this is the one direction where the delete set could widen, and it
      is registered rather than papered over.
    * **Non-id-shaped ids.** An id containing characters outside the token
      charset cannot be reasoned about this way and is conservatively KEPT. It
      can only arise from an injected id generator, and "keep" is the safe
      answer for a garbage collector.

    Cost: ``O(T log T)`` to sort the tokens once (lazily — only when some
    candidate misses the exact-membership fast path) plus ``O(log T)`` per
    remaining candidate, i.e. ``O(orphans × log T)`` rather than the
    ``O(orphans × T)`` a linear ``startswith`` sweep would pay. The bisect is
    exact, not a heuristic: every string beginning with ``key`` is ``>= key``,
    and any token strictly between ``key`` and the smallest such string must
    differ from ``key`` at an index inside ``key`` with a GREATER character, so
    it sorts after every ``key``-prefixed string. The first token ``>= key`` is
    therefore ``key``-prefixed if any token is.
    """
    tokens = set(tokens)
    kept = set()
    ordered: list[str] | None = None
    for asset_id in asset_ids:
        key = str(asset_id)
        if key in tokens:
            kept.add(asset_id)
            continue
        if _ASSET_ID_SHAPE_RE.fullmatch(key) is None:
            kept.add(asset_id)
            continue
        if ordered is None:
            ordered = sorted(tokens)
        # ``bisect_right`` would behave identically HERE only because the exact
        # membership branch above already consumed the ``token == key`` case;
        # ``bisect_left`` is the one that stays correct without that precondition,
        # so it is the spelling to keep if these branches are ever reordered.
        index = bisect.bisect_left(ordered, key)
        if index < len(ordered) and ordered[index].startswith(key):
            kept.add(asset_id)
    return kept


# ---------------------------------------------------------------------------
# History payload field extraction (moved here from the SQLite history store so
# both maintenance adapters can share it without importing each other).
# ---------------------------------------------------------------------------


#: Every ``record_change`` ``kind`` this extractor routes explicitly. Closed set
#: BY DESIGN — an unregistered kind raises rather than falling back to "treat the
#: whole payload as content", which would silently reintroduce the
#: code_text-leaks-through-unrelated-payload bug ``content_strings_in_payload``
#: exists to close. Module level (not a function local) so the guard in
#: ``tests/test_knowhow_history_kind_registry_guard.py`` can reconcile it against
#: the kinds the two ``KnowhowStore`` implementations actually record.
#: 与 spec §4.1 的 kind 枚举一一对应。⚠️ 别把 md_normalize.py 里 markdown
#: 分词器的 kind（"autolink" 等）扫进来——那是同名不同物，与变更流水无关。
REGISTERED_KINDS = frozenset({
    "cell_update", "revert", "row_add", "import_append", "row_delete",
    "table_create", "column_delete", "column_add", "column_rename",
    "column_kind", "anchor_set", "table_meta", "cell_code_put",
    "cell_code_delete",
})


def _rows_content(entries: "list[dict] | None") -> list:
    """``rows``-shaped entries (``row_add``/``import_append``/``row_delete``/
    ``table_create``'s own ``rows``, and ``revert``'s ``rows_added``/
    ``rows_removed``): each entry's ``cells`` dict values are content_md,
    each entry's ``code`` list is a row's remembered CODE ATTACHMENTS
    (CASCADE-doomed alongside the row) — deliberately never consulted here,
    see ``content_strings_in_payload``'s own docstring for why."""
    texts: list = []
    for entry in entries or []:
        texts.extend((entry.get("cells") or {}).values())
        # entry.get("code") intentionally never read — code_text, not content.
    return texts


def _column_cells_content(cells: "list[dict] | None") -> list:
    """``column_delete``-shaped ``cells`` entries (also ``revert``'s
    ``columns_added``/``columns_removed[].cells``): ``{row_id,
    content_md}`` — the sibling ``code`` list (if present on the same
    entry) is that column's remembered code attachments and is, again,
    deliberately never consulted."""
    texts: list = []
    for cell in cells or []:
        content = cell.get("content_md")
        if content is not None:
            texts.append(content)
    return texts


def content_strings_in_payload(kind: str, payload: dict) -> list:
    """Every ``content_md``-shaped string embedded ANYWHERE in one change's
    payload — the reference corpus the orphan-asset sweep (knowhow 表版本管理
    Task 13, spec §7.1) searches for a surviving ``asset://<id>`` substring in
    HISTORY.

    Deliberately narrower than "the whole payload_json blob". Several kinds
    (``row_delete``/``column_delete``/``revert``) carry a row's or column's
    remembered CODE ATTACHMENT (``knowhow_cell_code``, migration 17,
    CASCADE-doomed alongside the row/column it belonged to) in the SAME payload
    as its genuine, rendered ``content_md`` — a code attachment's ``code_text``
    is source-code text, not rendered markdown, the exact same "not a keeper
    reference" boundary the sweep has always drawn for a LIVE
    ``knowhow_cell_code`` row. A prior implementation tried to draw this line
    at the ``kind`` level (blanket-excluding only
    ``cell_code_put``/``cell_code_delete`` and then LIKE-scanning every OTHER
    kind's ``payload_json`` in full) — that missed exactly this case:
    ``row_delete``'s own payload embeds BOTH the row's ``cells`` (genuine
    content) AND its ``code`` array (code attachments) side by side, so a
    code-only reference inside a DELETED row's payload kept its asset alive
    forever. This function instead dispatches on ``kind`` and pulls out only
    the fields that are actually ``content_md``-shaped; a ``code``/``code_text``
    field is NEVER read by any branch below, whichever kind it rides along in.

    Kinds with no content_md-shaped field at all (``column_add``,
    ``column_rename``, ``column_kind``, ``anchor_set``, ``table_meta``,
    ``cell_code_put``, ``cell_code_delete``) fall through every branch below
    and correctly yield ``[]`` — column/table names and roles are never scanned
    (asset refs only ever live in rendered cell markdown, never a column name
    or table title, and the pre-Task-13 implementation never scanned those
    either). Adding a new ``record_change`` ``kind`` that DOES carry cell
    content requires adding it here too — there is no generic fallback, by
    design: guessing "unknown kind, treat the whole payload as content" would
    silently reintroduce the very code_text-leaks-through-unrelated-payload bug
    this function exists to close, the moment that new kind ALSO happens to
    carry a ``code``/``code_text`` field alongside its content."""
    # Failure on unknown kind catches runtime errors from future changes that
    # add a new kind carrying cell content but forget to register it, which
    # would silently leak asset references and cause images to be incorrectly
    # garbage-collected. See ``REGISTERED_KINDS`` above.
    if kind not in REGISTERED_KINDS:
        raise ValueError(f"content_strings_in_payload: 未登记的 kind={kind!r}")

    texts: list = []

    if kind in ("cell_update", "revert"):
        for cell in payload.get("cells", []) or []:
            for side in ("before", "after"):
                value = cell.get(side)
                if value is not None:
                    texts.append(value)

    if kind in ("row_add", "import_append", "row_delete", "table_create"):
        texts.extend(_rows_content(payload.get("rows")))

    if kind == "column_delete":
        texts.extend(_column_cells_content(payload.get("cells")))

    if kind == "revert":
        texts.extend(_rows_content(payload.get("rows_added")))
        texts.extend(_rows_content(payload.get("rows_removed")))
        for entry in payload.get("columns_added", []) or []:
            texts.extend(_column_cells_content(entry.get("cells")))
        for entry in payload.get("columns_removed", []) or []:
            texts.extend(_column_cells_content(entry.get("cells")))
        # payload.get("code") — revert's OWN top-level code-attachment
        # before/after list (mirrors cell_code_put/delete's shape) — is
        # deliberately never consulted, same reason as every branch above.

    return texts


__all__ = [
    "rendered_asset_ids",
    "required_asset_ids",
    "asset_ref_tokens",
    "collect_change_payload_tokens",
    "keepers_among",
    "content_strings_in_payload",
    "REGISTERED_KINDS",
]
