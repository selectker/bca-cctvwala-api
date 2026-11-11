"""UPI payment links (the `upi://pay?...` deep link that every UPI app understands, and that the invoice QR code carries)."""
from decimal import Decimal
from urllib.parse import quote


def upi_payment_uri(vpa: str, payee_name: str, amount: Decimal, note: str) -> str:
    """Link that opens a UPI app with the payee, amount and note filled in. The amount is the invoice balance."""
    parts = [
        ("pa", vpa.strip()),
        ("pn", (payee_name or "").strip()[:60]),
        ("am", f"{Decimal(str(amount)):.2f}"),
        ("cu", "INR"),
        ("tn", (note or "").strip()[:50]),
    ]
    return "upi://pay?" + "&".join(f"{k}={quote(v, safe='@.-_')}" for k, v in parts if v)
