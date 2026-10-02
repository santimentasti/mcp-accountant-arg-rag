"""Retrievers: BM25 (lexical, always on), dense (optional embeddings) and Reciprocal Rank Fusion.

Key ideas
- BM25 scores a chunk by how often query terms appear in it, discounted by how common the term is
  across the corpus (IDF) and normalised by chunk length. Great for exact legal tokens ("F.931", "RG 4034").
- Dense retrieval embeds query and chunk with a bi-encoder (a transformer that maps text to a vector) and
  ranks by cosine similarity: it matches paraphrases ("cargas sociales" ~ "contribuciones patronales").
- RRF fuses ranked lists without calibrating scores: score(d) = Σ_r 1 / (k + rank_r(d)).
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Callable, Protocol, Sequence

from .text import tokenize


class BM25:
    def __init__(self, docs: Sequence[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tfs: list[Counter[str]] = [Counter(tokenize(d)) for d in docs]
        self.lens = [sum(tf.values()) for tf in self.tfs]
        self.avgdl = (sum(self.lens) / len(self.lens)) if self.lens else 0.0
        df: Counter[str] = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(self.tfs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = []
        for tf, dl in zip(self.tfs, self.lens):
            s = 0.0
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
            out.append(s)
        return out


class Embedder(Protocol):
    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class FastEmbedEmbedder:
    """Local ONNX embeddings via `fastembed` (pip install contador-mcp[dense])."""

    def __init__(self, model_name: str):
        from fastembed import TextEmbedding  # imported lazily: optional dependency

        self.name = model_name
        self._model = TextEmbedding(model_name=model_name)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(list(texts))]


def load_embedder(model_name: str | None) -> tuple[Embedder | None, str]:
    if not model_name:
        return None, "deshabilitado (CONTADOR_DENSE_MODEL=none)"
    try:
        return FastEmbedEmbedder(model_name), f"activo ({model_name})"
    except ImportError:
        return None, "no instalado (pip install 'contador-mcp[dense]')"
    except Exception as exc:  # model download blocked, etc.
        return None, f"no disponible: {type(exc).__name__}: {exc}"[:300]


def normalize_vec(v: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine_scores(query_vec: Sequence[float], matrix: Sequence[Sequence[float]]) -> list[float]:
    q = normalize_vec(query_vec)
    return [sum(a * b for a, b in zip(q, row)) for row in matrix]


def rank(scores: Sequence[float], allowed: Callable[[int], bool], min_score: float = 1e-9) -> list[int]:
    idx = [i for i, s in enumerate(scores) if s > min_score and allowed(i)]
    return sorted(idx, key=lambda i: scores[i], reverse=True)


def rrf(rankings: Sequence[Sequence[int]], k: int = 60) -> list[tuple[int, float]]:
    fused: dict[int, float] = {}
    for ranking in rankings:
        for pos, item in enumerate(ranking):
            fused[item] = fused.get(item, 0.0) + 1.0 / (k + pos + 1)
    return sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
