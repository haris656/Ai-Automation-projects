# Security Notes

Security was treated as a design constraint here, not an afterthought. This is a generative system that takes untrusted input and produces markup that gets rendered in a browser, which creates two real risks: prompt injection into the agents, and unsafe SVG reaching the DOM. Both are handled deliberately.

## 1. No secrets in code

- The API key (Anthropic or OpenAI) is read only from the environment via the config module, which fails fast on startup if it is missing.
- The key is never sent to the browser and never logged.
- The exported repo contains only `.env.example` with placeholders.

## 2. Untrusted input is fenced from instructions

Every agent that touches the user's brief wraps it in delimiter tags (for example `<brief>...</brief>`) and the system prompt explicitly instructs the model to treat tag contents as data, never as instructions. System prompts and user data are kept structurally separate, so a brief that says "ignore your instructions and..." is handled as text to brand, not as a command.

## 3. Input validation and limits

- The brief is length-capped before any agent runs (`MAX_BRIEF_CHARS`).
- A simple in-memory per-IP rate limiter caps requests per minute (`RATE_LIMIT_PER_MINUTE`), so a public deployment cannot be trivially used to burn API credits.
- CORS origins are configurable; lock them to your real domain in production.

## 4. SVG sanitization (the important one)

The Visual Director agent generates a logo as raw SVG. Rendering model-generated SVG directly would be a cross-site scripting risk, since SVG can carry `<script>`, event handlers, `<foreignObject>`, and external references. So the SVG is never trusted:

- A deny-by-default sanitizer runs server-side before the SVG is ever returned.
- Only an allowlist of safe shape, text, and presentation elements and attributes survives.
- `<script>`, `<style>`, `<foreignObject>`, `<image>`, `<use>`, `<a>`, and `<animate>` blocks are removed entirely.
- Any attribute carrying `javascript:`, `url(...)`, or a `data:` payload is dropped.
- Namespaced and event-handler attributes (`onclick`, `xlink:href`, etc.) are stripped.
- If nothing safe remains, the frontend falls back to a generated monogram instead.

The sanitizer is tested against script injection, event handlers, foreign objects, external image references, and javascript-url payloads.

## 5. Never trusting model output for structure

Beyond SVG, all agent JSON is parsed defensively (code fences stripped, outermost braces extracted as a fallback) and every field is shape-checked and length-capped with sensible fallbacks. A malformed model response degrades gracefully rather than crashing or rendering garbage.

## 6. Safe failure

- Each agent call retries with backoff on transient API errors.
- If the Critic agent itself fails, the kit passes through rather than blocking.
- Server errors return a generic message to the client; internal details are never leaked in the response.

## Reporting

If you find a security issue in this sample project, please open an issue or contact the maintainer directly rather than disclosing it publicly.
