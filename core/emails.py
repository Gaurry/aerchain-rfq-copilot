"""Buyer → vendor emails: the RFQ itself, and follow-ups raised during review."""
from __future__ import annotations

from pathlib import Path

import anthropic

PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "vendor_email.md"

CONTACTS = {
    "Kraftline": ("Neha Kapoor", "sales@kraftline-pkg.example", "email"),
    "Shree Balaji": ("Mahesh Agarwal", "quotes@shreebalaji.example", "email"),
    "Pacific": ("Anand Krishnan", "north.accounts@pacificboxboard.example", "email"),
    "Gupta": ("Sanjay Gupta", "+91 98XXX XXXXX", "WhatsApp"),
    "Om Sai": ("Rakesh Yadav", "rakesh@omsaipackaging.example", "email"),
}


def nice_date(iso: str) -> str:
    from datetime import date
    try:
        return date.fromisoformat(iso).strftime("%-d %b %Y")
    except Exception:
        return iso


def rfq_email(vendor: str, terms: dict, n_lines: int, n_questions: int) -> dict:
    name, addr, channel = CONTACTS.get(vendor, ("Team", "", "email"))
    due = nice_date(terms["due_date"])
    if channel == "WhatsApp":
        body = (f"Namaste {name.split()[0]} ji, this is Procurement (Packaging), Bawal plant. "
                f"Sharing our RFQ for {n_lines} corrugated box items for FY27, Excel attached. "
                f"Please send your rates by {due}, delivered to Bawal, GST separate. "
                f"Also answer the {n_questions} quality questions in the second sheet. Thanks.")
        return {"channel": "WhatsApp", "to": addr, "subject": "", "body": body}
    body = (f"Dear {name},\n\nPlease find attached our RFQ for {terms['title']}: {n_lines} line items with "
            f"annual quantities, sizes and paper specs.\n\n"
            f"- Quote per piece. Basis: {terms['price_basis']}\n"
            f"- Payment terms: {terms['payment_days']} days\n"
            f"- Price validity: at least {terms['validity_days']} days\n"
            f"- Please also answer the {n_questions}-question supplier questionnaire (sheet 2) and attach your ISO 9001 certificate\n\n"
            f"Kindly reply by {due}. Any format is fine.\n\n"
            f"Regards,\nProcurement – Packaging, {terms['delivery_point']}")
    return {"channel": "Email", "to": addr, "subject": f"RFQ: {terms['title']} — reply by {due}", "body": body}


def draft_followup(client: anthropic.Anthropic, model: str, vendor: str, kind: str, points: list[str]) -> str:
    name = CONTACTS.get(vendor, ("Team",))[0]
    msg = client.messages.create(
        model=model, max_tokens=800, system=PROMPT.read_text(),
        messages=[{"role": "user", "content": f"Kind: {kind}\nVendor contact: {name} at {vendor}\nPoints:\n" +
                   "\n".join(f"- {p}" for p in points)}])
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def template_followup(vendor: str, kind: str, points: list[str]) -> str:
    name = CONTACTS.get(vendor, ("Team",))[0]
    head = ("we need a few clarifications before we can compare your quote:" if kind == "clarify"
            else "to avoid any misunderstanding, this is how we have read your quote:")
    pts = "\n".join(f"{i}. {p}" for i, p in enumerate(points, 1))
    ask = "Please reply with the details" if kind == "clarify" else 'Please reply "confirmed" or correct anything above'
    return f"Dear {name},\n\nThank you for your quotation. {head.capitalize()}\n\n{pts}\n\n{ask} within 2 working days.\n\nRegards,\nProcurement – Packaging, Bawal Plant"
