"""Retrieval ranking metrics for SwiftPay's labelled evidence."""

from __future__ import annotations


def precision_at_k(retrieved_ids, relevant_ids, k=5):
    """Fraction of the top-k slots containing relevant chunks."""
    relevant = set(relevant_ids)
    if not relevant:
        return None
    return sum(chunk_id in relevant for chunk_id in retrieved_ids[:k]) / k


def recall_at_k(retrieved_ids, relevant_ids, k=5):
    """Fraction of all labelled relevant chunks found in the top k."""
    relevant = set(relevant_ids)
    if not relevant:
        return None
    return len(set(retrieved_ids[:k]) & relevant) / len(relevant)


def reciprocal_rank(retrieved_ids, relevant_ids):
    """Reciprocal rank of the first relevant retrieved chunk."""
    relevant = set(relevant_ids)
    if not relevant:
        return None

    for rank, chunk_id in enumerate(retrieved_ids, start=1):
        if chunk_id in relevant:
            return 1.0 / rank

    return 0.0
