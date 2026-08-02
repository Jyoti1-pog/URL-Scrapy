"""Logging: rich console for humans, one JSON line per URL for machines.

Redaction is applied unconditionally, not as an opt-in. Credentials reach this
module by accident (a signed URL in a candidate list, an API key in a query
string) and the only safe assumption is that they will.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

console = Console(stderr=True)

_REDACT_KEYS = re.compile(
    r"(?i)\b(api[_-]?key|apikey|client[_-]?id|client[_-]?secret|secret|password|passwd|token|"
    r"authorization|auth|signature|x-amz-signature|access[_-]?key)\b"
)
_REDACT_QUERY = re.compile(
    r"(?i)([?&](?:api[_-]?key|key|token|sig|signature|access_token|client_secret)=)[^&\s]+"
)
_REDACTED = "***REDACTED***"


def redact(value: Any) -> Any:
    """Scrub secrets from anything on its way to a log sink."""
    if isinstance(value, str):
        return _REDACT_QUERY.sub(rf"\1{_REDACTED}", value)
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _REDACT_KEYS.search(str(k)) else redact(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "event", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), ensure_ascii=False, default=str)


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            record.event = redact(event)
        return True


def setup_logging(verbose: int = 0, log_file: Path | None = None) -> None:
    level = logging.WARNING if verbose == 0 else logging.INFO if verbose == 1 else logging.DEBUG

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    handler = RichHandler(console=console, show_path=False, rich_tracebacks=True, markup=False)
    handler.setLevel(level)
    handler.addFilter(_RedactingFilter())
    root.addHandler(handler)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_JsonFormatter())
        fh.addFilter(_RedactingFilter())
        root.addHandler(fh)

    # Third-party chatter is never what the operator wants to read.
    for noisy in ("httpx", "httpcore", "hpack", "PIL", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_row(logger: logging.Logger, **fields: Any) -> None:
    """The §10 structured per-URL line. `tier1_attempted` is always present and
    always true -- if it is ever missing, the image pipeline was bypassed."""
    logger.info(fields.get("status", "row"), extra={"event": fields})
