"""Redact PII from text: NRIC, phone, email, credit card.

Returns (redacted_text, pii_kinds) where:
  - redacted_text: original with PII replaced by [TYPE_<hash>] markers
  - pii_kinds: list of {"kind": str, "hash_short": str} for the detected types
"""
from __future__ import annotations

import hashlib
import re


# SG NRIC/FIN: S/T/F/G/M followed by 7 digits and check letter
_NRIC_RE = re.compile(r'\b[STFGM]\d{7}[A-Z]\b', re.IGNORECASE)

# SG phone: +65 optional, then 8 or 9 (mobile), then 7 more digits, spaces optional
_PHONE_RE = re.compile(r'(?:\+?65\s*)?[89]\d{3}\s?\d{4}\b')

# Email: standard pattern
_EMAIL_RE = re.compile(r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b')

# Credit card: 13-19 digits, optional dashes/spaces
_CCNUM_RE = re.compile(r'\b(?:\d[\s\-]?){12}(?:\d[\s\-]?)\d[\s\-]?\d{1,3}\b')


def _hash_short(text: str) -> str:
    """Hash text to 8-char hex."""
    h = hashlib.sha256(text.encode()).hexdigest()
    return h[:8]


def redact(text: str) -> tuple[str, list[dict]]:
    """Redact PII from text.

    Args:
        text: plaintext input, possibly containing PII

    Returns:
        (redacted_text, pii_kinds)
        - redacted_text: text with PII replaced by [TYPE_<hash>]
        - pii_kinds: list of {"kind": str, "hash_short": str} for each pattern
                     that matched (deduped by kind)
    """
    if not text:
        return text, []

    found = {}  # kind -> (hash, _)
    result = text

    # NRIC/FIN
    for m in _NRIC_RE.finditer(text):
        h = _hash_short(m.group())
        found.setdefault('nric', h)
    result = _NRIC_RE.sub(lambda m: f'[NRIC_{_hash_short(m.group())}]', result)

    # Phone
    for m in _PHONE_RE.finditer(text):
        h = _hash_short(m.group())
        found.setdefault('phone', h)
    result = _PHONE_RE.sub(lambda m: f'[PHONE_{_hash_short(m.group())}]', result)

    # Email
    for m in _EMAIL_RE.finditer(text):
        h = _hash_short(m.group())
        found.setdefault('email', h)
    result = _EMAIL_RE.sub(lambda m: f'[EMAIL_{_hash_short(m.group())}]', result)

    # Credit card
    for m in _CCNUM_RE.finditer(text):
        h = _hash_short(m.group())
        found.setdefault('ccnum', h)
    result = _CCNUM_RE.sub(lambda m: f'[CCNUM_{_hash_short(m.group())}]', result)

    pii_kinds = [{"kind": k, "hash_short": v} for k, v in found.items()]
    return result, pii_kinds
