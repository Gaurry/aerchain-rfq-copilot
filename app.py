"""RFQ Copilot — one screen per step, in the buyer's real order:

  Draft RFQ → Send → Responses → Review → Compare → Award

A chat panel on the left (voice or typing) drafts the RFQ first, then answers
questions once the review is done. Every number comes from live AI extraction
plus deterministic code; buyer decisions sit on top and are logged."""
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
from core.compare import ALLOWED, Decisions, award_total, build, l1, log_correction
from core.draft import RFQDraft
from core.emails import CONTACTS, draft_followup, nice_date, rfq_email, template_followup
from core.extract import INBOX, MODEL, OUT, VENDOR_FILES, _api_key, load_ai_matches, load_extracted, read_and_match
from core.rfq import Assumptions
from core.voice import voice_input

st.set_page_config(page_title="RFQ Copilot", layout="wide")
st.markdown("<style>section[data-testid='stSidebar']{width:440px !important; min-width:440px !important;}</style>",
            unsafe_allow_html=True)

STEPS = [("draft", "Draft RFQ"), ("send", "Send"), ("responses", "Responses"),
         ("review", "Review"), ("compare", "Compare"), ("award", "Award")]
KEYS = [k for k, _ in STEPS]
GATE_COLOR = {"cleared": "green", "cleared_stale": "green", "conditional": "orange", "failed": "red", "unknown": "gray"}
SEV = {"risk": ":red[**Risk**]", "review": ":orange[**Check**]", "info": ":gray[Info]"}
PREVIEWS = INBOX / "previews"

# What arrived, as the vendor sent it (the stubbed inbox). Attachments are the real files.
REPLIES = {
    "Kraftline": {"when": "29 Sep 2026, 11:42", "channel": "Email", "subject": "Re: RFQ: Corrugated boxes FY27 | Kraftline offer",
                  "body": "Dear Sir/Madam,\n\nPlease find attached our commercial offer against your RFQ dated 21-Sep. "
                          "Rates are FOR Bawal. We have also suggested a couple of value-engineered options.\n\n"
                          "Regards,\nNeha Kapoor, Key Accounts"},
    "Shree Balaji": {"when": "28 Sep 2026, 17:05", "channel": "Email", "subject": "Quotation SBC/2026/447",
                     "body": "Respected Sir,\n\nKindly find enclosed our best quotation along with our ISO certificate.\n\n"
                             "Thanking you,\nMahesh Agarwal"},
    "Pacific": {"when": "29 Sep 2026, 09:18", "channel": "Email", "subject": "Pacific Boxboard – proposal for Bawal",
                "body": "Dear Procurement Team,\n\nThank you for the opportunity. Our proposal is attached.\n\n"
                        "Warm regards,\nAnand Krishnan"},
    "Gupta": {"when": "25 Sep 2026, 20:31", "channel": "WhatsApp", "subject": "",
              "body": "Sir rate list attached. Any size we can do. Pad also. – Sanjay"},
    "Om Sai": {"when": "30 Sep 2026, 21:47", "channel": "Email", "subject": "Re: RFQ - Corrugated boxes FY27 (30 items)",
               "body": None},  # the email itself is the attachment
}

# ---------------------------------------------------------------- state
ss = st.session_state
ss.setdefault("step", "draft")
ss.setdefault("reached", 0)
ss.setdefault("draft", RFQDraft())
ss.setdefault("chat", [])
ss.setdefault("copilot_msgs", [])
ss.setdefault("analyst_msgs", [])
ss.setdefault("read_status", {})
ss.setdefault("sent_at", None)
ss.setdefault("replies_in", False)
ss.setdefault("decisions", Decisions())
ss.setdefault("ask_queue", {})       # vendor → list of points to send back
ss.setdefault("sent_emails", [])     # follow-ups sent during review
ss.setdefault("mic_n", 0)
ss.setdefault("uploads", {})         # vendor → [file paths] added by the buyer
ss.setdefault("ack_unplaced", set()) # vendors whose unmatched items the buyer has dealt with
_DEF = Assumptions()
WIDGETS = {"a_fx": "fx_usd_inr", "a_freight": "freight_per_kg", "a_gst": "gst_rate",
           "a_disc": "apply_conditional_discounts", "a_size": "size_deviation_review", "a_band": "price_sanity_band"}
for _k, _attr in WIDGETS.items():
    ss.setdefault(_k, getattr(_DEF, _attr))
ss["assumptions"] = Assumptions(**{attr: ss[k] for k, attr in WIDGETS.items()})

d: Decisions = ss.decisions
a: Assumptions = ss.assumptions
draft: RFQDraft = ss.draft
lines = draft.lines
is_read = bool(ss.read_status)
if is_read:
    resps = {v: r for v, r in load_extracted().items() if v in ss.read_status}
    comp = build(resps, load_ai_matches(), lines, a, d)
    by_code = {l.code: l for l in lines}


# ---------------------------------------------------------------- helpers
def api_key_present() -> bool:
    try:
        _api_key()
        return True
    except Exception:
        return False


def client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=_api_key())


def money(x: float) -> str:
    return f"₹{x/1e5:,.1f} L"


def crore(x: float) -> str:
    return f"₹{x/1e7:,.2f} Cr"


def inr(x: float | int | None, dec: int = 0) -> str:
    """Indian digit grouping: 1,23,45,678."""
    if x is None:
        return ""
    neg, x = x < 0, abs(x)
    whole, frac = f"{x:.{dec}f}".split(".") if dec else (f"{x:.0f}", "")
    head, tail = whole[:-3], whole[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    out = (head + "," + tail) if head else tail
    return ("-" if neg else "") + out + (f".{frac}" if dec else "")


def contact(v: str) -> tuple[str, str, str]:
    return CONTACTS.get(v, ("Team", "", "email"))


def key_of(*parts) -> str:
    return hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:10]


def go(step: str):
    ss.step = step
    ss.reached = max(ss.reached, KEYS.index(step))


def decide(kind: str, vendor: str, codes: list[str], **extra):
    log_correction({"at": datetime.now(timezone.utc).isoformat(), "kind": kind, "vendor": vendor, "codes": codes, **extra})


def review_groups() -> dict[tuple, list[str]]:
    g: dict[tuple, list[str]] = {}
    for (v, code) in comp.cells:
        for f in comp.open_flags(v, code, d):
            g.setdefault((v, f.severity, f.text), []).append(code)
    return g


def rfq_excel() -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as xw:
        pd.DataFrame([{"Code": l.code, "Description": l.description, "Type": l.box_type, "Size LxWxH (mm)": l.dims_label,
                       "Ply": l.ply, "Paper": l.paper_spec, "Print": "Plain" if not l.print_colours else f"{l.print_colours} colour",
                       "Annual qty": l.annual_qty, "Your price per piece (₹)": ""} for l in lines]).to_excel(xw, index=False, sheet_name="Line items")
        pd.DataFrame([{"No": q["no"], "Question": q["question"], "Required": q["type"], "Your answer": ""}
                      for q in draft.questionnaire]).to_excel(xw, index=False, sheet_name="Questionnaire")
    return buf.getvalue()


def load_finished_example():
    ss.draft = RFQDraft()
    ss.draft.start_from_last_year()
    ss.draft.sent = True
    ss.sent_at = "21 Sep 2026"
    ss.replies_in = True
    ss.read_status = {v: "Read earlier (live AI)" for v in VENDOR_FILES}
    go("review")
    ss.chat.append({"role": "assistant", "text": "Loaded the finished example: last year's 30 lines, sent to 5 vendors, "
                    "all replies read. Finish the review, then ask me anything about the quotes."})


# ---------------------------------------------------------------- chat panel (voice or typing)
SUGGEST = {"draft": ["Same boxes as last year, bump the atta shippers by 20%",
                     "Add a 2 kg atta shipper, 14x10x9 inch, 3-ply, 1 colour, 50,000 a year",
                     "Make ISO non-negotiable, due 15 October"],
           "analyst": ["Cheapest per line, only among vendors who cleared the quality questionnaire?",
                       "What is Balaji's expired certificate costing us?",
                       "Chart the award value by vendor if we include Balaji"]}
chat_mode = "draft" if ss.step == "draft" and not draft.sent else ("analyst" if ss.reached >= KEYS.index("compare") else None)


def send_chat(text: str, voice: bool = False):
    ss.chat.append({"role": "user", "text": text, "voice": voice})
    try:
        if chat_mode == "draft":
            reply, changes, calls = copilot.chat(client(), MODEL, draft, ss.copilot_msgs, text)
            ss.chat.append({"role": "assistant", "text": reply, "changes": changes, "calls": calls})
        elif chat_mode == "analyst":
            reply, calls, displays = analyst.ask(client(), MODEL, comp, d, a, ss.analyst_msgs, text, draft.changes)
            ss.chat.append({"role": "assistant", "text": reply, "calls": calls, "displays": displays})
    except Exception as e:
        ss.chat.append({"role": "assistant", "text": f"I couldn't reach the AI just now ({type(e).__name__}). Try again in a moment."})


def transcribe(wav: bytes) -> tuple[str | None, str | None]:
    try:
        import speech_recognition as sr
        r = sr.Recognizer()
        with sr.AudioFile(io.BytesIO(wav)) as src:
            audio = r.record(src)
        return r.recognize_google(audio, language="en-IN"), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:200]


def render_display(x: dict, i: int):
    if x["kind"] == "chart":
        fig = px.bar(x=x["labels"], y=x["values"], labels={"x": "", "y": x["unit"]}, title=x["title"])
        fig.update_layout(height=260, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, width="stretch", key=f"chart-{i}")
    elif x["kind"] == "export":
        buf = io.BytesIO()
        pd.DataFrame(x["rows"], columns=x["columns"]).to_excel(buf, index=False, sheet_name=x["title"][:30])
        st.download_button(f"Download: {x['title']}", buf.getvalue(), file_name=f"{x['title'][:40]}.xlsx", key=f"dl-{i}")


with st.sidebar:
    st.subheader("Co-pilot")
    can_chat = chat_mode is not None and api_key_present()
    if not api_key_present():
        reason = "Add the API key to talk to the co-pilot."
    elif chat_mode is None:
        reason = "I'm back once the review is done. Then ask me anything about the quotes."
    else:
        reason = None
    st.caption({"draft": "Tell me what you need. Speak or type.",
                "analyst": "Ask me about the quotes. Speak or type."}.get(chat_mode, reason or ""))
    if ss.reached < KEYS.index("review") and st.button("Skip to the finished example", width="stretch"):
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
    if can_chat:
        for s_ in SUGGEST[chat_mode]:
            if st.button(s_, key=f"sg-{key_of(s_)}", width="stretch"):
                with st.spinner("Thinking…"):
                    send_chat(s_)
                st.rerun()
    said = voice_input(disabled=not can_chat, reason=reason)
    if said:
        with st.spinner(f"“{said}”"):
            send_chat(said, voice=True)
        st.rerun()
    with st.expander("Voice button not working? Record instead"):
        rec = st.audio_input("Record a command", key=f"mic-{ss.mic_n}", disabled=not can_chat)
        if rec is not None:
            heard, err = transcribe(rec.getvalue())
            ss.mic_n += 1
            if heard:
                send_chat(heard, voice=True)
                st.rerun()
            st.error(f"Couldn't transcribe that ({err}). Please type it instead.")
    prompt = st.chat_input("…or type here" if can_chat else "Ask after the review", disabled=not can_chat)
    if prompt:
        with st.spinner("Thinking…"):
            send_chat(prompt)
        st.rerun()

# ---------------------------------------------------------------- step bar
bar = st.columns(len(STEPS))
for i, (k, label) in enumerate(STEPS):
    here = ss.step == k
    if bar[i].button(f"{i + 1}. {label}", key=f"step-{k}", width="stretch",
                     type="primary" if here else "secondary", disabled=i > ss.reached):
        ss.step = k
        st.rerun()
if not api_key_present():
    st.caption("The AI needs ANTHROPIC_API_KEY in Streamlit secrets. The finished example still works without it.")
st.write("")

# ================================================================= 1. Draft RFQ
if ss.step == "draft":
    t = draft.terms
    st.subheader(f"Draft RFQ · {t['title']}")
    if not lines:
        st.info("Tell the co-pilot what you need, e.g. *“Same boxes as last year, bump the atta shippers by 20%.”* "
                "Use 🎙️ Speak in the panel on the left, or type.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Lines", len(lines))
        m2.metric("Reply by", nice_date(t["due_date"]))
        m3.metric("Payment", f"{t['payment_days']} days")
        m4.metric("At last year's prices", money(sum(l.ly_spend for l in lines)))
        st.dataframe(pd.DataFrame([{
            "Code": l.code, "Description": l.description, "Size (mm)": l.dims_label, "Ply": l.ply,
            "Print": "Plain" if not l.print_colours else f"{l.print_colours} colour",
            "Annual qty": l.annual_qty, "Last-year ₹/pc": l.ly_price, "Note": l.notes} for l in lines]),
            hide_index=True, width="stretch", height=380)
        c1, c2 = st.columns(2)
        with c1.expander(f"Supplier questionnaire ({len(draft.questionnaire)})"):
            for q in draft.questionnaire:
                st.markdown(f"{q['no']}. {q['question']} · *{q['type']}*")
        with c2.expander(f"What changed ({len(draft.changes)})"):
            for ch in draft.changes:
                st.caption(ch)
    if st.button("Next: review the emails to vendors →", type="primary", disabled=not lines):
        go("send")
        st.rerun()

# ================================================================= 2. Send
elif ss.step == "send":
    st.subheader("Send the RFQ")
    st.caption("This is exactly what each vendor receives. Sending is simulated; the attachment is real.")
    xls = rfq_excel()
    vt = st.tabs([f"{v} · {CONTACTS[v][2]}" for v in VENDOR_FILES])
    for tab, v in zip(vt, VENDOR_FILES):
        e = rfq_email(v, draft.terms, len(lines), len(draft.questionnaire))
        with tab, st.container(border=True):
            st.markdown(f"**To:** {CONTACTS[v][0]} &lt;{e['to']}&gt;" + (f"  \n**Subject:** {e['subject']}" if e["subject"] else ""))
            st.text(e["body"])
            st.download_button("📎 RFQ_Corrugated_FY27.xlsx", xls, file_name="RFQ_Corrugated_FY27.xlsx", key=f"att-{v}")
    if ss.sent_at:
        st.success(f"Sent to all 5 vendors on {ss.sent_at}.")
        if st.button("Next: see the replies →", type="primary"):
            go("responses")
            st.rerun()
    elif st.button("Send to all 5 vendors", type="primary"):
        draft.sent = True
        ss.sent_at = "21 Sep 2026"
        go("responses")
        st.rerun()

# ================================================================= 3. Responses
elif ss.step == "responses":
    st.subheader("Vendor replies")
    if not ss.replies_in:
        st.info("RFQ sent on 21 Sep. Replies usually trickle in over the next nine days.")
        if st.button("Fast-forward 9 days: show the replies", type="primary"):
            ss.replies_in = True
            st.rerun()
    else:
        st.caption("Exactly as each vendor sent it. Nobody used our template.")
        all_v = list(VENDOR_FILES) + [u for u in ss.uploads if u not in VENDOR_FILES]
        vt = st.tabs([f"{v} · {REPLIES[v]['channel']}" if v in REPLIES else f"{v} · uploaded" for v in all_v])
        for tab, v in zip(vt, all_v):
            if v not in REPLIES:
                with tab:
                    st.caption("Added by you. Shown as received.")
                    for fp in ss.uploads[v]:
                        fp = Path(fp)
                        st.markdown(f"**📎 {fp.name}**")
                        ext = fp.suffix.lower()
                        try:
                            if ext in (".jpg", ".jpeg", ".png", ".webp"):
                                st.image(str(fp), width=420)
                            elif ext in (".xlsx", ".xlsm"):
                                for sheet, df_s in pd.read_excel(fp, sheet_name=None, header=None).items():
                                    st.dataframe(df_s.fillna("").astype(str), hide_index=True, width="stretch", height=260)
                            elif ext == ".pdf":
                                st.caption("PDF attached (the AI reads the pages directly).")
                            else:
                                from core.readers import to_content_blocks
                                st.text(to_content_blocks(fp)[1]["text"][:3000])
                        except Exception as e:
                            st.warning(f"Can't preview this file ({type(e).__name__}). The AI may still read it.")
                    st.caption(f"Status: {ss.read_status.get(v, 'Not read yet')}")
                continue
            r = REPLIES[v]
            files = VENDOR_FILES[v]
            with tab:
                with st.container(border=True):
                    st.markdown(f"**From:** {CONTACTS[v][0]}, {v} &lt;{CONTACTS[v][1]}&gt; · {r['when']}"
                                + (f"  \n**Subject:** {r['subject']}" if r["subject"] else ""))
                    if r["body"]:
                        st.text(r["body"])
                    else:
                        from core.readers import to_content_blocks
                        st.text(to_content_blocks(INBOX / files[0])[1]["text"].split("\n\n", 1)[1])
                    if v != "Om Sai":
                        st.caption("Attachments: " + ", ".join(f"📎 {f}" for f in files))
                if v == "Kraftline":
                    for sheet, df_s in pd.read_excel(INBOX / files[0], sheet_name=None, header=None).items():
                        st.markdown(f"**{files[0]} · sheet “{sheet}”**")
                        st.dataframe(df_s.fillna("").astype(str), hide_index=True, width="stretch", height=300)
                elif v == "Shree Balaji":
                    c1, c2 = st.columns([3, 2])
                    c1.image(str(PREVIEWS / "balaji_quote-1.png"), caption=f"{files[0]} · page 1")
                    c2.image(str(PREVIEWS / "balaji_iso-1.png"), caption=files[1])
                    with st.expander("Pages 2–3"):
                        st.image([str(PREVIEWS / "balaji_quote-2.png"), str(PREVIEWS / "balaji_quote-3.png")], width=320)
                elif v == "Pacific":
                    import docx as _docx
                    with st.container(border=True, height=320):
                        for p in _docx.Document(INBOX / files[0]).paragraphs:
                            if p.text.strip():
                                st.markdown(p.text)
                elif v == "Gupta":
                    st.image(str(INBOX / files[0]), width=420, caption="Photo sent on WhatsApp")
                status = ss.read_status.get(v)
                st.caption(f"Status: {status or 'Not read yet'}")

        if not is_read:
            st.write("")
            if st.button("Read all 5 replies with AI (about a minute)", type="primary", disabled=not api_key_present()):
                box = st.status("Reading all five in parallel… about a minute", expanded=True)
                with ThreadPoolExecutor(max_workers=5) as pool:
                    futs = {pool.submit(read_and_match, v, [INBOX / f for f in fs], lines): v for v, fs in VENDOR_FILES.items()}
                    for f in as_completed(futs):
                        v = futs[f]
                        try:
                            f.result()
                            ss.read_status[v] = "Read live by AI"
                            box.write(f"✓ {v}")
                        except Exception as e:
                            saved = (OUT / f"{v.replace(' ', '_')}.json").exists()
                            ss.read_status[v] = "Earlier AI read used (live read failed)" if saved else "Failed"
                            box.write(f"✗ {v}: {type(e).__name__}" + (", using the earlier read" if saved else ""))
                box.update(label="All replies read", state="complete")
                st.rerun()
        else:
            st.divider()
            st.markdown("**What the AI read**")
            st.dataframe(pd.DataFrame([{
                "Vendor": v, "Prices read": len(resps[v].items),
                "Lines matched": f"{sum(1 for l in lines if (v, l.code) in comp.cells)} / {len(lines)}",
                "Quality gate": comp.gates[v].label, "Status": ss.read_status[v]} for v in resps]),
                hide_index=True, width="stretch")
            with st.expander("A vendor sent something else? Add their reply (any format)"):
                up_name = st.text_input("Vendor name", placeholder="Sharma Cartons", key="up_name")
                up_files = st.file_uploader("Files: Excel, PDF, Word, photo, email or text", accept_multiple_files=True,
                                            type=["xlsx", "pdf", "docx", "jpg", "jpeg", "png", "eml", "txt", "csv"], key="up_files")
                name = (up_name or "").strip()
                clash = name.lower() in {x.lower() for x in VENDOR_FILES}
                if clash:
                    st.caption("That vendor already replied. Use a different name.")
                if st.button("Read this reply with AI", disabled=not (api_key_present() and name and up_files and not clash)):
                    dest = INBOX / "uploads" / key_of(name, datetime.now().isoformat())
                    dest.mkdir(parents=True, exist_ok=True)
                    paths = []
                    for f in up_files:
                        fp = dest / Path(f.name).name
                        fp.write_bytes(f.getvalue())
                        paths.append(fp)
                    with st.spinner(f"Reading {name}'s reply… usually 20–60 seconds"):
                        try:
                            read_and_match(name, paths, lines)
                            ss.uploads[name] = [str(x) for x in paths]
                            ss.read_status[name] = "Read live by AI (you added it)"
                            decide("upload", name, [], files=[x.name for x in paths])
                            ss.step = "review"
                            ss.chat.append({"role": "assistant", "text": f"Read {name}'s reply. Anything I'm unsure about is on the Review step."})
                            st.rerun()
                        except Exception as e:
                            st.error(f"Couldn't read {name}'s files ({type(e).__name__}). The file may be damaged, empty or "
                                     "password-protected. Ask the vendor to resend, or try another format.")
            if st.button("Next: review what needs a decision →", type="primary"):
                go("review")
                st.rerun()

# ================================================================= 4. Review
elif ss.step == "review":
    groups = review_groups()
    unplaced = {v: items for v, items in getattr(comp, "unplaced", {}).items() if items and v not in ss.ack_unplaced}
    total_decided = len(d.approved) + len(d.corrected) + len(d.excluded) + len(d.asked)
    queued = {v: pts for v, pts in ss.ask_queue.items() if pts}
    st.subheader("Review")
    st.caption("Only what the AI isn't sure about. Clean prices go straight through. Finish this before comparing.")
    m1, m2, m3 = st.columns(3)
    m1.metric("Still to decide", len(groups) + len(unplaced))
    m2.metric("Questions waiting to be emailed", sum(len(p) for p in queued.values()))
    m3.metric("Emails sent to vendors", len(ss.sent_emails))

    for v, items in unplaced.items():
        k = key_of(v, "unplaced")
        with st.container(border=True):
            st.markdown(f"{SEV['risk']} · **{v}** · {len(items)} quoted item(s) couldn't be matched to any of our lines")
            st.caption("They're left out of the comparison until you decide. Check whether they're items we asked for.")
            for it in items[:8]:
                st.markdown(f"- “{it.description}” · {it.price_text}")
            b1, b2 = st.columns(2)
            if b1.button("Leave them out", key=f"ack-{k}", width="stretch"):
                ss.ack_unplaced.add(v)
                decide("leave_out_unmatched", v, [], items=[it.description for it in items])
                st.rerun()
            if b2.button("Ask vendor", key=f"askun-{k}", width="stretch"):
                ss.ack_unplaced.add(v)
                ss.ask_queue.setdefault(v, []).append(
                    "We couldn't match these items to our RFQ lines. Please tell us which of our item codes each one is: "
                    + "; ".join(f"“{it.description}” ({it.price_text})" for it in items[:8]))
                decide("ask_vendor_unmatched", v, [])
                st.rerun()

    order = sorted(groups.items(), key=lambda kv: (kv[0][1] != "risk", kv[0][0], kv[0][2]))
    for (v, sev, text), codes in order:
        k = key_of(v, sev, text)
        first = comp.cells[(v, codes[0])]
        with st.container(border=True):
            st.markdown(f"{SEV[sev]} · **{v}** · {text}")
            names = ", ".join(f"{c} {by_code[c].description}" for c in codes[:3])
            st.caption(f"{len(codes)} line{'s' if len(codes) > 1 else ''}: {names}{' …' if len(codes) > 3 else ''}"
                       + (f" · now ₹{first.price:,.2f}/pc" if len(codes) == 1 and first.price else ""))
            if first.evidence:
                st.markdown(f"> {first.evidence}")
            b1, b2, b3, b4 = st.columns(4)
            if b1.button("Approve", key=f"ok-{k}", width="stretch"):
                for c in codes:
                    d.approved.add((v, c, text))
                decide("approve", v, codes, flag=text)
                st.rerun()
            if b2.button("Ask vendor", key=f"ask-{k}", width="stretch"):
                for c in codes:
                    d.asked.add((v, c, text))
                pts = ss.ask_queue.setdefault(v, [])
                pts.append(f"{text} (lines {', '.join(codes[:6])}{'…' if len(codes) > 6 else ''}; you wrote: “{first.evidence or ''}”)")
                decide("ask_vendor", v, codes, flag=text)
                st.rerun()
            if b4.button(f"Exclude {'line' if len(codes) == 1 else f'{len(codes)} lines'}", key=f"ex-{k}", width="stretch"):
                for c in codes:
                    d.excluded.add((v, c))
                decide("exclude", v, codes, flag=text)
                st.rerun()
            if len(codes) == 1:
                with b3.popover("Correct", width="stretch"):
                    new = st.number_input("Correct ₹/pc", min_value=0.0, value=round(first.price or 0.0, 2), step=0.1, key=f"num-{k}")
                    if st.button("Save", key=f"fix-{k}"):
                        d.corrected[(v, codes[0])] = new
                        d.approved.add((v, codes[0], text))
                        decide("correct", v, codes, flag=text, old=first.price, new=new)
                        st.rerun()

    if queued:
        st.markdown("#### Emails to vendors")
        for v, pts in queued.items():
            with st.container(border=True):
                st.markdown(f"**To {contact(v)[0]}, {v}** ({contact(v)[2]})")
                kind = st.radio("Purpose", ["clarify", "confirm"], horizontal=True, key=f"kind-{v}",
                                format_func=lambda x: {"clarify": "Ask for details", "confirm": "Confirm our reading"}[x])
                tk = f"mail-{v}"
                if tk not in ss:
                    ss[tk] = template_followup(v, kind, pts)
                if st.button("Draft with AI", key=f"ai-{v}", disabled=not api_key_present()):
                    try:
                        ss[tk] = draft_followup(client(), MODEL, v, kind, pts)
                    except Exception as e:
                        st.warning(f"AI draft failed ({type(e).__name__}); kept the template.")
                    st.rerun()
                st.text_area("Message", key=tk, height=230)
                if st.button(f"Send to {v}", key=f"send-{v}", type="primary"):
                    ss.sent_emails.append({"vendor": v, "kind": kind, "body": ss[tk],
                                           "at": datetime.now().strftime("%d %b %Y, %H:%M"), "points": len(pts)})
                    ss.ask_queue[v] = []
                    del ss[tk]
                    decide("email_sent", v, [], purpose=kind)
                    st.rerun()

    if ss.sent_emails:
        with st.expander(f"Sent to vendors ({len(ss.sent_emails)})"):
            for e in ss.sent_emails:
                st.markdown(f"**{e['vendor']}** · {e['at']} · {e['points']} point(s) · *awaiting reply*")
                st.text(e["body"])

    st.write("")
    if groups or unplaced:
        st.info(f"{len(groups) + len(unplaced)} item(s) still need a decision before you can compare.")
    elif queued:
        st.info("Send the queued vendor emails to finish the review.")
    else:
        st.success("Review complete. Lines you asked vendors about stay marked until they reply.")
    if st.button("Next: compare the quotes →", type="primary", disabled=bool(groups or queued or unplaced)):
        go("compare")
        ss.chat.append({"role": "assistant", "text": "Review done. Ask me anything about the quotes, by voice or by typing."})
        st.rerun()

# ================================================================= 5. Compare
elif ss.step == "compare":
    st.subheader("Compare · ₹ per piece, delivered to Bawal, before GST")
    scope = st.radio("Who can win a line?", list(ALLOWED), index=0, horizontal=True,
                     help="Filters by the supplier-questionnaire gate. Flip it to see what quality costs.")
    winners = l1(comp, ALLOWED[scope])
    total = award_total(comp, winners)
    ly_same = sum(by_code[c].ly_price * by_code[c].annual_qty for c in winners if by_code[c].ly_price)
    k1, k2, k3 = st.columns(3)
    k1.metric("Cheapest-per-line award", money(total))
    k2.metric("Saving vs last year", money(ly_same - total))
    k3.metric("Lines covered", f"{len(winners)} / {len(lines)}")
    gcols = st.columns(len(comp.vendors))
    for col, v in zip(gcols, comp.vendors):
        g = comp.gates[v]
        col.markdown(f"**{v}**  \n:{GATE_COLOR[g.status]}[{g.label}]")
        if g.reasons:
            col.caption(g.reasons[0])

    def cell_text(v, l):
        c = comp.cells.get((v, l.code))
        if c is None:
            return "Not quoted"
        if c.price is None:
            return "Excluded" if (v, l.code) in d.excluded else "Can't compare"
        return f"{c.price:,.2f}" + (" ?" if comp.awaiting_vendor(v, l.code, d) else "")

    df = pd.DataFrame([{**{"Code": l.code, "Item": l.description, "Qty": inr(l.annual_qty),
                           "Last yr": f"{l.ly_price:,.2f}" if l.ly_price else "New",
                           "L1": winners.get(l.code, ("No eligible quote",))[0]},
                        **{v: cell_text(v, l) for v in comp.vendors}} for l in lines])
    tot_row = {"Code": "Total", "Item": "Quoted lines only", "Qty": "", "Last yr": "", "L1": ""}
    for v in comp.vendors:
        q_lines = [l for l in lines if (v, l.code) in comp.cells and comp.cells[(v, l.code)].price is not None]
        tot_row[v] = f"{money(sum(comp.cells[(v, l.code)].price * l.annual_qty for l in q_lines))} · {len(q_lines)}/{len(lines)}"
    df = pd.concat([df, pd.DataFrame([tot_row])], ignore_index=True)

    def styles(_):
        css = pd.DataFrame("", index=df.index, columns=df.columns)
        for i, l in enumerate(lines):
            for v in comp.vendors:
                if df.loc[i, v] in ("Not quoted", "Excluded", "Can't compare"):
                    css.loc[i, v] = "color: #8a8a8a; font-style: italic"
                if comp.awaiting_vendor(v, l.code, d):
                    css.loc[i, v] = "background-color: rgba(30, 120, 255, 0.18)"
                if winners.get(l.code, (None,))[0] == v:
                    css.loc[i, v] = "background-color: rgba(46, 160, 67, 0.28); font-weight: 600"
        css.iloc[-1, :] = "font-weight: 600; background-color: rgba(128,128,128,0.08)"
        return css

    st.caption("Green = cheapest allowed (L1). “?” and blue = waiting on the vendor's reply. “Not quoted” = the vendor "
               "skipped this line; it's left out of their total, never counted as zero. Click a price to see how it was worked out.")
    ev = st.dataframe(df.style.apply(styles, axis=None).set_properties(subset=comp.vendors + ["Qty", "Last yr"], **{"text-align": "right"}),
        hide_index=True, width="stretch", height=600, on_select="rerun", selection_mode="single-cell", key="grid",
        column_config={"Code": st.column_config.TextColumn(width=70), "Item": st.column_config.TextColumn(width=170),
                       "Qty": st.column_config.TextColumn(width=78), "Last yr": st.column_config.TextColumn(width=62),
                       "L1": st.column_config.TextColumn(width=96),
                       **{v: st.column_config.TextColumn(width=96) for v in comp.vendors}})
    st.caption("Totals row: each vendor's total on the lines they quoted, with coverage. Don't compare totals across "
               "different line counts; use the L1 column or ask the co-pilot.")
    sel = ev.selection.cells if ev and ev.selection else []
    if sel:
        row_i, col = sel[0]
    if sel and row_i < len(lines):
        l, v = lines[row_i], (sel[0][1] if sel[0][1] in comp.vendors else None)
        with st.container(border=True):
            if v is None:
                st.caption("Click a vendor's price to see its working.")
            elif (v, l.code) not in comp.cells:
                st.markdown(f"**{v} didn't quote {l.code} {l.description}.** It's left out of their total, not counted as zero.")
            else:
                c = comp.cells[(v, l.code)]
                st.markdown(f"**{v} · {l.code} {l.description}** ({l.dims_label} mm, {l.ply}-ply)")
                left, right = st.columns([3, 2])
                left.dataframe(pd.DataFrame([{"Step": s.label, "Value": round(s.value, 2), "Unit": s.unit} for s in c.steps]),
                               hide_index=True, width="stretch")
                right.markdown(f"**Vendor's words** ({', '.join(resps[v].source_files)})")
                if c.evidence:
                    right.markdown(f"> {c.evidence}")
                for f in c.flags:
                    state = " · approved" if (v, l.code, f.text) in d.approved else (" · asked vendor" if (v, l.code, f.text) in d.asked else "")
                    right.markdown(f"{SEV[f.severity]} {f.text}{state}")
                alt = comp.alternates.get((v, l.code))
                if alt and alt.price is not None:
                    st.caption(f"Also offered an alternate spec at ₹{alt.price:,.2f}/pc (not like-for-like).")
    with st.expander("Assumptions used (change one and every number updates)"):
        c1, c2 = st.columns(2)
        c1.number_input("USD → INR (RBI reference rate, 30 Sep 2026, assumed)", step=0.25, format="%.2f", key="a_fx")
        c1.number_input("Freight estimate for ex-works quotes (₹/kg)", step=0.25, key="a_freight")
        c1.number_input("GST rate for inclusive quotes", step=0.01, format="%.2f", key="a_gst")
        c2.toggle("Apply conditional discounts (e.g. 'PO above ₹10L')", key="a_disc")
        c2.number_input("Flag size deviation above", step=0.01, format="%.2f", key="a_size")
        c2.number_input("Flag price change vs last year above", step=0.05, format="%.2f", key="a_band")
    if st.button("Next: award →", type="primary"):
        go("award")
        st.rerun()

# ================================================================= 6. Award
elif ss.step == "award":
    st.subheader("Award recommendation")
    scope_a = st.radio("Who can win a line?", list(ALLOWED), index=0, horizontal=True, key="award_scope")
    wins = l1(comp, ALLOWED[scope_a])
    tot = award_total(comp, wins)
    ly_same = sum(by_code[c].ly_price * by_code[c].annual_qty for c in wins if by_code[c].ly_price)
    k1, k2, k3 = st.columns(3)
    k1.metric("Annual cost", money(tot))
    k2.metric("Saving vs last year", money(ly_same - tot))
    k3.metric("Lines awarded", f"{len(wins)} / {len(lines)}")
    _award_rows = [{"Code": c, "Item": by_code[c].description, "Vendor": v, "₹/pc": round(p, 2),
                              "Annual qty": by_code[c].annual_qty, "Annual cost (₹)": round(p * by_code[c].annual_qty),
                              "Last-year ₹/pc": by_code[c].ly_price,
                              "Waiting on vendor": "yes" if comp.awaiting_vendor(v, c, d) else ""} for c, (v, p) in wins.items()]
    award_df = pd.DataFrame(_award_rows)
    split = award_df.groupby("Vendor").agg(Lines=("Code", "count"), **{"Annual cost (₹)": ("Annual cost (₹)", "sum")}).reset_index()
    split = split.sort_values("Annual cost (₹)", ascending=False)
    parts = " and ".join(f"{r.Lines} line{'s' if r.Lines > 1 else ''} to {r.Vendor}" for r in split.itertuples())
    st.markdown(f"#### Award {parts}: **{crore(tot)}** a year, **{money(ly_same - tot)}** "
                f"{'below' if ly_same >= tot else 'above'} last year.")
    if scope_a == "Cleared only":
        alt = l1(comp, ALLOWED["Cleared + conditional"])
        alt_tot = award_total(comp, alt)
        cond_v = sorted({v for v, _ in alt.values() if comp.gates[v].status == "conditional"})
        if cond_v and alt_tot < tot - 1:
            why = "; ".join(f"{v}: {comp.gates[v].reasons[0]}" for v in cond_v if comp.gates[v].reasons)
            st.info(f"**If {', '.join(cond_v)} fix{'es' if len(cond_v) == 1 else ''} the open quality issue, the award drops to "
                    f"{crore(alt_tot)}: {money(tot - alt_tot)} a year cheaper.** {why}. "
                    f"Worth asking for before you sign.")
    shown = split.assign(**{"Annual cost (₹)": split["Annual cost (₹)"].map(inr)})
    cA, cB = st.columns([2, 3])
    cA.dataframe(shown, hide_index=True, width="stretch")
    fig = px.bar(split.assign(lakh=split["Annual cost (₹)"] / 1e5), x="Vendor", y="lakh",
                 title="Award value by vendor", labels={"lakh": "₹ lakh"})
    fig.update_layout(height=250, margin=dict(l=10, r=10, t=40, b=10))
    cB.plotly_chart(fig, width="stretch")
    conds = []
    for v in split["Vendor"]:
        g = comp.gates[v]
        if g.status != "cleared":
            conds.append(f"{v}: {g.label}. " + "; ".join(g.reasons))
        for disc in comp.responses[v].discounts:
            if disc.condition:
                conds.append(f"{v}: price includes {disc.percent:g}% {disc.description}, only if {disc.condition}")
        conds += [f"{v}: {n_}" for n_ in g.notes]
    waiting = sorted({v for v, c in [(r["Vendor"], r["Code"]) for _, r in award_df.iterrows()] if comp.awaiting_vendor(v, c, d)})
    if waiting:
        conds.append("Waiting on vendor replies: " + ", ".join(waiting))
    missing = [l.code for l in lines if l.code not in wins]
    if missing:
        conds.append(f"No eligible quote for {', '.join(missing)}")
    st.markdown("**Before signing**")
    for c_ in conds or ["Nothing outstanding"]:
        st.markdown(f"- {c_}")
    with st.expander("Award by line"):
        st.dataframe(award_df.assign(**{"Annual qty": award_df["Annual qty"].map(inr),
                                        "Annual cost (₹)": award_df["Annual cost (₹)"].map(inr)}), hide_index=True, width="stretch")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as xw:
        award_df.to_excel(xw, index=False, sheet_name="Award by line")
        split.to_excel(xw, index=False, sheet_name="By vendor")
        pd.DataFrame({"Before signing": conds or ["Nothing outstanding"]}).to_excel(xw, index=False, sheet_name="Conditions")
        pd.DataFrame(list(a.to_dict().items()), columns=["Assumption", "Value"]).astype(str).to_excel(xw, index=False, sheet_name="Assumptions")
    st.download_button("Export for sign-off (Excel)", buf.getvalue(), file_name="award_recommendation.xlsx", type="primary")
