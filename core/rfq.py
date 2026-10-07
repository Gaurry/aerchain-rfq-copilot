"""The buyer's side of the world: the 30-line RFQ, last year's prices,
the questionnaire and default assumptions. Everything vendors send is
matched and normalized against this."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"
MASTER = DATA / "rfq" / "rfq_master.xlsx"


@dataclass
class RFQLine:
    line: int
    code: str
    description: str
    box_type: str
    dims_mm: tuple[int, ...]
    ply: int
    flute: str
    paper_spec: str
    print_colours: int
    annual_qty: int
    box_weight_kg: float
    ly_rate_per_kg: float | None
    ly_price: float | None          # None = new line, no history
    notes: str = ""

    @property
    def dims_label(self) -> str:
        return "x".join(str(d) for d in self.dims_mm)

    @property
    def ly_spend(self) -> float:
        return (self.ly_price or 0.0) * self.annual_qty


@dataclass
class Assumptions:
    """Every number the system assumes rather than reads. Editable in the UI;
    changing one re-computes every price and every answer."""
    fx_usd_inr: float = 88.0
    freight_per_kg: float = 2.0
    gst_rate: float = 0.18
    apply_conditional_discounts: bool = True
    # Last year's print adder per box by colour count (from FY26 contract)
    ly_print_adder: dict[int, float] = field(default_factory=lambda: {0: 0.0, 1: 0.60, 2: 1.10, 4: 2.40})
    size_deviation_review: float = 0.05   # >5% off on any dimension → needs review
    price_sanity_band: float = 0.40       # >40% away from last year → needs review
    rfq_due_date: str = "2026-09-30"

    def to_dict(self) -> dict:
        return asdict(self)


QUESTIONNAIRE = [
    {"no": 1, "question": "Valid ISO 9001:2015 certificate (attach copy)", "type": "mandatory"},
    {"no": 2, "question": "Bursting strength / ECT test report with every lot", "type": "mandatory"},
    {"no": 3, "question": "Monthly production capacity (MT)", "type": "info"},
    {"no": 4, "question": "Lead time from PO (days), must be 10 or fewer", "type": "mandatory"},
    {"no": 5, "question": "Food-safe, low-migration inks", "type": "mandatory"},
    {"no": 6, "question": "Accept 60-day payment terms", "type": "preferred"},
    {"no": 7, "question": "Price validity (days), 90+ preferred", "type": "preferred"},
    {"no": 8, "question": "Raw-material escalation mechanism", "type": "info"},
    {"no": 9, "question": "Customer rejection rate, last 12 months (%)", "type": "info"},
]


def load_rfq() -> list[RFQLine]:
    df = pd.read_excel(MASTER, sheet_name="RFQ Line Items")
    df = df[pd.to_numeric(df["Line"], errors="coerce").notna()]
    lines = []
    for _, r in df.iterrows():
        print_txt = str(r["Print"])
        colours = 0 if print_txt == "Plain" else int(print_txt[0])
        lines.append(RFQLine(
            line=int(r["Line"]), code=r["Item code"], description=r["Description"],
            box_type=r["Box type"],
            dims_mm=tuple(int(x) for x in str(r["Size LxWxH (mm)"]).split("x")),
            ply=int(r["Ply"]), flute=str(r["Flute"]), paper_spec=r["Paper spec"],
            print_colours=colours, annual_qty=int(r["Annual qty (pcs)"]),
            box_weight_kg=float(r["Box weight (g)"]) / 1000,
            ly_rate_per_kg=float(r["Last-year rate (₹/kg)"]),
            ly_price=float(r["Last-year price (₹/pc)"]),
            notes="" if pd.isna(r["Notes"]) else str(r["Notes"]),
        ))
    return lines


def load_vendor_history() -> pd.DataFrame:
    """Prior questionnaire answers on file (only Om Sai, the incumbent)."""
    return pd.read_excel(MASTER, sheet_name="Vendor History")


def incumbents() -> set[str]:
    """Vendor names (as on file) that held last year's contract, so 'same as last year' means something."""
    try:
        h = load_vendor_history()
    except Exception:
        return set()
    rows = h[(h["Item"].astype(str) == "Status") & h["Value"].astype(str).str.contains("ncumbent")]
    return set(rows["Vendor"].astype(str))


def is_incumbent(vendor: str) -> bool:
    v = vendor.strip().lower()
    return any(n.lower().startswith(v) or v.startswith(n.lower()) for n in incumbents())
