"""One retry-with-backoff helper shared by the embedding and reranking models."""

import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # tries before giving up
BACKOFF_SECONDS = 0.5  # wait 0.5s, then 1s, between tries


def run_with_retry[T](
    call: Callable[[], T],
    give_up_error: Callable[[], Exception],
    what: str,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run `call`; on any failure wait and try again, then raise `give_up_error()`.

    `what` names the operation in the log. The original exception is chained, so it shows
    in our logs but never reaches the client.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return call()
        except Exception as exc:  # model runtimes raise many unrelated types (e.g. out of memory)
            logger.warning("%s failed (try %d/%d): %s", what, attempt, MAX_ATTEMPTS, exc)
            if attempt == MAX_ATTEMPTS:
                raise give_up_error() from exc
            sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
    raise AssertionError("unreachable")  # the loop always returns or raises
