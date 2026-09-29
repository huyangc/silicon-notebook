"""The ONE digest that says "this is the text a citation rested on".

Every retrieval-time evidence snapshot and every terminal re-read of a global
Ask compares two of these, so there must be exactly one definition on the
Python side. Three places hash element or passage text in-process -- the
SQLite ``evidence_fingerprints`` / ``passage_evidence_snapshot`` twins and
``chunk_federation._text_sha`` -- plus ``services.evidence_attestation``, which
hashes the text a producer read itself. All of them call this function.

PostgreSQL keeps hashing IN SQL (``encode(sha256(convert_to(text,'UTF8')),
'hex')``), so element bodies never cross the wire there. The two sides agree
because both hash the UTF-8 encoding of the stored text with no normalization
of any kind: no Unicode normalization (a decomposed ``é`` and a precomposed one
are different evidence), no newline folding (CRLF stays CRLF), no trimming.
``convert_to(..., 'UTF8')`` converts from the database encoding, so the digest
does not depend on the server encoding either. ``test_evidence_fingerprint_twin``
pins the two spellings against each other on both backends.

Pure leaf: imports nothing from ``app``.
"""
from __future__ import annotations

import hashlib


def element_text_sha(text: str | None) -> str:
    """Hex SHA-256 of ``text`` encoded as UTF-8; ``None`` hashes as ``""``.

    ``None`` is folded into the empty string for the passage side, where a
    hit's text may legitimately be absent; ``source_elements.text`` is
    ``NOT NULL`` on both backends, so the fold never changes an element print.
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
