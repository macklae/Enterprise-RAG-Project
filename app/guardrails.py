"""Guardrails. Lightweight, rule-based, no extra services.

INPUT      length and empty check, control characters, prompt-injection patterns,
           sensitive data masking (phone, card, Aadhaar, PAN, secrets).
RETRIEVAL  role filter (also in Qdrant), relevance floor, instruction-like text in
           retrieved chunks neutralised, restricted chunks kept away from the LLM.
OUTPUT     must cite real sources, numbers must appear in the cited text, sensitive data
           masked, system-prompt canary check, length cap.

Each check returns a flag. Flags are returned to the UI and written to the log.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

CANARY = "ZXQ-CANARY-7731"

INJECTION_PATTERNS = [
    r"ignore\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier|preceding)\s+(?:instructions?|rules?|prompts?|messages?)",
    r"disregard\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier|preceding|system)\s+(?:instructions?|rules?|prompts?|messages?)",
    r"forget\s+(?:all\s+|everything\s+|your\s+)?(?:previous|prior|above|earlier)?\s*(?:instructions?|rules?)",
    r"(?:reveal|show|print|repeat|display|leak|output)\s+(?:me\s+)?(?:your|the)\s+(?:system|hidden|initial|original|secret)\s+(?:prompt|instructions?|message)",
    r"what\s+(?:is|are)\s+your\s+(?:system\s+prompt|instructions)",
    r"\byou\s+are\s+now\s+(?!able\b)",
    r"\bact\s+as\s+(?:if\s+you\s+(?:were|are)\s+)?(?:an?\s+|the\s+)?(?:admin|administrator|root|hr|sre|system|developer|unrestricted)",
    r"\bpretend\s+(?:to\s+be|you\s+are|that\s+you|i\s+am)",
    r"\b(?:developer|debug|god|jailbreak|dan)\s+mode\b",
    r"\b(?:bypass|circumvent|skip)\s+(?:the\s+)?(?:permissions?|access\s+(?:control|rules?|checks?)|security|filters?|guardrails?|role\s+checks?)",
    r"\boverride\s+(?:the\s+)?(?:permissions?|access|role|security|guardrails?|instructions?)",
    r"\b(?:disable|turn\s+off)\s+(?:the\s+)?(?:guardrails?|filters?|safety|restrictions?)",
    r"<\s*/?\s*(?:system|assistant|instructions?)\s*>",
    r"\[\s*(?:system|inst)\s*\]",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in INJECTION_PATTERNS), re.I)

# Sensitive data. The first group is masked in questions and in answers.
_PATTERNS = {
    "phone": re.compile(r"(?<![\w])(?:\+\d{1,3}[\s-]?\d{4,5}[\s-]?\d{4,6}|(?<!\d)[6-9]\d{9}(?!\d))"),
    "aadhaar": re.compile(r"(?<!\d)\d{4}[\s-]\d{4}[\s-]\d{4}(?!\d)"),
    "pan": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    "secret": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"),
}
_CARD = re.compile(r"(?<![\d])(?:\d[ -]?){13,19}(?!\d)")

REFUSALS = {
    "empty": "Please type a question.",
    "too_long": "That question is too long. Please shorten it.",
    "injection": "I can only answer questions about the documents. I cannot follow instructions that change my rules or access.",
    "rate": "Too many questions in a short time. Please wait a moment.",
}


@dataclass
class Flag:
    stage: str  # input | retrieval | output
    name: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"stage": self.stage, "name": self.name, "detail": self.detail}


@dataclass
class InputResult:
    ok: bool
    text: str
    reason: str = ""  # key in REFUSALS when blocked
    flags: list[Flag] = field(default_factory=list)


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def mask_sensitive(text: str) -> tuple[str, list[str]]:
    """Replace phone numbers, card numbers, Aadhaar, PAN and secrets. Returns (text, kinds found)."""
    found: list[str] = []

    def card_sub(m):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append("card")
            return "[card number removed]"
        return m.group(0)

    text = _CARD.sub(card_sub, text)
    for kind, rx in _PATTERNS.items():
        def sub(m, kind=kind):
            found.append(kind)
            return f"[{kind} removed]"
        text = rx.sub(sub, text)
    return text, sorted(set(found))


def normalise_input(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------------ input
def check_input(question: str, max_chars: int) -> InputResult:
    raw = question or ""
    if len(raw) > max_chars * 3:
        return InputResult(False, "", "too_long", [Flag("input", "too_long", f"{len(raw)} characters")])
    text = normalise_input(raw)
    if not text:
        return InputResult(False, "", "empty", [Flag("input", "empty")])
    if len(text) > max_chars:
        return InputResult(False, "", "too_long", [Flag("input", "too_long", f"{len(text)} characters")])
    if _INJECTION_RE.search(text):
        return InputResult(False, "", "injection", [Flag("input", "prompt_injection", "instruction override pattern")])
    flags: list[Flag] = []
    masked, kinds = mask_sensitive(text)
    if kinds:
        flags.append(Flag("input", "sensitive_data_masked", ", ".join(kinds)))
    return InputResult(True, masked, "", flags)


# ------------------------------------------------------------------ retrieval
def neutralise_context(text: str) -> tuple[str, bool]:
    """Retrieved text is data. Lines that try to give instructions to the model are removed."""
    changed = False
    out = []
    for line in text.split("\n"):
        if _INJECTION_RE.search(line):
            out.append("[removed: instruction-like text]")
            changed = True
        else:
            out.append(line)
    return "\n".join(out), changed


# ------------------------------------------------------------------ output
_CITE_RE = re.compile(r"\[(\d{1,2})\]")
_NUM_RE = re.compile(r"(?<![\w.-])\d[\d,]*(?:\.\d+)?(?![\w-])")


def cited_numbers(answer: str) -> list[int]:
    return [int(n) for n in _CITE_RE.findall(answer)]


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM_RE.findall(text)}


def unsupported_numbers(answer: str, cited_texts: list[str]) -> list[str]:
    """Numbers in the answer that appear in none of the cited chunks."""
    body = _CITE_RE.sub("", answer)
    body = re.sub(r"(?m)^\s*\d+[.)]\s+", "", body)  # list numbering
    allowed: set[str] = set()
    for t in cited_texts:
        allowed |= _numbers(t)
    return sorted(n for n in _numbers(body) if n not in allowed and n not in {"0", "1"})


@dataclass
class OutputResult:
    ok: bool
    text: str
    cited: list[int]
    flags: list[Flag] = field(default_factory=list)


def check_output(answer: str, n_sources: int, source_texts: dict[int, str], max_chars: int = 4000) -> OutputResult:
    flags: list[Flag] = []
    if CANARY.lower() in answer.lower():
        return OutputResult(False, "", [], [Flag("output", "system_prompt_leak", "blocked")])
    answer = answer.strip()[:max_chars]
    cited = sorted({n for n in cited_numbers(answer) if 1 <= n <= n_sources})
    invalid = sorted({n for n in cited_numbers(answer) if not (1 <= n <= n_sources)})
    if invalid:
        answer = re.sub(r"\[(?:%s)\]" % "|".join(map(str, invalid)), "", answer)
        flags.append(Flag("output", "invalid_citation_removed", ", ".join(map(str, invalid))))
    if not cited:
        return OutputResult(False, "", [], flags + [Flag("output", "no_citation", "answer had no valid source")])
    bad = unsupported_numbers(answer, [source_texts[i] for i in cited if i in source_texts])
    if bad:
        flags.append(Flag("output", "unsupported_numbers", ", ".join(bad[:6])))
    masked, kinds = mask_sensitive(answer)
    if kinds:
        flags.append(Flag("output", "sensitive_data_masked", ", ".join(kinds)))
    return OutputResult(True, masked, cited, flags)
