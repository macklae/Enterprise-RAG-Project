"""Builds and holds the shared components (embedder, Qdrant, Redis, OpenAI writer, RAG service)."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .cache import NullCache, RedisCache
from .config import Settings
from .embeddings import BGEM3Embedder, BGEReranker
from .ingestion.pipeline import IngestContext
from .jobs import JobManager
from .llm import OpenAIWriter
from .rag import RagService
from .vectorstore import QdrantStore

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    embedder: object
    store: QdrantStore
    cache: object
    writer: OpenAIWriter
    reranker: Optional[BGEReranker]
    rag: RagService
    jobs: JobManager
    ingest_ctx: IngestContext
    store_ready: bool = False


def build_services(settings: Optional[Settings] = None) -> Services:
    s = settings or Settings.load()
    embedder = BGEM3Embedder(s.embed_model)
    store = QdrantStore(s.qdrant_url, s.qdrant_api_key, s.qdrant_collection, dim=s.embed_dim)
    cache = (
        RedisCache(s.redis_url, s.cache_ttl_seconds, s.cache_notfound_ttl_seconds, s.cache_semantic_distance)
        if s.cache_enabled else NullCache()
    )
    writer = OpenAIWriter(s.openai_api_key, s.openai_model, s.openai_base_url, s.llm_temperature, s.llm_max_tokens, s.llm_timeout)
    reranker = BGEReranker(s.reranker_model) if s.reranker_enabled else None
    rag = RagService(s, embedder, store, cache, writer, reranker)
    ctx = IngestContext(settings=s, embedder=embedder, store=store, cache=cache)
    svc = Services(s, embedder, store, cache, writer, reranker, rag, JobManager(), ctx)
    return svc


def prepare(svc: Services, warm_models: bool = True) -> None:
    """Create the Qdrant collection (retry while the container starts) and warm the models."""
    def run():
        for attempt in range(10):
            try:
                svc.store.ensure_collection()
                svc.store_ready = True
                break
            except Exception as exc:
                log.warning("Qdrant not ready (%s). Retrying...", exc)
                time.sleep(2)
        if warm_models:
            try:
                svc.embedder.warmup()
                log.info("Embedding model ready")
            except Exception:
                log.exception("Could not load the embedding model")

    threading.Thread(target=run, daemon=True, name="prepare").start()
