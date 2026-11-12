"""Debit notes (purchase returns): goods sent back to a supplier.

Issued against ONE purchase, naming the purchase lines (quantity, serial numbers) that go back. Stock is taken out
(RETURN_OUT), the serial units become RETURNED, and the note value first reduces what is still owed to the supplier;
anything beyond that is money the supplier pays back (booked as a negative refund payment in `refund_mode`).
Settlement rows follow the same pattern as credit notes (a DEBIT_NOTE payment row on the purchase).
"""
import asyncio
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.db import get_pool
from app.schemas.notes import CancelNoteRequest, CreateDebitNoteRequest
from app.security import require_role
from app.utils.audit import AuditRoute
from app.utils.dates import today_local
from app.utils.doc_number import next_doc_number
from app.utils.gst import round2
from app.utils.logo import load_logo_bytes
from app.utils.notes import ZERO, check_quantity, settle, share, status_for
from app.utils.pagination import Page, paged_response
from app.utils.pdf import build_debit_note_pdf
from app.utils.rows import row_to_dict, rows_to_list
from app.utils.search import SearchTerm
from app.utils.signature import load_signature_bytes

router = APIRouter(route_class=AuditRoute, prefix="/debit-notes", tags=["debit-notes"],
                   dependencies=[Depends(require_role("OWNER"))])


@router.get("")
async def list_debit_notes(
    status_filter: str | None = Query(default=None, alias="status"),
    supplier_id: str | None = None,
    purchase_id: str | None = None,
    search: SearchTerm = None,
    page: Page = Depends(),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT n.*, s.name AS supplier_name, p.purchase_no, COUNT(*) OVER() AS _total
               FROM debit_notes n JOIN suppliers s ON s.id = n.supplier_id JOIN purchases p ON p.id = n.purchase_id
               WHERE ($1::text IS NULL OR n.status = $1)
                 AND ($2::uuid IS NULL OR n.supplier_id = $2)
                 AND ($3::uuid IS NULL OR n.purchase_id = $3)
                 AND ($4::text IS NULL OR n.debit_note_no ILIKE '%'||$4||'%' ESCAPE '\\' OR p.purchase_no ILIKE '%'||$4||'%' ESCAPE '\\'
                      OR s.name ILIKE '%'||$4||'%' ESCAPE '\\')
               ORDER BY n.note_date DESC, n.created_at DESC, n.id LIMIT $5 OFFSET $6""",
            status_filter, supplier_id, purchase_id, search, page.limit, page.offset,
        )
    return paged_response(rows)


async def _load(conn, note_id: str):
    note = await conn.fetchrow(
        """SELECT n.*, s.name AS supplier_name, s.address AS supplier_address, s.gstin AS supplier_gstin,
                  s.phone AS supplier_phone, s.state AS supplier_state, s.state_code AS supplier_state_code,
                  p.purchase_no, p.supplier_invoice_no
           FROM debit_notes n JOIN suppliers s ON s.id = n.supplier_id JOIN purchases p ON p.id = n.purchase_id
           WHERE n.id = $1""",
        note_id,
    )
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    items = await conn.fetch(
        """SELECT ni.*, pr.name AS product_name, pr.hsn_code FROM debit_note_items ni
           JOIN products pr ON pr.id = ni.product_id WHERE ni.debit_note_id = $1 ORDER BY ni.id""",
        note_id,
    )
    return note, items


@router.get("/{note_id}")
async def get_debit_note(note_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        note, items = await _load(conn, note_id)
    result = row_to_dict(note)
    result["items"] = rows_to_list(items)
    return result


@router.get("/{note_id}/pdf")
async def get_debit_note_pdf(note_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        note, items = await _load(conn, note_id)
        store = await conn.fetchrow("SELECT * FROM store_profile WHERE id = 1")
    store_data = row_to_dict(store) or {}
    logo = await load_logo_bytes(store_data)
    signature = await load_signature_bytes(store_data)
    pdf = await asyncio.to_thread(build_debit_note_pdf, store_data, row_to_dict(note), rows_to_list(items), logo, signature)
    safe = note["debit_note_no"].replace("/", "-")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{safe}.pdf"'})


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_debit_note(body: CreateDebitNoteRequest, owner=Depends(require_role("OWNER"))):
    """Return goods from a purchase to the supplier. Quantity can't exceed what is left to return on the line, and the
    goods must still be in stock (409/400 otherwise). A serial-tracked line needs one serial number per unit, each an
    IN_STOCK unit from that purchase line."""
    if body.note_date and body.note_date > today_local():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The date can't be in the future")
    asked = [ln.item_id for ln in body.items]
    if len(set(asked)) != len(asked):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Each purchase line can appear only once")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            number = await next_doc_number(conn, "debit_notes", "debit_note_no", "DN")
            purchase = await conn.fetchrow("SELECT * FROM purchases WHERE id = $1 FOR UPDATE", body.purchase_id)
            if purchase is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")
            if purchase["status"] == "CANCELLED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This purchase is cancelled")
            if body.note_date and body.note_date < purchase["purchase_date"]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="The debit note can't be dated before its purchase")

            lines = {str(r["id"]): r for r in await conn.fetch(
                """SELECT pi.*, pr.name AS product_name FROM purchase_items pi JOIN products pr ON pr.id = pi.product_id
                   WHERE pi.purchase_id = $1""", body.purchase_id)}
            given = {str(r["purchase_item_id"]): r for r in await conn.fetch(
                """SELECT ni.purchase_item_id, SUM(ni.quantity) AS qty, SUM(ni.taxable_value) AS taxable, SUM(ni.gst_amount) AS gst
                   FROM debit_note_items ni JOIN debit_notes n ON n.id = ni.debit_note_id
                   WHERE n.purchase_id = $1 AND n.status = 'ACTIVE' GROUP BY ni.purchase_item_id""", body.purchase_id)}

            out_items, unit_ids = [], []
            for ln in body.items:
                src = lines.get(ln.item_id)
                if src is None:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A line is not part of this purchase")
                label = src["product_name"]
                prev = given.get(ln.item_id)
                remaining = src["quantity"] - (prev["qty"] if prev else Decimal("0"))
                check_quantity(ln.quantity, remaining, label)
                line_taxable = round2(src["line_total"] - src["gst_amount"])
                taxable = share(line_taxable, src["quantity"], ln.quantity, remaining, prev["taxable"] if prev else ZERO)
                gst = share(src["gst_amount"], src["quantity"], ln.quantity, remaining, prev["gst"] if prev else ZERO)

                stock = await conn.fetchval("SELECT current_stock FROM products WHERE id = $1 FOR UPDATE", src["product_id"])
                if stock < ln.quantity:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                        detail=f"{label}: only {stock.normalize():f} in stock, so {ln.quantity.normalize():f} can't go back")

                barcodes = list(ln.barcodes or [])
                tracked = await conn.fetchval("SELECT COUNT(*) FROM part_warranties WHERE purchase_item_id = $1", ln.item_id)
                if tracked:
                    if ln.quantity != ln.quantity.to_integral_value() or len(barcodes) != int(ln.quantity):
                        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                            detail=f"{label}: give exactly {ln.quantity.normalize():f} serial number(s), one per unit")
                    if len(set(barcodes)) != len(barcodes):
                        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{label}: a serial number is repeated")
                    units = await conn.fetch(
                        "SELECT id, barcode, status FROM part_warranties WHERE barcode = ANY($1::text[]) AND purchase_item_id = $2 FOR UPDATE",
                        barcodes, ln.item_id)
                    found = {u["barcode"]: u for u in units}
                    missing = [b for b in barcodes if b not in found]
                    if missing:
                        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                            detail=f"{label}: not from this purchase line: {', '.join(missing)}")
                    gone = [b for b in barcodes if found[b]["status"] != "IN_STOCK"]
                    if gone:
                        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                            detail=f"{label}: no longer in stock (sold, claimed or already returned): {', '.join(gone)}")
                    unit_ids += [found[b]["id"] for b in barcodes]
                elif barcodes:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                        detail=f"{label}: this line has no serial-numbered units, so no serial numbers can be given")
                out_items.append({"src": src, "quantity": ln.quantity, "taxable": taxable, "gst": gst,
                                  "line_total": round2(taxable + gst), "barcodes": barcodes or None})

            subtotal = sum((i["taxable"] for i in out_items), ZERO)
            total_gst = sum((i["gst"] for i in out_items), ZERO)
            total = round2(subtotal + total_gst)
            applied, refund = settle(total, purchase["total_amount"], purchase["paid_amount"])
            note_date = body.note_date or today_local()
            note = await conn.fetchrow(
                """INSERT INTO debit_notes (debit_note_no, purchase_id, supplier_id, note_date, reason, subtotal, total_gst, total_amount,
                          applied_to_purchase, refund_amount, refund_mode, notes, created_by)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING *""",
                number, purchase["id"], purchase["supplier_id"], note_date, body.reason.strip(), subtotal, total_gst, total,
                applied, refund, body.refund_mode if refund > 0 else None, body.notes, owner["id"])
            for it in out_items:
                src = it["src"]
                await conn.execute(
                    """INSERT INTO debit_note_items (debit_note_id, purchase_item_id, product_id, quantity, unit_price, gst_rate,
                              taxable_value, gst_amount, line_total, barcodes) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                    note["id"], src["id"], src["product_id"], it["quantity"], src["unit_price"], src["gst_rate"],
                    it["taxable"], it["gst"], it["line_total"], it["barcodes"])
                await conn.execute(
                    """INSERT INTO stock_ledger (product_id, quantity, reference_type, reference_id, note, created_by)
                       VALUES ($1,$2,'RETURN_OUT',$3,$4,$5)""",
                    src["product_id"], -it["quantity"], note["id"], f"Debit note {number}", owner["id"])
            if unit_ids:
                await conn.execute("UPDATE part_warranties SET status = 'RETURNED', updated_at = now() WHERE id = ANY($1::uuid[])", unit_ids)

            await conn.execute(
                """INSERT INTO payments (purchase_id, amount, payment_mode, payment_date, notes, created_by, debit_note_id)
                   VALUES ($1,$2,'DEBIT_NOTE',$3,$4,$5,$6)""",
                purchase["id"], total, note_date, f"Debit note {number}", owner["id"], note["id"])
            if refund > 0:
                await conn.execute(
                    """INSERT INTO payments (purchase_id, amount, payment_mode, payment_date, notes, created_by, debit_note_id)
                       VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                    purchase["id"], -refund, body.refund_mode, note_date, f"Refund for debit note {number}", owner["id"], note["id"])
            new_paid = purchase["paid_amount"] + total - refund
            await conn.execute("UPDATE purchases SET paid_amount = $2, payment_status = $3 WHERE id = $1",
                               purchase["id"], new_paid, status_for(purchase["total_amount"], new_paid))
        note, items = await _load(conn, str(note["id"]))
    result = row_to_dict(note)
    result["items"] = rows_to_list(items)
    return result


@router.post("/{note_id}/cancel")
async def cancel_debit_note(note_id: str, body: CancelNoteRequest, owner=Depends(require_role("OWNER"))):
    """Cancels a debit note: the goods come back into stock, serial units go back to IN_STOCK, and the settlement /
    refund payment rows are reversed. Refused (409) when a returned unit has been changed since."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            note = await conn.fetchrow("SELECT * FROM debit_notes WHERE id = $1 FOR UPDATE", note_id)
            if note is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if note["status"] == "CANCELLED":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This debit note is already cancelled")
            purchase = await conn.fetchrow("SELECT * FROM purchases WHERE id = $1 FOR UPDATE", note["purchase_id"])
            items = await conn.fetch("SELECT * FROM debit_note_items WHERE debit_note_id = $1", note_id)

            unit_ids = []
            for it in items:
                if it["barcodes"]:
                    units = await conn.fetch(
                        "SELECT id, barcode, status FROM part_warranties WHERE barcode = ANY($1::text[]) AND purchase_item_id = $2 FOR UPDATE",
                        it["barcodes"], it["purchase_item_id"])
                    bad = [u["barcode"] for u in units if u["status"] != "RETURNED"]
                    if bad or len(units) != len(it["barcodes"]):
                        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                            detail=f"Serial(s) {', '.join(bad) or 'of this note'} changed since the return, so this debit note can't be cancelled")
                    unit_ids += [u["id"] for u in units]
                await conn.execute(
                    """INSERT INTO stock_ledger (product_id, quantity, reference_type, reference_id, note, created_by)
                       VALUES ($1,$2,'RETURN_OUT',$3,$4,$5)""",
                    it["product_id"], it["quantity"], note_id, f"Debit note {note['debit_note_no']} cancelled", owner["id"])
            if unit_ids:
                await conn.execute("UPDATE part_warranties SET status = 'IN_STOCK', updated_at = now() WHERE id = ANY($1::uuid[])", unit_ids)

            paid_back = ZERO
            for pay in await conn.fetch(
                    "SELECT * FROM payments WHERE debit_note_id = $1 AND reversed_at IS NULL AND reverses_payment_id IS NULL FOR UPDATE", note_id):
                await conn.execute(
                    """INSERT INTO payments (purchase_id, amount, payment_mode, payment_date, notes, created_by, reverses_payment_id, debit_note_id)
                       VALUES ($1,$2,$3,CURRENT_DATE,$4,$5,$6,$7)""",
                    note["purchase_id"], -pay["amount"], pay["payment_mode"],
                    f"Debit note {note['debit_note_no']} cancelled: {body.reason.strip()}"[:2000], owner["id"], pay["id"], note_id)
                await conn.execute("UPDATE payments SET reversed_at = now(), reversed_by = $2, reversal_reason = $3 WHERE id = $1",
                                   pay["id"], owner["id"], "Debit note cancelled")
                paid_back += pay["amount"]
            new_paid = purchase["paid_amount"] - paid_back
            await conn.execute("UPDATE purchases SET paid_amount = $2, payment_status = $3 WHERE id = $1",
                               purchase["id"], new_paid, status_for(purchase["total_amount"], new_paid))
            await conn.execute(
                "UPDATE debit_notes SET status = 'CANCELLED', cancelled_at = now(), cancel_reason = $2, cancelled_by = $3 WHERE id = $1",
                note_id, body.reason.strip(), owner["id"])
        note, items = await _load(conn, note_id)
    result = row_to_dict(note)
    result["items"] = rows_to_list(items)
    return result
