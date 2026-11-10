"""FastAPI entry point: app creation, startup/shutdown, and the HTTP endpoints."""

import logging
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from codelens.answering import Answerer
from codelens.config import Settings, get_settings
from codelens.db import check_database, create_pool
from codelens.embeddings import Embedder
from codelens.errors import register_error_handlers
from codelens.indexer import index_repo
from codelens.llm import LLMClient
from codelens.logging_setup import setup_logging
from codelens.reranker import Reranker
from codelens.review.github import resolve_review_input
from codelens.review.graph import Reviewer
from codelens.store import list_repos

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the DB pool on startup and close it on shutdown.

    If the database is unreachable, startup fails with a clear message instead of
    leaving a half-working app running.
    """
    settings = get_settings()
    setup_logging(settings.log_level)

    logger.info("Starting CodeLens, connecting to the database...")
    app.state.pool = create_pool(settings)
    logger.info("Database pool ready.")
    try:
        # Loaded once: model loading takes seconds, and the dimension check fails fast.
        logger.info("Loading embedding model %s...", settings.embedding_model)
        app.state.embedder = Embedder(
            settings.embedding_model, settings.embedding_dim, settings.embedding_batch_size
        )
        logger.info("Embedding model ready.")
        llm = build_llm(settings)
        app.state.answerer = build_answerer(settings, app.state.pool, app.state.embedder, llm)
        app.state.reviewer = Reviewer(app.state.pool, app.state.embedder, llm, settings)
        yield
    finally:
        app.state.pool.close()
        logger.info("Database pool closed.")


def build_llm(settings: Settings) -> LLMClient | None:
    """The language model client, or None when no API key is set (allowed, see config)."""
    api_key = settings.groq_api_key.get_secret_value() if settings.groq_api_key else ""
    if not api_key:
        logger.warning("GROQ_API_KEY is not set: POST /ask and POST /review will answer 503.")
        return None
    return LLMClient(
        api_key=api_key,
        model=settings.groq_model,
        base_url=settings.groq_base_url,
        timeout=settings.llm_timeout,
        max_retries=settings.llm_max_retries,
    )


def build_answerer(
    settings: Settings, pool: ConnectionPool, embedder: Embedder, llm: LLMClient | None
) -> Answerer:
    """Create the question-answering pieces around the shared language model client."""
    reranker = Reranker(settings.reranker_model) if settings.rerank_enabled else None
    return Answerer(pool, embedder, reranker, llm, settings)


STATIC_DIR = Path(__file__).parent / "static"

# The page loads only its own files. These headers tell the browser to refuse anything else
# (inline scripts, other sites), which limits the damage if untrusted text ever got through.
PAGE_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

app = FastAPI(title="CodeLens", version="0.1.0", lifespan=lifespan)
register_error_handlers(app)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def add_page_security_headers(request: Request, call_next):
    """Add the security headers to the UI page and its assets (not to the API docs page,
    which loads scripts from elsewhere)."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers.update(PAGE_SECURITY_HEADERS)
    return response


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    """The web UI."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    """Liveness: the process is up. Never touches the database."""
    return {"status": "ok"}


class IndexRepoRequest(BaseModel):
    url: str = Field(min_length=1, max_length=300, examples=["https://github.com/owner/repo"])


class IndexRepoResponse(BaseModel):
    repo: str
    commit_sha: str
    chunks: int
    embedded: int
    reused: int
    deleted: int


@app.post("/repos")
def index_repository(body: IndexRepoRequest, request: Request) -> IndexRepoResponse:
    """Clone a public GitHub repo and index it. Calling it again only re-embeds changes."""
    settings = get_settings()
    result = index_repo(
        body.url,
        pool=request.app.state.pool,
        embedder=request.app.state.embedder,
        max_chunks=settings.max_chunks_per_repo,
        clone_timeout=settings.clone_timeout,
    )
    return IndexRepoResponse(
        repo=result.repo, commit_sha=result.commit_sha, **asdict(result.summary)
    )


class AskRequest(BaseModel):
    repo: str = Field(min_length=1, max_length=200, examples=["owner/repo"])
    question: str = Field(min_length=1, max_length=5000, examples=["Where is auth handled?"])


class CitationOut(BaseModel):
    path: str
    start_line: int
    end_line: int
    symbol: str | None


class AskResponse(BaseModel):
    answer: str
    citations: list[CitationOut]  # verified: each one is a chunk the model was shown
    ungrounded_citations: int  # citations the model wrote that matched no retrieved chunk


@app.post("/ask")
def ask(body: AskRequest, request: Request) -> AskResponse:
    """Answer a question about an indexed repo, with `path:L10-L42` citations."""
    answer = request.app.state.answerer.ask(body.repo, body.question)
    return AskResponse(
        answer=answer.text,
        citations=[
            CitationOut(
                path=hit.path, start_line=hit.start_line, end_line=hit.end_line, symbol=hit.symbol
            )
            for hit in answer.citations
        ],
        ungrounded_citations=answer.ungrounded_citations,
    )


class RepoOut(BaseModel):
    name: str
    url: str
    commit_sha: str | None  # the commit that was indexed; citations link to it
    ingested_at: datetime
    chunks: int


@app.get("/repos")
def repositories(request: Request) -> list[RepoOut]:
    """The indexed repositories, so a client can offer them in a list."""
    return [RepoOut(**asdict(repo)) for repo in list_repos(request.app.state.pool)]


class ReviewRequest(BaseModel):
    """Send `diff` together with `repo`, or just a `pr_url` (a public GitHub pull request)."""

    repo: str | None = Field(default=None, max_length=200, examples=["owner/repo"])
    diff: str | None = Field(default=None, examples=["diff --git a/app.py b/app.py ..."])
    pr_url: str | None = Field(
        default=None, max_length=300, examples=["https://github.com/o/r/pull/7"]
    )


class ReviewCommentOut(BaseModel):
    path: str
    line: int  # a line number in the NEW version of the file
    severity: str  # "high", "medium" or "low"
    body: str
    evidence: list[str]  # verified references to related code, like "src/auth.py:L10-L42"
    checked: bool  # False if the self-check could not run for this comment
    note: str  # the checker's caveat when the main problem is real but a side remark is not


class ReviewResponse(BaseModel):
    comments: list[ReviewCommentOut]
    files_reviewed: int
    files_skipped: dict[str, int]  # reason -> number of files
    dropped: dict[str, int]  # comments the checks rejected: reason -> number
    errors: list[str]  # per-file problems that did not stop the review
    partial: bool  # True if a limit stopped the review before every file was covered


@app.post("/review")
def review(body: ReviewRequest, request: Request) -> ReviewResponse:
    """Review a pull request diff against an indexed repo, using the repo's code as context."""
    settings = get_settings()
    repo, diff = resolve_review_input(body.repo, body.diff, body.pr_url, settings.max_diff_bytes)
    result = request.app.state.reviewer.run(repo, diff)
    return ReviewResponse(
        comments=[ReviewCommentOut(**asdict(comment)) for comment in result.comments],
        files_reviewed=result.files_reviewed,
        files_skipped=result.files_skipped,
        dropped=dict(Counter(item.reason for item in result.dropped)),
        errors=result.errors,
        partial=result.partial,
    )


@app.get("/ready")
def ready(request: Request) -> dict:
    """Readiness: the database and pgvector are usable. Returns 503 if not."""
    pool: ConnectionPool = request.app.state.pool
    check_database(pool)
    return {"status": "ready"}
