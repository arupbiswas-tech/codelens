"""Application settings, read from environment variables (and an optional .env file)."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Required: the app refuses to start without it, with a clear validation error.
    database_url: str = Field(min_length=1)

    log_level: str = "INFO"

    # Connection pool and timeouts (seconds). Every DB call is bounded by these.
    db_pool_min_size: int = Field(default=1, ge=1)
    db_pool_max_size: int = Field(default=5, ge=1)
    db_connect_timeout: float = Field(default=10.0, gt=0)
    db_statement_timeout: float = Field(default=30.0, gt=0)  # max seconds for any one query

    # Embeddings. The dimension must match vector(384) in db/init.sql; the app checks this
    # at startup. The Dockerfile pre-downloads this same model, so keep the two in step.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = Field(default=384, ge=1)
    # Small batches waste less padding on variable-length code (8 beat 64 by ~2x in testing).
    embedding_batch_size: int = Field(default=8, ge=1)

    # Retrieval for POST /ask: each search returns `search_candidates`, the merged list is cut
    # to `fused_candidates`, the reranker keeps the best `context_chunks` for the prompt.
    search_candidates: int = Field(default=30, ge=1)
    fused_candidates: int = Field(default=20, ge=1)
    context_chunks: int = Field(default=6, ge=1)
    rerank_enabled: bool = True  # switch off to measure what reranking adds (milestone 5)
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    max_question_chars: int = Field(default=1000, ge=1)

    # LLM (Groq, through its OpenAI-compatible API). Without a key the app still starts and
    # indexes repos; only POST /ask answers 503.
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-20b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_timeout: float = Field(default=30.0, gt=0)  # seconds per request
    llm_max_retries: int = Field(default=3, ge=0)  # the SDK retries 429s, 5xx and timeouts
    # Model that judges answers in the evaluation. Empty = the same model that wrote them,
    # which is biased towards itself (see evaluation/judge.py); set another Groq model to avoid it.
    eval_judge_model: str | None = None

    # PR reviewer (POST /review). The caps bound cost and time: each reviewed file costs about
    # two LLM calls, and the free Groq tier allows ~200k tokens a day.
    review_max_files: int = Field(default=8, ge=1)  # files reviewed per request
    review_max_added_lines: int = Field(default=300, ge=1)  # bigger files are skipped
    review_context_chunks: int = Field(default=5, ge=0)  # related code chunks shown per file
    review_max_comments_per_file: int = Field(default=5, ge=1)
    # How hard the reasoning model thinks. Writing comments is the cheap first pass; the check
    # is the verifying pass. Measured: "low" answers in ~100-250 tokens instead of thousands
    # but makes more false claims, which is what the check is for. Empty = the model default.
    review_reasoning_effort: str | None = "low"
    review_check_reasoning_effort: str | None = "medium"
    review_max_output_tokens: int = Field(default=4000, ge=100)
    review_deadline_seconds: float = Field(default=90.0, gt=0)  # stop starting new files after this
    max_diff_bytes: int = Field(default=200_000, ge=1)

    # Limits for POST /repos, so one request cannot run for hours or fill the database.
    max_chunks_per_repo: int = Field(default=5000, ge=1)
    clone_timeout: float = Field(default=120.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    """Load settings once and reuse them."""
    return Settings()
