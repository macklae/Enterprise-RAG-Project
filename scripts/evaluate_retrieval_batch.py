"""Evaluate SwiftPay hybrid retrieval against section-level golden labels."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

from app.config import Settings
from app.embeddings import BGEM3Embedder
from app.models import Identity
from app.vectorstore import QdrantStore
from scripts.retrieval_metrics import (
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "tests" / "golden_dataset.json"
OUTPUT = ROOT / "data" / "retrieval_eval_report.json"
K = 5


def main():
    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    settings = Settings.load()
    embedder = BGEM3Embedder(settings.embed_model)
    store = QdrantStore(
        settings.qdrant_url,
        settings.qdrant_api_key,
        settings.qdrant_collection,
        dim=settings.embed_dim,
    )

    # Read the indexed chunk IDs and metadata once.
    indexed = []
    offset = None
    while True:
        points, offset = store.client.scroll(
            collection_name=store.collection,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        indexed.extend(points)
        if offset is None:
            break

    results = []
    for case in cases:
        if not case["expect_found"]:
            continue  # Abstention and permission-denial tests are separate.

        identity = Identity(role=case["role"], user=case["user"])
        expected_doc = case["expect_doc"].lower()
        expected_section = case["expect_section"].lower()

        # These are section-level proxy labels, not exhaustively
        # human-verified relevance judgements.
        relevant_ids = {
            str(point.id)
            for point in indexed
            if expected_doc in str(
                (point.payload or {}).get("doc_id") or ""
            ).lower()
            and expected_section in str(
                (point.payload or {}).get("section") or ""
            ).lower()
        }

        if not relevant_ids:
            print(f"UNLABELLED {case['name']}: no matching indexed section")
            continue

        embedding = embedder.embed_query(case["question"])
        hits = store.search(embedding, identity, limit=K)
        retrieved_ids = [hit.id for hit in hits]

        precision = precision_at_k(retrieved_ids, relevant_ids, K)
        recall = recall_at_k(retrieved_ids, relevant_ids, K)
        rr = reciprocal_rank(retrieved_ids, relevant_ids)

        results.append({
            "name": case["name"],
            "role": case["role"],
            "question": case["question"],
            "relevant_chunk_ids": sorted(relevant_ids),
            "retrieved_chunk_ids": retrieved_ids,
            "precision_at_5": precision,
            "recall_at_5": recall,
            "reciprocal_rank": rr,
        })

        print(
            f"{case['name']:28} "
            f"P@5={precision:.2f} "
            f"R@5={recall:.2f} "
            f"RR={rr:.2f}"
        )

    summary = {
        "evaluated": len(results),
        "precision_at_5": mean(
            r["precision_at_5"] for r in results
        ) if results else None,
        "recall_at_5": mean(
            r["recall_at_5"] for r in results
        ) if results else None,
        "mrr": mean(
            r["reciprocal_rank"] for r in results
        ) if results else None,
        "label_type": "document-section proxy",
        "limitations": [
            "Relevant IDs are inferred from document and section metadata.",
            "Relevance labels have not been manually verified.",
            "Role accessibility of all labelled chunks has not yet been audited.",
            "Queries bypass Redis and the LLM.",
            "Reranking is not included in this measurement.",
        ],
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(
            {"summary": summary, "cases": results},
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\nSECTION-LABELLED RETRIEVAL RESULTS")
    print(json.dumps(summary, indent=2))
    print(f"\nSaved: {OUTPUT}")


if __name__ == "__main__":
    main()
