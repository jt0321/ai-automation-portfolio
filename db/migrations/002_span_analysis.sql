-- Apply once to databases that already have the symbolic-layer migration.

CREATE TABLE IF NOT EXISTS analysis_runs (
    id SERIAL PRIMARY KEY,
    work_id INT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    analyzer_name TEXT NOT NULL,
    analyzer_version TEXT NOT NULL,
    configuration_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_sha256 TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS span_analyses (
    id SERIAL PRIMARY KEY,
    work_id INT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    analysis_run_id INT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    measure_start_index INT NOT NULL,
    measure_end_index INT NOT NULL,
    measure_start INT NOT NULL,
    measure_end INT NOT NULL,
    span_type TEXT NOT NULL DEFAULT 'candidate' CHECK (span_type IN ('candidate','phrase','theme','variation','transition')),
    label TEXT,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','accepted','rejected')),
    evidence_data JSONB NOT NULL,
    features_data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CHECK (measure_start_index <= measure_end_index)
);

CREATE TABLE IF NOT EXISTS span_relations (
    id SERIAL PRIMARY KEY,
    analysis_run_id INT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    source_span_id INT NOT NULL REFERENCES span_analyses(id) ON DELETE CASCADE,
    target_span_id INT NOT NULL REFERENCES span_analyses(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('repeats','varies','inverts','diminishes','augments','changes_meter_from','contrasts_with')),
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','accepted','rejected')),
    evidence_data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS analysis_runs_work_created_idx ON analysis_runs (work_id, created_at);
CREATE INDEX IF NOT EXISTS span_analyses_work_measure_idx ON span_analyses (work_id, measure_start_index, measure_end_index);
CREATE INDEX IF NOT EXISTS span_analyses_run_idx ON span_analyses (analysis_run_id);
CREATE INDEX IF NOT EXISTS span_relations_span_idx ON span_relations (source_span_id, target_span_id);
