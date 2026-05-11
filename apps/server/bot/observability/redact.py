"""PII redaction for OTel span attributes before export."""

from __future__ import annotations

import re

# Phone: 10-digit US numbers, common separators
_PHONE_RE = re.compile(r"\b(\+?1[-.\s]?)?(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})\b")

# Date of birth patterns: YYYY-MM-DD, MM/DD/YYYY, DD-MM-YYYY
_DOB_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\b")

# Simple email
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")


def redact(value: str) -> str:
    """Apply PII redaction patterns to a string span attribute value."""
    value = _PHONE_RE.sub("[PHONE]", value)
    value = _DOB_RE.sub("[DOB]", value)
    value = _EMAIL_RE.sub("[EMAIL]", value)
    return value


def redact_span_attributes(attrs: dict[str, str]) -> dict[str, str]:
    """Redact PII from a dict of OTel span attributes (string values only)."""
    return {k: redact(v) if isinstance(v, str) else v for k, v in attrs.items()}
