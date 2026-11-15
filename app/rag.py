"""Question answering: guardrails, cache, hybrid retrieval, rerank, writer, citations."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from . import guardrails as gr
from .access import chunk_visible
from .config import Settings
from .ingestion.policy import DOC_CODE_RE
from .models import Hit, Identity

log = logging.getLogger(__name__)

NOT_FOUND_TEXT = "Not found. The documents available to you do not contain an answer to this question."


def _source_view(n: int, hit: Hit, restricted: bool) -> dict[str, Any]:
    p = hit.payload
    text = p.get("text", "")
    return {
        "n": n, "doc_id": p.get("doc_id"), "doc_name": p.get("doc_name"), "version": p.get("version"),
        "section": p.get("section"), "page": p.get("page"), "page_end": p.get("page_end"),
        "sheet": p.get("sheet"), "row": p.get("row"), "classification": p.get("classification"),
        "org": p.get("org"), "source_file": p.get("source_file"), "strategy": p.get("strategy"),
        "excerpt": text[:700], "text": text, "score": round(hit.relevance, 4), "restricted": restricted,
    }


def public_source(s: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in s.items() if k not in {"text"}}


class RagService:
    def __init__(self, settings: Settings, embedder, store, cache, writer, reranker=None):
        self.s = settings
        self.embedder, self.store, self.cache = embedder, store, cache
        self.writer, self.reranker = writer, reranker

    # ------------------------------------------------------------------ helpers
    def _response(self, **kw) -> dict[str, Any]:
        base = {
            "answer": "", "found": False, "mode": "not_found", "sources": [],
            "cache": {"status": "off" if not getattr(self.cache, "enabled", False) else "miss", "distance": None},
            "guardrails": [], "timings_ms": {},
        }
        base.update(kw)
        return base

    def _not_found(self, flags, timings, cache_status, extra: str = "") -> dict[str, Any]:
        return self._response(
            answer=(NOT_FOUND_TEXT + (" " + extra if extra else "")), found=False, mode="not_found",
            guardrails=[f.as_dict() for f in flags], timings_ms=timings, cache=cache_status,
        )

    def _from_cache(self, hit, flags, timings, started) -> dict[str, Any]:
        e = hit.entry
        timings["total"] = round((time.perf_counter() - started) * 1000, 1)
        return self._response(
            answer=e["answer"], found=e["found"], mode="cache", sources=e.get("sources", []),
            cache={"status": hit.kind, "distance": round(hit.distance, 4) if hit.kind == "semantic" else 0.0,
                   "saved_mode": e.get("mode")},
            guardrails=[f.as_dict() for f in flags] + e.get("guardrails", []), timings_ms=timings,
        )

    def _missing_documents(self, question: str) -> tuple[list[str], bool]:
        codes = {c.upper() for c in DOC_CODE_RE.findall(question)}
        if not codes:
            return [], False
        try:
            known = {k.upper() for k in self.store.known_doc_ids()}
        except Exception:
            return [], False
        missing = sorted(c for c in codes if c not in known)
        return missing, bool(codes & known)

    # ------------------------------------------------------------------ main entry
    def answer(self, question: str, identity: Identity) -> dict[str, Any]:
        started = time.perf_counter()
        timings: dict[str, float] = {}
        flags: list[gr.Flag] = []

        # 1. input guardrails
        inp = gr.check_input(question, self.s.max_query_chars)
        flags += inp.flags
        if not inp.ok:
            return self._response(
                answer=gr.REFUSALS[inp.reason], found=False, mode="blocked",
                guardrails=[f.as_dict() for f in flags],
                timings_ms={"total": round((time.perf_counter() - started) * 1000, 1)},
            )
        q = inp.text

        # 2. exact cache
        t = time.perf_counter()
        hit = self.cache.lookup_exact(identity, q)
        timings["cache_exact"] = round((time.perf_counter() - t) * 1000, 1)
        if hit:
            return self._from_cache(hit, flags, timings, started)

        # 3. document named in the question that is not in the knowledge base
        missing, mentions_known = self._missing_documents(q)
        if missing and not mentions_known:
            names = ", ".join(missing)
            extra = f"{names} {'is' if len(missing) == 1 else 'are'} referenced in other documents but not in the knowledge base."
            timings["total"] = round((time.perf_counter() - started) * 1000, 1)
            return self._not_found(flags + [gr.Flag("retrieval", "missing_document", names)], timings,
                                   {"status": "miss", "distance": None}, extra)

        # 4. embed the question once. Reused for similar-question cache and search.
        t = time.perf_counter()
        qe = self.embedder.embed_query(q)
        timings["embed"] = round((time.perf_counter() - t) * 1000, 1)

        t = time.perf_counter()
        hit = self.cache.lookup_semantic(identity, q, qe.dense)
        timings["cache_semantic"] = round((time.perf_counter() - t) * 1000, 1)
        if hit:
            return self._from_cache(hit, flags, timings, started)

        # 5. retrieve. The permission filter runs inside the Qdrant search.
        t = time.perf_counter()
        hits = self.store.search(qe, identity, limit=self.s.retrieve_top_k)
        timings["search"] = round((time.perf_counter() - t) * 1000, 1)
        visible = [h for h in hits if chunk_visible(h.payload, identity)]  # defence in depth
        if len(visible) != len(hits):
            flags.append(gr.Flag("retrieval", "permission_recheck_removed", f"{len(hits) - len(visible)} chunk(s)"))
        hits = visible

        # 6. rerank (optional) and relevance floor
        t = time.perf_counter()
        if self.reranker is not None and hits:
            scores = self.reranker.score(q, [h.payload.get("context", "") + "\n" + h.payload.get("text", "") for h in hits])
            for h, sc in zip(hits, scores):
                h.rerank_score = sc
            hits.sort(key=lambda h: h.rerank_score or 0.0, reverse=True)
            hits = [h for h in hits if (h.rerank_score or 0.0) >= self.s.min_rerank_score]
        else:
            hits = [h for h in hits if h.dense_score >= self.s.min_dense_score]
        hits = hits[: self.s.final_top_k]
        timings["rerank"] = round((time.perf_counter() - t) * 1000, 1)

        if not hits:
            timings["total"] = round((time.perf_counter() - started) * 1000, 1)
            resp = self._not_found(flags + [gr.Flag("retrieval", "below_relevance_floor")], timings,
                                   {"status": "miss", "distance": None})
            self._store(identity, q, qe, resp, [])
            return resp

        # 7. build the context. Restricted chunks are shown as found text and never sent to the writer.
        sources: list[dict[str, Any]] = []
        for i, h in enumerate(hits, 1):
            restricted = h.payload.get("classification") == "restricted" and not self.s.send_restricted_to_llm
            sources.append(_source_view(i, h, restricted))
        llm_sources = []
        for s in sources:
            if s["restricted"]:
                continue
            clean, changed = gr.neutralise_context(s["text"])
            if changed:
                flags.append(gr.Flag("retrieval", "context_instruction_removed", f"source {s['n']}"))
            llm_sources.append({**s, "text": clean})
        restricted_sources = [s for s in sources if s["restricted"]]

        # 8. write the answer
        answer_text, mode, used = "", "llm", []
        t = time.perf_counter()
        if llm_sources and self.writer.available:
            try:
                raw = self.writer.answer(q, llm_sources)
            except Exception as exc:
                log.exception("OpenAI call failed")
                timings["total"] = round((time.perf_counter() - started) * 1000, 1)
                return self._response(
                    answer="The answer writer is unavailable right now. Please try again.", mode="error",
                    guardrails=[f.as_dict() for f in flags + [gr.Flag("output", "llm_error", type(exc).__name__)]],
                    timings_ms=timings,
                )
            if raw.strip().upper().startswith("NOT_FOUND") and not restricted_sources:
                timings["llm"] = round((time.perf_counter() - t) * 1000, 1)
                timings["total"] = round((time.perf_counter() - started) * 1000, 1)
                resp = self._not_found(flags + [gr.Flag("output", "writer_not_found")], timings, {"status": "miss", "distance": None})
                self._store(identity, q, qe, resp, [])
                return resp
            if not raw.strip().upper().startswith("NOT_FOUND"):
                out = gr.check_output(raw, len(sources), {s["n"]: s["text"] for s in llm_sources})
                flags += out.flags
                allowed = {s["n"] for s in llm_sources}
                used = [n for n in out.cited if n in allowed]
                if not out.ok or not used:
                    timings["total"] = round((time.perf_counter() - started) * 1000, 1)
                    return self._not_found(flags + [gr.Flag("output", "ungrounded_answer_blocked")], timings,
                                           {"status": "miss", "distance": None})
                answer_text = out.text
        elif llm_sources and not self.writer.available:
            mode = "extractive"
            flags.append(gr.Flag("output", "writer_not_configured", "set OPENAI_API_KEY and OPENAI_MODEL"))
            lines = ["The answer writer is not configured, so these are the most relevant passages:"]
            for s in llm_sources[:3]:
                lines.append(f"\n[{s['n']}] {s['text']}")
            answer_text = "\n".join(lines)
            used = [s["n"] for s in llm_sources[:3]]
        timings["llm"] = round((time.perf_counter() - t) * 1000, 1)

        if restricted_sources:
            lines = ["Found in restricted records. These rows are shown as found text and were not sent to the AI model:"]
            for s in restricted_sources:
                masked, _ = gr.mask_sensitive(s["text"])
                lines.append(f"\n[{s['n']}] {masked}")
            block = "\n".join(lines)
            answer_text = (answer_text + "\n\n" + block).strip() if answer_text else block
            used += [s["n"] for s in restricted_sources]
            if not llm_sources:
                mode = "extractive"
            flags.append(gr.Flag("retrieval", "restricted_not_sent_to_llm", f"{len(restricted_sources)} chunk(s)"))

        shown = [public_source(s) for s in sources if s["n"] in set(used)]
        timings["total"] = round((time.perf_counter() - started) * 1000, 1)
        resp = self._response(
            answer=answer_text, found=True, mode=mode, sources=shown,
            guardrails=[f.as_dict() for f in flags], timings_ms=timings,
            cache={"status": "miss" if getattr(self.cache, "enabled", False) else "off", "distance": None},
        )
        if mode == "llm":  # do not save passages-only fallbacks, so answers are rewritten once a key is set
            self._store(identity, q, qe, resp, sorted({s["doc_id"] for s in shown if s.get("doc_id")}))
        return resp

    def _store(self, identity, q, qe, resp, doc_ids) -> None:
        entry = {k: resp[k] for k in ("answer", "found", "mode", "sources")}
        entry["guardrails"] = [g for g in resp.get("guardrails", []) if g["stage"] in {"output", "retrieval"}]
        self.cache.store(identity, q, qe.dense, entry, doc_ids)
