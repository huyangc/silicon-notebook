"""Deployment-configurable settings for the arXiv sample plugin.

Core computes the accepted key set from ``model_fields`` itself, so
``extra="forbid"`` here is a second lock on the same door rather than the door.
It is kept because this model is also usable standalone (a plugin author can
validate a candidate TOML table without the backend present).

Two absences are deliberate:

* **No ``api_key_env``.**  The arXiv API takes no credential.  Inventing an
  unused key field would make the sample teach a shape it never exercises; the
  credential convention lives in the deployment-extensions SOP instead, and an
  in-house variant is where it actually gets used.
* **No client/connection object.**  ``configure`` may only store values — it
  runs inside startup composition, before the registry freezes and before the
  service is ready, so it must not start a thread or open a connection.

``politeness_interval_seconds`` defaults to 3.0 because arXiv's API terms ask
callers to leave at least three seconds between requests.  Lowering it is a
deployment's own decision against its own agreement with arXiv; ``0`` is
accepted so tests and mirrors are not forced to sleep.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ArxivSearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://export.arxiv.org/api/query"
    max_results: int = Field(10, ge=1, le=20)
    timeout_seconds: float = Field(10.0, gt=0, le=60)
    politeness_interval_seconds: float = Field(3.0, ge=0, le=30)
    user_agent: str = (
        "silicon-notebook-arxiv-sample/0.1 (+https://arxiv.org/help/api)"
    )
    # Installing the plugin is not the same as agreeing to send question-derived
    # keywords to arxiv.org on every thin answer.  Gap consultation stays off
    # until a deployment says otherwise.
    consult_enabled: bool = False
    consult_max_suggestions: int = Field(3, ge=1, le=5)
