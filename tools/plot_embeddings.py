"""Draw a 2D map of a repository's code embeddings.

Each dot is one chunk of code. Chunks with similar meaning sit close together, so you can see
how the embedding model groups the code. The 384 dimensions are squeezed to 2 with PCA, so
distances on the picture are only an approximation of the real distances.

Run it in a throwaway container on the app's network (no installation on your computer):

    docker run --rm --network codelens_default -v "$PWD:/work" -w /work \\
        -e DATABASE_URL=postgresql://codelens:YOUR_PASSWORD@db:5432/codelens python:3.12-slim \\
        sh -c "pip install -q 'psycopg[binary]' numpy scikit-learn matplotlib && \\
               python tools/plot_embeddings.py kennethreitz/records"

It writes docs/embeddings-<repo>.png. Options: --color kind|file, --out FILE.
"""

import argparse
import os
import sys
from collections import Counter

import matplotlib

matplotlib.use("Agg")  # draw to a file, no window needed
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import psycopg  # noqa: E402
from sklearn.decomposition import PCA  # noqa: E402

MAX_GROUPS = 8  # more colours than this are hard to tell apart; the rest become "other"

QUERY = """
    SELECT c.path, c.kind, c.symbol, c.embedding::text
    FROM chunks c JOIN repos r ON r.id = c.repo_id
    WHERE r.name = %s AND c.embedding IS NOT NULL
"""


def load(repo: str) -> tuple[list[dict], np.ndarray]:
    """The chunks of `repo` and their embeddings as one matrix (one row per chunk)."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows = conn.execute(QUERY, (repo,)).fetchall()
    if not rows:
        sys.exit(f"No indexed chunks for '{repo}'. Index it first.")
    meta = [{"path": path, "kind": kind, "symbol": symbol} for path, kind, symbol, _ in rows]
    # pgvector's text form "[0.1,0.2,...]" is valid JSON-ish: strip the brackets and split.
    matrix = np.array([[float(x) for x in text.strip("[]").split(",")] for *_, text in rows])
    return meta, matrix


def group_labels(meta: list[dict], key: str) -> list[str]:
    """The group of each chunk ('file' = its path, 'kind' = function/method/...)."""
    names = [item["path"] if key == "file" else item["kind"] for item in meta]
    common = {name for name, _ in Counter(names).most_common(MAX_GROUPS)}
    return [name if name in common else "other" for name in names]


def main() -> int:
    parser = argparse.ArgumentParser(description="Plot a repo's embeddings in 2D.")
    parser.add_argument("repo", help="indexed repository name, e.g. kennethreitz/records")
    parser.add_argument("--color", choices=["file", "kind"], default="file")
    parser.add_argument("--out", help="output PNG (default docs/embeddings-<repo>.png)")
    args = parser.parse_args()

    meta, matrix = load(args.repo)
    pca = PCA(n_components=2).fit(matrix)
    points = pca.transform(matrix)
    kept = pca.explained_variance_ratio_.sum()

    labels = group_labels(meta, args.color)
    fig, ax = plt.subplots(figsize=(9, 6.5), dpi=130)
    for name in sorted(set(labels)):
        mask = np.array([label == name for label in labels])
        ax.scatter(points[mask, 0], points[mask, 1], s=34, alpha=0.8, label=f"{name} ({mask.sum()})")
    ax.set_title(f"{args.repo}: {len(meta)} code chunks, colored by {args.color}")
    ax.set_xlabel("PCA component 1")
    ax.set_ylabel("PCA component 2")
    ax.legend(fontsize=7, loc="best", frameon=False)
    ax.grid(alpha=0.2)
    fig.text(0.01, 0.01, f"2 of 384 dimensions, keeping {kept:.0%} of the variance", fontsize=7, alpha=0.6)
    fig.tight_layout()

    out = args.out or f"docs/embeddings-{args.repo.replace('/', '-')}.png"
    fig.savefig(out)
    print(f"wrote {out} ({len(meta)} chunks, {kept:.0%} of the variance kept in 2D)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
