-- Apply once to an existing ScoreChat database created before symbolic layers.
-- Fresh databases receive the same tables from db/schema.sql.

CREATE TABLE IF NOT EXISTS score_sources (
    id          SERIAL PRIMARY KEY,
    work_id     INT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    format      TEXT NOT NULL DEFAULT 'humdrum-kern',
    file_path   TEXT NOT NULL,
    source_url  TEXT,
    sha256      TEXT NOT NULL,
    raw_content TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (work_id, sha256)
);

CREATE TABLE IF NOT EXISTS score_measures (
    id             SERIAL PRIMARY KEY,
    work_id        INT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    measure_index  INT NOT NULL,
    measure_number INT NOT NULL,
    symbolic_data  JSONB NOT NULL,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (work_id, measure_index)
);

CREATE TABLE IF NOT EXISTS measure_analyses (
    id               SERIAL PRIMARY KEY,
    measure_id       INT NOT NULL REFERENCES score_measures(id) ON DELETE CASCADE,
    analysis_version TEXT NOT NULL,
    analysis_data    JSONB NOT NULL,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (measure_id, analysis_version)
);

CREATE INDEX IF NOT EXISTS score_measures_work_index_idx
    ON score_measures (work_id, measure_index);
CREATE INDEX IF NOT EXISTS measure_analyses_measure_id_idx
    ON measure_analyses (measure_id);
