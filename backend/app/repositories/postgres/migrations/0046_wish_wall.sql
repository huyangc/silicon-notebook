-- Global wish wall: users submit bug reports and feature requests, while
-- administrators publish update plans. Votes are limited to one per user by
-- the composite primary key and disappear with their parent wish.

CREATE TABLE wishes (
  id text COLLATE "C" NOT NULL,
  kind text COLLATE "C" NOT NULL,
  title text NOT NULL,
  content text NOT NULL,
  author_id text COLLATE "C" NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  CONSTRAINT pk_wishes PRIMARY KEY (id)
);

CREATE INDEX idx_wishes_kind_created
  ON wishes(kind, created_at DESC, id DESC);

CREATE TABLE wish_votes (
  wish_id text COLLATE "C" NOT NULL REFERENCES wishes(id) ON DELETE CASCADE,
  user_id text COLLATE "C" NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL,
  CONSTRAINT pk_wish_votes PRIMARY KEY (wish_id, user_id)
);

CREATE INDEX idx_wish_votes_user ON wish_votes(user_id, wish_id);
