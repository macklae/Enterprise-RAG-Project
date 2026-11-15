"""FastAPI application: chat, documents, ingestion progress, health, and the web page."""
from __future__ import annotations

import logging
import re
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import guardrails as gr
from .access import ADMIN_ROLES, ROLES, make_identity
from .directory import load_managers
from .ingestion.pipeline import ALLOWED_EXT, IngestError, ingest_file
from .services import build_services, prepare

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("swiftpay-rag")

BASE = Path(__file__).resolve().parent.parent
FRONTEND = BASE / "frontend"

svc = build_services()
UPLOAD_DIR = svc.settings.data_dir / "uploads"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    prepare(svc)
    yield


app = FastAPI(title="SwiftPay Knowledge Assistant", version="1.0", lifespan=lifespan)
SAMPLE_QUESTIONS = [
    "How soon must a P1 incident be acknowledged?",
    "What is the hotel limit per night in Mumbai?",
    "Which team owns payments-service?",
    "What are the steps to restore a database?",
    "What are the RPO and RTO of the Payments database?",
    "Who is on call in October?",
    "What is SwiftPay's dress code?",
]


# ---------------------------------------------------------------- rate limit
_hits: dict[str, deque] = defaultdict(deque)


def _rate_ok(key: str) -> bool:
    now = time.time()
    q = _hits[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= svc.settings.rate_limit_per_minute:
        return False
    q.append(now)
    return True


def _identity(role: str, user: Optional[str]):
    try:
        return make_identity(role, user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _require_admin(role: str):
    if role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only HR / L&D and SRE / Operations can change documents.")


# ---------------------------------------------------------------- models
class ChatRequest(BaseModel):
    question: str = Field(..., max_length=5000)


# ---------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    s = svc
    store_ok = s.store.health()
    return {
        "qdrant": store_ok,
        "redis": s.cache.health(),
        "embedding_model_loaded": bool(getattr(s.embedder, "loaded", False)),
        "writer_configured": s.writer.available,
        "writer_model": s.writer.model or None,
        "reranker": s.reranker is not None,
        "chunks": s.store.count() if store_ok else 0,
        "cache_stats": getattr(s.cache, "stats", {}),
    }


@app.get("/api/identities")
def identities():
    return {
        "roles": [{"id": k, "label": v} for k, v in ROLES.items()],
        "managers": load_managers(svc.settings.data_dir),
        "admin_roles": list(ADMIN_ROLES),
        "sample_questions": SAMPLE_QUESTIONS,
    }


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request):
    session = request.state.auth_user
    
    ident = _identity(
    session["role"],
    session["manager_name"],
)
    client = request.client.host if request.client else "local"
    if not _rate_ok(f"{client}:{ident.role}"):
        return JSONResponse(status_code=429, content={
            "answer": gr.REFUSALS["rate"], "found": False, "mode": "blocked", "sources": [],
            "guardrails": [{"stage": "input", "name": "rate_limited", "detail": ""}], "timings_ms": {},
            "cache": {"status": "off", "distance": None},
        })
    result = svc.rag.answer(req.question, ident)
    if result.get("guardrails"):
        log.info("guardrails role=%s flags=%s", ident.role, [g["name"] for g in result["guardrails"]])
    return result


@app.get("/api/documents")
def documents(request: Request):
    session = request.state.auth_user
    ident = _identity(
        session["role"],
        session["manager_name"],
    )
    try:
        return {"documents": svc.store.list_documents(ident)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Vector database not reachable: {exc}")


@app.post("/api/documents")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    classification: str = Form("unlabelled"),
):
    session = request.state.auth_user

    ident = _identity(
        session["role"],
        session["manager_name"],
    )

    _require_admin(ident.role)
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(file.filename or "upload").name)[:120]
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXT))}")
    dest = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{name}"
    with open(dest, "wb") as out:
        size = 0
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > svc.settings.max_upload_mb * 1024 * 1024:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"File is larger than {svc.settings.max_upload_mb} MB.")
            out.write(chunk)
    job = svc.jobs.submit(name, lambda progress: _run_ingest(dest, name, classification, progress))
    return {"job_id": job.id}


def _run_ingest(path: Path, display_name: str, classification: str, progress):
    try:
        res = ingest_file(path, svc.ingest_ctx, progress, classification, display_name)
        return res.__dict__
    except IngestError as exc:
        raise RuntimeError(str(exc))


@app.post("/api/ingest-folder")
def ingest_folder(request: Request):
    session = request.state.auth_user

    ident = _identity(
        session["role"],
        session["manager_name"],
    )

    _require_admin(ident.role)
    folder = svc.settings.documents_dir
    files = sorted(p for p in folder.glob("*") if p.suffix.lower() in ALLOWED_EXT and p.is_file())
    if not files:
        raise HTTPException(status_code=404, detail="No supported files found in the documents folder.")

    from .ingestion.policy import find_policy

    unknown_files = [
        p.name for p in files
        if find_policy(p.name) is None
]

    if unknown_files:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Folder ingestion blocked: documents without a classification policy.",
                "files": unknown_files,
            },
        )

    
    ids = []
    for p in files:
        job = svc.jobs.submit(p.name, lambda progress, p=p: _run_ingest(p, p.name, "internal", progress))
        ids.append(job.id)
    return {"job_ids": ids, "files": [p.name for p in files]}


@app.get("/api/jobs")
def jobs(request: Request):
    session = request.state.auth_user
    _require_admin(session["role"])
    return {"jobs": svc.jobs.recent()}


@app.get("/api/jobs/{job_id}")
def job(job_id: str, request: Request):
    session = request.state.auth_user
    _require_admin(session["role"])

    j = svc.jobs.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Unknown job")
    return j.view()


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str, request: Request):
    session = request.state.auth_user

    ident = _identity(
        session["role"],
        session["manager_name"],
    )

    _require_admin(ident.role)
    svc.store.delete_document(doc_id)
    svc.cache.invalidate_document(doc_id)
    svc.cache.clear_not_found()
    return {"deleted": doc_id}


@app.post("/api/cache/clear")
def clear_cache(request: Request):
    session = request.state.auth_user

    ident = _identity(
        session["role"],
        session["manager_name"],
    )

    _require_admin(ident.role)
    svc.cache.clear_all()
    return {"cleared": True}


# ---------------------------------------------------------------- web page
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


# ---------------------------------------------------------------- authentication boundary
# Middleware executes before route handlers, regardless of its position in this file.
import secrets as _secrets
from .auth import get_session


@app.middleware("http")
async def authentication_boundary(request: Request, call_next):
    path = request.url.path

    if not path.startswith("/api/"):
        return await call_next(request)

    if path == "/api/health" or (
    path == "/api/auth/login" and request.method == "POST"
):
        return await call_next(request)

    token = request.cookies.get("swiftpay_session")
    session = get_session(token) if token else None

    if session is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
        )

    request.state.auth_user = session

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied_csrf = request.headers.get("X-CSRF-Token", "")
        if not _secrets.compare_digest(
            supplied_csrf, session["csrf_token"]
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid CSRF token"},
            )

    return await call_next(request)


# ---------------------------------------------------------------- local demo login
from fastapi import Response
from .auth import authenticate_user, create_session


class LoginRequest(BaseModel):
    username: str
    password: str


_login_attempts: dict[str, deque] = defaultdict(deque)


@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request, response: Response):
    # Simple per-IP login rate limit: 5 attempts per minute.
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = _login_attempts[ip]

    while attempts and now - attempts[0] > 60:
        attempts.popleft()

    if len(attempts) >= 5:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again later.",
        )

    attempts.append(now)

    user = authenticate_user(req.username, req.password)

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password",
        )

    session = create_session(user["id"])

    response.set_cookie(
        key="swiftpay_session",
        value=session["token"],
        httponly=True,
        secure=False,  # Local HTTP demo only; use True with HTTPS.
        samesite="strict",
        max_age=3600,
        path="/",
    )

    return {
        "username": user["username"],
        "role": user["role"],
        "manager_name": user["manager_name"],
        "csrf_token": session["csrf_token"],
    }

@app.get("/api/auth/me")
def current_user(request: Request):
    session = request.state.auth_user

    return {
        "username": session["username"],
        "role": session["role"],
        "manager_name": session["manager_name"],
        "csrf_token": session["csrf_token"],
    }

from .auth import revoke_session


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("swiftpay_session")

    if token:
        revoke_session(token)

    response.delete_cookie(
        key="swiftpay_session",
        path="/",
        samesite="strict",
        httponly=True,
        secure=False,  # Local HTTP demo only
    )

    return {"logged_out": True}