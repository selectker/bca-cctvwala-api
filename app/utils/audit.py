"""Audit trail: who did what, when.

Two ways rows get written, both best-effort (a failure here is logged and swallowed -- it must never
turn a successful business operation into an error):

* `AuditRoute` -- set as `route_class` on a router and every successful POST / PUT / PATCH / DELETE on
  it is recorded automatically (actor from the auth token, action / entity from the route, a readable
  summary, and the sanitized JSON request body). New write endpoints on such a router are covered
  without touching them.
* `log_event(...)` -- for events that aren't a plain successful write by a signed-in user: logins
  (failed ones only against a real account, and only the first of a streak and the one that locks the address --
  the counting itself lives in the prunable login_failures table), the two bootstrap steps, profile edits with before/after values, and the
  platform admin's backup / restore.

Rows are append-only (a DB trigger blocks UPDATE / DELETE, see db/migrations/011_audit_logs.sql).
Passwords, PINs and tokens are never stored: request bodies are redacted before they're written, and
response bodies are only ever used to pick out an id and a display name.
"""
import json
import logging
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response, status
from fastapi.routing import APIRoute

from app.utils.search import SearchTerm
from app.config import settings
from app.db import get_pool
from app.utils.dates import parse_date

log = logging.getLogger("audit")

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PLATFORM_ADMIN_ROLE = "ADMIN"  # users.role of the platform-admin account (see /auth/bootstrap-admin)

_MAX_REQUEST_BODY = 256 * 1024     # larger JSON bodies aren't captured (the action is still logged)
_MAX_RESPONSE_BODY = 1024 * 1024   # larger responses aren't parsed for an id / display name
_MAX_DETAILS_BYTES = 8 * 1024      # a bigger `details` is replaced by a stub that lists its keys

# ----------------------------------------------------------------------------- vocabulary
# Kept in one place so GET /audit-logs/meta can offer filter lists without scanning the table.
ENTITY_TYPES = [
    "AMC_CONTRACT", "AMC_VISIT", "CATEGORY", "CUSTOMER", "DATABASE", "EXPENSE", "INVOICE", "PRODUCT", "PURCHASE",
    "QUOTATION", "SERVICE_TICKET", "STORE_PROFILE", "SUPPLIER", "USER", "WARRANTY",
]

# action -> (summary text, whether the entity noun + display name follows it)
_ACTIONS: dict[str, tuple[str, bool]] = {
    "CREATE": ("Created", True),
    "CREATE_FROM_QUOTATION": ("Created from a quotation:", True),
    "UPDATE": ("Updated", True),
    "DELETE": ("Deleted", True),
    "CANCEL": ("Cancelled", True),
    "PAYMENT": ("Recorded a payment on", True),
    "UPDATE_PAYMENT": ("Corrected a payment on", True),
    "REVERSE_PAYMENT": ("Reversed a payment on", True),
    "UPDATE_DUE_DATE": ("Changed the due date of", True),
    "STATUS_CHANGE": ("Changed the status of", True),
    "STOCK_ADJUST": ("Adjusted stock of", True),
    "CLOSE": ("Closed", True),
    "RENEW": ("Renewed", True),
    "ATTEND": ("Marked attended:", True),
    "RESCHEDULE": ("Rescheduled", True),
    "REOPEN": ("Sent back to the technician:", True),
    "ADDENDUM": ("Added photos / notes to", True),
    "SELL": ("Sold", True),
    "CLAIM": ("Recorded a claim on", True),
    "REPLACE": ("Replaced", True),
    "RESET_PIN": ("Reset the PIN of", True),
    "RESET_PASSWORD": ("Reset the password of", True),
    "CHANGE_PIN": ("Changed own PIN", False),
    "CHANGE_PASSWORD": ("Changed own password", False),
    "UPDATE_PROFILE": ("Updated own profile", False),
    "UPDATE_PROFILE_IMAGE": ("Changed own profile image", False),
    "UPDATE_LOGO": ("Changed the shop logo", False),
    "UPDATE_SIGNATURE": ("Changed the invoice signature", False),
    "LOGIN": ("Signed in", False),
    "LOGOUT": ("Signed out", False),
    "LOGOUT_ALL": ("Signed out of every device", False),
    "REVOKE_SESSIONS": ("Signed out every device of", True),
    "LOGIN_FAILED": ("Failed sign-in attempt", False),
    "BOOTSTRAP": ("Created the first account:", True),
    "BACKUP": ("Downloaded a database backup", False),
    "BACKUP_FAILED": ("The scheduled database backup failed", False),
    "RESTORE": ("Restored the database from a backup", False),
    "RESTORE_SNAPSHOT": ("Restored the database from a safety snapshot", False),
}
ACTION_TYPES = sorted(_ACTIONS)

# last static path segment -> action (anything else falls back to the HTTP method)
_PATH_VERBS = {
    "cancel": "CANCEL", "payments": "PAYMENT", "reverse": "REVERSE_PAYMENT", "due-date": "UPDATE_DUE_DATE", "status": "STATUS_CHANGE", "adjust-stock": "STOCK_ADJUST",
    "close": "CLOSE", "renew": "RENEW", "attend": "ATTEND", "reschedule": "RESCHEDULE", "reopen": "REOPEN", "addendum": "ADDENDUM", "sell": "SELL", "claim": "CLAIM",
    "replace": "REPLACE", "reset-pin": "RESET_PIN", "reset-password": "RESET_PASSWORD",
    "change-pin": "CHANGE_PIN", "from-quotation": "CREATE_FROM_QUOTATION",
    "logout": "LOGOUT", "logout-all": "LOGOUT_ALL", "revoke-sessions": "REVOKE_SESSIONS",
    "logo": "UPDATE_LOGO",
    "signature": "UPDATE_SIGNATURE",
}
_METHOD_ACTIONS = {"POST": "CREATE", "PUT": "UPDATE", "PATCH": "UPDATE", "DELETE": "DELETE"}

# first path segment (after /api or /api/v1) -> entity type
_PATH_ENTITIES = {
    "customers": "CUSTOMER", "suppliers": "SUPPLIER", "products": "PRODUCT", "purchases": "PURCHASE",
    "quotations": "QUOTATION", "invoices": "INVOICE", "amc": "AMC_CONTRACT", "service-tickets": "SERVICE_TICKET",
    "expenses": "EXPENSE", "warranties": "WARRANTY", "store-profile": "STORE_PROFILE", "auth": "USER", "profile": "USER",
}

# keys looked at (in order) in a response body to give the summary a human name
_DISPLAY_KEYS = ("invoice_no", "purchase_no", "quotation_no", "ticket_no", "contract_no", "barcode", "sku", "name", "store_name")

# Where to look a record's name up when the response doesn't carry one (e.g. stock adjustments), and
# *before* a DELETE runs, because the response to a delete is empty.
_NAME_LOOKUP = {
    "CUSTOMER": ("customers", "name"), "SUPPLIER": ("suppliers", "name"), "PRODUCT": ("products", "name"),
    "INVOICE": ("invoices", "invoice_no"),
    "WARRANTY": ("part_warranties", "barcode"), "USER": ("users", "name"),
}

_REDACT_KEYS = {"pin", "new_pin", "current_pin", "token", "access_token", "authorization", "secret", "signature_base64"}
_MASK_KEYS = {"bank_account_no", "account_no"}


# ----------------------------------------------------------------------------- sanitising
def sanitize(value: Any) -> Any:
    """Copy of a JSON-ish value that is safe to store: secrets are replaced by '***', account numbers
    keep only their last 4 characters."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k).lower()
            if key in _REDACT_KEYS or "password" in key:
                out[k] = "***"
            elif key in _MASK_KEYS and isinstance(v, str) and len(v) > 4:
                out[k] = "****" + v[-4:]
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    return value


def _cap_details(details: dict | None) -> str | None:
    """JSON text for the `details` column, or a small stub when it would be too big."""
    if not details:
        return None
    text = json.dumps(details, default=str)
    if len(text.encode("utf-8")) > _MAX_DETAILS_BYTES:
        text = json.dumps({"truncated": True, "keys": sorted(details)[:30]})
    return text


def client_ip(request: Request | None) -> str | None:
    # app/client_ip.py has already replaced the proxy's address with the real client's (TRUSTED_PROXIES).
    if request is not None and request.client:
        return request.client.host
    return None


def summarize(action: str, entity_type: str, display: str | None) -> str:
    text, with_object = _ACTIONS.get(action, (action.replace("_", " ").capitalize(), True))
    if not with_object:
        return text
    noun = entity_type.replace("_", " ").lower()
    return f"{text} {noun} {display}" if display else f"{text} {noun}"


# ----------------------------------------------------------------------------- writing
async def log_event(
    user: dict | None,
    action: str,
    entity_type: str,
    entity_id: Any = None,
    summary: str | None = None,
    details: dict | None = None,
    request: Request | None = None,
    display: str | None = None,
) -> None:
    """Record one event. `user` is {id, name, role} (None for an unknown actor). Never raises."""
    try:
        text = summary or summarize(action, entity_type, display)
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO audit_logs (user_id, user_name, user_role, action, entity_type, entity_id,
                                           summary, details, ip_address)
                   VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)""",
                str(user["id"]) if user and user.get("id") else None,
                (user or {}).get("name"),
                (user or {}).get("role"),
                action,
                entity_type,
                str(entity_id) if entity_id is not None else None,
                text,
                _cap_details(details),
                client_ip(request),
            )
    except Exception:
        log.exception("could not write audit log (%s %s)", action, entity_type)


# ----------------------------------------------------------------------------- automatic logging
def _classify(method: str, template: str) -> tuple[str, str]:
    """(entity_type, action) for a write route, from its path template (e.g. /api/v1/invoices/{invoice_id}/cancel)."""
    segs = [s for s in template.split("/") if s]
    if segs and segs[0] == "api":
        segs = segs[1:]
        if segs and segs[0] == "v1":  # /api/v1/... (and the legacy /api/... alias)
            segs = segs[1:]
    first = segs[0] if segs else "unknown"
    static = [s for s in segs[1:] if not s.startswith("{")]
    entity = _PATH_ENTITIES.get(first, first.upper().replace("-", "_"))

    if first == "products" and static[:2] == ["meta", "categories"]:
        entity, static = "CATEGORY", static[2:]
    elif first == "amc" and static[:1] == ["visits"]:
        entity, static = "AMC_VISIT", static[1:]
    elif first == "amc" and static == ["tickets"]:  # a ticket raised under a contract is a new SERVICE_TICKET
        entity, static = "SERVICE_TICKET", []
    elif first == "auth":
        static = [s for s in static if s != "users"]

    action = _PATH_VERBS.get(static[-1]) if static else None
    if method == "PUT" and static and static[-1] == "payments":  # PUT .../payments/{id} edits one, POST .../payments adds one
        action = "UPDATE_PAYMENT"
    return entity, action or _METHOD_ACTIONS.get(method, method)


def _response_json(response: Response) -> dict:
    """The response body as a dict, or {} (only used to read an id and a display name)."""
    try:
        body = getattr(response, "body", None)
        if body and len(body) <= _MAX_RESPONSE_BODY and "json" in (response.media_type or ""):
            data = json.loads(body)
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


async def _request_json(request: Request) -> Any:
    """The request's JSON body (already read by FastAPI, so this is the cached copy), or None."""
    try:
        if "application/json" not in request.headers.get("content-type", ""):
            return None  # multipart photo uploads, raw restore files, ...
        declared = request.headers.get("content-length")
        if declared is None or int(declared) > _MAX_REQUEST_BODY:
            return None
        raw = await request.body()
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _lookup_name(entity: str, entity_id: Any) -> str | None:
    lookup = _NAME_LOOKUP.get(entity)
    if not lookup or entity_id is None:
        return None
    table, column = lookup
    try:
        async with get_pool().acquire() as conn:
            value = await conn.fetchval(f"SELECT {column} FROM {table} WHERE id = $1", entity_id)
        return str(value) if value else None
    except Exception:
        return None  # e.g. the id isn't a valid UUID -- the summary just falls back to the id


class AuditRoute(APIRoute):
    """APIRoute that records every successful write request in the audit log."""

    def get_route_handler(self):
        original = super().get_route_handler()
        template = self.path

        async def handler(request: Request) -> Response:
            if request.method not in MUTATING_METHODS:
                return await original(request)
            label = None
            if request.method == "DELETE":
                path_ids = list(request.path_params.values())
                label = await _lookup_name(_classify(request.method, template)[0], path_ids[0] if path_ids else None)

            response = await original(request)  # raises on failure -> nothing is logged

            if response.status_code < 400:
                try:
                    await self._record(request, response, template, label)
                except Exception:
                    log.exception("could not write audit log (%s %s)", request.method, template)
            return response

        return handler

    @staticmethod
    async def _record(request: Request, response: Response, template: str, label: str | None) -> None:
        user = getattr(request.state, "user", None)
        if not user:
            return  # unauthenticated endpoint (login / bootstrap): those log themselves, with more context

        entity, action = _classify(request.method, template)
        body = _response_json(response)

        display = label
        if display is None:
            display = next((str(body[k]) for k in _DISPLAY_KEYS if body.get(k)), None)

        path_ids = list(request.path_params.values())
        if action in ("CREATE", "CREATE_FROM_QUOTATION"):
            entity_id = body.get("id")
        elif path_ids:
            entity_id = path_ids[0]
        else:
            entity_id = body.get("id") or (user["id"] if entity == "USER" else None)

        if display is None:
            display = await _lookup_name(entity, entity_id)

        details: dict[str, Any] = {"endpoint": f"{request.method} {template}"}
        sent = await _request_json(request)
        if sent:
            details["request"] = sanitize(sent)

        await log_event(
            user, action, entity, entity_id, details=details, request=request,
            display=display or (str(entity_id) if entity_id else None),
        )


# ----------------------------------------------------------------------------- reading
class AuditFilters:
    """Query-string filters shared by GET /audit-logs, GET /admin/audit-logs and GET /profile/activity."""

    def __init__(
        self,
        user_id: str | None = Query(default=None, max_length=100),
        entity_type: str | None = Query(default=None, max_length=100),
        entity_id: str | None = Query(default=None, max_length=100),
        action: str | None = Query(default=None, max_length=100),
        from_: str | None = Query(default=None, alias="from", description="YYYY-MM-DD, store's local day"),
        to: str | None = Query(default=None, description="YYYY-MM-DD, store's local day (inclusive)"),
        search: SearchTerm = None,
    ):
        if user_id:
            try:
                UUID(user_id)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="user_id must be a user ID")
        self.user_id = user_id or None
        self.entity_type = entity_type.strip().upper() if entity_type and entity_type.strip() else None
        self.entity_id = entity_id.strip() if entity_id and entity_id.strip() else None
        self.action = action.strip().upper() if action and action.strip() else None
        self.date_from: date | None = parse_date(from_)
        self.date_to: date | None = parse_date(to)
        self.search = search.strip() if search and search.strip() else None


_LIST_COLS = "id, created_at, user_id, user_name, user_role, action, entity_type, entity_id, summary, ip_address"


async def fetch_logs(conn, *, scope: str, filters: AuditFilters, limit: int, offset: int):
    """Newest-first page of audit rows (no `details`; fetch one row for those).

    scope "business": everything except the platform admin's own activity -- the platform admin's
                      identity is kept out of every business-facing screen, and that includes this one.
    scope "admin":    only the platform admin's activity (sign-ins, backups, restores).
    scope "all":      no role restriction (used with a user_id filter for "my activity").
    """
    where: list[str] = []
    args: list[Any] = []

    def add(condition: str, *values: Any) -> None:
        # `condition` uses {0}, {1}, ... for the values' parameter numbers
        start = len(args) + 1
        args.extend(values)
        where.append(condition.format(*range(start, start + len(values))))

    if scope == "business":
        where.append(f"user_role IS DISTINCT FROM '{PLATFORM_ADMIN_ROLE}'")
    elif scope == "admin":
        where.append(f"user_role = '{PLATFORM_ADMIN_ROLE}'")
    elif scope != "all":
        raise ValueError(f"unknown audit scope {scope!r}")

    if filters.user_id:
        add("user_id = ${0}::uuid", filters.user_id)
    if filters.entity_type:
        add("entity_type = ${0}", filters.entity_type)
    if filters.entity_id:
        add("entity_id = ${0}", filters.entity_id)
    if filters.action:
        add("action = ${0}", filters.action)
    if filters.date_from:
        add("created_at >= ${0}::date::timestamp AT TIME ZONE ${1}::text", filters.date_from, settings.app_timezone)
    if filters.date_to:
        add("created_at < (${0}::date + 1)::timestamp AT TIME ZONE ${1}::text", filters.date_to, settings.app_timezone)
    if filters.search:
        add("(summary ILIKE '%'||${0}||'%' ESCAPE '\\' OR user_name ILIKE '%'||${0}||'%' ESCAPE '\\')", filters.search)

    args.extend([limit, offset])
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return await conn.fetch(
        f"""SELECT {_LIST_COLS}, COUNT(*) OVER() AS _total FROM audit_logs {clause}
            ORDER BY created_at DESC, id DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}""",
        *args,
    )
