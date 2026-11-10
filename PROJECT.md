# CodeLens: Codebase Q&A and PR Reviewer

Portfolio project for a Senior AI Engineer profile (agentic workflows, RAG, FastAPI, pgvector).
Upload this file to the Claude project knowledge and keep it current.

## Goal

1. Ingest a GitHub repo and answer questions like "where is auth handled?" with `path:L10-L42` citations.
2. Review a PR diff with a LangGraph agent that uses retrieved code as context.
3. Show measured retrieval quality (eval table in the README), not just a demo.

Demo data: repos under the `arupbiswas-tech` GitHub account.

## Non-goals (for now)

- Languages other than Python get plain line-window chunks, no AST.
- Only public GitHub repos (https URLs). No private repos or tokens yet.
- No auth, multi-tenancy, or billing.
- No fine-tuning. Embeddings and the LLM are used as-is.

## Hard requirements

- **Everything runs in Docker.** One `Dockerfile` (multi-stage: base, test, runtime) and one `docker-compose.yml` (app + Postgres/pgvector). No "install X locally" steps beyond Docker itself.
- **Simple, self-explanatory code.** Small functions, type hints, one-line docstrings, descriptive names. No clever abstractions.
- **Real error handling.** Custom exceptions mapped to JSON errors, timeouts on every network or DB call, retries with backoff on LLM and embedding calls, no bare `except`, no leaked stack traces to clients.
- **Config only via environment variables** (pydantic-settings). No secrets in the image or repo.
- **Tests run in Docker** (`make test`). Ruff lint must pass.

## Stack

Python 3.12, uv, FastAPI, psycopg 3 + pool, Postgres 16 + pgvector, stdlib `ast` (Python chunking; tree-sitter only if we add other languages),
fastembed (local ONNX embeddings, 384 dims, no API cost), Groq via OpenAI-compatible client (swappable),
LangGraph, pytest, Ruff, GitHub Actions.

## Architecture

```
GitHub repo -> git clone -> AST parser -> chunks (function / class / method / module)
  -> embeddings -> pgvector table (+ Postgres full-text column)

Question -> hybrid retrieve (vector + FTS) -> rerank -> LLM answer with file:line citations

PR diff -> LangGraph: parse diff -> retrieve related code -> review -> groundedness check -> comments
```

## Data model (see `db/init.sql`)

- `repos(id, name, url, commit_sha, ingested_at)`
- `chunks(id, repo_id, path, symbol, kind, start_line, end_line, content, content_hash, embedding vector(384), fts)`
- Unique on `(repo_id, path, start_line, content_hash)` so re-ingesting unchanged code is a no-op.

## API (planned)

| Method | Path | Milestone |
|---|---|---|
| GET | `/health` (liveness, no DB) | 1 |
| GET | `/ready` (readiness, checks DB + pgvector) | 1 |
| POST | `/repos` (ingest a repo by URL) | 3 |
| POST | `/ask` (question -> answer + citations) | 4 |
| POST | `/review` (diff -> review comments) | 6 |

## Milestones

- [x] 1. Scaffold: uv, Dockerfile, Compose with pgvector, config, logging, error handling, health/ready, tests
- [x] 2. Ingestion: clone repo, walk files, AST chunking for Python (stdlib `ast`), line-window fallback. Try it: `make ingest URL=https://github.com/owner/repo`
- [x] 3. Embed and store: batch embeddings, upsert by content hash, `POST /repos`
- [x] 4. Retrieval and Q&A: vector + FTS, rerank, answer with citations, `POST /ask`
- [x] 5. Evaluation: 25-30 hand-written questions, hit@k and faithfulness, results table in README
- [x] 6. LangGraph PR reviewer: diff parsing, context retrieval, review, self-check, `POST /review`
- [ ] 7. UI + deploy: small front end, GitHub Action for PR comments, live demo, README GIF
  - [x] web UI (served by the app), `GET /repos`, README GIF and screenshots, GitHub Action script and workflow (verified locally)
  - [ ] deploy: demo protection (admin token, rate limits, daily budget), production compose + HTTPS, docs/DEPLOY.md, live URL, run the Action on this repo

## Working agreement for Claude chats

- One chat per milestone. Start by reading this file and `DECISIONS.md`.
- Do not jump ahead of the current milestone.
- Explain trade-offs briefly. I am learning RAG, so say why, not just what.
- End each milestone with tests, a README update, and updated copies of this file and `DECISIONS.md`.
- Push back if a choice would hurt retrieval quality or break the "simple code" rule.
