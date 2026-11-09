# BusanMate — 부산 맞춤 여행코스 안내 서비스 (프로토타입)

## 문서
- 기준 문서: `docs/01_요구사항명세서SRS_BusanMate.md`, `docs/02_프로젝트계획서_BusanMate.md`, `docs/03_소프트웨어설계서_BusanMate.md`.
  md는 docx에서 자동 생성한 사본이다. docx가 바뀌면 `.venv/Scripts/python docs/docx2md.py`로 다시 만들고 md를 직접 고치지 않는다.
- 요구사항 ID(FR/NFR/AI-REQ/UC/AC)와 설계 ID(COMP/API/ADR/ERR)를 코드 주석·테스트 이름·PR에 적어 추적성을 유지한다.
- 문서의 수치·임계값은 모두 **초안 값**이다(SRS §9, SDD §1.1). 평가 후 조정한다.
- 문서 간 불일치: 계획서는 카카오 길찾기를 "도보·자동차"로 적었지만 SRS §2.5·SDD §3.1과 실제 API는 **자동차만**이다. 자동차만을 따른다.

## 아키텍처 (SDD §2)
6계층. 상위 계층은 **바로 아래 계층만** 호출한다.

| 계층 | 구성 | 디렉터리 |
|---|---|---|
| 사용자 | 브라우저 (Chrome/Edge) | – |
| 프레젠테이션 | Streamlit. 상태는 `session_id`만 보유, api만 호출 | `ui/` |
| API | FastAPI: 요청 검증, 세션 식별, 관리자 토큰, 요청 한도, 오류 표준화 | `api/` |
| 에이전트 | LangGraph 오케스트레이터, QueryParser, ResponseBuilder, SessionStore | `agent/` (프롬프트는 `agent/prompts/`, 버전 관리) |
| 서비스·도구 | SearchService, Scorer, RoutePlanner, ImageService / Weather·Festival·MapTool | `services/`, `tools/` |
| 데이터·외부 | PostgreSQL, Chroma, DataPipeline, 외부 API | `db/`, `pipeline/` |

ADR 요약: 001 계층형 모놀리식 API · 002 RAG(속성 필터+벡터) + `[[place_id]]` 검증 · 003 LLM은 해석·도구 선택·설명만, 점수화·경로는 코드 · 004 공공데이터는 배치 적재, 날씨·이동시간만 실시간 · 005 PostgreSQL(속성)+Chroma(벡터) · 006 공통 BaseTool · 007 세션은 서버 메모리 TTL 2h, 사진 비저장 · 008 LLM·VLM 응답 캐시(이동시간 제외).

## 컴포넌트 (SDD §3)
| ID | 컴포넌트 | 책임 | 위치 |
|---|---|---|---|
| COMP-01 | Streamlit UI | 채팅·사진 업로드·선호 설정, 코스·지도·출처 표시, 관리자 화면 | `ui/` |
| COMP-02 | API 서버 | 요청 검증, 세션, 관리자 토큰, 요청 한도, 오류 표준화 | `api/` |
| COMP-03 | AgentOrchestrator | LangGraph 흐름 제어, 도구 선택·호출, **턴당 도구 호출 6회 제한** | `agent/` |
| COMP-04 | QueryParser | 지역·날짜·기간(1~3일)·동행·테마·시간·이동수단 추출(structured output), 되묻기 | `agent/` |
| COMP-05 | SearchService | 속성 필터 + 벡터 하이브리드 검색, 후보 N, 조건 완화 | `services/` |
| COMP-06 | Scorer · RoutePlanner | 점수화, K 결정, 다양성 보정, 일별 분배, 방문 순서 최적화 | `services/` |
| COMP-07 | 도구 어댑터 | Weather·Festival·Map, BaseTool 구현, 실패 시 폴백 | `tools/` |
| COMP-08 | ImageService | 사진 검증·CLIP 임베딩, 유사 장소, VLM 장면 설명 폴백 | `services/` |
| COMP-09 | ResponseBuilder | 설명 생성, `verify_places`, 출처 첨부, 응답 조립 | `agent/` |
| COMP-10 | SessionStore | 선호·직전 코스·최근 N턴 (TTL 2h, 메모리) | `agent/` |
| COMP-11 | LLM/VLM Client | 모델 호출 추상화, 응답 캐시, 호출 로그(모델·토큰·비용·지연) | `services/` |
| COMP-12 | DataPipeline | 수집·정제·중복 제거·임베딩·적재, `ingest_log` | `pipeline/` |

### 핵심 규칙
- **환각 방지**: LLM에는 이번 턴 후보만(id·이름·분류·한 줄 요약) 보여 주고, 장소는 `[[place_id]]` 마커로만 가리키게 한다. `verify_places`가 마커 id를 DB·후보와 대조해 없으면 그 문장을 지우고, 통과한 마커는 DB 이름으로 치환한다. 이름·좌표·출처는 항상 DB에서 채운다. 출처 없는 장소는 응답에서 뺀다.
- **LLM vs 코드**(AI-REQ-04): 점수화·장소 선택·순서·시간 계산은 결정적 코드. `build_course`는 `search_places` 결과가 있을 때만 실행하고, 순서가 어긋난 호출은 오류로 돌려준다.
- **에이전트 도구**: `search_places`, `get_weather`, `get_festivals`, `build_course`, `get_route_times`, `edit_course` (`search_by_image`는 별도 엔드포인트).
- **BaseTool**: 속성 `name, description, args_schema, timeout_s`, 메서드 `run(args) -> ToolResult`, `fallback(args, err)`. `ToolResult = {ok, data, sources, notices, stale}`. 오케스트레이터는 레지스트리에서 이름으로 찾는다. 도구를 추가해도 에이전트 코드는 고치지 않는다(NFR-009).
- **알고리즘**(SDD §3.1): `S(p) = w1·sim + w2·pref + w3·weather − w4·dist`. weather는 강수확률 ≥ 임계면 실내 1/실외 0, 예보가 없으면 0.5. `K = clamp(floor(가용시간 / (평균 체류 + 평균 이동)), 2, 6)`. 같은 분류는 λ만큼 감점하고 3곳 연속은 금지. 순서는 순열 전수 탐색(≤720). 다일 일정은 위·경도 k-means(k=일수)로 나누고 전체 일정에서 장소 중복을 막는다. 후보가 2곳 미만이면 운영시간 → 실내 → 구 순서로 조건을 완화하고 사용자에게 알린다.
- **사진**: JPG/PNG ≤10MB(확장자와 매직바이트 확인). 메모리에서만 처리하고 응답 후 폐기한다. 최고 유사도 ≥ τ이면 인식, 미만이면 VLM 장면 설명 → `place_text` 검색으로 5곳을 고른다. 상위 10개 이미지를 place_id별 최대값으로 집계한다.

## API (SDD §6)
| ID | Method | Endpoint | Request → Response |
|---|---|---|---|
| API-001 | POST | `/api/chat` | `{session_id, message}` → `{answer, conditions, course, sources, notices}` |
| API-002 | POST | `/api/search/image` | multipart(session_id, file) → `{mode, matches, sources, notices}` |
| API-003 | GET | `/api/session/{id}/preferences` | → `{theme, mode, companion}` |
| API-004 | PUT | `/api/session/{id}/preferences` | `{theme, mode, companion}` → `{ok}` |
| API-005 | GET | `/api/session/{id}/course` | → `{course}` |
| API-006 | POST | `/api/admin/ingest` (X-Admin-Token) | `{sources?}` → `{run_id}` |
| API-007 | GET | `/api/admin/ingest/{run_id}` | → `{status, new, updated, skipped}` |
| API-008 | GET | `/api/health` | → `{status}` |

- `course`는 날짜별 `items[{order, place_id, name, lat, lng, reason, leg_to_next{minutes, meters, estimated}}]`로 구성한다.
- 관리자 토큰은 `ADMIN_TOKEN`과 **상수 시간 비교**(`hmac.compare_digest`)한다.
- 오류 코드: 400 INVALID_INPUT(되묻기) · 401 UNAUTHORIZED · 413/415 FILE_* · 429 RATE_LIMITED · 502/504 UPSTREAM_*(폴백 응답 + "최신 정보 아님") · 500 INTERNAL(상세는 서버 로그에만).

## DB (SDD §5.2)
- `place`: place_id BIGSERIAL PK, (source, source_id) UNIQUE, name, category, sub_category?, gu, address, lat, lng(좌표가 없으면 적재하지 않음), description?, open_hours?(JSONB, 파싱 실패 시 원문), indoor?(불명이면 NULL), tel?, url?, license, fetched_at, is_active
- `place_image`: image_id PK, place_id FK, url, license(이용 조건이 확인된 것만), embedding_id?
- `festival`: festival_id PK, source_id UNIQUE, name, description?, start_date, end_date, lat?, lng?, address?, url?, license, fetched_at
- `ingest_log`: log_id PK, run_at, source, new_cnt, updated_cnt, skipped_cnt, status, message?
- Chroma `place_text`(id=place_id, 문서=이름+설명, 메타 gu·category·indoor, cosine) / `place_image`(id=image_id, 메타 place_id).
- 세션은 DB에 저장하지 않는다(메모리 dict: preferences, last_course, 최근 N턴).
- 수집은 멱등이어야 한다. 2회 실행해도 중복이 0건이어야 한다(AC-007). 소스가 실패하면 건너뛰고 최대 3회 재시도한다.

## 설정 원칙: 수치는 config.yaml, 비밀값은 .env
초안 수치는 코드에 하드코딩하지 않고 전부 `config.yaml`에 둔다(SDD §10.3).
- 점수 가중치 w1~w4 = 0.4 / 0.3 / 0.2 / 0.1, 다양성 감점 λ = 0.1
- 강수확률 임계 60%, 날씨 캐시 1시간
- 구간 보정계수: 같은 구 1.3, 인접 구 1.5, 바다·다리 구간 1.8 (인접 구·바다 구간 표도 config)
- 후보 수 N = 30, 하루 장소 수 K = 2~6, 기본 가용 시간 9h, 최대 기간 3일, 기본 체류 시간, 수단별 이동 속도
- 사진 임계 τ = 0.80, 상위 이미지 10, 결과 5
- 세션 TTL 2h, 최근 대화 N턴, 요청 한도 세션당 분당 20회, 턴당 도구 호출 6회, 도구별 timeout
- LLM/VLM/임베딩 모델명

## 환경
- Python 3.11, 로컬 `.venv` (Windows: `.venv/Scripts/python`). `docker-compose.yml` 구성은 ui:8501, api:8000, db(PostgreSQL):5432, vector(Chroma):8001.
- Docker Desktop은 사용자 단위로 설치되어 있다: `%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin\docker.exe`(Git Bash에서는 PATH에 없으므로 이 경로를 PATH에 추가해 쓴다). 엔진이 꺼져 있으면 `Docker Desktop.exe`를 먼저 실행한다. 개발 중에는 `docker compose up -d db vector`만 띄우고, api와 ui는 로컬 `.venv`에서 실행한다.
- Gemini 무료 등급 임베딩 한도는 **분당 100건**이고, 배치 안의 문서 하나하나가 1건으로 계산된다. 그래서 `embedding.batch_size`=90, 배치 사이 61초 대기(`batch_interval_s`)로 설정했다.
- **LLM/VLM**: Google Gemini(AI Studio 무료 등급), 키는 `GEMINI_API_KEY`. 반드시 `LLMClient`/`VLMClient` 인터페이스 뒤에서 호출하고, 제공자는 `LLM_PROVIDER`, 모델명은 config.yaml로 고른다. 무료 등급 한도를 고려해 응답 캐시와 호출 로그를 둔다(NFR-010).
- **`.env` 변수** (`.env.example`에 이름만 있음):

| 변수 | 용도 |
|---|---|
| `DATA_GO_KR_SERVICE_KEY` | 공공데이터포털 서비스키 (부산 3종 + 기상청) |
| `BUSAN_ATTRACTION_URL` / `BUSAN_FOOD_URL` / `BUSAN_FESTIVAL_URL` | 부산명소·부산맛집·부산축제 정보 엔드포인트 |
| `KMA_FORECAST_URL` | 기상청 단기예보 엔드포인트 |
| `KAKAO_REST_API_KEY` | 카카오모빌리티 길찾기(서버) |
| `KAKAO_MOBILITY_DIRECTIONS_URL` | 카카오모빌리티 자동차 길찾기 엔드포인트 |
| `KAKAO_JAVASCRIPT_KEY` | 카카오맵 JS SDK(브라우저 지도 표시) |
| `LLM_PROVIDER` | LLMClient 구현 선택 (기본 gemini) |
| `GEMINI_API_KEY` | Gemini API |
| `ADMIN_TOKEN` | 관리자 API(`X-Admin-Token`) |
| `POSTGRES_PASSWORD` | docker-compose PostgreSQL 비밀번호 (api에는 `DATABASE_URL`로 주입) |

설정은 `settings.py`로 읽는다: `config()`는 config.yaml, `secret(NAME)`은 환경변수(없으면 이름만 담은 오류). 실행은 `uvicorn api.main:app --reload`, `streamlit run ui/app.py`, `pytest`.

### 비밀값 규칙 (절대)
- 키·토큰 **값**을 코드, 테스트, 로그, 커밋, 문서, 채팅 출력에 절대 쓰지 않는다. 값은 런타임에 `.env`(환경변수)에서만 읽는다.
- `.env`를 cat·print·grep으로 출력하지 않는다. 변수 존재 여부는 이름만 확인한다.
- `.env`는 `.gitignore`에 있다. 저장소에는 값이 빈 `.env.example`만 올린다.
- 외부 호출 URL이나 오류 메시지를 로그로 남길 때는 `serviceKey`, `authKey`, `Authorization` 같은 값을 마스킹한다. 예외 메시지 안에 키가 섞인 URL이 들어가는 경우도 조심한다.
- 질의 원문, 업로드 사진, 토큰은 로그에 남기지 않는다. 로그에는 request_id, 세션 해시, 단계별 지연, 도구 목록, 오류 코드, 폴백 여부만 남긴다(SDD §8).
- 카카오 JS 키는 브라우저에 노출되는 키다. 카카오 개발자 콘솔에 등록한 도메인(로컬은 localhost)에서만 쓰고, REST 키는 서버에서만 쓴다.

### 외부 API 제약
- **카카오모빌리티 길찾기는 자동차 경로만** 제공한다(REST API 키 사용). 도보·대중교통 이동시간, 그리고 자동차 조회가 실패했을 때는 **직선거리(haversine) × 구간 보정계수 ÷ 수단 속도**로 추정하고 `estimated=true`와 "예상"을 표시한다.
- **이동시간·경로는 저장·캐시하지 않는다**(DB, 파일, 메모리 캐시, LLM 응답 캐시 모두 해당). 매 요청마다 조회하거나 추정한다.
- **기상청 예보는 출처 표시 필수(공공누리 제1유형)**: 날씨를 반영한 응답과 화면에 "출처: 기상청"을 표기한다. 예보 범위 밖이거나 조회가 실패하면 날씨를 반영하지 않고(weather=0.5) 사용자에게 알린다.
- 공공 API 호출 한도는 기능별 하루 10,000건(개발계정)이다. 부산 3종 서비스는 이용허락범위 제한이 없다(SRS §2.5). 모든 장소·이미지에 `license`와 원본 링크를 저장하고 표시한다.
- 외부 API에 장애가 나면 DB·캐시 데이터로 응답하고 "최신 정보 아님"을 표시한다(NFR-003, AC-006). 테스트에서는 외부 API를 mock으로 대체한다.

## 데이터 수집 (1단계 결정, 2026-11-04)
- `.env`의 부산 URL은 서비스 경로까지만 있다. 호출할 때 오퍼레이션 `getAttractionKr` / `getFoodKr` / `getFestivalKr`를 붙인다(config `sources`). 응답은 JSON(UTF-8)이다. 명소 213, 맛집 471, 축제 41건(그중 기간을 읽을 수 있는 38건 적재).
- 키 필드: `UC_SEQ`(→source_id), `MAIN_TITLE`(끝의 "(한,영,중간,중번,일)" 제거), `GUGUN_NM`, `LAT/LNG`, `ADDR1`, `ITEMCNTNTS`, `USAGE_DAY_WEEK_AND_TIME`(운영시간, 축제는 기간), `HLDY_INFO`, `MAIN_IMG_NORMAL`(visitbusan.net 이미지 → place_image, 사진 기능 소스), `RPRSNTV_MENU`(맛집).
- 원본에는 분류 필드가 없다. 그래서 이름 규칙(config `classify`)으로 category와 indoor를 추정한다. 맛집은 음식점/실내로 둔다. 규칙에 걸리지 않으면 관광지/indoor NULL로 둔다.
- 축제 기간은 자유 형식 문자열이라 `parse_period`로 읽는다. 연도가 없으면 적재하지 않는다.
- 임베딩: Gemini `gemini-embedding-001`, 768차원. 문서는 `RETRIEVAL_DOCUMENT`, 질의는 `RETRIEVAL_QUERY`(`services/embedding.py`). 내용이 바뀐 장소만 다시 임베딩한다.
- 원본에서 사라진 장소는 `is_active=FALSE`로 바꾸고 Chroma에서도 지운다. 응답이 비어 있으면 실패로 처리한다(전체가 비활성화되는 것을 막기 위해).
- Chroma는 서버와 클라이언트 모두 1.5.8이다(0.6.x는 numpy<2를 요구해 pandas·Streamlit과 충돌).
- 실행: `docker compose up -d db vector` 후 `python -m pipeline.run [busan_attraction ...]`. 결과는 `ingest_log`에 남는다. 로그에는 serviceKey가 섞이지 않게 오류 종류만 남긴다(`pipeline/run.py:_safe`).
- 단계(`pipeline/run.py`): `collect`(API 또는 시드) → `clean`(필수값 없는 행 제외) → `dedup`(원본 id 기준) → `load_places`(바뀐 행만 upsert, 미커밋) → `deactivate` → `embed` → 커밋. 임베딩이 실패하면 DB도 롤백된다.
- **시드 모드:** `python -m pipeline.run --seed`는 `data/seed/busan_seed.csv`(API 원본 행 48건: 명소 30, 맛집 12, 축제 6, 2026-11-05 수집)를 같은 경로로 적재한다. 일부 데이터라 비활성화 단계는 건너뛴다. API가 바뀌면 같은 방식으로 다시 뽑는다.
- 2026-11-05 확인: 맛집 API가 536건으로 늘었다(적재는 471건). 다음 수집 때 신규로 들어간다.

## 검색·코스 (2단계 결정, 2026-11-04)
- **이동수단은 도보·택시만 쓴다.** 버스·대중교통은 카카오 API가 없고 추정 오차가 커서 제외했다. `mode`는 `auto`(기본: 보정 거리 ≤ `walk_max_km` 1.5km면 도보, 넘으면 택시) / `walk` / `taxi`다. 코스 계획은 모두 추정식(`services/route.py`)으로 하고, 택시 구간의 실측 시간과 요금은 화면 표시 시점에 카카오 자동차 길찾기로 조회한다(3단계 MapTool, 저장·캐시 안 함).
- `services/search.py`: `Conditions` dataclass + `search(cond)`. Chroma에서 metadata where(gu·category·indoor)로 거른 뒤 PG에서 활성 행만 붙인다. 후보 수는 `candidates_n × 일수`다. 후보가 부족하면 indoor → gu 순서로 조건을 풀고 안내문을 붙인다(open_hours 필터는 운영시간 파싱 전이라 아직 없음).
- `services/planner.py`: `score` → `select`(λ 다양성) → `best_order`(순열 전수, 같은 분류 3연속 금지) → 가용 시간 초과 시 최저점 제거. 후보가 한 분류에 몰려 3연속을 피할 순서가 없으면, 그 분류의 최저점 장소를 다른 분류 후보로 바꾼다(없으면 뺀다). 제약을 풀지는 않는다.
- 테스트 추적: TC-006 = `tests/test_planner.py::test_tc006_course_rules`(가용 4·6·9시간 × 분포 2종에서 2~6곳, 무작위 순서 100회 평균보다 짧은 동선, 가용 시간 이내, 3연속 없음). TC-014 = `test_tc014_multi_day`(날짜별 FR-006 충족, 전체 중복 없음, 비 오는 날 실내 비율 최대). 점수화·경로에 LLM을 쓰지 않는다는 검사 = `test_course_is_pure_code`. `dist` 항은 후보 전체 중심이 아니라 **유사도 1위 장소(앵커)에서의 거리**로 잰다. 하루 장소는 앵커 반경(도보 `walk_radius_km` 2.5, 그 외 `day_radius_km` 8) 안에서 고른다.
- 다일 일정: 점수 상위(필요 수 × 2)를 k-means로 나눈다(초기 중심은 상위 절반에서만). 그다음 각 그룹 1위 장소 반경 안의 남은 후보를 보태고, 강수확률이 높은 날에 실내 비율이 높은 그룹을 배정한다. 일정 전체에서 장소 중복은 없다.
- `build_course` 출력: `{"days": [{"day", "rain_prob", "items": [{order, place_id, name, category, gu, lat, lng, reason(None, 3단계 ResponseBuilder가 채움), leg_to_next{mode, minutes, meters, estimated}}]}], "notices": [...]}`.

## 에이전트 (3단계 결정, 2026-11-04)
- **LLM 모델:** `gemini-2.5-flash`는 신규 사용자에게 제공되지 않는다. config `llm.models` 순서대로 시도하고(`gemini-3.5-flash-lite` → `3.5-flash` → `3.8-flash`), 429·5xx면 다음 모델로 넘어간다(`services/llm.py`). 에이전트는 `LLMClient` 프로토콜(`chat`, `json`)에만 의존하고, `get_llm()`이 `LLM_PROVIDER`로 구현을 고른다.
- **Gemini 주의:** 함수 결과 메시지의 role은 `user`여야 한다(`tool`이면 400 오류). 모델 응답은 thought signature 보존을 위해 원본 `Content`를 그대로 되돌려 보낸다.
- **흐름 (LangGraph, `agent/orchestrator.py`):** parse → (되묻기·잡담이면 respond) → agent ⇄ tools(턴당 6회) → respond. 의도의 마지막 단계 도구(코스·수정은 `get_route_times`, 검색은 `search_places`)를 마치면 LLM에 되묻지 않고 바로 응답한다. LLM이 실패하면 `DEFAULT_PLAN` 순서로 도구를 실행한다(§7.4). 진입점은 `handle_chat(session_id, message)`이다.
- **도구:** `tools/` 아래 파일에 `@register`만 붙이면 자동 등록된다(`tools/__init__.py`). `BaseTool.call`은 시간 제한과 예외를 처리해 fallback으로 넘긴다. 도구는 상태(ctx)를 직접 고치지 않고 `ToolResult.updates`로 돌려준다.
- **QueryParser:** 코스 요청에서 지역(구)과 날짜가 없으면 되묻는다(둘 다 없으면 한 번에 둘 다 묻는다). "부산 전체", "어디든"이라고 하면 `region_any`로 보고 지역은 묻지 않는다. 원문을 세션 `pending`에 두었다가 다음 답과 이어 붙여 다시 해석한다. 장소 검색 의도는 지역 없이도 부산 전체로 찾는다. LLM이 실패하면 규칙 기반으로 간이 추출한다(이때는 묻지 않고 오늘·부산 전체로 진행).
- **ResponseBuilder:** 장소 정보는 DB에서 다시 읽고, LLM에는 `[[id]]` 마커만 쓰게 한다. `verify_places`가 후보 밖 id가 든 문장을 지우고, 남은 마커는 DB 이름으로 바꾼다. 출처는 장소별 제공처, 링크, 이용 조건에 기상청과 축제 출처를 더한다. 마커를 이름으로 바꿀 때 조사가 어색해질 수 있다(예: "해운대과").
- **날씨:** 단기예보(`getVilageFcst`). 검색 후보들의 중심 좌표를 기상청 격자로 바꿔 조회한다(`latlon_to_grid`, 후보가 없으면 부산시청 98·76). 캐시 키는 발표 시각과 격자다. 9~21시 강수확률 최댓값을 그날 값으로 쓴다. 5일 뒤 날짜는 예보 범위 밖이라 반영하지 않는다(중기예보 엔드포인트 없음).
- **카카오:** 택시 구간만 실측(시간·거리·요금)하고 저장·캐시하지 않는다. 결과 코드 102/103(출발지·도착지 주변 도로 없음, 산·계곡)은 추정치로 대체한다.
- **실측 응답시간:** 1일 코스 약 12초, 2박 3일 약 10~27초(LLM 왕복 5~6회, 무료 등급 429가 나면 늘어남).

## API·UI (4단계, 2026-11-05)
- `api/main.py`: API-001, 003~008. 오류는 모두 `{"error": {"code", "message"}}` 형식이다. 검증 오류는 400 INVALID_INPUT(필드 이름만, 값은 넣지 않음), 세션당 분당 20회를 넘으면 429다. 관리자 토큰은 `hmac.compare_digest`로 비교한다. 수집은 백그라운드 스레드에서 돌고 `run_id`로 조회하며, 동시에 하나만 실행한다(409). health는 DB·벡터 상태를 함께 돌려준다. API-002(사진)는 5단계다.
- `ui/app.py`: API만 호출한다. 선호 설정은 바뀔 때마다 PUT하고, 예시 질문 버튼, 날짜별 탭(지도 + 장소·구간 목록), 출처 펼치기, 관리자 수집 패널을 둔다. 지도는 카카오맵 JS SDK다(도보는 초록 점선, 택시는 파란 실선). 장소명은 `textContent`로 넣는다.
- **카카오맵 도메인 등록 필요:** 카카오 개발자 콘솔 → 앱 → 플랫폼 → Web 사이트 도메인에 `http://localhost:8501`(배포 시 그 주소)을 등록해야 한다. 등록하지 않으면 SDK가 401(domain mismatched)을 돌려주고, UI는 지도 칸에 안내 문구를 띄운다.
- **LLM 장애 대응:** 모든 모델이 실패(429·5xx·타임아웃)하면 `cooldown_s`(60초) 동안 LLM을 건너뛰고 규칙 기반 대체로 바로 처리한다(`services/llm.py`). 타임아웃은 10초다.
- **로컬 실행:** 미리보기 설정 `busanmate-api`(8000), `busanmate-ui`(8501)는 `GitHub/.claude/launch.json`에 있다. 이 PC는 스마트 앱 컨트롤을 사용자가 꺼서 psycopg DLL이 로드된다(켜져 있던 동안에는 차단됐음).

## 사진 탐색 (5단계, 2026-11-05)
- **모델:** 로컬 open_clip `ViT-B-32` / `laion2b_s34b_b79k`(config `embedding.image_*`)를 쓴다. PyTorch는 CPU 빌드다. 가중치는 첫 사용 때 HuggingFace에서 받는다. API 서버는 시작할 때(lifespan) 모델을 미리 불러온다.
- **색인:** `pipeline/images.py`가 `place_image` 중 `embedding_id`가 비어 있는 활성 장소 이미지를 내려받아 Chroma `place_image`(id=image_id, 메타 place_id)에 넣는다. `pipeline.run` 끝에서도 자동으로 돌고, `ingest_log`에는 source `place_image`로 남는다. 현재 636장(1장은 내려받기 실패).
- **`services/image.py`:** 매직바이트·10MB 검사(413/415) → CLIP → 상위 10장을 place_id별 최대 유사도로 묶는다(`rank_places`). 최고 유사도 ≥ τ면 `recognized`, 아니면 `similar`(Gemini `describe_image` 장면 설명 → 텍스트 검색 5곳). VLM이 실패하면 CLIP 상위 5곳을 그대로 쓴다. 업로드 사진은 메모리에서만 처리하고, 로그에는 크기·형식만 남긴다.
- **API-002** `POST /api/search/image`(multipart: session_id, file). 따뜻한 상태에서 약 3초 걸린다. UI에는 사이드바 "사진으로 찾기"와, 결과마다 "코스에 넣기" 버튼(채팅으로 추가 요청)이 있다.
- **평가:** `python -m eval.photo_eval <폴더>`(사진과 `labels.csv`(file,place_id), 색인에 없는 사진) → Top-1·Top-5·인식률. `--sanity`는 색인 원본의 썸네일이라 연결 점검용이다(Top-5 100%, 인식률 87.5%). 거의 같은 이미지도 12.5%가 τ=0.80 미만이었으므로, 실제 평가 사진으로 τ를 낮출지 검토해야 한다.

## 작업 순서
0. 뼈대: config 로더, `/api/health`, `db/schema.sql`, `docker-compose.yml`, requirements.txt. 빈 파일은 미리 만들지 않는다.
1. 데이터: 부산 3종 API 샘플로 필드 매핑 → DataPipeline(upsert, ingest_log) → 텍스트 임베딩 → Chroma
2. 검색·코스: SearchService, Scorer, RoutePlanner, 직선거리 추정 (pytest: 무작위 100회 평균보다 짧음, 3연속 금지)
3. 에이전트: LLMClient(Gemini), QueryParser, 도구 레지스트리, LangGraph(6회 제한), ResponseBuilder+verify_places, SessionStore, 도구 폴백
4. API·UI: API-001~008, 요청 한도, Streamlit + 카카오맵, 출처·기상청 표기
5. 사진: 이미지 수집(이용 조건 확인), CLIP, τ 판정, VLM 폴백, Top-5 평가

미결: CLIP 모델(PoC), 중기예보 엔드포인트 없음, 운영시간 파싱(현재 원문을 `open_hours.raw`에 저장), 규칙에 안 걸린 관광지 48곳의 indoor, 출처 원본 링크(HOMEPAGE_URL이 없는 장소가 많음).

## GitHub 규칙
- `main` 직접 푸시 금지. `feature/<기능>` 브랜치 → PR → 리뷰 1명 + CI(ruff, pytest, gitleaks).
- 커밋 접두어 `feat:` `fix:` `docs:` `test:`. PR 본문에 관련 FR/이슈 ID를 적는다.
