"""저장소 연결: PostgreSQL(장소 속성)과 Chroma(벡터). 접속 문자열은 비밀값이므로 출력하지 않는다."""
import os

import chromadb
import psycopg

from settings import secret


def connect() -> psycopg.Connection:
    # compose 안에서는 DATABASE_URL, 로컬에서는 compose가 연 localhost:5432
    dsn = os.environ.get("DATABASE_URL") or \
        f"postgresql://busanmate:{secret('POSTGRES_PASSWORD')}@localhost:5432/busanmate"
    return psycopg.connect(dsn)


def collection(name: str):
    client = chromadb.HttpClient(host=os.environ.get("CHROMA_HOST", "localhost"),
                                 port=int(os.environ.get("CHROMA_PORT", "8001")))
    return client.get_or_create_collection(name, embedding_function=None,
                                           configuration={"hnsw": {"space": "cosine"}})
