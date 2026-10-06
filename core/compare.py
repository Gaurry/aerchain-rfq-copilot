"""One place that turns extracted responses into the comparison every screen
uses: the grid, the review queue, award scenarios and (later) the analyst.

Buyer decisions are applied here, never by editing extracted data:
  approved  — (vendor, code, flag text) the buyer has accepted
  corrected — (vendor, code) → ₹/pc the buyer typed in
  excluded  — (vendor, code) treated as not comparable"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .matching import match_vendor
from .normalize import Flag, NormalizedPrice, Step, normalize
from .quality import Gate, assess
from .rfq import Assumptions, RFQLine
from .schema import AIMatch, QuotedItem, VendorResponse

ROOT = Path(__file__).resolve().parent.parent
CORRECTIONS_LOG = ROOT / "data" / "corrections.jsonl"
OPEN = ("review", "risk")


@dataclass
class Decisions:
    approved: set = field(default_factory=set)      # {(vendor, code, flag_text)}
    corrected: dict = field(default_factory=dict)   # {(vendor, code): price}
    excluded: set = field(default_factory=set)      # {(vendor, code)}
    asked: set = field(default_factory=set)         # {(vendor, code, flag_text)} sent back to the vendor


@dataclass
class Comparison:
    lines: list[RFQLine]
    vendors: list[str]
    cells: dict          # (vendor, code) → NormalizedPrice (primary quote)
    alternates: dict     # (vendor, code) → NormalizedPrice
    unmatched: dict      # vendor → list[QuotedItem]
    gates: dict          # vendor → Gate
    responses: dict      # vendor → VendorResponse

    def open_flags(self, vendor: str, code: str, d: Decisions) -> list[Flag]:
        c = self.cells.get((vendor, code))
        if c is None:
            return []
        return [f for f in c.flags if f.severity in OPEN and (vendor, code, f.text) not in d.approved
                and (vendor, code, f.text) not in d.asked]

    def awaiting_vendor(self, vendor: str, code: str, d: Decisions) -> list[Flag]:
        c = self.cells.get((vendor, code))
        return [] if c is None else [f for f in c.flags if (vendor, code, f.text) in d.asked]


def build(resps: dict[str, VendorResponse], ai: dict[str, list[AIMatch]], lines: list[RFQLine],
          a: Assumptions, d: Decisions | None = None) -> Comparison:
    d = d or Decisions()
    by = {l.code: l for l in lines}
    cells, alts, unmatched, gates = {}, {}, {}, {}
    for v, r in resps.items():
        ms, um = match_vendor(r, lines, ai.get(v))
        unmatched[v] = [r.items[i] for i in um]
        gates[v] = assess(r, a)
        for m in ms:
            it = r.items[m.item_index]
            n = normalize(by[m.rfq_code], r, it, m, a)
            (alts if it.is_alternate_spec else cells)[(v, m.rfq_code)] = n
    for (v, code), n in cells.items():
        if (v, code) in d.excluded:
            n.price = None
            n.flags.append(Flag("info", "Excluded by the buyer"))
        elif (v, code) in d.corrected:
            n.price = float(d.corrected[(v, code)])
            n.steps.append(Step("Buyer corrected the value", n.price, "₹/pc"))
            n.flags.append(Flag("info", "Corrected by the buyer"))
    return Comparison(lines, list(resps), cells, alts, unmatched, gates, resps)


ALLOWED = {
    "Cleared only": {"cleared", "cleared_stale"},
    "Cleared + conditional": {"cleared", "cleared_stale", "conditional"},
    "All vendors": {"cleared", "cleared_stale", "conditional", "failed", "unknown"},
}


def l1(comp: Comparison, allowed_status: set[str], exclude_vendors: set[str] = frozenset()) -> dict[str, tuple[str, float]]:
    """Cheapest comparable quote per line among vendors whose gate status is allowed."""
    out = {}
    for ln in comp.lines:
        best = None
        for v in comp.vendors:
            if v in exclude_vendors or comp.gates[v].status not in allowed_status:
                continue
            c = comp.cells.get((v, ln.code))
            if c is None or c.price is None:
                continue
            if best is None or c.price < best[1]:
                best = (v, c.price)
        if best:
            out[ln.code] = best
    return out


def award_total(comp: Comparison, winners: dict[str, tuple[str, float]]) -> float:
    q = {l.code: l.annual_qty for l in comp.lines}
    return sum(p * q[code] for code, (_, p) in winners.items())


def log_correction(record: dict) -> None:
    """Every buyer decision is kept; corrections become future test cases."""
    CORRECTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with CORRECTIONS_LOG.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
