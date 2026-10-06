"""Map each vendor item to one of the 30 RFQ lines.

Deterministic first: exact code, then spec (ply + dimensions, inches
converted to mm). Category rules ("₹42/kg for the 5-ply") and remaining
rules ("rest same as last year") expand to the lines they plausibly cover,
and every expansion is flagged as an interpretation."""
from __future__ import annotations

import re

from .rfq import RFQLine
from .schema import Match, QuotedItem, VendorResponse

BOX_TYPES = {"RSC", "Tray"}
MAX_SIZE_DEVIATION = 0.10   # beyond 10% on any dimension it is a different box
COMPATIBLE = {"RSC": {"box", "unknown"}, "Tray": {"tray", "box", "unknown"}, "Die-cut": {"die_cut"},
              "Partition": {"partition"}, "Pad": {"pad"}}


def parse_dims(text: str | None, unit: str) -> tuple[float, ...] | None:
    if not text:
        return None
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    if len(nums) < 2:
        return None
    nums = nums[:3]
    is_inch = unit == "inch" or (unit == "unknown" and max(nums) < 60)
    return tuple(n * 25.4 if is_inch else n for n in nums)


def size_deviation(item_mm: tuple[float, ...], line: RFQLine) -> float | None:
    target = line.dims_mm
    if len(item_mm) != len(target):
        return None
    return max(abs(a - b) / b for a, b in zip(item_mm, target))


def _spec_candidates(item: QuotedItem, lines: list[RFQLine]):
    dims = parse_dims(item.size_as_written, item.size_unit)
    if not dims:
        return []
    out = []
    for ln in lines:
        if item.ply and item.ply != ln.ply:
            continue
        if item.product_type not in COMPATIBLE.get(ln.box_type, {"unknown"}):
            continue
        dev = size_deviation(dims, ln)
        if dev is not None and dev <= MAX_SIZE_DEVIATION:
            out.append((dev, ln))
    return sorted(out, key=lambda x: x[0])


def _confidence_from_dev(dev: float) -> float:
    if dev <= 0.02:
        return 0.95
    if dev <= 0.05:
        return 0.85
    return 0.55


def match_vendor(resp: VendorResponse, lines: list[RFQLine]) -> tuple[list[Match], list[int]]:
    """Returns (matches, unmatched_item_indexes). One primary item per RFQ line;
    alternates are matched too but kept separate by is_alternate_spec."""
    by_code = {ln.code: ln for ln in lines}
    matches: list[Match] = []
    taken: set[str] = set()
    used_items: set[int] = set()

    # 1. exact code
    for i, it in enumerate(resp.items):
        if it.scope != "single_item" or it.is_alternate_spec:
            continue
        ref = (it.vendor_ref or "").strip().upper()
        if ref in by_code and ref not in taken:
            matches.append(Match(rfq_code=ref, item_index=i, method="code", confidence=0.98,
                                 reason=f"Vendor used our code {ref}"))
            taken.add(ref); used_items.add(i)

    # 2. spec matching, best deviation first across all pairs
    pairs = []
    for i, it in enumerate(resp.items):
        if i in used_items or it.scope != "single_item" or it.is_alternate_spec:
            continue
        for dev, ln in _spec_candidates(it, lines):
            pairs.append((dev, i, ln))
    for dev, i, ln in sorted(pairs, key=lambda p: p[0]):
        if i in used_items or ln.code in taken:
            continue
        it = resp.items[i]
        matches.append(Match(rfq_code=ln.code, item_index=i, method="spec", size_deviation=round(dev, 4),
                             confidence=_confidence_from_dev(dev),
                             reason=f"{it.ply}-ply {it.size_as_written} ({it.size_unit}) ≈ {ln.dims_label} mm, "
                                    f"max deviation {dev*100:.1f}%"))
        taken.add(ln.code); used_items.add(i)

    # 3. alternates: attach to the line their spec matches (not counted as primary)
    for i, it in enumerate(resp.items):
        if not it.is_alternate_spec:
            continue
        cands = _spec_candidates(it, lines)
        if cands:
            dev, ln = cands[0]
            matches.append(Match(rfq_code=ln.code, item_index=i, method="spec", size_deviation=round(dev, 4),
                                 confidence=_confidence_from_dev(dev),
                                 reason=f"Alternate spec offered against {ln.code}"))
            used_items.add(i)

    # 4. category rules, e.g. "the 5-ply" → 5-ply boxes not already quoted
    for i, it in enumerate(resp.items):
        if it.scope != "category":
            continue
        for ln in lines:
            if ln.code in taken or ln.ply != it.ply or ln.box_type not in BOX_TYPES:
                continue
            matches.append(Match(rfq_code=ln.code, item_index=i, method="category_rule", confidence=0.7,
                                 reason=f"Interpreted '{it.scope_description}' as all {ln.ply}-ply boxes"))
            taken.add(ln.code)
        used_items.add(i)

    # 5. "rest" rules → every line still open
    for i, it in enumerate(resp.items):
        if it.scope != "all_remaining":
            continue
        for ln in lines:
            if ln.code in taken:
                continue
            matches.append(Match(rfq_code=ln.code, item_index=i, method="remaining_rule", confidence=0.65,
                                 reason=f"Interpreted '{it.scope_description}' as covering {ln.code}"))
            taken.add(ln.code)
        used_items.add(i)

    unmatched = [i for i in range(len(resp.items)) if i not in used_items]
    return matches, unmatched
