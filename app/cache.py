"""Redis answer cache.

Lookup order:
  1. Exact match on the normalised question (no embedding needed).
  2. Similar match: cosine distance below the limit AND the same "signature" (numbers and
     codes such as P1, P2, SOP-OPS-005, Tier 0). The signature stops "P1 acknowledge time"
     from reusing the answer for "P2 acknowledge time".

Every entry is stored per scope (role, and per person for Line Managers), has a TTL, and
remembers which documents it used, so replacing a document clears its saved answers.
"Not found" answers live for a shorter time and are cleared whenever a document is added.
Works on plain Redis. No Redis Stack or search module is needed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from .models import Identity

log = logging.getLogger(__name__)
P = "rag:v1"
MAX_SEMANTIC_CANDIDATES = 200

_SIG_RE = re.compile(r"\b(?:[a-z]{1,4}-?\d+[\w-]*|\d+(?:[.,]\d+)*)\b", re.I)


def normalise_question(q: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[?!.\s]+$", "", q.strip().lower()))


def signature(q: str) -> list[str]:
    """Tokens that must match for two questions to share an answer."""
    return sorted(set(t.lower() for t in _SIG_RE.findall(q)))


@dataclass
class CacheHit:
    kind: str  # "exact" | "semantic"
    entry: dict[str, Any]
    distance: float = 0.0


class RedisCache:
    def __init__(self, url: str, ttl: int, ttl_not_found: int, max_distance: float, enabled: bool = True):
        self.enabled = enabled
        self.ttl = ttl
        self.ttl_not_found = ttl_not_found
        self.max_distance = max_distance
        self.client = None
        self.stats = {"exact_hits": 0, "semantic_hits": 0, "misses": 0, "stores": 0, "errors": 0}
        if enabled:
            try:
                import redis

                self.client = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
            except Exception as exc:  # redis package missing
                log.warning("Redis cache disabled: %s", exc)
                self.enabled = False

    # ------------------------------------------------------------------ helpers
    def _r(self):
        return self.client if (self.enabled and self.client is not None) else None

    def health(self) -> bool:
        r = self._r()
        if not r:
            return False
        try:
            return bool(r.ping())
        except Exception:
            return False

    @staticmethod
    def _exact_key(scope: str, question: str) -> str:
        h = hashlib.sha256(normalise_question(question).encode()).hexdigest()[:32]
        return f"{P}:ans:{scope}:{h}"

    # ------------------------------------------------------------------ lookup
    def lookup_exact(self, identity: Identity, question: str) -> Optional[CacheHit]:
        r = self._r()
        if not r:
            return None
        try:
            raw = r.get(self._exact_key(identity.cache_scope, question))
            if raw:
                self.stats["exact_hits"] += 1
                return CacheHit("exact", json.loads(raw))
        except Exception as exc:
            self._err(exc)
        return None

    def lookup_semantic(self, identity: Identity, question: str, q_dense) -> Optional[CacheHit]:
        r = self._r()
        if not r:
            return None
        try:
            scope = identity.cache_scope
            ids = [i.decode() for i in r.zrevrange(f"{P}:semidx:{scope}", 0, MAX_SEMANTIC_CANDIDATES - 1)]
            if not ids:
                self.stats["misses"] += 1
                return None
            raws = r.mget([f"{P}:sem:{scope}:{i}" for i in ids])
            sig = signature(question)
            q = np.asarray(q_dense, dtype=np.float32)
            qn = float(np.linalg.norm(q)) or 1.0
            best: Optional[tuple[float, dict[str, Any]]] = None
            stale = []
            for i, raw in zip(ids, raws):
                if raw is None:
                    stale.append(i)
                    continue
                item = json.loads(raw)
                if item["sig"] != sig:
                    continue
                v = np.frombuffer(base64.b64decode(item["emb"]), dtype=np.float32)
                dist = 1.0 - float(q @ v) / (qn * (float(np.linalg.norm(v)) or 1.0))
                if dist <= self.max_distance and (best is None or dist < best[0]):
                    best = (dist, item)
            if stale:
                r.zrem(f"{P}:semidx:{scope}", *stale)
            if best:
                raw = r.get(best[1]["exact_key"])
                if raw:
                    self.stats["semantic_hits"] += 1
                    return CacheHit("semantic", json.loads(raw), best[0])
            self.stats["misses"] += 1
        except Exception as exc:
            self._err(exc)
        return None

    # ------------------------------------------------------------------ store
    def store(self, identity: Identity, question: str, q_dense, entry: dict[str, Any], doc_ids: list[str]) -> None:
        r = self._r()
        if not r:
            return
        try:
            scope = identity.cache_scope
            found = bool(entry.get("found"))
            ttl = self.ttl if found else self.ttl_not_found
            ekey = self._exact_key(scope, question)
            entry = {**entry, "cached_at": time.time(), "question": question, "doc_ids": doc_ids}
            pipe = r.pipeline()
            pipe.set(ekey, json.dumps(entry), ex=ttl)
            if q_dense is not None:
                sid = uuid.uuid4().hex
                vec = np.asarray(q_dense, dtype=np.float32)
                skey = f"{P}:sem:{scope}:{sid}"
                pipe.set(skey, json.dumps({
                    "sig": signature(question), "emb": base64.b64encode(vec.tobytes()).decode(), "exact_key": ekey,
                }), ex=ttl)
                pipe.zadd(f"{P}:semidx:{scope}", {sid: time.time()})
                pipe.zremrangebyrank(f"{P}:semidx:{scope}", 0, -(MAX_SEMANTIC_CANDIDATES * 3) - 1)
                link_keys = [ekey, skey]
            else:
                link_keys = [ekey]
            for d in doc_ids:
                pipe.sadd(f"{P}:doc:{d}", *link_keys)
                pipe.expire(f"{P}:doc:{d}", self.ttl)
            if not found:
                pipe.sadd(f"{P}:notfound", *link_keys)
                pipe.expire(f"{P}:notfound", self.ttl_not_found)
            pipe.execute()
            self.stats["stores"] += 1
        except Exception as exc:
            self._err(exc)

    # ------------------------------------------------------------------ invalidation
    def _drop_set(self, set_key: str) -> int:
        r = self._r()
        if not r:
            return 0
        try:
            keys = [k.decode() for k in r.smembers(set_key)]
            if keys:
                r.delete(*keys)
            r.delete(set_key)
            return len(keys)
        except Exception as exc:
            self._err(exc)
            return 0

    def invalidate_document(self, doc_id: str) -> int:
        """Delete saved answers that used this document."""
        return self._drop_set(f"{P}:doc:{doc_id}")

    def clear_not_found(self) -> int:
        """A new document may answer a question that was 'Not found'."""
        return self._drop_set(f"{P}:notfound")

    def clear_all(self) -> None:
        r = self._r()
        if not r:
            return
        try:
            for k in r.scan_iter(match=f"{P}:*", count=500):
                r.delete(k)
        except Exception as exc:
            self._err(exc)

    def _err(self, exc: Exception) -> None:
        self.stats["errors"] += 1
        log.warning("Redis cache error (continuing without cache): %s", exc)


class NullCache:
    """Used when caching is off or in tests."""

    enabled = False
    stats = {"exact_hits": 0, "semantic_hits": 0, "misses": 0, "stores": 0, "errors": 0}

    def health(self) -> bool:
        return False

    def lookup_exact(self, *a, **k):
        return None

    def lookup_semantic(self, *a, **k):
        return None

    def store(self, *a, **k):
        return None

    def invalidate_document(self, *a, **k):
        return 0

    def clear_not_found(self, *a, **k):
        return 0

    def clear_all(self):
        return None
