"""Analyst: plain-language questions over the comparison. The model plans; these
tools compute. They call the same functions as the grid, so the chat and the
screen can never disagree."""
from __future__ import annotations

import difflib
import re
from pathlib import Path

import anthropic

from .compare import ALLOWED, Comparison, Decisions, l1
from .llm import run_tool_loop
from .rfq import Assumptions

PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "analyst.md"
SCOPES = {"cleared_only": "Cleared only", "cleared_plus_conditional": "Cleared + conditional", "all": "All vendors"}
CODES = {"type": "array", "items": {"type": "string"}, "description": "Item codes; omit for all lines"}
VENDORS = {"type": "array", "items": {"type": "string"}}

TOOLS = [
    {"name": "find_lines", "description": "Find RFQ lines by code, words in the description, ply (3/5/7), box type "
     "(RSC, Tray, Die-cut, Partition, Pad) or size. Use before answering about specific items or groups like '5-ply' or "
     "'partitions'. Every result lists code, description, type, size, ply and print.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Code or description words; omit to filter only by ply/type"},
         "ply": {"type": "integer", "enum": [3, 5, 7]},
         "box_type": {"type": "string", "enum": ["RSC", "Tray", "Die-cut", "Partition", "Pad"]}}}},
    {"name": "compare_lines", "description": "Normalized ₹/pc for each vendor on the given lines, with last year's price and open review flags.",
     "input_schema": {"type": "object", "properties": {"codes": CODES, "vendors": VENDORS}}},
    {"name": "cheapest_per_line", "description": "Cheapest eligible quote per line and the total annual cost and saving of awarding each line to it.",
     "input_schema": {"type": "object", "required": ["scope"], "properties": {
         "scope": {"type": "string", "enum": list(SCOPES), "description": "Which quality-gate statuses may win"},
         "exclude_vendors": VENDORS, "codes": CODES}}},
    {"name": "award_scenario", "description": "Cost a specific split: explicit line→vendor picks, with remaining lines going to the cheapest eligible vendor (or left out).",
     "input_schema": {"type": "object", "properties": {
         "assignments": {"type": "array", "items": {"type": "object", "required": ["code", "vendor"],
                                                    "properties": {"code": {"type": "string"}, "vendor": {"type": "string"}}}},
         "rest": {"type": "string", "enum": [*SCOPES, "leave_out"], "description": "How to award lines not assigned"},
         "exclude_vendors": VENDORS}}},
    {"name": "explain_price", "description": "How one vendor's price on one line was worked out: steps, source words, flags.",
     "input_schema": {"type": "object", "required": ["vendor", "code"], "properties": {"vendor": {"type": "string"}, "code": {"type": "string"}}}},
    {"name": "vendor_profile", "description": "A vendor's quality gate, terms, discounts, certificates, coverage and open review items.",
     "input_schema": {"type": "object", "required": ["vendor"], "properties": {"vendor": {"type": "string"}}}},
    {"name": "open_review_items", "description": "Flags the buyer hasn't resolved yet, grouped.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "current_assumptions", "description": "FX rate, freight estimate, discount handling and review thresholds in use.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "calculate", "description": "Exact arithmetic on numbers from tool results, e.g. '1614828 - 327000'. Use instead of mental maths.",
     "input_schema": {"type": "object", "required": ["expression"], "properties": {"expression": {"type": "string"}}}},
    {"name": "show_chart", "description": "Show a bar chart under your answer. Values must come from tool results.",
     "input_schema": {"type": "object", "required": ["title", "labels", "values", "unit"], "properties": {
         "title": {"type": "string"}, "labels": {"type": "array", "items": {"type": "string"}},
         "values": {"type": "array", "items": {"type": "number"}}, "unit": {"type": "string"}}}},
    {"name": "export_table", "description": "Offer a table as an Excel download under your answer.",
     "input_schema": {"type": "object", "required": ["title", "columns", "rows"], "properties": {
         "title": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}},
         "rows": {"type": "array", "items": {"type": "array", "items": {}}}}}},
]


class Analyst:
    def __init__(self, comp: Comparison, d: Decisions, a: Assumptions):
        self.c, self.d, self.a = comp, d, a
        self.by = {l.code: l for l in comp.lines}
        self.displays: list[dict] = []

    # ---- helpers -------------------------------------------------------------
    def vendor(self, name: str) -> str:
        n = name.strip().lower()
        for v in self.c.vendors:
            if v.lower() == n or n in v.lower() or v.lower() in n:
                return v
        raise ValueError(f"No vendor called '{name}'. Vendors: {', '.join(self.c.vendors)}")

    def codes(self, codes):
        if not codes:
            return [l.code for l in self.c.lines]
        bad = [x for x in codes if x.strip().upper() not in self.by]
        if bad:
            raise ValueError(f"Unknown line(s) {bad}. Use find_lines first.")
        return [x.strip().upper() for x in codes]

    def price(self, v, code):
        cell = self.c.cells.get((v, code))
        return None if cell is None else cell.price

    def ly_cost(self, codes):
        known = [code for code in codes if self.by[code].ly_price]
        return sum(self.by[c].ly_price * self.by[c].annual_qty for c in known), [c for c in codes if c not in known]

    # ---- tools ---------------------------------------------------------------
    @staticmethod
    def line_info(l) -> dict:
        return {"code": l.code, "description": l.description, "type": l.box_type, "size_mm": l.dims_label,
                "ply": l.ply, "print": "plain" if not l.print_colours else f"{l.print_colours} colour",
                "annual_qty": l.annual_qty}

    def find_lines(self, query: str | None = None, ply: int | None = None, box_type: str | None = None):
        pool = self.c.lines
        q = (query or "").strip()
        # "5-ply", "5 ply", "3ply" inside the query text become a ply filter
        m = re.search(r"\b([357])\s*-?\s*ply\b", q, re.I)
        if m and not ply:
            ply = int(m.group(1)); q = (q[:m.start()] + q[m.end():]).strip()
        if ply:
            pool = [l for l in pool if l.ply == int(ply)]
        if box_type:
            pool = [l for l in pool if l.box_type.lower() == box_type.lower()]
        if not q:
            return {"count": len(pool), "lines": [self.line_info(l) for l in pool]}
        code_like = re.fullmatch(r"([A-Za-z]{2})\s*-?\s*(\d{2,3})", q)
        if code_like:
            code = f"{code_like.group(1).upper()}-{code_like.group(2)}"
            if code in self.by:
                return {"count": 1, "lines": [self.line_info(self.by[code])]}
            close = difflib.get_close_matches(code, list(self.by), n=3, cutoff=0.5)
            return {"count": 0, "note": f"There is no line {code} in this RFQ.",
                    "closest": [self.line_info(self.by[c]) for c in close]}
        words = [w for w in re.findall(r"\w+", q.lower()) if len(w) > 1 and not re.fullmatch(r"cb|pt|pd|dc|line|lines|box|boxes", w)]
        hits = [l for l in pool if words and all(w in f"{l.description} {l.code} {l.box_type} {l.dims_label}".lower() for w in words)]
        if hits:
            return {"count": len(hits), "lines": [self.line_info(l) for l in hits]}
        close = difflib.get_close_matches(q.upper(), list(self.by), n=3, cutoff=0.5)
        return {"count": 0, "note": f"No line matches '{query}'.",
                "closest": [self.line_info(self.by[c]) for c in close]}

    def compare_lines(self, codes=None, vendors=None):
        vs = [self.vendor(v) for v in vendors] if vendors else self.c.vendors
        rows = []
        for code in self.codes(codes):
            l = self.by[code]
            rows.append({**self.line_info(l), "last_year_price": l.ly_price,
                         "prices": {v: (round(p, 2) if (p := self.price(v, code)) is not None else "not quoted") for v in vs},
                         "open_flags": {v: len(self.c.open_flags(v, code, self.d)) for v in vs if (v, code) in self.c.cells}})
        return {"basis": "₹/pc delivered to Bawal, before GST",
                "quality_gate": {v: self.c.gates[v].label for v in vs}, "rows": rows}

    def cheapest_per_line(self, scope, exclude_vendors=None, codes=None):
        ex = {self.vendor(v) for v in exclude_vendors or []}
        wins = l1(self.c, ALLOWED[SCOPES[scope]], ex)
        cs = self.codes(codes)
        rows, total, none = [], 0.0, []
        for code in cs:
            if code not in wins:
                none.append(code); continue
            v, p = wins[code]
            others = sorted((self.price(o, code), o) for o in self.c.vendors
                            if o != v and o not in ex and self.c.gates[o].status in ALLOWED[SCOPES[scope]]
                            and self.price(o, code) is not None)
            rows.append({"code": code, "description": self.by[code].description, "ply": self.by[code].ply,
                         "winner": v, "price": round(p, 2),
                         "runner_up": f"{others[0][1]} ₹{others[0][0]:.2f}" if others else "none",
                         "annual_cost": round(p * self.by[code].annual_qty)})
            total += p * self.by[code].annual_qty
        ly, no_hist = self.ly_cost([r["code"] for r in rows])
        by_vendor = {}
        for r in rows:
            by_vendor.setdefault(r["winner"], {"lines": 0, "annual_cost": 0})
            by_vendor[r["winner"]]["lines"] += 1; by_vendor[r["winner"]]["annual_cost"] += r["annual_cost"]
        return {"scope": SCOPES[scope], "excluded_vendors": sorted(ex),
                "vendors_eligible": [v for v in self.c.vendors if v not in ex and self.c.gates[v].status in ALLOWED[SCOPES[scope]]],
                "lines_considered": len(cs), "total_annual_cost": round(total), "last_year_cost_same_lines": round(ly),
                "saving_vs_last_year": round(ly - total), "lines_awarded": len(rows), "lines_with_no_eligible_quote": none,
                "lines_without_last_year_price": no_hist, "by_vendor": by_vendor, "lines": rows}

    def award_scenario(self, assignments=None, rest="leave_out", exclude_vendors=None):
        ex = {self.vendor(v) for v in exclude_vendors or []}
        picks, problems = {}, []
        for asg in assignments or []:
            code, v = self.codes([asg["code"]])[0], self.vendor(asg["vendor"])
            p = self.price(v, code)
            if p is None:
                problems.append(f"{v} did not quote {code}")
                continue
            picks[code] = (v, p)
            if self.c.gates[v].status not in ALLOWED["Cleared only"]:
                problems.append(f"{v} is {self.c.gates[v].label} on the quality gate ({code})")
        if rest != "leave_out":
            for code, wp in l1(self.c, ALLOWED[SCOPES[rest]], ex).items():
                picks.setdefault(code, wp)
        total = sum(p * self.by[c].annual_qty for c, (_, p) in picks.items())
        ly, no_hist = self.ly_cost(list(picks))
        by_vendor = {}
        for code, (v, p) in picks.items():
            by_vendor.setdefault(v, {"lines": [], "annual_cost": 0})
            by_vendor[v]["lines"].append(code); by_vendor[v]["annual_cost"] += round(p * self.by[code].annual_qty)
        uncovered = [l.code for l in self.c.lines if l.code not in picks]
        risks = sorted({f"{v}: {self.c.gates[v].label}" + (f" ({self.c.gates[v].reasons[0]})" if self.c.gates[v].reasons else "")
                        for v in by_vendor if self.c.gates[v].status != "cleared"})
        return {"total_annual_cost": round(total), "last_year_cost_same_lines": round(ly), "saving_vs_last_year": round(ly - total),
                "by_vendor": by_vendor, "lines_not_awarded": uncovered, "problems": problems, "quality_risks": risks,
                "lines_without_last_year_price": no_hist}

    def explain_price(self, vendor, code):
        v, code = self.vendor(vendor), self.codes([code])[0]
        cell = self.c.cells.get((v, code))
        if cell is None:
            return f"{v} did not quote {code}."
        return {"vendor": v, "code": code, "price": cell.price, "source_files": self.c.responses[v].source_files,
                "vendor_words": cell.evidence, "match_confidence": cell.match_confidence,
                "steps": [{"step": s.label, "value": round(s.value, 4), "unit": s.unit} for s in cell.steps],
                "flags": [{"severity": f.severity, "text": f.text,
                           "approved": (v, code, f.text) in self.d.approved} for f in cell.flags]}

    def vendor_profile(self, vendor):
        v = self.vendor(vendor); r = self.c.responses[v]; g = self.c.gates[v]
        quoted = [l.code for l in self.c.lines if self.price(v, l.code) is not None]
        return {"vendor": v, "quality_gate": g.label, "gate_reasons": g.reasons, "gate_notes": g.notes,
                "gst": r.gst_treatment, "freight": r.freight_treatment, "payment_days": r.payment_days,
                "validity_days": r.validity_days, "lead_time": r.lead_time_text,
                "discounts": [{"percent": x.percent, "description": x.description, "condition": x.condition} for x in r.discounts],
                "certificates": [{"standard": c.standard, "valid_until": c.valid_until, "source": c.source} for c in r.certificates],
                "lines_quoted": len(quoted), "lines_not_quoted": [l.code for l in self.c.lines if l.code not in quoted],
                "alternates_offered": [code for (vv, code) in self.c.alternates if vv == v],
                "open_review_items": sorted({f.text for (vv, code) in self.c.cells if vv == v for f in self.c.open_flags(v, code, self.d)})}

    def open_review_items(self):
        groups = {}
        for (v, code) in self.c.cells:
            for f in self.c.open_flags(v, code, self.d):
                groups.setdefault(f"{v} · {f.severity} · {f.text}", []).append(code)
        return [{"item": k, "lines": len(cs)} for k, cs in groups.items()] or "Nothing open."

    def current_assumptions(self):
        return self.a.to_dict()

    def calculate(self, expression):
        if not re.fullmatch(r"[\d\s.+\-*/()%,]+", expression):
            raise ValueError("Only numbers and + - * / ( ) are allowed")
        import ast, operator as op
        ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.USub: op.neg, ast.Mod: op.mod}
        def ev(n):
            if isinstance(n, ast.Constant):
                return n.value
            if isinstance(n, ast.BinOp):
                return ops[type(n.op)](ev(n.left), ev(n.right))
            if isinstance(n, ast.UnaryOp):
                return ops[type(n.op)](ev(n.operand))
            raise ValueError("Unsupported expression")
        return {"expression": expression, "result": round(ev(ast.parse(expression.replace(",", ""), mode="eval").body), 4)}

    def show_chart(self, title, labels, values, unit):
        self.displays.append({"kind": "chart", "title": title, "labels": labels, "values": values, "unit": unit})
        return "Chart shown under the answer."

    def export_table(self, title, columns, rows):
        self.displays.append({"kind": "export", "title": title, "columns": columns, "rows": rows})
        return "Download offered under the answer."

    def dispatch(self, name, args):
        if name not in {t["name"] for t in TOOLS}:
            raise ValueError(f"Unknown tool {name}")
        return getattr(self, name)(**args)


def ask(client: anthropic.Anthropic, model: str, comp: Comparison, d: Decisions, a: Assumptions,
        messages: list[dict], text: str, rfq_changes: list[str] | None = None):
    an = Analyst(comp, d, a)
    messages.append({"role": "user", "content": text})
    system = [{"type": "text", "text": PROMPT.read_text()},
              {"type": "text", "text": f"Vendors: {', '.join(comp.vendors)}. Lines: {len(comp.lines)}. "
                                       f"Quality gate: " + "; ".join(f"{v} {comp.gates[v].label}" for v in comp.vendors)
                                       + ". Changes the buyer made to this year's RFQ vs last year: "
                                       + ("; ".join(rfq_changes) if rfq_changes else "none")}]
    reply, calls = run_tool_loop(client, model, system, messages, TOOLS, an.dispatch, max_turns=12)
    return reply, calls, an.displays
