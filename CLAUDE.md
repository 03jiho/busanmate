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
| API-001s | POST | `/api/chat/stream` | API-001과 같은 요청 → NDJSON: 단계 `{stage: parse|search|weather|route|respond, phase, t}` 줄들 + 마지막 `{result, t}` |
| API-002 | POST | `/api/search/image` | multipart(session_id, file) → `{mode, matches, sources, notices}` |
| API-003 | GET | `/api/session/{id}/preferences` | → `{theme, mode, companion}` |
| API-004 | PUT | `/api/session/{id}/preferences` | `{theme, mode, companion}` → `{ok}` |
| API-005 | GET | `/api/session/{id}/course` | → `{course}` |
| API-006 | POST | `/api/admin/ingest` (X-Admin-Token) | `{sources?}` → `{run_id}` |
| API-007 | GET | `/api/admin/ingest/{run_id}` | → `{status, new, updated, skipped}` |
| API-008 | GET | `/api/health` | → `{status}` |
| (편집) | PUT | `/api/session/{id}/course` | `{days: [[place_id…]…]}`(드래그·빼기·교체 결과) → `{course, sources, notices}` (이동 시간 재계산) |
| (편집) | GET | `/api/session/{id}/alternatives?day&index` | → `{alternatives: [같은 분류 3곳, 동선 덜 돌아가는 순]}` |
| (공유) | POST | `/api/session/{id}/share` | → `{share_id}` (세션 코스를 `shared_course`에 저장) |
| (공유) | GET | `/api/course/{share_id}` | → `{course, conditions, dates, sources}` (읽기 전용 `/?course=<id>`) |
| (저장) | POST | `/api/session/{id}/load/{share_id}` | 저장한 코스를 세션의 직전 코스로 불러옴 |

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
- 테스트 추적: TC-006 = `tests/test_planner.py::test_tc006_course_rules`(가용 4·6·9시간 × 분포 2종에서 2~6곳, 무작위 순서 100회 평균보다 짧은 동선, 가용 시간 이내, 3연속 없음). TC-014 = `test_tc014_multi_day`(날짜별 FR-006 충족, 전체 중복 없음, 비 오는 날 실내 비율 최대). 점수화·경로에 LLM을 쓰지 않는다는 검사 = `test_course_is_pure_code`. `dist` 항은 후보 전체 중심이 아니라 **유사도 1위 장소(앵커)에서의 거리**로 잰다. 하루 장소는 앵커 반경(`day_radius_km` 8) 안에서 고른다. 도보만이면 기준 장소 반경 `walk_radius_km`(2.5) 안에서 먼저 고르고, 모자라면 기준점에서 가까운 순으로 채운다(장소 수는 줄이지 않는다). 걸어서 `walk_max_leg_min`(30분)을 넘는 구간은 그 구간만 택시로 간다고 보고 `estimate_leg`가 택시로 바꾼 뒤 `walk_minutes`(걸으면 걸리는 시간)를 남긴다. 기준 장소는 걸어서 30분 안에 이웃이 있는 장소 중 점수 1위로 잡는다(1위가 외딴 곳일 때). 테마 "바다"는 해변 분류에 더해, 해변 장소 1km 안이거나 이름에 해안 키워드(태종대·흰여울·해양 등)가 있으면 가산점을 준다(config `theme_near`, 해변 좌표는 `theme_points`가 DB에서 읽어 BuildCourse가 넘긴다). 공원·자연 전체를 넣으면 장산·수목원이 섞여서 넣지 않았다. 하루 음식점 상한은 `food_per_day`(기본 2, 맛집 테마 3)이고, 맛집 테마에서도 3연속을 금지한다. 분류를 음식점 하나로만 직접 지정한 경우에만 상한을 두지 않는다. 분류를 여러 개 지정하면 섞어 달라는 뜻으로 보고 3연속 금지를 켠다. 띄어쓰기만 다른 같은 이름(원천 중복)은 한 번만 고른다.
- 다일 일정: 점수 상위(필요 수 × 2)를 k-means로 나눈다(초기 중심은 상위 절반에서만, 그룹 크기 상한 ⌈n/일수⌉로 [1, 23] 같은 쏠림 방지). 테마가 분류 하나에만 대응하면(맛집 → 음식점) 분류를 지정한 것으로 보고 3연속 제한을 두지 않는다(FR-006). 3연속 교체 후보는 그날 그룹 전체에서 동선에 가장 가까운 다른 분류 장소다. 그다음 각 그룹 1위 장소 반경 안의 남은 후보를 보태고, 강수확률이 높은 날에 실내 비율이 높은 그룹을 배정한다. 일정 전체에서 장소 중복은 없다.
- `build_course` 출력: `{"days": [{"day", "rain_prob", "items": [{order, place_id, name, category, gu, lat, lng, reason(None, 3단계 ResponseBuilder가 채움), leg_to_next{mode, minutes, meters, estimated}}]}], "notices": [...]}`.

## 에이전트 (3단계 결정, 2026-11-04)
- **LLM 모델:** `gemini-2.5-flash`는 신규 사용자에게 제공되지 않는다. config `llm.models` 순서대로 시도하고(`gemini-3.5-flash-lite` → `3.5-flash` → `3.8-flash`), 429·5xx면 다음 모델로 넘어간다(`services/llm.py`). 에이전트는 `LLMClient` 프로토콜(`chat`, `json`)에만 의존하고, `get_llm()`이 `LLM_PROVIDER`로 구현을 고른다.
- **Gemini 주의:** 함수 결과 메시지의 role은 `user`여야 한다(`tool`이면 400 오류). 모델 응답은 thought signature 보존을 위해 원본 `Content`를 그대로 되돌려 보낸다.
- **흐름 (LangGraph, `agent/orchestrator.py`):** parse → (되묻기·잡담이면 respond) → agent ⇄ tools(턴당 6회) → respond. 의도의 마지막 단계 도구(코스·수정은 `get_route_times`, 검색은 `search_places`)를 마치면 LLM에 되묻지 않고 바로 응답한다. LLM이 실패하면 `DEFAULT_PLAN` 순서로 도구를 실행한다(§7.4). 진입점은 `handle_chat(session_id, message)`이다.
- **도구:** `tools/` 아래 파일에 `@register`만 붙이면 자동 등록된다(`tools/__init__.py`). `BaseTool.call`은 시간 제한과 예외를 처리해 fallback으로 넘긴다. 도구는 상태(ctx)를 직접 고치지 않고 `ToolResult.updates`로 돌려준다.
- **QueryParser:** 코스 요청에서 지역(구)과 날짜가 없으면 되묻는다(둘 다 없으면 한 번에 둘 다 묻는다). "부산 전체", "어디든"이라고 하면 `region_any`로 보고 지역은 묻지 않는다. 원문을 세션 `pending`에 두었다가 다음 답과 이어 붙여 다시 해석한다. 장소 검색 의도는 지역 없이도 부산 전체로 찾는다. LLM이 실패하면 규칙 기반으로 간이 추출한다(이때는 묻지 않고 오늘·부산 전체로 진행).
- **이름으로 말한 장소:** 해석기가 `places`(사용자가 콕 집은 장소명)를 뽑는다. LLM이 실패하면 "○○ 꼭 넣어줘/포함/가고 싶어" 앞 어절과 메시지 속 DB 장소명으로 뽑는다. `match_names`(config `named_place`, 임계 0.8)가 DB와 대조하는데, 앞부분 일치("용두산" → 용두산공원)와 DB 이름 + 일반 접미어("해운대해수욕장" → 해운대)는 같은 장소로 본다. DB 이름이 말한 이름 안에 들어 있는 것만으로는 일치가 아니다. 찾은 곳은 `Conditions.must`로 넘겨 검색 후보 맨 앞에 넣고, 플래너가 반드시 포함한다(기준점, 연속 제한·시간 초과로도 빼지 않음). 지역을 안 말했으면 그 장소의 구를 지역으로 쓴다. 못 찾은 곳은 notices 맨 앞에 "'○○'는 공공데이터에서 찾지 못해 코스에서 제외했어요. 비슷한 장소: A, B"를 띄운다(유사도가 `similar_min` 미만이면 비슷한 장소는 생략). 응답 LLM이 실패하면 원인(예외 종류와 메시지, 키는 가림)을 로그에 남기고 `rule_summary`로 "A → B → C (장소 n곳, 도보 x구간·택시 y구간)"를 쓴다.
- **코스 제자리 수정:** 세션에 직전 코스(전체 일차)·조건·날짜(`last_course`, `last_conditions`, `last_dates`)를 둔다. 화면의 "코스 고치기" 버튼은 API-001에 `edit: {kind: cafe|indoor|shorter, day?}`를 보내고, 오케스트레이터는 문장 해석과 LLM 없이 `edit_course → get_route_times`만 실행한다. 대화로 고칠 때도 직전 조건·날짜를 그대로 쓴다. `edit_course`는 순서를 다시 정렬하지 않고 바꾼 자리만 고친 뒤 `items_for`로 이동 시간만 다시 계산한다. 카페는 카페가 없는 날 중 장소가 적은 날의 점심 무렵(`lunch_window` 안에 도착하는 식당 다음, 없으면 가운데 시각에 가까운 틈)에 우회 거리가 가장 짧은 카페를 넣는다. 실내로는 비 올 확률이 높은 날의 마지막 실외 장소를 같은 자리에서 실내(음식점·카페 제외)로 바꾸고, 짧게는 가장 긴 날에서 빼면 가장 많이 줄어드는 곳을 뺀다. 카페 분류는 맛집 데이터 중 이름·대표 메뉴 키워드로 정하되 대표 메뉴가 식사(`food_menu_keywords`)면 음식점이다(config `classify.cafe`, 30곳; 2026-11-12 '본 앤 브레드'(스테이크하우스) 오분류 수정).
- **식사 배치:** 식당·카페는 연달아 두지 않는다. 먹는 곳은 하루 장소의 절반 이하, 카페는 `cafe_per_day`(1) 이하다. 하루 체류 합이 `lunch_min_stay_min`(5시간) 이상이면 `lunch_window`(11:30~13:30)에 도착하는 식당 1곳을 `best_order`가 보장한다. 식당이 안 뽑혔으면 넣고, 다일 일정에서 그날 묶음에 없으면 다른 날에 안 쓴 가까운 식당을 쓴다. 택시 실측(get_route_times) 뒤 점심이 시간대를 벗어나면 `fit_lunch`가 출발 시각을 5분 단위(±60분)로 옮긴다. 먹는 곳만 고른 요청(분류가 음식점·카페뿐)은 이 규칙을 적용하지 않는다. 해석기가 "맛집 투어"를 `categories=[음식점]`으로 읽어도 테마와 같으면 지운다('식당만'처럼 '만'이 있을 때만 분류 필터). 바꿀 후보가 없어 하루 최소보다 줄어들게 되면 규칙을 풀고 알린다.
- **실내·비 오는 날:** "실내", "비 오는 날", "우천"은 indoor=true다. 검색은 실내 명소(`search.indoor_categories`, 문화시설)를 따로 찾아 앞에 두고, 그 구에 3곳 미만이면 다른 구까지 찾는다. 점수는 그 분류에 가산점을 준다. 실내 명소가 비식사 자리를 채우지 못하면 이유를 알린다. 분류를 지정하지 않은 검색에 식당이 적으면 점심용 식당 후보를 보탠다.
- **카드 문구:** 추천 이유가 비거나 장소명만 되풀이하면(원천 설명 첫 줄이 제목) DB 소개 중 장소명이 아닌 첫 줄을, 그것도 없으면 "주소에 있는 분류입니다."를 쓴다. 원본 제목의 쉼표는 `split_title`로 처리한다. 세 어절 이상인 홍보 문구는 떼고, 나눈 이름이 모두 분류 규칙에 걸리면 장소 여럿(둘째부터 source_id `원본-2`)으로 저장한다. 같은 좌표의 장소는 한 코스에 한 번만 넣는다.
- **화면 (2026-11-11):** 대기 중에는 `/api/chat/stream`의 실제 진행 단계(조건 해석 → 장소 검색 → 날씨 확인 → 동선 계산 → 요약)를 `st.status`로 보여 준다. 단계는 해석·응답 노드와 도구 호출(`get_stream_writer`)에서 나온다. 예상 시간 문구는 실측(코스 3.7~12.4초, 중앙값 5초)을 바탕으로 '보통 5~15초'로 했다. 새 답변은 `st.write_stream`으로 흘려 보여 주는데, verify_places를 거친 뒤의 문장이다. 지도는 folium이 아니라 카카오맵이다. Streamlit 탭은 보이지 않는 탭을 크기 0으로 그리므로, 지도 iframe 안에서 크기가 바뀌면(ResizeObserver, 처음 크기가 생길 때까지는 300ms 타이머로도 확인) `relayout()`(Leaflet의 invalidateSize에 해당) 뒤 `setBounds`로 다시 맞춘다. 폭 640px 이하에서는 지도 높이가 340px다. 부산이 아닌 지역(서울·제주·도쿄 등, '대구탕' 같은 음식명은 제외)은 intent `outside`다. 검색 없이 "BusanMate는 부산 전용이에요. 부산에서 비슷한 곳을 찾아드릴까요?"와 지역만 '부산'으로 바꾼 `suggest`를 돌려주고, 화면은 '부산에서 찾기'와 예시 질문 버튼을 함께 보여 준다. 원본 링크가 없는 카드는 공공데이터포털 서비스 페이지(config `sources.*.portal_url`, `weather.portal_url`)를 링크한다.
- **첫 화면 (2026-11-11):** 대화 전에는 `hero()`가 그린다. 슬로건 "말만 하면 부산 하루 코스가 완성돼요"와 보조 문구, 같은 크기의 시작 카드 2개(코스 짜기: "질문 입력하기"를 누르면 하단 입력창에 포커스 / 사진으로 장소 찾기: 본문 업로더), 3단계 안내(Material 아이콘), 예시 카드 3개가 있다. 예시 카드 미리보기는 실제 실행 결과다(`EXAMPLE_CARDS`, 데이터가 바뀌면 다시 확인). 그 밖에 '이런 결과가 나와요' 샘플 이미지(`ui/assets/sample-course.jpg`), '더 알아보기' 접이식, 출처 배지 3개가 있다. 대화가 시작되면 히어로를 숨기고 작은 제목과 "새 코스"(대화를 비우고 새 세션)만 둔다. 사진 업로더는 그때 사이드바로 옮긴다. 사이드바 옵션마다 한 줄 도움말이 있다(동행은 점수에 아직 쓰지 않는다고 적음). 640px 이하에서는 카드가 세로로 쌓이고 높이 맞춤을 푼다. 이모지 대신 Material 아이콘을 쓴다.
- **설명 근거 (2026-11-12):** 요약 LLM에는 장소의 이름·분류·구·주소·소개문 앞부분만 주고, 입력에 없는 메뉴·시설·역사·가격을 더하지 말라고 프롬프트에 적는다. `response.grounded`가 추천 이유의 핵심 낱말이 절반 넘게 원문에 없으면 소개문 첫 문장(`intro_line`)으로 바꾼다(`STATS.reason_ungrounded`). 카드에는 '원문 보기'로 공공데이터 소개문을 그대로 보인다(`source_of.original`). 카테고리·설명 모순 점검은 `python -m eval.category_check [20|all]`.
- **출처 줄:** 카드·검색 결과·기상청 출처는 `출처: 제공처 · 원본 링크 · 이용 조건` 한 형식이다. 원본 링크가 없으면 `원본 링크 없음 · 공공데이터포털 서비스 페이지`. 사진은 `사진 출처: 제공처 · 원본 이미지 · 이용 조건`. PDF도 같은 형식(`export.source_text`).
- **비 오는 날 시간대:** 기상청 시간별 강수확률(`weather.HOURLY`, `rain_hours_by_day`)이 있으면 비 오는 날 순서를 정할 때 야외 장소 체류 시간대의 강수확률 × `rain_hour_weight_km`를 거리 비용에 더해 강수확률이 낮은 시간대로 옮긴다. 그 장소 앞뒤 도보 구간에는 '비 예보가 있어 택시 추천'을 붙인다(`leg.rain_taxi`).
- **체류 시간·추천 문구 (2026-11-12):** 장소별 체류는 `course.stay_min_by_category`(해변·거리 35, 문화시설 75, 음식점 60, 카페 40 …)와 이름 키워드 `stay_min_by_name`(미술관·박물관 90, 전망대 40)으로 정한다(`planner.stay_of`). '더 짧게'는 그날 체류 시간에 `shorter_ratio`(0.75)를 곱하고(하한 `min_stay_min` 20분), 모두 하한이면 한 곳을 뺀다. 드래그·교체해도 줄인 체류는 유지된다. '왜 이곳?'은 맞은 조건(요청한 장소·지역·테마·실내/날씨·식사 시간·앞 장소에서 걸어서)만 쓰고 없으면 생략한다. 요약은 조건 1개 이상을 넣어야 하며(`response.mentions_condition`), 없으면 조건 한 줄을 붙인 규칙 기반 요약으로 바꾼다.
- **비 오는 날 편성·택시 제안:** 비 오는 날은 시간별 예보가 없어도 가능하면 실내로 시작한다(`rain_first_outdoor_km`). 택시 제안은 도보 `walk_taxi_hint_min`(10분) 이상, 비 오는 날 야외 장소 앞뒤는 `walk_no_taxi_max_min`(5분) 초과일 때만. 예산 상자에 제안 구간을 모두 타면 늘어나는 택시비(`budget.taxi_hint_extra`)를 한 줄로 보인다.
- **교체 후보 거리:** '다른 곳으로 교체'는 같은 분류 중 우회 `alt_max_detour_km`(8km) 안인 곳만 낸다. 없으면 '근처에 같은 분류의 다른 후보가 없어요'로 안내한다(다른 구로 35km 돌아가는 후보가 나오던 문제).
- **요약·알림 상자 (2026-11-12):** 코스 요약은 날씨(강수확률 `summary_rain_min` 30% 이상인 날이 있을 때)·테마·동선 특징 중 해당 항목 2개 이상을 넣고, 비 예보(60% 이상)면 우산·실내 위주 문장을 넣어야 한다(`response.summary_ok`). 못 지키면 조건 줄을 붙인 규칙 기반 요약으로 바꾼다. 결과 위의 알림은 날씨가 코스를 바꾼 것(`WEATHER_NOTICE`: 강수확률·비 예보·우산)만 펼쳐 보이고, 나머지(예보 범위 밖·도보 예상치 등)는 접힌 '알림 N개' 하나로 묶는다.
- **분류 목록·매핑:** 분류 목록은 `classify.categories`, 매핑은 `classify.rules`(위에서부터 처음 맞는 규칙). 섬·공원·산책로(동백섬, 해안 산책로, 볼레길, 둘레길, 해변공원, 이름이 '섬'으로 끝남)는 해변보다 먼저 공원·자연으로 잡는다. 기존 DB 5곳을 정정했다.
- **사진 없는 카드:** 분류 아이콘이 든 회색 4:3 자리표시와 '사진 없음'을 넣어 사진 있는 카드와 높이·정렬을 맞춘다.
- **분류 다양성:** 한 분류는 하루 장소의 `max_category_share`(절반)까지, 같은 분류 3연속 금지. 구를 지정한 요청(분류 지정 없음)은 같은 구의 `mix_categories`(카페·시장·거리·마을·공원·자연·관광지, 비 오는 날은 야외 제외)를 유사도 최저값으로 후보에 보태 섞는다. 그래도 넘치면 "근처에 다른 종류 후보가 부족해 …" 한 줄로 알린다.
- **비 오는 날·식사 시간대 (2026-11-11):** 비 올 확률이 `rain_prob_threshold`(60%) 이상인 날은 야외 장소(`weather.outdoor_categories`·이름/설명 키워드, `planner.is_outdoor`)의 점수를 `rain_outdoor_penalty`만큼 깎고 하루 `rain_outdoor_max`(2곳)까지만 둔다. 넘치면 같은 구 실내 명소(먹는 곳 제외)로 바꾸고, 바꿀 곳이 없으면 남기고 카드에 '비 올 때는 우산 필요'를 단다. 식당은 `lunch_window`·`dinner_window`에 도착할 때만 두고(`slots_ok`, 첫 장소는 식당 아님), 맞출 수 없으면 최저점 식당을 먹는 곳이 아닌 곳으로 바꾸며, 바꿀 곳이 없어 하루 최소보다 줄면 시간대 규칙을 풀고 알린다. 다일 일정에서 한 날 묶음에 먹는 곳 아닌 장소가 모자라면 여유 있는 날에서 가까운 곳을 옮겨 온다. `walk_taxi_hint_min`(15분)을 넘는 도보 구간에는 택시 예상 시간을 함께 보인다. 없는 장소의 비슷한 후보는 이름 유사도와 요청 맥락 분류(`named_place.context_categories`)로 점수를 매겨 `similar_min` 미만은 숨긴다. 긴 설명은 문장 경계에서 자르고 잘렸을 때만 '…'을 붙인다(`response.clip`).
- **직접 편집·공유·근거·예산 (2026-11-11):** 카드 드래그 정렬(`streamlit-sortables`), 빼기, 다른 곳으로 교체는 채팅을 거치지 않는다. 날짜별 place_id 순서를 PUT하면 `course_ops.apply_order`가 순서를 그대로 둔 채 `items_for` → get_route_times(택시 실측·fit_lunch) → `enrich`로 다시 만든다. 교체 후보는 같은 분류에서 앞뒤 장소 사이 우회 거리가 짧은 순으로 고르고, 코스에 있는 곳과 같은 장소(`same_place`: 공백만 다른 이름·같은 좌표)는 뺀다. 공유·저장은 서버가 세션 코스로 만든 payload만 `shared_course`에 넣는다(질문 원문 없음, 만료 없음). '저장한 코스' 목록은 브라우저 세션(`st.session_state`)에만 있다. `agent/explain.enrich`가 카드마다 `why`(요청한 장소·테마(점수와 같은 판정: 분류·키워드·해변 근처)·실내/비 예보·점심 시간·앞 장소에서 걸어서), `sourced`(출처 확인 수), `estimate_basis`('예상' 툴팁), `budget`(`services/budget.py`: 식비 1인은 설명의 대표 메뉴 가격, 없으면 config 기본 단가 / 택시 1대는 카카오 실측, 없으면 부산 요금표 초안)를 붙인다. 화면은 사이드바의 1인 예산과 비교해 경고한다.
- **ResponseBuilder:** 장소 정보는 DB에서 다시 읽고, LLM에는 `[[id]]` 마커만 쓰게 한다. `verify_places`가 후보 밖 id가 든 문장을 지우고, 남은 마커는 DB 이름으로 바꾼다. 출처는 장소별 제공처, 링크, 이용 조건에 기상청과 축제 출처를 더한다. 마커를 이름으로 바꿀 때 조사가 어색해질 수 있다(예: "해운대과").
- **날씨:** 단기예보(`getVilageFcst`). 검색 후보들의 중심 좌표를 기상청 격자로 바꿔 조회한다(`latlon_to_grid`, 후보가 없으면 부산시청 98·76). 캐시 키는 발표 시각과 격자다. 9~21시 강수확률 최댓값을 그날 값으로 쓴다. 5일 뒤 날짜는 예보 범위 밖이라 반영하지 않는다(중기예보 엔드포인트 없음).
- **카카오:** 택시 구간만 실측(시간·거리·요금)하고 저장·캐시하지 않는다. 결과 코드 102/103(출발지·도착지 주변 도로 없음, 산·계곡)은 추정치로 대체한다.
- **실측 응답시간:** 1일 코스 약 12초, 2박 3일 약 10~27초(LLM 왕복 5~6회, 무료 등급 429가 나면 늘어남).

## API·UI (4단계, 2026-11-05)
- `api/main.py`: API-001, 003~008. 오류는 모두 `{"error": {"code", "message"}}` 형식이다. 검증 오류는 400 INVALID_INPUT(필드 이름만, 값은 넣지 않음), 세션당 분당 20회를 넘으면 429다. 관리자 토큰은 `hmac.compare_digest`로 비교한다. 수집은 백그라운드 스레드에서 돌고 `run_id`로 조회하며, 동시에 하나만 실행한다(409). health는 DB·벡터 상태를 함께 돌려준다. API-002(사진)는 5단계다.
- `ui/app.py`: API만 호출한다. 선호 설정은 바뀔 때마다 PUT하고, 예시 질문 버튼, 날짜별 탭(지도 + 장소·구간 목록), 출처 펼치기, 관리자 수집 패널을 둔다. 지도는 카카오맵 JS SDK다(도보는 초록 점선, 택시는 파란 실선). 장소명은 `textContent`로 넣는다.
- **카카오맵 도메인 등록 필요:** 카카오 개발자 콘솔 → 앱 → 플랫폼 → Web 사이트 도메인에 `http://localhost:8501`(배포 시 그 주소)을 등록해야 한다. 등록하지 않으면 SDK가 401(domain mismatched)을 돌려주고, UI는 지도 칸에 안내 문구를 띄운다.
- **LLM 장애 대응 (2026-11-11 개정):** 해석 단계가 한 번의 JSON 응답에 조건과 도구 계획(`steps`, 스키마는 `tools.base.steps_schema`)을 함께 준다. 계획이 없으면 도구 선택을 구조화 출력(JSON 스키마) 한 번으로 따로 묻고, 실패하면 기본 순서(DEFAULT_PLAN)로 한다. 함수 호출 왕복은 쓰지 않는다. 호출은 스레드에서 하고 질문 예산(`request_budget_s` 20초 − `reserve_s`)만큼만 기다린다(Gemini HTTP 기한 하한 `min_timeout_s` 10초와 별개). 남은 시간이 `min_wait_s`보다 적으면 부르지 않는다. 429·503은 `retry_429_backoff_s` 뒤 같은 모델로 한 번 다시, 그다음 모델로. 모든 모델이 429·5xx로 실패했을 때만 `cooldown_s` 동안 건너뛴다(타임아웃·예산 부족은 차단기 없음). 실패는 `services.llm.fallback(stage, e)`로 원인(timeout·429·auth·model·request·5xx·parse·cooldown·budget)과 응답 본문 앞 200자(`scrub`: 키 가림)를 로그에 남기고 `STATS`에 단계별(parser·agent·response) 호출·대체 횟수로 센다. `/api/health`의 `llm`과 관리자 화면(`?admin`)의 디버그 영역에서 본다. 사용자에게는 `FALLBACK_NOTICE`("추천 이유를 간단히 표시했어요.") 한 줄만 보인다.
- **로컬 실행:** 미리보기 설정 `busanmate-api`(8000), `busanmate-ui`(8501)는 `GitHub/.claude/launch.json`에 있다. 이 PC는 스마트 앱 컨트롤을 사용자가 꺼서 psycopg DLL이 로드된다(켜져 있던 동안에는 차단됐음).

## 사진 탐색 (5단계, 2026-11-05)
- **모델:** 로컬 open_clip `ViT-B-32` / `laion2b_s34b_b79k`(config `embedding.image_*`)를 쓴다. PyTorch는 CPU 빌드다. 가중치는 첫 사용 때 HuggingFace에서 받는다. API 서버는 시작할 때(lifespan) 모델을 미리 불러온다.
- **색인:** `pipeline/images.py`가 `place_image` 중 `embedding_id`가 비어 있는 활성 장소 이미지를 내려받아 Chroma `place_image`(id=image_id, 메타 place_id)에 넣는다. `pipeline.run` 끝에서도 자동으로 돌고, `ingest_log`에는 source `place_image`로 남는다. 현재 636장(1장은 내려받기 실패).
- **`services/image.py`:** 매직바이트·10MB 검사(413/415) → CLIP → 상위 10장을 place_id별 최대 유사도로 묶는다(`rank_places`). 최고 유사도 ≥ τ면 `recognized`, 아니면 `similar`(Gemini `describe_image` 장면 설명 → 텍스트 검색 5곳). VLM이 실패하면 CLIP 상위 5곳을 그대로 쓴다. 업로드 사진은 메모리에서만 처리하고, 로그에는 크기·형식만 남긴다.
- **API-002** `POST /api/search/image`(multipart: session_id, file). 따뜻한 상태에서 약 3초 걸린다. UI에는 사이드바 "사진으로 찾기"와, 결과마다 "코스에 넣기" 버튼(채팅으로 추가 요청)이 있다.
- **평가:** `python -m eval.photo_eval <폴더>`(사진과 `labels.csv`(file,place_id), 색인에 없는 사진) → Top-1·Top-5·인식률. `--sanity`는 색인 원본의 썸네일이라 연결 점검용이다(Top-5 100%, 인식률 87.5%). 거의 같은 이미지도 12.5%가 τ=0.80 미만이었으므로, 실제 평가 사진으로 τ를 낮출지 검토해야 한다.

## 수용 기준 점검 (2026-11-10)
- `python -m eval.acceptance`(실행 중인 API: AC-001·002·004·009 + 시나리오), `python -m eval.acceptance --outage`(AC-006: 외부 키를 그 프로세스에서만 무효화), `python -m eval.conditions_eval`(AC-003, `eval/queries.jsonl`은 Claude가 만든 초안이라 팀 검토 필요). AC-005(미사용 사진)와 AC-008(사용자 5명)은 사람·데이터가 필요하다.
- Gemini는 요청 기한이 10초보다 짧으면 400으로 거부한다. 그래서 모델마다 기한은 항상 10초이고, 남은 예산이 10초 미만이면 다음 모델을 건너뛴다.
- LLM이 모두 실패하면: 검색은 키워드 대체(`_keyword_query`), 추천 이유는 DB 설명 첫 줄, 코스 수정은 규칙 기반 해석으로 처리한다.

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
