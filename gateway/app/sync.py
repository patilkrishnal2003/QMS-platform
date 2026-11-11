"""Explicit, user-triggered sync: mock-eqms (read-only) -> knowledge index.
Knowledge and mock-eqms cannot call each other (closed loop), so the gateway moves the data."""
from . import allowlist


class SyncError(Exception):
    pass


def sync_eqms_records(actor: str, get=None, post=None) -> dict:
    get = get or allowlist.service_get
    post = post or allowlist.service_post
    r = get("mock-eqms", "/read/records", {"limit": 1000})
    if r.status_code != 200:
        raise SyncError(f"mock-eqms read failed: HTTP {r.status_code}")
    records = r.json()
    # Defence in depth: the read side never returns templates, but never forward one if it did.
    records = [x for x in records if x.get("record_kind") == "record"]
    p = post("knowledge", "/internal/index-records", {"records": records, "source": "mock-eqms", "synced_by": actor})
    if p.status_code != 200:
        raise SyncError(f"knowledge index failed: HTTP {p.status_code} {p.text[:200]}")
    return {"read": len(records), **p.json()}
