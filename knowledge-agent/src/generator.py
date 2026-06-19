"""
src/generator.py
─────────────────────────────────────────────────────────────
Prompt construction and LLM response generation.

Design principles:
  - User input is ALWAYS separated from system instructions
  - Retrieved context is clearly delimited in the prompt
  - The LLM is explicitly instructed not to answer outside the context
  - Confidence-based fallback is handled here, not in the UI
─────────────────────────────────────────────────────────────
"""

from dataclasses import dataclass
from typing import Optional

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from src.memory import ConversationMemory
from src.vector_store import SearchResult
from src.logger import get_logger, log_event, log_error

logger = get_logger(__name__)

# System prompt — defines the agent's behavior.
# This is never exposed to or modifiable by end users.
_SYSTEM_PROMPT = """You are a helpful and professional customer support assistant.
Your job is to answer customer questions accurately using only the information
provided in the CONTEXT section below. 

Rules you must follow:
1. Answer ONLY from the provided context. Do not use outside knowledge.
2. If the context does not contain enough information to answer the question,
   say clearly: "I don't have information about that in the provided documentation.
   Please contact our support team for further assistance."
3. Be concise and direct. Do not add unnecessary padding or filler.
4. Always cite which document and page your answer comes from at the end,
   using this format: [Source: <document name>, Page <number>]
5. If multiple sources support the answer, cite all of them.
6. Maintain a professional, friendly tone at all times.
7. Never make up information, estimates, or assumptions not in the context."""


@dataclass(frozen=True)
class GenerationResult:
    """Result of a single LLM generation call."""
    answer: str
    sources: list[str]
    had_relevant_context: bool
    error: Optional[str] = None


def _build_context_block(results: list[SearchResult]) -> str:
    """
    Format retrieved chunks into a clearly delimited context block
    that is injected into the prompt separately from user input.
    """
    if not results:
        return "No relevant context found."

    sections = []
    for i, result in enumerate(results, start=1):
        sections.append(
            f"[Chunk {i} | Source: {result.doc_name} | "
            f"Page {result.page_number} | "
            f"Relevance: {result.similarity_score:.2f}]\n"
            f"{result.content}"
        )
    return "\n\n---\n\n".join(sections)


def _extract_sources(results: list[SearchResult]) -> list[str]:
    """Extract unique source citations from search results."""
    seen = set()
    sources = []
    for result in results:
        citation = f"{result.doc_name} (Page {result.page_number})"
        if citation not in seen:
            seen.add(citation)
            sources.append(citation)
    return sources


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _call_openai(
    client: OpenAI,
    model: str,
    messages: list[dict],
) -> str:
    """
    Call the OpenAI chat completion API with retry logic.
    Retries up to 3 times with exponential backoff on transient failures.
    """
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.2,
        max_tokens=1024,
    )
    return response.choices[0].message.content.strip()


class ResponseGenerator:
    """
    Generates LLM responses grounded in retrieved document context.

    This class owns the OpenAI client and is responsible for building
    the prompt structure that keeps user input separate from system
    instructions and retrieved context.
    """

    def __init__(self, openai_api_key: str, model: str) -> None:
        self._client = OpenAI(api_key=openai_api_key)
        self._model = model

    def generate(
        self,
        query: str,
        search_results: list[SearchResult],
        memory: ConversationMemory,
    ) -> GenerationResult:
        """
        Generate a response to a user query using retrieved context.

        Args:
            query: The validated, sanitized user question.
            search_results: Retrieved chunks from the vector store.
            memory: Current conversation history.

        Returns:
            GenerationResult containing the answer, sources, and metadata.
        """
        had_relevant_context = len(search_results) > 0

        if not had_relevant_context:
            fallback = (
                "I don't have information about that in the provided "
                "documentation. Please contact our support team for "
                "further assistance."
            )
            log_event(logger, "generation_fallback", reason="no_context")
            return GenerationResult(
                answer=fallback,
                sources=[],
                had_relevant_context=False,
            )

        context_block = _build_context_block(search_results)
        sources = _extract_sources(search_results)

        # Build the messages list.
        # Structure: system prompt -> conversation history -> current turn.
        # The user's raw query and the context are clearly separated
        # so the model cannot be manipulated via the context content.
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            *memory.get_history_as_dicts(),
            {
                "role": "user",
                "content": (
                    f"CONTEXT:\n{context_block}\n\n"
                    f"QUESTION: {query}"
                ),
            },
        ]

        try:
            answer = _call_openai(self._client, self._model, messages)
            log_event(
                logger,
                "generation_complete",
                source_count=len(sources),
            )
            return GenerationResult(
                answer=answer,
                sources=sources,
                had_relevant_context=True,
            )

        except Exception as e:
            log_error(logger, "generation_failed", e)
            return GenerationResult(
                answer=(
                    "I encountered an error while generating a response. "
                    "Please try again."
                ),
                sources=[],
                had_relevant_context=had_relevant_context,
                error=type(e).__name__,
            )
