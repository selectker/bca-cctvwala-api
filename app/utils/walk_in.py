"""The shop's single "Walk-in Customer": counter sales that don't need a customer record are billed to it.

It is a normal customers row flagged is_walk_in, created the first time anyone asks for it and kept on the shop's own
state (so a walk-in sale always splits into CGST + SGST). It can't be edited, deleted or given a balance; the real
buyer's name / phone, when the shop wants them, go on the invoice itself (invoices.buyer_name / buyer_phone)."""
from app.utils.rows import row_to_dict

WALK_IN_NAME = "Walk-in Customer"


async def get_or_create_walk_in(conn) -> dict:
    store = await conn.fetchrow("SELECT state, state_code FROM store_profile WHERE id = 1")
    state, state_code = (store["state"], store["state_code"]) if store else (None, None)
    row = await conn.fetchrow("SELECT * FROM customers WHERE is_walk_in")
    if row is None:
        # The unique index makes a race between two first calls harmless: the loser inserts nothing and re-reads.
        await conn.execute(
            """INSERT INTO customers (name, customer_type, state, state_code, opening_balance, is_walk_in)
               VALUES ($1, 'B2C', $2, $3, 0, TRUE) ON CONFLICT DO NOTHING""",
            WALK_IN_NAME, state, state_code,
        )
        row = await conn.fetchrow("SELECT * FROM customers WHERE is_walk_in")
    elif row["state_code"] != state_code or row["state"] != state or not row["is_active"]:
        row = await conn.fetchrow(
            "UPDATE customers SET state = $1, state_code = $2, is_active = TRUE WHERE is_walk_in RETURNING *", state, state_code)
    return row_to_dict(row)
