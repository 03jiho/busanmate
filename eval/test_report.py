"""TC-001~014 자동 실행: pytest를 돌려 SRS 추적표의 테스트 케이스별 결과를 docs/test-report.md로 저장한다.
실행: python -m eval.test_report   (DB·Chroma가 없으면 DB 테스트는 skip으로 기록된다)"""
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

# TC → (요구사항, 대상 테스트 "파일::함수" 접두어)
TC = {
    "TC-001": ("FR-001 조건 추출", ["test_agent::test_course_asks_for_missing_region_or_date",
                                 "test_agent::test_llm_down_uses_rules_and_default_plan", "test_agent::test_course_flow"]),
    "TC-002": ("FR-002 하이브리드 검색", ["test_search_db::"]),
    "TC-003": ("FR-003 출처 표시", ["test_agent::test_course_flow", "test_image::test_recognized_when_above_threshold"]),
    "TC-004": ("FR-004 날씨 반영", ["test_planner::test_rain_prefers_indoor", "test_agent::test_weather_helpers",
                                "test_agent::test_latlon_to_grid_matches_kma"]),
    "TC-005": ("FR-005 선호 설정", ["test_api::test_preferences"]),
    "TC-006": ("FR-006 코스 구성", ["test_planner::test_tc006_course_rules",
                                "test_planner::test_no_three_in_a_row_even_when_one_category_dominates",
                                "test_planner::test_places_per_day", "test_planner::test_course_is_pure_code"]),
    "TC-007": ("FR-007 지도·동선", ["test_planner::test_leg_mode_auto_walk_or_taxi", "test_agent::test_course_flow"]),
    "TC-008": ("FR-008 사진 인식", ["test_image::test_validate", "test_image::test_aggregate_takes_max_per_place",
                                "test_image::test_recognized_when_above_threshold", "test_image::test_api_rejects_bad_files"]),
    "TC-009": ("FR-009 유사 분위기 추천", ["test_image::test_similar_mood_uses_vlm_description",
                                    "test_image::test_vlm_failure_falls_back_to_clip"]),
    "TC-010": ("FR-010 축제 반영", ["test_festival_db::"]),
    "TC-011": ("FR-011 근거 기반 설명", ["test_agent::test_verify_places_drops_unknown_and_names_known",
                                   "test_agent::test_marker_josa", "test_agent::test_course_flow"]),
    "TC-012": ("FR-012 대화형 코스 수정", ["test_agent::test_llm_down_uses_rules_and_default_plan",
                                     "test_agent::test_build_course_before_search_is_rejected"]),
    "TC-013": ("FR-013 데이터 수집·갱신", ["test_pipeline_db::", "test_busan::"]),
    "TC-014": ("FR-014 다일 일정", ["test_planner::test_tc014_multi_day", "test_planner::test_days_capped_at_three"]),
}


def run_pytest() -> dict[str, str]:
    """{'파일::함수[파라미터]': passed|failed|skipped}"""
    with tempfile.TemporaryDirectory() as d:
        xml = Path(d) / "junit.xml"
        subprocess.run([sys.executable, "-m", "pytest", "-q", f"--junitxml={xml}"], capture_output=True)
        out = {}
        for case in ET.parse(xml).iter("testcase"):
            key = f"{case.get('classname').split('.')[-1]}::{case.get('name')}"
            status = "failed" if case.find("failure") is not None or case.find("error") is not None else \
                "skipped" if case.find("skipped") is not None else "passed"
            out[key] = status
        return out


def main() -> None:
    results = run_pytest()
    lines = ["# 테스트 보고서 (TC-001~014)\n", f"- 실행: {datetime.now():%Y-%m-%d %H:%M}, `python -m eval.test_report`",
             f"- 전체 pytest: {len(results)}개 중 통과 {list(results.values()).count('passed')}, "
             f"실패 {list(results.values()).count('failed')}, 건너뜀 {list(results.values()).count('skipped')}\n",
             "| TC | 요구사항 | 테스트 | 통과 | 실패 | 건너뜀 | 판정 |", "|---|---|---|---|---|---|---|"]
    for tc, (req, prefixes) in TC.items():
        hits = [s for k, s in results.items() if any(k.startswith(p) for p in prefixes)]
        p, f, s = hits.count("passed"), hits.count("failed"), hits.count("skipped")
        verdict = "실패" if f else "미실행" if not p else "통과"
        lines.append(f"| {tc} | {req} | {len(hits)} | {p} | {f} | {s} | {verdict} |")
    lines += ["\n수용 기준 중 AC-005(미사용 사진 Top-5)·AC-008(사용자 5명)은 사람·데이터가 필요해 자동 테스트에 없다. "
              "평가 지표(조건 추출·P@5·환각·응답시간)는 `python -m eval.run_eval` → `docs/eval-report.md`."]
    Path("docs/test-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
