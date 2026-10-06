"""Real AI extraction: send a vendor's files to Claude with the extraction
rules and force the answer into the VendorResponse schema.

Usage:  python -m core.extract            (all vendors in the inbox)
        python -m core.extract Gupta      (one vendor)"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from .ai_match import propose_matches
from .matching import match_vendor
from .readers import to_content_blocks
from .rfq import QUESTIONNAIRE, load_rfq
from .schema import AIMatch, VendorResponse

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "data" / "inbox"
OUT = ROOT / "data" / "extracted"
PROMPT = ROOT / "prompts" / "extract_quote.md"
MODEL = os.environ.get("EXTRACT_MODEL", "claude-sonnet-5-5")

VENDOR_FILES = {
    "Kraftline": ["1_Kraftline_Offer.xlsx"],
    "Shree Balaji": ["2_ShreeBalaji_Quotation.pdf", "2b_ShreeBalaji_ISO_Certificate.pdf"],
    "Pacific": ["3_Pacific_Proposal.docx"],
    "Gupta": ["4_Gupta_RateCard_photo.jpg"],
    "Om Sai": ["5_OmSai_reply.eml"],
}


def _api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        import streamlit as st
        return st.secrets["ANTHROPIC_API_KEY"]
    except Exception as e:
        raise RuntimeError("Set ANTHROPIC_API_KEY (env var or Streamlit secrets)") from e


def _context_text() -> str:
    lines = load_rfq()
    rows = [f"{l.code} | {l.description} | {l.box_type} | {l.dims_label} mm | {l.ply}-ply | "
            f"{'plain' if not l.print_colours else str(l.print_colours) + ' colour'} | qty {l.annual_qty:,}"
            for l in lines]
    qs = [f"{q['no']}. {q['question']}" for q in QUESTIONNAIRE]
    return ("## Requested lines (context only)\n" + "\n".join(rows) +
            "\n\n## Questionnaire\n" + "\n".join(qs))


def extract_vendor(vendor: str, files: list[Path], model: str = MODEL) -> tuple[VendorResponse, dict]:
    client = anthropic.Anthropic(api_key=_api_key())
    content: list[dict] = []
    for f in files:
        content += to_content_blocks(f)
    content.append({"type": "text", "text":
                    f"These files are the complete response from vendor '{vendor}'. Record everything they quoted."})

    tool = {"name": "record_vendor_response",
            "description": "Record everything the vendor said, literally, with evidence.",
            "input_schema": VendorResponse.model_json_schema()}
    t0 = time.time()
    msg = client.messages.create(
        model=model, max_tokens=16000,
        system=[{"type": "text", "text": PROMPT.read_text()},
                {"type": "text", "text": _context_text(), "cache_control": {"type": "ephemeral"}}],
        tools=[tool], tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": content}],
    )
    block = next((b for b in msg.content if b.type == "tool_use"), None)
    if block is None:
        raise RuntimeError(f"{vendor}: model did not call record_vendor_response (stop={msg.stop_reason})")
    data = dict(block.input)
    data["vendor_name"] = vendor
    data["source_files"] = [f.name for f in files]
    resp = VendorResponse.model_validate(data)   # schema check: fails loudly on malformed output
    meta = {"model": model, "seconds": round(time.time() - t0, 1),
            "input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens,
            "stop_reason": msg.stop_reason, "extracted_at": datetime.now(timezone.utc).isoformat()}
    return resp, meta


def ai_match_leftovers(resp: VendorResponse, model: str = MODEL) -> list[AIMatch]:
    """Items that code/spec matching could not place go to the AI matcher."""
    lines = load_rfq()
    matches, unmatched = match_vendor(resp, lines)
    taken = {m.rfq_code for m in matches}
    leftovers = [i for i in unmatched if resp.items[i].scope == "single_item" and not resp.items[i].is_alternate_spec
                 and not resp.items[i].size_as_written]
    open_lines = [l for l in lines if l.code not in taken]
    client = anthropic.Anthropic(api_key=_api_key())
    return propose_matches(client, model, resp, leftovers, open_lines)


def save(vendor: str, resp: VendorResponse, meta: dict, ai_matches: list[AIMatch] | None = None) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{vendor.replace(' ', '_')}.json"
    p.write_text(json.dumps({"meta": meta, "response": resp.model_dump(mode="json"),
                             "ai_matches": [m.model_dump() for m in ai_matches or []]},
                            indent=1, ensure_ascii=False))
    return p


def load_ai_matches() -> dict[str, list[AIMatch]]:
    out = {}
    for p in sorted(OUT.glob("*.json")):
        d = json.loads(p.read_text())
        out[d["response"]["vendor_name"]] = [AIMatch.model_validate(m) for m in d.get("ai_matches", [])]
    return out


def load_extracted() -> dict[str, VendorResponse]:
    out = {}
    for p in sorted(OUT.glob("*.json")):
        d = json.loads(p.read_text())
        r = VendorResponse.model_validate(d["response"])
        out[r.vendor_name] = r
    return out


if __name__ == "__main__":
    wanted = sys.argv[1:] or list(VENDOR_FILES)
    for v in wanted:
        files = [INBOX / f for f in VENDOR_FILES[v]]
        resp, meta = extract_vendor(v, files)
        ai = ai_match_leftovers(resp)
        p = save(v, resp, meta, ai)
        print(f"{v}: {len(resp.items)} items, {meta['seconds']}s, {meta['input_tokens']}+{meta['output_tokens']} tokens, "
              f"stop={meta['stop_reason']} → {p.name}")


def rematch_only(vendor: str):
    """Re-run just the AI matching on an existing extraction."""
    p = OUT / f"{vendor.replace(' ', '_')}.json"
    d = json.loads(p.read_text())
    resp = VendorResponse.model_validate(d["response"])
    return save(vendor, resp, d["meta"], ai_match_leftovers(resp))
