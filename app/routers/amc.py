import asyncio
import uuid
from datetime import date, timedelta

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.utils.search import SearchTerm
from app.db import get_pool
from app.schemas.amc import CreateAmcRequest, RenewAmcRequest, UpdateAmcRequest, UpdateAmcStatusRequest, UpdateAmcVisitRequest
from app.schemas.tickets import CreateAmcTicketRequest
from app.security import get_current_user, require_role
from app.utils.dates import add_months as _add_months, today_local
from app.utils.logo import load_logo_bytes
from app.utils.pdf import build_amc_pdf
from app.utils.signature import load_signature_bytes
from app.utils.doc_number import next_doc_number
from app.utils.pagination import Page, paged_response
from app.utils.rows import row_to_dict, rows_to_list
from app.utils.audit import AuditRoute
from app.utils.ticket_refs import tickets_for_amc

router = APIRouter(route_class=AuditRoute, prefix="/amc", tags=["amc"])

# Business data is owner-only; only recording a visit is open to the field roles (see update_amc_visit).
_OWNER = [Depends(require_role("OWNER"))]
_FIELD_OR_OWNER = [Depends(require_role("OWNER", "STAFF", "TECHNICIAN"))]

FREQ_MONTHS = {"MONTHLY": 1, "QUARTERLY": 3, "HALF_YEARLY": 6, "YEARLY": 12}
MAX_VISITS = 600  # one contract never needs more visits than this; keeps a typo'd end date from creating thousands of rows

# The contract that renewed this one (a cancelled renewal doesn't count), if any.
_RENEWED_TO = """(SELECT r.id FROM amc_contracts r WHERE r.renewed_from_id = a.id AND r.status <> 'CANCELLED'
                   ORDER BY r.created_at DESC, r.id LIMIT 1)"""
_NOT_RENEWED = """NOT EXISTS (SELECT 1 FROM amc_contracts r WHERE r.renewed_from_id = a.id AND r.status <> 'CANCELLED')"""


def schedule_dates(start: date, end: date, frequency: str) -> list[date]:
    """The planned visit dates of a contract: the start date, then every frequency step until the end date."""
    step = FREQ_MONTHS.get(frequency, 3)
    dates: list[date] = []
    cursor = start
    while cursor <= end:
        dates.append(cursor)
        if len(dates) > MAX_VISITS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"That would be more than {MAX_VISITS} visits; check the dates and the visit frequency")
        cursor = _add_months(cursor, step)
    return dates


def term_months(start: date, end: date) -> int:
    """How long a contract's term is, in whole months, whether its end date is written as the last day of the term
    (1 Jan - 31 Dec) or the same day a year later (1 Jan - 1 Jan). 12 when it isn't a whole number of months."""
    following = end + timedelta(days=1)
    for months in range(1, 121):
        day = _add_months(start, months)
        if day in (end, following):
            return months
        if day > following:
            break
    return 12


def _contract_id(raw: str) -> str:
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


async def _load_detail(conn, contract_id: str, user_id: str) -> dict | None:
    """A contract with its customer, its visits (with technician names) and its renewal links."""
    contract = await conn.fetchrow(
        f"""SELECT a.*, c.name AS customer_name, c.phone AS customer_phone,
                   (SELECT f.contract_no FROM amc_contracts f WHERE f.id = a.renewed_from_id) AS renewed_from_contract_no,
                   {_RENEWED_TO} AS renewed_to_id,
                   (SELECT t.contract_no FROM amc_contracts t WHERE t.id = {_RENEWED_TO}) AS renewed_to_contract_no
            FROM amc_contracts a JOIN customers c ON c.id = a.customer_id WHERE a.id = $1::uuid""",
        contract_id,
    )
    if contract is None:
        return None
    visits = await conn.fetch(
        """SELECT v.*,
                  CASE WHEN u.is_platform_admin AND u.id <> $2::uuid THEN NULL ELSE u.name END AS technician_name
           FROM amc_service_visits v
           LEFT JOIN users u ON u.id = v.technician_id
           WHERE v.contract_id = $1::uuid ORDER BY v.scheduled_date, v.id""",
        contract_id, user_id,
    )
    result = row_to_dict(contract)
    result["visits"] = rows_to_list(visits)
    # The service tickets raised under this contract (newest 50; `service_ticket_count` is all of them).
    result["service_tickets"], result["service_ticket_count"] = await tickets_for_amc(conn, contract_id)
    return result


@router.get("", dependencies=_OWNER)
async def list_amc(
    status_filter: str | None = Query(default=None, alias="status"),
    expiring_within_days: int | None = Query(default=None, ge=0, le=3650),  # ACTIVE contracts ending soon
    customer_id: str | None = None,
    search: SearchTerm = None,  # contract no or customer name
    page: Page = Depends(),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT a.*, c.name AS customer_name, c.phone AS customer_phone, {_RENEWED_TO} AS renewed_to_id,
                      COUNT(*) OVER() AS _total
               FROM amc_contracts a
               JOIN customers c ON c.id = a.customer_id
               WHERE ($1::text IS NULL OR a.status = $1)
                 AND ($2::int IS NULL OR (a.status = 'ACTIVE' AND a.end_date <= CURRENT_DATE + $2::int AND {_NOT_RENEWED}))
                 AND ($3::uuid IS NULL OR a.customer_id = $3)
                 AND ($4::text IS NULL OR a.contract_no ILIKE '%'||$4||'%' ESCAPE '\\' OR c.name ILIKE '%'||$4||'%' ESCAPE '\\')
               ORDER BY a.end_date, a.id LIMIT $5 OFFSET $6""",
            status_filter, expiring_within_days, customer_id, search, page.limit, page.offset,
        )
    return paged_response(rows)


@router.get("/expiring", dependencies=_OWNER)
async def get_expiring_amc(days: int = 30, page: Page = Depends()):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT a.*, c.name AS customer_name, c.phone AS customer_phone, COUNT(*) OVER() AS _total
               FROM amc_contracts a
               JOIN customers c ON c.id = a.customer_id
               WHERE a.status = 'ACTIVE' AND a.end_date <= CURRENT_DATE + $1::int * INTERVAL '1 day' AND {_NOT_RENEWED}
               ORDER BY a.end_date, a.id
               LIMIT $2 OFFSET $3""",
            days, page.limit, page.offset,
        )
    return paged_response(rows)


@router.get("/my-visits", dependencies=_FIELD_OR_OWNER)
async def my_visits(
    status_filter: Literal["SCHEDULED", "COMPLETED", "MISSED"] | None = Query(default=None, alias="status"),
    page: Page = Depends(),
    current_user=Depends(get_current_user),
):
    """The visits the signed-in account may act on: assigned to them or to nobody yet (exactly the set
    PUT /amc/visits/{id} accepts from STAFF / TECHNICIAN), on ACTIVE contracts only. Each row carries the contract
    number, site address and customer name / phone so a technician can plan the day, and never the contract's
    price. Open visits come first, oldest due date first; finished ones follow, newest first. Pass `status` to
    narrow to one state. The owner gets the same list for their own account (their own and unassigned visits)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT v.id, v.contract_id, v.scheduled_date, v.completed_date, v.technician_id, v.status, v.remarks,
                      a.contract_no, a.site_address, c.name AS customer_name, c.phone AS customer_phone,
                      COUNT(*) OVER() AS _total
               FROM amc_service_visits v
               JOIN amc_contracts a ON a.id = v.contract_id AND a.status = 'ACTIVE'
               JOIN customers c ON c.id = a.customer_id
               WHERE (v.technician_id = $1::uuid OR v.technician_id IS NULL)
                 AND ($2::text IS NULL OR v.status = $2)
               ORDER BY (v.status <> 'SCHEDULED'),
                        CASE WHEN v.status = 'SCHEDULED' THEN v.scheduled_date END ASC,
                        v.scheduled_date DESC, v.id
               LIMIT $3 OFFSET $4""",
            current_user["id"], status_filter, page.limit, page.offset,
        )
    return paged_response(rows)


@router.get("/{contract_id}", dependencies=_OWNER)
async def get_amc_contract(contract_id: str, current_user=Depends(get_current_user)):
    """One contract with its visits. `renewed_from_contract_no` / `renewed_to_id` / `renewed_to_contract_no` link a
    renewal to the contract it replaced."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await _load_detail(conn, contract_id, current_user["id"])
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return result


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=_OWNER)
async def create_amc_contract(body: CreateAmcRequest, current_user=Depends(get_current_user)):
    if body.end_date < body.start_date:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="End date can't be before the start date")
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            contract_no = await next_doc_number(conn, "amc_contracts", "contract_no", "AMC")
            contract = await conn.fetchrow(
                """INSERT INTO amc_contracts (contract_no, customer_id, site_address, start_date, end_date,
                                               visit_frequency, amount, gst_rate, coverage_notes, created_by)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *""",
                contract_no, body.customer_id, body.site_address, body.start_date, body.end_date,
                body.visit_frequency, body.amount, body.gst_rate, body.coverage_notes, current_user["id"],
            )

            await conn.executemany(
                "INSERT INTO amc_service_visits (contract_id, scheduled_date) VALUES ($1,$2)",
                [(contract["id"], day) for day in schedule_dates(contract["start_date"], contract["end_date"], contract["visit_frequency"])],
            )

    return row_to_dict(contract)


@router.put("/{contract_id}", dependencies=_OWNER)
async def update_amc_contract(contract_id: str, body: UpdateAmcRequest, current_user=Depends(get_current_user)):
    """Edit an ACTIVE contract -- only the fields sent change (the customer can't). When the dates or the visit
    frequency change the visit plan follows: planned dates that are missing are added, and planned visits that no
    longer fit are removed -- but only ones nobody has touched (still SCHEDULED, no technician, no remarks).
    Completed, missed and assigned visits are never deleted. Returns the contract with its visits."""
    contract_id = _contract_id(contract_id)
    sent = body.model_fields_set
    editable = {"site_address", "start_date", "end_date", "visit_frequency", "amount", "gst_rate", "coverage_notes"}
    if not sent & editable:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nothing to update")
    for field in ("start_date", "end_date", "visit_frequency", "amount", "gst_rate"):
        if field in sent and getattr(body, field) is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field} can't be empty")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow("SELECT * FROM amc_contracts WHERE id = $1::uuid FOR UPDATE", contract_id)
            if existing is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if existing["status"] != "ACTIVE":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                    detail="Only an active contract can be edited (set it to ACTIVE first)")
            new = {f: getattr(body, f) if f in sent else existing[f] for f in editable}
            if new["end_date"] < new["start_date"]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="End date can't be before the start date")
            plan_changed = any(new[f] != existing[f] for f in ("start_date", "end_date", "visit_frequency"))
            wanted = schedule_dates(new["start_date"], new["end_date"], new["visit_frequency"]) if plan_changed else []

            await conn.execute(
                """UPDATE amc_contracts SET site_address=$1, start_date=$2, end_date=$3, visit_frequency=$4,
                                            amount=$5, gst_rate=$6, coverage_notes=$7 WHERE id=$8::uuid""",
                new["site_address"], new["start_date"], new["end_date"], new["visit_frequency"],
                new["amount"], new["gst_rate"], new["coverage_notes"], contract_id,
            )
            if plan_changed:
                have = await conn.fetch(
                    "SELECT id, scheduled_date, status, technician_id, remarks FROM amc_service_visits WHERE contract_id = $1::uuid",
                    contract_id,
                )
                wanted_set = set(wanted)
                have_dates = {r["scheduled_date"] for r in have}
                stale = [r["id"] for r in have
                         if r["status"] == "SCHEDULED" and r["technician_id"] is None and not r["remarks"]
                         and r["scheduled_date"] not in wanted_set]
                if stale:
                    await conn.execute("DELETE FROM amc_service_visits WHERE id = ANY($1::uuid[])", stale)
                missing = [day for day in wanted if day not in have_dates]
                if missing:
                    await conn.executemany(
                        "INSERT INTO amc_service_visits (contract_id, scheduled_date) VALUES ($1::uuid,$2)",
                        [(contract_id, day) for day in missing],
                    )
            result = await _load_detail(conn, contract_id, current_user["id"])
    return result


@router.post("/{contract_id}/renew", status_code=status.HTTP_201_CREATED, dependencies=_OWNER)
async def renew_amc_contract(contract_id: str, body: RenewAmcRequest, current_user=Depends(get_current_user)):
    """Renew a contract: creates a NEW contract (new number, new visit plan) that points back at this one
    (`renewed_from_id`). Anything left out is carried over: the new term starts the day after this one ends and lasts
    as long as this one did; frequency, amount, GST rate, site and coverage notes are copied. An ACTIVE or EXPIRED
    contract can be renewed once (409 if it already has a live renewal; 409 for a CANCELLED one). A renewed contract
    stops showing as expiring, and is marked EXPIRED right away when its term has already ended."""
    contract_id = _contract_id(contract_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # The number-series lock first (see next_doc_number), then the row lock: two renewals of one contract
            # then queue instead of deadlocking.
            contract_no = await next_doc_number(conn, "amc_contracts", "contract_no", "AMC")
            old = await conn.fetchrow("SELECT * FROM amc_contracts WHERE id = $1::uuid FOR UPDATE", contract_id)
            if old is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if old["status"] == "CANCELLED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A cancelled contract can't be renewed; make a new contract")
            done = await conn.fetchval(
                "SELECT contract_no FROM amc_contracts WHERE renewed_from_id = $1::uuid AND status <> 'CANCELLED' LIMIT 1", contract_id
            )
            if done:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"This contract has already been renewed ({done})")

            sent = body.model_fields_set
            start = body.start_date or old["end_date"] + timedelta(days=1)
            end = body.end_date or _add_months(start, term_months(old["start_date"], old["end_date"])) - timedelta(days=1)
            if end < start:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="End date can't be before the start date")
            frequency = body.visit_frequency or old["visit_frequency"]
            amount = body.amount if body.amount is not None else old["amount"]
            gst_rate = body.gst_rate if body.gst_rate is not None else old["gst_rate"]
            site = body.site_address if "site_address" in sent else old["site_address"]      # sent as null = cleared
            notes = body.coverage_notes if "coverage_notes" in sent else old["coverage_notes"]
            days = schedule_dates(start, end, frequency)

            new = await conn.fetchrow(
                """INSERT INTO amc_contracts (contract_no, customer_id, site_address, start_date, end_date, visit_frequency,
                                               amount, gst_rate, coverage_notes, renewed_from_id, created_by)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::uuid,$11) RETURNING *""",
                contract_no, old["customer_id"], site, start, end, frequency, amount, gst_rate, notes, contract_id, current_user["id"],
            )
            await conn.executemany(
                "INSERT INTO amc_service_visits (contract_id, scheduled_date) VALUES ($1,$2)",
                [(new["id"], day) for day in days],
            )
            if old["status"] == "ACTIVE" and old["end_date"] < today_local():
                await conn.execute("UPDATE amc_contracts SET status = 'EXPIRED' WHERE id = $1::uuid", contract_id)
            result = await _load_detail(conn, str(new["id"]), current_user["id"])
    return result


@router.post("/{contract_id}/tickets", status_code=status.HTTP_201_CREATED)
async def raise_ticket_from_contract(contract_id: str, body: CreateAmcTicketRequest,
                                     current_user=Depends(require_role("OWNER"))):
    """Raise a service ticket under this AMC contract (OWNER). The ticket gets type AMC and the contract's customer; it has
    no charge, and its site defaults to the contract's site. The contract must be ACTIVE and in its term today (400
    otherwise: renew it, or raise a Paid service). Technicians then attend it like any other ticket; the tickets of a
    contract are in `service_tickets` on GET /amc/{id}, and in GET /service-tickets?amc_id=.
    Visits (`/amc/visits`) are the planned periodic service; a ticket is a complaint or extra call-out. They stay separate."""
    from app.routers.tickets import raise_amc_ticket  # imported late: the tickets router imports nothing from here

    return await raise_amc_ticket(current_user, _contract_id(contract_id), body)


@router.get("/{contract_id}/pdf", dependencies=_OWNER)
async def get_amc_contract_pdf(contract_id: str):
    """The contract as a printable PDF for the customer: parties, site, term, price with GST, coverage, the visit
    schedule and signature lines."""
    contract_id = _contract_id(contract_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT a.*, c.name AS customer_name, c.phone AS customer_phone, c.address AS customer_address,
                      c.gstin AS customer_gstin,
                      (SELECT f.contract_no FROM amc_contracts f WHERE f.id = a.renewed_from_id) AS renewed_from_no
               FROM amc_contracts a JOIN customers c ON c.id = a.customer_id WHERE a.id = $1::uuid""",
            contract_id,
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        visits = await conn.fetch(
            "SELECT scheduled_date, status, completed_date FROM amc_service_visits WHERE contract_id = $1::uuid ORDER BY scheduled_date, id",
            contract_id,
        )
        store = await conn.fetchrow("SELECT * FROM store_profile WHERE id = 1")
    store_data = row_to_dict(store) or {}
    logo = await load_logo_bytes(store_data)
    signature = await load_signature_bytes(store_data)
    pdf_bytes = await asyncio.to_thread(build_amc_pdf, store_data, row_to_dict(row), rows_to_list(visits), logo, signature)
    safe_name = str(row["contract_no"]).replace("/", "-")
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{safe_name}.pdf"'})


@router.put("/{contract_id}/status", dependencies=_OWNER)
async def update_amc_status(contract_id: str, body: UpdateAmcStatusRequest):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE amc_contracts SET status = $1 WHERE id = $2 RETURNING *", body.status, contract_id
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row_to_dict(row)


@router.put("/visits/{visit_id}", dependencies=_FIELD_OR_OWNER)
async def update_amc_visit(visit_id: str, body: UpdateAmcVisitRequest, current_user=Depends(get_current_user)):
    """OWNER: any visit, any technician. STAFF / TECHNICIAN: only a visit assigned to them or to nobody yet (anything
    else is a 404, same as service tickets); acting on an unassigned visit assigns it to them, and they can't hand a
    visit to somebody else or clear its technician."""
    try:
        visit_id = str(uuid.UUID(visit_id))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    is_owner = current_user["role"] == "OWNER"
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow("SELECT * FROM amc_service_visits WHERE id = $1::uuid FOR UPDATE", visit_id)
            if existing is None or (
                not is_owner and existing["technician_id"] is not None and str(existing["technician_id"]) != current_user["id"]
            ):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

            sent = body.model_fields_set
            if not is_owner and "technician_id" in sent and str(body.technician_id) != current_user["id"]:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only assign a visit to yourself")
            technician_id = body.technician_id if "technician_id" in sent else existing["technician_id"]
            if not is_owner and technician_id is None:
                technician_id = current_user["id"]
            remarks = body.remarks if "remarks" in sent else existing["remarks"]
            if technician_id is not None:
                # The platform admin is never a valid technician pick — it's not offered in /auth/team,
                # and it must not be assignable directly by id either (that would leak its identity here).
                try:
                    found = await conn.fetchval(
                        "SELECT 1 FROM users WHERE id = $1::uuid AND is_active = TRUE AND is_platform_admin = FALSE",
                        str(technician_id),
                    )
                except Exception:  # not a valid UUID
                    found = None
                if not found:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That technician doesn't exist or is deactivated")
            completed_date = (body.completed_date or existing["completed_date"] or today_local()) if body.status == "COMPLETED" else None

            await conn.execute(
                """UPDATE amc_service_visits SET status=$1, completed_date=$2, technician_id=$3::uuid, remarks=$4 WHERE id=$5::uuid""",
                body.status, completed_date, str(technician_id) if technician_id is not None else None, remarks, visit_id,
            )
            row = await conn.fetchrow(
                """SELECT v.*,
                          CASE WHEN u.is_platform_admin AND u.id <> $2::uuid THEN NULL ELSE u.name END AS technician_name
                   FROM amc_service_visits v
                   LEFT JOIN users u ON u.id = v.technician_id WHERE v.id = $1::uuid""",
                visit_id, current_user["id"],
            )
    return row_to_dict(row)
