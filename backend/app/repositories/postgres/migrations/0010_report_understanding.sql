-- Persist the pre-retrieval deep-report question understanding, ambiguity
-- questions, user answers, and confirmation state.

ALTER TABLE reports
  ADD COLUMN IF NOT EXISTS understanding_json jsonb NOT NULL DEFAULT '{}'::jsonb;
