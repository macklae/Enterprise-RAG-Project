# SwiftPay Knowledge Assistant (RAG)

Answers questions from SwiftPay documents. Every answer shows its document, section and page. If the documents do not contain the answer it says "Not found". Each role sees only what it may see.

## Run it

1. Install Docker Desktop (or Docker Engine) and Python 3.10 or newer.
2. Put the 8 sample files in `data/documents/`.
3. Start Qdrant and Redis:
   ```
   cp .env.example .env        # then edit .env
   docker compose up -d
   ```
4. Install and start the app:
   ```
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   uvicorn app.api:app --port 8000
   ```
5. Open http://localhost:8000. Choose the role "SRE / Operations" or "HR / L&D", click **Load documents folder**, and watch the progress bars.

`.env` must contain `OPENAI_API_KEY` and `OPENAI_MODEL`. Set `QDRANT_API_KEY` to the same value docker compose uses. Without an OpenAI key the app still runs and returns the matching passages.

Word page numbers need LibreOffice (`brew install --cask libreoffice` on a Mac). Without it, Word files are cited by section only.

Command line loading: `python -m scripts.ingest_folder`. Scores: `python -m scripts.evaluate`. Tests: `python -m unittest discover -s tests -t .` (the end-to-end test needs the 8 files in `data/documents`).

## Structure

```
app/
  config.py            settings from .env
  access.py            roles, access labels, permission filter
  guardrails.py        input, retrieval and output guardrails
  embeddings.py        BGE-M3 (dense + sparse), optional BGE reranker
  vectorstore.py       Qdrant: hybrid search with the permission filter inside
  cache.py             Redis: exact + similar-question cache, TTL, invalidation
  llm.py               OpenAI writer and the system prompt
  rag.py               the question-answering flow
  ingestion/           loaders (pdf, docx, excel, html, text), chunking, policy, pipeline
  jobs.py, services.py, api.py
frontend/              index.html, styles.css, app.js
scripts/               ingest_folder.py, evaluate.py
tests/                 unit tests and an end-to-end test with in-memory fakes
```

## Chunking (as in the proposal)

| Route | Files | Method |
|---|---|---|
| A | PDF, Word | Structure-aware: one section is one chunk. A table stays with its header row and its introducing sentence. Long tables split by rows and repeat the header. Word is converted to PDF to get pages. |
| B | Excel | Row-based: one chunk per row written as labelled facts. Small reference sheets (About, Teams, Tiers, Summary) are one chunk each. |
| C | Web pages | Menus, scripts and styles removed, then cut at headings. |
| fallback | any long paragraph | Recursive split (paragraph, line, sentence) with a small overlap. |

Every chunk carries a label card: document, version, section, page or sheet and row, classification, allowed roles and team.

## Guardrails

Input: length and empty check, control characters removed, prompt-injection patterns blocked, phone, card (Luhn checked), Aadhaar, PAN and API keys masked before the question is cached, embedded or sent anywhere.

Retrieval: role filter inside the Qdrant search plus a second check in Python, relevance floor (below it the answer is "Not found"), instruction-like lines inside retrieved text removed, restricted chunks never sent to OpenAI (shown as found text instead), "Not found" names a referenced document that is not in the knowledge base.

Output: the answer must cite real sources or it is replaced by "Not found", numbers must appear in the cited text (otherwise flagged), personal data masked, system-prompt canary check, length cap.

Also: rate limit per client and role, upload type, size and format checks, upload and delete limited to HR / L&D and SRE / Operations.

## Notes

- Identity is a demo: the page sends a role (and a manager name for Line Managers). Replace it with real sign-in before real use.
- The cache is custom Redis code and works on plain Redis. It does not use RedisVL.
- `MIN_DENSE_SCORE` and the cache distance start as sensible guesses. Tune them with `scripts/evaluate.py`.
- Restricted rows are shown as found text, so questions that need a summary over many restricted rows return the matching rows rather than a written summary.
