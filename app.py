"""RFQ Copilot — Streamlit entry point.

The buyer's journey in order: draft the RFQ by chatting → send → replies arrive
→ the AI reads them live → review → compare → ask → award. One chat panel on the
left drafts the RFQ first, then answers questions once responses are read.
Every number comes from live AI extraction plus deterministic code."""
from __future__ import annotations

import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import pandas as pd
import plotly.express as px
import streamlit as st

from core import analyst, copilot
from core.compare import ALLOWED, OPEN, Decisions, award_total, build, l1, log_correction
from core.draft import RFQDraft
from core.extract import INBOX, MODEL, OUT, VENDOR_FILES, _api_key, load_ai_matches, load_extracted, read_and_match
from core.rfq import Assumptions
from core.voice import voice_input

st.set_page_config(page_title="RFQ Copilot", layout="wide")

GATE_COLOR = {"cleared": "green", "cleared_stale": "green", "conditional": "orange", "failed": "red", "unknown": "gray"}
SEV = {"risk": ":red[**Risk**]", "review": ":orange[**Review**]", "info": ":gray[Info]"}
STAGES = ["draft", "sent", "replies", "read"]
STEP_LABELS = ["1 · Draft the RFQ", "2 · Send to vendors", "3 · Replies arrive", "4 · AI reads them", "5 · Review, compare, ask, award"]
VENDOR_EMAILS = {"Kraftline": "sales@kraftline-pkg.example", "Shree Balaji": "quotes@shreebalaji.example",
                 "Pacific": "north.accounts@pacificboxboard.example", "Gupta": "WhatsApp +91 98XXX XXXXX",
                 "Om Sai": "rakesh@omsaipackaging.example"}
REPLY_DAYS = {"Om Sai": 9, "Kraftline": 8, "Shree Balaji": 7, "Pacific": 8, "Gupta": 4}

# ---------------------------------------------------------------- state
ss = st.session_state
ss.setdefault("stage", "draft")
ss.setdefault("draft", RFQDraft())
ss.setdefault("chat", [])            # what the buyer sees
ss.setdefault("copilot_msgs", [])    # full API context for the co-pilot
ss.setdefault("analyst_msgs", [])    # full API context for the analyst
ss.setdefault("read_status", {})
ss.setdefault("mic_n", 0)

ss.setdefault("decisions", Decisions())
_DEF = Assumptions()
WIDGETS = {"a_fx": "fx_usd_inr", "a_freight": "freight_per_kg", "a_gst": "gst_rate",
           "a_disc": "apply_conditional_discounts", "a_size": "size_deviation_review", "a_band": "price_sanity_band"}
for _k, _attr in WIDGETS.items():
    ss.setdefault(_k, getattr(_DEF, _attr))
ss["assumptions"] = Assumptions(**{attr: ss[k] for k, attr in WIDGETS.items()})


def api_key_present() -> bool:
    try:
        _api_key()
        return True
    except Exception:
        return False


def client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=_api_key())


def extraction_meta() -> dict[str, dict]:
    out = {}
    for p in sorted(OUT.glob("*.json")):
        dd = json.loads(p.read_text())
        out[dd["response"]["vendor_name"]] = dd["meta"]
    return out


def find_file(name: str) -> Path:
    for p in (INBOX / name, INBOX / "uploads" / name):
        if p.exists():
            return p
    raise FileNotFoundError(name)


def money(x: float) -> str:
    return f"₹{x/1e5:,.1f} L"


def decide(kind: str, vendor: str, codes: list[str], **extra):
    log_correction({"at": datetime.now(timezone.utc).isoformat(), "kind": kind, "vendor": vendor, "codes": codes, **extra})


def group_key(*parts) -> str:
    return hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:10]


def waiting():
    st.info("This opens once vendor responses have been read. Follow the steps on the RFQ tab, "
            "or use **Skip to the finished example** in the chat panel.")


def load_finished_example():
    ss.draft = RFQDraft()
    ss.draft.start_from_last_year()
    ss.draft.sent = True
    ss.read_status = {v: "loaded from an earlier live read" for v in VENDOR_FILES}
    ss.stage = "read"
    ss.chat.append({"role": "assistant", "text": "Loaded the finished example: last year's 30 lines, sent to 5 vendors, "
                    "all 5 replies read. Ask me anything about the quotes."})


d: Decisions = ss.decisions
a: Assumptions = ss.assumptions
draft: RFQDraft = ss.draft
ready = ss.stage == "read"
lines = draft.lines
if ready:
    resps = load_extracted()
    resps = {v: r for v, r in resps.items() if v in ss.read_status}
    comp = build(resps, load_ai_matches(), lines, a, d)
    by_code = {l.code: l for l in lines}
ly_total = sum(l.ly_spend for l in lines)

# ---------------------------------------------------------------- chat panel
SUGGEST = {
    "draft": ["Same boxes as last year, bump the atta shippers by 20%",
              "Add a 2 kg atta shipper, 14x10x9 inch, 3-ply, 1 colour, 50,000 a year",
              "Make ISO and test reports non-negotiable, due 15 October"],
    "read": ["Cheapest per line, only among vendors who cleared the quality questionnaire?",
             "What is Balaji's expired certificate costing us?",
             "Chart the award value by vendor if we include Balaji",
             "Which numbers are you least sure about?"],
}


def send_chat(text: str):
    ss.chat.append({"role": "user", "text": text})
    try:
        if ss.stage == "draft":
            reply, changes, calls = copilot.chat(client(), MODEL, draft, ss.copilot_msgs, text)
            ss.chat.append({"role": "assistant", "text": reply, "changes": changes, "calls": calls})
        elif ss.stage == "read":
            reply, calls, displays = analyst.ask(client(), MODEL, comp, d, a, ss.analyst_msgs, text, draft.changes)
            ss.chat.append({"role": "assistant", "text": reply, "calls": calls, "displays": displays})
    except Exception as e:
        ss.chat.append({"role": "assistant", "text": f"I couldn't reach the AI just now ({type(e).__name__}). "
                                                     "Try again in a moment."})


def transcribe(wav: bytes) -> tuple[str | None, str | None]:
    """Backup path: server-side speech → text (Google's free web speech service). Returns (text, error)."""
    try:
        import speech_recognition as sr
        r = sr.Recognizer()
        with sr.AudioFile(io.BytesIO(wav)) as src:
            audio = r.record(src)
        return r.recognize_google(audio, language="en-IN"), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:200]


def voice_command(text: str):
    with st.spinner(f"“{text}”"):
        send_chat(text)
    for m in reversed(ss.chat):
        if m["role"] == "user":
            m["voice"] = True
            break


def render_display(x: dict, i: int):
    if x["kind"] == "chart":
        fig = px.bar(x=x["labels"], y=x["values"], labels={"x": "", "y": x["unit"]}, title=x["title"])
        fig.update_layout(height=280, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, width="stretch", key=f"chart-{i}")
    elif x["kind"] == "export":
        df_x = pd.DataFrame(x["rows"], columns=x["columns"])
        buf = io.BytesIO()
        df_x.to_excel(buf, index=False, sheet_name=x["title"][:30])
        st.download_button(f"Download: {x['title']}", buf.getvalue(), file_name=f"{x['title'][:40]}.xlsx", key=f"dl-{i}")


with st.sidebar:
    st.subheader("Co-pilot")
    mode = {"draft": "Drafting the RFQ. Tell me what you need.",
            "sent": "RFQ sent. I'll answer questions once replies are read.",
            "replies": "Replies are in. Read them on the RFQ tab, then ask me anything.",
            "read": "Responses read. Ask me about the quotes."}[ss.stage]
    st.caption(mode)
    if ss.stage != "read" and st.button("Skip to the finished example", width="stretch"):
        load_finished_example()
        st.rerun()
    for i, m in enumerate(ss.chat):
        with st.chat_message(m["role"]):
            st.markdown(("🎙️ " if m.get("voice") else "") + m["text"])
            if m.get("changes"):
                st.caption("Changed: " + " · ".join(m["changes"]))
            for j, x in enumerate(m.get("displays", [])):
                render_display(x, i * 10 + j)
            if m.get("calls"):
                with st.expander("Working"):
                    for c in m["calls"]:
                        st.caption(f"{c['tool']}({json.dumps(c['input'], ensure_ascii=False)[:160]})")
    if ss.stage in SUGGEST and api_key_present():
        for s_ in SUGGEST[ss.stage]:
            if st.button(s_, key=f"sg-{group_key(s_)}", width="stretch"):
                with st.spinner("Thinking…"):
                    send_chat(s_)
                st.rerun()
    can_chat = ss.stage in ("draft", "read") and api_key_present()
    # Primary: the browser's own speech recognition (Chrome/Edge), live words as you speak
    reason = None if can_chat else ("Add the API key to talk to the co-pilot." if not api_key_present()
                                    else "Voice opens again once replies are read.")
    said = voice_input(disabled=not can_chat, reason=reason)
    if said:
        voice_command(said)
        st.rerun()
    # Backup: record, then transcribe on the server
    with st.expander("Voice button not working? Record instead"):
        rec = st.audio_input("Record a command", key=f"mic-{ss.mic_n}", disabled=not can_chat)
        if rec is not None:
            heard, err = transcribe(rec.getvalue())
            ss.mic_n += 1
            if heard:
                voice_command(heard)
                st.rerun()
            st.error(f"Couldn't transcribe that ({err}). Please type it instead.")
    prompt = st.chat_input("…or type here" if can_chat else "Waiting for responses…", disabled=not can_chat)
    if prompt:
        with st.spinner("Thinking…"):
            send_chat(prompt)
        st.rerun()

# ---------------------------------------------------------------- step bar
cur = STAGES.index(ss.stage) + (1 if ready else 0)
cols = st.columns(len(STEP_LABELS))
for i, (col, lab) in enumerate(zip(cols, STEP_LABELS)):
    col.markdown(f"**:blue[{lab}]**" if i == cur else (f":green[{lab}]" if i < cur else f":gray[{lab}]"))
st.progress(cur / (len(STEP_LABELS) - 1))
if not api_key_present():
    st.caption("The AI needs ANTHROPIC_API_KEY in Streamlit secrets. The finished example still works without it.")

tabs = st.tabs(["RFQ", "Responses", "Review", "Compare", "Award", "Assumptions"])

# ---------------------------------------------------------------- RFQ (draft → send → replies → read)
with tabs[0]:
    t = draft.terms
    st.subheader(t["title"])
    if not draft.lines:
        st.info("The draft is empty. Tell the co-pilot what you need, for example "
                "*\"Same boxes as last year, bump atta by 20%\"*.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Lines", len(draft.lines))
        m2.metric("Due", t["due_date"])
        m3.metric("Payment", f"{t['payment_days']} days")
        m4.metric("Value at last year's prices", money(ly_total))
        st.dataframe(pd.DataFrame([{
            "Code": l.code, "Description": l.description, "Type": l.box_type, "Size (mm)": l.dims_label,
            "Ply": l.ply, "Print": "Plain" if not l.print_colours else f"{l.print_colours} colour",
            "Annual qty": l.annual_qty, "Last-year ₹/pc": l.ly_price, "Note": l.notes} for l in draft.lines]),
            hide_index=True, width="stretch", height=420)
        c1, c2 = st.columns(2)
        with c1.expander(f"Supplier questionnaire ({len(draft.questionnaire)} questions)"):
            st.dataframe(pd.DataFrame(draft.questionnaire), hide_index=True, width="stretch")
        with c2.expander(f"Change log ({len(draft.changes)})"):
            for ch in draft.changes:
                st.caption(ch)

    if ss.stage == "draft":
        if st.button("Send RFQ to 5 vendors", type="primary", disabled=not draft.lines):
            draft.sent = True
            ss.stage = "sent"
            st.rerun()
    if ss.stage in ("sent", "replies", "read"):
        st.markdown("#### Sent")
        st.caption(f"Email is stubbed. Logged as sent on 21 Sep 2026, due {t['due_date']}.")
        st.dataframe(pd.DataFrame([{"Vendor": v, "Sent to": e, "Status": "Sent"} for v, e in VENDOR_EMAILS.items()]),
                     hide_index=True, width="stretch")
    if ss.stage == "sent" and st.button("Simulate vendor replies", type="primary"):
        ss.stage = "replies"
        st.rerun()
    if ss.stage in ("replies", "read"):
        st.markdown("#### Inbox")
        st.dataframe(pd.DataFrame([{
            "Vendor": v, "Arrived": f"Day {REPLY_DAYS[v]}", "Files": ", ".join(fs),
            "Status": ss.read_status.get(v, "Not read yet")} for v, fs in VENDOR_FILES.items()]),
            hide_index=True, width="stretch")
    if ss.stage == "replies":
        if st.button("Read all responses with AI", type="primary", disabled=not api_key_present()):
            box = st.status("Reading 5 responses in parallel… about a minute", expanded=True)
            with ThreadPoolExecutor(max_workers=5) as pool:
                futs = {pool.submit(read_and_match, v, [INBOX / f for f in fs], draft.lines): v
                        for v, fs in VENDOR_FILES.items()}
                for f in as_completed(futs):
                    v = futs[f]
                    try:
                        f.result()
                        ss.read_status[v] = "Read live"
                        box.write(f"✓ {v}: read")
                    except Exception as e:
                        saved = (OUT / f"{v.replace(' ', '_')}.json").exists()
                        ss.read_status[v] = "Loaded from an earlier read (live read failed)" if saved else "Failed"
                        box.write(f"✗ {v}: {type(e).__name__}" + (" — using the earlier read" if saved else ""))
            box.update(label="Responses read", state="complete")
            ss.stage = "read"
            ss.chat.append({"role": "assistant", "text": "All replies are read. Check the Review tab for anything I'm unsure "
                            "about, or ask me a question."})
            st.rerun()

# ---------------------------------------------------------------- Responses
with tabs[1]:
    if not ready:
        waiting()
    else:
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
                        read_and_match(v, files, lines=lines)
                    ss.read_status[v] = "Read live"
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
                read_and_match(up_name.strip(), paths, lines=lines)
            ss.read_status[up_name.strip()] = "Read live (uploaded)"
            st.rerun()


# ---------------------------------------------------------------- Review
with tabs[2]:
    if not ready:
        waiting()
    else:
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
    if not ready:
        waiting()
    else:
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


# ---------------------------------------------------------------- Award
with tabs[4]:
    if not ready:
        waiting()
    else:
        st.subheader("Award recommendation")
        scope_a = st.radio("Who can win a line?", list(ALLOWED), index=0, horizontal=True, key="award_scope")
        wins = l1(comp, ALLOWED[scope_a])
        tot = award_total(comp, wins)
        ly_same = sum(by_code[c].ly_price * by_code[c].annual_qty for c in wins if by_code[c].ly_price)
        k1, k2, k3 = st.columns(3)
        k1.metric("Annual cost", money(tot))
        k2.metric("Saving vs last year", money(ly_same - tot))
        k3.metric("Lines awarded", f"{len(wins)} / {len(lines)}")
        rows = [{"Code": c, "Item": by_code[c].description, "Vendor": v, "₹/pc": round(p, 2),
                 "Annual qty": by_code[c].annual_qty, "Annual cost (₹)": round(p * by_code[c].annual_qty),
                 "Last-year ₹/pc": by_code[c].ly_price,
                 "Open review items": len(comp.open_flags(v, c, d))} for c, (v, p) in wins.items()]
        award_df = pd.DataFrame(rows)
        split = award_df.groupby("Vendor").agg(Lines=("Code", "count"), **{"Annual cost (₹)": ("Annual cost (₹)", "sum")}).reset_index()
        cA, cB = st.columns([2, 3])
        cA.dataframe(split, hide_index=True, width="stretch")
        fig = px.bar(split, x="Vendor", y="Annual cost (₹)", title="Award value by vendor")
        fig.update_layout(height=260, margin=dict(l=10, r=10, t=40, b=10))
        cB.plotly_chart(fig, width="stretch")
        st.markdown("**Conditions and risks before signing**")
        conds = []
        for v in split["Vendor"]:
            g = comp.gates[v]
            if g.status != "cleared":
                conds.append(f"{v}: {g.label}. " + "; ".join(g.reasons))
            for disc in comp.responses[v].discounts:
                if disc.condition:
                    conds.append(f"{v}: price includes {disc.percent:g}% {disc.description}, only if {disc.condition}")
            for n_ in g.notes:
                conds.append(f"{v}: {n_}")
        open_n = int(award_df["Open review items"].sum())
        if open_n:
            conds.append(f"{open_n} open review flags on awarded lines (see the Review tab)")
        missing = [l.code for l in lines if l.code not in wins]
        if missing:
            conds.append(f"No eligible quote for {', '.join(missing)}")
        for c_ in conds or ["None"]:
            st.markdown(f"- {c_}")
        st.dataframe(award_df, hide_index=True, width="stretch")
        buf = io.BytesIO()
        with pd.ExcelWriter(buf) as xw:
            award_df.to_excel(xw, index=False, sheet_name="Award by line")
            split.to_excel(xw, index=False, sheet_name="By vendor")
            pd.DataFrame({"Conditions and risks": conds or ["None"]}).to_excel(xw, index=False, sheet_name="Conditions")
            pd.DataFrame([a.to_dict()]).T.reset_index().rename(columns={"index": "Assumption", 0: "Value"}).to_excel(
                xw, index=False, sheet_name="Assumptions")
        st.download_button("Export award for sign-off (Excel)", buf.getvalue(), file_name="award_recommendation.xlsx",
                           type="primary")

# ---------------------------------------------------------------- Assumptions
with tabs[5]:
    st.subheader("Assumptions")
    st.caption("Every number the system assumes rather than reads. Change one and every price and answer recalculates.")
    c1, c2 = st.columns(2)
    c1.number_input("USD → INR", step=0.25, format="%.2f", key="a_fx")
    c1.number_input("Freight estimate (₹/kg) for ex-works quotes", step=0.25, key="a_freight")
    c1.number_input("GST rate for inclusive quotes", step=0.01, format="%.2f", key="a_gst")
    c2.toggle("Apply conditional discounts (e.g. 'PO above ₹10L')", key="a_disc")
    c2.number_input("Flag size deviation above", step=0.01, format="%.2f", key="a_size")
    c2.number_input("Flag price change vs last year above", step=0.05, format="%.2f", key="a_band")
    if ready:
        st.caption(f"Award under current assumptions, cleared vendors only: {money(award_total(comp, l1(comp, ALLOWED['Cleared only'])))}")
