CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id     TEXT PRIMARY KEY,
    object_key      TEXT NOT NULL,
    mime_type       TEXT NOT NULL,
    filename_hint   TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    chat_id         TEXT NOT NULL,
    tool_name       TEXT,
    size_bytes      INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_artifacts_chat ON artifacts(chat_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_user ON artifacts(user_id);
