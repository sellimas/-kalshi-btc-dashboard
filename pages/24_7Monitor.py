
import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timezone

st.set_page_config(
    page_title="BTC 24/7 Monitor",
    page_icon="📡",
    layout="wide"
)

st.title("📡 BTC 24/7 Collector Monitor")
st.caption("Railway data • Read-only • Manual trades only")

url = st.secrets.get("RAILWAY_API_URL", "").rstrip("/")
token = st.secrets.get("RAILWAY_READ_TOKEN", "")

if not url or not token:
    st.error("Railway connection secrets are missing.")
    st.stop()

headers = {"x-read-token": token}

def fetch(endpoint, params=None):
    response = requests.get(
        f"{url}/{endpoint}",
        headers=headers,
        params=params,
        timeout=15
    )
    response.raise_for_status()
    return response.json()

if st.button("🔄 Refresh collector data"):
    st.rerun()

try:
    health = fetch("health")
    summary = fetch("summary")
    latest = fetch("latest")

    st.success("Connected to Railway")

    c1, c2, c3 = st.columns(3)
    c1.metric("Observations", summary.get("observations", 0))
    c2.metric("Collector alive", str(health.get("collector_alive", False)))
    c3.metric("Latest BTC price", latest.get("btc_price", "N/A"))

    st.subheader("Collector status")
    st.json(health)

    st.subheader("Collection summary")
    st.json(summary)

    st.subheader("Latest observation")
    st.json(latest)

    st.subheader("Historical observations")

    history = fetch("history", params={"limit": 200})
    if isinstance(history, dict):
        rows = history.get("observations", history.get("items", []))
    else:
        rows = history

    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True)

        price_col = next(
            (c for c in ["btc_price", "btc_price_real"]
             if c in df.columns),
            None
        )
        if price_col:
            st.line_chart(df[price_col])
    else:
        st.info("No historical observations returned.")

except requests.exceptions.RequestException as exc:
    st.error(f"Railway request failed: {exc}")
except Exception as exc:
    st.error(f"Collector monitor error: {exc}")
