"""Redaction applied before anything is stored or sent to a model (spec §9). Logs and tool
results never need credentials, so anything that looks like one is replaced."""

from __future__ import annotations

import re

MAX_TEXT_CHARS = 2000

REDACTIONS = [
    (re.compile(r"Basic\s+[A-Za-z0-9+/=]{8,}"), "Basic [REDACTED]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\$2[aby]?\$\d{2}\$[./A-Za-z0-9]{53}"), "[REDACTED_HASH]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"),
     r"\1\2[REDACTED]"),
]


def redact(value):
    """Removes credentials from strings (recursively in lists and dicts) and caps their length."""
    if isinstance(value, str):
        for pattern, replacement in REDACTIONS:
            value = pattern.sub(replacement, value)
        return value[:MAX_TEXT_CHARS]
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value
