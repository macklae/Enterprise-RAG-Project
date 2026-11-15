"""Qdrant wrapper: one collection, dense + sparse vectors, permission filter inside the search."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .access import build_qdrant_filter
from .models import Chunk, Hit, Identity

log = logging.getLogger(__name__)

DENSE = "dense"
SPARSE = "sparse"
INDEXED_FIELDS = ["doc_id", "allowed_roles", "team_manager", "classification"]


class QdrantStore:
    def __init__(self, url: str, api_key: str, collection: str, dim: int = 1024):
        from qdrant_client import QdrantClient

        self.collection = collection
        self.dim = dim
        self.client = QdrantClient(url=url, api_key=api_key or None, timeout=30)
        self._docs_cache: tuple[float, list[dict[str, Any]]] | None = None

    # ------------------------------------------------------------------ setup
    def ensure_collection(self) -> None:
        from qdrant_client import models as qm

        existing = {c.name for c in self.client.get_collections().collections}
        if self.collection not in existing:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config={DENSE: qm.VectorParams(size=self.dim, distance=qm.Distance.COSINE)},
                sparse_vectors_config={SPARSE: qm.SparseVectorParams()},
            )
            log.info("Created Qdrant collection %s", self.collection)
        for field in INDEXED_FIELDS:
            try:
                self.client.create_payload_index(
                    collection_name=self.collection, field_name=field,
                    field_schema=qm.PayloadSchemaType.KEYWORD,
                )
            except Exception:  # index already exists
                pass

    def health(self) -> bool:
        try:
            self.client.get_collections()
            return True
        except Exception:
            return False

    def count(self) -> int:
        try:
            return int(self.client.count(self.collection, exact=True).count)
        except Exception:
            return 0

    # ------------------------------------------------------------------ write
    def replace_document(self, doc_id: str, chunks: list[Chunk], embeddings: list) -> None:
        from qdrant_client import models as qm

        self.delete_document(doc_id)
        points = []
        for c, e in zip(chunks, embeddings):
            vector: dict[str, Any] = {DENSE: e.dense}
            if e.sparse_indices:
                vector[SPARSE] = qm.SparseVector(indices=e.sparse_indices, values=e.sparse_values)
            points.append(qm.PointStruct(id=c.chunk_id, vector=vector, payload=c.payload()))
        for i in range(0, len(points), 64):
            self.client.upsert(collection_name=self.collection, points=points[i:i + 64], wait=True)
        self._docs_cache = None

    def delete_document(self, doc_id: str) -> None:
        from qdrant_client import models as qm

        self.client.delete(
            collection_name=self.collection,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(must=[qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id))])
            ),
            wait=True,
        )
        self._docs_cache = None

    # ------------------------------------------------------------------ read
    def search(self, query, identity: Identity, limit: int = 20) -> list[Hit]:
        """Hybrid search (meaning + keyword, fused with RRF) over chunks the user may see."""
        from qdrant_client import models as qm

        flt = build_qdrant_filter(identity)
        prefetch = [qm.Prefetch(query=query.dense, using=DENSE, limit=limit, filter=flt)]
        if query.sparse_indices:
            prefetch.append(qm.Prefetch(
                query=qm.SparseVector(indices=query.sparse_indices, values=query.sparse_values),
                using=SPARSE, limit=limit, filter=flt,
            ))
        res = self.client.query_points(
            collection_name=self.collection,
            prefetch=prefetch,
            query=qm.FusionQuery(fusion=qm.Fusion.RRF),
            query_filter=flt,
            limit=limit,
            with_payload=True,
            with_vectors=[DENSE],
        )
        from .embeddings import cosine

        hits: list[Hit] = []
        for p in res.points:
            vec = p.vector.get(DENSE) if isinstance(p.vector, dict) else p.vector
            hits.append(Hit(
                id=str(p.id), payload=dict(p.payload or {}), fused_score=float(p.score or 0.0),
                dense_score=cosine(query.dense, vec) if vec is not None else 0.0,
            ))
        return hits

    def _all_docs(self) -> list[dict[str, Any]]:
        if self._docs_cache and time.time() - self._docs_cache[0] < 20:
            return self._docs_cache[1]
        docs: dict[str, dict[str, Any]] = {}
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.collection, limit=256, offset=offset, with_payload=True, with_vectors=False,
            )
            for p in points:
                pl = p.payload or {}
                d = docs.setdefault(pl.get("doc_id", "?"), {
                    "doc_id": pl.get("doc_id"), "doc_name": pl.get("doc_name"), "doc_type": pl.get("doc_type"),
                    "version": pl.get("version"), "classification": pl.get("classification"),
                    "allowed_roles": pl.get("allowed_roles", []), "source_file": pl.get("source_file"),
                    "org": pl.get("org"), "chunks": 0, "mixed_access": False,
                })
                d["chunks"] += 1
                if pl.get("allowed_roles") != d["allowed_roles"]:
                    d["mixed_access"] = True
                    d["allowed_roles"] = sorted(set(d["allowed_roles"]) | set(pl.get("allowed_roles", [])))
            if offset is None:
                break
        out = sorted(docs.values(), key=lambda d: d["doc_id"] or "")
        self._docs_cache = (time.time(), out)
        return out

    def list_documents(self, identity: Optional[Identity] = None) -> list[dict[str, Any]]:
        docs = self._all_docs()
        if identity is None:
            return docs
        return [d for d in docs if identity.role in d["allowed_roles"]]

    def known_doc_ids(self) -> set[str]:
        return {d["doc_id"] for d in self._all_docs() if d.get("doc_id")}
