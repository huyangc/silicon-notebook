-- Paper-card cache. Mirrors SQLite v104 / _migration_104.
--
-- source_paper_cards holds at most one lazily generated card per source (design
-- docs/superpowers/specs/2026-10-28-paper-cards-find-sources-design_zh.md
-- section 4.4). basis is the source's element-generation witness
-- (MAX(source_elements.created_at)) rendered as a UTC ISO string, so a change
-- of the session time zone never invalidates the cards; a card is valid only
-- while status = 'ready' and basis and card_version match the current ones, so
-- re-parsing or a prompt upgrade invalidates it without cleanup code.
-- attempts counts consecutive failures (reset on success) for the retry
-- cooldown. A pure cache: not copied, exported, imported or synced. No
-- backfill: cards are generated on demand.
CREATE TABLE source_paper_cards (
  source_id text COLLATE "C" NOT NULL,
  notebook_id text COLLATE "C" NOT NULL,
  basis text COLLATE "C" NOT NULL,
  card_version integer NOT NULL,
  status text COLLATE "C" NOT NULL,
  card jsonb NOT NULL DEFAULT '{}'::jsonb,
  raw_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  model text COLLATE "C" NOT NULL DEFAULT '',
  attempts integer NOT NULL DEFAULT 0,
  created_at timestamp with time zone NOT NULL,
  updated_at timestamp with time zone NOT NULL,
  CONSTRAINT pk_source_paper_cards PRIMARY KEY (source_id)
);

ALTER TABLE source_paper_cards
  ADD CONSTRAINT fk_source_paper_cards_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

ALTER TABLE source_paper_cards
  ADD CONSTRAINT fk_source_paper_cards_source_id__sources
  FOREIGN KEY (source_id) REFERENCES sources (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

CREATE INDEX idx_source_paper_cards_nb ON source_paper_cards (notebook_id);
