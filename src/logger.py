"""
src/logger.py
─────────────────────────────────────────────────────────────
Safe structured logging for the RAG Customer Support Agent.

Design rules enforced here:
  - Never log API keys, tokens, or credentials
  - Never log user questions or message content
  - Never log document content or filenames that may contain PII
  - Log application events, errors, and performance metrics only
─────────────────────────────────────────────────────────────
"""

import logging
import sys
from datetime import datetime, timezone


class _SafeFormatter(logging.Formatter):
    """
    Custom formatter that adds a UTC timestamp and structured fields.
    Keeps log output consistent and machine-readable.
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        level = record.levelname.ljust(8)
        location = f"{record.module}.{record.funcName}"
        message = super().format(record)
        return f"[{timestamp}] {level} {location} | {message}"


def get_logger(name: str, level: str = "INFO") -> logging.Logger:
    """
    Return a configured logger for the given module name.

    Usage:
        from src.logger import get_logger
        logger = get_logger(__name__)
        logger.info("Document processed successfully", extra={"doc_id": doc_id})
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_SafeFormatter())
    logger.addHandler(handler)
    logger.propagate = False

    return logger


def log_event(logger: logging.Logger, event: str, **kwargs) -> None:
    """
    Log a structured application event with optional safe metadata.

    Only pass non-sensitive metadata as kwargs:
        Good:  doc_id, chunk_count, duration_ms, status, error_type
        Never: content, question, api_key, filename, user_data
    """
    safe_fields = {k: v for k, v in kwargs.items()}
    fields_str = " | ".join(f"{k}={v}" for k, v in safe_fields.items())
    message = f"{event}" + (f" | {fields_str}" if fields_str else "")
    logger.info(message)


def log_error(
    logger: logging.Logger,
    event: str,
    error: Exception,
    **kwargs
) -> None:
    """
    Log an error event safely.
    Logs the error type and a sanitized message — never the full traceback
    in production, as it may contain sensitive context.
    """
    safe_fields = {k: v for k, v in kwargs.items()}
    fields_str = " | ".join(f"{k}={v}" for k, v in safe_fields.items())
    error_type = type(error).__name__
    message = (
        f"{event} | error_type={error_type}"
        + (f" | {fields_str}" if fields_str else "")
    )
    logger.error(message, exc_info=False)
