"""Structured JSON logging for Cloud Logging.

- Every line is a JSON object on stdout; Cloud Logging maps `severity`, `message`, the trace
  and the source location, and keeps all other keys under jsonPayload.
- `log_context(...)` / `bind(...)` attach fields (job_id, file, message_id, ...) to every
  line logged while handling a request, from any module, without passing them around.
- With the request's trace attached, Logs Explorer groups all lines for one Pub/Sub
  message under its HTTP request log.
"""

import json
import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_context: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})  # noqa: B039

_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName", "log_ctx"}

_base_factory = logging.getLogRecordFactory()


def _record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    # Capture the context when the line is logged, not when a handler formats it later.
    record = _base_factory(*args, **kwargs)
    record.log_ctx = _context.get()
    return record


logging.setLogRecordFactory(_record_factory)

TRACE_KEY = "logging.googleapis.com/trace"


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add fields to every log line inside the block (restored on exit)."""
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def bind(**fields: Any) -> None:
    """Add fields to the current log context (until the enclosing `log_context` exits)."""
    _context.set({**_context.get(), **{k: v for k, v in fields.items() if v is not None}})


def trace_field(header: str | None, project: str) -> dict[str, str]:
    """Build the Cloud Logging trace field from an `X-Cloud-Trace-Context: TRACE_ID/SPAN;o=1` header."""
    if not header:
        return {}
    trace_id = header.split("/", 1)[0]
    return {TRACE_KEY: f"projects/{project}/traces/{trace_id}"} if trace_id else {}


@contextmanager
def step(log: logging.Logger, name: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Log one line when a step finishes, with its duration and any results the block adds.

    with step(log, "download", uri=uri) as s:
        ...
        s["bytes"] = size
    """
    # stacklevel=3 attributes the log line to the caller's `with` statement, not this helper.
    info: dict[str, Any] = dict(fields)
    start = time.perf_counter()
    log.debug("%s started", name, extra={"step": name, **info}, stacklevel=3)
    try:
        yield info
    except Exception as exc:
        ms = round((time.perf_counter() - start) * 1000)
        log.warning(
            "%s failed after %d ms: %s", name, ms, exc, extra={"step": name, "duration_ms": ms, **info}
        )
        raise
    ms = round((time.perf_counter() - start) * 1000)
    details = " ".join(f"{k}={v}" for k, v in info.items())
    log.info(
        "%s done in %d ms %s",
        name,
        ms,
        details,
        extra={"step": name, "duration_ms": ms, **info},
        stacklevel=3,
    )


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "logging.googleapis.com/sourceLocation": {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            },
        }
        entry.update(getattr(record, "log_ctx", {}))
        # Anything passed via `extra=` becomes a top-level field.
        entry.update({k: v for k, v in vars(record).items() if k not in _STD_ATTRS})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel((level or os.getenv("LOG_LEVEL", "INFO")).upper())

    # Route uvicorn's own logs through the same JSON handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # Client libraries are very chatty at DEBUG.
    for name in ("google", "urllib3", "google.auth", "google.cloud"):
        logging.getLogger(name).setLevel(logging.WARNING)
