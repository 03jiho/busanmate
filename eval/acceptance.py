"""수용 기준 점검: 실행 중인 API(docker compose up)에 시나리오를 보내 통과/실패를 출력한다.
실행: python -m eval.acceptance [API_URL]     AC-001·002·004·009 + 시나리오(부모님 바다, 비 오는 날, 수정, 사진)
     python -m eval.acceptance --outage        AC-006: 외부 API 키를 이 프로세스에서만 무효로 바꿔 장애 상황을 만든다"""
import os
import statistics
import sys
import time
import uuid

import requests

API = next((a for a in sys.argv[1:] if a.startswith("http")), "http://localhost:8000")
results: list[tuple[str, bool, str]] = []


def check(ac: str, ok: bool, detail: str) -> None:
    results.append((ac, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {ac}: {detail}", flush=True)


def chat(sid: str, msg: str) -> tuple[dict, float]:
    t = time.time()
    r = requests.post(f"{API}/api/chat", json={"session_id": sid, "message": msg}, timeout=180)
    return r.json(), time.time() - t


def items(r: dict) -> list[dict]:
    return [i for d in (r.get("course") or {}).get("days", []) for i in d["items"]]


def ids(r: dict) -> list[int]:
    return [i["place_id"] for i in items(r)] + [p["place_id"] for p in r.get("places") or []]


def run_api() -> None:
    from services.search import fetch_places
    seen = []

    r, _ = chat(uuid.uuid4().hex, "광안리 근처 횟집 찾아줘")  # AC-001
    places, srcs = r.get("places") or [], {s.get("place_id") for s in r.get("sources") or []}
    seen.append(r)
    check("AC-001", bool(places) and all(p["license"] and p["place_id"] in srcs for p in places),
          f"장소 {len(places)}곳, 모두 출처·이용 조건 포함" if places else f"장소 없음: {r.get('answer', r)}")

    times, bad = [], []  # AC-002: 15초 이내(중앙값)에 코스·추천 이유·지도 좌표
    for q in ["이번 주 토요일 해운대 바다 코스", "내일 남포동 걸어서 하루 코스", "모레 광안리 데이트 코스",
              "이번 주 일요일 기장 하루 여행", "내일 서면 실내 코스"]:
        r, sec = chat(uuid.uuid4().hex, q)
        seen.append(r)
        times.append(sec)
        its = items(r)
        if not its or not all(i.get("reason") and i.get("lat") for i in its):
            bad.append(f"{q}({len(its)}곳, 이유 {sum(bool(i.get('reason')) for i in its)}개)")
    med = statistics.median(times)
    check("AC-002", med <= 15 and not bad, f"중앙값 {med:.1f}초 (각 {[round(t, 1) for t in times]})"
          + (f", 미흡: {bad}" if bad else ", 코스·이유·좌표 모두 있음"))

    r, sec = chat(uuid.uuid4().hex, "11월 21일부터 2박 3일 해운대와 남포동 여행")  # AC-009
    seen.append(r)
    days = (r.get("course") or {}).get("days", [])
    all_ids = ids(r)
    check("AC-009", len(days) == 3 and len(all_ids) == len(set(all_ids)) and all(len(d["items"]) >= 2 for d in days),
          f"{len(days)}일, 날짜별 {[len(d['items']) for d in days]}곳, 중복 {len(all_ids) - len(set(all_ids))}, {sec:.1f}초")

    sid = uuid.uuid4().hex  # 시나리오: 부모님 바다 → (되묻기) → 수정
    r, sec = chat(sid, "이번 토요일 부모님과 걸어서 돌기 좋은 바다 코스")
    first = r["answer"][:40]
    if not items(r):
        r, sec = chat(sid, "부산 전체")
    seen.append(r)
    before = [i["place_id"] for i in items(r)]
    modes = {i["leg_to_next"]["mode"] for i in items(r) if i.get("leg_to_next")}
    check("시나리오 부모님 바다", bool(before) and modes <= {"walk"},
          f"첫 응답 '{first}…' → {len(before)}곳, 구간 수단 {modes or '없음'}")
    r2, _ = chat(sid, "두 번째 장소 빼줘")
    seen.append(r2)
    after = [i["place_id"] for i in items(r2)]
    check("시나리오 두 번째 장소 빼줘", len(before) >= 3 and sorted(after) == sorted(before[:1] + before[2:]),
          f"{len(before)}곳 → {len(after)}곳, 나머지 유지 {sorted(after) == sorted(before[:1] + before[2:])}")

    r, _ = chat(uuid.uuid4().hex, "내일 비 온다는데 실내 위주로 서면 코스")  # 시나리오: 비 오는 날
    seen.append(r)
    its = items(r)
    indoor = sum(i["category"] in ("문화시설", "음식점") for i in its)
    check("시나리오 비 오는 날", bool(its) and indoor / len(its) >= 0.5, f"{len(its)}곳 중 실내 {indoor}곳")

    import db  # 시나리오: 사진 업로드 (색인된 명소의 썸네일)
    with db.connect() as conn:
        url, name = conn.execute("SELECT i.url, p.name FROM place_image i JOIN place p USING (place_id) "
                                 "WHERE p.name LIKE '%%태종대%%' LIMIT 1").fetchone()
    img = requests.get(url.replace("_ttiel", "_thumbL"), timeout=10, headers={"User-Agent": "BusanMate/0.1"}).content
    t = time.time()
    p = requests.post(f"{API}/api/search/image", data={"session_id": uuid.uuid4().hex},
                      files={"file": ("p.jpg", img, "image/jpeg")}, timeout=60).json()
    top = p.get("matches", [{}])[0].get("name")
    check("시나리오 사진 업로드", p.get("mode") == "recognized" and top == name,
          f"{p.get('mode')} 1위 {top} ({time.time() - t:.1f}초)")

    leaked = [i for r in seen for i in ids(r)]  # AC-004(부분): 응답 속 장소가 모두 활성 DB 행인지
    active = fetch_places(leaked)
    marker = [r for r in seen if "[[" in r.get("answer", "")]
    check("AC-004(부분)", set(leaked) <= set(active) and not marker,
          f"응답 {len(seen)}건의 장소 {len(set(leaked))}곳 모두 DB에 있음, 마커 잔존 {len(marker)}건")


def run_outage() -> None:
    """AC-006: Gemini·카카오·기상청이 모두 실패해도 DB 데이터로 응답하고 안내하는지."""
    os.environ.update(GEMINI_API_KEY="invalid-for-test", KAKAO_REST_API_KEY="invalid-for-test",
                      KMA_FORECAST_URL="http://127.0.0.1:9/down")
    from agent.orchestrator import handle_chat
    t = time.time()
    r = handle_chat(uuid.uuid4().hex, "이번 주 토요일 해운대 바다 코스")
    its = items(r)
    est = all(i["leg_to_next"]["estimated"] for i in its if i.get("leg_to_next"))
    check("AC-006", bool(its) and est and bool(r["notices"]),
          f"{len(its)}곳, 이동시간 추정치 {est}, {time.time() - t:.1f}초, 안내 {r['notices']}")


if __name__ == "__main__":
    run_outage() if "--outage" in sys.argv else run_api()
    print(f"\n통과 {sum(ok for _, ok, _ in results)}/{len(results)}")
