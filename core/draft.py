"""The RFQ the buyer is drafting. The co-pilot changes it only through these
functions, so every edit is explicit, checkable and listed in a change log."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from .rfq import QUESTIONNAIRE, RFQLine, load_rfq

# Paper by ply: (liner GSMs, flute GSMs, bursting factor) — same as the FY26 spec
PAPER = {3: ([150, 150], [120], "18 BF"), 5: ([180, 150, 150], [120, 120], "20 BF"),
         7: ([200, 180, 150, 180], [120, 120, 120], "22 BF")}
TAKE_UP, TRIM = 1.45, 1.04
TYPES = ["RSC", "Tray", "Die-cut", "Partition", "Pad"]


def box_weight_kg(box_type: str, dims: tuple[int, ...], ply: int) -> float:
    liners, flutes, _ = PAPER[ply]
    gsm = sum(liners) + sum(f * TAKE_UP for f in flutes)
    L, W = dims[0], dims[1]
    H = dims[2] if len(dims) > 2 else 0
    if box_type == "RSC":
        area = (2 * L + 2 * W + 35) * (H + W + 10)
    elif box_type == "Tray":
        area = (L + 2 * H) * (W + 2 * H)
    elif box_type == "Die-cut":
        area = (L + 2 * H + 40) * (W + 2 * H + 40) * 1.15
    elif box_type == "Partition":
        area = (L * H * 3 + W * H * 3) * 0.5
    else:
        area = L * W
    return round(area / 1e6 * gsm * TRIM / 1000, 3)


def paper_label(ply: int) -> str:
    liners, flutes, bf = PAPER[ply]
    return f"Liners {'/'.join(map(str, liners))} GSM, flutes {'/'.join(map(str, flutes))} GSM, {bf}"


@dataclass
class RFQDraft:
    lines: list[RFQLine] = field(default_factory=list)
    questionnaire: list[dict] = field(default_factory=list)
    terms: dict = field(default_factory=lambda: {
        "title": "Corrugated boxes FY27", "due_date": "2026-09-30",
        "delivery_point": "Bawal plant, Haryana", "payment_days": 60,
        "price_basis": "Delivered to plant, GST shown separately", "validity_days": 90})
    changes: list[str] = field(default_factory=list)
    sent: bool = False

    # ---- read ------------------------------------------------------------------
    def find(self, code: str) -> RFQLine | None:
        return next((l for l in self.lines if l.code.upper() == code.strip().upper()), None)

    def select(self, codes: list[str] | None = None, ply: int | None = None, box_type: str | None = None,
               text: str | None = None) -> list[RFQLine]:
        out = self.lines
        if codes:
            want = {c.strip().upper() for c in codes}
            out = [l for l in out if l.code.upper() in want]
        if ply:
            out = [l for l in out if l.ply == ply]
        if box_type:
            out = [l for l in out if l.box_type.lower() == box_type.lower()]
        if text:
            words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 1]
            out = [l for l in out if all(w in l.description.lower() for w in words)]
        return out

    def summary(self) -> str:
        spend = sum(l.ly_spend for l in self.lines if l.ly_price)
        return (f"{len(self.lines)} lines, {len(self.questionnaire)} questions, due {self.terms['due_date']}, "
                f"delivery {self.terms['delivery_point']}, payment {self.terms['payment_days']} days. "
                f"Value at last year's prices: ₹{spend/1e5:,.1f} L.")

    # ---- edit (each returns a one-line description of what changed) ---------------
    def start_from_last_year(self) -> str:
        self.lines = copy.deepcopy(load_rfq())
        self.questionnaire = copy.deepcopy(QUESTIONNAIRE)
        msg = f"Loaded last year's {len(self.lines)} lines and the 9-question supplier questionnaire"
        self.changes.append(msg)
        return msg

    def change_quantity(self, lines: list[RFQLine], percent: float | None = None,
                        new_qty: int | None = None) -> str:
        if not lines:
            return "No lines matched; nothing changed"
        for l in lines:
            l.annual_qty = int(new_qty) if new_qty is not None else int(round(l.annual_qty * (1 + percent / 100), -2))
        how = f"to {new_qty:,}" if new_qty is not None else f"by {percent:+g}%"
        msg = f"Changed annual quantity {how} on {', '.join(l.code for l in lines)}"
        self.changes.append(msg)
        return msg

    def update_line(self, code: str, **fields) -> str:
        l = self.find(code)
        if l is None:
            return f"No line {code} in the draft"
        done = []
        if fields.get("description"):
            l.description = fields["description"]; done.append("description")
        if fields.get("print_colours") is not None:
            l.print_colours = int(fields["print_colours"]); done.append(f"print {l.print_colours} colour")
        if fields.get("ply"):
            l.ply = int(fields["ply"]); l.paper_spec = paper_label(l.ply); done.append(f"{l.ply}-ply")
        if fields.get("dims_mm"):
            l.dims_mm = tuple(int(x) for x in fields["dims_mm"]); done.append(f"size {l.dims_label} mm")
        if fields.get("annual_qty"):
            l.annual_qty = int(fields["annual_qty"]); done.append(f"qty {l.annual_qty:,}")
        if fields.get("ply") or fields.get("dims_mm"):
            l.box_weight_kg = box_weight_kg(l.box_type, l.dims_mm, l.ply)
            l.ly_price = None  # spec changed: last year's price no longer comparable
            done.append("last-year price cleared (spec changed)")
        msg = f"Updated {code}: {', '.join(done) or 'nothing'}"
        self.changes.append(msg)
        return msg

    def add_line(self, description: str, box_type: str, dims_mm: list[int], ply: int,
                 print_colours: int, annual_qty: int) -> str:
        box_type = next((t for t in TYPES if t.lower() == box_type.lower()), "RSC")
        prefix = {"RSC": f"CB-{ply}", "Tray": f"CB-{ply}", "Die-cut": "DC-1", "Partition": "PT-1", "Pad": "PD-1"}[box_type]
        used = {l.code for l in self.lines}
        n = 1
        while f"{prefix}{n:02d}" in used:
            n += 1
        code = f"{prefix}{n:02d}"
        dims = tuple(int(x) for x in dims_mm)
        self.lines.append(RFQLine(
            line=len(self.lines) + 1, code=code, description=description, box_type=box_type, dims_mm=dims,
            ply=int(ply), flute={3: "B", 5: "BC", 7: "BCB"}[int(ply)], paper_spec=paper_label(int(ply)),
            print_colours=int(print_colours), annual_qty=int(annual_qty),
            box_weight_kg=box_weight_kg(box_type, dims, int(ply)), ly_rate_per_kg=None, ly_price=None,
            notes="new this year"))
        msg = f"Added {code} {description} ({box_type}, {'x'.join(map(str, dims))} mm, {ply}-ply, qty {int(annual_qty):,})"
        self.changes.append(msg)
        return msg

    def remove_lines(self, codes: list[str]) -> str:
        gone = [l.code for l in self.select(codes=codes)]
        self.lines = [l for l in self.lines if l.code not in gone]
        for i, l in enumerate(self.lines, 1):
            l.line = i
        msg = f"Removed {', '.join(gone)}" if gone else "No matching lines to remove"
        self.changes.append(msg)
        return msg

    def set_terms(self, **terms) -> str:
        done = {k: v for k, v in terms.items() if v not in (None, "") and k in self.terms}
        self.terms.update(done)
        msg = "Set " + ", ".join(f"{k.replace('_', ' ')} = {v}" for k, v in done.items()) if done else "No terms changed"
        self.changes.append(msg)
        return msg

    def add_question(self, question: str, qtype: str = "info") -> str:
        no = max((q["no"] for q in self.questionnaire), default=0) + 1
        self.questionnaire.append({"no": no, "question": question, "type": qtype})
        msg = f"Added question {no} ({qtype}): {question}"
        self.changes.append(msg)
        return msg

    def set_question_type(self, no: int, qtype: str) -> str:
        q = next((q for q in self.questionnaire if q["no"] == no), None)
        if q is None:
            return f"No question {no}"
        q["type"] = qtype
        msg = f"Question {no} is now {qtype}"
        self.changes.append(msg)
        return msg
