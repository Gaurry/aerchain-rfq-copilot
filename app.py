"""RFQ Copilot — Streamlit entry point.

Screens: Draft RFQ · Responses · Review · Compare · Ask · Award · Assumptions.
Extraction results live in data/extracted/ (written by the real AI pipeline,
never hand-made). Until a vendor has been extracted, it shows as pending."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from core.rfq import Assumptions, QUESTIONNAIRE, load_rfq

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / "data" / "inbox"
EXTRACTED = ROOT / "data" / "extracted"

st.set_page_config(page_title="RFQ Copilot", layout="wide")


@st.cache_data
def rfq_lines():
    return load_rfq()


if "assumptions" not in st.session_state:
    st.session_state.assumptions = Assumptions()


def api_key_present() -> bool:
    try:
        return "ANTHROPIC_API_KEY" in st.secrets
    except Exception:
        return False


VENDORS = [
    {"name": "Kraftline", "files": ["1_Kraftline_Offer.xlsx"], "format": "Excel"},
    {"name": "Shree Balaji", "files": ["2_ShreeBalaji_Quotation.pdf", "2b_ShreeBalaji_ISO_Certificate.pdf"], "format": "PDF + certificate"},
    {"name": "Pacific", "files": ["3_Pacific_Proposal.docx"], "format": "Word"},
    {"name": "Gupta", "files": ["4_Gupta_RateCard_photo.jpg"], "format": "Phone photo"},
    {"name": "Om Sai", "files": ["5_OmSai_reply.eml"], "format": "Email"},
]

# --- header --------------------------------------------------------------------
lines = rfq_lines()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Category", "Corrugated packaging")
c2.metric("Line items", len(lines))
c3.metric("Vendors invited", len(VENDORS))
c4.metric("Last-year spend", f"₹{sum(l.ly_spend for l in lines)/1e7:.2f} Cr")
if not api_key_present():
    st.caption("AI features need ANTHROPIC_API_KEY in Streamlit secrets.")

tabs = st.tabs(["Draft RFQ", "Responses", "Review", "Compare", "Ask", "Award", "Assumptions"])

# --- Draft RFQ -------------------------------------------------------------------
with tabs[0]:
    st.subheader("RFQ: corrugated boxes FY27")
    st.caption("Co-pilot drafting arrives next. Below is the structured RFQ every response is matched against.")
    st.dataframe(pd.DataFrame([{
        "Line": l.line, "Code": l.code, "Description": l.description, "Type": l.box_type,
        "Size (mm)": l.dims_label, "Ply": l.ply, "Paper": l.paper_spec,
        "Print": "Plain" if not l.print_colours else f"{l.print_colours} colour",
        "Annual qty": l.annual_qty, "Last-year ₹/pc": l.ly_price,
    } for l in lines]), hide_index=True, width="stretch")
    with st.expander("Supplier questionnaire"):
        st.dataframe(pd.DataFrame(QUESTIONNAIRE), hide_index=True, width="stretch")

# --- Responses ---------------------------------------------------------------------
with tabs[1]:
    st.subheader("Vendor responses")
    st.caption("Email is stubbed: replies land in the inbox folder. Reading them is real AI.")
    rows = []
    for v in VENDORS:
        done = (EXTRACTED / f"{v['name'].replace(' ', '_')}.json").exists()
        rows.append({"Vendor": v["name"], "Received as": v["format"], "Files": ", ".join(v["files"]),
                     "Status": "Read" if done else "Waiting to be read"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.file_uploader("Add a vendor response (any format)", accept_multiple_files=True, disabled=True,
                     help="Live upload arrives with the extraction pipeline")

# --- Review / Compare / Ask / Award (next build blocks) -----------------------------
with tabs[2]:
    st.info("Flags needing a buyer decision will appear here once responses are read.")
with tabs[3]:
    st.info("30 lines × 5 vendors, normalized to ₹/pc landed ex-GST, with the working behind every number.")
with tabs[4]:
    st.info("Ask questions across the comparison in plain language.")
with tabs[5]:
    st.info("Recommended award, savings and open risks, with export.")

# --- Assumptions -------------------------------------------------------------------
with tabs[6]:
    st.subheader("Assumptions")
    st.caption("Every number the system assumes rather than reads. Change one and every price recalculates.")
    a: Assumptions = st.session_state.assumptions
    col1, col2 = st.columns(2)
    a.fx_usd_inr = col1.number_input("USD → INR", value=a.fx_usd_inr, step=0.25, format="%.2f")
    a.freight_per_kg = col1.number_input("Freight estimate (₹/kg) for ex-works quotes", value=a.freight_per_kg, step=0.25)
    a.gst_rate = col1.number_input("GST rate for inclusive quotes", value=a.gst_rate, step=0.01, format="%.2f")
    a.apply_conditional_discounts = col2.toggle("Apply conditional discounts (e.g. 'PO above ₹10L')",
                                                value=a.apply_conditional_discounts)
    a.size_deviation_review = col2.number_input("Flag size deviation above", value=a.size_deviation_review,
                                                step=0.01, format="%.2f")
    a.price_sanity_band = col2.number_input("Flag price change vs last year above", value=a.price_sanity_band,
                                            step=0.05, format="%.2f")
