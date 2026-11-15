"""Inspect real SwiftPay hybrid retrieval results without using Redis or the LLM."""
from __future__ import annotations

import argparse

from app.config import Settings
from app.embeddings import BGEM3Embedder
from app.models import Identity
from app.vectorstore import QdrantStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--role", default="employee",
                        choices=["employee", "line_manager", "hr_ld", "sre_ops"])
    parser.add_argument("--user", default=None)
    args = parser.parse_args()

    if args.role == "line_manager" and not args.user:
        parser.error("--user is required for line_manager")

    settings = Settings.load()
    embedder = BGEM3Embedder(settings.embed_model)
    store = QdrantStore(
        settings.qdrant_url,
        settings.qdrant_api_key,
        settings.qdrant_collection,
        dim=settings.embed_dim,
    )

    identity = Identity(role=args.role, user=args.user)
    query_embedding = embedder.embed_query(args.question)
    hits = store.search(query_embedding, identity, limit=5)

    print(f"\nQuestion: {args.question}")
    print(f"Role: {identity.role}")
    print(f"Retrieved chunks: {len(hits)}\n")

    for rank, hit in enumerate(hits, start=1):
        payload = hit.payload
        print(
            f"{rank}. ID={hit.id}\n"
            f"   Document={payload.get('doc_id')}\n"
            f"   Section={payload.get('section')}\n"
            f"   Dense similarity={hit.dense_score:.4f}\n"
            f"   RRF score={hit.fused_score:.4f}\n"
            f"   Text={str(payload.get('text') or payload.get('context') or '')[:600]}\n"
        )


if __name__ == "__main__":
    main()
