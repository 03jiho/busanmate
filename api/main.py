"""API 서버 (COMP-02). 실행: uvicorn api.main:app --reload"""
from fastapi import FastAPI

app = FastAPI(title="BusanMate API")


@app.get("/api/health")  # API-008
def health():
    return {"status": "ok"}
