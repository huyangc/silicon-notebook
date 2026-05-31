"""QiefenDocument -> gold-ordered YAML."""
from __future__ import annotations

import yaml

from app.services.qiefen.models import QiefenDocument


def to_yaml(doc: QiefenDocument) -> str:
    return yaml.safe_dump(doc.to_pred_dict(), sort_keys=False, allow_unicode=True,
                          width=1000)
