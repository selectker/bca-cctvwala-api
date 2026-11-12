"""Credit notes (sales returns): goods the customer sends back, or a price correction after the sale.

A credit note is issued against ONE invoice and names the invoice lines (and quantities, and serial numbers) that come
back. It carries its own GST (the same CGST+SGST / IGST split as the invoice), a number from the "CN" series and a PDF.
Settlement goes through the invoice's payment rows, so receivables stay right without special cases:
  * a CREDIT_NOTE row of the full note value is booked on the invoice (it settles what the customer still owes), and
  * when the note is bigger than what is still owed, a negative refund row (in `refund_mode`) records the money handed back.
Cancelling a note takes everything back out the same way.
"""
import asyncio
import json
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.db import get_pool
from app.schemas.notes import CancelNoteRequest, CreateCreditNoteRequest
from app.security import require_role
from app.utils.audit import AuditRoute
from app.utils.dates import today_local
from app.utils.doc_number import next_doc_number
from app.utils.gst import round2
from app.utils.logo import load_logo_bytes
from app.utils.notes import ZERO, check_quantity, settle, share, status_for
from app.utils.pagination import Page, paged_response
from app.utils.pdf import build_credit_note_pdf
from app.utils.rows import row_to_dict, rows_to_list
from app.utils.search import SearchTerm
from app.utils.signature import load_signature_bytes

router = APIRouter(route_class=AuditRoute, prefix="/credit-notes", tags=["credit-notes"],
                   dependencies=[Depends(require_role("OWNER"))])

_OPEN_TICKET = ("OPEN", "RESCHEDULED", "ATTENDED")
_RUPEE = Decimal("1")


@router.get("")
async def list_credit_notes(
    status_filter: str | None = Query(default=None, alias="status"),
    customer_id: str | None = None,
    invoice_id: str | None = None,
    search: SearchTerm = None,
    page: Page = Depends(),
):
    """Newest first. `status` = ACTIVE / CANCELLED; `search` matches the credit note no., invoice no. or customer name."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT n.*, c.name AS customer_name, i.invoice_no, i.buyer_name, COUNT(*) OVER() AS _total
               FROM credit_notes n JOIN customers c ON c.id = n.customer_id JOIN invoices i ON i.id = n.invoice_id
               WHERE ($1::text IS NULL OR n.status = $1)
                 AND ($2::uuid IS NULL OR n.customer_id = $2)
                 AND ($3::uuid IS NULL OR n.invoice_id = $3)
                 AND ($4::text IS NULL OR n.credit_note_no ILIKE '%'||$4||'%' ESCAPE '\\' OR i.invoice_no ILIKE '%'||$4||'%' ESCAPE '\\'
                      OR c.name ILIKE '%'||$4||'%' ESCAPE '\\')
               ORDER BY n.note_date DESC, n.created_at DESC, n.id LIMIT $5 OFFSET $6""",
            status_filter, customer_id, invoice_id, search, page.limit, page.offset,
        )
    return paged_response(rows)


async def _load(conn, note_id: str):
    note = await conn.fetchrow(
        """SELECT n.*, c.name AS customer_name, c.address AS customer_address, c.gstin AS customer_gstin,
                  c.phone AS customer_phone, c.state AS customer_state, c.state_code AS customer_state_code,
                  i.invoice_no, i.invoice_date, i.buyer_name
           FROM credit_notes n JOIN customers c ON c.id = n.customer_id JOIN invoices i ON i.id = n.invoice_id
           WHERE n.id = $1""",
        note_id,
    )
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    items = await conn.fetch(
        """SELECT ni.*, p.name AS product_name, p.hsn_code AS product_hsn_code
           FROM credit_note_items ni LEFT JOIN products p ON p.id = ni.product_id
           WHERE ni.credit_note_id = $1 ORDER BY ni.id""",
        note_id,
    )
    return note, items


@router.get("/{note_id}")
async def get_credit_note(note_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        note, items = await _load(conn, note_id)
    result = row_to_dict(note)
    result["items"] = [{k: v for k, v in r.items() if k != "unit_snapshot"} for r in rows_to_list(items)]
    return result


@router.get("/{note_id}/pdf")
async def get_credit_note_pdf(note_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        note, items = await _load(conn, note_id)
        store = await conn.fetchrow("SELECT * FROM store_profile WHERE id = 1")
    store_data = row_to_dict(store) or {}
    logo = await load_logo_bytes(store_data)
    signature = await load_signature_bytes(store_data)
    data = row_to_dict(note)
    rows = [{k: v for k, v in r.items() if k != "unit_snapshot"} for r in rows_to_list(items)]
    pdf = await asyncio.to_thread(build_credit_note_pdf, store_data, data, rows, logo, signature)
    safe = note["credit_note_no"].replace("/", "-")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{safe}.pdf"'})


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_credit_note(body: CreateCreditNoteRequest, owner=Depends(require_role("OWNER"))):
    """Issue a credit note for goods returned against an invoice. Quantity can't exceed what is still left to return
    on a line (earlier live notes count). A serial-tracked line needs exactly one serial number per unit, and each must
    be a unit sold on that invoice line. Refused (409) when a returned unit has an open service ticket."""
    if body.note_date and body.note_date > today_local():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The date can't be in the future")
    asked = [ln.item_id for ln in body.items]
    if len(set(asked)) != len(asked):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Each invoice line can appear only once")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            number = await next_doc_number(conn, "credit_notes", "credit_note_no", "CN")
            invoice = await conn.fetchrow("SELECT * FROM invoices WHERE id = $1 FOR UPDATE", body.invoice_id)
            if invoice is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
            if invoice["status"] == "CANCELLED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This invoice is cancelled")
            if body.note_date and body.note_date < invoice["invoice_date"]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="The credit note can't be dated before its invoice")

            lines = {str(r["id"]): r for r in await conn.fetch(
                """SELECT ii.*, p.is_serialized, p.name AS product_name FROM invoice_items ii
                   LEFT JOIN products p ON p.id = ii.product_id WHERE ii.invoice_id = $1""", body.invoice_id)}
            given = {str(r["invoice_item_id"]): r for r in await conn.fetch(
                """SELECT ni.invoice_item_id, SUM(ni.quantity) AS qty, SUM(ni.taxable_value) AS taxable,
                          SUM(ni.cgst_amount) AS cgst, SUM(ni.sgst_amount) AS sgst, SUM(ni.igst_amount) AS igst,
                          SUM(ni.discount_amount) AS discount
                   FROM credit_note_items ni JOIN credit_notes n ON n.id = ni.credit_note_id
                   WHERE n.invoice_id = $1 AND n.status = 'ACTIVE' GROUP BY ni.invoice_item_id""", body.invoice_id)}
            credited_before = await conn.fetchval(
                "SELECT COALESCE(SUM(total_amount), 0) FROM credit_notes WHERE invoice_id = $1 AND status = 'ACTIVE'",
                body.invoice_id)

            out_items, unit_ids = [], []
            for ln in body.items:
                src = lines.get(ln.item_id)
                if src is None:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                        detail="A line is not part of this invoice")
                label = src["description"] or src["product_name"] or "Item"
                prev = given.get(ln.item_id)
                done = prev["qty"] if prev else Decimal("0")
                remaining = src["quantity"] - done
                check_quantity(ln.quantity, remaining, label)

                line_taxable = round2(src["line_total"] - src["cgst_amount"] - src["sgst_amount"] - src["igst_amount"])
                def part(total, key):
                    return share(total, src["quantity"], ln.quantity, remaining, prev[key] if prev else ZERO)
                taxable = part(line_taxable, "taxable")
                cgst, sgst, igst = (part(src["cgst_amount"], "cgst"), part(src["sgst_amount"], "sgst"),
                                    part(src["igst_amount"], "igst"))
                discount = part(src["discount_amount"], "discount")

                barcodes = [b for b in (ln.barcodes or [])]
                snapshot = []
                tracked = await conn.fetchval(
                    "SELECT COUNT(*) FROM part_warranties WHERE invoice_item_id = $1", ln.item_id)
                if tracked:
                    if ln.quantity != ln.quantity.to_integral_value() or len(barcodes) != int(ln.quantity):
                        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                            detail=f"{label}: give exactly {ln.quantity.normalize():f} serial number(s), one per unit")
                    if len(set(barcodes)) != len(barcodes):
                        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                            detail=f"{label}: a serial number is repeated")
                    units = await conn.fetch(
                        """SELECT id, barcode, status, invoice_id, invoice_item_id, sale_date, customer_warranty_months,
                                  customer_warranty_valid_until
                           FROM part_warranties WHERE barcode = ANY($1::text[]) AND invoice_item_id = $2 FOR UPDATE""",
                        barcodes, ln.item_id)
                    found = {u["barcode"]: u for u in units}
                    missing = [b for b in barcodes if b not in found]
                    if missing:
                        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                            detail=f"{label}: not sold on this line: {', '.join(missing)}")
                    wrong = [b for b in barcodes if found[b]["status"] != "SOLD"]
                    if wrong:
                        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                            detail=f"{label}: already returned or under a warranty claim: {', '.join(wrong)}")
                    ids = [found[b]["id"] for b in barcodes]
                    busy = await conn.fetch(
                        "SELECT ticket_no FROM service_tickets WHERE warranty_id = ANY($1::uuid[]) AND status = ANY($2::text[])",
                        ids, list(_OPEN_TICKET))
                    if busy:
                        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                            detail="Close or cancel the open service ticket first: "
                                                   + ", ".join(sorted(t["ticket_no"] for t in busy)))
                    unit_ids += ids
                    snapshot = [{"id": str(u["id"]), "barcode": u["barcode"],
                                 "invoice_id": str(u["invoice_id"]) if u["invoice_id"] else None,
                                 "invoice_item_id": str(u["invoice_item_id"]) if u["invoice_item_id"] else None,
                                 "sale_date": u["sale_date"].isoformat() if u["sale_date"] else None,
                                 "customer_warranty_months": u["customer_warranty_months"],
                                 "customer_warranty_valid_until": u["customer_warranty_valid_until"].isoformat()
                                 if u["customer_warranty_valid_until"] else None} for u in (found[b] for b in barcodes)]
                elif barcodes:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                        detail=f"{label}: this line has no serial-numbered units, so no serial numbers can be given")

                out_items.append({
                    "src": src, "quantity": ln.quantity, "taxable": taxable, "cgst": cgst, "sgst": sgst, "igst": igst,
                    "discount": discount, "line_total": round2(taxable + cgst + sgst + igst),
                    "barcodes": barcodes or None, "snapshot": snapshot,
                })

            subtotal = sum((i["taxable"] for i in out_items), ZERO)
            t_cgst = sum((i["cgst"] for i in out_items), ZERO)
            t_sgst = sum((i["sgst"] for i in out_items), ZERO)
            t_igst = sum((i["igst"] for i in out_items), ZERO)
            raw = subtotal + t_cgst + t_sgst + t_igst
            total = raw.quantize(_RUPEE, rounding=ROUND_HALF_UP)
            # Whole-rupee rounding like an invoice, but all notes together can never credit more than the invoice itself.
            total = round2(min(total, round2(invoice["total_amount"] - credited_before)))
            round_off = round2(total - raw)

            applied, refund = settle(total, invoice["total_amount"], invoice["paid_amount"])
            note_date = body.note_date or today_local()
            note = await conn.fetchrow(
                """INSERT INTO credit_notes (credit_note_no, invoice_id, customer_id, note_date, reason, place_of_supply_state_code,
                          is_interstate, subtotal, total_cgst, total_sgst, total_igst, round_off, total_amount,
                          applied_to_invoice, refund_amount, refund_mode, restock, notes, created_by)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19) RETURNING *""",
                number, invoice["id"], invoice["customer_id"], note_date, body.reason.strip(),
                invoice["place_of_supply_state_code"], invoice["is_interstate"], subtotal, t_cgst, t_sgst, t_igst,
                round_off, total, applied, refund, body.refund_mode if refund > 0 else None, body.restock,
                body.notes, owner["id"],
            )
            for it in out_items:
                src = it["src"]
                await conn.execute(
                    """INSERT INTO credit_note_items (credit_note_id, invoice_item_id, product_id, description, hsn_code, quantity,
                              unit_price, gst_rate, discount_amount, taxable_value, cgst_amount, sgst_amount, igst_amount,
                              line_total, barcodes, unit_snapshot)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16::jsonb)""",
                    note["id"], src["id"], src["product_id"], src["description"], src["hsn_code"], it["quantity"],
                    src["unit_price"], src["gst_rate"], it["discount"], it["taxable"], it["cgst"], it["sgst"], it["igst"],
                    it["line_total"], it["barcodes"], json.dumps(it["snapshot"]) if it["snapshot"] else None,
                )
                if body.restock and src["product_id"]:
                    await conn.execute(
                        """INSERT INTO stock_ledger (product_id, quantity, reference_type, reference_id, note, created_by)
                           VALUES ($1,$2,'RETURN_IN',$3,$4,$5)""",
                        src["product_id"], it["quantity"], note["id"], f"Credit note {number}", owner["id"])
            if unit_ids:
                if body.restock:
                    await conn.execute(
                        """UPDATE part_warranties SET status = 'IN_STOCK', invoice_id = NULL, invoice_item_id = NULL,
                                  sale_date = NULL, customer_warranty_months = NULL, customer_warranty_valid_until = NULL,
                                  updated_at = now() WHERE id = ANY($1::uuid[])""", unit_ids)
                else:
                    await conn.execute(
                        "UPDATE part_warranties SET status = 'SCRAPPED', updated_at = now() WHERE id = ANY($1::uuid[])",
                        unit_ids)

            await conn.execute(
                """INSERT INTO payments (invoice_id, amount, payment_mode, payment_date, notes, created_by, credit_note_id)
                   VALUES ($1,$2,'CREDIT_NOTE',$3,$4,$5,$6)""",
                invoice["id"], total, note_date, f"Credit note {number}", owner["id"], note["id"])
            if refund > 0:
                await conn.execute(
                    """INSERT INTO payments (invoice_id, amount, payment_mode, payment_date, notes, created_by, credit_note_id)
                       VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                    invoice["id"], -refund, body.refund_mode, note_date, f"Refund for credit note {number}", owner["id"], note["id"])
            new_paid = invoice["paid_amount"] + total - refund
            await conn.execute("UPDATE invoices SET paid_amount = $2, payment_status = $3 WHERE id = $1",
                               invoice["id"], new_paid, status_for(invoice["total_amount"], new_paid))
        result_id = str(note["id"])
        note, items = await _load(conn, result_id)
    result = row_to_dict(note)
    result["items"] = [{k: v for k, v in r.items() if k != "unit_snapshot"} for r in rows_to_list(items)]
    return result


@router.post("/{note_id}/cancel")
async def cancel_credit_note(note_id: str, body: CancelNoteRequest, owner=Depends(require_role("OWNER"))):
    """Cancels a credit note and puts everything back: the stock it added is taken out again, returned serial units go
    back to SOLD with their warranty, and the settlement / refund payment rows are reversed. Refused (409) when the goods
    have since left stock (sold again) or a returned unit has moved on."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            note = await conn.fetchrow("SELECT * FROM credit_notes WHERE id = $1 FOR UPDATE", note_id)
            if note is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if note["status"] == "CANCELLED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This credit note is already cancelled")
            invoice = await conn.fetchrow("SELECT * FROM invoices WHERE id = $1 FOR UPDATE", note["invoice_id"])
            items = await conn.fetch("SELECT * FROM credit_note_items WHERE credit_note_id = $1", note_id)

            restore = []
            for it in items:
                snaps = it["unit_snapshot"]
                if isinstance(snaps, str):
                    snaps = json.loads(snaps)
                for snap in snaps or []:
                    unit = await conn.fetchrow("SELECT id, status, invoice_id, barcode FROM part_warranties WHERE id = $1 FOR UPDATE", snap["id"])
                    ok = unit is not None and (
                        (note["restock"] and unit["status"] == "IN_STOCK" and unit["invoice_id"] is None)
                        or (not note["restock"] and unit["status"] == "SCRAPPED"))
                    if not ok:
                        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                            detail=f"Serial {snap.get('barcode')} has moved on since the return (sold again or claimed), so this credit note can't be cancelled")
                    restore.append(snap)
                if note["restock"] and it["product_id"]:
                    stock = await conn.fetchval("SELECT current_stock FROM products WHERE id = $1 FOR UPDATE", it["product_id"])
                    if stock is not None and stock < it["quantity"]:
                        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                            detail="The returned goods have already been sold again, so this credit note can't be cancelled")
            for it in items:
                if note["restock"] and it["product_id"]:
                    await conn.execute(
                        """INSERT INTO stock_ledger (product_id, quantity, reference_type, reference_id, note, created_by)
                           VALUES ($1,$2,'RETURN_IN',$3,$4,$5)""",
                        it["product_id"], -it["quantity"], note_id, f"Credit note {note['credit_note_no']} cancelled", owner["id"])
            for snap in restore:
                await conn.execute(
                    """UPDATE part_warranties SET status = 'SOLD', invoice_id = $2::uuid, invoice_item_id = $3::uuid,
                              sale_date = $4::text::date, customer_warranty_months = $5, customer_warranty_valid_until = $6::text::date,
                              updated_at = now() WHERE id = $1::uuid""",
                    snap["id"], snap["invoice_id"], snap["invoice_item_id"], snap["sale_date"],
                    snap["customer_warranty_months"], snap["customer_warranty_valid_until"])

            paid_back = ZERO
            for pay in await conn.fetch(
                    "SELECT * FROM payments WHERE credit_note_id = $1 AND reversed_at IS NULL AND reverses_payment_id IS NULL FOR UPDATE", note_id):
                await conn.execute(
                    """INSERT INTO payments (invoice_id, amount, payment_mode, payment_date, notes, created_by, reverses_payment_id, credit_note_id)
                       VALUES ($1,$2,$3,CURRENT_DATE,$4,$5,$6,$7)""",
                    note["invoice_id"], -pay["amount"], pay["payment_mode"],
                    f"Credit note {note['credit_note_no']} cancelled: {body.reason.strip()}"[:2000], owner["id"], pay["id"], note_id)
                await conn.execute("UPDATE payments SET reversed_at = now(), reversed_by = $2, reversal_reason = $3 WHERE id = $1",
                                   pay["id"], owner["id"], "Credit note cancelled")
                paid_back += pay["amount"]
            new_paid = invoice["paid_amount"] - paid_back
            await conn.execute("UPDATE invoices SET paid_amount = $2, payment_status = $3 WHERE id = $1",
                               invoice["id"], new_paid, status_for(invoice["total_amount"], new_paid))
            await conn.execute(
                """UPDATE credit_notes SET status = 'CANCELLED', cancelled_at = now(), cancel_reason = $2, cancelled_by = $3 WHERE id = $1""",
                note_id, body.reason.strip(), owner["id"])
        note, items = await _load(conn, note_id)
    result = row_to_dict(note)
    result["items"] = [{k: v for k, v in r.items() if k != "unit_snapshot"} for r in rows_to_list(items)]
    return result
