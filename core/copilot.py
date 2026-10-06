"""RFQ co-pilot: the buyer talks, the draft changes through explicit tools."""
from __future__ import annotations

from pathlib import Path

import anthropic

from .draft import RFQDraft
from .llm import run_tool_loop

PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "copilot.md"
SEL = {"codes": {"type": "array", "items": {"type": "string"}, "description": "Item codes like CB-301"},
       "ply": {"type": "integer", "enum": [3, 5, 7]},
       "box_type": {"type": "string", "enum": ["RSC", "Tray", "Die-cut", "Partition", "Pad"]},
       "text": {"type": "string", "description": "Words that must all appear in the description, e.g. 'atta'"}}

TOOLS = [
    {"name": "start_from_last_year", "description": "Load last year's 30 lines and the standard supplier questionnaire into the draft.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "view_lines", "description": "List draft lines matching a filter (all lines if no filter). Use to resolve loose names.",
     "input_schema": {"type": "object", "properties": SEL}},
    {"name": "change_quantity", "description": "Change annual quantity on the selected lines, by percent or to an exact number.",
     "input_schema": {"type": "object", "properties": {**SEL, "percent": {"type": "number"}, "new_qty": {"type": "integer"}}}},
    {"name": "update_line", "description": "Change one line's description, print colours, ply, size or quantity.",
     "input_schema": {"type": "object", "required": ["code"], "properties": {
         "code": {"type": "string"}, "description": {"type": "string"}, "print_colours": {"type": "integer"},
         "ply": {"type": "integer", "enum": [3, 5, 7]}, "dims_mm": {"type": "array", "items": {"type": "integer"}},
         "annual_qty": {"type": "integer"}}}},
    {"name": "add_line", "description": "Add a new item to the RFQ.",
     "input_schema": {"type": "object", "required": ["description", "box_type", "dims_mm", "ply", "print_colours", "annual_qty"],
                      "properties": {"description": {"type": "string"}, "box_type": SEL["box_type"],
                                     "dims_mm": {"type": "array", "items": {"type": "integer"}, "description": "L, W, H in mm (L, W for pads)"},
                                     "ply": {"type": "integer", "enum": [3, 5, 7]}, "print_colours": {"type": "integer"},
                                     "annual_qty": {"type": "integer"}}}},
    {"name": "remove_lines", "description": "Remove lines by code.",
     "input_schema": {"type": "object", "required": ["codes"], "properties": {"codes": SEL["codes"]}}},
    {"name": "set_terms", "description": "Set RFQ terms.",
     "input_schema": {"type": "object", "properties": {
         "title": {"type": "string"}, "due_date": {"type": "string", "description": "YYYY-MM-DD"},
         "delivery_point": {"type": "string"}, "payment_days": {"type": "integer"},
         "price_basis": {"type": "string"}, "validity_days": {"type": "integer"}}}},
    {"name": "view_questionnaire", "description": "List the current supplier questionnaire with each question's type.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "add_question", "description": "Add a supplier questionnaire question.",
     "input_schema": {"type": "object", "required": ["question", "type"], "properties": {
         "question": {"type": "string"}, "type": {"type": "string", "enum": ["mandatory", "preferred", "info"]}}}},
    {"name": "set_question_type", "description": "Make an existing question mandatory, preferred or info.",
     "input_schema": {"type": "object", "required": ["no", "type"], "properties": {
         "no": {"type": "integer"}, "type": {"type": "string", "enum": ["mandatory", "preferred", "info"]}}}},
]


def _lines_view(draft: RFQDraft, lines) -> list[dict]:
    return [{"code": l.code, "description": l.description, "type": l.box_type, "size_mm": l.dims_label,
             "ply": l.ply, "print_colours": l.print_colours, "annual_qty": l.annual_qty,
             "last_year_price": l.ly_price} for l in lines]


def dispatcher(draft: RFQDraft):
    def run(name: str, a: dict):
        sel = lambda: draft.select(a.get("codes"), a.get("ply"), a.get("box_type"), a.get("text"))
        if name == "start_from_last_year":
            return draft.start_from_last_year() + ". " + draft.summary()
        if name == "view_lines":
            return _lines_view(draft, sel() if any(a.get(k) for k in SEL) else draft.lines)
        if name == "change_quantity":
            return draft.change_quantity(sel(), a.get("percent"), a.get("new_qty"))
        if name == "update_line":
            code = a.pop("code")
            return draft.update_line(code, **a)
        if name == "add_line":
            return draft.add_line(**a)
        if name == "remove_lines":
            return draft.remove_lines(a["codes"])
        if name == "set_terms":
            return draft.set_terms(**a)
        if name == "view_questionnaire":
            return draft.questionnaire
        if name == "add_question":
            return draft.add_question(a["question"], a["type"])
        if name == "set_question_type":
            return draft.set_question_type(int(a["no"]), a["type"])
        raise ValueError(f"Unknown tool {name}")
    return run


def chat(client: anthropic.Anthropic, model: str, draft: RFQDraft, messages: list[dict], text: str):
    before = len(draft.changes)
    messages.append({"role": "user", "content": text})
    system = [{"type": "text", "text": PROMPT.read_text()},
              {"type": "text", "text": "Current draft: " + (draft.summary() if draft.lines else "empty (nothing loaded yet).")}]
    reply, calls = run_tool_loop(client, model, system, messages, TOOLS, dispatcher(draft))
    return reply, draft.changes[before:], calls
