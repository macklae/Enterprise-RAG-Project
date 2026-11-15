"""Plain data classes shared across the app."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Block:
    """One piece of a section: a paragraph, a list, or a table."""

    kind: str  # "text" | "table"
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)
    page: Optional[int] = None


@dataclass
class Section:
    number: str  # "5.2" or ""
    title: str
    path: list[str]  # heading trail, e.g. ["6. Procedure", "6.3 Escalate"]
    blocks: list[Block] = field(default_factory=list)
    page: Optional[int] = None

    @property
    def label(self) -> str:
        if not self.number:
            return self.title.strip()
        sep = "." if "." not in self.number and self.number[0].isdigit() else ""
        return f"{self.number}{sep} {self.title}".strip()


@dataclass
class DocStructure:
    """Output of the PDF, Word and HTML loaders (Routes A and C)."""

    title: str
    sections: list[Section]
    meta: dict[str, str] = field(default_factory=dict)  # Document ID, Version...


@dataclass
class DocMeta:
    doc_id: str
    doc_name: str
    doc_type: str
    version: str
    source_file: str
    file_hash: str
    classification: str
    allowed_roles: list[str]
    org: str = "swiftpay"  # "swiftpay" or "external"


@dataclass
class Chunk:
    """One retrievable piece of text plus its label card."""

    chunk_id: str
    text: str  # body shown to users and the LLM
    context: str  # one-line header added before embedding
    doc: DocMeta
    strategy: str  # structure | recursive | row | sheet
    section: str = ""
    section_path: list[str] = field(default_factory=list)
    page: Optional[int] = None
    page_end: Optional[int] = None
    sheet: Optional[str] = None
    row: Optional[int] = None
    allowed_roles: Optional[list[str]] = None  # chunk-level override
    classification: Optional[str] = None  # chunk-level override
    team_manager: str = "*"  # "*" = no team restriction

    @property
    def embed_text(self) -> str:
        return f"{self.context}\n{self.text}" if self.context else self.text

    def payload(self) -> dict[str, Any]:
        d = self.doc
        return {
            "chunk_id": self.chunk_id,
            "doc_id": d.doc_id,
            "doc_name": d.doc_name,
            "doc_type": d.doc_type,
            "version": d.version,
            "source_file": d.source_file,
            "file_hash": d.file_hash,
            "org": d.org,
            "classification": self.classification or d.classification,
            "allowed_roles": list(self.allowed_roles or d.allowed_roles),
            "team_manager": self.team_manager,
            "strategy": self.strategy,
            "section": self.section,
            "section_path": self.section_path,
            "page": self.page,
            "page_end": self.page_end,
            "sheet": self.sheet,
            "row": self.row,
            "context": self.context,
            "text": self.text,
        }


@dataclass
class Hit:
    """A chunk returned by the vector store."""

    id: str
    payload: dict[str, Any]
    fused_score: float = 0.0
    dense_score: float = 0.0
    rerank_score: Optional[float] = None

    @property
    def relevance(self) -> float:
        return self.rerank_score if self.rerank_score is not None else self.dense_score


@dataclass(frozen=True)
class Identity:
    role: str
    user: Optional[str] = None

    @property
    def cache_scope(self) -> str:
        """Saved answers are kept apart per role. Line managers also per person."""
        if self.role == "line_manager":
            return f"{self.role}:{(self.user or '-').lower()}"
        return self.role
