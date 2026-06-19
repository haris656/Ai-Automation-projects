"""
config.py
─────────────────────────────────────────────────────────────
Central configuration for the RAG Customer Support Agent.
All values are loaded from environment variables.
Nothing is hardcoded here.
─────────────────────────────────────────────────────────────
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    """Load a required environment variable or raise a clear error."""
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Copy .env.example to .env and fill in your values."
        )
    return value


def _optional(key: str, default: str) -> str:
    """Load an optional environment variable with a fallback default."""
    return os.getenv(key, default)


@dataclass(frozen=True)
class OpenAIConfig:
    api_key: str
    model: str
    embedding_model: str


@dataclass(frozen=True)
class PineconeConfig:
    api_key: str
    index_name: str
    environment: str


@dataclass(frozen=True)
class RAGConfig:
    chunk_size: int
    chunk_overlap: int
    retrieval_top_k: int
    confidence_threshold: float


@dataclass(frozen=True)
class UploadConfig:
    max_file_size_mb: int
    allowed_extensions: list[str]


@dataclass(frozen=True)
class AppConfig:
    title: str
    max_conversation_history: int
    log_level: str
    log_to_file: bool


@dataclass(frozen=True)
class Config:
    openai: OpenAIConfig
    pinecone: PineconeConfig
    rag: RAGConfig
    upload: UploadConfig
    app: AppConfig


def load_config() -> Config:
    """
    Load and validate all configuration from environment variables.
    Called once at application startup.
    Raises EnvironmentError if any required variable is missing.
    """
    return Config(
        openai=OpenAIConfig(
            api_key=_require("OPENAI_API_KEY"),
            model=_optional("OPENAI_MODEL", "gpt-4-turbo-preview"),
            embedding_model=_optional(
                "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"
            ),
        ),
        pinecone=PineconeConfig(
            api_key=_require("PINECONE_API_KEY"),
            index_name=_optional(
                "PINECONE_INDEX_NAME", "customer-support-agent"
            ),
            environment=_optional("PINECONE_ENVIRONMENT", "gcp-starter"),
        ),
        rag=RAGConfig(
            chunk_size=int(_optional("CHUNK_SIZE", "600")),
            chunk_overlap=int(_optional("CHUNK_OVERLAP", "100")),
            retrieval_top_k=int(_optional("RETRIEVAL_TOP_K", "5")),
            confidence_threshold=float(
                _optional("CONFIDENCE_THRESHOLD", "0.75")
            ),
        ),
        upload=UploadConfig(
            max_file_size_mb=int(_optional("MAX_FILE_SIZE_MB", "10")),
            allowed_extensions=_optional(
                "ALLOWED_EXTENSIONS", "pdf,txt"
            ).split(","),
        ),
        app=AppConfig(
            title=_optional("APP_TITLE", "AI Customer Support Agent"),
            max_conversation_history=int(
                _optional("MAX_CONVERSATION_HISTORY", "10")
            ),
            log_level=_optional("LOG_LEVEL", "INFO"),
            log_to_file=_optional("LOG_TO_FILE", "false").lower() == "true",
        ),
    )
