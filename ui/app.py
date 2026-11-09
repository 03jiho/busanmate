"""Streamlit UI (COMP-01, UI-01·02). api만 호출한다. 실행: streamlit run ui/app.py (테마는 .streamlit/config.toml)"""
import html
import json
import os
import uuid
from pathlib import Path

import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from export import ACCENT, WALK, course_pdf, day_label, hm, leg_text, schedule, share_text, source_url, summary_line

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
EDITS = {"실내로 바꾸기": "코스에서 실외 장소 하나를 실내 장소로 바꿔줘", "카페 추가": "코스에 카페 하나 추가해줘",
         "더 짧게": "코스에서 장소 하나를 빼서 더 짧게 해줘"}  # 버튼 → 코스 수정 요청 (FR-007, edit_course)
MAP_H = 480
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
.intro {{max-width: 640px; color: #3d3a35; line-height: 1.7; margin: 1rem 0 1.2rem;}}
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


def ask(text: str) -> None:
    """버튼 콜백: 다음 실행에서 이 문장을 질문으로 보낸다."""
    ss.queued = text


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
<div id="map" style="width:100%;height:{MAP_H - 10}px;border-radius:6px;color:#8a857c;font:13px sans-serif"></div>
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
  map.setBounds(bounds, 40, 40, 40, 40);
  // iframe 크기가 늦게 정해지면(탭 전환 등) 범위가 어긋나므로 한 번 더 맞춘다
  setTimeout(function () {{ map.relayout(); map.setBounds(bounds, 40, 40, 40, 40); }}, 400);
}});
</script>"""


def show_map(items: list[dict], keys: list[str]) -> None:
    if KAKAO_JS_KEY:
        components.html(kakao_map(items, keys), height=MAP_H)
    else:
        st.map([{"lat": i["lat"], "lon": i["lng"]} for i in items])


def place_card(key: str, title: str, sub: str, reason: str | None, source: dict | None) -> None:
    """장소 카드: 이름, 시각·분류, 추천 이유, 썸네일(이용 조건 확인된 공공데이터 이미지), 출처·원본 링크·이용 조건 (FR-003)."""
    with st.container(border=True, key=key):
        image = (source or {}).get("image")
        body = st
        if image:
            thumb, body = st.columns([1, 3], vertical_alignment="top")
            thumb.image(image["url"], use_container_width=True)
        body.markdown(f"**{title}**  \n:gray[{sub}]")
        if reason:
            body.write(reason)
        if source:
            link = f"[원본 링크]({url})" if (url := source_url(source)) else "원본 링크 없음"
            photo = f" · 사진: {image['license']}" if image else ""
            st.caption(f"출처: {source['label']} · {link} · {source['license']}{photo}")
        else:
            st.caption("출처 정보 없음 · 원본 링크 없음")


def chips(r: dict) -> None:
    """AI가 해석한 조건(날짜·지역·동행·이동수단·테마)을 칩으로 (FR-001, 해석이 맞는지 사용자가 바로 확인)."""
    c = r.get("conditions")
    # 수정 턴의 conditions는 수정 문장만 해석한 값이라 원래 코스 조건과 달라 보인다 → 칩을 숨긴다
    if not c or r.get("intent") == "edit" or not (r.get("course") or r.get("places")):
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
                       None, by_place.get(m["place_id"]))
            st.button("코스에 넣기", key=f"add-{k}", on_click=ask, args=(f"{m['name']}을(를) 코스에 추가해줘",))


def show_result(r: dict, key: str, latest: bool = False) -> None:
    if "error" in r:
        st.error(r["error"]["message"])
        return
    chips(r)
    for n in r.get("notices") or []:  # 안내(날씨 미반영, 추정치, 조건 완화 등)는 맨 위에
        st.info(n)
    st.markdown(r["answer"])
    by_place = {s["place_id"]: s for s in r.get("sources") or [] if "place_id" in s}
    course = r.get("course")
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
                    for i, k, (arrive, leave) in zip(d["items"], keys, s["times"]):
                        place_card(k, f"{i['order']}. {i['name']}",
                                   f"{arrive}–{leave} · 체류 {hm(i.get('stay_min') or 60)} · {i['category']}",
                                   i.get("reason"), by_place.get(i["place_id"]))
                        if leg := i.get("leg_to_next"):  # 지도 선과 같은 모양: 도보 점선, 택시 실선
                            st.markdown(f"<div class='leg {leg['mode']}'>{leg_text(leg)}</div>", unsafe_allow_html=True)
    for n, p in enumerate(r.get("places") or []):
        place_card(f"card-{key}-s-{n}", p["name"], f"{p['category']} · {p['gu']}", p.get("reason"),
                   by_place.get(p["place_id"]))
    others = [s for s in r.get("sources") or [] if "place_id" not in s]  # 기상청·축제 등
    for s in others:
        link = f"[원본 링크]({url})" if (url := source_url(s)) else "원본 링크 없음"
        st.caption(f"출처: {s['label']} · {link} · {s['license']}")
    if course and course.get("days"):
        export_tools(r, key)
        if latest:
            st.caption("코스 고치기")
            for col, (label, text) in zip(st.columns(len(EDITS)), EDITS.items()):
                col.button(label, key=f"edit-{key}-{label}", on_click=ask, args=(text,), use_container_width=True)


def export_tools(r: dict, key: str) -> None:
    """PDF 내보내기(만들기 → 내려받기, 지도 타일을 매 화면마다 받지 않도록)와 공유용 요약 복사."""
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


# --- 사이드바: 선호 설정(FR-005), 사진, 관리자 ---
with st.sidebar:
    st.header("여행 조건")
    theme = st.selectbox("테마", THEMES)
    mode = st.radio("이동", list(MODES), format_func=MODES.get)
    companion = st.selectbox("누구와", COMPANIONS)
    prefs = {"theme": None if theme == THEMES[0] else theme, "mode": mode,
             "companion": None if companion == COMPANIONS[0] else companion}
    if ss.get("prefs") != prefs:
        api("PUT", f"/api/session/{ss.sid}/preferences", json=prefs)
        ss.prefs = prefs
    st.caption("질문에 쓴 조건이 있으면 그쪽을 먼저 따릅니다.")

    st.header("사진으로 찾기")
    photo = st.file_uploader("JPG·PNG, 10MB까지", type=["jpg", "jpeg", "png"])
    if photo and st.button("비슷한 곳 찾기", use_container_width=True, type="primary"):
        ss.queued_photo = (photo.name, photo.getvalue(), photo.type)  # 브라우저 세션 메모리에만, 저장하지 않는다

    if "admin" in st.query_params:  # 관리자 화면은 ?admin 으로 들어올 때만 보인다 (API는 토큰으로 따로 막는다)
        st.header("관리자")
        token = st.text_input("관리자 토큰", type="password")
        if st.button("데이터 수집", disabled=not token):
            r = api("POST", "/api/admin/ingest", headers={"X-Admin-Token": token}, json={})
            ss.run_id = r.get("run_id")
            st.write(r.get("error", {}).get("message") or f"수집 시작 · {ss.run_id}")
        if ss.get("run_id") and st.button("진행 상황", disabled=not token):
            st.json(api("GET", f"/api/admin/ingest/{ss.run_id}", headers={"X-Admin-Token": token}))

# --- 본문: 대화 (UI-01, UI-02) ---
message = st.chat_input("예) 이번 토요일 해운대, 부모님과 걷기 좋은 바다 코스") or ss.pop("queued", None)
busy = bool(message or ss.get("queued_photo"))
st.title("BusanMate")

if not ss.history and not busy:  # 첫 화면: 소개 + 예시 질문
    st.markdown("<div class='intro'>날짜와 동네, 함께 가는 사람을 적으면 하루 동선을 짜고 장소마다 예상 시각과 "
                "이동 시간을 붙여 줍니다. 장소는 부산시 공공데이터(명소·맛집·축제)에 있는 곳만 쓰고, 카드마다 출처와 "
                "원본 링크를 답니다. 비 예보가 있는 날은 실내 장소에 점수를 더 주고, 짠 뒤에는 \"카페 추가\"처럼 "
                "말로 고칠 수 있습니다.</div>", unsafe_allow_html=True)
    st.caption("이렇게 물어보세요")
    for col, ex in zip(st.columns(3), EXAMPLES):
        col.button(ex, on_click=ask, args=(ex,), use_container_width=True)

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
        with st.spinner("코스를 짜는 중… 보통 10~20초 걸립니다."):
            result = api("POST", "/api/chat", json={"session_id": ss.sid, "message": message})
        show_result(result, key=f"h{len(ss.history) + 1}", latest=True)
    ss.history += [{"role": "user", "content": message}, {"role": "assistant", "result": result}]

st.markdown("<div class='footer'>데이터: 공공데이터포털 부산광역시 부산명소·부산맛집·부산축제정보 서비스(이용허락범위 제한 없음) · "
            "기상청 단기예보(공공누리 제1유형) · 카카오모빌리티 길찾기, 카카오맵 · PDF 지도 © OpenStreetMap contributors.<br>"
            "이동시간에 '예상'이 붙은 값과 방문 시각은 추정치입니다.</div>", unsafe_allow_html=True)
