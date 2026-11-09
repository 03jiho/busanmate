# BusanMate — 부산 맞춤 여행코스 안내

공공데이터에 실제로 있는 부산 장소만으로, 자연어 조건에 맞는 반나절~2박 3일 코스를 출처와 함께 추천하는 AI 서비스입니다.

- **자연어 장소 검색(RAG):** 속성 필터와 의미 검색을 함께 쓰는 하이브리드 검색
- **맞춤 코스 추천:** 선호, 날씨(기상청), 축제를 반영하고 하루 2~6곳의 방문 순서를 최적화. 이동은 가까우면 도보, 멀면 택시(카카오 실측)
- **사진 기반 장소 탐색:** CLIP으로 명소를 인식하고, 닮은 명소가 없으면 VLM 장면 설명으로 비슷한 분위기의 장소를 추천
- **대화형 코스 수정:** "두 번째 장소 빼고 카페 넣어줘"처럼 말하면 해당 장소만 바꿈

설계 문서는 [docs/](docs/)에 있습니다(SRS, 프로젝트 계획서, SDD).

## 구조

| 계층 | 위치 | 내용 |
|---|---|---|
| 프레젠테이션 | `ui/` | Streamlit 채팅·지도(카카오맵)·사진 업로드 |
| API | `api/` | FastAPI (API-001~008) |
| 에이전트 | `agent/` | LangGraph 오케스트레이터, 조건 추출, 근거 검증 응답 |
| 서비스·도구 | `services/`, `tools/` | 검색, 점수화·경로, 이미지, LLM 클라이언트 / 날씨·축제·길찾기 도구 |
| 데이터 | `pipeline/`, `db/` | 공공 API 수집·적재, PostgreSQL 스키마, Chroma 벡터 |

모든 수치(가중치, 임계값 등)는 [config.yaml](config.yaml)에 있는 초안 값입니다. 비밀값은 `.env`에만 둡니다.

## 실행

필요한 것: Docker(Docker Compose 포함), 공공데이터포털·카카오·Gemini API 키.

1. 키를 준비합니다. `.env.example`을 `.env`로 복사하고 값을 채웁니다(공공데이터포털, 카카오, Gemini, 관리자 토큰, DB 비밀번호 `POSTGRES_PASSWORD`). `.env`는 커밋하지 않습니다.
2. 한 번에 띄웁니다. ui(8501), api(8000), db(PostgreSQL, 5432), vector(Chroma, 8001)가 함께 뜨고, DB 스키마는 처음 뜰 때 자동으로 만들어집니다. 첫 빌드는 PyTorch 때문에 몇 분 걸립니다.
   ```bash
   docker compose up -d --build
   ```
3. 처음 한 번 데이터를 적재합니다. 장소·축제·이미지 임베딩까지 하며, Gemini 무료 한도 때문에 10분 정도 걸립니다. 이후에는 바뀐 장소만 처리합니다.
   ```bash
   docker compose exec api python -m pipeline.run
   ```
   API를 쓸 수 없을 때는 시드 CSV(공공데이터 원본 48행)로 적재할 수 있습니다.
   ```bash
   docker compose exec api python -m pipeline.run --seed
   ```
4. 브라우저에서 `http://localhost:8501`을 엽니다. 상태 확인은 `http://localhost:8000/api/health`입니다.
5. 카카오 개발자 콘솔에서 JavaScript 키의 "JavaScript SDK 도메인"에 UI 주소(예: `http://localhost:8501`)를 등록해야 지도가 보입니다.

중지는 `docker compose down`입니다. 데이터는 볼륨에 남고, 지우려면 `docker compose down -v`를 씁니다.

### 로컬 개발

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
```
```bash
docker compose up -d db vector
```
```bash
.venv/Scripts/uvicorn api.main:app --reload --port 8000
```
```bash
.venv/Scripts/streamlit run ui/app.py
```

테스트는 `pytest -q`로 돌립니다(DB가 없으면 DB 테스트는 skip). 사진 인식 평가는 `python -m eval.photo_eval <폴더>`입니다.

## 데이터 출처와 이용 조건

| 데이터 | 제공처 | 이용 조건 |
|---|---|---|
| 부산 명소·맛집·축제 정보 (장소 설명, 좌표, 대표 이미지) | 공공데이터포털 부산광역시_부산명소정보·부산맛집정보·부산축제정보 서비스 | 이용허락범위 제한 없음 |
| 단기예보 (강수확률) | 기상청 (공공데이터포털) | 공공누리 제1유형(출처 표시) |
| 자동차 길찾기 (택시 구간 시간·요금) | 카카오모빌리티 | 저장·캐시하지 않음 |
| 지도 | 카카오맵 | 카카오 이용약관 |

화면의 모든 장소에는 제공처와 원본 링크가 함께 표시됩니다. 도보 구간 시간과 조회에 실패한 택시 구간은 직선거리 기반 추정치이며 "예상"으로 표시합니다.

## AI 활용

LLM·VLM은 Google Gemini(무료 등급)를 `LLMClient` 인터페이스 뒤에서 사용합니다. 장소 이름·좌표·출처는 항상 DB에서 채우고, 점수화와 경로 계산은 코드가 합니다. 설계 문서 초안과 이 저장소의 코드 작성에는 Claude(Anthropic)를 활용했으며, 자세한 내역은 각 문서 부록에 있습니다.
