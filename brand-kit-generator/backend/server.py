"""
FastAPI server for the Brand Kit Generator.

Exposes a single streaming endpoint that runs the agent team and emits progress
events as newline-delimited JSON, so the frontend can show each agent working.

Security:
- Input is validated and length-capped before any agent runs.
- A simple in-memory rate limiter caps requests per client IP.
- The API key is never sent to the client and never logged.
- CORS origins are configurable; default is permissive only for local dev.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import Settings
from .llm import LLMClient
from .orchestrator import generate

settings = Settings.load()
llm = LLMClient(
    provider=settings.provider,
    api_key=settings.api_key,
    model=settings.model,
)

app = FastAPI(title="Brand Kit Generator", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allowed_origins),
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# ---- naive in-memory rate limiter (per IP, per minute) ----
_hits: dict[str, deque] = defaultdict(deque)


def _rate_ok(client_ip: str) -> bool:
    now = time.time()
    window = _hits[client_ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= settings.rate_limit_per_minute:
        return False
    window.append(now)
    return True


class BriefRequest(BaseModel):
    business: str = Field(..., min_length=2)
    audience: str = Field(default="", max_length=400)
    vibe: str = Field(default="", max_length=400)


def _validate_brief(req: BriefRequest) -> tuple[bool, str]:
    text = req.business.strip()
    if len(text) < 2:
        return False, "Tell us a little about the business."
    if len(text) > settings.max_brief_chars:
        return False, "That brief is too long. Keep it under a couple of paragraphs."
    return True, ""


def _compose_brief(req: BriefRequest) -> str:
    parts = [f"Business: {req.business.strip()}"]
    if req.audience.strip():
        parts.append(f"Audience: {req.audience.strip()}")
    if req.vibe.strip():
        parts.append(f"Desired vibe: {req.vibe.strip()}")
    return "\n".join(parts)[: settings.max_brief_chars]


@app.post("/api/generate")
async def api_generate(req: BriefRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_ok(client_ip):
        return JSONResponse(
            status_code=429,
            content={"error": "Too many requests. Wait a minute and try again."},
        )

    ok, msg = _validate_brief(req)
    if not ok:
        return JSONResponse(status_code=400, content={"error": msg})

    brief = _compose_brief(req)

    def stream():
        try:
            for event in generate(llm, brief):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # never leak internals to the client
            yield json.dumps(
                {"stage": "error", "status": "error", "detail": "Generation failed. Please try again."}
            ) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ---- serve the frontend ----
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")
