-- Notated sections from Humdrum expansion records (*>[A,A,B]) are spans like
-- any other, but they are engraved structure rather than derived candidates,
-- so they carry their own span_type.
ALTER TABLE span_analyses DROP CONSTRAINT IF EXISTS span_analyses_span_type_check;
ALTER TABLE span_analyses ADD CONSTRAINT span_analyses_span_type_check
    CHECK (span_type IN ('candidate','section','phrase','theme','variation','transition'));
