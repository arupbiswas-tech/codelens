"""One-line logging setup so every module can just use logging.getLogger(__name__)."""

import logging


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger. Unknown level names fall back to INFO."""
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        force=True,
    )
