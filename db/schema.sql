-- BusanMate 스키마 (SDD §5.2). 재실행해도 안전하도록 IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS place (
    place_id     BIGSERIAL PRIMARY KEY,           -- LLM이 [[place_id]]로 인용
    source       VARCHAR(50)  NOT NULL,
    source_id    VARCHAR(100) NOT NULL,
    name         VARCHAR(200) NOT NULL,
    category     VARCHAR(50)  NOT NULL,
    sub_category VARCHAR(100),
    gu           VARCHAR(20)  NOT NULL,
    address      VARCHAR(300) NOT NULL,
    lat          DOUBLE PRECISION NOT NULL,       -- 좌표 없으면 적재 제외
    lng          DOUBLE PRECISION NOT NULL,
    description  TEXT,
    open_hours   JSONB,                           -- 파싱 실패 시 {"raw": "..."}
    indoor       BOOLEAN,                         -- 불명이면 NULL
    tel          VARCHAR(200),                    -- 번호 여러 개가 들어오기도 함 (최대 59자 확인)
    url          VARCHAR(500),
    license      VARCHAR(100) NOT NULL,
    fetched_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
    UNIQUE (source, source_id)                    -- 재수집 시 중복 0 (AC-007)
);
CREATE INDEX IF NOT EXISTS place_filter_idx ON place (gu, category) WHERE is_active;

CREATE TABLE IF NOT EXISTS place_image (
    image_id     BIGSERIAL PRIMARY KEY,
    place_id     BIGINT NOT NULL REFERENCES place (place_id) ON DELETE CASCADE,
    url          VARCHAR(500) NOT NULL,
    license      VARCHAR(100) NOT NULL,           -- 이용 조건 확인된 것만 (NFR-007)
    embedding_id VARCHAR(100),
    UNIQUE (place_id, url)
);

CREATE TABLE IF NOT EXISTS festival (
    festival_id  BIGSERIAL PRIMARY KEY,
    source_id    VARCHAR(100) NOT NULL UNIQUE,
    name         VARCHAR(200) NOT NULL,
    description  TEXT,
    start_date   DATE NOT NULL,
    end_date     DATE NOT NULL,
    lat          DOUBLE PRECISION,
    lng          DOUBLE PRECISION,
    address      VARCHAR(300),
    url          VARCHAR(500),
    license      VARCHAR(100) NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS festival_period_idx ON festival (start_date, end_date);

CREATE TABLE IF NOT EXISTS ingest_log (
    log_id       BIGSERIAL PRIMARY KEY,
    run_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       VARCHAR(50) NOT NULL,
    new_cnt      INT NOT NULL DEFAULT 0,
    updated_cnt  INT NOT NULL DEFAULT 0,
    skipped_cnt  INT NOT NULL DEFAULT 0,
    status       VARCHAR(20) NOT NULL,
    message      TEXT
);

-- 공유 링크·저장한 코스 (/?course=<share_id>, 읽기 전용). 서버가 세션의 직전 코스로 만든 값만 저장한다
CREATE TABLE IF NOT EXISTS shared_course (
    share_id   VARCHAR(16) PRIMARY KEY,
    payload    JSONB       NOT NULL,            -- {course, conditions, dates, sources}
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
