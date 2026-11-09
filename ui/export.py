"""코스 일정 계산·공유 텍스트·PDF 내보내기 (COMP-01 화면용). API 응답(course, sources, dates)만 쓴다."""
import io
import math
import re
from datetime import date
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

ACCENT, WALK, INK, GRAY = "#1d5fa7", "#2a9d55", "#1d1d1f", "#6b6760"
WEEKDAYS = "월화수목금토일"
# 한글 폰트: 도커는 fonts-nanum(Dockerfile), 로컬 Windows는 맑은 고딕, macOS는 애플 산돌고딕
FONTS = {False: ["/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "C:/Windows/Fonts/malgun.ttf",
                 "/System/Library/Fonts/AppleSDGothicNeo.ttc"],
         True: ["/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf", "C:/Windows/Fonts/malgunbd.ttf",
                "/System/Library/Fonts/AppleSDGothicNeo.ttc"]}
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"  # 사용 정책: 식별 가능한 User-Agent, 출처 표기
TILE_HEADERS = {"User-Agent": "BusanMate/0.1 (university prototype)"}
DATA_CREDIT = ("데이터: 공공데이터포털 부산광역시 부산명소·부산맛집·부산축제정보(이용허락범위 제한 없음) · "
               "기상청 단기예보(공공누리 제1유형) · 카카오모빌리티 · 지도 © OpenStreetMap contributors")


def source_url(s: dict | None) -> str | None:
    """원본 링크. 원천 필드에 '공식홈페이지: https://… 인스타그램: …'처럼 글이 섞여 있어 첫 URL만 쓴다."""
    m = re.search(r"https?://[^\s,)]+", (s or {}).get("url") or "")
    return m.group(0) if m else None


def hm(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}시간 {m}분" if h and m else f"{h}시간" if h else f"{m}분"


def clock(minutes: int) -> str:
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


def day_label(dates: list[str], day: int) -> str:
    if day > len(dates or []):
        return ""
    d = date.fromisoformat(dates[day - 1])
    return f"{d.month}월 {d.day}일({WEEKDAYS[d.weekday()]})"


def leg_text(leg: dict) -> str:
    if leg["mode"] == "walk":
        return f"도보 {leg['minutes']}분 (예상)"
    far = f" · 걸으면 {hm(leg['walk_minutes'])} 걸려 택시로" if leg.get("walk_minutes") else ""  # 도보만인데 먼 구간
    if leg["estimated"]:
        return f"택시 약 {leg['minutes']}분 (예상){far}"
    fare = f" · 약 {leg['taxi_fare']:,}원" if leg.get("taxi_fare") else ""
    return f"택시 {leg['minutes']}분{fare}{far}"


def schedule(day: dict) -> dict:
    """출발 시각 + 체류 + 구간 이동으로 장소별 도착·출발 시각을 계산한다. 시각은 '예상'이다."""
    h, m = map(int, (day.get("start") or "10:00").split(":"))
    t = start = h * 60 + m
    times, move = [], 0
    for item in day["items"]:
        arrive = t
        t += item.get("stay_min") or 60
        times.append((clock(arrive), clock(t)))
        if item.get("leg_to_next"):
            move += item["leg_to_next"]["minutes"]
            t += item["leg_to_next"]["minutes"]
    return {"times": times, "start": clock(start), "end": clock(t), "total": t - start, "move": move,
            "count": len(day["items"])}


def summary_line(s: dict) -> str:
    return f"{s['start']} 출발 → {s['end']} 마무리 · 장소 {s['count']}곳 · 총 {hm(s['total'])} (이동 {hm(s['move'])})"


def share_text(r: dict) -> str:
    """메신저에 붙여 넣을 요약. 장소·시각·이동·출처만 담는다."""
    lines, dates = ["BusanMate 부산 코스"], r.get("dates") or []
    for d in r["course"]["days"]:
        s = schedule(d)
        lines += ["", f"[{d['day']}일차 {day_label(dates, d['day'])}] {summary_line(s)}".replace("  ", " ")]
        for item, (arrive, _) in zip(d["items"], s["times"]):
            lines.append(f"{arrive} {item['order']}. {item['name']} ({item['category']})")
            if item.get("leg_to_next"):
                lines.append(f"      ↓ {leg_text(item['leg_to_next'])}")
    lines += ["", "출처: 부산광역시 공공데이터(부산명소·맛집·축제), 기상청 단기예보(공공누리 제1유형). 시각·이동시간은 예상치입니다."]
    return "\n".join(lines)


# --- PDF: 페이지를 이미지로 그려 Pillow로 PDF를 만든다.
# ponytail: 글자가 이미지라 PDF 안에서 검색·선택이 안 된다, 필요하면 fpdf2 같은 벡터 PDF 라이브러리로 바꾼다
W, H, MARGIN = 1240, 1754, 80  # A4 150dpi


def _font(size: int, bold: bool = False):
    for path in FONTS[bold]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)  # 한글 폰트가 없는 환경(CI 등): 깨지더라도 PDF는 만든다


def _wrap(text: str, font, width: int) -> list[str]:
    out, line = [], ""
    for ch in text.replace("\n", " "):
        if font.getlength(line + ch) > width and line:
            out.append(line)
            line = ""
        line += ch
    return out + [line] if line else out


def _px(lat: float, lng: float, z: int) -> tuple[float, float]:
    n = 256 * 2 ** z
    return (lng + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n


def route_map(items: list[dict], w: int = 1080, h: int = 520) -> Image.Image:
    """OSM 타일 위에 구간 선(도보 점선, 택시 실선)과 번호 마커를 그린다. 타일을 못 받으면 빈 바탕에 그린다."""
    lats, lngs = [i["lat"] for i in items], [i["lng"] for i in items]
    z = next((z for z in range(17, 9, -1)
              if abs(_px(min(lats), max(lngs), z)[0] - _px(max(lats), min(lngs), z)[0]) < w - 160
              and abs(_px(min(lats), max(lngs), z)[1] - _px(max(lats), min(lngs), z)[1]) < h - 160), 10)
    cx, cy = _px((min(lats) + max(lats)) / 2, (min(lngs) + max(lngs)) / 2, z)
    ox, oy = cx - w / 2, cy - h / 2
    img = Image.new("RGB", (w, h), "#eceae4")
    for tx in range(int(ox // 256), int((ox + w) // 256) + 1):
        for ty in range(int(oy // 256), int((oy + h) // 256) + 1):
            try:
                r = requests.get(TILE_URL.format(z=z, x=tx, y=ty), headers=TILE_HEADERS, timeout=5)
                r.raise_for_status()
                img.paste(Image.open(io.BytesIO(r.content)).convert("RGB"), (int(tx * 256 - ox), int(ty * 256 - oy)))
            except (requests.RequestException, OSError):
                continue
    draw = ImageDraw.Draw(img)
    pts = [(x - ox, y - oy) for x, y in (_px(i["lat"], i["lng"], z) for i in items)]
    for item, a, b in zip(items, pts, pts[1:]):
        walk = item.get("leg_to_next") and item["leg_to_next"]["mode"] == "walk"
        if not walk:
            draw.line([a, b], fill=ACCENT, width=5)
            continue
        dist = math.dist(a, b) or 1
        for s in range(0, int(dist), 18):  # 점선: 10px 선, 8px 틈
            e = min(s + 10, dist)
            draw.line([(a[0] + (b[0] - a[0]) * s / dist, a[1] + (b[1] - a[1]) * s / dist),
                       (a[0] + (b[0] - a[0]) * e / dist, a[1] + (b[1] - a[1]) * e / dist)], fill=WALK, width=5)
    num = _font(20, True)
    for item, (x, y) in zip(items, pts):
        draw.ellipse([x - 17, y - 17, x + 17, y + 17], fill=ACCENT, outline="white", width=3)
        draw.text((x, y), str(item["order"]), font=num, fill="white", anchor="mm")
    credit = "© OpenStreetMap contributors"
    f = _font(15)
    tw = f.getlength(credit)
    draw.rectangle([w - tw - 16, h - 26, w, h], fill="white")
    draw.text((w - tw - 8, h - 23), credit, font=f, fill=GRAY)
    return img


def course_pdf(r: dict) -> bytes:
    """일자별 장소·시각·이동시간·지도·출처와 이용 조건을 담은 PDF (한 일차가 길면 다음 쪽으로 넘긴다)."""
    by_place = {s["place_id"]: s for s in r.get("sources") or [] if "place_id" in s}
    others = [s for s in r.get("sources") or [] if "place_id" not in s]
    f_title, f_bold, f_body, f_small = _font(40, True), _font(25, True), _font(22), _font(18)
    pages: list[Image.Image] = []

    def new_page():
        page = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(page)
        for i, line in enumerate(_wrap(DATA_CREDIT, f_small, W - 2 * MARGIN)):
            d.text((MARGIN, H - 90 + i * 24), line, font=f_small, fill=GRAY)
        pages.append(page)
        return d, MARGIN

    def text(d, y, s, font, fill=INK, indent=0, gap=8):
        for line in _wrap(s, font, W - 2 * MARGIN - indent):
            d.text((MARGIN + indent, y), line, font=font, fill=fill)
            y += font.size + gap
        return y

    for day in r["course"]["days"]:
        s = schedule(day)
        d, y = new_page()
        y = text(d, y, f"BusanMate 코스 · {day['day']}일차 {day_label(r.get('dates') or [], day['day'])}", f_title)
        y = text(d, y + 4, summary_line(s), f_body, GRAY) + 16
        map_img = route_map(day["items"])
        pages[-1].paste(map_img, (MARGIN, y))
        y += map_img.height + 30
        for item, (arrive, leave) in zip(day["items"], s["times"]):
            src = by_place.get(item["place_id"])
            block = [(f"{item['order']}. {item['name']}  ·  {arrive}–{leave} (체류 {hm(item.get('stay_min') or 60)})",
                      f_bold, INK, 0)]
            block.append((f"{item['category']} · {item.get('gu') or ''}".rstrip(" ·"), f_small, GRAY, 0))
            if item.get("reason"):
                block.append((item["reason"], f_body, INK, 0))
            if src:
                block.append((f"출처: {src['label']} · {source_url(src) or '원본 링크 없음'} · 이용 조건: {src['license']}",
                              f_small, GRAY, 0))
            if item.get("leg_to_next"):
                block.append((f"↓ {leg_text(item['leg_to_next'])}", f_small, WALK if item["leg_to_next"]["mode"] == "walk"
                              else ACCENT, 24))
            need = sum(len(_wrap(t, f, W - 2 * MARGIN - ind)) * (f.size + 8) for t, f, _, ind in block) + 20
            if y + need > H - 120:
                d, y = new_page()
            for t, f, color, ind in block:
                y = text(d, y, t, f, color, ind)
            y += 20
    if others:
        if y > H - 300:
            d, y = new_page()
        for o in others:
            y = text(d, y, f"출처: {o['label']} · {source_url(o) or '원본 링크 없음'} · 이용 조건: {o['license']}", f_small, GRAY)
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", resolution=150, save_all=True, append_images=pages[1:])
    return buf.getvalue()
