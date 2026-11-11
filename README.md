# BCAcctvWala API (Python / FastAPI) — v1.38.2

This is a drop-in replacement for the Node/Express backend — **same
PostgreSQL schema, same JSON request/response shapes**. Since v1.17.0 the
paths are under `/api/v1` (see section 2p); the old `/api/...` paths still
answer, so an Android app that is already installed keeps working.

`GET /api/v1/version` reports the running name/version; `GET /health` only says `ok` (no version).


Async throughout (FastAPI + asyncpg), same GST logic, same document numbering,
same invoice PDF endpoint (now rendered with reportlab instead of pdfkit).


## 2zzzzzzzzzzzzzzzzzzzzzz. Customer and supplier ledgers (v1.38.2) — no migration

`GET /customers/{id}/ledger` and `GET /suppliers/{id}/ledger` (OWNER; optional `from` / `to`, YYYY-MM-DD) return every transaction in date order with a running balance: `{kind, party, opening_balance, entries[{date,type,ref_no,particulars,doc_kind,doc_id,debit,credit,balance}], total_debit, total_credit, closing_balance}`. `/ledger/pdf` on each gives the A4 statement.
* Customer: invoices debit; receipts and credit notes credit; refunds paid and reversed receipts debit. Positive balance = the customer owes you (Dr). The party's opening balance starts it.
* Supplier: purchases credit; payments and debit notes debit; refunds received and reversed payments credit. Positive balance = you owe the supplier (Cr).
* With `from`, what stood before it becomes the opening balance. Cancelled documents are left out; the closing balance equals `/balance` (outstanding / payable) when no `to` is given.

## 2zzzzzzzzzzzzzzzzzzzzz. Cost on custom lines (v1.38.1) — apply migration 030 (`alembic upgrade head`)

A custom invoice line (no product, e.g. labour or a resold item) can carry an optional `cost_price` (per unit, before GST; `>= 0`). The profit-loss report costs such a line at `cost_price x quantity` (nothing when it is left out), so profit on it is sale - cost. Ignored when a product is picked (the product's purchase price is used). Invoice detail returns `cost_price` on each line. Migration `db/migrations/030_custom_line_cost.sql` (Alembic 0026).

## 2zzzzzzzzzzzzzzzzzzzz. Credit notes, debit notes, kept supplier invoice (v1.38.0) — apply migration 029 (`alembic upgrade head`)

* **Credit note (sales return)** — `POST /credit-notes` (OWNER): `{invoice_id, reason, items:[{item_id, quantity, barcodes?}], restock=true, refund_mode="CASH", note_date?, notes?}`.
  `item_id` is the **invoice line**. Number series `CN/…`; GST is the invoice's own split (CGST+SGST / IGST), whole-rupee rounding like an invoice.
  Quantity can't exceed what is left on the line (earlier live notes count; the last return takes the exact remainder, so no paisa drift, and all
  notes together never credit more than the invoice). Serial-numbered lines need one serial per unit, each a unit **sold on that line** (400/409 otherwise),
  and none may have an open service ticket (409). `restock=true`: stock +qty (ledger `RETURN_IN`), units back to IN_STOCK with their sale / warranty
  cleared; `restock=false` (damaged): no stock, units SCRAPPED.
* **Settlement** goes through the invoice's payment rows so receivables need no special case: a `CREDIT_NOTE` row of the note value is booked; if the note is more than
  what the customer still owes, the difference is paid back as a **negative refund row** in `refund_mode` (`applied_to_invoice` / `refund_amount` on the note).
  Those rows can't be edited or reversed by hand (409): cancel the note.
* `POST /credit-notes/{id}/cancel {reason}` undoes all of it (stock out again, units SOLD with their warranty restored, payment rows reversed); refused (409) if the goods
  were sold again / a unit moved on. `GET /credit-notes` (`status`, `customer_id`, `invoice_id`, `search`), `GET /credit-notes/{id}`, `GET /credit-notes/{id}/pdf`.
* **Debit note (purchase return)** — `POST /debit-notes` (OWNER): `{purchase_id, reason, items:[{item_id, quantity, barcodes?}], refund_mode, …}`; series `DN/…`. Stock −qty
  (`RETURN_OUT`, 400 if not enough in stock), serial units must be IN_STOCK from that purchase line and become RETURNED. A `DEBIT_NOTE` payment row reduces what you owe; any excess is
  a negative refund row (the supplier paying back). `POST /debit-notes/{id}/cancel`, `GET /debit-notes[/{id}[/pdf]]` as above.
* An invoice with a live credit note can't be edited or cancelled (409; cancel the note first), likewise a purchase with a debit note. `GET /invoices/{id}` now has `credit_notes`,
  per line `returned_quantity` and `sold_barcodes`; `GET /purchases/{id}` has `debit_notes`, per line `returned_quantity`, `in_stock_barcodes`, and `has_attachment`.
* **Reports are net of notes** (by the note's date): `gst-summary` (`credit_note_count`, `gross_*`), `gst-input-credit` (debit notes taken off), `gstr3b` (via both), `profit-loss`
  (sales and, for restocked goods, cost reduced; `credit_note_taxable`), `sales-summary`. **GSTR-1** gets a `cdn` section (one row per note per rate, registered flag), `credit_note_totals`,
  `net_totals`, credit notes in the documents summary and an HSN summary net of returns; CSV section `cdn` / `cdn.csv` in the zip. `/reports/gst-summary` totals now equal GSTR-1 `net_totals`.
* **Supplier invoice kept with the purchase** — `POST /purchases/{id}/attachment` (multipart field `file`: PDF or PNG/JPEG/WebP, up to `MAX_ATTACHMENT_MB`=8; photos are straightened and
  shrunk, PDFs kept as they are), `GET` (file) and `DELETE` (OWNER). Stored privately in R2.
* Migration `db/migrations/029_credit_debit_notes.sql` (Alembic 0025). Tests: `tests/test_credit_debit_notes.py` (credit/debit flows, serials, refunds, cancel, reports, attachment, walk-in quotation).
  `tests/test_api.py` needs an empty database (run it on its own).

## 2zzzzzzzzzzzzzzzzzzz. Walk-in customer (v1.37.0) — apply migration 028 (`alembic upgrade head`)

* **One built-in "Walk-in Customer"** for counter sales. `GET /customers/walk-in` returns it (and creates it on first use, on the
  shop's own state so the tax is always CGST + SGST). It can't be edited or deleted (409), has no balance, and is **left out of
  `GET /customers`** so the app can pin it at the top of its picker. Bill to it like any customer: `customer_id` = its id.
* **Who actually bought (optional).** `POST /invoices` takes `buyer_name` and `buyer_phone`, and `PUT /invoices/{id}` can change
  or clear them (left out = unchanged). They are only accepted for the walk-in customer (422 otherwise). The invoice PDF prints
  them in the Bill To box instead of "Walk-in Customer"; invoices are searchable by them. Invoice JSON now carries
  `buyer_name`, `buyer_phone` and `customer_is_walk_in`.
* Unpaid walk-in invoices still show in the receivables reports, under "Walk-in Customer": hiding them would hide real money.
* Migration: `db/migrations/028_walk_in_customer.sql` (Alembic 0024). New tests: `tests/test_walk_in_customer.py`.

## 2zzzzzzzzzzzzzzzzzz. No signature on the purchase record (v1.36.1) — no migration, no app change

* `GET /purchases/{id}/pdf` no longer prints the shop's signature image or the "Authorized Signatory" caption: a purchase record is an
  internal document, not something the shop issues, so it is not signed on the shop's behalf. The "For <shop>:" box stays, blank.
  Invoices, quotations, vouchers, warranty cards and AMC contracts still carry the signature.
* Internally: `Spec.show_signature` (default on) in `app/utils/invoice_pdf.py`; the purchase record turns it off.

## 2zzzzzzzzzzzzzzzzz. Every PDF follows the invoice's header and page pattern (v1.36.0) — no migration, no app change

* **One look for all documents.** The quotation, purchase record, service job card, warranty card, AMC contract and the payment
  receipt / voucher now print exactly like the invoice: centred title, the boxed shop header (logo, address, phone, email, GSTIN,
  state), grey-barred boxes that run edge to edge, rupee amounts with Indian digit grouping, dates as `dd-mm-yyyy`, the
  **"For <shop>:" signatory cell** (with the saved signature) and the grey "computer-generated" note at the bottom.
* **Long documents continue the same way.** Later pages carry the title and a right-aligned `<No label>: <number>  (continued)`;
  the page ends with "Continued on next page..."; the shop box, totals and signatures are printed once.
* **Quotation** (`GET /quotations/{id}/pdf`) and **purchase record** (`GET /purchases/{id}/pdf`) now use the invoice's item table, HSN-wise
  tax summary, totals and amount in words. The quotation shows the shop's bank details and notes; the purchase shows Paid / Balance.
  CGST + SGST or IGST is chosen from the customer's / supplier's state against the shop's. Both now carry the logo and signature,
  and the party box shows phone, state and GSTIN.
* **Job card, warranty card, AMC contract, receipt, voucher** use the same boxes: *Customer* + *<Document> Details* side by side,
  then sections (Site Address, Problem, Unit, Charges, History, Terms, Contract Value, Visit Schedule, ...) each under a grey bar.
  The job card ends with **Customer / Technician** signature cells; the others with the shop's signatory cell. The payment voucher
  now also carries the logo and signature. Field staff still never see the money on a job card.
* Money on the job card, AMC contract, receipt and voucher now uses the rupee sign (`₹ 1,770.00`) instead of `Rs. 1770.00`.
* Code: `app/utils/invoice_pdf.py` (item-table documents, `Spec`, `begin_page`) and the new `app/utils/doc_pdf.py` (page framework
  for the other documents); `app/utils/pdf.py` holds the builders. Tests: `tests/test_pdf_layout.py`.
* To apply: unzip, `./deploy/deploy.sh --yes`.

## 2zzzzzzzzzzzzzzzz. Invoice PDF redesigned to the boxed "Vyapar" layout (v1.35.0) — no migration, no app change

* `GET /api/v1/invoices/{id}/pdf` now prints: title, shop box (logo, address, phone, email, GSTIN, state), **Bill To**
  (customer name, address, contact number, state, GSTIN; **Ship To** only when it differs) beside **Invoice Details**
  (no., date, place of supply, due date while something is owed, service refs), a full-height item table with the
  Total row under it, an **HSN-wise tax summary** (IGST, or CGST + SGST), Sub Total / Total / amount in words /
  Received / Balance, bank details with the pay-by-QR code, and the **Authorized Signatory** cell.
* Amounts print with the rupee sign and Indian digit grouping. Helvetica has no rupee sign, so the fonts
  `app/assets/fonts/DejaVuSansCondensed*.ttf` are shipped (licence next to them); without them the PDF falls back to "Rs.".
* A Discount column appears only when an item has a discount; "You Saved" shows in the totals.
* Long invoices continue on more pages; totals, tax summary, bank and signature are printed once, on the last page.
* The buyer's phone/state are read from the customer record when the PDF is made.

## 2zzzzzzzzzzzzzzz. Automatic midnight backup, server time in IST (v1.34.0) — no migration

* **Daily backup, built in.** The API takes a full backup into R2 every day at `BACKUP_SCHEDULE_TIME` (default `00:00`),
  read in `APP_TIMEZONE` (`Asia/Kolkata`) — so **midnight IST**. It is the same backup as `POST /admin/backup/cloud`
  (same pg_dump, encryption when `BACKUP_ENCRYPTION_KEY` is set, `KEEP_CLOUD_BACKUPS` pruning). No cron job, no stored
  admin password. **Remove the old cron line** that runs `scripts/nightly-backup.sh`, or you will get two backups a day.
  * With `API_WORKERS=2` only one worker runs it (a Postgres advisory lock, plus a check that no scheduled backup was
    logged in the last 6 hours).
  * A failed backup is retried `BACKUP_SCHEDULE_RETRIES` (3) more times, `BACKUP_SCHEDULE_RETRY_MINUTES` (10) apart.
    Success and final failure are written to the platform admin's audit log (`GET /admin/audit-logs`, actor
    "Scheduler", action `BACKUP` / `BACKUP_FAILED`).
  * `BACKUP_SCHEDULE_ENABLED=false` turns it off. If the server is down at midnight that day's backup is not made up later.
* **IST everywhere.** The container runs with `TZ=Asia/Kolkata` (Dockerfile), `deploy.sh` sets the EC2 host to
  `Asia/Kolkata`, `GET /health` shows the time with `+05:30`, and backup / safety-snapshot **file names now carry IST**
  (`bcacctvwala-backup-20261109-000002.dump` = 00:00:02 IST on 9 Nov). Older UTC-named backups still list and restore.
  Stored timestamps (`created_at` etc.) stay absolute (`timestamptz`); only how they are shown changed.
  On a server **without Docker**: `sudo timedatectl set-timezone Asia/Kolkata`, then restart the API.
* To apply: `git pull` / unzip, `./deploy/deploy.sh --yes` (rebuilds the image), delete the old cron entry. Check with
  `docker logs bcacctvwala-api | grep "Daily backup scheduled"` → `Daily backup scheduled for 00:00 Asia/Kolkata.`

## 2zzzzzzzzzzzzzz. Edit an invoice, delete a cancelled one (v1.33.0) — no migration

* **`PUT /api/v1/invoices/{id}` (OWNER)** corrects a live invoice in place: same id, invoice number and customer.
  `items` replaces *all* the lines (send every line that should stay); stock, serial-number warranties, GST, discounts
  and totals are redone in one transaction, exactly as when making an invoice. Optional: `invoice_date` (not in the
  future), `notes`, `billing_address`, `shipping_address`, `payment_terms_days` / `due_date`, the discount fields,
  `allow_negative_stock`, `allow_over_credit_limit`. Anything left out is unchanged; `null` clears an address / notes /
  due date. Returns the invoice like `GET /invoices/{id}` plus `warnings`.
  * Stock goes back with a `RETURN_IN` row and the new lines go out again, so the stock history stays. Cancelling an
    edited invoice now puts back the *net* quantity still out (`SALE` rows minus `RETURN_IN` rows).
  * Payments received stay on the invoice and the status is recalculated. An edit that would bring the total below what
    was already received is refused (409): reverse or correct a payment first.
  * Refused (409): a cancelled invoice; a unit with a warranty claim / replacement; taking a serial number off an invoice
    when that unit has service tickets (cancel the invoice instead); not enough stock; going over the customer's credit
    limit (only checked when the invoice gets bigger).
* **`DELETE /api/v1/invoices/{id}` (OWNER)** permanently deletes an invoice that is already **cancelled** (409 on a live
  one). The invoice, its lines and its payment rows are removed; the stock-ledger rows stay (they net to zero) and the
  audit log records who deleted which invoice number. Deleting the *newest* invoice frees its number for the next one.
* `GET /invoices/{id}` lines now also carry `customer_warranty_months`, so an edit screen can pre-fill them.

## 2zzzzzzzzzzzzz. Faster loading (v1.32.1) — apply migration 027 (`alembic upgrade head`)

* **PDFs no longer freeze the API.** Every PDF (invoice, quotation, purchase, receipt, warranty card, AMC, ticket) was
  built on the event loop, so one PDF stopped every other request on that worker until it was done. They now run in a thread.
* **Ticket list:** it built the full 7-join row (and the sign-off subquery) for *every* matching ticket just to count
  them. It now filters / sorts / pages on the bare table and builds the full row only for the page. New index
  `idx_service_tickets_created` serves the newest-first sort. Same JSON, same `X-Total-Count`.
* **Compression:** `deploy/nginx-bcacctvwala.conf` now gzips JSON (copy it to the server and `sudo nginx -s reload`).

## 2zzzzzzzzzzzz. Stronger owner password rule (v1.32.0)

`POST /profile/change-password` now rejects a new password (400, with a readable `detail`) unless it has at least one
letter and one number and more than three distinct characters (`aaaa1111` is refused). The 8-character minimum is
unchanged (422). Existing passwords keep working; only a newly chosen one is checked. The Android app (1.45.0)
shows the same rules live while typing.

## Why this exists

Functionally there was no reason the Node version needed to be Node — this
is the same app in FastAPI for anyone more comfortable maintaining Python,
or who wants FastAPI's automatic interactive docs (`/docs`) and request
validation for free.

## 1. Local setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in DB_HOST etc. (and SETUP_TOKEN for first-run setup)
uvicorn app.main:app --reload --port 4000
```

With `ENABLE_DOCS=true` in `.env`, interactive API docs (Swagger UI) are at `http://localhost:4000/docs` —
useful for testing endpoints by hand. They are off by default (see v1.13.1) because they describe every endpoint
to anyone who can reach the server.

## 2. Database

Identical to the Node backend — same file, same steps:

```bash
psql "host=$DB_HOST port=5432 dbname=gst_billing user=postgres password=$DB_PASSWORD sslmode=verify-full sslrootcert=/etc/ssl/certs/rds-global-bundle.pem" -f db/schema.sql
```

See the Node backend's README for the full RDS/EC2 provisioning walkthrough —
it applies unchanged here. The only difference is how you run the app process
on EC2 (below).

**Already have a database running?** `schema.sql` is only for a brand-new
database (it fails if the tables already exist). To add the new part-warranty
tracking (barcode + valid-until dates) to an existing database, run the
migration instead:

```bash
psql "host=$DB_HOST port=5432 dbname=gst_billing user=postgres password=$DB_PASSWORD sslmode=verify-full sslrootcert=/etc/ssl/certs/rds-global-bundle.pem" -f db/migrations/001_part_warranties.sql
```

**Upgrading an existing v1.1 database to v1.2:** run the new migration (safe to re-run):

```bash
psql "host=$DB_HOST port=5432 dbname=gst_billing user=postgres password=$DB_PASSWORD sslmode=verify-full sslrootcert=/etc/ssl/certs/rds-global-bundle.pem" -f db/migrations/004_gaps.sql
```

It adds `users.password_changed_at` (password resets sign the user out everywhere) and
`quotation_items.customer_warranty_months` (warranty length chosen at quote time).

## 2b. What's new in v1.2

- **Pagination**: every list endpoint takes `limit` (default 100, max 500) and `offset`; the JSON body is still a plain
  array and the total row count is in the `X-Total-Count` response header.
  This includes `/reports/low-stock` (also sends `X-Out-Of-Stock-Count`), `/reports/outstanding-receivables` and
  `/reports/outstanding-payables` (both also send `X-Total-Outstanding`, the sum over all pages), `/amc/expiring`, and
  the customer/supplier `/history` endpoints (these page the invoices / purchases; each carries its items, and
  `X-Total-Count` is the invoice / purchase count). Only small pick-lists (`/auth/team`, categories) stay unpaged.
- **Filters**: invoices/purchases (`from`, `to`, `payment_status`, `search`), quotations (`status`, `search`),
  AMC (`status`, `expiring_within_days`, `search`), products (`low_stock`, `search`).
- **Users**: `GET /auth/users`, `PUT /auth/users/{id}` (name / role / `is_active`),
  `GET /auth/team` (id/name/role and `has_image` of active users, any login),
  `GET /auth/bootstrap-status` (public: `{needs_setup}`). Deactivated users and users whose password was reset
  are rejected immediately (the token is checked against the database on every request).
- **Store profile**: `GET /store-profile`, `PUT /store-profile` (OWNER) — invoice header, GSTIN, bank details, state code
- **Shop logo**: `POST /store-profile/logo` (OWNER, multipart field `logo`), `GET /store-profile/logo` (any signed-in user), `DELETE /store-profile/logo` (OWNER) — printed in the header of invoice and quotation PDFs (see §2n)
- **Invoice signature**: `PUT /store-profile/signature` (OWNER, JSON `signature_base64`), `GET /store-profile/signature` (any signed-in user), `DELETE /store-profile/signature` (OWNER) — printed above "Authorised Signatory" on invoice PDFs (see §2o)
  (drives CGST+SGST vs IGST) and the invoice / quotation number prefixes.
- **Payments**: `GET /invoices/{id}/payments`, `GET /purchases/{id}/payments`; payment modes are
  `CASH, UPI, BANK, CHEQUE, CARD`; amount must be > 0.
- **Quotation -> invoice**: `POST /invoices/from-quotation/{id}` accepts an optional body
  `{"items":[{"quotation_item_id","barcodes":[...],"customer_warranty_months"}]}` so serialized lines are linked to
  warranties. A quotation can only be converted once. The response has `warnings` for serialized lines sold without serials.
- **Warranties**: `PUT /warranties/{id}` can fix barcode/product/supplier + customer warranty and set `RETURNED` /
  `SCRAPPED` (or back to `IN_STOCK`), keeping the stock ledger in step. Responses include the replacement trail
  (`replaces_*` / `replaced_by_*`). `GET /warranties/expiring` has `include_expired=false` for reminders.
- **Balances**: customer list rows carry `outstanding`, supplier rows carry `payable`; the `/balance` endpoints also
  return the opening balance and the unpaid-document part.
- **Products**: `PUT` may update `purchase_price`; duplicate SKU returns 409.

## 2c. Platform admin, backup and restore (v1.3)

The **first account ever registered** is the *platform admin*
(`users.is_platform_admin`, enforced unique by a database index). It is the only account that can use `/api/v1/admin/*`.
It is protected: no other user can edit, deactivate or reset it, and it can't be demoted.
Upgrading an existing install? Run `db/migrations/005_platform_admin.sql` — the earliest-created account becomes the
platform admin (idempotent). To hand the role to someone else you must change it in SQL on purpose.

## Sign-in brute-force protection

Three layers protect the credential checks (`/auth/login`, `/auth/login-pin`, `/auth/change-pin`,
`/profile/change-password`, and the two bootstrap endpoints):

1. **Per-IP rate limit** — `LOGIN_RATE_LIMIT` (default `10/minute`), one shared bucket per client IP across all
   of those endpoints. Over the limit -> `429` `{"error": "Rate limit exceeded: ..."}`. The counter is in memory
   per worker process, so each worker gets `1/API_WORKERS` of the limit (set `API_WORKERS` to your `--workers`
   value) and the total stays at the configured number. The client IP comes from `app/client_ip.py`
   (`TRUSTED_PROXIES`, see section 2q), not from uvicorn's `--proxy-headers`.
2. **Lock per account AND client address** (`/auth/login`, `/auth/login-pin`) — after `LOGIN_MAX_FAILURES` (default 5)
   wrong credentials in a row for one account **from one IP address**, that address gets `429` + a `Retry-After`
   header for `LOGIN_LOCKOUT_MINUTES` (default 15), and **even the correct password / PIN is refused** from it while
   locked. A successful sign-in from that address resets its count. Because the lock belongs to the guesser's
   address, **knowing the owner's phone number no longer lets anyone lock the owner out** — the owner signing in
   from their own phone is not affected.
3. **Account-wide ceiling** — guessing spread over many addresses is stopped too: once an account has had
   `LOGIN_ACCOUNT_FAILURE_CEILING` (default 30) failures from all addresses together inside the window, addresses
   that have not signed in to it successfully within `LOGIN_KNOWN_IP_DAYS` (default 30) are refused until the window
   clears. The owner's usual addresses keep working; the owner on a brand-new address (e.g. a new mobile network)
   may have to wait out the window while such an attack is going on.

4. **Daily ceiling for staff PINs** — a 6-digit PIN has only a million possibilities, so guessing it slowly from many
   addresses must not go on for months. Once a staff / technician account has had `LOGIN_PIN_DAILY_FAILURE_CEILING`
   (default 100, `0` = off) failures in the last 24 hours from all addresses together, addresses that never signed in
   to it successfully are refused until the 24 hours roll on (`Retry-After` says how long). At 100 a day a full
   PIN search takes decades. The account's usual addresses keep working, and owner / admin passwords are not affected.

An unknown phone number / account ID is locked exactly like a real one, and a failed sign-in takes the same time
whether or not the account exists (an unknown phone is checked against a stand-in bcrypt hash), so neither the
response nor its timing shows which phones are registered. The counters live in the small `login_failures` table
(migration 013): one row per wrong credential holding the account id (or, for an unknown phone / ID, a keyed hash
of what was typed — never the text itself), the client address and the time. Rows are deleted after 25 hours,
a successful sign-in clears that address's count, and the table is neither backed up nor restored. Attempts made
while locked are not counted, so the lock can't be extended indefinitely.
Tests: `tests/test_login_protection.py`.

## 2d. Platform admin / owner split, staff PIN login (v1.6)

Bootstrap is now two separate steps, and each can only ever run once (both enforced by a database
unique index, not just app logic):

1. `POST /api/v1/auth/bootstrap-admin` — the very first account. Role `ADMIN`, `is_platform_admin = TRUE`.
   No business role, no business setup required. Blocked the instant any account exists.
2. `POST /api/v1/auth/bootstrap-owner` — the second account. Role `OWNER`. Blocked until the platform
   admin exists, and blocked forever after the first owner is created
   (`uq_users_single_owner` — at most one `OWNER` row). The client should send the person straight
   into business setup (`PUT /api/v1/store-profile`) right after this call succeeds.

Since the platform admin's role is `ADMIN`, not `OWNER`, it is automatically excluded from every
`OWNER`-only endpoint (staff management, store profile) by the existing `require_role("OWNER")`
checks — it genuinely has no functionality besides `/api/v1/admin/*` (backup/restore).

Staff and technician accounts have **no self-signup and no password**. The owner creates them
(`POST /api/v1/auth/users`, `role` must be `STAFF` or `TECHNICIAN`); the server generates a one-time
6-digit PIN, hashed into the same `password_hash` column, and returns the plaintext PIN exactly
once in that response — the API never exposes it again. They then log in with
`POST /api/v1/auth/login-pin` (`phone` = the mobile number the login was created with, or `account_id` = their user ID, plus `pin`), and `must_change_pin` in that
response tells the client to force the change screen. `POST /api/v1/auth/change-pin` is self-service
and requires the current PIN; if it's forgotten, only the owner can recover access, via
`POST /api/v1/auth/users/{id}/reset-pin`. `/api/v1/auth/login` (phone + password) now only accepts
`OWNER`/`ADMIN` accounts. `PUT /api/v1/auth/users/{id}` only ever touches `STAFF`/`TECHNICIAN` rows —
the platform admin and owner rows can't be modified through it. (The owner changes their own password with
`POST /api/v1/profile/change-password`, which needs the current password; v1.13.1 removed the old
`POST /auth/users/{id}/reset-password`, which didn't.)

Upgrading an existing install? Run `db/migrations/007_staff_pin_auth.sql` (or `alembic upgrade
head`). Any pre-existing STAFF/TECHNICIAN accounts keep their old password until an owner runs
reset-pin on them to switch them over.

| Endpoint | What it does |
|---|---|
| `GET /api/v1/admin/summary` | row counts per table, database size, safety snapshots |
| `GET /api/v1/admin/backup` | downloads a complete, consistent `pg_dump` backup (`.dump`, or `.dump.enc` if `BACKUP_ENCRYPTION_KEY` is set) of the **whole database** |
| `POST /api/v1/admin/restore?confirm=RESTORE` | body = the backup file; replaces **all** data |
| `GET /api/v1/admin/snapshots`, `POST /api/v1/admin/snapshots/{name}/restore?confirm=RESTORE` | undo a restore |
| `GET /api/v1/auth/me` | current user, incl. `is_platform_admin` |

How it works (v1.14.0): backup runs `pg_dump --format=custom` and restore runs `pg_restore --clean --single-transaction`,
so a backup carries the schema (tables, indexes, triggers, functions), the data and the sequence values. See section 2v
for installing the tools and for restoring into a **fresh database**. A restore is validated first (a readable archive of
this app), then a **safety snapshot of the current data is written to R2** (`snapshots/`, last `KEEP_SNAPSHOTS`,
default 5), then everything is replaced in **one transaction** — it either fully succeeds or changes nothing.
Users/passwords/audit log come from the backup and **every sign-in ends**: sign in again after a restore.
Settings (`.env`): `KEEP_SNAPSHOTS`, `MAX_RESTORE_MB` (default 500), `BACKUP_TIMEOUT_SECONDS`, `PG_DUMP_PATH`,
`PG_RESTORE_PATH`; R2 settings: section 2n. Backups contain password hashes — store them securely.

## 2d. Performance fixes + Alembic (v1.4)

Three fixes, applied the same way regardless of how you manage schema changes:

- **Missing FK indexes** on `invoice_items`, `quotation_items`, `purchase_items`, `payments`,
  `part_warranties`, `amc_service_visits` — these were causing sequential scans on invoice/purchase/
  quotation detail lookups.
- **`products.current_stock`** is now a real column kept in sync by a trigger on `stock_ledger`,
  instead of `SUM(stock_ledger.quantity)` being recomputed on every products list/detail/report request.
- **Trigram (`pg_trgm`) indexes** on every column searched with `ILIKE '%...%'` (invoice/purchase/
  quotation/AMC numbers, customer/supplier name+phone, product name/SKU, warranty barcode), so search
  stays fast as tables grow.

**Option A — raw SQL** (same style as the migrations above, idempotent, safe to re-run):

```bash
psql "host=$DB_HOST port=5432 dbname=gst_billing user=postgres password=$DB_PASSWORD sslmode=verify-full sslrootcert=/etc/ssl/certs/rds-global-bundle.pem" -f db/migrations/006_performance_fixes.sql
```

**Option B — Alembic** (new in v1.4; this project has no ORM, so migrations are hand-written
`op.execute(...)` SQL rather than autogenerated):

```bash
pip install -r requirements.txt   # now includes alembic + SQLAlchemy
```

Alembic reads DB credentials from the same `.env` / `app/config.py` settings the API uses — nothing
to configure in `alembic.ini`.

- **Brand-new database:** `alembic upgrade head` runs revision `0001` (the full schema, equivalent to
  `schema.sql`) then `0002` (this performance migration).
- **Existing database already created via `schema.sql` / the numbered `db/migrations/*.sql` files:**
  tell Alembic the baseline is already applied, then apply only what's new:
  ```bash
  alembic stamp 0001
  alembic upgrade head    # applies 0002 only
  ```
- **Already ran `db/migrations/006_performance_fixes.sql` by hand?** Stamp past it instead of re-running:
  ```bash
  alembic stamp 0002
  ```

From here on, new schema changes should be added as new Alembic revisions
(`alembic revision -m "..."`, filled in by hand with `op.execute`) rather than new files in
`db/migrations/`, so there's one source of truth for what's applied and in what order.

## 2e. More query indexes (v1.7)

Migration 006 covered missing FK indexes and search. This pass covers the rest of the list/report
endpoints that were still doing full or filtered-but-unindexed scans:

- **Date-range + sort columns**: `purchases.purchase_date`, `quotations.quotation_date` — filtered
  with `>=`/`<=` and sorted on in `GET /purchases` and `GET /quotations`.
- **Status/payment_status columns**: `purchases.payment_status`, `invoices.payment_status`,
  `quotations.status` — equality filters on `GET /purchases`, `GET /invoices`, `GET /quotations`.
- **`idx_invoices_customer_date`** — composite `(customer_id, invoice_date DESC)` for a customer's
  invoice history (`GET /customers/{id}`), which filters and sorts in the same query.
- **`idx_amc_status_end_date`** — composite `(status, end_date)` for `GET /amc?status=&expiring_within_days=`.
- **Partial `_active_name` indexes** on `customers`, `suppliers`, `products` — every list endpoint
  filters `is_active = TRUE` then `ORDER BY name`.
- **`idx_products_low_stock_active`** — partial index on `products(current_stock)` whose predicate
  (`is_active = TRUE AND current_stock <= reorder_level`) matches the low-stock report and dashboard
  counter exactly, so those queries can be answered with an index-only scan.

**Option A — raw SQL:**

```bash
psql "host=$DB_HOST port=5432 dbname=gst_billing user=postgres password=$DB_PASSWORD sslmode=verify-full sslrootcert=/etc/ssl/certs/rds-global-bundle.pem" -f db/migrations/008_query_indexes.sql
```

**Option B — Alembic:**

```bash
alembic upgrade head    # applies 0004
```

Already ran `db/migrations/008_query_indexes.sql` by hand? Stamp past it instead of re-running:

```bash
alembic stamp 0004
```

## 2g. Reports improvements (v1.6)

Additive only — existing fields are unchanged.

- `GET /api/v1/reports/sales-summary` now returns **one row per day** in `[from, to]`; days with no
  invoices come back with `invoice_count: 0, total_sales: 0` (previously they were missing). It also
  returns 400 if `from` is after `to` or the range exceeds 10 years.
- `GET /api/v1/reports/gst-input-credit` now also returns `total_cgst`, `total_sgst`, `total_igst`.
  Purchases store a single `total_gst`, so the split is derived like sales: supplier in a different
  state than the store -> IGST, otherwise half CGST + half SGST (suppliers with no state code are
  treated as same-state).

## 2h. Service tickets (v1.8)

A customer's service request, tracked from the owner raising it to the owner closing it.

| Step | Who | Endpoint |
|---|---|---|
| Raise (type `IN_WARRANTY`, `PAID` or `AMC`) | OWNER only | `POST /api/v1/service-tickets`, or `POST /api/v1/amc/{id}/tickets` (see 2zzzzzzzz) |
| List / read (timeline + photos) | OWNER; STAFF / TECHNICIAN see only tickets assigned to them or to nobody | `GET /api/v1/service-tickets`, `GET /api/v1/service-tickets/{id}` |
| Edit type / charge / assignee / date | OWNER, until it is closed or billed | `PUT /api/v1/service-tickets/{id}` |
| Mark attended (**1-6 photos required**; the customer's sign-off optional, see 2zzzzzzzz) | STAFF / TECHNICIAN only (not the owner) | `POST /api/v1/service-tickets/{id}/attend` (multipart: `images`, `remarks`, sign-off fields) |
| Reschedule | STAFF / TECHNICIAN only | `POST /api/v1/service-tickets/{id}/reschedule` `{new_date, reason}` |
| Close | OWNER only, once attended | `POST /api/v1/service-tickets/{id}/close` `{note?}` |
| Cancel | OWNER only, before it is attended | `POST /api/v1/service-tickets/{id}/cancel` `{reason}` |
| Add forgotten photos / a note / the customer's sign-off after attending | STAFF / TECHNICIAN only, while `ATTENDED` | `POST /api/v1/service-tickets/{id}/addendum` (multipart: `images`, `remarks`, sign-off fields) |
| Send back to the technician | OWNER only, while `ATTENDED` and not billed | `POST /api/v1/service-tickets/{id}/reopen` `{reason, scheduled_date?}` |
| Completion photo | whoever can see the ticket | `GET /api/v1/service-tickets/{id}/images/{image_id}` |

Statuses: `OPEN -> RESCHEDULED (any number of times) -> ATTENDED -> CLOSED`, or `CANCELLED`; the owner can send an `ATTENDED` ticket back to `OPEN` (`/reopen`).
Every step is appended to a timeline (`events`: `CREATED, UPDATED, RESCHEDULED, ATTENDED, ADDENDUM, REOPENED, CLOSED, CANCELLED, BILLED, UNBILLED`,
each with who did it, when, the note and, for `ATTENDED`, its photos).

- Ticket numbers are `TKT/<financial year>/0001`, like the other documents.
- Acting on an unassigned ticket (attend / reschedule) assigns it to that technician.
- `warranty_barcode` (optional) links the ticket to a tracked unit. For `IN_WARRANTY` the unit must have been sold to
  that customer and its customer warranty must still be valid, otherwise the API says to raise it as a paid service.
- `charge_amount` / `gst_rate` (PAID only) are the owner's estimate; they pre-fill the invoice line and are never
  sent to STAFF / TECHNICIAN logins.
- **Billing**: `GET /api/v1/service-tickets?billable=true&customer_id=...` (OWNER) lists attended / closed `PAID` tickets
  not yet on an invoice. Send their ids as `service_ticket_ids` on `POST /api/v1/invoices` with `invoice_type: "SERVICE"`
  (OWNER only; the price lines are still in `items`). Each ticket can be billed once; `GET /api/v1/invoices/{id}` now
  lists the billed `service_tickets`.

**Where photos live.** Only in Cloudflare R2 (section 2n) under `service_tickets/<ticket>/<random>.<ext>`; nothing is
written to the API server's disk. Per-photo limit is `MAX_UPLOAD_MB` (default 10).

**What happens to an uploaded photo** (`app/utils/photo.py`). Every photo is decoded with Pillow and re-encoded as a new
JPEG (max 2048 px on the long side), so the stored file carries **no EXIF / GPS / XMP** (a phone photo otherwise gives away
the customer's site location). Rotation is applied first. Stored photos are always `image/jpeg`, `.jpg`. Photos are processed
one at a time (JPEGs are decoded at reduced size), and at most 2 photos are processed at once across the API, so memory per
request stays small. The upload to R2 happens **before** the database transaction opens: the API checks the ticket (no
lock), processes the photos, uploads them, and only then opens a short DB-only transaction that locks the ticket and writes
the rows. A slow R2 can no longer hold a pool connection or a row lock. If the transaction fails, the uploaded photos are
deleted again.

1. Photos are never part of the database backup (`/api/v1/admin/backup`); R2 keeps them safe on its own.
2. nginx rejects bodies over 1 MB by default, so add `client_max_body_size 30m;` to the server block that proxies the API.

Upgrading an existing install: `pip install -r requirements.txt` (adds `python-multipart`), then run
`db/migrations/009_service_tickets.sql` or `alembic upgrade head` (applies `0005`; `alembic stamp 0005` if you ran the .sql by hand).

## 2n. Cloudflare R2 private storage (v1.11)

Ticket photos, backups and restore safety snapshots are stored in a **private** Cloudflare R2 bucket only. R2 is
**required**: the API will not start without the R2 settings, and nothing is written to the server's disk (except
short-lived temp files while a backup is being built).

**One-time setup**
1. Cloudflare dashboard > R2 > create a bucket (e.g. `bcacctvwala`). Leave it private: **do not** add a Custom Domain and
   do not enable the `r2.dev` public URL.
2. R2 > Manage API tokens > create a token with *Object Read & Write* for that bucket; note the account id, key id, secret.
3. `.env`:
   ```
   R2_ACCOUNT_ID=...   R2_ACCESS_KEY_ID=...   R2_SECRET_ACCESS_KEY=...   R2_BUCKET=bcacctvwala
   ```
   (Optional `R2_BACKUP_BUCKET` puts backups in a second private bucket.)
4. `pip install -r requirements.txt` (adds `boto3`) and start the API.

**Photos.** Stored with a random name under `service_tickets/<ticket>/`. They are only reachable through
`GET /api/v1/service-tickets/{id}/images/{image_id}`, which needs a valid login and applies the ticket rules (OWNER sees
all; STAFF / TECHNICIAN only tickets assigned to them or to nobody). The API reads the photo from R2 and returns the
bytes, so the Android app needs no change and no R2 address or key ever reaches a phone. Ticket JSON no longer lists
any storage key or URL for images, only `id`, `event_id`, `content_type`, `size_bytes`, `created_at`.

**Backups** (platform admin)
- `GET /api/v1/admin/backup` downloads the backup as before **and** keeps a copy at `backups/` in R2 (response header
  `X-Backup-Stored`). If the R2 upload fails the call fails with 502, so a backup is never silently missing.
  The newest `KEEP_CLOUD_BACKUPS` (default 30; 0 = keep all) are kept.
- `POST /api/v1/admin/backup/cloud` takes a backup into R2 without downloading it (use it from a nightly cron).
- `GET /api/v1/admin/backups` lists them; `POST /api/v1/admin/backups/{name}/restore?confirm=RESTORE` restores one directly.
- The automatic pre-restore safety snapshots also go to R2 (`snapshots/`), so they survive a server rebuild.
  `GET /api/v1/admin/summary` shows `snapshots` and `cloud_backups`.

## 2o. Security fixes (v1.11.3)

- **One-time PINs are really one-time.** After an owner creates a staff / technician login or resets its PIN, that account's
  token can only call `POST /auth/change-pin` and `GET /auth/me`. Everything else answers `403` with header
  `X-Must-Change-Pin: true` until the PIN is changed (`/auth/login-pin` already returns `must_change_pin`; the app must
  send the user to the change-PIN screen first). The new PIN must differ from the current one.
- **AMC visits belong to a technician.** `PUT /amc/visits/{id}`: the OWNER can edit any visit and assign anyone. STAFF /
  TECHNICIAN can only edit a visit assigned to them or to nobody (otherwise `404`); updating an unassigned visit assigns it to
  them; assigning a visit to someone else, or clearing its technician, is `403`. A malformed id is a `404`, not a `500`.
- **Bank details are owner-only.** `GET /store-profile` leaves out `bank_name`, `bank_account_no` and `bank_ifsc` for every
  role except OWNER. The shop name, address, GSTIN, state, phone and e-mail (printed on every invoice) stay visible.

## 2p. Security fixes (v1.12.0)

**Upgrading:** set `DB_SSL_CA_FILE` and make sure `JWT_SECRET` is at least 32 characters (the API now refuses to start
otherwise), run `db/migrations/012_sessions_and_audit_survives_restore.sql` (safe to re-run) or `alembic upgrade head`
(revision `0008`), then restart. **Everyone has to sign in once more** (old tokens carry no session id).

- **Nobody can lock the owner out any more, and sign-in timing doesn't reveal registered phones** — see
  *Sign-in brute-force protection* above.
- **Document numbers can't collide, and keep counting past 9999.** `next_doc_number` takes a transaction-level advisory
  lock on the number series (invoices, quotations, purchases, tickets, AMC contracts each have their own), so documents
  created at the same moment queue for a few milliseconds instead of one failing with a 500. A creator takes it before any
  row lock, so it can't deadlock with another. A rolled-back document gives its number back (numbers stay consecutive).
  The sequence is compared as a number, not text, so `…/9999` is followed by `…/10000`, `…/10001`. Test: `tests/test_doc_numbers.py`.
- **The database connection verifies the server.** `DB_SSL=true` now means libpq's `verify-full`: the certificate must
  chain to the CA bundle in `DB_SSL_CA_FILE` (only that bundle is trusted) and match `DB_HOST`. Download
  `https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem`, point `DB_SSL_CA_FILE` at it, and use the RDS endpoint
  name as `DB_HOST`. With no CA file the API refuses to start instead of connecting unverified. `DB_SSL=false` is for a local
  development database only. Alembic uses the same settings. Test: `tests/test_db_ssl.py`.
- **Sign-ins can be ended.** Every sign-in is a row in `auth_sessions` and a token only works while its row is active:
  - `POST /auth/logout` ends this device; `POST /auth/logout-all` ends every device of the account;
  - `POST /auth/users/{id}/revoke-sessions` (owner) signs a staff / technician member out everywhere;
  - changing a PIN / password, an owner PIN reset, and deactivating an account end the sessions too (reactivating an
    account does not bring them back);
  - a one-time-PIN session may call `/auth/logout` as well as `/auth/change-pin` and `/auth/me`.
  The lifetime is still `JWT_EXPIRES_MINUTES` (default 30 days) but is now an upper bound, not the only control, so it can be
  shortened freely. `JWT_SECRET` shorter than 32 characters is rejected at start-up. Sessions are neither backed up nor
  restored. Tests: `tests/test_sessions.py`, `tests/test_config.py`.
- **A restore no longer erases the audit log.** `audit_logs` has no foreign key to `users` any more, is never truncated or
  overwritten by a restore (the restore entry is added to the existing history), and a database trigger now refuses
  `TRUNCATE` on it just as it already refused `UPDATE` / `DELETE`. A backup still contains the audit rows; they are loaded
  only into an EMPTY audit log (rebuilding a new database). A restore refuses to run until migration 012 is applied.
  Note: the app's own database user owns the table and could still drop the trigger; if you need protection against someone
  with that database login, use a separate, less privileged role for the API.

## 2v. pg_dump backups, restore into a fresh database (v1.14.0)

**Install the PostgreSQL client tools on the API server**, same major version as the database or newer
(`sudo apt install postgresql-client-16`; for RDS 17 use the PGDG repository). Check with `pg_dump --version`. If several
versions exist, set `PG_DUMP_PATH` / `PG_RESTORE_PATH`. No database migration.

What changed: backup/restore no longer use the app's own JSON format. A backup is a `pg_dump` custom-format archive of the
`public` schema (no owners / grants; the rows of `auth_sessions` and `login_failures` are left out). Files are named
`bcacctvwala-backup-<time>.dump`, and any PostgreSQL 16+ `pg_restore` can read them. The platform admin has full access, so
they are **plain by default**. `BACKUP_ENCRYPTION_KEY` is now **optional**: set it and every backup, R2 copy and
safety snapshot is encrypted (`*.dump.enc`, AES-256-GCM, see below); leave it empty for plain files.

**Restore on a fresh database** (new server, new RDS instance, or a wiped database):

1. Create the database, apply the schema (`db/schema.sql`, or `alembic upgrade head`), fill in `.env` (R2 settings, `SETUP_TOKEN`)
   and start the API. The extensions `pgcrypto` and `pg_trgm` are created automatically if the database user may.
2. Create the platform admin: `POST /api/v1/auth/bootstrap-admin` with header `X-Setup-Token` (the app's first-run screen).
3. Sign in as that admin, open **Backup & restore**, pick the backup file (or a cloud backup), type `RESTORE`.
   Every table is dropped and recreated from the backup in one transaction.
4. Sign in again — with the accounts **from the backup** (the temporary admin from step 2 no longer exists).
5. If the answer carries a `warning`, the backup is older than the server: run `alembic upgrade head`.

- The audit log is part of the backup and is **replaced** by a restore (the `RESTORE` entry is added afterwards).
- Restoring needs the **same or a newer** app version than the one that made the backup; the other way round is refused by
  PostgreSQL where the schemas clash and nothing changes.
- **Old `.bca.enc` / `.bca.gz` backups (before v1.14.0) cannot be restored by this version** and are no longer listed. To keep
  one, restore it with v1.13.4 first and take a new backup.
- Only one backup or restore runs at a time (`409` otherwise). A backup or restore longer than `BACKUP_TIMEOUT_SECONDS` is stopped.
- Encrypted files: AES-256-GCM, a fresh key per file derived from `BACKUP_ENCRYPTION_KEY`, streamed in 1 MiB records; any change,
  reordering or truncation fails the integrity check. **Keep a copy of the key outside the server** and rotate with
  `BACKUP_ENCRYPTION_KEY=<new>,<old>` (the first key encrypts, every key is tried when decrypting). A server without the key
  cannot open an encrypted backup.
- The plain archive exists in the server's temp directory only while a backup, restore or snapshot runs, and is removed afterwards.

## 2t. Security fixes (v1.13.3) — no migration

- **Oversized requests are refused before they are read.** Starlette used to receive (and spool to disk) a whole
  multipart upload before the 10 MB photo check ran, so an authenticated account could fill the disk. A new
  middleware (`app/body_limit.py`) now answers `413 {"error": "The request is too large (the limit here is …)"}`
  from the declared `Content-Length`, and cuts a chunked upload off as soon as it passes the limit. Limits: ordinary
  JSON requests `MAX_BODY_KB` (default 2048); photo uploads on a ticket 6 × `MAX_UPLOAD_MB` + 1 MB; the shop logo `MAX_LOGO_MB` + 1 MB; the base64 signature `MAX_SIGNATURE_MB` x 4/3 + 1 MB; a restore
  `MAX_RESTORE_MB` + 1 MB. nginx's `client_max_body_size` (30m in the deployment notes) cuts requests off earlier; raise it if you need six 10 MB photos in one request or restores larger than 30 MB.
- **Free text is capped.** Ticket `remarks` (attend form), AMC visit `remarks` / contract `coverage_notes` and the
  `notes` on quotations, invoices, purchases and the like are limited to 2000 characters (422 beyond that).
- **Cloud-storage errors no longer leak details.** A failed R2 call used to return the raw error (storage endpoint with
  the Cloudflare account id, bucket, request ids) to the platform admin. The client now gets "Cloud storage (R2) is
  unavailable or not set up correctly. The server log has the details."; the full error is in the server log.
- **Restore refuses "zip bombs".** A backup file may unpack to at most `MAX_RESTORE_UNCOMPRESSED_MB` (default 5120)
  and never to more than 100× its own size (64 MB floor); a single line over 64 MB is refused too. The check runs
  during validation, before the database is touched.

## 2s. Security fixes (v1.13.2) — apply migration 013 first

Run `db/migrations/013_login_failures.sql` (or `alembic upgrade head`) **before** starting v1.13.2: the API now refuses
to start without the new `login_failures` table (instead of answering every login with a 500).
- **Failed sign-ins no longer fill the audit log.** They used to be written to the append-only `audit_logs` (which
  can't be pruned), so guessing could grow it without limit, and the typed phone number was stored in it. They are
  now counted in the short-lived `login_failures` table (see "Sign-in brute-force protection"). Only a failure
  against a real account reaches the audit log, and only the first of a streak and the one that locks the address
  (`details: {"attempts": n, "locked": bool}`); failures against unknown phones / IDs are never audited. Counters
  restart from zero after the upgrade; old `LOGIN_FAILED` rows stay in the log as history.
- **Staff PINs got a 24-hour cross-address ceiling** (`LOGIN_PIN_DAILY_FAILURE_CEILING`, default 100) so slow
  guessing from many addresses can't run for months. Trade-off: while a PIN account is under attack, a staff member
  signing in from a brand-new address (e.g. a new mobile network) may have to wait; their usual addresses are fine.

## 2r. Security fixes (v1.13.1)

No migration; set nothing new (optional `ENABLE_DOCS`).
- **`POST /auth/users/{id}/reset-password` removed.** It let anyone holding an owner token set a new owner password
  without the current one (and without the credential rate limit), which signs the real owner out everywhere. Use
  `POST /profile/change-password` (current password required). The Android app never used the removed endpoint.
- **bcrypt no longer blocks the server.** Every hash / verify (logins, PIN and password changes, bootstrap, staff
  creation, PIN reset) now runs in a worker thread, so a burst of sign-in attempts can't freeze other requests.
  Password / PIN-login fields are capped (password 128 characters, phone 40, account ID 64) so oversized bodies
  never reach the hashing step; over the cap is a 422.
- **Interactive docs off by default.** `/docs`, `/redoc` and `/openapi.json` answer 404 unless `ENABLE_DOCS=true`.

## 2q. My AMC visits (v1.13.0)

No migration. `GET /api/v1/amc/my-visits` (OWNER / STAFF / TECHNICIAN) lists the visits the caller may act on: assigned to
them or to nobody yet, on `ACTIVE` contracts — the same set `PUT /api/v1/amc/visits/{id}` accepts from field roles. Rows carry
`contract_no`, `site_address`, `customer_name`, `customer_phone` (never the contract price). Optional `status=SCHEDULED|COMPLETED|MISSED`,
`limit` / `offset`, total in `X-Total-Count`. Open visits first (oldest due date first), then finished ones (newest first).
Before this there was no way for staff / technicians to discover their visits, because `GET /amc` and `GET /amc/{id}` are owner-only.

## 2i. Cancel and edit documents, quotation / purchase PDFs (v1.9)

**Upgrading:** run `db/migrations/010_cancel_documents.sql` (safe to re-run) or `alembic upgrade head`
(revision `0006`). It adds `status` (`ACTIVE` / `CANCELLED`), `cancelled_at`, `cancel_reason` and
`cancelled_by` to `invoices` and `purchases`. A brand-new database gets them from `schema.sql`.

- `GET /quotations/{id}/pdf`, `GET /purchases/{id}/pdf` — printable documents in the same style as the invoice PDF.
  Cancelled invoices / purchases are stamped CANCELLED.
- `PUT /quotations/{id}` — revise a quotation while it is `PENDING` (same body as create: customer, `valid_till`,
  notes, items). Number and date stay; totals are re-worked. Anything else returns 409.
- `PUT /purchases/{id}` (OWNER) — fix supplier, date, supplier invoice no. and notes. Fields left out keep their
  value; `null` clears the invoice no. / notes. A new date re-dates the supplier-warranty end of its units (unless an
  end date was set by hand). The lines can't be edited: cancel and enter the purchase again. The supplier can't change
  once payments exist.
- `POST /invoices/{id}/cancel`, `POST /purchases/{id}/cancel` (OWNER) — body `{"reason": "...", "refund_payments": false}`.
  Cancelling an invoice also cancels the pending (OPEN / RESCHEDULED / ATTENDED) service tickets raised against its serialized units (timeline event `CANCELLED`); the response lists them in `cancelled_tickets`. Closed tickets stay as history, and tickets billed on another invoice are left alone.
  The document is kept (its number is never reused) and flagged `CANCELLED`; all of this happens in one transaction:
  - invoice: the stock it took out goes back (`RETURN_IN`), its units return to `IN_STOCK` with the customer warranty
    cleared, service tickets it billed become billable again, a converted quotation goes back to `ACCEPTED`.
  - purchase: the stock it added is taken out (`RETURN_OUT`), its units are deleted (so the serials can be entered
    again), and the product's purchase price falls back to the previous purchase's price (if nobody changed it since).
  - 409 when a unit is claimed / replaced (invoice), has left stock or is on a service ticket (purchase), or when there
    isn't enough stock left to take back (purchase).
  - 409 while money is recorded against it, unless `refund_payments` is `true`: then one negative payment is booked and
    `paid_amount` becomes 0, so the payment history nets to zero.
  - Payments can't be added to a cancelled document, and a cancelled one can't be cancelled or edited again.
- `GET /invoices` and `GET /purchases` take `status=ACTIVE|CANCELLED`; rows and details carry `status`,
  `cancel_reason`, `cancelled_at`. Cancelled documents are left out of customer / supplier balances and
  outstanding lists, GST summary and input credit, sales summary, top products and the dashboard, but still show in
  customer / supplier history.

## 2j. Audit log and My profile (v1.10)

**Upgrading:** run `db/migrations/011_audit_logs.sql` (safe to re-run) or `alembic upgrade head` (revision `0007`).
It creates `audit_logs`. A brand-new database gets it from `schema.sql`. Optional setting: `APP_TIMEZONE`
(default `Asia/Kolkata`) — the day the audit log's `from` / `to` filters mean.

### Audit log

Every successful write (POST / PUT / DELETE) by a signed-in user is recorded automatically — who, when, from which
IP, what it was done to, a readable summary (`Cancelled invoice INV-0012`) and the request body with secrets removed
(passwords / PINs / tokens are replaced by `***`, bank account numbers keep their last 4 digits). Also recorded:
sign-ins and failed sign-ins, the two bootstrap steps, own-profile edits (with before / after values) and, for the
platform admin, backups and restores. Rows are append-only — a database trigger refuses UPDATE and DELETE.
Failed requests (403 / 409 / 422 ...) are not recorded as actions.

- `GET /audit-logs` (OWNER) — newest first, paged (`limit` / `offset`, `X-Total-Count`). Filters: `user_id`,
  `entity_type` (`INVOICE`, `CUSTOMER`, `USER`, ...), `entity_id` (with `entity_type` = the history of one record),
  `action` (`CREATE`, `UPDATE`, `DELETE`, `CANCEL`, `PAYMENT`, `LOGIN`, ...), `from` / `to` (`YYYY-MM-DD`, inclusive)
  and `search` (summary or person's name). Rows: `id, created_at, user_id, user_name, user_role, action, entity_type,
  entity_id, summary, ip_address`.
- `GET /audit-logs/{id}` (OWNER) — one row plus `details` (`{"endpoint": "...", "request": {...}}`).
- `GET /audit-logs/meta` (OWNER) — the `actions` and `entity_types` lists, for filter drop-downs.
- `GET /admin/audit-logs` (platform admin) — the platform admin's own history (sign-ins, backups, restores). The
  platform admin's activity never appears in the owner's endpoints above, same as it never appears anywhere else.

New write endpoints are covered without extra code as long as their router is created with
`route_class=AuditRoute` (see `app/utils/audit.py`); anything else can call `log_event(...)` directly.
Writing the log never fails the request it belongs to. A restore replaces the audit log along with everything else
(the restore itself is then recorded in the restored log).

### My profile

Available to every signed-in account (owner, staff, technician and the platform admin — each only ever sees itself).

- `GET /profile` — `id, name, phone, email, role, is_platform_admin, must_change_pin, created_at`, plus
  `credential` (`PASSWORD` for owner / platform admin, `PIN` for staff / technician — which change-credential screen
  to show) and `last_login_at`. (`GET /auth/me` still works as before.)
- `GET /profile` also returns `has_image` and `image_updated_at` (see **Profile image** below).
- `PUT /profile` — change `name`, `phone` and / or `email`; only the fields sent change (`email` can be cleared with
  `null` or `""`; name and phone can't be empty). 409 if the phone / email belongs to another account. The owner and
  platform admin sign in with their phone number, so after changing it they use the new one.
  Role and active state can't be changed here.
- `POST /profile/change-password` (OWNER / platform admin) — `{"current_password", "new_password"}` (8+ chars).
  Needs the current password, signs the account out everywhere else, and returns a fresh `token` for this device.
  Staff / technician change their PIN with `POST /auth/change-pin`.
- `GET /profile/activity` — my own history from the audit log (writes and sign-ins), newest first, paged, same
  `from` / `to` / `action` / `entity_type` / `search` filters.

## 2k. Role checks on business routes (v1.10.1)

Before this release only the tickets, audit, store-update and cancel/edit endpoints checked the caller's
role; every other business route accepted any signed-in account (STAFF, TECHNICIAN and even the platform
admin). Now every business route is **OWNER-only** unless listed here:

| Who | What |
|---|---|
| Any signed-in account | `/auth/me`, `/auth/team`, `/auth/change-pin`, `/profile*`, `GET /store-profile` |
| OWNER / STAFF / TECHNICIAN | `PUT /amc/visits/{id}` (record a visit), plus the ticket endpoints described in 2h |
| OWNER only | customers, suppliers, products, purchases, quotations, invoices, warranties, reports, AMC contracts, staff management, store profile update |
| Platform admin only | `/admin/*` (backup / restore) |

`tests/test_role_policy.py` needs no database: it inspects every registered route and fails if a route's
role guard differs from this table, so a newly added endpoint is owner-only by default. To open an endpoint
to field staff, add it to `ALLOWED` in that test *and* give the route a `require_role(...)` dependency.
Upgrade note: if the Android app lets staff/technician screens call any of the owner-only routes, they now
get 403.

## 2l. First-run setup token (v1.10.2)

`POST /api/v1/auth/bootstrap-admin` and `POST /api/v1/auth/bootstrap-owner` used to be open to anyone who could reach a
fresh server, so whoever called first became the platform admin / owner. Both now require a one-time secret in the
`X-Setup-Token` header, matched against `SETUP_TOKEN` in the server's `.env` (constant-time comparison).

1. Generate one: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` and put it in `.env` as `SETUP_TOKEN=...`
2. Restart the API, then send the token with both setup calls (the app's first-run screens must ask for it, or you
   can do the two calls with curl/Swagger).
3. After both accounts exist the endpoints are closed anyway; remove `SETUP_TOKEN` from `.env` and restart.

If `SETUP_TOKEN` is empty, first-run setup is **disabled** (403) rather than open. A wrong/missing token also gets 403.
`GET /auth/bootstrap-status` stays public so the app knows which screen to show. Existing installs that already have
both accounts are unaffected. The test suite sets its own token in `tests/conftest.py`.

## 2n. Shop logo on invoices and quotations (v1.15.0)

**Run the migration first:** `alembic upgrade head` (or apply `db/migrations/014_store_logo.sql`, then `alembic stamp 0010`).
It adds `logo_key` and `logo_updated_at` to `store_profile`. `pip install -r requirements.txt` also picks up Pillow.

- `POST /api/v1/store-profile/logo` — OWNER only. Multipart form, field **`logo`**: a PNG, JPEG or WebP, up to `MAX_LOGO_MB`
  (default 2). The server checks the file's real content (not its name or declared type), shrinks it to at most 600 px
  and stores it in R2 as a PNG (`store/logo/<random>.png`), replacing any earlier logo. A transparent PNG looks best.
  Returns the store profile. 400 for an unusable file; 409 if the store profile has never been saved.
- `GET /api/v1/store-profile/logo` — any signed-in user; the PNG (404 when none). `GET /store-profile` now has `has_logo`
  and `logo_updated_at` (use it to refresh a cached image); the storage key is never exposed.
- `DELETE /api/v1/store-profile/logo` — OWNER only; documents print without a logo again.
- The logo prints top-left on **invoice** and **quotation** PDFs (scaled to fit about 110 × 56 pt, shop details beside it).
  Purchase records are internal and unchanged. PDFs are drawn on request, so a new logo shows on old invoices too when
  they are downloaded again. If the logo can't be loaded the PDF still prints, without it.
- Editing the store profile (`PUT /store-profile`) never touches the logo. The logo file lives in R2, not in the database
  backup, so keep that bucket's own backups if you care about it; if it goes missing, just upload it again.
- Changes are written to the audit log as "Changed the shop logo".

## 2o. Base64 signature on invoices (v1.16.0)

**Run the migration first:** `alembic upgrade head` (or apply `db/migrations/015_store_signature.sql`, then `alembic stamp 0011`).
It adds `signature_key` and `signature_updated_at` to `store_profile`. No new packages.

- `PUT /api/v1/store-profile/signature` — OWNER only. JSON body `{"signature_base64": "<base64>"}`. The value can be raw
  base64 or a data URL (`data:image/png;base64,...`, what a signature pad / canvas `toDataURL()` gives you); line breaks
  and missing `=` padding are tolerated. The decoded image must be a PNG, JPEG or WebP under `MAX_SIGNATURE_MB`
  (default 1). The server checks the real content, trims the empty margin (transparent, or a plain white page), shrinks it
  to at most 600 px and stores it in R2 as a PNG (`store/signature/<random>.png`), replacing any earlier signature.
  Returns the store profile. 400 for bad base64 or an unusable image; 409 if the store profile has never been saved.
  A transparent PNG looks best; a photo of a signature on white paper works too.
- `GET /api/v1/store-profile/signature` — any signed-in user; the PNG (404 when none). `GET /store-profile` now has
  `has_signature` and `signature_updated_at`; the storage key is never exposed.
- `DELETE /api/v1/store-profile/signature` — OWNER only; invoices print without a signature again.
- **Invoice PDF only** (`GET /invoices/{id}/pdf`): below the totals, on the right, "For <shop name>", the signature on a
  rule, and "Authorised Signatory" (image scaled to fit about 130 × 48 pt). No signature saved = the invoice looks exactly
  as before. If there is no room above the footer the block goes on a new page. Quotations and purchases are unchanged.
  PDFs are drawn on request, so a new signature shows on old invoices too when they are downloaded again. If the
  signature can't be loaded the PDF still prints, without it.
- The base64 text is never written to the audit log (it is replaced by `***`); the audit entry reads "Changed the invoice
  signature". Editing the store profile (`PUT /store-profile`) never touches the signature. Like the logo, the image lives
  in R2, not in the database backup.
- The request limit for this one endpoint is `MAX_SIGNATURE_MB` x 4/3 + 1 MB (base64 is a third bigger than the image).

## 2p. API versioning: `/api/v1` (v1.17.0)

**No migration, no new packages.**

- Every endpoint now lives under `/api/v1`, e.g. `POST /api/v1/auth/login`, `GET /api/v1/invoices`,
  `GET /api/v1/version`. `/health` is unversioned, as before. The interactive docs (when `ENABLE_DOCS=true`)
  list only `/api/v1` paths.
- The old `/api/...` paths answer with exactly the same handlers (hidden from the docs) only while
  `LEGACY_API_ENABLED=true`. Since 1.17.1 that is **off by default** (see 2q).
- CORS: no browser origin is allowed by default (the Android app does not need it). To serve a web client set
  `CORS_ALLOWED_ORIGINS=https://app.example.com` (comma-separated; `*` is refused).
- Free-text fields are capped (addresses 500, descriptions / notes 2000, barcodes 100, IDs 64 characters) and
  `search` filters accept at most 100 characters.
- Switching a client over is only a base-URL change: `API_BASE_URL` = `https://<your-host>/api/v1`.
- Request-size limits and audit-log entity/action names work the same under both prefixes.
- Reverse proxy: a rule that matches `/api` (nginx `location /api`, CloudFront path pattern) already covers
  `/api/v1`; a rule that matches `/api/` exactly or a list of paths needs `/api/v1/` added.
- The `v1` promise: do not change the shape of a `/api/v1` response in a way that breaks installed apps. A
  breaking change goes under a new prefix (`/api/v2`) while `/api/v1` keeps running.

## 2m. Tests

`tests/test_api.py` is an end-to-end scenario against a real, **empty** PostgreSQL database:

```bash
createdb gst_test && psql gst_test -f db/schema.sql
DB_HOST=localhost DB_NAME=gst_test DB_USER=... DB_PASSWORD=... DB_SSL=false pytest -q tests
# (tests/conftest.py sets a throw-away JWT_SECRET; the app itself needs 32+ characters)
```

## 3. Running on EC2

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv nginx
cd gst-billing-backend-py
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real RDS + JWT values

# Run under a process manager (systemd), not directly in a terminal:
sudo tee /etc/systemd/system/gst-api.service > /dev/null <<'EOF'
[Unit]
Description=GST Billing API
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/gst-billing-backend-py
Environment="PATH=/home/ubuntu/gst-billing-backend-py/venv/bin"
Environment="API_WORKERS=2"
ExecStart=/home/ubuntu/gst-billing-backend-py/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 4000 --workers 2 --no-proxy-headers
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now gst-api
```

Put nginx in front, proxying to `localhost:4000`, then `certbot --nginx` for HTTPS. The `location /` block
must pass the real client address on, or every request looks like it comes from `127.0.0.1`:

```nginx
location / {
    proxy_pass http://127.0.0.1:4000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 30m;
}
```

**Why these flags matter (v1.10.2):**

- `--host 127.0.0.1` — uvicorn only listens on loopback, so the API can't be reached on port 4000 directly
  (that would bypass nginx and let a caller send a fake `X-Forwarded-For`). Keep port 4000 closed in the
  EC2 security group as well.
- `--proxy-headers --forwarded-allow-ips 127.0.0.1` — uvicorn trusts `X-Forwarded-For` only from nginx on the
  same machine. Without this, the per-IP rate limiter put **all users in one bucket** (one noisy client could
  lock everyone out) and the audit log recorded `127.0.0.1` for every action. Never use `--forwarded-allow-ips "*"`
  unless port 4000 is firewalled off from everything except the proxy.
- After deploying, check it: sign in from your phone, then `GET /api/v1/audit-logs` — `ip_address` should be your
  phone's address, not `127.0.0.1`.

## 4. What's identical to the Node version

- **Database**: same `db/schema.sql`, same tables, same columns.
- **Auth**: same JWT claims (`id`, `name`, `role`), same bcrypt-compatible
  password hashing (passlib's bcrypt produces the same `$2b$...` hash format
  bcryptjs does). The bootstrap/staff-login flow itself has diverged from the
  Node version as of v1.6 — see §2d.
- **Every route** — customers, suppliers, products/inventory, purchases,
  quotations, invoices (incl. `/pdf`), AMC, reports/dashboard — same paths,
  same query params, same request bodies, same response fields.
- **GST logic**: `app/utils/gst.py` is a line-for-line port of the Node
  `gst.js` — same rounding rule, same CGST/SGST-vs-IGST decision based on
  `STORE_STATE_CODE`.
- **Document numbering**: same `PREFIX/2025-26/0001` scheme, reset per Indian
  financial year (Apr–Mar).
- **Error shape**: FastAPI's default `{"detail": "..."}` is overridden with a
  handler so every error still comes back as `{"error": "..."}`, matching
  what the Android app already expects.

## 5. What's different under the hood (shouldn't matter to any client)

- **PDF rendering**: reportlab instead of pdfkit. Verified against a sample
  invoice — same layout (header, bill-to, itemized table, GST breakdown,
  bank details), slightly cleaner column alignment (right-aligned numeric
  columns) than the original.
- **Numeric types**: asyncpg returns `NUMERIC` columns as Python `Decimal`;
  FastAPI's JSON encoder converts these to plain JSON numbers automatically,
  same as the Node version's explicit float-parsing fix.
- **Interactive docs**: FastAPI can serve OpenAPI/Swagger docs at
  `/docs` — the Node version had no equivalent. Off unless `ENABLE_DOCS=true`.
- **Rate limiting**: same 500-requests-per-15-minutes default, via `slowapi`
  instead of `express-rate-limit`.

## 6. Caveat

This was written and syntax-checked in a sandbox without network access, so
`pip install` and a live Postgres connection couldn't be exercised end-to-end
here. What *was* verified directly in this environment:
- Every `.py` file compiles (`python3 -m py_compile`).
- The GST calculation logic was unit-tested against the same inputs as the
  Node version and produces identical output (intra-state CGST+SGST split,
  inter-state IGST, invoice rounding).
- The invoice PDF generator was actually run end-to-end and the output
  rendered to confirm the layout looks right (see below).
- Every SQL query is a near-verbatim port of the already-working Node
  queries (asyncpg uses the same `$1, $2...` placeholder syntax as
  node-postgres, so most queries needed no translation at all).

Run `pip install -r requirements.txt` and a real request against your RDS
instance before treating this as production-ready — a first real run is
still the way to catch anything a static check can't.

## Money, limits and validation rules

- **Money is `Decimal`** end to end (GST, totals, payments, balances), rounded half-up to paise; the invoice
  total is rounded to the nearest rupee with the difference stored as `round_off`.
- **Line items** (invoices, quotations, purchases): `quantity > 0` (max 100,000), `unit_price >= 0` (max
  Rs. 1 crore), `gst_rate` 0-40, at most 2 decimal places each, line value capped at Rs. 10 crore, at most
  500 lines. Violations are `422`.
- **Payments** must be > 0, can't exceed the outstanding balance (`400`), and can't be dated in the future
  (checked in `APP_TIMEZONE`).
- **Stock**: a sale that would take a product below zero is refused with `409`. Lines that name barcodes are
  checked unit by unit instead. Send `"allow_negative_stock": true` (on `POST /invoices` or
  `POST /invoices/from-quotation/{id}`) to sell anyway; the response then carries a `warnings` entry per
  product. `adjust-stock` can't push stock below zero either.
- **Parties / store profile**: GSTIN format (15 chars, valid state prefix), GST state code (01-38, 97, 99),
  phone (7-15 digits, optional leading `+`; spaces/dashes are stripped), email, and name/address lengths.

## 2q. Proxy / client address, per-worker limits, literal search, legacy off (v1.17.1)

**No migration, no new packages.** Fixes the "informational" and "deployment" items of the security review.

- **Real client address** (`app/client_ip.py`). Rate limits, sign-in lock-outs and the audit log are keyed by client
  IP. The API now reads the address itself instead of relying on uvicorn's `--proxy-headers`:
  - a request that does **not** come from a trusted proxy keeps the address it really came from; its
    `X-Forwarded-For` is ignored, so a client can no longer pick the address it appears to have;
  - a request from a trusted proxy gets the address the proxy saw: `CLIENT_IP_HEADER` if set (e.g. `CF-Connecting-IP`,
    or `X-Real-IP` with `proxy_set_header X-Real-IP $remote_addr;`), otherwise `X-Forwarded-For` read from the right,
    skipping trusted proxies.
  - `TRUSTED_PROXIES` (IPs or CIDR blocks, comma-separated; default `127.0.0.1,::1` = nginx on the same machine).
    With a proxy on **another host**, list its address / private range, otherwise every user shares the proxy's
    address (one bucket, and one attacker could lock everyone out). `*`, `0.0.0.0/0` and `::/0` are refused.
  - Start uvicorn with `--no-proxy-headers` (the unit file above does). With `--forwarded-allow-ips '*'` uvicorn
    believes any client's header; the API logs a warning at start-up if `FORWARDED_ALLOW_IPS=*` is set.
- **Per-worker counters**: `API_WORKERS` (default 1) = your `--workers` value. Each worker enforces `1/N` of every
  per-IP limit (rounded up, never below 1), so N workers together allow about the configured number. Set it, or the
  limits stay as before (each worker allows the full amount).
- **`/auth/bootstrap-status`** (public, the app needs it before sign-in) is now rate limited per IP with the same
  ceiling as the credential endpoints (separate bucket).
- **`LEGACY_API_ENABLED` defaults to `false`** (as `.env.example` already said). The old `/api/...` paths return 404
  unless you set `LEGACY_API_ENABLED=true`: only needed for Android builds 1.25.x and older, which call `/api/`.
  Android 1.26.0+ uses `/api/v1/`.
- **Search is literal**: `%` and `_` typed in a search box are no longer wildcards (`SearchTerm` in
  `app/utils/search.py`, and every `ILIKE` now ends with `ESCAPE '\'`). `tests/test_client_ip.py` fails if a new
  `ILIKE` is added without it.

## 2r. Image decoding cap and owner-only signature (v1.17.2)

**No migration, no new packages.**

- **One cap for all image decoding** (`app/utils/image_gate.py`): ticket photos, the shop logo and the signature now
  share one gate, so at most 2 images are decoded at the same time per worker process; the rest wait their turn.
  Before, only photos had it, so a burst of logo / signature uploads could pile up memory.
- **Lower pixel ceiling** for logo and signature: 12 megapixels (about 3400 x 3400, was 25 MP), refused before
  decoding. JPEGs are also decoded at reduced size (`draft`), so a big photo of a signature never exists in memory at
  full size. Worst case per image is now roughly 100 MB instead of 200 MB, and never more than 2 at once.
- **`GET /store-profile/signature` is OWNER only** (was any signed-in user). The image is a forgery tool, so staff and
  technicians get `403`. Everything else is unchanged: they still print invoices that carry the signature, and
  `has_signature` in `GET /store-profile` still tells them whether one is set. The logo stays readable by everyone
  (it is printed on every document anyway).

## 2s. Credential changes share the sign-in lock (v1.17.3)

**No migration, no new packages.**

`POST /auth/change-pin` and `POST /profile/change-password` check the *current* PIN / password. Before this version they
were limited only by the per-IP request rate, so someone holding a stolen session token could test guesses through
them. They now use the same brute-force guard as sign-in (`app/utils/login_lock.py`, moved out of `routers/auth.py`):

- wrong current PIN / password counts as a failed attempt against the account, from that client address;
- after `LOGIN_MAX_FAILURES` (5) in a row the answer is `429` + `Retry-After`, even for the correct value, and it is
  checked *before* the value is verified;
- it is **one** counter with sign-in: 3 wrong PINs at the login screen and 2 in the change-PIN screen lock it, and a
  locked account can't sign in from that address either;
- the 6-digit PIN's daily ceiling (`LOGIN_PIN_DAILY_FAILURE_CEILING`) and the many-addresses ceiling apply to
  `change-pin` as well;
- a successful change clears the counter; the first and the locking failure are audited as `LOGIN_FAILED` with
  `details.during = "change-pin"` / `"change-password"`.

No change for the Android app: it already shows the server's message (`error`) and does not treat a 401 from these two
endpoints as an expired session.

## 2t. Malformed input is a 4xx, not a 500 (v1.17.4)

**No migration, no new packages.** Found by fuzzing every endpoint (path IDs, every query parameter, every body field,
about 1,350 requests plus a null / blank / odd-value sweep of every body field). Nothing leaked and nothing was
injectable, but input PostgreSQL can't take used to be an unhandled error: a `500` and a full stack trace in the log per
request, so a script could flood the log (and the app saw "Internal server error" for what was a typo).

- A path ID that isn't a UUID (`/customers/abc`, `%00`, ...) -> `404 {"error": "Not found"}` on all ~30 routes that had
  it; the same kind of error anywhere else in a request -> `400 {"error": "Invalid value in the request"}`. One handler
  for asyncpg's `DataError` (in `app/main.py`); it logs one line, no traceback, and never sends the database's message.
- `offset` is capped at 100,000,000 (was unbounded: a number over 2^63 was a 500); `reports/top-products?limit=` must be
  1-100 (a negative limit was a 500).
- A NUL byte in a `search` box is a 422 (PostgreSQL can't store it).
- `hsn_code: null` / `""` on product create / update was a pydantic `TypeError` (the length check ran after the blank
  became `None`); the check now runs first. Valid and invalid HSN codes behave as before.
- Log lines that include the request path now print it with `%r`, so a newline in a URL can't forge a log entry.

Also checked, nothing to change: with 12 simultaneous requests exactly one sale takes the last unit, exactly one of
12 full payments is accepted, exactly one invoice comes out of a quotation, and a cancel racing with payments leaves
the invoice cancelled with the payment rows netting to zero. `pip-audit` on `requirements.txt`: no known
vulnerabilities. `tests/test_malformed_input.py` covers the fixes.

## 2u. Security response headers (v1.17.5)

**No migration, no new packages.** `app/security_headers.py` adds these to **every** response, including the 413 / 429
answers from the other middleware. No status code or body changes; the Android app ignores them.

| Header | Value | Why |
|---|---|---|
| `X-Content-Type-Options` | `nosniff` | a browser must not guess another type for the body |
| `Cache-Control` | `no-store` | no proxy / CDN / browser keeps a copy of invoices, PDFs, customer data or tokens. Only when the route didn't set its own: ticket photos and the shop logo / signature keep theirs |
| `Referrer-Policy` | `no-referrer` | |
| `X-Frame-Options` | `DENY` | nothing from the API may be framed |
| `Content-Security-Policy` | `default-src 'none'; frame-ancestors 'none'` | the API serves data, never a page. Not sent on `/docs`, `/redoc`, `/openapi.json` (they need scripts when `ENABLE_DOCS=true`) |
| `Strict-Transport-Security` | `max-age=<HSTS_MAX_AGE>` | **off by default** (`HSTS_MAX_AGE=0`). Set `31536000` only once the API is reachable over HTTPS alone: browsers then refuse plain HTTP to the host for that long |

nginx can add the same headers; with this in the API they are there even when a request never reaches nginx's `location`
(and `add_header` in nginx doesn't repeat them on error pages unless `always` is used).

## 2v. Expenses and profit & loss (v1.18.0)

**One new table (`expenses`): run `alembic upgrade head`** (or `db/migrations/016_expenses.sql`; idempotent). No new packages.

**Expenses** (`/expenses`, OWNER only): rent, salary, electricity, travel... `amount` is the total paid, including any GST.
- `GET /expenses?from=&to=&category=&search=&limit=&offset=`: newest first. `X-Total-Count` = matches, `X-Total-Amount` = their sum (not just the page's).
- `GET /expenses/summary?from=&to=`: `{count, total, by_category[{category,total,count}], by_month[{month,total}]}`.
- `GET /expenses/categories`: the suggested categories (Rent, Salary & wages, Electricity & water, Internet & phone, Travel & fuel, Vehicle & transport, Repairs & maintenance, Office & stationery, Marketing, Bank & payment charges, Taxes & fees, Insurance, Professional fees, Miscellaneous) plus any others already used.
- `GET /expenses/{id}`, `POST /expenses`, `PUT /expenses/{id}`, `DELETE /expenses/{id}` (204).
  Body: `{expense_date (default today, not in the future), category, amount, payment_mode (CASH/UPI/BANK/CHEQUE/CARD), paid_to?, reference_no?, notes?}`.
- A category typed in any case or with extra spaces is saved with the existing spelling ("rent" -> "Rent"), so reports never split one category in two.
- Every create / change / delete is written to the audit log (`entity_type` `EXPENSE`) with the amounts; a deleted expense leaves a row saying what it was.

**`GET /reports/profit-loss?from=&to=`** (OWNER only): `sales_taxable` (invoices before GST, cancelled ones excluded; the same figure as `total_taxable` in gst-summary) - `cost_of_goods` = `gross_profit`; - `expenses_total` = `net_profit`; plus margins, `expenses_by_category` (top 8) and `lines_without_cost`.
- **The cost is an estimate**: invoices don't store the cost at the time of sale, so every product line is costed at the product's current `purchase_price` (ex-GST, updated by purchases). `cost_is_estimate` is always `true`; `lines_without_cost` counts product lines whose purchase price is 0 (profit is overstated for those). Service / labour lines (no product) cost nothing.
- Credit notes dated in the period are taken off the sales (and restocked goods off the cost) since v1.38.0.

**Also in this version**: `tests/test_route_guards.py` fails if any `/api/v1` route other than the five sign-in / first-run ones is unguarded, and if a money router (customers, suppliers, invoices, purchases, quotations, reports, expenses) isn't role-guarded. (It exists because the new expenses router briefly had unguarded GET routes during development; the test caught it before release.)

## 2w. Service ticket job card PDF (v1.18.1)

No migration, no new packages.

**`GET /service-tickets/{id}/pdf`** (OWNER, STAFF, TECHNICIAN; same visibility as `GET /service-tickets/{id}`, so a technician gets 404 for someone else's ticket): a printable job card with the shop header and logo, customer and site address, the problem, the tracked unit and its warranty, the visit history, and boxes for work done and the customer's and technician's signatures (since v1.29.0 the customer's box is filled in from their recorded sign-off, see 2zzzzzzzz). A cancelled ticket is stamped CANCELLED. The estimated charge and the invoice number are printed for the OWNER only; field staff never see money. Photos are not embedded. Tests: `tests/test_ticket_pdf.py`, plus the job-card lines in `test_service_tickets` in `tests/test_api.py`.

## 2x. Customer warranty card PDF (v1.18.2)

No migration, no new packages.

**`GET /warranties/{id}/pdf`** (OWNER only, like the rest of `/warranties`): a one-page customer warranty certificate for a unit that has been sold: shop header, logo and authorised signature (when saved in Store profile), customer name / phone / address, product, serial / barcode, invoice number and date, warranty start, period and end date, and a fixed terms paragraph (`WARRANTY_TERMS` in `app/utils/pdf.py`; edit the text there to change it). A unit that has not been sold, or has no customer warranty date, answers `400`; an unknown id answers `404`. Tests: `tests/test_warranty_card.py`.

## 2y. HSN / SAC on invoices, GSTR-1 style reports and CSV exports (v1.19.0)

**Run the migration first:** `psql ... -f db/migrations/017_invoice_item_hsn.sql` (or `alembic upgrade head`; if you applied the .sql by hand, `alembic stamp 0013`). It only adds the nullable column `invoice_items.hsn_code`, and is safe to run twice. `db/schema.sql` and `schema.sql` already contain it for new databases.

**HSN / SAC on invoices.** Each invoice line now stores its HSN/SAC when the invoice is made: the product's `hsn_code`, or for a free-text line (installation, labour) the optional `hsn_code` sent on that line (2-8 digits, e.g. `998719`). Changing a product's HSN later does not change old invoices or the reports. Older invoices have no stored code; their PDF and the reports fall back to the product's current HSN. The invoice PDF and the quotation / purchase PDFs now have an **HSN/SAC** column, and `GET /invoices/{id}` items carry `hsn_code` and `product_hsn_code`. Quotation lines do not store a code (a quotation shows the product's HSN), so a free-text line converted from a quotation has none until you add it.

**New reports** (all OWNER only, period of at most 400 days, `from` / `to` as `YYYY-MM-DD`; cancelled invoices are left out except in the document count):
- `GET /reports/gstr1`: `b2b` (buyer has a GSTIN; one row per invoice per rate), `b2cl` (no GSTIN, inter-state, invoice value above Rs. 1,00,000), `b2cs` (all other unregistered sales, totalled by place of supply and rate), `rate_summary`, `hsn` (by HSN, unit and rate), `documents` (first / last invoice number, issued, cancelled), `totals` and `warnings.lines_without_hsn`. `/reports/gst-summary` is unchanged and its totals equal `totals` here.
- `GET /reports/gstr1/csv?section=b2b|b2cl|b2cs|hsn|docs|all`: the same as CSV (UTF-8 with BOM so Excel reads it); `all` (the default) is a zip of every sheet. Text that a spreadsheet would run as a formula (a customer called `=...`) is prefixed with `'`.
- `GET /reports/purchase-register/csv`: purchases with supplier GSTIN, state, taxable value and the IGST / CGST / SGST split, for matching input tax credit against GSTR-2B.
- `GET /reports/gstr3b`: output tax, input tax credit and the net per head (IGST / CGST / SGST). An estimate: ITC is set off head by head without the cross-utilisation the portal allows.

Not covered yet: credit / debit notes (so a returned sale still counts as a sale), e-invoice (IRN) and e-way bill. Tests: `tests/test_gstr1.py`.


## 2z. Line discounts on quotations and invoices (v1.20.0)

**Run the migration first:** `psql ... -f db/migrations/018_discounts.sql` (or `alembic upgrade head`; if you applied the .sql by hand, `alembic stamp 0014`). It only adds columns with a default of 0, so it is safe on existing data and safe to run twice. `schema.sql` and `db/schema.sql` already contain them for new databases.

Every line on `POST/PUT /quotations` and `POST /invoices` accepts an optional **`discount_percent`** (0-100, max 2 decimals, default 0). The discount is taken off `quantity x unit_price` *before* GST, so GST is charged on the discounted (taxable) value: `taxable = gross - round2(gross x pct / 100)`.

- `invoice_items` / `quotation_items` store `discount_percent` and `discount_amount` (rupees); `invoices` / `quotations` store `total_discount`.
- `subtotal` keeps meaning "taxable value", i.e. it is already **after** discount. Gross = `subtotal + total_discount`. Old rows have 0 discount, so nothing changes for them.
- Converting a quotation (`POST /invoices/from-quotation/{id}`) carries each line's discount over.
- GSTR-1 style reports (`rate_summary`, HSN summary, CSV exports) now use the discounted taxable value.
- The invoice and quotation PDFs show `Disc x%` under the rate, a **Taxable** column after discount, and Subtotal / Discount / Taxable value in the totals when any discount was given.


## 2zz. Invoice-level discount (v1.21.0)

**Run the migration first:** `psql ... -f db/migrations/019_invoice_discount.sql` (or `alembic upgrade head`; if applied by hand, `alembic stamp 0015`). Adds two columns with default 0; safe to run twice.

`POST /invoices` accepts **`invoice_discount_percent`** (0-100) *or* **`invoice_discount_amount`** (flat rupees), not both (400 otherwise). It applies to the goods / custom lines (billed service tickets are not discounted) on top of any line discounts, and it is rejected (400) if it is more than the value of those lines.

- The discount is spread over the lines in proportion to their value (after line discounts) **before GST**, and each line's share is added to its `discount_amount`, so taxable values, CGST/SGST/IGST and GSTR-1 reports stay correct with no report changes. The last line absorbs the rounding remainder, so shares add up exactly.
- `invoices.total_discount` therefore includes it; `invoice_discount_percent` / `invoice_discount_amount` remember what was entered (percent is 0 for a flat amount). Line `discount_percent` stays as typed on the line.
- The PDF totals show Item discounts / Invoice discount (x%) when present. Tests: `tests/test_validation.py`.

**Quotations too (v1.22.0):** run `db/migrations/020_quotation_discount.sql` (or `alembic upgrade head`; `alembic stamp 0016` if applied by hand). `POST/PUT /quotations` accept the same `invoice_discount_percent` / `invoice_discount_amount`, worked out the same way; converting a quotation to an invoice carries the discount over (percent if it was entered as a percent, else the flat amount).


## 2zzz. Billing and shipping address on invoices (v1.24.0)

**Upgrading:** run `db/migrations/021_billing_shipping_address.sql` (safe to re-run) or `alembic upgrade head` (revision `0017`).

- Customers: `address` is the billing address; the new `shipping_address` is optional (empty = same as billing). `PUT /customers/{id}`
  leaves `shipping_address` alone when the field isn't sent, so an older app can't wipe it.
- `POST /invoices` and `POST /invoices/from-quotation/{id}` take optional `billing_address` / `shipping_address`. Left out, the
  customer's saved addresses are used; `""` means none (shipping `""` = same as billing). Every invoice stores its own copy, so editing a
  customer later never changes an old invoice. `GET /invoices/{id}` returns both.
- The invoice PDF prints **Bill To** and **Ship To** side by side (up to 4 lines each; "Same as billing address" when they match).
  Invoices made before this version print the customer's address as billing.

## 2zzzz. Profile image (v1.25.0)

**Upgrading:** run `db/migrations/022_profile_image.sql` (safe to re-run) or `alembic upgrade head` (revision `0018`). It adds
`avatar_key` and `avatar_updated_at` to `users`. Optional setting: `MAX_PROFILE_IMAGE_MB` (default 2).

Every signed-in account (owner, staff, technician, platform admin) can set its own picture; each only ever sees its own.

- `POST /api/v1/profile/image` — multipart form, field **`image`**: a JPEG, PNG or WebP up to `MAX_PROFILE_IMAGE_MB`. The server
  checks the real content, applies the phone's rotation flag, crops to a centred square, shrinks it to at most 512 x 512 and stores
  it in R2 as a JPEG (`users/profile/<random>.jpg`) with all metadata (EXIF / GPS) removed, replacing any earlier image.
  Returns the profile. 400 for an unusable file.
- `GET /api/v1/profile/image` — my picture as a JPEG (404 when none). `GET /profile` has `has_image` and `image_updated_at`
  (use it to refresh a cached image); the storage key is never exposed.
- `DELETE /api/v1/profile/image` — removes it; returns the profile.
- **Team list (v1.25.0):** `GET /auth/team` (any signed-in user) and `GET /auth/users` (owner) now include `has_image` and
  `image_updated_at` for every person. `GET /api/v1/auth/team/{user_id}/image` (any signed-in user) returns a teammate's JPEG:
  active accounts only, never the platform admin; 404 when they have none. Load it only for rows where `has_image` is true, and
  use `image_updated_at` to know when to refetch.
- Changes are written to the audit log as "Changed own profile image" / "Removed own profile image" (action `UPDATE_PROFILE_IMAGE`).
- Like the logo, the image file lives in R2, not in the database backup. If it goes missing, just upload it again.

## 2zzzzz. AMC edit, renewal and contract PDF (v1.26.0)

**Upgrading:** run `db/migrations/023_amc_renewal.sql` (safe to re-run) or `alembic upgrade head` (revision `0019`). It adds
`amc_contracts.renewed_from_id`. No new settings.

All three endpoints are OWNER only, like the rest of the AMC contracts.

- `PUT /api/v1/amc/{id}` — edit an **ACTIVE** contract; only the fields sent change: `site_address`, `start_date`, `end_date`,
  `visit_frequency`, `amount`, `gst_rate`, `coverage_notes` (the customer can't change; make a new contract). 400 when nothing is
  sent, when a required field is sent as null, or when the end date is before the start date; 409 for a contract that isn't
  ACTIVE (set it to ACTIVE first with `PUT /amc/{id}/status`). When the dates or the frequency change, the **visit plan follows**:
  planned dates that are missing are added, and planned visits that no longer fit are removed — but only visits nobody has
  touched (still `SCHEDULED`, no technician, no remarks). Completed, missed and assigned visits are never deleted. Returns the
  contract with its visits, same shape as `GET /amc/{id}`.
- `POST /api/v1/amc/{id}/renew` — creates a **new** contract (new `AMC/…` number, new visit plan) that points back at this one.
  Body is optional; whatever is left out is carried over: the new term starts the day after the old one ends and lasts as long
  as the old one did (a 1 Jan – 31 Dec contract renews to 1 Jan – 31 Dec; a 1 Jan – 1 Jan one to 2 Jan – 1 Jan), and frequency,
  amount, GST rate, site and coverage notes are copied. Send `start_date`, `end_date`, `visit_frequency`, `amount`, `gst_rate`,
  `site_address` or `coverage_notes` to change them. An ACTIVE or EXPIRED contract can be renewed once: 409 if it already has a
  live renewal (a cancelled renewal doesn't count) and 409 for a CANCELLED contract. Returns 201 with the new contract.
- A renewed contract **stops showing as expiring**: it is left out of `GET /amc/expiring`, of `GET /amc?expiring_within_days=`
  and of the dashboard's `amcExpiringCount`. If its term has already ended it is marked EXPIRED at once; otherwise it stays
  ACTIVE until it ends, so its remaining visits stay on the technicians' lists.
- `GET /amc/{id}` now also returns `renewed_from_contract_no`, `renewed_to_id` and `renewed_to_contract_no`; list rows carry
  `renewed_to_id` (null when not renewed).
- `GET /api/v1/amc/{id}/pdf` — a printable contract for the customer: shop header and logo, customer and site, term, visit
  frequency, **amount excl. GST + GST + total** (the contract `amount` is treated as the price before GST), what is covered, the
  visit schedule, and a customer / authorised-signatory signature block (the saved signature is printed when there is one). A
  cancelled contract carries a CANCELLED stamp.
- Renewals appear in the audit log as "Renewed AMC/…" (action `RENEW`); edits as an ordinary `UPDATE`.
- A contract can't have more than 600 planned visits (400), which also catches a mistyped end date.

## 2zzzzzz. Ticket follow-ups: send back, add proof later, invoice lines from the ticket (v1.27.0)

No database change; nothing to migrate.

- **`POST /service-tickets/{id}/reopen`** (OWNER) `{reason, scheduled_date?}`: an `ATTENDED` ticket the owner isn't happy
  with goes back to `OPEN` (the technician sees it again and can attend it again). The first visit, its photos and the
  reason stay on the timeline (`REOPENED` event). Refused (409) when the ticket isn't `ATTENDED`, is already closed, or is
  billed on an invoice (cancel the invoice first: that un-bills the ticket). The job card PDF shows the new events.
- **`POST /service-tickets/{id}/addendum`** (STAFF / TECHNICIAN, same visibility rules as `/attend`): add 0-6 photos
  and / or a note (at least one) to a ticket that is `ATTENDED` and not yet closed. Same photo handling as `/attend`
  (re-encoded, no EXIF / GPS, uploaded before the transaction). Shown as an `ADDENDUM` event; the status does not change.
  A ticket holds at most 20 photos in total (400 beyond that). Same request-size limit as `/attend`.
- **`include_ticket_charges`** on `POST /invoices` (OWNER, `invoice_type: "SERVICE"`, with `service_ticket_ids`): adds one
  line per ticket in front of `items` — `Service <ticket no>: <title>`, quantity 1, the ticket's `charge_amount` and
  `gst_rate` — so the owner doesn't retype the price; `items` may then be empty or hold extra lines. A ticket with no
  charge amount is refused (400); in-warranty, other-customer, unattended and already-billed tickets are refused as before.
- Audit log actions `REOPEN` and `ADDENDUM` were added.

Tests: the `v1.27` blocks in `test_service_tickets` (`tests/test_api.py`) and `tests/test_body_limits.py`.

## 2zzzzzzz. Tickets everywhere: invoice PDF, dashboard, report, customer and unit views (v1.28.0)

No database change; nothing to migrate. All additions are new JSON keys / one new endpoint, so the installed Android app keeps working.

- **Invoice PDF** (`GET /invoices/{id}/pdf`): a SERVICE invoice that bills tickets prints `Service ref: TKT/…, TKT/…` under
  the bill-to block (at most two lines; a very long list ends with "..."). An invoice with no tickets prints as before.
- **Dashboard** (`GET /reports/dashboard`, OWNER): new `serviceTickets` object — `pending` (OPEN / RESCHEDULED, waiting on a
  technician), `overdue` (pending with a visit date before today, store time zone), `awaitingClose` (ATTENDED, waiting on the
  owner), `unbilledPaid` and `unbilledPaidAmount` (attended / closed PAID tickets not on an invoice, and their estimated
  charges ex-GST) — plus `ticketsAwaitingClose`: the five attended tickets waiting longest (`id, ticket_no, title,
  customer_name, attended_at`), the owner's to-do list.
- **`GET /reports/service-tickets?from=&to=`** (OWNER, both dates required, at most 10 years): tickets raised in the period
  (store time zone) — `total`, `by_status`, `by_type`, `by_technician` (`assigned` / `attended` per login, cancelled tickets
  not counted), `avg_hours_to_attend`, `avg_hours_to_close` (null when nothing qualifies). `overdue` and `unbilled_paid`
  (`count`, `amount`) describe the situation right now, whatever the dates.
- **Customer history** (`GET /customers/{id}/history`): new `service_tickets` (the customer's newest 50 tickets: number, title,
  type, status, dates, charge, invoice number, tracked unit's barcode) and `service_ticket_count` (all of them). They come with
  every page of invoices, so paging through invoices doesn't hide them.
- **Warranty unit** (`GET /warranties/{id}`, `GET /warranties/lookup/{barcode}`): new `service_tickets` — every ticket raised
  against that unit, newest first, so a unit's repair history is visible where it is looked up.

Tests: `tests/test_invoice_ticket_ref.py` (no database) and the `v1.28` blocks in `test_service_tickets` (`tests/test_api.py`).

## 2zzzzzzzz. Customer sign-off and AMC-covered tickets (v1.29.0)

**Upgrading:** run `db/migrations/024_ticket_amc_and_signoff.sql` (safe to re-run) or `alembic upgrade head` (revision `0020`; if you
applied the .sql by hand, `alembic stamp 0020`). It adds `service_tickets.amc_id`, allows the ticket type `AMC`, and creates
`service_ticket_signoffs`. Existing tickets are untouched. No new packages. Optional setting: `REQUIRE_CUSTOMER_SIGNOFF` (default `false`).
Everything is additive: an installed Android app that knows nothing about it keeps working.

### Customer sign-off on completion

The job card always had blank signature boxes; now the app can capture the customer's sign-off when the technician finishes, and
the card prints it.

- **`POST /service-tickets/{id}/attend`** (STAFF / TECHNICIAN) takes four more optional multipart fields next to `images` and `remarks`:

  | Field | Meaning |
  |---|---|
  | `customer_signature` | the customer's drawn signature as base64 text: raw, or a `data:image/png;base64,...` URL (what a signature pad / canvas `toDataURL()` gives). PNG, JPEG or WebP; at most 700,000 characters of base64 (about 500 KB of image; a drawn signature is a few KB). It is trimmed, shrunk to 600 px and stored in R2 as a PNG, like the invoice signature |
  | `customer_acknowledged` | `true` when the customer confirmed the work without drawing (said so, read out an OTP...) |
  | `customer_name` | who signed. **Required** whenever a signature or an acknowledgement is sent. A name on its own is ignored (the app may pre-fill it) |
  | `no_signoff_reason` | up to 300 characters: why there is none ("customer not at site", "refused"). Not allowed together with a signature / acknowledgement (400) |

  Bad input is a 400 (not valid base64, not an image, no name, both a sign-off and a reason); a ticket that can't be attended any more is
  refused before anything is stored. **`REQUIRE_CUSTOMER_SIGNOFF=true`** makes `/attend` refuse (400) unless it carries a signature /
  acknowledgement or a `no_signoff_reason`, so a technician can't skip it silently but is never stuck when the customer isn't there.
- **`POST /service-tickets/{id}/addendum`** takes the same fields, for a technician who couldn't get the signature at the time (a note, photos
  or the sign-off alone is enough now). A visit the customer has already signed off can't be signed again (409); a visit that only has a
  "no sign-off" reason can still be signed later.
- **What the ticket shows.** `GET /service-tickets/{id}` has `customer_signoff` (the one that counts for the *latest* visit, or `null`) and each
  timeline event has `signoff` (or `null`). A sign-off is `{id, event_id, customer_name, acknowledged, has_signature, obtained,
  not_obtained_reason, signed_at, captured_by_name}`; `obtained` is false for a "could not get one" note. `/attend` and `/addendum` answer
  with the ticket row plus `customer_signoff`. Every ticket row (list and detail) has `customer_signed_off` (true = the latest visit has a real
  sign-off), so the app can badge it. When the owner sends a ticket back (`/reopen`) and it is attended again, the new visit needs its own
  sign-off; the earlier one stays on the timeline.
- **`GET /service-tickets/{id}/signoffs/{signoff_id}/signature`**: the PNG (404 when there is none). Same visibility as the ticket (the owner;
  STAFF / TECHNICIAN only for tickets assigned to them or to nobody), so a login that can't see the ticket can't see its signature.
  Cached privately (a stored signature never changes); the storage key is never exposed.
- **Job card PDF** (`GET /service-tickets/{id}/pdf`): the customer's box now holds the signature image with the customer's name and the date
  under it ("Acknowledged by customer" when they didn't draw), or "Not obtained: <reason>"; with nothing recorded the box stays blank exactly
  as before. The technician box is unchanged.
- The picture is sent in a multipart form, which the audit log never records, so it can't end up there. It is validated and decoded before
  any photo is processed, uploaded to R2 together with the photos *before* the database transaction, and deleted again if the transaction
  fails. Like the photos, signatures live in R2, not in the database backup (the rows that point to them are in the backup).

### AMC link on tickets

AMC visits (`/amc/visits`, the planned periodic service) and tickets (a complaint or an extra call-out) stay separate, but a ticket can now be
raised *under* a contract, and says so.

- New ticket type **`AMC`** next to `IN_WARRANTY` and `PAID`. An AMC ticket always carries `amc_id` (the contract), and no other ticket does:
  PostgreSQL enforces it (`service_tickets_amc_link_check`). Ticket rows and details carry `amc_id` and `amc_contract_no`; there is no charge
  on an AMC ticket (`charge_amount` is dropped, like for an in-warranty one), so it is never billable: putting one on a SERVICE invoice is
  refused with "covered by an AMC contract" and it never appears under `billable=true`.
- **Raise one two ways** (OWNER only):
  - `POST /service-tickets` with `service_type: "AMC"`, `customer_id` and **`amc_id`**;
  - `POST /amc/{contract_id}/tickets` with `{title, description?, site_address?, warranty_barcode?, assigned_to?, scheduled_date?}`: the
    customer comes from the contract, and the site defaults to the contract's site (then the customer's address). This is what a
    "Raise ticket" button on the contract screen calls.
  The contract must exist (404), belong to that customer, be `ACTIVE`, and be in its term today; otherwise 400 with the reason ("ended on...",
  "only starts on...", "is cancelled") and a hint to renew it or raise a Paid service. A `amc_id` on a ticket that is not of type AMC is a 400.
- **`PUT /service-tickets/{id}`** can move a ticket to or from AMC: `{"service_type": "AMC", "amc_id": "..."}` links it (checked as above);
  changing the type away from AMC clears the link; `amc_id` alone on a non-AMC ticket is a 400. The timeline records it ("Service type: Paid
  service → AMC covered (AMC/…)").
- **Where it shows up:** `GET /amc/{id}` (also after edit / renew) now has `service_tickets` (the contract's newest 50: number, title, type, status,
  dates, `amc_contract_no`) and `service_ticket_count`; `GET /service-tickets?amc_id=` and `?service_type=AMC` filter the list; the customer's
  history and a warranty unit's ticket list show the type and contract number; `GET /reports/service-tickets` has an `AMC` bucket in `by_type`;
  the job card prints the contract number (never a price, so field staff may see it).
- Tickets stay on the contract they were raised under when it is renewed (the renewal is a new contract with its own tickets).
- The audit log files `POST /amc/{id}/tickets` as a `CREATE` of a `SERVICE_TICKET`.

Tests: `tests/test_ticket_amc_signoff.py` (the job card without a database; then raising, validating, re-typing and billing AMC tickets, the
database constraint, the sign-off with a signature / acknowledgement / reason, compulsory mode, bad input leaving nothing in storage,
signing later, and a sign-off after a send-back).

## 2zzzzzzzzz. Fix: invoice PDF without a paid amount (v1.29.1)

- `build_invoice_pdf` no longer raises `KeyError` when the invoice dict has no `paid_amount`; it is treated as 0 (invoices read from the database always have it). Fixes `tests/test_invoice_addresses.py::test_invoice_pdf_builds_with_both_addresses`.
- Test note: `tests/test_api.py` needs an EMPTY database, so run it first (or on its own) on a fresh DB. `db/schema.sql` alone is behind the migrations; load it and then `db/migrations/*.sql` in order.

## 2zzzzzzzzzz. Due dates, credit limit, ageing, and fixing wrong payments (v1.30.0)

**Run migration `db/migrations/025_due_dates_and_payment_reversal.sql`** (or `alembic upgrade head`; if you applied the
.sql by hand, `alembic stamp 0021`). It is safe to run twice.

### Payment terms, due dates, credit limit

- **Customers** have two new optional fields on `POST` / `PUT /customers`: `payment_terms_days` (0-365, the default for
  their new invoices) and `credit_limit` (null = no limit). On `PUT`, a field that is left out stays as it was; `null`
  clears it.
- **Invoices** (`POST /invoices`, `POST /invoices/from-quotation/{id}`) take `payment_terms_days` *or* `due_date` (not
  both; a date can't be in the past). Left out = the customer's default terms; none = no due date. The invoice keeps
  `due_date` and `payment_terms_days`.
- `PUT /invoices/{id}/due-date` changes it later: `{"payment_terms_days": 30}` (counted from the invoice date) or
  `{"due_date": "2026-12-01"}`; both `null` removes it.
- **Overdue rule, everywhere:** an invoice with no due date is due on its own date. Every invoice now carries
  `effective_due_date` and `days_overdue` (0 unless it is live, still owing and past due). `GET /invoices?overdue=true`
  lists only the overdue ones. The invoice PDF prints a `Due:` date while something is still owed.
- **Credit limit:** an invoice that would take the customer's outstanding balance (opening balance + unpaid live
  invoices) over `credit_limit` is refused with 409 and a message that says how much they owe. Send
  `"allow_over_credit_limit": true` to invoice anyway.
- **`GET /reports/receivables-ageing`** (OWNER): one row per customer who owes money, biggest first, split into
  `not_due`, `days_1_30`, `days_31_60`, `days_61_90`, `days_over_90`, plus `opening_balance` (no age), `overdue_amount`,
  `total_outstanding` and `credit_limit`. Headers `X-Total-Outstanding` and `X-Total-Overdue` give the totals over all
  pages. The older `/reports/outstanding-receivables` is unchanged.

### Payments: reverse, correct, print

A payment is never deleted. Both `/invoices/{id}/payments/...` and `/purchases/{id}/payments/...` now have:

- `POST .../{payment_id}/reverse` `{"reason": "..."}` (OWNER): books a **negative payment** that points at the original,
  marks the original `reversed_at` / `reversal_reason`, and recalculates `paid_amount` / `payment_status`. To correct an
  amount, reverse and record it again. 409 if the document is cancelled or the payment was already reversed; 400 on a
  refund / reversal row. Returns the updated document plus `reversal_id`.
- `PUT .../{payment_id}` fixes `payment_mode`, `payment_date`, `reference_no` and `notes` only (never the amount).
- `GET /invoices/{id}/payments/{payment_id}/receipt` and `GET /purchases/{id}/payments/{payment_id}/voucher`: one-page PDF
  with the amount in words, mode, reference, the document it is against, and the balance on the day it is printed. Not
  available for a reversed payment or a refund. The number printed is the first 8 characters of the payment id
  (`R-...` / `V-...`); there is no separate receipt sequence.
- The payment list now also returns `reversed_at`, `reversal_reason` and `reverses_payment_id`, so the app can strike
  through reversed payments. Sums over the list still add up to what was really received.
- All of these are written to the audit log (`UPDATE_PAYMENT`, `REVERSE_PAYMENT`, `UPDATE_DUE_DATE`).

Tests: `tests/test_due_dates_and_payments.py`.

## 2zzzzzzzzzzz. Pay-by-UPI QR on invoices (v1.31.0)

**Run migration `db/migrations/026_store_upi_id.sql`** (or `alembic upgrade head`; `alembic stamp 0022` if you applied the
.sql by hand). Safe to run twice.

- `PUT /store-profile` takes a new optional `upi_id` (like `shop@okhdfc`; checked for shape, 400-style 422 otherwise).
  Left out = unchanged, so an older app can't wipe it; `""` or `null` clears it. Like the bank details it is shown to the
  OWNER only (`GET /store-profile` hides it from staff and technicians).
- With a UPI ID saved, the invoice PDF prints a **"Scan to pay" QR code** under the bank details, for the **balance still
  owed** (total minus payments), with the invoice number as the payment note. It is left off when the invoice is fully
  paid, cancelled, or the shop has no UPI ID. It sits on the left, the signature on the right; if there is no room above
  the footer both move to a fresh page.
- `GET /invoices/{id}/upi-link` returns `{upi_id, amount, upi_uri}` - the same `upi://pay?...` link the QR carries - so the
  app can open a UPI app or share the link by message. 400 when the shop has no UPI ID or nothing is owed, 409 on a
  cancelled invoice. OWNER only.
- Any UPI app can pay it, but nothing tells the API the money arrived: record the payment as usual
  (`POST /invoices/{id}/payments`, mode `UPI`, the UTR as `reference_no`).

Tests: `tests/test_upi_qr.py`.

## v1.38.3
- Unit serial numbers now print as "S/N: ..." under the item name on the invoice, credit note, debit note and purchase PDFs (only when serials were recorded on that line).
