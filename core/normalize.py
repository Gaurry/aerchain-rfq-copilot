"""Turn any quoted price into one comparable number: ₹ per piece, landed at
the buyer's plant, before GST.

Every step is recorded in a ledger the buyer can open from the comparison
grid. Nothing here calls the AI; it is plain arithmetic on extracted values
plus the editable Assumptions."""
from __future__ import annotations

from dataclasses import dataclass, field

from .rfq import Assumptions, RFQLine, is_incumbent
from .schema import Match, PriceUnit, QuotedItem, VendorResponse


@dataclass
class Step:
    label: str
    value: float
    unit: str


@dataclass
class Flag:
    severity: str          # "info" | "review" | "risk"
    text: str


@dataclass
class NormalizedPrice:
    vendor: str
    rfq_code: str
    item_index: int | None
    price: float | None                    # ₹/pc landed ex-GST; None = not comparable
    is_alternate: bool = False
    steps: list[Step] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    match_confidence: float = 0.0
    evidence: str | None = None

    @property
    def needs_review(self) -> bool:
        return any(f.severity in ("review", "risk") for f in self.flags)


def _print_adder_per_piece(resp: VendorResponse, colours: int) -> tuple[float, str] | None:
    for a in resp.adders:
        if a.unit == "per_100_per_colour":
            return a.amount / 100 * colours, a.description
        if a.unit == "per_piece_per_colour":
            return a.amount * colours, a.description
    return None


def normalize(line: RFQLine, resp: VendorResponse, item: QuotedItem, match: Match,
              a: Assumptions) -> NormalizedPrice:
    out = NormalizedPrice(vendor=resp.vendor_name, rfq_code=line.code, item_index=match.item_index,
                          price=None, is_alternate=item.is_alternate_spec,
                          match_confidence=match.confidence, evidence=item.evidence.quote)
    S, F = out.steps.append, out.flags.append
    w = line.box_weight_kg

    # --- base price, as quoted --------------------------------------------
    if item.price_unit == PriceUnit.same_as_last_year:
        if not is_incumbent(resp.vendor_name):
            F(Flag("risk", f"{resp.vendor_name} wrote '{item.price_text}', but we have no earlier prices from them. "
                           "Not comparable until they send rates"))
            return out
        if line.ly_price is None:
            F(Flag("risk", "'Same as last year' but this line has no last-year price"))
            return out
        v = line.ly_price
        S(Step(f"'{item.price_text}' → FY26 contract price", v, "₹/pc"))
        F(Flag("review", f"Resolved '{item.scope_description or item.price_text}' to last year's contract prices"))
    else:
        if item.price_value is None or item.price_unit == PriceUnit.unknown:
            F(Flag("risk", "Price or unit could not be read"))
            return out
        v = item.price_value
        cur = item.currency
        if cur == "unknown":
            known = [i.currency for i in resp.items if i.currency in ("INR", "USD")]
            if known:
                cur = max(set(known), key=known.count)
                F(Flag("review", f"Currency not written next to this price; assumed {cur} like the rest of the response"))
        S(Step(f"Quoted: {item.price_text}", v, f"{cur} {item.price_unit.value.replace('_', ' ')}"))

        if cur == "USD":
            v *= a.fx_usd_inr
            S(Step(f"Convert USD at ₹{a.fx_usd_inr:.2f}", v, "₹"))
            F(Flag("review", f"Quoted in USD; converted at ₹{a.fx_usd_inr:.2f} (RBI reference rate, 30 Sep 2026, assumed). "
                             "Invoice rate may differ"))
        elif cur not in ("INR",):
            F(Flag("risk", f"Currency '{cur}' not recognised"))

        if item.price_unit == PriceUnit.per_100:
            v /= 100; S(Step("Per 100 → per piece", v, "₹/pc"))
        elif item.price_unit == PriceUnit.per_1000:
            v /= 1000; S(Step("Per 1,000 → per piece", v, "₹/pc"))
        elif item.price_unit == PriceUnit.per_kg:
            v *= w; S(Step(f"× box weight {w:.3f} kg", v, "₹/pc"))
            F(Flag("info", "Per-kg rate converted using our computed box weight"))

        if item.scope == "category" and item.price_unit == PriceUnit.per_kg and line.print_colours:
            add = a.ly_print_adder.get(line.print_colours, 0.0)
            v += add
            S(Step(f"+ print, {line.print_colours} colour (last year's adder)", v, "₹/pc"))
            F(Flag("review", "Blanket per-kg rate assumed to exclude printing; last year's print adder applied"))

        if item.plain_box_rate and line.print_colours:
            got = _print_adder_per_piece(resp, line.print_colours)
            if got:
                v += got[0]
                S(Step(f"+ print, {line.print_colours} colour ({got[1]})", v, "₹/pc"))
            else:
                F(Flag("risk", "Plain-box rate, but no printing cost given"))

    # --- tax -----------------------------------------------------------------
    if resp.gst_treatment == "inclusive":
        rate = (resp.gst_rate_percent or a.gst_rate * 100) / 100
        v /= (1 + rate)
        S(Step(f"Remove GST {rate*100:.0f}%", v, "₹/pc"))
        F(Flag("info", "Quoted inclusive of GST; compared ex-GST"))
    elif resp.gst_treatment == "not_stated":
        F(Flag("review", "GST basis not stated; assumed exclusive"))

    # --- discounts -----------------------------------------------------------
    for d in resp.discounts:
        if d.condition and not a.apply_conditional_discounts:
            F(Flag("review", f"Conditional discount not applied: {d.description} ({d.condition})"))
            continue
        v *= (1 - d.percent / 100)
        S(Step(f"− {d.percent:g}% {d.description}", v, "₹/pc"))
        if d.condition:
            F(Flag("review", f"Discount is conditional: {d.condition}"))

    # --- freight -------------------------------------------------------------
    if resp.freight_treatment in ("ex_works", "extra"):
        fr = a.freight_per_kg * w
        v += fr
        S(Step(f"+ freight est. ₹{a.freight_per_kg:.2f}/kg × {w:.3f} kg", v, "₹/pc"))
        F(Flag("review", "Freight not included; estimated"))
    elif resp.freight_treatment == "not_stated":
        F(Flag("review", "Freight basis not stated; assumed delivered"))

    # --- item-level signals ---------------------------------------------------
    if item.is_alternate_spec:
        F(Flag("review", f"Alternate spec, not like-for-like: {item.alternate_note or item.paper_spec}"))
    if item.handwritten:
        F(Flag("review", "Handwritten value"))
    if item.legibility == "unclear":
        F(Flag("risk", "Value hard to read"))
    if item.paper_spec is None and item.price_unit != PriceUnit.same_as_last_year:
        F(Flag("info", "Paper grade not stated"))
    if match.size_deviation and match.size_deviation > a.size_deviation_review:
        F(Flag("risk", f"Size differs by {match.size_deviation*100:.1f}% from our spec"))
    elif match.method == "spec" and match.size_deviation:
        F(Flag("info", f"Matched by size ({match.size_deviation*100:.1f}% max deviation)"))
    if match.method == "category_rule" or (match.method == "remaining_rule"
                                           and item.price_unit != PriceUnit.same_as_last_year):
        F(Flag("review", match.reason))
    if match.method == "description":
        F(Flag("info" if match.confidence >= 0.9 else "review", match.reason))

    # --- sanity vs last year --------------------------------------------------
    ratio = v / line.ly_price - 1 if line.ly_price else 0.0
    if line.ly_price is None:
        F(Flag("info", "New line: no last-year price to sanity-check against"))
    elif abs(ratio) > a.price_sanity_band:
        F(Flag("risk", f"{ratio*100:+.0f}% vs last year; check unit or reading"))

    out.price = round(v, 4)
    return out
