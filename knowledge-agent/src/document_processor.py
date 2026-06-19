"""
src/document_processor.py
─────────────────────────────────────────────────────────────
Handles document loading, text extraction, and chunking.

Supports: PDF, plain text
Chunking strategy: recursive character splitting with overlap
to preserve sentence and paragraph context across chunk boundaries.
─────────────────────────────────────────────────────────────
"""

import io
import hashlib
from dataclasses import dataclass
from typing import Optional

import fitz  # PyMuPDF
from langchain.text_splitter import RecursiveCharacterTextSplitter

from src.logger import get_logger, log_event, log_error

logger = get_logger(__name__)


@dataclass(frozen=True)
class DocumentChunk:
    """A single chunk of text extracted from a document."""
    chunk_id: str          # Unique ID: sha256 of content
    content: str           # The text content of this chunk
    doc_id: str            # Identifier for the source document
    doc_name: str          # Display name of the source document
    page_number: int       # Page number this chunk came from (1-indexed)
    chunk_index: int       # Position of this chunk within the document


@dataclass(frozen=True)
class ProcessingResult:
    """Result of processing a single document."""
    success: bool
    doc_id: str
    doc_name: str
    chunks: list[DocumentChunk]
    chunk_count: int
    error_message: Optional[str] = None


def _generate_doc_id(filename: str, content_sample: bytes) -> str:
    """
    Generate a stable document ID from filename + first 1KB of content.
    This ensures the same file always gets the same ID.
    """
    payload = filename.encode() + content_sample[:1024]
    return hashlib.sha256(payload).hexdigest()[:16]


def _generate_chunk_id(content: str, doc_id: str, index: int) -> str:
    """Generate a unique, stable ID for a specific chunk."""
    payload = f"{doc_id}:{index}:{content[:100]}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _extract_text_from_pdf(file_bytes: bytes) -> list[tuple[str, int]]:
    """
    Extract text from a PDF file page by page.

    Returns:
        List of (text, page_number) tuples — one per page.
        Empty pages are skipped.
    """
    pages = []
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            for page_num, page in enumerate(doc, start=1):
                text = page.get_text("text").strip()
                if text:
                    pages.append((text, page_num))
    except Exception as e:
        raise ValueError(f"Failed to extract text from PDF: {type(e).__name__}") from e
    return pages


def _extract_text_from_txt(file_bytes: bytes) -> list[tuple[str, int]]:
    """
    Extract text from a plain text file.
    Treats the entire file as page 1.
    """
    try:
        text = file_bytes.decode("utf-8", errors="replace").strip()
        if not text:
            return []
        return [(text, 1)]
    except Exception as e:
        raise ValueError(f"Failed to decode text file: {type(e).__name__}") from e


def process_document(
    file_bytes: bytes,
    filename: str,
    chunk_size: int = 600,
    chunk_overlap: int = 100,
) -> ProcessingResult:
    """
    Process an uploaded document into chunks ready for embedding.

    Args:
        file_bytes: Raw file content.
        filename: Original filename (used for display and doc_id generation).
        chunk_size: Target size of each text chunk in characters.
        chunk_overlap: Number of characters to overlap between chunks
                       to preserve context at chunk boundaries.

    Returns:
        ProcessingResult with all chunks or an error message.
    """
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    doc_id = _generate_doc_id(filename, file_bytes)
    doc_name = filename

    log_event(
        logger,
        "document_processing_started",
        doc_id=doc_id,
        extension=extension,
    )

    # Extract raw text by page
    try:
        if extension == "pdf":
            pages = _extract_text_from_pdf(file_bytes)
        elif extension == "txt":
            pages = _extract_text_from_txt(file_bytes)
        else:
            return ProcessingResult(
                success=False,
                doc_id=doc_id,
                doc_name=doc_name,
                chunks=[],
                chunk_count=0,
                error_message=f"Unsupported file type: {extension}",
            )
    except ValueError as e:
        log_error(logger, "document_extraction_failed", e, doc_id=doc_id)
        return ProcessingResult(
            success=False,
            doc_id=doc_id,
            doc_name=doc_name,
            chunks=[],
            chunk_count=0,
            error_message=str(e),
        )

    if not pages:
        return ProcessingResult(
            success=False,
            doc_id=doc_id,
            doc_name=doc_name,
            chunks=[],
            chunk_count=0,
            error_message="No readable text found in the document.",
        )

    # Chunk each page using recursive character splitting.
    # This strategy respects paragraph and sentence boundaries
    # before falling back to character-level splitting.
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", "! ", "? ", " ", ""],
        length_function=len,
    )

    all_chunks: list[DocumentChunk] = []
    chunk_index = 0

    for page_text, page_number in pages:
        raw_chunks = splitter.split_text(page_text)
        for raw_chunk in raw_chunks:
            content = raw_chunk.strip()
            if not content:
                continue
            all_chunks.append(
                DocumentChunk(
                    chunk_id=_generate_chunk_id(content, doc_id, chunk_index),
                    content=content,
                    doc_id=doc_id,
                    doc_name=doc_name,
                    page_number=page_number,
                    chunk_index=chunk_index,
                )
            )
            chunk_index += 1

    log_event(
        logger,
        "document_processing_complete",
        doc_id=doc_id,
        chunk_count=len(all_chunks),
        page_count=len(pages),
    )

    return ProcessingResult(
        success=True,
        doc_id=doc_id,
        doc_name=doc_name,
        chunks=all_chunks,
        chunk_count=len(all_chunks),
    )
