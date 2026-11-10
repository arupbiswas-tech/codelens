-- Runs once when the Postgres volume is first created.
-- Changing this file later requires a migration (or `docker compose down -v` in dev).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS repos (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,          -- e.g. "arupbiswas-tech/smartinbox-ai"
    url         TEXT NOT NULL,
    commit_sha  TEXT,                          -- commit that was ingested
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id           BIGSERIAL PRIMARY KEY,
    repo_id      BIGINT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,                -- file path inside the repo
    symbol       TEXT,                         -- function/class name, NULL for window chunks
    kind         TEXT NOT NULL CHECK (kind IN ('function', 'class', 'method', 'module', 'window')),
    start_line   INTEGER NOT NULL,             -- 1-based, inclusive (used for citations)
    end_line     INTEGER NOT NULL,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,                -- lets re-ingestion skip unchanged chunks
    embedding    vector(384),                  -- must match the embedding model (see DECISIONS.md #3)
    fts          tsvector GENERATED ALWAYS AS (
                     to_tsvector(
                         'simple',
                         -- Punctuation becomes spaces so "src/auth.py" and "Greeter.hello"
                         -- are searchable by "auth" or "hello"; otherwise they stay one token.
                         regexp_replace(path || ' ' || coalesce(symbol, ''), '[^A-Za-z0-9]+', ' ', 'g')
                         || ' ' || content
                     )
                 ) STORED,
    CHECK (start_line >= 1 AND end_line >= start_line),
    UNIQUE (repo_id, path, start_line, content_hash)
);

-- Semantic search (cosine distance) and keyword search.
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_fts_idx ON chunks USING gin (fts);
CREATE INDEX IF NOT EXISTS chunks_repo_path_idx ON chunks (repo_id, path);
