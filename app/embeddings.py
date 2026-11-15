"""BGE-M3 embeddings (dense + sparse) and the optional BGE reranker.

Models load lazily on first use so the API starts quickly and tests can swap in a fake.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class EmbeddedText:
    dense: list[float]
    sparse_indices: list[int]
    sparse_values: list[float]


class BGEM3Embedder:
    """BAAI/bge-m3 through FlagEmbedding. One pass gives meaning numbers and keyword numbers."""

    def __init__(self, model_name: str = "BAAI/bge-m3", use_fp16: bool = False):
        self.model_name = model_name
        self.use_fp16 = use_fp16
        self.dim = 1024
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from FlagEmbedding import BGEM3FlagModel

                    log.info("Loading %s (first run downloads the model)", self.model_name)
                    self._model = BGEM3FlagModel(self.model_name, use_fp16=self.use_fp16)
        return self._model

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def warmup(self) -> None:
        self._load()

    def _encode(self, texts: list[str], max_length: int) -> list[EmbeddedText]:
        model = self._load()
        out = model.encode(
            texts, batch_size=8, max_length=max_length,
            return_dense=True, return_sparse=True, return_colbert_vecs=False,
        )
        result = []
        for dense, lex in zip(out["dense_vecs"], out["lexical_weights"]):
            pairs = sorted((int(k), float(v)) for k, v in lex.items() if float(v) > 0)
            result.append(EmbeddedText(
                dense=[float(x) for x in dense],
                sparse_indices=[p[0] for p in pairs],
                sparse_values=[p[1] for p in pairs],
            ))
        return result

    def embed_passages(self, texts: list[str]) -> list[EmbeddedText]:
        return self._encode(texts, max_length=1024)

    def embed_query(self, text: str) -> EmbeddedText:
        return self._encode([text], max_length=256)[0]


class BGEReranker:
    """BAAI/bge-reranker-v2-m3. Scores each (question, chunk) pair, 0 to 1."""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3", use_fp16: bool = False):
        self.model_name = model_name
        self.use_fp16 = use_fp16
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from FlagEmbedding import FlagReranker

                    log.info("Loading reranker %s", self.model_name)
                    self._model = FlagReranker(self.model_name, use_fp16=self.use_fp16)
        return self._model

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        scores = self._load().compute_score([[query, p] for p in passages], normalize=True)
        if isinstance(scores, (int, float)):
            scores = [scores]
        return [float(s) for s in scores]


# ----------------------------------------------------------------------------- test double
class HashEmbedder:
    """Deterministic, model-free embedder for unit tests. Not for real use."""

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.loaded = True

    def warmup(self) -> None:
        pass

    def _one(self, text: str) -> EmbeddedText:
        vec = np.zeros(self.dim, dtype=np.float32)
        sparse: dict[int, float] = {}
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
            sparse[h % 50000] = sparse.get(h % 50000, 0.0) + 1.0
        n = float(np.linalg.norm(vec)) or 1.0
        idx = sorted(sparse)
        return EmbeddedText((vec / n).tolist(), idx, [sparse[i] for i in idx])

    def embed_passages(self, texts: list[str]) -> list[EmbeddedText]:
        return [self._one(t) for t in texts]

    def embed_query(self, text: str) -> EmbeddedText:
        return self._one(text)


def cosine(a, b) -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    d = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / d if d else 0.0
