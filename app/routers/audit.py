"""Audit log, read side. Owner only. (Rows are written by app/utils/audit.py.)"""
import json

from fastapi import APIRouter, Depends, HTTPException, status

from app.db import get_pool
from app.security import require_role
from app.utils.audit import ACTION_TYPES, ENTITY_TYPES, PLATFORM_ADMIN_ROLE, AuditFilters, fetch_logs
from app.utils.pagination import Page, paged_response
from app.utils.rows import row_to_dict

router = APIRouter(prefix="/audit-logs", tags=["audit"], dependencies=[Depends(require_role("OWNER"))])


@router.get("")
async def list_audit_logs(filters: AuditFilters = Depends(), page: Page = Depends()):
    """Who did what, newest first (paged; X-Total-Count = matching rows). Filters: `user_id`, `entity_type`
    (INVOICE, CUSTOMER, ...), `entity_id` (with entity_type = the history of one record), `action`
    (CREATE, UPDATE, CANCEL, LOGIN, ...), `from` / `to` (YYYY-MM-DD, store's local day), `search` (summary or
    person's name). Rows have no `details` -- GET /audit-logs/{id} has those. The platform admin's own
    activity never appears here."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await fetch_logs(conn, scope="business", filters=filters, limit=page.limit, offset=page.offset)
    return paged_response(rows)


@router.get("/meta")
async def audit_meta():
    """The values `action` and `entity_type` can take, for filter drop-downs."""
    return {"actions": ACTION_TYPES, "entity_types": ENTITY_TYPES}


@router.get("/{log_id}")
async def get_audit_log(log_id: int):
    """One entry with its `details` (the sanitized request, or the before/after of a profile edit)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM audit_logs WHERE id = $1 AND user_role IS DISTINCT FROM $2", log_id, PLATFORM_ADMIN_ROLE
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    data = row_to_dict(row)
    if isinstance(data.get("details"), str):
        data["details"] = json.loads(data["details"])
    return data
