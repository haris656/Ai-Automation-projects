# Knowledge Agent

A production-grade RAG (Retrieval-Augmented Generation) system that answers
questions from your documentation with source citations, conversation memory,
and confidence-based fallback handling. Built with a FastAPI backend and a
custom JavaScript frontend over a Pinecone vector store and OpenAI.

![Python](https://img.shields.io/badge/Python-3.11-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688)
![Pinecone](https://img.shields.io/badge/Pinecone-Vector_DB-purple)
![OpenAI](https://img.shields.io/badge/OpenAI-GPT--4o-412991)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Overview

Upload product documentation and the agent answers questions from that
knowledge base. It retrieves the most relevant passages from a vector store,
generates a grounded answer, cites the exact source, and refuses to answer
when the documentation does not contain the information rather than
hallucinating.

**Capabilities**

- Multi-document retrieval across an indexed knowledge base
- Source citations on every answer (document and page)
- Confidence thresholding to prevent hallucinated responses
- Multi-turn conversation memory for natural follow-up questions
- Clean REST API with a custom single-page frontend

---

## Architecture

```
┌──────────────┐     REST API      ┌────────────────────┐
│   Frontend   │ ───────────────►  │   FastAPI Backend  │
│  (HTML/JS)   │ ◄───────────────  │   (api/server.py)  │
└──────────────┘                   └─────────┬──────────┘
                                             │
              ┌──────────────────────────────┼──────────────────────────┐
              ▼                               ▼                          ▼
      ┌───────────────┐             ┌──────────────────┐       ┌─────────────────┐
      │   Validator   │             │   Vector Store   │       │   Generator     │
      │ (sanitize +   │             │   (Pinecone +    │       │  (GPT-4o, with  │
      │  injection    │             │   OpenAI embed,  │       │  context kept   │
      │  detection)   │             │  confidence cut) │       │  separate from  │
      └───────────────┘             └──────────────────┘       │  user input)    │
                                                               └─────────────────┘
```

The RAG engine (`src/`) is fully decoupled from the API and frontend. The same
validated, secure components could be driven by any interface.

---

## Security

Security was designed in from the start, not added later.

| Area | Implementation |
|---|---|
| Secrets | Loaded only from environment variables, never hardcoded |
| Input validation | File type allowlist, size limits, query sanitization |
| Prompt injection | Pattern detection plus architectural separation of user input from system prompts |
| Data privacy | Documents processed in memory, never persisted to disk |
| Logging | Application events only — never logs queries, content, or keys |
| Dependencies | Pinned to exact versions |
| Disclosure | See [SECURITY.md](SECURITY.md) |

---

## Setup

### Prerequisites

- Python 3.11+
- OpenAI API key
- Pinecone account with an index (dimension 1536, metric cosine)

### Installation

```bash
git clone https://github.com/haris656/Ai-Automation-projects.git
cd Ai-Automation-projects/rag-customer-support

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env              # then fill in your keys
```

### Environment

Fill in `.env`:

```
OPENAI_API_KEY=your_key
OPENAI_MODEL=gpt-4o-mini
PINECONE_API_KEY=your_key
PINECONE_INDEX_NAME=customer-support-agent
PINECONE_ENVIRONMENT=us-east-1
```

### Run

```bash
uvicorn api.server:app --reload --port 8000
```

Open `http://localhost:8000` in your browser.

### Try it

A sample document (`sample_docs/nexaflow_support_doc.txt`) is included.
Upload it, then ask:

- "What is included in the Professional plan?"
- "How do I connect a new integration?"
- "What happens if I exceed my monthly execution limit?"

---

## API Reference

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/documents` | Upload and index a document |
| GET | `/api/documents` | List indexed documents |
| DELETE | `/api/documents/{doc_id}` | Remove a document |
| POST | `/api/query` | Ask a question |
| GET | `/api/stats` | Session statistics |
| DELETE | `/api/conversation` | Clear conversation memory |
| GET | `/health` | Health check |

---

## Project Structure

```
rag-customer-support/
├── api/
│   └── server.py              # FastAPI backend
├── frontend/
│   ├── index.html             # Single-page UI
│   ├── styles.css             # Styling
│   └── app.js                 # Frontend logic
├── src/
│   ├── document_processor.py  # Loading and chunking
│   ├── vector_store.py        # Pinecone + embeddings
│   ├── generator.py           # Prompt construction and generation
│   ├── memory.py              # Conversation memory
│   ├── validator.py           # Input validation and sanitization
│   └── logger.py              # Safe structured logging
├── sample_docs/
│   └── nexaflow_support_doc.txt
├── config.py                  # Configuration from environment
├── requirements.txt
├── .env.example
├── .gitignore
└── SECURITY.md
```

---

## Design Decisions

**Why RAG over a large context window** — the knowledge base stays updateable
without redeployment, scales beyond any single context window, enables per-answer
source attribution, and gives meaningful confidence scoring through vector similarity.

**Why confidence thresholding** — in a support context a hallucinated answer is
worse than an honest "I don't know." Retrieved passages below the similarity
threshold are discarded, and the agent falls back gracefully.

**Why separate the API from the RAG engine** — the engine in `src/` has no
knowledge of HTTP or the UI. It can be tested in isolation and reused behind any
interface.

---

## Author

**Muhammad Haris Sultan** — AI Automation Engineer

- Portfolio: m-haris-sultan-portfolio.vercel.app
- GitHub: github.com/haris656

## License

MIT
