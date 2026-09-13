"""Circuit-diagram element enrichment — a sample deployment extension.

The package is layered the same way the arXiv sample is, and for the same
reason: the half that knows the upstream vendor and the half that knows
Silicon Notebook must be separable, because an in-house variant replaces only
one of them.

* :mod:`.client` knows one OpenAI-compatible vision endpoint and nothing else
  — no ``app.*`` import, no settings model.  Pointing this plugin at a
  different model provider means rewriting that module and nothing more.
* :mod:`.settings`, :mod:`.enricher` and :mod:`.bundle` are the adapter: they
  map deployment settings onto the transport and expose the result through the
  SDK's ``source.element_enricher`` point.

Importing this package is side-effect free, and deliberately does not pull in
the extension SDK: ``BUNDLE`` lives in :mod:`.bundle` and is **not**
re-exported here, so a packaging check or a version probe can import this
package without a backend present.  A deployment's ``EXTENSIONS_CONFIG`` names
``silicon_notebook_circuit_diagram.bundle:BUNDLE`` for that reason.
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
