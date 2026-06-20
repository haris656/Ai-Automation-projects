# Security Notes

This project was built with security treated as a first-class concern, not an afterthought. This document explains the decisions so a reviewer can see the reasoning.

## 1. Webhook authentication (HMAC-SHA256)

The intake webhook is public by nature, so it cannot rely on network trust. Every request must carry an `X-Signature` header containing the hex HMAC-SHA256 of the raw request body, computed with a shared secret.

- The secret lives in an environment variable (`N8N_LEAD_HMAC_SECRET`), never in a node and never in the repo.
- The comparison uses `crypto.timingSafeEqual`, a constant-time compare, to avoid timing side channels.
- If a secret is configured and the signature is missing or wrong, the request is rejected with `401` before any downstream logic or any LLM call runs.

This means an attacker who finds the webhook URL still cannot drive the pipeline or burn your API credits without the secret.

## 2. Input validation

Before anything is processed, the payload is validated:

- Required fields (`name`, `email`, `message`) must be present and non-empty.
- `email` must match a basic format check.
- Anything invalid is rejected with a clean `400` and a machine-readable reason.

## 3. Input sanitization and prompt-injection resistance

User text is untrusted, especially because it is later passed to an LLM:

- Control characters are stripped, whitespace is collapsed, and every field length is capped.
- Capping the message length also limits the surface for prompt-injection attempts.
- In the LLM call, the user-supplied content is wrapped in `<lead>` delimiters and the system prompt explicitly instructs the model to treat everything inside those tags as data, never as instructions. System instructions and user data are kept structurally separate.

## 4. Never trusting the model output

The model's response is parsed defensively:

- Accidental code fences are stripped before `JSON.parse`.
- The parsed object is shape-validated; the score is coerced to a number and clamped to `0..100`; the tier is validated against an allow-list and re-derived from the score if the model returns something invalid.
- If parsing fails entirely, the lead is routed to manual review rather than dropped or mis-handled.

## 5. No secrets in code or logs

- All API keys (Anthropic, Slack, Google, SMTP) are stored as n8n credentials and referenced by the node, never hardcoded.
- The exported workflow JSON in this repo contains empty credential references only.
- The audit log writes lead and scoring data for traceability but deliberately excludes secrets, keys, and the signature.

## 6. Least privilege and safe failure

- Each external call (Anthropic, Slack, Email, Sheets) has retries with backoff and an error output.
- A failure in delivery or logging never silently drops a lead: it is routed to a failure alert while the rest of the pipeline continues.

## Reporting

If you find a security issue in this sample project, please open an issue or contact the maintainer directly rather than disclosing it publicly.
