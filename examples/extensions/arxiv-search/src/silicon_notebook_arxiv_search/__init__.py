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

# `BUNDLE` — the object a deployment's `extensions.toml` points at — is added
# here alongside `bundle.py`.  It is deliberately absent until then rather than
# imported optimistically: a module-level import of a file that does not exist
# would break `import silicon_notebook_arxiv_search` outright.
__all__ = ["__version__"]
