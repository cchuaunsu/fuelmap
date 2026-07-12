"""Station name / address normalization helpers."""

from __future__ import annotations

import re

_ABBREVIATIONS = {
    "ave": "avenue",
    "av": "avenue",
    "blvd": "boulevard",
    "hwy": "highway",
    "rd": "road",
    "cor": "corner",
    "brgy": "barangay",
    "bgy": "barangay",
    "sta": "santa",
    "sto": "santo",
    "gen": "general",
    "qc": "quezon city",
}

_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """Lowercase, strip punctuation, expand common Philippine abbreviations."""
    text = _PUNCT_RE.sub(" ", value.lower())
    tokens = [_ABBREVIATIONS.get(tok, tok) for tok in text.split()]
    return _SPACE_RE.sub(" ", " ".join(tokens)).strip()


def tokenize(value: str) -> set[str]:
    return set(normalize_text(value).split())


def token_jaccard(a: str, b: str) -> float:
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
