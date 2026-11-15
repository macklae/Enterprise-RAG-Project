"""Compare baseline hybrid retrieval with BGE reranking."""
import json
from pathlib import Path
from statistics import mean

from app.config import Settings
from app.embeddings import BGEM3Embedder, BGEReranker
from app.models import Identity
from app.vectorstore import QdrantStore

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "tests" / "golden_dataset.json"
OUTPUT = ROOT / "data" / "reranking_comparison.json"


def metrics(hits, relevant):
    ids = [str(h.id) for h in hits[:5]]
    matches = [i for i, chunk_id in enumerate(ids, 1) if chunk_id in relevant]
    return {
        "precision_at_5": len(matches) / 5,
        "hit_at_5": float(bool(matches)),
        "reciprocal_rank": 1 / matches[0] if matches else 0.0,
    }


def main():
    settings = Settings.load()
    embedder = BGEM3Embedder(settings.embed_model)
    reranker = BGEReranker(settings.reranker_model)
    store = QdrantStore(
        settings.qdrant_url,
        settings.qdrant_api_key,
        settings.qdrant_collection,
        dim=settings.embed_dim,
    )

    dataset = json.loads(DATASET.read_text())
    cases = dataset["cases"] if isinstance(dataset, dict) else dataset

    rows = []

    for case in cases:
        if not case.get("expect_found", True):
            continue

        question = case.get("question") or case.get("q")
        role = case.get("role", "employee")
        user = case.get("user")
        doc = case.get("expect_doc") or case.get("doc")
        section = case.get("expect_section") or case.get("section")

        if not question or not doc:
            raise ValueError(f"Incomplete golden case: {case}")

        identity = Identity(role=role, user=user)
        vector = embedder.embed_query(question)

        # Collect all chunk IDs matching the existing section-based labels.
        relevant = set()
        offset = None
        while True:
            points, offset = store.client.scroll(
                collection_name=settings.qdrant_collection,
                scroll_filter=None,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                if (
                    str(payload.get("doc_id", "")).lower() == str(doc).lower()
                    and str(section).lower() in str(payload.get("section", "")).lower()
                ):
                    relevant.add(str(point.id))
            if offset is None:
                break

        hits = store.search(vector, identity, limit=settings.retrieve_top_k)

        baseline = hits[:5]
        passages = [
            f"{h.payload.get('context', '')}\n{h.payload.get('text', '')}"
            for h in hits
        ]
        scores = reranker.score(question, passages)

        if len(scores) != len(hits):
            raise ValueError("Reranker returned an unexpected number of scores")

        reranked = [
            hit for _, hit in sorted(
                zip(scores, hits), key=lambda pair: pair[0], reverse=True
            )
        ][:5]

        base_metrics = metrics(baseline, relevant)
        rerank_metrics = metrics(reranked, relevant)

        rows.append({
            "name": case.get("name", question),
            "baseline": base_metrics,
            "reranked": rerank_metrics,
        })

        print(
            f"{str(case.get('name', question))[:28]:28} "
            f"P@5 {base_metrics['precision_at_5']:.2f} -> "
            f"{rerank_metrics['precision_at_5']:.2f} | "
            f"RR {base_metrics['reciprocal_rank']:.2f} -> "
            f"{rerank_metrics['reciprocal_rank']:.2f}"
        )

    def summary(key):
        return {
            metric: round(mean(row[key][metric] for row in rows), 4)
            for metric in ("precision_at_5", "hit_at_5", "reciprocal_rank")
        }

    report = {
        "cases": len(rows),
        "baseline": summary("baseline"),
        "reranked": summary("reranked"),
        "limitations": [
            "Relevance uses document/section matching, not manually verified chunks.",
            "This compares retrieval ranking, not final generated answers.",
            "Reranking latency is not measured.",
        ],
        "rows": rows,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2))
    print("\nCOMPARISON SUMMARY")
    print(json.dumps({
        "cases": report["cases"],
        "baseline": report["baseline"],
        "reranked": report["reranked"],
    }, indent=2))
    print(f"\nSaved: {OUTPUT}")


if __name__ == "__main__":
    main()
