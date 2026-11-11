"""Streamlit UI (COMP-01, UI-01·02). api만 호출한다. 실행: streamlit run ui/app.py (테마는 .streamlit/config.toml)"""
import html
import json
import os
import re
import time
import uuid
from pathlib import Path

import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from export import ACCENT, WALK, course_pdf, day_label, hm, leg_text, link_of, schedule, share_text, summary_line
from streamlit_sortables import sort_items

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
API_URL = os.environ.get("API_URL", "http://localhost:8000")
KAKAO_JS_KEY = os.environ.get("KAKAO_JAVASCRIPT_KEY", "")  # 브라우저용 키(도메인 제한), REST 키는 쓰지 않는다
THEMES = ["선택 안 함", "바다", "자연", "역사·문화", "맛집", "쇼핑·시장", "핫플"]
MODES = {"auto": "자동 (가까우면 도보, 멀면 택시)", "walk": "도보만", "taxi": "택시만"}
MODE_CHIP = {"auto": "도보·택시 자동", "walk": "도보", "taxi": "택시"}
COMPANIONS = ["선택 안 함", "혼자", "친구", "연인", "가족", "부모님", "아이와"]
RAIN_THRESHOLD = 60  # config weather.rain_prob_threshold와 같게 (UI는 api만 호출하므로 표시용으로만 둔다)
EXAMPLES = ["이번 토요일 해운대에서 부모님과 걷기 좋은 바다 코스", "다음 주 금요일부터 1박 2일 중구 역사·문화 여행",
            "광안리 근처 비 오는 날 갈 만한 실내 장소"]
# 첫 화면 예시 카드: (아이콘, 테마, 질문, 결과 미리보기). 미리보기는 2026-11-11 실제 실행 결과(두 번 같음)
EXAMPLE_CARDS = [("waves", "바다", EXAMPLES[0], "6곳 · 약 7시간 · 도보·택시"),
                 ("account_balance", "역사·문화", EXAMPLES[1], "1박 2일 · 하루 6곳 · 도보 위주"),
                 ("umbrella", "비 오는 날", EXAMPLES[2], "실내 명소 5곳")]
# 버튼 → 구조화된 수정 요청(API-001 edit.kind). 문장 해석 없이 직전 코스를 제자리에서 고친다 (FR-012, edit_course)
EDITS = {"실내로 바꾸기": "indoor", "카페 추가": "cafe", "더 짧게": "shorter"}
MAP_H = 480
STAGES = {"parse": "조건 해석", "search": "장소 검색", "weather": "날씨 확인", "route": "동선 계산", "respond": "요약"}
RUNNING = {"parse": "조건을 읽는 중", "search": "장소를 찾는 중", "weather": "날씨를 확인하는 중", "route": "동선을 계산하는 중",
           "respond": "요약을 쓰는 중"}
# 2026-11-11 실측(/api/chat/stream, 코스 질문 7개): 3.7~12.4초, 중앙값 5초. LLM이 정상일 때 12초 안팎,
# 무료 등급 한도로 규칙 기반 대체가 되면 4~5초. 장소 검색만 하는 질문은 1초 안쪽.
TYPICAL_WAIT = "5~15초"
CSS = f"""<style>
[data-testid="stMainBlockContainer"] {{padding-top: 2.5rem; max-width: 1180px;}}
h1 {{font-size: 1.6rem !important; font-weight: 700 !important; letter-spacing: -0.02em; padding-bottom: 0 !important;}}
[data-testid="stSidebar"] h2 {{font-size: .78rem !important; font-weight: 600 !important; color: #6b6760;
  margin: 1.4rem 0 .2rem !important; padding: 0 !important;}}
[data-testid="stFileUploaderDropzoneInstructions"] {{display: none;}}
[data-testid="stMainBlockContainer"] .stButton button * {{white-space: normal !important; overflow: visible !important;}}
.leg {{color: #6b6760; font-size: .82rem; margin: .15rem 0 .15rem 1rem; padding: .1rem 0 .1rem .7rem;
  border-left: 2px dashed {WALK};}}
.leg.taxi {{border-left: 2px solid {ACCENT};}}
.chips {{display: flex; flex-wrap: wrap; gap: .35rem; margin: 0 0 .6rem;}}
.chip {{font-size: .8rem; padding: .15rem .6rem; border: 1px solid #e3e0d8; border-radius: 999px; background: #fff;}}
.chip b {{color: #6b6760; font-weight: 500; margin-right: .3rem;}}
.daysum {{font-size: .88rem; color: #3d3a35; margin: .2rem 0 .8rem;}}
.bm-active {{outline: 2px solid {ACCENT}; outline-offset: 2px; background: #eef4fb;}}
@media (max-width: 640px) {{  /* 휴대폰: 지도는 낮게, 카드 썸네일은 작게 */
  [data-testid="stMainBlockContainer"] iframe[title="st.iframe"] {{height: 340px !important;}}
  [data-testid="stMainBlockContainer"] {{padding-left: 1rem; padding-right: 1rem;}}
}}
.hero {{text-align: center; margin: 4vh 0 1.6rem;}}
.brand-lg {{font-size: 2rem; font-weight: 700; letter-spacing: -0.02em;}}
.brand {{font-size: 1.15rem; font-weight: 700; letter-spacing: -0.01em;}}
.slogan {{font-size: 1.25rem; margin: .4rem 0 .2rem; color: #1d1d1f;}}
.sub {{font-size: .9rem; color: #6b6760; margin: 0;}}
.st-key-start-course, .st-key-start-photo {{min-height: 260px;}}
[class*='st-key-ex-card'] {{min-height: 245px;}}
@media (max-width: 640px) {{  /* 휴대폰: 카드는 세로로 쌓이니 높이 맞춤을 풀고, 첫 화면 위 여백을 줄인다 */
  .st-key-start-course, .st-key-start-photo, [class*='st-key-ex-card'] {{min-height: 0;}}
  .hero {{margin-top: 1vh;}}
}}
.st-key-steps {{text-align: center; margin: .6rem 0 .2rem;}}
.badges {{display: flex; flex-wrap: wrap; justify-content: center; gap: .4rem; margin: 1.4rem 0 .4rem;}}
.badges span {{font-size: .75rem; color: #6b6760; border: 1px solid #e3e0d8; border-radius: 999px; padding: .1rem .6rem; background: #fff;}}
.trusts {{display: flex; flex-wrap: wrap; gap: .35rem; margin: 0 0 .6rem;}}
.trust {{font-size: .78rem; padding: .15rem .6rem; border-radius: 999px; border: 1px solid;}}
.trust.ok {{color: #1d5fa7; border-color: #b9cfe8; background: #f2f7fc;}}
.trust.warn {{color: #8a5a00; border-color: #ead9b0; background: #fdf8ec;}}
.est {{border-bottom: 1px dotted #8a857c; cursor: help;}}
.umb {{display: inline-block; font-size: .75rem; color: #1d5fa7; background: #f2f7fc;
  border: 1px solid #b9cfe8; border-radius: 999px; padding: .05rem .55rem; margin: .1rem 0 .2rem;}}
[class*="st-key-card"] [data-testid="stImage"] img {{aspect-ratio: 4 / 3; object-fit: cover; border-radius: 6px;}}
.ph {{aspect-ratio: 4 / 3; width: 100%; border-radius: 6px; background: #eeece7; color: #8a857c;
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: .15rem;}}
.ph .ico {{font-size: 1.6rem; line-height: 1; filter: grayscale(1); opacity: .7;}}
.ph .txt {{font-size: .7rem;}}
.footer {{color: #8a857c; font-size: .75rem; line-height: 1.6; border-top: 1px solid #e3e0d8;
  padding-top: .8rem; margin-top: 2rem;}}
</style>"""

st.set_page_config(page_title="BusanMate", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
ss = st.session_state
ss.setdefault("sid", uuid.uuid4().hex)
ss.setdefault("history", [])


def api(method: str, path: str, **kw) -> dict:
    try:
        r = requests.request(method, f"{API_URL}{path}", timeout=90, **kw)
        body = r.json()
    except (requests.RequestException, ValueError):
        return {"error": {"message": "서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요."}}
    return body


def _chunks(text: str):
    for part in re.split(r"(\s+)", text):
        yield part
        time.sleep(0.012)


def chat_stream(body: dict):
    """/api/chat/stream(NDJSON)의 이벤트를 차례로 돌려준다. 실패하면 {"error"} 하나."""
    try:
        with requests.post(f"{API_URL}/api/chat/stream", json=body, stream=True, timeout=120) as r:
            if r.status_code != 200:  # 400 되묻기·429 요청 한도는 JSON 오류 한 덩어리
                yield r.json()
                return
            for line in r.iter_lines():
                if line:
                    yield json.loads(line)
    except (requests.RequestException, ValueError):
        yield {"error": {"message": "서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요."}}


def ask(text: str, edit: str | None = None) -> None:
    """버튼 콜백: 다음 실행에서 이 문장을 질문으로 보낸다. edit가 있으면 코스 제자리 수정 요청으로 보낸다."""
    ss.queued, ss.queued_edit = text, edit


@st.cache_data(show_spinner=False)
def pdf_bytes(result_json: str) -> bytes:
    return course_pdf(json.loads(result_json))


def kakao_map(items: list[dict], keys: list[str]) -> str:
    """번호 마커와 구간 선(도보는 점선, 택시는 실선). 마커를 누르면 같은 번호 카드를 강조한다.
    장소명은 textContent/title로 넣어 HTML 주입을 막는다."""
    pts = json.dumps([{"lat": i["lat"], "lng": i["lng"], "n": i["order"], "name": i["name"], "card": k,
                       "walk": bool(i["leg_to_next"]) and i["leg_to_next"]["mode"] == "walk"}
                      for i, k in zip(items, keys)], ensure_ascii=False)
    return f"""
<style>html,body{{margin:0;height:100%}}</style>
<div id="map" style="width:100%;height:calc(100% - 10px);border-radius:6px;color:#8a857c;font:13px sans-serif"></div>
<script src="https://dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JS_KEY}&autoload=false"></script>
<script>
function focusCard(card) {{  // 카드는 부모 문서(Streamlit)에 있다: st.container(key=...) → .st-key-<key>
  try {{
    var doc = window.parent.document;
    doc.querySelectorAll('.bm-active').forEach(function (e) {{ e.classList.remove('bm-active'); }});
    var el = doc.querySelector('.st-key-' + card);
    if (el) {{ el.classList.add('bm-active'); el.scrollIntoView({{behavior: 'smooth', block: 'center'}}); }}
  }} catch (e) {{}}
}}
if (!window.kakao) {{  // 키 미설정·도메인 미등록이면 SDK가 401을 돌려준다
  document.getElementById('map').textContent =
    '지도를 불러오지 못했어요. 카카오 개발자 콘솔에 이 주소(' + location.ancestorOrigins[0] + ')가 웹 도메인으로 등록돼 있는지 확인하세요.';
}} else kakao.maps.load(function () {{
  var pts = {pts};
  var map = new kakao.maps.Map(document.getElementById('map'), {{center: new kakao.maps.LatLng(pts[0].lat, pts[0].lng), level: 6}});
  var bounds = new kakao.maps.LatLngBounds();
  pts.forEach(function (p, i) {{
    var pos = new kakao.maps.LatLng(p.lat, p.lng);
    bounds.extend(pos);
    var pin = document.createElement('div');
    pin.textContent = p.n;
    pin.title = p.n + '. ' + p.name;
    pin.style.cssText = 'width:26px;height:26px;border-radius:50%;background:{ACCENT};color:#fff;border:2px solid #fff;' +
      'box-shadow:0 1px 3px rgba(0,0,0,.35);font:600 13px/26px sans-serif;text-align:center;cursor:pointer;';
    pin.onclick = function () {{ focusCard(p.card); }};
    new kakao.maps.CustomOverlay({{map: map, position: pos, content: pin, zIndex: pts.length - i}});
    if (i + 1 < pts.length) {{
      new kakao.maps.Polyline({{map: map, path: [pos, new kakao.maps.LatLng(pts[i + 1].lat, pts[i + 1].lng)],
        strokeWeight: 4, strokeColor: p.walk ? '{WALK}' : '{ACCENT}', strokeOpacity: 0.85,
        strokeStyle: p.walk ? 'shortdash' : 'solid'}});
    }}
  }});
  // Streamlit 탭은 보이지 않는 탭을 크기 0으로 그린다. 그 안에서 만든 지도는 크기를 잘못 잡으므로, 지도 칸의 크기가
  // 바뀔 때(탭이 보일 때, 화면 폭이 바뀔 때)마다 relayout(Leaflet의 invalidateSize에 해당) 뒤 범위를 다시 맞춘다
  function fit() {{ map.relayout(); map.setBounds(bounds, 40, 40, 40, 40); }}
  window.bmMap = map; window.bmPts = pts;  // 화면 점검용(모든 장소가 지도 범위 안에 있는지)
  var el = document.getElementById('map'), lastSize = '';
  function refit() {{  // 폭·높이 중 하나라도 바뀌면(탭 표시, 휴대폰 회전, 모바일 높이) 다시 맞춘다
    var size = el.offsetWidth + 'x' + el.offsetHeight;
    if (el.offsetWidth > 0 && el.offsetHeight > 0 && size !== lastSize) {{ lastSize = size; fit(); }}
    return el.offsetWidth > 0;
  }}
  refit();
  new ResizeObserver(refit).observe(el);
  // ResizeObserver 알림은 화면을 그릴 때만 오므로(백그라운드 탭 등) 처음 크기가 생길 때까지는 타이머로도 확인한다
  var poll = setInterval(function () {{ if (refit()) clearInterval(poll); }}, 300);
}});
</script>"""


def show_map(items: list[dict], keys: list[str]) -> None:
    if KAKAO_JS_KEY:
        components.html(kakao_map(items, keys), height=MAP_H)
    else:
        st.map([{"lat": i["lat"], "lon": i["lng"]} for i in items])


def link_md(s: dict | None) -> str:
    """원본 링크가 있으면 그것을, 없으면 '원본 링크 없음'과 공공데이터포털 서비스 페이지를 대체로 링크한다 (FR-003)."""
    found = link_of(s)
    if found and found[0] == "원본 링크":
        return f"[원본 링크]({found[1]})"
    return "원본 링크 없음" + (f" · [공공데이터포털 서비스 페이지]({found[1]})" if found else "")


def source_line(s: dict | None) -> str:
    """모든 출처 줄의 형식: 출처: 제공처 · 원본 링크(없으면 '원본 링크 없음' · 포털 페이지) · 이용 조건."""
    if not s:
        return "출처: 정보 없음 · 원본 링크 없음"
    return f"출처: {s['label']} · {link_md(s)} · {s.get('license') or '이용 조건 정보 없음'}"


def photo_line(s: dict, image: dict) -> str:
    """사진 출처 줄도 같은 형식: 사진 출처: 제공처 · 원본 이미지 링크 · 이용 조건."""
    return f"사진 출처: {s['label']} · [원본 이미지]({image['url']}) · {image.get('license') or '이용 조건 정보 없음'}"


# 날씨가 코스에 영향을 준 알림(펼쳐서 보임): "강수확률 60%라 실내 장소를 우선", "비 예보 … 야외 장소 N곳(우산)"
WEATHER_NOTICE = re.compile(r"강수확률|비 예보|우산")

# 사진 없는 카드의 자리표시 아이콘 (분류별)
CATEGORY_ICON = {"해변": "🏖️", "공원·자연": "🌳", "문화시설": "🏛️", "관광지": "📍", "카페": "☕", "음식점": "🍽️",
                 "시장": "🛍️", "거리·마을": "🏘️", "사찰·종교": "🛕"}


def place_card(key: str, title: str, sub: str, reason: str | None, source: dict | None,
               why: list[str] | None = None, edit: tuple | None = None, umbrella: bool = False,
               category: str | None = None) -> None:
    """장소 카드: 이름, 시각·분류, 추천 이유, 왜 이곳?(요청 조건과 맞은 항목), 썸네일, 출처·원본 링크·이용 조건 (FR-003).
    edit=(r, day, index)이면 빼기·다른 곳으로 교체 버튼을 붙인다(채팅을 거치지 않는 직접 편집)."""
    with st.container(border=True, key=key):
        image = (source or {}).get("image")
        thumb, body = st.columns([1, 3], vertical_alignment="top")  # 사진이 없어도 같은 자리·높이(4:3)
        if image:
            thumb.image(image["url"], use_container_width=True)
        else:
            icon = CATEGORY_ICON.get(category or "", "📍")
            thumb.markdown(f"<div class='ph' role='img' aria-label='사진 없음'><span class='ico'>{icon}</span>"
                           "<span class='txt'>사진 없음</span></div>", unsafe_allow_html=True)
        body.markdown(f"**{title}**  \n:gray[{sub}]")
        if reason:
            body.write(reason)
        if umbrella:  # 비 오는 날 불가피하게 넣은 야외 장소
            st.markdown("<span class='umb'>비 올 때는 우산 필요</span>", unsafe_allow_html=True)
        if why:
            st.caption(f"왜 이곳? {' · '.join(why)}")
        st.caption(source_line(source))
        if source and image:
            st.caption(photo_line(source, image))
        if (source or {}).get("original"):
            with st.expander("원문 보기"):  # 공공데이터 원문 소개를 고치지 않고 그대로
                st.text(source["original"])
        if edit:
            r, day, index = edit
            a, b = st.columns(2)
            a.button("빼기", key=f"rm-{key}", icon=":material/close:", use_container_width=True,
                     on_click=remove_place, args=(r, day, index))
            b.button("다른 곳으로 교체", key=f"alt-{key}", icon=":material/swap_horiz:", use_container_width=True,
                     on_click=load_alternatives, args=(key, day, index))
            alts = ss.get(f"alts-{key}")
            if alts:
                if "error" in alts:
                    st.caption(alts["error"]["message"])
                for n, alt in enumerate(alts.get("alternatives") or []):
                    st.button(f"{alt['name']} · {alt['gu']} · 동선 +{alt['extra_km']}km", key=f"pick-{key}-{n}",
                              help=alt["intro"], use_container_width=True,
                              on_click=replace_place, args=(r, day, index, alt["place_id"]))
                if alts.get("alternatives") == []:
                    st.caption(f"근처(우회 {alts.get('max_detour_km', 8)}km 안)에 같은 분류의 다른 후보가 없어요. "
                               "빼기를 눌러 보세요.")


def _apply(r: dict, days: list[list[int]]) -> None:
    """직접 편집 결과(날짜별 place_id 순서)를 서버에 보내고, 이동 시간을 다시 계산한 코스로 화면 결과를 바꾼다."""
    res = api("PUT", f"/api/session/{ss.sid}/course", json={"days": days})
    if "error" in res:
        ss.edit_error = res["error"]["message"]
        return
    keep = [x for x in r.get("sources") or [] if "place_id" not in x and not x["label"].startswith("기상청")]
    r["course"], r["sources"] = res["course"], res["sources"] + keep
    r["notices"] = [n for n in r.get("notices") or [] if "찾지 못해" in n] + res.get("notices", [])
    for k in [k for k in ss if str(k).startswith(("pdf-", "alts-", "share-"))]:
        del ss[k]


def _ids(r: dict) -> list[list[int]]:
    return [[i["place_id"] for i in d["items"]] for d in r["course"]["days"]]


def remove_place(r: dict, day: int, index: int) -> None:
    days = _ids(r)
    if len(days[day - 1]) <= 1:
        ss.edit_error = "하루에 한 곳은 남겨 두어야 해요."
        return
    days[day - 1].pop(index)
    _apply(r, days)


def load_alternatives(key: str, day: int, index: int) -> None:
    ss[f"alts-{key}"] = api("GET", f"/api/session/{ss.sid}/alternatives", params={"day": day, "index": index})


def replace_place(r: dict, day: int, index: int, place_id: int) -> None:
    days = _ids(r)
    days[day - 1][index] = place_id
    _apply(r, days)


def reorder(r: dict, day: int, labels: list[str], order: list[str]) -> None:
    days = _ids(r)
    days[day - 1] = [days[day - 1][labels.index(x)] for x in order]
    _apply(r, days)


def trust_badges(r: dict) -> None:
    """근거 있는 코스: 출처 확인 배지, 없는 장소 안내를 배지 옆에도."""
    src = (r.get("course") or {}).get("sourced")
    spans = []
    if src:
        ok = src["count"] == src["total"]
        text = f"공공데이터 장소 {src['total']}곳 · " + ("모두 출처 확인됨" if ok else f"{src['count']}곳 출처 확인")
        spans.append(f"<span class='trust {'ok' if ok else 'warn'}'>{html.escape(text)}</span>")
    spans += [f"<span class='trust warn'>{html.escape(n)}</span>" for n in r.get("notices") or [] if "찾지 못해" in n]
    if spans:
        st.markdown(f"<div class='trusts'>{''.join(spans)}</div>", unsafe_allow_html=True)


def budget_box(course: dict) -> None:
    """예상 비용(식비 1인 + 택시비 1대)과 사이드바 예산 비교."""
    b = course.get("budget")
    if not b:
        return
    est = " · 일부 추정" if b.get("estimated") else ""
    st.markdown(f"**예상 비용** 식비 {b['meals']:,}원(1인) · 택시비 {b['taxi']:,}원(1대) · 합계 **{b['total']:,}원**"
                f"<span style='color:#8a857c'>{est}</span>", unsafe_allow_html=True,
                help="식비는 공공데이터 맛집 설명의 대표 메뉴 가격(없으면 식당 12,000원·카페 6,000원), 택시비는 카카오 실측 "
                     "요금(없으면 부산 택시 요금표: 기본 4,800원·2km, 132m당 100원)으로 계산해요.")
    if len(b["days"]) > 1:
        st.caption(" / ".join(f"{d['day']}일차 {d['total']:,}원" for d in b["days"]))
    if b.get("taxi_hint_legs"):
        st.caption(f"택시 추천 구간 {b['taxi_hint_legs']}곳까지 택시로 가면 택시비가 도보 위주보다 약 "
                   f"{b['taxi_hint_extra']:,}원 늘어요(택시비 합계 약 {b['taxi'] + b['taxi_hint_extra']:,}원).")
    budget = ss.get("budget") or 0
    if budget and b["total"] > budget:
        st.warning(f"예산 {budget:,}원보다 {b['total'] - budget:,}원 많아요. '더 짧게'를 누르거나 택시 구간을 줄여 보세요.")
    elif budget:
        st.success(f"예산 {budget:,}원 안이에요. {budget - b['total']:,}원이 남아요.")


def chips(r: dict) -> None:
    """AI가 해석한 조건(날짜·지역·동행·이동수단·테마)을 칩으로 (FR-001, 해석이 맞는지 사용자가 바로 확인)."""
    c = r.get("conditions")
    if not c or not (r.get("course") or r.get("places")):  # 수정 응답도 직전 코스의 조건을 그대로 돌려준다
        return
    dates = r.get("dates") or []
    items = []
    if dates:
        items.append(("날짜", day_label(dates, 1) + (f" ~ {day_label(dates, len(dates))}" if len(dates) > 1 else "")))
    items.append(("지역", ", ".join(c.get("gu") or []) or "부산 전체"))
    if c.get("companion"):
        items.append(("동행", c["companion"]))
    items.append(("이동", MODE_CHIP.get(c.get("mode"), c.get("mode") or "")))
    if c.get("theme"):
        items.append(("테마", c["theme"]))
    if c.get("indoor") is not None:
        items.append(("장소", "실내" if c["indoor"] else "실외"))
    spans = "".join(f"<span class='chip'><b>{k}</b>{html.escape(str(v))}</span>" for k, v in items)
    st.markdown(f"<div class='chips'>{spans}</div>", unsafe_allow_html=True)


def show_photo_result(r: dict, key: str) -> None:
    """API-002 결과: 인식된 장소 또는 비슷한 분위기의 장소, 지도, 출처, 코스에 넣기."""
    if "error" in r:
        st.error(r["error"]["message"])
        return
    st.markdown("**사진 속 장소로 보이는 곳**" if r["mode"] == "recognized"
                else "**딱 맞는 명소는 없어서, 분위기가 비슷한 곳을 골랐어요.**")
    if r.get("description"):
        st.caption(f"사진 설명: {r['description']}")
    for n in r.get("notices") or []:
        st.caption(n)
    if not r["matches"]:
        return
    by_place = {s["place_id"]: s for s in r.get("sources") or [] if "place_id" in s}
    items = [{**m, "order": i + 1, "leg_to_next": None} for i, m in enumerate(r["matches"])]
    keys = [f"card-{key}-p-{i['order']}" for i in items]
    left, right = st.columns([3, 2])
    with left:
        show_map(items, keys)
    with right:
        for m, k in zip(items, keys):
            place_card(k, f"{m['order']}. {m['name']}", f"{m['category']} · {m['gu']} · 유사도 {m['similarity']:.0%}",
                       None, by_place.get(m["place_id"]), category=m["category"])
            st.button("코스에 넣기", key=f"add-{k}", on_click=ask, args=(f"{m['name']}을(를) 코스에 추가해줘",))


def show_result(r: dict, key: str, latest: bool = False, stream: bool = False, readonly: bool = False) -> None:
    if "error" in r:
        st.error(r["error"]["message"])
        return
    chips(r)
    trust_badges(r)
    if latest and (err := ss.pop("edit_error", None)):
        st.error(err)
    notices = [x for x in r.get("notices") or [] if "찾지 못해" not in x]  # 없는 장소 안내는 배지에서 한 번만
    for n in [x for x in notices if WEATHER_NOTICE.search(x)]:  # 날씨가 코스를 바꾼 알림만 펼쳐 둔다
        st.info(n, icon=":material/umbrella:")
    rest = [x for x in notices if not WEATHER_NOTICE.search(x)]
    if rest:  # 예보 범위 밖·도보 예상치 같은 참고 알림은 하나로 접어 둔다
        with st.expander(f"알림 {len(rest)}개", icon=":material/info:"):
            for n in rest:
                st.caption(n)
    if stream:  # 새 답변은 문장이 흘러나오듯 (verify_places를 거친 뒤의 문장이다)
        st.write_stream(_chunks(r["answer"]))
    else:
        st.markdown(r["answer"])
    if latest and r.get("intent") in ("outside", "chitchat"):  # 범위 밖: 부산에서 찾기 + 예시 질문
        if r.get("suggest"):
            st.button(f"부산에서 찾기: {r['suggest']}", key=f"sg-{key}", on_click=ask, args=(r["suggest"],), type="primary")
        st.caption("이렇게 물어보세요")
        for col, (n, ex) in zip(st.columns(len(EXAMPLES)), enumerate(EXAMPLES)):
            col.button(ex, key=f"ex-{key}-{n}", on_click=ask, args=(ex,), use_container_width=True)
    by_place = {s["place_id"]: s for s in r.get("sources") or [] if "place_id" in s}
    course = r.get("course")
    editable = latest and not readonly
    basis = (course or {}).get("estimate_basis") or "직선거리 기반 추정치예요."
    if course and course.get("days"):
        def rain(p):
            return "" if p is None else f" · {'비' if p >= RAIN_THRESHOLD else '강수'} {p:.0f}%"
        tabs = st.tabs([f"{d['day']}일차{rain(d.get('rain_prob'))}" for d in course["days"]])
        for tab, d in zip(tabs, course["days"]):
            with tab:
                s = schedule(d)
                when = day_label(r.get("dates") or [], d["day"])
                st.markdown(f"<div class='daysum'>{when + ' · ' if when else ''}{summary_line(s)} "
                            f"<span style='color:#8a857c'>· 시각은 예상</span></div>", unsafe_allow_html=True)
                keys = [f"card-{key}-{d['day']}-{i['order']}" for i in d["items"]]
                left, right = st.columns([3, 2])
                with left:
                    show_map(d["items"], keys)
                with right:
                    for n, (i, k, (arrive, leave)) in enumerate(zip(d["items"], keys, s["times"])):
                        won = i.get("price_won")
                        price = f" · 1인 {'약 ' if i.get('price_estimated') else ''}{won:,}원" if won else ""
                        place_card(k, f"{i['order']}. {i['name']}",
                                   f"{arrive}–{leave} · 체류 {hm(i.get('stay_min') or 60)} · {i['category']}{price}",
                                   i.get("reason"), by_place.get(i["place_id"]), i.get("why"),
                                   (r, d["day"], n) if editable else None, bool(i.get("umbrella")), i["category"])
                        if leg := i.get("leg_to_next"):  # 지도 선과 같은 모양: 도보 점선, 택시 실선
                            tip = html.escape(basis, quote=True)
                            text = html.escape(leg_text(leg)).replace("(예상)", f"<span class='est' title='{tip}'>(예상)</span>")
                            if leg.get("rain_taxi"):  # 비 오는 날 야외 장소 앞뒤 구간
                                text += f" · 비 예보가 있어 택시 추천(약 {leg['taxi_hint_min']}분)"
                            elif leg.get("taxi_hint_min"):  # 걸어서 10분 이상인 구간
                                text += f" · 택시를 타면 약 {leg['taxi_hint_min']}분"
                            st.markdown(f"<div class='leg {leg['mode']}'>{text}</div>", unsafe_allow_html=True)
                if editable and len(d["items"]) > 1:  # 드래그로 순서 바꾸기 → 이동 시간·시각 바로 다시 계산
                    with st.expander("드래그해서 순서 바꾸기"):
                        labels = [f"{i['order']}. {i['name']}" for i in d["items"]]
                        order = sort_items(labels, direction="vertical", key=f"sort-{key}-{d['day']}-{'|'.join(labels)}")
                        if order != labels:
                            reorder(r, d["day"], labels, order)
                            st.rerun()
    for n, p in enumerate(r.get("places") or []):
        place_card(f"card-{key}-s-{n}", p["name"], f"{p['category']} · {p['gu']}", p.get("reason"),
                   by_place.get(p["place_id"]), category=p["category"])
    others = [s for s in r.get("sources") or [] if "place_id" not in s]  # 기상청·축제 등
    for s in others:
        st.caption(source_line(s))
    if course and course.get("days"):
        budget_box(course)
        export_tools(r, key, readonly)
        if editable:
            st.caption("코스 고치기")
            for col, (label, kind) in zip(st.columns(len(EDITS)), EDITS.items()):
                col.button(label, key=f"edit-{key}-{label}", on_click=ask, args=(label, kind), use_container_width=True)


def share_url(share_id: str) -> str:
    base = getattr(st.context, "url", None) or "http://localhost:8501"
    return f"{base.split('?')[0].rstrip('/')}/?course={share_id}"


def save_course(r: dict, key: str, keep: bool) -> None:
    """서버가 세션의 코스를 저장하고 id를 준다. keep이면 '저장한 코스' 목록(이 브라우저 세션)에도 넣는다."""
    res = api("POST", f"/api/session/{ss.sid}/share")
    if "error" in res:
        ss.edit_error = res["error"]["message"]
        return
    ss[f"share-{key}"] = res["share_id"]
    if keep:
        days = r["course"]["days"]
        n = sum(len(d["items"]) for d in days)
        title = f"{day_label(r.get('dates') or [], 1) or '코스'} · {days[0]['items'][0]['name']} 외 {n - 1}곳"
        ss.setdefault("saved", []).insert(0, (title, res["share_id"]))


def export_tools(r: dict, key: str, readonly: bool = False) -> None:
    """PDF 내보내기(만들기 → 내려받기, 지도 타일을 매 화면마다 받지 않도록), 공유용 요약 복사, 공유 링크·저장."""
    if not readonly:
        a, b = st.columns(2)
        a.button("공유 링크 만들기", key=f"sh-{key}", icon=":material/link:", use_container_width=True,
                 on_click=save_course, args=(r, key, False))
        b.button("코스 저장", key=f"sv-{key}", icon=":material/bookmark:", use_container_width=True,
                 on_click=save_course, args=(r, key, True))
        if sid := ss.get(f"share-{key}"):
            st.caption("링크를 연 사람은 읽기 전용으로 볼 수 있어요. 오른쪽 위 아이콘으로 복사하세요.")
            st.code(share_url(sid), language=None)
    left, right = st.columns(2)
    with left:
        if ss.get(f"pdf-{key}"):
            with st.spinner("PDF 만드는 중…"):
                data = pdf_bytes(json.dumps(r, ensure_ascii=False, sort_keys=True))
            st.download_button("PDF 내려받기", data, file_name="busanmate-course.pdf", mime="application/pdf",
                               key=f"dl-{key}", type="primary", use_container_width=True)
        else:
            st.button("PDF로 내보내기", key=f"mk-{key}", on_click=lambda: ss.update({f"pdf-{key}": True}),
                      use_container_width=True)
    with right, st.popover("공유용 요약 복사", use_container_width=True):
        st.caption("오른쪽 위 복사 아이콘을 누르면 복사됩니다.")
        st.code(share_text(r), language=None, wrap_lines=True)


def photo_uploader(key: str) -> None:
    photo = st.file_uploader("JPG·PNG, 10MB까지", type=["jpg", "jpeg", "png"], key=key)
    if photo and st.button("비슷한 곳 찾기", key=f"{key}-go", use_container_width=True, type="primary"):
        ss.queued_photo = (photo.name, photo.getvalue(), photo.type)  # 브라우저 세션 메모리에만, 저장하지 않는다


def load_saved(share_id: str) -> None:
    """저장한 코스를 이 세션의 직전 코스로 불러와 대화에 보여 준다(불러온 뒤에도 편집할 수 있다)."""
    res = api("POST", f"/api/session/{ss.sid}/load/{share_id}")
    if "error" in res:
        ss.edit_error = res["error"]["message"]
        return
    ss.history += [{"role": "user", "content": "저장한 코스 불러오기"},
                   {"role": "assistant", "result": {**res, "answer": "저장한 코스를 불러왔어요.", "intent": "course",
                                                    "notices": []}}]


def new_course() -> None:
    """대화를 비우고 새 세션으로 시작한다 (서버의 직전 코스·되묻기 상태도 새 세션이라 이어지지 않는다)."""
    ss.history, ss.sid, ss.prefs = [], uuid.uuid4().hex, None


def hero() -> None:
    """첫 화면: 무엇을 하는 서비스인지 5초 안에 — 슬로건, 두 가지 시작 방법, 3단계, 예시, 샘플 결과, 출처 배지."""
    st.markdown("<div class='hero'><div class='brand-lg'>BusanMate</div>"
                "<p class='slogan'>말만 하면 부산 하루 코스가 완성돼요</p>"
                "<p class='sub'>공공데이터 기반 · 날씨 반영 · 출처 표시</p></div>", unsafe_allow_html=True)
    a, b = st.columns(2)
    with a, st.container(border=True, key="start-course"):
        st.markdown("#### :material/route: 코스 짜기")
        st.caption("날짜·동네·누구와를 적으면 지도와 시간표가 있는 하루 동선을 짜 줘요.")
        st.markdown(":gray[예) 이번 토요일 해운대, 부모님과 걷기 좋은 바다 코스]")
        st.button("질문 입력하기", on_click=lambda: ss.update(focus_input=True), type="primary",
                  use_container_width=True, icon=":material/edit:")
    with b, st.container(border=True, key="start-photo"):
        st.markdown("#### :material/photo_camera: 사진으로 장소 찾기")
        st.caption("부산 명소 사진을 올리면 어디인지, 분위기가 비슷한 곳은 어디인지 찾아요.")
        photo_uploader("photo_main")
    with st.container(key="steps"):
        st.markdown(":material/edit_note: **1** 날짜·동네·누구와 입력 &nbsp;→&nbsp; :material/partly_cloudy_day: **2** 날씨와 동선 반영"
                    " &nbsp;→&nbsp; :material/map: **3** 지도·타임라인·PDF")
    st.caption("이렇게 물어보세요")
    for col, (n, (icon, theme_, q, preview)) in zip(st.columns(3), enumerate(EXAMPLE_CARDS)):
        with col, st.container(border=True, key=f"ex-card-{n}"):
            st.markdown(f":material/{icon}: **{theme_}**")
            st.markdown(q)
            st.caption(preview)
            st.button("이 질문으로 시작", key=f"ex-go-{n}", on_click=ask, args=(q,), use_container_width=True)
    with st.expander("이런 결과가 나와요"):
        st.image(str(Path(__file__).parent / "assets" / "sample-course.jpg"),
                 caption="해운대 바다 코스 결과 화면 (조건 칩, 시간표, 번호 지도, 장소 카드와 출처)")
    with st.expander("더 알아보기"):
        st.markdown("장소는 부산시 공공데이터(명소·맛집·축제)에 있는 곳만 쓰고, 카드마다 출처와 원본 링크를 답니다. "
                    "비 예보가 있는 날은 실내 장소에 점수를 더 주고, 식당은 점심 시간에 맞춰 넣어요. 짠 뒤에는 "
                    "\"카페 추가\", \"더 짧게\"처럼 버튼이나 말로 고칠 수 있고, PDF로 내보낼 수 있어요.")
    st.markdown("<div class='badges'><span>공공데이터포털 부산시 제공</span><span>기상청 예보 반영</span>"
                "<span>모든 장소에 출처·원본 링크</span></div>", unsafe_allow_html=True)


# 입력은 사이드바보다 먼저 읽는다(첫 질문을 처리하는 실행에서도 대화 중 화면이 되게). 입력창은 하단에 고정된다
typed = st.chat_input("예) 이번 토요일 해운대, 부모님과 걷기 좋은 바다 코스")
message, edit_kind = typed or ss.pop("queued", None), ss.pop("queued_edit", None)
edit_kind = None if typed else edit_kind  # 직접 입력한 질문이면 버튼 수정 요청은 버린다
busy = bool(message or ss.get("queued_photo"))

# --- 사이드바: 선호 설정(FR-005), 사진, 관리자 ---
with st.sidebar:
    st.header("여행 조건")
    theme = st.selectbox("테마", THEMES)
    st.caption("고르면 그 테마의 장소가 추천에 우선 반영돼요.")
    mode = st.radio("이동", list(MODES), format_func=MODES.get)
    st.caption("도보만 골라도 걸어서 30분이 넘는 구간은 택시로 계산해요.")
    companion = st.selectbox("누구와", COMPANIONS)
    st.caption("조건과 추천 설명에 참고해요. 장소 점수에는 아직 쓰지 않아요.")
    prefs = {"theme": None if theme == THEMES[0] else theme, "mode": mode,
             "companion": None if companion == COMPANIONS[0] else companion}
    if ss.get("prefs") != prefs:
        api("PUT", f"/api/session/{ss.sid}/preferences", json=prefs)
        ss.prefs = prefs
    st.caption("질문에 쓴 조건이 있으면 그쪽을 먼저 따릅니다.")
    st.number_input("1인 예산(원)", min_value=0, step=10000, key="budget",
                    help="식비(1인)와 택시비(1대) 합계가 이보다 많으면 코스 아래에 알려 줘요. 0이면 비교하지 않아요.")
    st.caption("코스 아래 예상 비용과 비교해요.")
    if ss.get("saved"):
        st.header("저장한 코스")
        st.caption("이 브라우저 탭을 닫으면 목록이 사라져요. 공유 링크는 계속 열 수 있어요.")
        for n, (title, share_id) in enumerate(ss.saved):
            st.button(title, key=f"load-{n}-{share_id}", use_container_width=True, on_click=load_saved, args=(share_id,))

    if ss.history or busy:  # 첫 화면에서는 본문 카드에 업로더가 있고, 대화가 시작되면 사이드바로 옮긴다
        st.header("사진으로 찾기")
        photo_uploader("photo_side")

    if "admin" in st.query_params:  # 관리자 화면은 ?admin 으로 들어올 때만 보인다 (API는 토큰으로 따로 막는다)
        st.header("관리자")
        token = st.text_input("관리자 토큰", type="password")
        if st.button("데이터 수집", disabled=not token):
            r = api("POST", "/api/admin/ingest", headers={"X-Admin-Token": token}, json={})
            ss.run_id = r.get("run_id")
            st.write(r.get("error", {}).get("message") or f"수집 시작 · {ss.run_id}")
        if ss.get("run_id") and st.button("진행 상황", disabled=not token):
            st.json(api("GET", f"/api/admin/ingest/{ss.run_id}", headers={"X-Admin-Token": token}))
        with st.expander("디버그 · LLM 대체 횟수"):  # 서버 시작 이후 단계별 호출·대체·실패 원인 (키 없음)
            st.json(api("GET", "/api/health").get("llm") or {})

# --- 본문: 대화 (UI-01, UI-02) ---
shared = st.query_params.get("course")
if shared and busy:  # 공유 화면에서 질문을 하면 일반 화면으로
    st.query_params.clear()
elif shared:  # 공유 링크: 읽기 전용 (편집·저장 버튼 없음, PDF·요약 복사는 그대로)
    payload = api("GET", f"/api/course/{shared}")
    st.markdown("<div class='brand'>BusanMate</div>", unsafe_allow_html=True)
    st.caption("공유된 코스예요. 읽기 전용이라 고칠 수 없어요.")
    if "error" in payload:
        st.error(payload["error"]["message"])
    else:
        show_result({**payload, "answer": "", "intent": "course", "notices": []}, key="shared", readonly=True)
    st.button("나도 코스 짜기", type="primary", on_click=st.query_params.clear)
    st.stop()
if ss.history or busy:  # 대화 중: 히어로는 숨기고 작은 제목과 "새 코스"만
    left, right = st.columns([5, 1], vertical_alignment="center")
    left.markdown("<div class='brand'>BusanMate</div>", unsafe_allow_html=True)
    right.button("새 코스", on_click=new_course, icon=":material/add:", use_container_width=True)
else:
    hero()
if ss.pop("focus_input", False):  # "질문 입력하기": 하단 입력창에 커서를 둔다
    components.html("<script>const t = window.parent.document.querySelector('[data-testid=\"stChatInputTextArea\"]');"
                    "if (t) t.focus();</script>", height=0)

for n, h in enumerate(ss.history):
    with st.chat_message(h["role"]):
        if h["role"] == "user":
            st.markdown(h["content"])
        elif h.get("photo"):
            show_photo_result(h["result"], key=f"h{n}")
        else:
            show_result(h["result"], key=f"h{n}", latest=n == len(ss.history) - 1 and not busy)

if ss.get("queued_photo"):  # 사진 탐색 (UC-003)
    name, data, mime = ss.pop("queued_photo")
    with st.chat_message("user"):
        st.markdown(f"사진으로 찾기 · {name}")
    with st.chat_message("assistant"):
        with st.spinner("비슷한 장소를 찾는 중…"):
            result = api("POST", "/api/search/image", data={"session_id": ss.sid}, files={"file": (name, data, mime)})
        show_photo_result(result, key=f"h{len(ss.history) + 1}")
    ss.history += [{"role": "user", "content": f"사진으로 찾기 · {name}"},
                   {"role": "assistant", "result": result, "photo": True}]

if message:  # 질문은 바로 보이고, 답은 기다리는 동안 스피너
    with st.chat_message("user"):
        st.markdown(message)
    with st.chat_message("assistant"):
        body = {"session_id": ss.sid, "message": message} | ({"edit": {"kind": edit_kind}} if edit_kind else {})
        status = st.status("코스를 고치는 중…" if edit_kind else f"질문을 읽는 중… (보통 {TYPICAL_WAIT})", expanded=True)
        result, total, current, started, last_t = None, None, None, {}, 0.0
        for ev in chat_stream(body):  # 단계는 서버의 실제 진행(해석 → 도구 호출 → 요약)에서 온다
            if "result" in ev or "error" in ev:
                result, total = ev.get("result", ev), ev.get("t")
                break
            stage, t = ev["stage"], ev["t"]
            if stage != current:
                if current:
                    status.write(f"{STAGES[current]} 완료 · {last_t - started[current]:.1f}초")
                started[stage] = last_t if stage == "respond" else t  # 요약은 끝 이벤트만 온다
                current = stage
                status.update(label=f"{RUNNING[stage]}…")
            if stage == "route" and ev["phase"] == "done":  # 동선 계산 다음은 요약(요약은 끝날 때만 이벤트가 온다)
                status.update(label=f"{RUNNING['respond']}…")
            last_t = t
        if current:
            status.write(f"{STAGES[current]} 완료 · {last_t - started[current]:.1f}초")
        failed = "error" in (result or {"error": 1})
        status.update(label="문제가 생겼어요" if failed else f"완료 · {total:.1f}초", state="error" if failed else "complete",
                      expanded=False)
        show_result(result or {"error": {"message": "응답을 받지 못했어요."}}, key=f"h{len(ss.history) + 1}",
                    latest=True, stream=True)
    ss.history += [{"role": "user", "content": message}, {"role": "assistant", "result": result}]

st.markdown("<div class='footer'>데이터: 공공데이터포털 부산광역시 부산명소·부산맛집·부산축제정보 서비스(이용허락범위 제한 없음) · "
            "기상청 단기예보(공공누리 제1유형) · 카카오모빌리티 길찾기, 카카오맵 · PDF 지도 © OpenStreetMap contributors.<br>"
            "이동시간에 '예상'이 붙은 값과 방문 시각은 추정치입니다.</div>", unsafe_allow_html=True)
