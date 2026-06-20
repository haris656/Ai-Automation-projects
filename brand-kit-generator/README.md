# Brand Kit Studio

A multi-agent generative system that turns a short business brief into a complete brand identity: name, tagline, voice, color palette, font pairing, brand copy, and a real logo rendered as SVG. A team of AI agents does the work, and a critic agent reviews the result and sends weak pieces back for another pass.

Built with a FastAPI backend (plain-Python agent orchestration, no framework magic) and a custom frontend that renders the finished kit like a real brand guidelines page. Works with either Claude or OpenAI.

This is a portfolio project. It is built to read clearly and run cleanly. You add your own API key.

---

## What makes it interesting

Most "AI generates X" demos are a single prompt. This one is a coordinated team where agents build on each other and one agent judges the others' work:

1. **Strategist** turns the brief into a brand direction (personality, tone, audience, visual direction).
2. **Namer** produces a name and tagline grounded in that strategy.
3. **Copywriter** writes the one-liner, about paragraph, value props, and sample posts in the brand voice.
4. **Visual Director** designs the palette and font pairing, and generates the logo as SVG.
5. **Critic** reviews the assembled kit against the strategy and flags any weak pieces.
6. **Refiner** regenerates only the flagged pieces, using the critic's feedback. One bounded pass.

The critique-and-refine loop is the point. It is what separates this from four disconnected generations.

---

## Architecture

```
Brief
  -> Strategist        (sets the direction every other agent uses)
  -> Namer             (name + tagline)
  -> Copywriter        (one-liner, about, value props, posts)
  -> Visual Director   (palette, fonts, logo SVG  ->  sanitized server-side)
  -> assemble draft
  -> Critic            (pass/fail verdict per piece, with feedback)
  -> Refiner           (regenerate only failed pieces, then stop)
  -> final kit

The backend streams progress events (newline-delimited JSON) so the UI
shows each agent working live, then renders the finished kit.
```

### Project structure

```
brand-kit-generator/
  README.md
  SECURITY.md
  .env.example
  .gitignore
  requirements.txt
  preview.html                 Static mockup with sample data (open in a browser, no setup)
  backend/
    config.py                  Env-only config, fails fast, picks the provider
    llm.py                     One client for both Anthropic and OpenAI, retries, safe JSON
    orchestrator.py            The multi-agent spine (plain Python)
    server.py                  FastAPI: validation, rate limiting, streaming endpoint
    agents/
      strategist.py
      namer.py
      copywriter.py
      visual_director.py       Includes the SVG sanitizer
      critic.py
  frontend/
    index.html                 Single-page UI, renders the kit and the live agent panel
```

---

## Quick look without running anything

Open `preview.html` in any browser. It renders a finished brand kit with sample data so you can see the output design immediately, no backend or API key required.

---

## Setup

### 1. Install dependencies

```bash
cd brand-kit-generator
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```bash
# choose your provider
LLM_PROVIDER=anthropic            # or: openai

# set the key for the provider you chose
ANTHROPIC_API_KEY=sk-ant-...      # if anthropic
OPENAI_API_KEY=sk-...             # if openai
```

Models default sensibly per provider (`claude-sonnet-4-6` for Anthropic, `gpt-4o` for OpenAI). Override with `MODEL=` if you want a specific one.

### 3. Run

```bash
uvicorn backend.server:app --reload --port 8000
```

Open `http://localhost:8000`, type a business description, and watch the team build the kit.

---

## Switching providers

The provider is a single env var. The same agents, prompts, and orchestration run against either API; only the request and response shapes differ, and that difference is isolated in `backend/llm.py`. To switch:

```bash
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

No code changes.

---

## Security

See `SECURITY.md` for the full reasoning. In short:

- API key from environment only, never in code, never logged.
- User input is fenced from instructions to resist prompt injection.
- Brief is length-capped; requests are rate-limited per IP.
- The model-generated logo SVG is sanitized server-side with a deny-by-default allowlist before it ever reaches the browser, stripping scripts, handlers, foreign objects, and external references. Tested against common injection vectors.
- All model output is parsed defensively and degrades gracefully.

---

## Notes and limitations

- Generation makes several sequential model calls plus a possible refine pass, so a full run takes roughly a minute depending on the provider and model.
- The rate limiter is in-memory and per-process; for a real multi-instance deployment you would move it to a shared store.
- Logo quality depends on the model. The sanitizer guarantees safety, not artistry; if the model returns nothing usable, the UI falls back to a clean monogram.
- This is a demonstration of multi-agent orchestration and safe generative output, adapt the prompts and the agent set to your own use case.
