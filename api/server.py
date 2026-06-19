"""
api/server.py
─────────────────────────────────────────────────────────────
FastAPI backend for the Knowledge Agent.

Exposes a small, clean REST API over the RAG engine:
  POST /api/documents     upload + index a document
  GET  /api/documents     list indexed documents
  DELETE /api/documents/{doc_id}   remove a document
  POST /api/query         ask a question
  GET  /api/stats         session statistics
  GET  /health            health check

The same validated, secure RAG components power this API.
Session state (conversation memory, doc registry) is kept
server-side keyed by a session id passed from the client.
─────────────────────────────────────────────────────────────
"""

import sys
from pathlib import Path

# Allow importing the project's src package and config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import load_config
from src.document_processor import process_document
from src.generator import ResponseGenerator
from src.logger import get_logger, log_event, log_error
from src.memory import ConversationMemory
from src.validator import validate_file, validate_query
from src.vector_store import VectorStore

logger = get_logger(__name__)


# ─── Session management (server-side, in-memory) ──────────────────────────────
@dataclass
class Session:
    memory: ConversationMemory
    documents: dict = field(default_factory=dict)  # filename -> {doc_id, chunk_count}
    query_count: int = 0

    @property
    def total_chunks(self) -> int:
        return sum(d["chunk_count"] for d in self.documents.values())


class AppState:
    """Holds shared services and per-session state."""
    def __init__(self):
        self.config = None
        self.vector_store = None
        self.generator = None
        self.sessions: dict[str, Session] = {}

    def get_session(self, session_id: str) -> Session:
        if session_id not in self.sessions:
            self.sessions[session_id] = Session(
                memory=ConversationMemory(
                    max_history=self.config.app.max_conversation_history
                )
            )
        return self.sessions[session_id]


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize services once on startup."""
    try:
        state.config = load_config()
        state.vector_store = VectorStore(
            pinecone_api_key=state.config.pinecone.api_key,
            pinecone_environment=state.config.pinecone.environment,
            index_name=state.config.pinecone.index_name,
            openai_api_key=state.config.openai.api_key,
            embedding_model=state.config.openai.embedding_model,
        )
        state.generator = ResponseGenerator(
            openai_api_key=state.config.openai.api_key,
            model=state.config.openai.model,
        )
        log_event(logger, "api_startup_complete")
    except Exception as e:
        log_error(logger, "api_startup_failed", e)
        raise
    yield
    log_event(logger, "api_shutdown")


app = FastAPI(title="Knowledge Agent API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For local dev. Restrict in production.
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Request/Response models ──────────────────────────────────────────────────
class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)


class QueryResponse(BaseModel):
    answer: str
    sources: list[str]
    had_context: bool


class DocumentInfo(BaseModel):
    filename: str
    doc_id: str
    chunk_count: int


class StatsResponse(BaseModel):
    documents: int
    chunks: int
    queries: int


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _session_id(x_session_id: str | None) -> str:
    return x_session_id or "default"


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/stats", response_model=StatsResponse)
async def get_stats(x_session_id: str | None = Header(default=None)):
    session = state.get_session(_session_id(x_session_id))
    return StatsResponse(
        documents=len(session.documents),
        chunks=session.total_chunks,
        queries=session.query_count,
    )


@app.get("/api/documents", response_model=list[DocumentInfo])
async def list_documents(x_session_id: str | None = Header(default=None)):
    session = state.get_session(_session_id(x_session_id))
    return [
        DocumentInfo(filename=name, doc_id=meta["doc_id"], chunk_count=meta["chunk_count"])
        for name, meta in session.documents.items()
    ]


@app.post("/api/documents", response_model=DocumentInfo)
async def upload_document(
    file: UploadFile = File(...),
    x_session_id: str | None = Header(default=None),
):
    session = state.get_session(_session_id(x_session_id))
    config = state.config

    file_bytes = await file.read()
    filename = file.filename or "document"

    validation = validate_file(
        file_bytes=file_bytes,
        filename=filename,
        allowed_extensions=config.upload.allowed_extensions,
        max_size_mb=config.upload.max_file_size_mb,
    )
    if not validation.is_valid:
        raise HTTPException(status_code=400, detail=validation.error_message)

    if filename in session.documents:
        raise HTTPException(status_code=409, detail="Document already indexed.")

    result = process_document(
        file_bytes=file_bytes,
        filename=filename,
        chunk_size=config.rag.chunk_size,
        chunk_overlap=config.rag.chunk_overlap,
    )
    if not result.success:
        raise HTTPException(status_code=422, detail=result.error_message)

    success = state.vector_store.upsert_chunks(result.chunks)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to index document.")

    session.documents[filename] = {
        "doc_id": result.doc_id,
        "chunk_count": result.chunk_count,
    }
    log_event(logger, "document_indexed", doc_id=result.doc_id,
              chunk_count=result.chunk_count)

    return DocumentInfo(
        filename=filename,
        doc_id=result.doc_id,
        chunk_count=result.chunk_count,
    )


@app.delete("/api/documents/{doc_id}")
async def delete_document(
    doc_id: str,
    x_session_id: str | None = Header(default=None),
):
    session = state.get_session(_session_id(x_session_id))

    target = None
    for name, meta in session.documents.items():
        if meta["doc_id"] == doc_id:
            target = name
            break

    if target is None:
        raise HTTPException(status_code=404, detail="Document not found.")

    state.vector_store.delete_document(doc_id)
    del session.documents[target]
    log_event(logger, "document_removed", doc_id=doc_id)
    return {"status": "deleted", "doc_id": doc_id}


@app.post("/api/query", response_model=QueryResponse)
async def query(
    request: QueryRequest,
    x_session_id: str | None = Header(default=None),
):
    session = state.get_session(_session_id(x_session_id))
    config = state.config

    validation = validate_query(request.query)
    if not validation.is_valid:
        raise HTTPException(status_code=400, detail=validation.error_message)

    clean_query = validation.sanitized_value

    if not session.documents:
        raise HTTPException(
            status_code=400,
            detail="No documents indexed. Upload a document first.",
        )

    doc_ids = [meta["doc_id"] for meta in session.documents.values()]

    results = state.vector_store.search(
        query=clean_query,
        top_k=config.rag.retrieval_top_k,
        confidence_threshold=config.rag.confidence_threshold,
        doc_ids=doc_ids,
    )

    generation = state.generator.generate(
        query=clean_query,
        search_results=results,
        memory=session.memory,
    )

    session.memory.add_user_message(clean_query)
    session.memory.add_assistant_message(generation.answer)
    session.query_count += 1

    log_event(logger, "query_complete",
              had_context=generation.had_relevant_context,
              source_count=len(generation.sources))

    return QueryResponse(
        answer=generation.answer,
        sources=generation.sources,
        had_context=generation.had_relevant_context,
    )


@app.delete("/api/conversation")
async def clear_conversation(x_session_id: str | None = Header(default=None)):
    session = state.get_session(_session_id(x_session_id))
    session.memory.clear()
    session.query_count = 0
    return {"status": "cleared"}


# ─── Serve frontend ───────────────────────────────────────────────────────────
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/")
async def serve_index():
    return FileResponse(_FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")
