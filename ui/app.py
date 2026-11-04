"""Streamlit UI (COMP-01). api만 호출한다. 실행: streamlit run ui/app.py"""
import os

import requests
import streamlit as st

API_URL = os.environ.get("API_URL", "http://localhost:8000")

st.set_page_config(page_title="BusanMate", page_icon="🌊")
st.title("BusanMate")
st.caption("부산 맞춤 여행코스 안내 (프로토타입)")

try:
    status = requests.get(f"{API_URL}/api/health", timeout=3).json()["status"]
except requests.RequestException:
    status = "unreachable"
st.write(f"API 상태: {status}")
