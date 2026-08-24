"""arXiv literature search — a sample Silicon Notebook deployment extension.

The package is deliberately layered so the two halves that a real in-house
variant would replace stay separable:

* ``atom`` and ``client`` know arXiv and nothing else — no ``app.*`` import, no
  settings model, no extension SDK.  Swapping in a different upstream (an
  internal IEEE mirror, say) means rewriting these two and nothing more.
* ``settings``, ``routes``, ``consult`` and ``bundle`` are the Silicon Notebook
  adapter: they map deployment settings onto the client and expose the result
  through the SDK's contribution points.

Importing this package is side-effect free.
"""
from __future__ import annotations

__version__ = "0.1.0"

# `BUNDLE` — the object a deployment's `extensions.toml` points at — lives in
# `bundle.py` and is deliberately NOT re-exported here.  The config entry names
# `silicon_notebook_arxiv_search.bundle:BUNDLE` for that reason.
#
# Re-exporting it would mean that `import silicon_notebook_arxiv_search` — the
# harmless-looking thing a packaging check or a version probe does — pulls in
# FastAPI and the whole extension SDK behind it, because `bundle` imports
# `routes` which imports both.  Keeping this module free of them means the
# package can be imported for its version, and its settings model validated,
# without a backend present at all.
__all__ = ["__version__"]
