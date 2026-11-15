"""In-memory stand-ins so the pipeline can be tested without Qdrant, Redis, models or OpenAI."""
from __future__ import annotations

import fnmatch
import re
import time
from typing import Any

import numpy as np

from app.access import chunk_visible
from app.embeddings import cosine
from app.models import Hit


class FakeStore:
    def __init__(self):
        self.points: dict[str, dict[str, Any]] = {}

    def replace_document(self, doc_id, chunks, embeddings):
        self.delete_document(doc_id)
        for c, e in zip(chunks, embeddings):
            self.points[c.chunk_id] = {"payload": c.payload(), "dense": e.dense, "sp": dict(zip(e.sparse_indices, e.sparse_values))}

    def delete_document(self, doc_id):
        self.points = {k: v for k, v in self.points.items() if v["payload"]["doc_id"] != doc_id}

    def search(self, query, identity, limit=20):
        hits = []
        qsp = dict(zip(query.sparse_indices, query.sparse_values))
        for pid, p in self.points.items():
            if not chunk_visible(p["payload"], identity):
                continue
            dense = cosine(query.dense, p["dense"])
            sparse = sum(v * p["sp"].get(k, 0.0) for k, v in qsp.items())
            hits.append(Hit(id=pid, payload=p["payload"], fused_score=dense + 0.01 * sparse, dense_score=dense))
        hits.sort(key=lambda h: h.fused_score, reverse=True)
        return hits[:limit]

    def list_documents(self, identity=None):
        docs = {}
        for p in self.points.values():
            pl = p["payload"]
            d = docs.setdefault(pl["doc_id"], {"doc_id": pl["doc_id"], "doc_name": pl["doc_name"], "allowed_roles": set(), "chunks": 0})
            d["allowed_roles"] |= set(pl["allowed_roles"])
            d["chunks"] += 1
        out = [{**d, "allowed_roles": sorted(d["allowed_roles"])} for d in docs.values()]
        return [d for d in out if identity is None or identity.role in d["allowed_roles"]]

    def known_doc_ids(self):
        return {p["payload"]["doc_id"] for p in self.points.values()}

    def health(self):
        return True

    def count(self):
        return len(self.points)


class FakeRedis:
    """The small subset of redis-py that RedisCache uses."""

    def __init__(self):
        self.kv: dict[str, tuple[bytes, float | None]] = {}
        self.sets: dict[str, set[bytes]] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    def _alive(self, k):
        v = self.kv.get(k)
        if v and v[1] is not None and v[1] < time.time():
            del self.kv[k]
            return None
        return v

    def ping(self):
        return True

    def get(self, k):
        v = self._alive(k)
        return v[0] if v else None

    def mget(self, keys):
        return [self.get(k) for k in keys]

    def set(self, k, val, ex=None):
        self.kv[k] = (val.encode() if isinstance(val, str) else val, time.time() + ex if ex else None)

    def delete(self, *keys):
        for k in keys:
            self.kv.pop(k, None)
            self.sets.pop(k, None)
            self.zsets.pop(k, None)

    def sadd(self, k, *vals):
        self.sets.setdefault(k, set()).update(v.encode() if isinstance(v, str) else v for v in vals)

    def smembers(self, k):
        return set(self.sets.get(k, set()))

    def expire(self, k, s):
        pass

    def zadd(self, k, mapping):
        self.zsets.setdefault(k, {}).update(mapping)

    def zrevrange(self, k, a, b):
        items = sorted(self.zsets.get(k, {}).items(), key=lambda kv: kv[1], reverse=True)
        end = None if b == -1 else b + 1
        return [i.encode() for i, _ in items[a:end]]

    def zrem(self, k, *ids):
        for i in ids:
            self.zsets.get(k, {}).pop(i, None)

    def zremrangebyrank(self, k, a, b):
        pass

    def scan_iter(self, match=None, count=None):
        return [k for k in list(self.kv) + list(self.sets) if fnmatch.fnmatch(k, match or "*")]

    def pipeline(self):
        return _Pipe(self)


class _Pipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def __getattr__(self, name):
        def rec(*a, **k):
            self.ops.append((name, a, k))
            return self
        return rec

    def execute(self):
        for name, a, k in self.ops:
            getattr(self.r, name)(*a, **k)
        self.ops = []


class FakeWriter:
    """Answers from the first source, citing it. Returns NOT_FOUND when told to."""

    available = True
    model = "fake"

    def __init__(self, mode="cite"):
        self.mode = mode
        self.calls = 0
        self.last_sources = []

    def answer(self, question, sources):
        self.calls += 1
        self.last_sources = sources
        if self.mode == "not_found":
            return "NOT_FOUND"
        if self.mode == "no_cite":
            return "Something without a source."
        if self.mode == "leak":
            return "ZXQ-CANARY-7731 [1]"
        s = sources[0]
        first_line = s["text"].split("\n")[1] if "\n" in s["text"] else s["text"]
        return f"{first_line[:160]} [{s['n']}]"
