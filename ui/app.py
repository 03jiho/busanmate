"""Streamlit UI (COMP-01, UI-01·02). api만 호출한다. 실행: streamlit run ui/app.py (테마는 .streamlit/config.toml)"""
import json
import os
import uuid
from pathlib import Path

import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
API_URL = os.environ.get("API_URL", "http://localhost:8000")
KAKAO_JS_KEY = os.environ.get("KAKAO_JAVASCRIPT_KEY", "")  # 브라우저용 키(도메인 제한), REST 키는 쓰지 않는다
THEMES = ["선택 안 함", "바다", "자연", "역사·문화", "맛집", "쇼핑·시장", "핫플"]
MODES = {"auto": "자동 (가까우면 도보, 멀면 택시)", "walk": "도보만", "taxi": "택시만"}
COMPANIONS = ["선택 안 함", "혼자", "친구", "연인", "가족", "부모님", "아이와"]
RAIN_THRESHOLD = 60  # config weather.rain_prob_threshold와 같게 (UI는 api만 호출하므로 표시용으로만 둔다)
EXAMPLES = ["이번 토요일 해운대에서 부모님과 걷기 좋은 바다 코스", "다음 주 금요일부터 1박 2일 역사·문화 여행",
            "광안리 근처 비 오는 날 갈 만한 실내 장소"]
ACCENT, WALK = "#1d5fa7", "#2a9d55"  # 택시 구간·포인트 색, 도보 구간 색 (지도 선과 목록 구간 표시가 같은 색을 쓴다)
CSS = f"""<style>
[data-testid="stMainBlockContainer"] {{padding-top: 2.5rem; max-width: 1180px;}}
h1 {{font-size: 1.6rem !important; font-weight: 700 !important; letter-spacing: -0.02em; padding-bottom: 0 !important;}}
[data-testid="stSidebar"] h2 {{font-size: .78rem !important; font-weight: 600 !important; color: #6b6760;
  margin: 1.4rem 0 .2rem !important; padding: 0 !important;}}
[data-testid="stSidebar"] .stButton button {{justify-content: flex-start; text-align: left; font-weight: 400;}}
.leg {{color: #6b6760; font-size: .82rem; margin: .15rem 0 .15rem 1rem; padding: .1rem 0 .1rem .7rem;
  border-left: 2px dashed {WALK};}}
.leg.taxi {{border-left: 2px solid {ACCENT};}}
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


def kakao_map(items: list[dict]) -> str:
    """마커·순번·구간 선(도보는 점선, 택시는 실선). 장소명은 textContent로 넣어 HTML 주입을 막는다."""
    pts = json.dumps([{"lat": i["lat"], "lng": i["lng"], "label": f"{i['order']}. {i['name']}",
                       "walk": bool(i["leg_to_next"]) and i["leg_to_next"]["mode"] == "walk"} for i in items],
                     ensure_ascii=False)
    return f"""
<div id="map" style="width:100%;height:380px;border-radius:6px;color:#8a857c;font:13px sans-serif"></div>
<script src="https://dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JS_KEY}&autoload=false"></script>
<script>
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
    new kakao.maps.Marker({{map: map, position: pos}});
    var tag = document.createElement('div');
    tag.textContent = p.label;
    tag.style.cssText = 'background:#fff;border:1px solid {ACCENT};border-radius:4px;padding:2px 7px;font:12px sans-serif;color:#1d1d1f;';
    new kakao.maps.CustomOverlay({{map: map, position: pos, content: tag, yAnchor: 2.2}});
    if (i + 1 < pts.length) {{
      new kakao.maps.Polyline({{map: map, path: [pos, new kakao.maps.LatLng(pts[i + 1].lat, pts[i + 1].lng)],
        strokeWeight: 4, strokeColor: p.walk ? '{WALK}' : '{ACCENT}', strokeOpacity: 0.85,
        strokeStyle: p.walk ? 'shortdash' : 'solid'}});
    }}
  }});
  map.setBounds(bounds);
}});
</script>"""


def leg_text(leg: dict) -> str:
    if leg["mode"] == "walk":
        return f"도보 {leg['minutes']}분 (예상)"
    if leg["estimated"]:
        return f"택시 약 {leg['minutes']}분 (예상)"
    fare = f" · 약 {leg['taxi_fare']:,}원" if leg.get("taxi_fare") else ""
    return f"택시 {leg['minutes']}분{fare}"


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
    left, right = st.columns([3, 2])
    with left:
        items = [{**m, "order": i + 1, "leg_to_next": None} for i, m in enumerate(r["matches"])]
        if KAKAO_JS_KEY:
            components.html(kakao_map(items), height=390)
        else:
            st.map([{"lat": m["lat"], "lon": m["lng"]} for m in r["matches"]])
    with right:
        for i, m in enumerate(r["matches"]):
            st.markdown(f"**{i + 1}. {m['name']}**  \n:gray[{m['category']} · {m['gu']} · 유사도 {m['similarity']:.0%}]")
            if st.button("코스에 넣기", key=f"{key}-{m['place_id']}"):
                ss.queued = f"{m['name']}을(를) 코스에 추가해줘"
    with st.expander(f"출처 {len(r['sources'])}건"):
        for s in r["sources"]:
            link = f" ([원본]({s['url']}))" if s.get("url") else ""
            st.markdown(f"- {s['name']} — {s['label']}{link} · {s['license']}")


def show_result(r: dict) -> None:
    if "error" in r:
        st.error(r["error"]["message"])
        return
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
                left, right = st.columns([3, 2])
                with left:
                    if KAKAO_JS_KEY:
                        components.html(kakao_map(d["items"]), height=390)
                    else:
                        st.map([{"lat": i["lat"], "lon": i["lng"]} for i in d["items"]])
                with right:
                    for i in d["items"]:
                        place_card(f"{i['order']}. {i['name']}", i["category"], i.get("reason"), by_place.get(i["place_id"]))
                        if leg := i.get("leg_to_next"):  # 지도 선과 같은 모양: 도보 점선, 택시 실선
                            st.markdown(f"<div class='leg {leg['mode']}'>{leg_text(leg)}</div>", unsafe_allow_html=True)
    for p in r.get("places") or []:
        place_card(p["name"], f"{p['category']} · {p['gu']}", p.get("reason"), by_place.get(p["place_id"]))
    others = [s for s in r.get("sources") or [] if "place_id" not in s]  # 기상청·축제 등
    for s in others:
        link = f" ([원본]({s['url']}))" if s.get("url") else ""
        st.caption(f"출처: {s['label']}{link} · {s['license']}")


def place_card(title: str, sub: str, reason: str | None, source: dict | None) -> None:
    """코스 카드 한 장: 장소, 추천 이유, 출처(제공처·원본 링크·이용 조건) (FR-003)."""
    with st.container(border=True):
        st.markdown(f"**{title}**  \n:gray[{sub}]")
        if reason:
            st.write(reason)
        if source:
            link = f" · [원본]({source['url']})" if source.get("url") else ""
            st.caption(f"출처: {source['label']}{link} · {source['license']}")


# --- 사이드바: 선호 설정(FR-005), 예시, 관리자 ---
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

    st.header("예시 질문")
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True):
            ss.queued = ex

    st.header("사진으로 찾기")
    photo = st.file_uploader("JPG·PNG, 10MB까지", type=["jpg", "jpeg", "png"])
    if photo and st.button("비슷한 곳 찾기", use_container_width=True, type="primary"):
        ss.queued_photo = (photo.name, photo.getvalue(), photo.type)  # 브라우저 세션 메모리에만, 저장하지 않는다

    with st.expander("관리자"):
        token = st.text_input("관리자 토큰", type="password")
        if st.button("데이터 수집", disabled=not token):
            r = api("POST", "/api/admin/ingest", headers={"X-Admin-Token": token}, json={})
            ss.run_id = r.get("run_id")
            st.write(r.get("error", {}).get("message") or f"수집 시작 · {ss.run_id}")
        if ss.get("run_id") and st.button("진행 상황", disabled=not token):
            st.json(api("GET", f"/api/admin/ingest/{ss.run_id}", headers={"X-Admin-Token": token}))

# --- 본문: 대화 (UI-01, UI-02) ---
st.title("BusanMate")
st.caption("가고 싶은 날짜와 동네, 함께 가는 사람을 적으면 부산 여행 코스를 짜 줍니다. "
           "공공데이터에 있는 장소만 쓰고, 장소마다 출처를 붙입니다.")

for n, h in enumerate(ss.history):
    with st.chat_message(h["role"]):
        if h["role"] == "user":
            st.markdown(h["content"])
        elif h.get("photo"):
            show_photo_result(h["result"], key=f"h{n}")
        else:
            show_result(h["result"])

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

message = st.chat_input("예) 이번 토요일 해운대, 부모님과 걷기 좋은 바다 코스") or ss.pop("queued", None)
if message:  # 질문은 바로 보이고, 답은 기다리는 동안 스피너
    with st.chat_message("user"):
        st.markdown(message)
    with st.chat_message("assistant"):
        with st.spinner("코스를 짜는 중… 보통 10~20초 걸립니다."):
            result = api("POST", "/api/chat", json={"session_id": ss.sid, "message": message})
        show_result(result)
    ss.history += [{"role": "user", "content": message}, {"role": "assistant", "result": result}]

st.markdown("<div class='footer'>데이터: 공공데이터포털 부산광역시 부산명소·부산맛집·부산축제정보 서비스(이용허락범위 제한 없음) · "
            "기상청 단기예보(공공누리 제1유형) · 카카오모빌리티 길찾기, 카카오맵.<br>"
            "이동시간에 '예상'이 붙은 값은 직선거리로 추정한 것입니다.</div>", unsafe_allow_html=True)
