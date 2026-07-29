"""Build and upsert user-activity rows from source callbacks.

Each finance-module callback is one activity (create/update/delete). Rows are
keyed on `callback_id` so re-runs and backfills are idempotent.
"""
from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from models.finance import ActivityLog
from transforms.timeutil import to_wib


# Map the callback body method to a friendly activity type.
_ACTION_TO_TYPE = {
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}

ACTIVITY_COLUMNS = [
    "created_date",
    "created_by",
    "created_by_email",
    "activity_type",
    "action",
    "module",
    "endpoint",
    "ref_no",
    "source_id",
    "status",
    "doc_date",
    "note",
    "callback_id",
    "callback_at",
]

# Everything except the business key is refreshed if the same callback is
# re-processed (e.g. a corrected parse).
_UPDATE_COLUMNS = [c for c in ACTIVITY_COLUMNS if c != "callback_id"]


def build_activity_row(rec: Dict[str, Any], payload: Dict[str, Any], module_name: str) -> Dict[str, Any]:
    """Build one activity row from a source record + its parsed payload."""
    data = payload.get("data") or {}
    created = data.get("created") or {}
    updated = data.get("updated") or {}
    # The action's performer is the last person who touched the document
    # (`updated`); for a fresh create that equals `created`.
    actor = updated.get("user") or created.get("user") or {}
    action = (payload.get("method") or "POST").upper()
    action_time = updated.get("time") or created.get("time")

    return {
        "created_date": to_wib(action_time),
        "created_by": actor.get("name"),
        "created_by_email": actor.get("email"),
        "activity_type": _ACTION_TO_TYPE.get(action, "update"),
        "action": action,
        "module": module_name,
        "endpoint": payload.get("end_point"),
        "ref_no": data.get("number"),
        "source_id": data.get("id"),
        "status": data.get("status"),
        "doc_date": data.get("date"),
        "note": data.get("description"),
        "callback_id": str(rec.get("callback_id")) if rec.get("callback_id") is not None else None,
        "callback_at": to_wib(rec.get("created_at")),
    }


def _chunked(items: List[Dict[str, Any]], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def upsert_activity_rows(session: Session, rows: List[Dict[str, Any]], chunk_size: int = 500) -> int:
    """Idempotently upsert activity rows keyed on callback_id.

    Rows without a callback_id are skipped (no idempotency key). Duplicate
    callback_ids within the batch are collapsed to the last occurrence so a
    single INSERT does not hit "row twice in one statement".
    """
    by_key: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cid = row.get("callback_id")
        if not cid:
            continue
        clean = {col: row.get(col) for col in ACTIVITY_COLUMNS}
        by_key[cid] = clean

    prepared = list(by_key.values())
    if not prepared:
        return 0

    processed = 0
    for batch in _chunked(prepared, chunk_size):
        stmt = insert(ActivityLog).values(batch)
        stmt = stmt.on_duplicate_key_update(
            **{col: getattr(stmt.inserted, col) for col in _UPDATE_COLUMNS}
        )
        session.execute(stmt)
        processed += len(batch)

    return processed
