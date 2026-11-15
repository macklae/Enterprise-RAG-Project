"""Central settings, read once from environment variables (and .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # python-dotenv is optional at import time
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    return raw if raw not in (None, "") else default


@dataclass
class Settings:
    # OpenAI
    openai_api_key: str
    openai_model: str
    openai_base_url: str
    llm_temperature: float
    llm_max_tokens: int
    llm_timeout: int

    # Qdrant
    qdrant_url: str
    qdrant_api_key: str
    qdrant_collection: str

    # Redis cache
    redis_url: str
    cache_enabled: bool
    cache_ttl_seconds: int
    cache_notfound_ttl_seconds: int
    cache_semantic_distance: float

    # Embeddings and reranking
    embed_model: str
    embed_dim: int
    reranker_enabled: bool
    reranker_model: str

    # Retrieval
    retrieve_top_k: int
    final_top_k: int
    min_dense_score: float
    min_rerank_score: float

    # Chunking
    chunk_max_tokens: int
    chunk_overlap_tokens: int

    # Documents
    documents_dir: Path
    data_dir: Path
    convert_docx_to_pdf: bool
    soffice_bin: str
    max_upload_mb: int

    # Guardrails
    max_query_chars: int
    rate_limit_per_minute: int
    send_restricted_to_llm: bool

    @classmethod
    def load(cls) -> "Settings":
        docs = Path(_str("DOCUMENTS_DIR", "data/documents"))
        if not docs.is_absolute():
            docs = BASE_DIR / docs
        return cls(
            openai_api_key=_str("OPENAI_API_KEY"),
            openai_model=_str("OPENAI_MODEL"),
            openai_base_url=_str("OPENAI_BASE_URL"),
            llm_temperature=_float("LLM_TEMPERATURE", 0.0),
            llm_max_tokens=_int("LLM_MAX_TOKENS", 700),
            llm_timeout=_int("LLM_TIMEOUT", 60),
            qdrant_url=_str("QDRANT_URL", "http://localhost:6333"),
            qdrant_api_key=_str("QDRANT_API_KEY"),
            qdrant_collection=_str("QDRANT_COLLECTION", "swiftpay_chunks"),
            redis_url=_str("REDIS_URL", "redis://localhost:6379/0"),
            cache_enabled=_bool("CACHE_ENABLED", True),
            cache_ttl_seconds=_int("CACHE_TTL_SECONDS", 86400),
            cache_notfound_ttl_seconds=_int("CACHE_NOTFOUND_TTL_SECONDS", 900),
            cache_semantic_distance=_float("CACHE_SEMANTIC_DISTANCE", 0.1),
            embed_model=_str("EMBED_MODEL", "BAAI/bge-m3"),
            embed_dim=_int("EMBED_DIM", 1024),
            reranker_enabled=_bool("RERANKER_ENABLED", False),
            reranker_model=_str("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"),
            retrieve_top_k=_int("RETRIEVE_TOP_K", 20),
            final_top_k=_int("FINAL_TOP_K", 5),
            min_dense_score=_float("MIN_DENSE_SCORE", 0.30),
            min_rerank_score=_float("MIN_RERANK_SCORE", 0.05),
            chunk_max_tokens=_int("CHUNK_MAX_TOKENS", 500),
            chunk_overlap_tokens=_int("CHUNK_OVERLAP_TOKENS", 40),
            documents_dir=docs,
            data_dir=BASE_DIR / "data",
            convert_docx_to_pdf=_bool("CONVERT_DOCX_TO_PDF", True),
            soffice_bin=_str("SOFFICE_BIN", "soffice"),
            max_upload_mb=_int("MAX_UPLOAD_MB", 25),
            max_query_chars=_int("MAX_QUERY_CHARS", 1000),
            rate_limit_per_minute=_int("RATE_LIMIT_PER_MINUTE", 30),
            send_restricted_to_llm=_bool("SEND_RESTRICTED_TO_LLM", False),
        )
