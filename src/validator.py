"""
src/validator.py
─────────────────────────────────────────────────────────────
Input validation and sanitization for all user-supplied data.

Covers:
  - File upload validation (type, size, content safety)
  - User query validation and sanitization
  - Prompt injection detection and mitigation
─────────────────────────────────────────────────────────────
"""

import re
from dataclasses import dataclass
from typing import Optional

import bleach

from src.logger import get_logger

logger = get_logger(__name__)

# Maximum query length in characters
MAX_QUERY_LENGTH = 1000

# Patterns associated with prompt injection attempts.
# This is not an exhaustive list — it is a first line of defense.
# The real protection is architectural: user input is always kept
# separate from system instructions in the prompt builder.
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"you\s+are\s+now\s+(a\s+)?(?!a\s+customer)",
    r"forget\s+(everything|all)\s+(you\s+know|above)",
    r"new\s+instructions?\s*:",
    r"system\s*:\s*you",
    r"<\s*system\s*>",
    r"\[system\]",
    r"do\s+anything\s+now",
    r"jailbreak",
    r"dan\s+mode",
]

_COMPILED_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS
]


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    sanitized_value: Optional[str] = None
    error_message: Optional[str] = None


def validate_file(
    file_bytes: bytes,
    filename: str,
    allowed_extensions: list[str],
    max_size_mb: int,
) -> ValidationResult:
    """
    Validate an uploaded file for type, size, and basic safety.

    Args:
        file_bytes: Raw file content as bytes.
        filename: Original filename from the upload.
        allowed_extensions: List of permitted extensions e.g. ['pdf', 'txt'].
        max_size_mb: Maximum allowed file size in megabytes.

    Returns:
        ValidationResult with is_valid flag and error message if invalid.
    """
    # Extension check — allowlist only, never blocklist
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in allowed_extensions:
        log_event = f"file_rejected | reason=invalid_extension | ext={extension}"
        logger.warning(log_event)
        return ValidationResult(
            is_valid=False,
            error_message=(
                f"File type '.{extension}' is not supported. "
                f"Please upload one of: {', '.join(allowed_extensions)}"
            ),
        )

    # Size check
    max_bytes = max_size_mb * 1024 * 1024
    if len(file_bytes) > max_bytes:
        size_mb = len(file_bytes) / (1024 * 1024)
        logger.warning(
            f"file_rejected | reason=too_large | size_mb={size_mb:.1f}"
        )
        return ValidationResult(
            is_valid=False,
            error_message=(
                f"File size ({size_mb:.1f} MB) exceeds the "
                f"{max_size_mb} MB limit."
            ),
        )

    # Empty file check
    if len(file_bytes) == 0:
        logger.warning("file_rejected | reason=empty_file")
        return ValidationResult(
            is_valid=False,
            error_message="The uploaded file appears to be empty.",
        )

    logger.info("file_validated | status=ok")
    return ValidationResult(is_valid=True)


def validate_query(query: str) -> ValidationResult:
    """
    Validate and sanitize a user query before it is passed to the RAG pipeline.

    Steps:
      1. Strip and check for empty input
      2. Enforce length limit
      3. Strip HTML tags (XSS protection)
      4. Detect prompt injection patterns
      5. Return sanitized query

    Args:
        query: Raw user input from the chat interface.

    Returns:
        ValidationResult with sanitized query or error message.
    """
    if not query or not query.strip():
        return ValidationResult(
            is_valid=False,
            error_message="Please enter a question.",
        )

    query = query.strip()

    # Length check
    if len(query) > MAX_QUERY_LENGTH:
        return ValidationResult(
            is_valid=False,
            error_message=(
                f"Your question is too long ({len(query)} characters). "
                f"Please keep it under {MAX_QUERY_LENGTH} characters."
            ),
        )

    # Strip HTML/script tags — bleach handles this safely
    sanitized = bleach.clean(query, tags=[], strip=True)

    # Prompt injection detection
    for pattern in _COMPILED_PATTERNS:
        if pattern.search(sanitized):
            logger.warning("query_rejected | reason=injection_pattern_detected")
            return ValidationResult(
                is_valid=False,
                error_message=(
                    "Your question contains patterns that cannot be processed. "
                    "Please rephrase and try again."
                ),
            )

    logger.info("query_validated | status=ok")
    return ValidationResult(is_valid=True, sanitized_value=sanitized)
