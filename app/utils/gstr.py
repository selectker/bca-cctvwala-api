"""GSTR-1 style sales reporting, built from invoice rows: pure functions (no database), so they are easy to test.

The sections follow the GST return so an accountant can copy figures straight in:
  b2b   registered buyers (the customer has a GSTIN), one row per invoice per tax rate
  b2cl  unregistered buyers, inter-state, invoice value above B2CL_LIMIT, one row per invoice per rate
  b2cs  all other unregistered sales, totalled by place of supply and rate
  hsn   totals by HSN / SAC, unit and rate
  docs  the invoice number range issued, how many were cancelled
  cdn   credit notes issued in the period (registered and unregistered buyers), one row per note per tax rate; the sales
        sections above stay as invoiced, the credit notes are shown separately and netted in `net_totals`
"""
from __future__ import annotations

import csv
import io
import zipfile
from decimal import Decimal

from app.utils.gst import round2, to_decimal

# Since Aug 2024 an inter-state invoice to an unregistered buyer is "large" above Rs. 1,00,000 (it was 2,50,000 before).
B2CL_LIMIT = Decimal("100000")
MAX_RANGE_DAYS = 400  # one return period is a month or a quarter; a financial year still fits

STATE_NAMES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
    "17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh",
    "23": "Madhya Pradesh", "24": "Gujarat", "26": "Dadra & Nagar Haveli and Daman & Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman & Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory", "99": "Other Country",
}


def place_of_supply(code: str | None) -> str:
    """'29-Karnataka', the way the GST portal writes it."""
    if not code:
        return ""
    return f"{code}-{STATE_NAMES[code]}" if code in STATE_NAMES else str(code)


def _zero() -> dict:
    return {"taxable": Decimal("0"), "cgst": Decimal("0"), "sgst": Decimal("0"), "igst": Decimal("0")}


def _add(into: dict, row: dict) -> None:
    for k in ("taxable", "cgst", "sgst", "igst"):
        into[k] += to_decimal(row[k] or 0)


def _money(d: dict) -> dict:
    return {k: round2(v) if isinstance(v, Decimal) else v for k, v in d.items()}


def build_sales_sections(rate_rows: list[dict], store_state_code: str | None) -> dict:
    """rate_rows: one row per (active invoice, tax rate) with invoice_no, invoice_date, total_amount,
    place_of_supply_state_code, is_interstate, customer_name, customer_gstin, gst_rate, taxable, cgst, sgst, igst."""
    b2b, b2cl = [], []
    b2cs: dict[tuple[str, Decimal], dict] = {}
    by_rate: dict[Decimal, dict] = {}
    for r in rate_rows:
        rate = to_decimal(r["gst_rate"])
        gstin = (r.get("customer_gstin") or "").strip()
        pos = r.get("place_of_supply_state_code") or store_state_code or ""
        line = {
            "invoice_no": r["invoice_no"], "invoice_date": r["invoice_date"], "customer_name": r["customer_name"],
            "customer_gstin": gstin, "place_of_supply": place_of_supply(pos), "place_of_supply_code": pos,
            "invoice_value": round2(r["total_amount"]), "rate": rate,
            "taxable": round2(r["taxable"]), "cgst": round2(r["cgst"]), "sgst": round2(r["sgst"]), "igst": round2(r["igst"]),
        }
        _add(by_rate.setdefault(rate, _zero()), r)
        if gstin:
            b2b.append(line)
        elif r["is_interstate"] and to_decimal(r["total_amount"]) > B2CL_LIMIT:
            b2cl.append(line)
        else:
            bucket = b2cs.setdefault((pos, rate), _zero())
            _add(bucket, r)

    b2cs_rows = [
        {"place_of_supply": place_of_supply(pos), "place_of_supply_code": pos, "type": "OE", "rate": rate, **_money(v)}
        for (pos, rate), v in sorted(b2cs.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    ]
    rate_summary = [{"rate": rate, **_money(v)} for rate, v in sorted(by_rate.items())]
    return {"b2b": b2b, "b2cl": b2cl, "b2cs": b2cs_rows, "rate_summary": rate_summary}


def build_cdn_rows(rate_rows: list[dict], store_state_code: str | None) -> list[dict]:
    """rate_rows: one row per (active credit note, tax rate): credit_note_no, note_date, invoice_no, invoice_date, total_amount,
    place_of_supply_state_code, customer_name, customer_gstin, gst_rate, taxable, cgst, sgst, igst."""
    out = []
    for r in rate_rows:
        gstin = (r.get("customer_gstin") or "").strip()
        pos = r.get("place_of_supply_state_code") or store_state_code or ""
        out.append({
            "credit_note_no": r["credit_note_no"], "note_date": r["note_date"], "invoice_no": r["invoice_no"],
            "invoice_date": r["invoice_date"], "customer_name": r["customer_name"], "customer_gstin": gstin,
            "registered": bool(gstin), "place_of_supply": place_of_supply(pos), "note_value": round2(r["total_amount"]),
            "rate": to_decimal(r["gst_rate"]), "taxable": round2(r["taxable"]), "cgst": round2(r["cgst"]),
            "sgst": round2(r["sgst"]), "igst": round2(r["igst"]),
        })
    return out


def build_document_summary(invoices: list[dict]) -> dict:
    """invoices: every invoice in the period (active AND cancelled) as dicts with invoice_no and status, in the order
    they were issued."""
    if not invoices:
        return {"from_no": None, "to_no": None, "total_issued": 0, "cancelled": 0, "net_issued": 0}
    cancelled = sum(1 for i in invoices if i["status"] == "CANCELLED")
    return {
        "from_no": invoices[0]["invoice_no"], "to_no": invoices[-1]["invoice_no"],
        "total_issued": len(invoices), "cancelled": cancelled, "net_issued": len(invoices) - cancelled,
    }


def build_hsn_rows(rows: list[dict]) -> list[dict]:
    """rows: grouped by (hsn, unit, rate) in SQL. Fills the rounded money and flags lines with no code."""
    out = []
    for r in rows:
        taxable = round2(r["taxable"])
        cgst, sgst, igst = round2(r["cgst"]), round2(r["sgst"]), round2(r["igst"])
        out.append({
            "hsn_code": r["hsn_code"] or "", "description": r["description"] or "", "unit": r["unit"] or "NA",
            "rate": to_decimal(r["gst_rate"]), "quantity": to_decimal(r["quantity"]),
            "taxable": taxable, "cgst": cgst, "sgst": sgst, "igst": igst,
            "total_value": round2(taxable + cgst + sgst + igst), "missing_hsn": not r["hsn_code"],
        })
    return out


# ------------------------------------------------------------------------------------------------ CSV

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    """A CSV cell. Text that a spreadsheet would run as a formula (a customer named '=HYPERLINK(...)') gets a leading
    apostrophe; a plain negative number stays a number."""
    if value is None:
        return ""
    if isinstance(value, (int, float, Decimal)):
        return f"{value:.2f}" if isinstance(value, (float, Decimal)) else str(value)
    text = str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def to_csv(header: list[str], rows: list[list]) -> bytes:
    """UTF-8 with a byte-order mark, so Excel opens Indian names and symbols correctly."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return buf.getvalue().encode("utf-8-sig")


_B2B_HEADER = ["GSTIN/UIN of Recipient", "Receiver Name", "Invoice Number", "Invoice date", "Invoice Value",
               "Place Of Supply", "Rate", "Taxable Value", "CGST", "SGST", "IGST"]
_HSN_HEADER = ["HSN", "Description", "UQC", "Rate", "Total Quantity", "Total Value", "Taxable Value", "IGST", "CGST", "SGST",
               "Missing HSN"]


_CDN_HEADER = ["GSTIN/UIN of Recipient", "Receiver Name", "Note Number", "Note date", "Note Type", "Place Of Supply", "Note Value",
               "Rate", "Taxable Value", "CGST", "SGST", "IGST", "Against Invoice", "Invoice date", "Registered"]


def sales_csvs(sections: dict, hsn: list[dict], docs: dict) -> dict[str, bytes]:
    """File name -> CSV bytes for every section."""
    inv_rows = lambda items: [  # noqa: E731
        [r["customer_gstin"], r["customer_name"], r["invoice_no"], r["invoice_date"].strftime("%d-%m-%Y"), r["invoice_value"],
         r["place_of_supply"], r["rate"], r["taxable"], r["cgst"], r["sgst"], r["igst"]] for r in items
    ]
    b2cl_header = ["Receiver Name", "Invoice Number", "Invoice date", "Invoice Value", "Place Of Supply", "Rate",
                   "Taxable Value", "CGST", "SGST", "IGST"]
    return {
        "b2b.csv": to_csv(_B2B_HEADER, inv_rows(sections["b2b"])),
        "b2cl.csv": to_csv(b2cl_header, [r[1:] for r in inv_rows(sections["b2cl"])]),
        "b2cs.csv": to_csv(
            ["Type", "Place Of Supply", "Rate", "Taxable Value", "CGST", "SGST", "IGST"],
            [[r["type"], r["place_of_supply"], r["rate"], r["taxable"], r["cgst"], r["sgst"], r["igst"]] for r in sections["b2cs"]],
        ),
        "hsn.csv": to_csv(_HSN_HEADER, [
            [r["hsn_code"], r["description"], r["unit"], r["rate"], r["quantity"], r["total_value"], r["taxable"],
             r["igst"], r["cgst"], r["sgst"], "YES" if r["missing_hsn"] else ""] for r in hsn
        ]),
        "cdn.csv": to_csv(_CDN_HEADER, [
            [r["customer_gstin"], r["customer_name"], r["credit_note_no"], r["note_date"].strftime("%d-%m-%Y"), "C",
             r["place_of_supply"], r["note_value"], r["rate"], r["taxable"], r["cgst"], r["sgst"], r["igst"],
             r["invoice_no"], r["invoice_date"].strftime("%d-%m-%Y"), "YES" if r["registered"] else "NO"]
            for r in sections.get("cdn", [])]),
        "docs.csv": to_csv(
            ["Document", "Sr. No. From", "Sr. No. To", "Total Number", "Cancelled", "Net Issued"],
            [["Invoices for outward supply", docs["from_no"], docs["to_no"], docs["total_issued"], docs["cancelled"], docs["net_issued"]]]
            + ([["Credit Note", docs["cn_from_no"], docs["cn_to_no"], docs["cn_issued"], docs["cn_cancelled"], docs["cn_issued"] - docs["cn_cancelled"]]]
               if docs.get("cn_issued") else []),
        ),
    }


def zip_files(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()
