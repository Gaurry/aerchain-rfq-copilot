"""AI step for items described only in words. The model proposes; the rules
in core/matching.py accept or reject each proposal."""
from __future__ import annotations

from pathlib import Path

import anthropic

from .rfq import RFQLine
from .schema import AIMatch, AIMatchResult, VendorResponse

PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "match_lines.md"


def propose_matches(client: anthropic.Anthropic, model: str, resp: VendorResponse,
                    item_indexes: list[int], open_lines: list[RFQLine]) -> list[AIMatch]:
    if not item_indexes or not open_lines:
        return []
    items = "\n".join(
        f"[{i}] {resp.items[i].description} | ply={resp.items[i].ply} | type={resp.items[i].product_type} | "
        f"price='{resp.items[i].price_text}' | evidence: {resp.items[i].evidence.quote}" for i in item_indexes)
    rfq = "\n".join(f"{l.code} | {l.description} | {l.box_type} | {l.dims_label} mm | {l.ply}-ply | "
                    f"{'plain' if not l.print_colours else str(l.print_colours) + ' colour'}" for l in open_lines)
    tool = {"name": "record_matches", "description": "Record one match decision per vendor item. Every match must "
            "carry rfq_code: the RFQ line code, or exactly 'NONE'.", "input_schema": AIMatchResult.model_json_schema()}
    valid = {l.code for l in open_lines} | {"NONE"}
    messages = [{"role": "user", "content":
                 f"Vendor: {resp.vendor_name}\n\nVendor items (in the order they appeared):\n{items}\n\n"
                 f"Open RFQ lines:\n{rfq}"}]
    for attempt in range(2):
        msg = client.messages.create(model=model, max_tokens=4000, system=PROMPT.read_text(), tools=[tool],
                                     tool_choice={"type": "auto"}, messages=messages)
        block = next((b for b in msg.content if b.type == "tool_use"), None)
        if block is None:
            problem = "You must call record_matches."
        else:
            try:
                matches = AIMatchResult.model_validate(block.input).matches
                bad = [m for m in matches if m.rfq_code.strip().upper() not in valid]
                missing = set(item_indexes) - {m.item_index for m in matches}
                if not bad and not missing:
                    for m in matches:
                        m.rfq_code = m.rfq_code.strip().upper()
                    return [m for m in matches if m.rfq_code != "NONE"]
                problem = (f"Invalid rfq_code for items {[m.item_index for m in bad]}; " if bad else "") + \
                          (f"no decision for items {sorted(missing)}. " if missing else "") + \
                          "Use only codes from the open RFQ lines, or 'NONE'."
            except Exception as e:  # missing required field, wrong types
                problem = f"Your answer didn't fit the format ({str(e)[:300]}). Every match needs rfq_code."
        if block is not None:
            messages += [{"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in msg.content]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": block.id,
                                                       "content": problem, "is_error": True}]}]
        else:
            messages += [{"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in msg.content]},
                         {"role": "user", "content": problem}]
    # Still not usable: keep only the valid ones; the rest surface as unplaced items for the buyer
    try:
        return [m for m in AIMatchResult.model_validate(block.input).matches
                if m.rfq_code.strip().upper() in valid - {"NONE"}] if block is not None else []
    except Exception:
        return []
