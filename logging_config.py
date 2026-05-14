"""
Central logging setup for IRRES API and sync scripts.

Environment:
  LOG_LEVEL   — DEBUG, INFO, WARNING, ERROR, CRITICAL (default: INFO)
  LOG_JSON=1  — Emit one JSON object per line (for log aggregators)

Grep examples:
  grep 'event=http_request' logs.txt
  grep 'request_id=' logs.txt
  grep 'event=security_' logs.txt
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Set at sync entry; surfaced as request_id in log format for consistent grepping.
_sync_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "irres_sync_run_id", default=None
)


def begin_sync_run() -> contextvars.Token[str | None]:
    """Call at sync script start; pairs with end_sync_run."""
    import uuid

    return _sync_run_id.set(str(uuid.uuid4()))


def end_sync_run(token: contextvars.Token[str | None]) -> None:
    _sync_run_id.reset(token)


class IrresContextFilter(logging.Filter):
    """
    Adds request_id, client, path for Flask requests; otherwise sync run_id
    as request_id. Per-log extra= can set run_id to override the sync run id
    for a sub-phase.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        req_id = "-"
        client = "-"
        path = "-"
        try:
            from flask import g, has_request_context, request

            if has_request_context():
                req_id = getattr(g, "request_id", None) or "-"
                client = request.remote_addr or "-"
                path = request.path or "-"
        except Exception:
            pass

        if req_id == "-":
            sid = _sync_run_id.get()
            if sid:
                req_id = sid

        if getattr(record, "run_id", None):
            req_id = str(record.run_id)

        record.request_id = req_id
        record.client = getattr(record, "client", None) or client
        record.path = getattr(record, "path", None) or path
        return True


class JsonFormatter(logging.Formatter):
    """One-line JSON per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "client": getattr(record, "client", "-"),
            "path": getattr(record, "path", "-"),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class TerminalFormatter(logging.Formatter):
    """Pipe-separated fields; tokenized message body for ripgrep."""

    def __init__(self) -> None:
        super().__init__(
            fmt=(
                "%(asctime)s | %(levelname)-8s | %(name)s | "
                "request_id=%(request_id)s client=%(client)s path=%(path)s | %(message)s"
            ),
            datefmt="%Y-%m-%dT%H:%M:%S",
        )


_CONTEXT_FILTER = IrresContextFilter()


def _attach_filter_to_handlers() -> None:
    root = logging.getLogger()
    for h in root.handlers:
        if _CONTEXT_FILTER not in h.filters:
            h.addFilter(_CONTEXT_FILTER)


def configure_logging(service: str = "irres", level: str | None = None) -> None:
    """
    Configure root logging once. If handlers already exist (e.g. Gunicorn),
    only set level and ensure IrresContextFilter is attached to existing handlers.

    Environment:
      LOG_LEVEL   — DEBUG, INFO, WARNING, ERROR, CRITICAL (default: INFO)
      LOG_JSON=1  — One JSON object per line on stdout

    Grep: grep 'event=' app.log | grep 'request_id='
    """
    _ = service  # reserved for future per-service tuning
    raw = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    resolved = getattr(logging, raw, logging.INFO)

    root = logging.getLogger()
    root.setLevel(resolved)

    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setLevel(resolved)
        if os.getenv("LOG_JSON") == "1":
            h.setFormatter(JsonFormatter())
        else:
            h.setFormatter(TerminalFormatter())
        h.addFilter(_CONTEXT_FILTER)
        root.addHandler(h)
    else:
        _attach_filter_to_handlers()
