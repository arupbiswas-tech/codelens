"""Try clone + chunking from the command line (prints a summary, does not touch the database).

python -m codelens.ingestion https://github.com/owner/repo --show 5
"""

import argparse
import sys

from codelens.errors import CodeLensError
from codelens.ingestion.pipeline import ingest_repo
from codelens.logging_setup import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clone a GitHub repo and print its chunks.")
    parser.add_argument("url", help="e.g. https://github.com/owner/repo")
    parser.add_argument("--show", type=int, default=5, help="how many sample chunks to print")
    args = parser.parse_args(argv)

    setup_logging("INFO")
    try:
        result = ingest_repo(args.url)
    except CodeLensError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1

    print(f"\n{result.repo_name} @ {result.commit_sha}")
    print(f"stats: {result.stats}")
    for chunk in result.chunks[: args.show]:
        preview = chunk.content.splitlines()[0][:80] if chunk.content else ""
        print(f"  {chunk.citation:<50} {chunk.kind:<8} {chunk.symbol or '-':<30} {preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
