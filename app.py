import streamlit as st

st.set_page_config(page_title="RFQ Copilot", layout="wide")

st.title("RFQ Copilot")
st.write("Corrugated packaging · 30 line items · 5 vendors. Build in progress.")

try:
    key_ok = "ANTHROPIC_API_KEY" in st.secrets
except Exception:
    key_ok = False

if key_ok:
    st.success("API key found in Streamlit secrets.")
else:
    st.warning("No API key yet. Add ANTHROPIC_API_KEY under app settings → Secrets.")
