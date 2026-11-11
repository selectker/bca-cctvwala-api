"""Service tickets.

Flow:  OWNER raises a ticket (in-warranty or paid)  ->  STAFF / TECHNICIAN marks it attended (with
completion photos) or reschedules it  ->  OWNER reads the timeline and closes it  ->  a PAID ticket can
be picked when the OWNER raises a SERVICE invoice (see invoices.py).

Three kinds of ticket (`service_type`): IN_WARRANTY (a sold unit still under the customer warranty), PAID (billed on a
SERVICE invoice) and AMC (covered by an ACTIVE AMC contract, which the ticket points at through `amc_id`; nothing to bill).

When the visit is done the technician can also capture the customer's sign-off (a signature and / or an acknowledgement
with the customer's name, or the reason there is none); it is printed on the job card. See `_prepare_signoff`.

Who can do what:
  * create / edit / close / cancel / see charges: OWNER only
  * attend / reschedule:                          STAFF and TECHNICIAN only (never the owner)
  * STAFF / TECHNICIAN only ever see tickets assigned to them or not assigned to anyone yet; acting on an
    unassigned ticket assigns it to them.
"""
import asyncio
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response

from app.utils.search import SearchTerm
from app.config import settings
from app.db import get_pool
from app.schemas.tickets import (
    CancelTicketRequest,
    CloseTicketRequest,
    CreateAmcTicketRequest,
    CreateTicketRequest,
    ReopenTicketRequest,
    RescheduleTicketRequest,
    UpdateTicketRequest,
)
from app.security import require_role
from app.utils.doc_number import next_doc_number
from app.utils.pagination import Page, paged_response
from app.utils.rows import row_to_dict, rows_to_list
from app.utils.audit import AuditRoute
from app.utils import image_gate, photo, storage
from app.utils import signature as signature_util
from app.utils.dates import today_local
from app.utils.logo import load_logo_bytes
from app.utils.pdf import build_ticket_pdf

ANY_TICKET_USER = require_role("OWNER", "STAFF", "TECHNICIAN")
OWNER_ONLY = require_role("OWNER")
FIELD_ONLY = require_role("STAFF", "TECHNICIAN")

router = APIRouter(route_class=AuditRoute, prefix="/service-tickets", tags=["service-tickets"], dependencies=[Depends(ANY_TICKET_USER)])

MAX_PHOTOS = 6
MAX_TICKET_PHOTOS = 20  # all photos on one ticket (the first visit plus later additions)
MAX_SIGNOFF_B64 = 700_000  # characters of base64 (about 500 KB of image): a signature pad drawing is a few KB, and a form field may not pass 1 MB
SERVICE_TYPES = ("IN_WARRANTY", "PAID", "AMC")
NO_CHARGE_TYPES = ("IN_WARRANTY", "AMC")  # nothing to bill: no charge amount is kept
# Commercial fields the field staff never get to see.
_OWNER_ONLY_FIELDS = ("charge_amount", "gst_rate", "invoice_id", "invoice_no")
_TYPE_LABEL = {"IN_WARRANTY": "In warranty", "PAID": "Paid service", "AMC": "AMC covered"}

_SELECT = """
    SELECT t.*, c.name AS customer_name, c.phone AS customer_phone,
           a.name AS assigned_to_name, cb.name AS created_by_name,
           w.barcode AS warranty_barcode, p.name AS warranty_product_name,
           w.customer_warranty_valid_until AS warranty_valid_until,
           i.invoice_no AS invoice_no, amc.contract_no AS amc_contract_no,
           EXISTS (SELECT 1 FROM service_ticket_signoffs so
                   WHERE so.ticket_id = t.id AND (so.acknowledged OR so.signature_key IS NOT NULL)
                     AND so.created_at >= COALESCE((SELECT MAX(e.created_at) FROM service_ticket_events e
                                                    WHERE e.ticket_id = t.id AND e.event_type = 'ATTENDED'),
                                                   '-infinity'::timestamptz)) AS customer_signed_off
    FROM service_tickets t
    JOIN customers c ON c.id = t.customer_id
    LEFT JOIN users a ON a.id = t.assigned_to
    LEFT JOIN users cb ON cb.id = t.created_by
    LEFT JOIN part_warranties w ON w.id = t.warranty_id
    LEFT JOIN products p ON p.id = w.product_id
    LEFT JOIN invoices i ON i.id = t.invoice_id
    LEFT JOIN amc_contracts amc ON amc.id = t.amc_id
"""

# The sign-off that counts for the ticket now: one recorded since the latest visit (a ticket sent back and attended again
# needs a new one; the earlier sign-off stays on the timeline). A real sign-off beats a "could not get one" note.
_CURRENT_SIGNOFF_SQL = """
    SELECT s.id, s.event_id, s.customer_name, s.acknowledged, (s.signature_key IS NOT NULL) AS has_signature,
           (s.acknowledged OR s.signature_key IS NOT NULL) AS obtained, s.not_obtained_reason,
           s.created_at AS signed_at, u.name AS captured_by_name, s.signature_key
    FROM service_ticket_signoffs s LEFT JOIN users u ON u.id = s.captured_by
    WHERE s.ticket_id = $1
      AND s.created_at >= COALESCE((SELECT MAX(e.created_at) FROM service_ticket_events e
                                    WHERE e.ticket_id = $1 AND e.event_type = 'ATTENDED'), '-infinity'::timestamptz)
    ORDER BY (s.acknowledged OR s.signature_key IS NOT NULL) DESC, s.created_at DESC, s.id
    LIMIT 1
"""


# ---------- helpers ----------
def _is_owner(user: dict) -> bool:
    return user["role"] == "OWNER"


def _uuid_or_404(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _bad(detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _present(row, user: dict) -> dict:
    data = row_to_dict(row)
    if not _is_owner(user):
        for key in _OWNER_ONLY_FIELDS:
            data.pop(key, None)
    return data


def _field_user_may_see(ticket, user: dict) -> bool:
    return _is_owner(user) or ticket["assigned_to"] is None or str(ticket["assigned_to"]) == user["id"]


async def _load_visible(conn, ticket_id: str, user: dict):
    row = await conn.fetchrow(f"{_SELECT} WHERE t.id = $1", ticket_id)
    # 404 (not 403) so a technician can't tell that someone else's ticket exists.
    if row is None or not _field_user_may_see(row, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row


async def _lock_ticket(conn, ticket_id: str, user: dict, *, for_update: bool = True):
    """The ticket row (404 if it doesn't exist or isn't visible). for_update=False reads it without taking the row lock."""
    row = await conn.fetchrow(
        "SELECT * FROM service_tickets WHERE id = $1" + (" FOR UPDATE" if for_update else ""), ticket_id
    )
    if row is None or not _field_user_may_see(row, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row


async def _add_event(conn, ticket_id, event_type: str, actor_id: str, note: str | None = None,
                     scheduled_date: date | None = None):
    return await conn.fetchval(
        """INSERT INTO service_ticket_events (ticket_id, event_type, note, scheduled_date, actor_id)
           VALUES ($1,$2,$3,$4,$5) RETURNING id""",
        ticket_id, event_type, note, scheduled_date, actor_id,
    )


def _money(value) -> Decimal | None:
    """Two-decimal Decimal so a float from the request compares equal to the NUMERIC from the database."""
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.01"))


def _check_not_past(value: date | None, what: str) -> None:
    if value is not None and value < date.today():
        raise _bad(f"{what} can't be in the past")


def _assert_warranty_valid(valid_until: date | None) -> None:
    if valid_until is None:
        raise _bad("No customer warranty is recorded for that unit — raise this as a Paid service")
    if valid_until < date.today():
        raise _bad(f"The warranty on that unit ended on {valid_until.isoformat()} — raise this as a Paid service")


async def _resolve_warranty(conn, barcode: str, customer_id: str, service_type: str) -> str:
    unit = await conn.fetchrow(
        """SELECT w.id, w.status, w.customer_warranty_valid_until, i.customer_id AS sold_to
           FROM part_warranties w LEFT JOIN invoices i ON i.id = w.invoice_id
           WHERE w.barcode = $1""",
        barcode.strip(),
    )
    if unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No tracked unit has that barcode")
    if unit["sold_to"] is None or str(unit["sold_to"]) != customer_id:
        raise _bad("That unit wasn't sold to this customer")
    if unit["status"] in ("REPLACED", "RETURNED", "SCRAPPED"):
        raise _bad(f"That unit is no longer with the customer (status: {unit['status']})")
    if service_type == "IN_WARRANTY":
        _assert_warranty_valid(unit["customer_warranty_valid_until"])
    return str(unit["id"])


async def _check_assignee(conn, user_id: str) -> str:
    row = await conn.fetchrow(
        """SELECT id, name FROM users
           WHERE id = $1::uuid AND is_active = TRUE AND is_platform_admin = FALSE
             AND role IN ('STAFF', 'TECHNICIAN')""",
        _uuid_or_400(user_id),
    )
    if row is None:
        raise _bad("That person doesn't exist, is deactivated, or isn't a staff / technician login")
    return row["name"]


def _uuid_or_400(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise _bad("Invalid id")


def _signoff_json(row) -> dict | None:
    """A sign-off row as the API shows it. The storage key never leaves the server; the image is fetched through
    GET /service-tickets/{id}/signoffs/{signoff_id}/signature."""
    if row is None:
        return None
    data = row_to_dict(row)
    data.pop("signature_key", None)
    return data


async def _current_signoff(conn, ticket_id: str) -> dict | None:
    return _signoff_json(await conn.fetchrow(_CURRENT_SIGNOFF_SQL, ticket_id))


@dataclass
class Signoff:
    """What the technician captured at the end of a visit (validated; the image is already a clean PNG)."""
    customer_name: str | None = None
    acknowledged: bool = False
    png: bytes | None = None
    not_obtained_reason: str | None = None

    @property
    def obtained(self) -> bool:
        return self.acknowledged or self.png is not None


async def _prepare_signoff(customer_name: str | None, customer_signature: str | None, acknowledged: bool,
                           no_signoff_reason: str | None, *, required: bool) -> Signoff | None:
    """Validates the sign-off fields of /attend and /addendum. Returns None when none was sent.

    * a signature (base64 text: raw or a data URL, as a signature pad / canvas gives it) and / or `customer_acknowledged`
      (the customer confirmed the work without drawing, e.g. read out an OTP or said so) counts as the sign-off, and then
      the customer's name is required;
    * otherwise `no_signoff_reason` records why there is none ("customer not at site");
    * a name on its own means nothing (the app may pre-fill it) and is ignored.
    With REQUIRE_CUSTOMER_SIGNOFF=true, /attend needs one of the two."""
    name = (customer_name or "").strip() or None
    reason = (no_signoff_reason or "").strip() or None
    sig_text = (customer_signature or "").strip()
    if sig_text or acknowledged:
        if reason:
            raise _bad("Send either the customer's sign-off or the reason there is none, not both")
        if not name:
            raise _bad("Enter the name of the customer who signed")
        png = None
        if sig_text:
            try:
                raw = await asyncio.to_thread(signature_util.decode_base64, sig_text)
                if len(raw) > settings.max_signature_mb * 1024 * 1024:
                    raise signature_util.SignatureError(f"The signature image must be under {settings.max_signature_mb} MB")
                png = await image_gate.run_limited(signature_util.normalise_signature, raw)
            except signature_util.SignatureError as exc:
                raise _bad(str(exc))
        return Signoff(customer_name=name, acknowledged=bool(acknowledged), png=png)
    if reason:
        return Signoff(not_obtained_reason=reason)
    if required:
        raise _bad("Ask the customer to sign (or acknowledge) the finished work, or say why they couldn't "
                   "(no_signoff_reason)")
    return None


async def _load_signoff_png(key: str | None) -> bytes | None:
    """The signature image for the job card. Best effort: a storage hiccup must never stop the card printing."""
    if not key:
        return None
    try:
        return await asyncio.to_thread(storage.get_bytes, key)
    except Exception:
        return None


# ---------- AMC cover ----------
async def _assert_amc_covers(conn, amc_id: str, customer_id: str) -> dict:
    """The AMC contract must exist, belong to this customer, be ACTIVE and be in its term today. Takes a share lock on the
    contract row, so it can't be cancelled in the middle of raising the ticket. Returns the contract row."""
    contract = await conn.fetchrow(
        "SELECT id, contract_no, customer_id, site_address, status, start_date, end_date FROM amc_contracts "
        "WHERE id = $1::uuid FOR SHARE", _uuid_or_400(amc_id),
    )
    if contract is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AMC contract not found")
    no = contract["contract_no"]
    if str(contract["customer_id"]) != str(customer_id):
        raise _bad(f"AMC contract {no} belongs to a different customer")
    if contract["status"] != "ACTIVE":
        raise _bad(f"AMC contract {no} is {contract['status'].lower()} — renew it, or raise this as a Paid service")
    today = today_local()
    if contract["end_date"] < today:
        raise _bad(f"AMC contract {no} ended on {contract['end_date'].isoformat()} — renew it, or raise this as a Paid service")
    if contract["start_date"] > today:
        raise _bad(f"AMC contract {no} only starts on {contract['start_date'].isoformat()}")
    return contract


# ---------- list / read ----------
@router.get("")
async def list_tickets(
    status_filter: str | None = Query(default=None, alias="status"),
    customer_id: str | None = None,
    service_type: str | None = None,
    amc_id: str | None = None,  # the tickets raised under one AMC contract
    search: SearchTerm = None,  # ticket no, customer name or title
    billable: bool = False,  # OWNER: attended / closed PAID tickets not yet on an invoice
    page: Page = Depends(),
    user=Depends(ANY_TICKET_USER),
):
    if billable and not _is_owner(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    if status_filter and status_filter not in ("OPEN", "RESCHEDULED", "ATTENDED", "CLOSED", "CANCELLED"):
        raise _bad("Unknown status")
    if service_type and service_type not in SERVICE_TYPES:
        raise _bad("Unknown service type")
    visible_to = None if _is_owner(user) else user["id"]
    pool = get_pool()
    async with pool.acquire() as conn:
        # Filter, sort and page on the bare table first (cheap), then build the heavy row (7 joins and the sign-off
        # subquery in _SELECT) only for the <= `limit` tickets of this page. COUNT(*) OVER() is evaluated before
        # LIMIT, so putting the heavy SELECT in the same block ran that subquery for EVERY matching ticket.
        rows = await conn.fetch(
            f"""SELECT t.*, pg._total FROM (
                    SELECT s.id, COUNT(*) OVER() AS _total
                    FROM service_tickets s JOIN customers c ON c.id = s.customer_id
                    WHERE ($1::uuid IS NULL OR s.assigned_to IS NULL OR s.assigned_to = $1)
                      AND ($2::text IS NULL OR s.status = $2)
                      AND ($3::uuid IS NULL OR s.customer_id = $3)
                      AND ($4::text IS NULL OR s.service_type = $4)
                      AND ($5::text IS NULL OR s.ticket_no ILIKE '%'||$5||'%' ESCAPE '\\' OR c.name ILIKE '%'||$5||'%' ESCAPE '\\'
                           OR s.title ILIKE '%'||$5||'%' ESCAPE '\\')
                      AND (NOT $6::boolean OR (s.service_type = 'PAID' AND s.status IN ('ATTENDED', 'CLOSED')
                                               AND s.invoice_id IS NULL))
                      AND ($7::uuid IS NULL OR s.amc_id = $7)
                    ORDER BY s.created_at DESC, s.id LIMIT $8 OFFSET $9
                ) pg
                JOIN ({_SELECT}) t ON t.id = pg.id
                ORDER BY t.created_at DESC, t.id""",
            visible_to, status_filter,
            _uuid_or_400(customer_id) if customer_id else None,
            service_type, search, billable,
            _uuid_or_400(amc_id) if amc_id else None, page.limit, page.offset,
        )
    return paged_response(rows, extra_drop=() if _is_owner(user) else _OWNER_ONLY_FIELDS)


@router.get("/{ticket_id}")
async def get_ticket(ticket_id: str, user=Depends(ANY_TICKET_USER)):
    ticket_id = _uuid_or_404(ticket_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        ticket = await _load_visible(conn, ticket_id, user)
        events = await conn.fetch(
            """SELECT e.id, e.event_type, e.note, e.scheduled_date, e.created_at, e.actor_id, u.name AS actor_name
               FROM service_ticket_events e LEFT JOIN users u ON u.id = e.actor_id
               WHERE e.ticket_id = $1 ORDER BY e.created_at, e.id""",
            ticket_id,
        )
        images = await conn.fetch(
            """SELECT id, event_id, content_type, size_bytes, created_at, storage_key FROM service_ticket_images
               WHERE ticket_id = $1 ORDER BY created_at, id""",
            ticket_id,
        )
        signoffs = await conn.fetch(
            """SELECT s.id, s.event_id, s.customer_name, s.acknowledged, (s.signature_key IS NOT NULL) AS has_signature,
                      (s.acknowledged OR s.signature_key IS NOT NULL) AS obtained, s.not_obtained_reason,
                      s.created_at AS signed_at, u.name AS captured_by_name
               FROM service_ticket_signoffs s LEFT JOIN users u ON u.id = s.captured_by
               WHERE s.ticket_id = $1""",
            ticket_id,
        )
        current_signoff = await _current_signoff(conn, ticket_id)
    signoff_by_event = {str(r["event_id"]): r for r in rows_to_list(signoffs)}
    by_event: dict[str, list[dict]] = {}
    for img in rows_to_list(images):
        img.pop("storage_key")  # internal; the photo is fetched through /images/{image_id} (login required)
        by_event.setdefault(str(img["event_id"]), []).append(img)
    timeline = []
    for ev in rows_to_list(events):
        ev["images"] = by_event.get(str(ev["id"]), [])
        ev["signoff"] = signoff_by_event.get(str(ev["id"]))  # the customer's sign-off captured with this visit, if any
        timeline.append(ev)
    result = _present(ticket, user)
    result["events"] = timeline
    result["customer_signoff"] = current_signoff  # the one that counts for the latest visit (null = none)
    return result


@router.get("/{ticket_id}/pdf")
async def get_ticket_pdf(ticket_id: str, user=Depends(ANY_TICKET_USER)):
    """Printable job card. Same visibility as the ticket itself; the charge and invoice are left off for field staff."""
    ticket_id = _uuid_or_404(ticket_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        ticket = await _load_visible(conn, ticket_id, user)
        events = await conn.fetch(
            """SELECT e.event_type, e.note, e.scheduled_date, e.created_at, u.name AS actor_name
               FROM service_ticket_events e LEFT JOIN users u ON u.id = e.actor_id
               WHERE e.ticket_id = $1 ORDER BY e.created_at, e.id""",
            ticket_id,
        )
        store = await conn.fetchrow("SELECT * FROM store_profile WHERE id = 1")
        signoff_row = await conn.fetchrow(_CURRENT_SIGNOFF_SQL, ticket_id)
    store_data = row_to_dict(store) or {}
    logo = await load_logo_bytes(store_data)
    signoff = _signoff_json(signoff_row)
    signature_png = await _load_signoff_png(signoff_row["signature_key"]) if signoff_row is not None else None
    pdf_bytes = await asyncio.to_thread(build_ticket_pdf, store_data, row_to_dict(ticket), rows_to_list(events), _is_owner(user), logo,
                                 signoff, signature_png)
    safe_name = ticket["ticket_no"].replace("/", "-")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_name}.pdf"'},
    )


@router.get("/{ticket_id}/images/{image_id}")
async def get_ticket_image(ticket_id: str, image_id: str, user=Depends(ANY_TICKET_USER)):
    ticket_id, image_id = _uuid_or_404(ticket_id), _uuid_or_404(image_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        await _load_visible(conn, ticket_id, user)
        img = await conn.fetchrow(
            "SELECT storage_key, content_type FROM service_ticket_images WHERE id = $1 AND ticket_id = $2",
            image_id, ticket_id,
        )
    if img is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    try:
        data = await asyncio.to_thread(storage.get_bytes, img["storage_key"])
    except storage.ObjectMissing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The photo file is missing on the server")
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Photo storage is not reachable, try again")
    # Private: only the signed-in user's own app may cache it (a stored photo never changes).
    return Response(content=data, media_type=img["content_type"],
                    headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})


@router.get("/{ticket_id}/signoffs/{signoff_id}/signature")
async def get_signoff_signature(ticket_id: str, signoff_id: str, user=Depends(ANY_TICKET_USER)):
    """The customer's signature (PNG) captured with a visit. Same visibility as the ticket itself."""
    ticket_id, signoff_id = _uuid_or_404(ticket_id), _uuid_or_404(signoff_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        await _load_visible(conn, ticket_id, user)
        key = await conn.fetchval(
            "SELECT signature_key FROM service_ticket_signoffs WHERE id = $1 AND ticket_id = $2", signoff_id, ticket_id,
        )
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    try:
        data = await asyncio.to_thread(storage.get_bytes, key)
    except storage.ObjectMissing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The signature file is missing on the server")
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Signature storage is not reachable, try again")
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})


# ---------- OWNER: raise / edit / close / cancel ----------
async def raise_ticket(owner: dict, *, customer_id: str, service_type: str, title: str, description: str | None,
                       site_address: str | None, warranty_barcode: str | None, charge_amount, gst_rate,
                       assigned_to: str | None, scheduled_date: date | None, amc_id: str | None) -> dict:
    """Creates a ticket (shared by POST /service-tickets and POST /amc/{id}/tickets) and returns it as the owner sees it."""
    customer_id = _uuid_or_400(customer_id)
    title = title.strip()
    if not title:
        raise _bad("Describe the problem in a few words")
    _check_not_past(scheduled_date, "The scheduled date")
    if service_type == "AMC":
        if not amc_id:
            raise _bad("Choose the AMC contract that covers this request")
    elif amc_id:
        raise _bad("An AMC contract can only be linked to a ticket of type AMC")
    pool = get_pool()
    async with pool.acquire() as conn:
        customer = await conn.fetchrow("SELECT id, address FROM customers WHERE id = $1 AND is_active = TRUE", customer_id)
        if customer is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
        assignee_name = await _check_assignee(conn, assigned_to) if assigned_to else None
        async with conn.transaction():
            ticket_no = await next_doc_number(conn, "service_tickets", "ticket_no", "TKT")  # first: see next_doc_number
            contract = await _assert_amc_covers(conn, amc_id, customer_id) if service_type == "AMC" else None
            warranty_id = None
            if warranty_barcode and warranty_barcode.strip():
                warranty_id = await _resolve_warranty(conn, warranty_barcode, customer_id, service_type)
            charge = charge_amount if service_type == "PAID" else None
            row = await conn.fetchrow(
                """INSERT INTO service_tickets (ticket_no, customer_id, service_type, title, description, site_address,
                                                warranty_id, charge_amount, gst_rate, assigned_to, scheduled_date, created_by,
                                                amc_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::uuid,$11,$12,$13::uuid) RETURNING id""",
                ticket_no, customer_id, service_type, title,
                (description or "").strip() or None,
                (site_address or "").strip() or (contract["site_address"] if contract else None) or customer["address"],
                warranty_id, charge, gst_rate,
                _uuid_or_400(assigned_to) if assigned_to else None,
                scheduled_date, owner["id"],
                str(contract["id"]) if contract else None,
            )
            note = f"{_TYPE_LABEL[service_type]} request raised"
            if contract:
                note += f" under {contract['contract_no']}"
            if assignee_name:
                note += f", assigned to {assignee_name}"
            await _add_event(conn, row["id"], "CREATED", owner["id"], note, scheduled_date)
        created = await _load_visible(conn, str(row["id"]), owner)
    return _present(created, owner)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_ticket(body: CreateTicketRequest, owner=Depends(OWNER_ONLY)):
    return await raise_ticket(
        owner, customer_id=body.customer_id, service_type=body.service_type, title=body.title,
        description=body.description, site_address=body.site_address, warranty_barcode=body.warranty_barcode,
        charge_amount=body.charge_amount, gst_rate=body.gst_rate, assigned_to=body.assigned_to,
        scheduled_date=body.scheduled_date, amc_id=body.amc_id,
    )


async def raise_amc_ticket(owner: dict, contract_id: str, body: CreateAmcTicketRequest) -> dict:
    """POST /amc/{contract_id}/tickets (see routers/amc.py): a ticket of type AMC for that contract's customer."""
    pool = get_pool()
    async with pool.acquire() as conn:
        customer_id = await conn.fetchval("SELECT customer_id FROM amc_contracts WHERE id = $1::uuid", contract_id)
    if customer_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return await raise_ticket(
        owner, customer_id=str(customer_id), service_type="AMC", title=body.title, description=body.description,
        site_address=body.site_address, warranty_barcode=body.warranty_barcode, charge_amount=None,
        gst_rate=Decimal("18.00"), assigned_to=body.assigned_to, scheduled_date=body.scheduled_date, amc_id=contract_id,
    )


@router.put("/{ticket_id}")
async def update_ticket(ticket_id: str, body: UpdateTicketRequest, owner=Depends(OWNER_ONLY)):
    ticket_id = _uuid_or_404(ticket_id)
    sent = body.model_fields_set
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            t = await _lock_ticket(conn, ticket_id, owner)
            if t["status"] not in ("OPEN", "RESCHEDULED", "ATTENDED"):
                raise _bad(f"A {t['status'].lower()} ticket can't be edited", status.HTTP_409_CONFLICT)
            if t["invoice_id"] is not None:
                raise _bad("This ticket is already billed on an invoice", status.HTTP_409_CONFLICT)

            service_type = body.service_type or t["service_type"]
            old_amc_id = str(t["amc_id"]) if t["amc_id"] is not None else None
            amc_id = old_amc_id
            if "amc_id" in sent:
                amc_id = _uuid_or_400(body.amc_id) if body.amc_id else None
            amc_contract = None
            if service_type == "AMC":
                if amc_id is None:
                    raise _bad("Choose the AMC contract that covers this request")
                if t["service_type"] != "AMC" or amc_id != old_amc_id:
                    amc_contract = await _assert_amc_covers(conn, amc_id, str(t["customer_id"]))
            else:
                if "amc_id" in sent and body.amc_id:
                    raise _bad("An AMC contract can only be linked to a ticket of type AMC")
                amc_id = None  # a ticket that is not an AMC one is not covered by a contract
            title = body.title.strip() if body.title is not None else t["title"]
            if not title:
                raise _bad("Describe the problem in a few words")
            description = ((body.description or "").strip() or None) if "description" in sent else t["description"]
            site_address = ((body.site_address or "").strip() or None) if "site_address" in sent else t["site_address"]
            gst_rate = _money(body.gst_rate if body.gst_rate is not None else t["gst_rate"])
            charge = _money(body.charge_amount if "charge_amount" in sent else t["charge_amount"])
            if service_type in NO_CHARGE_TYPES:
                charge = None
            scheduled_date = body.scheduled_date if "scheduled_date" in sent else t["scheduled_date"]
            if "scheduled_date" in sent and scheduled_date != t["scheduled_date"]:
                _check_not_past(scheduled_date, "The scheduled date")
            assigned_to = t["assigned_to"]
            assignee_name = None
            if "assigned_to" in sent:
                if body.assigned_to:
                    assignee_name = await _check_assignee(conn, body.assigned_to)
                    assigned_to = uuid.UUID(_uuid_or_400(body.assigned_to))
                else:
                    assigned_to = None

            if service_type == "IN_WARRANTY" and t["service_type"] != "IN_WARRANTY" and t["warranty_id"] is not None:
                valid_until = await conn.fetchval(
                    "SELECT customer_warranty_valid_until FROM part_warranties WHERE id = $1", t["warranty_id"]
                )
                _assert_warranty_valid(valid_until)

            changes: list[str] = []
            if service_type != t["service_type"]:
                changes.append(f"Service type: {_TYPE_LABEL[t['service_type']]} → {_TYPE_LABEL[service_type]}")
            if amc_contract is not None and service_type == t["service_type"]:
                changes.append(f"AMC contract: {amc_contract['contract_no']}")
            elif amc_contract is not None:
                changes[-1] += f" ({amc_contract['contract_no']})"
            if assigned_to != t["assigned_to"]:
                changes.append(f"Assigned to {assignee_name}" if assigned_to else "Unassigned")
            if scheduled_date != t["scheduled_date"]:
                changes.append(f"Scheduled for {scheduled_date.isoformat()}" if scheduled_date else "Schedule cleared")
            if (title, description, site_address, charge, gst_rate) != (
                t["title"], t["description"], t["site_address"], _money(t["charge_amount"]), _money(t["gst_rate"])
            ):
                changes.append("Details updated")
            if not changes:
                return _present(await _load_visible(conn, ticket_id, owner), owner)

            await conn.execute(
                """UPDATE service_tickets SET service_type=$1, title=$2, description=$3, site_address=$4,
                          charge_amount=$5, gst_rate=$6, assigned_to=$7, scheduled_date=$8, amc_id=$10::uuid, updated_at=now()
                   WHERE id=$9""",
                service_type, title, description, site_address, charge, gst_rate, assigned_to, scheduled_date, ticket_id,
                amc_id,
            )
            await _add_event(conn, ticket_id, "UPDATED", owner["id"], "; ".join(changes))
        row = await _load_visible(conn, ticket_id, owner)
    return _present(row, owner)


@router.post("/{ticket_id}/close")
async def close_ticket(ticket_id: str, body: CloseTicketRequest | None = None, owner=Depends(OWNER_ONLY)):
    ticket_id = _uuid_or_404(ticket_id)
    note = ((body.note if body else None) or "").strip() or None
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            t = await _lock_ticket(conn, ticket_id, owner)
            if t["status"] != "ATTENDED":
                raise _bad("Only a ticket the technician has marked attended can be closed", status.HTTP_409_CONFLICT)
            await conn.execute(
                "UPDATE service_tickets SET status='CLOSED', closed_at=now(), updated_at=now() WHERE id=$1", ticket_id
            )
            await _add_event(conn, ticket_id, "CLOSED", owner["id"], note)
        row = await _load_visible(conn, ticket_id, owner)
    return _present(row, owner)


@router.post("/{ticket_id}/cancel")
async def cancel_ticket(ticket_id: str, body: CancelTicketRequest, owner=Depends(OWNER_ONLY)):
    ticket_id = _uuid_or_404(ticket_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            t = await _lock_ticket(conn, ticket_id, owner)
            if t["status"] not in ("OPEN", "RESCHEDULED"):
                raise _bad("Only a ticket that hasn't been attended yet can be cancelled", status.HTTP_409_CONFLICT)
            await conn.execute(
                "UPDATE service_tickets SET status='CANCELLED', updated_at=now() WHERE id=$1", ticket_id
            )
            await _add_event(conn, ticket_id, "CANCELLED", owner["id"], body.reason.strip())
        row = await _load_visible(conn, ticket_id, owner)
    return _present(row, owner)


@router.post("/{ticket_id}/reopen")
async def reopen_ticket(ticket_id: str, body: ReopenTicketRequest, owner=Depends(OWNER_ONLY)):
    """Send an ATTENDED ticket back to the technician (the work is wrong or incomplete). The ticket goes back to OPEN,
    the earlier visit, its photos and the reason stay on the timeline, and the technician can attend it again.
    Not possible once the ticket is closed, or while it is billed on an invoice (cancel that invoice first)."""
    ticket_id = _uuid_or_404(ticket_id)
    reason = body.reason.strip()
    if not reason:
        raise _bad("Say what needs to be redone")
    _check_not_past(body.scheduled_date, "The new visit date")
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            t = await _lock_ticket(conn, ticket_id, owner)
            if t["status"] != "ATTENDED":
                raise _bad("Only a ticket the technician has marked attended can be sent back", status.HTTP_409_CONFLICT)
            if t["invoice_id"] is not None:
                raise _bad("This ticket is billed on an invoice; cancel that invoice first", status.HTTP_409_CONFLICT)
            await conn.execute(
                """UPDATE service_tickets SET status='OPEN', attended_at=NULL,
                          scheduled_date=COALESCE($2::date, scheduled_date), updated_at=now() WHERE id=$1""",
                ticket_id, body.scheduled_date,
            )
            await _add_event(conn, ticket_id, "REOPENED", owner["id"], reason, body.scheduled_date)
        row = await _load_visible(conn, ticket_id, owner)
    return _present(row, owner)


# ---------- STAFF / TECHNICIAN: attend / reschedule ----------
def _assert_field_may_act(ticket) -> None:
    if ticket["status"] not in ("OPEN", "RESCHEDULED"):
        raise _bad(f"This ticket is already {ticket['status'].lower()}", status.HTTP_409_CONFLICT)


async def _clean_photos(images: list[UploadFile]) -> list[bytes]:
    """Re-encodes the photos one at a time (strips EXIF / GPS). Only the small clean result is kept, so a request never
    holds more than one original in memory."""
    max_bytes = settings.max_upload_mb * 1024 * 1024
    photos: list[bytes] = []
    for upload in images:
        raw = await upload.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise _bad(f"Each photo must be under {settings.max_upload_mb} MB")
        try:
            photos.append(await photo.process(raw))
        except photo.PhotoError as exc:
            raise _bad(str(exc))
        finally:
            del raw
            await upload.close()
    return photos


async def _record_visit(ticket_id: str, user: dict, photos: list[bytes], *, event_type: str, note: str | None,
                        check, apply, signoff: Signoff | None = None) -> dict:
    """Uploads the photos (and the customer's signature) to R2 BEFORE the transaction (no DB connection or row lock is held
    while R2 is slow), then in a short DB-only transaction locks the ticket, runs `check(conn, ticket)` (raises if it can't
    be done any more), writes the timeline event, the photo rows and the sign-off row, and runs `apply(conn, ticket)` for the
    ticket's own columns. If anything fails, the uploaded files are deleted again."""
    pool = get_pool()
    keys = [f"service_tickets/{ticket_id}/{uuid.uuid4().hex}.{photo.EXTENSION}" for _ in photos]
    uploads = list(zip(keys, photos, [photo.CONTENT_TYPE] * len(photos)))
    signature_key = None
    if signoff is not None and signoff.png is not None:
        signature_key = f"service_tickets/{ticket_id}/signoff/{uuid.uuid4().hex}.png"
        keys.append(signature_key)
        uploads.append((signature_key, signoff.png, "image/png"))
    try:
        results = await asyncio.gather(
            *(asyncio.to_thread(storage.put_bytes, key, data, content_type) for key, data, content_type in uploads),
            return_exceptions=True,  # let every upload finish so the clean-up below sees them all
        )
        if any(isinstance(r, BaseException) for r in results):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Photo storage is not reachable, try again")

        async with pool.acquire() as conn:
            async with conn.transaction():
                t = await _lock_ticket(conn, ticket_id, user)
                await check(conn, t)
                event_id = await _add_event(conn, ticket_id, event_type, user["id"], note)
                for key, data in zip(keys, photos):
                    await conn.execute(
                        """INSERT INTO service_ticket_images (ticket_id, event_id, storage_key, content_type, size_bytes, uploaded_by)
                           VALUES ($1,$2,$3,$4,$5,$6)""",
                        ticket_id, event_id, key, photo.CONTENT_TYPE, len(data), user["id"],
                    )
                if signoff is not None:
                    await conn.execute(
                        """INSERT INTO service_ticket_signoffs (ticket_id, event_id, customer_name, acknowledged, signature_key,
                                                                not_obtained_reason, captured_by)
                           VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                        ticket_id, event_id, signoff.customer_name, signoff.acknowledged, signature_key,
                        signoff.not_obtained_reason, user["id"],
                    )
                await apply(conn, t)
            row = await _load_visible(conn, ticket_id, user)
            current_signoff = await _current_signoff(conn, ticket_id)
    except BaseException:
        # Rejected, rolled back or cancelled: don't leave orphan photos in storage (shielded so a dropped client can't skip it).
        if keys:
            await asyncio.shield(asyncio.to_thread(storage.delete_keys, keys))
        raise
    result = _present(row, user)
    result["customer_signoff"] = current_signoff
    return result


@router.post("/{ticket_id}/attend")
async def attend_ticket(
    ticket_id: str,
    remarks: str | None = Form(default=None, max_length=2000),
    images: list[UploadFile] = File(default=[]),
    customer_name: str | None = Form(default=None, max_length=100),
    customer_signature: str | None = Form(default=None, max_length=MAX_SIGNOFF_B64),
    customer_acknowledged: bool = Form(default=False),
    no_signoff_reason: str | None = Form(default=None, max_length=300),
    user=Depends(FIELD_ONLY),
):
    """Mark the visit done. Multipart form: `images` = 1-6 photos of the finished work (JPEG / PNG / WebP; stored as
    re-encoded JPEGs without EXIF / GPS),
    `remarks` = optional free text. The ticket then waits for the owner to review the timeline and close it
    (or send it back with `/reopen`).

    Customer sign-off (all optional unless the server sets REQUIRE_CUSTOMER_SIGNOFF=true): `customer_signature` = the
    customer's signature as base64 (raw, or a `data:image/png;base64,` URL) and / or `customer_acknowledged=true`, with
    `customer_name` = who signed; or `no_signoff_reason` = why there is none (customer not at site, refused...).
    It is printed on the job card."""
    ticket_id = _uuid_or_404(ticket_id)
    if not images:
        raise _bad("Add at least one photo of the finished work")
    if len(images) > MAX_PHOTOS:
        raise _bad(f"At most {MAX_PHOTOS} photos can be added at once")

    # 1. Cheap early check, no row lock: don't decode or upload photos for a ticket that can't be attended.
    pool = get_pool()
    async with pool.acquire() as conn:
        _assert_field_may_act(await _lock_ticket(conn, ticket_id, user, for_update=False))

    # 2. Validate the sign-off and clean the photos, 3. upload, 4. short DB transaction (see _record_visit).
    signoff = await _prepare_signoff(customer_name, customer_signature, customer_acknowledged, no_signoff_reason,
                                     required=settings.require_customer_signoff)
    photos = await _clean_photos(images)

    async def check(conn, t) -> None:
        _assert_field_may_act(t)

    async def apply(conn, t) -> None:
        await conn.execute(
            """UPDATE service_tickets SET status='ATTENDED', attended_at=now(),
                      assigned_to=COALESCE(assigned_to, $2::uuid), updated_at=now() WHERE id=$1""",
            ticket_id, user["id"],
        )

    return await _record_visit(ticket_id, user, photos, event_type="ATTENDED",
                               note=(remarks or "").strip() or None, check=check, apply=apply, signoff=signoff)


async def _assert_may_add_to_attended(conn, ticket, extra_photos: int, adds_signoff: bool = False) -> None:
    if ticket["status"] != "ATTENDED":
        raise _bad(
            "Photos and notes can only be added after the visit is marked attended, and until the owner closes the ticket"
            if ticket["status"] in ("OPEN", "RESCHEDULED") else f"This ticket is already {ticket['status'].lower()}",
            status.HTTP_409_CONFLICT,
        )
    if extra_photos:
        have = await conn.fetchval("SELECT COUNT(*) FROM service_ticket_images WHERE ticket_id = $1", ticket["id"])
        if have + extra_photos > MAX_TICKET_PHOTOS:
            raise _bad(f"A ticket can hold at most {MAX_TICKET_PHOTOS} photos ({have} already added)")
    if adds_signoff:
        current = await _current_signoff(conn, str(ticket["id"]))
        if current is not None and current["obtained"]:
            raise _bad("The customer has already signed off this visit", status.HTTP_409_CONFLICT)


@router.post("/{ticket_id}/addendum")
async def add_to_attended_ticket(
    ticket_id: str,
    remarks: str | None = Form(default=None, max_length=2000),
    images: list[UploadFile] = File(default=[]),
    customer_name: str | None = Form(default=None, max_length=100),
    customer_signature: str | None = Form(default=None, max_length=MAX_SIGNOFF_B64),
    customer_acknowledged: bool = Form(default=False),
    no_signoff_reason: str | None = Form(default=None, max_length=300),
    user=Depends(FIELD_ONLY),
):
    """Add forgotten photos and / or a note to a ticket that is already ATTENDED, until the owner closes it. Multipart
    form like `/attend`: `images` = 0-6 photos, `remarks` = free text, and the customer sign-off fields of `/attend`
    (for a technician who couldn't get the signature at the time); at least one of them. It appears on the timeline as an
    `ADDENDUM` event and does not change the ticket's status. A visit the customer has already signed off can't be signed
    off again (409)."""
    ticket_id = _uuid_or_404(ticket_id)
    note = (remarks or "").strip() or None
    # What was sent, judged without decoding anything yet: the signature is only decoded once the ticket has passed the cheap check.
    signs_now = bool((customer_signature or "").strip() or customer_acknowledged)
    if not images and not note and not (signs_now or (no_signoff_reason or "").strip()):
        raise _bad("Add a photo, a note or the customer's sign-off")
    if len(images) > MAX_PHOTOS:
        raise _bad(f"At most {MAX_PHOTOS} photos can be added at once")

    pool = get_pool()
    async with pool.acquire() as conn:  # cheap early check, no row lock
        await _assert_may_add_to_attended(conn, await _lock_ticket(conn, ticket_id, user, for_update=False), len(images),
                                          signs_now)

    signoff = await _prepare_signoff(customer_name, customer_signature, customer_acknowledged, no_signoff_reason,
                                     required=False)
    photos = await _clean_photos(images)

    async def check(conn, t) -> None:
        await _assert_may_add_to_attended(conn, t, len(photos), signs_now)

    async def apply(conn, t) -> None:
        await conn.execute("UPDATE service_tickets SET updated_at=now() WHERE id=$1", ticket_id)

    return await _record_visit(ticket_id, user, photos, event_type="ADDENDUM", note=note, check=check, apply=apply,
                               signoff=signoff)


@router.post("/{ticket_id}/reschedule")
async def reschedule_ticket(ticket_id: str, body: RescheduleTicketRequest, user=Depends(FIELD_ONLY)):
    ticket_id = _uuid_or_404(ticket_id)
    _check_not_past(body.new_date, "The new date")
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            t = await _lock_ticket(conn, ticket_id, user)
            _assert_field_may_act(t)
            await conn.execute(
                """UPDATE service_tickets SET status='RESCHEDULED', scheduled_date=$2,
                          assigned_to=COALESCE(assigned_to, $3::uuid), updated_at=now() WHERE id=$1""",
                ticket_id, body.new_date, user["id"],
            )
            await _add_event(conn, ticket_id, "RESCHEDULED", user["id"], body.reason.strip(), body.new_date)
        row = await _load_visible(conn, ticket_id, user)
    return _present(row, user)
