"""AC-003: 조건 추출 정확도(목표 90%)와 Precision@5(목표 0.7). 평가셋은 eval/queries.jsonl(초안).
실행: python -m eval.conditions_eval [대기초]   (Gemini 호출 50회, 무료 한도를 고려해 질의마다 잠시 쉰다)"""
import json
import sys
import time
from datetime import date
from pathlib import Path

from agent.query_parser import parse
from services.search import search

TODAY = date(2026, 11, 9)  # 평가셋의 날짜 기준일


def grade(case: dict, p: dict) -> list[str]:
    """틀린 필드 목록(비면 정답)."""
    c, wrong = p["conditions"], []
    if p["intent"] != case["intent"]:
        wrong.append(f"intent={p['intent']}")
    if "gu" in case and sorted(c.gu) != sorted(case["gu"]):
        wrong.append(f"gu={c.gu}")
    if "days" in case and c.days != case["days"]:
        wrong.append(f"days={c.days}")
    if case["intent"] == "course" and (c.mode if c.mode != "auto" else None) != case.get("mode"):
        wrong.append(f"mode={c.mode}")
    if case.get("indoor") and c.indoor is not True:
        wrong.append(f"indoor={c.indoor}")
    if "date" in case and (not p["dates"] or p["dates"][0].isoformat() != case["date"]):
        wrong.append(f"date={p['dates'][:1]}")
    if "edit" in case:
        e = p["edit"] or {}
        if any(e.get(k) != v for k, v in case["edit"].items()):
            wrong.append(f"edit={e}")
    return wrong


def precision_at_5(case: dict, cond) -> float:
    top = search(cond)[0][:5]
    return sum(h["category"] in case["rel"] and (not case.get("gu") or h["gu"] in case["gu"]) for h in top) / 5


if __name__ == "__main__":
    cases = [json.loads(line) for line in Path(__file__).with_name("queries.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip() and "_note" not in line]
    correct, precisions = 0, []
    for case in cases:
        p = parse(case["q"], TODAY, has_course=case["intent"] == "edit")
        wrong = grade(case, p)
        correct += not wrong
        line = f"{'O' if not wrong else 'X'} {case['q']}" + (f"  <- {', '.join(wrong)}" if wrong else "")
        if "rel" in case:
            precisions.append(precision_at_5(case, p["conditions"]))
            line += f"  P@5={precisions[-1]:.1f}"
        print(line, flush=True)
        time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 4)
    print(f"\n조건 추출 정확도 {correct}/{len(cases)} = {correct / len(cases):.0%} (목표 90%)")
    print(f"Precision@5 평균 {sum(precisions) / len(precisions):.2f} ({len(precisions)}개 질의, 목표 0.7)")
