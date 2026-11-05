"""Identity of exactly the public content a reader reviewed before saving."""
import hashlib
import json


def public_share_revision(payload: dict) -> str:
    content = {key: value for key, value in payload.items() if key != "snapshot_revision"}
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
