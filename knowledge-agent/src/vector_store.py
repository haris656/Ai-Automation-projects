"""
src/vector_store.py
─────────────────────────────────────────────────────────────
Pinecone vector store operations using the Pinecone client
and OpenAI embeddings directly — no langchain-pinecone wrapper.

Handles:
  - Index initialization and connection
  - Upserting document chunk embeddings
  - Similarity search with score filtering
  - Document deletion by doc_id
─────────────────────────────────────────────────────────────
"""

from dataclasses import dataclass
from typing import Optional

from openai import OpenAI
from pinecone import Pinecone, ServerlessSpec

from src.document_processor import DocumentChunk
from src.logger import get_logger, log_event, log_error

logger = get_logger(__name__)

_EMBEDDING_DIMENSION = 1536
_METRIC = "cosine"
_BATCH_SIZE = 100


@dataclass(frozen=True)
class SearchResult:
    """A single result from a similarity search."""
    chunk_id: str
    content: str
    doc_name: str
    page_number: int
    similarity_score: float


class VectorStore:
    """
    Manages the Pinecone vector store for document chunks.
    Uses OpenAI embeddings directly via the OpenAI client.
    """

    def __init__(
        self,
        pinecone_api_key: str,
        pinecone_environment: str,
        index_name: str,
        openai_api_key: str,
        embedding_model: str,
    ) -> None:
        self._index_name = index_name
        self._embedding_model = embedding_model
        self._openai = OpenAI(api_key=openai_api_key)
        self._pc = Pinecone(api_key=pinecone_api_key, environment=pinecone_environment)
        self._index = None
        self._initialize_index()

    def _initialize_index(self) -> None:
        """Connect to Pinecone index, creating it if it does not exist."""
        try:
            existing = [i.name for i in self._pc.list_indexes()]
            if self._index_name not in existing:
                log_event(logger, "pinecone_index_creating", index=self._index_name)
                self._pc.create_index(
                    name=self._index_name,
                    dimension=_EMBEDDING_DIMENSION,
                    metric=_METRIC,
                    spec=ServerlessSpec(cloud="aws", region="us-east-1"),
                )
                log_event(logger, "pinecone_index_created", index=self._index_name)
            else:
                log_event(logger, "pinecone_index_connected", index=self._index_name)

            self._index = self._pc.Index(self._index_name)
            log_event(logger, "vector_store_ready")

        except Exception as e:
            log_error(logger, "pinecone_init_failed", e)
            raise

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a list of texts using OpenAI."""
        response = self._openai.embeddings.create(
            model=self._embedding_model,
            input=texts,
        )
        return [item.embedding for item in response.data]

    def upsert_chunks(self, chunks: list[DocumentChunk]) -> bool:
        """
        Embed and upsert document chunks into Pinecone in batches.

        Args:
            chunks: List of DocumentChunk objects to store.

        Returns:
            True if successful, False otherwise.
        """
        if not chunks:
            return True

        try:
            # Process in batches to stay within API limits
            for i in range(0, len(chunks), _BATCH_SIZE):
                batch = chunks[i:i + _BATCH_SIZE]
                texts = [chunk.content for chunk in batch]
                embeddings = self._embed(texts)

                vectors = [
                    {
                        "id": chunk.chunk_id,
                        "values": embedding,
                        "metadata": {
                            "chunk_id": chunk.chunk_id,
                            "doc_id": chunk.doc_id,
                            "doc_name": chunk.doc_name,
                            "page_number": chunk.page_number,
                            "chunk_index": chunk.chunk_index,
                            "content": chunk.content,
                        },
                    }
                    for chunk, embedding in zip(batch, embeddings)
                ]

                self._index.upsert(vectors=vectors)

            log_event(
                logger,
                "chunks_upserted",
                count=len(chunks),
                doc_id=chunks[0].doc_id,
            )
            return True

        except Exception as e:
            log_error(logger, "upsert_failed", e, chunk_count=len(chunks))
            return False

    def search(
        self,
        query: str,
        top_k: int = 5,
        confidence_threshold: float = 0.75,
        doc_ids: Optional[list[str]] = None,
    ) -> list[SearchResult]:
        """
        Search for the most relevant chunks for a given query.

        Args:
            query: The validated, sanitized user question.
            top_k: Maximum number of results to retrieve.
            confidence_threshold: Minimum similarity score (0-1).
            doc_ids: Optional list of doc_ids to restrict search to.

        Returns:
            List of SearchResult objects sorted by score descending.
        """
        try:
            query_embedding = self._embed([query])[0]

            filter_dict = None
            if doc_ids:
                filter_dict = {"doc_id": {"$in": doc_ids}}

            response = self._index.query(
                vector=query_embedding,
                top_k=top_k,
                include_metadata=True,
                filter=filter_dict,
            )

            results = []
            for match in response.matches:
                score = match.score
                if score >= confidence_threshold:
                    metadata = match.metadata or {}
                    results.append(
                        SearchResult(
                            chunk_id=metadata.get("chunk_id", match.id),
                            content=metadata.get("content", ""),
                            doc_name=metadata.get("doc_name", "Unknown"),
                            page_number=int(metadata.get("page_number", 0)),
                            similarity_score=round(score, 4),
                        )
                    )

            log_event(
                logger,
                "search_complete",
                results_found=len(results),
                threshold=confidence_threshold,
            )
            return results

        except Exception as e:
            log_error(logger, "search_failed", e)
            return []

    def delete_document(self, doc_id: str) -> bool:
        """
        Delete all chunks belonging to a document from Pinecone.

        Args:
            doc_id: The document ID whose chunks should be deleted.

        Returns:
            True if deletion succeeded, False otherwise.
        """
        try:
            self._index.delete(filter={"doc_id": {"$eq": doc_id}})
            log_event(logger, "document_deleted", doc_id=doc_id)
            return True
        except Exception as e:
            log_error(logger, "delete_failed", e, doc_id=doc_id)
            return False
