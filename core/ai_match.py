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
    tool = {"name": "record_matches", "description": "Record one match decision per vendor item.",
            "input_schema": AIMatchResult.model_json_schema()}
    msg = client.messages.create(
        model=model, max_tokens=4000, system=PROMPT.read_text(), tools=[tool], tool_choice={"type": "auto"},
        messages=[{"role": "user", "content":
                   f"Vendor: {resp.vendor_name}\n\nVendor items (in the order they appeared):\n{items}\n\n"
                   f"Open RFQ lines:\n{rfq}"}])
    block = next((b for b in msg.content if b.type == "tool_use"), None)
    if block is None:
        return []
    return AIMatchResult.model_validate(block.input).matches
