# CodeLens: how data flows through the system

This page explains the whole system with diagrams, from the big picture down to every step. Read it top to bottom: each diagram zooms into one box of the previous one.

| # | Diagram | Question it answers |
|---|---|---|
| 1 | [The big picture](#1-the-big-picture) | Who talks to whom? |
| 2 | [The three main flows](#2-the-three-main-flows) | What does the system do, and where is data kept? |
| 3 | [Flow A: indexing a repository](#3-flow-a-indexing-a-repository) | How does code become searchable? |
| 4 | [Flow B: asking a question](#4-flow-b-asking-a-question) | How is an answer found, written and checked? |
| 5 | [Flow C: reviewing a pull request](#5-flow-c-reviewing-a-pull-request) | How are review comments produced and verified? |
| 6 | [The GitHub Action](#6-the-github-action) | How do comments reach a pull request? |
| 7 | [What is stored](#7-what-is-stored) | What is in the database? |
| 8 | [What runs where](#8-what-runs-where) | Which containers, models and ports? |

GitHub draws the diagrams below by itself. PNG copies of each one are in [`docs/diagrams/`](diagrams/) for slides or places that do not render Mermaid (regenerate them with `make diagrams` after editing).

**Colour key used in every diagram:** blue = a step the system performs, green = stored data, orange = the language model (an outside service), grey = outside systems and people, red = a place where the request can be refused or stopped.

---

## 1. The big picture

```mermaid
%%{init: {"theme": "base", "themeVariables": {"fontSize": "15px", "lineColor": "#6b7280"}, "flowchart": {"wrappingWidth": 420, "nodeSpacing": 50, "rankSpacing": 90, "htmlLabels": true}}}%%
flowchart LR
    user(["<b>User</b><br/>browser or API client"]):::ext

    subgraph codelens ["CodeLens (Docker Compose)"]
        direction TB
        app["<b>app</b> container<br/>web UI + API (FastAPI)<br/>local AI models: embeddings, reranker"]:::proc
        db[("<b>db</b> container<br/>Postgres + pgvector<br/>repos, chunks, vectors")]:::store
    end

    github[("<b>GitHub</b><br/>public repositories, pull requests<br/>and the GitHub Action")]:::ext
    groq[("<b>Groq</b><br/>language model")]:::llm

    user <-->|"index a repo, ask, review<br/>answers and review comments"| app
    app <-->|"SQL"| db
    app <-->|"clone repos, fetch PR diffs<br/>the Action sends a diff, gets comments"| github
    app <-->|"a few code snippets in<br/>an answer or comments out"| groq

    classDef ext fill:#eceff4,stroke:#6b7280,color:#111;
    classDef proc fill:#dbe7ff,stroke:#2456d6,color:#111;
    classDef store fill:#dcf5e3,stroke:#1b6e3c,color:#111;
    classDef llm fill:#ffe8cc,stroke:#b45309,color:#111;
```

**How to read it.** The user (or the GitHub Action) only ever talks to the `app` container. The app is the only part that talks to the database, to GitHub and to the language model. The embeddings and the reranker run **inside** the app container on the CPU, so they cost nothing and need no network. Only the final answer or review is written by the outside language model, and it only ever sees a handful of retrieved code snippets, never the whole repository.

---

## 2. The three main flows

```mermaid
%%{init: {"theme": "base", "themeVariables": {"fontSize": "15px", "lineColor": "#6b7280"}, "flowchart": {"wrappingWidth": 420, "nodeSpacing": 28, "rankSpacing": 40, "htmlLabels": true}}}%%
flowchart TB
    subgraph once ["Prepare: once per repository (flow A)"]
        direction LR
        src[("GitHub<br/>source code")]:::ext --> cut["Cut the code into<br/>small chunks"]:::proc --> vec["Give every chunk a<br/>meaning vector (embedding)"]:::proc
    end

    store[("<b>Database</b><br/>each chunk: its code, its place (file and lines),<br/>its meaning vector and its words")]:::store

    subgraph every ["Use: on every request (flows B and C)"]
        direction LR
        input(["<b>Input</b><br/>B: a question<br/>C: a diff or a pull request"]):::ext --> find["<b>Find</b> the few chunks<br/>that matter<br/>(by meaning and by words)"]:::proc
        find --> ask["<b>Ask the language model</b><br/>with only those chunks"]:::llm
        ask --> verify["<b>Check</b> what it says<br/>against the code it saw"]:::proc
        verify --> output(["<b>Output</b><br/>B: answer with citations<br/>C: review comments"]):::ext
    end

    vec -->|"save"| store
    store -->|"search"| find

    classDef ext fill:#eceff4,stroke:#6b7280,color:#111;
    classDef proc fill:#dbe7ff,stroke:#2456d6,color:#111;
    classDef store fill:#dcf5e3,stroke:#1b6e3c,color:#111;
    classDef llm fill:#ffe8cc,stroke:#b45309,color:#111;
    classDef err fill:#fde8e7,stroke:#b3261e,color:#111;
```

**The idea in one paragraph.** Flow A prepares the code once: it cuts it into pieces and stores each piece together with a "meaning fingerprint" (a vector). Flows B and C then use that store as a search engine. When you ask a question, or when a pull request changes some code, CodeLens first *finds the few pieces that matter* and only then asks the language model, handing it exactly those pieces. This is called retrieval-augmented generation (RAG), and it is why answers can point at real lines of code.

---

## 3. Flow A: indexing a repository

`POST /repos` with `{"url": "https://github.com/owner/repo"}`. It takes seconds for a small repo and a minute or two for a large one.

```mermaid
%%{init: {"theme": "base", "themeVariables": {"fontSize": "15px", "lineColor": "#6b7280"}, "flowchart": {"wrappingWidth": 420, "nodeSpacing": 28, "rankSpacing": 40, "htmlLabels": true}}}%%
flowchart TB
    req(["<b>POST /repos</b><br/>a GitHub repository URL"]):::ext
    s0["<b>0. Check the URL</b><br/>only plain GitHub repo URLs are accepted<br/>(no local files, other protocols or git options)"]:::proc
    s1["<b>1. Clone</b><br/>shallow git clone, 120 s timeout, never asks for a password<br/>then reject trees over 200 MB"]:::proc
    s2["<b>2. Walk the files</b><br/>keep source and text files<br/>skip .git, node_modules, symlinks, lock files, binaries,<br/>files over 200 KB, files that are not UTF-8"]:::proc
    s3["<b>3. Chunk</b><br/>Python: one chunk per function, class header, method<br/>and module-level block (using ast)<br/>Other files: 60-line windows, 10 lines overlap<br/>Any chunk over 120 lines is split. More than 5000 chunks: refuse"]:::proc
    s4["<b>4. Reuse what is already stored</b><br/>an unchanged chunk keeps its vector<br/>key = path + symbol + content hash"]:::proc
    s5["<b>5. Embed only the new chunks</b><br/>text = path + symbol + code, local model, 384 numbers each<br/>sorted by length, batches of 8, retries with backoff"]:::proc
    s6["<b>6. Save in ONE transaction</b><br/>lock the repo name, upsert the repo row (commit sha),<br/>insert new chunks, delete chunks that no longer exist"]:::proc
    out(["<b>Response</b><br/>chunks, embedded, reused, deleted"]):::ext
    db[("<b>Database</b><br/>repos, chunks")]:::store
    gh[("GitHub")]:::ext

    e0["422 invalid_input"]:::err
    e1["502 repo_clone_failed<br/>(not found, private, timeout)"]:::err
    e3["422 repo_too_large"]:::err

    req --> s0 --> s1 --> s2 --> s3 --> s4 --> s5 --> s6 --> out
    gh --> s1
    db -.->|"existing vectors"| s4
    s6 --> db
    s0 -.->|"not valid"| e0
    s1 -.->|"fails"| e1
    s3 -.->|"too many"| e3

    classDef ext fill:#eceff4,stroke:#6b7280,color:#111;
    classDef proc fill:#dbe7ff,stroke:#2456d6,color:#111;
    classDef store fill:#dcf5e3,stroke:#1b6e3c,color:#111;
    classDef llm fill:#ffe8cc,stroke:#b45309,color:#111;
    classDef err fill:#fde8e7,stroke:#b3261e,color:#111;
```

**Why each step exists**

| Step | Why |
|---|---|
| URL check | The URL comes from a user, so only `https://github.com/owner/repo` is accepted. This blocks local files, other protocols and git options. |
| Shallow clone, size and chunk caps | Keep one request from running for hours or filling the disk. The temporary clone is always deleted. |
| Chunk by function, not by line count | A function is one idea. Retrieval works better on whole ideas, and the stored line numbers become exact citations. |
| Embed `path + symbol + code` | A method body alone does not say which file or class it belongs to. The header lets a question about "login" match `auth/service.py` even if the body never says it. The stored text (used for citations) stays the raw code. |
| Reuse by content hash | Re-indexing an unchanged repo embeds **0** chunks (about 1 s instead of about 5 s). Code that only moved to other lines keeps its embedding. |
| One transaction | Searches never see a half-updated repo, two indexing runs of the same repo cannot delete each other's rows, and a failure leaves the old data untouched. |

---

## 4. Flow B: asking a question

`POST /ask` with `{"repo": "owner/repo", "question": "Where is auth handled?"}`. Typically 2 to 5 seconds, most of it the language model.

```mermaid
%%{init: {"theme": "base", "themeVariables": {"fontSize": "15px", "lineColor": "#6b7280"}, "flowchart": {"wrappingWidth": 420, "nodeSpacing": 28, "rankSpacing": 40, "htmlLabels": true}}}%%
flowchart TB
    req(["<b>POST /ask</b><br/>repo + question"]):::ext
    v["<b>0. Validate</b><br/>question not empty, at most 1000 characters<br/>API key set, repo is indexed"]:::proc
    s1["<b>1. Embed the question</b><br/>same local model that embedded the chunks"]:::proc
    s2a["<b>2a. Search by meaning</b><br/>nearest vectors (cosine), top 30, this repo only"]:::proc
    s2b["<b>2b. Search by words</b><br/>exact words and names (full-text), top 30"]:::proc
    s3["<b>3. Merge the two rankings</b><br/>Reciprocal Rank Fusion, keep the best 20"]:::proc
    s4["<b>4. Rerank</b><br/>a cross-encoder reads question and chunk together<br/>keep the best 6 (if it fails, keep the merged order)"]:::proc
    s5["<b>5. Build the prompt</b><br/>rules + the 6 snippets, each headed with its place<br/>for example [src/auth.py:L10-L42] (AuthService.login)"]:::proc
    llm[("<b>Language model</b><br/>30 s timeout, retries on rate limits and outages")]:::llm
    s6["<b>6. Verify the citations</b><br/>every cited path and line range must match<br/>one of the 6 snippets that were shown"]:::proc
    out(["<b>Response</b><br/>answer, verified citations,<br/>how many made-up references were removed"]):::ext
    db[("<b>Database</b><br/>chunks")]:::store

    e0["422 empty or too long<br/>503 no API key<br/>404 repo not indexed"]:::err
    refuse["Answer: I could not find this in the indexed code<br/>(no language model call is made)"]:::err

    req --> v --> s1
    s1 --> s2a & s2b
    s2a & s2b --> s3
    db --> s2a
    db --> s2b
    s3 --> s4 --> s5 --> llm --> s6 --> out
    v -.->|"refused"| e0
    s3 -.->|"nothing found"| refuse
    refuse --> out

    classDef ext fill:#eceff4,stroke:#6b7280,color:#111;
    classDef proc fill:#dbe7ff,stroke:#2456d6,color:#111;
    classDef store fill:#dcf5e3,stroke:#1b6e3c,color:#111;
    classDef llm fill:#ffe8cc,stroke:#b45309,color:#111;
    classDef err fill:#fde8e7,stroke:#b3261e,color:#111;
```

**Why each step exists**

| Step | Why |
|---|---|
| Two searches (2a and 2b) | They fail in opposite ways. Meaning search finds "where is login handled" even if the word *login* is absent, but blurs exact names. Word search finds `DATABASE_URL` exactly but has no idea about synonyms. Measured: on questions about settings and names, keyword search beat vector search (0.75 vs 0.29 on the 2 such questions in the evaluation, a small sample); hybrid had the best top-3 overall. |
| Reciprocal Rank Fusion | The two searches score on different scales, so their scores cannot be added. Their *ranks* can: a chunk near the top of both lists wins. |
| Rerank | The first searches compare question and chunk separately, which is fast but approximate. A cross-encoder reads them together, which is precise but too slow for every chunk, so it only sees the 20 candidates. (Its benefit is not yet proven in the evaluation; see `DECISIONS.md` #19.) |
| Citation check | A language model can invent a believable `file.py:L10-L42`. Only references that match code it was actually shown are kept, and the response says how many were removed. |
| Refusal without a model call | If nothing relevant exists, the honest answer costs no tokens and cannot be a guess. |

---

## 5. Flow C: reviewing a pull request

`POST /review` with `{"repo": "owner/repo", "diff": "..."}` or `{"pr_url": "https://github.com/owner/repo/pull/7"}`. The repo must already be indexed: the reviewer uses its code as context. The review is a **loop** (a LangGraph): it handles one file per pass.

```mermaid
%%{init: {"theme": "base", "themeVariables": {"fontSize": "15px", "lineColor": "#6b7280"}, "flowchart": {"wrappingWidth": 420, "nodeSpacing": 28, "rankSpacing": 40, "htmlLabels": true}}}%%
flowchart TB
    req(["<b>POST /review</b><br/>a diff, or a pull request URL"]):::ext
    fetch["<b>0. Get the diff</b><br/>for a PR URL: fetch the .diff from github.com (15 s timeout, 200 KB cap)<br/>then check: diff size, API key set, repo indexed"]:::proc
    parse["<b>1. parse_diff</b><br/>read the unified diff<br/>skip docs, lock files, binaries, deleted files and pure deletions<br/>print every line with its NEW line number"]:::proc

    subgraph loop ["Loop: one file per pass"]
        direction TB
        pick["<b>2. pick_file</b><br/>take the next file, or stop<br/>(after 8 files or 90 seconds)"]:::proc
        ctx["<b>3. retrieve_context</b> (searches the database)<br/>search the repo with the added code, by meaning and by words<br/>drop chunks that are the OLD version of the changed lines<br/>keep the top 5"]:::proc
        rev["<b>4. review</b> (language model, quick pass)<br/>diff + related code go in, JSON comments come out<br/>at most 5 per file, low reasoning effort"]:::llm
        l1["<b>5. self_check, layer 1: rules</b><br/>the line must be an added line of the diff<br/>drop repeats, keep only evidence that was really retrieved"]:::proc
        l2["<b>6. self_check, layer 2: second look</b> (language model)<br/>judges the main problem of each comment:<br/>supported, partly (kept with a note) or unsupported (dropped)"]:::llm
        pick --> ctx --> rev --> l1 --> l2
        l2 -.->|"next file"| pick
    end

    out(["<b>Response</b><br/>comments (most severe first), files reviewed and skipped,<br/>comments rejected, errors, partial flag"]):::ext
    e0["422 not a diff, or too large<br/>503 no API key, 404 repo not indexed<br/>502 pull request not found"]:::err

    req --> fetch --> parse --> pick
    pick ==>|"no files left, or a limit was reached"| out
    fetch -.->|"refused"| e0
    parse -.->|"nothing reviewable"| out

    classDef ext fill:#eceff4,stroke:#6b7280,color:#111;
    classDef proc fill:#dbe7ff,stroke:#2456d6,color:#111;
    classDef store fill:#dcf5e3,stroke:#1b6e3c,color:#111;
    classDef llm fill:#ffe8cc,stroke:#b45309,color:#111;
    classDef err fill:#fde8e7,stroke:#b3261e,color:#111;
```

**Why each step exists**

| Step | Why |
|---|---|
| Numbered diff lines, then check the number | Models miscount lines in a raw diff. We print the numbers, the model copies one, and layer 1 verifies that line really is an addition. A wrong line number can no longer reach you. |
| Drop the old version of the changed code | The index holds the code *before* the pull request. Searching with the changed code would mostly return that same old code, calling it "related" and pushing out the callers and neighbours that actually help. |
| Quick pass, then a verifying pass | The first pass is cheap (about 100 to 250 tokens instead of thousands) but sometimes states things that are false. The second pass reads the code again and removes those. In the evaluation each layer removed a different false claim on a harmless change, and neither removed a real bug. |
| Three verdicts (`partly`) | A comment can be right about the problem but wrong about a side remark. Dropping it would throw away a true finding, so it is kept with the checker's caveat. |
| One file per pass, with a budget | Small prompts, one failing file cannot sink the rest, and the file and time limits live in one place. A stopped review is flagged `partial`. |

**If a language-model call fails:** that file is reported in `errors` and the review continues. Only if *every* file fails does the request fail (502). If only the second-look call fails, the comments are returned marked `checked: false` instead of being silently deleted.

---

## 6. The GitHub Action

How review comments reach a pull request. (This needs CodeLens deployed on a public address; GitHub's runners cannot reach `localhost`. You can try it locally with `--dry-run`, see `docs/RUNNING.md`, scenario 10.)

```mermaid
sequenceDiagram
    autonumber
    participant Dev as Developer
    participant GH as GitHub
    participant Run as Action runner<br/>(post_review.py)
    participant CL as CodeLens server
    Dev->>GH: open or update a pull request
    GH->>Run: start the workflow (skipped for forks: no secrets)
    Run->>GH: read the PR diff (GITHUB_TOKEN)
    Run->>CL: POST /review {repo, diff}
    CL-->>Run: comments, stats (flow C)
    Run->>GH: read comments already on the PR
    Note over Run: skip any comment whose hidden<br/>fingerprint is already there
    Run->>GH: post ONE review with inline comments<br/>(path, line, side RIGHT)
    alt GitHub rejects the inline positions (422)
        Run->>GH: post one plain comment listing the findings instead
    end
    Note over Run: any problem (CodeLens down, GitHub error)<br/>is logged and the job still succeeds:<br/>a review helper never blocks a merge
```

---

## 7. What is stored

```mermaid
erDiagram
    repos ||--o{ chunks : "has many"
    repos {
        bigint id PK
        text name "owner/repo (unique)"
        text url
        text commit_sha "the commit that was indexed"
        timestamptz ingested_at
    }
    chunks {
        bigint id PK
        bigint repo_id FK
        text path "file path in the repo"
        text symbol "function or class name, or empty"
        text kind "function, class, method, module, window"
        int start_line "1-based, used for citations"
        int end_line
        text content "the raw code"
        text content_hash "lets re-indexing skip unchanged code"
        vector_384 embedding "meaning fingerprint (pgvector)"
        tsvector fts "words of path + symbol + code (generated)"
    }
```

| Index | Used for |
|---|---|
| HNSW on `embedding` (cosine) | The "search by meaning" step (2a) |
| GIN on `fts` | The "search by words" step (2b) |
| B-tree on `(repo_id, path)` | Looking up a file's chunks |
| Unique `(repo_id, path, start_line, content_hash)` | Re-indexing unchanged code is a no-op |

Nothing else is stored. Questions, answers and reviews are **not** saved: each request is answered and forgotten. Deleting a repo row deletes its chunks.

---

## 8. What runs where

```mermaid
%%{init: {"theme": "base", "themeVariables": {"fontSize": "15px", "lineColor": "#6b7280"}, "flowchart": {"wrappingWidth": 420, "nodeSpacing": 40, "rankSpacing": 80, "htmlLabels": true}}}%%
flowchart LR
    browser(["<b>Browser</b><br/>the web UI"]):::ext
    pgadmin(["<b>pgAdmin or psql</b><br/>(optional)"]):::ext

    subgraph compose ["Docker Compose, on your computer or a server"]
        direction TB
        app["<b>app</b> container, port 8000<br/>uvicorn + FastAPI: web UI at /, API docs at /docs<br/>runs as a non-root user<br/>models baked into the image:<br/>embeddings bge-small-en-v1.5 (384 numbers), reranker MiniLM"]:::proc
        db[("<b>db</b> container, port 5432 (this computer only)<br/>Postgres 16 + pgvector<br/>data in the volume pgdata")]:::store
        app <-->|"internal network"| db
    end

    github[("<b>GitHub</b>")]:::ext
    groq[("<b>Groq</b><br/>language model")]:::llm

    browser -->|"localhost:8000"| app
    pgadmin -->|"127.0.0.1:5432"| db
    app --> github
    app --> groq

    classDef ext fill:#eceff4,stroke:#6b7280,color:#111;
    classDef proc fill:#dbe7ff,stroke:#2456d6,color:#111;
    classDef store fill:#dcf5e3,stroke:#1b6e3c,color:#111;
    classDef llm fill:#ffe8cc,stroke:#b45309,color:#111;
```

| Fact | Detail |
|---|---|
| Everything runs in Docker | One `Dockerfile` (stages: base, test, runtime) and one `docker-compose.yml`. |
| Settings | Environment variables only (`.env`): database password, `GROQ_API_KEY`, model names, limits. No secrets in the image or the repo. |
| Models are baked into the image | The container starts in under a second and needs no internet for embeddings or reranking. |
| The only paid-quota dependency | The language model (Groq free tier: 200,000 tokens a day, 8,000 a minute). Everything else is local. |
| If the database is down | `/health` still answers, `/ready` and the data endpoints return a clear 503, and the app recovers by itself when the database returns. |

---

### Where each step lives in the code

| Step | File |
|---|---|
| Clone, URL check | `src/codelens/ingestion/cloner.py` |
| Walk and chunk | `ingestion/walker.py`, `ingestion/chunker.py` |
| Embeddings | `src/codelens/embeddings.py` |
| Save (transaction, reuse, delete stale) | `src/codelens/store.py`, `src/codelens/indexer.py` |
| Hybrid search and fusion | `src/codelens/retrieval.py` |
| Rerank | `src/codelens/reranker.py` |
| Ask: prompt, citations | `src/codelens/answering.py`, `src/codelens/llm.py` |
| Review: diff, graph, checks | `src/codelens/review/diff.py`, `graph.py`, `checks.py`, `prompts.py` |
| GitHub Action | `scripts/post_review.py`, `.github/workflows/codelens-review.yml` |
| Web UI | `src/codelens/static/` |
| Database schema | `db/init.sql` |
