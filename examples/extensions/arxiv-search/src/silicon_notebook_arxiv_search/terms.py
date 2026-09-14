"""Latin search terms out of free text — the query side of the arXiv half.

The mirror image of :mod:`.atom`: that module turns arXiv's bytes into records,
this one turns free text into the handful of keywords arXiv can actually be
asked about.  Both are pure — no network, no filesystem, no clock, no ``app.*``
import — and both are what an in-house variant replaces wholesale when it
points at a different upstream.

It lives in its own module rather than in either contributor because **two**
contributions now need it and they must not disagree: :mod:`.consult` scans a
question plus its gap phrases, :mod:`.reflect_search` scans the one argument
the model wrote.  A second hand-written copy of the same scan would drift the
day one of them learned about a new identifier shape — and the two would then
send arXiv different queries for the same words, which is exactly the class of
bug this package's ``search_kwargs`` comment warns about one layer up.

The bound on how many terms come back is :data:`.client.MAX_QUERY_TERMS`,
imported rather than restated: it is the transport's own ceiling, and a caller
that extracted more would only have them dropped inside ``build_query_url``.
"""
from __future__ import annotations

import re

from .client import MAX_QUERY_TERMS

# A Latin word of two or more characters, allowing the punctuation that shows
# up inside real identifiers (``GPT-4``, ``C++``, ``e.g``).
_LATIN_TERM = re.compile(r"[A-Za-z][A-Za-z0-9+.#-]+")
_TERM_EDGE = ".-+#"

# English function words carry no retrieval signal and would crowd out the
# terms that do, since the query is capped at ``MAX_QUERY_TERMS``.  Kept small
# and plugin-private on purpose: this is a keyword extractor for one upstream,
# not a linguistics contribution.
_STOPWORDS = frozenset(
    {
        "about", "after", "all", "also", "and", "any", "are", "based", "been",
        "before", "being", "between", "both", "but", "can", "could", "did",
        "does", "doing", "done", "each", "even", "for", "from", "had", "has",
        "have", "how", "into", "its", "just", "may", "might", "more", "most",
        "much", "must", "not", "now", "one", "only", "other", "our", "out",
        "over", "same", "should", "since", "some", "such", "than", "that",
        "the", "their", "them", "then", "there", "these", "they", "this",
        "those", "through", "under", "use", "used", "using", "very", "was",
        "were", "what", "when", "where", "which", "while", "who", "why",
        "will", "with", "would", "you", "your",
    }
)


def latin_terms(*texts: object) -> tuple[str, ...]:
    """De-duplicated, lower-cased Latin terms from ``texts``, in reading order.

    Every argument is scanned in turn and the results share one seen-set, so a
    caller may pass several strings that overlap without getting the same term
    twice.  An argument that is not a string is skipped rather than refused:
    callers here receive their text from core or from a model, and a
    non-string is one more empty source, not a reason to fail a live question.

    Empty output is meaningful and both callers treat it as such: arXiv is a
    Latin-keyword index, so "no terms" means a request would be guaranteed to
    return nothing and is therefore not worth a politeness slot.
    """

    seen: set[str] = set()
    terms: list[str] = []
    for text in texts:
        if not isinstance(text, str):
            continue
        for match in _LATIN_TERM.finditer(text):
            term = match.group(0).strip(_TERM_EDGE).lower()
            if len(term) < 2 or term in _STOPWORDS or term in seen:
                continue
            seen.add(term)
            terms.append(term)
            if len(terms) >= MAX_QUERY_TERMS:
                return tuple(terms)
    return tuple(terms)
