from decimal import Decimal

from fastapi import HTTPException, status

from app.utils.gst import round2


def check_not_overpaid(amount: Decimal, total, paid, what: str) -> None:
    """Refuse a payment bigger than what's still owed on a document (call with the row locked, so two
    simultaneous payments can't both squeeze under the limit). `what` is "invoice" / "purchase"."""
    outstanding = round2(total) - round2(paid)
    if outstanding <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"This {what} is already fully paid")
    if amount > outstanding:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Payment of Rs. {amount:.2f} is more than the outstanding balance of Rs. {outstanding:.2f} on this {what}",
        )


# ---------------------------------------------------------------------------------------------- reverse / edit
# One implementation for invoices and purchases. `table` / `fk` are fixed strings chosen by the two routers, never user input.
_DOCS = {"invoice": ("invoices", "invoice_id", "invoice_no"), "purchase": ("purchases", "purchase_id", "purchase_no")}


async def _locked_payment(conn, what: str, doc_id: str, payment_id: str):
    """Locks the document and the payment (in that order, the same as adding a payment) and runs the checks both
    reversing and editing share. Returns (document row, payment row)."""
    table, fk, number_col = _DOCS[what]
    doc = await conn.fetchrow(f"SELECT id, status, total_amount, paid_amount, {number_col} AS doc_no FROM {table} WHERE id = $1 FOR UPDATE", doc_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    payment = await conn.fetchrow(f"SELECT * FROM payments WHERE id = $1 AND {fk} = $2 FOR UPDATE", payment_id, doc_id)
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    if doc["status"] == "CANCELLED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"This {what} is cancelled — its payments can't be changed")
    if payment["credit_note_id"] or payment["debit_note_id"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This entry was booked by a credit / debit note. Cancel that note instead")
    if payment["amount"] <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This entry is a refund / reversal, not a payment, so it can't be changed")
    if payment["reversed_at"] is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This payment was already reversed")
    return doc, payment


async def reverse_payment(conn, what: str, doc_id: str, payment_id: str, reason: str, user_id) -> dict:
    """Undoes a payment that was entered wrongly. Nothing is deleted: a negative payment pointing at the original is
    added (so the history still adds up to the amount really received), the original is marked reversed with the
    reason, and the document's paid amount / status are recalculated. To correct an amount, reverse and record again.
    Returns {"document": <updated row>, "reversal_id": ...}."""
    table, fk, _ = _DOCS[what]
    async with conn.transaction():
        doc, payment = await _locked_payment(conn, what, doc_id, payment_id)
        amount = payment["amount"]
        reversal_id = await conn.fetchval(
            f"""INSERT INTO payments ({fk}, amount, payment_mode, payment_date, reference_no, notes, created_by, reverses_payment_id)
                VALUES ($1, $2, $3, CURRENT_DATE, $4, $5, $6, $7) RETURNING id""",
            doc_id, -amount, payment["payment_mode"], payment["reference_no"],
            f"Reversal of the Rs. {amount:.2f} payment dated {payment['payment_date']:%d/%m/%Y}: {reason}"[:2000],
            user_id, payment["id"],
        )
        await conn.execute(
            "UPDATE payments SET reversed_at = now(), reversed_by = $2, reversal_reason = $3 WHERE id = $1",
            payment["id"], user_id, reason,
        )
        row = await conn.fetchrow(
            f"""UPDATE {table} SET paid_amount = paid_amount - $1,
                       payment_status = CASE WHEN paid_amount - $1 >= total_amount THEN 'PAID'
                                              WHEN paid_amount - $1 > 0 THEN 'PARTIAL'
                                              ELSE 'UNPAID' END
                WHERE id = $2 RETURNING *""",
            amount, doc_id,
        )
    return {"document": row, "reversal_id": reversal_id}


async def edit_payment(conn, what: str, doc_id: str, payment_id: str, fields: dict):
    """Fixes the mode / date / reference / notes of a payment (only the keys present in `fields` change)."""
    sets, args = [], []
    for column in ("payment_mode", "payment_date", "reference_no", "notes"):
        if column in fields:
            args.append(fields[column])
            sets.append(f"{column} = ${len(args)}")
    async with conn.transaction():
        _, payment = await _locked_payment(conn, what, doc_id, payment_id)
        args.append(payment["id"])
        return await conn.fetchrow(
            f"UPDATE payments SET {', '.join(sets)} WHERE id = ${len(args)} RETURNING *", *args
        )
