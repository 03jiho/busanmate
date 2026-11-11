"""SDD 7.5 평가: 평가 질의 50개(eval/queries.jsonl)로 조건 추출 정확도·Precision@5·환각 건수·응답시간 중앙값을 잰다.
실행: python -m eval.run_eval [API_URL] [대기초]   (docker compose up 후. 결과는 표로 출력하고 docs/eval-report.md에 저장)
환각 = 응답(코스·검색 결과)에 나온 장소 중 활성 DB 행이 아닌 것 + 답변·추천 이유에 남은 [[id]] 마커."""
import json
import statistics
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

from agent.query_parser import parse
from eval.conditions_eval import TODAY, grade, precision_at_5
from services.search import fetch_places

API = next((a for a in sys.argv[1:] if a.startswith("http")), "http://localhost:8000")
WAIT = float(next((a for a in sys.argv[1:] if a.replace(".", "").isdigit()), 6))  # 무료 한도 보호


def main() -> None:
    cases = [json.loads(line) for line in Path(__file__).with_name("queries.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip() and "_note" not in line]
    correct, precisions, times, hallucinated, places_seen, errors = 0, [], [], 0, 0, 0
    for n, case in enumerate(cases, 1):
        p = parse(case["q"], TODAY, has_course=case["intent"] == "edit")
        wrong = grade(case, p)
        correct += not wrong
        if "rel" in case:
            precisions.append(precision_at_5(case, p["conditions"]))
        note = ""
        if case["intent"] in ("search", "course"):  # 전체 응답으로 환각·응답시간
            t = time.time()
            try:
                r = requests.post(f"{API}/api/chat", json={"session_id": uuid.uuid4().hex, "message": case["q"]},
                                  timeout=180).json()
            except requests.RequestException as e:  # 한 건의 연결 실패로 평가 전체가 멈추지 않게
                errors += 1
                print(f"[{n:02d}] 요청 실패 {type(e).__name__}: {case['q']}", flush=True)
                time.sleep(WAIT)
                continue
            times.append(time.time() - t)
            items = [i for d in (r.get("course") or {}).get("days", []) for i in d["items"]] + (r.get("places") or [])
            ids = {i["place_id"] for i in items}
            bad = len(ids - set(fetch_places(list(ids)))) + ("[[" in r.get("answer", "")) \
                + sum("[[" in (i.get("reason") or "") for i in items)
            hallucinated, places_seen = hallucinated + bad, places_seen + len(ids)
            note = f" {times[-1]:.1f}초 장소{len(ids)} 환각{bad}"
        print(f"[{n:02d}] {'O' if not wrong else 'X'} {case['q']}{note}" + (f"  <- {', '.join(wrong)}" if wrong else ""), flush=True)
        time.sleep(WAIT)

    acc, p5, med = correct / len(cases), sum(precisions) / len(precisions), statistics.median(times)
    rows = [("조건 추출 정확도 (AC-003)", f"{correct}/{len(cases)} = {acc:.0%}", "≥ 90%", acc >= 0.9),
            ("Precision@5 (AC-003)", f"{p5:.2f} ({len(precisions)}개 질의)", "≥ 0.7", p5 >= 0.7),
            ("환각 건수 (AC-004)", f"{hallucinated}건 / 응답 장소 {places_seen}곳", "0건", hallucinated == 0),
            ("응답시간 중앙값 (NFR-001)", f"{med:.1f}초 ({len(times)}건)", "≤ 15초", med <= 15),
            ("API 요청 실패", f"{errors}건", "0건", errors == 0)]
    table = "\n".join(["| 지표 | 결과 | 목표 | 판정 |", "|---|---|---|---|"]
                      + [f"| {a} | {b} | {c} | {'통과' if ok else '미달'} |" for a, b, c, ok in rows])
    print("\n" + table)
    Path("docs/eval-report.md").write_text(
        f"# 평가 결과 (SDD 7.5)\n\n- 실행: {datetime.now():%Y-%m-%d %H:%M}, API {API}\n"
        f"- 평가셋: `eval/queries.jsonl` 50개(초안, 팀 검토 필요). 환각은 응답 속 장소를 DB와 자동 대조한 값이다.\n\n{table}\n",
        encoding="utf-8")


if __name__ == "__main__":
    main()
