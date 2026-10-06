"""RFQ Copilot — Streamlit entry point.

Screens: Draft RFQ · Responses · Review · Compare · Ask · Award · Assumptions.
Every number comes from data/extracted/ (written by the live AI pipeline) plus
deterministic matching and normalization. Buyer decisions are layered on top."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from core.compare import ALLOWED, OPEN, Decisions, award_total, build, l1, log_correction
from core.extract import INBOX, OUT, ai_match_leftovers, extract_vendor, load_ai_matches, load_extracted, save
from core.rfq import QUESTIONNAIRE, Assumptions, load_rfq

st.set_page_config(page_title="RFQ Copilot", layout="wide")

GATE_COLOR = {"cleared": "green", "cleared_stale": "green", "conditional": "orange", "failed": "red", "unknown": "gray"}
SEV = {"risk": ":red[**Risk**]", "review": ":orange[**Review**]", "info": ":gray[Info]"}


# ---------------------------------------------------------------- state & data
@st.cache_data
def rfq_lines():
    return load_rfq()


ss = st.session_state
ss.setdefault("decisions", Decisions())
_DEF = Assumptions()
WIDGETS = {"a_fx": "fx_usd_inr", "a_freight": "freight_per_kg", "a_gst": "gst_rate",
           "a_disc": "apply_conditional_discounts", "a_size": "size_deviation_review", "a_band": "price_sanity_band"}
for _k, _attr in WIDGETS.items():
    ss.setdefault(_k, getattr(_DEF, _attr))
ss["assumptions"] = Assumptions(**{attr: ss[k] for k, attr in WIDGETS.items()})


def api_key_present() -> bool:
    import os
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    try:
        return "ANTHROPIC_API_KEY" in st.secrets
    except Exception:
        return False


def extraction_meta() -> dict[str, dict]:
    out = {}
    for p in sorted(OUT.glob("*.json")):
        d = json.loads(p.read_text())
        out[d["response"]["vendor_name"]] = d["meta"]
    return out


def find_file(name: str) -> Path:
    for p in (INBOX / name, INBOX / "uploads" / name):
        if p.exists():
            return p
    raise FileNotFoundError(name)


lines = rfq_lines()
by_code = {l.code: l for l in lines}
a: Assumptions = ss.assumptions
d: Decisions = ss.decisions
resps = load_extracted()
comp = build(resps, load_ai_matches(), lines, a, d)
ly_total = sum(l.ly_spend for l in lines)


def money(x: float) -> str:
    return f"₹{x/1e5:,.1f} L"


def decide(kind: str, vendor: str, codes: list[str], **extra):
    rec = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, "vendor": vendor, "codes": codes, **extra}
    log_correction(rec)


def group_key(*parts) -> str:
    return hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:10]


# ---------------------------------------------------------------- header
h1, h2, h3, h4 = st.columns(4)
h1.metric("Category", "Corrugated packaging")
h2.metric("Line items", len(lines))
h3.metric("Responses read", f"{len(resps)} of 5")
h4.metric("Last-year spend", f"₹{ly_total/1e7:.2f} Cr")
if not api_key_present():
    st.caption("Reading new files needs ANTHROPIC_API_KEY in Streamlit secrets. Saved results still work.")

tabs = st.tabs(["Draft RFQ", "Responses", "Review", "Compare", "Ask", "Award", "Assumptions"])

# ---------------------------------------------------------------- Draft RFQ
with tabs[0]:
    st.subheader("RFQ: corrugated boxes FY27")
    st.caption("Co-pilot drafting comes next. This is the structured RFQ every response is matched against.")
    st.dataframe(pd.DataFrame([{
        "Line": l.line, "Code": l.code, "Description": l.description, "Type": l.box_type,
        "Size (mm)": l.dims_label, "Ply": l.ply, "Paper": l.paper_spec,
        "Print": "Plain" if not l.print_colours else f"{l.print_colours} colour",
        "Annual qty": l.annual_qty, "Last-year ₹/pc": l.ly_price,
    } for l in lines]), hide_index=True, width="stretch")
    with st.expander("Supplier questionnaire"):
        st.dataframe(pd.DataFrame(QUESTIONNAIRE), hide_index=True, width="stretch")

# ---------------------------------------------------------------- Responses
with tabs[1]:
    st.subheader("Vendor responses")
    st.caption("Email is stubbed: replies land in the inbox as the vendor sent them. Reading them is live AI.")
    meta = extraction_meta()
    rows = []
    for v in comp.vendors:
        r = resps[v]
        matched = sum(1 for (vv, _), c in comp.cells.items() if vv == v and c.price is not None)
        open_groups = {(f.severity, f.text) for (vv, code) in comp.cells if vv == v
                       for f in comp.open_flags(v, code, d)}
        rows.append({"Vendor": v, "Received as": ", ".join(r.source_files),
                     "Prices read": len(r.items), "Lines matched": f"{matched} / {len(lines)}",
                     "Not requested": len(comp.unmatched[v]), "Open review items": len(open_groups),
                     "Quality gate": comp.gates[v].label,
                     "Read in": f"{meta.get(v, {}).get('seconds', '–')} s",
                     "Model": meta.get(v, {}).get("model", "–")})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    for v in comp.vendors:
        r = resps[v]
        with st.expander(f"What we read from {v}"):
            c1, c2 = st.columns(2)
            c1.markdown(f"**GST:** {r.gst_treatment.replace('_', ' ')}  \n**Freight:** {r.freight_treatment.replace('_', ' ')}  \n"
                        f"**Payment:** {r.payment_days or '–'} days  \n**Validity:** {r.validity_days or '–'} days  \n"
                        f"**Lead time:** {r.lead_time_text or '–'}")
            g = comp.gates[v]
            c2.markdown(f"**Quality gate:** :{GATE_COLOR[g.status]}[{g.label}]")
            for x in g.reasons + g.notes:
                c2.caption(x)
            for disc in r.discounts:
                st.markdown(f"**Discount:** {disc.percent:g}% {disc.description}"
                            + (f" — *condition: {disc.condition}*" if disc.condition else "") + f"  \n> {disc.evidence.quote}")
            for ad in r.adders:
                st.markdown(f"**Extra charge:** {ad.description}")
            for cert in r.certificates:
                st.markdown(f"**Certificate:** {cert.standard} {cert.certificate_no or ''}, valid until {cert.valid_until} ({cert.source.replace('_', ' ')})")
            if r.refers_to_prior:
                st.markdown("**Refers to earlier documents:** " + "; ".join(f"“{x}”" for x in r.refers_to_prior))
            for w in r.extraction_warnings:
                st.caption(f"Reader's note: {w}")
            st.dataframe(pd.DataFrame([{
                "Their description": it.description, "Size as written": it.size_as_written, "Ply": it.ply,
                "Price as written": it.price_text, "Unit": it.price_unit.value, "Currency": it.currency,
                "Handwritten": "yes" if it.handwritten else "", "Alternate": "yes" if it.is_alternate_spec else "",
                "Evidence": it.evidence.quote} for it in r.items]), hide_index=True, width="stretch")
            if comp.unmatched[v]:
                st.caption("Quoted but not in our RFQ: " + ", ".join(
                    f"{it.size_as_written or it.description} ({it.ply}-ply)" for it in comp.unmatched[v]))
            if api_key_present() and st.button(f"Re-read {v}'s files", key=f"rerun-{v}"):
                with st.spinner(f"Reading {v}… usually 30–60 seconds"):
                    files = [find_file(f) for f in r.source_files]
                    resp, m = extract_vendor(v, files)
                    save(v, resp, m, ai_match_leftovers(resp))
                st.rerun()

    st.divider()
    st.markdown("**Add a vendor response**: any format (Excel, PDF, Word, photo, email).")
    up_name = st.text_input("Vendor name", placeholder="Sharma Cartons")
    up_files = st.file_uploader("Files", accept_multiple_files=True,
                                type=["xlsx", "pdf", "docx", "jpg", "jpeg", "png", "eml", "txt", "csv"])
    if st.button("Read it now", disabled=not (api_key_present() and up_name and up_files)):
        dest = INBOX / "uploads"
        dest.mkdir(parents=True, exist_ok=True)
        paths = []
        for f in up_files:
            p = dest / f.name
            p.write_bytes(f.getvalue())
            paths.append(p)
        with st.spinner("Reading… usually 30–60 seconds"):
            resp, m = extract_vendor(up_name.strip(), paths)
            save(up_name.strip(), resp, m, ai_match_leftovers(resp))
        st.rerun()

# ---------------------------------------------------------------- Review
with tabs[2]:
    st.subheader("Review queue")
    st.caption("Only what the system isn't sure about. Clean values go straight to the comparison.")
    groups: dict[tuple, list[str]] = {}
    for (v, code) in comp.cells:
        for f in comp.open_flags(v, code, d):
            groups.setdefault((v, f.severity, f.text), []).append(code)
    order = sorted(groups.items(), key=lambda kv: (kv[0][1] != "risk", kv[0][0], kv[0][2]))
    m1, m2, m3 = st.columns(3)
    m1.metric("Open items", len(groups))
    m2.metric("Lines affected", len({(v, c) for (v, _, _), cs in groups.items() for c in cs}))
    m3.metric("Decisions made", len(d.approved) + len(d.corrected) + len(d.excluded))
    pick = st.multiselect("Vendors", comp.vendors, default=comp.vendors)
    if not groups:
        st.success("Nothing left to review.")
    for (v, sev, text), codes in order:
        if v not in pick:
            continue
        k = group_key(v, sev, text)
        with st.container(border=True):
            st.markdown(f"{SEV[sev]} · **{v}** · {text}")
            first = comp.cells[(v, codes[0])]
            names = ", ".join(f"{c} {by_code[c].description}" for c in codes[:4])
            st.caption(f"{len(codes)} line{'s' if len(codes) > 1 else ''}: {names}{' …' if len(codes) > 4 else ''}")
            if first.evidence:
                st.markdown(f"> {first.evidence}  \n<small>from {', '.join(resps[v].source_files)}</small>",
                            unsafe_allow_html=True)
            if len(codes) == 1:
                st.caption("Working: " + " → ".join(f"{s.label} = {s.value:,.2f}" for s in first.steps))
            b1, b2, b3 = st.columns([1, 2, 1])
            if b1.button("Approve", key=f"ok-{k}", type="primary"):
                for c in codes:
                    d.approved.add((v, c, text))
                decide("approve", v, codes, flag=text)
                st.rerun()
            if len(codes) == 1:
                new = b2.number_input("Correct ₹/pc", min_value=0.0, value=round(first.price or 0.0, 2),
                                      step=0.1, key=f"num-{k}", label_visibility="collapsed")
                if b2.button("Save correction", key=f"fix-{k}"):
                    d.corrected[(v, codes[0])] = new
                    for f in comp.open_flags(v, codes[0], d):
                        d.approved.add((v, codes[0], f.text))
                    decide("correct", v, codes, flag=text, old=first.price, new=new)
                    st.rerun()
            if b3.button(f"Exclude {'line' if len(codes) == 1 else f'{len(codes)} lines'}", key=f"ex-{k}"):
                for c in codes:
                    d.excluded.add((v, c))
                decide("exclude", v, codes, flag=text)
                st.rerun()

# ---------------------------------------------------------------- Compare
with tabs[3]:
    st.subheader("Comparison: ₹ per piece, delivered to Bawal, before GST")
    scope = st.radio("Who can win a line?", list(ALLOWED), index=0, horizontal=True,
                     help="Filters by the questionnaire gate. Change it to see what quality costs.")
    winners = l1(comp, ALLOWED[scope])
    total = award_total(comp, winners)
    k1, k2, k3 = st.columns(3)
    k1.metric("Cheapest-per-line award", money(total))
    k2.metric("Saving vs last year", money(ly_total - total), f"{(ly_total - total) / ly_total * 100:.1f}%")
    k3.metric("Lines covered", f"{len(winners)} / {len(lines)}")

    gcols = st.columns(len(comp.vendors))
    for col, v in zip(gcols, comp.vendors):
        g = comp.gates[v]
        col.markdown(f"**{v}**  \n:{GATE_COLOR[g.status]}[{g.label}]")
        for x in g.reasons[:1]:
            col.caption(x)

    rows = []
    for ln in lines:
        row = {"Code": ln.code, "Item": ln.description, "Size (mm)": ln.dims_label, "Qty": ln.annual_qty,
               "Last yr": ln.ly_price}
        for v in comp.vendors:
            c = comp.cells.get((v, ln.code))
            row[v] = c.price if c else None
        row["Winner"] = winners.get(ln.code, ("–",))[0]
        rows.append(row)
    df = pd.DataFrame(rows)

    def styles(_):
        css = pd.DataFrame("", index=df.index, columns=df.columns)
        for i, ln in enumerate(lines):
            for v in comp.vendors:
                if comp.open_flags(v, ln.code, d):
                    css.loc[i, v] = "background-color: rgba(255, 170, 0, 0.22)"
                if winners.get(ln.code, (None,))[0] == v:
                    css.loc[i, v] = "background-color: rgba(46, 160, 67, 0.28); font-weight: 600"
        return css

    styler = (df.style.apply(styles, axis=None)
              .format({**{v: "{:,.2f}" for v in comp.vendors}, "Last yr": "{:,.2f}", "Qty": "{:,}"}, na_rep="—"))
    st.caption("Green = cheapest allowed quote. Amber = needs review. — = not quoted. Click any price to see how it was worked out.")
    ev = st.dataframe(styler, hide_index=True, width="stretch", height=1100,
                      on_select="rerun", selection_mode="single-cell", key="grid")

    totals = {v: sum((comp.cells[(v, l.code)].price or 0) * l.annual_qty for l in lines
                     if (v, l.code) in comp.cells and comp.cells[(v, l.code)].price is not None) for v in comp.vendors}
    counts = {v: sum(1 for l in lines if (v, l.code) in comp.cells and comp.cells[(v, l.code)].price is not None)
              for v in comp.vendors}
    st.caption("Annual value of each vendor's quoted lines: " + " · ".join(
        f"{v} {money(totals[v])} ({counts[v]} lines)" for v in comp.vendors))

    sel = ev.selection.cells if ev and ev.selection else []
    if sel:
        row_i, col = sel[0]
        ln = lines[row_i]
        v = col if col in comp.vendors else None
        with st.container(border=True):
            if v is None:
                st.info("Click a vendor's price to see its working.")
            elif (v, ln.code) not in comp.cells:
                st.markdown(f"**{v} · {ln.code} {ln.description}**")
                st.write(f"{v} did not quote this line. It's left out of {v}'s totals, not counted as zero.")
            else:
                c = comp.cells[(v, ln.code)]
                st.markdown(f"**{v} · {ln.code} {ln.description}** ({ln.dims_label} mm, {ln.ply}-ply)")
                left, right = st.columns([3, 2])
                with left:
                    st.metric("₹/pc landed, before GST", f"{c.price:,.2f}" if c.price is not None else "—",
                              f"{(c.price / ln.ly_price - 1) * 100:+.1f}% vs last year" if c.price else None,
                              delta_color="inverse")
                    st.dataframe(pd.DataFrame([{"Step": s.label, "Value": round(s.value, 4), "Unit": s.unit}
                                               for s in c.steps]), hide_index=True, width="stretch")
                with right:
                    st.markdown(f"**Source:** {', '.join(resps[v].source_files)}")
                    if c.evidence:
                        st.markdown(f"> {c.evidence}")
                    st.markdown(f"**Match confidence:** {c.match_confidence:.0%}")
                    for f in c.flags:
                        done = (v, ln.code, f.text) in d.approved
                        st.markdown(f"{SEV[f.severity]} {f.text}" + (" · *approved*" if done else ""))
                alt = comp.alternates.get((v, ln.code))
                if alt and alt.price is not None:
                    st.markdown(f"**Alternate offered:** ₹{alt.price:,.2f}/pc · " + "; ".join(f.text for f in alt.flags if f.severity != "info"))

# ---------------------------------------------------------------- Ask / Award
with tabs[4]:
    st.info("Ask questions across the comparison in plain language. Coming in the next build block.")
with tabs[5]:
    st.info("Recommended award, savings, conditions and export. Coming in the next build block.")

# ---------------------------------------------------------------- Assumptions
with tabs[6]:
    st.subheader("Assumptions")
    st.caption("Every number the system assumes rather than reads. Change one and every price recalculates.")
    c1, c2 = st.columns(2)
    c1.number_input("USD → INR", step=0.25, format="%.2f", key="a_fx")
    c1.number_input("Freight estimate (₹/kg) for ex-works quotes", step=0.25, key="a_freight")
    c1.number_input("GST rate for inclusive quotes", step=0.01, format="%.2f", key="a_gst")
    c2.toggle("Apply conditional discounts (e.g. 'PO above ₹10L')", key="a_disc")
    c2.number_input("Flag size deviation above", step=0.01, format="%.2f", key="a_size")
    c2.number_input("Flag price change vs last year above", step=0.05, format="%.2f", key="a_band")
    st.caption(f"Award under current assumptions, cleared vendors only: {money(award_total(comp, l1(comp, ALLOWED['Cleared only'])))}")
