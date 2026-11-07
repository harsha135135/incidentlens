-- At most one schema-repair model request per job (spec §8, §14): for a malformed action or a
-- report that cites unknown evidence. A second failure ends in the deterministic fallback report.
ALTER TABLE investigations ADD COLUMN repairs_used integer NOT NULL DEFAULT 0 CHECK (repairs_used BETWEEN 0 AND 1);
