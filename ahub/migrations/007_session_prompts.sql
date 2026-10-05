-- The prompt layers used by a worker/reviewer session (T133): the canonical
-- summary string as built by prompts.build_summary. Additive: old code
-- ignores the column, new code reads "" when it is missing.
ALTER TABLE session ADD COLUMN prompts TEXT NOT NULL DEFAULT '';
