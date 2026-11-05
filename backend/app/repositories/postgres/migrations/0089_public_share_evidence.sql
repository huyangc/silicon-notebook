-- Local capability snapshots: not copied, archived or synchronized. No FK:
-- capability tokens span reports, notebook/global conversations and MCP links.
CREATE TABLE public_share_evidence (
    token TEXT NOT NULL,
    turn_key TEXT NOT NULL,
    reference_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    CONSTRAINT public_share_evidence_pkey PRIMARY KEY (token, turn_key, reference_key)
);
