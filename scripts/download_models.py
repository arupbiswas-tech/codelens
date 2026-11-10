"""Download the embedding and reranker models into the image (run once, during `docker build`)."""

import os

from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder

TextEmbedding(os.environ["EMBEDDING_MODEL"])
TextCrossEncoder(os.environ["RERANKER_MODEL"])
print("models downloaded")
