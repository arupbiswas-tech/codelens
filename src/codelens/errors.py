"""Application errors and the handlers that turn them into JSON responses.

Raise a CodeLensError subclass anywhere in the code. The handlers below make sure
the client always gets a small JSON body and never a stack trace.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class CodeLensError(Exception):
    """Base class for errors we expect and know how to explain to the client."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class DatabaseUnavailableError(CodeLensError):
    status_code = 503
    code = "database_unavailable"


class NotFoundError(CodeLensError):
    status_code = 404
    code = "not_found"


class InvalidInputError(CodeLensError):
    status_code = 422
    code = "invalid_input"


class RepoCloneError(CodeLensError):
    """Cloning a repository failed (not found, private, timeout, git missing)."""

    status_code = 502
    code = "repo_clone_failed"


class RepoTooLargeError(CodeLensError):
    """The repo produced more chunks than we are willing to embed in one request."""

    status_code = 422
    code = "repo_too_large"


class EmbeddingError(CodeLensError):
    """The embedding model could not be loaded or failed while embedding."""

    status_code = 503
    code = "embedding_failed"


class RerankError(CodeLensError):
    """The reranking model could not be loaded or failed (answers fall back to no rerank)."""

    status_code = 503
    code = "rerank_failed"


class LLMError(CodeLensError):
    """The language model call failed (timeout, rate limit, outage, bad key)."""

    status_code = 502
    code = "llm_failed"


class LLMNotConfiguredError(CodeLensError):
    """No GROQ_API_KEY is set, so questions cannot be answered."""

    status_code = 503
    code = "llm_not_configured"


class DiffTooLargeError(CodeLensError):
    """The submitted diff is bigger than we are willing to review in one request."""

    status_code = 422
    code = "diff_too_large"


class DiffFetchError(CodeLensError):
    """Downloading a pull request's diff from GitHub failed."""

    status_code = 502
    code = "diff_fetch_failed"


class ReviewFailedError(CodeLensError):
    """Every file of the review failed (for example the language model was down)."""

    status_code = 502
    code = "review_failed"


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def register_error_handlers(app: FastAPI) -> None:
    """Attach handlers for our own errors and a safe catch-all for everything else."""

    @app.exception_handler(CodeLensError)
    async def handle_known_error(_request: Request, exc: CodeLensError) -> JSONResponse:
        logger.warning("%s: %s", exc.code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=_error_body(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def handle_bad_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Same JSON shape as every other error; name the bad fields, never echo the input back.
        fields = ", ".join(sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()}))
        return JSONResponse(
            status_code=422,
            content=_error_body("invalid_input", f"Invalid request: check {fields}."),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Log the full traceback for us, but tell the client nothing internal.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=_error_body("internal_error", "Something went wrong on our side."),
        )
